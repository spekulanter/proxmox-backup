#!/bin/bash
# Quick test script for Proxmox Backup Manager

set -e

echo "🧪 Testovanie Proxmox Backup Manager..."

# Test service status
echo "1️⃣ Kontrola stavu služby..."
systemctl is-active proxmox-backup.service --quiet && echo "✅ Služba beží" || echo "❌ Služba nebeží"

# Test HTTP response
echo "2️⃣ Test HTTP odpovede..."
if curl -fsS http://127.0.0.1:5000/ >/dev/null; then
    echo "✅ HTTP endpoint odpovedá"
else
    echo "❌ HTTP endpoint neodpovedá"
fi

# Test template rendering
echo "3️⃣ Test template-u..."
if curl -s http://127.0.0.1:5000/ | grep -q "Proxmox Backup Manager"; then
    echo "✅ Template sa načítava správne"
else
    echo "❌ Problém s template-om"
fi

# Test FTP test endpoint
echo "4️⃣ Test FTP endpoint..."
if curl -fsS -X POST -H "Content-Type: application/json" \
   -d '{"host":"test","username":"test","password":"test","port":21}' \
   http://127.0.0.1:5000/test_ftp >/dev/null; then
    echo "✅ FTP test endpoint funguje"
else
    echo "❌ FTP test endpoint nefunguje"
fi

echo ""
echo "🌐 Aplikácia je dostupná na: http://$(hostname -I | awk '{print $1}'):5000"
echo "📊 Stav služby: $(systemctl is-active proxmox-backup.service)"
