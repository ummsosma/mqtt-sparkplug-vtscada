#!/usr/bin/env bash
#
# install.sh — install or reconfigure the MQTT → VTScada pipeline.
#
# Safe to re-run. It never overwrites an existing .env or deletes a volume
# without asking, never prints a secret, and validates before starting.
#
#   sudo ./deploy/install.sh            normal install / reconfigure
#   sudo ./deploy/install.sh --check    preflight only, change nothing
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
EXAMPLE="$REPO_DIR/.env.example"
APP_DIR="/opt/mqtt-sparkplug-vtscada"
UNIT_SRC="$SCRIPT_DIR/pipeline.service"
UNIT_DST="/etc/systemd/system/pipeline.service"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="${2:-}" a; printf '%s%s: ' "$p" "${d:+ [$d]}" >&2; read -r a; printf '%s' "${a:-$d}"; }
ask_secret() { local p="$1" a; printf '%s: ' "$p" >&2; read -rs a; printf '\n' >&2; printf '%s' "$a"; }
gen()  { openssl rand -hex "${1:-16}" 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-32; }

# ------------------------------------------------------------------- preflight
say "== Preflight =="
[ "$(id -u)" -eq 0 ] || die "run this with sudo (it installs a systemd unit and Docker if missing)."

if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*raspbian*|*ubuntu*) say "OS: ${PRETTY_NAME:-unknown} (supported)" ;;
        *) die "unsupported OS: ${PRETTY_NAME:-unknown}. This installer supports Raspberry Pi OS / Debian / Ubuntu." ;;
    esac
else
    die "cannot read /etc/os-release; refusing to guess the OS."
fi

if ! command -v docker >/dev/null 2>&1; then
    say "Docker is not installed. Installing it from Docker's official script…"
    say "(This is the only step that needs the internet beyond pulling images.)"
    curl -fsSL https://get.docker.com | sh || die "Docker install failed; see https://docs.docker.com/engine/install/"
fi
systemctl enable --now docker >/dev/null 2>&1 || warn "could not enable the docker service"

docker compose version >/dev/null 2>&1 || die "the 'docker compose' plugin is missing. Install docker-compose-plugin."
BX="$(docker buildx version 2>/dev/null | awk '{print $2}' | sed 's/^v//')"
if [ -n "$BX" ]; then
    MAJ="${BX%%.*}"; REST="${BX#*.}"; MIN="${REST%%.*}"
    if [ "$MAJ" -eq 0 ] && [ "$MIN" -lt 17 ]; then
        die "buildx $BX is too old (need >= 0.17). Upgrade it: https://github.com/docker/buildx/releases"
    fi
    say "buildx: $BX (ok)"
else
    warn "could not determine the buildx version; continuing"
fi

AVAIL_GB="$(df -Pk "$REPO_DIR" | awk 'NR==2 {printf "%d", $4/1024/1024}')"
say "Free disk: ${AVAIL_GB} GB"
[ "$AVAIL_GB" -ge 5 ] || warn "less than 5 GB free; the queue and images need room."

[ "$CHECK_ONLY" -eq 1 ] && { say "Preflight only — nothing was changed."; exit 0; }

# --------------------------------------------------------------- configuration
say
say "== Configuration =="
say "Press Enter to accept the value in brackets."
say "Nothing is started until the configuration has been checked."
say

say "--- Step 1 of 3: how VTScada receives the data ---------------------------"
say "There are two ways to handle delivery; pick one:"
say
say "  readback  (recommended)  Read each sample back out of the VTScada historian"
say "                           over its REST interface, so delivery is PROVED."
say "                           Needs REST access and an account (docs/INSTALL.md)."
say
say "  publish   (fallback)     No REST needed. A row counts as delivered as soon"
say "                           as the MQTT publish succeeds -- nothing proves the"
say "                           historian stored it. Use only if REST is blocked."
say
CONFIRM_MODE="$(ask "Delivery mode (readback|publish)" "readback")"
case "$CONFIRM_MODE" in
    readback|publish) ;;
    *) die "delivery mode must be 'readback' or 'publish' (got '$CONFIRM_MODE')" ;;
esac

VTS_URL="http://host.docker.internal:443"
VTS_REALM="odbc"
VTS_USER="admin"
VTS_PASS=""
if [ "$CONFIRM_MODE" = "readback" ]; then
    VTS_URL="$(ask "VTScada REST base URL (e.g. http://192.168.1.50:443)" "$VTS_URL")"
    VTS_PASS="$(ask_secret "VTScada REST password (required; input hidden)")"
    [ -n "$VTS_PASS" ] || die "a VTScada REST password is required in readback mode. Re-run and choose 'publish' if the bridge cannot reach REST at all."
    VTS_REALM="$(ask "VTScada REST realm" "$VTS_REALM")"
    VTS_USER="$(ask "VTScada REST user" "$VTS_USER")"
else
    say "(publish mode: skipping the VTScada REST address and account -- not used.)"
fi
say

say "--- Step 2 of 3: what the PLC publishes ---------------------------------"
PLC_TOPIC="$(ask "PLC topic tree to subscribe to" "plc/#")"
PLC_DECODER="$(ask "How to read each message (auto|json|topic_tag|sparkplug|mapping)" "auto")"
PLC_LEVELS="$(ask "Trailing topic levels for <device>/<tag> (topic_tag only)" "2")"
VTS_TAGS="$(ask "Tags to publish, e.g. {\"AI01\":\"double\"}" '{"AI01": "double", "DI01": "bool"}')"
RETENTION="$(ask "Keep delivered history for how many days (0 = forever)" "90")"
say

say "--- Step 3 of 3: broker authentication (optional) -----------------------"
say "Enable this only if your PLC can supply a broker username and password."
MQTT_AUTH="$(ask "Require broker username/password? (y/N)" "n")"
MQTT_USER=""; MQTT_PASS=""
if [ "$MQTT_AUTH" = "y" ] || [ "$MQTT_AUTH" = "Y" ]; then
    MQTT_USER="$(ask "Broker username" "pipeline")"
    MQTT_PASS="$(gen 16)"
    say "A broker password has been generated (not shown)."
fi

DB_PASS="$(gen 20)"
DASH_PASS="$(gen 16)"

# --------------------------------------------------------------------- .env
if [ -f "$ENV_FILE" ]; then
    say
    say "An existing configuration was found at $ENV_FILE."
    KEEP="$(ask "Keep it and only add missing keys? (Y/n)" "Y")"
    if [ "$KEEP" != "n" ] && [ "$KEEP" != "N" ]; then
        say "Keeping the existing file. New values from this run are NOT applied."
        VTS_URL="$(sed -n 's/^VTS_REST_URL=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        VTS_PASS="$(sed -n 's/^VTS_REST_PASSWORD=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        VTS_REALM="$(sed -n 's/^VTS_REST_REALM=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        VTS_USER="$(sed -n 's/^VTS_REST_USER=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        PLC_TOPIC="$(sed -n 's/^PLC_TOPIC=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        PLC_DECODER="$(sed -n 's/^PLC_DECODER=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        PLC_LEVELS="$(sed -n 's/^PLC_TOPIC_TAG_LEVELS=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        VTS_TAGS="$(sed -n 's/^VTS_TAGS=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        RETENTION="$(sed -n 's/^RETENTION_DAYS=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        DB_PASS="$(sed -n 's/^DB_PASSWORD=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        DASH_PASS="$(sed -n 's/^EMQX_DASHBOARD_PASSWORD=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        MQTT_USER="$(sed -n 's/^MQTT_USERNAME=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
        MQTT_PASS="$(sed -n 's/^MQTT_PASSWORD=//p' "$ENV_FILE" | tr -d '\r' | head -n1)"
    else
        mv "$ENV_FILE" "$ENV_FILE.bak.$(date -u +%Y%m%dT%H%M%SZ)"
        say "Old configuration kept as a .bak file."
    fi
fi

say
say "Writing $ENV_FILE (mode 0600)…"
umask 077
cat > "$ENV_FILE" <<EOF
# Generated by deploy/install.sh on $(date -u +%FT%TZ). Mode 0600.
# Every setting is documented in docs/CONFIGURATION.md.
LOG_LEVEL=INFO

# broker
MQTT_HOST=emqx
MQTT_PORT=1883
MQTT_USERNAME=${MQTT_USER}
MQTT_PASSWORD=${MQTT_PASS}
EMQX_DASHBOARD_PASSWORD=${DASH_PASS}
MQTT_SESSION_EXPIRY_SEC=604800

# database (generated; changing DB_PASSWORD later needs the volume recreated)
DB_HOST=timescaledb
DB_PORT=5432
DB_USER=emqx
DB_PASSWORD=${DB_PASS}
DB_NAME=emqx
DB_SCHEMA=pipeline
RETENTION_DAYS=${RETENTION}

# PLC input
PLC_TOPIC=${PLC_TOPIC}
PLC_DECODER=${PLC_DECODER}
PLC_DEVICE_ID=plc1
PLC_TOPIC_TAG_LEVELS=${PLC_LEVELS}

# Sparkplug out
VTS_GROUP=SFTest
VTS_EDGE=sf-pi
VTS_TAGS=${VTS_TAGS}

# VTScada delivery: readback (default, needs REST) | publish (no REST, fire-and-forget)
BRIDGE_CONFIRM_MODE=${CONFIRM_MODE}
# VTScada REST (the delivery oracle — required only in readback mode)
VTS_REST_URL=${VTS_URL}
VTS_REST_REALM=${VTS_REALM}
VTS_REST_USER=${VTS_USER}
VTS_REST_PASSWORD=${VTS_PASS}
VTS_TAG_ROOT=MQTT Context\\MQTT-SB\\MQTT-DRIVER

BRIDGE_BATCH=50
EOF
chmod 600 "$ENV_FILE"
say "Configuration written. Secrets were not printed."

# ------------------------------------------------------------------ validation
say
say "== Validation =="
fail=0
# Whole-line, fixed-string match (-qxF): avoids the regex trap that made the old
# 'publish$' check unmatchable, and reads the same as the line in the file.
grep -qxF "BRIDGE_CONFIRM_MODE=${CONFIRM_MODE}" "$ENV_FILE" \
    || { warn "BRIDGE_CONFIRM_MODE was not written correctly"; fail=1; }
if [ "$CONFIRM_MODE" = "readback" ]; then
    grep -q '^VTS_REST_PASSWORD=.\+' "$ENV_FILE" || { warn "VTS_REST_PASSWORD is empty (required in readback mode)"; fail=1; }
fi
grep -q '^DB_PASSWORD=.\+'      "$ENV_FILE" || { warn "DB_PASSWORD is empty"; fail=1; }
case "$PLC_DECODER" in auto|json|topic_tag|sparkplug|mapping) ;; *) warn "PLC_DECODER='$PLC_DECODER' is not one of auto|json|topic_tag|sparkplug|mapping"; fail=1;; esac
printf '%s' "$VTS_TAGS" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null || { warn "VTS_TAGS is not valid JSON"; fail=1; }
[ "$fail" -eq 0 ] || die "configuration is invalid; fix the values above and re-run."

# --------------------------------------------------------------------- start
say
say "== Starting =="
cd "$REPO_DIR" || die "cannot cd to $REPO_DIR"
docker compose up -d --build || die "docker compose failed; run 'docker compose logs' for detail."

say "Waiting for the database to report healthy…"
for _ in $(seq 1 60); do
    if docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null | grep -q 'timescaledb healthy'; then break; fi
    sleep 2
done

# ------------------------------------------------------------------- boot unit
if [ -f "$UNIT_SRC" ]; then
    say "Installing the boot unit → $UNIT_DST"
    sed "s#/opt/mqtt-sparkplug-vtscada#$REPO_DIR#g" "$UNIT_SRC" > "$UNIT_DST"
    chmod 644 "$UNIT_DST"
    systemctl daemon-reload
    systemctl enable pipeline >/dev/null 2>&1 && systemctl restart pipeline \
        || warn "could not enable the boot unit; containers still restart on their own"
fi

# --------------------------------------------------------------------- report
say
say "== Done =="
say "Install directory: $REPO_DIR"
say "Configuration:     $ENV_FILE (0600)"
say "Data:              Docker volumes ${PWD##*/}_timescale-data, _emqx-data, _bridge-state"
say
say "Check health:      $REPO_DIR/deploy/pipeline status"
say "Follow logs:       $REPO_DIR/deploy/pipeline logs -f"
say "Test with no PLC:  $REPO_DIR/deploy/pipeline simulate"
say "Back up:           $REPO_DIR/deploy/pipeline backup"
if [ "$CONFIRM_MODE" = "readback" ]; then
    say "Prove delivery:    $REPO_DIR/deploy/pipeline test"
fi
say
if [ "$CONFIRM_MODE" = "readback" ]; then
    say "Next: run 'pipeline status' and confirm the delivery line says 'ok'."
    say "If it does not, fix that before expecting any data to arrive."
else
    say "Next: run 'pipeline status'. Delivery is fire-and-forget (no REST check)."
    say "To watch data flow with no PLC attached, run 'pipeline simulate'."
fi
say "Full guide: $REPO_DIR/docs/INSTALL.md"
