# Changelog

User-visible changes and any migration requirement. Newest first.

## [1.0.0] — 2026-10-22 — first public release

Deploy flexibility & non-technical path:

### Added
- **No-REST delivery mode** — `BRIDGE_CONFIRM_MODE=publish` marks rows delivered
  as soon as the MQTT publish succeeds, so the pipeline works when the bridge
  cannot reach VTScada REST at all (default remains `readback`, which proves
  delivery by reading the historian back). `pipeline status`/`pipeline configure`
  and the installer let you pick.
- **Payload mapping file (no Python)** — `PLC_DECODER=mapping` + `PLC_MAPPING_FILE`
  understand a new PLC message shape from a JSON file (tag/value/ts/seq/device
  field paths, per-tag rename, `scale`/`offset`/`map`/`invert` transforms), with
  no decoder code to write. Example at `plc-mapping/example.json`.
- `pipeline configure` — guided, validated editor for the important `.env`
  settings, so a non-technical operator cannot break the config with a typo.
- `docs/QUICKSTART.md` — 10-minute, plain-English setup.
- `pipeline simulate [start|stop]` — run the bundled fake PLC to exercise the
  whole chain (broker, collector, queue, bridge) with no hardware.
- README rewritten to lead with the non-technical path and de-emphasise Tailscale
  (LAN is the default way to reach VTScada; Tailscale is an optional extra).

### Changed
- `deploy/install.sh` asks for the confirm mode and only requires the VTScada REST
  password in `readback` mode.

### Fixed
- **The collector could not store anything.** `collector.py` called
  `db.utc_from_ms()`, which `db.py` never defined, so every sample failed to
  persist (`ALARM persist failed … module 'pipeline.db' has no attribute
  'utc_from_ms'`) and the queue stayed empty — a total outage that no offline
  test noticed, because none of them touch the database. The helper is now
  defined, and `tests/check_module_refs.py` (in CI) fails if any
  `pipeline.<module>.<name>` reference does not exist.
- **`install.sh` failed a correct publish-mode install.** The check used
  `grep '^BRIDGE_CONFIRM_MODE=publish\$'`; inside single quotes `\$` is a literal
  dollar, so it never matched and the installer aborted with
  "BRIDGE_CONFIRM_MODE is not set to publish". Now a whole-line fixed-string
  match against the chosen mode.
- **`install.sh` / `pipeline configure` corrupted every prompted value.** The
  `ask`/`ask_secret` helpers printed their prompt to stdout, so the caller's
  `VAR="$(ask "Prompt" "default")"` captured the prompt *and* the answer —
  `.env` got `VTS_REST_URL=VTScada REST base URL [...] http://...` and similar
  for the topic, decoder, tags and password, while still passing validation. The
  prompt now goes to stderr and only the answer is captured. Guarded by
  `tests/test_install_prompts.py` (added to CI).
- **`pipeline restart` did not apply `.env` changes.** It used `docker compose
  restart`, which reuses the existing containers, so a settings change was
  silently ignored — the one thing the command exists for. Now uses
  `docker compose up -d`, which recreates only the services whose configuration
  changed.
- **`pipeline stop` / `uninstall` left the simulator running**, holding the
  compose network open. Both now include the `sim` profile.
- **The simulator produced only duplicates after a restart** (`--seq-base`
  defaulted to 0, so it re-sent sequence numbers the queue already held) and died
  with `ConnectionRefusedError` if started before the broker was ready. It now
  retries the connection and derives its sequence from the clock.
- **`pipeline configure` offered `mapping` but never asked for the mapping file**,
  producing a config the collector cannot start with.

### Earlier pre-public milestone: deployment hardening

### Added
- `deploy/install.sh` — guided install: preflight, configuration, generated
  secrets, validation, start, boot unit. Re-runnable; never overwrites an existing
  `.env` or data without consent; never prints a secret.
- `deploy/pipeline` — `status`, `logs`, `start`, `stop`, `restart`, `test`,
  `backup`, `restore`, `update`, `uninstall`. `status` classifies VTScada
  reachability as `ok` / `credentials-rejected` / `unreachable` / `not-permitted`.
- `deploy/pipeline.service` — systemd boot unit.
- `RETENTION_DAYS` (default 90; `0` = keep forever). Prunes **delivered** rows and
  audit rows only; pending rows are never pruned.
- `docs/`: DEPLOYMENT_AUDIT, DECISIONS, DEPLOYMENT_DESIGN, CONFIGURATION,
  INSTALL, OPERATIONS, TROUBLESHOOTING, BACKUP_RESTORE, UPGRADING, SECURITY,
  DATA_MODEL, PROTOCOLS, TESTING, MAINTENANCE.
- `tests/check_settings_forwarded.py` and a CI workflow. The check fails if any
  setting the code reads is not forwarded by `docker-compose.yml`.

### Fixed
- The deploy scripts were committed non-executable and `git archive` on a Windows
  checkout produced **CRLF** line endings, so `#!/usr/bin/env bash\r` failed on
  Linux. `.gitattributes` now enforces LF and the scripts are `+x` in the index.
  Caught by the clean-install test on the Pi.
- A VTScada `401` is now `credentials-rejected`, distinct from `unreachable`
  (`VTScadaAuthError` caught ahead of `URLError`).
- Multi-tag starvation: `fetch_pending` now takes a fair per-tag share instead of
  letting the alphabetically-first tag fill every batch.
- An unwritable `STATE_DIR` is now a hard, loud failure (exit 2). Running with a
  bdSeq that cannot persist means the next restart is silently ignored by
  VTScada.
- `pipeline status` reports database size and queue-volume free space and warns
  at ≥90% used; the bridge heartbeat logs `db_size_mb`.

### Changed
- **All 52 settings are now forwarded by `docker-compose.yml`.** Previously 22
  were silently dropped, so setting them in `.env` did nothing — including
  `PLC_TOPIC_TAG_LEVELS`, `PLC_TAG_MAP` and `MQTT_SESSION_EXPIRY_SEC`.
- **The PostgreSQL port 5432 is no longer published.** Admin access is via
  `docker compose exec timescaledb psql`. The end-to-end test must now run inside
  the Compose network (or over an SSH tunnel) — see `docs/TESTING.md`.
- The installer generates database and dashboard passwords instead of using
  `emqx` / `public1234`, and requires `VTS_REST_PASSWORD`.

### Not changed
- The EMQX image stays `emqx/emqx-enterprise:6.3.1`. Since 5.9.0 it is the
  unified feature image under BSL 1.1, and single-node use is covered by the
  bundled Community License (ADR-002).

### Migration
- Existing deployments: adding settings to `.env` now actually takes effect.
  Nothing must be migrated.
- Dropping the published 5432 port is a `docker compose up -d` change; no data
  change.
- `RETENTION_DAYS` defaults to 90, so an upgraded deployment will begin pruning
  delivered rows older than 90 days at its next bridge start. **Set it to `0` or a
  larger value before upgrading if you must retain older audit rows.**
