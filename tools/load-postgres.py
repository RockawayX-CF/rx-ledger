#!/usr/bin/env python3
"""Copy ledger documents into the Postgres database named by RX_DATABASE_URL.

    python3 tools/load-postgres.py data/ledger.db                     # move from SQLite
    python3 tools/load-postgres.py data/backups/ledger-2026-W46.json  # restore a weekly JSON snapshot
    python3 tools/load-postgres.py SOURCE --replace                   # empty the table first
    python3 tools/load-postgres.py --snapshot 2026-W46 --replace      # restore a week from the snapshots table
    python3 tools/load-postgres.py --list-snapshots                   # show stored weekly snapshots

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
    ap.add_argument("source", type=Path, nargs="?", help="ledger.db or a ledger-*.json snapshot")
    ap.add_argument("--snapshot", metavar="WEEK", help="restore this week (e.g. 2026-W46) from the snapshots table")
    ap.add_argument("--list-snapshots", action="store_true", help="list stored weekly snapshots and exit")
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

    if args.list_snapshots:
        with psycopg.connect(url, application_name="rx-ledger-load") as con:
            for week, taken, n in con.execute("SELECT week, taken_at, doc_count FROM snapshots ORDER BY week"):
                print(f"{week}  {taken:%Y-%m-%d %H:%M %Z}  {n} documents")
        return
    if bool(args.source) == bool(args.snapshot):
        sys.exit("Give either a SOURCE file or --snapshot WEEK")
    if args.snapshot:
        with psycopg.connect(url, application_name="rx-ledger-load") as con:
            row = con.execute("SELECT data FROM snapshots WHERE week = %s", (args.snapshot,)).fetchone()
        if not row:
            sys.exit(f"No snapshot for {args.snapshot}; see --list-snapshots")
        source_name = "snapshot " + args.snapshot
        rows = [(c, i, d) for c, docs in row[0].items() for i, d in docs.items() if c in COLLECTIONS]
    else:
        source_name = args.source.name
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
    print(f"Loaded {len(rows)} documents from {source_name}: {counts}")


if __name__ == "__main__":
    main()
