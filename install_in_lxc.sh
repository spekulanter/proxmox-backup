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

# Konštanty
REPO_URL="https://github.com/spekulanter/proxmox-backup.git"
APP_DIR="/opt/proxmox-backup"
SERVICE_NAME="proxmox-backup"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Helper: spusti update skript, ak existuje
run_update_script() {
    if [ -x "${APP_DIR}/update.sh" ]; then
        "${APP_DIR}/update.sh"
        return 0
    fi
    return 1
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
        echo "🌐 Aplikácia: http://$(hostname -I | awk '{print $1}'):5000"
        exit 0
    fi

    # Fallback inline update
    msg_info "Zastavujem službu ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}.service" &>/dev/null || true
    msg_ok "Služba zastavená."

    msg_info "Aktualizujem kód z ${REPO_URL}..."
    cd "${APP_DIR}"
    git fetch origin &>/dev/null
    git reset --hard origin/main &>/dev/null
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

    echo "✅ Aktualizácia dokončená!"
    echo "🌐 Aplikácia: http://$(hostname -I | awk '{print $1}'):5000"
    exit 0
fi

echo "🆕 Spúšťam čerstvú inštaláciu..."

# Inštalácia systémových balíkov
msg_info "Aktualizujem systém a inštalujem potrebné balíčky..."
apt-get update -y &>/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3-pip python3-venv curl &>/dev/null
msg_ok "Systémové závislosti nainštalované."

# Príprava adresára a klon repozitára
msg_info "Pripravujem adresár aplikácie..."
mkdir -p "${APP_DIR}"
msg_ok "Adresár pripravený."

if [ ! -d "${APP_DIR}/.git" ]; then
    msg_info "Klonujem repozitár ${REPO_URL}..."
    git clone "${REPO_URL}" "${APP_DIR}" &>/dev/null
else
    msg_warn "Repozitár už existuje, preskakujem klonovanie."
fi
msg_ok "Zdrojové kódy pripravené."

# Uistime sa, že update skript je spustiteľný
if [ -f "${APP_DIR}/update.sh" ]; then
    chmod +x "${APP_DIR}/update.sh" || true
fi

# Ak chýba templates/index.html, ale existuje koreňový index.html, presuň ho
if [ ! -f "${APP_DIR}/templates/index.html" ] && [ -f "${APP_DIR}/index.html" ]; then
  msg_info "Presúvam index.html do templates/ pre Flask..."
  mkdir -p "${APP_DIR}/templates"
  mv "${APP_DIR}/index.html" "${APP_DIR}/templates/index.html"
  msg_ok "Šablóna premiestnená."
fi

# Uisti sa, že templates adresár existuje
mkdir -p "${APP_DIR}/templates"# Python venv a závislosti
msg_info "Vytváram Python virtualenv..."
python3 -m venv "${APP_DIR}/venv"
msg_ok "Virtualenv vytvorený."

msg_info "Inštalujem Python knižnice..."
source "${APP_DIR}/venv/bin/activate"
pip install --upgrade pip setuptools wheel &>/dev/null || true
pip install -r "${APP_DIR}/requirements.txt" &>/dev/null
deactivate
msg_ok "Knižnice nainštalované."

# Systemd služba
msg_info "Vytváram systemd službu ${SERVICE_NAME}.service..."
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
ExecStart=${APP_DIR}/venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
msg_ok "Služba vytvorená."

msg_info "Aktivujem a spúšťam službu..."
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service" &>/dev/null
msg_ok "Služba ${SERVICE_NAME}.service je aktívna."

echo "🎉 Inštalácia dokončená!"
echo "🌐 Aplikácia: http://$(hostname -I | awk '{print $1}'):5000"
echo "ℹ️ Opätovné spustenie tohto skriptu vykoná UPDATE."

echo ""
echo "📖 Užitočné príkazy:"
echo "   Reštart služby:    systemctl restart ${SERVICE_NAME}.service"
echo "   Stav služby:       systemctl status ${SERVICE_NAME}.service"
echo "   Logy služby:       journalctl -u ${SERVICE_NAME}.service -f"
echo "   Manuálny update:   ${APP_DIR}/update.sh"