# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CBDBTools is a suite of shell scripts and a Flask web application for automating deployment and initialization of Cloudberry/Greenplum/HashData MPP database clusters on CentOS/RHEL 7-9, Rocky Linux 8-9, and Ubuntu 20.04-24.04.

## Commands

### Command-line Deployment
```bash
# Start deployment (uses DEPLOY_TYPE from config)
bash run.sh

# Force specific deployment type
bash run.sh single   # Single-node deployment
bash run.sh multi    # Multi-node deployment
```

### Web UI Deployment
```bash
# Start the web UI (runs on port 5000)
bash start_web.sh
```

## Architecture

### Deployment Flow
```
run.sh → deploycluster.sh → init_env.sh → init_cluster.sh
                                ↓
                         init_env_segment.sh (multi-node only)
```

### System Tuning (per GP 7.7 best practices)
Both coordinator and segment nodes are configured with:
- Kernel parameters (sysctl): shared memory, semaphores, network, dirty memory (conditional on RAM size)
- Security limits (limits.conf + limits.d override for nproc)
- Transparent Huge Pages (THP) disabled and persisted
- NTP/chrony time synchronization
- SSH daemon optimization
- systemd-logind RemoveIPC=no
- Firewall and SELinux disabled

### Key Files

| File | Purpose |
|------|---------|
| `deploycluster_parameter.sh` | Central configuration file with all environment variables |
| `deploycluster.sh` | Main orchestration script; detects DB type, sets paths, calls init scripts |
| `init_env.sh` | Coordinator node setup: packages, system params, users, SSH, DB installation |
| `init_env_segment.sh` | Segment node setup for multi-node deployments |
| `init_cluster.sh` | Database cluster initialization using gpinitsystem |
| `common.sh` | Shared function library (OS detection, system tuning, package management) |
| `segmenthosts.conf` | Host/IP configuration for multi-node clusters |
| `run.sh` | Entry point; checks for existing processes, launches deployment in background |
| `cluster_deploy_web.py` | Flask web UI for local deployment on coordinator |
| `start_web.sh` | Web UI startup: installs deps, creates venv, starts gunicorn (1 worker, 4 threads) |
| `mirrorlessfailover.sh` | Utility for segment failover without mirrors |

### Important: bash vs sh
All scripts require `bash`. On Ubuntu, `/bin/sh` is dash which lacks bash features (`set -o pipefail`, arrays, `source`, etc). All script invocations use `bash` explicitly. The `gpadmin` user is created with `-s /bin/bash`.

### Database Type Detection
`deploycluster.sh` auto-detects database type from RPM/DEB filename and sets:
- `DB_TYPE`, `DB_VERSION`, `DB_KEYWORD`
- `CLOUDBERRY_BINARY_PATH` (e.g., `/usr/local/cloudberry-db`, `/usr/local/greenplum-db`)
- `CLUSTER_ENV` (environment script name)
- `LEGACY_VERSION` (for Greenplum < 7)

Supports both RPM naming (`hashdata-lightning-2.4.0-1.x86_64.rpm`) and DEB naming (`hashdata-lightning_2.4.0-1_amd64.deb`).

Supported databases: Cloudberry, Greenplum (5.x/6.x/7.x), HashData Lightning, SynxDB

### Multi-node Deployment Requirements
1. Configure `segmenthosts.conf` with coordinator and segment IPs/hostnames
2. Set `DEPLOY_TYPE="multi"` in `deploycluster_parameter.sh`
3. Configure segment access (`SEGMENT_ACCESS_METHOD` as `keyfile` or `password`)

### Web UI Architecture
- Flask app running locally on coordinator node
- Calls `deploycluster.sh` via subprocess (no SSH/paramiko)
- Gunicorn with `--workers 1 --threads 4` (required: in-process state sharing)
- Browser-based package upload (drag-drop or click, saved to `/tmp/uploads/`)
- Real-time log streaming via Server-Sent Events
- Configuration stored in Flask session + server-side global (single process)

## Configuration

### Mandatory Parameters (in `deploycluster_parameter.sh`)
- `ADMIN_USER` - OS user for cluster (default: gpadmin)
- `ADMIN_USER_PASSWORD` - Password for OS user and database admin
- `CLOUDBERRY_RPM` - Path to database RPM/DEB (must use absolute path)
- `COORDINATOR_HOSTNAME` / `COORDINATOR_IP`
- `DEPLOY_TYPE` - `single` or `multi`

### Key Optional Parameters
- `INIT_ENV_ONLY` - Set to `true` to skip cluster initialization
- `INSTALL_DB_SOFTWARE` - Set to `false` to skip RPM installation (for reinit)
- `WITH_MIRROR` / `WITH_STANDBY` - Enable mirrors/standby
- `MANUAL_REPO` - Set to `true` to skip auto YUM/APT repo configuration
