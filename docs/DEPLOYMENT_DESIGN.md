# Deployment design (Phase 2)

The simplest installation and maintenance model that meets the objective: a
technician who does not know Docker, Python, Linux, MQTT or PostgreSQL can
install, operate, back up, restore, update and troubleshoot the system from the
documentation alone. See `DECISIONS.md` for the decisions behind it and
`DEPLOYMENT_AUDIT.md` for the evidence.

## 1. Chosen model

Docker Compose stays the runtime (ADR-001). On top of it sit exactly three
artifacts, nothing more:

* `deploy/install.sh` — the one command a technician runs. Detects the OS and
  prerequisites, collects the few values that cannot be defaulted, validates
  configuration, starts the stack, and installs the boot unit.
* `deploy/pipeline` — a small command wrapper (`status`, `logs`, `test`,
  `backup`, `restore`, `update`, `uninstall`, plus `start`/`stop`). Every
  subcommand has a real implementation, error handling and `--help`.
* `deploy/pipeline.service` — a systemd unit so the stack starts after reboot
  and restarts after an unclean shutdown.

No installer framework, no web UI, no extra service.

## 2. Command interface

| Command | Does | Notes |
|---|---|---|
| `install.sh` | preflight → configure → validate → start → install unit | idempotent; never overwrites existing `.env`/volumes without consent |
| `pipeline status` | compose ps + pending backlog + last heartbeat + **REST capability probe** | one command answers "is it healthy?" |
| `pipeline logs [-f] [svc]` | follow collector/bridge (or a named service) logs | |
| `pipeline test` | run `verify_history.py reconcile` from inside the bridge container | the honest check |
| `pipeline start` / `stop` / `restart` | control the stack | `stop` never deletes data |
| `pipeline backup [dir]` | `pg_dump` of the pipeline schema + `bdSeq` + EMQX data | prints the path; verifies the dump is readable |
| `pipeline restore <file>` | restore into a stopped stack, after an explicit confirmation | refuses to run against a live stack |
| `pipeline update` | fetch the release artifact, rebuild, restart, re-verify | preserves volumes |
| `pipeline uninstall` | stop and remove containers/images; **asks twice** before touching volumes | default keeps all data |

Every subcommand must be able to fail with a useful, non-secret message. Nothing
prints a credential.

## 3. Installation flow (what the technician sees)

1. **Preflight.** Detect the OS (Raspberry Pi OS / Debian family). Verify
   Docker Engine, the Compose plugin, and buildx ≥ 0.17 (the version that caused
   the documented `compose build requires buildx` failure); install or explain.
   Check free disk and that the network can reach VTScada (see §6).
2. **Configure.** Prompt only for values that cannot be defaulted: the VTScada
   REST URL and password; whether to enable MQTT authentication (ADR-005);
   the retention window (ADR-004); the PLC topic/decoder shape. Every prompt shows
   the default and accepts Enter. **Generate** unique DB and EMQX dashboard
   passwords rather than shipping `emqx` / `public1234`.
3. **Validate.** Parse the assembled configuration, confirm required values are
   present and syntactically valid, and report **all** problems at once. Refuse to
   start on a real error rather than silently substituting a default that could
   misdeliver data.
4. **Write `.env`** with mode 0600. If one exists, offer to keep it and only add
   missing keys.
5. **Start** the stack; wait for `timescaledb` healthy and both services up; run
   the REST capability probe (§6) and report it plainly.
6. **Install the boot unit** and enable it.
7. **Print** where data lives, how to see status, and how to get help.

## 4. Secure baseline (applied by default, H = audit F1)

* Broker: authentication **available** and offered; when enabled, EMQX is
  configured to require it and the credentials land in `.env` and the PLC steps.
  Anonymous publishing onto `plc/#` is never the silent default in a fresh install.
* Database: **stop publishing port 5432** to the host. Generated password.
* EMQX dashboard: generated password; recommend binding `18083` to localhost or
  the tailnet only.
* VTScada REST account: password required; never echoed or logged.
* Secrets: only in `.env` (0600), never in tracked files, never in `pipeline`
  diagnostics. `pipeline status` redacts.
* Test vs production: a `.env.test` path for the simulator profile so a test
  install is visibly distinct from a production one.
* Firewall: the installer never weakens existing rules, and never exposes a
  service to the public internet.

## 5. Configuration integrity fix (audit F4)

The code reads 51 settings; `docker-compose.yml` forwards 31. The following 22 are
read by the code but never reach the container, so setting them in `.env` silently
does nothing today:

```
BRIDGE_BIRTH_WAIT_SEC   BRIDGE_ENABLED        BRIDGE_HEARTBEAT_SEC
BRIDGE_POLL_SEC         BRIDGE_RETRY_DELAY_SEC BRIDGE_SETTLE_SEC
BRIDGE_STALL_ALARM_SEC  BRIDGE_WARMUP_PROBE   BRIDGE_WARMUP_TIMEOUT_SEC
COLLECTOR_TICK_SEC      DB_STATEMENT_TIMEOUT_MS MQTT_CONNECT_WINDOW_SEC
MQTT_SESSION_EXPIRY_SEC MQTT_TLS              PLC_TAG_MAP
PLC_TOPIC_TAG_LEVELS    STATE_DIR             TS_MAX_FUTURE_SECONDS
TS_MIN_YEAR             TS_TOL                VALUE_TOL
VTS_REST_TIMEOUT_SEC
```

Fix: forward all of them through `docker-compose.yml` (or, where a value must not
be user-visible, document it as internal and remove it from `.env.example`). The
most important are `PLC_TOPIC_TAG_LEVELS` and `PLC_TAG_MAP` (DEPLOY step 8c tells
the technician to set these) and `MQTT_SESSION_EXPIRY_SEC` (a silent-data-loss
risk if it is lowered in `.env` and ignored). `CONFIGURATION.md` becomes the single
authoritative list; the duplicated tables in README/DEPLOY/`.env.example` are
reduced to pointers.

## 6. REST capability preflight (ADR-003)

Because a site may not permit REST access, and because without it the queue can
never be confirmed, the installer and `pipeline status` run one probe and report
exactly one of:

* **ok** — REST answered; delivery can be confirmed.
* **not-permitted** — TCP to the SCADA host is reachable but REST is refused/absent
  (wrong port, or REST not enabled, or blocked) → actionable instructions.
* **unreachable** — the SCADA host does not answer at all → network/tailnet issue.
* **credentials-rejected** — REST answered `401` → wrong account/password
  (this is the audit F3 failure mode, currently indistinguishable from an outage).

The bridge keeps today's behaviour (STANDBY, no fake delivery) but its log and the
status command must name the *reason*, so a technician fixes the account instead of
chasing a phantom outage.

## 7. Data, state and backup

| Item | Location | Backed up by |
|---|---|---|
| Queue + audit tables | `timescale-data` volume, schema `pipeline` | `pg_dump` |
| Broker data/logs | `emqx-data`, `emqx-log` volumes | volume copy |
| `bdSeq` | `bridge-state` volume, `STATE_DIR/bdseq` | file copy |

`pipeline backup` produces a directory containing all three plus a manifest, and
verifies the dump can be listed. `pipeline restore` stops the stack, restores, and
reports the pending count so restored data can be proven intact. Both are
exercised in Phase 6, including a restore of pending rows.

## 8. Files this plan will add or change

New: `deploy/install.sh`, `deploy/pipeline`, `deploy/pipeline.service`,
`docs/{INSTALL,CONFIGURATION,OPERATIONS,TROUBLESHOOTING,BACKUP_RESTORE,UPGRADING,SECURITY,DATA_MODEL,PROTOCOLS,TESTING,MAINTENANCE,EXAMPLE_DEPLOYMENT}.md`,
`CHANGELOG.md`, `LICENSE`.

Changed: `docker-compose.yml` (keep the pinned EMQX image as-is — single node;
drop the 5432 publish; forward all settings; add a healthcheck and a retention
env), `README.md`, `DEPLOY.md`
(split into the generic guide + the worked example), `ARCHITECTURE.md` → moved to
`docs/`, `.env.example`, `pyproject.toml`/`requirements.txt` (pin), `Dockerfile`
(digest-pin the base image), `src/pipeline/bridge.py` (retention prune, REST reason
logging), `src/pipeline/vtclient.py` (distinguish 401 from unreachable),
`tests/*` (deterministic outage phase), `.github/workflows/ci.yml` (offline unit
tests).

Removed: the duplicated config tables in `DEPLOY.md`/`README.md` once
`CONFIGURATION.md` is authoritative (F12). The EMQX image is unchanged (ADR-002).

## 9. What this model deliberately does not do

No Kubernetes, no second broker, no message bus, no web UI, no schema change beyond
an optional pruning statement, no new runtime service. Nothing here trades the
store-and-forward guarantee for a simpler-looking install.