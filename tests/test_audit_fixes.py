#!/usr/bin/env python3
"""Regresné testy opráv auditu z 2026-10-03 (A01–A11). Testujú správne správanie, nie pôvodné chyby."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402


class Log:
    def __init__(self):
        self.lines = []

    def log(self, message):
        self.lines.append(str(message))

    progress = log


class ScriptedHost:
    """Hosť s odpoveďami na presné príkazy; zaznamenáva všetky príkazy. Neznáme príkazy uspejú s prázdnym výstupom."""
    commands = []
    replies = {}
    defaults = {
        'cat /etc/machine-id': {'old': 'aaa\n', 'new': 'bbb\n'},
        'LC_ALL=C pveversion': 'pve-manager/9.2.3/abc (running kernel: 7.0.6-2-pve)\n',
        'LC_ALL=C hostname': 'nuc\n',
        'if test -e /etc/pve/corosync.conf; then echo cluster; else echo standalone; fi': 'standalone\n',
    }

    def __init__(self, config):
        self.name = config['host']
        self.role = 'new' if self.name == 'new' else 'old'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def try_run(self, command, timeout=None):
        ScriptedHost.commands.append((self.name, command))
        key = (self.name, command)
        if key in ScriptedHost.replies:
            reply = ScriptedHost.replies[key]
            return reply if isinstance(reply, tuple) else (reply, '')
        if command in ScriptedHost.defaults:
            value = ScriptedHost.defaults[command]
            return (value[self.role] if isinstance(value, dict) else value), ''
        if command.startswith('if test -f ') and '.pbm-share-check-' in command:
            return 'visible\n', ''
        if ' status ' in f' {command} ':
            return 'status: stopped\n', ''
        return '', ''

    run = try_run

    @classmethod
    def reset(cls):
        cls.commands = []
        cls.replies = {}


def make_mctx(**overrides):
    transfer = a.default_migration_transfer()
    values = dict(old_ssh={'host': 'old', 'port': 22}, target={'host': 'new', 'port': 22}, transfer=transfer, app_guest=None,
                  facts={'files': {'etc/pve/storage.cfg': 'lvmthin: local-lvm\n\tthinpool data\n\tvgname pve\n\tcontent rootdir,images\n'},
                         'members': set(), 'info': {}},
                  guest_info=lambda vmid: {'vmid': vmid, 'type': 'VM', 'name': 'test'},
                  archive_path='archive.tar.gz', entry={'filename': 'archive.tar.gz'}, node='nuc',
                  state={'guests': {}})
    values.update(overrides)
    return SimpleNamespace(**values)


def in_temp_cwd(function):
    """Aplikačné funkcie sa importujú už vyššie; stav migrácie sa smeruje do dočasného adresára."""
    def wrapper():
        with tempfile.TemporaryDirectory(prefix='pbm-audit-fix-', dir=str(ROOT)) as directory:
            names = ('MIGRATION_STATE_FILE', 'MIGRATION_TRANSFER_FILE', 'MIGRATION_TARGET_FILE', 'MIGRATION_JOBS_FILE',
                     'BACKUP_HISTORY_FILE', 'BACKUP_STORAGE_DIR')
            originals = {name: getattr(a, name) for name in names}
            for name in names:
                setattr(a, name, os.path.join(directory, name.lower()))
            os.makedirs(a.BACKUP_STORAGE_DIR)
            ScriptedHost.reset()
            try:
                function(Path(directory))
            finally:
                for name, value in originals.items():
                    setattr(a, name, value)
    wrapper.__name__ = function.__name__
    return wrapper


# --- A05: skutočný GNU tar -----------------------------------------------------------------

@in_temp_cwd
def test_a05_directory_and_children_extract_with_real_tar(directory):
    source = directory / 'src'
    (source / 'etc' / 'network').mkdir(parents=True)
    (source / 'etc' / 'network' / 'interfaces').write_text('auto lo\n')
    (source / 'etc' / 'network' / 'we[ir]d').write_text('glob-like name\n')
    archive = directory / 'a.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source / 'etc', arcname='etc')
    members = [member.name for member in a.restore_archive_members(str(archive), ['/etc/network'])]
    assert 'etc/network' in members and 'etc/network/interfaces' in members
    members_file = directory / 'members.txt'
    members_file.write_text(a.build_tar_members_list(members + members[:1]))  # duplicita sa odstráni
    staging = directory / 'staging'
    staging.mkdir()
    result = subprocess.run(a.build_tar_extract_command(str(archive), str(staging), str(members_file)),
                            shell=True, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (staging / 'etc/network/interfaces').read_text() == 'auto lo\n'
    assert (staging / 'etc/network/we[ir]d').exists()


# --- A03: konzistentný snapshot živej WAL databázy -----------------------------------------

@in_temp_cwd
def test_a03_backup_contains_committed_wal_rows(directory):
    live_path = directory / 'config.db'
    live = sqlite3.connect(str(live_path))
    live.execute('PRAGMA journal_mode=WAL')
    live.execute('PRAGMA wal_autocheckpoint=0')
    live.execute('CREATE TABLE config(value TEXT)')
    live.commit()
    live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    live.execute("INSERT INTO config VALUES ('LATEST_COMMITTED_CONFIG')")
    live.commit()
    try:
        raw_copy = directory / 'raw.db'
        shutil.copyfile(live_path, raw_copy)
        with sqlite3.connect(str(raw_copy)) as raw:
            assert raw.execute('SELECT * FROM config').fetchall() == [], 'predpoklad: surová kópia WAL zmeny nemá'

        archive = directory / 'backup.tar.gz'
        with patch.object(a, 'PVE_CONFIG_DB_PATH', str(live_path)), patch.object(a, 'generate_backup_info', lambda *args: []):
            report = a.create_backup_archive([{'path': str(live_path)}], str(archive), include_info=False)
        assert report['included'] and report['included'][0].get('snapshot') == 'sqlite', report
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            assert len(names) == 1 and names[0].endswith('config.db')
            snapshot = directory / 'extracted.db'
            snapshot.write_bytes(tar.extractfile(names[0]).read())
        with sqlite3.connect(str(snapshot)) as copied:
            assert copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert copied.execute('SELECT * FROM config').fetchall() == [('LATEST_COMMITTED_CONFIG',)]
    finally:
        live.close()


def test_a03_remote_tar_command_uses_snapshot_directory():
    source = a.RemoteSshBackupSource({'mode': 'remote_ssh', 'ssh': {'host': 'h', 'username': 'root', 'password': 'x'}})
    command = source.build_tar_command(['etc/hosts'], '/tmp/pve-host-backup-info.X', ['var/lib/pve-cluster/config.db'])
    assert '/tmp/pve-host-backup-info.X/db-snapshot var/lib/pve-cluster/config.db' in command
    assert command.index('etc/hosts') < command.index('db-snapshot') < command.rindex('backup-info')


@in_temp_cwd
def test_a03_archive_config_db_validation(directory):
    def db(names):
        path = directory / 'x.db'
        if path.exists():
            path.unlink()
        connection = sqlite3.connect(str(path))
        connection.execute('CREATE TABLE tree (inode INTEGER PRIMARY KEY, parent INT, version INT, writer INT, mtime INT, type INT, name TEXT, data BLOB)')
        for index, name in enumerate(names, start=1):
            connection.execute('INSERT INTO tree VALUES (?, 1, 1, 1, 0, 8, ?, NULL)', (index, name))
        connection.commit()
        connection.close()
        return path.read_bytes()

    facts = {'members': {'etc/pve/nodes/nuc/qemu-server/100.conf', 'etc/pve/nodes/nuc/lxc/113.conf'}}
    a.validate_archive_config_db(db(['100.conf', '113.conf']), facts, 'nuc')
    for bad, text in ((db(['100.conf']), 'zastaraná'), (b'not sqlite at all' * 100, 'nie je použiteľná')):
        try:
            a.validate_archive_config_db(bad, facts, 'nuc')
        except ValueError as exc:
            assert text in str(exc), exc
        else:
            raise AssertionError('neplatná databáza musí byť odmietnutá')
    try:
        a.validate_archive_config_db(db(['100.conf', '113.conf']), {'members': facts['members'] | {'etc/pve/corosync.conf'}}, 'nuc')
    except ValueError as exc:
        assert 'clustri' in str(exc)
    else:
        raise AssertionError('klastrový zdroj musí byť odmietnutý')


# --- A04: prázdna záloha ---------------------------------------------------------------------

@in_temp_cwd
def test_a04_empty_backup_is_not_success_and_skips_retention(directory):
    class EmptySource:
        def create_archive(self, selected, filename):
            Path(filename).write_bytes(b'only backup-info')
            return {'included': [], 'skipped': [{'path': '/etc/pve', 'reason': 'missing'}], 'generated_info': ['x']}

    ftp = {'host': 'ftp', 'username': 'u', 'password': 'p'}
    with patch.object(a, 'build_backup_source', lambda config: EmptySource()), \
            patch.object(a, 'upload_to_ftp', side_effect=AssertionError('prázdna záloha sa nesmie nahrať')), \
            patch.object(a, 'enforce_backup_retention', side_effect=AssertionError('retencia sa nesmie spustiť')):
        try:
            a.run_backup_job(['/etc/pve'], ftp, a.DEFAULT_SOURCE_CONFIG, [{'path': '/etc/pve', 'selected': True}], config=a.default_config())
        except ValueError as exc:
            assert 'neobsahuje žiadnu z vybraných ciest' in str(exc) and '/etc/pve (missing)' in str(exc)
        else:
            raise AssertionError('prázdna záloha musí zlyhať')
    assert os.listdir(a.BACKUP_STORAGE_DIR) == [], 'lokálny prázdny archív sa odstráni'
    assert a.load_backup_history() == []


# --- A02, A10, A16: rollback a stav hosťa ----------------------------------------------------

def test_a02_rollback_blocks_when_new_state_unknown():
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('new', 'qm status 100')] = (None, 'unavailable')
        ScriptedHost.replies[('new', 'if test -e /etc/pve/qemu-server/100.conf; then echo present; else echo absent; fi')] = 'present\n'
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_guest_rollback(Log(), make_mctx(), 100)
            except RuntimeError as exc:
                assert 'nie je potvrdený ako vypnutý' in str(exc)
            else:
                raise AssertionError('rollback pri neznámom stave novej kópie nesmie pokračovať')
        assert not any(host == 'old' and ' start ' in f' {command} ' for host, command in ScriptedHost.commands)


def test_a02_rollback_proceeds_when_definition_absent_and_clears_moved():
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('new', 'qm status 100')] = (None, 'does not exist')
        ScriptedHost.replies[('new', 'if test -e /etc/pve/qemu-server/100.conf; then echo present; else echo absent; fi')] = 'absent\n'
        ScriptedHost.replies[('old', 'qm status 100')] = ('status: stopped\n', '')
        a.update_migration_transfer(lambda t: t['guests'].update({'100': {'moved_at': 'x', 'original_onboot': True}}))
        with patch.object(a, 'MigrationHost', ScriptedHost), patch.object(a, 'set_migration_guest_status') as status:
            a.run_migration_guest_rollback(Log(), make_mctx(), 100)
        assert ('old', 'qm set 100 --onboot 1') in ScriptedHost.commands and ('old', 'qm start 100') in ScriptedHost.commands
        assert status.call_args.args[:2] == (100, 'rolled_back')
        guest = a.load_migration_transfer()['guests']['100']
        assert 'moved_at' not in guest and guest['original_onboot'] is True and guest['rolled_back_at']


def test_a02_rollback_requires_confirmed_shutdown_of_new_copy():
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('new', 'qm status 100')] = ('status: running\n', '')  # po shutdown stále beží
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_guest_rollback(Log(), make_mctx(), 100)
            except RuntimeError:
                pass
            else:
                raise AssertionError('bežiaca nová kópia blokuje štart starej')
        assert ('old', 'qm start 100') not in ScriptedHost.commands


def test_a16_original_onboot_is_captured_once():
    with in_temp_cwd_ctx():
        configs = iter(['onboot: 1\nname: x\n', 'onboot: 0\nname: x\n'])
        ScriptedHost.defaults = dict(ScriptedHost.defaults)
        restores = iter([(1, ['boom']), (0, ['ok'])])

        def stream(host, command, ctx, timeout=None, label=''):
            if command.startswith('vzdump'):
                return 0, ["INFO: creating vzdump archive '/d/vzdump-qemu-100-x.vma.zst'"]
            return next(restores)

        with patch.object(a, 'MigrationHost', ScriptedHost), patch.object(a, 'migration_stream', stream), \
                patch.object(a, 'set_migration_guest_status'):
            for attempt in range(2):
                ScriptedHost.replies[('old', 'qm config 100')] = (next(configs), '')
                ScriptedHost.replies[('old', 'qm status 100')] = ('status: stopped\n', '')
                try:
                    result = a.run_migration_guest_move(Log(), make_mctx(), 100, '/d', 'local-lvm')
                except RuntimeError:
                    assert attempt == 0
                    assert a.load_migration_transfer()['guests']['100']['original_onboot'] is True
        assert result['original_onboot'] is True
        assert a.load_migration_transfer()['guests']['100']['original_onboot'] is True


def test_a16_move_fails_when_old_config_unreadable():
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('old', 'qm config 100')] = (None, 'ssh error')
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_guest_move(Log(), make_mctx(), 100, '/d', 'local-lvm')
            except RuntimeError as exc:
                assert 'nepodarilo načítať' in str(exc)
            else:
                raise AssertionError('bez čitateľnej konfigurácie sa autostart neodhaduje')
        assert not any('--onboot 0' in command for _host, command in ScriptedHost.commands)


# --- A01: zdieľané storage -------------------------------------------------------------------

def test_a01_shared_or_unclear_storage_is_rejected():
    storage_cfg = (
        'lvmthin: local-lvm\n\tthinpool data\n\tvgname pve\n\tcontent rootdir,images\n\n'
        'nfs: nas\n\tserver 192.0.2.9\n\texport /vm\n\tcontent images\n\n'
        'dir: bind\n\tpath /mnt/pve/nasdir\n\tcontent images\n\n'
        'dir: fast\n\tpath /var/lib/vz\n\tcontent images\n\n'
        'lvm: san\n\tvgname san\n\tshared 1\n\tcontent images\n\n'
        'rbd: ceph\n\tpool vm\n\tcontent images\n')
    facts = {'files': {'etc/pve/storage.cfg': storage_cfg}}
    ScriptedHost.reset()
    ScriptedHost.replies[('old', 'findmnt -n -o FSTYPE -T /mnt/pve/nasdir')] = 'nfs4\n'
    ScriptedHost.replies[('old', 'findmnt -n -o FSTYPE -T /var/lib/vz')] = 'ext4\n'
    host = ScriptedHost({'host': 'old'})
    a.require_guest_disks_local(host, 'scsi0: local-lvm:vm-100-disk-0,size=32G\nide2: nas:iso/x.iso,media=cdrom\n'
                                      'mp0: /srv/data,mp=/data\nscsi1: fast:100/vm-100-disk-1.qcow2\nnet0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0\n'
                                      '\n[snap]\nscsi5: nas:vm-100-disk-9\n', facts, 'VM 100')
    for config, expected in (('scsi0: nas:vm-100-disk-0', 'typ „nfs“'), ('rootfs: ceph:vm-101-disk-0', 'typ „rbd“'),
                             ('scsi0: san:vm-100-disk-0', 'zdieľaný'), ('scsi0: bind:100/vm-100-disk-0.qcow2', 'nfs4'),
                             ('scsi0: nowhere:vm-100-disk-0', 'nie je v storage.cfg'), ('unused0: nas:vm-100-disk-3', 'unused0')):
        try:
            a.require_guest_disks_local(host, config, facts, 'VM 100')
        except ValueError as exc:
            assert expected in str(exc) and '--force' in str(exc), (config, exc)
        else:
            raise AssertionError(f'{config} musí byť odmietnuté')


def test_a01_move_stops_before_any_write_on_shared_storage():
    with in_temp_cwd_ctx():
        facts = {'files': {'etc/pve/storage.cfg': 'nfs: nas\n\tserver 192.0.2.9\n\texport /vm\n\tcontent images\n'}, 'members': set(), 'info': {}}
        ScriptedHost.replies[('old', 'qm config 100')] = ('scsi0: nas:vm-100-disk-0\nonboot: 1\n', '')
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_guest_move(Log(), make_mctx(facts=facts), 100, '/d', 'local-lvm')
            except ValueError as exc:
                assert 'zdieľanom' in str(exc)
            else:
                raise AssertionError('presun hosťa so zdieľaným diskom musí byť odmietnutý')
        forbidden = ('--onboot 0', 'shutdown', 'vzdump', 'qmrestore')
        assert not [command for _host, command in ScriptedHost.commands if any(word in command for word in forbidden)]


# --- A06: živé poistky a kontrola cieľa -----------------------------------------------------

def test_a06_live_guard_fails_closed():
    def guard(replies=None, **kwargs):
        ScriptedHost.reset()
        ScriptedHost.replies.update(replies or {})
        with patch.object(a, 'MigrationHost', ScriptedHost):
            return a.migration_live_guard(Log(), ScriptedHost({'host': 'old'}), ScriptedHost({'host': 'new'}), **kwargs)

    guard(node='nuc')
    cases = [
        ({('new', 'cat /etc/machine-id'): 'aaa\n'}, ValueError, 'ten istý server'),
        ({('new', 'cat /etc/machine-id'): (None, 'x')}, RuntimeError, 'machine-id'),
        ({('new', 'LC_ALL=C pveversion'): 'pve-manager/8.4.1/abc\n'}, ValueError, 'staršiu major'),
        ({('old', 'LC_ALL=C pveversion'): (None, 'x')}, RuntimeError, 'Verziu'),
        ({('old', 'if test -e /etc/pve/corosync.conf; then echo cluster; else echo standalone; fi'): 'cluster\n'}, ValueError, 'clustra'),
        ({('old', 'if test -e /etc/pve/corosync.conf; then echo cluster; else echo standalone; fi'): (None, 'x')}, RuntimeError, 'clustri'),
        ({('old', 'LC_ALL=C hostname'): 'iny\n'}, ValueError, 'archív patrí'),
    ]
    for replies, error, text in cases:
        try:
            guard(replies, node='nuc')
        except error as exc:
            assert text in str(exc), (replies, exc)
        else:
            raise AssertionError(f'{replies} musí zablokovať operáciu')


def test_a06_target_must_have_successful_check():
    def target(identity='ok', version='ok', ok=True):
        checks = [{'id': 'ssh', 'level': 'ok'}, {'id': 'identity', 'level': identity}, {'id': 'address', 'level': 'ok'},
                  {'id': 'pve', 'level': 'ok'}, {'id': 'version', 'level': version}]
        return {'last_check': {'ok': ok, 'checks': checks}}

    a.migration_target_ready(target())
    a.migration_target_ready(target(version='warning'))
    for bad in (target(identity='error', ok=False), target(identity='unknown'), target(version='unknown'), {'last_check': None}, None):
        try:
            a.migration_target_ready(bad)
        except ValueError as exc:
            assert 'Kontrola nového hosta' in str(exc)
        else:
            raise AssertionError('neúspešná kontrola cieľa musí blokovať zápisy')


# --- A07: relácia viazaná na hosty a archív --------------------------------------------------

@in_temp_cwd
def test_a07_session_pins_hosts_and_archive(directory):
    archive = directory / 'archive.tar.gz'
    archive.write_bytes(b'archive-bytes')
    ctx = a.MigrationContext.__new__(a.MigrationContext)
    ctx.session = None
    ctx.old_ssh = {'host': 'old', 'port': 22}
    ctx.target = {'host': 'new', 'port': 22}
    ctx.entry = {'id': 'e1', 'filename': 'archive.tar.gz'}
    ctx.archive_path = str(archive)
    ctx.transfer = a.default_migration_transfer()
    a.update_migration_transfer(lambda t: None)
    ctx.pin_session()
    session = a.load_migration_transfer()['session']
    assert session['source'] == 'old:22' and session['target'] == 'new:22' and session['archive_id'] == 'e1'
    assert session['archive_sha256'] == a.archive_sha256(str(archive))
    ctx.verify_session_hosts()
    ctx.target = {'host': 'other', 'port': 22}
    try:
        ctx.verify_session_hosts()
    except ValueError as exc:
        assert 'iné hosty' in str(exc)
    else:
        raise AssertionError('zmena hostov počas prenosu musí byť odmietnutá')


# --- A09: cutover ----------------------------------------------------------------------------

def cutover_mctx(**overrides):
    transfer = a.default_migration_transfer()
    transfer['config_db'] = {'jobs_disabled': ['job1']}
    transfer['files'] = {'timers_to_enable': ['backup.timer']}
    transfer['guests'] = {'100': {'type': 'VM', 'moved_at': 'x', 'original_onboot': True},
                          '101': {'type': 'VM', 'moved_at': 'x', 'original_onboot': True},
                          '102': {'type': 'VM', 'original_onboot': True, 'rolled_back_at': 'y'}}
    state = {'guests': {'100': {'type': 'VM', 'status': 'verified'}, '101': {'type': 'VM', 'status': 'rolled_back'},
                        '102': {'type': 'VM', 'status': 'rolled_back'}}}
    return make_mctx(transfer=transfer, state=state, **overrides)


def test_a09_cutover_blocks_when_old_scheduler_stays_enabled():
    with in_temp_cwd_ctx():
        # starý timer sa nepodarilo vypnúť
        ScriptedHost.replies[('old', 'systemctl is-active backup.timer || true')] = 'active\n'
        ScriptedHost.replies[('old', 'systemctl is-enabled backup.timer || true')] = 'enabled\n'
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_cutover(Log(), cutover_mctx())
            except RuntimeError as exc:
                assert 'Nový plánovač som nezapol' in str(exc) and 'timer backup.timer' in str(exc)
            else:
                raise AssertionError('cutover musí zlyhať')
        assert not [command for host, command in ScriptedHost.commands if host == 'new' and ('enable' in command and 'pvesh set' in command or 'enable --now' in command)]
        assert a.load_migration_transfer()['cutover'] is None


def test_a09_cutover_verifies_old_jobs_and_reports_new_failures():
    with in_temp_cwd_ctx():
        ScriptedHost.defaults = dict(ScriptedHost.defaults)
        ScriptedHost.replies[('old', 'systemctl is-active backup.timer || true')] = 'inactive\n'
        ScriptedHost.replies[('old', 'systemctl is-enabled backup.timer || true')] = 'disabled\n'
        ScriptedHost.replies[('old', 'pvesh get /cluster/backup --output-format json')] = json.dumps([{'id': 'job1', 'enabled': 1}])
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_cutover(Log(), cutover_mctx())
            except RuntimeError as exc:
                assert 'vzdump job job1' in str(exc)
            else:
                raise AssertionError('zapnutý starý job musí zablokovať cutover')
        assert not any(host == 'new' and '--enabled 1' in command for host, command in ScriptedHost.commands)

    with in_temp_cwd_ctx():
        ScriptedHost.replies[('old', 'systemctl is-active backup.timer || true')] = 'inactive\n'
        ScriptedHost.replies[('old', 'systemctl is-enabled backup.timer || true')] = 'disabled\n'
        ScriptedHost.replies[('old', 'pvesh get /cluster/backup --output-format json')] = json.dumps([{'id': 'job1', 'enabled': 0}])
        ScriptedHost.replies[('new', 'systemctl enable --now backup.timer')] = (None, 'exit 1')
        with patch.object(a, 'MigrationHost', ScriptedHost):
            try:
                a.run_migration_cutover(Log(), cutover_mctx())
            except RuntimeError as exc:
                assert 'čiastočné' in str(exc) and 'timer backup.timer' in str(exc)
            else:
                raise AssertionError('zlyhanie zapnutia na novom nesmie skončiť ako úspech')
        assert a.load_migration_transfer()['cutover'] is None


def test_a10_cutover_enables_autostart_only_for_moved_guests():
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('old', 'systemctl is-active backup.timer || true')] = 'inactive\n'
        ScriptedHost.replies[('old', 'systemctl is-enabled backup.timer || true')] = 'disabled\n'
        ScriptedHost.replies[('old', 'pvesh get /cluster/backup --output-format json')] = json.dumps([{'id': 'job1', 'enabled': 0}])
        with patch.object(a, 'MigrationHost', ScriptedHost):
            result = a.run_migration_cutover(Log(), cutover_mctx())
        assert result['autostart'] == ['100']
        new_commands = [command for host, command in ScriptedHost.commands if host == 'new']
        assert 'qm set 100 --onboot 1' in new_commands
        assert 'qm set 101 --onboot 1' not in new_commands and 'qm set 102 --onboot 1' not in new_commands


def test_a10_rolled_back_guest_does_not_satisfy_cutover():
    assert 'rolled_back' not in ('verified', 'skipped')
    assert a.MIGRATION_GUEST_TRANSITIONS['rolled_back'] == ['pending', 'skipped']
    steps = a.migration_final_steps(cutover_mctx())
    text = json.dumps(steps, ensure_ascii=False)
    assert 'VM 101 (rolled_back)' in text and 'po poweroff nepobežia nikde' in text


# --- A11: LXC s appkou ---------------------------------------------------------------------

def test_a11_app_guest_found_without_static_ip():
    facts = {'files': {
        'etc/pve/nodes/nuc/lxc/113.conf': 'hostname: proxmox-backup\nnet0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,type=veth\n',
        'etc/pve/nodes/nuc/lxc/114.conf': 'hostname: other\nnet0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:02,ip=dhcp,type=veth\n',
        'etc/pve/nodes/nuc/qemu-server/200.conf': 'net0: virtio=BC:24:11:AA:AA:AA,bridge=vmbr0\n',
    }}
    assert a.find_app_guest(facts, None, own_macs={'bc:24:11:00:00:01'}, own_hostname='x')['vmid'] == 113
    assert a.find_app_guest(facts, None, own_macs={'bc:24:11:aa:aa:aa'}, own_hostname='x')['vmid'] == 200
    assert a.find_app_guest(facts, None, own_macs=set(), own_hostname='Proxmox-Backup')['vmid'] == 113
    assert a.find_app_guest(facts, '10.0.0.1', own_macs=set(), own_hostname='nothing') is None


# --- A12: história záloh pod lockom a atomicky ------------------------------------------------

@in_temp_cwd
def test_a12_concurrent_history_updates_do_not_lose_entries(directory):
    import threading
    workers, per_worker = 6, 15
    errors = []

    def writer(index):
        try:
            for item in range(per_worker):
                a.update_backup_history(lambda history, key=f'{index}-{item}': history.append({'id': key}))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                a.load_backup_history()  # nikdy nesmie vidieť neúplný JSON
            except Exception as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(workers)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    watcher.join()
    assert not errors, errors
    assert len(a.load_backup_history()) == workers * per_worker
    assert (os.stat(a.BACKUP_HISTORY_FILE).st_mode & 0o777) == 0o600


@in_temp_cwd
def test_a12_nested_history_lock_is_reentrant(directory):
    with a.backup_history_lock():
        a.update_backup_history(lambda history: history.append({'id': 'x'}))
        a.save_backup_history(a.load_backup_history())
    assert a.load_backup_history() == [{'id': 'x'}]


# --- A13: štandardný systemd mask link ------------------------------------------------------

@in_temp_cwd
def test_a13_masked_systemd_unit_does_not_block_archive(directory):
    archive = directory / 'masked.tar.gz'

    def build(extra_link=None):
        with tarfile.open(archive, 'w:gz') as tar:
            data = b'nuc\n'
            info = tarfile.TarInfo('etc/hostname')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for name, target in [('etc/systemd/system/masked.service', '/dev/null')] + ([extra_link] if extra_link else []):
                link = tarfile.TarInfo(name)
                link.type = tarfile.SYMTYPE
                link.linkname = target
                tar.addfile(link)

    build()
    members = a.restore_archive_members(str(archive), ['/etc/hostname'])
    assert [member.name for member in members] == ['etc/hostname']
    assert a.read_archive_facts(str(archive))['files']['etc/hostname'].strip() == 'nuc'
    for name, target in (('etc/other-link', '/dev/null'), ('etc/systemd/system/x.service', '/proc/self/environ'),
                         ('etc/systemd/system/y.service', '/mnt/secret')):
        build((name, target))
        try:
            a.restore_archive_members(str(archive), ['/etc/hostname'])
        except ValueError as exc:
            assert 'Nebezpečný link' in str(exc), exc
        else:
            raise AssertionError(f'{name} -> {target} musí zostať odmietnutý')


# --- A15: NIC mapovanie ---------------------------------------------------------------------

def run_network(mapping, interfaces):
    uploaded = {}
    facts = {'files': {'etc/network/interfaces': interfaces}, 'members': set(), 'info': {}}
    with in_temp_cwd_ctx():
        ScriptedHost.replies[('new', 'LC_ALL=C ip -br link')] = 'enp1s0 UP aa\nenp2s0 UP bb\nenp3s0 UP cc\n'
        with patch.object(a, 'MigrationHost', ScriptedHost), \
                patch.object(a, 'sftp_put_bytes', lambda host, path, data, mode=0o600: uploaded.__setitem__(path, data.decode())), \
                patch.object(a, 'update_migration_transfer', lambda mutate: None):
            result = a.run_migration_network(Log(), make_mctx(facts=facts), mapping)
    return result, uploaded


def test_a15_nic_swap_is_simultaneous():
    interfaces = 'iface enp1s0 inet manual\niface enp2s0 inet manual\niface vmbr0 inet static\n bridge-ports enp1s0 enp2s0\n'
    result, uploaded = run_network({'enp1s0': 'enp2s0', 'enp2s0': 'enp1s0'}, interfaces)
    proposed = uploaded[f'{a.MIGRATION_WORKDIR}/interfaces.proposed']
    assert 'bridge-ports enp2s0 enp1s0' in proposed and 'iface enp2s0 inet manual\niface enp1s0 inet manual' in proposed
    assert result['unmapped'] == []


def test_a15_ambiguous_mapping_is_rejected():
    interfaces = 'iface enp1s0 inet manual\niface enp2s0 inet manual\niface vmbr0 inet static\n bridge-ports enp1s0 enp2s0\n'
    for mapping, text in (({'enp1s0': 'enp3s0', 'enp2s0': 'enp3s0'}, 'rovnakú kartu'),
                          ({'enp1s0': 'enp2s0'}, 'nemapovanej pôvodnej')):
        try:
            run_network(mapping, interfaces)
        except ValueError as exc:
            assert text in str(exc), exc
        else:
            raise AssertionError(f'{mapping} musí byť odmietnuté')


# --- A20: zdieľaný adresár záloh ------------------------------------------------------------

def test_a20_shared_dump_dir_is_proven_with_marker():
    markers = set()

    def make_reply(shared):
        def reply(self, command, timeout=None):
            if command.startswith('touch '):
                markers.add(command.split(' ', 1)[1])
                return '', ''
            if command.startswith('if test -f '):
                path = command.split(' ')[3].rstrip(';')
                return ('visible\n' if shared and path in markers else 'missing\n'), ''
            return '', ''
        return reply

    old, new = ScriptedHost({'host': 'old'}), ScriptedHost({'host': 'new'})
    with patch.object(ScriptedHost, 'try_run', make_reply(True)):
        a.verify_shared_dump_dir(Log(), old, new, '/dump')
    assert markers, 'marker sa musel zapísať na starom hoste'
    with patch.object(ScriptedHost, 'try_run', make_reply(False)):
        try:
            a.verify_shared_dump_dir(Log(), old, new, '/dump')
        except RuntimeError as exc:
            assert 'ten istý export' in str(exc)
        else:
            raise AssertionError('nezdieľaný adresár musí zablokovať presun')


# --- A22: retencia nad zjednoteným inventárom -----------------------------------------------

@in_temp_cwd
def test_a22_retention_counts_ftp_only_archives(directory):
    local = Path(a.BACKUP_STORAGE_DIR) / 'proxmox_backup_local_20260503_100000_000001.tar.gz'
    local.write_bytes(b'x')
    a.save_backup_history([{'id': 'l1', 'filename': local.name, 'timestamp': '2026-05-03T10:00:00', 'local_path': str(local), 'ftp_status': 'success'}])
    ftp_archives = [{'filename': 'proxmox_backup_local_20260501_100000_000001.tar.gz', 'timestamp': '', 'id': 'ftp:a'},
                    {'filename': 'proxmox_backup_local_20260502_100000_000001.tar.gz', 'timestamp': '2026-05-02T10:00:00', 'id': 'ftp:b'},
                    {'filename': local.name, 'timestamp': '2026-05-03T10:00:00', 'id': 'ftp:l'}]
    ftp = {'host': 'h', 'username': 'u', 'password': 'p'}
    deleted_ids = []

    def fake_delete(backup_id, config):
        deleted_ids.append(backup_id)
        return {'success': True, 'local_deleted': False, 'ftp_deleted': True}

    with patch.object(a, 'list_ftp_backups', lambda cfg: {'available': True, 'warning': '', 'archives': ftp_archives}), \
            patch.object(a, 'delete_backup_entry', fake_delete):
        result = a.enforce_backup_retention({'max_backup_count': 2}, ftp)
    assert deleted_ids == ['ftp:proxmox_backup_local_20260501_100000_000001.tar.gz'] or deleted_ids == ['ftp:a'], deleted_ids
    assert len(result['deleted']) == 1 and result['warnings'] == []

    deleted_ids.clear()
    with patch.object(a, 'list_ftp_backups', lambda cfg: {'available': False, 'warning': 'FTP down', 'archives': []}), \
            patch.object(a, 'delete_backup_entry', fake_delete):
        result = a.enforce_backup_retention({'max_backup_count': 1}, ftp)
    assert deleted_ids == [] and 'FTP down' in result['warnings'][0], 'pri výpadku FTP sa lokálna záloha nemaže navyše'
    assert local.exists()


# --- A23: SSH stream a atomický archív -----------------------------------------------------

class StreamStub:
    def __init__(self, data, exit_code=0):
        self._data = io.BytesIO(data)
        self.channel = SimpleNamespace(recv_exit_status=lambda: exit_code)

    def read(self, size=-1):
        return self._data.read(size)


class TarClient:
    def __init__(self, stdout, stderr, exit_code):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code

    def exec_command(self, command, timeout=None):
        return None, StreamStub(self.stdout, self.exit_code), StreamStub(self.stderr, self.exit_code)


@in_temp_cwd
def test_a23_stream_tar_drains_stderr_and_finalizes_atomically(directory):
    source = a.RemoteSshBackupSource({'mode': 'remote_ssh', 'ssh': {'host': 'h', 'username': 'root', 'password': 'x'}})
    target = str(directory / 'out.tar.gz')
    big_stderr = b'warn\n' * 100000
    code, text = source.stream_tar_to_local(TarClient(b'ARCHIVE' * 1000, big_stderr, 1), 'tar', target)
    assert code == 1 and Path(target).read_bytes() == b'ARCHIVE' * 1000
    assert len(text) <= 16 * 1024 and not os.path.exists(target + '.part')

    failed = str(directory / 'failed.tar.gz')
    try:
        source.stream_tar_to_local(TarClient(b'partial', b'boom', 2), 'tar', failed)
    except RuntimeError as exc:
        assert 'exit code 2' in str(exc) and 'boom' in str(exc)
    else:
        raise AssertionError('tar s exit 2 musí zlyhať')
    assert not os.path.exists(failed) and not os.path.exists(failed + '.part'), 'neúplný archív sa odstráni'

    empty = str(directory / 'empty.tar.gz')
    try:
        source.stream_tar_to_local(TarClient(b'', b'', 0), 'tar', empty)
    except RuntimeError as exc:
        assert 'žiadne dáta' in str(exc)
    assert not os.path.exists(empty) and not os.path.exists(empty + '.part')


# --- A24: dev auto timer ------------------------------------------------------------------

def test_a24_update_script_passes_app_port_to_auto_service():
    text = (ROOT / 'update.sh').read_text()
    service = text.split('cat > "${AUTO_SERVICE_FILE}" <<EOF', 1)[1].split('EOF', 1)[0]
    assert 'Environment=APP_PORT=${APP_PORT}' in service and 'Environment=APP_DIR=${APP_DIR}' in service


# --- pomocné ---------------------------------------------------------------------------------

class in_temp_cwd_ctx:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='pbm-audit-fix-', dir=str(ROOT))
        names = ('MIGRATION_STATE_FILE', 'MIGRATION_TRANSFER_FILE', 'MIGRATION_TARGET_FILE', 'MIGRATION_JOBS_FILE')
        self.originals = {name: getattr(a, name) for name in names}
        for name in names:
            setattr(a, name, os.path.join(self.directory.name, name.lower()))
        ScriptedHost.reset()
        ScriptedHost.defaults = dict(ScriptedHost.defaults)
        return self

    def __exit__(self, *_args):
        for name, value in self.originals.items():
            setattr(a, name, value)
        self.directory.cleanup()
        return False


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'test_audit_fixes: OK ({len(tests)} testov)')


if __name__ == '__main__':
    main()
