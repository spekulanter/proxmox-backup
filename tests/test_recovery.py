#!/usr/bin/env python3
"""Testy disaster recovery funkcionality (klasifikácia, readiness, wiki, snapshot, restore ochrany)."""

import io
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

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


def main():
    test_data_model()
    test_readiness()
    test_restore_guards()
    test_info_commands_resilience()
    test_api_and_snapshot()
    print('test_recovery: OK')


if __name__ == '__main__':
    main()
