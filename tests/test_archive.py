#!/usr/bin/env python3
"""Smoke tests for Proxmox host backup archive creation."""

import os
import sys
import tarfile
import tempfile
import io
import json
import ftplib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402


class FakeChannel:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code

    def recv_exit_status(self):
        return self.exit_code


class FakeStream:
    def __init__(self, data=b"", exit_code=0):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._data = io.BytesIO(data)
        self.channel = FakeChannel(exit_code)

    def read(self, size=-1):
        return self._data.read(size)


class FakeRemoteFile:
    def __init__(self, path, files):
        self.path = path
        self.files = files
        self.content = ""

    def write(self, content):
        self.content += str(content)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.files[self.path] = self.content


class FakeSftp:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.closed = False

    def file(self, path, mode):
        return FakeRemoteFile(path, self.files)

    def put(self, local_path, remote_path):
        self.puts.append((local_path, remote_path))
        self.files[remote_path] = Path(local_path).read_bytes()

    def close(self):
        self.closed = True


class FakeSshClient:
    def __init__(self):
        self.commands = []
        self.sftp = FakeSftp()
        self.closed = False

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def open_sftp(self):
        return self.sftp

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("mktemp -d /tmp/pve-restore"):
            return None, FakeStream("/tmp/pve-restore.TEST\n"), FakeStream()
        if command.startswith("mktemp -d"):
            return None, FakeStream("/tmp/pve-host-backup-info.TEST\n"), FakeStream()
        if command.startswith("mkdir -p"):
            return None, FakeStream(), FakeStream()
        if command.startswith("python3 -c"):
            return None, FakeStream(json.dumps(["/etc/pve/storage.cfg"])), FakeStream()
        if command.startswith("test -e"):
            exit_code = 0 if "/mnt" in command or "/etc/hostname" in command or "/tmp/pve-restore.TEST/staging" in command else 1
            return None, FakeStream(exit_code=exit_code), FakeStream()
        if command.startswith("tar -xzf"):
            return None, FakeStream(), FakeStream()
        if command.startswith("tar "):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                payload = b"remote info"
                info = tarfile.TarInfo("backup-info/pvesm-config.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            return None, FakeStream(buffer.getvalue()), FakeStream()
        if command.startswith("cp -a") or command.startswith("if test -e"):
            return None, FakeStream(), FakeStream()
        if command.startswith("rm -rf"):
            return None, FakeStream(), FakeStream()
        return None, FakeStream("ok\n"), FakeStream()

    def close(self):
        self.closed = True


class FakeFtp:
    instances = []

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.cwd_calls = []
        self.stored = []
        self.deleted = []
        self.files = {}
        self.closed = False
        FakeFtp.instances.append(self)

    def connect(self, host, port):
        self.host = host
        self.port = port

    def login(self, username, password):
        self.username = username
        self.password = password

    def cwd(self, remote_dir):
        self.cwd_calls.append(remote_dir)

    def pwd(self):
        return self.cwd_calls[-1] if self.cwd_calls else "/"

    def storbinary(self, command, file_obj):
        payload = file_obj.read()
        self.stored.append((command, payload))
        filename = command.replace("STOR ", "", 1)
        self.files[filename] = payload

    def delete(self, filename):
        self.deleted.append(filename)
        self.files.pop(filename, None)

    def size(self, filename):
        if filename not in self.files:
            raise ftplib.error_perm("550 File not found")
        return len(self.files[filename])

    def nlst(self, filename=None):
        if filename:
            if filename in self.files:
                return [filename]
            raise ftplib.error_perm("550 File not found")
        return list(self.files.keys())

    def quit(self):
        self.closed = True


def main():
    migrated = app_module.migrate_config({"backup_files": [{"path": "/etc/pve", "selected": False}]})
    migrated_by_path = {item["path"]: item for item in migrated["backup_files"]}
    for path in ["/etc/passwd", "/etc/group", "/etc/shadow", "/etc/subuid", "/etc/subgid", "/etc/ssh"]:
        assert path in migrated_by_path
        assert migrated_by_path[path]["selected"] is True
        assert migrated_by_path[path]["critical"] is True
    assert migrated_by_path["/etc/pve"]["selected"] is False

    with tempfile.TemporaryDirectory(prefix="pve-backup-test-", dir=str(ROOT)) as workdir:
        source_dir = Path(workdir) / "mock-config"
        source_dir.mkdir()
        (source_dir / "auto.nfs").write_text(
            "qnap-storage -fstype=nfs 192.168.150.2:/ProxmoxBackups\n",
            encoding="utf-8",
        )

        archive_path = Path(workdir) / "backup.tar.gz"
        report = app_module.create_backup_archive(
            [
                {"path": str(source_dir), "name": "Mock config"},
                {"path": "/mnt", "name": "Forbidden mount"},
                {"path": str(Path(workdir) / "missing"), "name": "Missing"},
            ],
            str(archive_path),
            include_info=False,
        )

        assert archive_path.exists(), "archive was not created"
        assert any(item["path"] == str(source_dir.resolve()) for item in report["included"])
        assert any(item["reason"] == "missing" for item in report["skipped"])

        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()

        assert any(name.endswith("mock-config/auto.nfs") for name in names), names
        assert not any(name == "mnt" or name.startswith("mnt/") for name in names), names

    with tempfile.TemporaryDirectory(prefix="pve-remote-test-", dir=str(ROOT)) as workdir:
        fake_client = FakeSshClient()
        source = app_module.RemoteSshBackupSource(
            {
                "mode": "remote_ssh",
                "ssh": {
                    "host": "pve.example",
                    "port": 22,
                    "username": "root",
                    "password": "secret",
                },
            },
            ssh_client_factory=lambda: fake_client,
        )
        archive_path = Path(workdir) / "remote.tar.gz"
        report = source.create_archive(
            [
                {"path": "/etc/pve/*.cfg", "name": "PVE cfg"},
                {"path": "/mnt", "name": "Forbidden mount"},
            ],
            str(archive_path),
        )

        assert archive_path.exists(), "remote archive was not created"
        assert any(item["path"] == "/etc/pve/storage.cfg" for item in report["included"]), report
        assert any(item["path"] == "/mnt" and item["reason"] == "excluded" for item in report["skipped"]), report
        assert any(command.startswith("tar ") and "etc/pve/storage.cfg" in command for command in fake_client.commands)
        assert fake_client.closed, "SSH client was not closed"

        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
        assert "backup-info/pvesm-config.txt" in names, names

    with tempfile.TemporaryDirectory(prefix="pve-api-test-", dir=str(ROOT)) as workdir:
        original_config_file = app_module.CONFIG_FILE
        original_history_file = app_module.BACKUP_HISTORY_FILE
        original_backup_dir = app_module.BACKUP_STORAGE_DIR
        original_build_source = app_module.build_backup_source
        original_upload = app_module.upload_to_ftp
        original_ssh_factory = app_module.SSH_CLIENT_FACTORY

        try:
            app_module.CONFIG_FILE = str(Path(workdir) / "backup_config.json")
            app_module.BACKUP_HISTORY_FILE = str(Path(workdir) / "backup_history.json")
            app_module.BACKUP_STORAGE_DIR = str(Path(workdir) / "backups")

            config = app_module.default_config()
            app_module.save_config(config)
            assert oct(os.stat(app_module.CONFIG_FILE).st_mode & 0o777) == "0o600"

            client = app_module.app.test_client()
            response = client.post(
                "/api/backup",
                json={
                    "files": ["/etc/pve"],
                    "ftp_config": {},
                    "source_config": config["source_config"],
                },
            )
            assert response.status_code == 400
            assert "FTP konfigurácia" in response.get_json()["error"]

            class FakeSource:
                def create_archive(self, selected_files, backup_filename):
                    assert str(Path(app_module.BACKUP_STORAGE_DIR)) in backup_filename
                    with tarfile.open(backup_filename, "w:gz") as tar:
                        payload = b"info"
                        info = tarfile.TarInfo("backup-info/README-RESTORE.txt")
                        info.size = len(payload)
                        tar.addfile(info, io.BytesIO(payload))
                    return {
                        "source": "remote_ssh",
                        "included": [{"path": "/etc/pve", "arcname": "etc/pve"}],
                        "skipped": [],
                        "generated_info": ["README-RESTORE.txt"],
                        "excluded_paths": app_module.ARCHIVE_EXCLUDE_PATHS,
                    }

            seen_uploads = []

            def fake_upload(local_file, ftp_config):
                seen_uploads.append((local_file, ftp_config))
                return Path(local_file).exists(), "ok"

            app_module.build_backup_source = lambda source_config: FakeSource()
            app_module.upload_to_ftp = fake_upload

            response = client.post(
                "/api/backup",
                json={
                    "files": ["/etc/pve"],
                    "ftp_config": {
                        "host": "ftp.example",
                        "port": 21,
                        "username": "backup",
                        "password": "secret",
                        "remote_dir": "/pve-backups",
                    },
                    "source_config": {
                        "mode": "remote_ssh",
                        "ssh": {
                            "host": "pve.example",
                            "port": 22,
                            "username": "root",
                            "password": "secret",
                        },
                    },
                },
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            data = response.get_json()
            assert data["ftp_status"] == "success"
            assert Path(data["local_path"]).exists()
            assert seen_uploads and seen_uploads[0][0] == data["local_path"]
            assert seen_uploads[0][1]["remote_dir"] == "/pve-backups"
            assert Path(app_module.BACKUP_HISTORY_FILE).exists()

            backup_dir = Path(app_module.BACKUP_STORAGE_DIR)
            restore_archive = backup_dir / "restore.tar.gz"
            with tarfile.open(restore_archive, "w:gz") as tar:
                for name, payload in {
                    "etc/passwd": b"root:x:0:0:root:/root:/bin/bash\n",
                    "etc/ssh/sshd_config": b"PermitRootLogin yes\n",
                    "opt/ignored": b"ignored\n",
                }.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
                link = tarfile.TarInfo("etc/network/if-pre-up.d/bridge")
                link.type = tarfile.SYMTYPE
                link.linkname = "/lib/bridge-utils/ifupdown.sh"
                tar.addfile(link)

            bad_archive = backup_dir / "bad.tar.gz"
            with tarfile.open(bad_archive, "w:gz") as tar:
                payload = b"bad"
                info = tarfile.TarInfo("../etc/passwd")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            bad_link_archive = backup_dir / "bad-link.tar.gz"
            with tarfile.open(bad_link_archive, "w:gz") as tar:
                link = tarfile.TarInfo("etc/network/if-up.d/runtime")
                link.type = tarfile.SYMTYPE
                link.linkname = "/run/unsafe-target"
                tar.addfile(link)

            app_module.save_backup_history([
                {
                    "id": "restore-ok",
                    "filename": restore_archive.name,
                    "timestamp": "2026-05-29T12:00:00",
                    "local_path": str(restore_archive),
                    "size": "1 KB",
                    "source_mode": "remote_ssh",
                    "source_host": "pve.example",
                },
                {
                    "id": "outside",
                    "filename": "outside.tar.gz",
                    "local_path": "/etc/passwd",
                },
                {
                    "id": "bad",
                    "filename": bad_archive.name,
                    "local_path": str(bad_archive),
                },
                {
                    "id": "bad-link",
                    "filename": bad_link_archive.name,
                    "local_path": str(bad_link_archive),
                },
            ])

            response = client.get("/api/restore/archives")
            assert response.status_code == 200
            archives = response.get_json()["archives"]
            assert any(item["id"] == "restore-ok" for item in archives)
            assert not any(item["id"] == "outside" for item in archives)

            response = client.get("/api/restore/preview/restore-ok")
            assert response.status_code == 200, response.get_data(as_text=True)
            preview_paths = {item["path"] for item in response.get_json()["items"]}
            assert "/etc/passwd" in preview_paths
            assert "/etc/ssh" in preview_paths
            assert "/etc/network" in preview_paths
            assert "/opt" not in preview_paths

            response = client.get("/api/restore/preview/missing")
            assert response.status_code == 404

            response = client.get("/api/restore/preview/outside")
            assert response.status_code == 400
            assert "mimo" in response.get_json()["error"]

            response = client.get("/api/restore/preview/bad")
            assert response.status_code == 400
            assert "Nebezpečný" in response.get_json()["error"]

            response = client.get("/api/restore/preview/bad-link")
            assert response.status_code == 400
            assert "Nebezpečný link" in response.get_json()["error"]

            response = client.post(
                "/api/restore",
                json={
                    "backup_id": "restore-ok",
                    "paths": ["/home"],
                    "source_config": {
                        "mode": "remote_ssh",
                        "ssh": {
                            "host": "pve.example",
                            "port": 22,
                            "username": "root",
                            "password": "secret",
                        },
                    },
                    "confirm": "OBNOVIT",
                },
            )
            assert response.status_code == 400
            assert "nie je povolená" in response.get_json()["error"]

            fake_restore_client = FakeSshClient()
            app_module.SSH_CLIENT_FACTORY = lambda: fake_restore_client
            response = client.post(
                "/api/restore",
                json={
                    "backup_id": "restore-ok",
                    "paths": ["/etc/passwd"],
                    "source_config": {
                        "mode": "remote_ssh",
                        "ssh": {
                            "host": "pve.example",
                            "port": 22,
                            "username": "root",
                            "password": "secret",
                        },
                    },
                    "confirm": "OBNOVIT",
                },
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            restore_data = response.get_json()
            assert restore_data["backup_dir"].startswith("/root/proxmox-backup-restore-preapply-")
            assert restore_data["applied"] == [{"path": "/etc/passwd"}]
            assert any(command.startswith("tar -xzf") for command in fake_restore_client.commands)
            assert any(command.startswith("cp -a") and "/etc/" in command for command in fake_restore_client.commands)
            assert any(command.startswith("rm -rf /tmp/pve-restore.TEST") for command in fake_restore_client.commands)
        finally:
            app_module.CONFIG_FILE = original_config_file
            app_module.BACKUP_HISTORY_FILE = original_history_file
            app_module.BACKUP_STORAGE_DIR = original_backup_dir
            app_module.build_backup_source = original_build_source
            app_module.upload_to_ftp = original_upload
            app_module.SSH_CLIENT_FACTORY = original_ssh_factory

    original_ftp = app_module.ftplib.FTP
    try:
        FakeFtp.instances = []
        app_module.ftplib.FTP = FakeFtp

        with tempfile.TemporaryDirectory(prefix="pve-ftp-test-", dir=str(ROOT)) as workdir:
            local_file = Path(workdir) / "backup.tar.gz"
            local_file.write_bytes(b"archive")
            ftp_config = app_module.sanitize_ftp_config(
                {
                    "host": "ftp.example",
                    "port": 21,
                    "username": "backup",
                    "password": "secret",
                    "remote_dir": "/pve-backups",
                }
            )

            success, message = app_module.test_ftp_connection(**ftp_config)
            assert success, message
            assert FakeFtp.instances[-1].cwd_calls == ["/pve-backups"]
            assert FakeFtp.instances[-1].stored[0][0].startswith("STOR proxmox_backup_test_")
            assert FakeFtp.instances[-1].deleted, "FTP write test should delete temp file"

            success, message = app_module.upload_to_ftp(str(local_file), ftp_config)
            assert success, message
            assert FakeFtp.instances[-1].cwd_calls == ["/pve-backups"]
            assert FakeFtp.instances[-1].stored == [("STOR backup.tar.gz", b"archive")]

        with tempfile.TemporaryDirectory(prefix="pve-delete-test-", dir=str(ROOT)) as workdir:
            original_config_file = app_module.CONFIG_FILE
            original_history_file = app_module.BACKUP_HISTORY_FILE
            original_backup_dir = app_module.BACKUP_STORAGE_DIR
            try:
                app_module.CONFIG_FILE = str(Path(workdir) / "backup_config.json")
                app_module.BACKUP_HISTORY_FILE = str(Path(workdir) / "backup_history.json")
                app_module.BACKUP_STORAGE_DIR = str(Path(workdir) / "backups")
                backup_dir = Path(app_module.BACKUP_STORAGE_DIR)
                backup_dir.mkdir()
                archive = backup_dir / "delete-me.tar.gz"
                archive.write_bytes(b"archive")
                missing_archive = backup_dir / "missing.tar.gz"

                config = app_module.default_config()
                config["ftp_config"] = ftp_config
                app_module.save_config(config)
                app_module.save_backup_history([
                    {
                        "id": "missing-local",
                        "filename": missing_archive.name,
                        "local_path": str(missing_archive),
                        "ftp_status": "failed",
                    },
                ])

                client = app_module.app.test_client()
                response = client.get("/api/config")
                assert response.status_code == 200
                visible_ids = {entry["id"] for entry in response.get_json()["backup_history"]}
                assert "missing-local" not in visible_ids

                app_module.save_backup_history([
                    {
                        "id": "delete-me",
                        "filename": archive.name,
                        "local_path": str(archive),
                        "ftp_status": "success",
                    },
                ])

                response = client.delete("/api/backups/delete-me")
                assert response.status_code == 200, response.get_data(as_text=True)
                assert not archive.exists()
                assert not app_module.load_backup_history()
                assert FakeFtp.instances[-1].deleted == [archive.name]
            finally:
                app_module.CONFIG_FILE = original_config_file
                app_module.BACKUP_HISTORY_FILE = original_history_file
                app_module.BACKUP_STORAGE_DIR = original_backup_dir
    finally:
        app_module.ftplib.FTP = original_ftp


if __name__ == "__main__":
    main()
