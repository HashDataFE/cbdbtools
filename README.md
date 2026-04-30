# CBDBTools

[中文文档](README_zh.md)

CBDBTools is a suite of scripts designed to automate the deployment and initialization of MPP database clusters. It supports Cloudberry, Greenplum, HashData Lightning, and SynxDB.

The tool provides two deployment methods:
1. **Web UI deployment** - 4-step wizard interface for guided deployment
2. **Command-line deployment** - Traditional approach using shell scripts

Both methods run on the **coordinator node** and execute the same underlying deployment scripts.

---

## Table of Contents

- [Repository Structure](#repository-structure)
- [Supported Platforms](#supported-platforms)
- [Prerequisites](#prerequisites)
- [Deployment Methods](#deployment-methods)
  - [Web UI Deployment](#web-ui-deployment)
  - [Command-line Deployment](#command-line-deployment)
- [System Tuning](#system-tuning)
- [Features](#features)
- [Scripts Overview](#scripts-overview)
- [Utility Scripts](#utility-scripts)
- [Troubleshooting](#troubleshooting)

---

## Repository Structure

```
.
├── run.sh                       # CLI entry point
├── deploycluster.sh             # Main orchestration script
├── deploycluster_parameter.sh   # Central configuration file
├── init_env.sh                  # Coordinator environment setup
├── init_env_segment.sh          # Segment node environment setup
├── init_cluster.sh              # Database cluster initialization
├── common.sh                    # Shared function library
├── multissh.sh                  # Parallel SSH command execution
├── multiscp.sh                  # Parallel file distribution
├── segmenthosts.conf            # Host/IP configuration
├── mirrorlessfailover.sh        # Segment failover utility (no mirrors)
├── cluster_deploy_web.py        # Flask web application
├── start_web.sh                 # Web UI startup script
├── wsgi.py                      # WSGI entry point
├── templates/
│   └── index.html               # Web UI (single-page application)
├── test_web.py                  # Web application tests
└── sshpass-1.10.tar.gz          # sshpass source (offline fallback)
```

---

## Supported Platforms

### Operating Systems

| OS | Versions | Package Format |
|----|----------|---------------|
| CentOS / RHEL | 7, 8, 9 | RPM |
| Rocky Linux | 8, 9 | RPM |
| Oracle Linux | 8 | RPM |
| Ubuntu | 20.04, 22.04, 24.04 | DEB |

### Databases

| Database | Versions | Install Path |
|----------|----------|-------------|
| Cloudberry DB | 1.x, 2.x | `/usr/local/cloudberry-db` |
| Greenplum | 5.x, 6.x, 7.x | `/usr/local/greenplum-db` |
| HashData Lightning | 1.x, 2.x | `/usr/local/hashdata-lightning` |
| SynxDB | 1.x, 2.x, 4.x | `/usr/local/synxdb` or `/usr/local/synxdb4` |

---

## Prerequisites

1. **Environment:**
   - The tool must be executed on the **coordinator** server
   - `root` user access is required (supports both password and keyfile authentication)
   - Disks formatted with XFS filesystem, mounted with `noatime,inode64` recommended
   - Sufficient RAM and CPU per database documentation

2. **Dependencies (auto-installed):**
   - `sshpass` (via package manager or compiled from bundled source)
   - `python3`, `pip`, `flask`, `gunicorn` (for Web UI)
   - `chrony` or `ntpd` (for time synchronization)

3. **Network:**
   - All cluster nodes must be reachable from the coordinator
   - Port 5000 accessible for Web UI (firewall is disabled during deployment)

---

## Deployment Methods

### Web UI Deployment

Start the web service on the coordinator:

```bash
bash start_web.sh
```

> **Note:** Always use `bash` (not `sh`) to run the scripts. On Ubuntu, `/bin/sh` is dash which doesn't support bash features used by these scripts.

Then open `http://<coordinator-ip>:5000` in a browser.

The Web UI is a 4-step wizard:

1. **Environment** - Select OS type (auto-detected), deployment mode (single/multi), and database package. You can either enter a path to a package already on the server, or **upload a package directly from your browser** via drag-and-drop (with real-time progress)
2. **Configuration** - Set admin user, coordinator info, segment hosts (multi-node), data directories, and advanced options. Toggle **Mirror Segments** for redundancy, and toggle **Standby Coordinator** to deploy a standby host (multi-node only — checking the box reveals IP + hostname inputs; cbdb supports exactly one standby; the standby reuses the same SSH credentials as segments). Click **Save Configuration** before proceeding
3. **Preview** - Review complete deployment summary, including the standby host when enabled, with warnings for missing mirrors/standby
4. **Deploy** - Real-time log streaming with phase progress indicators. Shows connection info on success

### Command-line Deployment

#### 1. Configure Parameters

Edit `deploycluster_parameter.sh`:

```bash
## Mandatory
export ADMIN_USER="gpadmin"
export ADMIN_USER_PASSWORD="Cbdb@1234"
export CLOUDBERRY_RPM="/root/hashdata-lightning-2.4.0-1.x86_64.rpm"  # or .deb for Ubuntu
export COORDINATOR_HOSTNAME="mdw"
export COORDINATOR_IP="192.168.1.100"
export DEPLOY_TYPE="single"   # or "multi"
```

| Parameter | Description |
|-----------|------------|
| `ADMIN_USER` | OS user for cluster (default: gpadmin) |
| `ADMIN_USER_PASSWORD` | Password for OS user and database admin |
| `CLOUDBERRY_RPM` | Absolute path to RPM or DEB package. Filename determines DB type/version. Supports both RPM naming (`hashdata-lightning-2.4.0-1.x86_64.rpm`) and DEB naming (`hashdata-lightning_2.4.0-1_amd64.deb`) |
| `COORDINATOR_HOSTNAME` | Hostname for coordinator (tool sets this) |
| `COORDINATOR_IP` | IP address of coordinator |
| `DEPLOY_TYPE` | `single` or `multi` |

#### Multi-node additional parameters:

```bash
export SEGMENT_ACCESS_METHOD="keyfile"    # or "password"
export SEGMENT_ACCESS_USER="root"
export SEGMENT_ACCESS_KEYFILE="/root/.ssh/id_rsa"
export SEGMENT_ACCESS_PASSWORD="XXXXXXXX"
```

#### Optional parameters:

| Parameter | Default | Description |
|-----------|---------|------------|
| `INIT_ENV_ONLY` | false | Only configure OS, skip DB install and cluster init |
| `INSTALL_DB_SOFTWARE` | true | Set false to skip RPM install (for reinit) |
| `WITH_MIRROR` | false | Enable mirror segments |
| `WITH_STANDBY` | false | Enable standby coordinator. Requires a `##Standby hosts` block in `segmenthosts.conf` (see below). |
| `MANUAL_REPO` | false | Skip auto YUM/APT repo configuration |
| `COORDINATOR_PORT` | 5432 | Database port |
| `DATA_DIRECTORY` | /data0/database/primary | Space-separated data directories |

#### 2. Define Hosts (multi-node only)

Edit `segmenthosts.conf`:

```
##Define hosts used for Hashdata
#Hashdata hosts begin
##Coordinator hosts
10.14.3.217 mdw
##Segment hosts
10.14.5.184 sdw1
10.14.5.177 sdw2
10.14.5.221 sdw3
10.14.4.65 sdw4
#Hashdata hosts end
```

To deploy a **standby coordinator**, set `WITH_STANDBY="true"` in
`deploycluster_parameter.sh` **and** add a `##Standby hosts` block
between the segment hosts and `#Hashdata hosts end`:

```
##Define hosts used for Hashdata
#Hashdata hosts begin
##Coordinator hosts
10.14.3.217 mdw
##Segment hosts
10.14.5.184 sdw1
10.14.5.177 sdw2
##Standby hosts
10.14.5.250 smdw
#Hashdata hosts end
```

Exactly one standby host is supported (this is a cbdb constraint —
`gpinitstandby` only manages a single standby coordinator). The
standby host receives the same OS-level prep as a segment
(`gpadmin` user, RPM install, kernel params, `/etc/hosts` merge);
`init_cluster.sh` then runs `gpinitstandby -a -s <standby-host>` on
the coordinator after `gpstop -u`. With `WITH_STANDBY="false"` (the
default) the `##Standby hosts` block is ignored, so it is safe to
keep in the config across both deployment modes.

#### 3. Start Deployment

```bash
bash run.sh            # Uses DEPLOY_TYPE from config
bash run.sh single     # Force single-node
bash run.sh multi      # Force multi-node
```

---

## System Tuning

The deployment automatically applies the following optimizations per Greenplum 7.7 best practices:

| Category | Configuration |
|----------|--------------|
| **Kernel parameters** | Shared memory, semaphores, network buffers, IP fragmentation |
| **Dirty memory** | Ratio-based for ≤64GB RAM, byte-based for >64GB RAM |
| **Transparent Huge Pages** | Disabled at runtime + persisted (rc.local / systemd) |
| **Time synchronization** | chrony installed and enabled |
| **Security limits** | nofile=524288, nproc=131072 (with limits.d override) |
| **SSH** | Optimized MaxStartups/MaxSessions/ClientAliveInterval |
| **systemd-logind** | RemoveIPC=no |
| **Firewall/SELinux** | Disabled |

---

## Features

- Auto-detection of database type and version from package filename
- Support for CentOS/RHEL 7-9, Rocky Linux 8-9, Ubuntu 20.04-24.04
- Parallel SSH/SCP operations for multi-node setup
- GP 7.7 compliant system tuning (THP, NTP, dirty memory, nproc)
- Web UI with 4-step wizard, real-time SSE log streaming, and progress tracking
- Both password and keyfile SSH authentication
- Conditional gpinitsystem error handling (warnings vs fatal errors)

---

## Scripts Overview

| Script | Purpose |
|--------|---------|
| `run.sh` | Entry point; prevents duplicate runs, launches deployment in background |
| `deploycluster.sh` | Orchestrates deployment: DB type detection → init_env → init_cluster |
| `common.sh` | Shared functions: OS detection, sysctl, limits, THP, NTP, user management, package install |
| `init_env.sh` | Coordinator setup: deps, system tuning, user creation, SSH keys, DB install, data dirs |
| `init_env_segment.sh` | Segment setup: same tuning + user + DB install (executed via multissh) |
| `init_cluster.sh` | gpinitsystem, admin password, pg_hba.conf, environment variables |
| `cluster_deploy_web.py` | Flask app: config management, package upload, deployment orchestration, SSE log streaming |
| `start_web.sh` | Installs deps, starts gunicorn (1 worker, 4 threads, port 5000) |

---

## Utility Scripts

### multissh.sh

Parallel command execution across multiple hosts.

```bash
sh multissh.sh [options] <command>

Options:
  -f, --hosts-file    Hosts file (one hostname/IP per line)
  -u, --user          SSH username
  -p, --password      SSH password
  -k, --key-file      SSH private key file
  -c, --concurrency   Max parallel connections (default: 5)
  -t, --timeout       Connection timeout (default: 30s)
  -P, --port          SSH port (default: 22)
  -v, --verbose       Verbose output
  -o, --output        Save output to file
```

### multiscp.sh

Parallel file distribution to multiple hosts. Same options as multissh.sh.

```bash
sh multiscp.sh [options] source_file destination_path
```

### Examples

```bash
# Check disk space on all segments
sh multissh.sh -k ~/.ssh/id_rsa -f hosts.txt -u root "df -h"

# Distribute a file to all segments
sh multiscp.sh -k ~/.ssh/id_rsa -f hosts.txt -u root ./config.ini /tmp/
```

---

## Troubleshooting

| Issue | Solution |
|-------|---------|
| SSH connection timeout to segments | Verify network connectivity, check firewall on segment nodes |
| `sysctl: Invalid argument` for dirty memory | Fixed in latest version; ensure common.sh is up to date |
| `gpinitsystem` FATAL errors | Check segment node connectivity, verify hosts in segmenthosts.conf |
| `set: Illegal option -o pipefail` | You ran the script with `sh` instead of `bash`. Use `bash run.sh` |
| `source: not found` on Ubuntu | gpadmin shell must be `/bin/bash`, not `/bin/sh`. Run `usermod -s /bin/bash gpadmin` |
| `ping: command not found` during gpinitsystem | Install `iputils-ping`: `apt install iputils-ping` (auto-installed in latest version) |
| DB type detected as "unknown" for DEB packages | Update deploycluster.sh; latest version supports both RPM and DEB naming |
| Web UI "Save request failed" | Refresh browser (Ctrl+F5), ensure gunicorn is running |
| Web UI shows no logs during deploy | Ensure gunicorn uses `--workers 1` (not multi-worker) |
| THP not disabled after reboot | Check `/sys/kernel/mm/transparent_hugepage/enabled`, verify rc.local or systemd service |

**Logs:**
- CLI deployment: `deploy_cluster_YYYYMMDD_HHMMSS.log` in project directory
- Web UI: Real-time via SSE + same log file
- gpinitsystem: `/home/gpadmin/gpAdminLogs/gpinitsystem_*.log`

---

## Support

For issues or questions, file an issue in this repository.
