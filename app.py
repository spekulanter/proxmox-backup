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

    entry, archive_path, cached = ensure_backup_cached(backup_id)
    available_paths = {item['path'] for item in preview_restore_archive(archive_path)}
    missing = [path for path in clean_paths if path not in available_paths]
    if missing:
        raise ValueError(f'Archív neobsahuje vybrané cesty: {", ".join(missing)}')

    restore_service = RemoteSshRestoreService(source_config)
    result = restore_service.restore(archive_path, clean_paths)
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
    app.run(debug=True, host='0.0.0.0', port=5000)
