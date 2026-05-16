# Codex Subagents

Tento adresár obsahuje odporúčané subagent profily pre projekt **Proxmox Backup Manager**.

Používaj ich ako špecializované prompt profily pri delegovaní práce alebo pri plánovaní väčších zmien. Každý agent má úzky rozsah zodpovednosti, aby zmeny zostali malé a ľahko kontrolovateľné.

## Odporúčaní agenti

- `backend-flask.md` - Flask API, konfigurácia, JSON persistencia a backup workflow
- `frontend-ui.md` - `templates/index.html`, UX, responzívne administrátorské rozhranie
- `lxc-deploy.md` - inštalácia, update skripty, systemd, gunicorn a LXC prevádzka
- `backup-security.md` - bezpečnosť záloh, FTP handling, citlivé údaje a práca s cestami
- `qa-reviewer.md` - testovanie, smoke testy, regresie a code review

## Ako ich používať

Pri väčšej úlohe vyber najmenšie množstvo agentov:

- Backend zmena endpointu: `backend-flask` + podľa rizika `qa-reviewer`
- Zmena vzhľadu alebo flow v UI: `frontend-ui`
- Úprava inštalačných skriptov: `lxc-deploy` + `qa-reviewer`
- Zmena výberu súborov alebo archívu záloh: `backup-security` + `backend-flask`
- Pred releasom alebo pushom: `qa-reviewer`

Agenti nemajú prepisovať cudzie lokálne zmeny. Pred editáciou majú vždy skontrolovať `git status --short --branch`.
