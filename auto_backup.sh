#!/bin/bash
# Proxmox Backup Manager - Auto backup (for cron/systemd timer)
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
CONFIG_FILE="${CONFIG_FILE:-${APP_DIR}/backup_config.json}"
HISTORY_FILE="${HISTORY_FILE:-${APP_DIR}/backup_history.json}"
AUTH_FILE="${AUTH_FILE:-${APP_DIR}/auth_config.json}"
API_URL="${BACKUP_MANAGER_URL:-http://127.0.0.1:5000/api/backup/auto}"
PYTHON_BIN="${APP_DIR}/venv/bin/python"

if [ ! -x "${PYTHON_BIN}" ]; then
	PYTHON_BIN="$(command -v python3)"
fi

decision="$("${PYTHON_BIN}" - "${CONFIG_FILE}" "${HISTORY_FILE}" <<'PY'
import json
import sys
from datetime import datetime

config_path, history_path = sys.argv[1], sys.argv[2]

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception as exc:
        print(f"SKIP|Konfigurácia sa nedá načítať: {exc}")
        sys.exit(0)

def parse_int(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number

def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None

config = load_json(config_path, {})
if not config:
    print("SKIP|Konfigurácia neexistuje alebo je prázdna.")
    sys.exit(0)

if not bool(config.get("auto_backup_enabled", False)):
    print("SKIP|Automatické zálohovanie je vypnuté.")
    sys.exit(0)

auto_files = config.get("auto_backup_files") or []
selected_count = sum(1 for item in auto_files if isinstance(item, dict) and item.get("selected"))
if selected_count == 0:
    print("SKIP|Nie sú vybrané žiadne položky pre automatickú zálohu.")
    sys.exit(0)

now = datetime.now()
frequency = config.get("auto_backup_frequency", "monthly")
hour = parse_int(config.get("auto_backup_hour", 2), 2, 0, 23)
minute = parse_int(config.get("auto_backup_minute", 0), 0, 0, 59)
day = parse_int(config.get("auto_backup_day", 6), 6, 0, 27)
scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

if now < scheduled_at:
    print(f"SKIP|Čas automatickej zálohy ešte nenastal ({hour:02d}:{minute:02d}).")
    sys.exit(0)

if frequency == "daily":
    period_key = ("daily", now.date().isoformat())
elif frequency == "weekly":
    if now.weekday() != day:
        print("SKIP|Dnes nie je nastavený deň týždennej automatickej zálohy.")
        sys.exit(0)
    iso = now.isocalendar()
    period_key = ("weekly", iso.year, iso.week)
elif frequency == "monthly":
    month_day = day + 1
    if now.day != month_day:
        print("SKIP|Dnes nie je nastavený deň mesačnej automatickej zálohy.")
        sys.exit(0)
    period_key = ("monthly", now.year, now.month)
else:
    print(f"SKIP|Neznáma frekvencia automatickej zálohy: {frequency}")
    sys.exit(0)

history = load_json(history_path, [])
if not isinstance(history, list):
    history = []

for entry in history:
    if not isinstance(entry, dict) or entry.get("backup_mode") != "auto":
        continue
    timestamp = parse_timestamp(entry.get("timestamp"))
    if not timestamp:
        continue
    if frequency == "daily":
        entry_key = ("daily", timestamp.date().isoformat())
    elif frequency == "weekly":
        entry_iso = timestamp.isocalendar()
        entry_key = ("weekly", entry_iso.year, entry_iso.week)
    else:
        entry_key = ("monthly", timestamp.year, timestamp.month)
    if entry_key == period_key:
        print("SKIP|Automatická záloha pre aktuálne obdobie už existuje.")
        sys.exit(0)

print("RUN|Spúšťam automatickú zálohu podľa uloženého rozvrhu.")
PY
)"

case "${decision}" in
	RUN\|*)
		echo "${decision#RUN|}"
		if [ "${AUTO_BACKUP_DRY_RUN:-0}" = "1" ]; then
			echo "Dry-run režim: API volanie preskakujem."
			exit 0
		fi
		;;
	SKIP\|*)
		echo "${decision#SKIP|}"
		exit 0
		;;
	*)
		echo "Neznámy výsledok plánovača: ${decision}" >&2
		exit 1
		;;
esac

auth_header=()
if [ -f "${AUTH_FILE}" ]; then
	service_token="$("${PYTHON_BIN}" - "${AUTH_FILE}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        print((json.load(handle).get("service_token") or "").strip())
except Exception:
    print("")
PY
)"
	if [ -n "${service_token}" ]; then
		auth_header=(-H "Authorization: Bearer ${service_token}")
	fi
fi

# Prefer local JSON API endpoint to trigger the backup from saved config
curl -fsS -X POST \
	"${auth_header[@]}" \
	-H "Content-Type: application/json" \
	-d '{}' \
	"${API_URL}"
