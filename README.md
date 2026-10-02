# Proxmox Backup Manager

Moderná webová aplikácia v Python Flask pre správu a automatizáciu záloh Proxmox VE serverov s nahrávaním na FTP server.

## ✨ Funkcie

- **🔄 Manuálne zálohovanie** - Vytvorenie zálohy na požiadanie jedným klikom
- **⏰ Automatické zálohovanie** - Naplánované zálohy (týždenne/mesačne) 
- **🖥️ Remote SSH zdroj** - LXC appka vie zálohovať Proxmox host cez IP/hostname, SSH meno a heslo
- **💾 Lokálna kópia v LXC** - Archív ostáva v `backups/` a následne sa uploadne na FTP
- **📤 FTP Upload** - Bezpečné nahrávanie záloh na vzdialený FTP server
- **📁 Kategorizovaný výber** - Critical, recommended, optional, large, sensitive a AUTO.FS/QNAP/WD položky
- **🧭 Restore checklist** - Archív obsahuje `backup-info/README-RESTORE.txt`, `recovery-manifest.json` a diagnostické výstupy
- **🛟 Obnova na novom HW** - Klasifikácia obnovy každej položky, pripravenosť READY/WARNING/INCOMPLETE, 11-krokový postup, interná wiki a snapshot pôvodného hosta
- **📊 História záloh** - Prehľad a správa vytvorených záloh
- **🔧 Test pripojenia** - Overenie FTP nastavení pred zálohou
- **🔐 Single-admin login + povinné 2FA** - Prvé otvorenie vynúti registráciu admina, ďalšie účty nie sú povolené
- **📲 Pushover notifikácie** - Voliteľné notifikácie pre manuálne/automatické zálohy a bezpečnostné udalosti
- **📱 Responzívny dizajn** - Moderné Tailwind rozhranie s tabmi

## 🚀 Rýchla inštalácia (LXC v Proxmoxe)

```bash
# Jednorazová inštalácia/update (idempotentný)
bash <(curl -fsSL https://raw.githubusercontent.com/spekulanter/proxmox-backup/main/install_in_lxc.sh)
```

Po inštalácii je aplikácia dostupná na: **http://LXC_IP:5000**

Pri prvom otvorení sa zobrazí registračná stránka. Admin účet sa vytvorí až po naskenovaní 2FA secretu do Google Authenticatora a overení 6-miestnym kódom. Recovery kódy sa zobrazia iba raz, preto si ich uložte mimo servera.

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

Sekcia "Obnova" používa archívy evidované v histórii (FTP-only archív sa najprv stiahne do lokálneho cache) a obnovuje ich cez Remote SSH. Archív sa rozbalí do staging adresára na Proxmox hoste. Potom má obnova dva režimy:

- **Iba pripraviť na kontrolu** (predvolené, odporúčané pre nový HW) - vybrané cesty sa skopírujú do `/root/proxmox-backup-restore-review-*` (práva `0700`, s `README-REVIEW.txt`). Živý systém sa nezmení.
- **Aplikovať na host** (rovnaký HW) - existujúce cieľové súbory/adresáre sa najprv skopírujú do rollback adresára `/root/proxmox-backup-restore-preapply-*` a potom sa prepíšu.

Ochrany:

- V restore zozname nie je nič predvolene vybrané a hromadné „vybrať všetko“ neexistuje.
- `/etc/pve`, `/var/lib/pve-cluster/config.db`, `/etc/passwd`, `/etc/group`, `/etc/shadow` a `/etc/apt` aplikácia **nikdy priamo neprepíše** (`restore_policy: stage_only`); API takýto request odmietne a UI ich aj v režime „Aplikovať“ iba pripraví.
- Priamy restore položiek REVIEW FIRST / SELECTIVE / REFERENCE ONLY vyžaduje výslovné potvrdenie kontroly (`acknowledged_paths` v `/api/restore`, checkbox v UI).
- Whitelist, staging, pre-apply záloha a potvrdenie textom `OBNOVIT` zostávajú.

Obnova je zámerne whitelistovaná na známe konfiguračné cesty a nepodporuje wildcard položky. Aplikácia po obnove nerobí automatický reload ani restart služieb; stav Proxmoxu skontrolujte ručne.

### 🛟 Obnova na novom HW (Disaster Recovery)

Scenár: pôvodný Proxmox server (napr. MSI Cubi) zomrel a konfiguráciu treba obnoviť na novom, prípadne inom hardvéri. Záložka **Obnova na novom HW** obsahuje:

- **Prehľad** - pripravenosť na obnovu a počty „X/Y aktuálne zálohované“ pre každú restore kategóriu.
- **Postup obnovy** - 11-krokový checklist (stav sa ukladá v prehliadači) s príkazmi a položkami zálohy pre každý krok.
- **Položky** - všetky zálohované cesty zoskupené podľa restore kategórie, s detailom.
- **Snapshot hosta** - DR metadata z archívu (NIC a MAC, IP, disky, UUID, storage, PCI, verzia PVE).
- **Wiki** - praktické články (prehľad DR, obnova na novom HW, `/etc/pve` a `config.db`, sieť, disky a fstab, používatelia, SSH, systemd/cron, AutoFS/QNAP/WD, VM/LXC, systémové nastavenia, snapshot, checklist po obnove). Odkaz na článok: `#wiki/<slug>`.

Na záložke Zálohovanie sú filtre (Critical, Recommended, Optional, New HW – Required/Review/Selective, Reference only, Sensitive, Network, Storage) a pri každej položke tlačidlo **Detail**. Detail ukazuje, prečo sa položka zálohuje, čo obsahuje, či sa dá obnoviť priamo, riziká a postup obnovy na rovnakom aj inom HW.

**Restore kategórie** sú nezávislé od tagov critical/recommended/optional. Tagy hovoria, ako dôležité je položku *zálohovať*. Restore kategória hovorí, ako bezpečné je ju *obnoviť* na nový HW:

| Kategória | Badge | Význam | Príklady |
|-----------|-------|--------|----------|
| 🟢 required | NEW HW REQUIRED | Potrebné pre rekonštrukciu pôvodného hosta | `/etc/pve`, `config.db` (ADVANCED RESTORE), `/etc/hostname`, `/etc/hosts` |
| 🟠 review | REVIEW FIRST | Dôležité, ale HW-závislé; najprv porovnať | `/etc/network`, `/etc/fstab`, `/etc/resolv.conf`, moduly, modprobe, sysctl, `/etc/default`, subuid/subgid |
| 🔵 selective | SELECTIVE | Obnoviť iba vlastné položky | `/etc/ssh`, systemd units, cron, `/root`, `/usr/local/*`, autofs, vzdump orchestrácia |
| ⚪ reference | REFERENCE ONLY | Neprepisovať, slúži ako predloha | `/etc/passwd`, `/etc/group`, `/etc/shadow`, `/etc/apt` |
| ▫️ optional | OPTIONAL | Nie je potrebné pre základnú obnovu | `/opt`, `/home`, `/var/lib/vz/template` |

**Pripravenosť na obnovu** sa počíta deterministicky iba z REQUIRED položiek. Položka je OK, ak je v zálohe mladšej ako 35 dní (`RECOVERY_MAX_AGE_DAYS`), nebola v nej preskočená a táto záloha je aj na FTP (mimo hosta):

- `INCOMPLETE` - niektorá REQUIRED položka nie je v žiadnej dostupnej zálohe,
- `WARNING` - všetko je zálohované, ale niečo je staršie ako limit, iba lokálne v LXC alebo nie je vybrané pre ďalšie zálohy,
- `READY` - všetky REQUIRED položky majú aktuálnu zálohu mimo hosta a sú vybrané pre ďalšie zálohy.

**Odporúčaný workflow na novom HW:** 1. čistá inštalácia PVE (rovnaká major verzia, pôvodný hostname) → 2. kontrola HW (NIC, disky, UUID, CPU) → 3. minimálna management sieť → 4. pripojenie QNAP/WD → 5. rozbalenie zálohy do `/root/pve-restore-review` → 6. porovnanie HW-závislej konfigurácie → 7. PVE konfigurácia (`config.db` iba pri zastavenom `pve-cluster`, nikdy `cp -r` do `/etc/pve`) → 8. kontrola hostname, hosts, storage, siete, firewallu → 9. selektívne SSH, systemd, cron, autofs, skripty → 10. VM/LXC z vzdump/PBS/NAS → 11. post-recovery kontrola.

**Čo aplikácia obnovuje automaticky:** nič. Každý restore spúšťa používateľ, vyberá konkrétne cesty a potvrdzuje ho. Disky VM/LXC aplikácia nezálohuje ani neobnovuje.

**DR metadata snapshot:** každá záloha pridá do `backup-info/` výstupy `pveversion -v`, `hostname`, `uname -a`, `lscpu`, `ip -br link`, `ip -br addr`, `ip addr`, `ip route`, `bridge link`, `lsblk -f`, `blkid`, `/dev/disk/by-id`, `findmnt`, `df -h`, `pvesm status`, `pvs`, `vgs`, `lvs`, `zpool status`, `zfs list`, `lspci -nn`, `systemctl --failed`, `qm list` a `pct list`. Pridá aj `recovery-manifest.json` s klasifikáciou položiek. Ak príkaz na hoste neexistuje, zapíše sa chyba a záloha pokračuje. Snapshot je REFERENCE ONLY a nikdy sa neobnovuje.

DR metadáta položiek sú definované v `recovery_data.py` (kľúč = cesta z `DEFAULT_BACKUP_FILES`). Neklasifikovaná vlastná položka dostane bezpečný fallback REVIEW FIRST. Metadáta sa pripájajú iba k API odpovediam (`recovery` objekt), do `backup_config.json` sa neukladajú, preto sa `CONFIG_VERSION` nemení.

Nové API endpointy: `GET /api/recovery/overview`, `GET /api/recovery/checklist`, `GET /api/recovery/wiki`, `GET /api/recovery/wiki/<slug>`, `GET /api/recovery/snapshot/<backup_id>`. `POST /api/restore` po novom prijíma aj `stage_paths` a `acknowledged_paths`.

### 💾 Ukladanie archívov

Každá úspešne vytvorená záloha sa uloží lokálne do `backups/` v LXC a následne sa nahrá na FTP. Ak FTP upload zlyhá, lokálny archív ostane v LXC a história záloh označí FTP stav ako `failed`.

### 🔄 Automatické zálohovanie
- **Denne, týždenne alebo mesačne** - podľa nastavenia v sekcii Automatická záloha
- Vyžaduje systemd timer `proxmox-backup-auto.timer` (pridáva sa automaticky pri inštalácii/update)
- Timer spúšťa `auto_backup.sh` na štvrťhodinách `:00/:15/:30/:45`; skript podľa `backup_config.json` rozhodne, či je automatická záloha zapnutá a či už nastal uložený deň/čas
- `auto_backup.sh` volá JSON API `/api/backup/auto` zo saved configu a pri zapnutom logine používa servisný token z `auth_config.json`, takže nepotrebuje browser session

### 🔐 Prihlásenie, 2FA a recovery

- Aplikácia povoľuje iba jeden admin účet. Ak účet existuje, registračné endpointy ďalšieho používateľa odmietnu.
- Login vyžaduje používateľské meno, heslo a TOTP kód z Google Authenticatora.
- Prihlásenie používa dlhodobú session cookie viazanú na stabilný secret v `auth_config.json`; zmena hesla, reset 2FA alebo logout session zneplatní.
- V správe účtu môžete zmeniť používateľské meno, heslo, resetovať 2FA, regenerovať recovery kódy a nastaviť Pushover.
- Obnova zabudnutého hesla cez web vyžaduje recovery kód a Pushover overovací kód. Ak Pushover nie je nastavený, recovery cez web nie je dostupné.
- Pushover používa App Key/Token a User Key z vášho Pushover účtu. Nastavenia umožňujú samostatne zapnúť notifikácie pre manuálne zálohy, automatické zálohy a bezpečnostné udalosti.

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

- ✅ Login a 2FA chránia webové UI, ale HTTP prenos nešifrujú. Pre produkciu použite VPN, firewall alebo reverse proxy s HTTPS.
- ✅ Stabilný Flask secret, admin hash, TOTP secret, recovery kódy, servisný token a Pushover tokeny sú v `auth_config.json` s právami `0600`
- ✅ Používajte silné FTP heslá a šifrovanie (FTPS/SFTP)
- ✅ `backup_config.json` obsahuje FTP a SSH heslá; aplikácia ho ukladá s právami `0600`
- ✅ Adresár `backups/` obsahuje citlivé archívy a má práva `0700`
- ✅ `auth_config.json` sa nevkladá do git repozitára ani do backup archívu
- ✅ Sensitive položky (`/etc/shadow`, SSH kľúče, `/root`, `/etc/pve`) sú v UI označené ako SECRET/sensitive. Web UI nikdy nezobrazuje obsah zálohovaných súborov, iba cesty, metadáta a názvy členov archívu.
- ✅ Snapshot hosta zobrazuje iba whitelisted diagnostické výstupy (`HOST_SNAPSHOT_FILES`). Konfiguračné súbory, crontab ani zoznam balíkov sa v UI nezobrazujú.
- ✅ Review adresáre `/root/proxmox-backup-restore-review-*` môžu obsahovať `shadow` a privátne kľúče; po dokončení obnovy ich zmažte
- ✅ Pravidelne kontrolujte vytvorené zálohy
- ✅ Otestujte obnovu z záloh

## 📄 Súbory

- `app.py` - Hlavná Flask aplikácia
- `recovery_data.py` - DR metadáta položiek, restore kategórie, wiki a recovery checklist (iba dáta)
- `templates/index.html` - webové rozhranie aplikácie
- `requirements.txt` - Python závislosti
- `install_in_lxc.sh` - Inštalačný skript pre LXC
- `update.sh` - Update skript
- `auto_backup.sh` - Skript pre automatické zálohy
- `test.sh` - Test funkčnosti
- `auth_config.json` - Login/2FA/Pushover runtime konfigurácia (vytvorí sa automaticky)
- `backup_config.json` - Konfigurácia (vytvorí sa automaticky)
- `backup_history.json` - História záloh (vytvorí sa automaticky)
- `backups/` - Lokálne archívy v LXC (vytvorí sa automaticky)

## 📝 Poznámky

- ✅ Aplikácia vytvára komprimované tar.gz archívy
- ✅ Do archívu pridáva `backup-info/` s Proxmox, storage, network, systemd a package inventárom
- ✅ V LXC režime bežia Proxmox info príkazy cez SSH na Proxmox hoste, nie v kontajneri
- ✅ AUTO.FS/QNAP/WD nastavenia sa zálohujú; restore máp a skriptov je SELECTIVE, wildcard units sa obnovujú ručne (postup vo Wiki)
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
venv/bin/python -m py_compile app.py recovery_data.py
venv/bin/python tests/test_archive.py
venv/bin/python tests/test_recovery.py
bash -n install_in_lxc.sh
bash -n update.sh
bash -n auto_backup.sh
bash -n test.sh
```

Voliteľná mobile/desktop UI kontrola (360–1280 px, horizontálny overflow, JS chyby, únik tajomstiev) cez Playwright: návod je v hlavičke `tests/ui_mobile_check.py`.
