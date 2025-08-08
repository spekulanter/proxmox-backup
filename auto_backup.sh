#!/bin/bash

# Proxmox Backup Manager
# Automatický zálohovací skript pre cron

# Nastavte cestu k vašej aplikácii
APP_DIR="/cesta/k/proxmox-backup-manager"
PYTHON_PATH="/usr/bin/python3"

cd "$APP_DIR"

# Spustenie zálohy cez API
curl -X POST http://localhost:5000/create_backup

# Alebo môžete spustiť priamo Python funkciu
# $PYTHON_PATH -c "
# import sys
# sys.path.append('$APP_DIR')
# from app import create_backup_function
# create_backup_function()
# "