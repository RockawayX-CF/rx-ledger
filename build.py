#!/usr/bin/env python3
"""Builds index.html (what the server serves) from page/ledger.html + page/shim.js.

page/ledger.html is the same page as the claude.ai version; shim.js stands in for claude.ai's database
and talks to server.py instead. Run after editing anything in page/:  python3 build.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
page = (ROOT / "page/ledger.html").read_text()
shim = (ROOT / "page/shim.js").read_text()
head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0;font:14px system-ui,-apple-system,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>'
        f'<script>{shim}</script></head><body>')
page = page.replace("The portfolio data loads when this page is opened in Claude while signed in.",
                    "Can’t reach the ledger server. Check your connection and reload.")
# wallet addresses are not kept on the self-hosted copy
for snippet in ["fld('Wallet address(es)','e-wallet',rec.wallet,{area:1}) + ", ", wallet:opt(val('e-wallet'))"]:
    assert page.count(snippet) == 1, snippet
    page = page.replace(snippet, "")
(ROOT / "index.html").write_text(head + page + "</body></html>")
print("wrote index.html")
