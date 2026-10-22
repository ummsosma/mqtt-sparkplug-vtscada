#!/usr/bin/env python3
"""Decode everything on spBv1.0/# so you can see what the bridge is sending.

Useful when a tag is not updating: it distinguishes "we never published" from
"we published and VTScada ignored it". Self-decoding only proves the payload is
well formed -- it does NOT prove the host accepted it. For that, read the
historian back with tools/verify_history.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import paho.mqtt.client as mqtt  # noqa: E402

from pipeline import settings  # noqa: E402
from pipeline.sparkplug import spb  # noqa: E402

NAMES = {1: "Int8", 2: "Int16", 3: "Int32", 4: "Int64", 5: "UInt8", 6: "UInt16",
         7: "UInt32", 8: "UInt64", 9: "Float", 10: "Double", 11: "Boolean", 12: "String"}


def describe(payload: bytes, topic: str) -> str:
    pb = spb.Payload()
    try:
        pb.ParseFromString(payload)
    except Exception as e:  # noqa: BLE001
        return f"{topic}  <not a Sparkplug payload: {e}>"
    lines = [f"{topic}  seq={pb.seq} ts={pb.timestamp} metrics={len(pb.metrics)}"]
    counts: dict[str, int] = {}
    for m in pb.metrics:
        counts[m.name] = counts.get(m.name, 0) + 1
    if len(counts) < len(pb.metrics):
        lines.append(f"  repeated metric names (this is the history-array form): "
                     f"{ {k: v for k, v in counts.items() if v > 1} }")
    for m in pb.metrics[:6]:
        if m.datatype == settings.DT_DOUBLE:
            value = m.double_value
        elif m.datatype == settings.DT_BOOL:
            value = m.boolean_value
        else:
            value = m.long_value
        lines.append(f"  {m.name} alias={m.alias} type={NAMES.get(m.datatype, m.datatype)} "
                     f"ts={m.timestamp} value={value}")
    if len(pb.metrics) > 6:
        lines.append(f"  ... {len(pb.metrics) - 6} more")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", default="spBv1.0/#")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = forever")
    a = ap.parse_args(argv)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"{settings.MQTT_CLIENT_PREFIX}-monitor",
                         protocol=mqtt.MQTTv311)
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    if settings.MQTT_TLS:
        client.tls_set()

    def on_connect(cl, ud, flags, rc, props=None):
        cl.subscribe(a.topic, qos=1)
        print(f"[monitor] subscribed to {a.topic} on "
              f"{settings.MQTT_HOST}:{settings.MQTT_PORT}")

    def on_message(cl, ud, msg):
        print(describe(msg.payload, msg.topic), flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=20)
    client.loop_start()
    started = time.time()
    try:
        while a.duration <= 0 or time.time() - started < a.duration:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
