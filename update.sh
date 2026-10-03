#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Názov služby, port a vetva sa odvodia z priečinka (proxmox-backup -> prod, proxmox-backup-dev -> dev)
SERVICE_NAME="${SERVICE_NAME:-$(basename "${APP_DIR}")}"
case "${SERVICE_NAME}" in
	*-dev) DEFAULT_PORT=5001; DEFAULT_BRANCH=dev; DEFAULT_AUTO_TIMER=0 ;;
	*)     DEFAULT_PORT=5000; DEFAULT_BRANCH=main; DEFAULT_AUTO_TIMER=1 ;;
esac
APP_PORT="${APP_PORT:-${DEFAULT_PORT}}"
GIT_BRANCH="${GIT_BRANCH:-${DEFAULT_BRANCH}}"
ENABLE_AUTO_TIMER="${ENABLE_AUTO_TIMER:-${DEFAULT_AUTO_TIMER}}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
AUTO_SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}-auto.service"
AUTO_TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}-auto.timer"

install_auto_backup_timer() {
	cat > "${AUTO_SERVICE_FILE}" <<EOF
[Unit]
Description=Run Proxmox Backup Manager automatic backup
Wants=network-online.target ${SERVICE_NAME}.service
After=network-online.target ${SERVICE_NAME}.service

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=${APP_DIR}
Environment=APP_DIR=${APP_DIR}
Environment=APP_PORT=${APP_PORT}
ExecStart=${APP_DIR}/auto_backup.sh
EOF

	cat > "${AUTO_TIMER_FILE}" <<EOF
[Unit]
Description=Schedule Proxmox Backup Manager automatic backup checks

[Timer]
OnBootSec=5min
OnCalendar=*:0/15
AccuracySec=1s
Persistent=true
Unit=${SERVICE_NAME}-auto.service

[Install]
WantedBy=timers.target
EOF
}

echo "🔄 Aktualizujem Proxmox Backup Manager..."
systemctl stop "${SERVICE_NAME}.service" || true
cd "${APP_DIR}"

# Uistime sa, že repo je čisté a sleduje origin/${GIT_BRANCH}
if [ -d .git ]; then
	git fetch origin
	git checkout "${GIT_BRANCH}"
	git reset --hard origin/${GIT_BRANCH}
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
Description=Proxmox Backup Manager (Flask) - ${SERVICE_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=APP_PORT=${APP_PORT}
ExecStart=${APP_DIR}/venv/bin/gunicorn --bind 0.0.0.0:${APP_PORT} --workers 2 --timeout 7200 --graceful-timeout 60 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
if [ "${ENABLE_AUTO_TIMER}" = "1" ]; then
	install_auto_backup_timer
fi
systemctl daemon-reload

systemctl start "${SERVICE_NAME}.service"
systemctl enable "${SERVICE_NAME}.service" || true
if [ "${ENABLE_AUTO_TIMER}" = "1" ]; then
	systemctl enable --now "${SERVICE_NAME}-auto.timer" || true
fi
echo "✅ Aplikácia bola úspešne aktualizovaná."
