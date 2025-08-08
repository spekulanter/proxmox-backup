# Proxmox Backup Manager

Jednoduchá webová aplikácia v Pythone (Flask) pre správu záloh Proxmox VE serverov.

## Funkcie

- **Manuálne zálohovanie** - Vytvorenie zálohy na požiadanie
- **Automatické zálohovanie** - Naplánované zálohy (týždenne/mesačne)
- **FTP Upload** - Nahrávanie záloh na FTP server
- **Výber súborov** - Konfigurovateľný výber súborov na zálohovanie
- **História záloh** - Prehľad vytvorených záloh
- **Test pripojenia** - Overenie FTP nastavení

## Inštalácia

1. Nainštalujte Python 3.7+
2. Nainštalujte závislosti:
   ```bash
   pip install -r requirements.txt
   ```

## Spustenie

```bash
python app.py
```

Aplikácia bude dostupná na `http://localhost:5000`

## Konfigurácia

### FTP Server
Nastavte FTP server v sekcii "Nastavenia":
- IP adresa/hostiteľ
- Port (predvolene 21)
- Používateľské meno
- Heslo

### Súbory na zálohovanie

Aplikácia má predkonfigurované kľúčové Proxmox súbory:

**Kritické súbory:**
- `/etc/pve/` - Hlavná konfigurácia Proxmox
- `/etc/network/interfaces` - Sieťová konfigurácia

**Ostatné dôležité súbory:**
- `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` - Systémové nastavenia
- `/etc/ssl/pve/` - SSL certifikáty
- `/root/` - Skripty administrátora
- `/etc/cron*` - Cron úlohy
- `/etc/vzdump.conf` - Vzdump konfigurácia

**Voliteľné (veľké súbory):**
- `/var/lib/vz/template/` - ISO obrazy a šablóny

## Bezpečnosť

- Zmente `secret_key` v `app.py` pre produkčné použitie
- Zabezpečte prístup k aplikácii (firewall, VPN)
- Používajte silné FTP heslá
- Pravidelně kontrolujte vytvorené zálohy

## Súbory

- `app.py` - Hlavná Flask aplikácia
- `templates/index.html` - Webové rozhranie
- `backup_config.json` - Konfiguračné nastavenia (vytvorí sa automaticky)
- `backup_history.json` - História záloh (vytvorí sa automaticky)

## Poznámky

- Aplikácia vytvára komprimované tar.gz archívy
- Zálohy sa nahrávajú na FTP server pre bezpečné uloženie
- História záloh sa ukladá lokálne
- Automatické zálohy vyžadujú externý cron alebo systemd timer