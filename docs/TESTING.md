# Testing

The tests are only meaningful if you know **what** each one proves and what it
does not. Read the "does not prove" column before quoting a pass.

## Levels

| Level | What it proves | Needs | Command |
|---|---|---|---|
| Unit — decoder | message → sample, timestamps, dedup ids | Python + protobuf | `python tests/test_decoder.py` |
| Unit — payload | Sparkplug contract, enums, bdSeq, throttle | Python + paho + psycopg2 + protobuf | `python tests/test_sparkplug_payload.py` |
| Integration | collector/bridge talk to a real broker + DB | the stack running | `pipeline status`, logs |
| End-to-end (simulator) | full path into a **real** VTScada historian, reconciled | running stack + VTScada + simulator | `tests/test_store_and_forward.py --outage-secs 60` |
| Outage / recovery | backlog persists, nothing falsely confirmed, drains, reconciles | as above + `vt_suspend.py` (Windows) | same, keeping the outage phase |
| Clean install | a novice can install on a fresh machine | a clean Pi/VM | `deploy/install.sh`, then §5 of INSTALL |
| Backup / restore | restored pending data is intact | a backup | `pipeline backup` → `pipeline restore` |

## Run the unit tests

```bash
cd /opt/mqtt-sparkplug-vtscada
python tests/test_decoder.py            # expect "23/23 passed"
python tests/test_sparkplug_payload.py  # expect "12/12 passed"
```

No network, no database. They are the fastest gate and run in CI.

**Requires** `protobuf>=7.35,<8` — the checked-in codec validates the runtime
version at import and raises `VersionError` if it is older. This is a real
failure mode: a service that cannot import the codec will not start.

## Run the end-to-end test

> **The database port is no longer published** (security baseline). The test
> connects to PostgreSQL directly, so it must run **inside** the Compose network,
> not from your laptop:

```bash
# from the Pi, with the stack up and VTScada reachable
docker compose exec -T bridge python tests/test_store_and_forward.py --outage-secs 60
```

The outage phase suspends `VTS.exe` and only works on Windows, so unless you are
running inside a Windows host that owns the VTScada process, use `--no-outage` for
the delivery/reconcile part and run the outage phase from the SCADA PC with an SSH
tunnel to PostgreSQL.

What a PASS means:

* baseline confirmed delivery works;
* during the outage `confirmed` does not increase and `pending` grows;
* after recovery the backlog drains;
* **reconcile** reads every delivered row back and reports 0 missing,
  0 mismatched, 0 duplicate timestamps.

It writes `tests/results/<run-id>/report.json` and per-process logs.

## What each level does NOT prove

| Claim | Not proved by |
|---|---|
| "Works with the real PLC" | the simulator — it emits a different shape |
| "Preserves PLC timestamps" | anything, for a PLC that sends no timestamp |
| "Survives a power cut" | a process restart; needs the Pi and a real outage |
| "Starts after reboot" | `docker compose up`; needs one full reboot |
| "A technician can install it" | a developer's machine |
| "Restores correctly" | a backup that was never restored |

## Claims that require a specific environment

| Claim | Environment |
|---|---|
| clean install follows the guide | clean Raspberry Pi / VM (owner authorised `your-pi`) |
| automatic start after reboot | the Pi, one reboot |
| outage → backlog → recovery → reconcile | real VTScada (owner authorised) |
| real PLC timestamp limitation | the physical CLICK PLUS via the Pi `eth0` (authorised) |
| credential / firewall baseline | the Pi and the site's VTScada account |

## Current status (honest)

Executed on **2026-10-19** on `your-pi` (Raspberry Pi), in an isolated image
built from this branch (`python:3.12-slim`, protobuf 7.36.2), with the live
broker/database and the real VTScada:

| Check | Result |
|---|---|
| `tests/check_settings_forwarded.py` | **PASS** — 52 read, 52 forwarded, none dropped |
| `tests/test_decoder.py` | **PASS — 23/23** |
| `tests/test_sparkplug_payload.py` | **PASS — 12/12** |
| `vtclient.probe()` vs real VTScada, correct password | `ok` |
| `vtclient.probe()` vs real VTScada, wrong password | `credentials-rejected` |
| `vtclient.probe()` vs dead port | `unreachable` |
| `db.prune(3650)` against the live queue | executed; deleted **0** rows (nothing that old) |
| new bridge, `--standby-only` | starts, applies schema, prunes, probes `ok`, publishes nothing |
| `deploy/pipeline status` vs live stack | containers + queue counts + heartbeat correct |

Caught by this exercise and fixed: the deploy scripts were committed **100644**
and `git archive` on the Windows checkout produced **CRLF** scripts, so the
shebang failed on Linux. `.gitattributes` (`eol=lf`) and `+x` in the index fix it;
the re-extracted archive was verified LF and `-rwxr-xr-x`.

Still **unverified**:

* the full outage → recovery → reconcile on *this* branch (it requires recreating
  the live containers, i.e. an upgrade window);
* a clean install of the whole stack on a fresh machine (the live stack holds
  ports 1883/18083/5432);
* automatic start after a real reboot;
* backup → restore with pending data;
* the real CLICK PLUS path since these changes.

Stored artifact from the previous shape: `tests/results/verify-pass/report.json`
(770 events reconciled, 0 missing / 0 mismatched / 0 duplicates through a 60 s
outage).

## Observed on the live Pi (not a test)

The bridge recovered on 2026-10-19 after VTScada had been unreachable for
**316,323.8 s (~3.66 days)**. The queue held **~188,000** pending rows and was
draining at **~41 samples/s** (measured 174,166 → 172,946 over 30 s). That is
real-world evidence of the store-and-forward guarantee, and a capacity datum: a
site whose PLC produces more than ~41 samples/s would not drain this backlog with
`BRIDGE_BATCH=50`.

## Real outage test on the upgraded stack (2026-10-19)

The branch was deployed over the live stack on `your-pi` (volumes preserved) and
VTScada was suspended for ~60 s on the SCADA PC (the real `VTS.exe`, pids [2840]).

| Phase | Observed | Result |
|---|---|---|
| before | pending 155,184; cumulative `confirmed` 168,004 | baseline |
| during | `confirmed` **froze** at 168,004; the in-flight batch stayed pending (`published=50 confirmed=0 unmatched=50`); `STANDBY entered -- … unreachable: timed out`; local REST refused | **PASS** — nothing falsely confirmed |
| recovery | `reachable again after 35.9s`; re-BIRTH with the **same** `bdSeq=32`; delivery resumed at 50/50 confirmed | **PASS** |
| reconcile | 600 rows confirmed in the last 12 min, checked against the historian: `missing=0 mismatched=0` | **PASS** |
| data safety | 819 samples received during/after the outage were `pending`, none lost | **PASS** |

The first batch after the re-BIRTH did not confirm (`unmatched=50`) and was
retried ~30 s later — the documented post-BIRTH warm-up behaviour, not a fault.

**Live issue found:** 63 pending rows carry tag `'1'`, which is not in
`VTS_TAGS`. The ALARM names them correctly; they will never be delivered and will
keep `pending` above zero until the PLC stops publishing that topic or the tag is
configured. This is the anti-starvation guard working, but it needs an operator
decision.

## Phase 5 fixes verified (2026-10-19, `your-pi`)

| Fix | Check | Result |
|---|---|---|
| F8 multi-tag fairness | `fetch_pending(50)` against the live queue | returns a **full** batch of 50 and lists unknown tag `['1']`; tags are interleaved by per-tag row number, so no tag can fill every batch |
| F9 bdSeq fail-fast | bridge started with `STATE_DIR=/proc/definitely-not-writable` | logs `ALARM could not persist bdSeq … refusing to run` and **exits 2**; publishes nothing |
| F2 storage reporting | `pipeline status` | `database: 175 MB`, `queue volume: 24% used, 21G free`, warning at ≥90% |
| F2 heartbeat | bridge log | `heartbeat backlog=… db_size_mb=175 …` |
| regression | unit tests after the changes | decoder **23/23**, payload **12/12** |
| live deploy | rebuilt `:local` image and recreated services | `bdSeq=33`, batches `published=50 confirmed=50 unmatched=0`, 550/550 confirmed |

The fairness change was validated read-only before deployment; the live queue has
only one backlogged tag, so the interleaving itself is exercised by the SQL shape
rather than by two simultaneously backlogged tags. That two-tag case belongs in
the Phase 6 integration test.

## Backup / restore verified (2026-10-19, `your-pi`)

`pipeline backup /root/backups/verify1` produced `queue.sql` (73 MB), `bdseq`,
`emqx-data.tar` (215 MB, `tar -tf` valid) and a manifest recording `pendingat:
119184`.

Restore was proven **without touching the live queue**: the dump was loaded into a
throwaway database `restore_test`, compared, then dropped.

| | delivered | pending | total |
|---|---|---|---|
| restored snapshot | 202,554 | 120,634 | **323,188** |
| live at compare time | 205,804 | 117,384 | **323,188** |

Identical totals, with 3,250 rows having moved pending→delivered in between — the
dump is a consistent snapshot and the restore is faithful. `bdseq` restored as 33.

Not yet tested: a full `pipeline restore` over the live schema (it stops the
stack and replaces `pipeline`), and a restore onto a *different* machine.

## Reboot / auto-start verified (2026-10-19, `your-pi`)

`reboot` was issued with the stack running. After ~75 s:

| Check | Result |
|---|---|
| host returned | `uptime -p` → `up 0 minutes` |
| containers auto-started | `bridge`, `collector`, `emqx`, `timescaledb` all `Up` with no manual step |
| cold broker handled | bridge logged `broker emqx:1883 not ready … retrying` then `MQTT connected rc=Success` once EMQX was healthy — no crash loop |
| bdSeq survived | `NBIRTH published (bdSeq=34)` (33 → 34 across the reboot) |
| delivery resumed | `published=50 confirmed=50 unmatched=0`, backlog draining (116,384 → 116,134) |

Docker is `systemctl is-enabled docker` → `enabled`, and every service is
`restart: unless-stopped`, so auto-start does not depend on a compose file being
run by hand.

Still unverified: a **clean install on a fresh machine** (this Pi's ports are held
by the running stack), and a full `pipeline restore` over the live schema.

Do not report a test as passing because it is documented. Record pass / fail /
skipped / blocked-by-environment, and name the environment.
