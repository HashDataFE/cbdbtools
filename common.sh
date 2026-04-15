#!/bin/bash
#
# common.sh - Shared functions for CBDB deployment scripts
# Source this file from other scripts: source "$(dirname "$0")/common.sh"
#

# Utility function for logging with timestamps
function log_time() {
  printf "[%s] %b\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}
export -f log_time

# Detect OS family, version, and package manager
detect_os() {
    if [ ! -f /etc/os-release ]; then
        log_time "Warning: /etc/os-release not found. Defaulting to RHEL family."
        export OS_FAMILY="rhel"
        export OS_ID="unknown"
        export OS_VERSION="0"
        export PKG_MANAGER="yum"
        export PKG_FORMAT="rpm"
        return 1
    fi

    source /etc/os-release

    export OS_ID="$ID"
    export OS_VERSION="$(echo "$VERSION_ID" | cut -d. -f1)"

    case "$ID" in
        ubuntu|debian)
            export OS_FAMILY="debian"
            export PKG_MANAGER="apt"
            export PKG_FORMAT="deb"
            ;;
        centos|rhel|ol|rocky|almalinux|fedora)
            export OS_FAMILY="rhel"
            export PKG_FORMAT="rpm"
            local version_major
            version_major="$(echo "$VERSION_ID" | cut -d. -f1)"
            if [ "$version_major" -ge 8 ] 2>/dev/null && command -v dnf &>/dev/null; then
                export PKG_MANAGER="dnf"
            else
                export PKG_MANAGER="yum"
            fi
            ;;
        *)
            log_time "Warning: Unrecognized OS ID '$ID'. Defaulting to RHEL family."
            export OS_FAMILY="rhel"
            export PKG_MANAGER="yum"
            export PKG_FORMAT="rpm"
            ;;
    esac

    log_time "Detected OS: ${OS_ID} ${OS_VERSION} (family=${OS_FAMILY}, pkg=${PKG_MANAGER}, format=${PKG_FORMAT})"
}

# Run OS detection immediately so all functions can use the variables
detect_os

# Change hostname across supported OS types
change_hostname() {
    local new_hostname="$1"

    if [[ $EUID -ne 0 ]]; then
        echo "Error: This operation requires root privileges." >&2
        return 1
    fi

    if [[ ! "$new_hostname" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]]; then
        echo "Error: Invalid hostname format: $new_hostname" >&2
        return 1
    fi

    local current_hostname
    current_hostname=$(hostname)
    echo "Changing hostname from $current_hostname to $new_hostname..."

    if [[ -f /etc/os-release ]]; then
        source /etc/os-release

        case "$ID" in
            ubuntu|debian)
                echo "$new_hostname" > /etc/hostname
                sed -i "s/127.0.1.1[[:space:]]*$current_hostname/127.0.1.1\t$new_hostname/g" /etc/hosts
                hostnamectl set-hostname "$new_hostname"
                ;;
            centos|rhel|fedora|ol)
                echo "$new_hostname" > /etc/hostname
                sed -i "s/127.0.0.1[[:space:]]*$current_hostname/127.0.0.1\t$new_hostname/g" /etc/hosts
                hostnamectl set-hostname "$new_hostname"
                ;;
            *)
                if [[ -f /etc/hostname ]]; then
                    echo "$new_hostname" > /etc/hostname
                fi
                if [[ -f /etc/hosts ]]; then
                    sed -i "s/127.0.1.1[[:space:]]*$current_hostname/127.0.1.1\t$new_hostname/g" /etc/hosts
                fi
                if command -v hostnamectl &>/dev/null; then
                    hostnamectl set-hostname "$new_hostname"
                else
                    hostname "$new_hostname"
                    echo "Warning: Hostname change may not be persistent after reboot." >&2
                fi
                ;;
        esac

    elif [[ "$(uname)" == "Darwin" ]]; then
        scutil --set HostName "$new_hostname"
        scutil --set LocalHostName "$new_hostname"
        scutil --set ComputerName "$new_hostname"
        dscacheutil -flushcache
    else
        echo "Error: Unsupported operating system." >&2
        return 1
    fi

    echo "Hostname changed to: $(hostname)"
    return 0
}
export -f change_hostname

# Disable firewall and SELinux
disable_firewall() {
    log_time "Disabling firewall and SELinux..."

    if [ "$OS_FAMILY" = "debian" ]; then
        # Ubuntu/Debian: disable ufw
        if command -v ufw &>/dev/null; then
            ufw disable 2>/dev/null || true
            log_time "ufw disabled."
        fi
        # No SELinux on Ubuntu/Debian by default, skip
    else
        # RHEL/CentOS: disable firewalld and SELinux
        systemctl stop firewalld.service 2>/dev/null || true
        systemctl disable firewalld.service 2>/dev/null || true

        if [ -f /etc/selinux/config ]; then
            sed -i 's/^SELINUX=.*$/SELINUX=disabled/' /etc/selinux/config
        fi
        setenforce 0 2>/dev/null || true
    fi
}

# Configure package repositories based on OS version
# Backward compatible: also available as configure_yum_repo()
configure_repo() {
    if [ "${MANUAL_REPO}" = "true" ]; then
        log_time "Manual repo mode - skipping auto configuration."
        return 0
    fi

    if [ "$OS_FAMILY" = "debian" ]; then
        log_time "Updating apt package lists..."
        apt-get update -y
        return $?
    fi

    # RHEL family: existing YUM/DNF repo configuration
    log_time "Detecting OS version for YUM repo configuration..."

    if [ ! -f /etc/os-release ]; then
        log_time "Warning: /etc/os-release not found. Skipping YUM repo configuration."
        return 1
    fi

    source /etc/os-release

    # Check for Oracle Linux
    local is_oracle=0
    if [[ "$ID" == "ol" || "$NAME" == *"Oracle Linux"* ]]; then
        is_oracle=1
        log_time "Detected Oracle Linux"
    fi

    local version_major
    version_major=$(echo "$VERSION_ID" | cut -d. -f1)

    case "$version_major" in
        7)
            log_time "Detected CentOS/RHEL 7"
            rm -rf /etc/yum.repos.d/*
            curl -o /etc/yum.repos.d/CentOS-Base.repo https://mirrors.huaweicloud.com/repository/conf/CentOS-7-anon.repo
            yum clean all
            yum makecache
            yum install -y libcgroup-tools
            ;;
        8)
            log_time "Detected CentOS/RHEL 8"
            if [ "$is_oracle" -ne 1 ]; then
                rm -rf /etc/yum.repos.d/*
                curl -o /etc/yum.repos.d/CentOS-Base.repo https://mirrors.huaweicloud.com/repository/conf/CentOS-8-anon.repo
                yum clean all
                yum makecache
            else
                log_time "Skipping YUM repo for Oracle Linux."
            fi
            ;;
        9)
            log_time "Detected CentOS/RHEL 9 - using default repos."
            ;;
        *)
            log_time "Warning: Unsupported OS version: $version_major"
            ;;
    esac
}

# Backward compatibility alias
configure_yum_repo() {
    configure_repo "$@"
}

# Configure sysctl kernel parameters
configure_sysctl() {
    log_time "Configuring kernel parameters..."

    local shmall shmmax min_free_kbytes
    shmall=$(expr $(getconf _PHYS_PAGES) / 2)
    shmmax=$(expr $(getconf _PHYS_PAGES) / 2 \* $(getconf PAGE_SIZE))
    min_free_kbytes=$(awk 'BEGIN {OFMT = "%.0f";} /MemTotal/ {print $2 * .03;}' /proc/meminfo)

    # Remove previous configuration blocks
    while grep -q '# BEGIN HashData sysctl CONFIG' /etc/sysctl.conf; do
        sed -i '/# BEGIN HashData sysctl CONFIG/,/# END HashData sysctl CONFIG/d' /etc/sysctl.conf
    done

    cat >> /etc/sysctl.conf <<EOF
# BEGIN HashData sysctl CONFIG
######################
# HashData CONFIG PARAMS #
######################

kernel.shmall = ${shmall}
kernel.shmmax = ${shmmax}
kernel.shmmni = 32768
vm.overcommit_memory = 2
vm.overcommit_ratio = 95
vm.min_free_kbytes = ${min_free_kbytes}
net.ipv4.ip_local_port_range = 10000 65535
kernel.sem = 32000 1048576000 1000 32768
kernel.sysrq = 1
kernel.core_uses_pid = 1
kernel.msgmnb = 65536
kernel.msgmax = 65536
kernel.msgmni = 32768
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.conf.all.arp_filter = 1
net.ipv4.ipfrag_high_thresh = 536870912
net.ipv4.ipfrag_low_thresh = 429496730
net.ipv4.ipfrag_time = 60
net.core.netdev_max_backlog = 10000
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
vm.swappiness = 1
vm.zone_reclaim_mode = 0
vm.dirty_expire_centisecs = 500
vm.dirty_writeback_centisecs = 100
$(awk '/MemTotal/{mem_kb=$2} END{
    if (mem_kb <= 67108864) {
        print "vm.dirty_background_ratio = 3"
        print "vm.dirty_ratio = 10"
    } else {
        print "vm.dirty_background_bytes = 1610612736"
        print "vm.dirty_bytes = 4294967296"
    }
}' /proc/meminfo)
kernel.core_pattern=/var/core/core.%h.%t
# END HashData sysctl CONFIG
EOF

    # Clear conflicting dirty memory settings before applying
    # (bytes and ratio cannot both be non-zero simultaneously)
    sysctl -w vm.dirty_background_ratio=0 vm.dirty_ratio=0 2>/dev/null
    sysctl -w vm.dirty_background_bytes=0 vm.dirty_bytes=0 2>/dev/null
    sysctl -p
}

# Configure security limits
configure_limits() {
    log_time "Configuring security limits..."

    while grep -q '# BEGIN HashData limits CONFIG' /etc/security/limits.conf; do
        sed -i '/# BEGIN HashData limits CONFIG/,/# END HashData limits CONFIG/d' /etc/security/limits.conf
    done

    cat >> /etc/security/limits.conf <<'EOF'
# BEGIN HashData limits CONFIG
######################
# HashData CONFIG PARAMS #
######################

* soft nofile 524288
* hard nofile 524288
* soft nproc 131072
* hard nproc 131072
* soft  core unlimited
# END HashData limits CONFIG
EOF

    # Override RHEL default nproc limits in limits.d (takes precedence over limits.conf)
    if [ -d /etc/security/limits.d ]; then
        cat > /etc/security/limits.d/99-hashdata-nproc.conf <<'NPROC'
# Override default nproc limits for database workloads
* soft nproc 131072
* hard nproc 131072
NPROC
    fi
}

# Configure SSH daemon
configure_sshd() {
    log_time "Configuring SSH daemon..."

    while grep -q '# BEGIN HashData sshd CONFIG' /etc/ssh/sshd_config; do
        sed -i '/# BEGIN HashData sshd CONFIG/,/# END HashData sshd CONFIG/d' /etc/ssh/sshd_config
    done

    cat >> /etc/ssh/sshd_config <<'EOF'
# BEGIN HashData sshd CONFIG
######################
# HashData SSH PARAMS #
######################

ClientAliveInterval 60
ClientAliveCountMax 9999
MaxStartups 1000:30:3000
MaxSessions 3000
# END HashData sshd CONFIG
EOF

    systemctl restart sshd
}

# Configure systemd logind (disable RemoveIPC)
configure_logind() {
    log_time "Configuring systemd logind (RemoveIPC=no)..."

    if [ ! -f /etc/systemd/logind.conf ]; then
        log_time "Warning: /etc/systemd/logind.conf not found. Skipping."
        return 0
    fi

    sed -i '/^#*RemoveIPC=/d' /etc/systemd/logind.conf
    echo "RemoveIPC=no" >> /etc/systemd/logind.conf

    if systemctl restart systemd-logind 2>/dev/null; then
        log_time "systemd-logind restarted successfully"
    else
        log_time "Warning: Failed to restart systemd-logind. Reboot may be required."
    fi
}

# Disable Transparent Huge Pages (THP) - causes performance degradation in database workloads
disable_thp() {
    log_time "Disabling Transparent Huge Pages (THP)..."

    # Runtime disable
    if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
        echo never > /sys/kernel/mm/transparent_hugepage/enabled
    fi
    if [ -f /sys/kernel/mm/transparent_hugepage/defrag ]; then
        echo never > /sys/kernel/mm/transparent_hugepage/defrag
    fi

    # Persist via rc.local for RHEL/CentOS
    if [ "$OS_FAMILY" = "rhel" ]; then
        local rc_file="/etc/rc.d/rc.local"
        if [ ! -f "$rc_file" ]; then
            echo '#!/bin/bash' > "$rc_file"
        fi
        chmod +x "$rc_file"
        sed -i '/transparent_hugepage/d' "$rc_file"
        cat >> "$rc_file" <<'THPEOF'
# Disable THP for database performance
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
fi
if [ -f /sys/kernel/mm/transparent_hugepage/defrag ]; then
    echo never > /sys/kernel/mm/transparent_hugepage/defrag
fi
THPEOF
    elif [ "$OS_FAMILY" = "debian" ]; then
        # Use systemd service for Ubuntu
        cat > /etc/systemd/system/disable-thp.service <<'THPSVC'
[Unit]
Description=Disable Transparent Huge Pages
DefaultDependencies=no
After=sysinit.target local-fs.target
Before=basic.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled && echo never > /sys/kernel/mm/transparent_hugepage/defrag'

[Install]
WantedBy=basic.target
THPSVC
        systemctl daemon-reload
        systemctl enable disable-thp 2>/dev/null
    fi

    # Verify
    local thp_status
    thp_status=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)
    if echo "$thp_status" | grep -q '\[never\]'; then
        log_time "THP disabled successfully."
    else
        log_time "Warning: THP may not be fully disabled. Current: ${thp_status}"
    fi
}

# Configure NTP/chrony time synchronization
configure_ntp() {
    log_time "Configuring time synchronization..."

    if command -v chronyd &>/dev/null; then
        systemctl enable chronyd 2>/dev/null
        systemctl start chronyd 2>/dev/null
        log_time "chrony is running."
    elif command -v ntpd &>/dev/null; then
        systemctl enable ntpd 2>/dev/null
        systemctl start ntpd 2>/dev/null
        log_time "ntpd is running."
    else
        # Install chrony
        if [ "$OS_FAMILY" = "debian" ]; then
            apt-get install -y chrony 2>/dev/null
        else
            ${PKG_MANAGER} install -y chrony 2>/dev/null
        fi
        systemctl enable chronyd 2>/dev/null
        systemctl start chronyd 2>/dev/null
        log_time "chrony installed and started."
    fi
}

# Configure timezone
configure_timezone() {
    local timezone="${1:-Asia/Shanghai}"
    log_time "Setting timezone to ${timezone}..."
    timedatectl set-timezone "$timezone" 2>/dev/null || true
    if [ -f "/usr/share/zoneinfo/${timezone}" ]; then
        cp "/usr/share/zoneinfo/${timezone}" /etc/localtime
    fi
}

# Install common dependencies
install_dependencies() {
    log_time "Installing common dependencies..."

    if [ "$OS_FAMILY" = "debian" ]; then
        apt-get update -y

        apt-get install -y \
            libapr1 libaprutil1 bash bzip2 curl iproute2 libkrb5-dev libcurl4 \
            libevent-dev uuid-runtime libuv1 libxml2 libyaml-0-2 libzstd1 \
            libldap2-dev openssh-client openssh-server openssl libssl-dev \
            perl python3 python3-psycopg2 python3-psutil python3-yaml \
            python3-setuptools python3-dev libreadline-dev rsync sed tar \
            zip zlib1g lz4 keyutils git passwd wget net-tools libicu-dev \
            iputils-ping hostname

        # nmon may not be available on all Ubuntu versions
        apt-get install -y nmon || true

        if [ $? -ne 0 ]; then
            log_time "Warning: Some packages may have failed to install."
        fi
    else
        # RHEL/CentOS family
        ${PKG_MANAGER} install -y epel-release

        ${PKG_MANAGER} install -y \
            apr apr-util bash bzip2 curl iproute krb5-devel libcurl libevent \
            libuuid libuv libxml2 libyaml libzstd openldap openssh openssh-clients \
            openssh-server openssl openssl-libs perl python3 python3-psycopg2 \
            python3-psutil python3-pyyaml python3-setuptools python3-devel python39 \
            readline rsync sed tar which zip zlib lz4 keyutils \
            git passwd wget net-tools nmon libicu

        if [ $? -ne 0 ]; then
            log_time "Warning: Some packages may have failed to install."
        fi
    fi
}

# Install sshpass (from repo or source)
install_sshpass() {
    if command -v sshpass &>/dev/null; then
        log_time "sshpass is already installed."
        return 0
    fi

    log_time "Installing sshpass..."

    if [ "$OS_FAMILY" = "debian" ]; then
        if apt-get install -y sshpass 2>/dev/null; then
            log_time "sshpass installed via apt."
            return 0
        fi
    else
        if ${PKG_MANAGER} install -y sshpass 2>/dev/null; then
            log_time "sshpass installed via ${PKG_MANAGER}."
            return 0
        fi
    fi

    log_time "Building sshpass from source..."
    if [ "$OS_FAMILY" = "debian" ]; then
        apt-get install -y tar gcc make
    else
        ${PKG_MANAGER} install -y tar gcc make
    fi

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [ -f "${script_dir}/sshpass-1.10.tar.gz" ]; then
        local build_dir
        build_dir=$(mktemp -d)
        tar -zxf "${script_dir}/sshpass-1.10.tar.gz" -C "$build_dir"
        cd "${build_dir}/sshpass-1.10" || return 1
        ./configure && make && make install
        local rc=$?
        cd "$script_dir"
        rm -rf "$build_dir"
        if [ $rc -ne 0 ]; then
            log_time "Error: Failed to install sshpass from source."
            return 1
        fi
    else
        log_time "Error: sshpass source tarball not found."
        return 1
    fi

    log_time "sshpass installed successfully."
}

# Create database admin user
create_admin_user() {
    local admin_user="${1:-$ADMIN_USER}"
    local admin_password="${2:-$ADMIN_USER_PASSWORD}"

    log_time "Creating/configuring database user ${admin_user}..."

    if ! id "$admin_user" &>/dev/null; then
        groupadd "$admin_user" 2>/dev/null || true
        useradd "$admin_user" -r -m -g "$admin_user" -s /bin/bash
    else
        # Clean up stale environment from previous installations
        if grep -qE 'COORDINATOR_DATA_DIRECTORY|MASTER_DATA_DIRECTORY|greenplum_path.sh|cluster_env.sh|synxdb_path.sh|cloudberry-env.sh|PGPORT' "/home/${admin_user}/.bashrc" 2>/dev/null; then
            log_time "Cleaning up previous environment in .bashrc..."
            cp "/home/${admin_user}/.bashrc" "/home/${admin_user}/bashrc.backup.$(date +%Y%m%d%H%M%S)"
            sed -i -E '/COORDINATOR_DATA_DIRECTORY|MASTER_DATA_DIRECTORY|greenplum_path.sh|cluster_env.sh|synxdb_path.sh|cloudberry-env.sh|PGPORT/d' "/home/${admin_user}/.bashrc"
        fi
    fi

    if [ "$OS_FAMILY" = "debian" ]; then
        # Ubuntu/Debian: use sudo group and chpasswd
        usermod -aG sudo "$admin_user"
        echo "${admin_user}:${admin_password}" | chpasswd

        if ! grep -q "%sudo ALL=(ALL) NOPASSWD: ALL" /etc/sudoers; then
            echo "%sudo ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
        fi
    else
        # RHEL/CentOS: use wheel group and passwd --stdin
        usermod -aG wheel "$admin_user"
        echo "$admin_password" | passwd --stdin "$admin_user"

        if ! grep -q "%wheel ALL=(ALL) NOPASSWD: ALL" /etc/sudoers; then
            echo "%wheel ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
        fi
    fi

    chown -R "${admin_user}:${admin_user}" "/home/${admin_user}"
}

# Install database software package (RPM or DEB)
install_db_software() {
    local pkg_path="$1"
    local keyword="$2"
    local soft_link="$3"

    if [ "${INSTALL_DB_SOFTWARE}" = "false" ]; then
        log_time "INSTALL_DB_SOFTWARE=false, skipping database software installation."
        return 0
    fi

    log_time "Installing database software..."

    # Check if package exists, try to download if not
    if [ ! -f "$pkg_path" ]; then
        # Support both new CLOUDBERRY_PKG_URL and old CLOUDBERRY_RPM_URL
        local download_url="${CLOUDBERRY_PKG_URL:-$CLOUDBERRY_RPM_URL}"
        if [ -n "$download_url" ] && [ "$download_url" != "http://downloadlink.com/cloudberry.rpm" ]; then
            log_time "Package not found, downloading from ${download_url}..."
            wget "$download_url" -O "$pkg_path"
            if [ $? -ne 0 ]; then
                log_time "Error: Failed to download package."
                return 1
            fi
        else
            log_time "Error: Package file not found: $pkg_path"
            return 1
        fi
    fi

    # Detect package format from file extension
    local pkg_format
    case "$pkg_path" in
        *.deb)  pkg_format="deb" ;;
        *.rpm)  pkg_format="rpm" ;;
        *)      pkg_format="$PKG_FORMAT" ;;
    esac

    if [ "$keyword" != "unknown" ]; then
        # Remove previous soft link if exists
        if [ -L "$soft_link" ]; then
            rm -f "$soft_link"
            log_time "Removed existing soft link: $soft_link"
        fi

        # Install package based on format
        if [ "$pkg_format" = "deb" ]; then
            if ! dpkg -i "$pkg_path" 2>/dev/null; then
                log_time "dpkg install had issues, resolving dependencies..."
                apt-get install -f -y
            fi
        else
            if ! rpm -ivh "$pkg_path" --force 2>/dev/null; then
                log_time "RPM install failed, trying ${PKG_MANAGER}..."
                ${PKG_MANAGER} install -y "$pkg_path"
            fi
        fi

        # Remove circular symlinks that cause filesystem loop errors
        # (some RPMs create self-referencing symlinks inside the install directory)
        for _dir in /usr/local/${keyword}*/; do
            [ -d "$_dir" ] || continue
            _base=$(basename "$_dir")
            if [ -L "${_dir}${_base}" ]; then
                rm -f "${_dir}${_base}"
                log_time "Removed circular symlink: ${_dir}${_base}"
            fi
        done

        # Fix ownership
        chown -R "${ADMIN_USER}:${ADMIN_USER}" /usr/local/${keyword}* 2>/dev/null || true
        chown -R "${ADMIN_USER}:${ADMIN_USER}" ${soft_link}* 2>/dev/null || true
    else
        log_time "Unknown database type, installing with package manager..."
        if [ "$pkg_format" = "deb" ]; then
            dpkg -i "$pkg_path" 2>/dev/null || true
            apt-get install -f -y
        else
            ${PKG_MANAGER} install -y "$pkg_path"
        fi
    fi

    log_time "Finished database software installation."
}

# Create data directories for the cluster
create_data_directories() {
    if [ "${INIT_ENV_ONLY}" = "true" ]; then
        log_time "INIT_ENV_ONLY mode, skipping data directory creation."
        return 0
    fi

    log_time "Creating data directories..."

    # shellcheck disable=SC2086
    rm -rf ${COORDINATOR_DIRECTORY} ${DATA_DIRECTORY}
    # shellcheck disable=SC2086
    mkdir -p ${COORDINATOR_DIRECTORY} ${DATA_DIRECTORY}
    # shellcheck disable=SC2086
    chown -R "${ADMIN_USER}:${ADMIN_USER}" ${COORDINATOR_DIRECTORY} ${DATA_DIRECTORY}

    if [ "${WITH_MIRROR}" = "true" ]; then
        # shellcheck disable=SC2086
        rm -rf ${MIRROR_DATA_DIRECTORY}
        # shellcheck disable=SC2086
        mkdir -p ${MIRROR_DATA_DIRECTORY}
        # shellcheck disable=SC2086
        chown -R "${ADMIN_USER}:${ADMIN_USER}" ${MIRROR_DATA_DIRECTORY}
    fi

    log_time "Data directories created."
}
