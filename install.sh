#!/usr/bin/env bash
# Installs the RockawayX ledger as a systemd service that listens only on the VPN address.
# Set SCRIPT_OWNER to the person or team responsible for the deployment.
# Options (environment):
#   RX_HOST=100.x.y.z   bind to this address instead of the detected Tailscale IP
#                       (use 127.0.0.1 when a reverse proxy such as Caddy sits in front)
#   RX_ALLOWED_HOSTS=a,b hostnames the page is opened under (e.g. ledger.1-2-3-4.sslip.io behind Caddy)
#   RX_PORT=8787        port to listen on
set -euo pipefail

APP=/opt/rx-ledger
DATA=/var/lib/rx-ledger
SVC=rx-ledger
PORT="${RX_PORT:-8787}"
OWNER="${SCRIPT_OWNER:?Set SCRIPT_OWNER to the lower-case owner of this deployment}"
INSTANCE="${LOG_INSTANCE:-main}"
for value in "$OWNER" "$INSTANCE"; do
  [[ "$value" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "Invalid SCRIPT_OWNER or LOG_INSTANCE" >&2; exit 1; }
done
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is missing: apt install -y python3"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "Python 3.8 or newer is needed"; exit 1; }

# --- which address to bind -------------------------------------------------
HOST="${RX_HOST:-}"
EXTRA="${RX_ALLOWED_HOSTS:-}"
if [ -z "$HOST" ]; then
  if ! command -v tailscale >/dev/null; then
    cat <<'MSG'
Tailscale is not installed on this server. Install it and join your tailnet, then run this again:

    curl -fsSL https://tailscale.com/install.sh | sh
    tailscale up

(To use another VPN instead, run:  sudo RX_HOST=<the server's VPN IP> ./install.sh)
MSG
    exit 1
  fi
  HOST="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  [ -n "$HOST" ] || { echo "Tailscale is installed but not connected. Run: tailscale up"; exit 1; }
  # MagicDNS name, so the page also opens as http://<name>:PORT
  EXTRA="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin)["Self"]["DNSName"].rstrip("."); print(d+","+d.split(".")[0])' 2>/dev/null || true)"
fi
echo "Binding to $HOST:$PORT"

# --- files and user --------------------------------------------------------
id -u rxledger >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin rxledger
install -d -m 755 "$APP"
install -d -m 750 -o rxledger -g rxledger "$DATA"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install -r "$SRC/requirements.txt"
install -m 644 "$SRC/server.py" "$SRC/logger_utils.py" "$SRC/index.html" "$APP/"
# seed.json is imported only into an empty database, so reinstalling never overwrites your edits
[ -f "$SRC/seed.json" ] && install -m 640 -o root -g rxledger "$SRC/seed.json" "$APP/seed.json"

# --- service ---------------------------------------------------------------
cat > /etc/systemd/system/$SVC.service <<UNIT
[Unit]
Description=RockawayX liquid book ledger (VPN only)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=rxledger
Group=rxledger
Environment=RX_HOST=$HOST
Environment=RX_PORT=$PORT
Environment=RX_DATA_DIR=$DATA
Environment=RX_ALLOWED_HOSTS=$EXTRA
Environment=SCRIPT_OWNER=$OWNER
Environment=LOG_MODE=journal
Environment=LOG_INSTANCE=$INSTANCE
StandardOutput=journal
StandardError=journal
SyslogIdentifier=rx-ledger.server.$INSTANCE
ExecStart=$APP/.venv/bin/python $APP/server.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now $SVC
systemctl restart $SVC
sleep 1
systemctl is-active --quiet $SVC || { journalctl -u $SVC -n 30 --no-pager; exit 1; }

echo
echo "Ledger is running on http://$HOST:$PORT"
[ -n "$EXTRA" ] && echo "Allowed hostnames: $EXTRA"
echo
echo "Data: $DATA/ledger.db   Daily backups: $DATA/backups (last 30 kept)"
echo "Logs: journalctl -u $SVC -f"
