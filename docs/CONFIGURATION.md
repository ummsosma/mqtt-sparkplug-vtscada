# Configuration reference

This is the **single authoritative list** of every setting. Everything the code
reads is here; where README, `DEPLOY.md`, `.env.example` or `docker-compose.yml`
repeat a setting, they link back here rather than restating it.

All settings are environment variables. A normal installation sets them in `.env`
next to `docker-compose.yml`; Docker Compose passes them into the containers.
Values are read by `src/pipeline/settings.py`; every default below is taken from
that file, not from prose.

Note the difference between a **module default** and a **Compose fallback**: inside
the containers `docker-compose.yml` supplies its own values (`DB_HOST` →
`timescaledb`, `DB_USER`/`DB_PASSWORD`/`DB_NAME` → `emqx`), while running the code
outside Docker uses the defaults printed below (`localhost`, `pipeline`, empty
password). This is audit finding F14. Set the value in `.env` and both paths agree.

To see what a running deployment actually resolved to (secrets excluded):

```bash
python -c "from pipeline import settings; print(settings.describe())"
# inside a container:
docker compose exec collector python -c "from pipeline import settings; print(settings.describe())"
```

## How to change a setting, safely

1. Edit `.env`.
2. Apply it with **`docker compose up -d --force-recreate collector bridge`**.
   A plain `docker compose restart` reuses the old container environment and the
   change silently does nothing.
3. Confirm it took: `docker inspect bridge --format '{{range .Config.Env}}{{println .}}{{end}}' | grep NAME`.

Changing configuration never touches the queue. Only `docker compose down -v`
destroys data (it deletes the volumes).

## Two integrity rules

* **Every setting below is forwarded by `docker-compose.yml`.** This was not true
  before: 22 of the 51 settings were read by the code but silently dropped by
  Compose (audit finding F4). That is fixed on this branch — the check is now
  `settings read: 52 — forwarded: 52 — still NOT forwarded: none`.
* **Never let a wrong value look like a right one.** The system does not silently
  substitute a default where doing so could misdeliver data.

Legend: **Req** = must be set for a real deployment. **Restart** = the service
must be recreated for a change to apply.

---

## 1. PLC and MQTT broker

| Name | Default | Req | Controls | Example | When to change | Restart | Risk |
|---|---|---|---|---|---|---|---|
| `MQTT_HOST` | `localhost` (compose: `emqx`) | yes | broker address | `emqx` | only if the broker moves | yes | data integrity |
| `MQTT_PORT` | `1883` | — | broker port | `8883` for TLS | with TLS | yes | — |
| `MQTT_USERNAME` | *(empty)* | — | broker user; empty = anonymous | `pipeline` | when broker auth is on | yes | **security** |
| `MQTT_PASSWORD` | *(empty)* | with user | broker password | — | with broker auth | yes | **security** |
| `MQTT_TLS` | `false` | — | enable TLS to the broker | `true` | production | yes | security/data |
| `MQTT_SESSION_EXPIRY_SEC` | `604800` (7 d) | — | how long the broker keeps this client's session — and unacked messages — while the collector is down | `604800` | rarely | yes | **silent data loss if lowered** |
| `MQTT_CONNECT_WINDOW_SEC` | `300.0` | — | how long a service retries a cold broker before exiting | `300` | slow hardware | yes | — |
| `MQTT_CLIENT_PREFIX` | `spb-vts` | — | client-id prefix | `plant1` | two deployments on one broker | yes | — |
| `PLC_TOPIC` | `plc/#` | yes | topic tree the collector subscribes to | `plc/#` | match the PLC | yes | data integrity |
| `PLC_DECODER` | `auto` | yes | how a message becomes a sample | see below | match the PLC payload | yes | data integrity |
| `PLC_DEVICE_ID` | `plc1` | — | device id when the payload/topic has none | `click1` | with `topic_tag` | yes | — |
| `PLC_TOPIC_TAG_LEVELS` | `2` | — | trailing topic levels read as `<device>/<tag>` | `2` | `plc/<device>/<tag>` shape | yes | data integrity |
| `PLC_TAG_MAP` | `{}` | — | map a whole topic to a tag name | `{"plc/1":"DS1"}` | topic carries no usable name | yes | data integrity |

`PLC_DECODER` values: `auto` (route on topic, then body) · `json` (field paths
below) · `topic_tag` (`plc/<device>/<tag>` + bare value) · `sparkplug`
(already-encoded Sparkplug B) · `mapping` (a [JSON mapping file](#mapping-a-new-payload-shape)). See `docs/PROTOCOLS.md`.

**The real CLICK PLUS here** publishes `plc/<device>/<tag>` with a bare numeric
payload and **no timestamp**. That is `PLC_TOPIC_TAG_LEVELS=2` with the default
`PLC_DECODER=auto`, and the tag from the topic (e.g. `testVal`) must also appear
in `VTS_TAGS`. Because it sends no timestamp, **arrival time is used and the PLC's
original time is not preserved** — see §3 and `docs/PROTOCOLS.md`.

JSON field paths (only used when `PLC_DECODER=json`; dotted paths reach nested
values, e.g. `payload.value`):

| Name | Default |
|---|---|
| `PLC_JSON_TAG_FIELD` | `tag` |
| `PLC_JSON_VALUE_FIELD` | `value` |
| `PLC_JSON_TS_FIELD` | `ts` |
| `PLC_JSON_SEQ_FIELD` | `seq` |
| `PLC_JSON_DEVICE_FIELD` | `device` |

### Mapping a new payload shape (no Python)

When your PLC sends a message shape the built-in decoders do not cover, and you
would rather edit a JSON file than Python, use a **payload mapping file**:

| Setting | Controls | Restart |
|---|---|---|
| `PLC_DECODER=mapping` | enable the mapping decoder | yes |
| `PLC_MAPPING_FILE` | path (in the container) to the JSON mapping file | yes |

The `collector` container mounts `./plc-mapping` read-only at `/plc-mapping`. So:
put your file at `plc-mapping/plc.json` (copy the `example.json`) and set
`PLC_MAPPING_FILE=/plc-mapping/plc.json`.

The file tells the collector, in plain terms, which fields to read and what to do
with the values:

```json
{
  "version": 1,
  "decoder": "json",
  "tag_field": "tag",
  "value_field": "value",
  "ts_field": "ts",
  "seq_field": "seq",
  "device_field": "device",
  "tags": {
    "AI01": { "to": "TankLevel", "transform": { "scale": 0.1 } },
    "DI01": { "to": "PumpRunning", "transform": { "map": { "1": true, "0": false } } }
  },
  "topics": { "plc/1": { "tag": "DS1", "transform": { "scale": 1.0 } } }
}
```

* `tag_field` / `value_field` / `ts_field` / `seq_field` / `device_field` — dotted
  JSON paths to where each piece lives (e.g. `"payload.value"`). Omit to keep the
  `PLC_JSON_*_FIELD` defaults.
* `tags` — key each **incoming** tag name; `to` renames it before publish;
  `transform` changes the value:
  * `scale` × and `offset` + (e.g. raw 0–4095 counts → real units),
  * `map` — turn strings/numbers into other values (`"ON"` → `true`),
  * `invert` — `true`/`false` flipped.
* `topics` — rename a tag by *topic* (for a topic that carries no usable name).

A tag that arrives but is **not** in the file passes through unchanged (its
original name, no transform). A tag named in the file but never seen is not an
error. A value a `map` does not recognise is dropped with a warning (logged), not
silently guessed. A malformed JSON file stops every message being stored, with an
`ALARM payload mapping not usable` line, until it is fixed.


Timestamp sanity (a message outside the window is quarantined, never delivered):

| Name | Default | Controls | When to change | Restart |
|---|---|---|---|---|
| `TS_MIN_YEAR` | `2020` | earliest acceptable timestamp year | legacy data | yes |
| `TS_MAX_FUTURE_SECONDS` | `300` | how far ahead a timestamp may be | clock skew | yes |

---

## 2. VTScada connection

**REST access is how delivery is *proved* by default (`readback` mode).** If the
bridge cannot reach VTScada REST at all, set `BRIDGE_CONFIRM_MODE=publish` (below)
to deliver on MQTT publish success instead — see the note under the table.

| Name | Default | Req | Controls | Example | When to change | Restart | Risk |
|---|---|---|---|---|---|---|---|
| `BRIDGE_CONFIRM_MODE` | `readback` | — | `readback` = REST required (prove via readback) · `publish` = no REST (mark delivered on MQTT publish) | `publish` | no REST reachable from the bridge | yes | **weakest guarantee** |
| `VTS_REST_URL` | `http://localhost:443` (compose: `host.docker.internal`) | yes* | where REST answers | `http://192.168.1.50:443` | point at the SCADA PC | yes | data integrity |
| `VTS_REST_REALM` | `odbc` | yes* | URL realm segment | `odbc` | match VTScada's realm | yes | data integrity |
| `VTS_REST_USER` | `admin` | yes* | REST account | `admin` | site policy | yes | security |
| `VTS_REST_PASSWORD` | *(empty)* | **yes*** | REST account password | — | always | yes | **security; empty = never delivers** |
| `VTS_REST_TIMEOUT_SEC` | `20.0` | — | REST request timeout | `20` | slow links | yes | — |
| `VTS_GROUP` | `SFTest` | yes | Sparkplug group id | `SFTest` | must match tag addresses | yes | data integrity |
| `VTS_EDGE` | `sf-pi` | yes | Sparkplug edge node id | `sf-pi` | must match tag addresses | yes | data integrity |
| `VTS_TAG_ROOT` | `MQTT Context\MQTT-SB\MQTT-DRIVER` | yes | prefix of the History column names | — | match VTScada | yes | data integrity |

`VTS_GROUP` and `VTS_EDGE` decide **both** the MQTT topic
(`spBv1.0/<GROUP>/NDATA/<EDGE>`) and the VTScada tag path. If either does not match
the driver's tag addresses, VTScada drops the payload **silently**. Read the real
addresses out of `<appfolder>\Tags\<Type>_<n>\*.tag`; see
`docs/vtscada-wire-contract.md`.

\* The "Req" cells marked with an asterisk are the REST/readback fields. They are
required only in **`readback`** mode (the default). If the bridge cannot reach
VTScada REST at all — disabled, blocked, or not licensed — set
`BRIDGE_CONFIRM_MODE=publish` and those fields are unused: the bridge marks rows
delivered as soon as the MQTT publish succeeds, with no REST access. This is
**weaker**: nothing proves VTScada actually stored the data, and there is no
STANDBY/backlog-on-outage protection. Use it only when REST is genuinely
unavailable, and verify delivery directly in VTScada — `pipeline test` cannot
help here, because it reads the historian back over the REST interface this mode
does not use.

---

## 3. Tag names and data types

| Name | Default | Req | Controls | Example | Restart | Risk |
|---|---|---|---|---|---|---|
| `VTS_TAGS` | `{"AI01":"double","DI01":"bool"}` | yes | tag → datatype; **only listed tags are published** | `{"testVal":"double"}` | yes | data integrity |

Allowed datatypes: `double`, `bool`, `int64`. A tag queued but absent here can
never be delivered; it is excluded from every batch and an `ALARM` names it, so it
cannot starve real data. The name that ends up on the sample (topic name, JSON
field, or `PLC_TAG_MAP` result) must match a key here.

---

## 4. Storage and retention

| Name | Default | Req | Controls | Restart | Risk |
|---|---|---|---|---|---|
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | — | query timeout; a wedged link fails loudly | yes | — |
| `RETENTION_DAYS` | `90` | — | delivered/audit rows older than this are pruned; `pending` rows are never pruned | yes | data retention policy |

`DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`/`DB_SCHEMA` are in §6
(they carry credentials). Database volume: `timescale-data`. Pruning runs in the
bridge at startup and once a day, in bounded batches, and deletes only
`delivery_state='delivered'` rows plus audit rows (`attempts`, `batches`,
`quarantine`) older than the window. Age is measured on **arrival**
(`received_ts`), not the PLC's clock, so a device with a slow or wrong clock does
not have its rows pruned immediately. Set `0` to keep everything forever. The
default of 90 days bounds growth for a technician who never thinks about it.

---

## 5. Reliability and queue limits

| Name | Default | Controls | When to change | Restart |
|---|---|---|---|---|
| `BRIDGE_BATCH` | `50` | samples per Sparkplug payload | larger/smaller payloads | yes |
| `BRIDGE_SETTLE_SEC` | `3.0` | how long to poll the historian for readback | slow historian | yes |
| `BRIDGE_BIRTH_WAIT_SEC` | `8.0` | delay after NBIRTH before delivering | slow host | yes |
| `BRIDGE_WARMUP_PROBE` | `false` | publish a 4242 probe after BIRTH and wait for it | debugging only (writes a real sample) | yes |
| `BRIDGE_WARMUP_TIMEOUT_SEC` | `45.0` | probe wait | — | yes |
| `BRIDGE_POLL_SEC` | `2.0` | idle poll interval | — | yes |
| `BRIDGE_RETRY_DELAY_SEC` | `30.0` | minimum gap before re-attempting an unconfirmed row | historian slow to store | yes |
| `BRIDGE_HEARTBEAT_SEC` | `30.0` | heartbeat log interval | — | yes |
| `BRIDGE_STALL_ALARM_SEC` | `300.0` | alarm if nothing is **confirmed** for this long | tune alarms | yes |
| `BRIDGE_ENABLED` | `true` | run the bridge at all | — | yes |
| `COLLECTOR_TICK_SEC` | `1.0` | collector loop period | — | yes |
| `TS_TOL` | `0.002` | timestamp match tolerance (s) when reading back | unusual clocks | yes |
| `VALUE_TOL` | `1e-9` | value match tolerance when reading back | — | yes |
| `LOG_LEVEL` | `INFO` | log verbosity (`DEBUG` for detail) | troubleshooting | yes |

Do **not** change the tolerances or the retry throttle to "make it deliver" — they
are part of the correctness argument (`docs/ARCHITECTURE.md`). A row that cannot
be matched stays pending and is retried; that is correct behaviour, not an error.

---

## 6. Security and credentials

| Name | Default | Req | Controls | Risk |
|---|---|---|---|---|
| `DB_USER` | `pipeline` (compose: `emqx`) | yes | database user | — |
| `DB_PASSWORD` | *(empty)* (compose: `emqx`) | **yes** | database password; set when the volume is created | **security** |
| `DB_NAME` | `pipeline` (compose: `emqx`) | yes | database name | — |
| `DB_HOST` / `DB_PORT` | `localhost` / `5432` (compose: `timescaledb`) | yes | database location | — |
| `DB_SCHEMA` | `pipeline` | — | schema holding the queue | — |
| `EMQX_DASHBOARD_PASSWORD` | `public1234` | — | EMQX dashboard password (`admin`) at `:18083` | **security** |
| `STATE_DIR` | `/var/lib/pipeline` | — | where `bdSeq` is persisted | data integrity |

**Secure baseline (applied by the installer, `DECISIONS.md` ADR-005):** unique
generated DB and dashboard passwords; `VTS_REST_PASSWORD` required; broker auth
offered; the database port **not** published to the host; `.env` mode `0600`; no
secret ever printed in logs or `pipeline status`. Test vs production: keep a
separate `.env.test` for the simulator profile so the two are never confused.

⚠️ Changing `DB_PASSWORD` after the first start requires recreating the database
volume, which **destroys the queue** — back up first (`docs/BACKUP_RESTORE.md`).

---

## 7. Advanced troubleshooting

* `LOG_LEVEL=DEBUG` then `pipeline logs -f`.
* `pipeline status` reports the REST capability outcome (`ok` / `not-permitted` /
  `unreachable` / `credentials-rejected`) — use it before assuming an outage.
* `python tools/monitor_sparkplug.py` decodes what the bridge publishes (proves
  the payload is well formed; it does **not** prove VTScada accepted it).
* `python tools/verify_history.py tables|show|reconcile` reads the historian back.
* Quarantine: `SELECT created_at, reason, topic, payload FROM pipeline.quarantine
  ORDER BY id DESC LIMIT 20;`
* Pending backlog: `SELECT count(*) FROM pipeline.samples WHERE
  delivery_state='pending';`

See `docs/TROUBLESHOOTING.md` for the symptom → cause → fix order.