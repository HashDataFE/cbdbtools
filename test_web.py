#!/usr/bin/env python3
"""
Tests for cluster_deploy_web.py
Covers: input validation, config generation, config parsing, hosts file generation,
        OS detection, DB detection, route behavior, and wizard workflow logic.
"""

import os
import sys
import json
import tempfile
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cluster_deploy_web import (
    app,
    parse_config_file,
    generate_config_file,
    generate_hosts_file,
    parse_segment_hosts,
    detect_os_info,
    detect_db_from_package,
    validate_ip,
    validate_hostname,
    validate_port,
    validate_path,
    DEPLOYMENT_STATUS,
    SAVED_CONFIG,
    SAVED_CONFIG_LOCK,
)


# ====================== Fixtures ======================

@pytest.fixture
def client():
    """Flask test client with session support."""
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret'
    # Clear server-side config state between tests
    with SAVED_CONFIG_LOCK:
        SAVED_CONFIG['params'] = {}
    with app.test_client() as c:
        yield c


@pytest.fixture
def sample_params():
    """Minimal valid deployment parameters."""
    return {
        'ADMIN_USER': 'gpadmin',
        'ADMIN_USER_PASSWORD': 'Test@1234',
        'CLOUDBERRY_RPM': '/root/cloudberry-db-1.6.0.rpm',
        'COORDINATOR_HOSTNAME': 'mdw',
        'COORDINATOR_IP': '192.168.1.100',
        'COORDINATOR_PORT': '5432',
        'COORDINATOR_DIRECTORY': '/data0/database/coordinator',
        'DATA_DIRECTORY': '/data0/database/primary',
        'DEPLOY_TYPE': 'single',
        'WITH_MIRROR': 'false',
    }


@pytest.fixture
def multi_params(sample_params):
    """Multi-node deployment parameters."""
    sample_params.update({
        'DEPLOY_TYPE': 'multi',
        'SEGMENT_IPS': '192.168.1.101,192.168.1.102',
        'SEGMENT_HOSTNAMES': 'sdw1,sdw2',
        'SEGMENT_ACCESS_METHOD': 'password',
        'SEGMENT_ACCESS_USER': 'root',
        'SEGMENT_ACCESS_PASSWORD': 'SegPass@123',
    })
    return sample_params


@pytest.fixture
def tmp_config_file():
    """Create a temporary deploycluster_parameter.sh for testing."""
    content = textwrap.dedent("""\
        export ADMIN_USER="gpadmin"
        export ADMIN_USER_PASSWORD="FromFile@123"
        export CLOUDBERRY_RPM="/root/test.rpm"
        export COORDINATOR_HOSTNAME="mdw"
        export COORDINATOR_IP="10.0.0.1"
        export DEPLOY_TYPE="single"
        export COORDINATOR_PORT="5432"
        export COORDINATOR_DIRECTORY="/data0/coordinator"
        export DATA_DIRECTORY="/data0/primary /data0/primary"
        export INIT_CONFIGFILE="/tmp/gpinitsystem_config"
        export MACHINE_LIST_FILE="/tmp/hostfile_gpinitsystem"
        export ARRAY_NAME="TEST_CLUSTER"
        export SEG_PREFIX="gpseg"
        export PORT_BASE="6000"
        export TRUSTED_SHELL="ssh"
        export CHECK_POINT_SEGMENTS="8"
        export ENCODING="UNICODE"
        export DATABASE_NAME="gpadmin"

        # Utility function for logging with timestamps
        function log_time() {
          printf "[%s] %b\\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
        }
        export -f log_time
    """)
    fd, path = tempfile.mkstemp(suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    yield path
    os.unlink(path)


@pytest.fixture
def tmp_hosts_file():
    """Create a temporary segmenthosts.conf for testing."""
    content = textwrap.dedent("""\
        ##Define hosts used for Hashdata
        #Hashdata hosts begin
        ##Coordinator hosts
        10.0.0.1 mdw
        ##Segment hosts
        10.0.0.2 sdw1
        10.0.0.3 sdw2
        10.0.0.4 sdw3
        #Hashdata hosts end
    """)
    fd, path = tempfile.mkstemp(suffix='.conf')
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    yield path
    os.unlink(path)


# ====================== 1. Input Validation ======================

class TestValidateIP:
    def test_valid_ips(self):
        assert validate_ip('192.168.1.1')
        assert validate_ip('10.0.0.1')
        assert validate_ip('255.255.255.255')
        assert validate_ip('0.0.0.0')

    def test_invalid_ips(self):
        assert not validate_ip('')
        assert not validate_ip('256.1.1.1')
        assert not validate_ip('192.168.1')
        assert not validate_ip('abc.def.ghi.jkl')
        assert not validate_ip('192.168.1.1.1')
        assert not validate_ip(None)


class TestValidateHostname:
    def test_valid_hostnames(self):
        assert validate_hostname('mdw')
        assert validate_hostname('sdw1')
        assert validate_hostname('my-host.domain.com')
        assert validate_hostname('a')

    def test_invalid_hostnames(self):
        assert not validate_hostname('')
        assert not validate_hostname(None)
        assert not validate_hostname('-starts-with-dash')
        assert not validate_hostname('a' * 64)


class TestValidatePort:
    def test_valid_ports(self):
        assert validate_port(22)
        assert validate_port('5432')
        assert validate_port(1)
        assert validate_port(65535)

    def test_invalid_ports(self):
        assert not validate_port(0)
        assert not validate_port(65536)
        assert not validate_port('abc')
        assert not validate_port('')
        assert not validate_port(None)


class TestValidatePath:
    def test_valid_paths(self):
        assert validate_path('/root/test.rpm')
        assert validate_path('/data0/database/primary')
        assert validate_path('/usr/local/cloudberry-db')

    def test_invalid_paths(self):
        assert not validate_path('')
        assert not validate_path(None)
        assert not validate_path('../etc/passwd')
        assert not validate_path('/root/../etc/passwd')
        assert not validate_path('relative/path')
        assert not validate_path('/path/with spaces')


# ====================== 2. Config File Parsing ======================

class TestParseConfigFile:
    def test_parse_valid_config(self, tmp_config_file):
        config = parse_config_file(tmp_config_file)
        assert config['ADMIN_USER'] == 'gpadmin'
        assert config['ADMIN_USER_PASSWORD'] == 'FromFile@123'
        assert config['COORDINATOR_PORT'] == '5432'
        assert config['ARRAY_NAME'] == 'TEST_CLUSTER'

    def test_parse_nonexistent_file(self):
        config = parse_config_file('/nonexistent/path.sh')
        assert config == {}

    def test_parse_ignores_comments_and_functions(self, tmp_config_file):
        config = parse_config_file(tmp_config_file)
        assert 'log_time' not in config
        assert 'printf' not in str(config.keys())


class TestParseSegmentHosts:
    def test_parse_valid_hosts(self, tmp_hosts_file):
        segments = parse_segment_hosts(tmp_hosts_file)
        assert len(segments) == 3
        assert segments[0] == ('10.0.0.2', 'sdw1')
        assert segments[1] == ('10.0.0.3', 'sdw2')
        assert segments[2] == ('10.0.0.4', 'sdw3')

    def test_parse_nonexistent_file(self):
        segments = parse_segment_hosts('/nonexistent/path.conf')
        assert segments == []


# ====================== 3. DB Detection ======================

class TestDetectDbFromPackage:
    def test_cloudberry_rpm(self):
        info = detect_db_from_package('/root/cloudberry-db-1.6.0-el8.x86_64.rpm')
        assert info['db_type'] == 'Cloudberry'
        assert info['db_version'] == '1.6.0'
        assert info['binary_path'] == '/usr/local/cloudberry-db'
        assert info['legacy'] is False

    def test_cloudberry_deb(self):
        info = detect_db_from_package('/root/cloudberry-db-1.6.0-ubuntu22.04.deb')
        assert info['db_type'] == 'Cloudberry'
        assert info['binary_path'] == '/usr/local/cloudberry-db'

    def test_greenplum_legacy(self):
        info = detect_db_from_package('/root/greenplum-db-6.25.3-el8.rpm')
        assert info['db_type'] == 'Greenplum'
        assert info['legacy'] is True

    def test_greenplum_modern(self):
        info = detect_db_from_package('/root/greenplum-db-7.1.0-el8.rpm')
        assert info['db_type'] == 'Greenplum'
        assert info['legacy'] is False

    def test_hashdata_lightning_2(self):
        info = detect_db_from_package('/root/hashdata-lightning-2.4.0-1.x86_64.rpm')
        assert info['db_type'] == 'HashData Lightning'
        assert info['db_version'] == '2.4.0'
        assert info['binary_path'] == '/usr/local/hashdata-lightning'

    def test_hashdata_lightning_2_deb(self):
        info = detect_db_from_package('/root/hashdata-lightning_2.4.0-1_amd64.deb')
        assert info['db_type'] == 'HashData Lightning'
        assert info['db_version'] == '2.4.0'
        assert info['binary_path'] == '/usr/local/hashdata-lightning'

    def test_hashdata_lightning_1(self):
        info = detect_db_from_package('/root/hashdata-lightning-1.5.0.rpm')
        assert info['db_type'] == 'HashData Lightning'
        assert info['db_version'] == '1.5.0'
        assert info['binary_path'] == '/usr/local/cloudberry-db'

    def test_hashdata_lightning_1_deb(self):
        info = detect_db_from_package('/root/hashdata-lightning_1.5.0-1_amd64.deb')
        assert info['db_type'] == 'HashData Lightning'
        assert info['db_version'] == '1.5.0'
        assert info['binary_path'] == '/usr/local/cloudberry-db'

    def test_synxdb4(self):
        info = detect_db_from_package('/root/synxdb4-4.1.0.rpm')
        assert info['db_type'] == 'SynxDB'
        assert info['binary_path'] == '/usr/local/synxdb4'

    def test_synxdb_2(self):
        info = detect_db_from_package('/root/synxdb-2.0.0.rpm')
        assert info['db_type'] == 'SynxDB'
        assert info['legacy'] is True

    def test_synxdb_1(self):
        info = detect_db_from_package('/root/synxdb-1.5.0.rpm')
        assert info['db_type'] == 'SynxDB'
        assert info['legacy'] is True

    def test_unknown_package(self):
        info = detect_db_from_package('/root/some-random-package.rpm')
        assert info is None

    def test_empty_path(self):
        info = detect_db_from_package('')
        assert info is None

    def test_none_path(self):
        info = detect_db_from_package(None)
        assert info is None


class TestDetectOsInfo:
    def test_returns_dict_with_expected_keys(self):
        info = detect_os_info()
        assert 'os_family' in info
        assert 'os_id' in info
        assert 'os_version' in info
        assert 'os_name' in info
        assert 'pkg_format' in info


# ====================== 4. Config File Generation ======================

class TestGenerateConfigFile:
    def test_contains_all_required_variables(self, sample_params):
        content = generate_config_file(sample_params)
        required_vars = [
            'INIT_CONFIGFILE', 'MACHINE_LIST_FILE', 'ARRAY_NAME',
            'SEG_PREFIX', 'PORT_BASE', 'TRUSTED_SHELL',
            'CHECK_POINT_SEGMENTS', 'ENCODING', 'DATABASE_NAME',
        ]
        for var in required_vars:
            assert f'export {var}=' in content, f'Missing required variable: {var}'

    def test_log_time_at_end(self, sample_params):
        content = generate_config_file(sample_params)
        lines = content.strip().split('\n')

        log_time_idx = None
        for i, line in enumerate(lines):
            if line.strip() == 'export -f log_time':
                log_time_idx = i
                break

        assert log_time_idx is not None, 'export -f log_time not found'

        for i, line in enumerate(lines):
            if line.startswith('export ') and '=' in line and i > log_time_idx:
                pytest.fail(f'Export line after log_time at line {i}: {line}')

    def test_user_params_override_defaults(self, sample_params):
        content = generate_config_file(sample_params)
        assert 'export COORDINATOR_PORT="5432"' in content
        assert 'export ADMIN_USER="gpadmin"' in content

    def test_multi_node_generates_segment_hosts(self, multi_params):
        content = generate_config_file(multi_params)
        assert 'export SEGMENT_HOSTS="sdw1,sdw2"' in content

    def test_multi_node_auto_generates_hostnames(self, sample_params):
        sample_params['DEPLOY_TYPE'] = 'multi'
        sample_params['SEGMENT_IPS'] = '10.0.0.2,10.0.0.3,10.0.0.4'
        sample_params['SEGMENT_HOSTNAMES'] = ''
        content = generate_config_file(sample_params)
        assert 'export SEGMENT_HOSTS="sdw1,sdw2,sdw3"' in content

    def test_single_node_no_segment_hosts(self, sample_params):
        content = generate_config_file(sample_params)
        assert 'SEGMENT_HOSTS' not in content

    def test_skip_keys_not_exported(self, multi_params):
        content = generate_config_file(multi_params)
        assert 'export SEGMENT_IPS=' not in content
        assert 'export SEGMENT_HOSTNAMES=' not in content
        assert 'export SEGMENT_COUNT=' not in content

    def test_empty_values_not_exported(self, sample_params):
        sample_params['EMPTY_PARAM'] = ''
        content = generate_config_file(sample_params)
        assert 'EMPTY_PARAM' not in content

    def test_special_characters_escaped(self, sample_params):
        sample_params['ADMIN_USER_PASSWORD'] = 'pass"word'
        content = generate_config_file(sample_params)
        assert 'pass\\"word' in content

    def test_valid_bash_syntax(self, sample_params):
        content = generate_config_file(sample_params)
        assert content.startswith('#!/bin/bash\n')


# ====================== 5. Hosts File Generation ======================

class TestGenerateHostsFile:
    def test_basic_structure(self, multi_params):
        content = generate_hosts_file(multi_params)
        assert '##Coordinator hosts' in content
        assert '##Segment hosts' in content
        assert '#Hashdata hosts end' in content

    def test_coordinator_entry(self, multi_params):
        content = generate_hosts_file(multi_params)
        assert '192.168.1.100 mdw' in content

    def test_segment_entries(self, multi_params):
        content = generate_hosts_file(multi_params)
        assert '192.168.1.101 sdw1' in content
        assert '192.168.1.102 sdw2' in content

    def test_auto_hostnames_when_empty(self):
        params = {
            'COORDINATOR_IP': '10.0.0.1',
            'COORDINATOR_HOSTNAME': 'mdw',
            'SEGMENT_IPS': '10.0.0.2,10.0.0.3',
            'SEGMENT_HOSTNAMES': '',
        }
        content = generate_hosts_file(params)
        assert '10.0.0.2 sdw1' in content
        assert '10.0.0.3 sdw2' in content


# ====================== 6. Route Tests ======================

class TestIndexRoute:
    def test_index_returns_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_index_contains_wizard_steps(self, client):
        resp = client.get('/')
        html = resp.data.decode()
        # Should have the 4 wizard steps
        assert 'step-1' in html or 'step1' in html or 'Environment' in html or 'environment' in html


class TestDetectOsRoute:
    def test_detect_os_returns_json(self, client):
        resp = client.get('/detect_os')
        data = json.loads(resp.data)
        assert data['success']
        assert 'os_family' in data
        assert 'pkg_format' in data


class TestValidatePkgPathRoute:
    def test_empty_path_rejected(self, client):
        resp = client.post('/validate_pkg_path', data={'pkg_path': ''})
        data = json.loads(resp.data)
        assert not data['valid']

    def test_path_traversal_rejected(self, client):
        resp = client.post('/validate_pkg_path', data={'pkg_path': '/root/../etc/shadow'})
        data = json.loads(resp.data)
        assert not data['valid']
        assert 'Invalid' in data['message']

    def test_nonexistent_file(self, client):
        resp = client.post('/validate_pkg_path', data={'pkg_path': '/nonexistent/file.rpm'})
        data = json.loads(resp.data)
        assert not data['valid']
        assert 'not found' in data['message'].lower()


class TestSaveConfigRoute:
    def test_save_config_returns_json(self, client):
        resp = client.post('/save_config', data={
            'deploy_type': 'single',
            'ADMIN_USER': 'gpadmin',
            'ADMIN_USER_PASSWORD': 'Test@123',
            'CLOUDBERRY_RPM': '/root/test.rpm',
            'COORDINATOR_HOSTNAME': 'mdw',
            'COORDINATOR_IP': '192.168.1.1',
            'COORDINATOR_PORT': '5432',
            'COORDINATOR_DIRECTORY': '/data0/coordinator',
            'DATA_DIRECTORY': '/data0/primary',
            'WITH_MIRROR': 'false',
        })
        data = json.loads(resp.data)
        assert data['success']

    def test_invalid_deploy_type_rejected(self, client):
        resp = client.post('/save_config', data={'deploy_type': 'invalid_type'})
        data = json.loads(resp.data)
        assert not data['success']
        assert 'Invalid' in data['message']

    def test_invalid_segment_ip_rejected(self, client):
        resp = client.post('/save_config', data={
            'deploy_type': 'multi',
            'SEGMENT_IP_0': '999.999.999.999',
            'SEGMENT_HOSTNAME_0': 'sdw1',
        })
        data = json.loads(resp.data)
        assert not data['success']
        assert 'Invalid segment IP' in data['message']


class TestPreviewConfigRoute:
    def test_preview_without_config(self, client):
        resp = client.get('/preview_config')
        data = json.loads(resp.data)
        assert not data['success']

    def test_preview_with_saved_config(self, client):
        # First save config
        client.post('/save_config', data={
            'deploy_type': 'single',
            'ADMIN_USER': 'gpadmin',
            'ADMIN_USER_PASSWORD': 'Test@123',
            'CLOUDBERRY_RPM': '/root/cloudberry-db-1.6.0.rpm',
            'COORDINATOR_HOSTNAME': 'mdw',
            'COORDINATOR_IP': '192.168.1.100',
            'COORDINATOR_PORT': '5432',
            'COORDINATOR_DIRECTORY': '/data0/coordinator',
            'DATA_DIRECTORY': '/data0/primary',
            'WITH_MIRROR': 'false',
        })
        resp = client.get('/preview_config')
        data = json.loads(resp.data)
        assert data['success']
        assert 'os' in data
        assert 'warnings' in data
        assert 'params' in data
        # Password should not be in preview
        assert 'ADMIN_USER_PASSWORD' not in data['params']


class TestDeployRoute:
    def test_deploy_without_config_rejected(self, client):
        resp = client.post('/deploy')
        data = json.loads(resp.data)
        assert not data['success']
        assert 'configuration' in data['message'].lower() or 'config' in data['message'].lower()


class TestDeploymentStatusRoute:
    def test_deployment_status_returns_json(self, client):
        resp = client.get('/deployment_status')
        data = json.loads(resp.data)
        assert 'running' in data
        assert 'success' in data
        assert 'phase' in data


class TestResetRoute:
    def test_reset_clears_status(self, client):
        resp = client.post('/reset')
        data = json.loads(resp.data)
        assert data['success']

        status_resp = client.get('/deployment_status')
        status = json.loads(status_resp.data)
        assert not status['running']
        assert status['success'] is None


# ====================== 7. Config Merge Priority ======================

class TestConfigMergePriority:
    def test_defaults_used_when_no_file_and_no_param(self, sample_params, tmp_path):
        empty = tmp_path / 'empty.sh'
        empty.write_text('')

        import cluster_deploy_web
        orig = cluster_deploy_web.CONFIG_FILE_PATH
        cluster_deploy_web.CONFIG_FILE_PATH = str(empty)
        try:
            content = generate_config_file(sample_params)
            assert 'export ARRAY_NAME="CBDB_SANDBOX"' in content
            assert 'export SEG_PREFIX="gpseg"' in content
        finally:
            cluster_deploy_web.CONFIG_FILE_PATH = orig

    def test_file_overrides_defaults(self, sample_params, tmp_path):
        config = tmp_path / 'config.sh'
        config.write_text('export ARRAY_NAME="MY_CUSTOM_CLUSTER"\n')

        import cluster_deploy_web
        orig = cluster_deploy_web.CONFIG_FILE_PATH
        cluster_deploy_web.CONFIG_FILE_PATH = str(config)
        try:
            content = generate_config_file(sample_params)
            assert 'export ARRAY_NAME="MY_CUSTOM_CLUSTER"' in content
        finally:
            cluster_deploy_web.CONFIG_FILE_PATH = orig

    def test_ui_params_override_file(self, tmp_path):
        config = tmp_path / 'config.sh'
        config.write_text('export COORDINATOR_PORT="15432"\n')

        import cluster_deploy_web
        orig = cluster_deploy_web.CONFIG_FILE_PATH
        cluster_deploy_web.CONFIG_FILE_PATH = str(config)
        try:
            params = {'COORDINATOR_PORT': '7777', 'DEPLOY_TYPE': 'single'}
            content = generate_config_file(params)
            assert 'export COORDINATOR_PORT="7777"' in content
            assert '15432' not in content
        finally:
            cluster_deploy_web.CONFIG_FILE_PATH = orig


# ====================== 8. Deploycluster.sh Compatibility ======================

class TestDeployclusterCompatibility:
    def test_truncation_preserves_all_params(self, sample_params):
        content = generate_config_file(sample_params)

        truncated_lines = []
        for line in content.split('\n'):
            truncated_lines.append(line)
            if line.strip() == 'export -f log_time':
                break

        truncated = '\n'.join(truncated_lines)

        critical_vars = [
            'ADMIN_USER', 'CLOUDBERRY_RPM', 'COORDINATOR_HOSTNAME',
            'COORDINATOR_PORT', 'COORDINATOR_DIRECTORY', 'DATA_DIRECTORY',
            'INIT_CONFIGFILE', 'MACHINE_LIST_FILE', 'ARRAY_NAME',
            'SEG_PREFIX', 'PORT_BASE', 'DATABASE_NAME',
        ]
        for var in critical_vars:
            assert f'export {var}=' in truncated, \
                f'{var} lost after deploycluster.sh truncation!'

    def test_init_cluster_variables_present(self, sample_params):
        content = generate_config_file(sample_params)
        assert 'INIT_CONFIGFILE' in content
        assert 'MACHINE_LIST_FILE' in content
        assert 'COORDINATOR_HOSTNAME' in content
        assert 'COORDINATOR_DIRECTORY' in content
        assert 'COORDINATOR_PORT' in content
        assert 'SEG_PREFIX' in content
        assert 'ADMIN_USER' in content
        assert 'DATABASE_NAME' in content
