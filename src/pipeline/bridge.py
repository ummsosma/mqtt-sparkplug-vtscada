"""Durable queue -> Sparkplug B -> VTScada historian.  The forward half.

The contract this implements was established experimentally against a real
VTScada install; see docs/vtscada-wire-contract.md for the evidence behind each
rule. Short version:

  * Historical data is NOT a JSON blob with timestamps in it. It is one metric
    name REPEATED inside a single Sparkplug payload -- N metrics all named
    `AI01`, each carrying its own `timestamp` (ms UTC) and value. VTScada writes
    every element into its historian at its own timestamp, in one go.
  * The tag's History address must carry `$$History$$`, and the payload must go
    to the topic named in that address. A topic matching no tag is dropped
    without an error.
  * Datatype enums must be right. The correct value field with the wrong enum is
    ignored silently -- no log line anywhere.
  * A trailing Boolean `false` is dropped by VTScada, so one duplicate element is
    appended after the last real element of every tag group. It coalesces and
    never corrupts a real record.
  * A payload published immediately after the NBIRTH that creates the tags is
    dropped, so delivery starts only after a probe timestamp has been read back.

And the rule that shapes the whole loop:

    a row is marked delivered ONLY after its (timestamp, value) has been read
    back out of the historian. An MQTT publish returning rc=0 proves nothing.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import NamedTuple

import paho.mqtt.client as mqtt
import psycopg2

from . import db, settings
from .sparkplug import spb
from .vtclient import VTScadaClient, VTScadaError, VTScadaUnreachable

log = logging.getLogger("bridge")

COUNTERS = {
    "batches": 0, "published_events": 0, "confirmed": 0, "unmatched": 0,
    "standby_polls": 0, "verify_errors": 0, "births": 0, "retries": 0,
    "throttled": 0,
}
_stop = threading.Event()


class Pending(NamedTuple):
    """One queued reading waiting to be delivered.

    A named tuple rather than a plain tuple: with five fields, two of them floats,
    positional access is unreadable and easy to get wrong.
    """
    ts_s: float        # the source timestamp, epoch seconds
    value: float
    event_id: str
    seq: int
    tag: str


# event_id -> monotonic time of the last publish attempt. A row that keeps failing
# to confirm is retried at BRIDGE_RETRY_DELAY_SEC instead of being re-published on
# every loop iteration -- without this, "VTScada answers REST but is not storing
# anything" turns into a hot loop hammering both ends.
_last_attempt: dict[str, float] = {}


def _throttled(event_id: str, now: float) -> bool:
    last = _last_attempt.get(event_id)
    return last is not None and (now - last) < settings.BRIDGE_RETRY_DELAY_SEC


# ------------------------------------------------------------------- bdSeq state
def next_bdseq() -> int:
    """The BIRTH/DEATH sequence number, persisted across restarts.

    Hosts treat a BIRTH whose bdSeq is <= the last one seen as stale and ignore
    it -- after which every following DATA message is dropped for sequence
    mismatch. That failure looks exactly like "tags created but values never
    update", so the counter must survive a restart. Wraps at 255 per spec.
    """
    path = os.path.join(settings.STATE_DIR, "bdseq")
    current = None
    try:
        with open(path, encoding="utf-8") as f:
            current = int(f.read().strip())
    except (OSError, ValueError):
        current = None
    if current is None:
        # First run on a fresh volume: any value works, so start from the clock.
        current = int(time.time()) % 256
    nxt = (current + 1) % 256
    try:
        os.makedirs(settings.STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(nxt))
    except OSError as e:
        # Running without a persisted bdSeq is worse than not running: the next
        # restart repeats a value VTScada has already seen, it treats the BIRTH
        # as stale, and every following DATA message is ignored -- silently
        # (audit F9). Fail loudly instead so the operator fixes the volume.
        log.error("ALARM could not persist bdSeq to %s (%s) -- refusing to run: "
                  "a restart would reuse this value and VTScada would ignore "
                  "every BIRTH after it", path, e)
        raise
    return nxt


# --------------------------------------------------------------------- publisher
class SparkplugPublisher:
    """Publishes NBIRTH once, then one NDATA payload per batch of samples."""

    def __init__(self):
        self.client = None
        self.connected = False
        self.bdseq = next_bdseq()
        self.seq = 0

    # ---- payload construction
    def _payload(self, metrics) -> bytes:
        p = spb.Payload()
        p.timestamp = int(time.time() * 1000)
        self.seq = (self.seq + 1) % 256 or 1
        p.seq = self.seq
        for m in metrics:
            p.metrics.append(m)
        return p.SerializeToString()

    @staticmethod
    def metric(name: str, alias: int, ts_s: float, datatype: int, value):
        """One metric with its ORIGINAL source timestamp.

        `name` and `alias` are both set: the host's address scheme is name-based,
        and alias-only is spec-legal but not universally resolved.
        """
        m = spb.Payload.Metric()
        m.name = name
        m.alias = alias
        m.timestamp = int(round(ts_s * 1000))
        m.datatype = datatype
        if datatype == settings.DT_BOOL:
            m.boolean_value = bool(value)
        elif datatype == settings.DT_DOUBLE:
            m.double_value = float(value)
        else:
            m.long_value = int(value)
        return m

    # ---- lifecycle
    def start(self) -> bool:
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{settings.MQTT_CLIENT_PREFIX}-bridge",
            protocol=mqtt.MQTTv311,
        )
        if settings.MQTT_USERNAME:
            self.client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
        if settings.MQTT_TLS:
            self.client.tls_set()
        # NDEATH registered as the broker-side will, with the SAME bdSeq as the
        # BIRTH, so the host can tell a clean death from a network drop.
        death = self._payload([self.metric("bdSeq", 0, time.time(), settings.DT_INT64, self.bdseq)])
        self.client.will_set(settings.VTS_TOPIC_DEATH, death, qos=1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        deadline = time.time() + settings.MQTT_CONNECT_WINDOW_SEC
        attempt = 0
        while not _stop.is_set():
            attempt += 1
            try:
                self.client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=20)
                break
            except Exception as e:  # noqa: BLE001 - "not ready yet"
                remaining = deadline - time.time()
                if remaining <= 0:
                    log.error("ALARM could not reach the broker at %s:%s within %.0fs: %s",
                              settings.MQTT_HOST, settings.MQTT_PORT,
                              settings.MQTT_CONNECT_WINDOW_SEC, e)
                    return False
                delay = min(5.0, max(1.0, remaining))
                log.warning("broker %s:%s not ready (attempt %d): %s -- retrying in %.0fs",
                            settings.MQTT_HOST, settings.MQTT_PORT, attempt, e, delay)
                time.sleep(delay)
        self.client.loop_start()
        while not self.connected and time.time() < deadline:
            time.sleep(0.2)
        return self.connected

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = reason_code == 0
        log.info("MQTT connected rc=%s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if not _stop.is_set():
            log.warning("MQTT disconnected rc=%s", reason_code)

    def birth(self) -> bool:
        """NBIRTH: the metric list that makes VTScada auto-create the tags."""
        metrics = [self.metric("bdSeq", 0, time.time(), settings.DT_INT64, self.bdseq)]
        for alias, (tag, datatype) in enumerate(
                ((t, settings.datatype(t)) for t in settings.VTS_TAGS), start=1):
            metrics.append(self.metric(
                tag, alias, time.time(), datatype, False if datatype == settings.DT_BOOL else 0.0))
        r = self.client.publish(settings.VTS_TOPIC_BIRTH, self._payload(metrics), qos=1)
        r.wait_for_publish(timeout=10)
        COUNTERS["births"] += 1
        log.info("NBIRTH published (bdSeq=%s) topic=%s", self.bdseq, settings.VTS_TOPIC_BIRTH)
        return r.rc == 0

    def publish_batch(self, groups: dict[str, list[Pending]]) -> tuple[int, int]:
        """One NDATA payload. `groups` is {tag: [Pending, ...]}."""
        metrics = []
        for alias, tag in enumerate(settings.VTS_TAGS, start=1):
            rows = groups.get(tag) or []
            if not rows:
                continue
            datatype = settings.datatype(tag)
            for row in rows:
                metrics.append(self.metric(tag, alias, row.ts_s, datatype, row.value))
            # The trailing duplicate. Measured workaround for VTScada dropping a
            # trailing Boolean 0; harmless for analog. It repeats the last real
            # element's timestamp and value, so if it is stored at all it
            # coalesces onto that record instead of creating a second one.
            metrics.append(self.metric(tag, alias, rows[-1].ts_s, datatype, rows[-1].value))
        if not metrics:
            return 0, None
        payload = self._payload(metrics)
        r = self.client.publish(settings.VTS_TOPIC_DATA, payload, qos=1)
        r.wait_for_publish(timeout=15)
        return len(payload), r.rc


# ------------------------------------------------------------------- readback
def confirm(client: VTScadaClient, groups: dict[str, list[Pending]],
            t_lo: float, t_hi: float) -> tuple[list[Pending], list[Pending]]:
    """Which published events are actually in the historian?

    Reads the batch window back out of the `History` table and matches every
    event on (timestamp, value). Everything that does not match stays pending.
    """
    confirmed, unmatched = [], []
    for tag, rows in groups.items():
        try:
            stored = client.history(tag, t_lo - 0.5, t_hi + 0.5)
        except VTScadaError as e:
            COUNTERS["verify_errors"] += 1
            log.error("ALARM readback failed for %s: %s", tag, e)
            unmatched.extend(rows)
            continue
        index = {}
        for ts, value in stored:
            index.setdefault(round(ts, 3), value)
        is_bool = settings.VTS_TAGS.get(tag) == "bool"
        for row in rows:
            want = (1.0 if row.value else 0.0) if is_bool else float(row.value)
            hit = None
            for ts, got in index.items():
                if abs(ts - round(row.ts_s, 3)) < settings.TS_TOL:
                    hit = (ts, got)
                    break
            if hit is None:
                unmatched.append(row)
            elif abs((hit[1] or 0.0) - want) < max(settings.VALUE_TOL, 1e-9):
                confirmed.append(row)
            else:
                unmatched.append(row)
                log.warning("value mismatch event=%s ts=%s want=%s got=%s",
                            row.event_id, row.ts_s, want, hit[1])
    return confirmed, unmatched


# ------------------------------------------------------------------- queue work
def fetch_pending(limit: int) -> tuple[dict[str, list[Pending]], list[str], int]:
    """Pending rows for configured tags, grouped by tag in ascending seq order.

    Returns `(groups, unknown_tags, throttled_count)`.

    Rows whose tag is not in VTS_TAGS are excluded rather than skipped later: they
    could never be published, so leaving them in the result would make the same
    dead rows the head of every batch and starve real data forever.

    Rows already attempted within BRIDGE_RETRY_DELAY_SEC are skipped too, so a
    batch that is not being confirmed is retried at a sane rate rather than on
    every loop iteration.
    """
    tags = list(settings.VTS_TAGS)
    # Fair ordering. A plain `ORDER BY tag, seq LIMIT n` lets the
    # alphabetically-first tag fill every batch, so a tag with a large backlog
    # starves the others until it drains (audit F8). Ordering by each tag's own
    # row number INTERLEAVES the tags: row 1 of every tag, then row 2, and so
    # on. That is fair, still fills the batch, and keeps per-tag seq order.
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT event_id, tag, seq, value, ts_s FROM ("
                "  SELECT event_id, tag, seq, value,"
                "         EXTRACT(EPOCH FROM source_ts)::double precision AS ts_s,"
                "         ROW_NUMBER() OVER (PARTITION BY tag ORDER BY seq) AS rn"
                f"  FROM {db.table('samples')} "
                "  WHERE delivery_state='pending' AND tag = ANY(%s)"
                ") q ORDER BY rn, tag, seq LIMIT %s",
                (tags, limit),
            )
            rows = cur.fetchall()
            cur.execute(
                f"SELECT DISTINCT tag FROM {db.table('samples')} "
                "WHERE delivery_state='pending' AND NOT (tag = ANY(%s))", (tags,))
            unknown = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    now = time.monotonic()
    groups: dict[str, list[Pending]] = {}
    throttled = 0
    for event_id, tag, seq, value, ts_s in rows:
        if _throttled(event_id, now):
            throttled += 1
            continue
        groups.setdefault(tag, []).append(
            Pending(float(ts_s), float(value), event_id, int(seq), tag))
    return groups, unknown, throttled


def mark_delivered(batch_id, published, confirmed, unmatched, t_lo, t_hi, mid, nbytes) -> None:
    """Audit rows, the delivered marking, and the progress cursor -- one transaction.

    Doing them together keeps the cursor honest: it can never be advanced past a
    row that was not confirmed. Note that nothing READS the cursor -- delivery is
    driven by delivery_state -- so it is a progress record, not a correctness
    mechanism.
    """
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {db.table('batches')} "
                "(batch_id, topic, n_events, ts_min, ts_max, payload_bytes, mqtt_rc, "
                " confirmed, unmatched, confirmed_at) "
                "VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, now()) "
                "ON CONFLICT (batch_id) DO UPDATE SET confirmed=EXCLUDED.confirmed, "
                "unmatched=EXCLUDED.unmatched, confirmed_at=now()",
                (batch_id, settings.VTS_TOPIC_DATA, len(published), t_lo, t_hi,
                 nbytes, mid, len(confirmed), len(unmatched)),
            )
            confirmed_ids = {c.event_id for c in confirmed}
            for row in published:
                cur.execute(
                    f"INSERT INTO {db.table('attempts')} "
                    "(batch_id, event_id, tag, seq, source_ts, value, state) "
                    "VALUES (%s, %s, %s, %s, to_timestamp(%s), %s, %s) "
                    "ON CONFLICT (batch_id, event_id) DO UPDATE SET state=EXCLUDED.state",
                    (batch_id, row.event_id, row.tag, row.seq, row.ts_s, row.value,
                     "confirmed" if row.event_id in confirmed_ids else "unmatched"),
                )
            if confirmed:
                cur.execute(
                    f"UPDATE {db.table('samples')} SET delivery_state='delivered' "
                    "WHERE event_id = ANY(%s)",
                    ([c.event_id for c in confirmed],),
                )
                last_seq = max(c.seq for c in confirmed)
                cur.execute(
                    f"INSERT INTO {db.table('checkpoints')} (consumer_id, last_seq) "
                    "VALUES (%s, %s) ON CONFLICT (consumer_id) DO UPDATE SET "
                    "last_seq=GREATEST(" + db.table('checkpoints') + ".last_seq, %s), "
                    "updated_at=now()",
                    (f"{settings.MQTT_CLIENT_PREFIX}-bridge", last_seq, last_seq),
                )
            if unmatched:
                cur.execute(
                    f"UPDATE {db.table('samples')} SET attempts=attempts+1, "
                    "last_error='not confirmed in the VTScada historian' "
                    "WHERE event_id = ANY(%s)",
                    ([u.event_id for u in unmatched],),
                )
    finally:
        conn.close()


# ------------------------------------------------------------------- warm-up
def warm_up(pub: SparkplugPublisher, client: VTScadaClient) -> tuple[bool, str]:
    """Prove the tag path is live before trusting it.

    A payload published immediately after the NBIRTH that creates the tags is
    dropped, so publish a distinctive probe and poll until that exact
    (timestamp, value) appears in the historian.
    """
    tag = next(iter(settings.VTS_TAGS))
    datatype = settings.datatype(tag)
    probe_ts = time.time() - 1.0
    probe_value = 4242.0 if datatype == settings.DT_DOUBLE else True
    metrics = []
    for alias, t in enumerate(settings.VTS_TAGS, start=1):
        dt = settings.datatype(t)
        value = probe_value if t == tag else (False if dt == settings.DT_BOOL else 0.0)
        metrics.append(pub.metric(t, alias, probe_ts, dt, value))
    metrics.append(pub.metric(tag, 1, probe_ts, datatype, probe_value))
    pub.client.publish(settings.VTS_TOPIC_DATA, pub._payload(metrics), qos=1).wait_for_publish(10)

    deadline = time.time() + settings.BRIDGE_WARMUP_TIMEOUT_SEC
    while time.time() < deadline and not _stop.is_set():
        try:
            for ts, value in client.history(tag, probe_ts - 1, probe_ts + 5):
                if abs(ts - round(probe_ts, 3)) < settings.TS_TOL:
                    if datatype == settings.DT_BOOL:
                        if bool(value) == bool(probe_value):
                            return True, f"probe ts={ts} value={value} confirmed"
                    elif abs((value or 0.0) - probe_value) < 1e-6:
                        return True, f"probe ts={ts} value={value} confirmed"
        except VTScadaError as e:
            # No point polling a dead interface for 45s -- the main loop's
            # reachability check will take over and hold us in STANDBY.
            return False, f"probe readback failed: {e}"
        time.sleep(1.0)
    return False, f"probe not confirmed within {settings.BRIDGE_WARMUP_TIMEOUT_SEC:.0f}s"


# --------------------------------------------------------------------- main loop
_last_unknown_warn = 0.0


def _warn_unknown_tags(unknown: list[str]) -> None:
    """Report tags that can never be delivered, without flooding the log.

    A stray topic on the PLC re-triggers this on every cycle -- thousands of
    identical lines an hour, which buries everything else worth reading.
    """
    global _last_unknown_warn
    now = time.time()
    if now - _last_unknown_warn < 60:
        return
    _last_unknown_warn = now
    log.warning("ALARM %d pending tag(s) are not in VTS_TAGS and will never be "
                "delivered: %s -- either add them to VTS_TAGS or stop the PLC "
                "publishing them", len(unknown), unknown)


def cycle(pub: SparkplugPublisher, vts: VTScadaClient) -> tuple[bool, int, int]:
    """One delivery attempt.

    Returns `(did_work, backlog, confirmed_count)`. `confirmed_count` is what the
    caller uses to decide whether anything is actually getting through -- not
    `did_work`, which is true even when every batch fails to confirm.
    """
    groups, unknown, throttled = fetch_pending(settings.BRIDGE_BATCH)
    if throttled:
        COUNTERS["throttled"] += throttled
    if unknown:
        _warn_unknown_tags(unknown)
    if not groups:
        return False, db.backlog(), 0

    published = [row for rows in groups.values() for row in rows]
    n = len(published)
    t_lo = min(row.ts_s for row in published)
    t_hi = max(row.ts_s for row in published)
    batch_id = f"{int(time.time() * 1000)}-{os.getpid()}"
    nbytes, rc = pub.publish_batch(groups)
    COUNTERS["batches"] += 1
    COUNTERS["published_events"] += n

    if rc not in (0, None):
        log.error("ALARM publish rc=%s batch=%s -- leaving rows pending", rc, batch_id)
        mark_delivered(batch_id, published, [], published, t_lo, t_hi, rc, nbytes)
        COUNTERS["unmatched"] += n
        return True, db.backlog(), 0

    if settings.BRIDGE_CONFIRM_MODE == "publish":
        # Fire-and-forget mode (no VTScada REST). The MQTT publish returning
        # rc==0 IS the delivery proof; there is no historian readback. Every
        # well-formed publish is marked delivered in one shot. This is weaker
        # than readback (nothing proves VTScada actually stored the data) but it
        # works when the bridge cannot reach a REST interface at all.
        mark_delivered(batch_id, published, published, [], t_lo, t_hi, rc, nbytes)
        COUNTERS["confirmed"] += n
        backlog = db.backlog()
        log.info("batch=%s published=%d confirmed=%d (publish-mode, no readback) "
                 "backlog=%d payload=%sB", batch_id, n, n, backlog, nbytes)
        return True, backlog, n

    # From here on these events are subject to the retry throttle, so a batch
    # that does not confirm is not re-published on the very next iteration.
    now = time.monotonic()
    for row in published:
        _last_attempt[row.event_id] = now

    # Poll for the readback instead of sleeping a fixed settle time, so rows are
    # marked delivered as soon as the historian shows them.
    deadline = time.time() + settings.BRIDGE_SETTLE_SEC
    confirmed, unmatched = confirm(vts, groups, t_lo, t_hi)
    while unmatched and time.time() < deadline and not _stop.is_set():
        time.sleep(0.5)
        confirmed, unmatched = confirm(vts, groups, t_lo, t_hi)

    mark_delivered(batch_id, published, confirmed, unmatched, t_lo, t_hi, rc, nbytes)
    COUNTERS["confirmed"] += len(confirmed)
    COUNTERS["unmatched"] += len(unmatched)
    COUNTERS["retries"] += len(unmatched)
    backlog = db.backlog()
    log.info("batch=%s published=%d confirmed=%d unmatched=%d backlog=%d payload=%sB",
             batch_id, n, len(confirmed), len(unmatched), backlog, nbytes)
    return True, backlog, len(confirmed)


def prune_once() -> None:
    """Apply the retention window. Never deletes pending rows."""
    try:
        deleted = db.prune(settings.RETENTION_DAYS)
    except Exception as e:  # noqa: BLE001 - retention must not stop delivery
        log.error("ALARM retention prune failed: %s", e)
        return
    if deleted and any(deleted.values()):
        log.info("retention: pruned %s (rows older than %d days)",
                 deleted, settings.RETENTION_DAYS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Drain the durable queue into VTScada.")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    ap.add_argument("--standby-only", action="store_true",
                    help="only watch VTScada reachability; publish nothing")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("bridge starting: %s", settings.describe())
    if settings.BRIDGE_CONFIRM_MODE not in ("readback", "publish"):
        # Refuse rather than guess. Anything that is not exactly 'publish' would
        # otherwise silently run in readback mode -- so a typo (e.g. 'publsh')
        # in a deployment that has no REST would sit in STANDBY forever, with a
        # log that blames the network instead of the typo.
        log.error("ALARM BRIDGE_CONFIRM_MODE=%r is not 'readback' or 'publish' -- "
                  "refusing to start rather than silently guessing",
                  settings.BRIDGE_CONFIRM_MODE)
        return 2
    db.apply_schema()
    prune_once()
    last_prune = time.time()
    vts = VTScadaClient()

    if a.standby_only:
        while not _stop.is_set():
            ok, detail = vts.reachable()
            log.info("VTScada reachable=%s (%s)", ok, detail)
            time.sleep(settings.BRIDGE_POLL_SEC)
        return 0

    try:
        pub = SparkplugPublisher()
    except OSError:
        # next_bdseq() has already logged the ALARM; running without a persisted
        # bdSeq guarantees a silent, total delivery failure after the next
        # restart, so refuse to start (audit F9).
        return 2
    if not pub.start():
        log.error("ALARM could not connect to the broker at %s:%s",
                  settings.MQTT_HOST, settings.MQTT_PORT)
        return 2
    pub.birth()
    time.sleep(settings.BRIDGE_BIRTH_WAIT_SEC)
    publish_mode = settings.BRIDGE_CONFIRM_MODE == "publish"
    if publish_mode:
        log.warning("BRIDGE_CONFIRM_MODE=publish: no VTScada REST readback. Rows are "
                    "marked delivered on MQTT publish success; the historian is NOT "
                    "proving delivery, and an outage cannot build a backlog to recover.")
    elif settings.BRIDGE_WARMUP_PROBE:
        ok, detail = warm_up(pub, vts)
        log.info("warm-up %s: %s", "ok" if ok else "FAILED", detail)
        if not ok:
            log.warning("continuing anyway -- every batch is still verified by readback")
    else:
        log.info("warm-up probe disabled (BRIDGE_WARMUP_PROBE=false); starting delivery. "
                 "A batch published inside the post-BIRTH window will not confirm and is "
                 "retried automatically.")

    def shutdown(*_):
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    in_standby = False
    standby_since = 0.0
    last_standby_log = 0.0
    last_beat = 0.0
    # The stall detector keys off the last CONFIRMED event, not the last attempted
    # batch. Attempting is not progress: if VTScada answers REST but stops storing,
    # every batch "succeeds" and nothing ever arrives -- the case this alarm exists
    # to catch. Timing off attempts would keep it silent forever.
    last_confirmed = time.time()
    idle = 0

    while not _stop.is_set():
        try:
            if not publish_mode:
                state, detail = vts.probe()
                if state != vts.OK:
                    if not in_standby:
                        in_standby, standby_since = True, time.time()
                        # Name the reason: 'credentials-rejected' and 'unreachable'
                        # need completely different fixes (audit F3).
                        log.warning("STANDBY entered -- VTScada REST %s: %s", state, detail)
                    COUNTERS["standby_polls"] += 1
                    if time.time() - last_standby_log >= 60:
                        last_standby_log = time.time()
                        try:
                            bl = db.backlog()
                        except Exception as e:  # noqa: BLE001
                            bl = f"ERR {e!r}"
                        log.warning("STANDBY %.0fs: backlog=%s (publishing nothing; rows stay pending)",
                                    time.time() - standby_since, bl)
                    time.sleep(settings.BRIDGE_POLL_SEC)
                    if a.once:
                        log.warning("--once requested while in STANDBY: nothing was published")
                        return 1
                    continue

                if in_standby:
                    log.info("VTScada historian reachable again after %.1fs", time.time() - standby_since)
                    # Re-register the tags before resuming. If VTScada itself
                    # restarted during the outage it has forgotten the metric list and
                    # would ignore every DATA message. The bdSeq is deliberately NOT
                    # incremented: the NDEATH will registered at connect carries this
                    # value, and a re-BIRTH with a different bdSeq would make the two
                    # disagree. A host that did not restart treats this as a stale
                    # BIRTH and ignores it, which is harmless -- the tags already exist.
                    pub.birth()
                    time.sleep(settings.BRIDGE_BIRTH_WAIT_SEC)
                    log.info("resuming delivery")
                    in_standby = False
                    last_confirmed = time.time()  # do not alarm on the outage we just left

            did, backlog, confirmed_n = cycle(pub, vts)
            if confirmed_n:
                last_confirmed = time.time()
            idle = 0 if did else idle + 1
            delay = settings.BRIDGE_POLL_SEC if idle < 5 else 5
            if did and backlog and backlog > 0:
                delay = 0.2  # do not idle while a backlog exists
            time.sleep(delay)
        except Exception as e:  # noqa: BLE001 - never let the loop die silently
            log.exception("ALARM cycle failed: %s", e)
            time.sleep(5)

        if time.time() - last_beat >= settings.BRIDGE_HEARTBEAT_SEC:
            last_beat = time.time()
            try:
                bl = db.backlog()
            except Exception as e:  # noqa: BLE001
                bl = f"ERR {e!r}"
            try:
                size_mb = round(db.database_size() / (1024 * 1024))
            except Exception:  # noqa: BLE001
                size_mb = "?"
            since_confirm = time.time() - last_confirmed
            log.info("heartbeat backlog=%s db_size_mb=%s counters=%s "
                     "since_last_confirm=%.0fs", bl, size_mb, COUNTERS, since_confirm)
            if isinstance(bl, int) and bl > 0 and since_confirm > settings.BRIDGE_STALL_ALARM_SEC:
                log.error("ALARM backlog=%s and nothing confirmed for %.0fs -- the "
                          "historian is answering but not storing what we send",
                          bl, since_confirm)
        if settings.RETENTION_DAYS > 0 and time.time() - last_prune >= 86400:
            prune_once()
            last_prune = time.time()
        if a.once:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
