# Proxmox Backup Manager - Správca Záloh Proxmox

Webová aplikácia pre správu a automatizáciu záloh Proxmox VE serverov s podporou FTP.

**Experience Qualities**: 
1. Jednoduchosť - Intuitívne ovládanie pre rýchle vytvorenie záloh
2. Spoľahlivosť - Jasné zobrazenie stavu záloh a chybových hlásení  
3. Prehľadnosť - Organizované zobrazenie konfiguračných súborov a nastavení

**Complexity Level**: Light Application (multiple features with basic state)
- Aplikácia obsahuje viacero funkcií ako správa FTP nastavení, výber súborov na zálohovanie, manuálne aj automatické zálohy, ale bez pokročilých používateľských účtov

## Essential Features

### Konfigurácia FTP Servera
- **Functionality**: Nastavenie FTP pripojenia (IP adresa, používateľské meno, heslo, port)
- **Purpose**: Umožniť bezpečné ukladanie záloh na vzdialený server
- **Trigger**: Kliknutie na "Nastavenia FTP" v hlavnom menu
- **Progression**: Otvorenie formulára → Vyplnenie údajov → Test pripojenia → Uloženie nastavení
- **Success criteria**: Úspešné test pripojenie a zobrazenie potvrdenia

### Výber Súborov na Zálohovanie
- **Functionality**: Checkbox zoznam kritických Proxmox súborov a adresárov
- **Purpose**: Umožniť prispôsobenie obsahu zálohy podľa potrieb
- **Trigger**: Zobrazenie na hlavnej stránke
- **Progression**: Zobrazenie zoznamu → Označenie požadovaných položiek → Automatické uloženie výberu
- **Success criteria**: Výber sa uloží a zobrazí sa pri ďalšom načítaní

### Manuálne Zálohovanie
- **Functionality**: Okamžité vytvorenie a odoslanie zálohy na FTP
- **Purpose**: Umožniť zálohovanie pred kritickými zmenami
- **Trigger**: Kliknutie na "Vytvoriť zálohu teraz"
- **Progression**: Kliknutie → Zobrazenie pokroku → Komprimácia súborov → Upload na FTP → Potvrdenie úspechu
- **Success criteria**: Záloha sa úspešne vytvorí a nahrá na FTP server

### Automatické Zálohovanie
- **Functionality**: Nastavenie pravidelných záloh (týždenne/mesačne)
- **Purpose**: Zabezpečiť pravidelnú ochranu dát bez manuálneho zásahu
- **Trigger**: Nastavenie v konfigurácii plánovača
- **Progression**: Výber frekvencie → Nastavenie času → Aktivácia → Zobrazenie ďalšej naplánovanej zálohy
- **Success criteria**: Zálohy sa automaticky vytvárajú podľa nastaveného rozvrhu

### História Záloh
- **Functionality**: Zobrazenie zoznamu vytvorených záloh s dátumom a veľkosťou
- **Purpose**: Sledovanie úspešnosti záloh a správa starších verzií
- **Trigger**: Automatické načítanie pri otvorení aplikácie
- **Progression**: Načítanie FTP → Zobrazenie zoznamu → Možnosť stiahnutia/vymazania
- **Success criteria**: Zobrazenie kompletného zoznamu záloh s možnosťami správy

## Edge Case Handling
- **Zlyhanie FTP pripojenia**: Zobrazenie chybovej správy s návrhom riešenia
- **Nedostatok miesta**: Upozornenie pred vytvorením zálohy
- **Poškodené súbory**: Skip súborov s chybami a pokračovanie v zálohovaní
- **Prerušenie internetu**: Opakovaný pokus o upload s exponenciálnym čakaním
- **Veľké súbory**: Progress bar pre dlhotrvajúce operácie

## Design Direction
Profesionálny a čistý dizajn s dôrazom na funkčnosť - moderný korporátny vzhľad inšpirovaný správcovskými nástrojmi ako Proxmox. Minimalistické rozhranie s jasnou hierarchiou informácií.

## Color Selection
Custom palette - Použitie farbieb inšpirovaných Proxmox VE pre konzistentnosť
- **Primary Color**: Proxmox modrá (oklch(0.45 0.15 240)) - reprezentuje technológiu a spoľahlivosť
- **Secondary Colors**: Svetlosivá (oklch(0.95 0.02 240)) pre pozadie kariet a tmavosivá (oklch(0.3 0.05 240)) pre text
- **Accent Color**: Zelená (oklch(0.6 0.15 140)) pre úspešné akcie a potvrdenia
- **Foreground/Background Pairings**: 
  - Background (Svetlá): Tmavosivý text (oklch(0.2 0.02 240)) - Ratio 12.3:1 ✓
  - Card (Biela): Tmavosivý text (oklch(0.2 0.02 240)) - Ratio 15.8:1 ✓
  - Primary (Modrá): Biely text (oklch(1 0 0)) - Ratio 6.2:1 ✓
  - Accent (Zelená): Biely text (oklch(1 0 0)) - Ratio 4.9:1 ✓

## Font Selection
Inter pre svoju vynikajúcu čitateľnosť v technických aplikáciách a profesionálny vzhľad vhodný pre správcovské nástroje.

- **Typographic Hierarchy**: 
  - H1 (Názov aplikácie): Inter Bold/32px/tight letter spacing
  - H2 (Sekcie): Inter SemiBold/24px/normal spacing  
  - H3 (Podsekcie): Inter Medium/20px/normal spacing
  - Body (Základný text): Inter Regular/16px/relaxed line height
  - Small (Pomocný text): Inter Regular/14px/tight line height

## Animations
Jemné funkčné animácie podporujúce UX bez rozptyľovania - focus na efektivitu namiesto efektov.

- **Purposeful Meaning**: Smooth prechodové animácie medzi stavmi (loading, success, error) komunikujúce pokrok operácií
- **Hierarchy of Movement**: Dôraz na progress indikátory pri zálohovaní a jemné hover efekty na tlačidlách

## Component Selection
- **Components**: Card pre sekcie nastavení, Button pre akcie, Input pre FTP údaje, Checkbox pre výber súborov, Progress pre zálohovanie, Alert pre stavové správy, Tabs pre organizáciu obsahu
- **Customizations**: Vlastný FileSelector komponent pre hierarchický výber súborov, BackupProgress komponent s detailným stavom
- **States**: Tlačidlá s loading stavom, disabled state pre neplatné konfigurácie, error state pre neúspešné operácie
- **Icon Selection**: Server, Download, Upload, Settings, Check, AlertTriangle z Phosphor Icons
- **Spacing**: Konzistentné 4px jednotky (space-4, space-6, space-8) pre harmonické rozloženie
- **Mobile**: Stack layout pre mobile s kolapsovateľnými sekciami, zachovanie funkčnosti na dotykových zariadeniach