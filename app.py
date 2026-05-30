#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify, redirect, url_for, flash, render_template
import os
import json
import tarfile
import ftplib
import tempfile
import glob
import fnmatch
import subprocess
import shlex
import posixpath
from datetime import datetime
import time

app = Flask(__name__)
app.secret_key = 'proxmox-backup-secret-key-change-in-production'

# Konfiguračný súbor
CONFIG_VERSION = 6
DEFAULT_MAX_BACKUP_COUNT = 10
CONFIG_FILE = 'backup_config.json'
BACKUP_HISTORY_FILE = 'backup_history.json'
BACKUP_STORAGE_DIR = os.environ.get('BACKUP_STORAGE_DIR', 'backups')

DEFAULT_SOURCE_CONFIG = {
    'mode': 'remote_ssh',
    'ssh': {
        'host': '',
        'port': 22,
        'username': 'root',
        'password': '',
    }
}

# Kategórie zobrazené v UI
BACKUP_CATEGORIES = [
    {
        'id': 'critical_proxmox',
        'name': 'Critical Proxmox',
        'description': 'Kľúčová Proxmox konfigurácia potrebná pri obnove hosta.'
    },
    {
        'id': 'host_access',
        'name': 'Host účty a SSH prístup',
        'description': 'Lokálne účty, shadow databáza a SSH server konfigurácia.'
    },
    {
        'id': 'system_config',
        'name': 'Systémová konfigurácia',
        'description': 'Sieť, balíky, systemd a ďalšie nastavenia hosta.'
    },
    {
        'id': 'admin_scripts',
        'name': 'Admin skripty a prístupy',
        'description': 'Ručné skripty, root nastavenia a lokálne nástroje.'
    },
    {
        'id': 'autofs_qnap_wd',
        'name': 'AUTO.FS / QNAP / WD',
        'description': 'Autofs mapy, vzdump orchestrátor a systemd timery pre NAS zálohy.'
    },
    {
        'id': 'optional_large',
        'name': 'Voliteľné veľké dáta',
        'description': 'Väčšie alebo site-specific adresáre, ktoré nemusia byť vhodné pre každú zálohu.'
    }
]

ARCHIVE_EXCLUDE_PATHS = [
    '/mnt',
    '/media',
    '/proc',
    '/sys',
    '/dev',
    '/run',
    '/tmp',
    '/var/tmp',
    '/var/cache',
    '/var/log',
    '/lost+found',
    '/etc/pve/.rrd',
    '/opt/proxmox-backup/venv',
]

ARCHIVE_EXCLUDE_GLOBS = [
    '*/__pycache__',
    '*/__pycache__/*',
    '*.pyc',
    '*.pyo',
    '*.log',
    '/opt/proxmox-backup/.git',
    '/opt/proxmox-backup/.git/*',
]

MIGRATION_PATH_ALIASES = {
    '/etc/network/interfaces': '/etc/network',
}

RETIRED_BACKUP_PATHS = {
    '/etc/ssl/pve',
}

INFO_COMMANDS = [
    ('pveversion-v.txt', ['pveversion', '-v']),
    ('qm-list.txt', ['qm', 'list']),
    ('pct-list.txt', ['pct', 'list']),
    ('pvesm-status.txt', ['pvesm', 'status']),
    ('pvesm-config.txt', ['pvesm', 'config']),
    ('pve-backup-jobs.json', ['pvesh', 'get', '/cluster/backup', '--output-format', 'json']),
    ('network-interfaces.txt', ['cat', '/etc/network/interfaces']),
    ('ip-addr.txt', ['ip', 'addr']),
    ('ip-route.txt', ['ip', 'route']),
    ('bridge-link.txt', ['bridge', 'link']),
    ('lsblk-f.txt', ['lsblk', '-f']),
    ('blkid.txt', ['blkid']),
    ('df-h.txt', ['df', '-h']),
    ('mount.txt', ['mount']),
    ('findmnt.txt', ['findmnt']),
    ('systemctl-unit-files.txt', ['systemctl', 'list-unit-files']),
    ('systemctl-timers.txt', ['systemctl', 'list-timers']),
    ('crontab-root.txt', ['crontab', '-l']),
    ('dpkg-selections.txt', ['dpkg', '--get-selections']),
    ('apt-manual.txt', ['apt-mark', 'showmanual']),
]

# Predvolené súbory na zálohovanie
DEFAULT_BACKUP_FILES = [
    {
        'path': '/etc/pve',
        'name': 'PVE konfigurácia',
        'description': 'VM/LXC configy, storage.cfg, users, firewall a datacenter nastavenia.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'sensitive', 'pve upgrade'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/var/lib/pve-cluster/config.db',
        'name': 'PVE cluster databáza',
        'description': 'Lokálna pmxcfs databáza dôležitá pri obnove Proxmox konfigurácie.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/network',
        'name': 'Sieťová konfigurácia',
        'description': 'Interfaces, bridge, VLAN a ďalšie sieťové nastavenia.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'network', 'pve upgrade'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/hosts',
        'name': 'Hosts súbor',
        'description': 'Mapovanie IP adries a názvov.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'network'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/hostname',
        'name': 'Názov hostiteľa',
        'description': 'Identifikácia servera.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/fstab',
        'name': 'Mounty a storage',
        'description': 'Lokálne mounty, NFS/CIFS a storage väzby hosta.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'storage'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/resolv.conf',
        'name': 'DNS konfigurácia',
        'description': 'Nastavenia DNS serverov.',
        'category': 'critical_proxmox',
        'priority': 'critical',
        'tags': ['critical', 'network', 'pve upgrade'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/passwd',
        'name': 'Lokálne používateľské účty',
        'description': 'Základná databáza lokálnych používateľov a systémových účtov.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive', 'pve upgrade'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/group',
        'name': 'Lokálne skupiny',
        'description': 'Základná databáza lokálnych skupín.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/shadow',
        'name': 'Shadow databáza',
        'description': 'Hashované heslá lokálnych účtov; extrémne citlivý súbor.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/subuid',
        'name': 'Subuid mapovanie',
        'description': 'Mapovanie subordinate UID rozsahov pre unprivileged kontajnery.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/subgid',
        'name': 'Subgid mapovanie',
        'description': 'Mapovanie subordinate GID rozsahov pre unprivileged kontajnery.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/ssh',
        'name': 'SSH konfigurácia hosta',
        'description': 'Konfigurácia SSH servera a host keys potrebné pri obnove identity hosta.',
        'category': 'host_access',
        'priority': 'critical',
        'tags': ['critical', 'sensitive'],
        'critical': True,
        'selected': True
    },
    {
        'path': '/etc/apt',
        'name': 'APT repozitáre',
        'description': 'Repozitáre a apt konfigurácia.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/systemd/system',
        'name': 'Vlastné systemd jednotky',
        'description': 'Lokálne services a timery vrátane vlastných backup jobov.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended', 'systemd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/default',
        'name': 'Default konfigurácie služieb',
        'description': 'Konfiguračné súbory pre systémové služby.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/modules',
        'name': 'Kernel moduly',
        'description': 'Moduly načítavané pri štarte.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/modprobe.d',
        'name': 'Modprobe konfigurácia',
        'description': 'Konfigurácia kernel modulov.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/sysctl.conf',
        'name': 'Sysctl konfigurácia',
        'description': 'Kernel runtime nastavenia.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/sysctl.d',
        'name': 'Sysctl konfigurácie',
        'description': 'Dodatočné kernel runtime nastavenia.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/var/spool/cron',
        'name': 'Cron úlohy',
        'description': 'Root/user cron joby.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/cron*',
        'name': 'Systémové cron úlohy',
        'description': 'Cron.d, cron.daily a ďalšie systémové plánované úlohy.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/vzdump.conf',
        'name': 'Vzdump konfigurácia',
        'description': 'Globálne nastavenia Proxmox vzdump záloh.',
        'category': 'system_config',
        'priority': 'recommended',
        'tags': ['recommended', 'proxmox'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/root',
        'name': 'Root adresár',
        'description': 'Skripty, SSH kľúče, poznámky a nastavenia administrátora.',
        'category': 'admin_scripts',
        'priority': 'recommended',
        'tags': ['recommended', 'sensitive'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/usr/local/bin',
        'name': 'Lokálne binárky',
        'description': 'Ručne pridané nástroje a skripty.',
        'category': 'admin_scripts',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/usr/local/sbin',
        'name': 'Lokálne admin skripty',
        'description': 'Admin skripty vrátane Proxmox backup orchestrátorov.',
        'category': 'admin_scripts',
        'priority': 'recommended',
        'tags': ['recommended'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/auto.master',
        'name': 'AutoFS master mapa',
        'description': 'Hlavná autofs konfigurácia pre on-demand NAS mounty.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/auto.master.d',
        'name': 'AutoFS master.d',
        'description': 'Dodatočné autofs master mapy.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/auto.nfs',
        'name': 'AutoFS NFS mapa',
        'description': 'QNAP/WD NFS mapy, napríklad qnap-storage a wd-storage.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/systemd/system/pve-backup-*.service',
        'name': 'PVE backup services',
        'description': 'Systemd služby pre QNAP/WD vzdump orchestráciu.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'systemd', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/etc/systemd/system/pve-backup-*.timer',
        'name': 'PVE backup timery',
        'description': 'Systemd timery pre QNAP/WD vzdump orchestráciu.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'systemd', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/usr/local/sbin/pve_vzdump_enable_run_disable.sh',
        'name': 'Vzdump enable/run/disable skript',
        'description': 'Orchestrátor, ktorý zapína storage, spúšťa vzdump a expirova autofs mount.',
        'category': 'autofs_qnap_wd',
        'priority': 'recommended',
        'tags': ['recommended', 'autofs', 'qnap/wd'],
        'critical': False,
        'selected': True
    },
    {
        'path': '/opt',
        'name': 'Voliteľný /opt',
        'description': 'Vlastné projekty a ručné inštalácie. Môže byť veľké.',
        'category': 'optional_large',
        'priority': 'optional',
        'tags': ['optional', 'large'],
        'critical': False,
        'selected': False
    },
    {
        'path': '/home',
        'name': 'Domovské adresáre',
        'description': 'Používateľské dáta a nastavenia, ak na hoste existujú.',
        'category': 'optional_large',
        'priority': 'optional',
        'tags': ['optional', 'sensitive'],
        'critical': False,
        'selected': False
    },
    {
        'path': '/var/lib/vz/template',
        'name': 'ISO a šablóny',
        'description': 'ISO obrazy a šablóny pre VM/CT. Zvyčajne veľké.',
        'category': 'optional_large',
        'priority': 'optional',
        'tags': ['optional', 'large'],
        'critical': False,
        'selected': False
    }
]

def default_config():
    """Predvolená konfigurácia aplikácie."""
    return {
        'config_version': CONFIG_VERSION,
        'ftp_config': {'host': '', 'username': '', 'password': '', 'port': 21, 'remote_dir': ''},
        'source_config': copy_source_config(DEFAULT_SOURCE_CONFIG),
        'backup_files': [item.copy() for item in DEFAULT_BACKUP_FILES],
        'auto_backup_files': [item.copy() for item in DEFAULT_BACKUP_FILES],
        'backup_categories': BACKUP_CATEGORIES,
        'max_backup_count': DEFAULT_MAX_BACKUP_COUNT,
        'auto_backup_enabled': False,
        'auto_backup_frequency': 'monthly',
        'auto_backup_day': 6,
        'auto_backup_hour': 2,
        'auto_backup_minute': 0
    }

def copy_source_config(source_config):
    """Bezpečná kópia nested source configu bez zdieľania referencií."""
    return json.loads(json.dumps(source_config))

def normalize_port(value, default):
    """Normalizácia portu z UI/JSON vstupu."""
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
    except (TypeError, ValueError):
        pass
    return default

def sanitize_max_backup_count(value):
    """Normalizácia spoločného retenčného limitu lokálnych aj FTP záloh."""
    try:
        count = int(value)
        if count >= 1:
            return min(count, 1000)
    except (TypeError, ValueError):
        pass
    return DEFAULT_MAX_BACKUP_COUNT

def sanitize_ftp_config(ftp_config):
    """Doplnenie a normalizácia FTP konfigurácie."""
    ftp_config = ftp_config if isinstance(ftp_config, dict) else {}
    return {
        'host': str(ftp_config.get('host', '')).strip(),
        'username': str(ftp_config.get('username', '')).strip(),
        'password': str(ftp_config.get('password', '')),
        'port': normalize_port(ftp_config.get('port', 21), 21),
        'remote_dir': str(ftp_config.get('remote_dir', '')).strip(),
    }

def sanitize_source_config(source_config):
    """Doplnenie a normalizácia zdroja zálohy."""
    source_config = source_config if isinstance(source_config, dict) else {}
    mode = source_config.get('mode') or 'remote_ssh'
    if mode not in ('remote_ssh', 'local'):
        mode = 'remote_ssh'

    ssh_config = source_config.get('ssh') if isinstance(source_config.get('ssh'), dict) else {}
    return {
        'mode': mode,
        'ssh': {
            'host': str(ssh_config.get('host', '')).strip(),
            'port': normalize_port(ssh_config.get('port', 22), 22),
            'username': str(ssh_config.get('username', 'root')).strip() or 'root',
            'password': str(ssh_config.get('password', '')),
        }
    }

def normalize_config_path(path):
    """Normalizácia ciest pri migrácii runtime konfigurácie."""
    if not path:
        return path
    normalized = path.rstrip('/') if path != '/' else path
    return MIGRATION_PATH_ALIASES.get(normalized, normalized)

def migrate_backup_item(item):
    """Doplnenie nových polí pre staršie backup_config.json položky."""
    path = item.get('path', '')
    normalized_path = normalize_config_path(path)
    default_by_path = {normalize_config_path(default['path']): default for default in DEFAULT_BACKUP_FILES}
    migrated = default_by_path.get(normalized_path, {}).copy()
    migrated.update(item)
    if migrated:
        migrated['path'] = normalized_path
    migrated.setdefault('name', path or 'Neznáma položka')
    migrated.setdefault('description', 'Vlastná alebo staršia položka konfigurácie')
    migrated.setdefault('category', 'system_config')
    migrated.setdefault('priority', 'optional')
    migrated.setdefault('tags', ['optional'])
    migrated.setdefault('critical', migrated.get('priority') == 'critical')
    migrated.setdefault('selected', True)
    return migrated

def migrate_backup_items(existing_items):
    """Migrácia zoznamu backup položiek pri zachovaní existujúceho výberu."""
    if not isinstance(existing_items, list):
        existing_items = []

    existing_by_path = {
        normalize_config_path(item.get('path')): item
        for item in existing_items
        if isinstance(item, dict) and item.get('path')
    }

    migrated_items = []
    used_paths = set()
    for default_item in DEFAULT_BACKUP_FILES:
        item = default_item.copy()
        normalized_default_path = normalize_config_path(item['path'])
        existing = existing_by_path.get(normalized_default_path)
        if existing:
            item['selected'] = bool(existing.get('selected', item['selected']))
        migrated_items.append(item)
        used_paths.add(normalized_default_path)

    for item in existing_items:
        if not isinstance(item, dict):
            continue
        normalized_path = normalize_config_path(item.get('path'))
        if normalized_path in RETIRED_BACKUP_PATHS:
            continue
        if normalized_path not in used_paths:
            migrated_items.append(migrate_backup_item(item))

    return migrated_items

def migrate_config(config):
    """Migrácia starého runtime JSON formátu na aktuálny model."""
    defaults = default_config()
    if not isinstance(config, dict):
        return defaults

    migrated_items = migrate_backup_items(config.get('backup_files'))
    if isinstance(config.get('auto_backup_files'), list):
        migrated_auto_items = migrate_backup_items(config.get('auto_backup_files'))
    else:
        migrated_auto_items = [item.copy() for item in migrated_items]

    migrated = defaults
    migrated['ftp_config'] = sanitize_ftp_config(config.get('ftp_config', defaults['ftp_config']))
    migrated['source_config'] = sanitize_source_config(config.get('source_config', defaults['source_config']))
    migrated['backup_files'] = migrated_items
    migrated['auto_backup_files'] = migrated_auto_items
    migrated['max_backup_count'] = sanitize_max_backup_count(config.get('max_backup_count', defaults['max_backup_count']))
    migrated['auto_backup_enabled'] = bool(config.get('auto_backup_enabled', False))
    migrated['auto_backup_frequency'] = config.get('auto_backup_frequency', 'monthly')
    migrated['auto_backup_day'] = int(config.get('auto_backup_day', 6))
    migrated['auto_backup_hour'] = int(config.get('auto_backup_hour', 2))
    migrated['auto_backup_minute'] = int(config.get('auto_backup_minute', 0))
    return migrated

def load_config():
    """Načítanie konfigurácie z JSON súboru"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return migrate_config(json.load(f))
    return default_config()

def save_config(config):
    """Uloženie konfigurácie do JSON súboru"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_FILE, 0o600)

def load_backup_history():
    """Načítanie histórie záloh"""
    if os.path.exists(BACKUP_HISTORY_FILE):
        with open(BACKUP_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_backup_history(history):
    """Uloženie histórie záloh"""
    with open(BACKUP_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.chmod(BACKUP_HISTORY_FILE, 0o600)

def ensure_backup_storage_dir():
    """Vytvorí lokálny adresár pre archívy v LXC a nastaví konzervatívne práva."""
    os.makedirs(BACKUP_STORAGE_DIR, exist_ok=True)
    os.chmod(BACKUP_STORAGE_DIR, 0o700)
    return os.path.abspath(BACKUP_STORAGE_DIR)

def effective_archive_excludes(base_excludes=None):
    """Cesty vylúčené z archívu vrátane lokálneho adresára vlastných záloh."""
    excludes = list(ARCHIVE_EXCLUDE_PATHS if base_excludes is None else base_excludes)
    backup_dir = os.path.realpath(os.path.abspath(BACKUP_STORAGE_DIR))
    if backup_dir not in excludes:
        excludes.append(backup_dir)
    return excludes

def default_ssh_client_factory():
    """Vytvorí Paramiko klienta až v momente, keď je SSH naozaj potrebné."""
    try:
        import paramiko
    except ImportError:
        raise RuntimeError('Paramiko nie je nainštalované. Spusti pip install -r requirements.txt.')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client

SSH_CLIENT_FACTORY = default_ssh_client_factory

def ftp_cwd_to_target(ftp, remote_dir):
    """Prepne FTP session do cieľového adresára, ak je nastavený."""
    remote_dir = str(remote_dir or '').strip()
    if remote_dir:
        ftp.cwd(remote_dir)

def test_ftp_connection(host, username, password, port=21, remote_dir='', write_test=True):
    """Test FTP pripojenia vrátane voliteľného testovacieho uploadu."""
    try:
        ftp = ftplib.FTP(timeout=30)
        ftp.connect(host, port)
        ftp.login(username, password)
        ftp_cwd_to_target(ftp, remote_dir)
        current_dir = ftp.pwd()
        if write_test:
            test_filename = f"proxmox_backup_test_{int(time.time())}.tmp"
            with tempfile.TemporaryFile() as test_file:
                test_file.write(b"proxmox-backup ftp write test\n")
                test_file.seek(0)
                ftp.storbinary(f"STOR {test_filename}", test_file)
            try:
                ftp.delete(test_filename)
            except Exception:
                pass
        ftp.quit()
        return True, f"Pripojenie a testovací upload úspešné ({current_dir})"
    except Exception as e:
        return False, f"Chyba pripojenia: {str(e)}"

def normalize_path(path):
    """Bezpečná normalizácia absolútnej cesty."""
    return os.path.normpath(os.path.abspath(path))

def path_is_under(path, parent):
    """True, ak path je parent alebo jeho potomok."""
    path = normalize_path(path)
    parent = os.path.normpath(parent)
    return path == parent or path.startswith(parent + os.sep)

def is_excluded_path(path, base_excludes=None):
    """Kontrola ciest, ktoré sa nikdy nemajú dostať do archívu."""
    normalized = normalize_path(path)
    excludes = ARCHIVE_EXCLUDE_PATHS if base_excludes is None else base_excludes
    for excluded in excludes:
        if path_is_under(normalized, excluded):
            return True

    for pattern in ARCHIVE_EXCLUDE_GLOBS:
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False

def tar_filter(tarinfo, base_excludes=None):
    """Filter pre rekurzívne tar.add volania."""
    if tarinfo.name == 'backup-info' or tarinfo.name.startswith('backup-info/'):
        return tarinfo

    archive_path = '/' + tarinfo.name.lstrip('/')
    if is_excluded_path(archive_path, effective_archive_excludes(base_excludes)):
        return None
    return tarinfo

def write_text_file(path, content):
    """Zapíše textový súbor s UTF-8 obsahom."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

def build_restore_readme(selected_files):
    """Restore checklist pridaný do archívu."""
    selected_paths = '\n'.join(f"- {item['path']}" for item in selected_files)
    return f"""# Proxmox Host Backup - Restore Checklist

Vygenerované: {datetime.now().isoformat(timespec='seconds')}

Táto záloha obsahuje konfiguráciu Proxmox hosta, nie VM/CT disky. Disky VM/LXC obnovuj
samostatne z Proxmox vzdump/PBS/NAS záloh alebo z pôvodného storage.
Pred každým prepisom najprv rozbaľ archív do dočasného adresára a skontroluj obsah.

## Vybrané cesty

{selected_paths}

## Keď zomrel celý server

1. Nainštaluj čistý Proxmox VE, ideálne rovnakú alebo kompatibilnú major verziu ako pôvodný host.
2. Počas inštalácie použi pôvodný hostname, ak ho chceš obnoviť bez presunu node configov
   v `/etc/pve/nodes/<hostname>/`. Ak zmeníš hostname, VM/LXC configy bude treba prispôsobiť.
3. Ako prvé spojazdni minimálnu sieť: správny management IP, gateway, DNS a bridge. Pred prepisom
   `/etc/network` porovnaj názvy sieťových kariet cez `ip link`, lebo nový hardvér môže mať iné názvy.
4. Získaj archív z FTP/NAS/lokálneho disku a rozbaľ ho iba do dočasného adresára, napríklad:
   `mkdir -p /root/pve-restore-review && tar -xzf proxmox_backup_*.tar.gz -C /root/pve-restore-review`
5. Skontroluj `backup-info/` výstupy: `pveversion-v.txt`, `pvesm-config.txt`, `pve-backup-jobs.json`,
   `network-interfaces.txt`, `ip-addr.txt`, `lsblk-f.txt`, `findmnt.txt` a storage konfiguráciu.
6. Obnov alebo znovu nainštaluj LXC s Proxmox Backup Managerom. Ak nemáš zálohu LXC, môžeš aplikáciu
   nainštalovať nanovo a archív obnovovať ručne alebo ho vložiť do lokálneho `backups/` adresára spolu
   s príslušnou históriou, aby ho aplikácia videla v restore UI.
7. Pred automatickou obnovou cez aplikáciu nastav SSH prístup na nový Proxmox host a otestuj pripojenie.
   Aplikácia pred prepisom vytvorí rollback kópiu existujúcich cieľových ciest v `/root`.

## Poradie obnovy konfigurácie

1. Najprv rieš sieť a identitu hosta: `/etc/hostname`, `/etc/hosts`, `/etc/network`,
   `/etc/resolv.conf` a podľa potreby `/etc/fstab`.
2. Proxmox konfiguráciu obnovuj hlavne z `/etc/pve` a `/var/lib/pve-cluster/config.db`.
   Úplnú obnovu `config.db` rob iba na novom hoste, keď nič nebeží: zastav `pve-cluster`, nahraď
   `/var/lib/pve-cluster/config.db`, nastav práva `0600`, uprav hostname/hosts podľa pôvodného hosta
   a reštartuj server.
3. VM/LXC definície sú v `/etc/pve/nodes/<node>/qemu-server/` a `/etc/pve/nodes/<node>/lxc/`.
   Poznámky z Proxmox GUI sú súčasťou týchto configov ako `description`.
4. Obnov storage nastavenia až po overení, že nové disky, ZFS pooly, mounty, NFS/CIFS exporty a názvy
   storage sedia s pôvodnou konfiguráciou.
5. Lokálne účty a mapovania (`/etc/passwd`, `/etc/group`, `/etc/shadow`, `/etc/subuid`, `/etc/subgid`)
   obnovuj opatrne, najmä ak už na novom hoste vznikli nové účty.
6. SSH konfiguráciu (`/etc/ssh`) obnov len vtedy, keď chceš zachovať starú SSH identitu hosta a kľúče.

## AUTO.FS / QNAP / WD

1. Nainštaluj potrebné balíky:
   `apt update && apt install -y autofs nfs-common`
2. Obnov `/etc/auto.master`, `/etc/auto.master.d/`, `/etc/auto.nfs`.
3. Obnov `/usr/local/sbin/pve_vzdump_enable_run_disable.sh` a nastav:
   `chmod 0755 /usr/local/sbin/pve_vzdump_enable_run_disable.sh`
4. Obnov `pve-backup-*.service` a `pve-backup-*.timer` do `/etc/systemd/system/`.
5. Ručne skontroluj `NODE`, `JOB_ID`, IP adresy QNAP/WD a NFS exporty.
6. Spusti:
    `systemctl daemon-reload`
    `systemctl enable --now autofs`
    `systemctl list-timers | grep pve-backup`
7. Otestuj autofs mount cez `ls -la /autofs/<storage>` a až potom spúšťaj vzdump service.

## Kontroly po obnove

1. Over sieť cez konzolu aj SSH, až potom reštartuj ďalšie služby.
2. Skontroluj `pvesm status`, `pvesm config`, `qm list`, `pct list` a Proxmox GUI.
3. Over, že VM/CT disky existujú na storage, ktoré ukazujú configy v `/etc/pve`.
4. Spusti iba tie VM/LXC, pri ktorých sedí storage, bridge a mount pointy.

## Dôležité bezpečnostné poznámky

- Archív môže obsahovať heslá, tokeny a SSH kľúče z `/root`, `/etc` alebo Proxmox konfigurácie.
- Ukladaj ho iba na dôveryhodný FTP/NAS a zváž šifrovanie transportu alebo archívu.
- Nikdy nerozbaľuj celý archív priamo do `/`.
- Cesty `/mnt`, `/media`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, `/var/tmp`, `/var/cache`, `/var/log` sú zámerne vynechané.
"""

def run_info_command(command):
    """Spustí informačný príkaz a vráti textový report bez zhadzovania zálohy."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False
        )
        return (
            f"$ {' '.join(command)}\n"
            f"exit_code={result.returncode}\n\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )
    except FileNotFoundError as exc:
        return f"$ {' '.join(command)}\ncommand_not_found={exc}\n"
    except subprocess.TimeoutExpired as exc:
        return f"$ {' '.join(command)}\ntimeout_after_seconds={exc.timeout}\n"
    except Exception as exc:
        return f"$ {' '.join(command)}\nerror={exc}\n"

def generate_backup_info(info_dir, selected_files):
    """Vygeneruje obnovovací checklist a diagnostické info súbory."""
    generated = []
    readme_path = os.path.join(info_dir, 'README-RESTORE.txt')
    write_text_file(readme_path, build_restore_readme(selected_files))
    generated.append('README-RESTORE.txt')

    for filename, command in INFO_COMMANDS:
        output_path = os.path.join(info_dir, filename)
        write_text_file(output_path, run_info_command(command))
        generated.append(filename)

    return generated

def expand_backup_path(path):
    """Rozbalí wildcard položky a zachová presný report chýbajúcich ciest."""
    if glob.has_magic(path):
        return sorted(glob.glob(path))
    return [path] if os.path.exists(path) else []

def add_path_to_archive(tar, source_path, report, base_excludes=None):
    """Pridá jednu cestu do archívu alebo ju zapíše do skipped reportu."""
    normalized = normalize_path(source_path)
    excludes = effective_archive_excludes(base_excludes)
    if is_excluded_path(normalized, excludes):
        report['skipped'].append({'path': source_path, 'reason': 'excluded'})
        return

    arcname = os.path.relpath(normalized, '/')
    try:
        tar.add(normalized, arcname=arcname, recursive=True, filter=lambda tarinfo: tar_filter(tarinfo, excludes))
        report['included'].append({'path': normalized, 'arcname': arcname})
    except (OSError, tarfile.TarError) as exc:
        report['skipped'].append({'path': source_path, 'reason': f'error: {exc}'})

def create_backup_archive(selected_files, backup_filename, include_info=True, base_excludes=None):
    """Vytvorenie archívu so zálohou a reportom zahrnutých/chýbajúcich položiek."""
    excludes = effective_archive_excludes(base_excludes)
    report = {
        'included': [],
        'skipped': [],
        'generated_info': [],
        'excluded_paths': excludes,
    }

    with tempfile.TemporaryDirectory(prefix='pve-host-backup-info-') as info_dir:
        if include_info:
            report['generated_info'] = generate_backup_info(info_dir, selected_files)

        with tarfile.open(backup_filename, 'w:gz') as tar:
            for file_info in selected_files:
                file_path = file_info['path']
                matches = expand_backup_path(file_path)
                if not matches:
                    report['skipped'].append({'path': file_path, 'reason': 'missing'})
                    continue

                for matched_path in matches:
                    if is_excluded_path(matched_path, excludes):
                        report['skipped'].append({'path': matched_path, 'reason': 'excluded'})
                        continue
                    add_path_to_archive(tar, matched_path, report, excludes)

            if include_info:
                tar.add(info_dir, arcname='backup-info', recursive=True)

    return report

class LocalBackupSource:
    """Zdroj zálohy pre prípad, keď appka beží priamo na Proxmox hoste."""

    source_type = 'local'

    def create_archive(self, selected_files, backup_filename):
        report = create_backup_archive(selected_files, backup_filename)
        report['source'] = self.source_type
        return report

def decode_stream_value(value):
    """Dekóduje stdout/stderr z SSH alebo lokálneho mocku."""
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value or '')

def shell_join(command):
    """Bezpečné zloženie shell príkazu zo zoznamu argumentov."""
    return ' '.join(shlex.quote(str(part)) for part in command)

def normalize_remote_path(path):
    """Normalizácia absolútnej POSIX cesty na vzdialenom hoste."""
    if not path:
        return '/'
    path = str(path)
    if not path.startswith('/'):
        path = '/' + path
    return posixpath.normpath(path)

def remote_path_is_under(path, parent):
    """True, ak je remote path parent alebo jeho potomok."""
    path = normalize_remote_path(path)
    parent = normalize_remote_path(parent)
    return path == parent or path.startswith(parent.rstrip('/') + '/')

def is_remote_excluded_path(path):
    """Kontrola vzdialených ciest, ktoré nikdy nepatria do archívu."""
    normalized = normalize_remote_path(path)
    for excluded in ARCHIVE_EXCLUDE_PATHS:
        if remote_path_is_under(normalized, excluded):
            return True

    for pattern in ARCHIVE_EXCLUDE_GLOBS:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(normalized.lstrip('/'), pattern.lstrip('/')):
            return True
    return False

def remote_tar_exclude_patterns():
    """GNU tar exclude vzory pre remote stream s relatívnymi aj absolútnymi cestami."""
    patterns = []
    for excluded in ARCHIVE_EXCLUDE_PATHS:
        clean = normalize_remote_path(excluded).lstrip('/')
        patterns.extend([clean, f'{clean}/*', f'/{clean}', f'/{clean}/*'])
    patterns.extend(ARCHIVE_EXCLUDE_GLOBS)
    patterns.extend(pattern.lstrip('/') for pattern in ARCHIVE_EXCLUDE_GLOBS)
    return sorted(set(patterns))

class RemoteSshBackupSource:
    """Zdroj zálohy pre samostatné LXC, ktoré číta Proxmox host cez SSH."""

    source_type = 'remote_ssh'

    def __init__(self, source_config, ssh_client_factory=None):
        self.source_config = sanitize_source_config(source_config)
        self.ssh_config = self.source_config['ssh']
        self.ssh_client_factory = ssh_client_factory or SSH_CLIENT_FACTORY

    def validate(self):
        """Overí minimálne SSH údaje pred spustením zálohy."""
        missing = []
        if not self.ssh_config.get('host'):
            missing.append('host')
        if not self.ssh_config.get('username'):
            missing.append('username')
        if not self.ssh_config.get('password'):
            missing.append('password')
        if missing:
            raise ValueError(f"SSH konfigurácia chýba: {', '.join(missing)}")

    def connect(self):
        """Pripojí sa na Proxmox cez SSH."""
        self.validate()
        client = self.ssh_client_factory()
        client.connect(
            hostname=self.ssh_config['host'],
            port=self.ssh_config['port'],
            username=self.ssh_config['username'],
            password=self.ssh_config['password'],
            timeout=20,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    def run_command(self, client, command, timeout=30):
        """Spustí remote command a vráti exit code, stdout, stderr."""
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdout_data = stdout.read()
        stderr_data = stderr.read()
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, decode_stream_value(stdout_data), decode_stream_value(stderr_data)

    def write_remote_file(self, client, remote_path, content):
        """Zapíše malý textový súbor do remote backup-info adresára."""
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_path, 'w') as remote_file:
                remote_file.write(content)
        finally:
            sftp.close()

    def generate_remote_backup_info(self, client, remote_info_dir, selected_files):
        """Vygeneruje backup-info priamo na Proxmox hoste, aby príkazy bežali tam."""
        generated = []
        self.write_remote_file(
            client,
            posixpath.join(remote_info_dir, 'README-RESTORE.txt'),
            build_restore_readme(selected_files),
        )
        generated.append('README-RESTORE.txt')

        for filename, command in INFO_COMMANDS:
            command_str = shell_join(command)
            try:
                exit_code, stdout, stderr = self.run_command(client, command_str, timeout=30)
                content = (
                    f"$ {command_str}\n"
                    f"exit_code={exit_code}\n\n"
                    f"--- stdout ---\n{stdout}\n"
                    f"--- stderr ---\n{stderr}\n"
                )
            except Exception as exc:
                content = f"$ {command_str}\nerror={exc}\n"

            self.write_remote_file(client, posixpath.join(remote_info_dir, filename), content)
            generated.append(filename)

        return generated

    def expand_path(self, client, path):
        """Rozbalí remote wildcard alebo overí existenciu jednej remote cesty."""
        normalized = normalize_remote_path(path)
        if glob.has_magic(normalized):
            script = (
                "import glob, json; "
                f"print(json.dumps(sorted(glob.glob({json.dumps(normalized)}))))"
            )
            exit_code, stdout, _stderr = self.run_command(client, 'python3 -c ' + shlex.quote(script), timeout=30)
            if exit_code != 0:
                return []
            try:
                matches = json.loads(stdout)
            except json.JSONDecodeError:
                return []
            return [normalize_remote_path(match) for match in matches]

        exit_code, _stdout, _stderr = self.run_command(client, f'test -e {shlex.quote(normalized)}', timeout=10)
        return [normalized] if exit_code == 0 else []

    def build_tar_command(self, archive_names, remote_workdir):
        """Zloží remote tar príkaz, ktorý streamuje gzip archív na stdout."""
        command = ['tar', '--warning=no-file-changed', '--ignore-failed-read', '-czf', '-']
        for pattern in remote_tar_exclude_patterns():
            command.extend(['--exclude', pattern])
        command.extend(['-C', '/'])
        command.extend(archive_names)
        command.extend(['-C', remote_workdir, 'backup-info'])
        return shell_join(command)

    def stream_tar_to_local(self, client, tar_command, backup_filename):
        """Streamuje remote tar stdout do lokálneho súboru v LXC."""
        stdin, stdout, stderr = client.exec_command(tar_command, timeout=3600)
        with open(backup_filename, 'wb') as output_file:
            while True:
                chunk = stdout.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)

        stderr_text = decode_stream_value(stderr.read())
        exit_code = stdout.channel.recv_exit_status()
        if exit_code not in (0, 1):
            raise RuntimeError(f"Remote tar zlyhal s exit code {exit_code}: {stderr_text.strip()}")
        if not os.path.exists(backup_filename) or os.path.getsize(backup_filename) == 0:
            raise RuntimeError('Remote tar nevytvoril žiadne dáta')
        return exit_code, stderr_text

    def create_archive(self, selected_files, backup_filename):
        """Vytvorí lokálny archív v LXC zo vzdialeného Proxmox hosta."""
        report = {
            'source': self.source_type,
            'remote_host': self.ssh_config.get('host'),
            'included': [],
            'skipped': [],
            'generated_info': [],
            'excluded_paths': ARCHIVE_EXCLUDE_PATHS,
            'warnings': [],
        }

        client = self.connect()
        remote_workdir = None
        try:
            exit_code, stdout, stderr = self.run_command(
                client,
                'mktemp -d /tmp/pve-host-backup-info.XXXXXX',
                timeout=10,
            )
            if exit_code != 0:
                raise RuntimeError(f"Remote mktemp zlyhal: {stderr.strip()}")
            remote_workdir = stdout.strip()
            remote_info_dir = posixpath.join(remote_workdir, 'backup-info')
            exit_code, _stdout, stderr = self.run_command(
                client,
                f'mkdir -p {shlex.quote(remote_info_dir)} && chmod 700 {shlex.quote(remote_workdir)}',
                timeout=10,
            )
            if exit_code != 0:
                raise RuntimeError(f"Remote backup-info adresár sa nedá vytvoriť: {stderr.strip()}")

            report['generated_info'] = self.generate_remote_backup_info(client, remote_info_dir, selected_files)

            archive_names = []
            for file_info in selected_files:
                file_path = file_info['path']
                matches = self.expand_path(client, file_path)
                if not matches:
                    report['skipped'].append({'path': file_path, 'reason': 'missing'})
                    continue

                for matched_path in matches:
                    normalized = normalize_remote_path(matched_path)
                    if is_remote_excluded_path(normalized):
                        report['skipped'].append({'path': matched_path, 'reason': 'excluded'})
                        continue
                    arcname = normalized.lstrip('/')
                    archive_names.append(arcname)
                    report['included'].append({'path': normalized, 'arcname': arcname})

            tar_command = self.build_tar_command(archive_names, remote_workdir)
            tar_exit_code, tar_stderr = self.stream_tar_to_local(client, tar_command, backup_filename)
            report['remote_tar_exit_code'] = tar_exit_code
            if tar_exit_code == 1:
                report['warnings'].append('Remote tar skončil s exit code 1; archív existuje, ale skontroluj stderr.')
            if tar_stderr.strip():
                report['remote_tar_stderr'] = tar_stderr.strip()[-4000:]

            return report
        finally:
            if remote_workdir:
                safe_workdir = remote_workdir.strip()
                if safe_workdir.startswith('/tmp/pve-host-backup-info.'):
                    self.run_command(client, f'rm -rf {shlex.quote(safe_workdir)}', timeout=10)
            client.close()

def build_backup_source(source_config):
    """Factory pre lokálny alebo remote SSH zdroj zálohy."""
    source_config = sanitize_source_config(source_config)
    if source_config['mode'] == 'local':
        return LocalBackupSource()
    return RemoteSshBackupSource(source_config)

def test_ssh_connection(source_config):
    """Overenie SSH pripojenia na Proxmox host."""
    source = RemoteSshBackupSource(source_config)
    client = None
    try:
        client = source.connect()
        command = 'hostname && pveversion -v'
        exit_code, stdout, stderr = source.run_command(client, command, timeout=30)
        if exit_code == 0:
            first_line = stdout.strip().splitlines()[0] if stdout.strip() else source.ssh_config['host']
            return True, f"SSH pripojenie úspešné: {first_line}"
        return False, f"SSH funguje, ale Proxmox príkaz zlyhal: {stderr.strip() or stdout.strip()}"
    except Exception as exc:
        return False, f"Chyba SSH pripojenia: {exc}"
    finally:
        if client:
            client.close()

def upload_to_ftp(local_file, ftp_config):
    """Nahratie súboru na FTP server"""
    try:
        ftp = ftplib.FTP(timeout=60)
        ftp.connect(ftp_config['host'], ftp_config['port'])
        ftp.login(ftp_config['username'], ftp_config['password'])
        ftp_cwd_to_target(ftp, ftp_config.get('remote_dir', ''))
        
        with open(local_file, 'rb') as f:
            ftp.storbinary(f'STOR {os.path.basename(local_file)}', f)
        
        ftp.quit()
        return True, "Súbor úspešne nahraný na FTP server"
    except Exception as e:
        return False, f"Chyba pri nahrávaní na FTP: {str(e)}"

def ftp_config_complete(ftp_config):
    ftp_config = sanitize_ftp_config(ftp_config)
    return bool(ftp_config.get('host') and ftp_config.get('username') and ftp_config.get('password'))

def delete_from_ftp(filename, ftp_config):
    """Zmaže archív z FTP; chýbajúci súbor berie ako hotový stav."""
    if not filename:
        return False, 'Záznam nemá názov súboru pre FTP'
    ftp_config = sanitize_ftp_config(ftp_config)
    if not ftp_config_complete(ftp_config):
        return False, 'FTP konfigurácia chýba, vzdialený súbor sa nedá zmazať'

    ftp = None
    try:
        ftp = ftplib.FTP(timeout=30)
        ftp.connect(ftp_config['host'], ftp_config['port'])
        ftp.login(ftp_config['username'], ftp_config['password'])
        ftp_cwd_to_target(ftp, ftp_config.get('remote_dir', ''))
        try:
            ftp.delete(filename)
            return True, 'Súbor zmazaný z FTP'
        except ftplib.error_perm as exc:
            if str(exc).startswith('550'):
                return True, 'Súbor na FTP už neexistoval'
            raise
    except Exception as exc:
        return False, f'Chyba pri mazaní z FTP: {exc}'
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass

def ftp_file_exists(filename, ftp_config):
    """Best-effort kontrola existencie súboru na FTP."""
    if not filename or not ftp_config_complete(ftp_config):
        return None

    ftp = None
    try:
        ftp = ftplib.FTP(timeout=10)
        ftp_config = sanitize_ftp_config(ftp_config)
        ftp.connect(ftp_config['host'], ftp_config['port'])
        ftp.login(ftp_config['username'], ftp_config['password'])
        ftp_cwd_to_target(ftp, ftp_config.get('remote_dir', ''))
        try:
            ftp.size(filename)
            return True
        except Exception:
            try:
                names = ftp.nlst(filename)
                return bool(names)
            except ftplib.error_perm as exc:
                if str(exc).startswith('550'):
                    return False
                return None
            except Exception:
                return None
    except Exception:
        return None
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass

def get_file_size(filepath):
    """Získanie veľkosti súboru v ľudsky čitateľnom formáte"""
    if os.path.exists(filepath):
        size = os.path.getsize(filepath)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
    return "0 B"

def resolve_selected_file_objects(selected_paths, configured_files):
    """Premení zoznam path stringov na plné backup položky z konfigurácie."""
    configured_by_path = {f['path']: f for f in configured_files}
    selected_file_objects = []
    for selected_path in selected_paths:
        if selected_path in configured_by_path:
            selected_file_objects.append(configured_by_path[selected_path])
        else:
            selected_file_objects.append(migrate_backup_item({'path': selected_path, 'selected': True}))
    return selected_file_objects

def build_backup_filename(source_config):
    """Názov archívu s timestampom a krátkym označením zdroja."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    source_config = sanitize_source_config(source_config)
    if source_config['mode'] == 'remote_ssh' and source_config['ssh'].get('host'):
        host_part = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in source_config['ssh']['host'])
        return f"proxmox_backup_{host_part}_{timestamp}.tar.gz"
    return f"proxmox_backup_local_{timestamp}.tar.gz"

def validate_ftp_for_backup(ftp_config):
    """FTP je povinný cieľ pre aktuálny release."""
    missing = []
    if not ftp_config.get('host'):
        missing.append('host')
    if not ftp_config.get('username'):
        missing.append('username')
    if not ftp_config.get('password'):
        missing.append('password')
    if missing:
        raise ValueError(f"FTP konfigurácia chýba: {', '.join(missing)}")

def run_backup_job(selected_paths, ftp_config, source_config, configured_files, config=None, backup_mode='manual'):
    """Spoločný backup flow pre API aj starší formulárový route handler."""
    if not selected_paths:
        raise ValueError('No files selected')

    config = config or load_config()
    ftp_config = sanitize_ftp_config(ftp_config)
    source_config = sanitize_source_config(source_config)
    validate_ftp_for_backup(ftp_config)

    selected_file_objects = resolve_selected_file_objects(selected_paths, configured_files)
    backup_dir = ensure_backup_storage_dir()
    backup_filename = build_backup_filename(source_config)
    local_path = os.path.join(backup_dir, backup_filename)

    source = build_backup_source(source_config)
    report = source.create_archive(selected_file_objects, local_path)
    os.chmod(local_path, 0o600)

    ftp_success, ftp_message = upload_to_ftp(local_path, ftp_config)
    now = datetime.now()
    history_entry = {
        'id': str(time.time_ns()),
        'filename': backup_filename,
        'timestamp': now.isoformat(),
        'date': now.strftime('%d.%m.%Y %H:%M'),
        'files': selected_paths,
        'backup_mode': backup_mode,
        'source_mode': source_config['mode'],
        'source_host': source_config['ssh'].get('host') if source_config['mode'] == 'remote_ssh' else 'local',
        'local_path': local_path,
        'ftp_status': 'success' if ftp_success else 'failed',
        'ftp_message': ftp_message,
        'status': 'success' if ftp_success else 'ftp_failed',
        'size': get_file_size(local_path),
        'included_count': len(report['included']),
        'skipped_count': len(report['skipped']),
        'skipped': report['skipped'],
        'generated_info_count': len(report['generated_info']),
    }

    history = load_backup_history()
    history.append(history_entry)
    save_backup_history(history)

    sync_results = sync_missing_ftp_backups(ftp_config, skip_ids={history_entry['id']})
    retention_result = enforce_backup_retention(config, ftp_config)
    warnings = []
    if not ftp_success:
        warnings.append(ftp_message)
    warnings.extend(result.get('message', '') for result in sync_results if not result.get('success'))
    warnings.extend(retention_result.get('warnings', []))
    warnings = [warning for warning in warnings if warning]
    if warnings:
        annotate_backup_history_entry(history_entry['id'], {'retention_warnings': warnings})

    return {
        'success': True,
        'message': 'Backup created successfully' if ftp_success else 'Backup created locally, FTP upload failed',
        'report': report,
        'filename': backup_filename,
        'local_path': local_path,
        'ftp_status': history_entry['ftp_status'],
        'ftp_message': ftp_message,
        'ftp_sync_results': sync_results,
        'retention_deleted': retention_result.get('deleted', []),
        'retention_warnings': warnings,
        'size': get_file_size(local_path),
    }

def restore_whitelist_items():
    """Cesty, ktoré v1 restore smie aplikovať späť na host."""
    items = []
    for item in DEFAULT_BACKUP_FILES:
        path = item['path']
        if glob.has_magic(path):
            continue
        if is_excluded_path(path):
            continue
        items.append(item)
    return items

def restore_whitelist_paths():
    return {item['path'] for item in restore_whitelist_items()}

def archive_name_for_path(path):
    """Prevedie absolútnu cestu na tar arcname bez úvodného lomítka."""
    return normalize_remote_path(path).lstrip('/')

def tar_name_is_safe(name):
    """Overí, že tar člen nemôže uniknúť zo staging adresára."""
    if not name or name.startswith('/'):
        return False
    normalized = posixpath.normpath(name)
    if normalized in ('', '.') or normalized.startswith('../') or normalized == '..':
        return False
    if '\x00' in name or '\n' in name or '\r' in name:
        return False
    return '..' not in normalized.split('/')

def tar_link_is_safe(member):
    """Povolí systémové linky, ale nie linky do runtime/mount ciest."""
    if not (member.issym() or member.islnk()):
        return True
    linkname = member.linkname or ''
    if not linkname:
        return False
    if '\x00' in linkname or '\n' in linkname or '\r' in linkname:
        return False

    if linkname.startswith('/'):
        return not is_remote_excluded_path(linkname)

    base = posixpath.dirname(member.name)
    normalized_target = posixpath.normpath(posixpath.join(base, linkname))
    if not tar_name_is_safe(normalized_target):
        return False
    return not is_remote_excluded_path('/' + normalized_target.lstrip('/'))

def validate_tar_member(member):
    if not tar_name_is_safe(member.name):
        raise ValueError(f"Nebezpečný názov v archíve: {member.name}")
    if member.ischr() or member.isblk() or member.isfifo():
        raise ValueError(f"Nepodporovaný špeciálny súbor v archíve: {member.name}")
    if not tar_link_is_safe(member):
        raise ValueError(f"Nebezpečný link v archíve: {member.name} -> {member.linkname}")

def member_matches_restore_path(member_name, restore_path):
    arcname = archive_name_for_path(restore_path)
    return member_name == arcname or member_name.startswith(arcname.rstrip('/') + '/')

def restore_archive_members(archive_path, selected_paths=None):
    """Vráti validované tar členy, voliteľne iba pre vybrané restore cesty."""
    selected_paths = selected_paths or []
    members = []
    with tarfile.open(archive_path, 'r:gz') as tar:
        for member in tar.getmembers():
            validate_tar_member(member)
            if not selected_paths or any(member_matches_restore_path(member.name, path) for path in selected_paths):
                members.append(member)
    return members

def preview_restore_archive(archive_path):
    """Zistí, ktoré whitelisted cesty sú dostupné v archíve."""
    members = restore_archive_members(archive_path)
    member_names = [member.name for member in members]
    available = []
    for item in restore_whitelist_items():
        path = item['path']
        matching_names = [name for name in member_names if member_matches_restore_path(name, path)]
        if matching_names:
            available.append({
                'path': path,
                'name': item.get('name', path),
                'description': item.get('description', ''),
                'category': item.get('category', 'system_config'),
                'critical': bool(item.get('critical')),
                'tags': item.get('tags', []),
                'member_count': len(matching_names),
            })
    return available

def restore_member_type(member):
    if member.isdir():
        return 'dir'
    if member.issym():
        return 'symlink'
    if member.islnk():
        return 'hardlink'
    if member.isfile():
        return 'file'
    return 'other'

def restore_archive_member_details(archive_path, restore_path, limit=500):
    """Vráti členy archívu pre jednu restore cestu s limitom pre veľké adresáre."""
    normalized_path = normalize_remote_path(restore_path)
    if normalized_path not in restore_whitelist_paths():
        raise ValueError(f'Cesta nie je povolená pre restore: {restore_path}')
    if glob.has_magic(normalized_path):
        raise ValueError(f'Wildcard cesty nie sú podporované pre restore: {restore_path}')

    members = restore_archive_members(archive_path, [normalized_path])
    details = []
    for member in members[:limit]:
        details.append({
            'name': member.name,
            'type': restore_member_type(member),
            'size': member.size if member.isfile() else 0,
            'linkname': member.linkname if (member.issym() or member.islnk()) else '',
        })
    return {
        'path': normalized_path,
        'total': len(members),
        'limit': limit,
        'truncated': len(members) > limit,
        'members': details,
    }

def restore_archive_all_member_details(archive_path, limit=2000):
    """Vráti spoločný zoznam členov pre všetky obnoviteľné cesty."""
    allowed_paths = [item['path'] for item in restore_whitelist_items()]
    members = restore_archive_members(archive_path, allowed_paths)
    seen = set()
    unique_members = []
    for member in members:
        if member.name in seen:
            continue
        seen.add(member.name)
        unique_members.append(member)

    details = []
    for member in unique_members[:limit]:
        details.append({
            'name': member.name,
            'type': restore_member_type(member),
            'size': member.size if member.isfile() else 0,
            'linkname': member.linkname if (member.issym() or member.islnk()) else '',
        })
    return {
        'total': len(unique_members),
        'limit': limit,
        'truncated': len(unique_members) > limit,
        'members': details,
    }

def resolve_history_archive(backup_id):
    """Nájde archív v histórii a overí, že stále leží v lokálnom backup adresári."""
    history = load_backup_history()
    entry = next((item for item in history if str(item.get('id')) == str(backup_id)), None)
    if not entry:
        raise FileNotFoundError('Archív nie je v histórii záloh')

    archive_path = resolve_backup_entry_local_path(entry)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError('Lokálny archív už neexistuje')
    return entry, archive_path

def resolve_backup_entry_local_path(entry):
    """Bezpečne určí lokálnu cestu archívu z history entry."""
    backup_root = os.path.realpath(os.path.abspath(BACKUP_STORAGE_DIR))
    local_path = entry.get('local_path')
    if local_path:
        archive_path = os.path.realpath(os.path.abspath(local_path))
    elif entry.get('filename'):
        archive_path = os.path.realpath(os.path.abspath(os.path.join(backup_root, entry['filename'])))
    else:
        raise ValueError('Záznam histórie neobsahuje cestu k archívu')

    if not path_is_under(archive_path, backup_root):
        raise ValueError('Archív je mimo lokálneho backup adresára')
    return archive_path

def backup_entry_is_visible(entry, ftp_config=None):
    """História má zobrazovať lokálne dostupné archívy aj pri dočasnom FTP výpadku."""
    try:
        archive_path = resolve_backup_entry_local_path(entry)
    except ValueError:
        return False
    return os.path.isfile(archive_path)

def visible_backup_history(config=None, persist_pruned=True):
    """Vyfiltruje históriu od záznamov bez lokálneho archívu."""
    config = config or load_config()
    history = load_backup_history()
    visible = [entry for entry in history if backup_entry_is_visible(entry)]
    if persist_pruned and len(visible) != len(history):
        save_backup_history(visible)
    return visible

def delete_backup_entry(backup_id, ftp_config):
    """Zmaže lokálny archív, vzdialený FTP súbor a odstráni záznam z histórie."""
    history = load_backup_history()
    entry = next((item for item in history if str(item.get('id')) == str(backup_id)), None)
    if not entry:
        raise FileNotFoundError('Záloha nie je v histórii')

    try:
        archive_path = resolve_backup_entry_local_path(entry)
    except ValueError as exc:
        raise ValueError(str(exc))

    local_deleted = False
    local_message = 'Lokálny archív neexistoval'
    ftp_deleted = None
    ftp_message = 'FTP mazanie nebolo potrebné'
    if entry.get('ftp_status') == 'success':
        ftp_deleted, ftp_message = delete_from_ftp(entry.get('filename'), ftp_config)
        if not ftp_deleted:
            return {
                'success': False,
                'local_deleted': local_deleted,
                'local_message': local_message,
                'ftp_deleted': False,
                'ftp_message': ftp_message,
                'history_removed': False,
            }

    if os.path.exists(archive_path):
        os.remove(archive_path)
        local_deleted = True
        local_message = 'Lokálny archív zmazaný'

    save_backup_history([item for item in history if str(item.get('id')) != str(backup_id)])
    return {
        'success': True,
        'local_deleted': local_deleted,
        'local_message': local_message,
        'ftp_deleted': ftp_deleted,
        'ftp_message': ftp_message,
        'history_removed': True,
    }

def backup_history_sort_key(entry):
    """Stabilné zoradenie histórie od najstaršej zálohy."""
    timestamp = entry.get('timestamp')
    if timestamp:
        try:
            return datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            pass
    try:
        return datetime.fromtimestamp(int(entry.get('id', 0)))
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.min

def annotate_backup_history_entry(backup_id, updates):
    """Doplní metadáta do záznamu, ak ešte nebol odstránený retenciou."""
    history = load_backup_history()
    changed = False
    for entry in history:
        if str(entry.get('id')) == str(backup_id):
            entry.update(updates)
            changed = True
            break
    if changed:
        save_backup_history(history)
    return changed

def sync_missing_ftp_backups(ftp_config, skip_ids=None):
    """Best-effort dohratie lokálnych archívov, ktoré na FTP chýbajú."""
    ftp_config = sanitize_ftp_config(ftp_config)
    if not ftp_config_complete(ftp_config):
        return []

    skip_ids = {str(item) for item in (skip_ids or set())}
    history = load_backup_history()
    results = []
    changed = False

    for entry in sorted(history, key=backup_history_sort_key):
        entry_id = str(entry.get('id'))
        if entry_id in skip_ids:
            continue
        filename = entry.get('filename')
        if not filename:
            continue
        try:
            archive_path = resolve_backup_entry_local_path(entry)
        except ValueError:
            continue
        if not os.path.isfile(archive_path):
            continue

        needs_upload = entry.get('ftp_status') != 'success'
        if not needs_upload:
            exists = ftp_file_exists(filename, ftp_config)
            if exists is False:
                needs_upload = True
            elif exists is None:
                continue
        if not needs_upload:
            continue

        success, message = upload_to_ftp(archive_path, ftp_config)
        results.append({
            'id': entry_id,
            'filename': filename,
            'success': success,
            'message': message,
        })
        entry['ftp_status'] = 'success' if success else 'failed'
        entry['ftp_message'] = 'Dodatočne nahrané na FTP' if success else message
        if success and entry.get('status') == 'ftp_failed':
            entry['status'] = 'success'
        changed = True
        if not success:
            break

    if changed:
        save_backup_history(history)
    return results

def enforce_backup_retention(config, ftp_config):
    """Udrží najviac max_backup_count lokálnych archívov a zmaže ich aj z FTP."""
    max_count = sanitize_max_backup_count((config or {}).get('max_backup_count', DEFAULT_MAX_BACKUP_COUNT))
    history = load_backup_history()
    candidates = []
    for entry in history:
        try:
            archive_path = resolve_backup_entry_local_path(entry)
        except ValueError:
            continue
        if os.path.isfile(archive_path):
            candidates.append(entry)

    overflow = len(candidates) - max_count
    if overflow <= 0:
        return {'deleted': [], 'warnings': []}

    deleted = []
    warnings = []
    for entry in sorted(candidates, key=backup_history_sort_key)[:overflow]:
        result = delete_backup_entry(entry.get('id'), ftp_config)
        if result.get('success'):
            deleted.append({
                'id': entry.get('id'),
                'filename': entry.get('filename'),
                'local_deleted': result.get('local_deleted'),
                'ftp_deleted': result.get('ftp_deleted'),
            })
        else:
            warnings.append(
                f"Retencia nezmazala {entry.get('filename') or entry.get('id')}: "
                f"{result.get('ftp_message') or result.get('local_message') or 'neznáma chyba'}"
            )

    return {'deleted': deleted, 'warnings': warnings}

def list_restore_archives():
    """Zoznam archívov z histórie, ktoré sú stále dostupné lokálne."""
    archives = []
    for entry in reversed(load_backup_history()):
        try:
            _entry, archive_path = resolve_history_archive(entry.get('id'))
        except (ValueError, FileNotFoundError):
            continue
        archives.append({
            'id': entry.get('id'),
            'filename': entry.get('filename') or os.path.basename(archive_path),
            'timestamp': entry.get('timestamp') or entry.get('date') or '',
            'size': entry.get('size') or get_file_size(archive_path),
            'source_host': entry.get('source_host'),
            'source_mode': entry.get('source_mode'),
            'local_path': archive_path,
        })
    return archives

class RemoteSshRestoreService:
    """Bezpečný restore lokálneho archívu na Proxmox host cez SSH/SFTP."""

    def __init__(self, source_config, ssh_client_factory=None):
        self.source = RemoteSshBackupSource(source_config, ssh_client_factory=ssh_client_factory)

    def write_remote_file(self, client, remote_path, content):
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_path, 'w') as remote_file:
                remote_file.write(content)
        finally:
            sftp.close()

    def upload_archive(self, client, local_archive, remote_archive):
        sftp = client.open_sftp()
        try:
            sftp.put(local_archive, remote_archive)
        finally:
            sftp.close()

    def run_required(self, client, command, timeout=120):
        exit_code, stdout, stderr = self.source.run_command(client, command, timeout=timeout)
        if exit_code != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"Remote command zlyhal: {command}")
        return stdout

    def apply_path(self, client, staging_dir, backup_dir, restore_path):
        arcname = archive_name_for_path(restore_path)
        staged_path = posixpath.join(staging_dir, arcname)
        target_path = normalize_remote_path(restore_path)
        target_parent = posixpath.dirname(target_path) or '/'
        backup_parent = posixpath.join(backup_dir, posixpath.dirname(arcname))

        test_command = f'test -e {shlex.quote(staged_path)} || test -L {shlex.quote(staged_path)}'
        exit_code, _stdout, _stderr = self.source.run_command(client, test_command, timeout=30)
        if exit_code != 0:
            return {'path': restore_path, 'reason': 'missing_in_staging'}

        self.run_required(client, f'mkdir -p {shlex.quote(target_parent)} {shlex.quote(backup_parent)}', timeout=30)
        backup_command = (
            f'if test -e {shlex.quote(target_path)} || test -L {shlex.quote(target_path)}; then '
            f'cp -a {shlex.quote(target_path)} {shlex.quote(backup_parent)}/; '
            f'fi'
        )
        self.run_required(client, backup_command, timeout=300)
        self.run_required(client, f'cp -a {shlex.quote(staged_path)} {shlex.quote(target_parent)}/', timeout=300)
        return None

    def restore(self, archive_path, selected_paths):
        member_names = [member.name for member in restore_archive_members(archive_path, selected_paths)]
        if not member_names:
            raise ValueError('Archív neobsahuje vybrané obnoviteľné položky')

        client = self.source.connect()
        remote_workdir = None
        try:
            remote_workdir = self.run_required(client, 'mktemp -d /tmp/pve-restore.XXXXXX', timeout=10).strip()
            remote_archive = posixpath.join(remote_workdir, 'restore.tar.gz')
            remote_members = posixpath.join(remote_workdir, 'members.txt')
            staging_dir = posixpath.join(remote_workdir, 'staging')
            backup_dir = f"/root/proxmox-backup-restore-preapply-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

            self.upload_archive(client, archive_path, remote_archive)
            self.write_remote_file(client, remote_members, '\n'.join(member_names) + '\n')
            self.run_required(client, f'mkdir -p {shlex.quote(staging_dir)} {shlex.quote(backup_dir)}', timeout=30)
            extract_command = (
                f'tar -xzf {shlex.quote(remote_archive)} '
                f'-C {shlex.quote(staging_dir)} '
                f'-T {shlex.quote(remote_members)}'
            )
            self.run_required(client, extract_command, timeout=600)

            applied = []
            skipped = []
            for restore_path in selected_paths:
                skip = self.apply_path(client, staging_dir, backup_dir, restore_path)
                if skip:
                    skipped.append(skip)
                else:
                    applied.append({'path': restore_path})

            return {
                'success': True,
                'remote_host': self.source.ssh_config.get('host'),
                'backup_dir': backup_dir,
                'applied': applied,
                'skipped': skipped,
            }
        finally:
            if remote_workdir:
                safe_workdir = remote_workdir.strip()
                if safe_workdir.startswith('/tmp/pve-restore.'):
                    self.source.run_command(client, f'rm -rf {shlex.quote(safe_workdir)}', timeout=30)
            client.close()

def run_restore_job(backup_id, selected_paths, source_config):
    """Spoločný restore flow pre API."""
    if not selected_paths:
        raise ValueError('Nevybral si žiadne cesty na obnovu')

    source_config = sanitize_source_config(source_config)
    if source_config['mode'] != 'remote_ssh':
        raise ValueError('Obnova je v tejto verzii podporovaná iba cez Remote SSH')

    allowed_paths = restore_whitelist_paths()
    clean_paths = []
    for path in selected_paths:
        normalized = normalize_remote_path(path)
        if normalized not in allowed_paths:
            raise ValueError(f'Cesta nie je povolená pre restore: {path}')
        if glob.has_magic(normalized):
            raise ValueError(f'Wildcard cesty nie sú podporované pre restore: {path}')
        clean_paths.append(normalized)

    _entry, archive_path = resolve_history_archive(backup_id)
    available_paths = {item['path'] for item in preview_restore_archive(archive_path)}
    missing = [path for path in clean_paths if path not in available_paths]
    if missing:
        raise ValueError(f'Archív neobsahuje vybrané cesty: {", ".join(missing)}')

    restore_service = RemoteSshRestoreService(source_config)
    return restore_service.restore(archive_path, clean_paths)

@app.route('/')
def index():
    """Hlavná stránka"""
    return render_template('index.html')

@app.route('/api/config')
def get_config():
    """API endpoint pre konfiguráciu"""
    config = load_config()
    backup_history = visible_backup_history(config)
    
    selected_count = sum(1 for f in config['backup_files'] if f['selected'])
    critical_selected = sum(1 for f in config['backup_files'] if f['critical'] and f['selected'])
    critical_total = sum(1 for f in config['backup_files'] if f['critical'])
    recommended_selected = sum(1 for f in config['backup_files'] if f.get('priority') == 'recommended' and f['selected'])
    recommended_total = sum(1 for f in config['backup_files'] if f.get('priority') == 'recommended')
    
    return jsonify({
        'config': config,
        'backup_history': backup_history,
        'selected_count': selected_count,
        'critical_selected': critical_selected,
        'critical_total': critical_total,
        'recommended_selected': recommended_selected,
        'recommended_total': recommended_total,
        'backup_categories': BACKUP_CATEGORIES
    })

@app.route('/api/files')
def get_files():
    """API endpoint pre zoznam súborov na zálohovanie"""
    config = load_config()
    return jsonify(config['backup_files'])

@app.route('/api/files/<int:file_index>/toggle', methods=['POST'])
def toggle_file_api(file_index):
    """API endpoint pre prepnutie výberu súboru"""
    config = load_config()
    if 0 <= file_index < len(config['backup_files']):
        config['backup_files'][file_index]['selected'] = not config['backup_files'][file_index]['selected']
        save_config(config)
        return jsonify({'success': True, 'selected': config['backup_files'][file_index]['selected']})
    return jsonify({'success': False, 'error': 'Invalid file index'}), 400

@app.route('/api/files/selection', methods=['POST'])
def set_file_selection_api():
    """API endpoint pre hromadné nastavenie výberu súborov."""
    data = request.get_json(silent=True) or {}
    selected = bool(data.get('selected'))
    config = load_config()
    for item in config['backup_files']:
        item['selected'] = selected
    save_config(config)
    return jsonify({'success': True, 'selected': selected, 'backup_files': config['backup_files']})

@app.route('/api/auto-files')
def get_auto_files():
    """API endpoint pre zoznam súborov automatickej zálohy."""
    config = load_config()
    return jsonify(config['auto_backup_files'])

@app.route('/api/auto-files/<int:file_index>/toggle', methods=['POST'])
def toggle_auto_file_api(file_index):
    """API endpoint pre prepnutie výberu súboru automatickej zálohy."""
    config = load_config()
    if 0 <= file_index < len(config['auto_backup_files']):
        config['auto_backup_files'][file_index]['selected'] = not config['auto_backup_files'][file_index]['selected']
        save_config(config)
        return jsonify({'success': True, 'selected': config['auto_backup_files'][file_index]['selected']})
    return jsonify({'success': False, 'error': 'Invalid file index'}), 400

@app.route('/api/auto-files/selection', methods=['POST'])
def set_auto_file_selection_api():
    """API endpoint pre hromadné nastavenie výberu automatickej zálohy."""
    data = request.get_json(silent=True) or {}
    selected = bool(data.get('selected'))
    config = load_config()
    for item in config['auto_backup_files']:
        item['selected'] = selected
    save_config(config)
    return jsonify({'success': True, 'selected': selected, 'backup_files': config['auto_backup_files']})

@app.route('/api/test-ftp', methods=['POST'])
def test_ftp_api():
    """API endpoint pre test FTP pripojenia"""
    data = request.get_json()
    ftp_config = sanitize_ftp_config(data)
    success, message = test_ftp_connection(
        ftp_config['host'],
        ftp_config['username'],
        ftp_config['password'],
        ftp_config['port'],
        ftp_config.get('remote_dir', ''),
        write_test=True,
    )
    status_code = 200 if success else 400
    return jsonify({'success': success, 'message': message}), status_code

@app.route('/api/test-ssh', methods=['POST'])
def test_ssh_api():
    """API endpoint pre test SSH pripojenia na Proxmox host."""
    data = request.get_json(silent=True) or {}
    source_config = data.get('source_config') or data
    success, message = test_ssh_connection(source_config)
    status_code = 200 if success else 400
    return jsonify({'success': success, 'message': message}), status_code

@app.route('/api/settings', methods=['POST'])
def save_settings_api():
    """Uloženie FTP a source konfigurácie z moderného UI."""
    data = request.get_json(silent=True) or {}
    config = load_config()
    if 'ftp_config' in data:
        config['ftp_config'] = sanitize_ftp_config(data.get('ftp_config'))
    if 'source_config' in data:
        config['source_config'] = sanitize_source_config(data.get('source_config'))
    if 'max_backup_count' in data:
        config['max_backup_count'] = sanitize_max_backup_count(data.get('max_backup_count'))
    save_config(config)
    return jsonify({'success': True, 'config': config})

@app.route('/api/backup', methods=['POST'])
def create_backup_api():
    """API endpoint pre vytvorenie zálohy"""
    data = request.get_json(silent=True) or {}
    config = load_config()
    if 'files' in data:
        selected_files = data.get('files', [])
    else:
        selected_files = [f['path'] for f in config['backup_files'] if f['selected']]
    ftp_config = data.get('ftp_config') or config.get('ftp_config', {})
    source_config = data.get('source_config') or config.get('source_config', DEFAULT_SOURCE_CONFIG)
    
    try:
        result = run_backup_job(selected_files, ftp_config, source_config, config['backup_files'], config=config, backup_mode='manual')
        return jsonify(result)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/backup/auto', methods=['POST'])
def create_auto_backup_api():
    """API endpoint pre automatickú zálohu so samostatným výberom súborov."""
    config = load_config()
    selected_files = [f['path'] for f in config['auto_backup_files'] if f['selected']]
    try:
        result = run_backup_job(
            selected_files,
            config.get('ftp_config', {}),
            config.get('source_config', DEFAULT_SOURCE_CONFIG),
            config['auto_backup_files'],
            config=config,
            backup_mode='auto',
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/backups/<backup_id>', methods=['DELETE'])
def delete_backup_api(backup_id):
    """Zmaže zálohu lokálne, na FTP a z histórie."""
    config = load_config()
    try:
        result = delete_backup_entry(backup_id, config.get('ftp_config', {}))
        status_code = 200 if result.get('success') else 502
        return jsonify(result), status_code
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restore/archives')
def restore_archives_api():
    """Archívy z histórie, ktoré sú stále lokálne dostupné na restore."""
    return jsonify({'success': True, 'archives': list_restore_archives()})

@app.route('/api/restore/preview/<backup_id>')
def restore_preview_api(backup_id):
    """Preview obnoviteľných whitelisted ciest v lokálnom archíve."""
    try:
        entry, archive_path = resolve_history_archive(backup_id)
        return jsonify({
            'success': True,
            'archive': {
                'id': entry.get('id'),
                'filename': entry.get('filename') or os.path.basename(archive_path),
                'timestamp': entry.get('timestamp') or entry.get('date') or '',
                'local_path': archive_path,
            },
            'items': preview_restore_archive(archive_path),
        })
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restore/preview/<backup_id>/members')
def restore_members_api(backup_id):
    """Detail členov archívu pre jednu obnoviteľnú cestu."""
    restore_path = request.args.get('path', '')
    try:
        _entry, archive_path = resolve_history_archive(backup_id)
        if restore_path:
            detail = restore_archive_member_details(archive_path, restore_path)
        else:
            detail = restore_archive_all_member_details(archive_path)
        return jsonify({'success': True, **detail})
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restore', methods=['POST'])
def restore_api():
    """Bezpečný restore vybraných ciest na Proxmox host cez SSH."""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'OBNOVIT':
        return jsonify({'success': False, 'error': 'Pre obnovu je potrebné potvrdenie textom OBNOVIT'}), 400

    config = load_config()
    source_config = data.get('source_config') or config.get('source_config', DEFAULT_SOURCE_CONFIG)
    try:
        result = run_restore_job(data.get('backup_id'), data.get('paths') or [], source_config)
        return jsonify(result)
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/test_ftp', methods=['POST'])
def test_ftp():
    """Test FTP pripojenia"""
    data = request.get_json()
    ftp_config = sanitize_ftp_config(data)
    success, message = test_ftp_connection(
        ftp_config['host'],
        ftp_config['username'],
        ftp_config['password'],
        ftp_config['port'],
        ftp_config.get('remote_dir', ''),
        write_test=True,
    )
    status_code = 200 if success else 400
    return jsonify({'success': success, 'message': message}), status_code

@app.route('/save_ftp_config', methods=['POST'])
def save_ftp_config():
    """Uloženie FTP konfigurácie"""
    config = load_config()
    config['ftp_config'] = sanitize_ftp_config({
        'host': request.form['host'],
        'username': request.form['username'],
        'password': request.form['password'],
        'port': int(request.form.get('port', 21))
    })
    save_config(config)
    flash('FTP konfigurácia uložená', 'success')
    return redirect(url_for('index'))

@app.route('/toggle_file/<int:file_index>')
def toggle_file(file_index):
    """Prepnutie výberu súboru"""
    config = load_config()
    if 0 <= file_index < len(config['backup_files']):
        config['backup_files'][file_index]['selected'] = not config['backup_files'][file_index]['selected']
        save_config(config)
    return redirect(url_for('index'))

@app.route('/create_backup', methods=['POST'])
def create_backup():
    """Vytvorenie zálohy"""
    config = load_config()
    selected_paths = [f['path'] for f in config['backup_files'] if f['selected']]
    
    if not selected_paths:
        flash('Vyberte aspoň jeden súbor na zálohovanie', 'error')
        return redirect(url_for('index'))
    
    try:
        result = run_backup_job(
            selected_paths,
            config.get('ftp_config', {}),
            config.get('source_config', DEFAULT_SOURCE_CONFIG),
            config['backup_files'],
            config=config,
            backup_mode='manual',
        )
        if result['success']:
            if result.get('ftp_status') == 'success':
                flash('Záloha úspešne vytvorená a nahraná na FTP server!', 'success')
            else:
                flash(f"Záloha bola vytvorená lokálne, FTP upload zlyhal: {result.get('ftp_message')}", 'error')
        else:
            flash(f"Archív ostal lokálne v LXC, ale FTP upload zlyhal: {result['message']}", 'error')
    except ValueError as e:
        flash(str(e), 'error')
    except Exception as e:
        flash(f'Chyba pri vytváraní zálohy: {str(e)}', 'error')
    
    return redirect(url_for('index'))

@app.route('/delete_backup/<backup_id>')
def delete_backup(backup_id):
    """Legacy route: zmaže zálohu lokálne, na FTP a z histórie."""
    config = load_config()
    try:
        result = delete_backup_entry(backup_id, config.get('ftp_config', {}))
        if result.get('success'):
            flash('Záloha zmazaná lokálne, na FTP a z histórie', 'success')
        else:
            flash(result.get('ftp_message') or 'Zálohu sa nepodarilo úplne zmazať', 'error')
    except Exception as exc:
        flash(f'Chyba pri mazaní zálohy: {exc}', 'error')
    return redirect(url_for('index'))

@app.route('/api/auto-backup-settings', methods=['POST'])
def save_auto_backup_settings_api():
    """Uloženie nastavení automatickej zálohy (frekvencia, deň, čas)."""
    data = request.get_json(silent=True) or {}
    config = load_config()
    if 'auto_backup_enabled' in data:
        config['auto_backup_enabled'] = bool(data['auto_backup_enabled'])
    if 'auto_backup_frequency' in data:
        freq = data['auto_backup_frequency']
        if freq in ('weekly', 'monthly'):
            config['auto_backup_frequency'] = freq
    if 'auto_backup_day' in data:
        try:
            config['auto_backup_day'] = max(0, min(27, int(data['auto_backup_day'])))
        except (TypeError, ValueError):
            pass
    if 'auto_backup_hour' in data:
        try:
            config['auto_backup_hour'] = max(0, min(23, int(data['auto_backup_hour'])))
        except (TypeError, ValueError):
            pass
    if 'auto_backup_minute' in data:
        try:
            config['auto_backup_minute'] = max(0, min(59, int(data['auto_backup_minute'])))
        except (TypeError, ValueError):
            pass
    save_config(config)
    return jsonify({'success': True, 'config': {
        'auto_backup_enabled': config['auto_backup_enabled'],
        'auto_backup_frequency': config['auto_backup_frequency'],
        'auto_backup_day': config['auto_backup_day'],
        'auto_backup_hour': config['auto_backup_hour'],
        'auto_backup_minute': config['auto_backup_minute'],
    }})

@app.route('/toggle_auto_backup')
def toggle_auto_backup():
    """Prepnutie automatického zálohovania"""
    config = load_config()
    config['auto_backup_enabled'] = not config['auto_backup_enabled']
    save_config(config)
    return redirect(url_for('index'))

@app.route('/set_backup_frequency/<frequency>')
def set_backup_frequency(frequency):
    """Nastavenie frekvencie automatického zálohovania"""
    if frequency in ['weekly', 'monthly']:
        config = load_config()
        config['auto_backup_frequency'] = frequency
        save_config(config)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
