# Proxmox Backup Manager

Moderná webová aplikácia v Python Flask pre správu a automatizáciu záloh Proxmox VE serverov s nahrávaním na FTP server.

## ✨ Funkcie

- **🔄 Manuálne zálohovanie** - Vytvorenie zálohy na požiadanie jedným klikom
- **⏰ Automatické zálohovanie** - Naplánované zálohy (týždenne/mesačne) 
- **🖥️ Remote SSH zdroj** - LXC appka vie zálohovať Proxmox host cez IP/hostname, SSH meno a heslo
- **💾 Lokálna kópia v LXC** - Archív ostáva v `backups/` a následne sa uploadne na FTP
- **📤 FTP Upload** - Bezpečné nahrávanie záloh na vzdialený FTP server
- **📁 Kategorizovaný výber** - Critical, recommended, optional, large, sensitive a AUTO.FS/QNAP/WD položky
- **🧭 Restore checklist** - Archív obsahuje `backup-info/README-RESTORE.txt` a diagnostické výstupy
- **📊 História záloh** - Prehľad a správa vytvorených záloh
- **🔧 Test pripojenia** - Overenie FTP nastavení pred zálohou
- **📱 Responzívny dizajn** - Moderné Tailwind rozhranie s tabmi

## 🚀 Rýchla inštalácia (LXC v Proxmoxe)

```bash
# Jednorazová inštalácia/update (idempotentný)
bash <(curl -fsSL https://raw.githubusercontent.com/spekulanter/proxmox-backup/main/install_in_lxc.sh)
```

Po inštalácii je aplikácia dostupná na: **http://LXC_IP:5000**

## 📦 Manuálna inštalácia

1. **Systémové závislosti:**
   ```bash
   apt update && apt install -y python3 python3-pip python3-venv git curl
   ```

2. **Klonovanie a setup:**
   ```bash
   git clone https://github.com/spekulanter/proxmox-backup.git /opt/proxmox-backup
   cd /opt/proxmox-backup
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Systemd služba:**
   ```bash
   # Skopíruj a uprav service súbor z install_in_lxc.sh
   systemctl enable --now proxmox-backup.service
   ```

## ⚙️ Konfigurácia

### 🖥️ Zdroj Proxmoxu

Pri odporúčanom LXC nasadení nastavte v sekcii "Nastavenia":
- **Režim zdroja** - `Remote SSH z LXC`
- **Proxmox IP / Hostiteľ** - IP alebo DNS názov Proxmox hosta
- **SSH port** - Predvolene 22
- **SSH používateľ** - Typicky `root`
- **SSH heslo** - Rovnaké heslo ako pri SSH prihlásení na Proxmox host
- **Test SSH** - Overí SSH login aj dostupnosť Proxmox príkazu `pveversion -v`

Rovnaká VLAN nestačí na čítanie host súborov. LXC appka potrebuje buď SSH prístup na Proxmox host, alebo lokálny režim pri inštalácii priamo na hoste. `nesting` ani `keyctl` samy o sebe nedajú LXC prístup k `/etc/pve`.

### 🌐 FTP Server
Nastavte FTP server v sekcii "Nastavenia":
- **Host/IP adresa** - IP alebo doménové meno FTP servera
- **Port** - Predvolene 21 pre FTP
- **Používateľské meno** - FTP account username  
- **Heslo** - FTP account password
- **Cieľový adresár na FTP** - Voliteľný adresár, napr. `/backups/proxmox`, ak FTP login nemá právo zapisovať do koreňa
- **Test pripojenia** - Overí login, prepnutie do cieľového adresára, testovací upload a delete

### 📁 Súbory na zálohovanie

Aplikácia má predkonfigurované kľúčové Proxmox súbory rozdelené do kategórií:

**🔴 Critical Proxmox:**
- `/etc/pve` - VM/LXC configy, storage, users, firewall, datacenter
- `/var/lib/pve-cluster/config.db` - pmxcfs cluster databáza
- `/etc/network`, `/etc/hosts`, `/etc/hostname`, `/etc/fstab`, `/etc/resolv.conf`

**🟡 Systémová konfigurácia:**
- `/etc/apt`, `/etc/systemd/system`, `/etc/default`
- `/etc/modules`, `/etc/modprobe.d`, `/etc/sysctl.conf`, `/etc/sysctl.d`
- `/var/spool/cron`, `/etc/cron*`, `/etc/vzdump.conf`, `/etc/ssl/pve`

**🔐 Host účty a SSH prístup:**
- `/etc/passwd`, `/etc/group`, `/etc/shadow`
- `/etc/subuid`, `/etc/subgid`
- `/etc/ssh`

**🛠️ Admin a AUTO.FS/QNAP/WD:**
- `/root`, `/usr/local/bin`, `/usr/local/sbin`
- `/etc/auto.master`, `/etc/auto.master.d`, `/etc/auto.nfs`
- `/etc/systemd/system/pve-backup-*.service`
- `/etc/systemd/system/pve-backup-*.timer`
- `/usr/local/sbin/pve_vzdump_enable_run_disable.sh`

**🟢 Voliteľné veľké položky:**
- `/opt`, `/home`, `/var/lib/vz/template`

Archív vždy vynecháva mount/runtime/cache cesty ako `/mnt`, `/media`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, `/var/tmp`, `/var/cache`, `/var/log`, `/lost+found` a `/etc/pve/.rrd`.

### 🔁 Obnova cez SSH

Sekcia "Obnova" používa iba lokálne archívy evidované v histórii a obnovuje ich cez Remote SSH. Pred aplikovaním vybraných ciest sa archív rozbalí do staging adresára na Proxmox hoste a existujúce cieľové súbory/adresáre sa skopírujú do rollback adresára `/root/proxmox-backup-restore-preapply-*`.

Obnova je zámerne whitelistovaná na známe konfiguračné cesty a nepodporuje voliteľné veľké dáta ani wildcard položky. Aplikácia po obnove nerobí automatický reload ani restart služieb; stav Proxmoxu skontrolujte ručne.

### 💾 Ukladanie archívov

Každá úspešne vytvorená záloha sa uloží lokálne do `backups/` v LXC a následne sa nahrá na FTP. Ak FTP upload zlyhá, lokálny archív ostane v LXC a história záloh označí FTP stav ako `failed`.

### 🔄 Automatické zálohovanie
- **Týždenne** - Každú nedeľu o 02:00
- **Mesačne** - 1. deň v mesiaci o 02:00
- Vyžaduje systemd timer (pridáva sa automaticky)
- `auto_backup.sh` spúšťa JSON API zálohu zo saved configu, takže zlyhá viditeľne pri chýbajúcom SSH/FTP nastavení

## 🛠️ Správa služby

```bash
# Stav služby
systemctl status proxmox-backup.service

# Reštart služby  
systemctl restart proxmox-backup.service

# Zobrazenie logov
journalctl -u proxmox-backup.service -f

# Manuálny update
/opt/proxmox-backup/update.sh

# Test funkčnosti
/opt/proxmox-backup/test.sh
```

## 🔒 Bezpečnosť

- ✅ Zmente `secret_key` v `app.py` pre produkčné použitie
- ✅ Zabezpečte prístup k aplikácii (firewall, VPN, reverse proxy)
- ✅ Používajte silné FTP heslá a šifrovanie (FTPS/SFTP)
- ✅ `backup_config.json` obsahuje FTP a SSH heslá; aplikácia ho ukladá s právami `0600`
- ✅ Adresár `backups/` obsahuje citlivé archívy a má práva `0700`
- ✅ Pravidelne kontrolujte vytvorené zálohy
- ✅ Otestujte obnovu z záloh

## 📄 Súbory

- `app.py` - Hlavná Flask aplikácia
- `templates/index.html` - webové rozhranie aplikácie
- `requirements.txt` - Python závislosti
- `install_in_lxc.sh` - Inštalačný skript pre LXC
- `update.sh` - Update skript
- `auto_backup.sh` - Skript pre automatické zálohy
- `test.sh` - Test funkčnosti
- `backup_config.json` - Konfigurácia (vytvorí sa automaticky)
- `backup_history.json` - História záloh (vytvorí sa automaticky)
- `backups/` - Lokálne archívy v LXC (vytvorí sa automaticky)

## 📝 Poznámky

- ✅ Aplikácia vytvára komprimované tar.gz archívy
- ✅ Do archívu pridáva `backup-info/` s Proxmox, storage, network, systemd a package inventárom
- ✅ V LXC režime bežia Proxmox info príkazy cez SSH na Proxmox hoste, nie v kontajneri
- ✅ AUTO.FS/QNAP/WD nastavenia sa zálohujú, ale restore je zatiaľ manuálny podľa checklistu
- ✅ Zálohy ostávajú lokálne v LXC a nahrávajú sa na FTP server pre bezpečné uloženie mimo servera
- ✅ História záloh sa ukladá lokálne v JSON súbore
- ✅ Beží cez systemd službu s automatickým reštartom
- ✅ Responzívny Tailwind dizajn pre mobily a tablety
- ✅ Real-time FTP test s vizuálnym feedbackom

## 🆘 Riešenie problémov

**Služba nebeží:**
```bash
systemctl status proxmox-backup.service
journalctl -u proxmox-backup.service --no-pager
```

**Aplikácia nedostupná:**
```bash
curl -I http://127.0.0.1:5000/
netstat -tlnp | grep :5000
```

**FTP problémy:**
- Skontrolujte firewall na FTP serveri (port 21)
- Overte FTP credentials
- Ak záloha skončí s `550 Forbidden filename`, FTP server pravdepodobne nepovoľuje zápis v aktuálnom adresári; nastavte konkrétny cieľový adresár s právom zápisu
- Testujte manuálne: `ftp your-ftp-server.com`

## 🏗️ Vývoj

Projekt využíva:
- **Backend:** Python 3.11+ + Flask 2.3+
- **Frontend:** Tailwind CDN + vanilla JavaScript
- **Deployment:** systemd + gunicorn
- **Architecture:** Single-file Flask app s JSON persistenciou

Pre vývoj:
```bash
cd /opt/proxmox-backup
source venv/bin/activate
python app.py  # Development server na :5000
```

Lokálne kontroly:
```bash
venv/bin/python -m py_compile app.py
venv/bin/python tests/test_archive.py
bash -n install_in_lxc.sh
bash -n update.sh
bash -n auto_backup.sh
bash -n test.sh
```
