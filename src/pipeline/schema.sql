-- The durable queue.
--
-- One row per PLC sample. The collector inserts a row BEFORE acknowledging the
-- MQTT message, and the bridge marks it 'delivered' ONLY after reading that
-- sample back out of the VTScada historian. A row that is still 'pending' is
-- therefore never lost, and re-sending one is safe.
--
-- Applied idempotently by db.apply_schema() on service startup, so this file is
-- the single definition of the schema -- there is no initdb ordering to get
-- wrong and nothing to keep in sync by hand.

CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE IF NOT EXISTS pipeline.samples (
    event_id       text PRIMARY KEY,
    device_id      text NOT NULL,
    tag            text NOT NULL,
    seq            bigint NOT NULL,
    value          double precision,
    source_ts      timestamptz NOT NULL,     -- the PLC's own timestamp
    received_ts    timestamptz NOT NULL DEFAULT now(),
    topic          text,
    payload        text,                     -- the raw message, for forensics
    delivery_state text NOT NULL DEFAULT 'pending'
                   CHECK (delivery_state IN ('pending', 'delivered')),
    attempts       int NOT NULL DEFAULT 0,
    last_error     text,
    -- Idempotent input: a PLC replaying its buffer must not create a second row.
    UNIQUE (device_id, seq, tag)
);

-- The bridge's only hot query. Partial, so it stays small as delivered rows
-- accumulate: SELECT ... WHERE delivery_state='pending' ORDER BY tag, seq.
CREATE INDEX IF NOT EXISTS samples_pending_idx
    ON pipeline.samples (tag, seq) WHERE delivery_state = 'pending';

-- Samples that could not be decoded or whose timestamp is implausible. Kept
-- rather than dropped: a PLC that changes its payload shape should be visible.
CREATE TABLE IF NOT EXISTS pipeline.quarantine (
    id         bigserial PRIMARY KEY,
    event_id   text,
    reason     text NOT NULL,
    topic      text,
    payload    text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Per-consumer progress cursor. Written in the same transaction as the delivered
-- marking, so it can never point past a row that was not confirmed.
--
-- NOTE: nothing in the pipeline READS this. Delivery is driven entirely by
-- delivery_state='pending'. The cursor exists so an operator can ask "how far have
-- we got?" with one query instead of scanning the queue. It is a progress record,
-- not a correctness mechanism -- do not build on it as though it were.
CREATE TABLE IF NOT EXISTS pipeline.checkpoints (
    consumer_id text PRIMARY KEY,
    last_seq    bigint NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Audit trail: one row per publish attempt against the historian.
CREATE TABLE IF NOT EXISTS pipeline.batches (
    batch_id      text PRIMARY KEY,
    published_at  timestamptz NOT NULL DEFAULT now(),
    topic         text NOT NULL,
    n_events      int NOT NULL,
    ts_min        timestamptz,
    ts_max        timestamptz,
    payload_bytes int,
    mqtt_rc       int,
    confirmed     int NOT NULL DEFAULT 0,
    unmatched     int NOT NULL DEFAULT 0,
    confirmed_at  timestamptz,
    note          text
);

-- Audit trail: one row per event per attempt. This is what reconciliation
-- reads -- it is the record of what we published and what came back.
CREATE TABLE IF NOT EXISTS pipeline.attempts (
    id         bigserial PRIMARY KEY,
    batch_id   text NOT NULL,
    event_id   text NOT NULL,
    tag        text NOT NULL,
    seq        bigint,
    source_ts  timestamptz NOT NULL,
    value      double precision,
    state      text NOT NULL CHECK (state IN ('published', 'confirmed', 'unmatched')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, event_id)
);
CREATE INDEX IF NOT EXISTS attempts_state_idx ON pipeline.attempts (state);
CREATE INDEX IF NOT EXISTS attempts_event_idx ON pipeline.attempts (event_id);
