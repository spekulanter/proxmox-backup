#!/usr/bin/env python3
"""Testy disaster recovery a plánovanej migrácie (API, stav, archív, offline príručka)."""

import io
import re
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module  # noqa: E402
import recovery_data  # noqa: E402
from test_archive import FakeSshClient, FakeStream, create_test_auth_config, make_authed_client  # noqa: E402

REQUIRED_PATHS = {'/etc/pve', '/var/lib/pve-cluster/config.db', '/etc/hosts', '/etc/hostname'}
STAGE_ONLY_PATHS = {'/etc/pve', '/var/lib/pve-cluster/config.db', '/etc/passwd', '/etc/group', '/etc/shadow', '/etc/apt'}
SECRET_MARKER = 'SECRET_HASH_MUST_NOT_LEAK'
EXPECTED_WIKI = {
    'dr-overview', 'new-hardware', 'pve-config-db', 'network', 'disks-fstab', 'users-permissions',
    'ssh', 'systemd-cron', 'autofs-nas', 'vm-lxc', 'post-recovery-checklist',
}


def add_member(tar, name, payload):
    data = payload.encode('utf-8')
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def history_entry(entry_id, days_ago, files, ftp_status='success', skipped=None, now=None):
    now = now or datetime.now()
    return {
        'id': entry_id,
        'filename': f'{entry_id}.tar.gz',
        'timestamp': (now - timedelta(days=days_ago)).isoformat(),
        'files': list(files),
        'skipped': skipped or [],
        'ftp_status': ftp_status,
    }


def test_data_model():
    default_paths = [item['path'] for item in app_module.DEFAULT_BACKUP_FILES]
    assert set(default_paths) == set(recovery_data.RECOVERY_PROFILES), 'každá default položka musí mať DR profil'
    category_ids = set(recovery_data.RESTORE_CATEGORY_IDS)
    assert category_ids == {'required', 'review', 'selective', 'reference', 'optional'}
    wiki_slugs = set(recovery_data.WIKI_ARTICLE_SLUGS)
    assert EXPECTED_WIKI <= wiki_slugs
    assert len(wiki_slugs) == len(recovery_data.WIKI_ARTICLES), 'duplicitný wiki slug'

    for path in default_paths:
        profile = app_module.recovery_profile_for_path(path)
        assert profile['classified'] is True
        assert profile['restore_category'] in category_ids
        assert profile['wiki_slug'] in wiki_slugs, path
        assert 1 <= profile['restore_order'] <= 11, path
        assert profile['sensitivity'] in recovery_data.SENSITIVITY_LEVELS, path
        for key in ('why_backup', 'restore_same_hardware', 'restore_new_hardware', 'when_needed'):
            assert profile[key], (path, key)

    required = {path for path in default_paths if app_module.recovery_profile_for_path(path)['required_for_new_hardware']}
    assert required == REQUIRED_PATHS, required
    stage_only = {path for path in default_paths if not app_module.recovery_profile_for_path(path)['direct_restore_allowed']}
    assert stage_only == STAGE_ONLY_PATHS, stage_only

    pve = app_module.recovery_profile_for_path('/etc/pve')
    assert pve['advanced_restore'] and any('pmxcfs' in warning for warning in pve['warnings'])
    config_db = app_module.recovery_profile_for_path('/var/lib/pve-cluster/config.db')
    assert config_db['advanced_restore'] and any('ADVANCED RESTORE' in warning for warning in config_db['warnings'])
    network = app_module.recovery_profile_for_path('/etc/network')
    assert network['restore_category'] == 'review' and network['hardware_dependent']
    fstab = app_module.recovery_profile_for_path('/etc/fstab')
    assert fstab['restore_category'] == 'review' and fstab['hardware_dependent']
    for path in ('/etc/passwd', '/etc/group', '/etc/shadow'):
        profile = app_module.recovery_profile_for_path(path)
        assert profile['restore_category'] == 'reference' and profile['restore_category_alt'] == 'selective'
        assert profile['requires_review'] is True
    shadow = app_module.recovery_profile_for_path('/etc/shadow')
    assert shadow['sensitivity'] == 'secret'
    assert any(warning.startswith('SECURITY') for warning in shadow['warnings'])
    for path in ('/etc/hosts', '/etc/hostname'):
        profile = app_module.recovery_profile_for_path(path)
        assert profile['restore_category'] == 'required' and profile['restore_category_alt'] == 'review'
    assert app_module.recovery_profile_for_path('/root')['sensitivity'] == 'secret'
    assert app_module.recovery_profile_for_path('/var/lib/vz/template')['restore_category'] == 'optional'
    assert app_module.recovery_profile_for_path('/etc/systemd/system/pve-backup-*.timer')['wildcard'] is True

    custom = app_module.recovery_profile_for_path('/srv/custom-thing/')
    assert custom['classified'] is False and custom['restore_category'] == 'review' and custom['requires_review']

    # Pôvodné tagy a priority zostali bez zmeny.
    by_path = {item['path']: item for item in app_module.DEFAULT_BACKUP_FILES}
    assert by_path['/etc/pve']['tags'] == ['critical', 'sensitive', 'pve upgrade']
    assert by_path['/etc/shadow']['priority'] == 'critical'

    steps = recovery_data.RECOVERY_CHECKLIST
    assert [step['step'] for step in steps] == list(range(1, 12))
    assert all(step['wiki_slug'] in wiki_slugs for step in steps)
    assert len({step['id'] for step in steps}) == 11

    # Wiki: /etc/pve článok vysvetľuje pmxcfs a varuje pred cp -r.
    article = app_module.find_wiki_article('pve-config-db')
    text = json.dumps(article, ensure_ascii=False)
    assert 'pmxcfs' in text and 'cp -r backup/etc/pve /etc/pve' in text and 'systemctl stop pve-cluster' in text
    for block in (b for a in recovery_data.WIKI_ARTICLES for b in a['blocks']):
        assert block['type'] in {'p', 'h', 'ul', 'ol', 'code', 'warn', 'danger', 'tip'}


def test_readiness():
    config = app_module.default_config()
    now = datetime(2026, 10, 1, 12, 0, 0)
    all_selected = [item['path'] for item in config['backup_files'] if item['selected']]

    overview = app_module.build_recovery_overview(config, history=[], now=now)
    assert overview['readiness']['status'] == 'INCOMPLETE'
    assert overview['readiness']['required_total'] == 4

    fresh = [history_entry('a', 1, all_selected, now=now)]
    overview = app_module.build_recovery_overview(config, history=fresh, now=now)
    assert overview['readiness']['status'] == 'READY', overview['readiness']
    assert overview['readiness']['required_ok'] == 4
    totals = sum(category['total'] for category in overview['categories'])
    assert totals == len(config['backup_files'])
    assert all('recovery' in item and 'backup_status' in item for item in overview['items'])
    order = [recovery_data.RESTORE_CATEGORY_IDS.index(item['recovery']['restore_category']) for item in overview['items']]
    assert order == sorted(order)

    stale = [history_entry('a', app_module.RECOVERY_MAX_AGE_DAYS + 5, all_selected, now=now)]
    overview = app_module.build_recovery_overview(config, history=stale, now=now)
    assert overview['readiness']['status'] == 'WARNING'
    assert {issue['path'] for issue in overview['readiness']['issues']} == REQUIRED_PATHS

    local_only = [history_entry('a', 1, all_selected, ftp_status='failed', now=now)]
    overview = app_module.build_recovery_overview(config, history=local_only, now=now)
    assert overview['readiness']['status'] == 'WARNING'
    assert all(item['backup_status']['status'] == 'local_only' for item in overview['items'] if item['path'] in REQUIRED_PATHS)

    # Novšia lokálna záloha + staršia (ešte aktuálna) FTP záloha = OK.
    mixed = [history_entry('a', 1, all_selected, ftp_status='failed', now=now), history_entry('b', 3, all_selected, now=now)]
    assert app_module.build_recovery_overview(config, history=mixed, now=now)['readiness']['status'] == 'READY'

    skipped = [history_entry('a', 1, all_selected, skipped=[{'path': '/var/lib/pve-cluster/config.db', 'reason': 'missing'}], now=now)]
    overview = app_module.build_recovery_overview(config, history=skipped, now=now)
    assert overview['readiness']['status'] == 'INCOMPLETE'
    assert any(issue['level'] == 'error' and issue['path'] == '/var/lib/pve-cluster/config.db' for issue in overview['readiness']['issues'])

    deselected = json.loads(json.dumps(config))
    for key in ('backup_files', 'auto_backup_files'):
        for item in deselected[key]:
            if item['path'] == '/etc/hosts':
                item['selected'] = False
    overview = app_module.build_recovery_overview(deselected, history=fresh, now=now)
    assert overview['readiness']['status'] == 'WARNING'

    auto_missing = json.loads(json.dumps(config))
    auto_missing['auto_backup_enabled'] = True
    for item in auto_missing['auto_backup_files']:
        if item['path'] == '/etc/pve':
            item['selected'] = False
    overview = app_module.build_recovery_overview(auto_missing, history=fresh, now=now)
    assert overview['readiness']['status'] == 'WARNING'
    assert any('Automatická' in issue['message'] for issue in overview['readiness']['issues'])


def test_restore_guards():
    source = {'mode': 'remote_ssh', 'ssh': {'host': 'pve.example', 'port': 22, 'username': 'root', 'password': 'x'}}
    for path in STAGE_ONLY_PATHS:
        try:
            app_module.run_restore_job('missing-archive', [path], source, acknowledged_paths=[path])
            raise AssertionError(f'{path} sa nesmie dať aplikovať priamo')
        except app_module.RestoreAcknowledgementRequired:
            raise AssertionError(f'{path} má byť blokované, nie iba vyžadovať potvrdenie')
        except ValueError as exc:
            assert 'priamo neprepisuje' in str(exc), exc

    try:
        app_module.run_restore_job('missing-archive', ['/etc/network', '/etc/fstab'], source)
        raise AssertionError('REVIEW položky vyžadujú potvrdenie')
    except app_module.RestoreAcknowledgementRequired as exc:
        assert exc.paths == ['/etc/network', '/etc/fstab']

    # Po potvrdení / pri stage režime / pri OPTIONAL položke validácia prejde a zastaví sa až na archíve.
    for kwargs in (
        {'selected_paths': ['/etc/network'], 'acknowledged_paths': ['/etc/network']},
        {'selected_paths': [], 'stage_paths': ['/etc/shadow', '/etc/pve']},
        {'selected_paths': ['/var/lib/vz/template']},
        {'selected_paths': ['/etc/shadow'], 'stage_paths': ['/etc/shadow']},
    ):
        try:
            app_module.run_restore_job('missing-archive', kwargs.pop('selected_paths'), source, **kwargs)
            raise AssertionError('archív neexistuje')
        except FileNotFoundError:
            pass

    try:
        app_module.run_restore_job('missing-archive', [], source, stage_paths=['/etc/cron*'])
        raise AssertionError('wildcard nie je obnoviteľný')
    except ValueError as exc:
        assert 'nie je povolená' in str(exc) or 'Wildcard' in str(exc)


def test_info_commands_resilience():
    output = app_module.run_info_command(['definitely-missing-command-xyz', '--version'])
    assert 'command_not_found' in output
    parsed = app_module.parse_info_command_output(output)
    assert parsed['error'] and parsed['stdout'] == ''
    parsed_ok = app_module.parse_info_command_output('$ ip -br link\nexit_code=0\n\n--- stdout ---\nlo UNKNOWN\n\n--- stderr ---\n\n')
    assert parsed_ok['exit_code'] == 0 and parsed_ok['stdout'] == 'lo UNKNOWN' and parsed_ok['command'] == 'ip -br link'

    snapshot_names = {name for name, _label in recovery_data.HOST_SNAPSHOT_FILES}
    info_names = {name for name, _command in app_module.INFO_COMMANDS}
    assert snapshot_names <= info_names, snapshot_names - info_names
    for required in ('hostname.txt', 'uname-a.txt', 'ip-br-link.txt', 'ip-br-addr.txt', 'lspci-nn.txt',
                     'systemctl-failed.txt', 'pvs.txt', 'vgs.txt', 'lvs.txt', 'zpool-status.txt'):
        assert required in info_names, required

    original_commands = app_module.INFO_COMMANDS
    with tempfile.TemporaryDirectory(prefix='pve-recovery-info-', dir=str(ROOT)) as workdir:
        source = Path(workdir) / 'hosts'
        source.write_text('127.0.0.1 localhost\n', encoding='utf-8')
        archive_path = Path(workdir) / 'backup.tar.gz'
        try:
            app_module.INFO_COMMANDS = [
                ('hostname.txt', ['hostname']),
                ('zpool-status.txt', ['definitely-missing-zpool-xyz', 'status']),
            ]
            report = app_module.create_backup_archive(
                [{'path': str(source), 'name': 'hosts'}, {'path': '/etc/pve', 'name': 'PVE'}],
                str(archive_path),
            )
        finally:
            app_module.INFO_COMMANDS = original_commands
        assert 'recovery-manifest.json' in report['generated_info']
        with tarfile.open(archive_path, 'r:gz') as tar:
            names = tar.getnames()
            assert 'backup-info/zpool-status.txt' in names
            zpool = tar.extractfile('backup-info/zpool-status.txt').read().decode()
            assert 'command_not_found' in zpool
            manifest = json.loads(tar.extractfile('backup-info/recovery-manifest.json').read())
            readme = tar.extractfile('backup-info/README-RESTORE.txt').read().decode()
        pve_entry = next(item for item in manifest['items'] if item['path'] == '/etc/pve')
        assert pve_entry['restore_category'] == 'required' and pve_entry['restore_policy'] == 'stage_only'
        assert 'Klasifikácia obnovy na novom HW' in readme and 'ADVANCED RESTORE' in readme

    class FlakySshClient(FakeSshClient):
        def exec_command(self, command, timeout=None):
            if command.startswith('zpool') or command.startswith('lspci'):
                raise OSError('channel closed')
            if command.startswith(('hostname', 'pvs', 'ip ')):
                self.commands.append(command)
                return None, FakeStream(exit_code=127), FakeStream('bash: command not found', exit_code=127)
            return super().exec_command(command, timeout)

    fake = FlakySshClient()
    remote = app_module.RemoteSshBackupSource(
        {'mode': 'remote_ssh', 'ssh': {'host': 'pve.example', 'port': 22, 'username': 'root', 'password': 'x'}},
        ssh_client_factory=lambda: fake,
    )
    with tempfile.TemporaryDirectory(prefix='pve-recovery-remote-', dir=str(ROOT)) as workdir:
        report = remote.create_archive([{'path': '/etc/hostname', 'name': 'hostname'}], str(Path(workdir) / 'r.tar.gz'))
    assert 'zpool-status.txt' in report['generated_info'] and 'lspci-nn.txt' in report['generated_info']
    assert 'recovery-manifest.json' in report['generated_info']
    written = fake.sftp.files
    assert 'error=channel closed' in written['/tmp/pve-host-backup-info.TEST/backup-info/zpool-status.txt']
    assert 'exit_code=127' in written['/tmp/pve-host-backup-info.TEST/backup-info/pvs.txt']


def test_api_and_snapshot():
    with tempfile.TemporaryDirectory(prefix='pve-recovery-api-', dir=str(ROOT)) as workdir:
        workdir = Path(workdir)
        originals = (app_module.CONFIG_FILE, app_module.BACKUP_HISTORY_FILE, app_module.BACKUP_STORAGE_DIR, app_module.list_ftp_backups)
        app_module.CONFIG_FILE = str(workdir / 'backup_config.json')
        app_module.BACKUP_HISTORY_FILE = str(workdir / 'backup_history.json')
        app_module.BACKUP_STORAGE_DIR = str(workdir / 'backups')
        app_module.list_ftp_backups = lambda cfg: {'available': False, 'warning': 'test', 'archives': []}
        (workdir / 'backups').mkdir()
        original_auth, secret, _password = create_test_auth_config(workdir / 'auth_config.json')
        try:
            anonymous = app_module.app.test_client()
            assert anonymous.get('/api/recovery/overview').status_code == 401
            assert anonymous.get('/api/recovery/wiki/network').status_code == 401

            client = make_authed_client(secret)
            files = client.get('/api/files').get_json()
            assert all('recovery' in item for item in files)
            assert next(item for item in files if item['path'] == '/etc/shadow')['recovery']['restore_category'] == 'reference'
            auto_files = client.get('/api/auto-files').get_json()
            assert all('recovery' in item for item in auto_files)

            response = client.post('/api/files/0/toggle')
            assert response.status_code == 200
            response = client.post('/api/auto-files/selection', json={'selected': True})
            assert all('recovery' in item for item in response.get_json()['backup_files'])
            saved = Path(app_module.CONFIG_FILE).read_text(encoding='utf-8')
            assert '"recovery"' not in saved, 'DR metadáta sa nesmú ukladať do backup_config.json'
            assert json.loads(saved)['config_version'] == app_module.CONFIG_VERSION

            config_payload = client.get('/api/config').get_json()
            for key in ('config', 'backup_history', 'selected_count', 'critical_total', 'backup_categories'):
                assert key in config_payload
            assert [category['id'] for category in config_payload['restore_categories']] == recovery_data.RESTORE_CATEGORY_IDS

            overview = client.get('/api/recovery/overview').get_json()
            assert overview['success'] and overview['readiness']['status'] == 'INCOMPLETE'
            assert len(overview['checklist']) == 11 and len(overview['wiki']) == len(recovery_data.WIKI_ARTICLES)

            assert len(client.get('/api/recovery/checklist').get_json()['steps']) == 11
            index = client.get('/api/recovery/wiki').get_json()['articles']
            for article in index:
                response = client.get(f"/api/recovery/wiki/{article['slug']}")
                assert response.status_code == 200, article['slug']
                assert response.get_json()['article']['blocks']
            related = client.get('/api/recovery/wiki/users-permissions').get_json()['related_items']
            assert {'/etc/passwd', '/etc/group', '/etc/shadow'} <= {item['path'] for item in related}
            assert client.get('/api/recovery/wiki/neexistuje').status_code == 404

            archive = workdir / 'backups' / 'snap.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                add_member(tar, 'etc/shadow', f'root:{SECRET_MARKER}:19000::::::\n')
                add_member(tar, 'etc/hostname', 'cubi\n')
                add_member(tar, 'backup-info/ip-br-link.txt', '$ ip -br link\nexit_code=0\n\n--- stdout ---\nenp2s0 UP 58:47:ca:00:00:01\n\n--- stderr ---\n\n')
                add_member(tar, 'backup-info/zpool-status.txt', '$ zpool status\ncommand_not_found=[Errno 2] zpool\n')
                add_member(tar, 'backup-info/crontab-root.txt', f'$ crontab -l\nexit_code=0\n\n--- stdout ---\n{SECRET_MARKER}\n')
                add_member(tar, 'backup-info/network-interfaces.txt', f'{SECRET_MARKER}\n')
            bad_archive = workdir / 'backups' / 'bad.tar.gz'
            with tarfile.open(bad_archive, 'w:gz') as tar:
                add_member(tar, '../backup-info/hostname.txt', 'x')
            now = datetime.now()
            app_module.save_backup_history([
                {'id': 'snap', 'filename': archive.name, 'local_path': str(archive), 'timestamp': now.isoformat(),
                 'ftp_status': 'success', 'files': sorted(REQUIRED_PATHS), 'skipped': []},
                {'id': 'bad', 'filename': bad_archive.name, 'local_path': str(bad_archive), 'timestamp': (now - timedelta(days=2)).isoformat()},
            ])

            response = client.get('/api/recovery/snapshot/snap')
            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_data(as_text=True)
            assert SECRET_MARKER not in body, 'snapshot nesmie vrátiť konfiguračné súbory ani tajomstvá'
            data = response.get_json()
            assert data['reference_only'] is True
            by_file = {item['file']: item for item in data['files']}
            assert set(by_file) == {name for name, _label in recovery_data.HOST_SNAPSHOT_FILES}
            assert 'crontab-root.txt' not in by_file and 'network-interfaces.txt' not in by_file
            assert by_file['ip-br-link.txt']['stdout'] == 'enp2s0 UP 58:47:ca:00:00:01'
            assert by_file['zpool-status.txt']['available'] and by_file['zpool-status.txt']['error']
            assert by_file['lsblk-f.txt']['available'] is False
            assert client.get('/api/recovery/snapshot/bad').status_code == 400
            assert client.get('/api/recovery/snapshot/missing').status_code == 404

            preview = client.get('/api/restore/preview/snap').get_json()
            shadow = next(item for item in preview['items'] if item['path'] == '/etc/shadow')
            assert shadow['recovery']['direct_restore_allowed'] is False
            assert SECRET_MARKER not in json.dumps(preview)
            members = client.get('/api/restore/preview/snap/members?path=/etc/shadow').get_data(as_text=True)
            assert SECRET_MARKER not in members, 'zoznam členov archívu nesmie obsahovať obsah súborov'

            overview = client.get('/api/recovery/overview').get_json()
            assert overview['readiness']['status'] == 'READY', overview['readiness']
            assert SECRET_MARKER not in json.dumps(overview)

            html = client.get('/').get_data(as_text=True)
            for marker in (
                'id="tab-recovery"', 'id="content-recovery"', 'id="file-filter-chips"', 'id="item-detail-overlay"',
                "label: 'New HW – Required'", "label: 'New HW – Review'", "label: 'New HW – Selective'",
                "label: 'Reference only'", "label: 'Sensitive'", "label: 'Network'", "label: 'Storage'",
                "label: 'Critical'", "label: 'Recommended'", "label: 'Optional'",
                'name="restore-mode" value="stage" class="mt-1 h-4 w-4 shrink-0" checked',
                'id="restore-ack"', '/api/recovery/overview', '/api/recovery/snapshot/', '/api/recovery/wiki/',
                'name="viewport" content="width=device-width, initial-scale=1.0"',
            ):
                assert marker in html, marker
            assert 'setAllRestoreSelection(true)' not in html, 'restore nesmie ponúkať hromadné „vybrať všetko“'
            assert 'data-path="${escapeHtml(item.path)}" checked' not in html, 'restore položky nesmú byť predvolene vybrané'
            assert 'Proxmox Backup Manager' in html
        finally:
            app_module.CONFIG_FILE, app_module.BACKUP_HISTORY_FILE, app_module.BACKUP_STORAGE_DIR, app_module.list_ftp_backups = originals
            app_module.AUTH_CONFIG_FILE = original_auth
            app_module.sync_flask_secret()


def info_file(stdout, command='cmd', exit_code=0):
    return f'$ {command}\nexit_code={exit_code}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n\n'


def build_host_archive(path, jobs_json=True, hook_report=None, node_in_jobs='nuc'):
    """Syntetický archív hosta s backup-info, sieťou, NAS mapami a configmi hostí."""
    jobs = [
        {'id': 'backup-qnap', 'vmid': '100,113', 'node': node_in_jobs, 'enabled': 0, 'storage': 'qnap.autofs',
         'schedule': 'sun 04:00', 'script': '/usr/local/bin/missing_hook.sh'},
    ]
    with tarfile.open(path, 'w:gz') as tar:
        for directory in ('usr/local/bin', 'etc/systemd/system'):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        add_member(tar, 'usr/local/bin/figlet', 'binary')
        add_member(tar, 'etc/systemd/system/pve-backup-qnap.timer', '[Timer]\n')
        add_member(tar, 'etc/systemd/system/pve-backup-qnap.service', 'Environment=NODE=nuc\n')
        add_member(tar, 'etc/hostname', 'nuc\n')
        add_member(tar, 'etc/hosts', '127.0.0.1 localhost\n192.0.2.2 nuc.lan nuc\n')
        add_member(tar, 'etc/resolv.conf', 'nameserver 192.0.2.1\n')
        add_member(tar, 'etc/network/interfaces', (
            'auto lo\niface lo inet loopback\n\niface enp45s0 inet manual\n\nauto vmbr0\niface vmbr0 inet manual\n'
            '\tbridge-ports enp45s0\n\tbridge-vlan-aware yes\n\tbridge-vids 2-4094\n\n'
            'auto vmbr0.200\niface vmbr0.200 inet static\n\taddress 192.0.2.2/24\n\tgateway 192.0.2.1\n'
        ))
        add_member(tar, 'etc/auto.master', '+auto.master\n/autofs /etc/auto.nfs --timeout=600\n')
        add_member(tar, 'etc/auto.nfs', 'qnap -fstype=nfs,rw,vers=4.0 198.51.100.2:/Backups/Host\n')
        add_member(tar, 'etc/pve/storage.cfg', (
            'lvmthin: local-lvm\n\tthinpool data\n\tvgname pve\n\tcontent rootdir,images\n\n'
            'dir: qnap.autofs\n\tdisable\n\tpath /autofs/qnap\n\tcontent backup\n\n'
            'cifs: smb\n\tserver 198.51.100.9\n\tshare backup\n\tusername admin\n\tpassword ' + SECRET_MARKER + '\n\tcontent backup\n'
        ))
        add_member(tar, 'etc/pve/jobs.cfg', (
            f'vzdump: backup-qnap\n\tnode {node_in_jobs}\n\tvmid 100,113\n\tstorage qnap.autofs\n\tenabled 0\n'
            '\tscript /usr/local/bin/missing_hook.sh\n'
        ))
        add_member(tar, 'etc/pve/nodes/nuc/lxc/113.conf', (
            'hostname: proxmox-backup\nnet0: name=eth0,bridge=vmbr0,gw=192.0.2.1,hwaddr=BC:24:11:00:00:01,'
            'ip=127.0.0.1/8,tag=200,type=veth\nrootfs: local-lvm:vm-113-disk-0,size=20G\n\n[snap]\nparent: x\n'
        ))
        add_member(tar, 'etc/shadow', f'root:{SECRET_MARKER}:19000::::::\n')
        add_member(tar, 'root/.ssh/id_ed25519', SECRET_MARKER)
        add_member(tar, 'backup-info/hostname.txt', info_file('nuc', 'hostname'))
        add_member(tar, 'backup-info/pveversion-v.txt', info_file('proxmox-ve: 9.2.0\npve-manager: 9.2.3', 'pveversion -v'))
        add_member(tar, 'backup-info/ip-br-link.txt', info_file('enp45s0 UP 34:5a:60:60:57:b6', 'ip -br link'))
        add_member(tar, 'backup-info/ip-route.txt', info_file('default via 192.0.2.1 dev vmbr0.200', 'ip route'))
        add_member(tar, 'backup-info/qm-list.txt', info_file(
            '      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n'
            '       100 home-assistant       running    8192              60.00 1790\n'
            '       122 mikrotik-chr         running    512                8.00 2324', 'qm list'))
        add_member(tar, 'backup-info/pct-list.txt', info_file(
            'VMID       Status     Lock         Name\n113        running                 proxmox-backup\n'
            '124        stopped                 games', 'pct list'))
        add_member(tar, 'backup-info/crontab-root.txt', info_file(SECRET_MARKER, 'crontab -l'))
        if jobs_json:
            add_member(tar, 'backup-info/pve-backup-jobs.json', info_file(json.dumps(jobs), 'pvesh get /cluster/backup'))
        if hook_report is not None:
            add_member(tar, 'backup-info/hook-scripts.txt', info_file(hook_report, 'sh -c ...'))


def test_wiki_additions():
    slugs = set(recovery_data.WIKI_ARTICLE_SLUGS)
    assert 'backup-manager-recovery' in slugs
    pve_text = json.dumps(app_module.find_wiki_article('pve-config-db'), ensure_ascii=False)
    assert 'jobs.cfg' in pve_text and 'NODE=' in pve_text and 'PRED obnovou' in pve_text
    migration = json.dumps(app_module.find_wiki_article('hw-migration'), ensure_ascii=False)
    for expected in ('Presun systémového disku', 'nový host vedľa starého', 'onboot 0', 'Cesta späť', 'Vyradenie starého servera', 'iba na jednom hoste'):
        assert expected in migration, expected
    app_text = json.dumps(app_module.find_wiki_article('backup-manager-recovery'), ensure_ascii=False)
    assert 'install_in_lxc.sh' in app_text and 'pct restore' in app_text and 'auth_config.json' in app_text


def test_risk_analysis():
    with tempfile.TemporaryDirectory(prefix='pve-recovery-risk-', dir=str(ROOT)) as workdir:
        archive = Path(workdir) / 'host.tar.gz'
        build_host_archive(archive, node_in_jobs='nuc')
        facts = app_module.read_archive_facts(str(archive))
        assert 'etc/shadow' not in facts['files'] and 'root/.ssh/id_ed25519' not in facts['files']
        assert 'crontab-root.txt' not in facts['info']
        analysis = app_module.analyze_recovery_risks(facts)
        by_id = {risk['id']: risk for risk in analysis['risks']}
        assert analysis['node'] == 'nuc'
        uncovered = by_id['guests-without-vzdump']
        assert uncovered['level'] == 'error'
        assert any(item.startswith('122 mikrotik-chr') for item in uncovered['items'])
        assert any(item.startswith('124 games') for item in uncovered['items'])
        assert not any(item.startswith(('100 ', '113 ')) for item in uncovered['items'])
        assert 'job-node-mismatch' not in by_id
        assert '/usr/local/bin/missing_hook.sh' in by_id['hook-script-missing']['items'][0]
        assert 'podľa obsahu archívu' in by_id['hook-script-missing']['items'][0]
        assert by_id['jobs-disabled']['level'] == 'info'

        archive2 = Path(workdir) / 'host2.tar.gz'
        build_host_archive(archive2, jobs_json=False, hook_report='OK /usr/local/bin/missing_hook.sh', node_in_jobs='oldnode')
        analysis = app_module.analyze_recovery_risks(app_module.read_archive_facts(str(archive2)))
        by_id = {risk['id']: risk for risk in analysis['risks']}
        assert analysis['jobs'][0]['vmids'] == {100, 113}, 'fallback na jobs.cfg'
        assert by_id['job-node-mismatch']['level'] == 'error'
        assert 'hook-script-missing' not in by_id, 'hook-scripts.txt z hosta má prednosť'

        legacy = {'info': {'ip-addr.txt': app_module.parse_info_command_output(info_file(
            '1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n    link/loopback 00:00:00:00:00:00\n'
            '2: enp45s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n    link/ether 34:5a:60:60:57:b6 brd ff:ff:ff:ff:ff:ff\n'
            '6: vmbr0.200@vmbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n    link/ether 34:5a:60:60:57:b6 brd ff:ff:ff:ff:ff:ff\n'
            '11: tap102i0: <BROADCAST,UP,LOWER_UP> mtu 1500\n    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff', 'ip addr'))}}
        summary = app_module.nic_summary(legacy)
        assert 'enp45s0' in summary and '34:5a:60:60:57:b6' in summary and 'vmbr0.200' in summary
        assert 'tap102i0' not in summary, 'virtuálne tap/veth rozhrania hostí sa vynechajú'

        empty = app_module.build_recovery_risks([])
        assert {risk['id'] for risk in empty['risks']} == {'no-local-archive'}


def test_downloads_and_handbook():
    with tempfile.TemporaryDirectory(prefix='pve-recovery-hb-', dir=str(ROOT)) as workdir:
        workdir = Path(workdir)
        originals = (app_module.CONFIG_FILE, app_module.BACKUP_HISTORY_FILE, app_module.BACKUP_STORAGE_DIR, app_module.list_ftp_backups)
        original_detect = app_module.detect_own_ip
        app_module.CONFIG_FILE = str(workdir / 'backup_config.json')
        app_module.BACKUP_HISTORY_FILE = str(workdir / 'backup_history.json')
        app_module.BACKUP_STORAGE_DIR = str(workdir / 'backups')
        app_module.list_ftp_backups = lambda cfg: {'available': False, 'warning': 'test', 'archives': []}
        app_module.detect_own_ip = lambda host, port=22: '127.0.0.1' if host == '192.0.2.2' else None
        (workdir / 'backups').mkdir()
        original_auth, secret, _password = create_test_auth_config(workdir / 'auth_config.json')
        try:
            config = app_module.default_config()
            config['ftp_config'].update({'host': '198.51.100.2', 'username': 'maros', 'password': 'FTP-' + SECRET_MARKER,
                                         'remote_dir': '/Proxmox Backup Manager/Host'})
            config['source_config']['ssh'].update({'host': '192.0.2.2', 'password': 'SSH-' + SECRET_MARKER})
            config['auto_backup_enabled'] = True
            config['auto_backup_frequency'] = 'weekly'
            config['auto_backup_day'] = 5
            config['auto_backup_hour'] = 15
            app_module.save_config(config)
            archive = workdir / 'backups' / 'proxmox_backup_host.tar.gz'
            build_host_archive(archive)
            app_module.save_backup_history([{
                'id': 'hb1', 'filename': archive.name, 'local_path': str(archive), 'timestamp': datetime.now().isoformat(),
                'ftp_status': 'success', 'files': sorted(REQUIRED_PATHS), 'skipped': [],
            }])

            anonymous = app_module.app.test_client()
            assert anonymous.get('/api/recovery/handbook').status_code == 401

            client = make_authed_client(secret)
            overview = client.get('/api/recovery/overview').get_json()
            risk_ids = {risk['id'] for risk in overview['risks']['risks']}
            assert {'guests-without-vzdump', 'hook-script-missing', 'no-offline-copy'} <= risk_ids
            assert overview['readiness']['status'] == 'READY', 'riziká nemenia readiness'

            response = client.get('/api/backups/hb1/download')
            assert response.status_code == 200
            response.close()
            assert app_module.load_backup_history()[0].get('downloaded_at')
            overview = client.get('/api/recovery/overview').get_json()
            assert 'no-offline-copy' not in {risk['id'] for risk in overview['risks']['risks']}

            response = client.get('/api/recovery/handbook')
            assert response.status_code == 200, response.get_data(as_text=True)[:500]
            assert response.headers['Content-Disposition'].startswith('attachment; filename="DR-prirucka-nuc-')
            html = response.get_data(as_text=True)
            assert SECRET_MARKER not in html, 'príručka nesmie obsahovať heslá ani citlivé súbory'
            assert 'password ***' in html, 'heslo zo storage.cfg je zamaskované'
            for expected in (
                'nuc.lan', '192.0.2.2/24', 'vmbr0.200', 'bridge-vlan-aware yes', 'VLAN 200',
                '198.51.100.2:/Backups/Host', '/Proxmox Backup Manager/Host', 'mount -t nfs -o rw,vers=4.0',
                'týždenne – sobota 15:00', '122 mikrotik-chr', 'pct restore 113', 'LXC <strong>113</strong>',
                'pve-backup-qnap.timer', '/autofs/qnap/dump', 'Obnova Proxmox Backup Managera (LXC)',
                'missing_hook.sh',
            ):
                assert expected in html, expected
            loop_line = next(line for line in html.splitlines() if line.startswith('for id in'))
            assert '113' not in loop_line, 'LXC appky sa v hromadnom restore neobnovuje znova'
            assert not re.search(r'<(script|link)[^>]+(src|href)="https?://', html), 'príručka musí byť offline'

            empty_history = client.get('/api/recovery/handbook?inline=1')
            assert empty_history.headers['Content-Disposition'].startswith('inline;')
            app_module.save_backup_history([])
            html = client.get('/api/recovery/handbook').get_data(as_text=True)
            assert 'Príručka bez údajov z archívu' in html and SECRET_MARKER not in html
        finally:
            app_module.CONFIG_FILE, app_module.BACKUP_HISTORY_FILE, app_module.BACKUP_STORAGE_DIR, app_module.list_ftp_backups = originals
            app_module.detect_own_ip = original_detect
            app_module.AUTH_CONFIG_FILE = original_auth
            app_module.sync_flask_secret()


def test_migration_data_model():
    methods = {method['id'] for method in recovery_data.MIGRATION_METHODS}
    assert methods == {'disk_move', 'side_by_side'}
    assert len(methods) == len(recovery_data.MIGRATION_METHODS)
    for method in recovery_data.MIGRATION_METHODS:
        assert method['title'] and method['description']
    steps = recovery_data.MIGRATION_STEPS
    assert len({step['id'] for step in steps}) == len(steps), 'duplicitné migration step id'
    for step in steps:
        assert step['title'] and step['goal']
        assert step['methods'] and set(step['methods']) <= methods
        assert step['wiki_slug'] in recovery_data.WIKI_ARTICLE_SLUGS
        for key in ('tasks', 'commands', 'warnings'):
            assert isinstance(step[key], list), (step['id'], key)
    by_method = {
        method: [step['id'] for step in steps if method in step['methods']]
        for method in methods
    }
    assert by_method['side_by_side'] == [
        'prepare', 'fresh-backups', 'new-host', 'selective-config', 'move-guests',
        'cutover', 'verify-rollback', 'retire-old',
    ]
    assert by_method['disk_move'] == [
        'prepare', 'fresh-backups', 'disk-move', 'cutover', 'verify-rollback', 'retire-old',
    ]


def test_migration_api():
    """Migrácia iba eviduje stav; testy používajú archív a nikdy nepripájajú SSH."""
    with tempfile.TemporaryDirectory(prefix='pve-migration-', dir=str(ROOT)) as workdir:
        workdir = Path(workdir)
        globals_to_patch = ('CONFIG_FILE', 'BACKUP_HISTORY_FILE', 'BACKUP_STORAGE_DIR', 'MIGRATION_STATE_FILE', 'list_ftp_backups')
        originals = {name: getattr(app_module, name) for name in globals_to_patch}
        app_module.CONFIG_FILE = str(workdir / 'backup_config.json')
        app_module.BACKUP_HISTORY_FILE = str(workdir / 'backup_history.json')
        app_module.BACKUP_STORAGE_DIR = str(workdir / 'backups')
        app_module.MIGRATION_STATE_FILE = str(workdir / 'migration_state.json')
        app_module.list_ftp_backups = lambda cfg: {'available': False, 'warning': 'test', 'archives': []}
        (workdir / 'backups').mkdir()
        original_auth, secret, _password = create_test_auth_config(workdir / 'auth_config.json')
        base = '/api/recovery/migration'
        initial = {
            'method': 'side_by_side',
            'old_host': {'ip': '192.0.2.2', 'hostname': 'old-pve.example'},
            'new_host': {'ip': '192.0.2.3', 'hostname': 'new-pve.example'},
        }
        try:
            config = app_module.default_config()
            config['ftp_config']['password'] = 'FTP-' + SECRET_MARKER
            config['source_config']['ssh']['password'] = 'SSH-' + SECRET_MARKER
            app_module.save_config(config)
            original_config = Path(app_module.CONFIG_FILE).read_bytes()
            app_module.save_backup_history([])
            anonymous = app_module.app.test_client()
            assert anonymous.get(base).status_code == 401
            for path, body in (
                (base, initial), (base + '/steps/prepare', {'completed': True}),
                (base + '/guests/100', {'status': 'stopped_on_old'}), (base + '/reset', {}),
            ):
                response = anonymous.post(path, json=body)
                assert response.status_code == 401 and response.is_json, path

            client = make_authed_client(secret)
            # Helper pridáva CSRF iba ak chýba hlavička; prázdny/zlý token overí skutočnú ochranu.
            for path, body in (
                (base, initial), (base + '/steps/prepare', {'completed': True}),
                (base + '/guests/100', {'status': 'stopped_on_old'}), (base + '/reset', {}),
            ):
                response = client.post(path, json=body, headers={'X-CSRF-Token': ''})
                assert response.status_code == 403 and response.is_json, path
            assert client.post(base, json=initial, headers={'X-CSRF-Token': 'invalid'}).status_code == 403

            response = client.get(base)
            assert response.status_code == 200 and response.get_json()['success']
            payload = response.get_json()
            assert payload['state']['version'] == 1
            assert not payload['state']['method'] and not payload['state']['started_at']
            assert payload['inventory']['available'] is False
            assert not payload['guests']
            assert {method['id'] for method in payload['methods']} == {'disk_move', 'side_by_side'}

            invalid_base = [
                {}, {'method': 'cluster'}, {'method': ['side_by_side']},
                {**initial, 'password': SECRET_MARKER},
                {**initial, 'old_host': {'ip': '999.1.1.1', 'hostname': 'old-pve'}},
                {**initial, 'new_host': {'ip': '192.0.2.3; reboot', 'hostname': 'new-pve'}},
                {**initial, 'new_host': {'ip': '192.0.2.3', 'hostname': 'bad host'}},
                {**initial, 'new_host': {'ip': '192.0.2.3', 'hostname': '<script>alert(1)</script>'}},
                {**initial, 'old_host': []},
                {**initial, 'new_host': {'ip': '192.0.2.3', 'hostname': 'new-pve', 'password': SECRET_MARKER}},
                {**initial, 'new_host': {'ip': '192.0.2.2', 'hostname': 'new-pve'}},
                {**initial, 'new_host': {'ip': '192.0.2.3', 'hostname': 'OLD-PVE.EXAMPLE'}},
                {**initial, 'new_host': {'ip': '192.0.2.3', 'hostname': 'old-pve.other.example'}},
                {**initial, 'old_host': {'ip': '2001:db8::2', 'hostname': 'old-pve'},
                 'new_host': {'ip': '2001:db8:0:0:0:0:0:2', 'hostname': 'new-pve'}},
            ]
            for body in invalid_base:
                response = client.post(base, json=body)
                assert response.status_code == 400 and response.is_json, (body, response.get_data(as_text=True))
            for malformed in ('{', '[]', 'null', '"side_by_side"'):
                response = client.post(base, data=malformed, content_type='application/json')
                assert response.status_code == 400 and response.is_json, malformed
            assert not client.get(base).get_json()['state']['started_at'], 'neplatný vstup nesmie začať migráciu'

            # Najnovší archív obsahuje VM aj LXC, vrátane hostí mimo vzdump jobu.
            archive = workdir / 'backups' / 'migration.tar.gz'
            build_host_archive(archive)
            app_module.save_backup_history([{
                'id': 'migration', 'filename': archive.name, 'local_path': str(archive),
                'timestamp': datetime.now().isoformat(), 'ftp_status': 'success',
                'files': sorted(REQUIRED_PATHS), 'skipped': [],
            }])
            response = client.post(base, json=initial)
            assert response.status_code == 200, response.get_data(as_text=True)
            payload = client.get(base).get_json()
            state = payload['state']
            assert state['method'] == 'side_by_side' and state['version'] == 1
            assert state['old_host'] == initial['old_host'] and state['new_host'] == initial['new_host']
            assert state['started_at'] and state['updated_at'] and not state['finished_at']
            assert payload['inventory']['available'] is True
            assert payload['cutover_ready'] is False
            guests = {int(guest['vmid']): guest for guest in payload['guests']}
            assert set(guests) == {100, 113, 122, 124}
            assert guests[100]['type'] == 'VM' and guests[113]['type'] == 'LXC'
            assert guests[100]['name'] == 'home-assistant' and guests[124]['name'] == 'games'
            assert {vmid for vmid, guest in guests.items() if guest['backed_up']} == {100, 113}
            assert all(guest['status'] == 'pending' for guest in guests.values())
            assert all(set(guest['allowed_transitions']) == {'stopped_on_old', 'skipped'} for guest in guests.values())
            assert 'guests-without-vzdump' in {risk['id'] for risk in payload['risks']['risks']}
            for vmid, guest in guests.items():
                old_commands = '\n'.join(guest['commands']['old_host'])
                new_commands = '\n'.join(guest['commands']['new_host'])
                tool, restore = ('qm', 'qmrestore') if guest['type'] == 'VM' else ('pct', 'pct restore')
                assert f'{tool} set {vmid} --onboot 0' in old_commands
                assert f'vzdump {vmid}' in old_commands and '--mode stop' in old_commands
                assert restore in new_commands and str(vmid) in new_commands
            state_file = Path(app_module.MIGRATION_STATE_FILE)
            assert state_file.stat().st_mode & 0o777 == 0o600
            persisted = state_file.read_text(encoding='utf-8')
            assert json.loads(persisted) == state
            assert SECRET_MARKER not in persisted and 'password' not in persisted.lower()
            assert Path(app_module.CONFIG_FILE).read_bytes() == original_config, 'stav migrácie nemá meniť backup_config'
            assert SECRET_MARKER not in json.dumps(payload)
            assert client.post(base, json={**initial, 'method': 'disk_move'}).status_code == 400
            for suffix in ('/steps/prepare', '/guests/100', '/reset'):
                for malformed in ('{', '[]', 'null'):
                    response = client.post(base + suffix, data=malformed, content_type='application/json')
                    assert response.status_code == 400 and response.is_json, (suffix, malformed)

            for step_id, body in (
                ('unknown', {'completed': True}), ('disk-move', {'completed': True}),
                ('prepare', {'completed': 'true'}), ('prepare', {'completed': 1}),
                ('prepare', {'completed': True, 'password': SECRET_MARKER}),
            ):
                response = client.post(base + '/steps/' + step_id, json=body)
                assert response.status_code == 400, (step_id, body)
            for completed in (True, False, True):
                response = client.post(base + '/steps/prepare', json={'completed': completed})
                assert response.status_code == 200, response.get_data(as_text=True)
                step = client.get(base).get_json()['state']['steps']['prepare']
                assert step['completed'] is completed and step['updated_at']
            # Požiadavka používateľa: cutover chráni iba UI, API umožní evidovať krok.
            assert client.post(base + '/steps/cutover', json={'completed': True}).status_code == 200
            assert client.get(base).get_json()['cutover_ready'] is False

            for vmid, body in (
                ('oops', {'status': 'stopped_on_old'}), ('999999999', {'status': 'stopped_on_old'}),
                ('100', {'status': 'running'}), ('100', {'status': ['pending']}),
                ('100', {'status': 'verified'}), ('100', {'status': 'restored_on_new'}),
                ('100', {'status': 'pending', 'note': []}),
                ('100', {'status': 'pending', 'password': SECRET_MARKER}),
            ):
                response = client.post(base + '/guests/' + vmid, json=body)
                assert response.status_code == 400 and response.is_json, (vmid, body)

            note = '<script>alert("migration")</script> & kontrola po presune'
            for status in ('stopped_on_old', 'restored_on_new', 'verified'):
                response = client.post(base + '/guests/100', json={'status': status, 'note': note})
                assert response.status_code == 200, response.get_data(as_text=True)
                guest_state = client.get(base).get_json()['state']['guests']['100']
                assert guest_state['status'] == status and guest_state['note'] == note and guest_state['updated_at']
                illegal_next = 'skipped' if status in ('restored_on_new', 'verified') else 'pending'
                assert client.post(base + '/guests/100', json={'status': illegal_next}).status_code == 400
            assert client.post(base + '/guests/100', json={'status': 'pending'}).status_code == 400
            assert client.post(base + '/guests/100', json={'status': 'verified', 'note': note}).status_code == 200
            for vmid in (113, 122, 124):
                assert client.post(base + '/guests/' + str(vmid), json={'status': 'skipped', 'note': 'ponechaný'}).status_code == 200
            assert client.get(base).get_json()['cutover_ready'] is True
            assert client.post(base + '/guests/124', json={'status': 'restored_on_new'}).status_code == 400
            assert client.post(base + '/guests/124', json={'status': 'pending'}).status_code == 200
            assert client.get(base).get_json()['cutover_ready'] is False
            for status in ('stopped_on_old', 'skipped'):
                assert client.post(base + '/guests/124', json={'status': status}).status_code == 200

            # Stav overeného hosťa prežije nový archív, v ktorom už na starom hoste nie je.
            new_archive = workdir / 'backups' / 'migration-new.tar.gz'
            with tarfile.open(archive, 'r:gz') as source, tarfile.open(new_archive, 'w:gz') as target:
                for member in source.getmembers():
                    if member.name != 'backup-info/qm-list.txt':
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
                add_member(target, 'backup-info/qm-list.txt', info_file(
                    'VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n'
                    '122 mikrotik-chr running 512 8.00 2324', 'qm list'))
            app_module.save_backup_history([{
                'id': 'migration-new', 'filename': new_archive.name, 'local_path': str(new_archive),
                'timestamp': (datetime.now() + timedelta(seconds=1)).isoformat(),
                'ftp_status': 'success', 'files': sorted(REQUIRED_PATHS), 'skipped': [],
            }])
            payload = client.get(base).get_json()
            guests = {int(guest['vmid']): guest for guest in payload['guests']}
            assert payload['inventory']['available'] is True and set(guests) == {100, 113, 122, 124}
            assert guests[100]['status'] == 'verified' and guests[100]['note'] == note
            assert guests[100]['name'] == 'home-assistant' and payload['cutover_ready'] is True

            # Sekcia sa exportuje ako snapshot uloženého postupu a escapuje poznámky.
            html = client.get('/api/recovery/handbook').get_data(as_text=True)
            assert '<section id="migration">' in html
            assert 'old-pve.example' in html and 'new-pve.example' in html
            assert 'kontrola po presune' in html and '&lt;script&gt;' in html
            assert '<script>alert("migration")</script>' not in html and SECRET_MARKER not in html
            assert 'home-assistant' in html and 'ponechaný' in html
            side_steps = client.get(base).get_json()['steps']
            for step in side_steps:
                assert client.post(base + '/steps/' + step['id'], json={'completed': True}).status_code == 200
            assert client.get(base).get_json()['state']['finished_at']
            assert client.post(base + '/steps/prepare', json={'completed': False}).status_code == 200
            assert not client.get(base).get_json()['state']['finished_at']

            # Nedostupný archív nesmie vymazať stav a nesmie vyhlásiť cutover za bezpečný.
            app_module.save_backup_history([])
            payload = client.get(base).get_json()
            assert payload['inventory']['available'] is False and payload['cutover_ready'] is False
            assert payload['state']['guests']['100']['status'] == 'verified'

            assert client.post(base + '/reset', json={'password': SECRET_MARKER}).status_code == 400
            response = client.post(base + '/reset', json={})
            assert response.status_code == 200, response.get_data(as_text=True)
            state = client.get(base).get_json()['state']
            assert not state['method'] and not state['started_at'] and not state['finished_at']
            assert not state['steps'] and not state['guests']
            html = client.get('/api/recovery/handbook').get_data(as_text=True)
            assert note not in html and '<section id="migration">' not in html
            # Po resete sa spôsob dá zmeniť, disk_move neponúka presun jednotlivých hostí.
            assert client.post(base, json={**initial, 'method': 'disk_move'}).status_code == 200
            payload = client.get(base).get_json()
            assert not payload['guests']
            assert 'disk-move' in {step['id'] for step in payload['steps']}
            assert 'new-host' not in {step['id'] for step in payload['steps']}
            assert client.post(base + '/guests/100', json={'status': 'stopped_on_old'}).status_code == 400
            assert client.post(base + '/steps/new-host', json={'completed': True}).status_code == 400
            assert state_file.stat().st_mode & 0o777 == 0o600
            assert Path(str(state_file) + '.lock').stat().st_mode & 0o777 == 0o600

            # Zlyhanie atomického replace zachová starý platný stav a odstráni tmp súbor.
            before_failed_write = state_file.read_bytes()
            with patch.object(app_module.os, 'replace', side_effect=OSError('simulovaný výpadok disku')):
                response = client.post(base + '/steps/prepare', json={'completed': True})
            assert response.status_code == 503 and response.is_json
            assert state_file.read_bytes() == before_failed_write
            assert not list(workdir.glob('.migration-state-*.tmp'))
            assert 'prepare' not in client.get(base).get_json()['state']['steps']

            # Poškodený/nepodporovaný stav sa nesmie ticho zahodiť ani vypísať do odpovede.
            for corrupt in ('{"password":"' + SECRET_MARKER + '"', '{"version":999}', '[]'):
                state_file.write_text(corrupt, encoding='utf-8')
                response = client.get(base)
                assert response.status_code == 503 and response.is_json
                assert SECRET_MARKER not in response.get_data(as_text=True)
                assert state_file.read_text(encoding='utf-8') == corrupt
                assert client.post(base, json=initial).status_code == 503
                handbook = client.get('/api/recovery/handbook')
                assert handbook.status_code == 200, 'poškodená migrácia nesmie rozbiť existujúcu DR príručku'
                html = handbook.get_data(as_text=True)
                assert SECRET_MARKER not in html
                assert 'Stav migrácie nie je dostupný' in html and 'poškodený stav' in html
                assert '<section id="migration">' not in html
                assert client.post(base + '/reset', json={}).status_code == 200
                assert not client.get(base).get_json()['state']['method']
                assert state_file.stat().st_mode & 0o777 == 0o600
        finally:
            for name, value in originals.items():
                setattr(app_module, name, value)
            app_module.AUTH_CONFIG_FILE = original_auth
            app_module.sync_flask_secret()


class MigrationCompareChannel:
    """Paramiko channel double: stdout aj stderr sa musia čítať v obmedzených blokoch."""
    def __init__(self, stdout='', stderr='', exit_code=0):
        self.stdout = stdout.encode('utf-8') if isinstance(stdout, str) else stdout
        self.stderr = stderr.encode('utf-8') if isinstance(stderr, str) else stderr
        self.exit_code = exit_code
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout)

    def recv(self, size):
        assert 0 < size <= 65536
        block, self.stdout = self.stdout[:size], self.stdout[size:]
        return block

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv_stderr(self, size):
        assert 0 < size <= 65536
        block, self.stderr = self.stderr[:size], self.stderr[size:]
        return block

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.exit_code

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class MigrationCompareStream:
    def __init__(self, channel):
        self.channel = channel

    def read(self, *_args):
        raise AssertionError('compare nesmie použiť neobmedzené stream.read()')


class MigrationCompareSshClient:
    def __init__(self, outputs, connect_error=None, command_errors=None):
        self.outputs = outputs
        self.connect_error = connect_error
        self.command_errors = command_errors or {}
        self.commands = []
        self.channels = []
        self.closed = False

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error:
            raise self.connect_error

    def exec_command(self, command, timeout=None):
        by_command = {item['command']: item['id'] for item in recovery_data.MIGRATION_COMPARE_COMMANDS}
        assert command in by_command, 'compare smie vykonať iba pevný read-only allowlist'
        self.commands.append(command)
        command_id = by_command[command]
        if command_id in self.command_errors:
            raise self.command_errors[command_id]
        output = self.outputs[command_id]
        stdout, stderr, exit_code = output if isinstance(output, tuple) else (output, '', 0)
        channel = MigrationCompareChannel(stdout, stderr, exit_code)
        self.channels.append(channel)
        return io.BytesIO(), MigrationCompareStream(channel), MigrationCompareStream(channel)

    def open_sftp(self):
        raise AssertionError('compare nesmie zapisovať cez SFTP')

    def close(self):
        self.closed = True


def migration_compare_outputs(new_host=False):
    """Výstupy read-only príkazov s reálnym tvarom PVE/Linux tabuliek."""
    if new_host:
        return {
            'hostname': 'new-pve\n',
            'version': 'proxmox-ve: 8.4.0\npve-manager: 8.4.1\n',
            'storage': 'Name Type Status Total Used Available %\nlocal-lvm lvmthin inactive 0 0 0 0%\n',
            'links': 'eno2 UP 02:00:00:00:00:02 <BROADCAST,UP>\nvmbr1 UP 02:00:00:00:00:02 <BROADCAST,UP>\n',
            'addresses': 'lo UNKNOWN 127.0.0.1/8 ::1/128\nvmbr1 UP 192.0.2.3/24\n',
            'network': 'auto vmbr1\niface vmbr1 inet manual\n bridge-ports eno2\n bridge-vlan-aware no\n'
                       'auto vmbr1.300\niface vmbr1.300 inet static\n vlan-raw-device vmbr1\n vlan-id 300\n',
            'cpu': 'Architecture: x86_64\nVendor ID: AuthenticAMD\nFlags: fpu sse\n',
            'qm': 'VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n',
            'pct': 'VMID Status Lock Name\n100 running moved-copy\n113 stopped proxmox-backup\n',
            'timers': 'Sun 2026-10-04 04:00:00 CEST 1h Sat 2026-10-03 04:00:00 CEST 23h pve-backup.timer pve-backup.service\n',
        }
    return {
        'hostname': 'old-pve\n',
        'version': 'proxmox-ve: 9.2.0\npve-manager: 9.2.3\n',
        'storage': 'Name Type Status Total Used Available %\nlocal-lvm lvmthin active 100 30 70 30%\n'
                   'nas dir active 500 100 400 20%\n',
        'links': 'eno1 UP 02:00:00:00:00:01 <BROADCAST,UP>\nvmbr0 UP 02:00:00:00:00:01 <BROADCAST,UP>\n',
        'addresses': 'lo UNKNOWN 127.0.0.1/8 ::1/128\nvmbr0 UP 192.0.2.2/24\n',
        'network': 'auto vmbr0\niface vmbr0 inet manual\n bridge-ports eno1\n bridge-vlan-aware yes\n'
                   ' bridge-vids 100 200\nauto vmbr0.200\niface vmbr0.200 inet static\n'
                   ' vlan-raw-device vmbr0\n vlan-id 200\n',
        'cpu': 'Architecture: x86_64\nVendor ID: GenuineIntel\nFlags: fpu sse avx avx2\n',
        'qm': 'VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n100 shared-vm running 2048 32.00 123\n'
              '101 offline-vm stopped 1024 8.00 0\n',
        'pct': 'VMID Status Lock Name\n113 running proxmox-backup\n',
        'timers': 'Sun 2026-10-04 04:00:00 CEST 1h Sat 2026-10-03 04:00:00 CEST 23h pve-backup.timer pve-backup.service\n',
    }


def test_migration_compare():
    commands = recovery_data.MIGRATION_COMPARE_COMMANDS
    expected_commands = {
        'hostname': 'LC_ALL=C hostname', 'version': 'LC_ALL=C pveversion -v',
        'storage': 'LC_ALL=C pvesm status', 'links': 'LC_ALL=C ip -br link',
        'addresses': 'LC_ALL=C ip -br addr', 'network': 'LC_ALL=C cat /etc/network/interfaces',
        'cpu': 'LC_ALL=C lscpu', 'qm': 'LC_ALL=C qm list', 'pct': 'LC_ALL=C pct list',
        'timers': 'LC_ALL=C systemctl list-timers --all --no-pager --no-legend',
    }
    assert {item['id']: item['command'] for item in commands} == expected_commands
    assert len(commands) == len(expected_commands), 'duplicitný compare príkaz'
    with tempfile.TemporaryDirectory(prefix='pve-migration-compare-', dir=str(ROOT)) as workdir:
        workdir = Path(workdir)
        names = ('CONFIG_FILE', 'BACKUP_HISTORY_FILE', 'BACKUP_STORAGE_DIR', 'MIGRATION_STATE_FILE', 'SSH_CLIENT_FACTORY', 'list_ftp_backups')
        originals = {name: getattr(app_module, name) for name in names}
        app_module.CONFIG_FILE = str(workdir / 'backup_config.json')
        app_module.BACKUP_HISTORY_FILE = str(workdir / 'backup_history.json')
        app_module.BACKUP_STORAGE_DIR = str(workdir / 'backups')
        app_module.MIGRATION_STATE_FILE = str(workdir / 'migration_state.json')
        app_module.list_ftp_backups = lambda cfg: {'available': False, 'warning': 'test', 'archives': []}
        (workdir / 'backups').mkdir()
        original_auth, secret, _password = create_test_auth_config(workdir / 'auth_config.json')
        endpoint = '/api/recovery/migration/compare'
        request_body = {'new_host': {'host': '192.0.2.3', 'password': 'NEW-' + SECRET_MARKER}}
        created = []
        pending_clients = []

        def factory():
            assert pending_clients, 'neočakávané SSH pripojenie'
            ssh = pending_clients.pop(0)
            created.append(ssh)
            return ssh

        def compare(old=None, new=None):
            created.clear()
            pending_clients[:] = [
                old or MigrationCompareSshClient(migration_compare_outputs()),
                new or MigrationCompareSshClient(migration_compare_outputs(True)),
            ]
            response = client.post(endpoint, json=request_body)
            assert response.status_code == 200, response.get_data(as_text=True)
            payload = response.get_json()
            assert payload['success'] and payload['read_only'] is True and payload['compared_at']
            assert len(created) == 2 and not pending_clients
            assert all(ssh.closed for ssh in created), 'každý klient sa musí zavrieť aj pri chybe'
            assert all(channel.closed for ssh in created for channel in ssh.channels)
            assert SECRET_MARKER not in response.get_data(as_text=True), 'compare nesmie echo password/stderr/exception'
            return payload

        app_module.SSH_CLIENT_FACTORY = factory
        try:
            config = app_module.default_config()
            config['source_config']['mode'] = 'remote_ssh'
            config['source_config']['ssh'].update({
                'host': '192.0.2.2', 'port': 22, 'username': 'root', 'password': 'OLD-' + SECRET_MARKER,
            })
            app_module.save_config(config)
            app_module.save_backup_history([])
            app_module.save_migration_state(app_module.default_migration_state())
            config_before = Path(app_module.CONFIG_FILE).read_bytes()
            state_before = Path(app_module.MIGRATION_STATE_FILE).read_bytes()
            files_before = {path.name for path in workdir.iterdir()}
            assert app_module.app.test_client().post(endpoint, json=request_body).status_code == 401
            client = make_authed_client(secret)
            assert client.post(endpoint, json=request_body, headers={'X-CSRF-Token': ''}).status_code == 403
            invalid = [
                {}, {'new_host': []}, {'new_host': {'host': '192.0.2.3', 'password': ''}},
                {'new_host': {'host': '192.0.2.3; reboot', 'password': 'x'}},
                {'new_host': {'host': 'bad host', 'password': 'x'}},
                {'new_host': {'host': '999.1.1.1', 'password': 'x'}},
                {'new_host': {'host': '192.0.2.3', 'password': None}},
                {'new_host': {'host': '192.0.2.3', 'password': ['x']}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x' * 4097}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x', 'port': 0}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x', 'port': 65536}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x', 'port': True}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x', 'port': '22'}},
                {'new_host': {'host': '192.0.2.3', 'password': 'x', 'username': 'other'}},
                {'new_host': {'host': '192.0.2.2', 'password': 'x'}},
                {**request_body, 'old_host': {'password': 'x'}},
            ]
            for body in invalid:
                response = client.post(endpoint, json=body)
                assert response.status_code == 400 and response.is_json, body
                assert SECRET_MARKER not in response.get_data(as_text=True)
            for malformed in ('{', 'null', '[]'):
                response = client.post(endpoint, data=malformed, content_type='application/json')
                assert response.status_code == 400 and response.is_json
            assert not created, 'neplatný vstup ani auth nesmie spustiť SSH'

            payload = compare()
            old_facts, new_facts = payload['old_host']['facts'], payload['new_host']['facts']
            assert payload['old_host']['connected'] and payload['new_host']['connected']
            assert payload['old_host']['complete'] and payload['new_host']['complete']
            assert not payload['old_host']['errors'] and not payload['new_host']['errors']
            assert old_facts['hostname'] == 'old-pve' and new_facts['hostname'] == 'new-pve'
            assert '9.2' in old_facts['pve_version'] and '8.4' in new_facts['pve_version']
            assert {storage['id'] for storage in old_facts['storage']} == {'local-lvm', 'nas'}
            assert old_facts['cpu']['vendor'] == 'GenuineIntel' and new_facts['cpu']['vendor'] == 'AuthenticAMD'
            assert set(old_facts['cpu']['flags']) == {'fpu', 'sse', 'avx', 'avx2'}
            assert old_facts['network']['bridges'][0]['name'] == 'vmbr0'
            assert old_facts['network']['bridges'][0]['vlan_aware'] in (True, 'yes')
            assert old_facts['network']['vlans'][0]['name'] == 'vmbr0.200'
            assert {guest['vmid'] for guest in old_facts['guests']} == {100, 101, 113}
            assert old_facts['timers'][0]['unit'] == 'pve-backup.timer'
            rows = {row['id']: row for row in payload['rows']}
            assert len(rows) == len(payload['rows'])
            assert rows['guests-running']['level'] == 'error', 'duplicitné running VMID platí aj pri rozdielnom VM/LXC type'
            for row_id in ('pve-version', 'storage-ids', 'storage-status', 'bridges', 'vlans', 'cpu-vendor', 'cpu-flags', 'backup-timers'):
                assert rows[row_id]['level'] in ('warning', 'error'), (row_id, rows[row_id])
            assert rows['identity']['level'] == 'ok'
            for level, count in payload['summary'].items():
                assert count == sum(row['level'] == level for row in payload['rows'])
            for ssh in created:
                assert ssh.commands == [item['command'] for item in commands]
            assert created[0].connect_kwargs['hostname'] == '192.0.2.2'
            assert created[0].connect_kwargs['password'] == 'OLD-' + SECRET_MARKER
            assert created[1].connect_kwargs['hostname'] == '192.0.2.3'
            assert created[1].connect_kwargs['username'] == 'root' and created[1].connect_kwargs['port'] == 22

            same_hostname = migration_compare_outputs(True)
            same_hostname['hostname'] = 'old-pve\n'
            payload = compare(new=MigrationCompareSshClient(same_hostname))
            assert next(row for row in payload['rows'] if row['id'] == 'identity')['level'] == 'error'
            old_fqdn, new_fqdn = migration_compare_outputs(), migration_compare_outputs(True)
            old_fqdn['hostname'], new_fqdn['hostname'] = 'same-pve.old.example\n', 'same-pve.new.example\n'
            payload = compare(old=MigrationCompareSshClient(old_fqdn), new=MigrationCompareSshClient(new_fqdn))
            assert next(row for row in payload['rows'] if row['id'] == 'identity')['level'] == 'error'
            # Slabé heslo zhodné s enumom nesmie zmeniť závažnosť bezpečnostného nálezu.
            assert app_module.redact_migration_comparison(
                {'level': 'error', 'detail': 'credential: error'}, ['error']) == {
                    'level': 'error', 'detail': 'credential: [skryté]'}
            secret_old_outputs, secret_new_outputs = migration_compare_outputs(), migration_compare_outputs(True)
            secret_old_outputs['storage'] = secret_old_outputs['storage'].replace('nas dir', 'OLD-' + SECRET_MARKER + ' dir')
            secret_new_outputs['cpu'] = 'Vendor ID: NEW-' + SECRET_MARKER + '\nFlags: fpu sse\n'
            payload = compare(old=MigrationCompareSshClient(secret_old_outputs), new=MigrationCompareSshClient(secret_new_outputs))
            assert SECRET_MARKER not in payload['new_host']['facts']['cpu']['vendor']
            for host in ('NEW-PVE.EXAMPLE.', '2001:db8::3'):
                request_body['new_host'].update(host=host, port=2222)
                payload = compare()
                assert created[1].connect_kwargs['hostname'] == host.lower().rstrip('.')
                assert created[1].connect_kwargs['port'] == 2222
            request_body['new_host'] = {'host': '192.0.2.3', 'password': 'NEW-' + SECRET_MARKER}

            # SSH spojenie padne, ale druhý host sa porovná a oba klienty sa zavrú.
            payload = compare(old=MigrationCompareSshClient({}, connect_error=RuntimeError(SECRET_MARKER)))
            assert payload['old_host']['connected'] is False and payload['old_host']['complete'] is False
            assert payload['old_host']['errors'] and payload['new_host']['connected']
            assert all(row['level'] == 'unknown' for row in payload['rows'])
            assert not created[0].commands
            payload = compare(new=MigrationCompareSshClient({}, connect_error=RuntimeError(SECRET_MARKER)))
            assert payload['new_host']['connected'] is False and payload['old_host']['connected'] is True

            # Zlyhaný príkaz vracia unknown, nie zavádzajúce úspešné porovnanie.
            payload = compare(new=MigrationCompareSshClient(
                migration_compare_outputs(True), command_errors={'storage': RuntimeError(SECRET_MARKER)}))
            rows = {row['id']: row for row in payload['rows']}
            assert payload['new_host']['connected'] is True and payload['new_host']['complete'] is False
            assert rows['storage-ids']['level'] == 'unknown' and rows['storage-status']['level'] == 'unknown'
            assert any(error['command_id'] == 'storage' for error in payload['new_host']['errors'])
            assert rows['guests-running']['level'] == 'error', 'chyba jedného príkazu nesmie skryť zistený konflikt hostí'
            payload = compare(new=MigrationCompareSshClient(
                migration_compare_outputs(True), command_errors={'storage': ValueError(SECRET_MARKER)}))
            assert any(error['command_id'] == 'storage' for error in payload['new_host']['errors'])

            invalid_version = migration_compare_outputs(True)
            invalid_version['version'] = 'pve-manager: 9BROKEN\n'
            payload = compare(new=MigrationCompareSshClient(invalid_version))
            assert any(error['command_id'] == 'version' for error in payload['new_host']['errors'])
            assert next(row for row in payload['rows'] if row['id'] == 'pve-version')['level'] == 'unknown'

            failed_outputs = migration_compare_outputs(True)
            failed_outputs['cpu'] = ('Vendor ID: ' + SECRET_MARKER, SECRET_MARKER, 1)
            payload = compare(new=MigrationCompareSshClient(failed_outputs))
            rows = {row['id']: row for row in payload['rows']}
            assert rows['cpu-vendor']['level'] == 'unknown' and rows['cpu-flags']['level'] == 'unknown'
            assert any(error['command_id'] == 'cpu' for error in payload['new_host']['errors'])

            # Limit výstupu zahodí celé fakty z príkazu; parsovaný fragment nie je spoľahlivý.
            huge_outputs = migration_compare_outputs(True)
            huge_outputs['qm'] = 'VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n' + '120 synthetic running 1024 8.00 123\n' * 10000
            huge_outputs['pct'] = 'VMID Status Lock Name\n113 stopped proxmox-backup\n'
            payload = compare(new=MigrationCompareSshClient(huge_outputs))
            rows = {row['id']: row for row in payload['rows']}
            assert payload['new_host']['complete'] is False
            assert any(error['command_id'] == 'qm' for error in payload['new_host']['errors'])
            assert rows['guests-running']['level'] == 'unknown' and rows['guests-inventory']['level'] == 'unknown'

            included_network = migration_compare_outputs(True)
            included_network['network'] += '\nsource /etc/network/interfaces.d/*\n'
            payload = compare(new=MigrationCompareSshClient(included_network))
            assert payload['new_host']['facts']['network']['includes'] is True
            rows = {row['id']: row for row in payload['rows']}
            assert rows['bridges']['level'] == 'unknown' and rows['vlans']['level'] == 'unknown'

            # Porovnanie neukladá zadané heslo ani nemení konfiguráciu či priebeh migrácie.
            assert Path(app_module.CONFIG_FILE).read_bytes() == config_before
            assert Path(app_module.MIGRATION_STATE_FILE).read_bytes() == state_before
            assert {path.name for path in workdir.iterdir()} == files_before

            for source in ({'mode': 'local'}, {'mode': 'remote_ssh', 'ssh': {'host': '', 'password': 'x'}}):
                config['source_config'] = source
                app_module.save_config(config)
                created.clear()
                pending_clients.clear()
                response = client.post(endpoint, json=request_body)
                assert response.status_code == 400 and response.is_json
                assert not created, 'neplatný SSH zdroj nesmie začať porovnanie'
        finally:
            for name, value in originals.items():
                setattr(app_module, name, value)
            app_module.AUTH_CONFIG_FILE = original_auth
            app_module.sync_flask_secret()


def main():
    test_data_model()
    test_readiness()
    test_restore_guards()
    test_info_commands_resilience()
    test_api_and_snapshot()
    test_wiki_additions()
    test_risk_analysis()
    test_downloads_and_handbook()
    test_migration_data_model()
    test_migration_api()
    test_migration_compare()
    print('test_recovery: OK')


if __name__ == '__main__':
    main()
