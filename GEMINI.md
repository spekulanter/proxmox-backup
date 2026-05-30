# GEMINI.md

Pokyny pre Gemini-based agentov pracujúcich na projekte **Proxmox Backup Manager**.

> **Primárny zdroj pokynov je [AGENTS.md](./AGENTS.md).** Prečítaj ho celý pred akoukoľvek zmenou.
> Tento súbor ho dopĺňa o poznámky špecifické pre Gemini workflow.

## Čo nájdeš v AGENTS.md

- Kontext projektu a technologický stack
- Zoznam dôležitých súborov (vrátane `backups/`, `tests/test_archive.py`)
- Produktové očakávania a prehľad všetkých funkcií (záloha, restore, SSH, FTP)
- Bezpečnostné pravidlá (vylúčené cesty, citlivé súbory, práva)
- Vývojové zásady (`CONFIG_VERSION`, `migrate_config()`, legacy routes)
- Prehľad API endpointov (`/api/*`)
- UI smerovanie a testovacie príkazy
- Nasadenie a prevádzkové príkazy

## Doplnky pre Gemini

### Štýl odpovedí

- Komunikuj v slovenčine, ak používateľ píše po slovensky.
- Krátke odpovede pre jednoduché otázky, štruktúrovaná odpoveď pre komplexné témy.
- Pred väčšou zmenou si vytvor plán (v prípade Antigravity IDE použi štandardný "planning mode" a vytvor artefakt pre implementačný plán) a počkaj na schválenie od používateľa.

### Prístup k nástrojom a súborom

- `app.py` má 2000+ riadkov — pri čítaní použi cielený rozsah riadkov (`view_file`), nie celý súbor naraz.
- Frontend je v jedinom súbore `templates/index.html` — Tailwind CDN, žiadne build kroky.
- Runtime dáta (`backup_config.json`, `backup_history.json`, `backups/`) sú mimo git — necommituj ich.
- Na manipuláciu so súbormi uprednostňuj špecifické nástroje (`view_file`, `replace_file_content`, `grep_search`) namiesto bash príkazov typu `cat`, `sed` alebo `grep` v termináli.

### Testovanie po zmene

```bash
# Po akejkoľvek zmene backendu:
venv/bin/python -m py_compile app.py
venv/bin/python tests/test_archive.py

# Po zmene shell skriptov:
bash -n install_in_lxc.sh
bash -n update.sh
bash -n auto_backup.sh
bash -n test.sh
```

### Kľúčové architektonické pravidlá

- **Backup zdroj**: `remote_ssh` (LXC → Proxmox cez SSH/paramiko) alebo `local` (appka priamo na hosta).
  Továreň: `build_backup_source(source_config)` → `LocalBackupSource` / `RemoteSshBackupSource`.
- **Config migrácia**: Pri zmene schémy vždy zvýš `CONFIG_VERSION` a aktualizuj `migrate_config()`.
- **Restore whitelist**: Restore smie aplikovať len cesty z `restore_whitelist_paths()`.
  Neobchádzaj staging adresár ani pre-apply zálohu.
- **Nové endpointy**: Pridávaj pod `/api/*`. Legacy form-routes nechaj bez zmeny.
- **Vylúčené cesty z archívu**: `/mnt`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, `/var/log` atď.
  Kompletný zoznam: `ARCHIVE_EXCLUDE_PATHS` v `app.py`.
