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
from test_recovery import PreflightSshClient, preflight_hosts  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
try:
    from playwright.sync_api import sync_playwright  # noqa: E402
except ImportError:
    print('SKIP: playwright nie je nainštalovaný')
    sys.exit(0)

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='pbm-ui-shots-'))
OUT.mkdir(parents=True, exist_ok=True)
SECRET_MARKER = 'SECRET_HASH_DO_NOT_SHOW'

COMPARE_PASSWORD_MARKER = 'COMPARE_PASSWORD_NOT_FOR_STORAGE'
COMPARE_HOST_FIXTURE = {
    'host': 'old-pve.example', 'port': 22, 'username': 'root', 'connected': True,
    'complete': True, 'errors': [],
    'facts': {
        'hostname': 'old-pve', 'pve_version': 'proxmox-ve: 9.2.0',
        'storage': [{'id': 'local-lvm', 'type': 'lvmthin', 'status': 'active'}],
        'links': [{'name': 'enp45s0', 'state': 'UP'}],
        'addresses': [{'name': 'vmbr0.200', 'state': 'UP', 'addresses': ['192.0.2.10/24']}],
        'network': {
            'bridges': [{'name': 'vmbr0', 'ports': ['enp45s0'], 'vlan_aware': 'yes', 'vids': '2-4094'}],
            'vlans': [{'name': 'vmbr0.200', 'raw_device': 'vmbr0', 'vlan_id': '200'}], 'includes': True,
        },
        'cpu': {'vendor': 'GenuineIntel', 'flags': ['sse', 'sse2', 'vmx']},
        'guests': [{'vmid': 100, 'type': 'VM', 'name': 'home-assistant', 'status': 'running'}],
        'timers': [{'unit': 'pve-backup-to-a-very-long-nas-name-for-mobile-wrapping.timer', 'activates': 'pve-backup.service'}],
    },
}
COMPARE_FIXTURE = {
    'success': True, 'read_only': True, 'compared_at': '2026-10-03T10:20:30+00:00',
    'old_host': COMPARE_HOST_FIXTURE,
    'new_host': {
        **COMPARE_HOST_FIXTURE, 'host': 'new-pve.example', 'complete': False,
        'errors': [{'command_id': 'timers', 'title': 'Systemd timery', 'message': 'Diagnostický príkaz zlyhal; over host ručne.'}],
        'facts': {**COMPARE_HOST_FIXTURE['facts'], 'hostname': 'new-pve', 'cpu': {'vendor': 'AuthenticAMD', 'flags': ['sse', 'sse2', 'svm']}},
    },
    'summary': {'ok': 1, 'info': 1, 'warning': 2, 'error': 1, 'unknown': 1},
    'rows': [
        {'id': 'pve-version', 'label': 'Verzia PVE', 'level': 'ok', 'old_value': '9.2.0', 'new_value': '9.2.0', 'detail': 'Verzie PVE sú zhodné.'},
        {'id': 'storage', 'label': 'Storage ID', 'level': 'warning', 'old_value': 'local-lvm, qnap.autofs', 'new_value': 'local-lvm', 'detail': 'Na novom hoste chýba storage ID qnap.autofs.'},
        {'id': 'network', 'label': 'Bridge a VLAN', 'level': 'info', 'old_value': 'vmbr0.200', 'new_value': 'vmbr0.200', 'detail': 'Skontroluj fyzické porty a include súbory.'},
        {'id': 'cpu', 'label': 'CPU vendor a flags', 'level': 'warning', 'old_value': 'GenuineIntel / vmx', 'new_value': 'AuthenticAMD / svm', 'detail': 'Zmena CPU Intel ↔ AMD: over kompatibilitu hostí.'},
        {'id': 'duplicate-running', 'label': 'Hostia bežiaci na oboch serveroch', 'level': 'error', 'old_value': '100 running', 'new_value': '100 running', 'detail': 'Rovnaký VMID 100 beží na oboch hostoch. Pred pokračovaním ho na jednom zastav.'},
        {'id': 'timers', 'label': 'Backup timery', 'level': 'unknown', 'old_value': 'pve-backup.timer', 'new_value': 'neoverené', 'detail': 'Výstup nového hosta nie je dostupný. Timery povoľ iba na jednom hoste.'},
    ],
}


work = Path(tempfile.mkdtemp(prefix='pbm-ui-'))
app_module.CONFIG_FILE = str(work / 'backup_config.json')
app_module.BACKUP_HISTORY_FILE = str(work / 'backup_history.json')
app_module.BACKUP_STORAGE_DIR = str(work / 'backups')
app_module.MIGRATION_STATE_FILE = str(work / 'migration_state.json')
app_module.MIGRATION_TARGET_FILE = str(work / 'migration_target.json')
app_module.MIGRATION_JOBS_FILE = str(work / 'migration_jobs.json')
app_module.MIGRATION_TRANSFER_FILE = str(work / 'migration_transfer.json')
app_module.RECOVERY_PROGRESS_FILE = str(work / 'recovery_progress.json')
# Kontrola nového hosta používa iba falošné SSH (nikdy sa nepripája na reálny server).
app_module.SSH_CLIENT_FACTORY = lambda: PreflightSshClient(preflight_hosts())
TARGET_PASSWORD_MARKER = 'TARGET_ROOT_PASSWORD_NOT_IN_UI'
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
    add('etc/pve/jobs.cfg', 'vzdump: daily\n\tvmid 100\n\tstorage local\n\tenabled 1\n')
    add('backup-info/qm-list.txt', '$ qm list\nexit_code=0\n\n--- stdout ---\nVMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n100 home-assistant running 8192 60 1790\n\n--- stderr ---\n\n')
    add('backup-info/pct-list.txt', '$ pct list\nexit_code=0\n\n--- stdout ---\nVMID Status Lock Name\n113 stopped proxmox-backup-with-a-very-long-name-to-check-mobile-wrapping\n\n--- stderr ---\n\n')
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


def migration_go(page, step_id):
    """Otvorí zoznam krokov sprievodcu migráciou a prejde na krok."""
    page.evaluate("document.querySelector('[data-stepper-nav=migration]').open = true")
    page.click(f"#migration-steps [data-stepper-go='{step_id}']")
    page.wait_for_selector(f'#migration-current-step[data-step-id="{step_id}"]')


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
        assert page.locator('#backup-files-list .file-checkbox').count() > 0, 'výber súborov je hneď viditeľný'
        if width == 390:
            page.screenshot(path=str(OUT / 'm-backup.png'), full_page=False)
        if width == 390:
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
        assert page.locator('#recovery-chooser button').count() == 3, 'rozcestník Čo chceš urobiť'
        page.evaluate("localStorage.removeItem('pbm-dr-current-step'); postRecoveryProgress('/reset', {}).then(renderRecoveryChecklist)")
        page.wait_for_function("() => recoveryOverview && Object.keys(recoveryOverview.checklist_progress.steps).length === 0")
        page.click("#recovery-subnav [data-rsec='checklist']")
        page.wait_for_selector('#dr-current-step[data-step-id="install-pve"]')
        page.evaluate("document.querySelector('[data-stepper-nav=dr]').open = true")
        page.locator('#step-check-1').check()
        page.wait_for_function("() => !recoveryProgressBusy && recoveryOverview.checklist_progress.steps['install-pve']")
        page.wait_for_selector('#dr-current-step[data-step-id="check-hardware"]')
        page.click('#dr-current-step [data-stepper-done]')
        page.wait_for_selector('#dr-current-step[data-step-id="mgmt-network"]')
        page.click("#dr-current-step [data-stepper-go='check-hardware']")
        page.wait_for_selector('#dr-current-step[data-step-id="check-hardware"]')
        check(page, 'recovery-checklist', vp)
        if width == 390:
            page.locator('#dr-current-step').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-checklist.png'))
        page.reload()
        page.wait_for_selector('#readiness-mini:not(:has-text("Načítavam"))', timeout=20000)
        page.click('#tab-recovery')
        page.click("#recovery-subnav [data-rsec='checklist']")
        page.wait_for_selector('#dr-current-step[data-step-id="check-hardware"]')
        assert page.locator('#step-check-1').is_checked() and page.locator('#step-check-2').is_checked(), 'postup obnovy je uložený na serveri'
        if width == 360:
            Path(app_module.MIGRATION_STATE_FILE).write_text('{broken-json', encoding='utf-8')
        page.click("#recovery-subnav [data-rsec='migration']")
        if width == 360:
            page.wait_for_selector("#migration-body [data-migration-action='reset']")
            check(page, 'migration-corrupt-state', vp)
            page.once('dialog', lambda dialog: dialog.accept())
            page.click("#migration-body [data-migration-action='reset']")
        page.wait_for_selector('#migration-form')
        # Každý viewport začne bez stavu z predchádzajúceho priechodu.
        if page.locator('#migration-reset-btn').count():
            page.once('dialog', lambda dialog: dialog.accept())
            page.click('#migration-reset-btn')
            page.wait_for_selector('#migration-reset-btn', state='detached')
        page.locator("input[name='migration-method'][value='side_by_side']").check()
        page.fill('#migration-old_host-ip', '192.0.2.10')
        page.fill('#migration-old_host-hostname', 'old-pve.example')
        page.fill('#migration-new_host-ip', '192.0.2.11')
        page.fill('#migration-new_host-hostname', 'new-pve.example')
        page.click('#migration-save-btn')
        page.wait_for_selector('#migration-current-step')
        assert page.locator('#migration-current-step').get_attribute('data-step-id') == 'prepare', 'sprievodca začína prvým krokom'
        assert not page.locator('#migration-guests').count(), 'hostia sú iba v kroku Presun hostí'
        migration_go(page, 'new-host')
        page.fill('#migration-target-host', '192.0.2.3')
        page.fill('#migration-target-password', TARGET_PASSWORD_MARKER)
        page.click('#migration-target-save')
        assert page.locator('#migration-target-password').input_value() == '', 'heslo nového hosta sa z formulára ihneď vymaže'
        page.wait_for_selector("#migration-target-checks [data-target-check='identity']", timeout=20000)
        assert 'OK' in page.locator("[data-target-check='pve']").inner_text()
        assert page.locator('#migration-target-password').get_attribute('placeholder') == 'uložené – nechaj prázdne'
        assert TARGET_PASSWORD_MARKER not in page.content()
        assert page.evaluate('(secret) => !JSON.stringify(migrationData).includes(secret) && !Object.values(localStorage).some(v => v.includes(secret))', TARGET_PASSWORD_MARKER)
        assert TARGET_PASSWORD_MARKER not in Path(app_module.MIGRATION_STATE_FILE).read_text()
        check(page, 'migration-new-host', vp)
        if width == 390:
            page.locator('#migration-target').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-migration-target.png'), full_page=True)
        migration_go(page, 'selective-config')
        page.wait_for_selector('#migration-transfer #migration-config-db', timeout=20000)
        assert page.locator("#migration-files [data-migration-file][value='/etc/auto.nfs']").is_checked(), 'autofs mapy sú predvolene vybrané'
        assert not page.locator("#migration-files [data-migration-file][value='/root']").is_checked(), '/root je predvolene nevybraný'
        check(page, 'migration-transfer', vp)
        # Obnovenie stavu nesmie prepísať rozpracovaný výber súborov, adresár záloh ani poznámky (A14).
        first_file = page.locator('[data-migration-file]').first
        original_state = first_file.is_checked()
        first_file.set_checked(not original_state)
        page.evaluate('loadMigration()')
        assert page.locator('[data-migration-file]').first.is_checked() == (not original_state), 'výber súborov prežije obnovenie'
        first_file.set_checked(original_state)
        page.evaluate('loadMigration()')
        # Chyba pred štartom (HTTP 400, bez failed jobu) nesmie hneď zmiznúť po obnovení stavu (A18).
        page.evaluate("migrationOperation('/transfer/network', { mapping: {} })")
        assert 'Kontrola nového hosta' in page.locator('#migration-status').inner_text(), 'validačná chyba ostane zobrazená: ' + page.locator('#migration-status').inner_text()
        page.evaluate("migrationMessage('')")
        if width == 390:
            page.locator('#migration-transfer').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-migration-transfer.png'), full_page=True)
        migration_go(page, 'cutover')
        page.wait_for_selector('#migration-cutover')
        assert page.locator("#migration-cutover [data-migration-action='cutover']").is_disabled(), 'prepnutie bez config.db a overených hostí je zablokované'
        check(page, 'migration-cutover', vp)
        migration_go(page, 'move-guests')
        page.wait_for_selector('#migration-move-settings')
        page.wait_for_selector("[data-migration-guest='100']")
        assert page.locator('#migration-step-cutover').is_disabled(), 'cutover pred overením hostí'
        assert page.locator("[data-migration-guest='113']").inner_text().find('Bez vzdump jobu') >= 0
        # Rekonfigurácia nesmie prepísať rozpracovaný spôsob ani zdieľať IP.
        page.evaluate("document.getElementById('migration-setup').open = true")
        page.locator("input[name='migration-method'][value='disk_move']").check()
        page.click('#migration-save-btn')
        page.wait_for_function("() => !migrationBusy && document.querySelector('#migration-status').textContent.includes('resetuj')")
        assert page.evaluate("migrationData.state.method === 'side_by_side'"), 'odmietnutá zmena spôsobu zachová stav'
        assert page.locator('#migration-save-btn').is_enabled(), 'po chybe sa formulár odblokuje'
        page.locator("input[name='migration-method'][value='side_by_side']").check()
        page.fill('#migration-new_host-ip', '192.0.2.10')
        page.click('#migration-save-btn')
        page.wait_for_function("() => !migrationBusy && document.querySelector('#migration-status').textContent.includes('inú IP')")
        assert page.evaluate("migrationData.state.new_host.ip === '192.0.2.11'"), 'odmietnutá duplicitná IP zachová stav'
        check(page, 'migration-validation-errors', vp)
        page.fill('#migration-new_host-ip', '192.0.2.11')
        page.click('#migration-save-btn')
        page.wait_for_function("() => !migrationBusy && document.querySelector('#migration-status').textContent.includes('uložený')")
        page.locator("[data-migration-detail='guest-100']").evaluate('el => el.open = true')
        page.locator("[data-migration-detail='guest-113']").evaluate('el => el.open = true')
        check(page, 'migration-side-by-side', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-migration.png'), full_page=True)
        note = '<img src=x onerror="window.migrationXss=true">' + 'a' * 160
        page.fill('#migration-guest-note-100', note)
        page.click("[data-migration-action='guest-note'][data-vmid='100']")
        page.wait_for_function("() => document.querySelector('#migration-status').textContent.includes('uložený') && !migrationBusy")
        assert page.evaluate('window.migrationXss === undefined'), 'poznámka musí byť escapovaná'
        page.locator('#migration-step-prepare').check()
        page.wait_for_function('() => !migrationBusy')
        page.reload()
        page.wait_for_selector('#readiness-mini:not(:has-text("Načítavam"))', timeout=20000)
        page.click('#tab-recovery')
        page.click("#recovery-subnav [data-rsec='migration']")
        page.wait_for_selector("[data-migration-guest='100']")
        assert page.locator('#migration-step-prepare').is_checked(), 'krok musí prežiť reload'
        page.locator("[data-migration-detail='guest-100']").evaluate('el => el.open = true')
        assert page.locator('#migration-guest-note-100').input_value() == note, 'poznámka musí prežiť reload'
        for status in ('stopped_on_old', 'restored_on_new', 'verified'):
            page.select_option('#migration-guest-status-100', status)
            page.wait_for_function('(status) => !migrationBusy && migrationData.guests.find(g => g.vmid === 100).status === status', arg=status)
        page.locator("[data-migration-detail='guest-113']").evaluate('el => el.open = true')
        page.select_option('#migration-guest-status-113', 'skipped')
        page.wait_for_function("() => !migrationBusy && migrationData.guests.find(g => g.vmid === 113).status === 'skipped'")
        assert page.locator('#migration-step-cutover').is_enabled(), 'cutover po overení/vynechaní hostí'
        check(page, 'migration-verified-guests', vp)
        page.once('dialog', lambda dialog: dialog.dismiss())
        page.click('#migration-reset-btn')
        assert page.locator('#migration-guest-note-100').input_value() == note, 'zrušený reset zachová stav'
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#migration-reset-btn')
        page.wait_for_selector('#migration-reset-btn', state='detached')
        page.locator("input[name='migration-method'][value='disk_move']").check()
        page.click('#migration-save-btn')
        page.wait_for_selector('#migration-step-disk-move', state='attached')
        assert not page.locator('#migration-guests').count(), 'disk_move nemá presun hostí po jednom'
        assert not page.locator('#migration-step-selective-config').count(), 'disk_move má vlastné kroky'
        assert not page.locator('#migration-current-step #migration-compare').count(), 'disk_move nemá porovnanie dvoch hostov'
        check(page, 'migration-disk-move', vp)

        # Porovnanie hostov patrí do kroku Príprava pri presune vedľa starého hosta.
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#migration-reset-btn')
        page.wait_for_selector('#migration-reset-btn', state='detached')
        page.locator("input[name='migration-method'][value='side_by_side']").check()
        page.fill('#migration-old_host-ip', '192.0.2.10')
        page.fill('#migration-old_host-hostname', 'old-pve.example')
        page.fill('#migration-new_host-ip', '192.0.2.11')
        page.fill('#migration-new_host-hostname', 'new-pve.example')
        page.click('#migration-save-btn')
        page.wait_for_selector('#migration-current-step')
        migration_go(page, 'prepare')
        page.wait_for_selector('#migration-current-step #migration-compare')

        # Porovnanie používa iba syntetické HTTP odpovede; nikdy neotvára SSH.
        compare_attempts = {'count': 0}

        def mock_compare(route):
            compare_attempts['count'] += 1
            payload = route.request.post_data_json
            assert set(payload) == {'new_host'}
            assert payload['new_host'] == {'host': 'new-pve.example', 'port': 2222, 'password': COMPARE_PASSWORD_MARKER}
            assert route.request.headers.get('x-csrf-token'), 'porovnanie musí používať CSRF'
            if compare_attempts['count'] == 2:
                route.fulfill(status=400, content_type='application/json', body=json.dumps({'success': False, 'error': 'Skontroluj uložený SSH cieľ aplikácie.'}))
            else:
                route.fulfill(status=200, content_type='application/json', body=json.dumps(COMPARE_FIXTURE))

        page.route('**/api/recovery/migration/compare', mock_compare)
        assert not page.locator('#migration-compare-result article').count(), 'porovnanie sa nespúšťa automaticky'
        page.fill('#migration-compare-host', 'new-pve.example')
        page.fill('#migration-compare-port', '2222')
        page.fill('#migration-compare-password', COMPARE_PASSWORD_MARKER)
        page.click('#migration-compare-btn')
        assert page.locator('#migration-compare-password').input_value() == '', 'heslo vymaž ihneď po odoslaní'
        page.wait_for_selector("[data-compare-row='duplicate-running']")
        assert 'Chyba' in page.locator("[data-compare-row='duplicate-running']").inner_text()
        assert 'Intel' in page.locator("[data-compare-row='cpu']").inner_text()
        page.locator('#migration-compare-result details').evaluate('el => el.open = true')
        check(page, 'migration-compare-mismatches', vp)
        assert COMPARE_PASSWORD_MARKER not in page.content()
        assert page.evaluate('(secret) => !Object.values(localStorage).some(v => v.includes(secret)) && !Object.values(sessionStorage).some(v => v.includes(secret)) && !JSON.stringify(appConfig).includes(secret) && !JSON.stringify(migrationData).includes(secret)', COMPARE_PASSWORD_MARKER)
        assert COMPARE_PASSWORD_MARKER not in Path(app_module.CONFIG_FILE).read_text()
        assert COMPARE_PASSWORD_MARKER not in Path(app_module.MIGRATION_STATE_FILE).read_text()
        page.locator('#migration-step-prepare').check()
        page.wait_for_function('() => !migrationBusy')
        assert page.locator("[data-compare-row='duplicate-running']").count(), 'výsledok prežije prekreslenie krokov'
        if width == 390:
            page.locator('#migration-compare').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-migration-compare.png'), full_page=True)
        page.fill('#migration-compare-password', COMPARE_PASSWORD_MARKER)
        page.click('#migration-compare-btn')
        page.wait_for_function("() => !migrationCompareBusy && document.querySelector('#migration-compare-status').textContent.includes('zadaj heslo znova')")
        assert page.locator('#migration-compare-password').input_value() == '', 'heslo vymaž aj po HTTP chybe'
        assert not page.locator('#migration-compare-result article').count(), 'pri chybe nezobrazuj starý výsledok'
        assert page.locator('#migration-compare-btn').is_enabled(), 'porovnanie možno zopakovať'
        check(page, 'migration-compare-failure', vp)
        page.fill('#migration-compare-password', COMPARE_PASSWORD_MARKER)
        page.click('#migration-compare-btn')
        page.wait_for_selector("[data-compare-row='duplicate-running']")
        page.evaluate("document.getElementById('migration-setup').open = true")
        page.click('#migration-save-btn')
        page.wait_for_function('() => !migrationBusy')
        assert not page.locator('#migration-compare-result article').count(), 'rekonfigurácia vymaže starú snímku'
        page.fill('#migration-compare-password', COMPARE_PASSWORD_MARKER)
        page.click('#migration-compare-btn')
        page.wait_for_selector("[data-compare-row='duplicate-running']")
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#migration-reset-btn')
        page.wait_for_selector('#migration-reset-btn', state='detached')
        assert not page.locator('#migration-compare-result article').count(), 'reset vymaže výsledok porovnania'
        assert page.locator('#migration-compare-password').input_value() == ''
        assert compare_attempts['count'] == 4
        page.unroute('**/api/recovery/migration/compare', mock_compare)


        page.evaluate("document.getElementById('recovery-refs').open = true")
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
        page.evaluate("document.getElementById('recovery-refs').open = true")
        page.click("#recovery-subnav [data-rsec='snapshot']")
        page.click('#snapshot-load-btn')
        page.wait_for_selector('#snapshot-result details')
        check(page, 'snapshot', vp)
        if width == 390:
            page.screenshot(path=str(OUT / 'm-snapshot.png'), full_page=True)

        assert not page.locator('#tab-restore').count(), 'Obnova súborov je sekcia záložky Obnova a migrácia'
        page.click('#tab-recovery')
        page.click("#recovery-subnav [data-rsec='files']")
        page.wait_for_selector("[data-restore-step='1']:not(.hidden)")
        page.wait_for_function("() => restorePreviewItems.length > 0 && document.getElementById('restore-archive-select').value")
        check(page, 'restore-step-archive', vp)
        page.click('#restore-next')
        page.wait_for_selector("[data-restore-step='2']:not(.hidden) .restore-checkbox")
        assert page.locator('#restore-next').is_disabled(), 'bez výberu položky sa nedá pokračovať'
        page.locator(".restore-checkbox[data-path='/etc/network']").check()
        page.locator(".restore-checkbox[data-path='/etc/pve']").check()
        check(page, 'restore-step-items', vp)
        page.click('#restore-next')
        page.wait_for_selector("[data-restore-step='3']:not(.hidden)")
        page.locator("input[name='restore-mode'][value='apply']").check()
        assert page.locator('#restore-next').is_disabled(), 'REVIEW položka pri aplikovaní vyžaduje potvrdenie kontroly'
        page.locator('#restore-ack').check()
        check(page, 'restore-step-mode', vp)
        page.click('#restore-next')
        page.wait_for_selector("[data-restore-step='4']:not(.hidden)")
        assert page.locator('#restore-btn').is_disabled(), 'bez textu OBNOVIT sa obnova nespustí'
        assert '/etc/pve' in page.locator('#restore-plan-summary').inner_text(), 'stage_only položka sa iba pripraví'
        check(page, 'restore-apply-plan', vp)
        if width == 390:
            page.locator('#restore-plan-summary').scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / 'm-restore-plan.png'))
        page.click('#restore-prev')
        page.wait_for_selector("[data-restore-step='3']:not(.hidden)")

        # Krok obnovy po havárii otvorí sprievodcu s predvybranými položkami a návratom späť.
        page.evaluate("openFilesRestoreForStep('restore-pve-config')")
        page.wait_for_selector("[data-restore-step='2']:not(.hidden) .restore-checkbox")
        assert page.locator(".restore-checkbox[data-path='/etc/pve']").is_checked()
        assert not page.locator(".restore-checkbox[data-path='/etc/network']").is_checked(), 'predvoľba nahradí predchádzajúci výber'
        assert page.locator("input[name='restore-mode'][value='stage']").is_checked()
        assert 'krok' in page.locator('#restore-back-btn').inner_text() and page.locator('#restore-origin').is_visible()
        check(page, 'restore-dr-preset', vp)
        page.click('#restore-back-btn')
        page.wait_for_selector('#dr-current-step')

        page.click('#tab-history')
        page.wait_for_selector("#backup-history-list button:has-text('Obnoviť')")
        page.click("#backup-history-list button:has-text('Obnoviť') >> nth=0")
        page.wait_for_selector("[data-restore-step='1']:not(.hidden)")
        page.wait_for_function("() => document.getElementById('restore-archive-select').value")
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
            page.click('#recovery-refs > summary')
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
