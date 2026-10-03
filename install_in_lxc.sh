#!/bin/bash
#
# Proxmox Backup Manager - Smart Installer/Updater for LXC (Proxmox)
# Deteguje existujúcu inštaláciu a spustí buď inštaláciu alebo update
#
set -euo pipefail

# Farebný výstup
msg_info() { echo -e "\033[1;34mINFO\033[0m: $1"; }
msg_ok()   { echo -e "\033[1;32mSUCCESS\033[0m: $1"; }
msg_warn() { echo -e "\033[1;33mWARNING\033[0m: $1"; }
msg_err()  { echo -e "\033[1;31mERROR\033[0m: $1" >&2; }
die()      { msg_err "$1"; exit 1; }

# Predpoklady: root a Debian/Ubuntu (apt)
[ "$(id -u)" -eq 0 ] || die "Skript musí bežať ako root."
command -v apt-get >/dev/null 2>&1 || die "Podporované sú Debian/Ubuntu LXC (chýba apt-get)."

# Konštanty (každú možno prebiť premennou prostredia)
REPO_URL="${REPO_URL:-https://github.com/spekulanter/proxmox-backup.git}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
INSTALL_LOG="${INSTALL_LOG:-/var/log/proxmox-backup-install.log}"

# Ak skript beží z už stiahnutého repozitára (git clone/pull), inštaluje sa práve tento adresár.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "${APP_DIR:-}" ]; then
    if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/app.py" ] && [ -d "${SCRIPT_DIR}/.git" ]; then
        APP_DIR="${SCRIPT_DIR}"
    else
        APP_DIR="/opt/proxmox-backup"
    fi
fi
# Názov služby, port a vetva sa odvodia z priečinka (rovnako ako v update.sh)
SERVICE_NAME="${SERVICE_NAME:-$(basename "${APP_DIR}")}"
case "${SERVICE_NAME}" in
    *-dev) DEFAULT_PORT=5001; DEFAULT_BRANCH=dev; DEFAULT_AUTO_TIMER=0 ;;
    *)     DEFAULT_PORT=5000; DEFAULT_BRANCH=main; DEFAULT_AUTO_TIMER=1 ;;
esac
APP_PORT="${APP_PORT:-${DEFAULT_PORT}}"
GIT_BRANCH="${GIT_BRANCH:-${DEFAULT_BRANCH}}"
ENABLE_AUTO_TIMER="${ENABLE_AUTO_TIMER:-${DEFAULT_AUTO_TIMER}}"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
AUTO_SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}-auto.service"
AUTO_TIMER_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}-auto.timer"

# Spustí príkaz s výstupom do logu; pri chybe vypíše koniec logu (nič sa nestratí potichu)
run_logged() {
    local description="$1"; shift
    if ! "$@" >>"${INSTALL_LOG}" 2>&1; then
        msg_err "${description} zlyhalo. Posledné riadky z ${INSTALL_LOG}:"
        tail -n 15 "${INSTALL_LOG}" >&2 || true
        exit 1
    fi
}

install_auto_backup_timer() {
    msg_info "Vytváram systemd timer pre automatické zálohy..."
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

    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}-auto.timer" &>/dev/null || true
    msg_ok "Timer ${SERVICE_NAME}-auto.timer je pripravený."
}

# Helper: spusti update skript, ak existuje
run_update_script() {
    if [ -x "${APP_DIR}/update.sh" ]; then
        "${APP_DIR}/update.sh"
        return 0
    fi
    return 1
}

# Pomocné: čaká, kým služba odpovie na HTTP; inak vypíše log služby a skončí chybou
wait_for_service() {
    if [ "${INSTALL_SKIP_HEALTHCHECK:-0}" = "1" ]; then
        return 0
    fi
    msg_info "Čakám na odpoveď služby na porte ${APP_PORT}..."
    local attempt
    for attempt in $(seq 1 30); do
        if curl -fsS -o /dev/null "http://127.0.0.1:${APP_PORT}/" 2>/dev/null; then
            msg_ok "Služba odpovedá (HTTP OK)."
            return 0
        fi
        sleep 1
    done
    msg_err "Služba ${SERVICE_NAME}.service neodpovedá na porte ${APP_PORT}. Posledné riadky z logu:"
    journalctl -u "${SERVICE_NAME}.service" -n 20 --no-pager >&2 || true
    exit 1
}

app_url() {
    echo "http://$(hostname -I 2>/dev/null | awk '{print $1}'):${APP_PORT}"
}

# Zisti, či je už aplikácia nainštalovaná
is_installed=false
if [ -d "${APP_DIR}" ] && [ -f "${SERVICE_FILE}" ]; then
    if systemctl is-enabled "${SERVICE_NAME}.service" &>/dev/null || systemctl status "${SERVICE_NAME}.service" &>/dev/null; then
        is_installed=true
    fi
fi

if ${is_installed}; then
    echo "🔄 Detegovaná existujúca inštalácia - spúšťam aktualizáciu..."
    # Pokus o update cez lokálny skript (udržiava logiku na jednom mieste)
    if run_update_script; then
        echo "✅ Aktualizácia dokončená!"
        echo "🌐 Aplikácia: $(app_url)"
        exit 0
    fi

    # Fallback inline update
    msg_info "Zastavujem službu ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}.service" &>/dev/null || true
    msg_ok "Služba zastavená."

    msg_info "Aktualizujem kód z ${REPO_URL}..."
    cd "${APP_DIR}"
    git fetch origin &>/dev/null
    git reset --hard "origin/${GIT_BRANCH}" &>/dev/null
    msg_ok "Kód aktualizovaný."

    msg_info "Aktualizujem Python závislosti..."
    source "${APP_DIR}/venv/bin/activate"
    pip install --upgrade pip setuptools wheel &>/dev/null || true
    pip install -r "${APP_DIR}/requirements.txt" &>/dev/null
    deactivate
    msg_ok "Závislosti aktualizované."

    msg_info "Spúšťam službu ${SERVICE_NAME}..."
    systemctl start "${SERVICE_NAME}.service" &>/dev/null
    msg_ok "Služba spustená."

    if [ "${ENABLE_AUTO_TIMER}" = "1" ]; then
        install_auto_backup_timer
    fi

    echo "✅ Aktualizácia dokončená!"
    echo "🌐 Aplikácia: $(app_url)"
    exit 0
fi

echo "🆕 Spúšťam čerstvú inštaláciu do ${APP_DIR} (služba ${SERVICE_NAME}, port ${APP_PORT})..."
: > "${INSTALL_LOG}" 2>/dev/null || INSTALL_LOG=/dev/null
chmod 600 "${INSTALL_LOG}" 2>/dev/null || true

# Systémové balíky. Čistý LXC ich často nemá:
#   git, ca-certificates  - stiahnutie a aktualizácia kódu cez https
#   curl                  - auto_backup.sh volá API appky; health check
#   python3 (+venv, pip)  - beh appky (Python 3.9+; Debian 12 má 3.11)
#   tzdata                - appka používa časovú zónu Europe/Bratislava (bez nej by padla registrácia admina)
msg_info "Aktualizujem zoznam balíkov a inštalujem potrebné balíčky..."
run_logged "apt-get update" apt-get update -y
run_logged "Inštalácia systémových balíkov" env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y git curl ca-certificates python3 python3-venv python3-pip tzdata
msg_ok "Systémové závislosti nainštalované."

# Zdrojové kódy: použije sa už stiahnutý repozitár, inak sa naklonuje
if [ -d "${APP_DIR}/.git" ]; then
    msg_info "Repozitár už existuje v ${APP_DIR}, preskakujem klonovanie."
else
    if [ -d "${APP_DIR}" ] && [ -n "$(ls -A "${APP_DIR}" 2>/dev/null)" ]; then
        die "Adresár ${APP_DIR} existuje, nie je prázdny a nie je to git repozitár. Presuň ho alebo nastav APP_DIR."
    fi
    msg_info "Klonujem repozitár ${REPO_URL} (vetva ${GIT_BRANCH})..."
    mkdir -p "${APP_DIR}"
    run_logged "git clone" git clone --branch "${GIT_BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi
[ -f "${APP_DIR}/app.py" ] || die "V ${APP_DIR} chýba app.py – nie je to repozitár Proxmox Backup Manager."
msg_ok "Zdrojové kódy pripravené."

# Prevádzkové skripty spustiteľné; adresár záloh iba pre root
for script in install_in_lxc.sh update.sh auto_backup.sh test.sh; do
    if [ -f "${APP_DIR}/${script}" ]; then
        chmod +x "${APP_DIR}/${script}" || true
    fi
done
mkdir -p "${APP_DIR}/templates" "${APP_DIR}/backups"
chmod 700 "${APP_DIR}/backups" || true

# Python venv a závislosti
msg_info "Vytváram Python virtualenv a inštalujem knižnice (môže chvíľu trvať)..."
run_logged "Vytvorenie virtualenv" python3 -m venv "${APP_DIR}/venv"
run_logged "Aktualizácia pip" "${APP_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel
run_logged "Inštalácia Python knižníc" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
msg_ok "Knižnice nainštalované."

# Overenie, že appka sa dá importovať a má časové zóny
msg_info "Overujem prostredie..."
(cd "${APP_DIR}" && run_logged "Overenie prostredia (import appky, tzdata)" "${APP_DIR}/venv/bin/python" -c \
    "import zoneinfo, sqlite3, flask, paramiko, qrcode, gunicorn; zoneinfo.ZoneInfo('Europe/Bratislava'); import app")
msg_ok "Prostredie je v poriadku."

# Systemd služba
msg_info "Vytváram systemd službu ${SERVICE_NAME}.service..."
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
msg_ok "Služba vytvorená."

msg_info "Aktivujem a spúšťam službu..."
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service" &>/dev/null || die "Službu ${SERVICE_NAME}.service sa nepodarilo spustiť (journalctl -u ${SERVICE_NAME}.service)."
msg_ok "Služba ${SERVICE_NAME}.service je aktívna."

if [ "${ENABLE_AUTO_TIMER}" = "1" ]; then
    install_auto_backup_timer
fi

wait_for_service

echo ""
echo "🎉 Inštalácia dokončená!"
echo "🌐 Aplikácia: $(app_url)"
echo "ℹ️ Opätovné spustenie tohto skriptu vykoná UPDATE."
echo ""
echo "🚀 Prvé kroky v appke:"
echo "   1. Otvor adresu vyššie a vytvor admin účet (povinné TOTP 2FA, uložiť recovery kódy)."
echo "   2. Nastavenia -> Zdroj zálohy: Remote SSH (IP Proxmox hosta, root + heslo; na hoste musí byť povolený root login heslom) -> Test SSH."
echo "   3. Nastavenia -> FTP server -> Test FTP; potom vyber súbory a spusti prvú zálohu."
echo "   4. LXC musí dosiahnuť Proxmox host (SSH, port 22) a FTP server; port ${APP_PORT} povoľ vo firewalle."
echo "   5. Časovú zónu LXC nastav podľa potreby (napr. timedatectl set-timezone Europe/Bratislava) – plánovač záloh ide podľa lokálneho času."

echo ""
echo "📖 Užitočné príkazy:"
echo "   Reštart služby:    systemctl restart ${SERVICE_NAME}.service"
echo "   Stav služby:       systemctl status ${SERVICE_NAME}.service"
echo "   Logy služby:       journalctl -u ${SERVICE_NAME}.service -f"
echo "   Manuálny update:   ${APP_DIR}/update.sh"
