#!/bin/bash
set -euo pipefail

SERVICE_NAME="proxmox-backup"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "🔄 Aktualizujem Proxmox Backup Manager..."
systemctl stop "${SERVICE_NAME}.service" || true
cd "${APP_DIR}"

# Uistime sa, že repo je čisté a sleduje origin/main
if [ -d .git ]; then
	git fetch origin
	git reset --hard origin/main
else
	echo "Repozitár nie je git repo. Preskakujem git update."
fi

source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
deactivate

mkdir -p "${APP_DIR}/backups"
chmod 700 "${APP_DIR}/backups" || true

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Proxmox Backup Manager (Flask)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 7200 --graceful-timeout 60 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

systemctl start "${SERVICE_NAME}.service"
echo "✅ Aplikácia bola úspešne aktualizovaná."
