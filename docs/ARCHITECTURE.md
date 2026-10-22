# Architecture

Two services, one queue table. The whole design follows from a single rule:

> **A sample is written to the database before anything else happens to it, and
> it is marked `delivered` only after it has been read back out of the VTScada
> historian. An MQTT publish returning `rc=0` proves nothing.**

Everything else — the standby state, the retry behaviour, the audit tables — is a
consequence of taking that rule seriously.

---

## Data flow

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

Rows are never deleted. Delivered rows are the audit trail, and
`tools/verify_history.py reconcile` reads them back against the historian. If the
volume matters, prune `delivery_state='delivered'` rows older than some window —
nothing reads them except reconciliation.

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
