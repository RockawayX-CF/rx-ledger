#!/usr/bin/env bash
# Installs the ledger under your own account (no sudo).
#   SCRIPT_OWNER=<owner> RX_HOST=10.137.203.5 RX_ALLOWED_HOSTS=traderplayground ./install-user.sh
# Runs as a systemd user service when possible; otherwise a cron job keeps it running.
# Reinstalling keeps your data: seed.json only loads into an empty database.
set -euo pipefail

HOST="${RX_HOST:?Set RX_HOST to the address to listen on, e.g. RX_HOST=10.137.203.5}"
PORT="${RX_PORT:-8787}"
EXTRA="${RX_ALLOWED_HOSTS:-}"
OWNER="${SCRIPT_OWNER:?Set SCRIPT_OWNER to the lower-case owner of this deployment}"
INSTANCE="${LOG_INSTANCE:-main}"
for value in "$OWNER" "$INSTANCE"; do
  [[ "$value" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "Invalid SCRIPT_OWNER or LOG_INSTANCE" >&2; exit 1; }
done
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/rx-ledger"
DATA="$APP/data"
PY="$APP/.venv/bin/python"

mkdir -p "$APP" "$DATA"
chmod 700 "$APP" "$DATA"
python3 -m venv "$APP/.venv"
"$PY" -m pip install -r "$SRC/requirements.txt"
install -m 644 "$SRC/server.py" "$SRC/logger_utils.py" "$SRC/index.html" "$APP/"
[ -f "$SRC/seed.json" ] && install -m 600 "$SRC/seed.json" "$APP/seed.json"
cat > "$APP/env" <<ENV
RX_HOST=$HOST
RX_PORT=$PORT
RX_DATA_DIR=$DATA
RX_ALLOWED_HOSTS=$EXTRA
SCRIPT_OWNER=$OWNER
LOG_INSTANCE=$INSTANCE
ENV
cat > "$APP/run.sh" <<RUN
#!/usr/bin/env bash
set -a; . "$APP/env"; set +a
export LOG_MODE="\${LOG_MODE:-jsonl}" LOG_CONSOLE="\${LOG_CONSOLE:-false}"
exec "$PY" "$APP/server.py"
RUN
chmod 700 "$APP/run.sh"

started=""
# --- preferred: systemd user service, kept alive after logout by "linger" ---
if systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/rx-ledger.service" <<UNIT
[Unit]
Description=RockawayX liquid book ledger

[Service]
ExecStart=$APP/run.sh
Environment=LOG_MODE=journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=rx-ledger.server.$INSTANCE
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable rx-ledger >/dev/null 2>&1
  systemctl --user restart rx-ledger
  sleep 1
  if systemctl --user is-active --quiet rx-ledger; then
    loginctl enable-linger "$USER" >/dev/null 2>&1 || true
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ]; then
      started="systemd"
    else
      # without linger the service stops when you log out; fall back to cron
      systemctl --user disable --now rx-ledger >/dev/null 2>&1 || true
    fi
  else
    journalctl --user -u rx-ledger -n 20 --no-pager 2>/dev/null || true
    systemctl --user disable --now rx-ledger >/dev/null 2>&1 || true
  fi
fi

# --- fallback: cron starts it at boot and restarts it within a minute if it stops ---
if [ -z "$started" ]; then
  command -v crontab >/dev/null || { echo "Neither a systemd user service nor cron is available. Ask IT for sudo, or paste this output to Claude."; exit 1; }
  pkill -f "$APP/[s]erver.py" 2>/dev/null || true
  LINE="* * * * * pgrep -f '$APP/[s]erver.py' >/dev/null || nohup '$APP/run.sh' >>'$DATA/server.log' 2>&1 &"
  ( { crontab -l 2>/dev/null | grep -v 'rx-ledger' || true; } ; echo "@reboot nohup '$APP/run.sh' >>'$DATA/server.log' 2>&1 & # rx-ledger"; echo "$LINE # rx-ledger" ) | crontab -
  nohup "$APP/run.sh" >>"$DATA/server.log" 2>&1 &
  sleep 1.5
  pgrep -f "$APP/[s]erver.py" >/dev/null || { tail -n 20 "$DATA/server.log"; exit 1; }
  started="cron"
fi

echo
echo "Ledger is running on http://$HOST:$PORT  (kept alive by $started)"
echo "Data: $DATA/ledger.db   Daily backups: $DATA/backups (last 30 kept)"
if [ "$started" = "systemd" ]; then
  echo "Logs: journalctl --user -u rx-ledger -f    Restart: systemctl --user restart rx-ledger"
else
  echo "Logs: /var/log/trading/$USER/rx-ledger/server/$INSTANCE/    Stderr fallback: $DATA/server.log"
  echo "Restart: pkill -f $APP/[s]erver.py  (cron starts it again within a minute)"
fi
