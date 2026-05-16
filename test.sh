#!/bin/bash
# Quick test script for Proxmox Backup Manager

set -e

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
"${PYTHON_BIN}" -m py_compile app.py
"${PYTHON_BIN}" tests/test_archive.py
echo "✅ Python smoke testy prešli"

# Test service status
echo "1️⃣ Kontrola stavu služby..."
systemctl is-active proxmox-backup.service --quiet 2>/dev/null && echo "✅ Služba beží" || echo "❌ Služba nebeží"

# Test HTTP response
echo "2️⃣ Test HTTP odpovede..."
if curl -fsS http://127.0.0.1:5000/ >/dev/null 2>&1; then
    echo "✅ HTTP endpoint odpovedá"
else
    echo "❌ HTTP endpoint neodpovedá"
fi

# Test template rendering
echo "3️⃣ Test template-u..."
if curl -s http://127.0.0.1:5000/ 2>/dev/null | grep -q "Proxmox Backup Manager"; then
    echo "✅ Template sa načítava správne"
else
    echo "❌ Problém s template-om"
fi

# Test FTP test endpoint
echo "4️⃣ Test FTP endpoint..."
FTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
   -d '{"host":"test","username":"test","password":"test","port":21}' \
   http://127.0.0.1:5000/test_ftp 2>/dev/null || true)"
if [ "${FTP_STATUS}" = "200" ] || [ "${FTP_STATUS}" = "400" ]; then
    echo "✅ FTP test endpoint funguje"
else
    echo "❌ FTP test endpoint nefunguje"
fi

echo ""
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
SERVICE_STATUS="$(systemctl is-active proxmox-backup.service 2>/dev/null || true)"
echo "🌐 Aplikácia je dostupná na: http://${HOST_IP:-LXC_IP}:5000"
echo "📊 Stav služby: ${SERVICE_STATUS:-nedostupný}"
