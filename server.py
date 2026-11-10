#!/usr/bin/env python3
"""RockawayX liquid book ledger: serves the page and stores its documents in SQLite.

Loads a local .env if present. Binds to RX_HOST (meant to be the VPN address, e.g. the Tailscale IP),
so nothing is reachable from the public internet. No login: the VPN is the access control.

API (all JSON):
  GET    /api/data                  every document, {collection: {id: data}}
  PUT    /api/doc/<coll>/<id>       replace a document
  PATCH  /api/doc/<coll>/<id>       merge top-level fields into a document
  DELETE /api/doc/<coll>/<id>       delete a document
  GET    /api/events                server-sent events, one per change: {"coll", "id", "data" | null}
"""
import json, os, queue, re, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from logger_utils import (
    SigtermReceived, emit_logging_initialization_failure, install_sigterm_handler,
    logger as log, resolve_script_owner, restore_sigterm_handler, setup_logger,
    shutdown_logger,
)

APP_DIR = Path(__file__).resolve().parent
REPOSITORY = SERVICE_NAME = "rx-ledger"
RUNTIME = "server"

COLLECTIONS = {"positions", "loans", "settings", "plans"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")
MAX_BODY = 256 * 1024

_db_lock = threading.Lock()


def configure():
    global HOST, PORT, DATA_DIR, EXTRA_HOSTS, BACKUP_KEEP

    HOST = os.environ.get("RX_HOST", "127.0.0.1")
    PORT = int(os.environ.get("RX_PORT", "8787"))
    DATA_DIR = Path(os.environ.get("RX_DATA_DIR", "./data"))
    # Extra hostnames the page may be opened under (e.g. the Tailscale MagicDNS name), comma-separated.
    EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("RX_ALLOWED_HOSTS", "").split(",") if h.strip()}
    BACKUP_KEEP = int(os.environ.get("RX_BACKUP_KEEP", "30"))


def open_database():
    global _db

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(DATA_DIR / "ledger.db", check_same_thread=False)
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
        count = sum(len(docs) for coll, docs in data.items() if coll in COLLECTIONS)
        log.bind(event="seed_imported", document_count=count).info("Imported seed.json: {} documents", count)


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
    try:
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
                log.bind(event="backup_completed").info("Daily ledger backup completed")
            else:
                log.bind(event="process_heartbeat").info("Ledger backup worker is running")
            time.sleep(3600)
    except Exception:
        log.bind(event="backup_failed").exception("Ledger backup worker stopped")


class Handler(BaseHTTPRequestHandler):
    server_version = "rx-ledger"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # Keep routine HTTP requests quiet.
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


class LedgerServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        log.bind(event="request_failed", error_type=type(error).__name__).error("Ledger request failed")


def main() -> int:
    env_file = APP_DIR / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file)

    level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    try:
        owner = resolve_script_owner()
        log.level(level)
    except ValueError as error:
        emit_logging_initialization_failure(
            repository=REPOSITORY, service_name=SERVICE_NAME, runtime=RUNTIME,
            log_level=level, error=error,
        )
        return 2
    try:
        setup_logger(RUNTIME, level, repository=REPOSITORY, service_name=SERVICE_NAME, script_owner=owner)
    except Exception:
        return 2

    previous = install_sigterm_handler()
    try:
        log.bind(event="process_started").info("RX Ledger started")
        try:
            configure()
        except ValueError as error:
            log.bind(event="configuration_invalid", error_type=type(error).__name__).error(
                "Invalid ledger configuration"
            )
            return 2
        open_database()
        import_seed()
        threading.Thread(target=backup_loop, daemon=True).start()
        with LedgerServer((HOST, PORT), Handler) as httpd:
            log.bind(event="server_listening", bind_port=httpd.server_port).info(
                "rx-ledger listening on http://{}:{}", HOST, httpd.server_port
            )
            httpd.serve_forever()
    except SigtermReceived:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.bind(event="process_crashed").exception("RX Ledger failed")
        return 1
    finally:
        log.bind(event="process_stopping").info("RX Ledger stopping")
        shutdown_logger()
        restore_sigterm_handler(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
