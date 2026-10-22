#!/usr/bin/env python3
"""Read VTScada's historian back over REST, and reconcile the queue against it.

This is the oracle the whole pipeline trusts. Nothing is reported as delivered
on the strength of an MQTT ack, so this is how you check by hand what the bridge
checks automatically.

    # what tags exist in the history?
    python tools/verify_history.py tables

    # the last 20 rows for one tag
    python tools/verify_history.py show AI01 --limit 20

    # a window, in epoch seconds
    python tools/verify_history.py show AI01 --since 1792033051 --until 1792033111

    # reconcile: is every delivered queue row actually in the historian?
    python tools/verify_history.py reconcile
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pipeline import db, settings  # noqa: E402
from pipeline.vtclient import VTScadaClient, VTScadaError  # noqa: E402


def cmd_tables(args):
    rows = VTScadaClient().tables()
    print("\n".join(rows))
    return 0


def cmd_show(args):
    client = VTScadaClient()
    print(f"# column: {client.tag_column(args.tag)}")
    t1 = args.until if args.until else __import__("time").time() + 60
    t0 = args.since if args.since else t1 - 3600
    rows = client.history(args.tag, t0, t1)
    if not rows:
        print(f"(no rows between {t0} and {t1})")
        return 0
    for ts, value in rows[-args.limit:]:
        print(f"{ts:.3f}  {value}")
    print(f"# {len(rows)} row(s) in window, showing last {min(len(rows), args.limit)}")
    return 0


def _compare_window(client, tag, rows):
    """(missing, mismatched, historian_rows, duplicate_timestamps) for one window.

    One tag, one bounded slice of time -- so neither the database read nor the
    historian read has to hold the whole history in memory.
    """
    is_bool = settings.VTS_TAGS.get(tag) == "bool"
    lo = min(r[0] for r in rows)
    hi = max(r[0] for r in rows)
    stored = client.history(tag, lo - 1, hi + 1)

    index = {}
    dupes = 0
    for ts, value in stored:
        if ts in index:
            dupes += 1
        index.setdefault(ts, value)

    missing = mismatched = 0
    for ts_s, value, _event_id in rows:
        want = (1.0 if value else 0.0) if is_bool else value
        hit = next((v for ts, v in index.items()
                    if abs(ts - round(ts_s, 3)) < settings.TS_TOL), None)
        if hit is None:
            missing += 1
        elif abs((hit or 0.0) - want) > max(settings.VALUE_TOL, 1e-9):
            mismatched += 1
    return missing, mismatched, len(stored), dupes


def cmd_reconcile(args):
    """Every row the bridge marked delivered must be readable in the historian.

    Reads the queue with a server-side cursor and the historian in bounded
    windows, so this works on a deployment with months of history rather than
    falling over on it.
    """
    client = VTScadaClient()
    conn = db.connect()
    total = missing = mismatched = 0
    duplicate_timestamps = {}
    per_tag = {}

    def flush(tag, rows):
        nonlocal total, missing, mismatched
        try:
            m, bad, stored_n, dupes = _compare_window(client, tag, rows)
        except VTScadaError as e:
            print(f"{tag}: READBACK FAILED for {len(rows)} row(s): {e}")
            return
        total += len(rows)
        missing += m
        mismatched += bad
        if dupes:
            duplicate_timestamps[tag] = duplicate_timestamps.get(tag, 0) + dupes
        agg = per_tag.setdefault(tag, {"delivered": 0, "missing": 0, "mismatched": 0})
        agg["delivered"] += len(rows)
        agg["missing"] += m
        agg["mismatched"] += bad
        print(f"{tag}: {len(rows)} delivered, {m} missing, {bad} mismatched, "
              f"{stored_n} rows in historian window")

    try:
        with conn, conn.cursor(name="reconcile") as cur:
            cur.itersize = 5000
            cur.execute(
                f"SELECT tag, event_id, EXTRACT(EPOCH FROM source_ts)::double precision, value "
                f"FROM {db.table('samples')} WHERE delivery_state='delivered' "
                "ORDER BY tag, source_ts")
            current_tag, chunk = None, []
            for tag, event_id, ts_s, value in cur:
                if tag != current_tag:
                    if chunk:
                        flush(current_tag, chunk)
                    current_tag, chunk = tag, []
                chunk.append((float(ts_s), float(value), event_id))
                if len(chunk) >= args.window:
                    flush(current_tag, chunk)
                    chunk = []
            if chunk:
                flush(current_tag, chunk)
    finally:
        conn.close()

    if total == 0:
        print("no delivered rows in the queue -- nothing to reconcile")
        return 0

    for tag, agg in sorted(per_tag.items()):
        print(f"  {tag}: {agg['delivered']} delivered, {agg['missing']} missing, "
              f"{agg['mismatched']} mismatched")
    print(f"\nTOTAL {total} delivered | missing {missing} | mismatched {mismatched}")
    if duplicate_timestamps:
        print(f"duplicate timestamps found: {duplicate_timestamps}")
    if missing or mismatched or duplicate_timestamps:
        print("VERDICT: FAIL -- the queue claims delivery for data the historian "
              "does not have, or has it more than once")
        return 1
    print("VERDICT: PASS -- every delivered row is present in the historian at its source time")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tables", help="list the tables the REST interface exposes")

    show = sub.add_parser("show", help="print history rows for one tag")
    show.add_argument("tag")
    show.add_argument("--since", type=float, default=0.0, help="epoch seconds")
    show.add_argument("--until", type=float, default=0.0, help="epoch seconds")
    show.add_argument("--limit", type=int, default=20)

    rec = sub.add_parser("reconcile", help="check every delivered queue row is in the historian")
    rec.add_argument("--window", type=int, default=20000,
                     help="rows compared per historian read; bounds memory on a "
                          "deployment with a long history")

    a = ap.parse_args(argv)
    return {"tables": cmd_tables, "show": cmd_show, "reconcile": cmd_reconcile}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
