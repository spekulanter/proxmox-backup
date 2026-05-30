#!/bin/bash
# Proxmox Backup Manager - Auto backup (for cron/systemd timer)
set -euo pipefail

APP_DIR="/opt/proxmox-backup"

# Prefer local JSON API endpoint to trigger the backup from saved config
curl -fsS -X POST \
	-H "Content-Type: application/json" \
	-d '{}' \
	http://127.0.0.1:5000/api/backup/auto || {
	echo "API trigger zlyhal, skúšam priamo Python..."
	if [ -x "${APP_DIR}/venv/bin/python" ]; then
		"${APP_DIR}/venv/bin/python" - <<'PY'
import sys, os
sys.path.insert(0, os.environ.get('APP_DIR', '/opt/proxmox-backup'))
from app import create_backup
# Simulácia POST volania - v app je to route handler, takže tento fallback nemusí fungovať
print('Záloha vyžaduje HTTP volanie /create_backup; API nebolo dostupné.')
PY
	fi
}
