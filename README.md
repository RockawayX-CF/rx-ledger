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

- `server.py` runs on Python 3.8 or newer and listens on one address and port.
  It uses the UnderQuant Loguru setup. Installed services supply settings through the environment.
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
| `requirements.txt` | Logging and local `.env` dependencies |
| `install-user.sh` | Install without sudo: systemd user service, or cron keep-alive |
| `install.sh` | Install as root with a systemd service |
| `tools/import-history.py` | One-off load of NAV + benchmark history into the database |

## Run in screen

Set up the virtual environment once from the clone.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If `install-user.sh` already set up the ledger under this account, copy its configuration
to `.env` to keep the same VPN address and database.

```sh
[ -f .env ] || cp ~/.local/share/rx-ledger/env .env
```

For a fresh setup, copy `.env.example` to `.env` and edit it. Set `RX_HOST` to the
server's VPN IP for remote access. To reuse a database from another installation,
set `RX_DATA_DIR` to the directory containing `ledger.db`. The server loads `.env`
from the script's directory. Existing environment variables take precedence.
Set `SCRIPT_OWNER` and the screen logging settings below before launch.

Stop any previous instance and its supervisor before starting the ledger in the attached screen.

```sh
. .venv/bin/activate
python3 server.py
```

Press **Ctrl+A, then D** to detach.

## Logging

This uses the UnderQuant logger from `underquant_agent/agent_controller/logger_utils.py`.
The repository and service are `rx-ledger`, and the runtime is `server`.
Set `SCRIPT_OWNER` to the lower-case person or team responsible for the deployment.
Local runs use the UnderQuant human-readable console by default.

For the `honza` screen deployment on `traderplayground`, set these in `.env`:

```dotenv
LOG_MODE=jsonl
LOG_INSTANCE=playground
LOG_CONSOLE=true
```

Use a distinct `LOG_INSTANCE` for each concurrent ledger and a screen name such as
`rx_ledger_playground`. Alloy matches
`/var/log/trading/honza/rx-ledger/server/playground/<run_id>.<segment>.jsonl`.
The installers persist `SCRIPT_OWNER` from the shell, use the journal under systemd
with identifier `rx-ledger.server.<instance>`, and use JSONL for the cron fallback.

Events include the PID, run ID, and screen name when available. The backup worker
emits a backup success or heartbeat about once an hour. Routine HTTP requests stay
quiet. The `docs` table in `ledger.db` is the source of truth for ledger records.

## Install (company server, no sudo)

```bash
git clone git@github.com:<you>/rx-ledger.git ~/rx-ledger && cd ~/rx-ledger
SCRIPT_OWNER=your-owner RX_HOST=10.137.203.5 RX_ALLOWED_HOSTS=traderplayground ./install-user.sh
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
cp index.html ~/.local/share/rx-ledger/index.html  # page only, takes effect on reload
```

For an installed copy, rerun the same installer with its existing configuration
when Python files or dependencies change. For a screen launch from the clone,
update the virtual environment from `requirements.txt` and restart the server.

## Change the page

Edit `page/ledger.html`, run `python3 build.py`, commit both `page/` and `index.html`.
To try it locally against a throwaway database:

```bash
SCRIPT_OWNER=your-owner RX_HOST=127.0.0.1 RX_DATA_DIR=/tmp/rx python3 server.py
```

## Day to day

| Task | Command |
|---|---|
| Is it running? | `pgrep -af '[s]erver.py'` |
| Logs | Read the journal for a service or the instance’s JSONL files for screen and cron |
| Restart | `pkill -f '.local/share/rx-ledger/[s]erver.py'` (cron starts it within a minute) |
| Copy of the database | `cp ~/.local/share/rx-ledger/data/ledger.db ~/ledger-$(date +%F).db` |
| Restore a backup | stop it as above, then `cp data/backups/ledger-YYYYMMDD.db data/ledger.db && rm -f data/ledger.db-wal data/ledger.db-shm` (in `~/.local/share/rx-ledger`); cron restarts it |

Kept per browser only (not on the server): scenarios, loop calculators, whether the time series is open.

## API

`GET /api/data` · `PUT|PATCH|DELETE /api/doc/<positions|loans|plans|settings>/<id>` · `GET /api/events` (SSE)
