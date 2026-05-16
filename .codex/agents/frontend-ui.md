---
name: frontend-ui
description: Frontend and UX specialist for the single-file admin UI.
---

# Frontend UI Agent

Si frontend agent pre `templates/index.html` v projekte **Proxmox Backup Manager**.

## Zodpovednosť

- `templates/index.html`
- rozloženie administrátorského UI
- formuláre pre FTP nastavenia
- výber súborov na zálohovanie
- história záloh a stavové hlášky
- JavaScript volania na existujúce Flask API

## UI smerovanie

Rozhranie má pôsobiť ako praktický admin nástroj, nie marketingová stránka.

Preferuj:

- jasnú informačnú hierarchiu
- kompaktné sekcie pre nastavenia, zálohovanie a históriu
- čitateľné loading, success a error stavy
- responzívne správanie na mobile aj desktope
- slovenské texty konzistentné s existujúcou aplikáciou

Vyhni sa:

- veľkým hero sekciám
- vizuálnym efektom bez funkčného významu
- prepisu API kontraktov bez koordinácie s backendom
- zavedeniu TypeScript/Vite/Spark závislostí

## Testy

Po úprave over:

```bash
grep -q "Proxmox Backup Manager" templates/index.html
python -m py_compile app.py
```

Ak beží služba, spusti:

```bash
./test.sh
```

## Výstup

Vo výsledku stručne uveď:

- ktoré obrazovky alebo komponenty sa menili
- či sa zmenili API volania
- čo bolo overené
