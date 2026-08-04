#!/bin/bash
# deploy-on-merge.sh — install/refresh nousergon-console on the dashboard box.
# Invoked via SSM (as root) from .github/workflows/deploy.yml AFTER the caller
# has already pulled the repo to the target SHA.
#
# Steps: venv + editable install with [aws] extra · fetch private config.yaml
# from SSM · install/refresh the systemd unit · restart · health-check :5180.
#
# Usage: bash infrastructure/deploy-on-merge.sh <target-sha>

set -uo pipefail

REPO_DIR="/home/ec2-user/nousergon-console"
LOG="/var/log/nousergon-console-deploy.log"
TARGET_SHA="${1:-HEAD}"
UNIT_SRC="$REPO_DIR/infrastructure/nousergon-console.service"
UNIT_DST="/etc/systemd/system/nousergon-console.service"
# Private config — Parameter Store path, never a repo path. The parameter holds
# the full config.yaml body. Create/update with:
#   AWS_PROFILE=ne-admin aws ssm put-parameter --name "$CONFIG_SSM" --type SecureString --overwrite --value file://config.yaml
CONFIG_SSM="${CONSOLE_CONFIG_SSM:-/alpha-engine/nousergon-console/config.yaml}"
CONFIG_DST="$REPO_DIR/config.yaml"
HEALTH_URL="http://127.0.0.1:5180/"
VENV_PY="$REPO_DIR/.venv/bin/python"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
fail() { log "FAIL $*"; exit 1; }

wait_for_health() {
    local url="$1" label="$2" n=0
    while [ $n -lt 30 ]; do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            log "OK   $label — health passed at t=${n}s"
            return 0
        fi
        sleep 1
        n=$((n + 1))
    done
    log "FAIL $label — health check timed out after 30s"
    return 1
}

log "=== nousergon-console deploy start sha=$TARGET_SHA ==="

# 1. venv + install (aws extra pulls boto3 for the production adapters)
if [ ! -x "$VENV_PY" ]; then
    log "creating venv at $REPO_DIR/.venv"
    python3.12 -m venv "$REPO_DIR/.venv" 2>/dev/null \
        || python3 -m venv "$REPO_DIR/.venv" \
        || fail "venv create failed"
fi
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -e "$REPO_DIR[aws]" || fail "pip install failed"
log "installed package with [aws] extra"

# 2. private config from SSM (never from the repo)
if ! conf_body=$(aws ssm get-parameter --name "$CONFIG_SSM" --with-decryption \
        --query 'Parameter.Value' --output text 2>/dev/null); then
    fail "could not read $CONFIG_SSM — create it before the first deploy"
fi
if [ -z "$conf_body" ] || [ "$conf_body" = "None" ]; then
    fail "$CONFIG_SSM is empty"
fi
# Write as ec2-user-owned, mode 0600. No echo of contents.
umask 077
printf '%s\n' "$conf_body" > "$CONFIG_DST"
chown ec2-user:ec2-user "$CONFIG_DST"
chmod 600 "$CONFIG_DST"
log "wrote config.yaml from $CONFIG_SSM ($(wc -c < "$CONFIG_DST") bytes)"

# 3. systemd unit
if [ -f "$UNIT_SRC" ]; then
    cp "$UNIT_SRC" "$UNIT_DST"
    systemctl daemon-reload
    systemctl enable nousergon-console.service >/dev/null 2>&1 || true
    log "installed/refreshed nousergon-console.service"
fi

# 4. restart + health
systemctl restart nousergon-console.service || fail "systemctl restart failed"
log "restarted nousergon-console.service"
wait_for_health "$HEALTH_URL" "nousergon-console" || fail "health check failed"

log "=== nousergon-console deploy OK sha=$TARGET_SHA ==="
