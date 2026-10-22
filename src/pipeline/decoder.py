"""Turn a raw PLC message into normalized samples.

A CLICK PLUS can be made to publish in more than one shape, and which shape it
uses is a property of *your* PLC program, not of this pipeline. So decoding is
explicit and configurable rather than guessed:

    PLC_DECODER=auto        pick from the topic (sparkplug) then try json, then topic_tag
    PLC_DECODER=json        JSON body, field paths from PLC_JSON_*_FIELD
    PLC_DECODER=topic_tag   topic ends in <device>/<tag>, body is a bare value
    PLC_DECODER=sparkplug   body is an already-encoded Sparkplug B payload

This is the one module to change when you point the pipeline at a real PLC.

Sequencing
----------
The durable queue deduplicates on (device_id, seq, tag). A PLC that does not
send a sequence number gets one derived from its own timestamp, which makes a
replayed message deduplicate instead of inserting a second row -- the correct
semantic for a device that only reports (tag, value, timestamp).
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, replace

from . import settings
from .sparkplug import spb

log = logging.getLogger("decoder")

# Epoch seconds above this are read as milliseconds. 1e11 seconds is the year
# 5138, so no plausible seconds value is mistaken for milliseconds.
_MS_THRESHOLD = 1e11

_TRUE = {"true", "1", "on", "yes", "t"}
_FALSE = {"false", "0", "off", "no", "f"}


class Undecodable(Exception):
    """The message cannot be turned into a sample. The collector quarantines it."""


@dataclass(frozen=True)
class Sample:
    device_id: str
    tag: str
    value: float | bool
    source_ts_ms: int
    seq: int
    topic: str = ""
    payload: str = ""

    @property
    def event_id(self) -> str:
        """Stable identity, so a replayed message is recognized as the same event."""
        return f"{self.device_id}:{self.seq}:{self.tag}"


# --------------------------------------------------------------------- helpers
def dig(obj, path: str, default=None):
    """Read a dotted path out of nested dicts/lists: 'payload.value', 'm.0.v'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def to_epoch_ms(raw) -> int:
    """Accept epoch ms, epoch seconds, or an ISO 8601 string. Return epoch ms."""
    if isinstance(raw, bool):
        raise Undecodable("boolean is not a timestamp")
    if isinstance(raw, (int, float)):
        v = float(raw)
        return int(v if abs(v) >= _MS_THRESHOLD else v * 1000)
    if isinstance(raw, str):
        s = raw.strip()
        try:
            v = float(s)
        except ValueError:
            pass
        else:
            return int(v if abs(v) >= _MS_THRESHOLD else v * 1000)
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise Undecodable(f"unparseable timestamp {raw!r}") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    raise Undecodable(f"unsupported timestamp type {type(raw).__name__}")


def to_value(raw):
    """Coerce to float or bool. Anything else is undecodable, not silently zero."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        try:
            return float(raw)
        except ValueError as e:
            raise Undecodable(f"value {raw!r} is neither numeric nor boolean") from e
    raise Undecodable(f"unsupported value type {type(raw).__name__}")


# -------------------------------------------------------------------- decoders
def _decode_json(topic: str, body: dict) -> list[Sample]:
    """One JSON object per message, with configurable field paths."""
    tag = dig(body, settings.PLC_JSON_TAG_FIELD)
    if tag is None:
        raise Undecodable(f"no field {settings.PLC_JSON_TAG_FIELD!r} in payload")
    raw_ts = dig(body, settings.PLC_JSON_TS_FIELD)
    if raw_ts is None:
        raise Undecodable(f"no field {settings.PLC_JSON_TS_FIELD!r} in payload")
    device = dig(body, settings.PLC_JSON_DEVICE_FIELD) or settings.PLC_DEVICE_ID
    ts_ms = to_epoch_ms(raw_ts)
    raw_seq = dig(body, settings.PLC_JSON_SEQ_FIELD)
    seq = int(raw_seq) if isinstance(raw_seq, (int, float)) and not isinstance(raw_seq, bool) else ts_ms
    return [Sample(
        device_id=str(device),
        tag=str(tag),
        value=to_value(dig(body, settings.PLC_JSON_VALUE_FIELD)),
        source_ts_ms=ts_ms,
        seq=seq,
        topic=topic,
        payload=json.dumps(body, default=str),
    )]


def _decode_topic_tag(topic: str, payload: bytes) -> list[Sample]:
    """`plc/<device>/<tag>` with a bare value (or a JSON scalar/object) as body.

    Covers the common CLICK PLUS / generic-gateway shape where the tag name is
    carried by the topic and the payload is just the value.
    """
    parts = [p for p in topic.split("/") if p]
    if not parts:
        raise Undecodable(f"topic {topic!r} has no levels")
    n = max(1, settings.PLC_TOPIC_TAG_LEVELS)
    tag = parts[-1]
    # With PLC_TOPIC_TAG_LEVELS=2 the last two levels are <device>/<tag>; with 1
    # only the tag is in the topic and the device comes from configuration.
    device = parts[-n] if n >= 2 and len(parts) >= n else settings.PLC_DEVICE_ID

    text = payload.decode("utf-8", errors="replace").strip()
    ts_ms = None
    raw = text
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        raw = obj.get("value", obj.get("v"))
        if raw is None:
            raise Undecodable("payload object has no 'value' field")
        for key in ("ts", "timestamp", "source_ts_ms", "time"):
            if key in obj:
                ts_ms = to_epoch_ms(obj[key])
                break
        device = str(obj.get("device", obj.get("device_id", device)))
    elif isinstance(obj, (int, float, bool)):
        raw = obj

    if ts_ms is None:
        # No timestamp in the message: use arrival time. Documented in README --
        # a PLC that cannot stamp its data loses the "original timestamp" property.
        ts_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return [Sample(
        device_id=str(device),
        tag=str(tag),
        value=to_value(raw),
        source_ts_ms=ts_ms,
        seq=ts_ms,
        topic=topic,
        payload=text,
    )]


def _decode_sparkplug(topic: str, payload: bytes) -> list[Sample]:
    """Already-encoded Sparkplug B NDATA/DDATA. One sample per metric."""
    try:
        pb = spb.Payload()
        pb.ParseFromString(payload)
    except Exception as e:  # noqa: BLE001 - any protobuf failure is a decode failure
        raise Undecodable(f"not a Sparkplug payload: {e}") from e
    parts = topic.split("/")
    if len(parts) < 4:
        raise Undecodable(f"topic {topic!r} is not spBv1.0/<group>/<type>/<edge>")
    device = parts[3] if len(parts) == 4 else parts[4]
    out = []
    for m in pb.metrics:
        if m.name in ("bdSeq", ""):
            continue
        if not m.HasField("timestamp") and not pb.HasField("timestamp"):
            continue
        ts_ms = m.timestamp if m.HasField("timestamp") else pb.timestamp
        try:
            value = to_value(m.double_value) if m.datatype == settings.DT_DOUBLE else (
                m.boolean_value if m.datatype == settings.DT_BOOL else float(m.long_value))
        except (ValueError, TypeError):
            continue
        out.append(Sample(
            device_id=device,
            tag=m.name,
            value=value,
            source_ts_ms=int(ts_ms),
            seq=int(ts_ms),
            topic=topic,
            payload=payload.decode("utf-8", errors="replace"),
        ))
    if not out:
        raise Undecodable("Sparkplug payload carried no usable metrics")
    return out


def _apply_tag_map(samples: list[Sample]) -> list[Sample]:
    """Rename samples whose topic has an entry in PLC_TAG_MAP.

    A CLICK PLUS publishing a bare value to `plc/1` carries no tag name in either
    the topic or the payload, so what the number *means* has to be configured.
    Doing the rename here, on the way in, keeps the name consistent everywhere
    downstream: the queue, the Sparkplug metric name, and the tag VTScada
    auto-creates from it.
    """
    if not settings.PLC_TAG_MAP:
        return samples
    out = []
    for s in samples:
        name = settings.PLC_TAG_MAP.get(s.topic)
        if name:
            log.debug("tag map: %s -> %s", s.topic, name)
            out.append(replace(s, tag=name))
        else:
            out.append(s)
    return out


def decode(topic: str, payload: bytes) -> list[Sample]:
    """Decode one message. Raises `Undecodable` if it cannot be read."""
    mode = settings.PLC_DECODER.lower()
    text = payload.decode("utf-8", errors="replace").strip()

    def as_json():
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise Undecodable(f"body is not JSON: {e}") from e
        if not isinstance(obj, dict):
            raise Undecodable("JSON body is not an object")
        return obj

    if mode == "json":
        samples = _decode_json(topic, as_json())
    elif mode == "topic_tag":
        samples = _decode_topic_tag(topic, payload)
    elif mode == "sparkplug":
        samples = _decode_sparkplug(topic, payload)
    elif mode == "mapping":
        # Declarative new-shape support: a JSON file, no Python. The mapping is
        # loaded once and cached; a malformed file raises on the first message,
        # which the collector treats as quarantinable, then logs the MappingError
        # loudly (see collector).
        from .mapping import MappingError, get_mapping
        try:
            mapping_obj = get_mapping()
        except MappingError as e:
            log.error("ALARM payload mapping not usable: %s -- every PLC message "
                      "will be quarantined until this is fixed", e)
            raise Undecodable(str(e)) from e
        samples = mapping_obj.decode(topic, payload)
    # auto: route on the topic first, then on the body.
    elif topic.startswith("spBv1.0/"):
        samples = _decode_sparkplug(topic, payload)
    elif text.startswith("{"):
        samples = _decode_json(topic, as_json())
    else:
        samples = _decode_topic_tag(topic, payload)

    return _apply_tag_map(samples)


def valid_timestamp(ts_ms: int) -> tuple[bool, str]:
    """Guard against a PLC with an unset clock: implausible times are quarantined."""
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc)
    if dt.year < settings.TS_MIN_YEAR:
        return False, f"timestamp {dt.isoformat()} is before {settings.TS_MIN_YEAR}"
    horizon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=settings.TS_MAX_FUTURE_SECONDS)
    if dt > horizon:
        return False, f"timestamp {dt.isoformat()} is in the future"
    return True, ""
