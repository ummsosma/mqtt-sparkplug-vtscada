"""Single source of truth for configuration.

Every setting is an environment variable with a safe default, so the same image
runs unchanged in docker-compose, in CI, and against a real broker -- override
values instead of editing code.

No secrets live in this file. Credentials belong in `.env` (see `.env.example`).
"""
from __future__ import annotations

import json
import os

from . import __version__


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return default if v is None or v == "" else int(v)


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return default if v is None or v == "" else float(v)


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _json(name: str, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return json.loads(v)


def load_dotenv(path: str | None = None) -> str | None:
    """Load a `.env` file into the environment, without overriding real env vars.

    For running the services straight from a checkout. In Docker the values come
    from compose, so this finds nothing and does nothing. Existing environment
    variables always win, so `VAR=x python -m pipeline.bridge` still overrides.
    """
    candidates = [path] if path else [
        os.path.join(os.getcwd(), ".env"),
        # src/pipeline/settings.py -> repo root
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), ".env"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
            return candidate
    return None


load_dotenv()



# --------------------------------------------------------------------- general
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()
STATE_DIR = _str("STATE_DIR", "/var/lib/pipeline")   # bdSeq persistence (bridge)

# ---------------------------------------------------------------------- broker
MQTT_HOST = _str("MQTT_HOST", "localhost")
MQTT_PORT = _int("MQTT_PORT", 1883)
MQTT_USERNAME = _str("MQTT_USERNAME")
MQTT_PASSWORD = _str("MQTT_PASSWORD")
MQTT_TLS = _bool("MQTT_TLS", False)
# How long the broker keeps this client's session (and therefore any messages it
# has not acknowledged yet) after the client disconnects.
#
# IMPORTANT: for an MQTT v5 client this is the value in the CONNECT packet, and it
# is what actually applies. The broker's own `mqtt.session_expiry_interval`
# setting does NOT override it -- that setting is for 3.1/3.1.1 clients, which
# have no way to ask for one. Setting this too low is a silent data-loss bug: the
# broker has already acknowledged the PLC, so the PLC will not resend, and once
# the session expires the queued messages are discarded.
MQTT_SESSION_EXPIRY_SEC = _int("MQTT_SESSION_EXPIRY_SEC", 604800)   # 7 days
# How long a service keeps retrying its first connection to the broker before
# giving up and letting Docker restart it. A broker coming up from cold can take
# a minute; exiting immediately produces a crash-loop of tracebacks that reads
# like a broken deployment.
MQTT_CONNECT_WINDOW_SEC = _float("MQTT_CONNECT_WINDOW_SEC", 300.0)
# Prefix for every client id we open, so our connections are identifiable on the
# broker and two deployments do not fight over one session.
MQTT_CLIENT_PREFIX = _str("MQTT_CLIENT_PREFIX", "spb-vts")

# ------------------------------------------------------- PLC input (collector)
# The topic tree the PLC publishes on. `plc/#` matches everything below `plc`.
PLC_TOPIC = _str("PLC_TOPIC", "plc/#")
# How to read a sample out of a message: auto | json | topic_tag | sparkplug.
# See decoder.py -- this is the one thing to change for a real CLICK PLUS.
PLC_DECODER = _str("PLC_DECODER", "auto")
# Device id to use when the payload does not carry one.
PLC_DEVICE_ID = _str("PLC_DEVICE_ID", "plc1")
# Dotted field paths for the json decoder, e.g. PLC_JSON_VALUE_FIELD=payload.value
PLC_JSON_TAG_FIELD = _str("PLC_JSON_TAG_FIELD", "tag")
PLC_JSON_VALUE_FIELD = _str("PLC_JSON_VALUE_FIELD", "value")
PLC_JSON_TS_FIELD = _str("PLC_JSON_TS_FIELD", "ts")
PLC_JSON_SEQ_FIELD = _str("PLC_JSON_SEQ_FIELD", "seq")
PLC_JSON_DEVICE_FIELD = _str("PLC_JSON_DEVICE_FIELD", "device")
# A declarative payload-mapping file (JSON) so a new PLC message shape can be
# understood WITHOUT editing Python. Point PLC_DECODER=mapping at it:
#     PLC_DECODER=mapping
#     PLC_MAPPING_FILE=/etc/pipeline/plc.json
# See docs/CONFIGURATION.md "Mapping a new payload shape" for the schema.
PLC_MAPPING_FILE = _str("PLC_MAPPING_FILE")
# topic_tag decoder: how many trailing topic levels are <device>/<tag> (2 means
# `plc/<device>/<tag>`; 1 means only the tag is in the topic).
PLC_TOPIC_TAG_LEVELS = _int("PLC_TOPIC_TAG_LEVELS", 2)
# Map a topic to a tag name, for a PLC whose topic carries no usable name.
# The CLICK PLUS here publishes a bare number to `plc/1` -- the topic is the only
# clue to what that number is, so the meaning has to be stated:
#     PLC_TAG_MAP={"plc/1": "DS1"}
# The name used here must also appear in VTS_TAGS, which decides its datatype and
# whether it is published to VTScada at all.
PLC_TAG_MAP = _json("PLC_TAG_MAP", {})

# Timestamp sanity window: anything outside it is quarantined, never delivered.
TS_MIN_YEAR = _int("TS_MIN_YEAR", 2020)
TS_MAX_FUTURE_SECONDS = _int("TS_MAX_FUTURE_SECONDS", 300)

# ---------------------------------------------------------------- durable queue
DB_HOST = _str("DB_HOST", "localhost")
DB_PORT = _int("DB_PORT", 5432)
DB_USER = _str("DB_USER", "pipeline")
DB_PASSWORD = _str("DB_PASSWORD")
DB_NAME = _str("DB_NAME", "pipeline")
DB_SCHEMA = _str("DB_SCHEMA", "pipeline")
DB_STATEMENT_TIMEOUT_MS = _int("DB_STATEMENT_TIMEOUT_MS", 15000)
# Retention for DELIVERED rows and the audit tables, in days. Pending rows are
# never pruned -- they are the work still to do. 0 disables pruning (keep forever),
# which is what a deployment with a small, known data volume may want.
RETENTION_DAYS = _int("RETENTION_DAYS", 90)
# The collector's main loop period, and how often it logs its counters.
COLLECTOR_TICK_SEC = _float("COLLECTOR_TICK_SEC", 1.0)

# ------------------------------------------------------- Sparkplug / VTScada
BRIDGE_ENABLED = _bool("BRIDGE_ENABLED", True)
# How the bridge proves delivery:
#   readback  (default) VTScada REST is required: rows are marked delivered only
#              after their (timestamp,value) is read back out of the historian,
#              and the bridge holds in STANDBY while REST is unreachable.
#   publish   NO REST needed: rows are marked delivered as soon as the MQTT
#              publish returns success. Use this only when VTScada has no REST
#              access from the bridge (fire-and-forget loses the delivery proof,
#              and there is no STANDBY/backlog-on-outage protection).
BRIDGE_CONFIRM_MODE = _str("BRIDGE_CONFIRM_MODE", "readback").lower()
# Sparkplug group id and edge node id. VTScada auto-creates a tag per metric on
# the topic spBv1.0/<GROUP>/NDATA/<EDGE>, so these two values decide the topic
# AND the tag path -- they must match the driver's tag addresses.
VTS_GROUP = _str("VTS_GROUP", "SFTest")
VTS_EDGE = _str("VTS_EDGE", "sf-pi")
# tag -> sparkplug datatype ("double" | "bool" | "int64"). Only these tags are
# published; a tag present in the queue but absent here is skipped.
VTS_TAGS = _json("VTS_TAGS", {"AI01": "double", "DI01": "bool"})
# VTScada REST interface (the delivery oracle). Reachable on the VTScada host.
VTS_REST_URL = _str("VTS_REST_URL", "http://localhost:443")
VTS_REST_REALM = _str("VTS_REST_REALM", "odbc")
VTS_REST_USER = _str("VTS_REST_USER", "admin")
VTS_REST_PASSWORD = _str("VTS_REST_PASSWORD")
VTS_REST_TIMEOUT_SEC = _float("VTS_REST_TIMEOUT_SEC", 20.0)
# The tag path prefix used by the History table's column names:
# '<VTS_TAG_ROOT>\<GROUP>\<EDGE>\<TAG>:Value'
VTS_TAG_ROOT = _str("VTS_TAG_ROOT", r"MQTT Context\MQTT-SB\MQTT-DRIVER")

# Delivery tuning.
BRIDGE_BATCH = _int("BRIDGE_BATCH", 50)              # samples per NDATA payload
BRIDGE_SETTLE_SEC = _float("BRIDGE_SETTLE_SEC", 3.0)  # readback polling window
BRIDGE_BIRTH_WAIT_SEC = _float("BRIDGE_BIRTH_WAIT_SEC", 8.0)
# Publish a distinctive probe value (4242) after the BIRTH and wait for it to
# appear in the historian before delivering. OFF by default: the probe writes a
# real sample into the tag's history on every bridge start, which shows up as a
# spike in trend data. It is not needed for correctness -- a batch published
# inside the post-BIRTH warm-up window simply fails to confirm and is retried.
BRIDGE_WARMUP_PROBE = _bool("BRIDGE_WARMUP_PROBE", False)
BRIDGE_WARMUP_TIMEOUT_SEC = _float("BRIDGE_WARMUP_TIMEOUT_SEC", 45.0)
BRIDGE_POLL_SEC = _float("BRIDGE_POLL_SEC", 2.0)     # idle poll when no work
BRIDGE_RETRY_DELAY_SEC = _float("BRIDGE_RETRY_DELAY_SEC", 30.0)
BRIDGE_HEARTBEAT_SEC = _float("BRIDGE_HEARTBEAT_SEC", 30.0)
BRIDGE_STALL_ALARM_SEC = _float("BRIDGE_STALL_ALARM_SEC", 300.0)
# Match tolerances when reading a sample back out of the historian.
TS_TOL = _float("TS_TOL", 0.002)          # seconds
VALUE_TOL = _float("VALUE_TOL", 1e-9)

VTS_TOPIC_BIRTH = f"spBv1.0/{VTS_GROUP}/NBIRTH/{VTS_EDGE}"
VTS_TOPIC_DATA = f"spBv1.0/{VTS_GROUP}/NDATA/{VTS_EDGE}"
VTS_TOPIC_DEATH = f"spBv1.0/{VTS_GROUP}/NDEATH/{VTS_EDGE}"

# Sparkplug datatype enum values. These are NOT interchangeable: sending the
# right value field with the wrong enum makes VTScada silently ignore the
# metric -- no error appears anywhere.
DT_INT64 = 4
DT_DOUBLE = 10
DT_BOOL = 11

_DT_BY_NAME = {"double": DT_DOUBLE, "bool": DT_BOOL, "int64": DT_INT64}


def datatype(tag: str) -> int | None:
    """Sparkplug datatype for a configured tag, or None if it is not configured."""
    kind = VTS_TAGS.get(tag)
    return _DT_BY_NAME.get(kind) if kind else None


def describe() -> dict:
    """Everything needed to explain a running deployment, secrets excluded."""
    return {
        "version": __version__,
        "mqtt": f"{MQTT_HOST}:{MQTT_PORT}",
        "plc_topic": PLC_TOPIC,
        "plc_decoder": PLC_DECODER,
        "db": f"{DB_HOST}:{DB_PORT}/{DB_NAME} schema={DB_SCHEMA}",
        "sparkplug_topic": VTS_TOPIC_DATA,
        "tags": VTS_TAGS,
        "vts_rest": f"{VTS_REST_URL}/{VTS_REST_REALM}",
    }
