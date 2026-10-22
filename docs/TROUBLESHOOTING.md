# Troubleshooting

Work down this list in order. Almost every symptom is "data is not arriving", and
these causes all look identical from the outside. Each has a different fix.

Start with:

```bash
./deploy/pipeline status
```

---

## 1. Delivery is not `ok`

The status command names the reason. Do not skip this and go log-diving.

### `credentials-rejected`
REST answered `401`/`403`. The account or password in `VTS_REST_USER` /
`VTS_REST_PASSWORD` is wrong, or lacks Remote Data Access / Remote Tag
Value-History Retrieve.

* Nothing is delivered while this is wrong. The queue is safe and grows.
* Fix `.env`, then `./deploy/pipeline restart`.
* This used to look exactly like an outage in the logs (audit F3). It no longer
  does.

### `unreachable`
No answer at `VTS_REST_URL`.

* Is the VTScada **application** running (not just the program)? The Application
  Manager being open is not enough.
* `curl -s -o /dev/null -w "%{http_code}\n" "$VTS_REST_URL/<realm>/REST/SQLQuery?query=GET+Tables"`
  — you want `401`.
* Check the network path: tailnet up, correct IP, no firewall change.

### `not-permitted`
REST is refused without a credential error: REST not enabled, wrong port/realm, or
the site blocks it.

* Confirm the realm is configured and the application is inside it
  (Application Configuration → Thin Client/Server Setup).
* If the site genuinely will not allow REST, this deployment cannot prove
  delivery. See `docs/DECISIONS.md` ADR-003. Do not disable the oracle.

---

## 2. The collector is not receiving anything

`received` in the collector heartbeat is flat.

* Is the PLC publishing? Check the broker:
  `docker compose exec emqx emqx ctl trace start client "plc*" --topic "plc/#"`
  then `docker compose logs -f emqx | tail -40`.
* Is it publishing where the collector listens? `PLC_TOPIC` must match.
  `plc/#` does **not** match a topic with a leading slash or a different root.
* Broker auth: if the PLC uses credentials and the collector does not (or vice
  versa), the connection is refused. Check `MQTT_USERNAME`/`MQTT_PASSWORD`.

---

## 3. Data arrives but is quarantined

`quarantined` climbing. Look at why:

```bash
docker compose exec timescaledb psql -U "$DB_USER" -d "$DB_NAME" -c \
  "SELECT created_at, reason, topic, payload FROM pipeline.quarantine ORDER BY id DESC LIMIT 20;"
```

Common reasons and fixes:

| Reason contains | Cause | Fix |
|---|---|---|
| `no field 'ts'` | JSON payload has no timestamp | send one, or use `topic_tag` |
| `is neither numeric nor boolean` | value is text | fix the PLC value or the field path |
| `before 2020` / `in the future` | PLC clock unset/wrong | give the PLC a clock; check the Pi's time |
| `body is not JSON` | `PLC_DECODER=json` but the body is bare | use `auto` or `topic_tag` |

---

## 4. Queued but never delivered

`pending` climbs, delivery is `ok`, no quarantine growth.

* **Tag not in `VTS_TAGS`.** You will see
  `ALARM … not in VTS_TAGS and will never be delivered: ['1']`.
  Add the tag (with its type) or stop the PLC publishing it. These rows stay
  pending, so they accumulate — and they are the fastest way to spot a second
  publish action left on the PLC.
* **Topic/address mismatch.** `VTS_GROUP`, `VTS_EDGE`, `VTS_TAGS` must match the
  driver's `ReadAddress`. Read them from `<appfolder>\Tags\<Type>_<n>\*.tag`
  (plain text). If the topic does not match exactly, VTScada drops the payload
  **silently**.
* **Wrong datatype.** A double sent with the Boolean enum (or vice versa) is
  ignored with no error. Check `VTS_TAGS`.
* **History address missing.** The tag needs a `HistoryAddress` ending
  `$$History$$`, different from its `ReadAddress`.

---

## 5. Things were working, then stopped after a restart

* **Stale BIRTH.** If `bdSeq` did not persist, VTScada treats the BIRTH as stale
  and ignores everything after it: "tags exist but values never update".
  Check that `STATE_DIR` is writable and the `bridge-state` volume is mounted.
  The bridge now logs a warning if it cannot persist it (audit F9).
* **A batch published inside the post-BIRTH window** does not confirm; it is
  retried automatically. Not a fault.
* **VTScada restarted during an outage.** The bridge re-sends the BIRTH on
  recovery with the same `bdSeq`; you should see it in the log.

---

## 6. Duplicate or missing history rows

* Re-sending the same timestamp **coalesces** in VTScada; that is what makes
  retries safe. A retry can therefore re-publish an event that was already
  stored — the historian still holds one row.
* `pipeline test` reports `missing`, `mismatched` and `duplicate_timestamps`
  separately. Do not treat one as another.
* If `missing` is non-zero, the queue claims delivery for data the historian does
  not have: that is serious. Capture the output and the bridge log before changing
  anything.

---

## 7. Disk or database pressure

* `df -h /var/lib/docker` and `docker system df`.
* Lower `RETENTION_DAYS` and `pipeline restart` to prune sooner.
* A full disk causes persist failures (logged `ALARM persist failed`) and the
  broker redelivers unacked messages. It is not silent, but it is late — watch
  disk before it reaches zero.
* Never `docker compose down -v` to "clean up": that deletes the queue.

---

## 8. When to stop and escalate

Stop and capture evidence (do not "fix" in place) if:

* `pipeline test` reports `missing` or `mismatched` non-zero;
* the queue is growing while delivery says `ok`;
* you are about to change `DB_PASSWORD`, a volume, or the schema.

Capture: `pipeline status`, `pipeline logs` (last 200 lines), the `pipeline test`
output, and the quarantine tail. Those four answer most questions.
