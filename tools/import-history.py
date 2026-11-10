#!/usr/bin/env python3
"""One-off: load NAV + benchmark history (a JSON file kept outside git) into the running ledger.

    python3 tools/import-history.py ~/perf-history.json

Each month becomes settings/perf-YYYY-MM. Values already typed in the page win over the file,
so it is safe to run more than once. Reads host and port from ~/.local/share/rx-ledger/env.
"""
import json, sys, urllib.request
from pathlib import Path

env = dict(l.split("=", 1) for l in (Path.home() / ".local/share/rx-ledger/env").read_text().split() if "=" in l)
base = f"http://{env.get('RX_HOST', '127.0.0.1')}:{env.get('RX_PORT', '8787')}"
history = json.loads(Path(sys.argv[1]).read_text())
current = json.load(urllib.request.urlopen(base + "/api/data"))["settings"]

n = 0
for ym, vals in sorted(history.items()):
    doc = {**vals, **current.get("perf-" + ym, {})}
    if doc == current.get("perf-" + ym):
        continue
    req = urllib.request.Request(f"{base}/api/doc/settings/perf-{ym}", data=json.dumps(doc).encode(),
                                 method="PUT", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    n += 1
print(f"Imported {n} of {len(history)} months into {base}")
