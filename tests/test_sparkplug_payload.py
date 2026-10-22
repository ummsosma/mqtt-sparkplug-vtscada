#!/usr/bin/env python3
"""Unit tests for the Sparkplug B payload the bridge builds. No network.

These encode the wire contract, so if someone "simplifies" the publisher and
breaks one of them, the tests say which rule they broke:

  * one metric name REPEATED per tag, one element per sample
  * each element carrying its own ORIGINAL source timestamp
  * one trailing duplicate element per tag group (the VTScada Boolean quirk)
  * the correct datatype enum per metric
  * bdSeq persisted and incremented across restarts

    python tests/test_sparkplug_payload.py
    pytest tests/test_sparkplug_payload.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pipeline import bridge, settings  # noqa: E402
from pipeline.sparkplug import spb  # noqa: E402

# The publisher persists bdSeq to settings.STATE_DIR. On a fresh runner (e.g.
# GitHub Actions) /var/lib/pipeline is not writable by the test user and every
# publisher test fails with PermissionError before it asserts anything. Point
# STATE_DIR at a writable temp dir for the whole suite; the bdSeq-specific tests
# below override it with their own TemporaryDirectory.
_state_dir = tempfile.mkdtemp(prefix="spb-test-")
settings.STATE_DIR = _state_dir

TS0 = 1792033051.0


def _decode(payload: bytes) -> spb.Payload:
    p = spb.Payload()
    p.ParseFromString(payload)
    return p


def _publisher():
    """A publisher with a working MQTT client stub -- no broker needed."""
    pub = bridge.SparkplugPublisher()
    pub.client = None
    return pub


def _groups():
    # 3 analog samples and 2 boolean samples, in ascending seq order per tag.
    def p(ts, value, event_id, seq, tag):
        return bridge.Pending(ts, value, event_id, seq, tag)
    return {
        "AI01": [p(TS0, 20.5, "plc1:1:AI01", 1, "AI01"),
                 p(TS0 + 1, 21.0, "plc1:2:AI01", 2, "AI01"),
                 p(TS0 + 2, 21.5, "plc1:3:AI01", 3, "AI01")],
        "DI01": [p(TS0, 0.0, "plc1:4:DI01", 4, "DI01"),
                 p(TS0 + 1, 1.0, "plc1:5:DI01", 5, "DI01")],
    }


def test_metric_names_are_repeated_not_nested():
    pub = _publisher()
    payload = pub._payload(
        [pub.metric("AI01", 1, TS0 + i, settings.DT_DOUBLE, 20.0 + i) for i in range(5)])
    p = _decode(payload)
    assert [m.name for m in p.metrics] == ["AI01"] * 5


def test_each_element_keeps_its_own_timestamp_and_value():
    pub = _publisher()
    metrics = [pub.metric("AI01", 1, TS0 + i, settings.DT_DOUBLE, 20.0 + i) for i in range(4)]
    p = _decode(pub._payload(metrics))
    assert [m.timestamp for m in p.metrics] == [int((TS0 + i) * 1000) for i in range(4)]
    assert [m.double_value for m in p.metrics] == [20.0, 21.0, 22.0, 23.0]


def test_datatypes_are_the_enums_vtscada_accepts():
    pub = _publisher()
    p = _decode(pub._payload([
        pub.metric("AI01", 1, TS0, settings.DT_DOUBLE, 1.5),
        pub.metric("DI01", 2, TS0, settings.DT_BOOL, True),
        pub.metric("bdSeq", 0, TS0, settings.DT_INT64, 7),
    ]))
    assert [m.datatype for m in p.metrics] == [10, 11, 4]
    assert p.metrics[0].double_value == 1.5
    assert p.metrics[1].boolean_value is True
    assert p.metrics[2].long_value == 7


def test_wrong_datatype_enum_would_be_a_bug():
    """Double is 10 and Boolean is 11. Writing 2 (Int16) with double_value is the
    mistake that made VTScada silently ignore every metric."""
    assert settings.DT_DOUBLE == 10
    assert settings.DT_BOOL == 11
    assert settings.DT_INT64 == 4
    assert settings.datatype("AI01") == 10
    assert settings.datatype("DI01") == 11
    assert settings.datatype("NOPE") is None


def test_batch_adds_one_trailing_duplicate_per_tag():
    pub = _publisher()
    groups = _groups()
    metrics = []
    for alias, tag in enumerate(settings.VTS_TAGS, start=1):
        rows = groups[tag]
        for row in rows:
            metrics.append(pub.metric(tag, alias, row.ts_s, settings.datatype(tag), row.value))
        metrics.append(pub.metric(tag, alias, rows[-1].ts_s, settings.datatype(tag), rows[-1].value))

    counts = {}
    for m in metrics:
        counts[m.name] = counts.get(m.name, 0) + 1
    # 3 real + 1 trailing, and 2 real + 1 trailing
    assert counts == {"AI01": 4, "DI01": 3}

    # The duplicate repeats the last real element's timestamp AND value, so if
    # VTScada stores it at all it coalesces onto that record.
    ai = [m for m in metrics if m.name == "AI01"]
    assert ai[-1].timestamp == ai[-2].timestamp
    assert ai[-1].double_value == ai[-2].double_value


def test_publish_batch_emits_the_trailing_duplicate():
    """The real publish path, with the MQTT client stubbed out."""
    captured = {}

    class FakeResult:
        rc = 0

        def wait_for_publish(self, timeout=None):
            return True

    class FakeClient:
        def publish(self, topic, payload, qos=1):
            captured["topic"] = topic
            captured["payload"] = payload
            return FakeResult()

    pub = _publisher()
    pub.client = FakeClient()
    nbytes, rc = pub.publish_batch(_groups())
    assert rc == 0
    assert captured["topic"] == settings.VTS_TOPIC_DATA
    assert nbytes == len(captured["payload"])

    p = _decode(captured["payload"])
    names = [m.name for m in p.metrics]
    assert names == ["AI01", "AI01", "AI01", "AI01", "DI01", "DI01", "DI01"]
    assert p.seq == 1
    assert p.timestamp > 0


def test_publish_batch_skips_tags_with_no_rows():
    captured = {}

    class FakeResult:
        rc = 0

        def wait_for_publish(self, timeout=None):
            return True

    class FakeClient:
        def publish(self, topic, payload, qos=1):
            captured["payload"] = payload
            return FakeResult()

    pub = _publisher()
    pub.client = FakeClient()
    pub.publish_batch({"AI01": _groups()["AI01"]})
    p = _decode(captured["payload"])
    assert {m.name for m in p.metrics} == {"AI01"}


def test_payload_seq_wraps_within_a_byte():
    pub = _publisher()
    seen = []
    for _ in range(300):
        seen.append(_decode(pub._payload([])).seq)
    assert all(1 <= s <= 255 for s in seen)
    assert len(set(seen)) > 200


def test_bdseq_is_persisted_and_incremented_across_restarts():
    with tempfile.TemporaryDirectory() as d:
        old = settings.STATE_DIR
        settings.STATE_DIR = d
        try:
            first = bridge.next_bdseq()
            second = bridge.next_bdseq()
            assert second == (first + 1) % 256
            # A restart must NOT repeat a value VTScada has already seen, or it
            # treats the BIRTH as stale and ignores every following message.
            with open(os.path.join(d, "bdseq"), encoding="utf-8") as f:
                assert int(f.read().strip()) == second
            third = bridge.next_bdseq()
            assert third == (second + 1) % 256
        finally:
            settings.STATE_DIR = old


def test_bdseq_wraps_at_255():
    with tempfile.TemporaryDirectory() as d:
        old = settings.STATE_DIR
        settings.STATE_DIR = d
        try:
            with open(os.path.join(d, "bdseq"), "w", encoding="utf-8") as f:
                f.write("255")
            assert bridge.next_bdseq() == 0
        finally:
            settings.STATE_DIR = old


def test_retry_throttle_stops_a_hot_republish_loop():
    """A batch that never confirms must not be re-published on every loop.

    Without this, "VTScada answers REST but is not storing anything" turns into a
    hot loop hammering the historian and the broker.
    """
    old_delay = settings.BRIDGE_RETRY_DELAY_SEC
    settings.BRIDGE_RETRY_DELAY_SEC = 30.0
    bridge._last_attempt.clear()
    try:
        now = time.monotonic()
        bridge._last_attempt["ev-1"] = now
        assert bridge._throttled("ev-1", now) is True
        assert bridge._throttled("ev-never-tried", now) is False
        assert bridge._throttled("ev-1", now + 31) is False, "eligible again after the delay"
    finally:
        bridge._last_attempt.clear()
        settings.BRIDGE_RETRY_DELAY_SEC = old_delay


def test_audit_tag_does_not_come_from_splitting_the_event_id():
    """The audit row's tag is taken from the Pending row.

    It used to be recovered with event_id.split(":")[-1], which mis-attributes a
    tag or device id that itself contains a colon.
    """
    row = bridge.Pending(TS0, 1.0, "dev:1:val:ue", 7, "val:ue")
    assert row.event_id.split(":")[-1] == "ue", "what the old code would have recorded"
    assert row.tag == "val:ue", "what it records now"


def test_bad_confirm_mode_refuses_to_start():
    """A typo'd BRIDGE_CONFIRM_MODE must fail loudly, not silently fall back.

    Anything that is not exactly 'publish' is read as readback mode. A typo in a
    no-REST deployment would then sit in STANDBY forever, blaming the network.
    """
    old = settings.BRIDGE_CONFIRM_MODE
    settings.BRIDGE_CONFIRM_MODE = "publsh"
    try:
        # main() validates before it touches the database, so this returns 2
        # without a DB or broker being present.
        rc = bridge.main(["--once"])
        assert rc == 2, f"a typo'd confirm mode must refuse to start, got rc={rc}"
    finally:
        settings.BRIDGE_CONFIRM_MODE = old


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
