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
- **🛟 Obnova a migrácia** - Obnova po havárii, plánovaná migrácia a obnova súborov ako sprievodcovia krok za krokom; klasifikácia obnovy každej položky, pripravenosť READY/WARNING/INCOMPLETE, 11-krokový postup, interná wiki a snapshot pôvodného hosta
- **📊 História záloh** - Prehľad a správa vytvorených záloh
- **🔧 Test pripojenia** - Overenie FTP nastavení pred zálohou
- **🔐 Single-admin login + povinné 2FA** - Prvé otvorenie vynúti registráciu admina, ďalšie účty nie sú povolené
- **📲 Pushover notifikácie** - Voliteľné notifikácie pre manuálne/automatické zálohy a bezpečnostné udalosti
- **📱 Responzívny dizajn** - Moderné Tailwind rozhranie s tabmi

## 🚀 Rýchla inštalácia (LXC v Proxmoxe)

Podporované sú Debian 12 / Ubuntu 22.04+ LXC (Python 3.9+), spustené ako root. Čistý LXC nemusí mať ani `curl`, preto sa najprv doinštaluje:

```bash
apt-get update && apt-get install -y curl ca-certificates
# Jednorazová inštalácia/update (idempotentný) – skript si sám naklonuje repozitár do /opt/proxmox-backup
bash <(curl -fsSL https://raw.githubusercontent.com/spekulanter/proxmox-backup/main/install_in_lxc.sh)
```

**Alternatíva – repozitár už máš stiahnutý** (git clone/pull). Skript nainštaluje práve tento adresár, bez ďalšieho klonovania:

```bash
apt-get update && apt-get install -y git ca-certificates
git clone https://github.com/spekulanter/proxmox-backup.git /opt/proxmox-backup
/opt/proxmox-backup/install_in_lxc.sh
```

Skript nainštaluje systémové balíky (`git curl ca-certificates python3 python3-venv python3-pip tzdata`), vytvorí `venv`, nainštaluje knižnice, overí import appky, vytvorí systemd službu a timer automatických záloh, spustí ju a počká na HTTP odpoveď. Pri chybe vypíše koniec logu (`/var/log/proxmox-backup-install.log`). Voliteľné premenné prostredia: `APP_DIR`, `APP_PORT`, `GIT_BRANCH`, `REPO_URL`, `ENABLE_AUTO_TIMER`. Adresár s názvom `*-dev` sa nainštaluje na port 5001, vetvu `dev` a bez auto timera.

Po inštalácii je aplikácia dostupná na: **http://LXC_IP:5000**

**Čo musí platiť mimo LXC:** LXC dosiahne Proxmox host (SSH, port 22) a FTP server; na Proxmox hoste je povolený root login heslom (appka sa pripája cez paramiko); port appky je povolený vo firewalle.

Pri prvom otvorení sa zobrazí registračná stránka. Admin účet sa vytvorí až po naskenovaní 2FA secretu do Google Authenticatora a overení 6-miestnym kódom. Recovery kódy sa zobrazia iba raz, preto si ich uložte mimo servera.

## 📦 Manuálna inštalácia

1. **Systémové závislosti:**
   ```bash
   apt update && apt install -y python3 python3-pip python3-venv git curl ca-certificates tzdata
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

Obnova je zámerne whitelistovaná na známe konfiguračné cesty a nepodporuje wildcard položky. Štandardné maskované systemd jednotky (`systemctl mask`, symlink na `/dev/null` pod `/etc/systemd/`) archív neblokujú; iné nebezpečné linky sa stále odmietajú. Aplikácia po obnove nerobí automatický reload ani restart služieb; stav Proxmoxu skontrolujte ručne.

### 🛟 Obnova a migrácia (Disaster Recovery)

Scenár: pôvodný Proxmox server (napr. MSI Cubi) zomrel a konfiguráciu treba obnoviť na novom, prípadne inom hardvéri. Záložka **Obnova a migrácia** obsahuje:

- **Prehľad** - pripravenosť na obnovu, počty „X/Y aktuálne zálohované“ pre každú restore kategóriu, **riziká obnovy** a tlačidlo **Stiahnuť offline príručku**.
- **Postup obnovy** - 11-krokový checklist (stav sa ukladá v prehliadači) s príkazmi a položkami zálohy pre každý krok.
- **Položky** - všetky zálohované cesty zoskupené podľa restore kategórie, s detailom.
- **Snapshot hosta** - DR metadata z archívu (NIC a MAC, IP, disky, UUID, storage, PCI, verzia PVE).
- **Wiki** - praktické články (prehľad DR, obnova na novom HW, `/etc/pve` a `config.db` vrátane nového názvu nodu, sieť, disky a fstab, používatelia, SSH, systemd/cron, AutoFS/QNAP/WD, VM/LXC, obnova samotného Proxmox Backup Managera, systémové nastavenia, snapshot, checklist po obnove). Odkaz na článok: `#wiki/<slug>`.

**Riziká obnovy** sa počítajú deterministicky z najnovšieho lokálneho archívu (`backup-info/` + `/etc/pve`). Stav READY/WARNING/INCOMPLETE neovplyvňujú:

- hostia (VM/LXC), ktorí nie sú v žiadnom vzdump jobe,
- vzdump job viazaný na iný node, než ako sa host volá (napr. po zmene hostname) – taký job nezálohuje nič,
- hook skript vzdump jobu, ktorý na hoste neexistuje (overí `backup-info/hook-scripts.txt`, pri starších archívoch odhad podľa obsahu archívu),
- všetky vzdump joby vypnuté (info, ak ich spúšťa vlastný systemd timer),
- najnovší archív ešte nebol stiahnutý cez prehliadač mimo servera (`downloaded_at` v histórii).

**Offline príručka** (`GET /api/recovery/handbook`) je jeden HTML súbor bez externých závislostí. Obsahuje postup obnovy od A po Z s hodnotami tvojho hosta odvodenými z najnovšieho archívu a nastavení appky: hostname, management IP, bridge/VLAN a šablónu `/etc/network/interfaces`, NAS automounty s príkazmi na ručný mount, backup storage, vzdump joby, zoznam hostí so stavom zálohy, VMID a sieť LXC s appkou (podľa vlastnej IP). Pridá aj riziká, klasifikáciu položiek a celú wiki. Neobsahuje heslá ani obsah citlivých súborov. Hodnoty kľúčov ako `password` v `storage.cfg` sú zamaskované. Po zmene prostredia (IP, VLAN, NAS, VMID) si príručku stiahni znova.

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

**DR metadata snapshot:** každá záloha pridá do `backup-info/` výstupy `pveversion -v`, `hostname`, `uname -a`, `lscpu`, `ip -br link`, `ip -br addr`, `ip addr`, `ip route`, `bridge link`, `lsblk -f`, `blkid`, `/dev/disk/by-id`, `findmnt`, `df -h`, `pvesm status`, `pvs`, `vgs`, `lvs`, `zpool status`, `zfs list`, `lspci -nn`, `systemctl --failed`, `qm list`, `pct list` a kontrolu hook skriptov vzdump jobov (`hook-scripts.txt`). Pridá aj `recovery-manifest.json` s klasifikáciou položiek. Ak príkaz na hoste neexistuje, zapíše sa chyba a záloha pokračuje. Snapshot je REFERENCE ONLY a nikdy sa neobnovuje.

DR metadáta položiek sú definované v `recovery_data.py` (kľúč = cesta z `DEFAULT_BACKUP_FILES`). Neklasifikovaná vlastná položka dostane bezpečný fallback REVIEW FIRST. Metadáta sa pripájajú iba k API odpovediam (`recovery` objekt), do `backup_config.json` sa neukladajú, preto sa `CONFIG_VERSION` nemení.

Hlavné záložky sú štyri: **Zálohovanie · Obnova a migrácia · História · Nastavenia**. Záložka **Obnova a migrácia** začína rozcestníkom **„Čo chceš urobiť?“** (Server zomrel – obnova · Plánovaná migrácia na nový HW · Obnoviť súbory na tom istom serveri). Všetky tri cesty sú **sprievodcovia krok za krokom** s rovnakým ovládaním: hore „krok X z Y“, rozbalený iba aktuálny krok a dole tlačidlá Späť / Ďalej (na mobile pripnuté k spodnému okraju), plus „← Späť na výber“. Obsah je viazaný na krok (zoznam hostí iba v kroku Presun hostí, porovnanie hostov v Príprave a Overení). Wiki, Položky zálohy a Snapshot hosta sú pod jedným odkazom **Wiki a referencie**.

**Obnova súborov** (bývalá samostatná záložka Obnova) má 4 kroky: **Archív** (predvolene najnovší) → **Čo obnoviť** → **Ako obnoviť** (Iba pripraviť na kontrolu / Aplikovať, potvrdenie kontroly REVIEW položiek) → **Potvrdenie** (plán, text OBNOVIT, výsledok a ďalší krok). Kroky obnovy po havárii ju otvárajú priamo s **predvybranými položkami** v režime Iba pripraviť (napr. krok 7: config.db, /etc/pve, hostname, hosts) a po dokončení vedú späť do postupu. V Histórii má každý dostupný archív tlačidlo **Obnoviť**. Bezpečnostné poistky obnovy sa nezmenili.

**Pripojenie nového hosta (migrácia vedľa starého, krok „Nový host“):** zadáš dočasnú IP, SSH port a root heslo nového Proxmoxu. Appka ich uloží do `migration_target.json` (0600, nikdy do `migration_state.json`) a na pozadí spustí **kontrolu nového hosta** (iba čítanie): SSH prihlásenie, beží Proxmox VE, **nie je to ten istý stroj ako starý** (`/etc/machine-id`), iná adresa ako starý host, beží `pve-cluster`, na novom hoste nebežia ani neexistujú hostia (pred prenosom konfigurácie 1:1 musí byť prázdny), verzia PVE nie je staršia. Operácie bežia na pozadí s priebehom a logom (`migration_jobs.json`); naraz smie bežať iba jedna, počas nej nejde reset migrácie ani zabudnutie hosta, a operácia bez heartbeatu (napr. po reštarte služby) sa označí ako prerušená. Heslá sa v logoch maskujú.

**Prenos 1:1, presun hostí a prepnutie (migrácia vedľa starého hosta).** Stratégia: nový Proxmox sa nainštaluje s **pôvodným hostname** (názov nodu) a **dočasnou IP**; starý host sa počas prenosu nemení a zostáva cestou späť.

**Poistky platné pre všetky zápisové operácie:** kontrola nového hosta musí byť úspešná (SSH, iný stroj, podporovaná verzia). Tesne pred každým zápisom appka znova overí, že starý a nový host majú rôzne `machine-id`, nový nemá staršiu major verziu PVE a starý host je samostatný node (nie cluster). Pri neistote (príkaz zlyhal, výstup chýba) sa nepokračuje. Prenos sa pri prvej operácii **pripne** na konkrétny starý a nový host a na konkrétny archív (vrátane SHA-256); zmena hostov, cieľa počas bežiacej operácie alebo archívu sa odmietne a vyžaduje reset migrácie. Pripnutý archív preto nesmie zmazať retencia, kým migrácia trvá.

1. **Prenos konfigurácie 1:1** (krok *Prenos konfigurácie 1:1*):
   - **config.db** – appka nahrá `config.db` z čerstvého archívu (max. 24 h) na nový host. Databáza v archíve sa lokálne skontroluje (platný SQLite, `integrity_check`, **zhoda s VM/LXC z `/etc/pve` v tom istom archíve**, archív nie je z clustra). Pôvodný config.db nového hosta sa zálohuje konzistentným SQLite snapshotom do `/root/pbm-migration/`, zastaví sa `pvescheduler`, `pve-firewall` a `pve-cluster` a databáza sa nahradí. Potom sa **vypnú a overia** vzdump joby (podľa živého zoznamu), autostart hostí a firewall datacentra. Ak zlyhá čokoľvek v ktorejkoľvek fáze, automaticky sa vráti pôvodná databáza. Podmienky: rovnaký hostname, nový host prázdny (bez VM/LXC), nie je v clustri; potvrdenie textom `KOPIA`. Joby, autostart a firewall sa zapnú až pri prepnutí.
   - **Súbory hosta** – náhľad rozdielov (hash súborov, pri malých necitlivých textových súboroch aj unified diff) a prenos vybraných položiek cez restore engine s pre-apply zálohou na novom hoste. Prenášať sa dajú iba položky, pre ktoré je načítaný rozdiel. Skopírované systemd timery sa na novom hoste vypnú a zapnú až pri prepnutí. Po prenose AutoFS máp sa `autofs` na novom hoste načíta (balík musíš nainštalovať ty). Ponúkajú sa iba položky z `MIGRATION_FILE_ITEMS` (autofs, skripty, systemd, sysctl, vzdump.conf; HW-závislé a cron/`/root` predvolene nevybraté).
   - **Návrh siete** – pôvodný `/etc/network/interfaces` s premapovanými sieťovkami (starý NIC → nový NIC; náhrada je simultánna, takže výmena A↔B funguje; duplicitné alebo kolidujúce mapovanie sa odmietne) a pôvodný `/etc/hosts` sa uložia na nový host do `/root/pbm-migration/*.proposed`; neaplikujú sa (zmena siete by prerušila SSH), použijú sa pri prepnutí z konzoly.
2. **Presun hostí** – „Presunúť automaticky“ pri každom hosťovi: appka overí, že hosť nemá disky na **zdieľanom alebo nejasnom storage** (NFS/CIFS/Ceph/iSCSI, `shared 1`, adresár na sieťovom súborovom systéme – obnova s `--force` by inak mohla odstrániť pôvodné disky; takýto hosť sa odmietne a presúva sa ručne) a že zdieľaný adresár záloh je **naozaj ten istý export** na oboch hostoch (marker súbor). Potom na starom vypne autostart (pôvodná hodnota sa zachytí iba raz), korektne vypne hosťa, `vzdump --mode stop` do zdieľaného adresára, overí, že hosť ostal vypnutý, a na novom `qmrestore`/`pct restore --force` (config už prišiel s config.db); hosť ostane vypnutý. „Spustiť na novom“ ho spustí iba ak je na starom vypnutý. „Vrátiť na starý“ (aj z Overený) spustí starú kópiu až po **potvrdení, že nová je vypnutá**; hosť dostane stav *Vrátený na starý*, ktorý nesplní podmienku prepnutia – treba ho znova presunúť alebo vedome vynechať. **LXC s touto appkou** sa zisťuje podľa IP, MAC adresy a hostname (funguje aj pri DHCP) a automaticky sa nepresúva; ak ho appka nevie spoľahlivo určiť, vyžiada si výslovné potvrdenie.
3. **Prepnutie** – automaticky (potvrdenie `PREPNUT`, iba keď sú všetci hostia okrem appky overení alebo vedome vynechaní): na starom vypne backup timery a vzdump joby a **overí, že sú naozaj vypnuté** – ak nie, nový plánovač nezapne. Potom na novom zapne joby, timery a vráti autostart skutočne presunutým hosťom; čiastočné zlyhanie sa nehlási ako úspech a prepnutie sa dá zopakovať. Sprievodca potom vypíše **ručné kroky z konzoly** s konkrétnymi hodnotami (a upozorní, ktorí hostia na novom hoste nie sú): vzdump LXC s appkou na starom → `poweroff` starého → nový host prevezme pôvodnú IP (návrh siete + hosts, `ifreload -a`, `pvecm updatecerts`) → obnova a štart LXC s appkou → zapnutie firewallu → v appke Test SSH a nová záloha. Tento postup je aj v offline príručke (stiahni ju tesne pred vypnutím appky).

Stav prenosu je v `migration_transfer.json` (0600; pripnutá relácia, config.db, súbory, sieť, presunutí hostia, prepnutie). Všetky príkazy na hostoch sú pevné buildery s `shlex.quote`; API neprijíma ľubovoľné príkazy. Dlhé operácie (vzdump/restore) bežia na pozadí s priebežným logom. Reset migrácie zmaže aj stav prenosu (hostov nemení). Spôsob „Presun systémového disku“ zostáva ručný.

Postup obnovy po havárii sa ukladá na serveri v `recovery_progress.json` (0600, mimo git; pomocný `.lock`), takže v ňom pokračuješ z mobilu aj z PC. Starý postup uložený v prehliadači sa pri prvom načítaní jednorazovo prenesie na server. Aktuálne otvorený krok si pamätá iba prehliadač.

Nové API endpointy: `GET /api/recovery/overview` (vrátane `risks` a `checklist_progress`), `GET /api/recovery/checklist` (kroky + uložený postup), `POST /api/recovery/checklist/steps/<id>` (`{"completed": bool}`), `POST /api/recovery/checklist/reset`, `GET|POST|DELETE /api/recovery/migration/target` (pripojenie nového hosta; POST spustí kontrolu), `POST /api/recovery/migration/target/check`, `GET /api/recovery/migration/jobs`, `GET /api/recovery/migration/jobs/<id>`, `GET /api/recovery/migration/transfer`, `POST /api/recovery/migration/transfer/{config-db,files-diff,files-apply,network}`, `POST /api/recovery/migration/guests/<vmid>/{move,start,rollback}`, `POST /api/recovery/migration/cutover`, `GET /api/recovery/wiki`, `GET /api/recovery/wiki/<slug>`, `GET /api/recovery/snapshot/<backup_id>`, `GET /api/recovery/handbook` (`?inline=1` na zobrazenie v prehliadači). `POST /api/restore` po novom prijíma aj `stage_paths` a `acknowledged_paths`.

### 💾 Ukladanie archívov

Každá úspešne vytvorená záloha sa uloží lokálne do `backups/` v LXC a následne sa nahrá na FTP. Ak FTP upload zlyhá, lokálny archív ostane v LXC a história záloh označí FTP stav ako `failed`.

- **Záloha bez vybraných dát nie je úspech.** Ak neexistuje žiadna zo zvolených ciest, archív sa zmaže, nenahrá a retencia sa nespustí, takže nevytlačí poslednú dobrú zálohu.
- **`config.db` sa zálohuje konzistentne** cez SQLite backup API (aj pri živej databáze v režime WAL), nie kopírovaním súboru. Na Proxmox hoste preto musí byť `python3` (v Proxmoxe je predvolene).
- **Retencia** (`max_backup_count`) počíta zjednotený inventár: lokálne archívy, záznamy známe na FTP aj archívy iba na FTP. Najstaršie nadlimitné sa mažú lokálne aj z FTP; pri výpadku FTP sa varuje a lokálne zálohy sa navyše nemažú.
- História záloh (`backup_history.json`) sa zapisuje atomicky pod zámkom, takže súbežné zálohy nestrácajú záznamy. SSH stream sa ukladá do `.part` súboru a finalizuje sa až po úspechu.

### 🔄 Automatické zálohovanie
- **Denne, týždenne alebo mesačne** - podľa nastavenia v sekcii Automatická záloha
- Vyžaduje systemd timer `proxmox-backup-auto.timer` (pridáva sa automaticky pri inštalácii/update)
- Timer spúšťa `auto_backup.sh` na štvrťhodinách `:00/:15/:30/:45`; skript podľa `backup_config.json` rozhodne, či je automatická záloha zapnutá a či už nastal uložený deň/čas
- `auto_backup.sh` volá JSON API `/api/backup/auto` zo saved configu a pri zapnutom logine používa servisný token z `auth_config.json`, takže nepotrebuje browser session

### Plánovaná migrácia na nový HW

Podsekcia **Obnova a migrácia → Migrácia na nový HW** vedie migráciu živého starého servera. Vyber **presun systémového disku** (`disk_move`) alebo **nový host vedľa starého** (`side_by_side`). Kroky vychádzajú z existujúcej wiki „Migrácia na nový HW (plánovaná)“. Cluster a `qm remote-migrate` sú iba odkazy vo wiki. Pri `disk_move` príkazy vykonáva administrátor ručne a kroky evidujú jeho potvrdenia. Pri `side_by_side` appka po potvrdení vykoná vybrané kroky sama cez SSH pevnými príkazmi (prenos konfigurácie, presun hostí, prepnutie; popis vyššie). Samostatné porovnanie hostov načíta cez SSH iba diagnostiku podľa pevného read-only zoznamu.

Pri `side_by_side` sa VM/LXC a priradenie vzdump jobov čítajú z najnovšieho **lokálneho** archívu rovnakými whitelisted parsermi ako Riziká obnovy. FTP archív najprv načítaj lokálne v Histórii. Priradený job nie je dôkaz úspešnej zálohy; bez jobu je nutný ručný vzdump. Pôvodní hostia ostanú v uloženom inventári aj po zálohe nového hosta. Neúplný alebo nedostupný inventár zobrazí upozornenie.

Stavy hosťa: `pending → stopped_on_old → restored_on_new → verified`, vynechanie `pending/stopped_on_old → skipped`, návrat `skipped → pending`. Automatický rollback nastaví `rolled_back` (hosť beží na starom; ďalej `pending` alebo `skipped`). Poznámku možno upraviť aj bez zmeny stavu. Pred vzdump hosťa samostatne vypni a over `stopped` pred aj po zálohe: `--mode stop` môže pôvodne bežiacu VM znovu spustiť. Na novom hosťa spusti až po overení, že stará kópia nebeží. Nikdy dva hosty s rovnakou IP/hostname/SSH identitou naraz; joby a backup timery smú zapisovať/prune mazať zálohy iba na jednom hoste. UI aj automatické prepnutie (`/cutover`) vyžadujú dostupný inventár a každého hosťa `verified` alebo `skipped`; samotné ručné odškrtnutie kroku v API je len evidencia rozhodnutia administrátora.

Pri `side_by_side` pôvodné disky/configy hostí na starom hoste zostávajú; restore vzdump vytvorí kópiu na novom. Starú kópiu nechaj vypnutú. Dáta zmenené na novom sa do pôvodnej kópie nesynchronizujú, preto pri návrate naplánuj aj prenos aktuálnych dát. Sprievodca pôvodné dáta nemaže. Pri `disk_move` presúvaš fyzický disk, takže starý stroj nezachová samostatnú kópiu.

`migration_state.json` je samostatný lokálny runtime súbor s právami **0600**, mimo git aj `backup_config.json`. Schéma `version: 1` obsahuje `method`, `old_host/new_host: {ip, hostname}`, `steps: {id: {completed, updated_at}}`, `guests: {vmid: {type, name, status, note, updated_at}}`, `started_at`, `updated_at`, `finished_at`. Časy obsahujú časové pásmo Europe/Bratislava. Dokončenie všetkých krokov nastaví `finished_at`; odznačenie kroku ho vymaže. Zmena spôsobu vyžaduje reset. Nevkladaj heslá ani tajomstvá do poznámok; API na uloženie stavu neprijíma polia pre prihlasovacie údaje.

Zápis používa dočasný súbor s 0600, fsync a `os.replace`. Pomocný `migration_state.json.lock` (0600, mimo git) serializuje zmeny medzi gunicorn workermi. Poškodený stav sa potichu neprepisuje; API vráti 503 a explicitný reset ho môže obnoviť. Offline príručka pri začatej migrácii pridá snapshot krokov, údajov hostov a stavov/poznámok hostí.

Všetky endpointy vyžadujú login a POST aj CSRF:

| Metóda | Endpoint | Telo / výsledok |
|---|---|---|
| GET | `/api/recovery/migration` | Stav, metódy, kroky, hostia, inventár, riziká a `cutover_ready` |
| POST | `/api/recovery/migration` | `{method, old_host: {ip, hostname}, new_host: {ip, hostname}}` |
| POST | `/api/recovery/migration/steps/<step_id>` | `{completed: boolean}` |
| POST | `/api/recovery/migration/guests/<vmid>` | `{status, note}` (poznámka max. 2000 znakov) |
| POST | `/api/recovery/migration/reset` | `{}`; UI vyžaduje potvrdenie |
| POST | `/api/recovery/migration/compare` | `{new_host: {host, port: 22, password}}`; nový používateľ je vždy root, heslo jednorazové |

**Porovnanie hostov (Fáza 2)** používa starý SSH cieľ a prihlasovacie údaje z uložených Nastavení (`remote_ssh`). Nový cieľ (IPv4/IPv6 alebo hostname), port a jednorazové root heslo zadáš iba pre porovnanie. UI heslo ihneď vymaže; server ho neukladá do konfigurácie, migračného stavu ani reportu. Report zostáva iba v aktuálnom UI a pri zmene cieľov/nastavení alebo resete sa vymaže. Do offline príručky sa neukladá.

Na oboch hostoch sa spúšťa pevný read-only zoznam s `LC_ALL=C`: `hostname`, `pveversion -v`, `pvesm status`, `ip -br link`, `ip -br addr`, `cat /etc/network/interfaces`, `lscpu`, `qm list`, `pct list` a `systemctl list-timers --all --no-pager --no-legend`. Žiadne SFTP, restore, štartovanie, zastavovanie ani zápisové príkazy. Čítanie stdout/stderr má spoločný limit 64 KiB na príkaz, časový limit 10 s na príkaz a 60 s na zber po pripojení každého hosta. SSH klient aj kanály sa uzatvoria pri úspechu aj chybe. Surové výstupy, stderr a texty SSH výnimiek sa do API neposielajú; report obsahuje iba parsované fakty a bezpečné chyby.

Report obsahuje `compared_at`, `read_only`, `old_host/new_host` (cieľ, stav pripojenia, úplnosť, chyby a fakty), `rows` s úrovňami `ok/info/warning/error/unknown` a `summary` s ich počtami. Kontroluje PVE verziu, chýbajúce/neaktívne storage ID, bridge/VLAN, CPU vendor/flags, NIC a IP, inventár hostí a timery. Rovnaké bežiace VMID na oboch hostoch (aj naprieč VM/LXC typmi) je chyba. Výpadok pripojenia alebo príkazu znamená čiastočný report a `unknown`, nie úspešné overenie. Sieťové `source/source-directory` sa nerozbaľujú, takže zahrnuté bridge/VLAN definície treba overiť ručne. Zber nie je simultánny ani priebežný; porovnanie nemení uložené stavy sprievodcu. Vzdump joby, SSH host keys a obsah storage ešte over ručne.

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
- `migration_state.json` - Lokálny postup plánovanej migrácie (0600; pomocný `.lock`, bez prihlasovacích údajov)
- `recovery_progress.json` - Lokálny postup obnovy po havárii (0600; pomocný `.lock`)
- `migration_target.json` - Pripojenie nového hosta počas migrácie: adresa, port, root heslo a výsledok kontroly (0600; pomocný `.lock`). Heslo sa nikdy nevracia cez API, pri dokončení migrácie sa zmaže, pri resete sa zmaže celý súbor
- `migration_jobs.json` - Posledných 20 operácií migrácie na pozadí so stavom a logom, bez hesiel (0600; pomocný `.lock`)
- `migration_transfer.json` - Stav prenosu 1:1: pripnutá relácia (starý a nový host, archív so SHA-256), presunutí hostia a prepnutie (0600; pomocný `.lock`)
- `backup_history.json` - História záloh; zapisuje sa atomicky pod zámkom `backup_history.json.lock` (prázdny pomocný súbor, mimo git, nemaž ho počas behu služby)
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
venv/bin/python tests/test_audit_fixes.py
bash -n install_in_lxc.sh
bash -n update.sh
bash -n auto_backup.sh
bash -n test.sh
```

Voliteľná mobile/desktop UI kontrola (360–1280 px, horizontálny overflow, JS chyby, únik tajomstiev) cez Playwright: návod je v hlavičke `tests/ui_mobile_check.py`.
