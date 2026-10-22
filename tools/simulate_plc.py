#!/usr/bin/env python3
"""A stand-in for a CLICK PLUS, so the pipeline can be exercised without hardware.

Two shapes, matching the two decoders you are most likely to need:

  --mode json        publish to `plc/<device>` a JSON object per tag:
                     {"device": "plc1", "tag": "AI01", "value": 21.5,
                      "ts": 1792033051030, "seq": 42}
  --mode topic_tag   publish to `plc/<device>/<tag>` a bare value: `21.5`

Use it to prove the whole chain end to end, and as the reference for what your
real PLC program should emit (or for what the decoder should be told to expect).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import paho.mqtt.client as mqtt  # noqa: E402

from pipeline import settings  # noqa: E402


def build_client(device: str) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{settings.MQTT_CLIENT_PREFIX}-sim-{device}",
        protocol=mqtt.MQTTv311,
    )
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    if settings.MQTT_TLS:
        client.tls_set()
    return client


def connect_with_retry(client: mqtt.Client, window: float = 60.0) -> None:
    """Connect, retrying while the broker finishes booting.

    Without this, `pipeline simulate` run right after `pipeline start` dies with
    ConnectionRefusedError and only comes back via Docker's restart policy --
    which reads like the simulator is broken.
    """
    deadline = time.time() + window
    attempt = 0
    while True:
        attempt += 1
        try:
            client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=20)
            return
        except Exception as e:  # noqa: BLE001 - any failure means "not ready yet"
            if time.time() >= deadline:
                raise
            delay = min(5.0, max(1.0, deadline - time.time()))
            print(f"[sim] broker {settings.MQTT_HOST}:{settings.MQTT_PORT} not ready "
                  f"(attempt {attempt}): {e} -- retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=settings.PLC_DEVICE_ID)
    ap.add_argument("--topic-prefix", default="plc",
                    help="root of the topic tree the collector subscribes to")
    ap.add_argument("--tags", default="AI01,DI01")
    ap.add_argument("--rate", type=float, default=1.0, help="samples per second per tag")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = forever")
    ap.add_argument("--mode", choices=["json", "topic_tag"], default="json")
    ap.add_argument("--seq-base", type=int, default=None,
                    help="start sequence above MAX(seq) in the queue when re-running. "
                         "Default: derived from the clock, so restarting the simulator "
                         "does not collide with rows already stored (which would make "
                         "every sample look like a duplicate input).")
    ap.add_argument("--noise", action="store_true", help="add jitter to analog values")
    ap.add_argument("--bad-timestamp-pct", type=float, default=0.0,
                    help="fraction of samples to send with an implausible timestamp, "
                         "to exercise the quarantine path")
    a = ap.parse_args(argv)

    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    client = build_client(a.device)
    connect_with_retry(client)
    client.loop_start()
    time.sleep(0.5)

    analog = {t: 20.0 for t in tags}
    # A clock-derived base means a restart continues above the rows already in
    # the queue instead of colliding with them (every sample would deduplicate).
    seq = a.seq_base if a.seq_base is not None else int(time.time())
    sent = 0
    started = time.time()
    period = 1.0 / max(a.rate, 0.001)
    print(f"[sim] {a.mode} -> {a.topic_prefix}/{a.device} tags={tags} "
          f"rate={a.rate}Hz duration={a.duration or 'forever'}s "
          f"broker={settings.MQTT_HOST}:{settings.MQTT_PORT}")

    try:
        while a.duration <= 0 or time.time() - started < a.duration:
            tick_start = time.time()
            for tag in tags:
                seq += 1
                is_bool = tag.upper().startswith("D")
                if is_bool:
                    value = bool(int(time.time() / 10) % 2)
                else:
                    analog[tag] += 0.5
                    value = round(analog[tag] + (random.uniform(-0.1, 0.1) if a.noise else 0), 3)
                ts_ms = int(time.time() * 1000)
                if a.bad_timestamp_pct and random.random() * 100 < a.bad_timestamp_pct:
                    ts_ms = 1000  # 1970: must be quarantined, never delivered

                if a.mode == "json":
                    topic = f"{a.topic_prefix}/{a.device}"
                    body = json.dumps({"device": a.device, "tag": tag, "value": value,
                                       "ts": ts_ms, "seq": seq})
                else:
                    topic = f"{a.topic_prefix}/{a.device}/{tag}"
                    body = str(value)
                client.publish(topic, body, qos=1)
                sent += 1
            if a.duration > 0 and time.time() - started >= a.duration:
                break
            time.sleep(max(0.0, period - (time.time() - tick_start)))
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    print(f"[sim] sent {sent} messages in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
