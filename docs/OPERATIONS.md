# Operations

Audience: the technician running the system day to day. All commands run on the
Pi, from `/opt/mqtt-sparkplug-vtscada`, unless stated otherwise.

`pipeline` is `./deploy/pipeline` (installed by `deploy/install.sh`).

---

## The one command: status

```bash
./deploy/pipeline status
```

It prints three things you must learn to read:

1. **containers** — `emqx`, `timescaledb`, `collector`, `bridge`. `timescaledb`
   shows `(healthy)`. Restarting counts should not climb.
2. **queue** — `pending` is data waiting to be proven in the historian.
   `delivered` is the audit trail. A small, steady `pending` is normal; a
   permanently climbing `pending` is not.
3. **delivery (VTScada REST)** — `ok` / `credentials-rejected` / `unreachable` /
   `not-permitted`. This line tells you *why* data is not moving, which the logs
   alone historically did not.

**Healthy looks like:** four containers up, `pending` flat or falling,
delivery `ok`, and the latest bridge heartbeat showing `confirmed` equal to
`published`.

---

## Everyday commands

| I want to… | Command |
|---|---|
| See whether it is healthy | `./deploy/pipeline status` |
| Watch the pipeline | `./deploy/pipeline logs -f` |
| See only the bridge | `./deploy/pipeline logs -f bridge` |
| Prove the historian has the data | `./deploy/pipeline test` |
| See how many readings are waiting | `./deploy/pipeline status` (queue section) |
| See what could not be decoded | see *Quarantine* below |
| Restart the pipeline services | `./deploy/pipeline restart` |
| Stop everything (keeps data) | `./deploy/pipeline stop` |
| Start again | `./deploy/pipeline start` |
| Apply a settings change | edit `.env`, then `./deploy/pipeline restart` |
| Back up | `./deploy/pipeline backup` |
| Restore | `./deploy/pipeline restore <dir>` (stack stopped) |
| Update | `./deploy/pipeline update` |
| Remove the system | `./deploy/pipeline uninstall` |

> `pipeline restart` applies a `.env` change by recreating the services whose
> configuration changed (`docker compose up -d`). It does **not** use
> `docker compose restart`, which reuses the existing containers and would
> silently ignore your edit.

---

## Reading the logs

Both services log a heartbeat so a problem is visible rather than silent.

```
[collector] counters={'received': 281, 'persisted': 280, 'duplicate_input': 0,
                      'quarantined': 0, 'persist_failed': 0, 'acknowledged': 280}
[bridge]    heartbeat backlog=4 counters={...} since_last_confirm=1s
```

| What you see | What it means |
|---|---|
| `confirmed == published` | Normal. Every reading was proven to land. |
| `unmatched` non-zero, briefly | Normal at an outage boundary. Those rows stay queued and are retried. |
| `STANDBY entered` | VTScada is unreachable. Nothing is published; the queue grows. Check the status line for the *reason*. |
| `ALARM … not in VTS_TAGS` | The PLC sends a tag you did not configure. It can never be delivered; add it to `VTS_TAGS` or stop the PLC publishing it. |
| `quarantined` climbing | The PLC's message shape is not what the decoder expects. Look at the quarantine table. |
| `ALARM no delivery progress for Ns` | The bridge is running but confirming nothing for 5 minutes. Check VTScada and the status line. |
| `retention: pruned {...}` | Normal daily housekeeping. |
| `ALARM retention prune failed` | Pruning could not run; delivery is unaffected. Check disk/database. |

Set `LOG_LEVEL=DEBUG` in `.env` and `pipeline restart` for detail; set it back to
`INFO` afterwards.

---

## Quarantine (messages that could not be read)

```bash
docker compose exec timescaledb psql -U "$DB_USER" -d "$DB_NAME" -c \
  "SELECT created_at, reason, topic, payload FROM pipeline.quarantine ORDER BY id DESC LIMIT 20;"
```

A row here is a message the pipeline refused to guess about. Fix the decoder
configuration or the PLC, not the quarantine table. Quarantine rows age out with
`RETENTION_DAYS`.

---

## Routine maintenance

**Daily (or on the shift check):** `pipeline status` — delivery `ok`, `pending`
not climbing, restart counts stable.

**Weekly:** `pipeline test` (readback reconcile) and a glance at free disk:

```bash
df -h /var/lib/docker
docker system df
```

**Monthly:** `pipeline backup` and confirm the file is readable
(`head -5 <dir>/queue.sql`). Verify a restore on a test machine at least once a
year — an untested backup is not a backup.

**When you change a setting:** edit `.env` → `pipeline restart` → confirm with
`docker inspect bridge --format '{{range .Config.Env}}{{println .}}{{end}}' | grep NAME`.

---

## Disk and retention

`RETENTION_DAYS` (default 90) controls how much delivered history is kept.
`pending` rows are never pruned. To free space immediately, lower it and restart;
pruning runs at bridge start.

Do **not** delete Docker volumes by hand. `pipeline stop` and `pipeline uninstall`
preserve them; only `pipeline uninstall` with two explicit confirmations deletes
them.

---

## Start, stop, reboot

* After a reboot the system starts by itself (boot unit + container restart
  policy). Confirm with `pipeline status`.
* `pipeline stop` is a clean stop. Data stays in the volumes.
* If the Pi loses power, the queue is whatever was committed before the outage;
  anything already PUBACKed is on disk. On boot the bridge resumes and drains.

---

## Updating

```bash
./deploy/pipeline update
```

It pulls the release, rebuilds, restarts, and re-runs `pipeline test`. Volumes are
preserved. See `docs/UPGRADING.md` for rollback limits.

---

## What to record for the next person

Keep a one-page site note with: Pi address, SCADA PC address, VTScada realm,
where `.env` lives, the PLC topic shape and tags, and the date/result of the last
successful `pipeline test`. That page is worth more than any log.
