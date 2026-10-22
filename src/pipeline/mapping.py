"""Declarative payload mapping for a new PLC shape, no Python required.

The problem this solves: pointing the pipeline at a new PLC message format used
to mean writing a new decoder function in Python. A mapping file turns that into
an edit of a JSON document:

    PLC_DECODER=mapping
    PLC_MAPPING_FILE=/etc/pipeline/plc.json

The mapping file describes, in plain terms:

  * which kind of message this is (json | topic_tag | sparkplug),
  * which JSON fields carry the tag / value / timestamp / sequence / device
    (dotted paths, e.g. "payload.value"),
  * a per-tag rename and optional value transform (scale / offset / map),
  * an optional topic -> tag map for messages whose topic carries no name.

A mapping only applies to tags it names, matching on the tag as it arrives
BEFORE any rename. Tags named in the file but never seen on the wire are simply
never produced -- they are not an error. Tags that arrive but are not in the
file pass through unchanged (with their original name, no transform).

Schema (JSON):

    {
      "version": 1,
      "decoder": "json",
      "tag_field": "tag",
      "value_field": "value",
      "ts_field": "ts",
      "seq_field": "seq",
      "device_field": "device",
      "topic_tag_levels": 2,
      "default_device_id": "plc1",
      "tags": {
        "<incoming tag>": {
          "to": "<name to publish as>",
          "scale": 0.1,
          "offset": -5,
          "map": {"1": true, "0": false, "ON": true, "OFF": false}
        }
      },
      "topics": {
        "<topic>": {"tag": "<name>", "scale": 1.0}
      }
    }

All keys are optional. Omitted field paths fall back to the PLC_JSON_*_FIELD
environment settings. A tag with no entry passes through unchanged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from . import settings
from .decoder import Sample, Undecodable, dig, to_value

log = logging.getLogger("mapping")


class MappingError(Exception):
    """The mapping file is malformed. Refuse to start rather than guess."""


def _map_candidates(raw):
    """Possible lookup keys for a raw value, most specific first.

    JSON map keys are always strings, so we normalise a mixed input to
    string/bool candidates the author is most likely to have written:
      * "1" / 1 / 1.0  -> "1"
      * "ON"           -> "ON"
      * True           -> True, "true"
    """
    if isinstance(raw, bool):
        return [raw, str(raw).lower()]
    if isinstance(raw, (int, float)):
        f = float(raw)
        if f == int(f):
            return [str(int(f)), str(raw)]
        return [str(raw), str(int(f))]
    return [str(raw)]


@dataclass(frozen=True)
class Transform:
    scale: float | None = None
    offset: float | None = None
    mapping: dict | None = None
    invert: bool = False

    def apply(self, raw):
        """Map / scale / offset / invert a RAW (not yet coerced) value to its target.

        A `map` runs on the raw value first (its keys are strings like "ON",
        "1"), then the result is coerced to a bool or float. Without a map the
        raw value is coerced with `to_value` and then scaled/offset/inverted.
        """
        if self.mapping is not None:
            for key in _map_candidates(raw):
                if key in self.mapping:
                    return to_value(self.mapping[key])
            raise Undecodable(f"value {raw!r} is not in the tag map")
        value = to_value(raw)
        if self.invert:
            value = to_value(not bool(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            if self.scale is not None:
                v = v * self.scale
            if self.offset is not None:
                v = v + self.offset
            return v
        return value


@dataclass(frozen=True)
class TagRule:
    to: str | None = None
    transform: Transform = Transform()

    @classmethod
    def from_dict(cls, d: dict) -> "TagRule":
        t = d.get("transform") or {}
        if isinstance(t, dict):
            tr = Transform(
                scale=t.get("scale"),
                offset=t.get("offset"),
                mapping=t.get("map"),
                invert=bool(t.get("invert")),
            )
        else:
            tr = Transform()
        return cls(to=d.get("to"), transform=tr)


class PayloadMapping:
    """Parsed mapping file. None-safe: used by decoder with PLC_DECODER=mapping."""

    def __init__(self, path: str | None = None):
        if not path:
            raise MappingError("PLC_MAPPING_FILE is required when PLC_DECODER=mapping")
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except OSError as e:
            raise MappingError(f"cannot read mapping file {path}: {e}") from e
        except ValueError as e:
            raise MappingError(f"mapping file {path} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise MappingError("mapping file must be a JSON object")

        self.decoder = str(data.get("decoder", "json"))
        self.tag_field = data.get("tag_field", settings.PLC_JSON_TAG_FIELD)
        self.value_field = data.get("value_field", settings.PLC_JSON_VALUE_FIELD)
        self.ts_field = data.get("ts_field", settings.PLC_JSON_TS_FIELD)
        self.seq_field = data.get("seq_field", settings.PLC_JSON_SEQ_FIELD)
        self.device_field = data.get("device_field", settings.PLC_JSON_DEVICE_FIELD)
        self.topic_tag_levels = int(data.get("topic_tag_levels", settings.PLC_TOPIC_TAG_LEVELS))
        self.default_device_id = str(data.get("default_device_id", settings.PLC_DEVICE_ID))

        self.tags: dict[str, TagRule] = {}
        raw_tags = data.get("tags") or {}
        if not isinstance(raw_tags, dict):
            raise MappingError("'tags' must be a JSON object of tag -> rule")
        for name, rule in raw_tags.items():
            self.tags[str(name)] = TagRule.from_dict(rule if isinstance(rule, dict) else {})

        self.topics: dict[str, dict] = {}
        raw_topics = data.get("topics") or {}
        if not isinstance(raw_topics, dict):
            raise MappingError("'topics' must be a JSON object of topic -> rule")
        for topic, rule in raw_topics.items():
            self.topics[str(topic)] = rule if isinstance(rule, dict) else {}

    # ------------------------------------------------------------ extraction
    def extract_json(self, body: dict) -> dict:
        """Pull tag/value/ts/seq/device out of a JSON object using field paths."""
        tag = dig(body, self.tag_field)
        if tag is None:
            raise Undecodable(f"no field {self.tag_field!r} in payload for a tag name")
        ts = dig(body, self.ts_field)
        if ts is None:
            raise Undecodable(f"no field {self.ts_field!r} in payload for a timestamp")
        device = dig(body, self.device_field) or self.default_device_id
        return {
            "tag": str(tag),
            "value": dig(body, self.value_field),
            "ts_raw": ts,
            "seq_raw": dig(body, self.seq_field),
            "device": str(device),
        }

    def apply(self, samples: list[Sample], topic: str) -> list[Sample]:
        """Rename and transform decoded samples per the mapping file.

        The sample's `.value` carries the RAW extracted value; a matching tag or
        topic rule resolves it to the final value (map / scale / offset / invert),
        and a sample with no rule is simply coerced (numeric -> float, text that
        reads as a bool -> bool). Always runs so raw values are coerced.
        """
        out = []
        for s in samples:
            out_tag = s.tag
            transform = Transform()
            # A topic rule can override the tag name and add a transform.
            topic_rule = self.topics.get(topic)
            if topic_rule:
                if topic_rule.get("tag"):
                    out_tag = str(topic_rule["tag"])
                sub = topic_rule.get("transform") or {}
                if isinstance(sub, dict):
                    transform = Transform(scale=sub.get("scale"),
                                          offset=sub.get("offset"),
                                          mapping=sub.get("map"),
                                          invert=bool(sub.get("invert")))
            # A tag rule (keyed on the incoming tag name) renames + transforms.
            rule = self.tags.get(s.tag)
            if rule:
                if rule.to:
                    out_tag = rule.to
                transform = rule.transform
            try:
                value = transform.apply(s.value)
            except Undecodable:
                log.warning("mapping: dropping %s (transform rejected %r on %s)",
                            s.event_id, s.value, topic)
                continue
            out.append(replace(s, tag=out_tag, value=value, device_id=s.device_id))
        return out

    def decode(self, topic: str, payload: bytes) -> list[Sample]:
        """Full decode for PLC_DECODER=mapping: extract, then rename/transform."""
        from .decoder import to_epoch_ms

        text = payload.decode("utf-8", errors="replace").strip()
        if self.decoder == "json":
            try:
                body = json.loads(text)
            except ValueError as e:
                raise Undecodable(f"body is not JSON: {e}") from e
            if not isinstance(body, dict):
                raise Undecodable("JSON body is not an object")
            e = self.extract_json(body)
            ts_ms = to_epoch_ms(e["ts_raw"])
            seq_raw = e["seq_raw"]
            seq = int(seq_raw) if isinstance(seq_raw, (int, float)) and not isinstance(seq_raw, bool) else ts_ms
            # Carry the RAW extracted value in the transient sample: the single
            # `apply` pass below resolves the transform (so a `map` such as
            # "1"->true sees the string before it is coerced to a bool).
            sample = Sample(device_id=e["device"], tag=e["tag"], value=e["value"],
                            source_ts_ms=ts_ms, seq=seq, topic=topic, payload=text)
            return self.apply([sample], topic)
        elif self.decoder == "topic_tag":
            raise Undecodable("mapping 'topic_tag' mode: use PLC_DECODER=topic_tag "
                              "plus PLC_TAG_MAP; a mapping file mainly adds transforms "
                              "on top of json")
        elif self.decoder == "sparkplug":
            raise Undecodable("mapping 'sparkplug' mode: use PLC_DECODER=sparkplug")
        raise MappingError(f"unsupported mapping decoder {self.decoder!r}")


# Cached instance: the file is read once at first use, not per message.
_mapping_cache: "PayloadMapping | None" = None


def get_mapping() -> "PayloadMapping":
    """Load (and cache) the configured mapping file. Raises MappingError on bad config."""
    global _mapping_cache
    if _mapping_cache is not None:
        return _mapping_cache
    if not settings.PLC_MAPPING_FILE:
        raise MappingError("PLC_MAPPING_FILE is not set (PLC_DECODER=mapping needs it)")
    log.info("loading payload mapping from %s", settings.PLC_MAPPING_FILE)
    _mapping_cache = PayloadMapping(settings.PLC_MAPPING_FILE)
    return _mapping_cache
