#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify, redirect, url_for, flash, render_template, send_file, session, g
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
import difflib
import re
import ipaddress
import fcntl
import threading
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from datetime import datetime
import time
import base64
import hashlib
import hmac
import io
import secrets
import struct
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from werkzeug.security import generate_password_hash, check_password_hash

from recovery_data import (
    FALLBACK_RECOVERY_PROFILE,
    HOST_SNAPSHOT_FILE_NAMES,
    HOST_SNAPSHOT_FILES,
    RECOVERY_CHECKLIST,
    RECOVERY_PROFILES,
    RESTORE_CATEGORIES,
    RESTORE_CATEGORY_IDS,
    RESTORE_POLICIES,
    REVIEW_CATEGORY_IDS,
    SENSITIVITY_LEVELS,
    WIKI_ARTICLES,
    MIGRATION_METHODS,
    MIGRATION_STEPS,
    MIGRATION_GUEST_TRANSITIONS,
    MIGRATION_COMPARE_COMMANDS,
)

app = Flask(__name__)
app.secret_key = 'proxmox-backup-secret-key-change-in-production'
app.permanent_session_lifetime = timedelta(days=3650)

# Konfiguračný súbor
CONFIG_VERSION = 6
AUTH_CONFIG_VERSION = 1
DEFAULT_MAX_BACKUP_COUNT = 10
CONFIG_FILE = 'backup_config.json'
AUTH_CONFIG_FILE = 'auth_config.json'
BACKUP_HISTORY_FILE = 'backup_history.json'
MIGRATION_STATE_FILE = 'migration_state.json'
RECOVERY_PROGRESS_FILE = 'recovery_progress.json'
MIGRATION_TARGET_FILE = 'migration_target.json'
MIGRATION_JOBS_FILE = 'migration_jobs.json'
MIGRATION_TRANSFER_FILE = 'migration_transfer.json'
BACKUP_STORAGE_DIR = os.environ.get('BACKUP_STORAGE_DIR', 'backups')
APP_ISSUER = 'Proxmox Backup Manager'
PUSHOVER_MESSAGE_URL = 'https://api.pushover.net/1/messages.json'
PUSHOVER_VALIDATE_URL = 'https://api.pushover.net/1/users/validate.json'

DEFAULT_SOURCE_CONFIG = {
    'mode': 'remote_ssh',
    'ssh': {
        'host': '',
        'port': 22,
        'username': 'root',
        'password': '',
    }
}

def now_iso():
    return datetime.now().isoformat(timespec='seconds')

def default_auth_config():
    """Predvolená autentifikačná konfigurácia bez vytvoreného admin účtu."""
    return {
        'auth_version': AUTH_CONFIG_VERSION,
        'secret_key': secrets.token_urlsafe(48),
        'service_token': secrets.token_urlsafe(48),
        'admin': None,
    }

def migrate_auth_config(auth_config):
    defaults = default_auth_config()
    if not isinstance(auth_config, dict):
        return defaults
    migrated = defaults
    migrated.update({key: value for key, value in auth_config.items() if key in migrated})
    migrated['auth_version'] = AUTH_CONFIG_VERSION
    if not migrated.get('secret_key'):
        migrated['secret_key'] = secrets.token_urlsafe(48)
    if not migrated.get('service_token'):
        migrated['service_token'] = secrets.token_urlsafe(48)
    admin = migrated.get('admin')
    if isinstance(admin, dict):
        admin.setdefault('session_version', 1)
        admin.setdefault('failed_login_count', 0)
        admin.setdefault('recovery_codes', [])
        pushover = admin.get('pushover') if isinstance(admin.get('pushover'), dict) else {}
        admin['pushover'] = {
            'app_token': str(pushover.get('app_token', '')),
            'user_key': str(pushover.get('user_key', '')),
            'device': str(pushover.get('device', '')),
            'notify_manual_backups': bool(pushover.get('notify_manual_backups', False)),
            'notify_auto_backups': bool(pushover.get('notify_auto_backups', False)),
            'notify_security': bool(pushover.get('notify_security', True)),
        }
        migrated['admin'] = admin
    else:
        migrated['admin'] = None
    return migrated

def save_auth_config(auth_config):
    auth_config = migrate_auth_config(auth_config)
    tmp_path = f"{AUTH_CONFIG_FILE}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(auth_config, f, ensure_ascii=False, indent=2)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, AUTH_CONFIG_FILE)
    os.chmod(AUTH_CONFIG_FILE, 0o600)

def load_auth_config():
    if os.path.exists(AUTH_CONFIG_FILE):
        with open(AUTH_CONFIG_FILE, 'r', encoding='utf-8') as f:
            auth_config = migrate_auth_config(json.load(f))
    else:
        auth_config = default_auth_config()
        save_auth_config(auth_config)
    return auth_config

def sync_flask_secret():
    auth_config = load_auth_config()
    app.secret_key = auth_config['secret_key']
    return auth_config

def password_is_strong_enough(password):
    return isinstance(password, str) and len(password) >= 10

def generate_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')

def normalize_totp_secret(secret):
    return ''.join(str(secret or '').strip().replace(' ', '').split()).upper()

def totp_token(secret, timestamp=None, interval=30, digits=6):
    timestamp = int(time.time() if timestamp is None else timestamp)
    counter = timestamp // interval
    secret = normalize_totp_secret(secret)
    padded_secret = secret + ('=' * ((8 - len(secret) % 8) % 8))
    key = base64.b32decode(padded_secret, casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)

def verify_totp(secret, code, window=1):
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != 6:
        return False
    current = int(time.time())
    return any(hmac.compare_digest(totp_token(secret, current + offset * 30), code) for offset in range(-window, window + 1))

def build_otpauth_uri(username, secret):
    label = urllib.parse.quote(f"{APP_ISSUER}:{username}")
    query = urllib.parse.urlencode({
        'secret': normalize_totp_secret(secret),
        'issuer': APP_ISSUER,
        'algorithm': 'SHA1',
        'digits': '6',
        'period': '30',
    })
    return f"otpauth://totp/{label}?{query}"

def build_qr_data_uri(otpauth_uri):
    try:
        import qrcode
        image = qrcode.make(otpauth_uri)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
        return f"data:image/png;base64,{encoded}"
    except Exception:
        escaped = (
            otpauth_uri
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
        )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">'
            '<rect width="320" height="320" fill="white"/>'
            '<text x="18" y="32" font-size="15" font-family="monospace" fill="#111">QR knižnica nie je nainštalovaná.</text>'
            '<text x="18" y="58" font-size="12" font-family="monospace" fill="#111">Zadaj secret ručne v Authenticatori.</text>'
            f'<foreignObject x="18" y="82" width="284" height="210"><div xmlns="http://www.w3.org/1999/xhtml" style="font:10px monospace;word-break:break-all;color:#111">{escaped}</div></foreignObject>'
            '</svg>'
        )
        return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode('utf-8')).decode('ascii')

def generate_recovery_codes(count=10):
    return ['-'.join([secrets.token_hex(2).upper(), secrets.token_hex(2).upper(), secrets.token_hex(2).upper()]) for _ in range(count)]

def hash_recovery_codes(codes):
    return [{'hash': generate_password_hash(code), 'used': False, 'used_at': None} for code in codes]

def recovery_codes_remaining(admin):
    if not isinstance(admin, dict):
        return 0
    return sum(1 for item in admin.get('recovery_codes', []) if isinstance(item, dict) and not item.get('used'))

def find_recovery_code(admin, code):
    code = str(code or '').strip().upper()
    for index, item in enumerate(admin.get('recovery_codes', [])):
        if item.get('used'):
            continue
        if check_password_hash(item.get('hash', ''), code):
            return index
    return None

def increment_session_version(admin):
    admin['session_version'] = int(admin.get('session_version', 1)) + 1

def session_is_authenticated(auth_config=None):
    auth_config = auth_config or load_auth_config()
    admin = auth_config.get('admin')
    if not admin:
        return False
    return (
        session.get('auth_user') == admin.get('username')
        and int(session.get('auth_version', 0)) == int(admin.get('session_version', 1))
    )

def ensure_csrf_token():
    if not session.get('csrf_token'):
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']

def login_session(admin):
    session.clear()
    session.permanent = True
    session['auth_user'] = admin['username']
    session['auth_version'] = int(admin.get('session_version', 1))
    return ensure_csrf_token()

def clear_auth_session():
    session.clear()

def json_error(message, status_code=400):
    return jsonify({'success': False, 'error': message}), status_code

def masked_secret(value):
    value = str(value or '')
    if not value:
        return ''
    if len(value) <= 8:
        return '••••'
    return f"{value[:4]}...{value[-4:]}"

def auth_public_endpoint(endpoint):
    if endpoint in ('index', 'auth_status_api', 'auth_setup_start_api', 'auth_setup_complete_api',
                    'auth_login_api', 'auth_logout_api', 'auth_recovery_start_api',
                    'auth_recovery_complete_api', 'auth_totp_recovery_start_api',
                    'auth_totp_recovery_complete_api'):
        return True
    return False

def request_has_valid_service_token(auth_config):
    header = request.headers.get('Authorization', '')
    token = auth_config.get('service_token') or ''
    return bool(token and header.startswith('Bearer ') and hmac.compare_digest(header.replace('Bearer ', '', 1), token))

def csrf_required_for_request():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return True
    return request.endpoint in ('toggle_file', 'delete_backup', 'toggle_auto_backup', 'set_backup_frequency')

def csrf_token_valid():
    expected = session.get('csrf_token') or ''
    provided = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or request.args.get('csrf_token') or ''
    return bool(expected and provided and hmac.compare_digest(expected, provided))

def pushover_configured(admin):
    pushover = admin.get('pushover', {}) if isinstance(admin, dict) else {}
    return bool(pushover.get('app_token') and pushover.get('user_key'))

def default_pushover_post(url, payload, timeout=10):
    encoded = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(url, data=encoded, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode('utf-8')
        return response.getcode(), json.loads(body or '{}')

PUSHOVER_POSTER = default_pushover_post

def send_pushover_message(admin, title, message, priority=0):
    pushover = admin.get('pushover', {}) if isinstance(admin, dict) else {}
    if not pushover_configured(admin):
        raise RuntimeError('Pushover nie je nastavený')
    payload = {
        'token': pushover.get('app_token', ''),
        'user': pushover.get('user_key', ''),
        'message': message,
        'title': title,
        'priority': str(priority),
    }
    if pushover.get('device'):
        payload['device'] = pushover['device']
    try:
        status_code, data = PUSHOVER_POSTER(PUSHOVER_MESSAGE_URL, payload)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f'Pushover odoslanie zlyhalo: {exc}') from exc
    if status_code != 200 or int(data.get('status', 0)) != 1:
        errors = ', '.join(data.get('errors', [])) if isinstance(data.get('errors'), list) else data.get('error', 'neznáma chyba')
        raise RuntimeError(f'Pushover odoslanie zlyhalo: {errors}')
    return data

def validate_pushover_config(app_token, user_key, device=''):
    payload = {'token': app_token, 'user': user_key}
    if device:
        payload['device'] = device
    try:
        status_code, data = PUSHOVER_POSTER(PUSHOVER_VALIDATE_URL, payload)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f'Pushover validácia zlyhala: {exc}') from exc
    if status_code != 200 or int(data.get('status', 0)) != 1:
        errors = ', '.join(data.get('errors', [])) if isinstance(data.get('errors'), list) else data.get('error', 'neplatný token alebo user key')
        raise RuntimeError(f'Pushover validácia zlyhala: {errors}')
    return data

def notify_pushover(event_type, title, message, priority=0):
    auth_config = load_auth_config()
    admin = auth_config.get('admin') or {}
    if not admin or not pushover_configured(admin):
        return None
    pushover = admin.get('pushover', {})
    enabled = {
        'manual_backup': pushover.get('notify_manual_backups', False),
        'auto_backup': pushover.get('notify_auto_backups', False),
        'security': pushover.get('notify_security', True),
    }.get(event_type, False)
    if not enabled:
        return None
    try:
        send_pushover_message(admin, title, message, priority=priority)
        return None
    except Exception as exc:
        return str(exc)

sync_flask_secret()

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
    '/opt/proxmox-backup/auth_config.json',
    '/opt/auth_config.json',
    '*/auth_config.json',
]

MIGRATION_PATH_ALIASES = {
    '/etc/network/interfaces': '/etc/network',
}

RETIRED_BACKUP_PATHS = {
    '/etc/ssl/pve',
}

# Overí existenciu hook skriptov z vzdump jobov (riadky "script <cesta>" v jobs.cfg / vzdump.conf).
HOOK_SCRIPT_CHECK = (
    "for f in $(sed -n 's/^[[:space:]]*script:\\{0,1\\}[[:space:]]\\{1,\\}//p' "
    "/etc/pve/jobs.cfg /etc/vzdump.conf 2>/dev/null | sort -u); do "
    "if [ -x \"$f\" ]; then echo \"OK $f\"; elif [ -e \"$f\" ]; then echo \"NOEXEC $f\"; "
    "else echo \"MISSING $f\"; fi; done"
)

# Diagnostika hosta do backup-info/. Časť z nich tvorí DR metadata snapshot (REFERENCE ONLY,
# HOST_SNAPSHOT_FILES v recovery_data.py). Chýbajúci príkaz zapíše chybu, záloha nezlyhá.
INFO_COMMANDS = [
    ('pveversion-v.txt', ['pveversion', '-v']),
    ('hostname.txt', ['hostname']),
    ('uname-a.txt', ['uname', '-a']),
    ('lscpu.txt', ['lscpu']),
    ('qm-list.txt', ['qm', 'list']),
    ('pct-list.txt', ['pct', 'list']),
    ('pvesm-status.txt', ['pvesm', 'status']),
    ('pvesm-config.txt', ['pvesm', 'config']),
    ('pve-backup-jobs.json', ['pvesh', 'get', '/cluster/backup', '--output-format', 'json']),
    ('network-interfaces.txt', ['cat', '/etc/network/interfaces']),
    ('ip-br-link.txt', ['ip', '-br', 'link']),
    ('ip-br-addr.txt', ['ip', '-br', 'addr']),
    ('ip-addr.txt', ['ip', 'addr']),
    ('ip-route.txt', ['ip', 'route']),
    ('bridge-link.txt', ['bridge', 'link']),
    ('lsblk-f.txt', ['lsblk', '-f']),
    ('blkid.txt', ['blkid']),
    ('disk-by-id.txt', ['ls', '-l', '/dev/disk/by-id']),
    ('df-h.txt', ['df', '-h']),
    ('mount.txt', ['mount']),
    ('findmnt.txt', ['findmnt']),
    ('pvs.txt', ['pvs']),
    ('vgs.txt', ['vgs']),
    ('lvs.txt', ['lvs']),
    ('zpool-status.txt', ['zpool', 'status']),
    ('zfs-list.txt', ['zfs', 'list']),
    ('lspci-nn.txt', ['lspci', '-nn']),
    ('systemctl-failed.txt', ['systemctl', '--failed', '--no-pager']),
    ('hook-scripts.txt', ['sh', '-c', HOOK_SCRIPT_CHECK]),
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
    migrated.pop('recovery', None)
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
    auto_backup_frequency = config.get('auto_backup_frequency', 'monthly')
    migrated['auto_backup_frequency'] = auto_backup_frequency if auto_backup_frequency in ('daily', 'weekly', 'monthly') else 'monthly'
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

# ---------------------------------------------------------------------------
# Disaster recovery metadáta (dáta v recovery_data.py, do configu sa neukladajú)
# ---------------------------------------------------------------------------

RECOVERY_MANIFEST_FILENAME = 'recovery-manifest.json'
# Záloha REQUIRED položky je "aktuálna", ak je mladšia ako tento limit (mesačná auto záloha + rezerva).
RECOVERY_MAX_AGE_DAYS = 35
HOST_SNAPSHOT_MAX_BYTES = 64 * 1024
RESTORE_CATEGORY_BY_ID = {category['id']: category for category in RESTORE_CATEGORIES}
RECOVERY_STATUS_LABELS = {
    'ok': 'Aktuálna záloha aj mimo hosta (FTP)',
    'local_only': 'Aktuálna záloha je iba lokálne v LXC, nie na FTP',
    'stale': f'Posledná záloha je staršia ako {RECOVERY_MAX_AGE_DAYS} dní',
    'missing': 'Nie je v žiadnej dostupnej zálohe',
}
READINESS_LABELS = {
    'READY': 'Pripravené na obnovu',
    'WARNING': 'Obnova možná, ale s rizikom',
    'INCOMPLETE': 'Neúplné – chýbajú REQUIRED dáta',
}

def recovery_profile_for_path(path):
    """DR profil položky; neznáma vlastná položka dostane bezpečný REVIEW FIRST fallback."""
    normalized = normalize_config_path(path) or ''
    source = RECOVERY_PROFILES.get(normalized)
    profile = json.loads(json.dumps(source if source is not None else FALLBACK_RECOVERY_PROFILE))
    category = profile.get('restore_category')
    if category not in RESTORE_CATEGORY_IDS:
        category = 'review'
    alt = profile.get('restore_category_alt')
    if alt not in RESTORE_CATEGORY_IDS or alt == category:
        alt = None
    profile['restore_category'] = category
    profile['restore_category_alt'] = alt
    profile['badge'] = RESTORE_CATEGORY_BY_ID[category]['badge']
    profile['required_for_new_hardware'] = category == 'required'
    profile['requires_review'] = category in REVIEW_CATEGORY_IDS or alt in REVIEW_CATEGORY_IDS
    if profile.get('restore_policy') not in RESTORE_POLICIES:
        profile['restore_policy'] = 'direct'
    profile['direct_restore_allowed'] = profile['restore_policy'] != 'stage_only'
    profile['wildcard'] = glob.has_magic(normalized)
    profile['classified'] = source is not None
    return profile

def with_recovery_metadata(item):
    """Kópia backup položky s pripojeným `recovery` profilom (iba pre API odpoveď)."""
    decorated = dict(item)
    decorated['recovery'] = recovery_profile_for_path(item.get('path'))
    return decorated

def items_with_recovery_metadata(items):
    return [with_recovery_metadata(item) for item in (items or []) if isinstance(item, dict)]

def build_recovery_manifest(selected_files):
    """JSON manifest klasifikácie obnovy pribalený do backup-info/ (čitateľný aj bez aplikácie)."""
    items = []
    for item in selected_files:
        profile = recovery_profile_for_path(item.get('path'))
        items.append({
            'path': item.get('path'),
            'name': item.get('name', item.get('path')),
            'restore_category': profile['restore_category'],
            'restore_category_alt': profile['restore_category_alt'],
            'badge': profile['badge'],
            'required_for_new_hardware': profile['required_for_new_hardware'],
            'restore_order': profile.get('restore_order'),
            'restore_policy': profile['restore_policy'],
            'advanced_restore': bool(profile.get('advanced_restore')),
            'hardware_dependent': bool(profile.get('hardware_dependent')),
            'sensitivity': profile.get('sensitivity'),
            'wiki_slug': profile.get('wiki_slug'),
            'restore_new_hardware': profile.get('restore_new_hardware'),
            'warnings': profile.get('warnings', []),
        })
    return json.dumps({
        'format': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'description': 'Klasifikácia obnovy na novom HW. Nič z toho sa neobnovuje automaticky.',
        'categories': [
            {'id': category['id'], 'label': category['label'], 'badge': category['badge']}
            for category in RESTORE_CATEGORIES
        ],
        'items': items,
    }, ensure_ascii=False, indent=2) + '\n'

def build_recovery_readme_section(selected_files):
    """Sekcia README-RESTORE.txt so zoskupením vybraných ciest podľa restore kategórie."""
    profiled = [(item, recovery_profile_for_path(item.get('path'))) for item in selected_files]
    lines = [
        '## Klasifikácia obnovy na novom HW',
        '',
        'Tagy critical/recommended hovoria, ako dôležité je položku zálohovať. Restore kategória',
        'hovorí, ako bezpečné je ju obnoviť na nový/iný hardvér. Detail: recovery-manifest.json.',
        '',
    ]
    for category in RESTORE_CATEGORIES:
        entries = [(item, profile) for item, profile in profiled if profile['restore_category'] == category['id']]
        if not entries:
            continue
        lines.append(f"### {category['icon']} {category['badge']} – {category['label']}")
        for item, profile in entries:
            flags = []
            if profile.get('advanced_restore'):
                flags.append('ADVANCED RESTORE')
            if profile['restore_policy'] == 'stage_only':
                flags.append('nikdy neprepisovať priamo')
            if profile.get('hardware_dependent'):
                flags.append('HW-závislé')
            if profile.get('sensitivity') == 'secret':
                flags.append('SECRET')
            suffix = f" [{', '.join(flags)}]" if flags else ''
            lines.append(f"- {item.get('path')}{suffix}: {profile.get('restore_new_hardware', '')}")
        lines.append('')
    return '\n'.join(lines)

def parse_backup_timestamp(entry):
    """Čas zálohy z history entry (ISO timestamp alebo starší formát dátumu)."""
    value = entry.get('timestamp')
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            pass
    value = entry.get('date')
    if value:
        try:
            return datetime.strptime(str(value), '%d.%m.%Y %H:%M')
        except ValueError:
            pass
    return None

def backup_entry_contains_path(entry, path):
    """True, ak bola cesta vybraná v zálohe a nebola v nej preskočená (missing/excluded/error)."""
    if path not in (entry.get('files') or []):
        return False
    skipped = {item.get('path') for item in (entry.get('skipped') or []) if isinstance(item, dict)}
    return path not in skipped

def recovery_item_backup_status(path, history, now=None, max_age_days=RECOVERY_MAX_AGE_DAYS):
    """Deterministický stav zálohy jednej položky: ok / local_only / stale / missing."""
    now = now or datetime.now()
    limit = now - timedelta(days=max_age_days)
    covering = []
    for entry in history:
        timestamp = parse_backup_timestamp(entry)
        if timestamp and backup_entry_contains_path(entry, path):
            covering.append((timestamp, entry))
    covering.sort(key=lambda pair: pair[0], reverse=True)

    if not covering:
        status = 'missing'
        latest = offsite = None
    else:
        latest = covering[0]
        offsite = next((pair for pair in covering if pair[1].get('ftp_status') == 'success'), None)
        if latest[0] < limit:
            status = 'stale'
        elif not offsite or offsite[0] < limit:
            status = 'local_only'
        else:
            status = 'ok'

    return {
        'status': status,
        'label': RECOVERY_STATUS_LABELS[status],
        'last_backup': latest[0].isoformat(timespec='seconds') if latest else None,
        'last_backup_filename': latest[1].get('filename') if latest else None,
        'last_offsite_backup': offsite[0].isoformat(timespec='seconds') if offsite else None,
    }

def build_recovery_overview(config=None, history=None, now=None):
    """Prehľad pripravenosti na obnovu; READY/WARNING/INCOMPLETE iba z REQUIRED položiek."""
    config = config or load_config()
    if history is None:
        history = visible_backup_history(config, persist_pruned=False)
    now = now or datetime.now()
    manual_selected = {item.get('path') for item in config.get('backup_files', []) if item.get('selected')}
    auto_selected = {item.get('path') for item in config.get('auto_backup_files', []) if item.get('selected')}
    auto_enabled = bool(config.get('auto_backup_enabled'))

    items = []
    for item in config.get('backup_files', []):
        decorated = with_recovery_metadata(item)
        decorated['backup_status'] = recovery_item_backup_status(item.get('path'), history, now)
        decorated['selected_manual'] = item.get('path') in manual_selected
        decorated['selected_auto'] = item.get('path') in auto_selected
        items.append(decorated)

    category_order = {category_id: index for index, category_id in enumerate(RESTORE_CATEGORY_IDS)}
    items.sort(key=lambda entry: (
        category_order[entry['recovery']['restore_category']],
        entry['recovery'].get('restore_order') or 99,
        entry.get('name', ''),
    ))

    categories = []
    for category in RESTORE_CATEGORIES:
        in_category = [entry for entry in items if entry['recovery']['restore_category'] == category['id']]
        counts = {
            status: sum(1 for entry in in_category if entry['backup_status']['status'] == status)
            for status in RECOVERY_STATUS_LABELS
        }
        categories.append({**category, 'total': len(in_category), 'backed_up': counts['ok'], 'counts': counts})

    required = [entry for entry in items if entry['recovery']['required_for_new_hardware']]
    issues = []
    for entry in required:
        status = entry['backup_status']['status']
        if status != 'ok':
            issues.append({
                'path': entry['path'],
                'name': entry.get('name', entry['path']),
                'level': 'error' if status == 'missing' else 'warning',
                'message': entry['backup_status']['label'],
            })
        if not entry['selected_manual'] and not entry['selected_auto']:
            issues.append({
                'path': entry['path'],
                'name': entry.get('name', entry['path']),
                'level': 'warning',
                'message': 'Nie je vybraná v ručnej ani automatickej zálohe – ďalšie zálohy ju nebudú obsahovať',
            })
        elif auto_enabled and not entry['selected_auto']:
            issues.append({
                'path': entry['path'],
                'name': entry.get('name', entry['path']),
                'level': 'warning',
                'message': 'Automatická záloha ju nezahŕňa',
            })

    if not required or not history or any(issue['level'] == 'error' for issue in issues):
        readiness_status = 'INCOMPLETE'
    elif issues:
        readiness_status = 'WARNING'
    else:
        readiness_status = 'READY'

    latest_entry = max(history, key=lambda entry: parse_backup_timestamp(entry) or datetime.min, default=None)
    latest_backup = None
    if latest_entry:
        latest_timestamp = parse_backup_timestamp(latest_entry)
        latest_backup = {
            'id': latest_entry.get('id'),
            'filename': latest_entry.get('filename'),
            'timestamp': latest_timestamp.isoformat(timespec='seconds') if latest_timestamp else None,
            'ftp_status': latest_entry.get('ftp_status'),
            'backup_mode': latest_entry.get('backup_mode'),
        }

    return {
        'success': True,
        'generated_at': now.isoformat(timespec='seconds'),
        'readiness': {
            'status': readiness_status,
            'label': READINESS_LABELS[readiness_status],
            'required_total': len(required),
            'required_ok': sum(1 for entry in required if entry['backup_status']['status'] == 'ok'),
            'max_age_days': RECOVERY_MAX_AGE_DAYS,
            'issues': issues,
            'latest_backup': latest_backup,
            'rules': [
                f'Hodnotia sa iba položky NEW HW REQUIRED ({len(required)}).',
                f'Položka je OK, ak je v zálohe mladšej ako {RECOVERY_MAX_AGE_DAYS} dní, nebola v nej preskočená a táto záloha je aj na FTP (mimo hosta).',
                'INCOMPLETE: niektorá REQUIRED položka nie je v žiadnej dostupnej zálohe.',
                'WARNING: všetko je zálohované, ale niečo je staršie ako limit, iba lokálne alebo nie je vybrané pre ďalšie zálohy.',
                'READY: všetky REQUIRED položky majú aktuálnu zálohu mimo hosta a sú vybrané pre ďalšie zálohy.',
            ],
        },
        'categories': categories,
        'items': items,
        'auto_backup_enabled': auto_enabled,
        'restore_policies': RESTORE_POLICIES,
        'sensitivity_levels': SENSITIVITY_LEVELS,
        'snapshot_files': [{'file': name, 'label': label} for name, label in HOST_SNAPSHOT_FILES],
        'wiki': wiki_index(),
        'checklist': RECOVERY_CHECKLIST,
        'checklist_progress': safe_recovery_progress(),
        'risks': build_recovery_risks(history),
    }

def safe_recovery_progress():
    try:
        return load_recovery_progress()
    except (OSError, RuntimeError):
        return None

def wiki_index():
    return [
        {'slug': article['slug'], 'title': article['title'], 'summary': article['summary']}
        for article in WIKI_ARTICLES
    ]

def find_wiki_article(slug):
    return next((article for article in WIKI_ARTICLES if article['slug'] == slug), None)

def parse_info_command_output(text):
    """Rozdelí výstup run_info_command na príkaz, exit code, stdout a stderr."""
    lines = text.split('\n')
    result = {'command': '', 'exit_code': None, 'stdout': '', 'stderr': '', 'error': ''}
    if lines and lines[0].startswith('$ '):
        result['command'] = lines[0][2:]
    match = re.search(r'^exit_code=(-?\d+)$', text, re.MULTILINE)
    if match:
        result['exit_code'] = int(match.group(1))
    if '--- stdout ---\n' in text:
        after = text.split('--- stdout ---\n', 1)[1]
        stdout, _sep, stderr = after.partition('\n--- stderr ---\n')
        result['stdout'] = stdout.rstrip('\n')
        result['stderr'] = stderr.rstrip('\n')
    else:
        result['error'] = '\n'.join(lines[1:]).strip()
    return result

def read_host_snapshot(archive_path):
    """Načíta iba whitelisted DR snapshot súbory z backup-info/ (žiadne konfiguračné súbory)."""
    found = {}
    with tarfile.open(archive_path, 'r:gz') as tar:
        for member in tar.getmembers():
            validate_tar_member(member)
            if not member.isfile() or not member.name.startswith('backup-info/'):
                continue
            filename = member.name[len('backup-info/'):]
            if filename in HOST_SNAPSHOT_FILE_NAMES:
                found[filename] = member

        files = []
        for filename, label in HOST_SNAPSHOT_FILES:
            member = found.get(filename)
            if not member:
                files.append({'file': filename, 'label': label, 'available': False})
                continue
            handle = tar.extractfile(member)
            data = handle.read(HOST_SNAPSHOT_MAX_BYTES + 1) if handle else b''
            truncated = len(data) > HOST_SNAPSHOT_MAX_BYTES
            parsed = parse_info_command_output(data[:HOST_SNAPSHOT_MAX_BYTES].decode('utf-8', errors='replace'))
            files.append({
                'file': filename,
                'label': label,
                'available': True,
                'truncated': truncated,
                **parsed,
            })
    return files

# ---------------------------------------------------------------------------
# Fakty o pôvodnom hoste z najnovšieho archívu (riziká obnovy, offline príručka)
# ---------------------------------------------------------------------------

# Iba konfiguračné súbory bez tajomstiev; shadow, priv/ ani /root sa nikdy nečítajú.
ARCHIVE_FACT_FILES = {
    'etc/hostname', 'etc/hosts', 'etc/resolv.conf', 'etc/network/interfaces', 'etc/fstab',
    'etc/auto.master', 'etc/auto.nfs', 'etc/pve/storage.cfg', 'etc/pve/jobs.cfg',
}
ARCHIVE_FACT_INFO_FILES = {
    'hostname.txt', 'pveversion-v.txt', 'ip-br-link.txt', 'ip-br-addr.txt', 'ip-route.txt',
    'lsblk-f.txt', 'pvesm-status.txt', 'qm-list.txt', 'pct-list.txt', 'pve-backup-jobs.json',
    'hook-scripts.txt', 'lscpu.txt', 'ip-addr.txt',
}
ARCHIVE_FACT_MAX_BYTES = 64 * 1024
GUEST_CONF_PATTERN = re.compile(r'^etc/pve/nodes/([^/]+)/(lxc|qemu-server)/(\d+)\.conf$')
_ARCHIVE_FACTS_CACHE = {}

def archive_fact_wanted(name):
    return (
        name in ARCHIVE_FACT_FILES
        or name.startswith('etc/auto.master.d/')
        or bool(GUEST_CONF_PATTERN.match(name))
        or (name.startswith('backup-info/') and name[len('backup-info/'):] in ARCHIVE_FACT_INFO_FILES)
    )

def read_archive_facts(archive_path):
    """Načíta whitelisted konfiguráciu a diagnostiku z archívu (cache podľa mtime/veľkosti)."""
    stat = os.stat(archive_path)
    cache_key = (os.path.realpath(archive_path), stat.st_mtime_ns, stat.st_size)
    if cache_key in _ARCHIVE_FACTS_CACHE:
        return _ARCHIVE_FACTS_CACHE[cache_key]

    members = set()
    files = {}
    info = {}
    with tarfile.open(archive_path, 'r:gz') as tar:
        for member in tar.getmembers():
            validate_tar_member(member)
            name = member.name.rstrip('/')
            members.add(name)
            if not member.isfile() or not archive_fact_wanted(name):
                continue
            handle = tar.extractfile(member)
            text = (handle.read(ARCHIVE_FACT_MAX_BYTES) if handle else b'').decode('utf-8', errors='replace')
            if name.startswith('backup-info/'):
                info[name[len('backup-info/'):]] = parse_info_command_output(text)
            else:
                files[name] = text

    facts = {'members': members, 'files': files, 'info': info}
    if len(_ARCHIVE_FACTS_CACHE) > 8:
        _ARCHIVE_FACTS_CACHE.clear()
    _ARCHIVE_FACTS_CACHE[cache_key] = facts
    return facts

def info_stdout(facts, filename):
    parsed = (facts.get('info') or {}).get(filename)
    if not parsed or parsed.get('error'):
        return ''
    return parsed.get('stdout', '')

def latest_local_archive_entry(history):
    """Najnovší záznam histórie, ktorého archív leží lokálne (fakty sa čítajú bez FTP)."""
    for entry in sorted(history, key=lambda item: parse_backup_timestamp(item) or datetime.min, reverse=True):
        try:
            path = resolve_backup_entry_local_path(entry)
        except (ValueError, TypeError):
            continue
        if os.path.isfile(path):
            return entry, path
    return None, None

def parse_guest_lists(facts):
    """VM a LXC z `qm list` a `pct list` v backup-info."""
    guests = []
    for filename, guest_type in (('qm-list.txt', 'VM'), ('pct-list.txt', 'LXC')):
        for line in info_stdout(facts, filename).splitlines():
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            if guest_type == 'VM':
                name = parts[1] if len(parts) > 1 else ''
                status = parts[2] if len(parts) > 2 else ''
            else:
                status = parts[1] if len(parts) > 1 else ''
                name = parts[-1] if len(parts) > 2 else ''
            guests.append({'vmid': int(parts[0]), 'type': guest_type, 'name': name, 'status': status})
    return sorted(guests, key=lambda guest: guest['vmid'])

def parse_vmid_list(value):
    result = set()
    for part in re.split(r'[,\s]+', str(value or '')):
        if part.isdigit():
            result.add(int(part))
    return result

def parse_pve_section_config(text):
    """Jednoduchý parser PVE section configu (jobs.cfg, storage.cfg): `typ: id` + odsadené `kľúč hodnota`."""
    sections = []
    current = None
    for raw in (text or '').splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        if not raw[0].isspace() and ':' in raw:
            section_type, _sep, section_id = raw.partition(':')
            current = {'type': section_type.strip(), 'id': section_id.strip(), 'props': {}}
            sections.append(current)
        elif current is not None:
            key, _sep, value = raw.strip().partition(' ')
            current['props'][key] = value.strip()
    return sections

def normalize_vzdump_job(job):
    def flag(value, default):
        if value in (None, ''):
            return default
        return str(value).strip().lower() not in ('0', 'false', 'no')
    return {
        'id': str(job.get('id', '')),
        'vmids': parse_vmid_list(job.get('vmid')),
        'all': flag(job.get('all'), False),
        'exclude': parse_vmid_list(job.get('exclude')),
        'pool': str(job.get('pool') or ''),
        'node': str(job.get('node') or ''),
        'enabled': flag(job.get('enabled'), True),
        'storage': str(job.get('storage') or ''),
        'schedule': str(job.get('schedule') or ''),
        'script': str(job.get('script') or ''),
    }

def parse_vzdump_jobs(facts):
    """Vzdump joby z `pvesh get /cluster/backup` (JSON), fallback na /etc/pve/jobs.cfg."""
    raw = info_stdout(facts, 'pve-backup-jobs.json').strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [normalize_vzdump_job(job) for job in data if isinstance(job, dict)]
        except json.JSONDecodeError:
            pass
    jobs = []
    for section in parse_pve_section_config(facts['files'].get('etc/pve/jobs.cfg', '')):
        if section['type'] == 'vzdump':
            jobs.append(normalize_vzdump_job({'id': section['id'], **section['props']}))
    return jobs

def nic_summary(facts):
    """Názvy NIC a MAC pôvodného hosta: `ip -br link`, pre staršie archívy odvodené z `ip addr`."""
    brief = info_stdout(facts, 'ip-br-link.txt').strip()
    if brief:
        return brief
    lines = []
    current = None
    for line in info_stdout(facts, 'ip-addr.txt').splitlines():
        match = re.match(r'^\d+:\s+([^:@\s]+)(?:@\S+)?:\s+<([^>]*)>', line)
        if match:
            current = [match.group(1), 'UP' if 'LOWER_UP' in match.group(2) else 'DOWN', '']
            if not current[0].startswith(('tap', 'veth', 'fwbr', 'fwpr', 'fwln')):
                lines.append(current)
            continue
        ether = re.match(r'^\s+link/ether\s+(\S+)', line)
        if ether and current is not None:
            current[2] = ether.group(1)
    return '\n'.join(f'{name:<16} {state:<5} {mac}'.rstrip() for name, state, mac in lines)

def archive_node_name(facts):
    name = info_stdout(facts, 'hostname.txt').strip().splitlines()
    if name:
        return name[0].strip()
    return facts['files'].get('etc/hostname', '').strip().split('.')[0]

def hook_script_status(facts, script_path):
    """OK / NOEXEC / MISSING z hook-scripts.txt, inak odhad podľa zálohovaného adresára, inak unknown."""
    for line in info_stdout(facts, 'hook-scripts.txt').splitlines():
        status, _sep, path = line.strip().partition(' ')
        if path == script_path and status in ('OK', 'NOEXEC', 'MISSING'):
            return status, 'host'
    arcname = archive_name_for_path(script_path)
    parent = posixpath.dirname(arcname)
    if arcname in facts['members']:
        return 'OK', 'archive'
    if parent and parent in facts['members']:
        return 'MISSING', 'archive'
    return 'unknown', ''

def analyze_recovery_risks(facts, entry=None):
    """Deterministické riziká obnovy z faktov archívu (nemenia READY/WARNING/INCOMPLETE)."""
    risks = []
    guests = parse_guest_lists(facts)
    jobs = parse_vzdump_jobs(facts)
    node = archive_node_name(facts)

    if guests:
        if not jobs:
            risks.append({
                'id': 'no-vzdump-jobs', 'level': 'error',
                'title': 'Žiadny vzdump job',
                'detail': 'Na hoste nie je definovaný žiadny vzdump job – disky VM a LXC sa nezálohujú.',
                'items': [],
            })
        else:
            pool_jobs = [job['id'] for job in jobs if job['pool']]
            uncovered = []
            for guest in guests:
                covered = any(
                    (job['all'] and guest['vmid'] not in job['exclude']) or guest['vmid'] in job['vmids']
                    for job in jobs
                )
                guest['backed_up'] = covered
                if not covered:
                    uncovered.append(guest)
            if uncovered:
                running = [guest for guest in uncovered if guest['status'] == 'running']
                risks.append({
                    'id': 'guests-without-vzdump',
                    'level': 'info' if pool_jobs else ('error' if running else 'warning'),
                    'title': f'Hostia bez vzdump zálohy ({len(uncovered)})',
                    'detail': (
                        'Tieto VM/LXC nie sú v žiadnom vzdump jobe. Ich config sa obnoví z /etc/pve, ale disky nie.'
                        + (f' Joby s poolom ({", ".join(pool_jobs)}) sa nedajú overiť – skontroluj ručne.' if pool_jobs else '')
                    ),
                    'items': [f"{guest['vmid']} {guest['name']} ({guest['type']}, {guest['status'] or 'n/a'})" for guest in uncovered],
                })

    if node:
        mismatched = [job for job in jobs if job['node'] and job['node'] != node]
        if mismatched:
            risks.append({
                'id': 'job-node-mismatch', 'level': 'error',
                'title': 'Vzdump job je viazaný na iný node',
                'detail': f'Host sa volá „{node}“, ale tieto joby majú iný node – nezálohujú nič (napr. po zmene hostname).',
                'items': [f"{job['id']}: node {job['node']}" for job in mismatched],
            })

    scripts = sorted({job['script'] for job in jobs if job['script']})
    missing, unknown = [], []
    for script in scripts:
        status, source = hook_script_status(facts, script)
        label = 'podľa kontroly na hoste' if source == 'host' else 'podľa obsahu archívu'
        if status in ('MISSING', 'NOEXEC'):
            missing.append(f"{script} – {'neexistuje' if status == 'MISSING' else 'nie je spustiteľný'} ({label})")
        elif status == 'unknown':
            unknown.append(script)
    if missing:
        risks.append({
            'id': 'hook-script-missing', 'level': 'error',
            'title': 'Hook skript vzdump jobu chýba',
            'detail': 'Vzdump job odkazuje na hook skript, ktorý na hoste nie je. Over v `journalctl`, či zálohy VM/LXC reálne vznikajú.',
            'items': missing,
        })
    if unknown:
        risks.append({
            'id': 'hook-script-unknown', 'level': 'info',
            'title': 'Hook skript sa nedá overiť',
            'detail': 'Adresár skriptu nie je v zálohe. Novšie zálohy to overia priamo na hoste (hook-scripts.txt).',
            'items': unknown,
        })

    if jobs and not any(job['enabled'] for job in jobs):
        orchestrated = any(
            name.startswith('etc/systemd/system/') and name.endswith('.timer') and 'backup' in name
            for name in facts['members']
        )
        risks.append({
            'id': 'jobs-disabled', 'level': 'info' if orchestrated else 'warning',
            'title': 'Všetky vzdump joby sú v PVE vypnuté',
            'detail': (
                'Joby majú enabled 0 – spúšťa ich vlastný systemd timer (orchestrátor). Over `systemctl list-timers`.'
                if orchestrated else
                'Joby majú enabled 0 a nenašiel sa vlastný timer – zálohy VM/LXC sa pravdepodobne nespúšťajú.'
            ),
            'items': [job['id'] for job in jobs],
        })

    return {'node': node, 'guests': guests, 'jobs': jobs, 'risks': risks}

def build_recovery_risks(history):
    """Riziká obnovy z najnovšieho lokálneho archívu + pripomienka offline kópie archívu."""
    risks = []
    entry, archive_path = latest_local_archive_entry(history)
    archive = None
    if entry:
        archive = {
            'id': entry.get('id'),
            'filename': entry.get('filename'),
            'timestamp': entry.get('timestamp'),
        }
        try:
            analysis = analyze_recovery_risks(read_archive_facts(archive_path), entry)
            risks.extend(analysis['risks'])
        except (OSError, tarfile.TarError, ValueError) as exc:
            risks.append({'id': 'archive-unreadable', 'level': 'warning', 'title': 'Archív sa nedá analyzovať',
                          'detail': str(exc), 'items': []})
    else:
        risks.append({
            'id': 'no-local-archive', 'level': 'info',
            'title': 'Žiadny lokálny archív na analýzu',
            'detail': 'Riziká sa počítajú z najnovšieho lokálneho archívu. Vytvor zálohu alebo načítaj archív z FTP (História).',
            'items': [],
        })

    downloads = [item.get('downloaded_at') for item in history if item.get('downloaded_at')]
    latest = max(history, key=lambda item: parse_backup_timestamp(item) or datetime.min, default=None)
    if latest and not latest.get('downloaded_at'):
        last_download = max(downloads) if downloads else None
        risks.append({
            'id': 'no-offline-copy', 'level': 'warning',
            'title': 'Najnovší archív nemáš stiahnutý mimo servera',
            'detail': (
                'Archívy sú na FTP/NAS. Ak zomrie aj NAS, konfiguráciu hosta nebudeš mať. Stiahni si najnovší archív '
                '(História → Stiahnuť) a offline príručku na svoje PC.'
                + (f' Posledné stiahnutie: {last_download[:16].replace("T", " ")}.' if last_download else ' Zatiaľ si nestiahol žiadny archív.')
            ),
            'items': [],
        })
    return {'archive': archive, 'risks': risks}

# ---------------------------------------------------------------------------
# Plánovaná migrácia – samostatný runtime stav; žiadne vykonávanie SSH príkazov.
# ---------------------------------------------------------------------------

def migration_now():
    return datetime.now(ZoneInfo('Europe/Bratislava')).isoformat(timespec='seconds')

def default_migration_state():
    return {
        'version': 1, 'method': None,
        'old_host': {'ip': '', 'hostname': ''}, 'new_host': {'ip': '', 'hostname': ''},
        'steps': {}, 'guests': {}, 'started_at': None, 'updated_at': None, 'finished_at': None,
    }

@contextmanager
def json_state_lock(path):
    """Stabilný lock serializuje read/modify/replace aj medzi gunicorn workermi."""
    fd = os.open(f'{path}.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def migration_state_lock():
    return json_state_lock(MIGRATION_STATE_FILE)

def write_json_state(path, state):
    """0600 už pri vytvorení tmp, flush/fsync a atomický replace na rovnakom FS."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

def load_migration_state():
    if not os.path.exists(MIGRATION_STATE_FILE):
        return default_migration_state()
    try:
        with open(MIGRATION_STATE_FILE, encoding='utf-8') as handle:
            state = json.load(handle)
        defaults = default_migration_state()
        if (not isinstance(state, dict) or set(state) != set(defaults)
                or state['version'] != 1
                or state['method'] not in (None, 'disk_move', 'side_by_side')
                or not isinstance(state['steps'], dict) or not isinstance(state['guests'], dict)):
            raise ValueError()
        state['old_host'] = validate_migration_host(state['old_host'])
        state['new_host'] = validate_migration_host(state['new_host'])
        for step_id, step in state['steps'].items():
            if (step_id not in {s['id'] for s in MIGRATION_STEPS}
                    or not isinstance(step, dict) or set(step) != {'completed', 'updated_at'}
                    or type(step['completed']) is not bool or not isinstance(step['updated_at'], str)):
                raise ValueError()
        for vmid, guest in state['guests'].items():
            validate_migration_vmid(vmid)
            if (not isinstance(guest, dict) or set(guest) != {'status', 'note', 'updated_at', 'type', 'name'}
                    or guest['status'] not in MIGRATION_GUEST_TRANSITIONS
                    or guest['type'] not in ('VM', 'LXC')
                    or not isinstance(guest['name'], str)
                    or not isinstance(guest['note'], str) or len(guest['note']) > 2000
                    or not isinstance(guest['updated_at'], (str, type(None)))):
                raise ValueError()
        for key in ('started_at', 'updated_at', 'finished_at'):
            if not isinstance(state[key], (str, type(None))):
                raise ValueError()
        return state
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('Stav migrácie je poškodený alebo má nepodporovanú verziu. Obnov súbor zo zálohy alebo použi reset.') from None

def save_migration_state(state):
    write_json_state(MIGRATION_STATE_FILE, state)

# Postup obnovy po havárii (RECOVERY_CHECKLIST) – na serveri, aby fungoval z mobilu aj z PC.

def default_recovery_progress():
    return {'version': 1, 'steps': {}, 'updated_at': None}

def load_recovery_progress():
    if not os.path.exists(RECOVERY_PROGRESS_FILE):
        return default_recovery_progress()
    try:
        with open(RECOVERY_PROGRESS_FILE, encoding='utf-8') as handle:
            state = json.load(handle)
        step_ids = {step['id'] for step in RECOVERY_CHECKLIST}
        if (not isinstance(state, dict) or set(state) != {'version', 'steps', 'updated_at'}
                or state['version'] != 1 or not isinstance(state['steps'], dict)
                or not isinstance(state['updated_at'], (str, type(None)))):
            raise ValueError()
        for step_id, step in state['steps'].items():
            if (step_id not in step_ids or not isinstance(step, dict)
                    or set(step) != {'completed', 'updated_at'} or type(step['completed']) is not bool
                    or not isinstance(step['updated_at'], str)):
                raise ValueError()
        return state
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('Stav postupu obnovy je poškodený. Použi reset postupu.') from None

def recovery_progress_response(update=None, reset=False):
    try:
        with json_state_lock(RECOVERY_PROGRESS_FILE):
            state = default_recovery_progress() if reset else load_recovery_progress()
            if update:
                update(state)
                state['updated_at'] = migration_now()
            if update or reset:
                write_json_state(RECOVERY_PROGRESS_FILE, state)
        response = jsonify({'success': True, 'steps': RECOVERY_CHECKLIST, 'progress': state})
        response.headers['Cache-Control'] = 'no-store'
        return response
    except ValueError as exc:
        return json_error(str(exc))
    except (OSError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Stav postupu obnovy sa nedá uložiť.'
        return json_error(message, 503)

def validate_migration_host(host):
    if not isinstance(host, dict) or set(host) - {'ip', 'hostname'}:
        raise ValueError('Údaje hosta môžu obsahovať iba IP a hostname, bez hesiel.')
    clean = {}
    for key in ('ip', 'hostname'):
        value = host.get(key, '')
        if not isinstance(value, str):
            raise ValueError('IP aj hostname musia byť text.')
        clean[key] = value.strip()
    if clean['ip']:
        try:
            # Bez CIDR a scope ID; samostatná IPv4/IPv6 adresa.
            if '%' in clean['ip']:
                raise ValueError()
            clean['ip'] = str(ipaddress.ip_address(clean['ip']))
        except ValueError:
            raise ValueError('Neplatná IP adresa hosta.') from None
    name = clean['hostname']
    if name:
        name = name.rstrip('.').lower()
        if (len(name) > 253 or not name or any(
                not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in name.split('.'))):
            raise ValueError('Neplatný hostname hosta.')
        clean['hostname'] = name
    return clean

def validate_migration_vmid(vmid):
    if not re.fullmatch(r'[1-9][0-9]{2,8}', str(vmid)) or not 100 <= int(vmid) <= 999999999:
        raise ValueError('Neplatné VMID.')
    return str(vmid)

def migration_inventory(history):
    entry, path = latest_local_archive_entry(history)
    result = {'available': False, 'archive': None, 'error': None}
    if not path:
        result['error'] = 'Chýba lokálny archív. Vytvor zálohu alebo načítaj archív z FTP v Histórii.'
        return result, []
    result['archive'] = {key: entry.get(key) for key in ('id', 'filename', 'timestamp')}
    try:
        facts = read_archive_facts(path)
        if any(not (facts['info'].get(name) and not facts['info'][name].get('error')
                    and facts['info'][name].get('exit_code') == 0)
               for name in ('qm-list.txt', 'pct-list.txt')):
            raise ValueError('Inventár VM/LXC je neúplný. Vytvor novú zálohu s úspešným qm list aj pct list.')
        analysis = analyze_recovery_risks(facts, entry)
        guests = analysis['guests']
        ids = [validate_migration_vmid(g['vmid']) for g in guests]
        if len(ids) != len(set(ids)):
            raise ValueError('Inventár obsahuje duplicitné VMID; skontroluj archív.')
        for guest in guests:
            guest.setdefault('backed_up', False)
        result['available'] = True
        return result, guests
    except (OSError, tarfile.TarError, ValueError) as exc:
        result['error'] = str(exc)
        return result, []

def migration_guest_commands(guest):
    vmid = int(guest['vmid'])
    command = 'qm' if guest['type'] == 'VM' else 'pct'
    archive_type = 'qemu' if guest['type'] == 'VM' else 'lxc'
    extension = 'vma.zst' if guest['type'] == 'VM' else 'tar.zst'
    archive = f'/CESTA_K_ZALOHE/vzdump-{archive_type}-{vmid}-CAS.{extension}'
    restore = (f'qmrestore "{archive}" {vmid} --storage TARGET_STORAGE' if command == 'qm'
               else f'pct restore {vmid} "{archive}" --storage TARGET_STORAGE')
    return {
        'old_host': [f'{command} set {vmid} --onboot 0', f'{command} shutdown {vmid}',
                     f'{command} status {vmid}  # pokračuj iba pri stopped',
                     f'vzdump {vmid} --storage BACKUP_STORAGE --mode stop --compress zstd',
                     f'{command} status {vmid}  # po dokončení musí byť stopped'],
        'new_host': ['# Nahraď CESTA_K_ZALOHE, CAS a TARGET_STORAGE; VMID musí byť voľné.',
                     restore, f'{command} set {vmid} --onboot 0', f'{command} config {vmid}',
                     '# Pred štartom over stopped na STAROM hoste a správnu sieť/passthrough.',
                     f'{command} start {vmid}', f'{command} status {vmid}'],
    }

def build_migration_payload(state, history=None):
    history = history if history is not None else load_backup_history()
    inventory, archived_guests = migration_inventory(history)
    guests = []
    if state['method'] == 'side_by_side':
        by_id = {str(g['vmid']): g for g in archived_guests}
        # Pôvodní hostia nezmiznú zo sprievodcu, keď sa zálohuje už nový host.
        for vmid, saved in state['guests'].items():
            current = by_id.get(vmid)
            if current and current['type'] != saved['type']:
                inventory.update(available=False, error='Typ hosťa s rovnakým VMID sa zmenil; over inventár ručne.')
            by_id.setdefault(vmid, {'vmid': int(vmid), 'type': saved['type'], 'name': saved['name'], 'backed_up': False})
        for vmid, guest in sorted(by_id.items(), key=lambda pair: int(pair[0])):
            saved = state['guests'].get(vmid, {})
            guest = {**guest, 'type': saved.get('type', guest['type']), 'status': saved.get('status', 'pending'),
                     'note': saved.get('note', ''), 'updated_at': saved.get('updated_at')}
            guest['allowed_transitions'] = MIGRATION_GUEST_TRANSITIONS[guest['status']]
            guest['commands'] = migration_guest_commands(guest)
            guests.append(guest)
    steps = [step for step in MIGRATION_STEPS if state['method'] in step['methods']]
    target, target_error = safe_public_migration_target()
    app_vmid = migration_app_vmid(history) if state['method'] == 'side_by_side' else None
    try:
        jobs = [public_migration_job(job) for job in list_migration_jobs()[:5]]
    except (OSError, RuntimeError):
        jobs = []
    return {
        'success': True, 'state': state, 'methods': MIGRATION_METHODS, 'steps': steps,
        'target': target, 'target_error': target_error, 'jobs': jobs,
        'guests': guests, 'inventory': inventory, 'risks': build_recovery_risks(history),
        'transfer': safe_migration_transfer(),
        'app_vmid': app_vmid,
        # LXC s appkou sa presúva až pri prepnutí (ručne, ako posledný), preto sa do podmienky neráta.
        'cutover_ready': state['method'] == 'disk_move' or (
            state['method'] == 'side_by_side' and inventory['available']
            and all(g['status'] in ('verified', 'skipped') for g in guests if g['vmid'] != app_vmid)),
    }

def migration_app_vmid(history):
    """VMID LXC/VM, v ktorom beží táto appka (podľa jej IP v configoch z archívu)."""
    try:
        _entry, path = latest_local_archive_entry(history)
        old_ssh = migration_old_ssh()
        if not path or not old_ssh:
            return None
        guest = find_app_guest(read_archive_facts(path), detect_own_ip(old_ssh['host'], old_ssh['port']))
        return guest['vmid'] if guest else None
    except (OSError, ValueError, RuntimeError, tarfile.TarError):
        return None

def remember_migration_guests(state, guests):
    for guest in guests:
        state['guests'].setdefault(str(guest['vmid']), {
            key: guest[key] for key in ('type', 'name', 'status', 'note', 'updated_at')
        })

MIGRATION_COMPARE_MAX_BYTES = 64 * 1024
MIGRATION_COMPARE_COMMAND_TIMEOUT = 10
MIGRATION_COMPARE_HOST_TIMEOUT = 60

class MigrationCompareReadError(ValueError):
    """Iba vlastné chyby s bezpečným textom pre používateľa."""

def validate_migration_ssh_host(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('IP alebo hostname nového hosta je povinný.')
    value = value.strip()
    try:
        return validate_migration_host({'ip': value})['ip']
    except ValueError:
        if ':' in value or re.fullmatch(r'[0-9.]+', value):
            raise ValueError('Neplatná IP adresa nového hosta.') from None
        return validate_migration_host({'hostname': value})['hostname']

def run_migration_compare_command(client, command, timeout):
    """Ohraničené čítanie oboch SSH streamov; žiadny stderr/exception do API."""
    channel = None
    deadline = time.monotonic() + timeout
    output = bytearray()
    received = 0
    try:
        stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
        channel = stdout.channel
        if stdin is not None:
            stdin.close()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            for ready, read, keep in ((channel.recv_ready, channel.recv, True),
                                      (channel.recv_stderr_ready, channel.recv_stderr, False)):
                if ready():
                    chunk = read(4096)
                    received += len(chunk)
                    if received > MIGRATION_COMPARE_MAX_BYTES:
                        raise MigrationCompareReadError('Výstup prekročil bezpečný limit; kontrola nie je úplná.')
                    if keep:
                        output.extend(chunk)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                status = channel.recv_exit_status()
                if status != 0:
                    raise MigrationCompareReadError(f'Príkaz zlyhal (exit {int(status)}); skontroluj ho ručne.')
                return output.decode('utf-8', errors='replace')
            time.sleep(0.01)
    finally:
        if channel is not None:
            channel.close()

def parse_migration_network(text):
    """Iba bridge/VLAN atribúty; hooky, heslá a iné riadky sa do výstupu nedostanú."""
    sections = {}
    current = None
    includes = False
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] in ('source', 'source-directory'):
            includes = True
            current = None
        elif parts[0] == 'iface' and len(parts) >= 4:
            current = sections.setdefault(parts[1], {})
        elif parts[0] in ('auto', 'allow-hotplug', 'mapping'):
            current = None
        elif current is not None and parts[0] in (
                'bridge-ports', 'bridge-vlan-aware', 'bridge-vids', 'vlan-raw-device', 'vlan-id'):
            current[parts[0]] = ' '.join(parts[1:])
    bridges, vlans = [], []
    for name, props in sorted(sections.items()):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', name):
            continue
        if name.startswith('vmbr') and '.' not in name or 'bridge-ports' in props:
            bridges.append({'name': name, 'ports': props.get('bridge-ports', '').split(),
                            'vlan_aware': props.get('bridge-vlan-aware', ''), 'vids': props.get('bridge-vids', '')})
        if '.' in name or 'vlan-raw-device' in props or 'vlan-id' in props:
            base, _sep, tag = name.rpartition('.')
            vlans.append({'name': name, 'raw_device': props.get('vlan-raw-device', base),
                          'vlan_id': props.get('vlan-id', tag if tag.isdigit() else '')})
    return {'bridges': bridges, 'vlans': vlans, 'includes': includes}

def parse_migration_compare_output(facts, command_id, text):
    lines = text.strip().splitlines()
    if command_id == 'hostname':
        name = text.strip()
        if not name:
            raise ValueError()
        facts['hostname'] = validate_migration_host({'hostname': name})['hostname']
    elif command_id == 'version':
        match = re.search(r'(?:^pve-manager:\s*|^pve-manager/)([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[+~.-][A-Za-z0-9.-]+)?)(?=\s|/|$)', text, re.MULTILINE)
        if not match:
            raise ValueError()
        facts['pve_version'] = match.group(1)
    elif command_id == 'storage':
        if not lines or not re.match(r'^Name\s+Type\s+Status\b', lines[0]):
            raise ValueError()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 3:
                raise ValueError()
            facts['storage'].append({'id': parts[0], 'type': parts[1], 'status': parts[2]})
    elif command_id in ('links', 'addresses'):
        if not lines:
            raise ValueError()
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                raise ValueError()
            name = parts[0].split('@')[0]
            item = {'name': name, 'state': parts[1]}
            if command_id == 'addresses':
                item['addresses'] = []
                for address in parts[2:]:
                    try:
                        item['addresses'].append(str(ipaddress.ip_interface(address)))
                    except ValueError:
                        raise ValueError() from None
            facts[command_id].append(item)
    elif command_id == 'network':
        if not re.search(r'^\s*iface\s+\S+\s+inet6?\s+', text, re.MULTILINE):
            raise ValueError()
        facts['network'] = parse_migration_network(text)
    elif command_id == 'cpu':
        vendor = re.search(r'^Vendor ID:\s*(\S+)', text, re.MULTILINE)
        flags = re.search(r'^(?:Flags|Features):\s*(.*)', text, re.MULTILINE)
        if not vendor:
            raise ValueError()
        facts['cpu'] = {'vendor': vendor.group(1), 'flags': sorted(set(flags.group(1).split())) if flags else []}
    elif command_id in ('qm', 'pct'):
        if not lines or not re.match(r'^\s*VMID\s+', lines[0]):
            raise ValueError()
        guest_type = 'VM' if command_id == 'qm' else 'LXC'
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 3:
                raise ValueError()
            vmid = int(validate_migration_vmid(parts[0]))
            status = parts[2] if guest_type == 'VM' else parts[1]
            if status not in ('running', 'stopped', 'paused'):
                raise ValueError()
            facts['guests'].append({'vmid': vmid, 'type': guest_type,
                                    'name': parts[1] if guest_type == 'VM' else parts[-1], 'status': status})
    elif command_id == 'timers':
        for line in lines:
            if not line.strip() or re.match(r'^\d+ timers? listed\.', line.strip()):
                continue
            match = re.search(r'\b([A-Za-z0-9_.@:-]+\.timer)\s+([A-Za-z0-9_.@:-]+)\s*$', line)
            if not match:
                raise ValueError()
            facts['timers'].append({'unit': match.group(1), 'activates': match.group(2)})

def collect_migration_host_facts(ssh_config):
    result = {
        'host': ssh_config['host'], 'port': ssh_config['port'], 'username': ssh_config['username'],
        'connected': False, 'complete': False, 'errors': [],
        'facts': {'hostname': '', 'pve_version': '', 'storage': [], 'links': [], 'addresses': [],
                  'network': {'bridges': [], 'vlans': [], 'includes': False},
                  'cpu': {'vendor': '', 'flags': []}, 'guests': [], 'timers': []},
    }
    client = None
    try:
        client = SSH_CLIENT_FACTORY()
        client.connect(hostname=ssh_config['host'], port=ssh_config['port'],
                       username=ssh_config['username'], password=ssh_config['password'],
                       timeout=10, banner_timeout=10, auth_timeout=10,
                       look_for_keys=False, allow_agent=False)
        result['connected'] = True
        deadline = time.monotonic() + MIGRATION_COMPARE_HOST_TIMEOUT
        for spec in MIGRATION_COMPARE_COMMANDS:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError()
                text = run_migration_compare_command(client, spec['command'],
                                                     min(MIGRATION_COMPARE_COMMAND_TIMEOUT, remaining))
            except MigrationCompareReadError as exc:
                message = str(exc)  # iba vlastné bezpečné chyby readera, nie SSH stderr
            except TimeoutError:
                message = 'Načítanie prekročilo časový limit; over príkaz ručne.'
            except Exception:
                message = 'Príkaz sa nepodarilo načítať; over ho ručne cez SSH.'
            else:
                try:
                    parse_migration_compare_output(result['facts'], spec['id'], text)
                except (ValueError, TypeError):
                    message = 'Výstup má neznámy alebo neúplný formát; over ho ručne.'
                else:
                    continue
            result['errors'].append({'command_id': spec['id'], 'title': spec['title'], 'message': message})
        result['complete'] = not result['errors']
    except Exception:
        result['errors'].append({'command_id': 'connection', 'title': 'SSH pripojenie',
                                 'message': 'SSH pripojenie zlyhalo. Over cieľ, port a prihlasovacie údaje.'})
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return result

def build_migration_comparison(old, new):
    rows = []
    a, b = old['facts'], new['facts']
    def row(row_id, label, commands, old_value, new_value, level, detail):
        errors = {e['command_id'] for host in (old, new) for e in host['errors']}
        if not old['connected'] or not new['connected'] or errors.intersection(commands):
            level, detail = 'unknown', 'Kontrola nie je úplná: niektorý SSH príkaz zlyhal alebo mal neznámy výstup.'
        rows.append({'id': row_id, 'label': label, 'level': level,
                     'old_value': old_value, 'new_value': new_value, 'detail': detail})
    def names(values, key='name'):
        return ', '.join(sorted(v[key] for v in values)) or 'žiadne'
    def network(values):
        return '\n'.join(f"{v['name']}: porty {', '.join(v['ports']) or 'none'}, VLAN-aware {v['vlan_aware'] or 'neurčené'}, vids {v['vids'] or 'neurčené'}" for v in values) or 'žiadne'
    def vlan(values):
        return '\n'.join(f"{v['name']}: {v['raw_device']} / VLAN {v['vlan_id'] or 'neurčené'}" for v in values) or 'žiadne'
    same_target = old['host'] == new['host'] and old['port'] == new['port']
    same_name = bool(a['hostname'] and b['hostname'] and a['hostname'].split('.')[0] == b['hostname'].split('.')[0])
    # Pri prenose 1:1 má nový host zámerne rovnaký názov nodu; kolízia je iba rovnaká adresa.
    row('identity', 'Identita hostov', ['hostname'], a['hostname'], b['hostname'],
        'error' if same_target else ('info' if same_name else 'ok'),
        'Porovnávaš ten istý SSH cieľ. Zadaj adresu nového hosta.' if same_target else (
            'Rovnaký názov nodu – očakávané pri prenose konfigurácie 1:1. Hosty rozlišuje rôzna IP; kontrola nového hosta overí aj /etc/machine-id.'
            if same_name else 'Hosty majú odlišné hostname. Prenos config.db 1:1 vyžaduje rovnaký názov nodu.'))
    av, bv = a['pve_version'], b['pve_version']
    amajor, bmajor = int(av.split('.')[0]) if av else 0, int(bv.split('.')[0]) if bv else 0
    version_level = 'error' if bmajor < amajor else ('warning' if bmajor != amajor else ('info' if av != bv else 'ok'))
    row('pve-version', 'Verzia PVE', ['version'], av, bv, version_level,
        'Nový host má staršiu major verziu; pred obnovou over kompatibilitu.' if bmajor < amajor else
        'Major verzie sa líšia; over podporovaný postup a kompatibilitu hostí.' if bmajor != amajor else
        'Verzie sa líšia.' if av != bv else 'Verzie PVE sa zhodujú.')
    ast, bst = {s['id'] for s in a['storage']}, {s['id'] for s in b['storage']}
    missing = sorted(ast - bst)
    row('storage-ids', 'Storage ID', ['storage'], names(a['storage'], 'id'), names(b['storage'], 'id'),
        'error' if missing else ('info' if ast != bst else 'ok'),
        'Na novom chýbajú storage ID: ' + ', '.join(missing) if missing else 'Všetky pôvodné storage ID sú na novom hoste.')
    inactive = [s['id'] for s in b['storage'] if s['id'] in ast and s['status'] != 'active']
    row('storage-status', 'Dostupnosť storage', ['storage'], '\n'.join(f"{s['id']}: {s['status']} ({s['type']})" for s in a['storage']),
        '\n'.join(f"{s['id']}: {s['status']} ({s['type']})" for s in b['storage']), 'warning' if inactive else 'info',
        'Na novom nie sú aktívne: ' + ', '.join(inactive) if inactive else 'Over aj obsah a mapovanie storage; rovnaké ID nezaručuje rovnaké dáta.')
    abr, bbr = a['network']['bridges'], b['network']['bridges']
    missing_br = {v['name'] for v in abr} - {v['name'] for v in bbr}
    partial_network = a['network']['includes'] or b['network']['includes']
    row('bridges', 'Bridge a VLAN rozsahy', ['network'], network(abr), network(bbr),
        'unknown' if partial_network else ('error' if missing_br else ('warning' if abr != bbr else 'ok')),
        'Konfigurácia zahŕňa source/source-directory; zahrnuté súbory nie sú načítané.' if partial_network else
        'Na novom chýbajú bridge: ' + ', '.join(sorted(missing_br)) if missing_br else
        'Nastavenia bridge sa líšia; over NIC, VLAN-aware a bridge-vids.' if abr != bbr else 'Definície bridge sa zhodujú.')
    avlan, bvlan = a['network']['vlans'], b['network']['vlans']
    row('vlans', 'VLAN rozhrania', ['network'], vlan(avlan), vlan(bvlan),
        'unknown' if partial_network else ('warning' if avlan != bvlan else 'ok'),
        'Zahrnuté sieťové súbory nie sú načítané; VLAN over ručne.' if partial_network else
        'VLAN rozhrania sa líšia; over tagy a nadradené bridge.' if avlan != bvlan else 'Definície VLAN sa zhodujú.')
    acpu, bcpu = a['cpu'], b['cpu']
    row('cpu-vendor', 'CPU vendor', ['cpu'], acpu['vendor'], bcpu['vendor'],
        'warning' if acpu['vendor'] != bcpu['vendor'] else 'ok',
        'CPU vendor sa mení (napr. Intel ↔ AMD); over CPU typ VM a passthrough.' if acpu['vendor'] != bcpu['vendor'] else 'CPU vendor sa zhoduje.')
    flags = sorted(set(acpu['flags']) - set(bcpu['flags']))
    row('cpu-flags', 'CPU flags', ['cpu'], ' '.join(acpu['flags']), ' '.join(bcpu['flags']),
        'unknown' if not acpu['flags'] or not bcpu['flags'] else ('warning' if flags else 'ok'),
        'CPU flags nie sú dostupné; kompatibilitu over ručne.' if not acpu['flags'] or not bcpu['flags'] else
        'Na novom chýbajú: ' + ', '.join(flags) if flags else 'Nový CPU obsahuje všetky zistené pôvodné flags; CPU model VM ešte over ručne.')
    running_a = {g['vmid'] for g in a['guests'] if g['status'] == 'running'}
    running_b = {g['vmid'] for g in b['guests'] if g['status'] == 'running'}
    duplicate = sorted(running_a & running_b)
    # Potvrdený konflikt sa musí zobraziť aj keď druhý inventár/príkaz zlyhal.
    row('guests-running', 'Súbežný beh hostí', [] if duplicate else ['qm', 'pct'],
        ', '.join(map(str, sorted(running_a))) or 'žiadni', ', '.join(map(str, sorted(running_b))) or 'žiadni',
        'error' if duplicate else 'ok', 'Rovnaké VMID beží na oboch hostoch: ' + ', '.join(map(str, duplicate)) + '. Over identitu a zabráň dvojitému behu.' if duplicate else
        'V okamihu kontroly nebolo zistené rovnaké bežiace VMID; zber nie je simultánny a názvy/IP hostí ešte over ručne.')
    guest_text = lambda values: '\n'.join(f"{g['type']} {g['vmid']} {g['name']}: {g['status']}" for g in values) or 'žiadni'
    row('guests-inventory', 'Inventár VM/LXC', ['qm', 'pct'], guest_text(a['guests']), guest_text(b['guests']), 'info',
        'Rozdiely sú počas presunu očakávané; tento snapshot nemení uložené stavy sprievodcu.')
    ta, tb = {t['unit'] for t in a['timers']}, {t['unit'] for t in b['timers']}
    common = sorted(t for t in ta & tb if 'backup' in t.lower() or 'vzdump' in t.lower())
    row('backup-timers', 'Systemd timery', ['timers'], names(a['timers'], 'unit'), names(b['timers'], 'unit'),
        'warning' if common else 'info', 'Backup timery sú na oboch: ' + ', '.join(common) + '. Over, že zálohujú iba na jednom hoste.' if common else
        'Over vlastné backup timery aj vzdump joby ručne. Zoznam timerov nedokazuje, že nezapisujú do spoločného NAS.')
    row('interfaces', 'Sieťové rozhrania', ['links'], names(a['links']), names(b['links']), 'info',
        'Nový HW môže mať iné názvy NIC; over bridge-ports z konzoly.')
    def addresses(values):
        return '\n'.join(f"{v['name']} ({v['state']}): {', '.join(v['addresses']) or 'bez IP'}" for v in values)
    def routable(values):
        ips = {ipaddress.ip_interface(ip).ip for v in values if v['name'] != 'lo' for ip in v['addresses']}
        return {ip for ip in ips if not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified}
    common_ip = sorted(str(ip) for ip in routable(a['addresses']) & routable(b['addresses']))
    row('addresses', 'IP adresy hostov', ['addresses'], addresses(a['addresses']), addresses(b['addresses']),
        'error' if common_ip else 'info', 'Rovnaké IP na oboch hostoch: ' + ', '.join(common_ip) + '. Over kolíziu.' if common_ip else
        'Na novom používaj pri súbehu dočasnú IP. Pri cutover starý host najprv vypni alebo odpoj.')
    return rows


# ---------------------------------------------------------------------------
# Migrácia – pripojenie nového hosta a operácie na pozadí (fáza 2)
# ---------------------------------------------------------------------------
# Prihlasovacie údaje nového hosta sú v samostatnom súbore (0600), nikdy v migration_state.json
# a nikdy v API odpovediach. Zmažú sa pri resete migrácie, pri dokončení sa zmaže heslo.

MIGRATION_JOB_HEARTBEAT_SECONDS = 20
MIGRATION_JOB_STALE_SECONDS = 180
MIGRATION_JOB_LOG_LIMIT = 300
MIGRATION_JOBS_KEEP = 20
MIGRATION_JOB_THREADS = {}
PVE_MANAGER_VERSION = re.compile(r'pve-manager/(\d+)\.(\d+)(?:\.(\d+))?')

def load_migration_target():
    if not os.path.exists(MIGRATION_TARGET_FILE):
        return None
    try:
        with open(MIGRATION_TARGET_FILE, encoding='utf-8') as handle:
            target = json.load(handle)
        if (not isinstance(target, dict)
                or set(target) != {'host', 'port', 'username', 'password', 'saved_at', 'last_check'}
                or not isinstance(target['host'], str) or not target['host']
                or type(target['port']) is not int or not 1 <= target['port'] <= 65535
                or target['username'] != 'root' or not isinstance(target['password'], str)
                or not isinstance(target['saved_at'], str)
                or not isinstance(target['last_check'], (dict, type(None)))):
            raise ValueError()
        return target
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('Údaje nového hosta sú poškodené. Zabudni pripojenie a zadaj ho znova.') from None

def forget_migration_target(keep_host=False):
    """Zmaže pripojenie nového hosta; keep_host ponechá IP a výsledok kontroly, zmaže iba heslo."""
    with json_state_lock(MIGRATION_TARGET_FILE):
        if not os.path.exists(MIGRATION_TARGET_FILE):
            return
        if keep_host:
            try:
                target = load_migration_target()
            except RuntimeError:
                target = None
            if target:
                target['password'] = ''
                write_json_state(MIGRATION_TARGET_FILE, target)
                return
        os.unlink(MIGRATION_TARGET_FILE)

def public_migration_target(target):
    if not target:
        return None
    return {
        'host': target['host'], 'port': target['port'], 'username': target['username'],
        'has_password': bool(target['password']), 'saved_at': target['saved_at'],
        'last_check': target['last_check'],
    }

def safe_public_migration_target():
    try:
        return public_migration_target(load_migration_target()), None
    except (OSError, RuntimeError) as exc:
        return None, str(exc) if isinstance(exc, RuntimeError) else 'Údaje nového hosta sa nedajú načítať.'

def migration_old_ssh():
    """SSH cieľ starého hosta z Nastavení (iba remote_ssh s kompletnými údajmi)."""
    source = sanitize_source_config(load_config().get('source_config'))
    if source['mode'] != 'remote_ssh':
        return None
    try:
        RemoteSshBackupSource(source).validate()
    except ValueError:
        return None
    return dict(source['ssh'])

class MigrationHost:
    """Krátkodobé SSH spojenie na hosta migrácie; príkazy iba z pevných builderov v kóde."""

    def __init__(self, ssh_config):
        self.ssh_config = ssh_config
        self.client = None

    def __enter__(self):
        self.client = SSH_CLIENT_FACTORY()
        self.client.connect(hostname=self.ssh_config['host'], port=self.ssh_config['port'],
                            username=self.ssh_config.get('username') or 'root',
                            password=self.ssh_config['password'],
                            timeout=10, banner_timeout=10, auth_timeout=10,
                            look_for_keys=False, allow_agent=False)
        return self

    def run(self, command, timeout=MIGRATION_COMPARE_COMMAND_TIMEOUT):
        return run_migration_compare_command(self.client, command, timeout)

    def try_run(self, command, timeout=MIGRATION_COMPARE_COMMAND_TIMEOUT):
        try:
            return self.run(command, timeout), ''
        except MigrationCompareReadError as exc:
            return None, str(exc)
        except TimeoutError:
            return None, 'Príkaz prekročil časový limit.'
        except Exception:
            return None, 'Príkaz sa nepodarilo vykonať.'

    def __exit__(self, *exc):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        return False

def redact_secrets(text, secrets):
    text = str(text)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, '[skryté]')
    return text

def load_migration_jobs():
    if not os.path.exists(MIGRATION_JOBS_FILE):
        return []
    try:
        with open(MIGRATION_JOBS_FILE, encoding='utf-8') as handle:
            jobs = json.load(handle)
        if not isinstance(jobs, list) or not all(isinstance(job, dict) and isinstance(job.get('id'), str) for job in jobs):
            raise ValueError()
        return jobs
    except (ValueError, TypeError):
        raise RuntimeError('Záznam operácií migrácie je poškodený.') from None

def parse_job_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

def mark_stale_migration_jobs(jobs):
    """Bežiaca operácia bez heartbeatu (reštart služby) sa označí ako prerušená."""
    now = datetime.now(ZoneInfo('Europe/Bratislava'))
    for job in jobs:
        if job.get('status') != 'running':
            continue
        heartbeat = parse_job_time(job.get('heartbeat_at'))
        thread = MIGRATION_JOB_THREADS.get(job['id'])
        alive_here = thread is not None and thread.is_alive()
        if not alive_here and (heartbeat is None or (now - heartbeat).total_seconds() > MIGRATION_JOB_STALE_SECONDS):
            job.update(status='interrupted', finished_at=migration_now(),
                       error='Operácia bola prerušená (napr. reštart služby). Skontroluj stav ručne a spusti ju znova.')
    return jobs

def list_migration_jobs():
    with json_state_lock(MIGRATION_JOBS_FILE):
        jobs = mark_stale_migration_jobs(load_migration_jobs())
        if jobs:
            write_json_state(MIGRATION_JOBS_FILE, jobs)
    return list(reversed(jobs))

def running_migration_job():
    return next((job for job in list_migration_jobs() if job.get('status') == 'running'), None)

def update_migration_job(job_id, mutate):
    with json_state_lock(MIGRATION_JOBS_FILE):
        jobs = load_migration_jobs()
        for job in jobs:
            if job['id'] == job_id:
                mutate(job)
                job['heartbeat_at'] = migration_now()
                break
        write_json_state(MIGRATION_JOBS_FILE, jobs)

class MigrationJobContext:
    def __init__(self, job_id, secrets):
        self.job_id = job_id
        self.secrets = list(secrets)

    def log(self, message):
        line = f'{datetime.now(ZoneInfo("Europe/Bratislava")).strftime("%H:%M:%S")} {redact_secrets(message, self.secrets)[:500]}'
        def mutate(job):
            job['log'] = (job.get('log') or [])[-(MIGRATION_JOB_LOG_LIMIT - 1):] + [line]
        update_migration_job(self.job_id, mutate)

    def progress(self, text):
        update_migration_job(self.job_id, lambda job: job.update(progress=redact_secrets(text, self.secrets)[:200]))

    def heartbeat(self):
        update_migration_job(self.job_id, lambda job: None)

def run_migration_job(job_id, func, secrets):
    ctx = MigrationJobContext(job_id, secrets)
    stop = threading.Event()

    def beat():
        while not stop.wait(MIGRATION_JOB_HEARTBEAT_SECONDS):
            try:
                ctx.heartbeat()
            except Exception:
                pass

    threading.Thread(target=beat, daemon=True).start()
    try:
        result = func(ctx)
        update_migration_job(job_id, lambda job: job.update(status='success', finished_at=migration_now(), result=result))
    except (ValueError, RuntimeError, MigrationCompareReadError) as exc:
        message = redact_secrets(exc, secrets)
        update_migration_job(job_id, lambda job: job.update(status='failed', finished_at=migration_now(), error=message))
    except Exception:
        app.logger.exception('Migration job %s failed', job_id)
        update_migration_job(job_id, lambda job: job.update(
            status='failed', finished_at=migration_now(), error='Neočakávaná chyba operácie; podrobnosti sú v logu služby.'))
    finally:
        stop.set()
        MIGRATION_JOB_THREADS.pop(job_id, None)

def start_migration_job(kind, title, func, secrets=()):
    """Spustí operáciu na pozadí; súčasne smie bežať iba jedna."""
    with json_state_lock(MIGRATION_JOBS_FILE):
        jobs = mark_stale_migration_jobs(load_migration_jobs())
        running = next((job for job in jobs if job.get('status') == 'running'), None)
        if running:
            raise ValueError(f'Už beží operácia „{running.get("title")}“. Počkaj na jej dokončenie.')
        now = migration_now()
        job = {'id': f'{int(time.time() * 1000)}-{secrets_token()}', 'kind': kind, 'title': title,
               'status': 'running', 'started_at': now, 'finished_at': None, 'heartbeat_at': now,
               'progress': '', 'log': [], 'result': None, 'error': None}
        jobs = (jobs + [job])[-MIGRATION_JOBS_KEEP:]
        write_json_state(MIGRATION_JOBS_FILE, jobs)
    thread = threading.Thread(target=run_migration_job, args=(job['id'], func, list(secrets)), daemon=True)
    MIGRATION_JOB_THREADS[job['id']] = thread
    thread.start()
    return job

def secrets_token():
    return secrets.token_hex(4)

def pve_version_tuple(text):
    match = PVE_MANAGER_VERSION.search(text or '')
    return tuple(int(part) for part in match.groups() if part is not None) if match else None

def parse_guest_status_lists(qm_text, pct_text):
    """(VMID, typ, názov, stav) z `qm list` a `pct list`."""
    guests = []
    for line in (qm_text or '').splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            guests.append({'vmid': int(parts[0]), 'type': 'VM', 'name': parts[1] if len(parts) > 1 else '',
                           'status': parts[2] if len(parts) > 2 else ''})
    for line in (pct_text or '').splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            guests.append({'vmid': int(parts[0]), 'type': 'LXC', 'name': parts[-1] if len(parts) > 2 else '',
                           'status': parts[1] if len(parts) > 1 else ''})
    return guests

def run_migration_preflight(ctx, target, old_ssh):
    """Read-only kontrola nového hosta pred prenosom konfigurácie a hostí."""
    checks = []
    facts = {'hostname': '', 'pve_version': '', 'guests': [], 'running_guests': [], 'links': []}

    def add(check_id, level, title, detail):
        checks.append({'id': check_id, 'level': level, 'title': title, 'detail': detail})
        ctx.log(f'[{level}] {title}: {detail}')

    old_ids = {}
    if old_ssh:
        ctx.progress('Načítavam starý host')
        try:
            with MigrationHost(old_ssh) as old:
                machine, _err = old.try_run('cat /etc/machine-id')
                version, _err = old.try_run('LC_ALL=C pveversion')
                old_ids = {'machine_id': (machine or '').strip(), 'version': pve_version_tuple(version)}
        except Exception:
            add('old-host', 'warning', 'Starý host', 'Starý host sa nepodarilo načítať; porovnanie identity a verzie je neúplné.')
    else:
        add('old-host', 'warning', 'Starý host', 'V Nastaveniach nie je kompletný Remote SSH cieľ starého hosta.')

    ctx.progress('Pripájam sa na nový host')
    try:
        with MigrationHost(target) as host:
            add('ssh', 'ok', 'SSH pripojenie', f"root@{target['host']}:{target['port']} – prihlásenie úspešné")
            hostname, _err = host.try_run('LC_ALL=C hostname')
            version_text, version_err = host.try_run('LC_ALL=C pveversion')
            machine, machine_err = host.try_run('cat /etc/machine-id')
            cluster, _err = host.try_run('systemctl is-active pve-cluster || true')
            qm_text, qm_err = host.try_run('LC_ALL=C qm list')
            pct_text, pct_err = host.try_run('LC_ALL=C pct list')
            links_text, _err = host.try_run('LC_ALL=C ip -br link')
    except Exception:
        add('ssh', 'error', 'SSH pripojenie', 'Prihlásenie na nový host zlyhalo. Over IP, port a root heslo.')
        return {'ok': False, 'checks': checks, 'facts': facts, 'checked_at': migration_now()}

    facts['hostname'] = (hostname or '').strip()
    facts['links'] = new_physical_nics(links_text)
    new_version = pve_version_tuple(version_text)
    if new_version:
        facts['pve_version'] = '.'.join(str(part) for part in new_version)
        add('pve', 'ok', 'Proxmox VE', f"Na novom hoste beží Proxmox VE {facts['pve_version']}.")
    else:
        add('pve', 'error', 'Proxmox VE', 'Na novom hoste sa nenašiel Proxmox VE (pveversion). ' + (version_err or ''))

    new_machine = (machine or '').strip()
    if new_machine and old_ids.get('machine_id'):
        if new_machine == old_ids['machine_id']:
            add('identity', 'error', 'Iný server ako starý', 'Nový cieľ má rovnaké /etc/machine-id ako starý host – je to ten istý server.')
        else:
            add('identity', 'ok', 'Iný server ako starý', 'Nový host je iný stroj ako starý (rôzne /etc/machine-id).')
    else:
        add('identity', 'unknown', 'Iný server ako starý', 'Identitu sa nepodarilo porovnať. ' + (machine_err or ''))

    if old_ssh and old_ssh['host'] == target['host']:
        add('address', 'error', 'Iná adresa', 'Nový host má rovnakú adresu ako starý. Pri súbehu musí mať dočasnú inú IP.')
    else:
        add('address', 'ok', 'Iná adresa', f"Nový host {target['host']} má inú adresu ako starý.")

    if (cluster or '').strip() == 'active':
        add('pve-cluster', 'ok', 'pve-cluster', 'Služba pve-cluster beží.')
    else:
        add('pve-cluster', 'error', 'pve-cluster', f"Služba pve-cluster nebeží (stav: {(cluster or 'neznámy').strip() or 'neznámy'}).")

    if qm_text is None or pct_text is None:
        add('guests', 'unknown', 'Hostia na novom hoste', 'Zoznam VM/LXC sa nepodarilo načítať. ' + (qm_err or pct_err))
    else:
        guests = parse_guest_status_lists(qm_text, pct_text)
        facts['guests'] = guests
        facts['running_guests'] = [guest for guest in guests if guest['status'] == 'running']
        if facts['running_guests']:
            names = ', '.join(f"{g['type']} {g['vmid']} {g['name']}".strip() for g in facts['running_guests'])
            add('guests', 'error', 'Hostia na novom hoste', f'Na novom hoste bežia hostia ({names}). Pred prenosom konfigurácie musí byť nový host bez bežiacich hostí.')
        elif guests:
            names = ', '.join(f"{g['type']} {g['vmid']}" for g in guests)
            add('guests', 'warning', 'Hostia na novom hoste', f'Na novom hoste už existujú hostia ({names}). Prenos konfigurácie 1:1 by ich definície prepísal.')
        else:
            add('guests', 'ok', 'Hostia na novom hoste', 'Nový host je prázdny – bez VM a LXC.')

    old_version = old_ids.get('version')
    if new_version and old_version:
        if new_version[0] < old_version[0] or (new_version[0] == old_version[0] and new_version[1] < old_version[1]):
            add('version', 'error' if new_version[0] < old_version[0] else 'warning', 'Verzia PVE',
                f"Nový host má staršiu verziu ({facts['pve_version']}) ako starý ({'.'.join(map(str, old_version))}). Najprv ho aktualizuj.")
        elif new_version[0] > old_version[0]:
            add('version', 'warning', 'Verzia PVE', f"Nový host má vyššiu major verziu ({facts['pve_version']}). Over kompatibilitu configov hostí.")
        else:
            add('version', 'ok', 'Verzia PVE', f"Rovnaká alebo novšia verzia ({facts['pve_version']}).")
    else:
        add('version', 'unknown', 'Verzia PVE', 'Verzie sa nepodarilo porovnať.')

    result = {'ok': not any(check['level'] == 'error' for check in checks), 'checks': checks,
              'facts': facts, 'checked_at': migration_now()}
    with json_state_lock(MIGRATION_TARGET_FILE):
        saved = load_migration_target()
        if saved and saved['host'] == target['host'] and saved['port'] == target['port']:
            saved['last_check'] = result
            write_json_state(MIGRATION_TARGET_FILE, saved)
    ctx.progress('Kontrola dokončená')
    return result

def start_migration_preflight(target):
    old_ssh = migration_old_ssh()
    secrets_list = [target['password'], (old_ssh or {}).get('password', '')]
    return start_migration_job('preflight', 'Kontrola nového hosta',
                               lambda ctx: run_migration_preflight(ctx, target, old_ssh), secrets_list)

def public_migration_job(job):
    return {key: job.get(key) for key in ('id', 'kind', 'title', 'status', 'started_at', 'finished_at',
                                          'heartbeat_at', 'progress', 'log', 'result', 'error')}

def no_store(response, code=200):
    response.headers['Cache-Control'] = 'no-store'
    return response, code


# ---------------------------------------------------------------------------
# Migrácia – prenos konfigurácie 1:1, presun hostí a prepnutie (fázy 3–5)
# ---------------------------------------------------------------------------
# Všetky príkazy na hostoch sú z pevných builderov s shlex.quote; API neprijíma ľubovoľné príkazy.

MIGRATION_WORKDIR = '/root/pbm-migration'
MIGRATION_SOURCE_MAX_AGE_HOURS = 24
MIGRATION_LONG_TIMEOUT = 24 * 3600
MIGRATION_FILE_ITEMS = [
    {'path': '/etc/auto.master', 'default': True},
    {'path': '/etc/auto.master.d', 'default': True},
    {'path': '/etc/auto.nfs', 'default': True},
    {'path': '/usr/local/sbin', 'default': True},
    {'path': '/usr/local/bin', 'default': True},
    {'path': '/etc/systemd/system', 'default': True,
     'note': 'Skopírované timery sa na novom hoste vypnú a zapnú až pri prepnutí.'},
    {'path': '/etc/sysctl.conf', 'default': True},
    {'path': '/etc/sysctl.d', 'default': True},
    {'path': '/etc/vzdump.conf', 'default': True},
    {'path': '/etc/modules', 'default': False, 'note': 'Viazané na HW – prenes iba po kontrole rozdielu.'},
    {'path': '/etc/modprobe.d', 'default': False, 'note': 'Viazané na HW (PCI ID, blacklisty) – iba po kontrole.'},
    {'path': '/etc/default', 'default': False, 'note': 'GRUB/IOMMU parametre sú viazané na HW.'},
    {'path': '/var/spool/cron', 'default': False, 'note': 'Cron joby začnú bežať hneď aj na novom hoste – radšej až pri prepnutí.'},
    {'path': '/root', 'default': False, 'note': 'Citlivé (kľúče, skripty); prenes iba ak potrebuješ.'},
]
MIGRATION_FILE_PATHS = {item['path'] for item in MIGRATION_FILE_ITEMS}
VZDUMP_ARCHIVE_PATTERN = re.compile(r"creating (?:vzdump )?archive '([^']+)'")
REMOTE_TREE_SCRIPT = (
    "import hashlib,json,os,sys\n"
    "p=sys.argv[1];out={}\n"
    "def h(f):\n"
    " d=hashlib.sha256()\n"
    " with open(f,'rb') as x:\n"
    "  for b in iter(lambda:x.read(65536),b''):d.update(b)\n"
    " return d.hexdigest()\n"
    "def add(rel,full):\n"
    " if os.path.islink(full):out[rel]='link:'+os.readlink(full)\n"
    " elif os.path.isfile(full):out[rel]=h(full)\n"
    "if os.path.isdir(p) and not os.path.islink(p):\n"
    " for root,dirs,files in os.walk(p):\n"
    "  for n in dirs+files:\n"
    "   full=os.path.join(root,n)\n"
    "   if n in dirs and not os.path.islink(full):continue\n"
    "   add(os.path.relpath(full,p),full)\n"
    "else:add('',p)\n"
    "print(json.dumps({'exists':os.path.lexists(p),'files':out}))\n"
)

def default_migration_transfer():
    return {'version': 1, 'config_db': None, 'files': None, 'network': None, 'guests': {}, 'cutover': None}

def load_migration_transfer():
    if not os.path.exists(MIGRATION_TRANSFER_FILE):
        return default_migration_transfer()
    try:
        with open(MIGRATION_TRANSFER_FILE, encoding='utf-8') as handle:
            transfer = json.load(handle)
        if not isinstance(transfer, dict) or set(transfer) != set(default_migration_transfer()) or transfer['version'] != 1 \
                or not isinstance(transfer['guests'], dict):
            raise ValueError()
        return transfer
    except (ValueError, TypeError):
        raise RuntimeError('Stav prenosu migrácie je poškodený. Resetuj migráciu.') from None

def update_migration_transfer(mutate):
    with json_state_lock(MIGRATION_TRANSFER_FILE):
        transfer = load_migration_transfer()
        mutate(transfer)
        write_json_state(MIGRATION_TRANSFER_FILE, transfer)
        return transfer

def forget_migration_transfer():
    with json_state_lock(MIGRATION_TRANSFER_FILE):
        if os.path.exists(MIGRATION_TRANSFER_FILE):
            os.unlink(MIGRATION_TRANSFER_FILE)

def safe_migration_transfer():
    try:
        return load_migration_transfer()
    except (OSError, RuntimeError):
        return None

def archive_member_bytes(archive_path, name, limit=256 * 1024 * 1024):
    with tarfile.open(archive_path, 'r:gz') as tar:
        try:
            member = tar.getmember(name)
        except KeyError:
            return None
        validate_tar_member(member)
        if not member.isfile() or member.size > limit:
            return None
        handle = tar.extractfile(member)
        return handle.read() if handle else None

def archive_tree_hashes(archive_path, path):
    """{relatívna cesta: sha256 | 'link:cieľ'} pre cestu v archíve (ako REMOTE_TREE_SCRIPT na hoste)."""
    arcname = archive_name_for_path(path)
    files = {}
    with tarfile.open(archive_path, 'r:gz') as tar:
        for member in tar.getmembers():
            validate_tar_member(member)
            name = member.name.rstrip('/')
            if name != arcname and not name.startswith(arcname + '/'):
                continue
            rel = '' if name == arcname else name[len(arcname) + 1:]
            if member.issym():
                files[rel] = 'link:' + member.linkname
            elif member.isfile():
                digest = hashlib.sha256()
                handle = tar.extractfile(member)
                for block in iter(lambda: handle.read(65536), b''):
                    digest.update(block)
                files[rel] = digest.hexdigest()
    return files

class MigrationContext:
    """Spoločné predpoklady operácií prenosu: archív, oba hosty, node, LXC s appkou."""

    def __init__(self, require_target=True, require_fresh=False):
        self.state = load_migration_state()
        if self.state['method'] != 'side_by_side':
            raise ValueError('Prenos konfigurácie a hostí je dostupný iba pri spôsobe „Nový host vedľa starého“.')
        self.target = load_migration_target()
        if require_target and (not self.target or not self.target['password']):
            raise ValueError('Najprv v kroku „Nový host“ ulož pripojenie nového hosta a spusti kontrolu.')
        self.old_ssh = migration_old_ssh()
        if not self.old_ssh:
            raise ValueError('V Nastaveniach chýba kompletný Remote SSH cieľ starého hosta.')
        if self.target and self.target['host'] == self.old_ssh['host']:
            raise ValueError('Nový host má rovnakú adresu ako starý.')
        history = visible_backup_history(load_config(), persist_pruned=False)
        self.entry, self.archive_path = latest_local_archive_entry(history)
        if not self.archive_path:
            raise ValueError('Chýba lokálny archív konfigurácie hosta. Vytvor čerstvú zálohu (krok Čerstvé zálohy).')
        self.archive_time = parse_backup_timestamp(self.entry)
        if require_fresh and (not self.archive_time or datetime.now() - self.archive_time > timedelta(hours=MIGRATION_SOURCE_MAX_AGE_HOURS)):
            raise ValueError(f'Najnovší archív je starší ako {MIGRATION_SOURCE_MAX_AGE_HOURS} h. Vytvor čerstvú zálohu hosta '
                             '(krok Čerstvé zálohy), aby kópia zodpovedala aktuálnemu stavu.')
        self.facts = read_archive_facts(self.archive_path)
        self.node = archive_node_name(self.facts)
        self.transfer = load_migration_transfer()
        self.app_guest = find_app_guest(self.facts, detect_own_ip(self.old_ssh['host'], self.old_ssh['port']))
        self.secrets = [self.old_ssh.get('password', ''), (self.target or {}).get('password', '')]

    def guest_info(self, vmid):
        for guest in parse_guest_lists(self.facts):
            if guest['vmid'] == vmid:
                return guest
        raise ValueError('Hosť s týmto VMID nie je v najnovšom archíve.')

def migration_default_dump_dir(facts):
    storages = {storage['id']: storage for storage in parse_backup_storages(facts)}
    for job in parse_vzdump_jobs(facts):
        storage = storages.get(job['storage'])
        if storage and storage['path']:
            return posixpath.join(storage['path'], 'dump')
    for storage in storages.values():
        if storage['path'] and storage['id'] != 'local':
            return posixpath.join(storage['path'], 'dump')
    return ''

def migration_guest_storages(facts):
    """Storage pre disky hostí (rootdir/images) z pôvodného storage.cfg."""
    result = []
    for section in parse_pve_section_config(facts['files'].get('etc/pve/storage.cfg', '')):
        content = section['props'].get('content', '')
        if 'images' in content or 'rootdir' in content:
            result.append(section['id'])
    return result or ['local-lvm']

def validate_remote_dir(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value or '\n' in value \
            or posixpath.normpath(value) != value.rstrip('/') or len(value) > 255:
        raise ValueError('Neplatný adresár záloh (absolútna cesta bez špeciálnych znakov).')
    return posixpath.normpath(value)

def validate_storage_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]{0,63}', value):
        raise ValueError('Neplatné ID storage.')
    return value

def migration_stream(host, command, ctx, timeout=MIGRATION_LONG_TIMEOUT, label=''):
    """Dlhý príkaz (vzdump/restore): priebežné riadky do logu, vráti (exit_code, posledné riadky)."""
    stdin, stdout, _stderr = host.client.exec_command(command, timeout=timeout)
    channel = stdout.channel
    if stdin is not None:
        try:
            stdin.close()
        except Exception:
            pass
    deadline = time.monotonic() + timeout
    buffer = ''
    tail = []
    last_log = 0.0
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            got = False
            for ready, read in ((channel.recv_ready, channel.recv), (channel.recv_stderr_ready, channel.recv_stderr)):
                if ready():
                    got = True
                    buffer += read(65536).decode('utf-8', errors='replace')
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue
                tail = (tail + [line])[-40:]
                important = ('error' in line.lower() or 'warn' in line.lower() or 'creating' in line.lower()
                             or 'finished' in line.lower() or 'total' in line.lower())
                if important or time.monotonic() - last_log > 15:
                    ctx.log(f'{label}{line}')
                    ctx.progress(f'{label}{line}')
                    last_log = time.monotonic()
            if not got and channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                if buffer.strip():
                    tail = (tail + [buffer.strip()])[-40:]
                return channel.recv_exit_status(), tail
            if not got:
                time.sleep(0.2)
    finally:
        channel.close()

def require_ok(host, command, message, timeout=MIGRATION_COMPARE_COMMAND_TIMEOUT):
    output, error = host.try_run(command, timeout)
    if output is None:
        raise RuntimeError(f'{message} ({error})')
    return output

def sftp_put_bytes(host, remote_path, data, mode=0o600):
    sftp = host.client.open_sftp()
    try:
        with sftp.file(remote_path, 'wb') as handle:
            handle.write(data)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()

def guest_cli(guest_type):
    return 'qm' if guest_type == 'VM' else 'pct'

def guest_status(host, guest_type, vmid):
    output, _err = host.try_run(f'{guest_cli(guest_type)} status {int(vmid)}')
    match = re.search(r'status:\s*(\w+)', output or '')
    return match.group(1) if match else 'unknown'

def onboot_guests_from_facts(facts, node):
    guests = []
    for name, text in facts['files'].items():
        match = GUEST_CONF_PATTERN.match(name)
        if match and match.group(1) == node and re.search(r'^onboot:\s*1\s*$', text.split('\n[', 1)[0], re.MULTILINE):
            guests.append({'vmid': int(match.group(3)), 'type': 'VM' if match.group(2) == 'qemu-server' else 'LXC'})
    return sorted(guests, key=lambda guest: guest['vmid'])

def firewall_enabled_in_archive(archive_path):
    data = archive_member_bytes(archive_path, 'etc/pve/firewall/cluster.fw', limit=1024 * 1024)
    if not data:
        return False
    options = data.decode('utf-8', errors='replace').split('[OPTIONS]', 1)
    if len(options) < 2:
        return False
    section = options[1].split('\n[', 1)[0]
    return bool(re.search(r'^\s*enable:\s*1\s*$', section, re.MULTILINE))

def set_migration_guest_status(vmid, status, note=None, force=False):
    with migration_state_lock():
        state = load_migration_state()
        guest = state['guests'].get(str(vmid))
        if not guest:
            return
        if not force and status != guest['status'] and status not in MIGRATION_GUEST_TRANSITIONS[guest['status']]:
            raise ValueError(f'Nepovolený prechod hosťa {vmid}: {guest["status"]} → {status}.')
        guest['status'] = status
        if note:
            guest['note'] = (f"{guest['note']}\n{note}" if guest['note'] else note)[-2000:]
        guest['updated_at'] = migration_now()
        state['updated_at'] = migration_now()
        save_migration_state(state)

# --- Fáza 3: config.db 1:1 -------------------------------------------------

def run_migration_config_db(ctx, mctx):
    archive_db = archive_member_bytes(mctx.archive_path, 'var/lib/pve-cluster/config.db')
    if not archive_db:
        raise ValueError('Archív neobsahuje /var/lib/pve-cluster/config.db. Zapni túto položku v zálohe a vytvor novú zálohu.')
    if not mctx.node:
        raise ValueError('Z archívu sa nepodarilo zistiť názov pôvodného nodu.')
    jobs = parse_vzdump_jobs(mctx.facts)
    enabled_jobs = [job['id'] for job in jobs if job['enabled']]
    onboot = onboot_guests_from_facts(mctx.facts, mctx.node)
    firewall = firewall_enabled_in_archive(mctx.archive_path)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = f'{MIGRATION_WORKDIR}/config.db.before-{stamp}'
    new_db = f'{MIGRATION_WORKDIR}/config.db.new'

    with MigrationHost(mctx.target) as host:
        ctx.progress('Kontrolujem nový host')
        hostname = require_ok(host, 'LC_ALL=C hostname', 'Hostname nového hosta sa nedá zistiť').strip().split('.')[0]
        if hostname != mctx.node:
            raise ValueError(f'Nový host sa volá „{hostname}“, pôvodný node „{mctx.node}“. Pre kópiu 1:1 nainštaluj nový '
                             f'Proxmox s hostname „{mctx.node}“ (s dočasnou IP), alebo použi postup Nový názov nodu z wiki.')
        if host.try_run('test -e /etc/pve/corosync.conf')[0] is not None:
            raise ValueError('Nový host je členom clustra. Prenos config.db je iba pre samostatný node.')
        guests = parse_guest_status_lists(require_ok(host, 'LC_ALL=C qm list', 'qm list zlyhal'),
                                          require_ok(host, 'LC_ALL=C pct list', 'pct list zlyhal'))
        if guests:
            raise ValueError('Nový host nie je prázdny (' + ', '.join(f"{g['type']} {g['vmid']}" for g in guests) +
                             '). Config.db sa prenáša iba na čistú inštaláciu – inak by sa prepísali existujúce definície.')
        if require_ok(host, 'systemctl is-active pve-cluster || true', 'Stav pve-cluster').strip() != 'active':
            raise ValueError('Na novom hoste nebeží pve-cluster.')

        ctx.log(f'Nahrávam config.db z archívu {mctx.entry.get("filename")} ({len(archive_db)} B)')
        require_ok(host, f'mkdir -p {MIGRATION_WORKDIR} && chmod 700 {MIGRATION_WORKDIR}', 'Pracovný adresár sa nedá vytvoriť')
        sftp_put_bytes(host, new_db, archive_db)
        check_script = ("import sqlite3,sys;c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
                        "print(c.execute('PRAGMA integrity_check').fetchone()[0])")
        integrity = require_ok(host, f'python3 -c {shlex.quote(check_script)} {shlex.quote(new_db)}', 'Kontrola integrity zlyhala').strip()
        if integrity != 'ok':
            raise ValueError(f'config.db z archívu neprešiel kontrolou integrity ({integrity[:200]}).')
        ctx.log('Integrita config.db: ok')
        require_ok(host, f'cp -a /var/lib/pve-cluster/config.db {shlex.quote(backup_path)}', 'Záloha pôvodného config.db zlyhala')
        ctx.log(f'Pôvodný config.db nového hosta zálohovaný: {backup_path}')

        ctx.progress('Nahrádzam config.db (pve-cluster je krátko zastavený)')
        require_ok(host, 'systemctl stop pve-firewall', 'pve-firewall sa nedá zastaviť', timeout=60)
        require_ok(host, 'systemctl stop pve-cluster', 'pve-cluster sa nedá zastaviť', timeout=120)
        try:
            require_ok(host, f'cp {shlex.quote(new_db)} /var/lib/pve-cluster/config.db && chown root:root /var/lib/pve-cluster/config.db '
                             '&& chmod 0600 /var/lib/pve-cluster/config.db', 'Nahradenie config.db zlyhalo')
            require_ok(host, 'systemctl start pve-cluster', 'pve-cluster sa po nahradení nespustil', timeout=120)
            for _attempt in range(15):
                if (host.try_run('systemctl is-active pve-cluster || true')[0] or '').strip() == 'active' \
                        and host.try_run(f'test -d /etc/pve/nodes/{shlex.quote(mctx.node)}')[0] is not None:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('pve-cluster po nahradení nie je aktívny alebo chýba adresár nodu.')
        except Exception:
            ctx.log('Chyba – vraciam pôvodný config.db nového hosta')
            host.try_run(f'systemctl stop pve-cluster; cp {shlex.quote(backup_path)} /var/lib/pve-cluster/config.db; '
                         'chmod 0600 /var/lib/pve-cluster/config.db; systemctl start pve-cluster; systemctl start pve-firewall', 180)
            raise
        ctx.log('config.db nahradený, pve-cluster beží')

        if firewall:
            require_ok(host, "sed -i 's/^enable: 1$/enable: 0/' /etc/pve/firewall/cluster.fw", 'Dočasné vypnutie firewallu zlyhalo')
            ctx.log('Firewall datacentra dočasne vypnutý (zapne sa po prepnutí)')
        host.try_run('systemctl start pve-firewall', 60)
        for job_id in enabled_jobs:
            output, error = host.try_run(f'pvesh set /cluster/backup/{shlex.quote(job_id)} --enabled 0', 60)
            ctx.log(f'Vzdump job {job_id} vypnutý' if output is not None else f'Vzdump job {job_id} sa nepodarilo vypnúť: {error}')
        for guest in onboot:
            output, error = host.try_run(f'{guest_cli(guest["type"])} set {guest["vmid"]} --onboot 0', 60)
            ctx.log(f'Autostart {guest["type"]} {guest["vmid"]} vypnutý' if output is not None
                    else f'Autostart {guest["type"]} {guest["vmid"]} sa nepodarilo vypnúť: {error}')
        host.try_run('pvecm updatecerts --force', 120)
        host.try_run('systemctl restart pvedaemon pveproxy pvestatd', 120)
        guests_after = parse_guest_status_lists(host.try_run('LC_ALL=C qm list')[0] or '', host.try_run('LC_ALL=C pct list')[0] or '')
        ctx.log(f'Na novom hoste sú teraz definície {len(guests_after)} hostí (bez diskov – presunú sa v ďalšom kroku)')

    result = {'applied_at': migration_now(), 'node': mctx.node, 'archive': mctx.entry.get('filename'),
              'backup_path': backup_path, 'jobs_disabled': enabled_jobs, 'firewall_was_enabled': firewall,
              'onboot_guests': onboot, 'guest_definitions': len(guests_after)}
    update_migration_transfer(lambda transfer: transfer.update(config_db=result))
    ctx.progress('Kópia PVE konfigurácie dokončená')
    return result

# --- Fáza 3: súbory hosta --------------------------------------------------

def run_migration_files_diff(ctx, mctx, paths):
    items = []
    with MigrationHost(mctx.target) as host:
        for path in paths:
            ctx.progress(f'Porovnávam {path}')
            profile = recovery_profile_for_path(path)
            backup_files = archive_tree_hashes(mctx.archive_path, path)
            output, error = host.try_run(f'python3 -c {shlex.quote(REMOTE_TREE_SCRIPT)} {shlex.quote(path)}', 60)
            item = {'path': path, 'in_backup': bool(backup_files), 'status': 'unknown', 'changed': [], 'only_backup': [],
                    'only_new': [], 'diff': '', 'error': ''}
            if output is None:
                item['error'] = error
            else:
                remote = json.loads(output)
                new_files = remote['files'] if remote['exists'] else {}
                item['changed'] = sorted(rel or path for rel in backup_files if rel in new_files and backup_files[rel] != new_files[rel])[:200]
                item['only_backup'] = sorted(rel or path for rel in backup_files if rel not in new_files)[:200]
                item['only_new'] = sorted(rel or path for rel in new_files if rel not in backup_files)[:200]
                if not backup_files:
                    item['status'] = 'not_in_backup'
                elif not remote['exists']:
                    item['status'] = 'missing_on_new'
                elif item['changed'] or item['only_backup']:
                    item['status'] = 'different'
                else:
                    item['status'] = 'same'
                text_diff_allowed = profile.get('sensitivity') == 'normal' and set(backup_files) == {''}
                if text_diff_allowed and item['status'] in ('different', 'missing_on_new'):
                    old_bytes = archive_member_bytes(mctx.archive_path, archive_name_for_path(path), limit=64 * 1024) or b''
                    new_text = ''
                    if remote['exists']:
                        new_text = host.try_run(f'head -c 65536 {shlex.quote(path)}')[0] or ''
                    diff = difflib.unified_diff(new_text.splitlines(), old_bytes.decode('utf-8', errors='replace').splitlines(),
                                                f'nový host: {path}', f'záloha: {path}', lineterm='')
                    item['diff'] = '\n'.join(list(diff)[:200])
            ctx.log(f'{path}: {item["status"]}')
            items.append(item)
    return {'items': items, 'archive': mctx.entry.get('filename'), 'generated_at': migration_now()}

def run_migration_files_apply(ctx, mctx, paths):
    ctx.progress('Prenášam súbory na nový host (pred prepisom sa zálohujú)')
    service = RemoteSshRestoreService({'mode': 'remote_ssh', 'ssh': {
        'host': mctx.target['host'], 'port': mctx.target['port'], 'username': 'root', 'password': mctx.target['password']}})
    available = {item['path'] for item in preview_restore_archive(mctx.archive_path)}
    missing = [path for path in paths if path not in available]
    if missing:
        raise ValueError(f'Archív neobsahuje: {", ".join(missing)}')
    result = service.restore(mctx.archive_path, paths, [])
    for item in result.get('applied', []):
        ctx.log(f'Prenesené: {item["path"]}')
    for item in result.get('skipped', []):
        ctx.log(f'Preskočené: {item["path"]} ({item.get("reason")})')
    timers_enabled, timers_disabled = [], []
    if '/etc/systemd/system' in paths:
        timers = sorted(posixpath.basename(name) for name in mctx.facts['members']
                        if name.startswith('etc/systemd/system/') and name.count('/') == 3 and name.endswith('.timer'))
        wanted = {posixpath.basename(name) for name in mctx.facts['members']
                  if name.startswith('etc/systemd/system/timers.target.wants/')}
        with MigrationHost(mctx.target) as host:
            host.try_run('systemctl daemon-reload', 60)
            for timer in timers:
                if timer in wanted:
                    timers_enabled.append(timer)
                output, _err = host.try_run(f'systemctl disable --now {shlex.quote(timer)}', 60)
                if output is not None:
                    timers_disabled.append(timer)
            ctx.log(f'Timery na novom hoste vypnuté do prepnutia: {", ".join(timers_disabled) or "žiadne"}')
    summary = {'applied_at': migration_now(), 'paths': [item['path'] for item in result.get('applied', [])],
               'skipped': result.get('skipped', []), 'backup_dir': result.get('backup_dir'),
               'timers_to_enable': timers_enabled}

    def mutate(transfer):
        previous = transfer['files'] or {}
        summary['paths'] = sorted(set(previous.get('paths', [])) | set(summary['paths']))
        summary['timers_to_enable'] = sorted(set(previous.get('timers_to_enable', [])) | set(timers_enabled))
        transfer['files'] = summary
    update_migration_transfer(mutate)
    return summary

# --- Fáza 3: návrh siete ---------------------------------------------------

PHYSICAL_NIC_PATTERN = re.compile(r'^(en|eth|eno|ens|enp|enx|wl)[a-z0-9]+$')

def old_physical_nics(facts):
    stanzas = parse_interfaces_stanzas(facts['files'].get('etc/network/interfaces', ''))
    names = set()
    for stanza in stanzas:
        if PHYSICAL_NIC_PATTERN.match(stanza['name']):
            names.add(stanza['name'])
        for key in ('bridge-ports', 'bond-slaves', 'slaves', 'bridge_ports'):
            for port in stanza['options'].get(key, '').split():
                if PHYSICAL_NIC_PATTERN.match(port):
                    names.add(port)
    return sorted(names)

def new_physical_nics(links_text):
    nics = []
    for line in (links_text or '').splitlines():
        parts = line.split()
        if parts and PHYSICAL_NIC_PATTERN.match(parts[0].split('@')[0]):
            nics.append({'name': parts[0].split('@')[0], 'state': parts[1] if len(parts) > 1 else '',
                         'mac': parts[2] if len(parts) > 2 else ''})
    return nics

def run_migration_network(ctx, mctx, mapping):
    old_text = mctx.facts['files'].get('etc/network/interfaces', '')
    if not old_text:
        raise ValueError('Archív neobsahuje /etc/network/interfaces.')
    old_nics = old_physical_nics(mctx.facts)
    with MigrationHost(mctx.target) as host:
        new_nics = new_physical_nics(require_ok(host, 'LC_ALL=C ip -br link', 'ip -br link zlyhal'))
        new_names = {nic['name'] for nic in new_nics}
        clean = {}
        for old, new in (mapping or {}).items():
            if old not in old_nics:
                raise ValueError(f'Neznáma pôvodná sieťovka: {old}')
            if new and new not in new_names:
                raise ValueError(f'Sieťovka {new} na novom hoste neexistuje.')
            clean[old] = new
        proposed = old_text
        for old, new in clean.items():
            if new:
                proposed = re.sub(rf'(?<![\w.-]){re.escape(old)}(?![\w-])', new, proposed)
        unmapped = [old for old in old_nics if not clean.get(old)]
        header = (f'# Návrh /etc/network/interfaces pre nový HW – Proxmox Backup Manager {migration_now()}\n'
                  f'# Mapovanie: {", ".join(f"{o} -> {n}" for o, n in clean.items() if n) or "žiadne"}\n'
                  + (f'# POZOR: bez mapovania ostali {", ".join(unmapped)} – uprav ručne.\n' if unmapped else '')
                  + '# Aplikuj až pri prepnutí z konzoly: cp do /etc/network/interfaces && ifreload -a\n')
        proposed = header + proposed
        hosts_text = mctx.facts['files'].get('etc/hosts', '')
        require_ok(host, f'mkdir -p {MIGRATION_WORKDIR} && chmod 700 {MIGRATION_WORKDIR}', 'Pracovný adresár sa nedá vytvoriť')
        sftp_put_bytes(host, f'{MIGRATION_WORKDIR}/interfaces.proposed', proposed.encode('utf-8'), 0o644)
        if hosts_text:
            sftp_put_bytes(host, f'{MIGRATION_WORKDIR}/hosts.proposed', hosts_text.encode('utf-8'), 0o644)
        ctx.log(f'Návrh siete uložený na novom hoste: {MIGRATION_WORKDIR}/interfaces.proposed')
    result = {'generated_at': migration_now(), 'mapping': clean, 'unmapped': unmapped,
              'proposed_path': f'{MIGRATION_WORKDIR}/interfaces.proposed',
              'hosts_path': f'{MIGRATION_WORKDIR}/hosts.proposed' if hosts_text else '',
              'interfaces': proposed, 'new_nics': new_nics}
    update_migration_transfer(lambda transfer: transfer.update(network={k: v for k, v in result.items() if k != 'interfaces'}))
    return result

# --- Fáza 4: presun hostí --------------------------------------------------

def run_migration_guest_move(ctx, mctx, vmid, dump_dir, target_storage):
    guest = mctx.guest_info(vmid)
    cli = guest_cli(guest['type'])
    kind = 'qemu' if guest['type'] == 'VM' else 'lxc'
    with MigrationHost(mctx.old_ssh) as old, MigrationHost(mctx.target) as new:
        ctx.progress('Kontrolujem zdieľaný adresár záloh')
        require_ok(old, f'test -d {shlex.quote(dump_dir)} && test -w {shlex.quote(dump_dir)}',
                   f'Adresár {dump_dir} nie je na starom hoste dostupný na zápis')
        require_ok(new, f'test -d {shlex.quote(dump_dir)}', f'Adresár {dump_dir} nie je dostupný na novom hoste (autofs/NAS?)')
        if guest_status(new, guest['type'], vmid) == 'running':
            raise ValueError(f'{guest["type"]} {vmid} už beží na novom hoste.')
        config_text = old.try_run(f'{cli} config {vmid}')[0] or ''
        original_onboot = bool(re.search(r'^onboot:\s*1\s*$', config_text, re.MULTILINE))
        update_migration_transfer(lambda t: t['guests'].setdefault(str(vmid), {}).update(original_onboot=original_onboot))
        require_ok(old, f'{cli} set {vmid} --onboot 0', 'Vypnutie autostartu na starom zlyhalo', 60)
        ctx.log(f'Starý host: autostart {guest["type"]} {vmid} vypnutý (pôvodne {"zapnutý" if original_onboot else "vypnutý"})')
        if guest_status(old, guest['type'], vmid) == 'running':
            ctx.progress(f'Vypínam {guest["type"]} {vmid} na starom hoste')
            old.try_run(f'{cli} shutdown {vmid} --timeout 600', 660)
        if guest_status(old, guest['type'], vmid) != 'stopped':
            raise RuntimeError(f'{guest["type"]} {vmid} sa na starom hoste nevypol. Vypni ho ručne a skús znova.')
        ctx.log(f'Starý host: {guest["type"]} {vmid} je vypnutý')
        set_migration_guest_status(vmid, 'stopped_on_old', force=True)

        ctx.progress(f'vzdump {vmid} na starom hoste')
        code, tail = migration_stream(old, f'vzdump {vmid} --mode stop --compress zstd --dumpdir {shlex.quote(dump_dir)} --remove 0',
                                      ctx, label='vzdump: ')
        if code != 0:
            raise RuntimeError(f'vzdump skončil s chybou (exit {code}): {" | ".join(tail[-3:])}')
        archive = next((m.group(1) for line in tail for m in [VZDUMP_ARCHIVE_PATTERN.search(line)] if m), '')
        if not archive:
            archive = (old.try_run(f'ls -1t {shlex.quote(dump_dir)}/vzdump-{kind}-{vmid}-* 2>/dev/null | grep -v "\\.log$" | head -1')[0] or '').strip()
        if not archive or not archive.startswith(dump_dir):
            raise RuntimeError('Nepodarilo sa zistiť súbor zálohy z vzdump.')
        if guest_status(old, guest['type'], vmid) != 'stopped':
            old.try_run(f'{cli} shutdown {vmid} --timeout 300', 360)
            raise RuntimeError(f'{guest["type"]} {vmid} sa po vzdump znova spustil na starom hoste; bol vypnutý, skontroluj a skús znova.')
        ctx.log(f'Záloha: {archive}')

        require_ok(new, f'test -f {shlex.quote(archive)}', 'Súbor zálohy nie je viditeľný na novom hoste')
        ctx.progress(f'Obnova {vmid} na novom hoste')
        restore = (f'qmrestore {shlex.quote(archive)} {vmid} --storage {shlex.quote(target_storage)} --force' if cli == 'qm'
                   else f'pct restore {vmid} {shlex.quote(archive)} --storage {shlex.quote(target_storage)} --force')
        code, tail = migration_stream(new, restore, ctx, label='restore: ')
        if code != 0:
            raise RuntimeError(f'Obnova na novom hoste zlyhala (exit {code}): {" | ".join(tail[-3:])}. Hosť ostáva vypnutý na starom – '
                               'môžeš ho tam znova spustiť (Vrátiť na starý).')
        new.try_run(f'{cli} set {vmid} --onboot 0', 60)
        set_migration_guest_status(vmid, 'restored_on_new', force=True)
    result = {'vmid': vmid, 'type': guest['type'], 'dump_file': archive, 'target_storage': target_storage,
              'original_onboot': original_onboot, 'moved_at': migration_now()}
    update_migration_transfer(lambda t: t['guests'].setdefault(str(vmid), {}).update(result))
    ctx.progress(f'{guest["type"]} {vmid} je obnovený na novom hoste (vypnutý)')
    return result

def run_migration_guest_start(ctx, mctx, vmid):
    guest = mctx.guest_info(vmid)
    cli = guest_cli(guest['type'])
    with MigrationHost(mctx.old_ssh) as old, MigrationHost(mctx.target) as new:
        if guest_status(old, guest['type'], vmid) != 'stopped':
            raise ValueError(f'{guest["type"]} {vmid} nie je vypnutý na starom hoste – na novom ho nespustím.')
        require_ok(new, f'{cli} start {vmid}', f'Štart {guest["type"]} {vmid} na novom hoste zlyhal', 300)
        status = guest_status(new, guest['type'], vmid)
    ctx.log(f'{guest["type"]} {vmid} na novom hoste: {status}')
    return {'vmid': vmid, 'status': status}

def run_migration_guest_rollback(ctx, mctx, vmid):
    guest = mctx.guest_info(vmid)
    cli = guest_cli(guest['type'])
    original = (mctx.transfer['guests'].get(str(vmid)) or {}).get('original_onboot', False)
    with MigrationHost(mctx.target) as new, MigrationHost(mctx.old_ssh) as old:
        if guest_status(new, guest['type'], vmid) == 'running':
            ctx.progress(f'Vypínam {vmid} na novom hoste')
            new.try_run(f'{cli} shutdown {vmid} --timeout 600', 660)
            if guest_status(new, guest['type'], vmid) != 'stopped':
                raise RuntimeError(f'{guest["type"]} {vmid} sa na novom hoste nevypol; na starom ho nespustím.')
        new.try_run(f'{cli} set {vmid} --onboot 0', 60)
        if original:
            old.try_run(f'{cli} set {vmid} --onboot 1', 60)
        require_ok(old, f'{cli} start {vmid}', f'Štart {vmid} na starom hoste zlyhal', 300)
        status = guest_status(old, guest['type'], vmid)
    set_migration_guest_status(vmid, 'skipped', note=f'{migration_now()}: vrátený na starý host (beží tam).', force=True)
    ctx.log(f'{guest["type"]} {vmid} beží znova na starom hoste ({status})')
    return {'vmid': vmid, 'status_old': status}

# --- Fáza 5: prepnutie -----------------------------------------------------

def migration_final_steps(mctx):
    """Ručné kroky po automatickom prepnutí: presun LXC s appkou, vypnutie starého, IP nového hosta."""
    transfer = mctx.transfer
    dump_dir = migration_default_dump_dir(mctx.facts) or '/CESTA/dump'
    app = mctx.app_guest
    app_id = app['vmid'] if app else None
    storage = (transfer['guests'].get(str(app_id), {}) if app_id else {}).get('target_storage') \
        or (migration_guest_storages(mctx.facts)[0])
    old_ip = mctx.old_ssh['host']
    temp_ip = mctx.target['host'] if mctx.target else '<dočasná IP>'
    network = transfer.get('network') or {}
    steps = []
    if app_id:
        steps.append({'where': f'STARÝ host ({old_ip}) – konzola alebo SSH', 'title': f'Zálohuj LXC {app_id} s appkou (appka sa tým vypne)',
                      'commands': [f'pct set {app_id} --onboot 0', f'pct shutdown {app_id} --timeout 300',
                                   f'vzdump {app_id} --mode stop --compress zstd --dumpdir {dump_dir} --remove 0']})
    steps.append({'where': f'STARÝ host ({old_ip})', 'title': 'Vypni starý server (uvoľní IP a SSH identitu)', 'commands': ['poweroff']})
    net_cmds = []
    if network.get('proposed_path'):
        net_cmds += ['cp /etc/network/interfaces /root/pbm-migration/interfaces.before-cutover',
                     f'cp {network["proposed_path"]} /etc/network/interfaces']
        if network.get('hosts_path'):
            net_cmds += ['cp /etc/hosts /root/pbm-migration/hosts.before-cutover', f'cp {network["hosts_path"]} /etc/hosts']
    else:
        net_cmds += [f'# Návrh siete nie je vytvorený – v /etc/network/interfaces a /etc/hosts zmeň {temp_ip} na {old_ip}',
                     'nano /etc/network/interfaces', 'nano /etc/hosts']
    net_cmds += ['ifreload -a', 'ip -br addr', 'pvecm updatecerts --force && systemctl restart pveproxy']
    steps.append({'where': f'NOVÝ host – KONZOLA (nie SSH na {temp_ip})', 'title': f'Nový host prevezme pôvodnú IP {old_ip}', 'commands': net_cmds})
    if app_id:
        steps.append({'where': 'NOVÝ host – konzola', 'title': f'Obnov a spusti LXC {app_id} s appkou',
                      'commands': [f'pct restore {app_id} $(ls -1t {dump_dir}/vzdump-lxc-{app_id}-*.tar.zst | head -1) --storage {storage} --force',
                                   f'pct set {app_id} --onboot 1', f'pct start {app_id}']})
    if (transfer.get('config_db') or {}).get('firewall_was_enabled'):
        steps.append({'where': 'NOVÝ host – konzola', 'title': 'Zapni firewall datacentra (bol dočasne vypnutý)',
                      'commands': ["sed -i 's/^enable: 0$/enable: 1/' /etc/pve/firewall/cluster.fw", 'pve-firewall status']})
    steps.append({'where': 'Appka (po spustení na novom hoste)', 'title': 'Overenie',
                  'commands': [f'Nastavenia → SSH host {old_ip} (teraz nový server) + root heslo nového hosta → Test SSH',
                               'Vytvoriť zálohu teraz → Obnova a migrácia → stav READY',
                               'Migrácia → krok Overenie a cesta späť']})
    return steps

def run_migration_cutover(ctx, mctx):
    transfer = mctx.transfer
    config_db = transfer.get('config_db') or {}
    timers = (transfer.get('files') or {}).get('timers_to_enable', [])
    jobs = config_db.get('jobs_disabled', [])
    app_id = mctx.app_guest['vmid'] if mctx.app_guest else None
    restored = {vmid: info for vmid, info in transfer['guests'].items() if info.get('moved_at')}
    with MigrationHost(mctx.old_ssh) as old:
        ctx.progress('Starý host: vypínam zálohovanie')
        for timer in timers:
            output, error = old.try_run(f'systemctl disable --now {shlex.quote(timer)}', 60)
            ctx.log(f'Starý host: timer {timer} vypnutý' if output is not None else f'Starý host: timer {timer}: {error}')
        for job_id in jobs:
            output, error = old.try_run(f'pvesh set /cluster/backup/{shlex.quote(job_id)} --enabled 0', 60)
            ctx.log(f'Starý host: vzdump job {job_id} vypnutý' if output is not None else f'Starý host: job {job_id}: {error}')
    with MigrationHost(mctx.target) as new:
        ctx.progress('Nový host: zapínam zálohovanie a autostart')
        for job_id in jobs:
            output, error = new.try_run(f'pvesh set /cluster/backup/{shlex.quote(job_id)} --enabled 1', 60)
            ctx.log(f'Nový host: vzdump job {job_id} zapnutý' if output is not None else f'Nový host: job {job_id}: {error}')
        new.try_run('systemctl daemon-reload', 60)
        for timer in timers:
            output, error = new.try_run(f'systemctl enable --now {shlex.quote(timer)}', 60)
            ctx.log(f'Nový host: timer {timer} zapnutý' if output is not None else f'Nový host: timer {timer}: {error}')
        for vmid, info in sorted(restored.items(), key=lambda pair: int(pair[0])):
            if info.get('original_onboot') and int(vmid) != app_id:
                output, error = new.try_run(f'{guest_cli(info.get("type", "VM"))} set {int(vmid)} --onboot 1', 60)
                ctx.log(f'Nový host: autostart {vmid} zapnutý' if output is not None else f'Nový host: autostart {vmid}: {error}')
    result = {'applied_at': migration_now(), 'timers': timers, 'jobs': jobs,
              'autostart': [vmid for vmid, info in restored.items() if info.get('original_onboot') and int(vmid) != app_id]}
    update_migration_transfer(lambda t: t.update(cutover=result))
    ctx.progress('Zálohovanie a autostart prepnuté – pokračuj ručnými krokmi nižšie')
    return result

def public_migration_transfer(mctx_or_none=None):
    transfer = safe_migration_transfer()
    if transfer is None:
        return None, 'Stav prenosu sa nedá načítať.'
    return transfer, None

def migration_transfer_info():
    """Údaje pre UI krokov prenosu (bez hesiel)."""
    info = {'transfer': safe_migration_transfer(), 'file_items': [], 'old_nics': [], 'new_nics': [],
            'dump_dir': '', 'storages': [], 'app_guest': None, 'node': '', 'archive': None, 'final_steps': [], 'error': None}
    try:
        mctx = MigrationContext(require_target=False)
    except (ValueError, RuntimeError, OSError) as exc:
        info['error'] = str(exc)
        return info
    info.update(node=mctx.node, dump_dir=migration_default_dump_dir(mctx.facts), storages=migration_guest_storages(mctx.facts),
                old_nics=old_physical_nics(mctx.facts),
                archive={'filename': mctx.entry.get('filename'), 'timestamp': mctx.archive_time.isoformat() if mctx.archive_time else None,
                         'fresh': bool(mctx.archive_time and datetime.now() - mctx.archive_time <= timedelta(hours=MIGRATION_SOURCE_MAX_AGE_HOURS))},
                app_guest={k: mctx.app_guest[k] for k in ('vmid', 'type')} if mctx.app_guest else None)
    profiles = {item['path']: item for item in MIGRATION_FILE_ITEMS}
    info['file_items'] = [{**profiles[path], 'name': next((d['name'] for d in DEFAULT_BACKUP_FILES if d['path'] == path), path),
                           'badge': recovery_profile_for_path(path)['badge']} for path in [i['path'] for i in MIGRATION_FILE_ITEMS]]
    target = mctx.target
    if target and target.get('last_check'):
        info['new_nics'] = target['last_check'].get('facts', {}).get('links', [])
    if mctx.target:
        info['final_steps'] = migration_final_steps(mctx)
    return info

def start_migration_operation(kind, title, runner, require_fresh=False):
    mctx = MigrationContext(require_target=True, require_fresh=require_fresh)
    return start_migration_job(kind, title, lambda ctx: runner(ctx, mctx), mctx.secrets)

def migration_json_body(allowed):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError('Očakáva sa JSON objekt s povolenými poľami, bez hesiel.')
    return data

def migration_api_response(update=None, reset=False):
    try:
        with migration_state_lock():
            state = default_migration_state() if reset else load_migration_state()
            if update:
                payload = build_migration_payload(state)
                update(state, payload)
                state['updated_at'] = migration_now()
                steps = [s for s in MIGRATION_STEPS if state['method'] in s['methods']]
                complete = bool(steps) and all(state['steps'].get(s['id'], {}).get('completed') for s in steps)
                state['finished_at'] = (state['finished_at'] or state['updated_at']) if complete else None
            if update or reset:
                save_migration_state(state)
        if update and state['finished_at']:
            forget_migration_target(keep_host=True)
        with migration_state_lock():
            payload = build_migration_payload(load_migration_state())
        response = jsonify(payload)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except ValueError as exc:
        return json_error(str(exc))
    except (OSError, RuntimeError):
        return json_error('Stav migrácie sa nedá načítať alebo uložiť. Skontroluj súbor a jeho práva; poškodený stav možno resetovať.', 503)

# ---------------------------------------------------------------------------
# Offline DR príručka (HTML generovaná z najnovšieho archívu + nastavení appky)
# ---------------------------------------------------------------------------

APP_REPO_URL = 'https://github.com/spekulanter/proxmox-backup'
APP_INSTALL_SCRIPT_URL = 'https://raw.githubusercontent.com/spekulanter/proxmox-backup/main/install_in_lxc.sh'
WEEKDAY_NAMES = ['pondelok', 'utorok', 'streda', 'štvrtok', 'piatok', 'sobota', 'nedeľa']
REDACT_KEY_PATTERN = re.compile(r'(pass|secret|token|apikey|api-key|encryption)', re.IGNORECASE)

def redact_config_text(text):
    """Pre istotu zamaskuje hodnoty kľúčov, ktoré vyzerajú ako tajomstvá."""
    lines = []
    for line in (text or '').splitlines():
        key = line.strip().split(' ', 1)[0].rstrip(':=')
        lines.append(re.sub(r'^(\s*\S+[\s:=]+).*$', r'\1***', line) if key and REDACT_KEY_PATTERN.search(key) else line)
    return '\n'.join(lines)

def parse_interfaces_stanzas(text):
    stanzas = []
    current = None
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if parts[0] == 'iface' and len(parts) >= 2:
            current = {'name': parts[1], 'method': parts[3] if len(parts) > 3 else '', 'options': {}}
            stanzas.append(current)
        elif parts[0] in ('auto', 'allow-hotplug', 'source', 'source-directory'):
            current = None
        elif current is not None:
            current['options'][parts[0]] = ' '.join(parts[1:])
    return stanzas

def describe_management_network(facts, host_ip):
    """Zistí management rozhranie, bridge, VLAN a vygeneruje minimálnu šablónu /etc/network/interfaces."""
    stanzas = parse_interfaces_stanzas(facts['files'].get('etc/network/interfaces', ''))
    by_name = {stanza['name']: stanza for stanza in stanzas}
    mgmt = None
    for stanza in stanzas:
        address = stanza['options'].get('address', '')
        if host_ip and (address == host_ip or address.startswith(host_ip + '/')):
            mgmt = stanza
            break
    if not mgmt:
        return None

    name = mgmt['name']
    vlan = None
    bridge = name
    if mgmt['options'].get('vlan-raw-device'):
        bridge = mgmt['options']['vlan-raw-device']
        vlan = mgmt['options'].get('vlan-id') or ''.join(ch for ch in name if ch.isdigit()) or None
    elif '.' in name:
        bridge, vlan = name.rsplit('.', 1)
    bridge_stanza = by_name.get(bridge, {'options': {}})
    ports = bridge_stanza['options'].get('bridge-ports', '')
    address = mgmt['options'].get('address', '')
    if '/' not in address and mgmt['options'].get('netmask'):
        address = f"{address} (netmask {mgmt['options']['netmask']})"
    gateway = mgmt['options'].get('gateway', '')
    is_bridge = bridge.startswith('vmbr')

    lines = ['auto lo', 'iface lo inet loopback', '', 'iface NIC inet manual', '']
    if is_bridge and vlan:
        lines += [f'auto {bridge}', f'iface {bridge} inet manual', '\tbridge-ports NIC', '\tbridge-stp off', '\tbridge-fd 0',
                  '\tbridge-vlan-aware yes', '\tbridge-vids 2-4094', '', f'auto {bridge}.{vlan}',
                  f'iface {bridge}.{vlan} inet static', f'\taddress {address}']
    elif is_bridge:
        lines += [f'auto {bridge}', f'iface {bridge} inet static', f'\taddress {address}', '\tbridge-ports NIC',
                  '\tbridge-stp off', '\tbridge-fd 0']
    else:
        lines = ['auto lo', 'iface lo inet loopback', '', 'auto NIC', 'iface NIC inet static', f'\taddress {address}']
    if gateway:
        lines.append(f'\tgateway {gateway}')

    return {
        'interface': name,
        'bridge': bridge if is_bridge else '',
        'vlan': vlan,
        'vlan_aware': bridge_stanza['options'].get('bridge-vlan-aware') == 'yes',
        'bridge_ports': ports,
        'address': address,
        'gateway': gateway,
        'template': '\n'.join(lines),
    }

def parse_automounts(facts):
    """AutoFS NFS/CIFS mapy z auto.master + map súborov v archíve."""
    mounts = []
    master_lines = facts['files'].get('etc/auto.master', '').splitlines()
    for name, text in facts['files'].items():
        if name.startswith('etc/auto.master.d/'):
            master_lines += text.splitlines()
    for line in master_lines:
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith(('#', '+')):
            continue
        root, map_file = parts[0], parts[1]
        map_text = facts['files'].get(archive_name_for_path(map_file), '') if map_file.startswith('/') else ''
        for entry in map_text.splitlines():
            fields = entry.split()
            if len(fields) < 3 or fields[0].startswith('#'):
                continue
            key, options, source = fields[0], fields[1], fields[-1]
            option_list = [opt for opt in options.lstrip('-').split(',') if opt]
            fstype = next((opt.split('=', 1)[1] for opt in option_list if opt.startswith('fstype=')), 'nfs')
            mount_options = ','.join(opt for opt in option_list if not opt.startswith('fstype='))
            mounts.append({
                'key': key, 'fstype': fstype, 'options': mount_options, 'source': source,
                'server': source.split(':', 1)[0].lstrip('/').split('/', 1)[0],
                'path': posixpath.join(root, key) if root != '/-' else key,
                'map_file': map_file,
            })
    return mounts

def parse_backup_storages(facts):
    storages = []
    for section in parse_pve_section_config(facts['files'].get('etc/pve/storage.cfg', '')):
        props = section['props']
        content = props.get('content', '')
        if 'backup' not in content and section['type'] not in ('pbs',):
            continue
        storages.append({
            'id': section['id'], 'type': section['type'], 'path': props.get('path', ''),
            'server': props.get('server', ''), 'export': props.get('export', '') or props.get('share', ''),
            'disabled': 'disable' in props, 'datastore': props.get('datastore', ''),
        })
    return storages

def detect_own_ip(peer_host, peer_port=22):
    """Lokálna IP, cez ktorú appka dosiahne Proxmox host (UDP connect – nič sa neposiela)."""
    if not peer_host:
        return None
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((peer_host, int(peer_port or 22)))
            return sock.getsockname()[0]
    except (OSError, ValueError):
        return None

def find_app_guest(facts, own_ip):
    """LXC/VM s appkou podľa IP v net0 configu pôvodného hosta."""
    if not own_ip:
        return None
    for name, text in sorted(facts['files'].items()):
        match = GUEST_CONF_PATTERN.match(name)
        if not match:
            continue
        main = text.split('\n[', 1)[0]
        if re.search(rf'ip={re.escape(own_ip)}/', main):
            net = {}
            for line in main.splitlines():
                if line.startswith('net0:'):
                    for part in line.split(':', 1)[1].strip().split(','):
                        key, _sep, value = part.partition('=')
                        net[key] = value
            return {
                'vmid': int(match.group(3)), 'node': match.group(1),
                'type': 'LXC' if match.group(2) == 'lxc' else 'VM',
                'config': '\n'.join(line for line in main.splitlines() if line and not line.startswith('#')),
                'net': net,
            }
    return None

def describe_auto_backup(config):
    if not config.get('auto_backup_enabled'):
        return 'vypnutá'
    frequency = config.get('auto_backup_frequency', 'monthly')
    time_text = f"{int(config.get('auto_backup_hour', 2)):02d}:{int(config.get('auto_backup_minute', 0)):02d}"
    day = int(config.get('auto_backup_day', 0))
    if frequency == 'daily':
        return f'denne o {time_text}'
    if frequency == 'weekly':
        return f'týždenne – {WEEKDAY_NAMES[day % 7]} {time_text}'
    return f'mesačne – {day + 1}. deň v mesiaci {time_text}'

def app_git_version():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        commit = subprocess.run(['git', '-C', app_dir, 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=5).stdout.strip()
        branch = subprocess.run(['git', '-C', app_dir, 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True, timeout=5).stdout.strip()
        return f'{branch}@{commit}' if commit else ''
    except (OSError, subprocess.SubprocessError):
        return ''

def build_handbook_context(config=None):
    """Údaje pre offline príručku – bez hesiel a bez obsahu citlivých súborov."""
    config = config or load_config()
    history = visible_backup_history(config, persist_pruned=False)
    overview = build_recovery_overview(config, history=history)
    migration = None
    migration_error = None
    try:
        with migration_state_lock():
            migration_state = load_migration_state()
        if migration_state['method']:
            migration = build_migration_payload(migration_state, history)
    except (OSError, RuntimeError):
        migration_error = 'Stav plánovanej migrácie sa nedá načítať. Skontroluj súbor a práva alebo resetuj poškodený stav v appke.'
    source = sanitize_source_config(config.get('source_config'))
    ftp = sanitize_ftp_config(config.get('ftp_config'))
    host_ip = source['ssh']['host'] if source['mode'] == 'remote_ssh' else ''

    entry, archive_path = latest_local_archive_entry(history)
    facts = None
    archive_error = ''
    if archive_path:
        try:
            facts = read_archive_facts(archive_path)
        except (OSError, tarfile.TarError, ValueError) as exc:
            archive_error = str(exc)

    context = {
        'generated_at': datetime.now(),
        'app_version': app_git_version(),
        'repo_url': APP_REPO_URL,
        'install_url': APP_INSTALL_SCRIPT_URL,
        'archive': None,
        'archive_error': archive_error,
        'host_ip': host_ip,
        'source_mode': source['mode'],
        'ssh': {'host': source['ssh']['host'], 'port': source['ssh']['port'], 'username': source['ssh']['username']},
        'ftp': {'host': ftp['host'], 'port': ftp['port'], 'username': ftp['username'], 'remote_dir': ftp['remote_dir']},
        'max_backup_count': config.get('max_backup_count'),
        'auto_backup': describe_auto_backup(config),
        'readiness': overview['readiness'],
        'risks': overview['risks']['risks'],
        'items': overview['items'],
        'categories': overview['categories'],
        'wiki': WIKI_ARTICLES,
        'checklist': RECOVERY_CHECKLIST,
        'migration': migration,
        'migration_error': migration_error,
        'node': '', 'fqdn': '', 'pve_version': '', 'dns': [], 'network': None,
        'automounts': [], 'storages': [], 'jobs': [], 'guests': [], 'app_guest': None, 'own_ip': None,
        'dump_dir': '', 'raw': {}, 'diag': {}, 'timers': [],
    }
    if entry:
        timestamp = parse_backup_timestamp(entry)
        context['archive'] = {'filename': entry.get('filename'), 'timestamp': timestamp, 'ftp_status': entry.get('ftp_status')}
    if not facts:
        return context

    analysis = analyze_recovery_risks(facts)
    for guest in analysis['guests']:
        guest.setdefault('backed_up', False)
    node = analysis['node']
    fqdn = ''
    for line in facts['files'].get('etc/hosts', '').splitlines():
        parts = line.split()
        if parts and host_ip and parts[0] == host_ip and len(parts) > 1:
            fqdn = parts[1]
    if not host_ip:
        for line in info_stdout(facts, 'ip-br-addr.txt').splitlines():
            parts = line.split()
            if len(parts) > 2 and parts[0].startswith('vmbr'):
                host_ip = parts[2].split('/')[0]
                break
    network = describe_management_network(facts, host_ip)
    if network and not network['gateway']:
        route = re.search(r'^default via (\S+)', info_stdout(facts, 'ip-route.txt'), re.MULTILINE)
        network['gateway'] = route.group(1) if route else ''

    storages = parse_backup_storages(facts)
    automounts = parse_automounts(facts)
    jobs = analysis['jobs']
    storage_by_id = {storage['id']: storage for storage in storages}
    dump_dir = ''
    for job in jobs:
        storage = storage_by_id.get(job['storage'])
        if storage and storage['path']:
            dump_dir = posixpath.join(storage['path'], 'dump')
            break
    own_ip = detect_own_ip(host_ip, source['ssh']['port']) if source['mode'] == 'remote_ssh' else None

    context.update({
        'node': node,
        'fqdn': fqdn or (f'{node}.<doména>' if node else ''),
        'host_ip': host_ip,
        'pve_version': '\n'.join(info_stdout(facts, 'pveversion-v.txt').splitlines()[:2]),
        'dns': [line.split()[1] for line in facts['files'].get('etc/resolv.conf', '').splitlines()
                if line.startswith('nameserver') and len(line.split()) > 1],
        'network': network,
        'automounts': automounts,
        'storages': storages,
        'jobs': [{**job, 'vmids': sorted(job['vmids']), 'exclude': sorted(job['exclude'])} for job in jobs],
        'guests': analysis['guests'],
        'backed_up_lxc': [guest['vmid'] for guest in analysis['guests'] if guest['backed_up'] and guest['type'] == 'LXC'],
        'backed_up_vm': [guest['vmid'] for guest in analysis['guests'] if guest['backed_up'] and guest['type'] == 'VM'],
        'own_ip': own_ip,
        'app_guest': find_app_guest(facts, own_ip),
        'dump_dir': dump_dir,
        'timers': sorted(
            posixpath.basename(name) for name in facts['members']
            if name.startswith('etc/systemd/system/') and name.count('/') == 3
            and name.endswith(('.service', '.timer')) and 'backup' in name
        ),
        'raw': {
            'interfaces': facts['files'].get('etc/network/interfaces', ''),
            'hosts': facts['files'].get('etc/hosts', ''),
            'auto_master': facts['files'].get('etc/auto.master', ''),
            'auto_nfs': facts['files'].get('etc/auto.nfs', ''),
            'storage_cfg': redact_config_text(facts['files'].get('etc/pve/storage.cfg', '')),
            'jobs_cfg': redact_config_text(facts['files'].get('etc/pve/jobs.cfg', '')),
            'fstab': facts['files'].get('etc/fstab', ''),
        },
        'diag': {
            'ip_br_link': nic_summary(facts),
            'lsblk': info_stdout(facts, 'lsblk-f.txt'),
            'pvesm': info_stdout(facts, 'pvesm-status.txt'),
        },
    })
    return context

def render_inline_markup(text):
    """`kód` → <code>; všetko ostatné escapované (pre wiki texty v príručke)."""
    from markupsafe import Markup, escape
    return Markup(re.sub(r'`([^`]+)`', r'<code>\1</code>', str(escape(text or ''))))

app.add_template_filter(render_inline_markup, 'dr_inline')

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

{build_recovery_readme_section(selected_files)}
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
    write_text_file(os.path.join(info_dir, RECOVERY_MANIFEST_FILENAME), build_recovery_manifest(selected_files))
    generated.append(RECOVERY_MANIFEST_FILENAME)

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
        self.write_remote_file(
            client,
            posixpath.join(remote_info_dir, RECOVERY_MANIFEST_FILENAME),
            build_recovery_manifest(selected_files),
        )
        generated.append(RECOVERY_MANIFEST_FILENAME)

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

def ftp_connect(ftp_config, timeout=30):
    """Otvorí FTP session a prepne ju do nakonfigurovaného adresára."""
    ftp_config = sanitize_ftp_config(ftp_config)
    ftp = ftplib.FTP(timeout=timeout)
    ftp.connect(ftp_config['host'], ftp_config['port'])
    ftp.login(ftp_config['username'], ftp_config['password'])
    ftp_cwd_to_target(ftp, ftp_config.get('remote_dir', ''))
    return ftp

def safe_backup_filename(filename):
    """Povolí iba jednoduchý názov .tar.gz archívu bez adresárových častí."""
    name = os.path.basename(str(filename or '').strip())
    if not name or name != str(filename or '').strip():
        raise ValueError('Neplatný názov archívu')
    if not name.endswith('.tar.gz'):
        raise ValueError('Podporované sú iba .tar.gz archívy')
    if any(char in name for char in ('/', '\\', '\x00', '\n', '\r')):
        raise ValueError('Neplatný názov archívu')
    return name

def filename_from_backup_id(backup_id):
    """Preloží virtual FTP id späť na bezpečný názov súboru."""
    backup_id = str(backup_id or '')
    if backup_id.startswith('ftp:'):
        return safe_backup_filename(backup_id[4:])
    return None

def ftp_backup_id(filename):
    return f"ftp:{safe_backup_filename(filename)}"

def format_file_size_bytes(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return 'n/a'
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def parse_ftp_mdtm(value):
    """Prevedie FTP MDTM odpoveď na ISO timestamp, ak ju server poskytne."""
    if not value:
        return ''
    raw = str(value).strip()
    if raw.startswith('213 '):
        raw = raw[4:].strip()
    try:
        return datetime.strptime(raw[:14], '%Y%m%d%H%M%S').isoformat()
    except (TypeError, ValueError):
        return ''

def list_ftp_backups(ftp_config):
    """Best-effort zoznam .tar.gz archívov z FTP."""
    ftp_config = sanitize_ftp_config(ftp_config)
    if not ftp_config_complete(ftp_config):
        return {
            'available': False,
            'warning': 'FTP konfigurácia nie je kompletná',
            'archives': [],
        }

    ftp = None
    try:
        ftp = ftp_connect(ftp_config, timeout=20)
        names = ftp.nlst()
        archives = []
        for raw_name in names:
            filename = os.path.basename(str(raw_name))
            try:
                filename = safe_backup_filename(filename)
            except ValueError:
                continue

            size_bytes = None
            timestamp = ''
            try:
                size_bytes = ftp.size(filename)
            except Exception:
                size_bytes = None
            try:
                timestamp = parse_ftp_mdtm(ftp.sendcmd(f'MDTM {filename}'))
            except Exception:
                timestamp = ''

            archives.append({
                'filename': filename,
                'id': ftp_backup_id(filename),
                'timestamp': timestamp,
                'size_bytes': size_bytes,
                'size': format_file_size_bytes(size_bytes) if size_bytes is not None else 'n/a',
            })
        return {
            'available': True,
            'warning': '',
            'archives': sorted(archives, key=lambda item: item.get('timestamp') or item.get('filename') or ''),
        }
    except Exception as exc:
        return {
            'available': False,
            'warning': f'FTP zálohy sa nepodarilo načítať: {exc}',
            'archives': [],
        }
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass

def download_from_ftp(filename, ftp_config):
    """Stiahne FTP archív do lokálneho backup adresára a vráti jeho cestu."""
    filename = safe_backup_filename(filename)
    ftp_config = sanitize_ftp_config(ftp_config)
    if not ftp_config_complete(ftp_config):
        raise ValueError('FTP konfigurácia chýba, archív sa nedá stiahnuť')

    backup_dir = ensure_backup_storage_dir()
    local_path = os.path.realpath(os.path.abspath(os.path.join(backup_dir, filename)))
    backup_root = os.path.realpath(os.path.abspath(backup_dir))
    if not path_is_under(local_path, backup_root):
        raise ValueError('Archív je mimo lokálneho backup adresára')

    temp_path = f"{local_path}.download"
    ftp = None
    try:
        ftp = ftp_connect(ftp_config, timeout=60)
        with open(temp_path, 'wb') as output_file:
            ftp.retrbinary(f'RETR {filename}', output_file.write)
        os.replace(temp_path, local_path)
        os.chmod(local_path, 0o600)
        return local_path
    except Exception as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise RuntimeError(f'Chyba pri sťahovaní z FTP: {exc}') from exc
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass

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

def notify_backup_result(result, backup_mode):
    event_type = 'auto_backup' if backup_mode == 'auto' else 'manual_backup'
    title = 'Automatická záloha Proxmoxu' if backup_mode == 'auto' else 'Manuálna záloha Proxmoxu'
    filename = result.get('filename', 'backup')
    if result.get('ftp_status') == 'success':
        message = f"Záloha {filename} bola vytvorená a nahraná na FTP. Veľkosť: {result.get('size', 'n/a')}."
        priority = 0
    else:
        message = f"Záloha {filename} ostala lokálne, FTP upload zlyhal: {result.get('ftp_message', 'neznáma chyba')}"
        priority = 1
    warning = notify_pushover(event_type, title, message, priority=priority)
    if warning:
        result.setdefault('retention_warnings', []).append(warning)
        result['pushover_warning'] = warning
    return result

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
                'recovery': recovery_profile_for_path(path),
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
    """História zobrazuje lokálne archívy a známe FTP archívy."""
    try:
        archive_path = resolve_backup_entry_local_path(entry)
    except ValueError:
        return False
    return os.path.isfile(archive_path) or entry.get('ftp_status') == 'success'

def visible_backup_history(config=None, persist_pruned=True):
    """Vyfiltruje históriu od záznamov bez lokálneho archívu alebo známeho FTP súboru."""
    config = config or load_config()
    history = load_backup_history()
    visible = [entry for entry in history if backup_entry_is_visible(entry)]
    if persist_pruned and len(visible) != len(history):
        save_backup_history(visible)
    return visible

def local_backup_available(entry):
    try:
        return os.path.isfile(resolve_backup_entry_local_path(entry))
    except (ValueError, TypeError):
        return False

def decorate_backup_entry(entry, local_available=False, ftp_available=False, ftp_unknown=False, ftp_warning=''):
    """Doplní storage metadáta pre UI bez zmeny uloženého záznamu."""
    decorated = dict(entry)
    filename = decorated.get('filename') or os.path.basename(str(decorated.get('local_path') or ''))
    decorated['id'] = str(decorated.get('id') or (ftp_backup_id(filename) if filename else ''))
    decorated['filename'] = filename
    decorated['local_available'] = bool(local_available)
    decorated['ftp_available'] = bool(ftp_available)
    decorated['ftp_unknown'] = bool(ftp_unknown)
    decorated['storage_locations'] = []
    if local_available:
        decorated['storage_locations'].append('local')
        try:
            archive_path = resolve_backup_entry_local_path(decorated)
            decorated['local_path'] = archive_path
            decorated['size'] = decorated.get('size') or get_file_size(archive_path)
        except ValueError:
            pass
    if ftp_available:
        decorated['storage_locations'].append('ftp')
        decorated['ftp_status'] = 'success'
    elif ftp_unknown:
        decorated['ftp_status'] = decorated.get('ftp_status') or 'unknown'
        if ftp_warning:
            decorated['ftp_message'] = ftp_warning
    elif decorated.get('ftp_status') == 'success':
        decorated['ftp_status'] = 'missing'
    decorated.setdefault('size', 'n/a')
    return decorated

def merge_backup_history_with_ftp(config=None):
    """Zlúči lokálnu históriu so živým FTP zoznamom bez pádu pri FTP výpadku."""
    config = config or load_config()
    ftp_config = config.get('ftp_config', {})
    history = visible_backup_history(config, persist_pruned=False)
    ftp_result = list_ftp_backups(ftp_config)
    ftp_by_filename = {
        item['filename']: item
        for item in ftp_result.get('archives', [])
        if item.get('filename')
    }

    merged = []
    known_filenames = set()
    for entry in history:
        filename = entry.get('filename')
        known_filenames.add(filename)
        local_available = local_backup_available(entry)
        ftp_meta = ftp_by_filename.get(filename)
        ftp_available = bool(ftp_meta)
        ftp_unknown = not ftp_result.get('available') and entry.get('ftp_status') == 'success'
        decorated = decorate_backup_entry(
            entry,
            local_available=local_available,
            ftp_available=ftp_available,
            ftp_unknown=ftp_unknown,
            ftp_warning=ftp_result.get('warning', ''),
        )
        if ftp_meta:
            decorated['ftp_size'] = ftp_meta.get('size')
            decorated['ftp_size_bytes'] = ftp_meta.get('size_bytes')
            if not decorated.get('timestamp'):
                decorated['timestamp'] = ftp_meta.get('timestamp', '')
            if not decorated.get('size') or decorated.get('size') == 'n/a':
                decorated['size'] = ftp_meta.get('size') or 'n/a'
        merged.append(decorated)

    for filename, ftp_meta in ftp_by_filename.items():
        if filename in known_filenames:
            continue
        merged.append(decorate_backup_entry(
            {
                'id': ftp_backup_id(filename),
                'filename': filename,
                'timestamp': ftp_meta.get('timestamp', ''),
                'date': ftp_meta.get('timestamp', ''),
                'size': ftp_meta.get('size') or 'n/a',
                'ftp_status': 'success',
                'status': 'ftp_only',
                'backup_mode': 'ftp',
            },
            local_available=False,
            ftp_available=True,
        ))

    merged.sort(key=backup_history_sort_key, reverse=True)
    return {
        'success': True,
        'backups': merged,
        'ftp': {
            'available': bool(ftp_result.get('available')),
            'warning': ftp_result.get('warning', ''),
            'count': len(ftp_result.get('archives', [])),
        },
    }

def find_backup_entry_or_virtual(backup_id, config=None):
    """Nájde uložený záznam alebo vytvorí virtuálny FTP-only záznam."""
    config = config or load_config()
    history = load_backup_history()
    entry = next((item for item in history if str(item.get('id')) == str(backup_id)), None)
    if entry:
        return entry, False

    filename = filename_from_backup_id(backup_id)
    if not filename:
        raise FileNotFoundError('Záloha nie je v histórii')
    entry = next((item for item in history if item.get('filename') == filename), None)
    if entry:
        return entry, False

    ftp_result = list_ftp_backups(config.get('ftp_config', {}))
    if not ftp_result.get('available'):
        raise FileNotFoundError(ftp_result.get('warning') or 'FTP archívy nie sú dostupné')
    ftp_meta = next((item for item in ftp_result.get('archives', []) if item.get('filename') == filename), None)
    if not ftp_meta:
        raise FileNotFoundError('Archív nie je dostupný na FTP')
    return {
        'id': ftp_backup_id(filename),
        'filename': filename,
        'timestamp': ftp_meta.get('timestamp', ''),
        'date': ftp_meta.get('timestamp', ''),
        'size': ftp_meta.get('size') or 'n/a',
        'ftp_status': 'success',
        'status': 'ftp_only',
        'backup_mode': 'ftp',
    }, True

def persist_cached_backup_entry(entry, archive_path):
    """Zapíše alebo aktualizuje históriu po lokálnom cache FTP archívu."""
    filename = safe_backup_filename(entry.get('filename'))
    history = load_backup_history()
    existing = next(
        (item for item in history if str(item.get('id')) == str(entry.get('id')) or item.get('filename') == filename),
        None,
    )
    now = datetime.now()
    if existing:
        existing.update({
            'filename': filename,
            'local_path': archive_path,
            'ftp_status': 'success',
            'ftp_message': 'Archív dostupný na FTP',
            'status': 'success',
            'size': get_file_size(archive_path),
        })
        if not existing.get('timestamp'):
            existing['timestamp'] = entry.get('timestamp') or now.isoformat()
        if not existing.get('date'):
            existing['date'] = entry.get('date') or now.strftime('%d.%m.%Y %H:%M')
    else:
        existing = dict(entry)
        if str(existing.get('id', '')).startswith('ftp:'):
            existing['id'] = str(time.time_ns())
        existing.update({
            'filename': filename,
            'local_path': archive_path,
            'ftp_status': 'success',
            'ftp_message': 'Archív stiahnutý z FTP',
            'status': 'success',
            'size': get_file_size(archive_path),
            'timestamp': existing.get('timestamp') or now.isoformat(),
            'date': existing.get('date') or now.strftime('%d.%m.%Y %H:%M'),
        })
        history.append(existing)
    save_backup_history(history)
    return existing

def ensure_backup_cached(backup_id, config=None):
    """Zaistí lokálnu kópiu archívu z histórie alebo FTP-only záznamu."""
    config = config or load_config()
    entry, _virtual = find_backup_entry_or_virtual(backup_id, config)
    try:
        archive_path = resolve_backup_entry_local_path(entry)
        if os.path.isfile(archive_path):
            return entry, archive_path, False
    except ValueError:
        if entry.get('local_path'):
            raise

    if entry.get('ftp_status') != 'success' and not str(entry.get('id', '')).startswith('ftp:'):
        raise FileNotFoundError('Lokálny archív už neexistuje a FTP kópia nie je potvrdená')

    archive_path = download_from_ftp(entry.get('filename'), config.get('ftp_config', {}))
    persisted_entry = persist_cached_backup_entry(entry, archive_path)
    return persisted_entry, archive_path, True

def restore_archive_source(entry, cached=False):
    """Popíše, odkiaľ sa pre preview/restore reálne číta archív."""
    if cached:
        return {
            'source': 'ftp_cached',
            'label': 'FTP -> lokálna cache',
            'message': 'Archív bol stiahnutý z FTP a používa sa jeho lokálna cache kópia.',
        }
    if entry.get('ftp_status') == 'success':
        return {
            'source': 'local',
            'label': 'Lokálna kópia',
            'message': 'Používa sa lokálna kópia archívu; FTP kópia je dostupná ako ďalšie úložisko.',
        }
    return {
        'source': 'local',
        'label': 'Lokálna kópia',
        'message': 'Používa sa lokálna kópia archívu.',
    }

def delete_backup_entry(backup_id, ftp_config):
    """Zmaže lokálny archív, vzdialený FTP súbor a odstráni záznam z histórie."""
    history = load_backup_history()
    entry = next((item for item in history if str(item.get('id')) == str(backup_id)), None)
    virtual_entry = False
    if not entry:
        filename = filename_from_backup_id(backup_id)
        if not filename:
            raise FileNotFoundError('Záloha nie je v histórii')
        entry = {
            'id': backup_id,
            'filename': filename,
            'ftp_status': 'success',
        }
        virtual_entry = True

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

    if virtual_entry:
        new_history = [item for item in history if item.get('filename') != entry.get('filename')]
    else:
        new_history = [item for item in history if str(item.get('id')) != str(backup_id)]
    save_backup_history(new_history)
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
    """Zoznam archívov dostupných lokálne alebo cez FTP cache-on-demand."""
    result = merge_backup_history_with_ftp(load_config())
    return [
        {
            'id': entry.get('id'),
            'filename': entry.get('filename'),
            'timestamp': entry.get('timestamp') or entry.get('date') or '',
            'size': entry.get('size') or 'n/a',
            'source_host': entry.get('source_host'),
            'source_mode': entry.get('source_mode'),
            'local_path': entry.get('local_path', ''),
            'local_available': entry.get('local_available', False),
            'ftp_available': entry.get('ftp_available', False),
            'storage_locations': entry.get('storage_locations', []),
        }
        for entry in result.get('backups', [])
        if entry.get('local_available') or entry.get('ftp_available')
    ]

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

    def stage_path(self, client, staging_dir, review_dir, restore_path):
        """Pripraví cestu do review adresára v /root bez zásahu do živého systému."""
        arcname = archive_name_for_path(restore_path)
        staged_path = posixpath.join(staging_dir, arcname)
        review_parent = posixpath.join(review_dir, posixpath.dirname(arcname))

        test_command = f'test -e {shlex.quote(staged_path)} || test -L {shlex.quote(staged_path)}'
        exit_code, _stdout, _stderr = self.source.run_command(client, test_command, timeout=30)
        if exit_code != 0:
            return {'path': restore_path, 'reason': 'missing_in_staging'}

        self.run_required(client, f'mkdir -p {shlex.quote(review_parent)}', timeout=30)
        self.run_required(client, f'cp -a {shlex.quote(staged_path)} {shlex.quote(review_parent)}/', timeout=300)
        return None

    def restore(self, archive_path, selected_paths, stage_paths=None):
        stage_paths = list(stage_paths or [])
        member_names = [member.name for member in restore_archive_members(archive_path, list(selected_paths) + stage_paths)]
        if not member_names:
            raise ValueError('Archív neobsahuje vybrané obnoviteľné položky')

        client = self.source.connect()
        remote_workdir = None
        try:
            remote_workdir = self.run_required(client, 'mktemp -d /tmp/pve-restore.XXXXXX', timeout=10).strip()
            remote_archive = posixpath.join(remote_workdir, 'restore.tar.gz')
            remote_members = posixpath.join(remote_workdir, 'members.txt')
            staging_dir = posixpath.join(remote_workdir, 'staging')
            run_stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup_dir = f"/root/proxmox-backup-restore-preapply-{run_stamp}" if selected_paths else None
            review_dir = f"/root/proxmox-backup-restore-review-{run_stamp}" if stage_paths else None

            self.upload_archive(client, archive_path, remote_archive)
            self.write_remote_file(client, remote_members, '\n'.join(member_names) + '\n')
            mkdir_targets = [staging_dir] + [path for path in (backup_dir, review_dir) if path]
            self.run_required(client, 'mkdir -p ' + ' '.join(shlex.quote(path) for path in mkdir_targets), timeout=30)
            if review_dir:
                self.run_required(client, f'chmod 700 {shlex.quote(review_dir)}', timeout=30)
            extract_command = (
                f'tar -xzf {shlex.quote(remote_archive)} '
                f'-C {shlex.quote(staging_dir)} '
                f'-T {shlex.quote(remote_members)}'
            )
            self.run_required(client, extract_command, timeout=600)

            applied = []
            staged = []
            skipped = []
            for restore_path in selected_paths:
                skip = self.apply_path(client, staging_dir, backup_dir, restore_path)
                if skip:
                    skipped.append(skip)
                else:
                    applied.append({'path': restore_path})

            for restore_path in stage_paths:
                skip = self.stage_path(client, staging_dir, review_dir, restore_path)
                if skip:
                    skipped.append(skip)
                else:
                    staged.append({'path': restore_path})

            if review_dir and staged:
                self.write_remote_file(
                    client,
                    posixpath.join(review_dir, 'README-REVIEW.txt'),
                    build_review_dir_readme(review_dir, [item['path'] for item in staged]),
                )

            return {
                'success': True,
                'remote_host': self.source.ssh_config.get('host'),
                'backup_dir': backup_dir,
                'review_dir': review_dir,
                'applied': applied,
                'staged': staged,
                'skipped': skipped,
            }
        finally:
            if remote_workdir:
                safe_workdir = remote_workdir.strip()
                if safe_workdir.startswith('/tmp/pve-restore.'):
                    self.source.run_command(client, f'rm -rf {shlex.quote(safe_workdir)}', timeout=30)
            client.close()

class RestoreAcknowledgementRequired(ValueError):
    """Priamy restore REVIEW/SELECTIVE/REFERENCE položiek bez výslovného potvrdenia kontroly."""

    def __init__(self, paths):
        self.paths = list(paths)
        super().__init__(
            'Tieto položky vyžadujú kontrolu pred obnovou (REVIEW FIRST / SELECTIVE / REFERENCE ONLY). '
            f'Potvrď ich kontrolu alebo použi režim „Iba pripraviť na kontrolu“: {", ".join(self.paths)}'
        )

def clean_restore_paths(paths):
    """Normalizuje restore cesty a overí whitelist; zachová poradie bez duplicít."""
    allowed_paths = restore_whitelist_paths()
    clean_paths = []
    for path in paths or []:
        normalized = normalize_remote_path(path)
        if normalized not in allowed_paths:
            raise ValueError(f'Cesta nie je povolená pre restore: {path}')
        if glob.has_magic(normalized):
            raise ValueError(f'Wildcard cesty nie sú podporované pre restore: {path}')
        if normalized not in clean_paths:
            clean_paths.append(normalized)
    return clean_paths

def build_review_dir_readme(review_dir, staged_paths):
    """Návod v review adresári: čo bolo pripravené a ako s tým bezpečne naložiť."""
    lines = [
        'Proxmox Backup Manager – pripravené na kontrolu',
        f'Vygenerované: {datetime.now().isoformat(timespec="seconds")}',
        '',
        'Tieto súbory NEBOLI aplikované na systém. Porovnaj ich s aktuálnym stavom (diff -u)',
        'a prenes ručne iba to, čo potrebuješ. Detailné postupy sú vo Wiki aplikácie.',
        '',
    ]
    for path in staged_paths:
        profile = recovery_profile_for_path(path)
        lines.append(f"## {path} [{profile['badge']}]")
        lines.append(f"Na novom HW: {profile.get('restore_new_hardware', '')}")
        for warning in profile.get('warnings', []):
            lines.append(f'! {warning}')
        lines.append(f'  diff -ru {review_dir}{path} {path}')
        lines.append('')
    lines.append(f'Po dokončení adresár zmaž: rm -rf {review_dir}')
    return '\n'.join(lines) + '\n'

def run_restore_job(backup_id, selected_paths, source_config, stage_paths=None, acknowledged_paths=None):
    """Spoločný restore flow pre API: `selected_paths` sa aplikujú, `stage_paths` iba pripravia na kontrolu."""
    if not selected_paths and not stage_paths:
        raise ValueError('Nevybral si žiadne cesty na obnovu')

    source_config = sanitize_source_config(source_config)
    if source_config['mode'] != 'remote_ssh':
        raise ValueError('Obnova je v tejto verzii podporovaná iba cez Remote SSH')

    clean_stage_paths = clean_restore_paths(stage_paths)
    clean_paths = [path for path in clean_restore_paths(selected_paths) if path not in clean_stage_paths]

    blocked = [path for path in clean_paths if not recovery_profile_for_path(path)['direct_restore_allowed']]
    if blocked:
        raise ValueError(
            'Tieto cesty aplikácia nikdy priamo neprepisuje (ADVANCED RESTORE / REFERENCE ONLY), '
            f'použi režim „Iba pripraviť na kontrolu“ a postup z Wiki: {", ".join(blocked)}'
        )

    acknowledged = {normalize_remote_path(path) for path in (acknowledged_paths or []) if path}
    unacknowledged = [
        path for path in clean_paths
        if recovery_profile_for_path(path)['requires_review'] and path not in acknowledged
    ]
    if unacknowledged:
        raise RestoreAcknowledgementRequired(unacknowledged)

    entry, archive_path, cached = ensure_backup_cached(backup_id)
    available_paths = {item['path'] for item in preview_restore_archive(archive_path)}
    missing = [path for path in clean_paths + clean_stage_paths if path not in available_paths]
    if missing:
        raise ValueError(f'Archív neobsahuje vybrané cesty: {", ".join(missing)}')

    restore_service = RemoteSshRestoreService(source_config)
    result = restore_service.restore(archive_path, clean_paths, clean_stage_paths)
    result['archive_source'] = restore_archive_source(entry, cached)
    result['archive_filename'] = entry.get('filename') or os.path.basename(archive_path)
    return result

@app.before_request
def require_authentication():
    """Vynúti single-admin login pre celé UI a API okrem setup/login/recovery toku."""
    g.service_auth = False
    auth_config = sync_flask_secret()
    admin = auth_config.get('admin')

    if request.endpoint is None or auth_public_endpoint(request.endpoint):
        return None

    if not admin:
        if request.path.startswith('/api/'):
            return json_error('Najprv je potrebné vytvoriť admin účet', 403)
        return redirect(url_for('index'))

    if request.endpoint == 'create_auto_backup_api' and request_has_valid_service_token(auth_config):
        g.service_auth = True
        return None

    if not session_is_authenticated(auth_config):
        if request.path.startswith('/api/'):
            return json_error('Vyžaduje sa prihlásenie', 401)
        return redirect(url_for('index'))

    ensure_csrf_token()
    if csrf_required_for_request() and not csrf_token_valid():
        return json_error('Neplatný CSRF token', 403)
    return None

def current_admin_or_error():
    auth_config = load_auth_config()
    admin = auth_config.get('admin')
    if not admin:
        raise ValueError('Admin účet ešte neexistuje')
    return auth_config, admin

def auth_status_payload(auth_config=None):
    auth_config = auth_config or load_auth_config()
    admin = auth_config.get('admin')
    authenticated = session_is_authenticated(auth_config)
    return {
        'success': True,
        'setup_required': admin is None,
        'authenticated': authenticated,
        'csrf_token': ensure_csrf_token() if authenticated else '',
        'username': admin.get('username', '') if admin else '',
        'recovery_codes_remaining': recovery_codes_remaining(admin),
        'pushover_configured': pushover_configured(admin) if admin else False,
    }

@app.route('/api/auth/status')
def auth_status_api():
    return jsonify(auth_status_payload())

@app.route('/api/auth/setup/start', methods=['POST'])
def auth_setup_start_api():
    auth_config = load_auth_config()
    if auth_config.get('admin'):
        return json_error('Registrácia ďalšieho používateľa nie je povolená', 409)
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username:
        return json_error('Používateľské meno je povinné')
    if not password_is_strong_enough(password):
        return json_error('Heslo musí mať aspoň 10 znakov')
    secret = generate_totp_secret()
    session['pending_setup'] = {
        'username': username,
        'password_hash': generate_password_hash(password),
        'totp_secret': secret,
        'created_at': time.time(),
    }
    otpauth_uri = build_otpauth_uri(username, secret)
    return jsonify({'success': True, 'totp_secret': secret, 'otpauth_uri': otpauth_uri, 'qr_data_uri': build_qr_data_uri(otpauth_uri)})

@app.route('/api/auth/setup/complete', methods=['POST'])
def auth_setup_complete_api():
    auth_config = load_auth_config()
    if auth_config.get('admin'):
        return json_error('Registrácia ďalšieho používateľa nie je povolená', 409)
    pending = session.get('pending_setup') or {}
    if not pending or time.time() - float(pending.get('created_at', 0)) > 900:
        return json_error('Registrácia expirovala, spusti nastavenie znova', 400)
    data = request.get_json(silent=True) or {}
    if not verify_totp(pending.get('totp_secret'), data.get('totp_code')):
        return json_error('Neplatný 2FA kód', 400)
    recovery_codes = generate_recovery_codes()
    auth_config['admin'] = {
        'username': pending['username'],
        'password_hash': pending['password_hash'],
        'totp_secret': normalize_totp_secret(pending['totp_secret']),
        'recovery_codes': hash_recovery_codes(recovery_codes),
        'session_version': 1,
        'failed_login_count': 0,
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'pushover': {
            'app_token': '',
            'user_key': '',
            'device': '',
            'notify_manual_backups': False,
            'notify_auto_backups': False,
            'notify_security': True,
        },
    }
    save_auth_config(auth_config)
    session.clear()
    return jsonify({'success': True, 'recovery_codes': recovery_codes})

@app.route('/api/auth/login', methods=['POST'])
def auth_login_api():
    auth_config = load_auth_config()
    admin = auth_config.get('admin')
    if not admin:
        return json_error('Admin účet ešte neexistuje', 403)
    data = request.get_json(silent=True) or {}
    username_ok = hmac.compare_digest(str(data.get('username', '')).strip(), admin.get('username', ''))
    password_ok = check_password_hash(admin.get('password_hash', ''), str(data.get('password', '')))
    totp_ok = verify_totp(admin.get('totp_secret'), data.get('totp_code'))
    if not (username_ok and password_ok and totp_ok):
        admin['failed_login_count'] = int(admin.get('failed_login_count', 0)) + 1
        auth_config['admin'] = admin
        save_auth_config(auth_config)
        if admin['failed_login_count'] in (3, 5) or admin['failed_login_count'] % 10 == 0:
            notify_pushover('security', 'Proxmox Backup Manager', f"Neúspešné prihlásenia: {admin['failed_login_count']}", priority=1)
        return json_error('Neplatné prihlasovacie údaje alebo 2FA kód', 401)
    admin['failed_login_count'] = 0
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    csrf = login_session(admin)
    warning = notify_pushover('security', 'Proxmox Backup Manager', f"Úspešné prihlásenie používateľa {admin['username']}")
    return jsonify({'success': True, 'csrf_token': csrf, 'username': admin['username'], 'pushover_warning': warning})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout_api():
    clear_auth_session()
    return jsonify({'success': True})

def start_pushover_recovery(kind, username, recovery_code, require_password=None):
    auth_config, admin = current_admin_or_error()
    if not hmac.compare_digest(str(username or '').strip(), admin.get('username', '')):
        raise ValueError('Neplatné recovery údaje')
    if require_password is not None and not check_password_hash(admin.get('password_hash', ''), str(require_password)):
        raise ValueError('Neplatné recovery údaje')
    recovery_index = find_recovery_code(admin, recovery_code)
    if recovery_index is None:
        raise ValueError('Neplatný alebo už použitý recovery kód')
    if not pushover_configured(admin):
        raise RuntimeError('Pushover nie je nastavený, recovery cez web nie je dostupné')
    pushover_code = str(secrets.randbelow(1000000)).zfill(6)
    session[f'pending_{kind}'] = {
        'kind': kind,
        'code_hash': generate_password_hash(pushover_code),
        'recovery_index': recovery_index,
        'created_at': time.time(),
    }
    send_pushover_message(admin, 'Proxmox Backup Manager recovery', f"Overovací kód: {pushover_code}", priority=1)
    return auth_config, admin

def pending_recovery(kind):
    pending = session.get(f'pending_{kind}') or {}
    if not pending or time.time() - float(pending.get('created_at', 0)) > 600:
        raise ValueError('Recovery kód expiroval, spusti obnovu znova')
    return pending

@app.route('/api/auth/recovery/start', methods=['POST'])
def auth_recovery_start_api():
    data = request.get_json(silent=True) or {}
    try:
        start_pushover_recovery('password_recovery', data.get('username'), data.get('recovery_code'))
        return jsonify({'success': True, 'message': 'Pushover overovací kód bol odoslaný'})
    except RuntimeError as exc:
        return json_error(str(exc), 503)
    except Exception:
        return json_error('Neplatné recovery údaje', 400)

@app.route('/api/auth/recovery/complete', methods=['POST'])
def auth_recovery_complete_api():
    data = request.get_json(silent=True) or {}
    new_password = str(data.get('new_password', ''))
    if not password_is_strong_enough(new_password):
        return json_error('Nové heslo musí mať aspoň 10 znakov')
    try:
        pending = pending_recovery('password_recovery')
        if not check_password_hash(pending.get('code_hash', ''), str(data.get('pushover_code', ''))):
            return json_error('Neplatný Pushover kód', 400)
        auth_config, admin = current_admin_or_error()
        recovery_index = int(pending['recovery_index'])
        admin['password_hash'] = generate_password_hash(new_password)
        admin['recovery_codes'][recovery_index]['used'] = True
        admin['recovery_codes'][recovery_index]['used_at'] = now_iso()
        admin['updated_at'] = now_iso()
        increment_session_version(admin)
        auth_config['admin'] = admin
        save_auth_config(auth_config)
        session.clear()
        warning = notify_pushover('security', 'Proxmox Backup Manager', 'Heslo bolo obnovené cez recovery')
        return jsonify({'success': True, 'pushover_warning': warning})
    except ValueError as exc:
        return json_error(str(exc), 400)

@app.route('/api/auth/totp-recovery/start', methods=['POST'])
def auth_totp_recovery_start_api():
    data = request.get_json(silent=True) or {}
    try:
        _auth_config, admin = start_pushover_recovery('totp_recovery', data.get('username'), data.get('recovery_code'), require_password=data.get('password'))
        new_secret = generate_totp_secret()
        pending = session.get('pending_totp_recovery') or {}
        pending['totp_secret'] = new_secret
        session['pending_totp_recovery'] = pending
        otpauth_uri = build_otpauth_uri(admin.get('username', 'admin'), new_secret)
        return jsonify({'success': True, 'message': 'Pushover overovací kód bol odoslaný', 'totp_secret': new_secret, 'otpauth_uri': otpauth_uri, 'qr_data_uri': build_qr_data_uri(otpauth_uri)})
    except RuntimeError as exc:
        return json_error(str(exc), 503)
    except Exception:
        return json_error('Neplatné recovery údaje', 400)

@app.route('/api/auth/totp-recovery/complete', methods=['POST'])
def auth_totp_recovery_complete_api():
    data = request.get_json(silent=True) or {}
    try:
        pending = pending_recovery('totp_recovery')
        if not check_password_hash(pending.get('code_hash', ''), str(data.get('pushover_code', ''))):
            return json_error('Neplatný Pushover kód', 400)
        if not verify_totp(pending.get('totp_secret'), data.get('totp_code')):
            return json_error('Neplatný nový 2FA kód', 400)
        auth_config, admin = current_admin_or_error()
        recovery_codes = generate_recovery_codes()
        admin['totp_secret'] = normalize_totp_secret(pending['totp_secret'])
        admin['recovery_codes'] = hash_recovery_codes(recovery_codes)
        admin['updated_at'] = now_iso()
        increment_session_version(admin)
        auth_config['admin'] = admin
        save_auth_config(auth_config)
        session.clear()
        warning = notify_pushover('security', 'Proxmox Backup Manager', '2FA bolo obnovené cez recovery')
        return jsonify({'success': True, 'recovery_codes': recovery_codes, 'pushover_warning': warning})
    except ValueError as exc:
        return json_error(str(exc), 400)

@app.route('/api/account')
def account_api():
    _auth_config, admin = current_admin_or_error()
    pushover = admin.get('pushover', {})
    return jsonify({'success': True, 'username': admin.get('username', ''), 'recovery_codes_remaining': recovery_codes_remaining(admin), 'pushover': {
        'configured': pushover_configured(admin),
        'app_token_masked': masked_secret(pushover.get('app_token', '')),
        'user_key_masked': masked_secret(pushover.get('user_key', '')),
        'device': pushover.get('device', ''),
        'notify_manual_backups': bool(pushover.get('notify_manual_backups', False)),
        'notify_auto_backups': bool(pushover.get('notify_auto_backups', False)),
        'notify_security': bool(pushover.get('notify_security', True)),
    }})

@app.route('/api/account/username', methods=['POST'])
def account_username_api():
    auth_config, admin = current_admin_or_error()
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    if not username:
        return json_error('Používateľské meno je povinné')
    if not check_password_hash(admin.get('password_hash', ''), str(data.get('password', ''))):
        return json_error('Neplatné heslo', 401)
    admin['username'] = username
    admin['updated_at'] = now_iso()
    increment_session_version(admin)
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    login_session(admin)
    warning = notify_pushover('security', 'Proxmox Backup Manager', f"Používateľské meno bolo zmenené na {username}")
    return jsonify({'success': True, 'username': username, 'csrf_token': session['csrf_token'], 'pushover_warning': warning})

@app.route('/api/account/password', methods=['POST'])
def account_password_api():
    auth_config, admin = current_admin_or_error()
    data = request.get_json(silent=True) or {}
    if not check_password_hash(admin.get('password_hash', ''), str(data.get('current_password', ''))):
        return json_error('Neplatné aktuálne heslo', 401)
    if not verify_totp(admin.get('totp_secret'), data.get('totp_code')):
        return json_error('Neplatný 2FA kód', 400)
    new_password = str(data.get('new_password', ''))
    if not password_is_strong_enough(new_password):
        return json_error('Nové heslo musí mať aspoň 10 znakov')
    admin['password_hash'] = generate_password_hash(new_password)
    admin['updated_at'] = now_iso()
    increment_session_version(admin)
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    login_session(admin)
    warning = notify_pushover('security', 'Proxmox Backup Manager', 'Heslo bolo zmenené')
    return jsonify({'success': True, 'csrf_token': session['csrf_token'], 'pushover_warning': warning})

@app.route('/api/account/totp/start', methods=['POST'])
def account_totp_start_api():
    _auth_config, admin = current_admin_or_error()
    data = request.get_json(silent=True) or {}
    if not check_password_hash(admin.get('password_hash', ''), str(data.get('password', ''))):
        return json_error('Neplatné heslo', 401)
    if not verify_totp(admin.get('totp_secret'), data.get('totp_code')):
        return json_error('Neplatný aktuálny 2FA kód', 400)
    new_secret = generate_totp_secret()
    session['pending_account_totp'] = {'totp_secret': new_secret, 'created_at': time.time()}
    otpauth_uri = build_otpauth_uri(admin.get('username', 'admin'), new_secret)
    return jsonify({'success': True, 'totp_secret': new_secret, 'otpauth_uri': otpauth_uri, 'qr_data_uri': build_qr_data_uri(otpauth_uri)})

@app.route('/api/account/totp/complete', methods=['POST'])
def account_totp_complete_api():
    auth_config, admin = current_admin_or_error()
    pending = session.get('pending_account_totp') or {}
    if not pending or time.time() - float(pending.get('created_at', 0)) > 900:
        return json_error('Reset 2FA expiroval, spusti ho znova')
    data = request.get_json(silent=True) or {}
    if not verify_totp(pending.get('totp_secret'), data.get('totp_code')):
        return json_error('Neplatný nový 2FA kód', 400)
    recovery_codes = generate_recovery_codes()
    admin['totp_secret'] = normalize_totp_secret(pending['totp_secret'])
    admin['recovery_codes'] = hash_recovery_codes(recovery_codes)
    admin['updated_at'] = now_iso()
    increment_session_version(admin)
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    session.pop('pending_account_totp', None)
    login_session(admin)
    warning = notify_pushover('security', 'Proxmox Backup Manager', '2FA bolo resetované v účte')
    return jsonify({'success': True, 'recovery_codes': recovery_codes, 'csrf_token': session['csrf_token'], 'pushover_warning': warning})

@app.route('/api/account/recovery-codes', methods=['POST'])
def account_recovery_codes_api():
    auth_config, admin = current_admin_or_error()
    data = request.get_json(silent=True) or {}
    if not check_password_hash(admin.get('password_hash', ''), str(data.get('password', ''))):
        return json_error('Neplatné heslo', 401)
    if not verify_totp(admin.get('totp_secret'), data.get('totp_code')):
        return json_error('Neplatný 2FA kód', 400)
    recovery_codes = generate_recovery_codes()
    admin['recovery_codes'] = hash_recovery_codes(recovery_codes)
    admin['updated_at'] = now_iso()
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    warning = notify_pushover('security', 'Proxmox Backup Manager', 'Recovery kódy boli regenerované')
    return jsonify({'success': True, 'recovery_codes': recovery_codes, 'pushover_warning': warning})

@app.route('/api/account/pushover', methods=['POST'])
def account_pushover_api():
    auth_config, admin = current_admin_or_error()
    data = request.get_json(silent=True) or {}
    pushover = admin.get('pushover', {})
    app_token = str(data.get('app_token', '')).strip() or pushover.get('app_token', '')
    user_key = str(data.get('user_key', '')).strip() or pushover.get('user_key', '')
    device = str(data.get('device', '')).strip()
    if app_token and user_key:
        try:
            validate_pushover_config(app_token, user_key, device)
        except RuntimeError as exc:
            return json_error(str(exc), 400)
    admin['pushover'] = {
        'app_token': app_token,
        'user_key': user_key,
        'device': device,
        'notify_manual_backups': bool(data.get('notify_manual_backups', False)),
        'notify_auto_backups': bool(data.get('notify_auto_backups', False)),
        'notify_security': bool(data.get('notify_security', True)),
    }
    admin['updated_at'] = now_iso()
    auth_config['admin'] = admin
    save_auth_config(auth_config)
    warning = notify_pushover('security', 'Proxmox Backup Manager', 'Pushover nastavenia boli uložené')
    return jsonify({'success': True, 'pushover_warning': warning})

@app.route('/api/account/pushover/test', methods=['POST'])
def account_pushover_test_api():
    _auth_config, admin = current_admin_or_error()
    try:
        send_pushover_message(admin, 'Proxmox Backup Manager', 'Test Pushover notifikácie pre Proxmox Backup Manager')
        return jsonify({'success': True})
    except RuntimeError as exc:
        return json_error(str(exc), 400)

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
        'backup_categories': BACKUP_CATEGORIES,
        'restore_categories': RESTORE_CATEGORIES,
    })

@app.route('/api/files')
def get_files():
    """API endpoint pre zoznam súborov na zálohovanie"""
    config = load_config()
    return jsonify(items_with_recovery_metadata(config['backup_files']))

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
    return jsonify({'success': True, 'selected': selected, 'backup_files': items_with_recovery_metadata(config['backup_files'])})

@app.route('/api/auto-files')
def get_auto_files():
    """API endpoint pre zoznam súborov automatickej zálohy."""
    config = load_config()
    return jsonify(items_with_recovery_metadata(config['auto_backup_files']))

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
    return jsonify({'success': True, 'selected': selected, 'backup_files': items_with_recovery_metadata(config['auto_backup_files'])})

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
        result = notify_backup_result(result, 'manual')
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
        result = notify_backup_result(result, 'auto')
        return jsonify(result)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/backups')
def list_backups_api():
    """Zlúčený zoznam lokálnych a FTP archívov."""
    config = load_config()
    result = merge_backup_history_with_ftp(config)
    return jsonify(result)

@app.route('/api/backups/<backup_id>/cache', methods=['POST'])
def cache_backup_api(backup_id):
    """Stiahne FTP-only archív do lokálneho backup adresára."""
    config = load_config()
    try:
        entry, archive_path, cached = ensure_backup_cached(backup_id, config)
        return jsonify({
            'success': True,
            'cached': cached,
            'archive': {
                'id': entry.get('id'),
                'filename': entry.get('filename') or os.path.basename(archive_path),
                'local_path': archive_path,
                'size': get_file_size(archive_path),
            },
        })
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/backups/<backup_id>/download')
def download_backup_api(backup_id):
    """Stiahne archív cez browser; FTP-only archívy najprv cacheuje lokálne."""
    config = load_config()
    try:
        entry, archive_path, _cached = ensure_backup_cached(backup_id, config)
        filename = safe_backup_filename(entry.get('filename') or os.path.basename(archive_path))
        annotate_backup_history_entry(entry.get('id'), {'downloaded_at': now_iso()})
        return send_file(archive_path, as_attachment=True, download_name=filename)
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
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
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restore/archives')
def restore_archives_api():
    """Archívy z histórie, ktoré sú stále lokálne dostupné na restore."""
    return jsonify({'success': True, 'archives': list_restore_archives()})

@app.route('/api/restore/preview/<backup_id>')
def restore_preview_api(backup_id):
    """Preview obnoviteľných whitelisted ciest v lokálnom alebo FTP-cached archíve."""
    try:
        entry, archive_path, cached = ensure_backup_cached(backup_id)
        return jsonify({
            'success': True,
            'cached': cached,
            'archive_source': restore_archive_source(entry, cached),
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
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restore/preview/<backup_id>/members')
def restore_members_api(backup_id):
    """Detail členov archívu pre jednu obnoviteľnú cestu."""
    restore_path = request.args.get('path', '')
    try:
        _entry, archive_path, _cached = ensure_backup_cached(backup_id)
        if restore_path:
            detail = restore_archive_member_details(archive_path, restore_path)
        else:
            detail = restore_archive_all_member_details(archive_path)
        return jsonify({'success': True, **detail})
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
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
        result = run_restore_job(
            data.get('backup_id'),
            data.get('paths') or [],
            source_config,
            stage_paths=data.get('stage_paths') or [],
            acknowledged_paths=data.get('acknowledged_paths') or [],
        )
        return jsonify(result)
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except RestoreAcknowledgementRequired as e:
        return jsonify({'success': False, 'error': str(e), 'requires_acknowledgement': e.paths}), 400
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/recovery/overview')
def recovery_overview_api():
    """Pripravenosť na obnovu na novom HW, klasifikované položky, checklist a index wiki."""
    return jsonify(build_recovery_overview(load_config()))

@app.route('/api/recovery/migration', methods=['GET', 'POST'])
def recovery_migration_api():
    def update(state, payload):
        data = migration_json_body({'method', 'old_host', 'new_host'})
        method = data.get('method')
        if not isinstance(method, str) or method not in ('disk_move', 'side_by_side'):
            raise ValueError('Vyber platný spôsob migrácie.')
        if state['method'] and state['method'] != method:
            raise ValueError('Pred zmenou spôsobu resetuj existujúcu migráciu.')
        old_host = validate_migration_host(data.get('old_host', state['old_host']))
        new_host = validate_migration_host(data.get('new_host', state['new_host']))
        if method == 'side_by_side' and old_host['ip'] and old_host['ip'] == new_host['ip']:
            # Rovnaký hostname je pri prenose 1:1 žiaduci; súbeh rozlišuje dočasná IP nového hosta.
            raise ValueError('Pri súbežnej prevádzke musí mať nový host inú IP ako starý (dočasnú adresu).')
        state.update(method=method, old_host=old_host, new_host=new_host)
        state['started_at'] = state['started_at'] or migration_now()
        if method == 'side_by_side':
            remember_migration_guests(state, build_migration_payload(state)['guests'])
    return migration_api_response(update if request.method == 'POST' else None)

@app.route('/api/recovery/migration/steps/<step_id>', methods=['POST'])
def recovery_migration_step_api(step_id):
    def update(state, payload):
        data = migration_json_body({'completed'})
        if step_id not in {step['id'] for step in payload['steps']}:
            raise ValueError('Krok neexistuje pre zvolený spôsob migrácie.')
        if type(data.get('completed')) is not bool:
            raise ValueError('completed musí byť boolean.')
        remember_migration_guests(state, payload['guests'])
        state['steps'][step_id] = {'completed': data['completed'], 'updated_at': migration_now()}
        # Cutover je zámerne iba UI poistka, API môže evidovať ručné rozhodnutie.
    return migration_api_response(update)

@app.route('/api/recovery/migration/guests/<vmid>', methods=['POST'])
def recovery_migration_guest_api(vmid):
    def update(state, payload):
        data = migration_json_body({'status', 'note'})
        guest_id = validate_migration_vmid(vmid)
        if state['method'] != 'side_by_side':
            raise ValueError('Zoznam hostí je dostupný iba pri presune vedľa starého hosta.')
        guest = next((guest for guest in payload['guests'] if str(guest['vmid']) == guest_id), None)
        if not guest:
            raise ValueError('Hosť s týmto VMID nie je v inventári migrácie.')
        status = data.get('status', guest['status'])
        if not isinstance(status, str) or status not in MIGRATION_GUEST_TRANSITIONS:
            raise ValueError('Neplatný stav hosťa.')
        if status != guest['status'] and status not in guest['allowed_transitions']:
            raise ValueError('Nepovolený prechod. Najprv vypni hosťa na starom, potom obnov a over na novom.')
        note = data.get('note', guest['note'])
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError('Poznámka musí byť text do 2000 znakov. Nevkladaj heslá.')
        remember_migration_guests(state, payload['guests'])
        state['guests'][guest_id].update(status=status, note=note, updated_at=migration_now())
    return migration_api_response(update)

@app.route('/api/recovery/migration/reset', methods=['POST'])
def recovery_migration_reset_api():
    try:
        migration_json_body(set())
        running = running_migration_job()
        if running:
            raise ValueError(f'Počas operácie „{running.get("title")}“ nemožno resetovať migráciu. Počkaj na jej dokončenie.')
        forget_migration_target()
        forget_migration_transfer()
    except ValueError as exc:
        return json_error(str(exc))
    except (OSError, RuntimeError):
        return json_error('Stav operácií alebo nového hosta sa nedá načítať; reset nebol vykonaný.', 503)
    return migration_api_response(reset=True)

def migration_operation_response(starter):
    try:
        job = starter()
        return no_store(jsonify({'success': True, 'job': public_migration_job(job)}))
    except ValueError as exc:
        return no_store(*json_error(str(exc)))
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Operáciu nemožno spustiť.'
        return no_store(*json_error(message, 503))

def migration_guest_for_operation(vmid, allowed_statuses):
    guest_id = validate_migration_vmid(vmid)
    state = load_migration_state()
    guest = state['guests'].get(guest_id)
    if not guest:
        raise ValueError('Hosť nie je v zozname migrácie. Ulož spôsob migrácie s dostupným inventárom.')
    if guest['status'] not in allowed_statuses:
        raise ValueError(f'Hosť je v stave „{guest["status"]}“; táto operácia sa v ňom nedá spustiť.')
    return int(guest_id)

@app.route('/api/recovery/migration/transfer')
def recovery_migration_transfer_api():
    return no_store(jsonify({'success': True, **migration_transfer_info()}))

@app.route('/api/recovery/migration/transfer/config-db', methods=['POST'])
def recovery_migration_config_db_api():
    def starter():
        data = migration_json_body({'confirm'})
        if data.get('confirm') != 'KOPIA':
            raise ValueError('Potvrď prenos config.db textom KOPIA.')
        transfer = load_migration_transfer()
        if transfer['config_db']:
            raise ValueError('config.db už bol prenesený. Opakovanie je možné iba po resete migrácie a novej inštalácii cieľa.')
        return start_migration_operation('config-db', 'Kópia PVE konfigurácie (config.db)', run_migration_config_db, require_fresh=True)
    return migration_operation_response(starter)

def migration_file_paths(data):
    paths = data.get('paths')
    if not isinstance(paths, list) or not paths or len(paths) > len(MIGRATION_FILE_PATHS) \
            or any(not isinstance(path, str) or path not in MIGRATION_FILE_PATHS for path in paths):
        raise ValueError('Vyber aspoň jednu povolenú položku na prenos.')
    return sorted(set(paths), key=[item['path'] for item in MIGRATION_FILE_ITEMS].index)

@app.route('/api/recovery/migration/transfer/files-diff', methods=['POST'])
def recovery_migration_files_diff_api():
    def starter():
        paths = migration_file_paths(migration_json_body({'paths'}))
        return start_migration_operation('files-diff', 'Rozdiely súborov hosta',
                                         lambda ctx, mctx: run_migration_files_diff(ctx, mctx, paths))
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/transfer/files-apply', methods=['POST'])
def recovery_migration_files_apply_api():
    def starter():
        data = migration_json_body({'paths', 'confirm'})
        paths = migration_file_paths(data)
        if data.get('confirm') is not True:
            raise ValueError('Potvrď, že si skontroloval rozdiely vybraných položiek.')
        return start_migration_operation('files-apply', 'Prenos súborov hosta',
                                         lambda ctx, mctx: run_migration_files_apply(ctx, mctx, paths))
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/transfer/network', methods=['POST'])
def recovery_migration_network_api():
    def starter():
        mapping = migration_json_body({'mapping'}).get('mapping') or {}
        if not isinstance(mapping, dict) or len(mapping) > 32 or any(
                not isinstance(k, str) or not isinstance(v, str) or len(k) > 32 or len(v) > 32 for k, v in mapping.items()):
            raise ValueError('Neplatné mapovanie sieťových kariet.')
        return start_migration_operation('network', 'Návrh siete pre nový HW',
                                         lambda ctx, mctx: run_migration_network(ctx, mctx, mapping))
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/guests/<vmid>/move', methods=['POST'])
def recovery_migration_guest_move_api(vmid):
    def starter():
        data = migration_json_body({'dump_dir', 'target_storage', 'confirm'})
        guest_id = migration_guest_for_operation(vmid, ('pending', 'stopped_on_old'))
        if data.get('confirm') is not True:
            raise ValueError('Potvrď presun: hosť sa na starom hoste vypne.')
        dump_dir = validate_remote_dir(data.get('dump_dir'))
        storage = validate_storage_id(data.get('target_storage'))
        mctx = MigrationContext(require_target=True)
        if not mctx.transfer.get('config_db'):
            raise ValueError('Najprv v kroku Prenos konfigurácie prenes config.db (definície hostí musia byť na novom hoste).')
        if mctx.app_guest and mctx.app_guest['vmid'] == guest_id:
            raise ValueError('Toto je LXC, v ktorom beží táto appka. Presúva sa ručne ako posledný pri prepnutí.')
        if storage not in migration_guest_storages(mctx.facts):
            raise ValueError('Cieľový storage nie je medzi storage pre disky hostí.')
        guest = mctx.guest_info(guest_id)
        return start_migration_job('guest-move', f'Presun {guest["type"]} {guest_id} {guest["name"]}'.strip(),
                                   lambda ctx: run_migration_guest_move(ctx, mctx, guest_id, dump_dir, storage), mctx.secrets)
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/guests/<vmid>/start', methods=['POST'])
def recovery_migration_guest_start_api(vmid):
    def starter():
        migration_json_body(set())
        guest_id = migration_guest_for_operation(vmid, ('restored_on_new',))
        return start_migration_operation('guest-start', f'Štart hosťa {guest_id} na novom hoste',
                                         lambda ctx, mctx: run_migration_guest_start(ctx, mctx, guest_id))
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/guests/<vmid>/rollback', methods=['POST'])
def recovery_migration_guest_rollback_api(vmid):
    def starter():
        data = migration_json_body({'confirm'})
        guest_id = migration_guest_for_operation(vmid, ('stopped_on_old', 'restored_on_new'))
        if data.get('confirm') is not True:
            raise ValueError('Potvrď vrátenie hosťa na starý host.')
        return start_migration_operation('guest-rollback', f'Vrátenie hosťa {guest_id} na starý host',
                                         lambda ctx, mctx: run_migration_guest_rollback(ctx, mctx, guest_id))
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/cutover', methods=['POST'])
def recovery_migration_cutover_api():
    def starter():
        data = migration_json_body({'confirm'})
        if data.get('confirm') != 'PREPNUT':
            raise ValueError('Potvrď prepnutie textom PREPNUT.')
        mctx = MigrationContext(require_target=True)
        if not mctx.transfer.get('config_db'):
            raise ValueError('Prepnutie vyžaduje prenesený config.db.')
        payload = build_migration_payload(mctx.state)
        if not payload['cutover_ready']:
            raise ValueError('Najprv over alebo vedome vynechaj všetkých hostí (okrem LXC s appkou).')
        return start_migration_job('cutover', 'Prepnutie zálohovania a autostartu', lambda ctx: run_migration_cutover(ctx, mctx), mctx.secrets)
    return migration_operation_response(starter)

@app.route('/api/recovery/migration/target', methods=['GET', 'POST', 'DELETE'])
def recovery_migration_target_api():
    """Pripojenie nového hosta (heslo sa nikdy nevracia) + spustenie kontroly cieľa."""
    try:
        if request.method == 'GET':
            target, error = safe_public_migration_target()
            return no_store(jsonify({'success': True, 'target': target, 'target_error': error,
                                     'jobs': [public_migration_job(job) for job in list_migration_jobs()[:5]]}))
        if request.method == 'DELETE':
            running = running_migration_job()
            if running:
                raise ValueError('Počas bežiacej operácie nemožno zabudnúť pripojenie nového hosta.')
            forget_migration_target()
            return no_store(jsonify({'success': True, 'target': None}))

        data = migration_json_body({'host', 'port', 'password'})
        host = validate_migration_ssh_host(data.get('host'))
        port = data.get('port', 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('SSH port musí byť celé číslo od 1 do 65535.')
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 4096 or '\x00' in password:
            raise ValueError('Root heslo môže mať najviac 4096 znakov.')
        old_ssh = migration_old_ssh()
        if old_ssh and old_ssh['host'] == host:
            raise ValueError('Nový host musí mať inú adresu ako starý host uložený v Nastaveniach.')
        with json_state_lock(MIGRATION_TARGET_FILE):
            try:
                existing = load_migration_target()
            except RuntimeError:
                existing = None
            if not password:
                if existing and existing['host'] == host and existing['port'] == port and existing['password']:
                    password = existing['password']
                else:
                    raise ValueError('Zadaj root heslo nového hosta.')
            target = {'host': host, 'port': port, 'username': 'root', 'password': password,
                      'saved_at': migration_now(), 'last_check': None}
            write_json_state(MIGRATION_TARGET_FILE, target)
        job = start_migration_preflight(target)
        return no_store(jsonify({'success': True, 'target': public_migration_target(target),
                                 'job': public_migration_job(job)}))
    except ValueError as exc:
        return no_store(*json_error(str(exc)))
    except (OSError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Pripojenie nového hosta sa nedá uložiť.'
        return no_store(*json_error(message, 503))

@app.route('/api/recovery/migration/target/check', methods=['POST'])
def recovery_migration_target_check_api():
    try:
        migration_json_body(set())
        target = load_migration_target()
        if not target or not target['password']:
            raise ValueError('Najprv ulož pripojenie nového hosta vrátane root hesla.')
        job = start_migration_preflight(target)
        return no_store(jsonify({'success': True, 'job': public_migration_job(job)}))
    except ValueError as exc:
        return no_store(*json_error(str(exc)))
    except (OSError, RuntimeError) as exc:
        return no_store(*json_error(str(exc) if isinstance(exc, RuntimeError) else 'Kontrolu nemožno spustiť.', 503))

@app.route('/api/recovery/migration/jobs')
def recovery_migration_jobs_api():
    try:
        return no_store(jsonify({'success': True, 'jobs': [public_migration_job(job) for job in list_migration_jobs()]}))
    except (OSError, RuntimeError):
        return no_store(*json_error('Záznam operácií sa nedá načítať.', 503))

@app.route('/api/recovery/migration/jobs/<job_id>')
def recovery_migration_job_api(job_id):
    try:
        job = next((job for job in list_migration_jobs() if job['id'] == job_id), None)
    except (OSError, RuntimeError):
        return no_store(*json_error('Záznam operácií sa nedá načítať.', 503))
    if not job:
        return no_store(*json_error('Operácia neexistuje.', 404))
    return no_store(jsonify({'success': True, 'job': public_migration_job(job)}))

def redact_migration_comparison(value, passwords):
    """Poistka aj pre nečakané tajomstvo v parsovanom názve, nie iba SSH chybe."""
    if isinstance(value, str):
        for password in sorted(set(passwords), key=len, reverse=True):
            if password:
                value = value.replace(password, '[skryté]')
        return value
    if isinstance(value, list):
        return [redact_migration_comparison(item, passwords) for item in value]
    if isinstance(value, dict):
        # Úroveň je pevný enum, nie text z SSH. Heslo "error" nesmie skryť chybu.
        return {key: item if key == 'level' and item in ('ok', 'info', 'warning', 'error', 'unknown')
                else redact_migration_comparison(item, passwords) for key, item in value.items()}
    return value

@app.route('/api/recovery/migration/compare', methods=['POST'])
def recovery_migration_compare_api():
    """Jednorazová read-only kontrola; heslo ani report sa nikdy neukladajú."""
    try:
        data = migration_json_body({'new_host'})
        new = data.get('new_host')
        if not isinstance(new, dict) or set(new) - {'host', 'port', 'password'}:
            raise ValueError('Zadaj nový SSH cieľ: host, port a jednorazové heslo používateľa root.')
        host = validate_migration_ssh_host(new.get('host'))
        port = new.get('port', 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('SSH port musí byť celé číslo od 1 do 65535.')
        password = new.get('password')
        if not isinstance(password, str) or not password or len(password) > 4096 or '\x00' in password:
            raise ValueError('Jednorazové root heslo je povinné a môže mať najviac 4096 znakov.')
        source = sanitize_source_config(load_config().get('source_config'))
        if source['mode'] != 'remote_ssh':
            raise ValueError('Pre porovnanie nastav starý host v Nastaveniach ako Remote SSH a ulož údaje.')
        RemoteSshBackupSource(source).validate()
        old_ssh = source['ssh']
        old_ssh['host'] = validate_migration_ssh_host(old_ssh['host'])
        new_ssh = {'host': host, 'port': port, 'username': 'root', 'password': password}
        if old_ssh['host'] == host and old_ssh['port'] == port:
            raise ValueError('Nový SSH cieľ je rovnaký ako starý. Zadaj druhý host.')
        old_result = collect_migration_host_facts(old_ssh)
        new_result = collect_migration_host_facts(new_ssh)
        rows = build_migration_comparison(old_result, new_result)
        payload = {
            'success': True, 'read_only': True, 'compared_at': migration_now(),
            'old_host': old_result, 'new_host': new_result, 'rows': rows,
            'summary': {level: sum(row['level'] == level for row in rows)
                        for level in ('ok', 'info', 'warning', 'error', 'unknown')},
        }
        response = jsonify(redact_migration_comparison(payload, [old_ssh['password'], password]))
    except ValueError as exc:
        response, code = json_error(str(exc))
        response.headers['Cache-Control'] = 'no-store'
        return response, code
    except Exception:
        response, code = json_error('Porovnanie sa nepodarilo dokončiť. Over SSH nastavenia a skús znova.', 503)
        response.headers['Cache-Control'] = 'no-store'
        return response, code
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/recovery/checklist')
def recovery_checklist_api():
    """Krokový postup obnovy Proxmox hosta na novom HW + uložený postup."""
    return recovery_progress_response()

@app.route('/api/recovery/checklist/steps/<step_id>', methods=['POST'])
def recovery_checklist_step_api(step_id):
    def update(state):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'completed'} or type(data['completed']) is not bool:
            raise ValueError('Očakáva sa JSON {"completed": true|false}.')
        if step_id not in {step['id'] for step in RECOVERY_CHECKLIST}:
            raise ValueError('Krok postupu obnovy neexistuje.')
        if data['completed']:
            state['steps'][step_id] = {'completed': True, 'updated_at': migration_now()}
        else:
            state['steps'].pop(step_id, None)
    return recovery_progress_response(update)

@app.route('/api/recovery/checklist/reset', methods=['POST'])
def recovery_checklist_reset_api():
    return recovery_progress_response(reset=True)

@app.route('/api/recovery/wiki')
def recovery_wiki_index_api():
    """Zoznam článkov internej DR wiki."""
    return jsonify({'success': True, 'articles': wiki_index()})

@app.route('/api/recovery/wiki/<slug>')
def recovery_wiki_article_api(slug):
    """Jeden článok DR wiki vrátane položiek, ktoré naň odkazujú."""
    article = find_wiki_article(slug)
    if not article:
        return jsonify({'success': False, 'error': 'Článok neexistuje'}), 404
    related = [
        {'path': item['path'], 'name': item.get('name', item['path'])}
        for item in DEFAULT_BACKUP_FILES
        if recovery_profile_for_path(item['path']).get('wiki_slug') == slug
    ]
    return jsonify({'success': True, 'article': article, 'related_items': related})

@app.route('/api/recovery/handbook')
def recovery_handbook_api():
    """Offline DR príručka ako samostatný HTML súbor (bez hesiel a obsahu citlivých súborov)."""
    context = build_handbook_context(load_config())
    html = render_template('handbook.html', **context)
    node = re.sub(r'[^A-Za-z0-9_-]', '_', context.get('node') or 'proxmox')
    filename = f"DR-prirucka-{node}-{datetime.now():%Y%m%d}.html"
    response = app.response_class(html, mimetype='text/html')
    disposition = 'inline' if request.args.get('inline') == '1' else 'attachment'
    response.headers['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/recovery/snapshot/<backup_id>')
def recovery_snapshot_api(backup_id):
    """DR metadata snapshot pôvodného hosta z backup-info/ (REFERENCE ONLY, iba whitelisted výstupy)."""
    try:
        entry, archive_path, cached = ensure_backup_cached(backup_id)
        return jsonify({
            'success': True,
            'reference_only': True,
            'cached': cached,
            'archive': {
                'id': entry.get('id'),
                'filename': entry.get('filename') or os.path.basename(archive_path),
                'timestamp': entry.get('timestamp') or entry.get('date') or '',
                'source_host': entry.get('source_host'),
            },
            'files': read_host_snapshot(archive_path),
        })
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 502
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
        notify_backup_result(result, 'manual')
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
        if freq in ('daily', 'weekly', 'monthly'):
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
    if frequency in ['daily', 'weekly', 'monthly']:
        config = load_config()
        config['auto_backup_frequency'] = frequency
        save_config(config)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('APP_PORT', '5000')))
