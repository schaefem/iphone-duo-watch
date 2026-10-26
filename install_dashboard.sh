#!/usr/bin/env bash
# Richtet das Dashboard als Dienst ein (startet automatisch beim Booten).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
sed -i 's/\r$//' run.sh dashboard.py 2>/dev/null || true
chmod +x run.sh

PORT=$(grep -E '^DASHBOARD_PORT=' config.env 2>/dev/null | cut -d= -f2 | tr -d '"' || true)
PORT=${PORT:-8080}

sudo tee /etc/systemd/system/duowatch-dashboard.service > /dev/null <<EOF
[Unit]
Description=iPhone-Duo-Waechter Dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$(whoami)
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/dashboard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now duowatch-dashboard.service
sudo systemctl restart duowatch-dashboard.service
sleep 2
systemctl --no-pager --lines=3 status duowatch-dashboard.service || true
echo
echo "== Dashboard: http://$(hostname).local:$PORT  (oder http://$(hostname -I | awk '{print $1}'):$PORT) =="
