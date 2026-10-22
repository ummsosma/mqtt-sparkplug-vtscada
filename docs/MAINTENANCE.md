# Maintenance

Audience: whoever inherits this in five or twenty years. The goal is that they
can run, diagnose, upgrade and eventually replace it without the original author.

## Dependencies and why each exists

| Component | Version | Why | Failure mode if unavailable |
|---|---|---|---|
| EMQX | `emqx/emqx-enterprise:6.3.1` | broker; durable sessions hold unacked messages while the collector is down | PLC cannot publish; collector cannot subscribe |
| TimescaleDB | `timescale/timescaledb:2.30.2-pg16` | durable queue | no store-and-forward; collector cannot persist |
| Python | `python:3.12-slim` | runs collector/bridge | — |
| paho-mqtt | `>=2.1,<3` | MQTT client, 2.x callback API + manual ack | collector cannot withhold PUBACK |
| psycopg2-binary | `>=2.9,<3` | PostgreSQL driver | no database access |
| protobuf | `>=7.35,<8` | Sparkplug B codec | **import fails** if runtime < gencode |

### EMQX licensing (important, and not obvious)

Since EMQX **5.9.0** the Open Source and Enterprise editions were consolidated
into one image with the full feature set, under the **Business Source License
(BSL) 1.1**. The Enterprise image includes a bundled **single-node Community
License** permitting limited commercial use, so this single-node deployment needs
no licence key. Running it as a **cluster**, or for "full commercial usage",
would require a commercial licence. BSL is not OSI-open-source; each release has
a Change Date after which that version becomes fully open. Record the Change Date
for the pinned version when you upgrade. (ADR-002.)

### Pinning policy

* Container images are pinned to exact tags. Do not use `latest`.
* Python dependencies are currently **ranges**, not exact pins. A future
  maintainer should pin exact versions (and ideally the base image by digest) so
  a rebuild years later produces the same bytes. This is an open recommendation,
  not done yet.
* `sparkplug_b_pb2.py` is generated and validates the protobuf runtime version at
  import. If you regenerate it, regenerate with the matching `protoc` **and**
  update `requirements.txt`; otherwise the service will not start.

## Updating a dependency deliberately

1. Read the upstream changelog for the pinned component.
2. Change the pin in `docker-compose.yml` (images) or `requirements.txt`.
3. In a test environment: `docker compose up -d --build`, then
   `pipeline test` against a real VTScada.
4. Run the outage/recovery test.
5. Update this file and `CHANGELOG.md` with the new version and any behaviour
   change.

Never upgrade broker or database on a production Pi without a backup and a
tested restore.

## Database migrations

Schema lives in `src/pipeline/schema.sql` and is applied idempotently at startup.
`CREATE TABLE IF NOT EXISTS` does not alter an existing table, so any change needs
an explicit idempotent step in `db._migrate()`. The only historical migration is
`payload: jsonb → text` (lossless). There is no down-migration: the backup is the
rollback.

**Before any schema change:** take a backup, document the risk, and provide a
tested recovery path. Never run an untested migration against a queue you cannot
recreate.

## Replacing a component

The design deliberately keeps the seams narrow, so a component can be replaced
without touching the rest:

* **Broker (EMQX).** Anything MQTT 3.1.1/5 with QoS 1, durable sessions and a
  will message works. The collector needs `clean_start=0` + session expiry; the
  bridge needs a will and QoS 1 publishes.
* **Database.** Plain PostgreSQL 16 is sufficient; TimescaleDB is used as an
  ordinary table (no hypertable). To move: `pg_dump` the `pipeline` schema, restore
  into the new instance, point `DB_HOST`/`DB_PORT` at it.
* **Bridge/collector.** They share only the database. Either can be rewritten
  independently as long as it honours the invariants in
  `docs/DEPLOYMENT_AUDIT.md` and the wire contract in
  `docs/vtscada-wire-contract.md`.
* **VTScada.** The REST `SQLQuery` + `History` wide-table contract is documented
  with evidence. If VTScada changes it, `vtclient.py` is the one file to change.

## Artifacts to archive for a rebuild

Keep, off the Pi, with the site documentation:

* the exact release (git tag or tarball) and `CHANGELOG.md`;
* `.env` **secrets kept separately** from the code archive;
* the `docker save` image tarball if the site may be offline;
* a recent `pipeline backup`;
* a copy of `docs/vtscada-wire-contract.md` — the VTScada behaviour was learned
  experimentally and is not in any vendor manual in this form.

## Support lifecycle / end of life

This is a maintenance objective, not a promise that today's versions stay
supported. When a component reaches EOL:

1. check whether a newer pinned version is compatible with the wire contract;
2. test in isolation against a real VTScada before touching production;
3. if the vendor product is discontinued, use the replacement notes above — the
   queue and the Sparkplug contract are the durable parts, not the vendor.

## Diagnosability without cloud or AI

Everything needed is local: `pipeline status`, the two service logs, the four
audit tables, `tools/monitor_sparkplug.py` (what we send) and
`tools/verify_history.py` (what actually landed). No cloud service, no account,
no external dependency is required to diagnose a fault.
