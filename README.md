# RockawayX liquid book ledger

A live, shared ledger of the liquid book: positions, loans, looped yields, scenarios, re-investment plans,
a cash calendar, charts, loop calculators, the NAV sheet, benchmark statistics and a notes strip.
One Python file serves one HTML page; edits are saved in SQLite and pushed live to every open tab.

**No fund data is kept in this repository.** Positions, loans, NAV history and notes live only in the
server's database. `seed.json` and `perf-history.json` are git-ignored on purpose.

## How it runs

```
browser ──GET /──────────────► server.py ──► index.html  (re-read on every request)
        ──GET /api/data──────►           ──► data/ledger.db  (SQLite, every record)
        ──PUT/DELETE /api/doc►           ──► writes one record, then tells every open tab
        ◄─GET /api/events (SSE)─          (live updates)
```

- `server.py`: Python standard library only (3.8+), no pip packages. Listens on one address and port.
- All calculations happen in the browser; the server only stores and returns records.
- Collections: `positions`, `loans`, `plans`, `settings`. `settings` also holds the NAV/benchmark months
  (`perf-YYYY-MM`) and notes (`note-<id>`).
- The server answers only requests addressed to its own host name/IP (`RX_HOST`, `RX_ALLOWED_HOSTS`),
  which blocks DNS-rebinding pages. There is no login: keep it on a VPN-only address.
- Daily backups to `data/backups/`, last 30 kept.

## Layout

| Path | What |
|---|---|
| `server.py` | The server |
| `index.html` | Built page that the server serves (commit it after `build.py`) |
| `page/ledger.html` | Page source (same page as the claude.ai version) |
| `page/shim.js` | Small adapter that points the page at `server.py` |
| `build.py` | `page/` → `index.html` (also strips the wallet-address field) |
| `run.sh` | Start the server from the clone with local `.env` settings |
| `install-user.sh` | Install without sudo: systemd user service, or cron keep-alive |
| `install.sh` | Install as root with a systemd service |
| `tools/import-history.py` | One-off load of NAV + benchmark history into the database |

## Run in screen

From the clone, create `.env` once. If `install-user.sh` already set up the ledger
under this account, use its configuration to keep the same VPN address and database:

```sh
[ -f .env ] || cp ~/.local/share/rx-ledger/env .env
```

For a fresh setup, copy `.env.example` to `.env` and edit it. Set `RX_HOST` to the
server's VPN IP for remote access. To reuse a database from another installation,
set `RX_DATA_DIR` to the directory containing `ledger.db`. Use shell assignments in
`.env` and quote paths with spaces.

Stop any previous instance and its supervisor before starting the ledger in the attached screen:

```sh
./run.sh
```

Press **Ctrl+A, then D** to detach.

## Install (company server, no sudo)

```bash
git clone git@github.com:<you>/rx-ledger.git ~/rx-ledger && cd ~/rx-ledger
RX_HOST=10.137.203.5 RX_ALLOWED_HOSTS=traderplayground ./install-user.sh
```

Then open `http://10.137.203.5:8787`. App files and data live in `~/.local/share/rx-ledger/`.
An empty database starts empty; to start from an export put it next to the installer as `seed.json`
first (it only ever loads into an empty database).

NAV and benchmark history (once per database; safe to repeat, values typed in the page win):

```bash
python3 tools/import-history.py ~/perf-history.json
```

## Update

```bash
cd ~/rx-ledger && git pull
cp index.html ~/.local/share/rx-ledger/index.html          # page only: takes effect on reload
# if server.py changed too:
cp server.py ~/.local/share/rx-ledger/ && pkill -f '.local/share/rx-ledger/[s]erver.py'   # cron restarts it within a minute
```

Data is never touched by an update.

## Change the page

Edit `page/ledger.html`, run `python3 build.py`, commit both `page/` and `index.html`.
To try it locally against a throwaway database:

```bash
RX_HOST=127.0.0.1 RX_DATA_DIR=/tmp/rx python3 server.py   # then open http://127.0.0.1:8787
```

## Day to day

| Task | Command |
|---|---|
| Is it running? | `pgrep -af '[s]erver.py'` |
| Logs | `tail -f ~/.local/share/rx-ledger/data/server.log` |
| Restart | `pkill -f '.local/share/rx-ledger/[s]erver.py'` (cron starts it within a minute) |
| Copy of the database | `cp ~/.local/share/rx-ledger/data/ledger.db ~/ledger-$(date +%F).db` |
| Restore a backup | stop it as above, then `cp data/backups/ledger-YYYYMMDD.db data/ledger.db && rm -f data/ledger.db-wal data/ledger.db-shm` (in `~/.local/share/rx-ledger`); cron restarts it |

Kept per browser only (not on the server): scenarios, loop calculators, whether the time series is open.

## API

`GET /api/data` · `PUT|PATCH|DELETE /api/doc/<positions|loans|plans|settings>/<id>` · `GET /api/events` (SSE)
