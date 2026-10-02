#!/usr/bin/env python3
"""Voliteľná mobile/desktop UI kontrola (horizontálny overflow, JS chyby, únik tajomstiev).

Nie je súčasťou test.sh – vyžaduje Playwright a Chromium mimo requirements.txt:
    python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright && /tmp/pw/bin/playwright install chromium
    PYTHONPATH=venv/lib/python3.11/site-packages /tmp/pw/bin/python tests/ui_mobile_check.py [screenshot_dir]

Spúšťa appku s dočasnými runtime súbormi (reálny backup_config.json/história sa nemenia).
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.chdir(ROOT)

import app as app_module  # noqa: E402
from test_archive import create_test_auth_config  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
try:
    from playwright.sync_api import sync_playwright  # noqa: E402
except ImportError:
    print('SKIP: playwright nie je nainštalovaný')
    sys.exit(0)

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='pbm-ui-shots-'))
OUT.mkdir(parents=True, exist_ok=True)
SECRET_MARKER = 'SECRET_HASH_DO_NOT_SHOW'

work = Path(tempfile.mkdtemp(prefix='pbm-ui-'))
app_module.CONFIG_FILE = str(work / 'backup_config.json')
app_module.BACKUP_HISTORY_FILE = str(work / 'backup_history.json')
app_module.BACKUP_STORAGE_DIR = str(work / 'backups')
os.makedirs(app_module.BACKUP_STORAGE_DIR)
_orig, totp_secret, password = create_test_auth_config(work / 'auth_config.json')

config = app_module.default_config()
config['ftp_config'].update({'host': 'ftp.example', 'username': 'u', 'password': 'p'})
config['source_config']['ssh'].update({'host': 'pve.example', 'password': 'x'})
app_module.save_config(config)

archive = Path(app_module.BACKUP_STORAGE_DIR) / 'proxmox_backup_pve_20261001.tar.gz'
with tarfile.open(archive, 'w:gz') as tar:
    def add(name, payload):
        data = payload.encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    add('etc/shadow', f'root:{SECRET_MARKER}:19000:0:99999:7:::\n')
    add('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\n')
    add('etc/hostname', 'cubi\n')
    add('etc/hosts', '127.0.0.1 localhost\n')
    add('etc/network/interfaces', 'auto vmbr0\n')
    add('etc/pve/storage.cfg', 'dir: local\n')
    add('var/lib/pve-cluster/config.db', 'sqlite')
    add('etc/ssh/sshd_config', 'PermitRootLogin yes\n')
    add('etc/ssh/ssh_host_ed25519_key', f'{SECRET_MARKER}\n')
    add('backup-info/ip-br-link.txt', '$ ip -br link\nexit_code=0\n\n--- stdout ---\nlo UNKNOWN 00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>\nenp2s0 UP 58:47:ca:aa:bb:cc <BROADCAST,MULTICAST,UP,LOWER_UP> averyveryverylongtokenwithoutanyspacesthatcouldoverflowthemobilelayoutifnotwrappedproperly\n\n--- stderr ---\n\n')
    add('backup-info/zpool-status.txt', '$ zpool status\ncommand_not_found=[Errno 2] No such file or directory: zpool\n')
    add('backup-info/crontab-root.txt', f'$ crontab -l\nexit_code=0\n\n--- stdout ---\n{SECRET_MARKER}\n--- stderr ---\n\n')

now = datetime.now()
app_module.save_backup_history([{
    'id': 'b1', 'filename': archive.name, 'timestamp': (now - timedelta(days=1)).isoformat(),
    'local_path': str(archive), 'ftp_status': 'success', 'status': 'success', 'size': '1 KB',
    'files': [item['path'] for item in config['backup_files'] if item['selected']],
    'skipped': [{'path': '/etc/auto.master', 'reason': 'missing'}],
    'source_mode': 'remote_ssh', 'source_host': 'pve.example',
}])

# FTP listing nesmie volať sieť
app_module.list_ftp_backups = lambda cfg: {'available': False, 'warning': 'FTP vypnuté v teste', 'archives': []}

server = make_server('127.0.0.1', 5099, app_module.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:5099'

results = []
VIEWPORTS = [(360, 780), (390, 844), (768, 1024), (1280, 900)]

OVERFLOW_JS = """
() => {
  const vw = document.documentElement.clientWidth;
  const offenders = [];
  document.querySelectorAll('body *').forEach(el => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    // ignoruj obsah vnútri scrollovateľných kontajnerov
    let p = el.parentElement, clipped = false;
    while (p && p !== document.body) {
      const ps = getComputedStyle(p);
      if (['auto','scroll','hidden'].includes(ps.overflowX)) { clipped = true; break; }
      p = p.parentElement;
    }
    if (!clipped && r.right > vw + 1) {
      offenders.push((el.id ? '#' + el.id : el.tagName.toLowerCase()) + '.' + String(el.className).slice(0, 60) + ' right=' + Math.round(r.right));
    }
  });
  return { vw, sw: document.documentElement.scrollWidth, offenders: offenders.slice(0, 8) };
}
"""


def check(page, label, vp):
    data = page.evaluate(OVERFLOW_JS)
    ok = data['sw'] <= data['vw'] + 1 and not data['offenders']
    results.append({'viewport': vp, 'view': label, 'ok': ok, **data})
    return ok


with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, height in VIEWPORTS:
        ctx = browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=1)
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        r = ctx.request.post(BASE + '/api/auth/login', data=json.dumps({
            'username': 'admin', 'password': password, 'totp_code': app_module.totp_token(totp_secret)}),
            headers={'Content-Type': 'application/json'})
        assert r.ok, r.text()
        page.goto(BASE + '/')
        page.wait_for_selector('#readiness-mini:not(:has-text("Načítavam"))', timeout=20000)
        page.wait_for_timeout(500)
        vp = f'{width}x{height}'
        check(page, 'backup', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-backup.png'), full_page=False)
            page.locator('#file-filter-chips').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-filters.png'))
        page.click("#file-filter-chips button:has-text('New HW – Review')")
        check(page, 'backup-filter-review', vp)
        page.click("button:has-text('Relevantné pre obnovu na novom HW')")
        check(page, 'backup-filter-newhw', vp)

        page.click('#tab-recovery')
        page.wait_for_selector('#rsec-overview .readiness-READY, #rsec-overview .readiness-WARNING, #rsec-overview .readiness-INCOMPLETE')
        check(page, 'recovery-overview', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-recovery-overview.png'), full_page=True)
        page.click("#recovery-subnav [data-rsec='checklist']")
        page.locator('#rsec-checklist details').nth(6).evaluate('el => el.open = true')
        page.locator('#step-check-1').check()
        check(page, 'recovery-checklist', vp)
        if width == 390:
            page.locator('#rsec-checklist details').nth(6).scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-checklist.png'))
        page.click("#recovery-subnav [data-rsec='items']")
        check(page, 'recovery-items', vp)
        page.click("#rsec-items button[data-path='/etc/shadow']")
        page.wait_for_selector('#item-detail-overlay:not(.hidden)')
        check(page, 'detail-shadow', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-detail-shadow.png'))
        page.keyboard.press('Escape')
        page.click("#rsec-items button[data-path='/etc/pve']")
        page.click("#item-detail-body button:has-text('Wiki')")
        page.wait_for_selector('#rsec-wiki article')
        check(page, 'wiki-pve-config-db', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-wiki.png'), full_page=True)
        page.click("#recovery-subnav [data-rsec='snapshot']")
        page.click('#snapshot-load-btn')
        page.wait_for_selector('#snapshot-result details')
        check(page, 'snapshot', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-snapshot.png'), full_page=True)

        page.click('#tab-restore')
        page.wait_for_selector('.restore-checkbox')
        page.locator(".restore-checkbox[data-path='/etc/network']").check()
        page.locator(".restore-checkbox[data-path='/etc/pve']").check()
        page.locator("input[name='restore-mode'][value='apply']").check()
        check(page, 'restore-apply-plan', vp)
        if width == 390:
            page.locator('#restore-plan-summary').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-restore-plan.png'))
        page.click('#tab-history')
        check(page, 'history', vp)
        page.click('#tab-settings')
        check(page, 'settings', vp)

        page.goto(BASE + '/#wiki/network')
        page.wait_for_selector('#rsec-wiki article h2:has-text("siete")', timeout=15000)
        check(page, 'deeplink-wiki-network', vp)

        page.goto(BASE + '/api/recovery/handbook?inline=1')
        page.wait_for_selector('#rychle-udaje')
        check(page, 'offline-handbook', vp)
        handbook_html = page.content()
        results.append({'viewport': vp, 'view': 'handbook-no-secrets', 'ok': SECRET_MARKER not in handbook_html})
        page.goto(BASE + '/')
        page.wait_for_selector('#readiness-mini:not(:has-text("Načítavam"))', timeout=20000)

        html_dump = page.content()
        results.append({'viewport': vp, 'view': 'no-secrets-in-dom', 'ok': SECRET_MARKER not in html_dump})
        results.append({'viewport': vp, 'view': 'no-js-errors', 'ok': not errors, 'errors': errors})
        if width == 360:
            page.emulate_media(color_scheme='dark')
            page.evaluate("localStorage.setItem('theme','dark')")
            page.goto(BASE + '/')
            page.wait_for_selector('#readiness-mini:not(:has-text("Načítavam"))', timeout=20000)
            page.click('#tab-recovery')
            page.wait_for_selector('#rsec-overview .rounded-xl')
            page.screenshot(path=str(OUT / 'm-dark-overview.png'), full_page=True)
            page.click("#recovery-subnav [data-rsec='items']")
            page.screenshot(path=str(OUT / 'm-dark-items.png'))
        ctx.close()
    browser.close()

server.shutdown()
failed = [r for r in results if not r['ok']]
for r in results:
    print(('OK  ' if r['ok'] else 'FAIL'), r['viewport'], r['view'], '' if r['ok'] else json.dumps({k: r.get(k) for k in ('vw', 'sw', 'offenders', 'errors')}, ensure_ascii=False))
print('FAILED:', len(failed), 'of', len(results), '| screenshots:', OUT)
sys.exit(1 if failed else 0)
