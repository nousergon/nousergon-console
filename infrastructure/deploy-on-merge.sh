#!/bin/bash
# deploy-on-merge.sh — install/refresh nousergon-console on the dashboard box.
# Invoked via SSM (as root) from .github/workflows/deploy.yml AFTER the caller
# has already pulled the repo to the target SHA.
#
# Steps: venv + editable install with [aws,calendar] extras · resolve the private
# config.yaml (SSM pointer -> S3 body) · install/refresh the systemd unit ·
# restart · health-check :5180.
#
# Usage: bash infrastructure/deploy-on-merge.sh <target-sha>

set -uo pipefail

REPO_DIR="/home/ec2-user/nousergon-console"
LOG="/var/log/nousergon-console-deploy.log"
TARGET_SHA="${1:-HEAD}"
UNIT_SRC="$REPO_DIR/infrastructure/nousergon-console.service"
UNIT_DST="/etc/systemd/system/nousergon-console.service"
# Private config — Parameter Store path, never a repo path.
#
# The parameter used to hold the whole config.yaml body. It now holds a POINTER
# (`config_source`, `config_sha256`, `config_chars`, `generated_utc`) naming an
# S3 object that holds the body — alpha-engine-config-I9802: the SSM Advanced
# tier caps a value at 8,192 characters and the assembled body reached 8,129
# after ONE adapter fragment, so the console's "onboarding a module is writing
# one file" contract had become bounded by its transport.
#
# BOTH SHAPES ARE ACCEPTED, deliberately. The writer lives in another repo
# (nous-ergon-ops/scripts/console_config.py) and neither end may assume the
# other has landed; a parameter with no `config_source:` line is used as the
# config body, exactly as before.
#
# The digest is NOT optional once a pointer is present. A body that does not
# hash to what the pointer declares means the two halves drifted apart, and
# deploying either one deploys a config nobody wrote — the console would come up
# healthy on the wrong adapters, which is the one failure this file can cause
# that nothing downstream can see.
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
"$VENV_PY" -m pip install -q -e "$REPO_DIR[aws,calendar]" || fail "pip install failed"
log "installed package with [aws,calendar] extras"

# 2. private config (never from the repo): SSM pointer -> S3 body, or a whole
#    body still in the parameter.
if ! conf_body=$(aws ssm get-parameter --name "$CONFIG_SSM" --with-decryption \
        --query 'Parameter.Value' --output text 2>/dev/null); then
    fail "could not read $CONFIG_SSM — create it before the first deploy"
fi
if [ -z "$conf_body" ] || [ "$conf_body" = "None" ]; then
    fail "$CONFIG_SSM is empty"
fi

# Written as ec2-user-owned, mode 0600. No echo of contents, either shape.
umask 077
config_source=$(printf '%s\n' "$conf_body" | sed -n 's/^config_source:[[:space:]]*//p' | head -1)
if [ -n "$config_source" ]; then
    config_sha=$(printf '%s\n' "$conf_body" | sed -n 's/^config_sha256:[[:space:]]*//p' | head -1)
    if [ -z "$config_sha" ]; then
        fail "$CONFIG_SSM names a config_source with no config_sha256 — an unverifiable pointer is a config nobody can vouch for"
    fi
    rest=${config_source#s3://}
    src_bucket=${rest%%/*}
    src_key=${rest#*/}
    if [ -z "$src_bucket" ] || [ "$src_key" = "$rest" ] || [ -z "$src_key" ]; then
        fail "$CONFIG_SSM names an unparseable config_source"
    fi
    if ! aws s3api get-object --bucket "$src_bucket" --key "$src_key" "$CONFIG_DST" \
            >/dev/null 2>&1; then
        fail "could not read the config body at $config_source named by $CONFIG_SSM"
    fi
    got_sha=$(sha256sum "$CONFIG_DST" | cut -d' ' -f1)
    if [ "$got_sha" != "$config_sha" ]; then
        rm -f "$CONFIG_DST"
        fail "$config_source does not match the digest $CONFIG_SSM declares — the pointer and the body have drifted apart; re-run console_config.py apply. Nothing was installed."
    fi
    log "wrote config.yaml from $config_source via $CONFIG_SSM (digest verified)"
else
    printf '%s\n' "$conf_body" > "$CONFIG_DST"
    log "wrote config.yaml from $CONFIG_SSM (inline body — no config_source declared)"
fi
chown ec2-user:ec2-user "$CONFIG_DST"
chmod 600 "$CONFIG_DST"
log "config.yaml is $(wc -c < "$CONFIG_DST") bytes"

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
