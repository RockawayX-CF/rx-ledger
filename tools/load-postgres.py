#!/usr/bin/env python3
"""Copy ledger documents into the Postgres database named by RX_DATABASE_URL.

    python3 tools/load-postgres.py data/ledger.db                     # move from SQLite
    python3 tools/load-postgres.py data/backups/ledger-2026-W46.json  # restore a weekly JSON snapshot
    python3 tools/load-postgres.py SOURCE --replace                   # empty the table first

Stop the ledger server before running it. Without --replace it refuses to write into a table that
already holds documents. Reads RX_DATABASE_URL from the environment or a .env next to the repo;
the password comes from ~/.pgpass or PGPASSWORD.
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path

import psycopg

COLLECTIONS = {"positions", "loans", "settings", "plans"}


def read_source(path):
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        return [(c, i, d) for c, docs in data.items() for i, d in docs.items()]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [(c, i, json.loads(d)) for c, i, d in con.execute("SELECT coll, id, data FROM docs")]
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="ledger.db or a ledger-*.json snapshot")
    ap.add_argument("--replace", action="store_true", help="delete every document in Postgres first")
    args = ap.parse_args()

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists() and "RX_DATABASE_URL" not in os.environ:
        for line in env_file.read_text().splitlines():
            if line.startswith("RX_DATABASE_URL="):
                os.environ["RX_DATABASE_URL"] = line.split("=", 1)[1].strip().strip("'\"")
    url = os.environ.get("RX_DATABASE_URL", "").strip()
    if not url:
        sys.exit("RX_DATABASE_URL is not set")

    rows = [r for r in read_source(args.source) if r[0] in COLLECTIONS]
    with psycopg.connect(url, application_name="rx-ledger-load") as con:
        con.execute("CREATE TABLE IF NOT EXISTS docs (coll text NOT NULL, id text NOT NULL, data jsonb NOT NULL, "
                    "updated timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (coll, id))")
        existing = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        if existing and not args.replace:
            sys.exit(f"Postgres already holds {existing} documents; rerun with --replace to overwrite them")
        if args.replace:
            con.execute("DELETE FROM docs")
        with con.cursor() as cur:
            cur.executemany("INSERT INTO docs (coll, id, data) VALUES (%s, %s, %s::jsonb)",
                            [(c, i, json.dumps(d)) for c, i, d in rows])
        # the connection block commits here, so a failure part-way leaves Postgres unchanged
    counts = {}
    for c, _, _ in rows:
        counts[c] = counts.get(c, 0) + 1
    print(f"Loaded {len(rows)} documents from {args.source.name}: {counts}")


if __name__ == "__main__":
    main()
