# Data model

The queue lives in the PostgreSQL/TimescaleDB schema named by `DB_SCHEMA`
(default `pipeline`). The schema is defined once in
`src/pipeline/schema.sql` and applied idempotently at service startup by
`db.apply_schema()`. There is no separate migration tool.

## `pipeline.samples` — the durable queue

| Column | Type | Meaning |
|---|---|---|
| `event_id` | text PK | `device_id:seq:tag` — stable identity of one logical sample |
| `device_id` | text | PLC/edge identity |
| `tag` | text | tag name (must match a key in `VTS_TAGS` to deliver) |
| `seq` | bigint | ordering/identity; derived from the source timestamp when the PLC sends no sequence |
| `value` | double precision | numeric value; booleans stored as 0/1 (the type comes from `VTS_TAGS`) |
| `source_ts` | timestamptz | the timestamp that is published and recorded in the historian |
| `received_ts` | timestamptz | when the collector stored it (default `now()`) |
| `topic` | text | originating MQTT topic |
| `payload` | text | raw message, for forensics |
| `delivery_state` | text | `pending` \| `delivered` (CHECK) |
| `attempts` | int | failed confirmation attempts |
| `last_error` | text | why the last attempt did not confirm |

**Idempotency key:** `UNIQUE (device_id, seq, tag)`. A replayed PLC buffer or a
redelivered QoS 1 message collapses onto the existing row (`ON CONFLICT DO
NOTHING`) instead of creating a second sample.

**Hot index:** `samples_pending_idx (tag, seq) WHERE delivery_state='pending'` —
partial, so it stays small as delivered rows accumulate.

## `pipeline.quarantine`

Undecodable or implausible messages, with `reason`, `topic`, `payload`,
`created_at`. Nothing is silently dropped or zeroed; quarantine is terminal (the
message is acknowledged because redelivery could never succeed).

## `pipeline.checkpoints`

`consumer_id`, `last_seq`, `updated_at`. Written in the same transaction as a
delivered marking. **Nothing reads it** — delivery is driven entirely by
`delivery_state`. It is a progress record, not a correctness mechanism.

## `pipeline.batches` and `pipeline.attempts`

Audit trail for reconciliation. `batches` is one row per publish attempt (counts,
window, payload size, MQTT rc). `attempts` is one row per event per attempt with
`state ∈ {published, confirmed, unmatched}`. `tools/verify_history.py reconcile`
reads these against the historian.

## Four different times — do not conflate them

| Concept | Where | What it means |
|---|---|---|
| **Source time** | `source_ts` | the PLC's own clock, if it sent one |
| **Arrival time** | `received_ts` | when the collector received it |
| **Event identity** | `event_id` / `seq` | which logical sample this is |
| **Historian confirmation** | `delivery_state` + `attempts` | proof it is stored |

For a PLC that sends no timestamp (`topic_tag` / `sparkplug` decoders), **source
time is arrival time**. The original measurement time is then lost — say so
plainly; never claim the system preserves timestamps it was never given.

## States and transitions

```
insert (pending) ──publish──► confirm(readback) ──► delivered
       │                          │
       │                          └─ no match ──► stays pending, attempts+1,
       │                                            retried after BRIDGE_RETRY_DELAY_SEC
       └─ undecodable / bad ts ──► quarantine (never enters samples)
```

Only the **bridge** writes `delivery_state`, and only in the transaction that
records the readback result. The collector never touches it.

## Retention

`RETENTION_DAYS` (default 90, `0` = forever) prunes:

* `samples` **where `delivery_state='delivered'`** and `received_ts` older than the
  window — **pending rows are never pruned** (age is arrival age, not the PLC's
  clock);
* `attempts`, `batches`, `quarantine` older than the window.

Pruning runs at bridge startup and once a day, in bounded batches (`ctid IN
(SELECT … LIMIT n)`) so a large backlog is not held in one long transaction.

## Useful queries

```sql
-- backlog
SELECT count(*) FROM pipeline.samples WHERE delivery_state='pending';

-- by state
SELECT delivery_state, count(*) FROM pipeline.samples GROUP BY 1;

-- oldest pending (is the head of the queue stuck?)
SELECT tag, seq, source_ts, attempts, last_error
FROM pipeline.samples WHERE delivery_state='pending'
ORDER BY source_ts LIMIT 20;

-- unknown tags that can never deliver
SELECT DISTINCT tag FROM pipeline.samples
WHERE delivery_state='pending' AND tag <> ALL (ARRAY['AI01','DI01']);

-- recent quarantine
SELECT created_at, reason, topic, payload
FROM pipeline.quarantine ORDER BY id DESC LIMIT 20;
```

## Schema changes

`CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so changes need
an explicit idempotent step in `db._migrate()`. The only historical migration is
`payload: jsonb → text` (a bare PLC value is not valid JSON and a jsonb column
rejected it). There is no down-migration; a backup is the rollback.
