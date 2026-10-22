"""MQTT in -> durable queue.  This is the store half of store-and-forward.

The only rule that matters here:

    the PUBACK is withheld until the row is committed to the database.

A message acknowledged on arrival lives only in this process's memory, so killing
the process loses it silently. Acknowledging after the commit means the broker
re-delivers anything that was not durably stored.

The collector knows nothing about VTScada and never writes `delivery_state`. The
bridge owns that transition and nothing else does, so "why is this row delivered?"
always has one answer.
"""
from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.properties import PacketTypes, Properties

from . import db, decoder, settings

log = logging.getLogger("collector")

COUNTERS = {
    "received": 0,
    "persisted": 0,
    "duplicate_input": 0,
    "quarantined": 0,
    "persist_failed": 0,
    "acknowledged": 0,
    "pending_now": 0,
}

_work: "queue.Queue[tuple[str, bytes, int]]" = queue.Queue(maxsize=10000)
_stop = threading.Event()
_conn = None
_conn_lock = threading.Lock()


def _db():
    """One reused connection. DB work never runs on the MQTT network thread."""
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            try:
                _conn = db.connect()
            except Exception as e:  # noqa: BLE001
                log.error("ALARM database unavailable: %s", e)
                return None
        return _conn


def persist(sample: decoder.Sample) -> int:
    """Insert one sample. Returns the pending backlog, or -1 on failure.

    `-1` means "do not acknowledge": the broker will retransmit.
    """
    conn = _db()
    if conn is None:
        COUNTERS["persist_failed"] += 1
        return -1
    ok, reason = decoder.valid_timestamp(sample.source_ts_ms)
    if not ok:
        db.quarantine(sample.event_id, reason, sample.topic, sample.payload)
        COUNTERS["quarantined"] += 1
        return -2  # acknowledged: re-delivering it would never succeed
    try:
        with _conn_lock, conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {db.table('samples')} "
                "(event_id, device_id, tag, seq, value, source_ts, topic, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (device_id, seq, tag) DO NOTHING",
                (sample.event_id, sample.device_id, sample.tag, sample.seq,
                 # Bools are stored as 0.0/1.0; the tag's type comes from
                 # VTS_TAGS, so nothing is lost and the column stays numeric.
                 float(sample.value), db.utc_from_ms(sample.source_ts_ms),
                 sample.topic, sample.payload or ""),
            )
            inserted = cur.rowcount
            if inserted == 0:
                COUNTERS["duplicate_input"] += 1
                log.debug("duplicate input ignored event=%s", sample.event_id)
            else:
                COUNTERS["persisted"] += 1
            cur.execute(
                f"SELECT count(*) FROM {db.table('samples')} WHERE delivery_state='pending'")
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        COUNTERS["persist_failed"] += 1
        log.error("ALARM persist failed event=%s: %s", sample.event_id, e)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        with _conn_lock:
            global _conn
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None
        return -1


def _backlog() -> int:
    """Pending backlog, reusing the shared connection.

    Opening a fresh connection every tick would mean a TCP connect and an auth
    round-trip per second for no reason.
    """
    conn = _db()
    if conn is None:
        return -1
    with _conn_lock:
        return db.backlog(conn)


def handle(topic: str, payload: bytes, mid: int, client: mqtt.Client) -> None:
    try:
        samples = decoder.decode(topic, payload)
    except decoder.Undecodable as e:
        db.quarantine(None, str(e), topic, payload.decode("utf-8", errors="replace"))
        COUNTERS["quarantined"] += 1
        client.ack(mid, 1)  # no amount of redelivery will make this decodable
        return

    acknowledge = True
    backlog = None
    for s in samples:
        backlog = persist(s)
        if backlog == -1:
            acknowledge = False
    if acknowledge:
        client.ack(mid, 1)
        COUNTERS["acknowledged"] += 1
    else:
        log.warning("not acknowledging mid=%s -- broker will redeliver", mid)


def worker(client: mqtt.Client) -> None:
    while not _stop.is_set():
        try:
            topic, payload, mid = _work.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            handle(topic, payload, mid, client)
        except Exception as e:  # noqa: BLE001 - a bad message must not kill the worker
            log.exception("sample processing failed: %s", e)
        finally:
            _work.task_done()


def build_client() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{settings.MQTT_CLIENT_PREFIX}-collector",
        protocol=mqtt.MQTTv5,
    )
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    if settings.MQTT_TLS:
        client.tls_set()

    def on_connect(cl, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("connect refused: %s", reason_code)
            return
        log.info("connected to %s:%s, subscribing to %s",
                 settings.MQTT_HOST, settings.MQTT_PORT, settings.PLC_TOPIC)
        cl.subscribe(settings.PLC_TOPIC, qos=1)

    def on_disconnect(cl, userdata, flags, reason_code, properties=None):
        if not _stop.is_set():
            log.warning("disconnected (%s), reconnecting", reason_code)

    def on_message(cl, userdata, msg):
        # Never do DB work on the network thread: a slow query would delay ack
        # processing and make the broker retransmit messages we already have.
        COUNTERS["received"] += 1
        try:
            _work.put_nowait((msg.topic, msg.payload, msg.mid))
        except queue.Full:
            log.error("ALARM work queue full -- dropping message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def connect_with_retry(client: mqtt.Client, props) -> None:
    """Connect, retrying while the broker finishes booting.

    Exiting immediately and letting Docker restart the container also "works",
    but a cold broker takes tens of seconds and the result is a crash-loop of
    tracebacks with a rising restart count -- which reads like a broken
    deployment. Retrying here keeps the log honest about what is happening.
    """
    deadline = time.time() + settings.MQTT_CONNECT_WINDOW_SEC
    attempt = 0
    while not _stop.is_set():
        attempt += 1
        try:
            client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=10,
                           clean_start=0, properties=props)
            return
        except Exception as e:  # noqa: BLE001 - any failure means "not ready yet"
            remaining = deadline - time.time()
            if remaining <= 0:
                log.error("ALARM could not reach the broker at %s:%s within %.0fs: %s",
                          settings.MQTT_HOST, settings.MQTT_PORT,
                          settings.MQTT_CONNECT_WINDOW_SEC, e)
                raise
            delay = min(5.0, max(1.0, remaining))
            log.warning("broker %s:%s not ready (attempt %d): %s -- retrying in %.0fs",
                        settings.MQTT_HOST, settings.MQTT_PORT, attempt, e, delay)
            time.sleep(delay)


def main() -> int:
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("collector starting: %s", settings.describe())
    db.apply_schema()

    client = build_client()
    props = Properties(PacketTypes.CONNECT)
    # What the broker holds for us while we are disconnected. This is the v5
    # CONNECT value, so it -- not the broker's own session_expiry_interval
    # setting -- is what applies; see settings.py.
    props.SessionExpiryInterval = settings.MQTT_SESSION_EXPIRY_SEC
    # Set manual ack BEFORE the loop starts. Paho's network thread begins
    # delivering as soon as loop_start() returns, and a message arriving in that
    # window would be auto-acknowledged -- i.e. acknowledged before it is stored.
    client.manual_ack_set(True)
    connect_with_retry(client, props)
    client.loop_start()

    threading.Thread(target=worker, args=(client,), daemon=True, name="persist").start()

    def shutdown(*_):
        log.info("shutting down")
        _stop.set()
        client.loop_stop()
        client.disconnect()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    last_log = 0.0
    while not _stop.is_set():
        try:
            COUNTERS["pending_now"] = _backlog()
        except Exception as e:  # noqa: BLE001
            log.error("ALARM backlog query failed: %s", e)
            COUNTERS["pending_now"] = -1
        if time.time() - last_log >= 30:
            last_log = time.time()
            log.info("counters=%s", COUNTERS)
        time.sleep(settings.COLLECTOR_TICK_SEC)

    with _conn_lock:
        if _conn is not None and not _conn.closed:
            _conn.close()
    log.info("stopped: %s", COUNTERS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
