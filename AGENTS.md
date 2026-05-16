# AGENTS.md

Pokyny pre agentov pracujúcich na projekte **Proxmox Backup Manager**.

## Kontext projektu

Proxmox Backup Manager je jednoduchá Flask webová aplikácia na správu a automatizáciu záloh Proxmox VE serverov. Umožňuje nastaviť FTP server, vybrať kritické súbory a adresáre, spustiť manuálnu zálohu a sledovať históriu záloh.

Projekt je určený hlavne na beh v LXC kontajneri na Proxmoxe, typicky ako root služba cez systemd a gunicorn.

## Technologický stack

- Backend: Python 3, Flask
- Frontend: samostatné `index.html` rozhranie s Bootstrap štýlom
- Persistencia: lokálne JSON súbory
- Deployment: `install_in_lxc.sh`, `update.sh`, systemd, gunicorn
- Automatizácia záloh: `auto_backup.sh`

## Dôležité súbory

- `app.py` - hlavná Flask aplikácia a API endpointy
- `index.html` - webové rozhranie aplikácie
- `requirements.txt` - Python závislosti
- `install_in_lxc.sh` - idempotentný inštalačný/update skript pre LXC
- `update.sh` - manuálny update nainštalovanej aplikácie
- `auto_backup.sh` - spúšťač automatickej zálohy
- `test.sh` - rýchly smoke test služby a HTTP endpointov
- `backup_config.json` - lokálna runtime konfigurácia, obsahuje citlivé údaje
- `backup_history.json` - lokálna runtime história záloh, vytvára sa aplikáciou

## Produktové očakávania

Aplikácia má zostať ľahká, praktická a zrozumiteľná pre administrátora Proxmox servera.

Hlavné funkcie:

- konfigurácia FTP pripojenia
- test FTP pripojenia pred uložením alebo zálohou
- výber súborov a adresárov na zálohovanie
- manuálne vytvorenie `.tar.gz` archívu
- upload zálohy na FTP
- lokálna história záloh
- týždenné alebo mesačné automatické zálohovanie

Pri úpravách preferuj spoľahlivosť a jasné chybové hlášky pred vizuálnymi efektmi alebo veľkými refaktormi.

## Bezpečnostné pravidlá

- Necommituj reálne FTP heslá, IP adresy zákazníkov ani produkčný obsah `backup_config.json`.
- `backup_config.json` a `backup_history.json` považuj za runtime dáta.
- Pri zmene backup logiky dávaj pozor na prácu s absolútnymi cestami ako `/etc/pve/`, `/root/` a `/var/lib/vz/template/`.
- Pri práci so súbormi používaj bezpečné dočasné cesty a po dokončení ich uprac.
- Ak meníš update alebo install skripty, mysli na to, že môžu bežať ako root.
- Nepoužívaj deštruktívne príkazy typu `git reset --hard`, `rm -rf` alebo prepis systemd súborov mimo jasne očakávaného inštalačného toku.

## Vývojové zásady

- Zachovaj jednoduchú single-app architektúru, pokiaľ úloha výslovne nevyžaduje väčšie delenie.
- Preferuj malé, čitateľné zmeny pred plošným refaktorom.
- API odpovede vracaj ako JSON a používateľské akcie vo webovom rozhraní komunikuj jasnými stavmi.
- Ak pridáš novú konfiguráciu, zahrň jej predvolenú hodnotu do `load_config()`.
- Ak pridáš nový runtime súbor, aktualizuj dokumentáciu a `.gitignore`, ak má zostať lokálny.
- Slovenské texty v UI a hláškach drž konzistentné.

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
python -m py_compile app.py
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

## Nasadenie a prevádzka

Predvolená služba:

- názov služby: `proxmox-backup.service`
- pracovný adresár: `/opt/proxmox-backup`
- port: `5000`
- produkčný server: gunicorn

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
- Dokument `AGENTS.md` je zdroj pokynov pre prácu v tomto repozitári; pôvodný produktový brief bol zredukovaný do týchto praktických pravidiel.
