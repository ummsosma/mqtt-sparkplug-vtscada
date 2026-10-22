# mqtt-sparkplug-vtscada

Take data from a **CLICK PLUS PLC** over MQTT, store it durably, and land it in a
**VTScada historian** as Sparkplug B — with delivery proved by reading the
historian back rather than assumed from an MQTT ack. Whether the historian records
the PLC's *own* time depends on the PLC: the CLICK PLUS at this site sends no
timestamp, so arrival time is used and the original time is lost (see
[Pointing it at a real CLICK PLUS](#pointing-it-at-a-real-click-plus)).

```
 CLICK PLUS ──MQTT──► broker ──► collector ──► TimescaleDB ──► bridge ──Sparkplug B──► VTScada
   (PLC)              (EMQX)     (persist)     durable queue   (encode)   (MQTT driver)
                                                  ▲                        │
                                                  └── readback: only a ────┘
                                                      confirmed row is
                                                      marked delivered
```

Two long-running services, one queue table, no shared mutable state:

| Piece | File | Job |
|---|---|---|
| collector | `src/pipeline/collector.py` | MQTT in → decode → durable queue. Withholds the PUBACK until the row is committed. |
| bridge | `src/pipeline/bridge.py` | queue → Sparkplug B → VTScada. Marks a row delivered only after reading it back out of the historian. |

Everything else is a tool or a test. Pointing the pipeline at a *new* PLC shape
used to mean editing `src/pipeline/decoder.py` in Python; now you can use a
[JSON mapping file](#mapping-a-new-payload-shape-without-writing-code) instead.

## Start here — you do NOT need to know Docker or Python

| If you are … | Read |
|---|---|
| **New / non-technical** | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 10-minute setup, then use `pipeline status` / `pipeline configure` |
| Doing a clean install | [`docs/INSTALL.md`](docs/INSTALL.md) |
| Changing how data is read | [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) → "Mapping a new payload shape" |
| Operating day to day | `pipeline status` — one screen. `docs/OPERATIONS.md` if you need more. |

The `deploy/install.sh` installer and the `deploy/pipeline` command hide Docker
from you. After install there are only a handful of commands you will ever
need:

    pipeline status      health in one screen
    pipeline logs -f     watch data flow
    pipeline configure   change settings safely (validates what you type)
    pipeline simulate    run a fake PLC so you can test with no hardware
    pipeline start/stop  start / stop (your data is kept)
    pipeline backup      back up the queue

The `pipeline configure` command asks you plain-English questions and validates
your answers, so you cannot break the config with a typo.

## Read the rest only when you hit a problem

| Document | When to open it |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | clean install, prerequisites, first run |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | status, logs, routine maintenance |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | symptom → cause → fix |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | **the** authoritative setting reference + payload mapping schema |
| [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md) | backup, restore, verification |
| [`docs/SECURITY.md`](docs/SECURITY.md) | credentials, exposure, incident response |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | components, data flow, failure boundaries |
| [`docs/UPGRADING.md`](docs/UPGRADING.md) | update and rollback limits |

<details>
<summary>Reference docs for maintainers (skip unless you change the code)</summary>

| Document | Purpose |
|---|---|
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | schema, states, timestamps, dedup |
| [`docs/PROTOCOLS.md`](docs/PROTOCOLS.md) | MQTT, Sparkplug B, VTScada REST |
| [`docs/TESTING.md`](docs/TESTING.md) | test levels and what each does **not** prove |
| [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md) | dependencies, lifecycle, replacing parts |
| [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) | pass/fail gates before tagging a release |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADRs and rejected alternatives |
| [`docs/DEPLOYMENT_AUDIT.md`](docs/DEPLOYMENT_AUDIT.md) | the Phase 1 audit and its evidence |
| [`docs/vtscada-wire-contract.md`](docs/vtscada-wire-contract.md) | VTScada behaviour, with experimental evidence |

</details>

---

## Install on a Raspberry Pi

```bash
sudo mkdir -p /opt && sudo tar -xzf mqtt-sparkplug-vtscada-<version>.tar.gz -C /opt
cd /opt/mqtt-sparkplug-vtscada
sudo ./deploy/install.sh          # guided; generates secrets; starts; enables boot
./deploy/pipeline status          # one-screen health
./deploy/pipeline test            # read the historian back and reconcile
```

Full prerequisites — including the VTScada REST setup, which is a **hard
requirement** — are in [`docs/INSTALL.md`](docs/INSTALL.md).

## Developer quickstart (everything in Docker)

```bash
cp .env.example .env       # edit it; .env is gitignored
docker compose up -d --build
docker compose logs -f collector bridge
docker compose --profile sim up -d simulator     # a stand-in PLC
```

Then reconcile against the historian:

```bash
docker compose exec bridge python tools/verify_history.py reconcile
```

### What a healthy log looks like

```
[collector] connected to emqx:1883, subscribing to plc/#
[collector] counters={'received': 120, 'persisted': 120, ...}
[bridge]    NBIRTH published (bdSeq=132) topic=spBv1.0/SFTest/NBIRTH/sf-pi
[bridge]    warm-up ok: probe ts=1792033051.03 value=4242.0 confirmed
[bridge]    batch=1792033051234-1 published=50 confirmed=50 unmatched=0 backlog=12 payload=4821B
[bridge]    heartbeat backlog=4 counters={...} stall=0s
```

`confirmed` should equal `published`. `unmatched` is non-zero only at an outage
boundary, and those rows stay pending and are retried — which is correct, not an
error.

---

## Running without Docker

Needs Python 3.10+, a broker, and PostgreSQL/TimescaleDB.

```bash
pip install -r requirements.txt          # or: pip install -e ".[dev]"
cp .env.example .env                     # settings are read from .env or the environment

python -m pipeline.collector             # terminal 1
python -m pipeline.bridge                # terminal 2
PYTHONPATH=src python tools/simulate_plc.py --duration 120   # terminal 3
```

`PYTHONPATH=src` is only needed for the scripts in `tools/`; the two services are
installed as a package.

---

## Adapting to a real CLICK PLUS

The pipeline cannot guess your PLC's payload shape, so decoding is explicit. Pick
a decoder with `PLC_DECODER`, or tell the JSON decoder which fields to read.

**1. Find out what your PLC actually publishes.** Subscribe and look:

```bash
mosquitto_sub -h <broker> -t 'plc/#' -v
```

**2. Match a decoder to it.**

| Your PLC publishes | Set |
|---|---|
| `plc/<device>` with `{"tag":"AI01","value":21.5,"ts":1792033051030}` | `PLC_DECODER=json` (default field names) |
| ...with different field names, e.g. `{"p":{"v":21.5,"t":...},"id":"AI01"}` | `PLC_DECODER=json` + `PLC_JSON_VALUE_FIELD=p.v`, `PLC_JSON_TS_FIELD=p.t`, `PLC_JSON_TAG_FIELD=id` |
| `plc/<device>/<tag>` with a bare value as the body | `PLC_DECODER=topic_tag` |
| already-encoded Sparkplug B | `PLC_DECODER=sparkplug` |
| unsure / mixed | `PLC_DECODER=auto` (routes on topic, then body) |

Timestamps are accepted as epoch **milliseconds**, epoch **seconds**, or an
**ISO 8601** string. `auto` tries Sparkplug by topic, then JSON, then `topic_tag`.

**3. If your PLC sends no sequence number**, one is derived from its timestamp.
That is deliberate: the queue deduplicates on `(device, seq, tag)`, so a replayed
message collapses onto the existing row instead of inserting a second one.

**4. If your PLC sends no timestamp at all**, what happens depends on the decoder.
`topic_tag` and `sparkplug` fall back to arrival time, so the "original timestamp"
property is lost. `json` treats a missing timestamp as undecodable and quarantines
the message instead — so if you are sending JSON, send a timestamp with it. Give
the PLC a clock if the distinction matters to you.

Whatever cannot be decoded — unknown shape, non-numeric value, a timestamp before
2020 or in the future — is written to `pipeline.quarantine` with the reason and
the raw payload, rather than being silently dropped or turned into a zero:

```sql
SELECT created_at, reason, topic, payload FROM pipeline.quarantine ORDER BY id DESC LIMIT 20;
```

---

## Configuration

Every setting is an environment variable with a default. The authoritative
reference — every name, default, whether it is required, and its risk — is
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md). `.env.example` carries the
common ones with comments.

All settings are forwarded by `docker-compose.yml`; a CI check
(`tests/check_settings_forwarded.py`) fails the build if one is ever dropped, so a
documented setting cannot silently do nothing.

The ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `PLC_TOPIC` | `plc/#` | Topic tree the collector subscribes to |
| `PLC_DECODER` | `auto` | `auto` \| `json` \| `topic_tag` \| `sparkplug` |
| `VTS_GROUP` / `VTS_EDGE` | `SFTest` / `sf-pi` | Sparkplug group and edge node. These decide **both** the MQTT topic (`spBv1.0/<GROUP>/NDATA/<EDGE>`) and the VTScada tag path, so they must match the driver's tag addresses |
| `VTS_TAGS` | `{"AI01":"double","DI01":"bool"}` | tag → datatype. Only listed tags are published |
| `VTS_REST_URL` | `http://localhost:443` | Where VTScada's REST interface answers |
| `VTS_REST_PASSWORD` | — | **Required** for real delivery |
| `VTS_TAG_ROOT` | `MQTT Context\MQTT-SB\MQTT-DRIVER` | Prefix of the `History` column names |
| `BRIDGE_BATCH` | `50` | Samples per Sparkplug payload |

Run `python -c "from pipeline import settings; print(settings.describe())"` to see
exactly what a deployment resolved to, secrets excluded.

---

## Connecting a real VTScada

Three things have to line up, and if any one of them is wrong **nothing arrives
and nothing is logged as an error**.

1. **The MQTT driver must be in Sparkplug B mode with tag auto-creation on**, so
   the tags exist. Each tag's `ReadAddress` looks like
   `spBv1.0/SFTest/NDATA/sf-pi++AI01` and its `HistoryAddress` is the same with
   `$$History$$` appended. Read them straight out of
   `<appfolder>\Tags\<Type>_<n>\*.tag` rather than transcribing a dialog.
2. **`VTS_GROUP`, `VTS_EDGE` and `VTS_TAGS` must match those addresses.**
3. **The REST interface must be enabled** — Application Configuration → Thin
   Client/Server Setup, with a realm containing the application. The account needs
   Remote Data Access plus Remote Tag Value/History Retrieve.

Then confirm it works end to end:

```bash
python tools/verify_history.py tables          # is the REST interface answering?
python tools/verify_history.py show AI01       # recent history for one tag
python tools/verify_history.py reconcile       # is every 'delivered' row really there?
```

`reconcile` is the honest test. It reads every row the bridge marked delivered
back out of the historian and checks the timestamp and value. A pass means the
pipeline is not lying to you.

---

## Testing

```bash
python tests/test_decoder.py            # 23 unit tests, no network
python tests/test_sparkplug_payload.py  # 12 unit tests, no network
python tests/test_store_and_forward.py --help
```

The store-and-forward test is the real one. It runs a baseline, makes VTScada
unavailable (suspending `VTS.exe`, which is reversible and needs no admin),
checks that rows accumulate and **nothing** is confirmed, restores it, and
reconciles everything.

> **The database port is no longer published**, so the test must run inside the
> Compose network rather than from your laptop:

```bash
docker compose exec -T bridge python tests/test_store_and_forward.py --outage-secs 60
```

It writes `tests/results/<run-id>/report.json` plus the logs of every process it
started. What each test level does **not** prove is in
[`docs/TESTING.md`](docs/TESTING.md).

---

## How it behaves when things break

| What breaks | What happens | Why it is safe |
|---|---|---|
| VTScada stops answering | Bridge enters STANDBY and publishes **nothing**; the queue grows | Publishing into the void would produce rows marked delivered that are not in the historian |
| Bridge is killed mid-batch | In-flight rows are still `pending` | A row is only marked delivered in the same transaction that confirms it |
| Collector is killed | Broker redelivers (PUBACK is withheld until commit) | A message is never acknowledged while it exists only in memory |
| Broker restarts | Collector reconnects; unacknowledged messages are held for `MQTT_SESSION_EXPIRY_SEC` (7 days by default) | QoS 1 with `clean_start=0`, and the v5 CONNECT value is the one the broker honours |
| PLC replays its buffer | Duplicates are counted, not re-queued | `UNIQUE (device_id, seq, tag)` |
| A batch is re-sent after a hiccup | No duplicate history rows | VTScada coalesces per timestamp |
| A tag is queued that is not in `VTS_TAGS` | Excluded from every batch, and an `ALARM` names it | Otherwise those dead rows would sit at the head of every batch and starve real data |
| The bridge restarts | `bdSeq` continues from a file in `STATE_DIR` | A BIRTH with a bdSeq the host has already seen is treated as stale and every following message is ignored |

---

## Status: what is verified and what is not

Labels: **verified** = supported by a stored run artifact under `tests/results/`;
**reported** = a past claim with no committed artifact; **unverified** = not
exercised. The evidence labels and the full finding list are in
[`docs/DEPLOYMENT_AUDIT.md`](docs/DEPLOYMENT_AUDIT.md).

**Verified** on 2026-10-15 against the live broker, the live TimescaleDB and a
real VTScada historian (artifact: `tests/results/verify-pass/report.json`):

* collector end to end: a simulated PLC on MQTT → decoded → persisted in
  TimescaleDB (`pipeline.samples`), 0 quarantined, 0 persist failures
* the standby gate: with the historian unreachable the bridge published no DATA
  payload at all (confirmed by an independent monitor on `spBv1.0/#`) and marked
  no row delivered
* `bdSeq` persisted across restarts and wrapping at 255
* **the full store-and-forward test — PASS:** 770 events delivered into the real
  VTScada historian, `missing 0`, `mismatched 0`, no duplicate timestamps, across
  a 60 s outage in which VTScada stopped answering entirely. Catch-up 28.1 s,
  backlog drained to exactly 0 in 5.9 s once the source stopped.

Reproduce that last one with:

```bash
python tests/test_store_and_forward.py --start-services --run-simulator --outage-secs 60
```

It writes `tests/results/<run-id>/report.json` and the logs of every process it
started.

### Reported as a full deployment on the Pi (no artifact committed)

The whole stack — EMQX, TimescaleDB, collector and bridge in containers on
`your-pi` — was reported deployed and exercised on 2026-10-15:

* all four containers up, EMQX and TimescaleDB reporting healthy
* the bridge container reaches VTScada on the SCADA PC over the tailnet
  (`http://100.64.1.20:443`, 165 tables visible from inside the container)
* a simulated PLC publishing on the real `plc/#` topic tree: 80 messages
  received, 80 persisted, 0 quarantined, 0 persist failures
* queue drained to 0 pending, and `verify_history.py reconcile` run **from
  inside the bridge container on the Pi**:

```
AI01: 59 delivered, 0 missing, 0 mismatched, 59 rows in historian window
DI01: 59 delivered, 0 missing, 0 mismatched, 59 rows in historian window

TOTAL 118 delivered | missing 0 | mismatched 0
VERDICT: PASS -- every delivered row is present in the historian at its source time
```

That is the complete path proven: PLC → EMQX → collector → TimescaleDB → bridge
→ VTScada historian on a different machine → read back and confirmed.

### Reported with a real CLICK PLUS (no artifact committed)

A real CLICK PLUS publishing over MQTT was reported writing into the VTScada
historian. **No report.json or log for this run exists in the repository**, so
treat it as reported, not verified:

```
topic:   plc/1/testVal        plc / <device> / <tag>
payload: -16165               a bare number, no timestamp, no tag name

testVal: 234 delivered, 0 missing, 0 mismatched, 235 rows in historian window
TOTAL 352 delivered | missing 0 | mismatched 0
VERDICT: PASS -- every delivered row is present in the historian at its source time
```

The whole path, with no simulation anywhere in it: CLICK PLUS → EMQX → collector
→ TimescaleDB → bridge, all in containers on the Pi → VTScada historian on the
SCADA PC over the tailnet → read back and confirmed.

Two properties of this PLC are worth knowing and are described in step 8c of
[`docs/EXAMPLE_DEPLOYMENT.md`](docs/EXAMPLE_DEPLOYMENT.md): it sends **no timestamp**, so arrival time is used (fine
at 1 Hz on a LAN, not fine over a slow link), and the payload is a **bare
number** with no unit or quality flag.

**Still not verified:**

* clean install on a fresh Pi, and automatic start after a real reboot;
* backup → restore with pending data;
* the real PLC path since the settings/retention changes on this branch;
* broker-authenticated operation;
* a site where REST access is restricted;
* the full outage → recovery → reconcile on **this** branch (needs an upgrade
  window to recreate the live containers);
* the longest outage exercised with the real PLC attached is a few minutes; the
  60 s and 300 s outage runs used the simulator.

**Additionally verified on `your-pi` on 2026-10-19** (isolated image, live
broker/database, real VTScada): decoder tests **23/23**, payload tests **12/12**,
the settings-forwarding guard, `probe()` returning `ok` / `credentials-rejected` /
`unreachable` for good, wrong and dead endpoints, and `db.prune(3650)` deleting
zero rows. Details and the limits of each check are in
[`docs/TESTING.md`](docs/TESTING.md).

The mechanisms this build implements were previously *reported* proven end to end
against a real VTScada historian (1092/1092 events reconciled through a 300 s
outage) — again, no artifact is committed for that run. This
repository is that work restructured, dockerised and made configurable, and the
delivery path above is the re-run in the new shape.

---

## Repository layout

```
src/pipeline/
  collector.py     MQTT in -> durable queue
  bridge.py        durable queue -> Sparkplug B -> VTScada
  decoder.py       PLC payload -> sample          <- change this for a real PLC
  vtclient.py      VTScada REST client (the oracle)
  settings.py      every setting, one place
  schema.sql       the queue tables
  sparkplug/       official Sparkplug B protobuf
tools/
  simulate_plc.py       stand-in for a CLICK PLUS
  verify_history.py     read the historian back; reconcile
  monitor_sparkplug.py  decode what the bridge is sending
tests/
  test_decoder.py             unit, no network
  test_sparkplug_payload.py   unit, no network
  test_store_and_forward.py   the real end-to-end outage test
  vt_suspend.py               make VTScada unavailable, reversibly
  check_settings_forwarded.py CI guard: no setting silently dropped by compose
deploy/
  install.sh                  guided install (preflight, config, start, boot unit)
  pipeline                    ops CLI: status/logs/backup/restore/update/...
  pipeline.service            systemd boot unit
  90-allow-plc-subnet         NetworkManager hook for the PLC cable
docs/                         all documentation (see the map above)
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Dallas Davis.

Third-party components keep their own licences; see [NOTICE](NOTICE):

| Component | Licence |
|---|---|
| `src/pipeline/sparkplug/sparkplug_b.proto` and the generated `_pb2.py` | Eclipse Public License 2.0 (Eclipse Sparkplug B, © Cirrus Link Solutions and others) |
| `emqx/emqx-enterprise` container image | Business Source License 1.1 — a single node runs under the bundled Community licence |
| `timescale/timescaledb` container image | Timescale Licence + PostgreSQL Licence |
| `python:3.12-slim` base image | Python Software Foundation Licence |
