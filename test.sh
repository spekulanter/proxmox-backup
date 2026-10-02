#!/bin/bash
# Quick test script for Proxmox Backup Manager

set -e

SERVICE_NAME="${SERVICE_NAME:-$(basename "$(pwd)")}"
case "${SERVICE_NAME}" in *-dev) APP_PORT="${APP_PORT:-5001}" ;; *) APP_PORT="${APP_PORT:-5000}" ;; esac

echo "🧪 Testovanie Proxmox Backup Manager..."

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "${PYTHON_BIN}" ]; then
    if [ -x "venv/bin/python" ]; then
        PYTHON_BIN="venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi

echo "0️⃣ Lokálne Python smoke testy..."
"${PYTHON_BIN}" -m py_compile app.py recovery_data.py
"${PYTHON_BIN}" tests/test_archive.py
"${PYTHON_BIN}" tests/test_recovery.py
echo "✅ Python smoke testy prešli"

# Test service status
echo "1️⃣ Kontrola stavu služby..."
systemctl is-active "${SERVICE_NAME}.service" --quiet 2>/dev/null && echo "✅ Služba beží" || echo "❌ Služba nebeží"

# Test HTTP response
echo "2️⃣ Test HTTP odpovede..."
if curl -fsS http://127.0.0.1:${APP_PORT}/ >/dev/null 2>&1; then
    echo "✅ HTTP endpoint odpovedá"
else
    echo "❌ HTTP endpoint neodpovedá"
fi

# Test template rendering
echo "3️⃣ Test template-u..."
if curl -s http://127.0.0.1:${APP_PORT}/ 2>/dev/null | grep -q "Proxmox Backup Manager"; then
    echo "✅ Template sa načítava správne"
else
    echo "❌ Problém s template-om"
fi

# Test auth status endpoint
echo "4️⃣ Test auth endpoint..."
AUTH_STATUS="$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:${APP_PORT}/api/auth/status 2>/dev/null || true)"
if [ "${AUTH_STATUS}" = "200" ]; then
    echo "✅ Auth status endpoint funguje"
else
    echo "❌ Auth status endpoint nefunguje"
fi

echo ""
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
SERVICE_STATUS="$(systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || true)"
echo "🌐 Aplikácia je dostupná na: http://${HOST_IP:-LXC_IP}:${APP_PORT}"
echo "📊 Stav služby: ${SERVICE_STATUS:-nedostupný}"
