# Deployment and maintenance audit

Phase 1 audit of this repository, produced before any implementation change.
Baseline: branch `main`, commit `5ab73d1` (2026-10-15), 31 tracked files, clean
tree. Nothing in the repository was changed to produce this document beyond
adding it.

**Reading this in the public repo.** Two things cited as evidence are not in the
tree. `DEPLOY.md` was split into `docs/INSTALL.md` and `docs/EXAMPLE_DEPLOYMENT.md`,
so the `DEPLOY.md:…` line numbers below are historical, not clickable. And
`tests/results/` is gitignored, so the `[V]` run artifacts are held by the owner
rather than published: a reader of this repository cannot re-verify a `[V]` claim.

## How each claim is known

Every statement below is labelled:

* **[V] Verified by an executed test** — a stored run artifact in `tests/results/`.
* **[C] Confirmed by inspection** — read from the current source or config.
* **[D] Documented but not independently verified** — a README/DEPLOY claim with
  no supporting artifact.
* **[I] Inferred** — requires validation in a real environment.
* **[L] Known limitation / unresolved risk.**

## What was run, and what could not be run

Read: every tracked file that carries behaviour (README, DEPLOY, ARCHITECTURE,
`docker-compose.yml`, `Dockerfile`, `Makefile`, `pyproject.toml`,
`requirements.txt`, `.env.example`, `.git*ignore`, `src/pipeline/*`,
`src/pipeline/schema.sql`, `tests/*`, `tools/*`, `deploy/*`,
`docs/vtscada-wire-contract.md`); `git log/status/branch/tag/ls-files`; the
ignored artifact directory `tests/results/`.

Attempted and **blocked** (recorded, not worked around — installing to unblock
was out of scope for the audit):

* `python tests/test_decoder.py` — the installed protobuf runtime is 6.33.6;
  the checked-in codec `sparkplug_b_pb2.py` requires gencode 7.35.1, so it raises
  `google.protobuf.runtime_version.VersionError` at import. No test ran.
* `python tests/test_sparkplug_payload.py` — same, plus `paho-mqtt` and
  `psycopg2` are absent.
* The end-to-end store-and-forward test and any `docker compose` command — no
  Docker on the audit machine, and the test needs a live broker, TimescaleDB and
  a real VTScada.

No destructive or service-affecting command was run. No live PLC, broker,
database or VTScada was touched.

## Inventory

| Component | Where | Role |
|---|---|---|
| emqx | container | broker; durable sessions; session expiry 7 d; dashboard :18083 |
| timescaledb | container | durable queue in schema `pipeline`; :5432 published |
| collector | container | MQTT in → decode → INSERT **before** PUBACK; knows nothing of VTScada |
| bridge | container | pending rows → one Sparkplug B NDATA payload → VTScada; marks delivered only after REST readback |
| VTScada | external (SCADA PC) | consumes Sparkplug B over MQTT; read back over REST `SQLQuery` |

Persistent data/state: `timescale-data`, `emqx-data`, `emqx-log` volumes;
`bdseq` in the `bridge-state` volume at `STATE_DIR=/var/lib/pipeline`; secrets and
config in `.env` (gitignored; no secret found in any tracked file). Container runs
as non-root uid 10001.

Dependencies: `paho-mqtt>=2.1,<3`, `psycopg2-binary>=2.9,<3`, `protobuf>=7.35,<8`,
base image `python:3.12-slim`. Images pinned to exact tags; Python deps are ranges.

Schema: `pipeline.samples` (`UNIQUE(device_id,seq,tag)`, `delivery_state ∈
{pending,delivered}`, `attempts`, `last_error`), partial pending index,
`pipeline.quarantine`, `pipeline.checkpoints` (written, never read),
`pipeline.batches`, `pipeline.attempts`. Applied idempotently at startup; a
`jsonb → text` payload migration exists.

## Working versus unverified

**[V] Verified by stored artifact** — `tests/results/verify-pass/report.json`:
verdict PASS; P1 baseline OK; P2 outage OK (confirmed 542 → 542, backlog 43 →
170, REST unreachable); P3 catch-up 28.1 s; P4 drained to 0 in 5.9 s; reconcile
expected=770 missing=0 mismatched=0 duplicate_timestamps={}. `verify-pass/bridge.log`
corroborates STANDBY entry, re-NBIRTH with the same `bdSeq`, and 50/50 batches.
`tests/results/state/bdseq` = 35 and `local-verify/state` = 131 show bdSeq
persisted across restarts.

**[C] Confirmed by inspection** — PUBACK withheld until commit; manual ack set
before `loop_start`; DB work off the MQTT network thread; `delivery_state` written
only by the bridge; idempotent insert and timestamp-derived `seq`; datatype enums
4/10/11 with matching value fields; both `name` and `alias` set; trailing
duplicate element per tag group; unknown-tag rows excluded from the batch query;
retry throttle; stall alarm keyed off the last **confirmed** event;
`statement_timeout` + TCP keepalives; quarantine path.

**[D] Reported but not verifiable from the repo** — the real-CLICK-PLUS run
(`plc/1/testVal`, 352 delivered), the "118/118 reconciled from the Pi" run, and
the predecessor "1092/1092 through a 300 s outage". No artifact for any of them
exists under `tests/results/`. The unit-test counts ("23/23", "12/12") match the
test functions present, but the tests could not be executed here.

## Findings

Class: R = Required, C = Recommended, O = Optional. Severity: H / M / L.

| ID | Sev | Class | Finding | Evidence |
|---|---|---|---|---|
| F1 | H | R | Insecure-by-default stack: anonymous broker, DB port 5432 published, dashboard password `public1234`, empty `VTS_REST_PASSWORD`, plaintext MQTT. | `docker-compose.yml:70-74,107-108`; `.env.example:11-15,30-32,75`; `DEPLOY.md:606-623` [C] |
| F2 | H | R | No retention/pruning and no storage-exhaustion detection; queue and audit tables grow without bound. | `schema.sql` (no TTL); `ARCHITECTURE.md:114-117`; `README.md:301-305` [C] |
| F3 | H | R | Wrong/absent VTScada REST password is indistinguishable from "historian down" — `reachable()` returns False on 401, so the bridge sits in STANDBY and the backlog grows. | `vtclient.py:83-89`; `tests/results/local-verify/bridge.log` ("STANDBY … 401 Unauthorized") [V/C] |
| F4 | H | R | 22 of the 51 settings the code reads are not forwarded by `docker-compose.yml`, so documented `.env` changes silently do nothing — including `PLC_TOPIC_TAG_LEVELS` and `PLC_TAG_MAP` (used in DEPLOY step 8c) and `MQTT_SESSION_EXPIRY_SEC`. | settings-vs-compose diff (see design doc §5) [C] |
| F5 | H | R | Installation requires a private GitHub repo, an SSH deploy key and `git pull`; no self-contained/offline artifact. | `DEPLOY.md:87-129,580` [C] |
| F6 | H | R | No operational interface (install/status/logs/backup/restore/update/uninstall) and no explicit boot-start unit. | (no such scripts exist) [C] |
| F7 | H | R | The E2E test's outage phase is racy and its stored evidence contradicts itself: `verify-full/report.json` records P2 `ok:false` with confirmed 100→150, while `verify-full-runner.log` records "P2 FAIL … confirmed stayed 150"; the later `verify-pass` run PASSES. | `tests/results/verify-full/{report.json,..}` [V] |
| F8 | M | C | Batch fairness: `ORDER BY tag, seq LIMIT n` lets the alphabetically-first tag fill every batch and starve later tags during recovery. | `bridge.py:292-299` [C] |
| F9 | M | C | An unwritable `STATE_DIR` only warns, then the stale-BIRTH failure presents as "tags never update". | `bridge.py:100-107` [C] |
| F10 | M | R | Not reproducible: Python deps are ranges, base image not digest-pinned, and the codec hard-fails on any runtime older than 7.35.1 (observed: 6.33.6 → VersionError). | `requirements.txt`; `Dockerfile:5`; observed import failure [C/V] |
| F11 | M | C | No CI, LICENSE, CHANGELOG, tags or release procedure. | no `.github`; `git tag -l` empty [C] |
| F12 | M | C | Doc gaps and location drift: ARCHITECTURE at repo root, DEPLOY bound to one site's IPs/tailnet/MAC, no operations/backup/config/testing set. | root vs `docs/`; `DEPLOY.md:57-71,376` [C] |
| F13 | M | C | Collector work queue bounded at 10000, drops on full with only a log line under a sustained DB outage. | `collector.py:41,191-192` [C] |
| F14 | L | C | Running outside Docker is misconfigured by default (`DB_USER`/`DB_NAME`=`pipeline`, `DB_PASSWORD` empty vs compose `emqx`). | `settings.py:137-139` vs `docker-compose.yml:29-31` [C] |
| F15 | M | O | Unverified end-to-end claims carry no artifacts; only the simulator path is stored. | `tests/results/` [V/D] |
| F16 | — | L | No clock-skew detection beyond a coarse range check. | `decoder.valid_timestamp` [C] |
| F17 | — | L | Ordering is per-tag/per-batch, not global across tags. | `bridge.py:271-316` [C] |
| F18 | — | L | Exactly-once is correctly not claimed; safe retry relies on VTScada coalescing equal timestamps. | `ARCHITECTURE.md:168-170` [C] |
| F19 | L | — | EMQX 6 image is `emqx/emqx-enterprise`, which since 5.9.0 carries the unified feature set under BSL 1.1 and a bundled single-node Community License (limited commercial use). No image change is needed and no commercial key is required for a single node; the only constraint is not to run it as a cluster without one. **Corrected after Phase 1** (see `DECISIONS.md` ADR-002). | `docker-compose.yml:67`; emqx licensing FAQ [C] |
| F20 | L | O | TimescaleDB is used as a plain PostgreSQL table (no hypertable). | `ARCHITECTURE.md:179-181` [C] |

## Invariants that must be preserved

1. PUBACK withheld until the row is committed.
2. A row becomes `delivered` only after its (timestamp, value) is read back from
   the historian; `delivery_state` is written by the bridge alone, in the same
   transaction as the readback result.
3. Idempotent input via `UNIQUE(device_id, seq, tag)`; `seq` derived from the
   source timestamp when the PLC sends none.
4. Nothing is published while REST is unreachable (STANDBY).
5. Sparkplug contract: metric name repeated per tag, one element per sample, each
   with its own source timestamp; one trailing duplicate element per tag group;
   enums 4/10/11 with the matching value field; both `name` and `alias` set.
6. `bdSeq` persisted, incremented, wrapping at 255; recovery re-BIRTH reuses the
   same `bdSeq`.
7. Only tags in `VTS_TAGS` are published; unknown pending tags are excluded and
   ALARMed rather than starving the head of the queue.
8. Undecodable/implausible-timestamp messages are quarantined; quarantine is
   terminal.
9. `topic_tag` and `sparkplug` decoders fall back to **arrival time** when the
   message has no timestamp — the original-timestamp property is then **lost**
   and must always be documented as lost. `json` treats a missing timestamp as
   undecodable.
10. Retry throttle, and stall alarm keyed off the last **confirmed** event.
11. DB `statement_timeout` + keepalives; idempotent schema with the `jsonb → text`
    migration; non-root container; `bdSeq` in a mounted volume.
12. `tools/verify_history.py reconcile` reads every delivered row back and reports
    missing / mismatched / duplicates separately; PASS requires zero of each.

## Unverified assumptions

* Real-CLICK-PLUS delivery, "118/118", "1092/1092" — no artifacts [D].
* Clean-machine / clean-Pi install, first-run image pulls, buildx ≥ 0.17
  workaround, reboot auto-start — never exercised [I].
* VTScada coalescing equal timestamps wholesale (relied on for safe retry) —
  evidenced once, not re-verifiable here [I].
* That the community EMQX image provides the required durable sessions once the
  image is switched — see `DECISIONS.md` ADR-002.

## Claims needing a specific environment

| Claim | Needs |
|---|---|
| Clean install follows the guide | a clean Raspberry Pi / fresh OS (Q3: `your-pi` authorised) |
| Automatic start after reboot | the Pi, one full reboot |
| Outage + recovery + reconcile | real VTScada (Q2: authorised) |
| Real PLC timestamp limitation | the physical CLICK PLUS via the Pi's `eth0` (Q9: authorised) |
| Credential/network baseline | the Pi plus the site's VTScada account |

## Resolution status on branch `deployment-hardening`

This audit is a Phase 1 snapshot. What has changed since, and what still has not:

| Finding | Status | Where |
|---|---|---|
| F4 settings silently dropped by compose | **Fixed.** All 52 settings are forwarded; the diff now reports "still NOT forwarded: none". | `docker-compose.yml`; `docs/CONFIGURATION.md` |
| F2 unbounded growth / storage exhaustion | **Fixed.** `RETENTION_DAYS` (default 90, `0` = forever) prunes delivered + audit rows, never pending, at start and daily. `pipeline status` now reports database size and volume free space, warns at ≥90% used, and the bridge heartbeat logs `db_size_mb`. | `settings.py`, `db.py`, `bridge.py`, `deploy/pipeline` |
| F1 insecure defaults | **Partly fixed.** 5432 no longer published; installer generates DB/dashboard passwords and requires the VTScada password. Broker auth is offered, not forced (ADR-005). | `docker-compose.yml`, `deploy/install.sh` |
| F6 no operational interface | **Addressed.** `deploy/pipeline` implements status/logs/start/stop/restart/test/backup/restore/update/uninstall; `deploy/install.sh` + `deploy/pipeline.service`. | `deploy/` |
| F19 EMQX licensing | **Corrected, not a finding.** Unified BSL image since 5.9.0; single-node Community License. No change. | `docs/DECISIONS.md` ADR-002 |
| F3 401 vs outage | **Fixed in code.** `vtclient` now raises `VTScadaAuthError` for 401/403 (ahead of the `URLError` catch), `probe()` returns `ok`/`credentials-rejected`/`unreachable`/`not-permitted`, and both `pipeline status` and the bridge's STANDBY line name the reason. | `vtclient.py`, `bridge.py`, `deploy/pipeline` |
| F8 multi-tag fairness | **Fixed.** `fetch_pending` takes a fair per-tag share (`ROW_NUMBER() OVER (PARTITION BY tag …)`) so one tag cannot fill every batch and starve the rest. | `bridge.py` |
| F9 bdSeq write failure | **Fixed.** An unwritable `STATE_DIR` logs `ALARM` and the bridge exits 2 rather than running with an unsafe bdSeq. | `bridge.py` |
| F5, F7, F10–F18, F20 | Open. | Phases 5–7 |

**Consequence of dropping the published 5432 port:** the end-to-end test and
`tools/verify_history.py` connect to PostgreSQL directly. They can no longer run
from another machine against the Pi; they must run inside the Compose network
(`docker compose exec bridge python …`) or over an SSH tunnel. The test procedure
in `docs/TESTING.md` must say so. This was accepted deliberately: an exposed
database with a default password is the greater risk.