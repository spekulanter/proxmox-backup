# Proxmox Backup Manager

Moderná webová aplikácia v Python Flask pre správu a automatizáciu záloh Proxmox VE serverov s nahrávaním na FTP server.

## ✨ Funkcie

- **🔄 Manuálne zálohovanie** - Vytvorenie zálohy na požiadanie jedným klikom
- **⏰ Automatické zálohovanie** - Naplánované zálohy (týždenne/mesačne) 
- **📤 FTP Upload** - Bezpečné nahrávanie záloh na vzdialený FTP server
- **📁 Výber súborov** - Konfigurovateľný výber kritických Proxmox súborov
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

### 🌐 FTP Server
Nastavte FTP server v sekcii "Nastavenia":
- **Host/IP adresa** - IP alebo doménové meno FTP servera
- **Port** - Predvolene 21 pre FTP
- **Používateľské meno** - FTP account username  
- **Heslo** - FTP account password
- **Test pripojenia** - Overenie nastavení pred uložením

### 📁 Súbory na zálohovanie

Aplikácia má predkonfigurované kľúčové Proxmox súbory rozdelené do kategórií:

**🔴 Kritické súbory:**
- `/etc/pve/` - Hlavná konfigurácia Proxmox (VM, storage, users)
- `/etc/network/interfaces` - Sieťová konfigurácia

**🟡 Dôležité súbory:**
- `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` - Systémové nastavenia
- `/etc/ssl/pve/` - SSL certifikáty pre webové rozhranie
- `/root/` - Skripty a nastavenia administrátora
- `/etc/cron*` - Cron úlohy a plánované úlohy
- `/etc/vzdump.conf` - Vzdump konfigurácia

**🟢 Voliteľné (veľké súbory):**
- `/var/lib/vz/template/` - ISO obrazy a šablóny (môže byť veľké)

### 🔄 Automatické zálohovanie
- **Týždenne** - Každú nedeľu o 02:00
- **Mesačne** - 1. deň v mesiaci o 02:00
- Vyžaduje systemd timer (pridáva sa automaticky)

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

## 📝 Poznámky

- ✅ Aplikácia vytvára komprimované tar.gz archívy
- ✅ Zálohy sa nahrávajú na FTP server pre bezpečné uloženie mimo servera
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
