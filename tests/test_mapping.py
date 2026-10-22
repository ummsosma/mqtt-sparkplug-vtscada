#!/usr/bin/env python3
"""Unit tests for the declarative payload-mapping decoder. No network, no DB.

    python tests/test_mapping.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pipeline.mapping as mapping_mod  # noqa: E402
from pipeline import decoder, settings  # noqa: E402
from pipeline.decoder import Undecodable  # noqa: E402
from pipeline.mapping import MappingError, PayloadMapping, Transform  # noqa: E402

TS_MS = 1792033051030

_BASE = {
    "version": 1,
    "decoder": "json",
    "tag_field": "tag",
    "value_field": "value",
    "ts_field": "ts",
    "seq_field": "seq",
    "device_field": "device",
}


def _write(cfg) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def _msg(tag="AI01", value=21.5, ts=TS_MS, seq=7):
    return json.dumps({"tag": tag, "value": value, "ts": ts, "seq": seq}).encode()


def _set_mapping(path):
    """Point the settings at a mapping file and clear the cached instance."""
    old_file, old_mode = settings.PLC_MAPPING_FILE, settings.PLC_DECODER
    old_cache = mapping_mod._mapping_cache
    mapping_mod._mapping_cache = None
    settings.PLC_MAPPING_FILE = path
    settings.PLC_DECODER = "mapping"
    return old_file, old_mode, old_cache


def _restore(path, old):
    settings.PLC_MAPPING_FILE, settings.PLC_DECODER = old[0], old[1]
    mapping_mod._mapping_cache = old[2]
    os.unlink(path)


def test_scale_and_offset_transform():
    t = Transform(scale=0.1, offset=-5.0)
    assert t.apply(100.0) == 5.0
    assert isinstance(t.apply(100.0), float)


def test_map_transform_string_to_bool():
    t = Transform(mapping={"ON": True, "OFF": False})
    assert t.apply("ON") is True
    assert t.apply("OFF") is False


def test_map_transform_rejects_unknown():
    t = Transform(mapping={"1": True, "0": False})
    try:
        t.apply("7")
    except Undecodable:
        return
    raise AssertionError("a value missing from the map must be rejected, not guessed")


def test_invert_transform():
    assert Transform(invert=True).apply(True) is False
    assert Transform(invert=True).apply(False) is True


def test_full_mapping_end_to_end():
    path = _write({
        **_BASE,
        "tags": {
            "AI01": {"to": "TankLevel", "transform": {"scale": 0.1}},
            "DI01": {"to": "PumpRunning",
                     "transform": {"map": {"1": True, "0": False}}},
        },
    })
    old = _set_mapping(path)
    try:
        (a,) = decoder.decode("plc/plc1", _msg(tag="AI01", value=100.0))
        assert (a.tag, a.value) == ("TankLevel", 10.0)
        assert a.device_id == "plc1"
        (d,) = decoder.decode("plc/plc1", _msg(tag="DI01", value="1"))
        assert d.tag == "PumpRunning"
        assert d.value is True
    finally:
        _restore(path, old)


def test_unmapped_tag_passes_through():
    path = _write({**_BASE, "tags": {"AI01": {"to": "TankLevel"}}})
    old = _set_mapping(path)
    try:
        (s,) = decoder.decode("plc/plc1", _msg(tag="OTHER", value=3.0))
        assert s.tag == "OTHER"
        assert s.value == 3.0
    finally:
        _restore(path, old)


def test_nested_field_paths():
    path = _write({
        "_base_note": None,
        "decoder": "json",
        "tag_field": "meta.tag",
        "value_field": "payload.value",
        "ts_field": "payload.ts",
        "device_field": "device",
        "tags": {},
    })
    old = _set_mapping(path)
    try:
        body = json.dumps({"meta": {"tag": "AI01"},
                           "payload": {"value": 3.5, "ts": TS_MS},
                           "device": "plc9"}).encode()
        (s,) = decoder.decode("plc/plc1", body)
        assert (s.tag, s.value, s.device_id) == ("AI01", 3.5, "plc9")
    finally:
        _restore(path, old)


def test_topic_rule_renames_by_topic():
    path = _write({**_BASE, "topics": {"plc/1": {"tag": "DS1", "transform": {"scale": 2.0}}}})
    old = _set_mapping(path)
    try:
        (s,) = decoder.decode("plc/1", json.dumps(
            {"tag": "x", "value": 21.0, "ts": TS_MS}).encode())
        assert (s.tag, s.value) == ("DS1", 42.0)
    finally:
        _restore(path, old)


def test_bad_file_raises_mapping_error():
    path = _write(["not", "an", "object"])
    try:
        PayloadMapping(path)
    except MappingError:
        os.unlink(path)
        return
    os.unlink(path)
    raise AssertionError("a non-object mapping file must raise MappingError")


def test_missing_file_raises():
    try:
        PayloadMapping("/nonexistent/plc.json")
    except MappingError:
        return
    raise AssertionError("a missing mapping file must raise MappingError")


def test_missing_mapping_env_raises():
    old_file, old_mode = settings.PLC_MAPPING_FILE, settings.PLC_DECODER
    mapping_mod._mapping_cache = None
    settings.PLC_MAPPING_FILE = ""
    try:
        try:
            mapping_mod.get_mapping()
        except MappingError:
            return
        raise AssertionError("get_mapping() with no PLC_MAPPING_FILE must raise")
    finally:
        settings.PLC_MAPPING_FILE = old_file
        settings.PLC_DECODER = old_mode
        mapping_mod._mapping_cache = None


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
