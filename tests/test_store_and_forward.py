#!/usr/bin/env python3
"""End-to-end store-and-forward test against a real VTScada historian.

What it proves, in order:

  P1  baseline     samples flow through the pipeline and are confirmed by readback
  P2  outage       VTScada is suspended; rows accumulate and NOTHING is confirmed
  P3  catch-up     VTScada returns; the backlog drains while the PLC keeps publishing
  P4  quiesce      the PLC stops; the backlog reaches exactly zero
  R   reconcile    every delivered row is in the historian at its own source
                   timestamp and value, with no duplicate timestamps

Usage (against a running pipeline, e.g. `docker compose up -d`):

    python tests/test_store_and_forward.py --run-simulator --outage-secs 60

Or fully self-contained on this machine, starting the services itself:

    python tests/test_store_and_forward.py --start-services --run-simulator --outage-secs 60

Windows only for the outage phase (it suspends VTS.exe); the rest is portable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pipeline import db, settings  # noqa: E402
from pipeline.vtclient import VTScadaClient, VTScadaError  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def queue_stats() -> dict:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FILTER (WHERE delivery_state='pending'), "
                f"       count(*) FILTER (WHERE delivery_state='delivered'), count(*) "
                f"FROM {db.table('samples')}")
            pending, delivered, total = cur.fetchone()
            cur.execute(f"SELECT coalesce(sum(confirmed),0), coalesce(sum(unmatched),0), "
                        f"count(*) FROM {db.table('batches')}")
            confirmed, unmatched, batches = cur.fetchone()
    finally:
        conn.close()
    return {"pending": pending, "delivered": delivered, "total": total,
            "confirmed_events": int(confirmed), "unmatched_events": int(unmatched),
            "batches": batches}


def suspend_vts(suspend: bool) -> bool:
    action = "suspend" if suspend else "resume"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "vt_suspend.py"), action],
                       capture_output=True, text=True)
    log(f"vt_suspend {action}: rc={r.returncode} {r.stdout.strip()}")
    return r.returncode == 0


def reconcile(vts: VTScadaClient) -> dict:
    """Check every delivered queue row against the historian, and look for dupes."""
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT tag, event_id, EXTRACT(EPOCH FROM source_ts)::double precision, value "
                f"FROM {db.table('samples')} WHERE delivery_state='delivered' "
                "ORDER BY tag, source_ts")
            rows = cur.fetchall()
    finally:
        conn.close()

    by_tag: dict[str, list] = {}
    for tag, event_id, ts_s, value in rows:
        by_tag.setdefault(tag, []).append((float(ts_s), float(value), event_id))

    report = {"expected": 0, "missing": 0, "mismatched": 0,
              "duplicate_timestamps": {}, "per_tag": {}}
    for tag, events in sorted(by_tag.items()):
        is_bool = settings.VTS_TAGS.get(tag) == "bool"
        lo = min(e[0] for e in events)
        hi = max(e[0] for e in events)
        stored = vts.history(tag, lo - 1, hi + 1)

        seen: dict[float, int] = {}
        for ts, _v in stored:
            seen[ts] = seen.get(ts, 0) + 1
        dupes = {ts: n for ts, n in seen.items() if n > 1}

        missing = mismatched = 0
        for ts_s, value, _event_id in events:
            want = (1.0 if value else 0.0) if is_bool else value
            hit = next((v for ts, v in stored if abs(ts - round(ts_s, 3)) < settings.TS_TOL), None)
            if hit is None:
                missing += 1
            elif abs((hit or 0.0) - want) > max(settings.VALUE_TOL, 1e-9):
                mismatched += 1
        report["expected"] += len(events)
        report["missing"] += missing
        report["mismatched"] += mismatched
        if dupes:
            report["duplicate_timestamps"][tag] = dupes
        report["per_tag"][tag] = {"delivered": len(events), "missing": missing,
                                 "mismatched": mismatched, "historian_rows": len(stored)}
        log(f"  {tag}: {len(events)} delivered, {missing} missing, {mismatched} mismatched, "
            f"{len(stored)} rows in historian")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outage-secs", type=float, default=60.0)
    ap.add_argument("--baseline-secs", type=float, default=45.0)
    ap.add_argument("--catchup-timeout", type=float, default=600.0)
    ap.add_argument("--quiesce-timeout", type=float, default=300.0)
    ap.add_argument("--run-simulator", action="store_true",
                    help="start tools/simulate_plc.py for the duration of the test")
    ap.add_argument("--start-services", action="store_true",
                    help="start the collector and bridge as local subprocesses")
    ap.add_argument("--no-outage", action="store_true",
                    help="skip the suspend/resume phase (e.g. on a non-Windows host)")
    ap.add_argument("--run-id", default=time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime()))
    ap.add_argument("--topic-prefix", default=None,
                    help="topic root for the simulator; defaults to the first level of "
                         "PLC_TOPIC, so the simulator publishes where the collector listens")
    a = ap.parse_args(argv)

    results_dir = os.path.join(ROOT, "tests", "results", a.run_id)
    os.makedirs(results_dir, exist_ok=True)
    report_path = os.path.join(results_dir, "report.json")
    children: list[subprocess.Popen] = []

    def spawn(name, cmd):
        out = open(os.path.join(results_dir, f"{name}.log"), "w")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
        children.append(p)
        log(f"started {name} (pid {p.pid})")
        return p

    result = {"run_id": a.run_id, "config": settings.describe(), "phases": {}, "verdict": "FAIL"}
    try:
        log(f"config: {json.dumps(settings.describe())}")
        db.apply_schema()
        vts = VTScadaClient()

        ok, detail = vts.reachable()
        log(f"VTScada REST reachable={ok} ({detail})")
        if not ok:
            log("FAIL: VTScada is not answering. Start the VTScada application first.")
            result["phases"]["preflight"] = {"vts_reachable": False, "detail": detail}
            return 1
        result["phases"]["preflight"] = {"vts_reachable": True}

        if a.start_services:
            spawn("collector", [sys.executable, "-u", "-m", "pipeline.collector"])
            spawn("bridge", [sys.executable, "-u", "-m", "pipeline.bridge"])
            time.sleep(12)  # BIRTH + warm-up

        sim = None
        if a.run_simulator:
            max_seq = 0
            conn = db.connect()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(f"SELECT coalesce(max(seq),0) FROM {db.table('samples')}")
                    max_seq = cur.fetchone()[0]
            finally:
                conn.close()
            # Start above MAX(seq) or every sample is rejected as a duplicate input
            # and reconciliation would report zero expected events -- which looks
            # exactly like a pass.
            log(f"simulator starting at seq_base={max_seq}")
            topic_prefix = a.topic_prefix or settings.PLC_TOPIC.split("/")[0].strip("+")
            sim = spawn("simulator", [sys.executable, "-u", os.path.join("tools", "simulate_plc.py"),
                                      "--duration", "0", "--rate", "1", "--seq-base", str(max_seq),
                                      "--topic-prefix", topic_prefix])

        # ---- P1 baseline
        log(f"P1 baseline: watching for confirmed delivery for {a.baseline_secs:.0f}s")
        t0 = time.time()
        baseline_start = queue_stats()
        while time.time() - t0 < a.baseline_secs:
            time.sleep(5)
            s = queue_stats()
            log(f"  pending={s['pending']} delivered={s['delivered']} "
                f"confirmed={s['confirmed_events']}")
            if s["confirmed_events"] > baseline_start["confirmed_events"]:
                break
        p1 = queue_stats()
        baseline_ok = p1["confirmed_events"] > baseline_start["confirmed_events"]
        result["phases"]["P1_baseline"] = {"ok": baseline_ok, "stats": p1}
        log(f"P1 {'OK' if baseline_ok else 'FAIL'}: confirmed events "
            f"{baseline_start['confirmed_events']} -> {p1['confirmed_events']}")

        # ---- P2 outage
        if not a.no_outage:
            log(f"P2 outage: suspending VTScada for {a.outage_secs:.0f}s")
            if not suspend_vts(True):
                log("FAIL: could not suspend VTScada")
                result["phases"]["P2_outage"] = {"ok": False, "detail": "suspend failed"}
                return 1
            ok, detail = vts.reachable()
            log(f"  during outage VTScada reachable={ok} ({detail})")
            # Capture the "before" numbers AFTER the outage is actually in effect.
            # Suspend takes a moment to land, and a batch confirmed in that gap
            # would otherwise be counted as a confirmation during the outage.
            before = queue_stats()
            t0 = time.time()
            peak = before["pending"]
            while time.time() - t0 < a.outage_secs:
                time.sleep(10)
                s = queue_stats()
                peak = max(peak, s["pending"])
                log(f"  t={time.time() - t0:5.0f}s pending={s['pending']} "
                    f"delivered={s['delivered']} confirmed={s['confirmed_events']}")
            during = queue_stats()
            grew = during["pending"] > before["pending"]
            quiet = during["confirmed_events"] == before["confirmed_events"]
            result["phases"]["P2_outage"] = {
                "ok": grew and quiet, "backlog_before": before["pending"],
                "backlog_peak": peak, "backlog_after": during["pending"],
                "confirmed_before": before["confirmed_events"],
                "confirmed_after": during["confirmed_events"],
                "rest_reachable": ok}
            log(f"P2 {'OK' if grew and quiet else 'FAIL'}: backlog {before['pending']} -> "
                f"{during['pending']} (peak {peak}); confirmed {before['confirmed_events']} -> "
                f"{during['confirmed_events']} (must not change); REST reachable={ok}")

            # ---- P3 catch-up
            log("P3 catch-up: resuming VTScada while the PLC keeps publishing")
            t0 = time.time()
            suspend_vts(False)
            ok, detail = vts.reachable()
            log(f"  REST back after {time.time() - t0:.2f}s: {ok} ({detail})")
            floor = None
            while time.time() - t0 < a.catchup_timeout:
                time.sleep(5)
                s = queue_stats()
                if floor is None or s["pending"] < floor:
                    floor = s["pending"]
                if s["pending"] <= max(4, before["pending"]):
                    break
            result["phases"]["P3_catchup"] = {
                "ok": floor is not None and floor <= max(4, before["pending"]),
                "catchup_sec": round(time.time() - t0, 1), "backlog_floor": floor,
                "stats": queue_stats()}
            log(f"P3: catch-up {result['phases']['P3_catchup']['catchup_sec']}s, "
                f"backlog floor {floor}")

        # ---- P4 quiesce
        log("P4 quiesce: stopping the source and draining to zero")
        if sim is not None:
            sim.terminate()
            sim.wait(timeout=30)
            log("  simulator stopped")
        t0 = time.time()
        final = queue_stats()
        while time.time() - t0 < a.quiesce_timeout:
            final = queue_stats()
            if final["pending"] == 0:
                break
            time.sleep(2)
        reached_zero = final["pending"] == 0
        result["phases"]["P4_quiesce"] = {
            "ok": reached_zero, "drain_sec": round(time.time() - t0, 1), "stats": final}
        log(f"P4 {'OK' if reached_zero else 'FAIL'}: pending {final['pending']} after "
            f"{result['phases']['P4_quiesce']['drain_sec']}s")

        # ---- R reconcile
        log("R reconcile: reading every delivered row back out of the historian")
        rec = reconcile(vts)
        result["phases"]["R_reconcile"] = rec
        log(f"R: expected={rec['expected']} missing={rec['missing']} "
            f"mismatched={rec['mismatched']} duplicate_timestamps={rec['duplicate_timestamps']}")

        ok = (rec["missing"] == 0 and rec["mismatched"] == 0
              and not rec["duplicate_timestamps"]
              and rec["expected"] > 0
              and result["phases"]["P1_baseline"]["ok"]
              and result["phases"]["P4_quiesce"]["ok"]
              and (a.no_outage or result["phases"]["P2_outage"]["ok"]))
        result["verdict"] = "PASS" if ok else "FAIL"
        log(f"VERDICT: {result['verdict']}")
        return 0 if ok else 1
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        log(f"report: {report_path}")


if __name__ == "__main__":
    sys.exit(main())
