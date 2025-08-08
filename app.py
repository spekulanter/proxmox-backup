#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import os
import json
import tarfile
import ftplib
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
import threading
import time

app = Flask(__name__)
app.secret_key = 'proxmox-backup-secret-key-change-in-production'

# Konfiguračný súbor
CONFIG_FILE = 'backup_config.json'
BACKUP_HISTORY_FILE = 'backup_history.json'

# Predvolené súbory na zálohovanie
DEFAULT_BACKUP_FILES = [
    {'path': '/etc/pve/', 'name': 'PVE Konfigurácia', 'description': 'Hlavná konfigurácia Proxmox (VM, storage, users)', 'critical': True, 'selected': True},
    {'path': '/etc/network/interfaces', 'name': 'Sieťová konfigurácia', 'description': 'Nastavenia sietí, mostov a VLAN', 'critical': True, 'selected': True},
    {'path': '/etc/hosts', 'name': 'Hosts súbor', 'description': 'Mapovanie IP adries a názvov', 'critical': False, 'selected': True},
    {'path': '/etc/hostname', 'name': 'Názov hostiteľa', 'description': 'Identifikácia servera', 'critical': False, 'selected': True},
    {'path': '/etc/resolv.conf', 'name': 'DNS konfigurácia', 'description': 'Nastavenia DNS serverov', 'critical': False, 'selected': True},
    {'path': '/etc/ssl/pve/', 'name': 'SSL certifikáty', 'description': 'Certifikáty pre webové rozhranie', 'critical': False, 'selected': True},
    {'path': '/root/', 'name': 'Root adresár', 'description': 'Skripty a nastavenia administrátora', 'critical': False, 'selected': True},
    {'path': '/var/lib/vz/template/', 'name': 'ISO a šablóny', 'description': 'Obrazy a šablóny pre VM/CT (môže byť veľké)', 'critical': False, 'selected': False},
    {'path': '/etc/cron*', 'name': 'Cron úlohy', 'description': 'Naplánované automatické úlohy', 'critical': False, 'selected': True},
    {'path': '/etc/vzdump.conf', 'name': 'Vzdump konfigurácia', 'description': 'Nastavenia zálohovania VM/CT', 'critical': False, 'selected': True}
]

def load_config():
    """Načítanie konfigurácie z JSON súboru"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'ftp_config': {'host': '', 'username': '', 'password': '', 'port': 21},
        'backup_files': DEFAULT_BACKUP_FILES,
        'auto_backup_enabled': False,
        'auto_backup_frequency': 'monthly'
    }

def save_config(config):
    """Uloženie konfigurácie do JSON súboru"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

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

def test_ftp_connection(host, username, password, port=21):
    """Test FTP pripojenia"""
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port)
        ftp.login(username, password)
        ftp.pwd()  # Test príkazu
        ftp.quit()
        return True, "Pripojenie úspešné"
    except Exception as e:
        return False, f"Chyba pripojenia: {str(e)}"

def create_backup_archive(selected_files, backup_filename):
    """Vytvorenie archívu so zálohou"""
    with tarfile.open(backup_filename, 'w:gz') as tar:
        for file_info in selected_files:
            file_path = file_info['path']
            if os.path.exists(file_path):
                if file_path.endswith('*'):
                    # Spracovanie wildcard ciest (napr. /etc/cron*)
                    import glob
                    for path in glob.glob(file_path):
                        if os.path.exists(path):
                            tar.add(path, arcname=os.path.relpath(path, '/'))
                else:
                    tar.add(file_path, arcname=os.path.relpath(file_path, '/'))
    return True

def upload_to_ftp(local_file, ftp_config):
    """Nahratie súboru na FTP server"""
    try:
        ftp = ftplib.FTP()
        ftp.connect(ftp_config['host'], ftp_config['port'])
        ftp.login(ftp_config['username'], ftp_config['password'])
        
        with open(local_file, 'rb') as f:
            ftp.storbinary(f'STOR {os.path.basename(local_file)}', f)
        
        ftp.quit()
        return True, "Súbor úspešne nahraný na FTP server"
    except Exception as e:
        return False, f"Chyba pri nahrávaní na FTP: {str(e)}"

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

@app.route('/')
def index():
    """Hlavná stránka"""
    config = load_config()
    backup_history = load_backup_history()
    
    selected_count = sum(1 for f in config['backup_files'] if f['selected'])
    critical_selected = sum(1 for f in config['backup_files'] if f['critical'] and f['selected'])
    critical_total = sum(1 for f in config['backup_files'] if f['critical'])
    
    return render_template('index.html', 
                         config=config, 
                         backup_history=backup_history,
                         selected_count=selected_count,
                         critical_selected=critical_selected,
                         critical_total=critical_total)

@app.route('/test_ftp', methods=['POST'])
def test_ftp():
    """Test FTP pripojenia"""
    data = request.get_json()
    success, message = test_ftp_connection(
        data['host'], 
        data['username'], 
        data['password'], 
        data.get('port', 21)
    )
    return jsonify({'success': success, 'message': message})

@app.route('/save_ftp_config', methods=['POST'])
def save_ftp_config():
    """Uloženie FTP konfigurácie"""
    config = load_config()
    config['ftp_config'] = {
        'host': request.form['host'],
        'username': request.form['username'],
        'password': request.form['password'],
        'port': int(request.form.get('port', 21))
    }
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
    selected_files = [f for f in config['backup_files'] if f['selected']]
    
    if not selected_files:
        flash('Vyberte aspoň jeden súbor na zálohovanie', 'error')
        return redirect(url_for('index'))
    
    if not config['ftp_config']['host']:
        flash('Nakonfigurujte FTP server', 'error')
        return redirect(url_for('index'))
    
    # Vytvorenie názvu súboru
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_filename = f'proxmox-backup-{timestamp}.tar.gz'
    temp_path = os.path.join(tempfile.gettempdir(), backup_filename)
    
    try:
        # Vytvorenie archívu
        create_backup_archive(selected_files, temp_path)
        
        # Nahranie na FTP
        success, message = upload_to_ftp(temp_path, config['ftp_config'])
        
        if success:
            # Pridanie do histórie
            history = load_backup_history()
            backup_entry = {
                'id': str(int(time.time())),
                'filename': backup_filename,
                'date': datetime.now().strftime('%d.%m.%Y %H:%M'),
                'size': get_file_size(temp_path),
                'status': 'success'
            }
            history.insert(0, backup_entry)
            save_backup_history(history)
            
            flash('Záloha úspešne vytvorená a nahraná na FTP server!', 'success')
        else:
            flash(f'Chyba pri nahrávaní: {message}', 'error')
            
    except Exception as e:
        flash(f'Chyba pri vytváraní zálohy: {str(e)}', 'error')
    finally:
        # Vymazanie dočasného súboru
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    return redirect(url_for('index'))

@app.route('/delete_backup/<backup_id>')
def delete_backup(backup_id):
    """Vymazanie zálohy z histórie"""
    history = load_backup_history()
    history = [b for b in history if b['id'] != backup_id]
    save_backup_history(history)
    flash('Záloha vymazaná z histórie', 'success')
    return redirect(url_for('index'))

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