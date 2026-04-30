#!/bin/bash
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VARS_FILE="deploycluster_parameter.sh"

source "${SCRIPT_DIR}/${VARS_FILE}"

if [ "${1}" = "single" ] || [ "${1}" = "multi" ]; then
  cluster_type="${1}"
else
  cluster_type="${DEPLOY_TYPE}"
fi

if [ "$cluster_type" = "multi" ]; then
  COORDINATOR_HOSTNAME=$(sed -n '/##Coordinator hosts/,/##Segment hosts/p' "${SCRIPT_DIR}/segmenthosts.conf" | sed '1d;$d' | awk '{print $2}')
fi

log_time "LEGACY_VERSION=${LEGACY_VERSION}"

# Validate environment file
env_file="${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV}"
if [ ! -f "${env_file}" ]; then
    log_time "Environment file ${env_file} not found, searching for alternatives..."

    config_files=("greenplum_path.sh" "cluster_env.sh" "synxdb_path.sh" "cloudberry-env.sh")
    found_config=""

    for config in "${config_files[@]}"; do
        if [ -f "${CLOUDBERRY_BINARY_PATH}/${config}" ]; then
            found_config="${config}"
            log_time "Found: ${CLOUDBERRY_BINARY_PATH}/${config}"
            break
        fi
    done

    if [ -n "${found_config}" ]; then
        export CLUSTER_ENV="${found_config}"
        log_time "Using CLUSTER_ENV: ${CLUSTER_ENV}"
    else
        log_time "ERROR: No configuration files found in ${CLOUDBERRY_BINARY_PATH}"
        log_time "Searched for: ${config_files[*]}"
        exit 1
    fi
else
    log_time "Using environment file: ${env_file}"
fi

# Generate gpinitsystem config
rm -f "${INIT_CONFIGFILE}" "${MACHINE_LIST_FILE}"

cat > "${INIT_CONFIGFILE}" <<EOF
ARRAY_NAME=${ARRAY_NAME}
MACHINE_LIST_FILE=${MACHINE_LIST_FILE}
SEG_PREFIX=${SEG_PREFIX}
PORT_BASE=${PORT_BASE}
declare -a DATA_DIRECTORY=(${DATA_DIRECTORY})
TRUSTED_SHELL=${TRUSTED_SHELL}
CHECK_POINT_SEGMENTS=${CHECK_POINT_SEGMENTS}
ENCODING=${ENCODING}
DATABASE_NAME=${DATABASE_NAME}
EOF

if [ "$LEGACY_VERSION" = "true" ]; then
  cat >> "${INIT_CONFIGFILE}" <<EOF
MASTER_HOSTNAME=${COORDINATOR_HOSTNAME}
MASTER_DIRECTORY=${COORDINATOR_DIRECTORY}
MASTER_PORT=${COORDINATOR_PORT}
EOF
else
  cat >> "${INIT_CONFIGFILE}" <<EOF
COORDINATOR_HOSTNAME=${COORDINATOR_HOSTNAME}
COORDINATOR_DIRECTORY=${COORDINATOR_DIRECTORY}
COORDINATOR_PORT=${COORDINATOR_PORT}
EOF
fi

if [ "$WITH_MIRROR" = "true" ]; then
  cat >> "${INIT_CONFIGFILE}" <<EOF
MIRROR_PORT_BASE=${MIRROR_PORT_BASE}
MIRROR_DATA_DIRECTORY=(${MIRROR_DATA_DIRECTORY})
EOF
fi

# Generate machine list. The sed range is bounded at ##Standby hosts
# (alternation falls through to #Hashdata hosts end when no standby
# block is present, so the no-standby case is unchanged). Without the
# bound, the awk picks up the literal "hosts" word from the
# ##Standby hosts header line and feeds it to ping, FATALing
# gpinitsystem with "Unknown host hosts"; the standby IP would also
# leak in and gpinitsystem would create a gpseg-N on the standby host.
if [ "$cluster_type" = "single" ]; then
  echo "${COORDINATOR_HOSTNAME}" > "${MACHINE_LIST_FILE}"
elif [ "$cluster_type" = "multi" ]; then
  sed -n '/##Segment hosts/,/##Standby hosts\|#Hashdata hosts end/p' "${SCRIPT_DIR}/segmenthosts.conf" | \
    awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/ {print $2}' > "${MACHINE_LIST_FILE}"
else
  log_time "Error: DEPLOY_TYPE must be 'single' or 'multi', got: ${cluster_type}"
  exit 1
fi

chown -R "${ADMIN_USER}:${ADMIN_USER}" "${CLOUDBERRY_BINARY_PATH}" "${INIT_CONFIGFILE}" "${MACHINE_LIST_FILE}"

COORDINATOR_DATA_DIRECTORY="${COORDINATOR_DIRECTORY}/${SEG_PREFIX}-1"

# Initialize cluster
log_time "Running gpinitsystem..."
gpinit_exit=0
su "${ADMIN_USER}" -l -c "source ${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV}; gpinitsystem -a -c ${INIT_CONFIGFILE} -h ${MACHINE_LIST_FILE}" || gpinit_exit=$?

if [ "$gpinit_exit" -gt 1 ]; then
    # Exit code > 1 indicates fatal error (code 1 is warnings only)
    log_time "FATAL: gpinitsystem failed with exit code ${gpinit_exit}. Aborting."
    exit "$gpinit_exit"
elif [ "$gpinit_exit" -eq 1 ]; then
    log_time "Warning: gpinitsystem returned warnings. Check logs for details."
fi

# Set admin user password
log_time "Setting database admin password..."
DB_PASSWORD="${ADMIN_USER_PASSWORD:-Hashdata@123}"
su "${ADMIN_USER}" -l -c "export COORDINATOR_DATA_DIRECTORY=${COORDINATOR_DATA_DIRECTORY}; source ${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV}; psql -d ${DATABASE_NAME} -c \"alter user ${ADMIN_USER} password '${DB_PASSWORD}'\"" || \
    log_time "Warning: Failed to set admin password."

# Configure pg_hba.conf for remote access
# WARNING: Using md5 authentication. For stricter security, consider scram-sha-256
# and restricting the IP range (e.g., 192.168.0.0/16 instead of 0.0.0.0/0).
echo "host all all 0.0.0.0/0 md5" >> "${COORDINATOR_DATA_DIRECTORY}/pg_hba.conf"

# Setup environment variables for admin user
log_time "Setting up environment variables for ${ADMIN_USER}..."

echo "source ${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV}" >> "/home/${ADMIN_USER}/.bashrc"

if [ "$LEGACY_VERSION" = "true" ]; then
  sed -i '/MASTER_DATA_DIRECTORY/d' "/home/${ADMIN_USER}/.bashrc"
  echo "export MASTER_DATA_DIRECTORY=${COORDINATOR_DATA_DIRECTORY}" >> "/home/${ADMIN_USER}/.bashrc"
else
  sed -i '/COORDINATOR_DATA_DIRECTORY/d' "/home/${ADMIN_USER}/.bashrc"
  echo "export COORDINATOR_DATA_DIRECTORY=${COORDINATOR_DATA_DIRECTORY}" >> "/home/${ADMIN_USER}/.bashrc"
fi

echo "export PGPORT=${COORDINATOR_PORT}" >> "/home/${ADMIN_USER}/.bashrc"

log_time "Finished setting up environment variables for ${ADMIN_USER}."

# Reload configuration
su "${ADMIN_USER}" -l -c "source ${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV}; gpstop -u" || \
    log_time "Warning: gpstop -u failed."

# Standby coordinator init. WITH_STANDBY=true means init_env.sh has
# already provisioned the standby host (gpadmin user, RPM, /etc/hosts
# merge, gpadmin SSH trust to all hosts). Now wire it up via
# gpinitstandby on the coordinator. cbdb supports exactly one standby
# coordinator so standby_hosts.txt is expected to carry one hostname.
# init_env.sh's working_dir is a local var in that script's process;
# rebuild it from SEGMENT_ACCESS_USER (same convention) so this script
# stands alone.
standby_hosts_file="/tmp/${SEGMENT_ACCESS_USER}/standby_hosts.txt"
if [ "${WITH_STANDBY}" = "true" ] && [ -s "${standby_hosts_file}" ]; then
  STANDBY_HOSTNAME=$(head -n1 "${standby_hosts_file}")
  log_time "Initializing standby coordinator on ${STANDBY_HOSTNAME}..."
  gpinit_stdby_exit=0
  su "${ADMIN_USER}" -l -c "
    export COORDINATOR_DATA_DIRECTORY=${COORDINATOR_DATA_DIRECTORY};
    source ${CLOUDBERRY_BINARY_PATH}/${CLUSTER_ENV};
    gpinitstandby -a -s ${STANDBY_HOSTNAME}
  " || gpinit_stdby_exit=$?
  if [ "${gpinit_stdby_exit}" -gt 1 ]; then
    log_time "Warning: gpinitstandby exited ${gpinit_stdby_exit}; standby may not be configured."
  else
    log_time "Standby coordinator initialised on ${STANDBY_HOSTNAME}."
  fi
fi

log_time "Finished cluster initialization."
