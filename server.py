#!/usr/bin/env python3
"""RockawayX liquid book ledger: serves the page and stores its documents in SQLite.

Standard library only. Binds to RX_HOST (meant to be the VPN address, e.g. the Tailscale IP),
so nothing is reachable from the public internet. No login: the VPN is the access control.

API (all JSON):
  GET    /api/data                  every document, {collection: {id: data}}
  PUT    /api/doc/<coll>/<id>       replace a document
  PATCH  /api/doc/<coll>/<id>       merge top-level fields into a document
  DELETE /api/doc/<coll>/<id>       delete a document
  GET    /api/events                server-sent events, one per change: {"coll", "id", "data" | null}
"""
import json, os, queue, re, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("RX_HOST", "127.0.0.1")
PORT = int(os.environ.get("RX_PORT", "8787"))
DATA_DIR = Path(os.environ.get("RX_DATA_DIR", "./data"))
APP_DIR = Path(__file__).resolve().parent
# Extra hostnames the page may be opened under (e.g. the Tailscale MagicDNS name), comma-separated.
EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("RX_ALLOWED_HOSTS", "").split(",") if h.strip()}
BACKUP_KEEP = int(os.environ.get("RX_BACKUP_KEEP", "30"))

COLLECTIONS = {"positions", "loans", "settings", "plans"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")
MAX_BODY = 256 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ledger.db"
_db_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (coll, id))")
_db.commit()


def import_seed():
    """First start only: load seed.json if the database is empty."""
    seed = APP_DIR / "seed.json"
    with _db_lock:
        if _db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] or not seed.exists():
            return
        data = json.loads(seed.read_text())
        now = time.time()
        for coll, docs in data.items():
            if coll in COLLECTIONS:
                for doc_id, body in docs.items():
                    _db.execute("INSERT INTO docs VALUES (?,?,?,?)", (coll, doc_id, json.dumps(body), now))
        _db.commit()
        print(f"Imported seed.json: {sum(len(v) for v in data.values())} documents", flush=True)


def all_docs():
    out = {c: {} for c in COLLECTIONS}
    with _db_lock:
        for coll, doc_id, data in _db.execute("SELECT coll, id, data FROM docs"):
            out.setdefault(coll, {})[doc_id] = json.loads(data)
    return out


def get_doc(coll, doc_id):
    row = _db.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, doc_id)).fetchone()
    return json.loads(row[0]) if row else None


def write_doc(coll, doc_id, body, merge=False):
    with _db_lock:
        if merge:
            current = get_doc(coll, doc_id)
            if current is None:
                return None
            body = {**current, **body}
        _db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", (coll, doc_id, json.dumps(body), time.time()))
        _db.commit()
    broadcast({"coll": coll, "id": doc_id, "data": body})
    return body


def delete_doc(coll, doc_id):
    with _db_lock:
        _db.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, doc_id))
        _db.commit()
    broadcast({"coll": coll, "id": doc_id, "data": None})


# ---- live updates ----
_subscribers = set()
_sub_lock = threading.Lock()


def broadcast(event):
    msg = json.dumps(event)
    with _sub_lock:
        for q in list(_subscribers):
            q.put(msg)


# ---- daily backups ----
def backup_loop():
    bdir = DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    while True:
        target = bdir / time.strftime("ledger-%Y%m%d.db")
        if not target.exists():
            with _db_lock:
                dst = sqlite3.connect(target)
                _db.backup(dst)
                dst.close()
            for old in sorted(bdir.glob("ledger-*.db"))[:-BACKUP_KEEP]:
                old.unlink()
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    server_version = "rx-ledger"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: only errors reach the journal
        pass

    # Requests must name this server; blocks DNS-rebinding pages from reaching the API.
    def host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in {HOST.lower(), "localhost", "127.0.0.1"} | EXTRA_HOSTS

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        m = re.fullmatch(r"/api/doc/([^/]+)/([^/]+)", self.path.split("?")[0])
        if not m or m.group(1) not in COLLECTIONS or not ID_RE.match(m.group(2)):
            return None
        return m.group(1), m.group(2)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self):
        if not self.host_ok():
            return self.send_json(403, {"error": "unknown host"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            return self.wfile.write(page)
        if path == "/api/data":
            return self.send_json(200, all_docs())
        if path == "/api/events":
            return self.stream()
        self.send_json(404, {"error": "not found"})

    def stream(self):
        q = queue.Queue()
        with _sub_lock:
            _subscribers.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sub_lock:
                _subscribers.discard(q)
            self.close_connection = True

    def write(self, merge):
        if not self.host_ok():
            return self.send_json(403, {"error": "unknown host"})
        target = self.route()
        if not target:
            return self.send_json(400, {"error": "bad collection or id"})
        body = self.read_body()
        if body is None:
            return self.send_json(400, {"error": "body must be a JSON object under 256 KiB"})
        result = write_doc(*target, body, merge=merge)
        if result is None:
            return self.send_json(404, {"error": "document does not exist"})
        self.send_json(200, {"ok": True})

    def do_PUT(self):
        self.write(merge=False)

    def do_PATCH(self):
        self.write(merge=True)

    def do_DELETE(self):
        if not self.host_ok():
            return self.send_json(403, {"error": "unknown host"})
        target = self.route()
        if not target:
            return self.send_json(400, {"error": "bad collection or id"})
        delete_doc(*target)
        self.send_json(200, {"ok": True})


if __name__ == "__main__":
    import_seed()
    threading.Thread(target=backup_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"rx-ledger listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()
