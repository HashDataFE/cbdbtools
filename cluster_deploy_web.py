#!/usr/bin/env python3
"""
CBDB Cluster Deployment Web UI
A web-based interface for deploying Cloudberry/Greenplum/HashData clusters.
Runs locally on the Coordinator node - calls deployment scripts directly.
"""

from flask import Flask, render_template, request, jsonify, Response, session
from werkzeug.utils import secure_filename
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime

app = Flask(__name__)

# Generate a stable secret key: persist to file so it survives restarts
# and is shared across gunicorn workers
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_secret_key_file = os.path.join(SCRIPT_DIR, '.flask_secret_key')
if os.environ.get('FLASK_SECRET_KEY'):
    app.secret_key = os.environ['FLASK_SECRET_KEY']
elif os.path.isfile(_secret_key_file):
    with open(_secret_key_file, 'rb') as f:
        app.secret_key = f.read()
else:
    _key = os.urandom(32)
    with open(_secret_key_file, 'wb') as f:
        f.write(_key)
    os.chmod(_secret_key_file, 0o600)
    app.secret_key = _key

app.config['MAX_CONTENT_LENGTH'] = None  # No upload size limit (database packages can exceed 2GB)

# Path to local files
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, 'deploycluster_parameter.sh')
HOSTS_FILE_PATH = os.path.join(SCRIPT_DIR, 'segmenthosts.conf')
UPLOAD_DIR = '/tmp/uploads'

# Saved config params (server-side, shared across threads within single worker process)
# Requires --workers 1 in gunicorn; multiple workers would isolate this state
SAVED_CONFIG = {
    'params': {},
}
SAVED_CONFIG_LOCK = threading.RLock()

# Deployment status (protected by lock)
DEPLOYMENT_STATUS = {
    'running': False,
    'success': None,
    'log_content': '',
    'start_time': None,
    'log_file': None,
    'deploy_type': 'single',
    'phase': '',  # Current deployment phase for progress tracking
}

DEPLOYMENT_LOCK = threading.RLock()

# Phase detection patterns for progress tracking
PHASE_PATTERNS = [
    ('env_init', re.compile(r'Step 1:|Installing Software Dependencies')),
    ('install_db', re.compile(r'Step 6:|Installing database software')),
    ('segment_init', re.compile(r'Step 8:|Setup env on segment')),
    ('cluster_init', re.compile(r'Running gpinitsystem')),
    ('config_done', re.compile(r'Finished cluster initialization|Finished deploy cluster')),
]

# Input validation patterns
IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
HOSTNAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')
PORT_PATTERN = re.compile(r'^\d{1,5}$')
PATH_PATTERN = re.compile(r'^/[a-zA-Z0-9_./-]+$')


def validate_ip(ip):
    """Validate IP address format."""
    if not ip or not IP_PATTERN.match(ip):
        return False
    parts = ip.split('.')
    return all(0 <= int(p) <= 255 for p in parts)


def validate_hostname(hostname):
    """Validate hostname format."""
    return bool(hostname and HOSTNAME_PATTERN.match(hostname))


def validate_port(port):
    """Validate port number."""
    try:
        p = int(port)
        return 1 <= p <= 65535
    except (ValueError, TypeError):
        return False


def validate_path(path):
    """Validate file path (basic safety check)."""
    if not path:
        return False
    if '..' in path or '\0' in path:
        return False
    return PATH_PATTERN.match(path) is not None


def parse_config_file(config_path=None):
    """Parse deploycluster_parameter.sh and return a dict of exported variables."""
    if config_path is None:
        config_path = CONFIG_FILE_PATH
    config = {}
    if not os.path.isfile(config_path):
        return config
    export_pattern = re.compile(r'^export\s+([A-Za-z_][A-Za-z0-9_]*)=["\']?(.*?)["\']?\s*$')
    with open(config_path, 'r') as f:
        for line in f:
            line = line.strip()
            m = export_pattern.match(line)
            if m:
                config[m.group(1)] = m.group(2)
    return config


def parse_segment_hosts(hosts_path=None):
    """Parse segmenthosts.conf and return list of (ip, hostname) tuples for segment hosts."""
    if hosts_path is None:
        hosts_path = HOSTS_FILE_PATH
    segments = []
    if not os.path.isfile(hosts_path):
        return segments
    in_segment_section = False
    with open(hosts_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('##Segment hosts'):
                in_segment_section = True
                continue
            if line.startswith('#') or not line:
                if in_segment_section and line.startswith('#'):
                    break
                continue
            if in_segment_section:
                parts = line.split()
                if len(parts) >= 2:
                    segments.append((parts[0], parts[1]))
                elif len(parts) == 1:
                    segments.append((parts[0], ''))
    return segments


def parse_standby_host(hosts_path=None):
    """Parse the optional ##Standby hosts block; returns (ip, hostname) or ('', '')."""
    if hosts_path is None:
        hosts_path = HOSTS_FILE_PATH
    if not os.path.isfile(hosts_path):
        return ('', '')
    in_section = False
    with open(hosts_path, 'r') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('##Standby hosts'):
                in_section = True
                continue
            if not in_section:
                continue
            if stripped.startswith('#') or not stripped:
                if stripped.startswith('#'):
                    break
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                return (parts[0], parts[1])
            if len(parts) == 1:
                return (parts[0], '')
    return ('', '')


def detect_os_info():
    """Detect current operating system type and version."""
    info = {
        'os_family': 'unknown',
        'os_id': 'unknown',
        'os_version': 'unknown',
        'os_name': 'Unknown',
        'pkg_format': 'rpm',
    }
    try:
        if os.path.isfile('/etc/os-release'):
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('ID='):
                        info['os_id'] = line.split('=', 1)[1].strip('"').lower()
                    elif line.startswith('VERSION_ID='):
                        info['os_version'] = line.split('=', 1)[1].strip('"')
                    elif line.startswith('PRETTY_NAME='):
                        info['os_name'] = line.split('=', 1)[1].strip('"')

            if info['os_id'] in ('ubuntu', 'debian'):
                info['os_family'] = 'debian'
                info['pkg_format'] = 'deb'
            elif info['os_id'] in ('centos', 'rhel', 'ol', 'fedora', 'rocky', 'almalinux'):
                info['os_family'] = 'rhel'
                info['pkg_format'] = 'rpm'
        elif os.path.isfile('/etc/redhat-release'):
            info['os_family'] = 'rhel'
            info['pkg_format'] = 'rpm'
            info['os_name'] = open('/etc/redhat-release').read().strip()
    except Exception:
        pass
    return info


def detect_db_from_package(pkg_path):
    """Detect database type from package filename (same logic as deploycluster.sh)."""
    if not pkg_path:
        return None
    filename = os.path.basename(pkg_path).lower()

    if 'greenplum' in filename:
        m = re.search(r'greenplum-db-([0-9.]+)', filename)
        version = m.group(1) if m else 'unknown'
        major = version.split('.')[0] if version != 'unknown' else '7'
        legacy = int(major) < 7 if major.isdigit() else False
        return {
            'db_type': 'Greenplum',
            'db_version': version,
            'binary_path': '/usr/local/greenplum-db',
            'legacy': legacy,
        }
    elif 'hashdata-lightning' in filename:
        # Support both RPM (hashdata-lightning-2.4.0) and DEB (hashdata-lightning_2.4.0) naming
        m = re.search(r'hashdata-lightning[-_]([0-9]+(?:\.[0-9]+)*)', filename)
        version = m.group(1) if m else 'unknown'
        major = version.split('.')[0] if version != 'unknown' else '2'
        if major == '1':
            binary_path = '/usr/local/cloudberry-db'
        else:
            binary_path = '/usr/local/hashdata-lightning'
        return {
            'db_type': 'HashData Lightning',
            'db_version': version,
            'binary_path': binary_path,
            'legacy': False,
        }
    elif 'synxdb4' in filename:
        m = re.search(r'synxdb4-([0-9.]+)', filename)
        return {
            'db_type': 'SynxDB',
            'db_version': m.group(1) if m else 'unknown',
            'binary_path': '/usr/local/synxdb4',
            'legacy': False,
        }
    elif 'synxdb-2' in filename:
        m = re.search(r'synxdb-([0-9.]+)', filename)
        return {
            'db_type': 'SynxDB',
            'db_version': m.group(1) if m else 'unknown',
            'binary_path': '/usr/local/synxdb',
            'legacy': True,
        }
    elif 'synxdb-1' in filename:
        m = re.search(r'synxdb-([0-9.]+)', filename)
        return {
            'db_type': 'SynxDB',
            'db_version': m.group(1) if m else 'unknown',
            'binary_path': '/usr/local/synxdb',
            'legacy': True,
        }
    elif 'cloudberry' in filename:
        m = re.search(r'cloudberry-db-([0-9.]+)', filename)
        return {
            'db_type': 'Cloudberry',
            'db_version': m.group(1) if m else 'unknown',
            'binary_path': '/usr/local/cloudberry-db',
            'legacy': False,
        }
    return None


def generate_config_file(params):
    """Generate configuration file content."""
    original_config = parse_config_file()

    defaults = {
        'INIT_CONFIGFILE': '/tmp/gpinitsystem_config',
        'MACHINE_LIST_FILE': '/tmp/hostfile_gpinitsystem',
        'ARRAY_NAME': 'CBDB_SANDBOX',
        'SEG_PREFIX': 'gpseg',
        'PORT_BASE': '6000',
        'TRUSTED_SHELL': 'ssh',
        'CHECK_POINT_SEGMENTS': '8',
        'ENCODING': 'UNICODE',
        'DATABASE_NAME': 'gpadmin',
        'MIRROR_PORT_BASE': '7000',
        'MIRROR_DATA_DIRECTORY': '/data0/database/mirror /data0/database/mirror',
        'INSTALL_DB_SOFTWARE': 'true',
        'INIT_ENV_ONLY': 'false',
        'WITH_STANDBY': 'false',
        'MANUAL_REPO': 'false',
        'TIMEZONE': 'Asia/Shanghai',
    }

    merged = {}
    merged.update(defaults)
    merged.update(original_config)
    merged.update({k: v for k, v in params.items() if v})

    # Normalize legacy variable name
    if 'MAUNAL_YUM_REPO' in merged:
        merged['MANUAL_REPO'] = merged.pop('MAUNAL_YUM_REPO')

    lines = [
        '#!/bin/bash',
        '# Auto-generated configuration file',
        f'# Generated at: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '',
    ]

    skip_keys = {'SEGMENT_IPS', 'SEGMENT_HOSTNAMES', 'SEGMENT_COUNT',
                 'STANDBY_IP', 'STANDBY_HOSTNAME'}

    for key, value in merged.items():
        if value and key not in skip_keys:
            safe_value = str(value).replace('"', '\\"')
            lines.append(f'export {key}="{safe_value}"')

    deploy_type = merged.get('DEPLOY_TYPE', 'single')
    if deploy_type == 'multi':
        segment_ips = params.get('SEGMENT_IPS', '')
        segment_hostnames = params.get('SEGMENT_HOSTNAMES', '')

        if segment_ips:
            hostname_list = [h.strip() for h in segment_hostnames.split(',') if h.strip()]
            ip_list = [ip.strip() for ip in segment_ips.split(',') if ip.strip()]

            if not hostname_list:
                hostname_list = [f'sdw{i+1}' for i in range(len(ip_list))]

            segment_hosts = ','.join(hostname_list)
            lines.append(f'\n# Segment hosts for gpinitsystem')
            lines.append(f'export SEGMENT_HOSTS="{segment_hosts}"')

    lines.append('')
    lines.append('# Utility function for logging with timestamps')
    lines.append('function log_time() {')
    lines.append('  printf "[%s] %b\\n" "$(date \'+%Y-%m-%d %H:%M:%S\')" "$1"')
    lines.append('}')
    lines.append('export -f log_time')

    return '\n'.join(lines) + '\n'


def generate_hosts_file(params):
    """Generate hosts file content for multi-node deployment."""
    lines = [
        '##Define hosts used for Hashdata',
        '#Hashdata hosts begin',
    ]

    coord_ip = params.get('COORDINATOR_IP', '')
    coord_host = params.get('COORDINATOR_HOSTNAME', 'mdw')
    if coord_ip:
        lines.append('##Coordinator hosts')
        lines.append(f'{coord_ip} {coord_host}')

    lines.append('##Segment hosts')
    segment_ips = params.get('SEGMENT_IPS', '')
    segment_hostnames = params.get('SEGMENT_HOSTNAMES', '')

    ip_list = [ip.strip() for ip in segment_ips.split(',') if ip.strip()]
    hostname_list = [host.strip() for host in segment_hostnames.split(',') if host.strip()]

    for i, ip in enumerate(ip_list):
        hostname = hostname_list[i] if i < len(hostname_list) else f'sdw{i + 1}'
        lines.append(f'{ip} {hostname}')

    # Optional standby block. Consumed by init_env.sh + init_cluster.sh
    # (PR #34) when WITH_STANDBY=true. cbdb supports exactly one standby.
    if params.get('WITH_STANDBY') == 'true':
        standby_ip = params.get('STANDBY_IP', '').strip()
        standby_host = params.get('STANDBY_HOSTNAME', '').strip()
        if standby_ip:
            lines.append('##Standby hosts')
            lines.append(f'{standby_ip} {standby_host or "smdw"}')

    lines.append('#Hashdata hosts end')
    return '\n'.join(lines) + '\n'


def execute_local_deployment():
    """Execute deployment locally in background thread."""
    global DEPLOYMENT_STATUS

    log_file = DEPLOYMENT_STATUS['log_file']
    deploy_type = DEPLOYMENT_STATUS.get('deploy_type', 'single')

    if deploy_type not in ('single', 'multi'):
        deploy_type = 'single'

    try:
        # Start deploycluster.sh locally
        log_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting local deployment (mode: {deploy_type})...\n"
        with open(log_file, 'w') as f:
            f.write(log_msg)

        safe_type = shlex.quote(deploy_type)
        cmd = f'bash {shlex.quote(os.path.join(SCRIPT_DIR, "deploycluster.sh"))} {safe_type}'

        with open(log_file, 'a') as f:
            process = subprocess.Popen(
                cmd, shell=True,
                stdout=f, stderr=subprocess.STDOUT,
                cwd=SCRIPT_DIR
            )

        # Monitor the process
        monitor_local_deployment(process, log_file)

    except Exception as e:
        with DEPLOYMENT_LOCK:
            DEPLOYMENT_STATUS['log_content'] += f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: {str(e)}\n"
            DEPLOYMENT_STATUS['success'] = False
    finally:
        with DEPLOYMENT_LOCK:
            DEPLOYMENT_STATUS['running'] = False


def monitor_local_deployment(process, log_file):
    """Monitor local deployment progress."""
    global DEPLOYMENT_STATUS

    max_wait = 3600  # 1 hour max
    start = time.time()

    while time.time() - start < max_wait:
        # Read log file
        try:
            with open(log_file, 'r') as f:
                log_content = f.read()
            with DEPLOYMENT_LOCK:
                DEPLOYMENT_STATUS['log_content'] = log_content
                # Detect current phase
                for phase_name, pattern in PHASE_PATTERNS:
                    if pattern.search(log_content):
                        DEPLOYMENT_STATUS['phase'] = phase_name
        except FileNotFoundError:
            pass

        # Check if process finished
        retcode = process.poll()
        if retcode is not None:
            time.sleep(1)  # Brief wait for final log writes
            try:
                with open(log_file, 'r') as f:
                    log_content = f.read()
            except FileNotFoundError:
                log_content = ''

            with DEPLOYMENT_LOCK:
                DEPLOYMENT_STATUS['log_content'] = log_content
                if 'Finished deploy cluster' in log_content or 'Finished cluster initialization' in log_content:
                    DEPLOYMENT_STATUS['success'] = True
                    DEPLOYMENT_STATUS['phase'] = 'config_done'
                elif retcode == 0:
                    DEPLOYMENT_STATUS['success'] = True
                    DEPLOYMENT_STATUS['phase'] = 'config_done'
                else:
                    DEPLOYMENT_STATUS['success'] = False
            break

        time.sleep(2)
    else:
        # Timeout
        process.kill()
        with DEPLOYMENT_LOCK:
            DEPLOYMENT_STATUS['log_content'] += '\n[TIMEOUT] Deployment exceeded 1 hour limit.\n'
            DEPLOYMENT_STATUS['success'] = False

    with DEPLOYMENT_LOCK:
        DEPLOYMENT_STATUS['running'] = False


# ==================== Flask Routes ====================

@app.route('/')
def index():
    """Main page - wizard interface."""
    file_config = parse_config_file()
    segment_hosts = parse_segment_hosts()
    standby_host = parse_standby_host()
    return render_template('index.html',
                           file_config=file_config,
                           segment_hosts=segment_hosts,
                           standby_host=standby_host)


@app.route('/detect_os')
def detect_os():
    """Auto-detect current operating system."""
    info = detect_os_info()
    return jsonify({'success': True, **info})


@app.route('/validate_pkg_path', methods=['POST'])
def validate_pkg_path():
    """Validate package file path exists locally and detect DB type."""
    pkg_path = request.form.get('pkg_path', '').strip()

    if not pkg_path:
        return jsonify({'valid': False, 'message': 'Please enter package path'})

    if not validate_path(pkg_path):
        return jsonify({'valid': False, 'message': 'Invalid path format'})

    if not os.path.isfile(pkg_path):
        return jsonify({'valid': False, 'message': f'File not found: {pkg_path}'})

    # Get file info
    file_size = os.path.getsize(pkg_path)

    # Check package format matches OS
    os_info = detect_os_info()
    ext = os.path.splitext(pkg_path)[1].lower()
    if os_info['pkg_format'] == 'deb' and ext != '.deb':
        return jsonify({
            'valid': False,
            'message': f'OS is {os_info["os_name"]} (DEB), but file is not a .deb package'
        })
    if os_info['pkg_format'] == 'rpm' and ext != '.rpm':
        return jsonify({
            'valid': False,
            'message': f'OS is {os_info["os_name"]} (RPM), but file is not a .rpm package'
        })

    # Detect database type
    db_info = detect_db_from_package(pkg_path)

    result = {
        'valid': True,
        'message': 'File exists',
        'file_size': file_size,
    }
    if db_info:
        result['db_info'] = db_info

    return jsonify(result)


@app.route('/upload_package', methods=['POST'])
def upload_package():
    """Upload a database package file (RPM/DEB) from the client browser."""
    if 'package' not in request.files:
        return jsonify({'success': False, 'message': 'No file provided'})

    f = request.files['package']
    if not f.filename:
        return jsonify({'success': False, 'message': 'No file selected'})

    filename = secure_filename(f.filename)
    if not filename.endswith(('.rpm', '.deb')):
        return jsonify({'success': False, 'message': 'Only .rpm and .deb files are allowed'})

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest_path = os.path.join(UPLOAD_DIR, filename)

    try:
        f.save(dest_path)
    except OSError as e:
        return jsonify({'success': False, 'message': f'Failed to save file: {e}'})

    file_size = os.path.getsize(dest_path)
    db_info = detect_db_from_package(dest_path)

    return jsonify({
        'success': True,
        'file_path': dest_path,
        'file_size': file_size,
        'db_info': db_info,
    })


@app.route('/save_config', methods=['POST'])
def save_config():
    """Save deployment configuration to session."""
    deploy_type = request.form.get('deploy_type', 'single')
    if deploy_type not in ('single', 'multi'):
        return jsonify({'success': False, 'message': 'Invalid deploy type'})

    # Collect segment hosts for multi-node
    segment_ips = []
    segment_hostnames = []

    if deploy_type == 'multi':
        for key, value in request.form.items():
            if key.startswith('SEGMENT_IP_'):
                idx = key.replace('SEGMENT_IP_', '')
                ip = value.strip()
                hostname = request.form.get(f'SEGMENT_HOSTNAME_{idx}', '').strip()
                if ip:
                    if not validate_ip(ip):
                        return jsonify({'success': False, 'message': f'Invalid segment IP: {ip}'})
                    if hostname and not validate_hostname(hostname):
                        return jsonify({'success': False, 'message': f'Invalid hostname: {hostname}'})
                    segment_ips.append(ip)
                    segment_hostnames.append(hostname or f'sdw{len(segment_ips)}')

    # Standby coordinator (multi-node only — single-node has no second host
    # to host a standby; gpinitstandby would fail).
    with_standby = request.form.get('WITH_STANDBY', 'false')
    standby_ip = request.form.get('STANDBY_IP', '').strip()
    standby_hostname = request.form.get('STANDBY_HOSTNAME', '').strip()
    if with_standby == 'true':
        if deploy_type != 'multi':
            return jsonify({'success': False, 'message': 'Standby coordinator requires multi-node deployment'})
        if not standby_ip:
            return jsonify({'success': False, 'message': 'Standby IP is required when standby coordinator is enabled'})
        if not validate_ip(standby_ip):
            return jsonify({'success': False, 'message': f'Invalid standby IP: {standby_ip}'})
        if standby_hostname and not validate_hostname(standby_hostname):
            return jsonify({'success': False, 'message': f'Invalid standby hostname: {standby_hostname}'})
        coord_ip = request.form.get('COORDINATOR_IP', '').strip()
        coord_host = request.form.get('COORDINATOR_HOSTNAME', '').strip()
        if standby_ip == coord_ip:
            return jsonify({'success': False, 'message': 'Standby IP must differ from coordinator IP'})
        if standby_hostname and standby_hostname == coord_host:
            return jsonify({'success': False, 'message': 'Standby hostname must differ from coordinator hostname'})
        if standby_ip in segment_ips:
            return jsonify({'success': False, 'message': 'Standby IP must differ from segment IPs'})
        if standby_hostname and standby_hostname in segment_hostnames:
            return jsonify({'success': False, 'message': 'Standby hostname must differ from segment hostnames'})
    else:
        # Drop any leftover values so generate_hosts_file does not emit a block.
        standby_ip = ''
        standby_hostname = ''

    deploy_params = {
        'ADMIN_USER': request.form.get('ADMIN_USER', 'gpadmin').strip(),
        'ADMIN_USER_PASSWORD': request.form.get('ADMIN_USER_PASSWORD', ''),
        'CLOUDBERRY_RPM': request.form.get('CLOUDBERRY_RPM', '').strip(),
        'COORDINATOR_HOSTNAME': request.form.get('COORDINATOR_HOSTNAME', 'mdw').strip(),
        'COORDINATOR_IP': request.form.get('COORDINATOR_IP', '').strip(),
        'COORDINATOR_PORT': request.form.get('COORDINATOR_PORT', '5432').strip(),
        'COORDINATOR_DIRECTORY': request.form.get('COORDINATOR_DIRECTORY', '/data0/database/coordinator').strip(),
        'DATA_DIRECTORY': request.form.get('DATA_DIRECTORY', '/data0/database/primary').strip(),
        'DEPLOY_TYPE': deploy_type,
        'WITH_MIRROR': request.form.get('WITH_MIRROR', 'false'),
        'MIRROR_PORT_BASE': request.form.get('MIRROR_PORT_BASE', '7000').strip(),
        'MIRROR_DATA_DIRECTORY': request.form.get('MIRROR_DATA_DIRECTORY', '').strip(),
        'WITH_STANDBY': request.form.get('WITH_STANDBY', 'false'),
        'INIT_ENV_ONLY': request.form.get('INIT_ENV_ONLY', 'false'),
        'INSTALL_DB_SOFTWARE': request.form.get('INSTALL_DB_SOFTWARE', 'true'),
        'MANUAL_REPO': request.form.get('MANUAL_REPO', 'false'),
        'TIMEZONE': request.form.get('TIMEZONE', 'Asia/Shanghai').strip(),
        'ARRAY_NAME': request.form.get('ARRAY_NAME', '').strip(),
        'SEG_PREFIX': request.form.get('SEG_PREFIX', '').strip(),
        'PORT_BASE': request.form.get('PORT_BASE', '').strip(),
    }

    if deploy_type == 'multi' and segment_ips:
        deploy_params['SEGMENT_IPS'] = ','.join(segment_ips)
        deploy_params['SEGMENT_HOSTNAMES'] = ','.join(segment_hostnames)
        deploy_params['SEGMENT_COUNT'] = str(len(segment_ips))
        deploy_params['SEGMENT_ACCESS_METHOD'] = request.form.get('SEGMENT_ACCESS_METHOD', 'password')
        deploy_params['SEGMENT_ACCESS_USER'] = request.form.get('SEGMENT_ACCESS_USER', 'root').strip()
        deploy_params['SEGMENT_ACCESS_PASSWORD'] = request.form.get('SEGMENT_ACCESS_PASSWORD', '')
        deploy_params['SEGMENT_ACCESS_KEYFILE'] = request.form.get('SEGMENT_ACCESS_KEYFILE', '').strip()

    # Stash standby info for generate_hosts_file (filtered out of
    # deploycluster_parameter.sh by skip_keys; standby SSH access reuses
    # SEGMENT_ACCESS_* — same machines, same credentials).
    if with_standby == 'true' and standby_ip:
        deploy_params['STANDBY_IP'] = standby_ip
        deploy_params['STANDBY_HOSTNAME'] = standby_hostname or 'smdw'

    session['deploy_params'] = deploy_params
    session['deploy_type'] = deploy_type

    # Also save to server-side global (survives session loss across workers)
    with SAVED_CONFIG_LOCK:
        SAVED_CONFIG['params'] = deploy_params

    return jsonify({'success': True, 'message': 'Configuration saved'})


@app.route('/preview_config')
def preview_config():
    """Return full configuration summary for the confirmation step."""
    params = session.get('deploy_params', {})
    if not params:
        # Fallback to server-side global
        with SAVED_CONFIG_LOCK:
            params = SAVED_CONFIG.get('params', {})
    if not params:
        return jsonify({'success': False, 'message': 'No configuration saved'})

    os_info = detect_os_info()
    db_info = detect_db_from_package(params.get('CLOUDBERRY_RPM', ''))

    # Build segment list
    segments = []
    if params.get('DEPLOY_TYPE') == 'multi':
        ips = [ip.strip() for ip in params.get('SEGMENT_IPS', '').split(',') if ip.strip()]
        hosts = [h.strip() for h in params.get('SEGMENT_HOSTNAMES', '').split(',') if h.strip()]
        for i, ip in enumerate(ips):
            hostname = hosts[i] if i < len(hosts) else f'sdw{i+1}'
            segments.append({'ip': ip, 'hostname': hostname})

    standby = None
    if params.get('WITH_STANDBY') == 'true' and params.get('STANDBY_IP'):
        standby = {'ip': params.get('STANDBY_IP', ''),
                   'hostname': params.get('STANDBY_HOSTNAME', '')}

    # Calculate segment count
    data_dirs = params.get('DATA_DIRECTORY', '/data0/database/primary').split()
    segs_per_host = len(data_dirs)
    total_hosts = max(len(segments), 1)
    total_segments = segs_per_host * total_hosts

    # Build warnings
    warnings = []
    if params.get('WITH_MIRROR') != 'true':
        warnings.append({'level': 'warning', 'message': 'Mirror not enabled - segment failure will cause data unavailability'})
    if params.get('WITH_STANDBY') != 'true':
        warnings.append({'level': 'warning', 'message': 'Standby Coordinator not enabled - manual recovery needed on failure'})
    warnings.append({'level': 'info', 'message': 'Deployment will disable firewall and SELinux'})
    warnings.append({'level': 'info', 'message': f'System user {params.get("ADMIN_USER", "gpadmin")} will be created with passwordless sudo'})

    preview = {
        'success': True,
        'os': os_info,
        'db': db_info,
        'params': {k: v for k, v in params.items() if k != 'ADMIN_USER_PASSWORD' and k != 'SEGMENT_ACCESS_PASSWORD'},
        'segments': segments,
        'standby': standby,
        'segs_per_host': segs_per_host,
        'total_segments': total_segments,
        'warnings': warnings,
    }
    return jsonify(preview)


@app.route('/deploy', methods=['POST'])
def deploy():
    """Start local deployment."""
    with DEPLOYMENT_LOCK:
        if DEPLOYMENT_STATUS.get('running', False):
            return jsonify({'success': False, 'message': 'Deployment already in progress'})

    config_params = session.get('deploy_params', {})
    if not config_params:
        # Fallback to server-side global
        with SAVED_CONFIG_LOCK:
            config_params = SAVED_CONFIG.get('params', {})
    if not config_params:
        return jsonify({'success': False, 'message': 'No configuration saved. Please complete Steps 1-2 first.'})

    pkg_path = config_params.get('CLOUDBERRY_RPM', '')
    if not pkg_path:
        return jsonify({'success': False, 'message': 'Package path not specified'})

    if not validate_path(pkg_path):
        return jsonify({'success': False, 'message': 'Invalid package path format'})

    if not os.path.isfile(pkg_path):
        return jsonify({'success': False, 'message': f'Package file not found: {pkg_path}'})

    deploy_type = config_params.get('DEPLOY_TYPE', 'single')
    if deploy_type not in ('single', 'multi'):
        return jsonify({'success': False, 'message': 'Invalid deploy type'})

    # Generate configuration file
    config_content = generate_config_file(config_params)
    with open(CONFIG_FILE_PATH, 'w') as f:
        f.write(config_content)

    # Generate hosts file for multi-node
    if deploy_type == 'multi':
        hosts_content = generate_hosts_file(config_params)
        with open(HOSTS_FILE_PATH, 'w') as f:
            f.write(hosts_content)

    # Setup log file
    log_filename = f'deploy_cluster_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    log_file = os.path.join(SCRIPT_DIR, log_filename)

    with DEPLOYMENT_LOCK:
        # Double-check under lock to prevent TOCTOU race
        if DEPLOYMENT_STATUS.get('running', False):
            return jsonify({'success': False, 'message': 'Deployment already in progress'})
        DEPLOYMENT_STATUS['running'] = True
        DEPLOYMENT_STATUS['success'] = None
        DEPLOYMENT_STATUS['log_content'] = ''
        DEPLOYMENT_STATUS['start_time'] = time.time()
        DEPLOYMENT_STATUS['phase'] = 'env_init'
        DEPLOYMENT_STATUS['deploy_type'] = deploy_type
        DEPLOYMENT_STATUS['log_file'] = log_file

    # Start deployment in background thread
    thread = threading.Thread(target=execute_local_deployment)
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True,
        'log_file': log_filename,
    })


@app.route('/deployment_status')
def deployment_status():
    """Get current deployment status and phase."""
    with DEPLOYMENT_LOCK:
        return jsonify({
            'running': DEPLOYMENT_STATUS.get('running', False),
            'success': DEPLOYMENT_STATUS.get('success'),
            'phase': DEPLOYMENT_STATUS.get('phase', ''),
            'log_lines': DEPLOYMENT_STATUS.get('log_content', '').count('\n'),
        })


@app.route('/stream_logs')
def stream_logs():
    """Server-sent events for real-time log streaming."""
    def generate():
        last_pos = 0
        while True:
            with DEPLOYMENT_LOCK:
                log_content = DEPLOYMENT_STATUS.get('log_content', '')
                running = DEPLOYMENT_STATUS.get('running', False)
                phase = DEPLOYMENT_STATUS.get('phase', '')

            if log_content and len(log_content) > last_pos:
                new_content = log_content[last_pos:]
                last_pos = len(log_content)
                # Encode for SSE (escape newlines)
                escaped = new_content.replace('\n', '\ndata: ')
                yield f"data: {escaped}\n\n"

            if not running:
                with DEPLOYMENT_LOCK:
                    success = DEPLOYMENT_STATUS.get('success')
                yield f"event: done\ndata: {json.dumps({'success': success, 'phase': phase})}\n\n"
                break

            time.sleep(1)

    import json
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/reset', methods=['POST'])
def reset():
    """Reset deployment status."""
    with DEPLOYMENT_LOCK:
        if DEPLOYMENT_STATUS.get('running', False):
            return jsonify({'success': False, 'message': 'Cannot reset while deployment is running'})
        DEPLOYMENT_STATUS.update({
            'running': False,
            'success': None,
            'log_content': '',
            'start_time': None,
            'log_file': None,
            'deploy_type': 'single',
            'phase': '',
        })

    session.pop('deploy_params', None)

    with SAVED_CONFIG_LOCK:
        SAVED_CONFIG['params'] = {}
    session.pop('deploy_type', None)
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
