#!/usr/bin/env python3
"""Reproduce current backup/restore defects without production or remote access.

Assertions describe the audited defects, not desired behavior. If fixed, a
defect assertion will fail. All writable state is inside TemporaryDirectory.
FTP calls are replaced with local functions. No SSH commands are executed.
Run: venv/bin/python audit/2026-10-03/reproduce_backup.py
"""

import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading


REPO = Path(__file__).resolve().parents[2]


def reproduce_tar_selection(a, root):
    archive = root / 'directory.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        directory = tarfile.TarInfo('etc/network')
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        member = tarfile.TarInfo('etc/network/interfaces')
        member.size = 1
        tar.addfile(member, io.BytesIO(b'x'))
    names = [member.name for member in a.restore_archive_members(str(archive), ['/etc/network'])]
    members = root / 'members.txt'
    members.write_text('\n'.join(names) + '\n', encoding='utf-8')
    staging = root / 'staging'
    staging.mkdir()
    result = subprocess.run(
        ['tar', '-xzf', str(archive), '-C', str(staging), '-T', str(members)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, (result.returncode, result.stderr)
    assert 'Not found in archive' in result.stderr, result.stderr
    assert (staging / 'etc/network/interfaces').read_bytes() == b'x'
    print('CONFIRMED: directory-plus-child extraction returns tar exit 2 before restore applies files.')
    print('  ' + result.stderr.strip().replace('\n', ' | '))


def reproduce_masked_unit(a, root):
    archive = root / 'masked.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo('etc/hostname')
        member.size = 3
        tar.addfile(member, io.BytesIO(b'pve'))
        link = tarfile.TarInfo('etc/systemd/system/masked.service')
        link.type = tarfile.SYMTYPE
        link.linkname = '/dev/null'
        tar.addfile(link)
    checks = [
        ('selected hostname restore', lambda: a.restore_archive_members(str(archive), ['/etc/hostname'])),
        ('selected hostname migration hash', lambda: a.archive_tree_hashes(str(archive), '/etc/hostname')),
    ]
    for name, operation in checks:
        try:
            operation()
        except ValueError as exc:
            assert 'masked.service' in str(exc) and '/dev/null' in str(exc), str(exc)
            print(f'CONFIRMED: {name} blocked by unrelated standard masked unit.')
        else:
            raise AssertionError(f'Audited defect no longer reproduced: {name}')


def reproduce_empty_backup_retention(a, root):
    a.BACKUP_STORAGE_DIR = str(root / 'backups')
    a.BACKUP_HISTORY_FILE = str(root / 'retention_history.json')
    backups = Path(a.ensure_backup_storage_dir())
    old = backups / 'valid.tar.gz'
    with tarfile.open(old, 'w:gz') as tar:
        member = tarfile.TarInfo('etc/hostname')
        member.size = 3
        tar.addfile(member, io.BytesIO(b'pve'))
    a.save_backup_history([{
        'id': 'old', 'filename': old.name, 'local_path': str(old),
        'timestamp': '2026-01-01T00:00:00', 'ftp_status': 'success',
    }])
    missing = root / 'absent-selected-config'
    selected = [{'path': str(missing), 'name': 'synthetic absent config'}]
    deleted_ftp = []
    # Generate only harmless metadata; avoid local inventory subprocesses.
    def generate_info(info_dir, _selected):
        Path(info_dir, 'README-RESTORE.txt').write_text('Synthetic audit fixture\n', encoding='utf-8')
        return ['README-RESTORE.txt']
    def delete_ftp(filename, _config):
        deleted_ftp.append(filename)
        return True, 'mock FTP deletion'
    replacements = {
        'generate_backup_info': generate_info,
        'upload_to_ftp': lambda _path, _config: (True, 'mock FTP upload'),
        'sync_missing_ftp_backups': lambda *args, **kwargs: [],
        'delete_from_ftp': delete_ftp,
    }
    originals = {name: getattr(a, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(a, name, value)
        result = a.run_backup_job(
            [str(missing)], {'host': 'mock.invalid', 'username': 'mock', 'password': 'synthetic'},
            {'mode': 'local'}, selected, config={'max_backup_count': 1},
        )
    finally:
        for name, value in originals.items():
            setattr(a, name, value)
    assert result['success'] is True
    assert result['report']['included'] == []
    assert result['report']['skipped'] == [{'path': str(missing), 'reason': 'missing'}]
    assert not old.exists()
    assert deleted_ftp == ['valid.tar.gz']
    assert [entry['id'] for entry in result['retention_deleted']] == ['old']
    print('CONFIRMED: backup with no selected data reports success and deletes prior valid local/FTP backup.')


def reproduce_history_lost_update(a, root):
    a.BACKUP_HISTORY_FILE = str(root / 'concurrent_history.json')
    a.save_backup_history([])
    barrier = threading.Barrier(2)
    first_saved = threading.Event()
    errors = []
    def writer(entry_id):
        try:
            history = a.load_backup_history()
            barrier.wait(timeout=5)
            history.append({'id': entry_id})
            if entry_id == 'B':
                assert first_saved.wait(timeout=5)
            a.save_backup_history(history)
            if entry_id == 'A':
                first_saved.set()
        except BaseException as exc:
            errors.append(exc)
            first_saved.set()
    threads = [threading.Thread(target=writer, args=(entry_id,)) for entry_id in ('A', 'B')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), 'unexpected thread timeout'
    assert not errors, errors
    assert a.load_backup_history() == [{'id': 'B'}]
    print('CONFIRMED: two completed read/append/save history transactions lose entry A.')


def main():
    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='pbm-backup-audit-', dir='/tmp') as work:
        root = Path(work)
        # app.py initializes auth state at import. Ensure it uses ONLY /tmp.
        os.chdir(root)
        sys.path.insert(0, str(REPO))
        try:
            import app as a
            reproduce_tar_selection(a, root)
            reproduce_masked_unit(a, root)
            reproduce_empty_backup_retention(a, root)
            reproduce_history_lost_update(a, root)
        finally:
            os.chdir(previous_cwd)
    print('All four audited defects reproduced safely; temporary fixtures removed.')


if __name__ == '__main__':
    main()
