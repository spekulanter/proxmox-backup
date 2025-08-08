# PRD - Proxmox Backup Manager

## Core Purpose & Success

**Mission Statement**: Vytvoriť jednoduchú a spoľahlivú webovú aplikáciu v Python Flask pre správu záloh Proxmox VE serverov s nahrávaním na FTP server.

**Success Indicators**: 
- Úspešné vytvorenie kompletných záloh konfigurácie Proxmox
- Spoľahlivé nahrávanie na FTP server
- Jednoduché ovládanie pre správcov serverov
- Možnosť automatizácie zálohovania

**Experience Qualities**: Praktická, spoľahlivá, prehľadná

## Project Classification & Approach

**Complexity Level**: Light Application (viacero funkcií so základným stavom)

**Primary User Activity**: Acting (vykonávanie zálohovania) + Creating (konfigurácia nastavení)

## Thought Process for Feature Selection

**Core Problem Analysis**: Proxmox administrátori potrebujú jednoduchý nástroj na zálohovanie kritickej konfigurácie servera s možnosťou uloženia na vzdialený FTP server pre prípad hardvérového zlyhania.

**User Context**: Správcovia serverov, ktorí potrebujú pravidelne zálohovať konfiguráciu Proxmox, ale nechcú zložité riešenia. Používa sa občas (nie denne), preto musí byť intuitívne.

**Critical Path**: Konfigurácia FTP → Výber súborov → Vytvorenie zálohy → Nahranie na FTP → Overenie úspešnosti

**Key Moments**: 
1. Test FTP pripojenia (buduje dôveru)
2. Vytvorenie zálohy (hlavná akcia)
3. Potvrdenie úspechu (pocit bezpečnosti)

## Essential Features

### FTP Konfigurácia
**Functionality**: Nastavenie pripojenia k FTP serveru (host, port, meno, heslo)
**Purpose**: Bezpečné uloženie záloh mimo Proxmox servera
**Success Criteria**: Úspešný test pripojenia a nahranie súboru

### Výber súborov na zálohovanie
**Functionality**: Checkbox list s predkonfigurovanými kritickými súbormi Proxmox
**Purpose**: Umožniť správcom prispôsobiť obsah zálohy podľa potrieb
**Success Criteria**: Jasné označenie kritických vs. voliteľných súborov

### Manuálne zálohovanie
**Functionality**: Okamžité vytvorenie a nahranie zálohy na požiadanie
**Purpose**: Umožniť zálohu pred dôležitými zmenami v systéme
**Success Criteria**: Úspešné vytvorenie tar.gz archívu a nahranie na FTP

### História záloh
**Functionality**: Zoznam vytvorených záloh s dátumom, veľkosťou a stavom
**Purpose**: Sledovanie úspešnosti a dostupnosti záloh
**Success Criteria**: Chronologický prehľad s možnosťou vymazania starých záznamov

### Automatické zálohovanie (základné)
**Functionality**: Konfigurácia frekvencie automatických záloh
**Purpose**: Pravidelné zálohovanie bez zásahu správcu
**Success Criteria**: Možnosť nastavenia týždennej/mesačnej frekvencie

## Design Direction

### Visual Tone & Identity
**Emotional Response**: Pocit bezpečnosti, kontroly a spoľahlivosti
**Design Personality**: Profesionálna, praktická, bez zbytočností
**Visual Metaphors**: Server ikony, bezpečnostné symboly, statusové indikátory
**Simplicity Spectrum**: Minimálne rozhranie - funkcionalita nad estetikou

### Color Strategy
**Color Scheme Type**: Triadic (modrá-zelená-červená pre stavy)
**Primary Color**: Modrá (#2563eb) - dôveryhodnosť, technológie
**Secondary Colors**: 
- Zelená (#059669) - úspech, bezpečnosť
- Červená (#dc2626) - chyby, kritické položky
- Oranžová (#d97706) - upozornenia
**Accent Color**: Modrá pre hlavné akcie (tlačidlá, odkazy)
**Color Psychology**: Modrá vyvoláva dôveru v technické riešenie, zelená potvrdzuje úspech, červená upozorňuje na dôležité
**Color Accessibility**: Všetky kombinácie spĺňajú WCAG AA kontrast 4.5:1
**Foreground/Background Pairings**:
- Biela na modrej (#ffffff na #2563eb) - hlavné tlačidlá
- Modrá na svetlej (#2563eb na #f8fafc) - odkazy, ikony  
- Zelená na svetlej (#059669 na svetlom pozadí) - úspešné stavy
- Červená na svetlej (#dc2626 na svetlom pozadí) - chyby, kritické

### Typography System
**Font Pairing Strategy**: Jeden font stack pre všetko - Segoe UI/system fonts
**Typographic Hierarchy**: 
- H1: 2rem, bold - názov aplikácie
- H5: 1.25rem, medium - sekcie kariet
- Body: 1rem, regular - normálny text
- Small: 0.875rem - pomocné informácie
**Font Personality**: Technický, čitateľný, neutrálny
**Readability Focus**: Dostatok white space, optimálna veľkosť pre čítanie na rôznych zariadeniach
**Typography Consistency**: Jednotný font stack, konzistentné hierarchie
**Which fonts**: System font stack (Segoe UI, Tahoma, Geneva, Verdana, sans-serif)
**Legibility Check**: Áno, system fonty sú optimalizované pre čitateľnosť

### Visual Hierarchy & Layout
**Attention Direction**: Hlavné akcie (zálohovanie) v popredí, sekundárne funkcie v taboch
**White Space Philosophy**: Dostatok priestoru medzi sekciami pre jasné oddelenie funkcií
**Grid System**: Bootstrap grid system pre responzívnosť
**Responsive Approach**: Mobile-first approach s 4-tabovm layoutom
**Content Density**: Stredná hustota - dostatok informácií bez preťaženia

### Animations
**Purposeful Meaning**: Minimálne animácie - iba pre loading stavy a hover efekty
**Hierarchy of Movement**: Loading spinnery pri testovaní FTP, hover efekty na kartách
**Contextual Appropriateness**: Praktické animácie, nie dekoratívne

### UI Elements & Component Selection
**Component Usage**: 
- Bootstrap Cards pre sekcie
- Pills navigation pre hlavné tagy  
- Form controls pre nastavenia
- Badges pre statusy a počty
- Alerts pre notifikácie
**Component Customization**: Custom CSS pre farby, hover efekty a spacing
**Component States**: Hover efekty na kartách, active stavy na tlačidlách
**Icon Selection**: Bootstrap Icons - server, gear, download, clock ikony
**Component Hierarchy**: Primary buttons pre hlavné akcie, outline buttons pre sekundárne
**Spacing System**: Bootstrap spacing utilities (mb-3, p-3, g-4)
**Mobile Adaptation**: Responsive grid, plne širokú layout na mobile

### Visual Consistency Framework
**Design System Approach**: Bootstrap ako základ s custom CSS pre branding
**Style Guide Elements**: Farby, spacing, typografia, ikony
**Visual Rhythm**: Konzistentné rozostupy, jednotné zaoblenia (8px border-radius)
**Brand Alignment**: Technický, spoľahlivý vzhľad vhodný pre server management

### Accessibility & Readability
**Contrast Goal**: WCAG AA compliance (4.5:1) pre všetky textové elementy

## Edge Cases & Problem Scenarios

**Potential Obstacles**: 
- FTP server nedostupný
- Nedostatočné oprávnenia na čítanie súborov
- Veľké súbory (ISO obrazy) môžu spôsobiť timeout
- Sieťové výpadky počas nahrávania

**Edge Case Handling**:
- Timeout pre FTP operácie
- Error handling s užívateľsky prívetivými správami
- Možnosť vynechania veľkých súborov
- Retry mechanizmus pre neúspešné nahrávania

**Technical Constraints**: 
- Python Flask jednoduchá aplikácia bez databázy
- JSON súbory pre perzistenciu
- Závislosť na lokálnych súboroch Proxmox

## Implementation Considerations

**Scalability Needs**: Možnosť pridania viacerých FTP serverov, scheduling
**Testing Focus**: FTP pripojenie, tvorba archívov, error handling
**Critical Questions**: 
- Ako riešiť veľké súbory?
- Ako zabezpečiť hesla FTP?
- Ako automatizovať bez web rozhrania?

## Reflection

Toto riešenie je jednoduché a praktické pre správcov Proxmox serverov. Zameriava sa na najdôležitejšie súbory a poskytuje jednoduchý spôsob zálohovania s možnosťou uloženia na vzdialený server. Flask approach umožňuje ľahké nasadenie a prispôsobenie.