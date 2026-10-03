#!/usr/bin/env python3
"""Isolated audit reproductions. No network; only synthetic data under /tmp.

Assertions describe defects present at audit time, not desired behavior.
Run from repository: venv/bin/python audit/2026-10-03/reproduce_migration.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class Log:
    def log(self, *_args):
        pass

    progress = log


def main():
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix='pbm-audit-', dir='/tmp') as directory:
        os.chdir(directory)
        try:
            import app as a
            results = []

            def found(name, evidence):
                results.append({'finding': name, 'evidence': evidence})

            # Same journaling mode as current pmxcfs, with uncheckpointed commits.
            live = sqlite3.connect('live.db')
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('PRAGMA wal_autocheckpoint=0')
            live.execute('CREATE TABLE config(value TEXT)')
            live.commit()
            live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            live.execute("INSERT INTO config VALUES ('LATEST_COMMITTED_CONFIG')")
            live.commit()
            shutil.copyfile('live.db', 'raw-copy.db')
            with sqlite3.connect('raw-copy.db') as copied:
                integrity = copied.execute('PRAGMA integrity_check').fetchone()[0]
                rows = copied.execute('SELECT * FROM config').fetchall()
            assert integrity == 'ok' and rows == []
            found('M01-live-config-db', {'integrity_check': integrity, 'latest_committed_rows_in_copy': len(rows)})
            live.close()

            commands = []
            replies = {}

            class Host:
                def __init__(self, config):
                    self.name = config['host']

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def try_run(self, command, timeout=None):
                    commands.append((self.name, command))
                    default = ('status: stopped\n', '') if ' status ' in command else ('', '')
                    return replies.get((self.name, command), default)

            transfer = a.default_migration_transfer()
            mctx = SimpleNamespace(old_ssh={'host': 'old'}, target={'host': 'new'},
                                   transfer=transfer, app_guest=None,
                                   facts={'files': {}, 'members': set(), 'info': {}},
                                   guest_info=lambda vmid: {'vmid': vmid, 'type': 'VM', 'name': 'test'},
                                   archive_path='archive.tar.gz', entry={'filename': 'archive.tar.gz'}, node='nuc')

            def update(mutate):
                mutate(transfer)
                return transfer

            with ExitStack() as stack:
                stack.enter_context(patch.object(a, 'MigrationHost', Host))
                stack.enter_context(patch.object(a, 'set_migration_guest_status', lambda *args, **kwargs: None))
                stack.enter_context(patch.object(a, 'update_migration_transfer', update))

                # New guest may still be running, status retrieval has failed.
                replies[('new', 'qm status 100')] = (None, 'unavailable')
                a.run_migration_guest_rollback(Log(), mctx, 100)
                assert ('old', 'qm start 100') in commands
                found('M02-rollback-unknown', {'new_status': 'unknown', 'old_start_issued': True})

                # A guest rolled back to old retains moved_at in transfer metadata.
                commands.clear()
                transfer['guests']['100'] = {'type': 'VM', 'moved_at': 'previous-move', 'original_onboot': True}
                a.run_migration_cutover(Log(), mctx)
                assert ('new', 'qm set 100 --onboot 1') in commands
                found('M03-cutover-rolled-back-guest', {'new_onboot_enabled_for_rolled_back_guest': True})

                # Failure to stop old backup timer doesn't prevent new timer start.
                commands.clear()
                transfer['guests'] = {}
                transfer['files'] = {'timers_to_enable': ['backup.timer']}
                replies[('old', 'systemctl disable --now backup.timer')] = (None, 'exit 1')
                result = a.run_migration_cutover(Log(), mctx)
                assert ('new', 'systemctl enable --now backup.timer') in commands and result['applied_at']
                found('M04-cutover-false-success', {'old_timer_disable': 'failed', 'new_timer_enable_issued': True, 'cutover_recorded': True})

                # Sequential substitution cascades when NIC names are swapped.
                commands.clear()
                mctx.facts['files']['etc/network/interfaces'] = (
                    'iface enp1s0 inet manual\niface enp2s0 inet manual\n'
                    'iface vmbr0 inet static\n bridge-ports enp1s0 enp2s0\n')
                replies[('new', 'LC_ALL=C ip -br link')] = ('enp1s0 UP aa\nenp2s0 UP bb\n', '')
                with patch.object(a, 'sftp_put_bytes', lambda *args, **kwargs: None):
                    result = a.run_migration_network(Log(), mctx, {'enp1s0': 'enp2s0', 'enp2s0': 'enp1s0'})
                assert 'bridge-ports enp1s0 enp1s0' in result['interfaces']
                found('M05-nic-mapping-cascade', {'bridge_ports': 'enp1s0 enp1s0'})

                # Retry overwrites original onboot saved during failed first attempt.
                commands.clear()
                transfer['guests']['100'] = {'original_onboot': True}
                replies.pop(('new', 'qm status 100'))
                replies[('old', 'qm config 100')] = ('name: test\nonboot: 0\n', '')
                with patch.object(a, 'migration_stream', side_effect=[
                    (0, ["INFO: creating vzdump archive '/shared/vzdump-qemu-100-test.vma.zst'"]),
                    (0, ['restore complete'])]):
                    a.run_migration_guest_move(Log(), mctx, 100, '/shared', 'local-lvm')
                assert transfer['guests']['100']['original_onboot'] is False
                found('M06-retry-loses-onboot', {'original_saved': True, 'after_retry': False})

                # Failure after pmxcfs restart lies outside database rollback scope.
                commands.clear()
                replies[('new', 'LC_ALL=C hostname')] = ('nuc\n', '')
                replies[('new', 'test -e /etc/pve/corosync.conf')] = (None, 'exit 1')
                replies[('new', 'systemctl is-active pve-cluster || true')] = ('active\n', '')
                replies[('new', "sed -i 's/^enable: 1$/enable: 0/' /etc/pve/firewall/cluster.fw")] = (None, 'exit 1')
                original_try = Host.try_run

                def with_integrity(host, command, timeout=None):
                    if 'integrity_check' in command:
                        commands.append((host.name, command))
                        return 'ok\n', ''
                    return original_try(host, command, timeout)

                with patch.object(a, 'archive_member_bytes', return_value=b'synthetic-db'), \
                        patch.object(a, 'firewall_enabled_in_archive', return_value=True), \
                        patch.object(a, 'sftp_put_bytes', lambda *args, **kwargs: None), \
                        patch.object(Host, 'try_run', with_integrity):
                    try:
                        a.run_migration_config_db(Log(), mctx)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('Expected firewall operation failure')
                restored_db = any('cp /root/pbm-migration/config.db.before-' in command for _, command in commands)
                assert ('new', 'systemctl start pve-cluster') in commands and not restored_db
                found('M07-config-db-partial-failure', {'new_db_started': True, 'original_db_restored': False})

            # Transfer context does not enforce a failed identity/version preflight.
            state = a.default_migration_state()
            state['method'] = 'side_by_side'
            target = {'host': 'new', 'password': 'synthetic', 'last_check': {'ok': False,
                      'checks': [{'id': 'identity', 'level': 'error'}]}}
            Path('placeholder').write_bytes(b'test')
            with ExitStack() as stack:
                for name, value in {
                    'load_migration_state': state, 'load_migration_target': target,
                    'migration_old_ssh': {'host': 'old', 'port': 22, 'password': 'synthetic'},
                    'visible_backup_history': [], 'latest_local_archive_entry': ({}, 'placeholder'),
                    'read_archive_facts': {'files': {}, 'members': set(), 'info': {}},
                    'detect_own_ip': None,
                }.items():
                    stack.enter_context(patch.object(a, name, return_value=value))
                context = a.MigrationContext()
            assert context.target['last_check']['ok'] is False
            found('M08-failed-preflight-accepted', {'MigrationContext_created': True, 'identity_preflight': 'error'})

            # Saving a new target mutates it even if an active job rejects the request.
            with a.app.test_request_context('/api/recovery/migration/target', method='POST',
                                           json={'host': '192.0.2.99', 'password': 'synthetic'}), \
                    patch.object(a, 'migration_old_ssh', return_value=None), \
                    patch.object(a, 'start_migration_preflight', side_effect=ValueError('An operation is running')):
                _response, status = a.recovery_migration_target_api()
            assert status == 400 and a.load_migration_target()['host'] == '192.0.2.99'
            found('M09-target-changes-on-rejected-save', {'HTTP': status, 'persisted_target': '192.0.2.99'})

            # The app protection only finds an explicit static IPv4 in guest config.
            dhcp_facts = {'files': {'etc/pve/nodes/nuc/lxc/113.conf':
                          'net0: name=eth0,bridge=vmbr0,ip=dhcp\n'}}
            assert a.find_app_guest(dhcp_facts, '192.0.2.10') is None
            found('M10-app-lxc-dhcp-not-identified', {'app_guest_detected': False})

            print(json.dumps(results, ensure_ascii=False, indent=2))
            print(f'Confirmed {len(results)} audit reproductions; no network or host operations executed.')
        finally:
            os.chdir(original_cwd)


if __name__ == '__main__':
    main()
