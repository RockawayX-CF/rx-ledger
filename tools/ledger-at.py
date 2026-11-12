#!/usr/bin/env python3
"""Look back in the ledger's change history (Postgres only).

    python3 tools/ledger-at.py "2026-11-30 18:00"                 # summary of the ledger at that moment
    python3 tools/ledger-at.py "2026-11-30 18:00" --out at.json   # save it; restore with load-postgres.py at.json --replace
    python3 tools/ledger-at.py --changes positions/p01-bitfinex   # every recorded version of one record
    python3 tools/ledger-at.py --since "2026-11-25"               # every change since a moment

Times without a zone are read in this machine's local time zone. Reads RX_DATABASE_URL like load-postgres.py.
"""
import argparse, json, os, sys
from datetime import datetime
from pathlib import Path

import psycopg


def database_url():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists() and "RX_DATABASE_URL" not in os.environ:
        for line in env_file.read_text().splitlines():
            if line.startswith("RX_DATABASE_URL="):
                os.environ["RX_DATABASE_URL"] = line.split("=", 1)[1].strip().strip("'\"")
    url = os.environ.get("RX_DATABASE_URL", "").strip()
    if not url:
        sys.exit("RX_DATABASE_URL is not set")
    return url


def moment(text):
    ts = datetime.fromisoformat(text)
    return ts if ts.tzinfo else ts.astimezone()   # naive -> local time zone


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("at", nargs="?", type=moment, help="moment to rebuild, e.g. '2026-11-30 18:00'")
    ap.add_argument("--out", type=Path, help="write the ledger at that moment as JSON")
    ap.add_argument("--changes", metavar="COLL/ID", help="list every recorded version of one record")
    ap.add_argument("--since", type=moment, help="list every change since this moment")
    args = ap.parse_args()
    if sum(x is not None for x in (args.at, args.changes, args.since)) != 1:
        ap.error("give a moment, --changes COLL/ID or --since MOMENT")

    with psycopg.connect(database_url(), application_name="rx-ledger-history") as con:
        first = con.execute("SELECT min(changed_at) FROM docs_history").fetchone()[0]
        if args.changes:
            coll, _, doc_id = args.changes.partition("/")
            prev = None
            for when, op, data, src in con.execute(
                    "SELECT changed_at, op, data, source FROM docs_history WHERE coll=%s AND id=%s ORDER BY change_id",
                    (coll, doc_id)):
                if data is None:
                    what = ""
                elif prev is None:
                    what = json.dumps(data)[:200]
                else:   # only the fields that changed, old -> new
                    what = "; ".join(f"{k}: {json.dumps(prev.get(k))} -> {json.dumps(data.get(k))}"
                                     for k in sorted(set(prev) | set(data)) if k != "updatedAt" and prev.get(k) != data.get(k))
                print(f"{when.astimezone():%Y-%m-%d %H:%M:%S}  {op:8}  {src or '':18}  {what}")
                prev = data
            return
        if args.since:
            for when, op, coll, doc_id, src in con.execute(
                    "SELECT changed_at, op, coll, id, source FROM docs_history WHERE changed_at >= %s AND op <> 'baseline' "
                    "ORDER BY change_id", (args.since,)):
                print(f"{when.astimezone():%Y-%m-%d %H:%M:%S}  {op:6}  {coll}/{doc_id}  ({src or ''})")
            return
        if first is None or args.at < first:
            sys.exit(f"History starts at {first.astimezone():%Y-%m-%d %H:%M:%S %Z}" if first else "No history recorded yet")
        out = {}
        for coll, doc_id, data in con.execute("SELECT coll, id, data FROM ledger_at(%s)", (args.at,)):
            out.setdefault(coll, {})[doc_id] = data
    total = sum((d.get("value") or 0) for d in out.get("positions", {}).values() if isinstance(d.get("value"), (int, float)))
    loans = sum((d.get("amount") or 0) for d in out.get("loans", {}).values() if isinstance(d.get("amount"), (int, float)))
    print(f"Ledger at {args.at:%Y-%m-%d %H:%M %Z}: " + ", ".join(f"{c} {len(v)}" for c, v in sorted(out.items())))
    print(f"  positions value {total:,.0f}   loans {loans:,.0f}")
    if args.out:
        args.out.write_text(json.dumps(out, indent=1, sort_keys=True))
        print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()
