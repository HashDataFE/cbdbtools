#!/bin/bash
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VARS_FILE="deploycluster_parameter.sh"

source "${SCRIPT_DIR}/${VARS_FILE}"
source "${SCRIPT_DIR}/common.sh"

working_dir="/tmp/${SEGMENT_ACCESS_USER}"

# Clean and recreate working directory
rm -rf "${working_dir}"
mkdir -p "${working_dir}"
chmod 777 "${working_dir}"

if [ "${1}" = "single" ] || [ "${1}" = "multi" ]; then
  cluster_type="${1}"
else
  cluster_type="${DEPLOY_TYPE}"
fi

function config_hostsfile() {
  log_time "Setting up /etc/hosts on coordinator..."

  # Clear existing entries
  sed -i '/#Hashdata hosts begin/,/#Hashdata hosts end/d' /etc/hosts
  sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' /etc/hosts

  local temp_hosts="${working_dir}/hostsfile"

  echo "#Hashdata hosts begin" > "$temp_hosts"
  echo "##Coordinator hosts" >> "$temp_hosts"
  sed -n '/##Coordinator hosts/,/##Segment hosts/p' "${SCRIPT_DIR}/segmenthosts.conf" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' >> "$temp_hosts"
  echo "##Segment hosts" >> "$temp_hosts"
  sed -n '/##Segment hosts/,/#Hashdata hosts end/p' "${SCRIPT_DIR}/segmenthosts.conf" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' >> "$temp_hosts"
  echo "#Hashdata hosts end" >> "$temp_hosts"

  cat "$temp_hosts" >> /etc/hosts

  log_time "Completed /etc/hosts setup"
}

function copyfile_segment() {
  log_time "Copying deployment files to segment hosts..."
  local hosts_file="${working_dir}/segment_hosts.txt"
  local access_opts

  if [ "${SEGMENT_ACCESS_METHOD}" = "keyfile" ]; then
    access_opts="-k ${SEGMENT_ACCESS_KEYFILE}"
  else
    access_opts="-p ${SEGMENT_ACCESS_PASSWORD}"
  fi

  local common_args="-v ${access_opts} -f ${hosts_file} -u ${SEGMENT_ACCESS_USER}"

  # Create working directory on segments
  bash "${SCRIPT_DIR}/multissh.sh" ${common_args} "rm -rf ${working_dir}; mkdir -p ${working_dir}"

  # Copy required files
  local files_to_copy=(
    "init_env_segment.sh"
    "deploycluster_parameter.sh"
    "common.sh"
    "${working_dir}/hostsfile"
    "/home/${ADMIN_USER}/.ssh/id_rsa.pub"
    "${CLOUDBERRY_RPM}"
  )

  for f in "${files_to_copy[@]}"; do
    local src="$f"
    # If it's a script name without path, prefix with SCRIPT_DIR
    if [[ "$f" != /* ]] && [[ "$f" != "${working_dir}"/* ]]; then
      src="${SCRIPT_DIR}/${f}"
    fi
    log_time "Copying ${src} to segments..."
    bash "${SCRIPT_DIR}/multiscp.sh" ${common_args} "${src}" "${working_dir}"
  done

  log_time "Finished copying files to segment hosts."
}

function init_segment() {
  log_time "Initializing segment hosts..."
  local logfilename
  logfilename="$(date +%Y%m%d_%H%M%S)"
  local hosts_file="${working_dir}/segment_hosts.txt"

  for i in $(cat "${hosts_file}"); do
    local log_file="${working_dir}/init_env_segment_${i}_${logfilename}.log"
    if [ "${SEGMENT_ACCESS_METHOD}" = "keyfile" ]; then
      log_time "Initializing segment: ${i}"
      ssh -n -q -i "${SEGMENT_ACCESS_KEYFILE}" "${SEGMENT_ACCESS_USER}@${i}" \
        "bash -c 'sudo bash ${working_dir}/init_env_segment.sh ${i} ${working_dir} &> ${log_file}'" &
    else
      sshpass -p "${SEGMENT_ACCESS_PASSWORD}" ssh -n -q "${SEGMENT_ACCESS_USER}@${i}" \
        "bash -c 'sudo bash ${working_dir}/init_env_segment.sh ${i} ${working_dir} &> ${log_file}'" &
    fi
  done
  wait

  log_time "Finished segment host initialization."
}

# ==================== Main Execution ====================

log_time "Start env init setting on coordinator..."

#Step 1: Installing Software Dependencies
log_time "Step 1: Installing Software Dependencies..."
configure_repo
configure_timezone "${TIMEZONE:-Asia/Shanghai}"
install_sshpass
install_dependencies

#Step 2: Turn off firewalls
log_time "Step 2: Turn off firewalls..."
disable_firewall

#Step 3: Configuring system parameters
log_time "Step 3: Configuring system parameters..."
configure_sysctl
configure_limits
configure_sshd
configure_logind
disable_thp
configure_ntp

#Step 4: Create database admin user
log_time "Step 4: Create database user ${ADMIN_USER}..."
create_admin_user "${ADMIN_USER}" "${ADMIN_USER_PASSWORD}"

#Step 5: Setup user no-password access
log_time "Step 5: Setup user no-password SSH access..."

# Configure coordinator in /etc/hosts
sed -i '/#Hashdata hosts begin/,/#Hashdata hosts end/d' /etc/hosts
sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' /etc/hosts
echo -e '#Hashdata hosts begin\n'"${COORDINATOR_IP} ${COORDINATOR_HOSTNAME}"'\n#Hashdata hosts end' >> /etc/hosts

change_hostname "${COORDINATOR_HOSTNAME}"

rm -f "/home/${ADMIN_USER}/.ssh/id_rsa.pub"
rm -f "/home/${ADMIN_USER}/.ssh/id_rsa"
rm -f "/home/${ADMIN_USER}/.ssh/authorized_keys"
rm -f "/home/${ADMIN_USER}/.ssh/known_hosts"

# Configure SSH client to suppress host key warnings for cluster hosts only
cat > "/home/${ADMIN_USER}/.ssh/config" <<SSHEOF
Host ${COORDINATOR_HOSTNAME} ${COORDINATOR_IP} localhost
    StrictHostKeyChecking no
    LogLevel ERROR
SSHEOF
chown "${ADMIN_USER}:${ADMIN_USER}" "/home/${ADMIN_USER}/.ssh/config"
chmod 600 "/home/${ADMIN_USER}/.ssh/config"

su "${ADMIN_USER}" -l -c "ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ''"

su "${ADMIN_USER}" -l -c "cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys"
su "${ADMIN_USER}" -l -c "chmod 600 /home/${ADMIN_USER}/.ssh/authorized_keys"

#Step 6: Installing database software
install_db_software "${CLOUDBERRY_RPM}" "${DB_KEYWORD}" "${CLOUDBERRY_BINARY_PATH}"

#Step 7: Create data directories
create_data_directories

log_time "Finished env init setting on coordinator."

# ==================== Multi-node setup ====================
if [ "$cluster_type" = "multi" ]; then
  log_time "Step 8: Setup env on segment nodes..."

  if [ "${SEGMENT_ACCESS_METHOD}" = "keyfile" ]; then
    chmod 600 "${SEGMENT_ACCESS_KEYFILE}"
  fi

  # Extract segment hostnames
  sed -n '/##Segment hosts/,/#Hashdata hosts end/p' "${SCRIPT_DIR}/segmenthosts.conf" | \
    awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/ {print $2}' > "${working_dir}/segment_hosts.txt"

  config_hostsfile

  # Extend SSH config to include segment hosts
  seg_hosts=$(paste -sd' ' "${working_dir}/segment_hosts.txt")
  sed -i "s/^Host .*/& ${seg_hosts}/" "/home/${ADMIN_USER}/.ssh/config"

  # Add segment hosts to known_hosts for root
  for i in $(cat "${working_dir}/segment_hosts.txt"); do
    ssh-keyscan "$i" >> ~/.ssh/known_hosts 2>/dev/null
  done

  copyfile_segment
  init_segment

  #Step 9: Setup no-password access for all nodes
  log_time "Step 9: Setup no-password access for all nodes..."

  # Collect host keys for admin user (coordinator + all segments)
  for i in $(cat "${working_dir}/segment_hosts.txt"); do
    su "${ADMIN_USER}" -l -c "ssh-keyscan -t rsa,ecdsa,ed25519 ${i} >> ~/.ssh/known_hosts 2>/dev/null"
    ip_addr=$(getent hosts "$i" | awk '{print $1}')
    if [ -n "$ip_addr" ]; then
      su "${ADMIN_USER}" -l -c "ssh-keyscan -t rsa,ecdsa,ed25519 ${ip_addr} >> ~/.ssh/known_hosts 2>/dev/null"
    fi
  done

  # Get coordinator hostname from segmenthosts.conf
  export COORDINATOR_HOSTNAME=$(sed -n '/##Coordinator hosts/,/##Segment hosts/p' "${SCRIPT_DIR}/segmenthosts.conf" | sed '1d;$d' | awk '{print $2}')
  change_hostname "${COORDINATOR_HOSTNAME}"

  # Build list of all hosts
  awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/ {print $2}' "${SCRIPT_DIR}/segmenthosts.conf" > "${working_dir}/all_hosts.txt"

  mkdir -p "${working_dir}/ssh_keys"

  # Collect public keys from all nodes
  for node in $(cat "${working_dir}/all_hosts.txt"); do
    sshpass -p "${ADMIN_USER_PASSWORD}" scp -o StrictHostKeyChecking=no -o LogLevel=ERROR \
      "${ADMIN_USER}@${node}:/home/${ADMIN_USER}/.ssh/id_rsa.pub" \
      "${working_dir}/ssh_keys/${node}.pub"
  done

  # Distribute public keys and known_hosts to all nodes
  for target in $(cat "${working_dir}/all_hosts.txt"); do
    sshpass -p "${ADMIN_USER_PASSWORD}" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR \
      "${ADMIN_USER}@${target}" "echo '' > /home/${ADMIN_USER}/.ssh/authorized_keys"

    for keyfile in "${working_dir}"/ssh_keys/*.pub; do
      cat "$keyfile" | sshpass -p "${ADMIN_USER_PASSWORD}" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR \
        "${ADMIN_USER}@${target}" "cat >> /home/${ADMIN_USER}/.ssh/authorized_keys"
    done

    cat "/home/${ADMIN_USER}/.ssh/known_hosts" | sshpass -p "${ADMIN_USER_PASSWORD}" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR \
      "${ADMIN_USER}@${target}" "cat > /home/${ADMIN_USER}/.ssh/known_hosts"

    sshpass -p "${ADMIN_USER_PASSWORD}" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR \
      "${ADMIN_USER}@${target}" "chmod 700 /home/${ADMIN_USER}/.ssh && chmod 600 /home/${ADMIN_USER}/.ssh/authorized_keys && chmod 644 /home/${ADMIN_USER}/.ssh/known_hosts"
  done

  log_time "Finished env init setting on coordinator and all segment nodes."
else
  log_time "Single mode, no segment node setup needed."
fi
