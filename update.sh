#!/bin/bash
set -euo pipefail

SERVICE_NAME="proxmox-backup"
APP_DIR="/opt/proxmox-backup"

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

systemctl start "${SERVICE_NAME}.service"
echo "✅ Aplikácia bola úspešne aktualizovaná."
