#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env

echo "Installerer systempakker ..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "Oppretter virtuelt Python-miljø ..."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

APP_DIR=$(pwd)
USER_NAME=$(id -un)
APP_PORT=$(sed -n 's/^APP_PORT=//p' .env | tail -n 1)
: "${APP_PORT:=8080}"
SERVICE_FILE=/etc/systemd/system/bokhylla.service

echo "Oppretter systemd-tjeneste ..."
sudo sh -c "cat > '$SERVICE_FILE' <<EOF
[Unit]
Description=Bokhylla
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable --now bokhylla.service

echo "Bokhylla er startet på http://ENHETENS-IP:$APP_PORT"
echo "Vis logger med: sudo journalctl -u bokhylla -f"
