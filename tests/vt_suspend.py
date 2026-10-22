#!/usr/bin/env python3
"""Make VTScada unavailable and available again, reversibly and without the GUI.

Suspending the VTS.exe process stops it ingesting MQTT *and* stops it answering
REST, which is exactly what a dead SCADA host looks like to the pipeline -- and
unlike killing it, suspending is reversible headlessly (VTScada has no
start-from-CLI switch for an application).

    python tests/vt_suspend.py status
    python tests/vt_suspend.py suspend
    python tests/vt_suspend.py resume
    python tests/vt_suspend.py test --secs 5

Windows only: uses ntdll's NtSuspendProcess/NtResumeProcess, which needs no
administrator rights for a process you own.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

PROCESS_SUSPEND_RESUME = 0x0800


def find_vts() -> list[tuple[int, str]]:
    """Every running VTS.exe, as (pid, exe path)."""
    if os.name != "nt":
        return []
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq VTS.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout
    found = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "vts.exe":
            try:
                found.append((int(parts[1]), parts[0]))
            except ValueError:
                continue
    return found


def _nt(pid: int, suspend: bool) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
    try:
        fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        status = fn(handle)
        if status != 0:
            raise OSError(f"{'NtSuspendProcess' if suspend else 'NtResumeProcess'} "
                          f"returned NTSTATUS 0x{status & 0xFFFFFFFF:08X}")
    finally:
        kernel32.CloseHandle(handle)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["status", "suspend", "resume", "test"])
    ap.add_argument("--secs", type=float, default=5.0, help="for 'test': outage length")
    a = ap.parse_args(argv)

    if os.name != "nt":
        print("this helper is Windows-only")
        return 2

    procs = find_vts()
    if not procs:
        print("no VTS.exe is running -- start the VTScada application first")
        return 2
    print(f"VTS.exe pids: {[p for p, _ in procs]}")

    if a.action == "status":
        return 0

    if a.action in ("suspend", "resume"):
        for pid, _ in procs:
            _nt(pid, a.action == "suspend")
            print(f"{a.action}ed pid {pid}")
        return 0

    # test: suspend, confirm REST is dead, resume, confirm it is back
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from pipeline.vtclient import VTScadaClient

    client = VTScadaClient()
    print("before :", client.reachable())
    for pid, _ in procs:
        _nt(pid, True)
    time.sleep(1.0)
    print("suspended:", client.reachable())
    time.sleep(a.secs)
    t0 = time.time()
    for pid, _ in procs:
        _nt(pid, False)
    ok, detail = client.reachable()
    print(f"resumed : {ok} ({detail}) after {time.time() - t0:.2f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
