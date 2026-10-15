#!/usr/bin/env bash
# Installs tradebot + newswatch services and a 10-minute healthcheck timer.
# Run from the project folder:  bash install_services.sh      (or --print to just show the files)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ME="$(whoami)"
PY="$DIR/venv/bin/python"
OUT="$(mktemp -d)"

unit() {  # name, description, script
cat > "$OUT/$1.service" <<UNIT
[Unit]
Description=$2
After=network-online.target ntfy.service
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$DIR
ExecStart=$PY $DIR/$3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
}
unit tradebot "Options trade assistant" tradebot.py
unit newswatch "News scanner" newswatch.py

cat > "$OUT/healthcheck.service" <<UNIT
[Unit]
Description=Trading setup watchdog

[Service]
Type=oneshot
User=$ME
WorkingDirectory=$DIR
ExecStart=$PY $DIR/healthcheck.py
UNIT
cat > "$OUT/healthcheck.timer" <<UNIT
[Unit]
Description=Run the watchdog every 10 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT

if [ "$1" = "--print" ]; then
  for f in "$OUT"/*; do echo "=== $f"; cat "$f"; done
  exit 0
fi
[ -x "$PY" ] || { echo "No venv found at $PY - finish Step 1 of SETUP.md first"; exit 1; }
sudo cp "$OUT"/*.service "$OUT"/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradebot newswatch healthcheck.timer
sleep 5
systemctl is-active tradebot newswatch
