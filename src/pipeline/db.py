"""Database access and schema management.

The schema lives in `schema.sql` next to this file and is applied idempotently
on service startup, so there is exactly one definition of it.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from contextlib import contextmanager

import psycopg2

from . import settings

log = logging.getLogger("db")

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect():
    """A connection hardened against a wedged WAN link.

    `statement_timeout` plus TCP keepalives mean a half-open connection fails
    loudly instead of hanging the caller forever -- a silent wedge looks exactly
    like "the historian stopped accepting data", which is the wrong thing to go
    and debug.
    """
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        dbname=settings.DB_NAME,
        connect_timeout=5,
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=3,
        options=f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}",
    )
    conn.autocommit = False
    return conn


@contextmanager
def cursor(conn=None):
    """Yield a cursor on `conn`, or on a short-lived connection if none given."""
    own = conn is None
    c = connect() if own else conn
    try:
        cur = c.cursor()
        yield cur
        if own:
            c.commit()
    except Exception:
        if own:
            c.rollback()
        raise
    finally:
        if own:
            c.close()


def apply_schema() -> None:
    """Create the queue tables if they are not there yet. Safe to run always."""
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        sql = f.read()
    # Substitute the schema name everywhere it appears -- both `pipeline.samples`
    # and the bare `CREATE SCHEMA IF NOT EXISTS pipeline;`. Matching on the word
    # rather than on "pipeline." is deliberate: the earlier dot-only replacement
    # left the CREATE SCHEMA statement naming the default, so any non-default
    # DB_SCHEMA failed at startup with "schema does not exist".
    if settings.DB_SCHEMA != "pipeline":
        sql = re.sub(r"\bpipeline\b", settings.DB_SCHEMA, sql)
    with cursor() as cur:
        cur.execute(sql)
        _migrate(cur)
    log.info("schema applied (%s)", settings.DB_SCHEMA)


def _migrate(cur) -> None:
    """Bring an existing deployment's tables up to the current definition.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
    changes to the schema need an explicit, idempotent step here.
    """
    cur.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name='samples' AND column_name='payload'",
        (settings.DB_SCHEMA,),
    )
    row = cur.fetchone()
    if row and row[0] == "jsonb":
        # A PLC payload is not necessarily JSON: a bare value is fine, but `ON`,
        # `DS1:V=21` or an empty body are not valid JSON and a jsonb column
        # rejects them outright -- turning a perfectly readable message into a
        # failed insert that is never acknowledged. Store it as text.
        cur.execute(f"ALTER TABLE {table('samples')} "
                    "ALTER COLUMN payload TYPE text USING payload::text")
        log.info("migrated %s.payload from jsonb to text", table("samples"))


def table(name: str) -> str:
    return f"{settings.DB_SCHEMA}.{name}"


def utc_from_ms(ms: int) -> datetime.datetime:
    """Epoch milliseconds -> timezone-aware UTC datetime.

    Used for the `source_ts timestamptz` column. psycopg2 would store a naive
    datetime using the session timezone, so the tz must be explicit or the
    PLC's timestamp is silently shifted.
    """
    return datetime.datetime.fromtimestamp(int(ms) / 1000, datetime.timezone.utc)


def database_size() -> int:
    """Size of the current database in bytes (for storage reporting)."""
    with cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def backlog(conn=None) -> int:
    """Rows waiting to be confirmed by the historian."""
    with cursor(conn) as cur:
        cur.execute(f"SELECT count(*) FROM {table('samples')} WHERE delivery_state='pending'")
        return cur.fetchone()[0]


def prune(retention_days: int, batch: int = 5000) -> dict[str, int]:
    """Delete delivered/audit rows older than `retention_days`.

    `pending` rows are NEVER deleted: they are the work still owed to the
    historian. Deletes run in bounded batches inside one transaction so a large
    backlog is not held in a single long lock.

    Returns {table: rows_deleted}. `0` days means keep forever.
    """
    out: dict[str, int] = {}
    if retention_days <= 0:
        return out
    # Age is measured on arrival (received_ts), not the PLC's clock: a device
    # with a slow or wrong clock must not have its rows pruned immediately.
    targets = (
        ("samples", "received_ts", "delivery_state='delivered' AND "),
        ("attempts", "created_at", ""),
        ("batches", "published_at", ""),
        ("quarantine", "created_at", ""),
    )
    with cursor() as cur:
        for name, ts_col, extra in targets:
            total = 0
            while True:
                cur.execute(
                    f"DELETE FROM {table(name)} WHERE ctid IN ("
                    f"  SELECT ctid FROM {table(name)} "
                    f"  WHERE {extra}{ts_col} < now() - make_interval(days => %s) "
                    f"  LIMIT %s)",
                    (retention_days, batch),
                )
                n = cur.rowcount
                total += n
                if n < batch:
                    break
            out[name] = total
    return out


def quarantine(event_id, reason, topic, payload) -> None:
    """Record an undecodable or implausible message. Never raises: losing the
    quarantine record must not take down the collector."""
    try:
        with cursor() as cur:
            cur.execute(
                f"INSERT INTO {table('quarantine')} (event_id, reason, topic, payload) "
                "VALUES (%s, %s, %s, %s)",
                (event_id, reason, topic, payload if isinstance(payload, str)
                 else json.dumps(payload, default=str)),
            )
        log.warning("quarantined event=%s reason=%s", event_id, reason)
    except Exception as e:  # noqa: BLE001 - deliberately broad
        log.error("could not write quarantine record for event=%s: %s", event_id, e)
