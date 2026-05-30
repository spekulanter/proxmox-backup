# AGENTS.md

Pokyny pre agentov pracujúcich na projekte **Proxmox Backup Manager**.

> Tento súbor je autoritatívny zdroj pokynov. `CLAUDE.md` naň odkazuje a dopĺňa ho.

## Kontext projektu

Proxmox Backup Manager je jednoduchá Flask webová aplikácia na správu a automatizáciu záloh Proxmox VE serverov. Umožňuje nastaviť FTP server, vybrať kritické súbory a adresáre, spustiť manuálnu zálohu a sledovať históriu záloh.

Projekt je určený hlavne na beh v LXC kontajneri na Proxmoxe, typicky ako root služba cez systemd a gunicorn.

## Technologický stack

- Backend: Python 3, Flask
- Frontend: samostatné `templates/index.html` rozhranie s Tailwind CDN štýlom
- Persistencia: lokálne JSON súbory (`backup_config.json`, `backup_history.json`)
- SSH zdroj zálohy: `paramiko` (remote_ssh mode — appka beží v LXC, číta Proxmox host cez SSH)
- Deployment: `install_in_lxc.sh`, `update.sh`, systemd, gunicorn
- Automatizácia záloh: `auto_backup.sh`

## Dôležité súbory

- `app.py` - hlavná Flask aplikácia a API endpointy (2000+ riadkov, single-file)
- `templates/index.html` - webové rozhranie aplikácie
- `requirements.txt` - Python závislosti (Flask, Werkzeug, gunicorn, paramiko)
- `install_in_lxc.sh` - idempotentný inštalačný/update skript pre LXC
- `update.sh` - manuálny update nainštalovanej aplikácie
- `auto_backup.sh` - spúšťač automatickej zálohy
- `test.sh` - rýchly smoke test služby a HTTP endpointov
- `tests/test_archive.py` - rozsiahly unit/integration test (archív, SSH, FTP, restore, API)
- `backup_config.json` - lokálna runtime konfigurácia, obsahuje citlivé údaje
- `backup_history.json` - lokálna runtime história záloh, vytvára sa aplikáciou
- `backups/` - lokálny adresár pre uložené `.tar.gz` archívy (chmod 700, mimo git)

## Produktové očakávania

Aplikácia má zostať ľahká, praktická a zrozumiteľná pre administrátora Proxmox servera.

Hlavné funkcie:

- konfigurácia FTP pripojenia
- konfigurácia zdroja zálohy: `remote_ssh` (LXC číta Proxmox cez SSH) alebo `local` (appka beží priamo na hosta)
- test FTP aj SSH pripojenia pred uložením alebo zálohou
- kategorizovaný výber súborov a adresárov na zálohovanie (6 kategórií)
- samostatný výber položiek pre manuálnu a automatickú zálohu (`backup_files` vs. `auto_backup_files`)
- manuálne vytvorenie `.tar.gz` archívu (lokálne alebo streamom cez SSH)
- generovanie `backup-info/` inventára a `README-RESTORE.txt`
- upload zálohy na FTP
- lokálna história záloh s FTP sync stavom; pri dočasnom FTP výpadku ostane archív lokálne a pri ďalšej dostupnosti FTP sa chýbajúce lokálne archívy dohrávajú automaticky
- zjednotený prehľad záloh z lokálnej histórie aj FTP servera; FTP výpadok nesmie rozbiť zobrazenie lokálnych záloh
- browser download archívov; FTP-only archív sa pred downloadom, preview alebo restore najprv stiahne do lokálneho `backups/` cache
- retencia záloh cez `max_backup_count` - spoločný limit pre lokálny archív aj FTP, najstaršie nadlimitné zálohy sa mažú z oboch úložísk
- mazanie zálohy lokálne aj z FTP
- **restore workflow**: preview obsahu archívu → výber ciest → bezpečný restore na Proxmox host cez SSH (whitelist, staging, pre-apply záloha)
- týždenné alebo mesačné automatické zálohovanie
- záloha AUTO.FS/QNAP/WD konfigurácie

Pri úpravách preferuj spoľahlivosť a jasné chybové hlášky pred vizuálnymi efektmi alebo veľkými refaktormi.

## Bezpečnostné pravidlá

- Necommituj reálne FTP heslá, IP adresy zákazníkov ani produkčný obsah `backup_config.json`.
- `backup_config.json` a `backup_history.json` považuj za runtime dáta.
- Pri zmene backup logiky dávaj pozor na prácu s absolútnymi cestami ako `/etc/pve/`, `/root/` a `/var/lib/vz/template/`.
- Pri práci so súbormi používaj bezpečné dočasné cesty a po dokončení ich uprac.
- Do archívu nikdy nepridávaj runtime/mount cesty ako `/mnt`, `/media`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, `/var/tmp`, `/var/cache`, `/var/log` a `/lost+found`.
- Ak meníš update alebo install skripty, mysli na to, že môžu bežať ako root.
- Nepoužívaj deštruktívne príkazy typu `git reset --hard`, `rm -rf` alebo prepis systemd súborov mimo jasne očakávaného inštalačného toku.

## Vývojové zásady

- Zachovaj jednoduchú single-app architektúru, pokiaľ úloha výslovne nevyžaduje väčšie delenie.
- Preferuj malé, čitateľné zmeny pred plošným refaktorom.
- API odpovede vracaj ako JSON a používateľské akcie vo webovom rozhraní komunikuj jasnými stavmi.
- Ak pridáš novú konfiguráciu, zahrň jej predvolenú hodnotu do `default_config()` aj `migrate_config()`.
- Ak meníš štruktúru `backup_config.json`, zvýš `CONFIG_VERSION` a aktualizuj `migrate_config()`.
- `backup_files` je ručný výber a `auto_backup_files` je samostatný výber pre automatické zálohy; nespájaj ich späť do jedného stavu.
- `max_backup_count` je spoločná retencia pre lokálne archívy aj FTP. Pri zmene retenčnej logiky zachovaj best-effort FTP mazanie a varovania namiesto pádu úspešne vytvorenej lokálnej zálohy.
- Pri FTP výpadku musí lokálne vytvorená záloha zostať v histórii s FTP stavom a neskorší backup má skúsiť chýbajúce lokálne archívy dohrať na FTP.
- FTP-only archívy z `/api/backups` sú virtuálne, kým ich používateľ nestiahne, nenačíta lokálne alebo nepoužije na restore; vtedy sa bezpečne cacheujú do `BACKUP_STORAGE_DIR`.
- Pri restore platí: ak existuje lokálna kópia, používa sa lokálna kópia; FTP sa použije iba ako zdroj na dotiahnutie chýbajúceho archívu do lokálneho cache.
- Ak pridáš nový runtime súbor, aktualizuj dokumentáciu a `.gitignore`, ak má zostať lokálny.
- Slovenské texty v UI a hláškach drž konzistentné.
- Legacy form-based routes (`/toggle_file`, `/create_backup`, `/save_ftp_config` atď.) sú zachované pre kompatibilitu; nové funkcie pridávaj cez `/api/*` endpointy.
- Restore je povolený iba na cesty z `restore_whitelist_paths()` (odvodené z `DEFAULT_BACKUP_FILES` bez wildcardov a exclude-ciest).

## UI smerovanie

Rozhranie má pôsobiť ako administrátorský nástroj: čisté, vecné, prehľadné a použiteľné na mobile aj desktope.

Preferované prvky:

- karty alebo sekcie pre nastavenia a stav
- jasné primary akcie pre spustenie zálohy
- checkboxy pre výber súborov
- progress alebo loading stav pri dlhších operáciách
- alerty pre úspech, varovanie a chybu
- ikony iba tam, kde zlepšujú rýchlu orientáciu

Vyhni sa marketingovej landing page. Prvá obrazovka má byť samotná pracovná aplikácia.

## Testovanie

Po backend zmenách spusti aspoň:

```bash
venv/bin/python -m py_compile app.py
venv/bin/python tests/test_archive.py
```

Ak je služba dostupná v cieľovom prostredí, použi:

```bash
./test.sh
```

Pri zmenách install/update skriptov skontroluj shell syntax:

```bash
bash -n install_in_lxc.sh
bash -n update.sh
bash -n auto_backup.sh
bash -n test.sh
```

Pri zmenách frontendu over, že hlavná stránka stále obsahuje názov aplikácie a vie volať existujúce API endpointy.

## API endpointy (prehľad)

| Endpoint | Metóda | Popis |
|----------|--------|-------|
| `/api/config` | GET | Konfigurácia + história záloh + štatistiky |
| `/api/files` | GET | Zoznam súborov na zálohovanie |
| `/api/files/<idx>/toggle` | POST | Prepnutie výberu jedného súboru |
| `/api/files/selection` | POST | Hromadný výber/odznačenie |
| `/api/auto-files` | GET | Zoznam súborov pre automatické zálohovanie |
| `/api/auto-files/<idx>/toggle` | POST | Prepnutie výberu jednej položky automatickej zálohy |
| `/api/auto-files/selection` | POST | Hromadný výber/odznačenie automatickej zálohy |
| `/api/settings` | POST | Uloženie FTP a source konfigurácie |
| `/api/test-ftp` | POST | Test FTP pripojenia |
| `/api/test-ssh` | POST | Test SSH pripojenia na Proxmox host |
| `/api/backup` | POST | Spustenie zálohy |
| `/api/backup/auto` | POST | Spustenie automatickej zálohy podľa `auto_backup_files` |
| `/api/backups` | GET | Zjednotený zoznam lokálnych a FTP archívov vrátane dostupnosti úložísk |
| `/api/backups/<id>/cache` | POST | Stiahnutie FTP-only archívu do lokálneho cache |
| `/api/backups/<id>/download` | GET | Stiahnutie archívu cez browser; FTP-only archív sa najprv cacheuje lokálne |
| `/api/backups/<id>` | DELETE | Zmazanie zálohy (lokálne + FTP + história) |
| `/api/restore/archives` | GET | Archívy dostupné na restore lokálne alebo cez FTP cache-on-demand |
| `/api/restore/preview/<id>` | GET | Preview obnoviteľných ciest v archíve |
| `/api/restore/preview/<id>/members` | GET | Detail členov archívu (`?path=...` alebo všetky) |
| `/api/restore` | POST | Spustenie restore (vyžaduje `confirm: "OBNOVIT"`) |

## Subagenti

Projekt má odporúčané subagent profily v `.codex/agents/`:

- `backend-flask` - Flask API, konfigurácia a backup workflow
- `frontend-ui` - single-file UI v `templates/index.html`
- `lxc-deploy` - install/update skripty, systemd a LXC prevádzka
- `backup-security` - správnosť záloh, FTP a citlivé údaje
- `qa-reviewer` - review, smoke testy a regresné riziká

Pri väčšej úlohe použi najmenší praktický počet subagentov. Nepúšťaj viac agentov na rovnaký zápisový rozsah naraz.

## Nasadenie a prevádzka

Predvolená služba:

- názov služby: `proxmox-backup.service`
- pracovný adresár: `/opt/proxmox-backup` (v repozitári kód leží v `/opt`, nie v podadresári)
- port: `5000`
- produkčný server: gunicorn (2 workers, timeout 7200s kvôli dlhým SSH streamom)
- env premenná: `BACKUP_STORAGE_DIR` (predvolene `backups/` relatívne k working directory)

Užitočné prevádzkové príkazy:

```bash
systemctl status proxmox-backup.service
systemctl restart proxmox-backup.service
journalctl -u proxmox-backup.service -f
/opt/proxmox-backup/update.sh
```

## Poznámky pre budúcich agentov

- V pracovnom strome môžu byť lokálne zmeny používateľa. Pred úpravami skontroluj stav a neprepisuj cudzie zmeny.
- Tento projekt môže bežať priamo v `/opt`; nepredpokladaj vždy štandardný repozitár v domovskom adresári.
- Ak `rg` nie je dostupné, použi `find`, `grep` alebo bežné shell nástroje.
- `app.py` má 2000+ riadkov — pred väčšou zmenou si prečítaj relevantnú sekciu, nie len prvých 100 riadkov.
- Remote SSH záloha streamuje `tar` výstup cez paramiko do lokálneho súboru — timeout gunicornu 7200s je zámerný.
- `auto_backup.sh` má volať `/api/backup/auto`, aby automatické zálohy používali samostatný výber `auto_backup_files`.
- História má zachovať lokálne existujúce archívy aj pri chýbajúcom FTP súbore, inak by ich neskorší FTP dosync nevedel nájsť.
- Restore má vlastný whitelist (`restore_whitelist_paths`), staging adresár a pre-apply zálohu na hostovi — neobchádzaj tieto kroky.
- `CLAUDE.md` je alias/doplnok tohto súboru pre Claude-based agenty.
- Dokument `AGENTS.md` je autoritatívny zdroj pokynov pre prácu v tomto repozitári.
