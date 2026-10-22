#!/usr/bin/env python3
"""Unit tests for the PLC payload decoder. No network, no database.

    python tests/test_decoder.py      # no pytest needed
    pytest tests/test_decoder.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pipeline import decoder, settings  # noqa: E402
from pipeline.decoder import Sample, Undecodable  # noqa: E402
from pipeline.sparkplug import spb  # noqa: E402

TS_MS = 1792033051030
TS_S = 1792033051


def test_epoch_ms_passthrough():
    assert decoder.to_epoch_ms(TS_MS) == TS_MS


def test_epoch_seconds_upgraded():
    assert decoder.to_epoch_ms(TS_S) == TS_S * 1000


def test_epoch_numeric_string():
    assert decoder.to_epoch_ms(str(TS_MS)) == TS_MS


def test_iso_timestamp():
    assert decoder.to_epoch_ms("2026-10-15T03:00:00Z") == 1792033200000


def test_iso_timestamp_naive_is_utc():
    a = decoder.to_epoch_ms("2026-10-15T03:00:00")
    b = decoder.to_epoch_ms("2026-10-15T03:00:00+00:00")
    assert a == b


def test_boolean_is_not_a_timestamp():
    try:
        decoder.to_epoch_ms(True)
    except Undecodable:
        return
    raise AssertionError("a boolean must not be accepted as a timestamp")


def test_value_coercion():
    assert decoder.to_value(21.5) == 21.5
    assert decoder.to_value("21.5") == 21.5
    assert decoder.to_value(True) is True
    assert decoder.to_value("true") is True
    assert decoder.to_value("OFF") is False
    assert decoder.to_value("0") is False


def test_value_rejects_text():
    try:
        decoder.to_value("running")
    except Undecodable:
        return
    raise AssertionError("non-numeric text must not silently become 0")


def test_dig_nested_and_list():
    obj = {"payload": {"readings": [{"v": 7}]}}
    assert decoder.dig(obj, "payload.readings.0.v") == 7
    assert decoder.dig(obj, "payload.missing") is None


def _json_message(tag="AI01", value=21.5, ts=TS_MS, seq=7, device="plc1"):
    return json.dumps({"device": device, "tag": tag, "value": value, "ts": ts, "seq": seq}).encode()


def test_json_decoder():
    settings.PLC_DECODER = "json"
    (s,) = decoder.decode("plc/plc1", _json_message())
    assert s.tag == "AI01"
    assert s.value == 21.5
    assert s.device_id == "plc1"
    assert s.seq == 7
    assert s.source_ts_ms == TS_MS
    assert s.event_id == "plc1:7:AI01"


def test_json_decoder_nested_fields():
    old = (settings.PLC_JSON_VALUE_FIELD, settings.PLC_JSON_TS_FIELD, settings.PLC_JSON_TAG_FIELD)
    settings.PLC_JSON_VALUE_FIELD = "payload.value"
    settings.PLC_JSON_TS_FIELD = "payload.ts"
    settings.PLC_JSON_TAG_FIELD = "meta.tag"
    try:
        body = json.dumps({"meta": {"tag": "AI01"},
                           "payload": {"value": 3.5, "ts": TS_MS}}).encode()
        (s,) = decoder.decode("plc/plc1", body)
        assert (s.tag, s.value, s.source_ts_ms) == ("AI01", 3.5, TS_MS)
    finally:
        (settings.PLC_JSON_VALUE_FIELD, settings.PLC_JSON_TS_FIELD,
         settings.PLC_JSON_TAG_FIELD) = old


def test_json_decoder_without_seq_derives_it_from_the_timestamp():
    settings.PLC_DECODER = "json"
    body = json.dumps({"tag": "AI01", "value": 1.0, "ts": TS_MS}).encode()
    (s,) = decoder.decode("plc/plc1", body)
    assert s.seq == TS_MS, "a replayed message must deduplicate, so seq must be derived"
    (again,) = decoder.decode("plc/plc1", body)
    assert again.event_id == s.event_id


def test_json_decoder_missing_field_is_undecodable():
    settings.PLC_DECODER = "json"
    try:
        decoder.decode("plc/plc1", json.dumps({"tag": "AI01"}).encode())
    except Undecodable:
        return
    raise AssertionError("a payload with no timestamp must be rejected")


def test_topic_tag_decoder_bare_value():
    settings.PLC_DECODER = "topic_tag"
    (s,) = decoder.decode("plc/line1/AI01", b"42.5")
    assert (s.tag, s.device_id, s.value) == ("AI01", "line1", 42.5)
    assert s.seq == s.source_ts_ms


def test_topic_tag_decoder_json_object():
    settings.PLC_DECODER = "topic_tag"
    (s,) = decoder.decode("plc/line1/DI01", json.dumps({"value": True, "ts": TS_MS}).encode())
    assert (s.tag, s.value, s.source_ts_ms) == ("DI01", True, TS_MS)


def test_sparkplug_decoder_reads_every_metric():
    settings.PLC_DECODER = "sparkplug"
    p = spb.Payload()
    p.timestamp = TS_MS
    for i in range(3):
        m = p.metrics.add()
        m.name = "AI01"
        m.alias = 1
        m.timestamp = TS_MS + i
        m.datatype = settings.DT_DOUBLE
        m.double_value = 100.0 + i
    m = p.metrics.add()
    m.name = "DI01"
    m.alias = 2
    m.timestamp = TS_MS
    m.datatype = settings.DT_BOOL
    m.boolean_value = True

    samples = decoder.decode("spBv1.0/SFTest/NDATA/edge9", p.SerializeToString())
    assert len(samples) == 4
    assert [s.value for s in samples[:3]] == [100.0, 101.0, 102.0]
    assert samples[3].value is True
    assert all(s.device_id == "edge9" for s in samples)


def test_auto_routes_by_topic_and_body():
    settings.PLC_DECODER = "auto"
    (by_topic,) = decoder.decode("plc/line1/AI01", b"7")
    assert by_topic.tag == "AI01"
    (by_body,) = decoder.decode("plc/plc1", _json_message(tag="DI01", value=False))
    assert by_body.tag == "DI01"


def test_valid_timestamp_rejects_1970_and_the_future():
    ok, why = decoder.valid_timestamp(1000)
    assert not ok and "before" in why
    future = int((datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(days=1)).timestamp() * 1000)
    ok, why = decoder.valid_timestamp(future)
    assert not ok and "future" in why
    ok, _ = decoder.valid_timestamp(TS_MS)
    assert ok


# --------------------------------------------------------------- the real PLC
# The CLICK PLUS publishes a bare number to `plc/1`, with no tag name in the
# topic or the payload and no timestamp. These are the tests that matter for it.

def test_click_plus_shape_is_readable():
    """`plc/1` with a bare numeric body, tag level 1, device from config."""
    old_levels, old_dev = settings.PLC_TOPIC_TAG_LEVELS, settings.PLC_DEVICE_ID
    settings.PLC_DECODER = "auto"
    settings.PLC_TOPIC_TAG_LEVELS = 1
    settings.PLC_DEVICE_ID = "click1"
    try:
        (s,) = decoder.decode("plc/1", b"-16165")
        assert s.value == -16165.0
        assert s.tag == "1", "the topic's last level is the only name available"
        assert s.device_id == "click1"
        # No timestamp in the payload, so arrival time is used and seq is derived
        # from it -- which is what makes a replayed message deduplicate.
        assert s.source_ts_ms > 1_700_000_000_000
        assert s.seq == s.source_ts_ms
    finally:
        settings.PLC_TOPIC_TAG_LEVELS, settings.PLC_DEVICE_ID = old_levels, old_dev


def test_tag_map_gives_the_bare_value_a_meaning():
    old_map, old_levels = settings.PLC_TAG_MAP, settings.PLC_TOPIC_TAG_LEVELS
    settings.PLC_DECODER = "auto"
    settings.PLC_TOPIC_TAG_LEVELS = 1
    settings.PLC_TAG_MAP = {"plc/1": "DS1"}
    try:
        (s,) = decoder.decode("plc/1", b"-16165")
        assert s.tag == "DS1"
        assert s.value == -16165.0
        assert s.event_id.endswith(":DS1")
    finally:
        settings.PLC_TAG_MAP, settings.PLC_TOPIC_TAG_LEVELS = old_map, old_levels


def test_tag_map_leaves_other_topics_alone():
    old_map = settings.PLC_TAG_MAP
    settings.PLC_TAG_MAP = {"plc/1": "DS1"}
    settings.PLC_DECODER = "json"
    try:
        (s,) = decoder.decode("plc/plc1", _json_message(tag="AI01"))
        assert s.tag == "AI01", "an unmapped topic keeps the name it arrived with"
    finally:
        settings.PLC_TAG_MAP = old_map


def test_non_json_payload_is_accepted_by_topic_tag():
    """`ON`, `DS1:V=21` and friends are not JSON. They must still decode, and the
    raw text must be what gets stored -- a jsonb column would reject them."""
    settings.PLC_DECODER = "topic_tag"
    settings.PLC_TOPIC_TAG_LEVELS = 2
    for body, expected in ((b"ON", True), (b"OFF", False), (b"21.5", 21.5)):
        (s,) = decoder.decode("plc/line1/DI01", body)
        assert s.value == expected
        assert s.payload == body.decode()


def test_sample_event_id_is_stable():
    a = Sample(device_id="d", tag="AI01", value=1.0, source_ts_ms=TS_MS, seq=5)
    b = Sample(device_id="d", tag="AI01", value=99.0, source_ts_ms=TS_MS, seq=5)
    assert a.event_id == b.event_id


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failures.append((name, e))
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
