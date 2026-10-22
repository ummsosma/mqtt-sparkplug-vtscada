# Architecture

Two services, one queue table. The whole design follows from a single rule:

> **A sample is written to the database before anything else happens to it, and
> it is marked `delivered` only after it has been read back out of the VTScada
> historian. An MQTT publish returning `rc=0` proves nothing.**

Everything else — the standby state, the retry behaviour, the audit tables — is a
consequence of taking that rule seriously.

This document has three parts:

* **Part 1 — How the pieces fit**: the shape of the system. Read this first.
* **Part 2 — The life of one message**: a single MQTT message from the PLC,
  walked step by step to the VTScada historian, with the exact data passed
  between the modules at each hand-off and *why*.
* **Part 3 — Under the hood**: the queue, the design rules, the failure
  behaviour. The depth you need only if you intend to change the code.

---

# Part 1 — How the pieces fit

## The pipeline in one picture

```
   CLICK PLUS
       │  MQTT QoS 1
       ▼
   ┌─────────┐
   │  EMQX   │
   └────┬────┘
        │  subscribe  PLC_TOPIC
        ▼
  ┌──────────────┐   decode (decoder.py)
  │  collector   │──────────────────────────────┐
  └──────┬───────┘                              │ undecodable / bad timestamp
         │ INSERT ... before PUBACK             ▼
         │                                ┌────────────┐
         │                                │ quarantine │
         ▼                                └────────────┘
   ┌──────────────────┐
   │   TimescaleDB    │  pipeline.samples
   │  durable queue   │  delivery_state = 'pending'
   └────────┬─────────┘
            │ SELECT ... WHERE delivery_state='pending'
            ▼
     ┌─────────────┐   one NDATA payload per batch:
     │   bridge    │   per tag, the metric name REPEATED, one element per
     └──────┬──────┘   sample, each with its own source timestamp, plus one
            │          trailing duplicate element
            │ publish  spBv1.0/<GROUP>/NDATA/<EDGE>
            ▼
   ┌────────────────────┐
   │ VTScada MQTT driver│  tag HistoryAddress = <topic>++<tag>$$History$$
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ VTScada historian  │
   └─────────┬──────────┘
             │ read the batch's timestamps back
             │   REST/SQLQuery on the History table
             ▼
     ┌─────────────┐
     │   bridge    │  mark delivered + advance checkpoint, ONE transaction
     └─────────────┘
```

The readback edge is what makes the design work: it is the only thing that turns
"we sent it" into "it is there".

## The modules, and what each one is for

| Module | File | One-line job |
|---|---|---|
| collector | `src/pipeline/collector.py` | Listens on the broker, decodes messages, stores samples. Knows nothing about VTScada. |
| decoder | `src/pipeline/decoder.py` | Turns a raw PLC payload into a normalized `Sample`. The one module to change for a new PLC. |
| mapping | `src/pipeline/mapping.py` | Optional: rename / scale / value-map tags from a JSON file instead of code. |
| bridge | `src/pipeline/bridge.py` | Reads the queue, publishes Sparkplug B, proves delivery by historian readback. |
| vtclient | `src/pipeline/vtclient.py` | Talks to VTScada's REST interface. The delivery oracle. |
| db | `src/pipeline/db.py` | Database connections, schema, queue queries, retention pruning. |
| settings | `src/pipeline/settings.py` | Every configuration variable, in one file, with defaults. |

The dependency arrows point one way only: collector → decoder → settings, and
bridge → vtclient → settings. The collector never talks to VTScada; the bridge
never decodes a PLC message. That is deliberate, and it is why either side can be
restarted, replaced or debugged alone.

---

## Why two services and not one

An earlier iteration had the collector both persisting *and* delivering, and two
different components writing `delivery_state`. That made "who marked this row
delivered, and why?" unanswerable, and it meant a slow delivery path could stall
the ingestion path.

Now the split is by responsibility, with a clean seam:

* the **collector** owns getting every sample into the queue exactly once, and
  knows nothing about VTScada;
* the **bridge** owns the queue → historian transition, and nothing else writes
  `delivery_state`.

They share no state except the database, so either can be restarted, scaled or
replaced independently. It also means the queue can be drained by something else
entirely without touching the collector.

---

# Part 2 — The life of one message

Follow one real message through the system. The PLC in this example publishes a
tank level: 21.5, on topic `plc/1`, at timestamp 1792033051030.

## Step 0 — the PLC publishes

```
topic:   plc/1
payload: 21.5
```

QoS 1 means the broker promises to deliver the message *at least once* — it
re-sends anything its subscriber never acknowledged. Every guarantee downstream
is built on that.

## Step 1 — the collector receives it (collector.py)

The MQTT network thread puts `(topic, payload, message_id)` onto an in-process
work queue and returns immediately. **No database work happens on the network
thread** — a slow query there would delay acknowledgements and cause the broker
to re-send messages already stored. A worker thread picks the tuple up.

*Data at this hand-off:* the raw bytes and topic, untouched. Nothing has been
interpreted yet.

## Step 2 — the collector calls the decoder (decoder.py)

`decoder.decode(topic, payload)` is asked: *what does this message mean?*

Which decoder runs is a setting (`PLC_DECODER`): `json` for JSON bodies with
configurable field names, `topic_tag` for a bare value whose meaning is carried
by the topic, `sparkplug` for an already-encoded payload, `auto` to try them in
order, `mapping` for a declarative JSON mapping file. This example is
`topic_tag`: the payload is a bare number and the topic is the only clue to what
it is.

The decoder returns a **`Sample`** — the pipeline's internal shape for one
reading:

```
Sample(device_id='plc1', tag='DS1', value=21.5, source_ts_ms=1792033051030, seq=1792033051030)
```

Why each field exists:

* `device_id, tag` — *who* measured it; feeds both the dedup key and the
  eventual VTScada tag name.
* `value` — coerced to a float or bool. Anything non-numeric raises
  `Undecodable` rather than becoming a zero: a silent zero in a historian is a
  lie that is very expensive to find later.
* `source_ts_ms` — the PLC's own timestamp. This, not arrival time, is what the
  historian will record — the whole point of the pipeline. A message with no
  timestamp falls back to arrival time and is documented as such.
* `seq` — the deduplication sequence number. If the PLC does not send one (this
  one does not) it is derived from the timestamp, so a replayed message collapses
  onto the existing row instead of becoming a second one.

If the decoder cannot make sense of the message it raises `Undecodable`. The
collector writes the raw payload to `pipeline.quarantine` with the reason, and
**acknowledges** it — re-delivering an undecodable message can never succeed, so
there is nothing to gain.

## Step 3 — the collector validates and stores (db.py)

Before inserting, the collector checks the timestamp is plausible — not before
`TS_MIN_YEAR` (2020), not more than `TS_MAX_FUTURE_SECONDS` in the future. A PLC
with an unset clock is common, and a 1970 timestamp landing in the historian
poisons every trend that reads it. An implausible timestamp is quarantined like
an undecodable message.

Then one row goes into `pipeline.samples`:

```
event_id='plc1:1792033051030:DS1'  tag='DS1'  value=21.5
source_ts=1792033051030            delivery_state='pending'
```

`event_id` is the stable identity (`device:seq:tag`). The insert uses
`ON CONFLICT (device_id, seq, tag) DO NOTHING`: if a broker re-delivery or a PLC
buffer replay produces the same event again, the insert is counted as a duplicate
and discarded. The duplicate was never stored, so it does not need to be
delivered twice.

*Data at this hand-off:* a `Sample` from the decoder → one row in the queue.
The raw topic and payload are stored alongside for forensics — when a reading
looks wrong three weeks later, the original message is still there.

## Step 4 — the collector acknowledges

Only after the row is committed does the collector tell the broker "received"
(the PUBACK). The order is the entire guarantee:

* store **then** ack → the broker re-sends anything not durably stored;
* ack **then** store → a crash in between silently loses the message forever.

If the database is unreachable, the collector does **not** ack. The message stays
unacknowledged at the broker (for up to seven days, the session expiry) and is
re-delivered when the collector reconnects. Input is never lost ahead of the
database.

**The collector's contract ends here.** It never talks to VTScada and never
touches `delivery_state` again. One row in `pipeline.samples` is `pending`, and
everything after this step belongs to the bridge.

## Step 5 — the bridge fetches pending rows (bridge.py)

The bridge's main loop queries the queue for pending rows of *configured* tags
(`VTS_TAGS` decides what is published at all), grouped per tag, in fair
interleaved order so a tag with a huge backlog cannot starve the others.

*Data at this hand-off:* rows out of the database as `Pending` tuples —
`(source timestamp, value, event_id, seq, tag)`. A tag in the queue but not in
`VTS_TAGS` is excluded here, and an ALARM names it once a minute: otherwise those
dead rows would sit at the head of every batch forever.

Rows attempted within the last `BRIDGE_RETRY_DELAY_SEC` are skipped too, so a
batch that is not being confirmed is retried at a sane rate rather than hammered
on every loop iteration.

## Step 6 — the bridge publishes Sparkplug B (bridge.py → MQTT)

The pending rows are packed into one Sparkplug B `NDATA` payload for the topic
`spBv1.0/<GROUP>/NDATA/<EDGE>` (GROUP and EDGE come from `VTS_GROUP` / `VTS_EDGE`).

One counter-intuitive property of VTScada, established experimentally (see
[vtscada-wire-contract.md](vtscada-wire-contract.md)): the payload is **not** a
list of different metrics. It is the *same metric name repeated* — one element
per sample, each carrying its own source timestamp and value. VTScada writes
every element into its historian at its own timestamp in one go.

Two defensive quirks in the payload, each defeating a silent VTScada failure:

* a **trailing duplicate** of each tag's last element, because VTScada drops a
  trailing Boolean `false` (the duplicate coalesces and never corrupts a record);
* a **warm-up probe** (4242.0) after the BIRTH that creates the tags, because the
  first payload after a BIRTH is dropped. The probe is read back before real
  delivery starts.

The bridge publishes with QoS 1 and records `mqtt_rc`, the payload size and the
row set in the `pipeline.batches` audit table. But — the rule the whole design
hangs on — **`rc=0` proves nothing about storage.** The batch is not done.

## Step 7 — the bridge reads the historian back (vtclient.py)

`vtclient.history(tag, t_lo, t_hi)` sends a REST request to VTScada: `SQLQuery`
on the `History` table for the tag's column over the batch's time window,
following pagination. It returns `[(timestamp, value), ...]` — what the historian
*actually* has.

`bridge.confirm()` then matches every published event against that readback on
timestamp (within `TS_TOL`) and value (within `VALUE_TOL`). Everything that
matches is confirmed; anything else stays `pending` and is retried.

If REST itself is unreachable, the bridge never got to this step: before every
cycle it probes REST and, while it is down, sits in **STANDBY** — publishing
nothing, heart-logging the growing backlog every 60 s. Publishing into a void and
then marking rows delivered is the one outcome the design exists to prevent.
The collector is unaffected and keeps filling the queue; on recovery the bridge
re-registers its tags and drains.

*Data at this hand-off:* `(timestamp, value)` pairs from VTScada's History
table → matched against the batch → a list of confirmed event_ids.

## Step 8 — the bridge marks delivered (db.py)

In **one transaction** the bridge

1. records the batch audit row,
2. writes per-event rows to `pipeline.attempts` (`confirmed` / `unmatched` —
   what reconciliation later reads),
3. flips confirmed rows to `delivery_state='delivered'`,
4. advances the progress cursor.

One transaction, because the cursor can never be allowed to claim progress past a
row that was not confirmed. (Nothing in the pipeline *reads* the cursor —
`delivery_state` drives delivery — it is a progress record, not a mechanism.)

At this point the sample has provably landed: it is in the historian at its
source timestamp, and the row in the queue says so. `pipeline status` shows the
backlog shrinking toward zero; `tools/verify_history.py reconcile` reads every
delivered row back out of the historian as the independent end-to-end check.

## What if something dies at each step?

| Crash between | Result | Why nothing is lost |
|---|---|---|
| PLC → broker | Broker never got it; PLC's QoS 1 re-sends | Nothing was acknowledged |
| collector → DB | No PUBACK; broker re-delivers | The row was never committed |
| DB → MQTT publish | Rows still `pending`; the bridge retries | `delivery_state` never changed |
| publish → readback | Rows still `pending`; re-published | VTScada coalesces per timestamp, so no duplicate history rows |
| readback → delivered-marking | Rows still `pending`; confirmed again next cycle | The marking is idempotent |

The pattern in every row is the same: **a transition is only recorded after the
thing it records has durably happened.** That is the entire architecture in one
sentence.

---

# Part 3 — Under the hood

## The queue

```sql
pipeline.samples (
    event_id, device_id, tag, seq, value, source_ts, received_ts, topic, payload,
    delivery_state,   -- 'pending' | 'delivered'
    attempts, last_error,
    UNIQUE (device_id, seq, tag)
)
```

`UNIQUE (device_id, seq, tag)` is the idempotency key. A PLC replaying its buffer
after a reconnect, or a broker redelivering a QoS 1 message, produces a duplicate
insert that is counted and discarded rather than queued twice. For a PLC that
sends no sequence number the collector derives `seq` from the sample's own
timestamp, which makes the key mean "this device, this tag, this instant" — the
right deduplication semantic for a device that only reports `(tag, value, time)`.

`source_ts` is the PLC's own clock, not arrival time. It is what gets published as
the Sparkplug metric timestamp, and therefore what the historian records. This is
the whole point of the pipeline: data lands at the time it was measured.

The bridge's hot query is served by a **partial** index, so it stays small as
delivered rows accumulate:

```sql
CREATE INDEX samples_pending_idx ON pipeline.samples (tag, seq)
    WHERE delivery_state = 'pending';
```

Alongside `samples`, four small tables earn their keep:

* `quarantine` — undecodable or implausible messages, with the reason and the raw
  payload. Kept, not dropped: a PLC that changes its payload shape should be
  visible, not silent.
* `attempts` — one row per event per publish attempt. What
  `tools/verify_history.py reconcile` reads to compare the queue against the
  historian.
* `batches` — one row per publish attempt, with counts and the MQTT return code.
* `checkpoints` — the progress cursor written in the same transaction as the
  delivered marking. Nothing reads it; it exists so an operator can ask "how far
  have we got?" with one query.

Rows are never deleted while `pending`. Delivered rows are the audit trail, and
pruning (`RETENTION_DAYS`) removes only delivered rows and old audit rows —
never pending ones.

---

## Design rules and the failure each one prevents

| Rule | The failure it prevents |
|---|---|
| PUBACK withheld until the row is committed | A message acknowledged on arrival lives only in process memory; killing the process loses it silently |
| DB work never runs on the MQTT network thread | A slow query blocks the socket loop, delays acks, and makes the broker retransmit messages we already have |
| `delivery_state` is written by exactly one component | Unanswerable "who delivered this?" and two components fighting over one row |
| A batch is confirmed by readback, never by publish rc | Rows marked delivered that are not in the historian |
| The delivered marking and the progress cursor advance in one transaction | A cursor that claims progress past rows that were never confirmed. (Nothing reads the cursor — `delivery_state` drives delivery — so this is about the record being truthful, not about behaviour) |
| A row already attempted is not re-attempted for `BRIDGE_RETRY_DELAY_SEC` | "VTScada answers REST but is not storing" becoming a hot loop that hammers the historian and the broker |
| The stall alarm keys off the last **confirmed** event, not the last attempted batch | The alarm staying silent through the one failure it exists to catch, because attempting a batch counts as progress |
| Nothing is published while REST is unreachable | Publishing into the void, then marking it delivered |
| A BIRTH is re-sent on recovery, with the same `bdSeq` | VTScada restarting during an outage forgets the metric list and ignores every following message. The `bdSeq` is deliberately not incremented, because the NDEATH will registered at connect carries that value |
| `bdSeq` is persisted and incremented across restarts | A BIRTH the host considers stale, after which every message is ignored — presenting as "tags exist but never update" |
| One trailing duplicate element per tag group | VTScada silently drops a trailing Boolean `false` |
| A warm-up probe before real delivery | The first payload after the BIRTH that creates the tags is dropped |
| Queued tags not in `VTS_TAGS` are excluded from the batch query | Dead rows sitting at the head of every batch, starving real data forever |
| `statement_timeout` + TCP keepalives on the DB connection | A half-open WAN link wedging the loop; a silent wedge looks exactly like "the historian stopped accepting data" |
| Every loop iteration is wrapped and logged; a heartbeat carries the backlog and a stall counter | A wedged loop that looks like a quiet one |

---

## State that lives outside the database

| What | Where | Why it must persist |
|---|---|---|
| `bdSeq` | `STATE_DIR/bdseq` (a Docker volume) | A host ignores a BIRTH whose `bdSeq` it has already seen, then ignores everything after it |

Nothing else is kept in memory across a restart. The bridge holds only a
short-lived per-event retry throttle so a just-failed batch is not hammered; the
authoritative retry state is `delivery_state='pending'` in the database.

---

## Failure behaviour

**VTScada unreachable.** The bridge probes REST before every cycle. While it is
down the bridge enters STANDBY, logs a heartbeat with the growing backlog every
60 s, and publishes nothing. The collector is unaffected and keeps filling the
queue. On recovery the bridge re-registers its tags and drains.

**The backlog under live load never reaches exactly zero**, because samples are
always inside the publish → confirm window. That is why the store-and-forward test
reports *catch-up time* (backlog back to its steady floor while the source keeps
publishing) separately from *backlog == 0* (measured with the source stopped). A
drain loop that waits for `pending == 0` under live load will hang forever.

**Re-publishing is safe.** VTScada coalesces per timestamp, so a retry after an
interruption adds no duplicate record. The test asserts this explicitly by
checking for duplicate timestamps in the historian window.

---

## What was deliberately left out

* **No web UI, no API.** The tools and SQL are the interface. Adding a service to
  serve a status page is a bigger liability than it is worth at this size.
* **No hypertable.** `pipeline.samples` is a plain table. At one sample per second
  per tag the volume is trivial, and a hypertable would force the partition column
  into the primary key and complicate `ON CONFLICT`. If you ever need it, the
  column to partition on is `received_ts`.
* **No message-schema registry.** The decoder is configuration; see
  `src/pipeline/decoder.py`.
* **No wrapper library for Sparkplug.** Use the official protobuf directly.
  Wrapper libraries have a habit of pinning `paho-mqtt` to 1.x, which breaks the
  2.x callback API and manual acknowledgement that the collector depends on.
