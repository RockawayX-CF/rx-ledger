#!/usr/bin/env python3
"""RockawayX liquid book ledger: serves the page and stores its documents in SQLite or Postgres.

Loads a local .env if present. Binds to RX_HOST (meant to be the VPN address, e.g. the Tailscale IP),
so nothing is reachable from the public internet. No login: the VPN is the access control.
Storage is SQLite in RX_DATA_DIR unless RX_DATABASE_URL points at a Postgres database.

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
_store = None


def configure():
    global HOST, PORT, DATA_DIR, EXTRA_HOSTS, DATABASE_URL

    HOST = os.environ.get("RX_HOST", "127.0.0.1")
    PORT = int(os.environ.get("RX_PORT", "8787"))
    DATA_DIR = Path(os.environ.get("RX_DATA_DIR", "./data"))
    # e.g. postgresql://rx_ledger@10.137.203.1:5432/rx_ledger ; empty means SQLite in DATA_DIR
    DATABASE_URL = os.environ.get("RX_DATABASE_URL", "").strip()
    if DATABASE_URL and not DATABASE_URL.startswith(("postgresql://", "postgres://")):
        raise ValueError("RX_DATABASE_URL must start with postgresql://")
    # Extra hostnames the page may be opened under (e.g. the Tailscale MagicDNS name), comma-separated.
    EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("RX_ALLOWED_HOSTS", "").split(",") if h.strip()}


class SqliteStore:
    """Default: one SQLite file in RX_DATA_DIR."""
    kind = "sqlite"

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (coll, id))")
        self.conn.commit()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def rows(self):
        return [(c, i, json.loads(d)) for c, i, d in self.conn.execute("SELECT coll, id, data FROM docs")]

    def get(self, coll, doc_id):
        row = self.conn.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, coll, doc_id, body):
        self.conn.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", (coll, doc_id, json.dumps(body), time.time()))
        self.conn.commit()

    def delete(self, coll, doc_id):
        self.conn.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, doc_id))
        self.conn.commit()

    def backup(self, bdir, stem):
        target = bdir / (stem + ".db")
        dst = sqlite3.connect(target)
        self.conn.backup(dst)
        dst.close()
        return target


class PostgresStore:
    """Used when RX_DATABASE_URL is set. The password comes from ~/.pgpass or PGPASSWORD, not the URL."""
    kind = "postgres"

    def __init__(self, url):
        import psycopg  # only needed for Postgres deployments
        self.pg, self.url, self.conn = psycopg, url, None
        self._run(lambda c: c.execute(
            "CREATE TABLE IF NOT EXISTS docs (coll text NOT NULL, id text NOT NULL, data jsonb NOT NULL, "
            "updated timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (coll, id))"))

    def _run(self, fn):
        # One connection, used under _db_lock. Reconnect once if the database restarted or the link dropped;
        # every statement here is safe to repeat.
        for attempt in (1, 2):
            try:
                if self.conn is None or self.conn.closed:
                    self.conn = self.pg.connect(self.url, autocommit=True, connect_timeout=10, application_name="rx-ledger")
                return fn(self.conn)
            except self.pg.OperationalError:
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                self.conn = None
                if attempt == 2:
                    raise
                log.bind(event="database_reconnect").warning("Lost the Postgres connection, reconnecting")

    def count(self):
        return self._run(lambda c: c.execute("SELECT COUNT(*) FROM docs").fetchone()[0])

    def rows(self):
        return self._run(lambda c: c.execute("SELECT coll, id, data FROM docs").fetchall())

    def get(self, coll, doc_id):
        row = self._run(lambda c: c.execute("SELECT data FROM docs WHERE coll=%s AND id=%s", (coll, doc_id)).fetchone())
        return row[0] if row else None

    def put(self, coll, doc_id, body):
        self._run(lambda c: c.execute(
            "INSERT INTO docs (coll, id, data, updated) VALUES (%s, %s, %s::jsonb, now()) "
            "ON CONFLICT (coll, id) DO UPDATE SET data = EXCLUDED.data, updated = now()",
            (coll, doc_id, json.dumps(body))))

    def delete(self, coll, doc_id):
        self._run(lambda c: c.execute("DELETE FROM docs WHERE coll=%s AND id=%s", (coll, doc_id)))

    def backup(self, bdir, stem):
        # A JSON snapshot of every document, readable without Postgres; tools/load-postgres.py loads it back.
        target = bdir / (stem + ".json")
        out = {}
        for coll, doc_id, data in self.rows():
            out.setdefault(coll, {})[doc_id] = data
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1, sort_keys=True))
        tmp.replace(target)
        return target


def open_database():
    global _store

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_URL:
        _store = PostgresStore(DATABASE_URL)
    else:
        _store = SqliteStore(DATA_DIR / "ledger.db")
    log.bind(event="database_opened", backend=_store.kind, document_count=_store.count()).info(
        "Ledger storage: {}", _store.kind)


def import_seed():
    """First start only: load seed.json if the database is empty."""
    seed = APP_DIR / "seed.json"
    with _db_lock:
        if _store.count() or not seed.exists():
            return
        data = json.loads(seed.read_text())
        for coll, docs in data.items():
            if coll in COLLECTIONS:
                for doc_id, body in docs.items():
                    _store.put(coll, doc_id, body)
        count = sum(len(docs) for coll, docs in data.items() if coll in COLLECTIONS)
        log.bind(event="seed_imported", document_count=count).info("Imported seed.json: {} documents", count)


def all_docs():
    out = {c: {} for c in COLLECTIONS}
    with _db_lock:
        for coll, doc_id, data in _store.rows():
            out.setdefault(coll, {})[doc_id] = data
    return out


def write_doc(coll, doc_id, body, merge=False):
    with _db_lock:
        if merge:
            current = _store.get(coll, doc_id)
            if current is None:
                return None
            body = {**current, **body}
        _store.put(coll, doc_id, body)
    broadcast({"coll": coll, "id": doc_id, "data": body})
    return body


def delete_doc(coll, doc_id):
    with _db_lock:
        _store.delete(coll, doc_id)
    broadcast({"coll": coll, "id": doc_id, "data": None})


# ---- live updates ----
_subscribers = set()
_sub_lock = threading.Lock()


def broadcast(event):
    msg = json.dumps(event)
    with _sub_lock:
        for q in list(_subscribers):
            q.put(msg)


# ---- weekly backups (ISO week, server local time); every copy is kept ----
# SQLite: a copy of ledger.db. Postgres: a JSON snapshot of every document.
def backup_loop():
    try:
        bdir = DATA_DIR / "backups"
        bdir.mkdir(exist_ok=True)
        while True:
            stem = time.strftime("ledger-%G-W%V")
            if not any((bdir / (stem + ext)).exists() for ext in (".db", ".json")):
                with _db_lock:
                    target = _store.backup(bdir, stem)
                log.bind(event="backup_completed", backup_file=target.name).info("Weekly ledger backup completed")
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
