# Backup and restore

> **The queue is the data.** Everything the PLC sent and the historian has not
> yet confirmed lives in the `timescale-data` volume. Losing it loses readings
> that were never delivered. Back it up.

## What is in a backup

`./deploy/pipeline backup` writes one directory containing:

| File | Contents | Critical? |
|---|---|---|
| `queue.sql` | `pg_dump` of the `pipeline` schema — samples, quarantine, attempts, batches, checkpoints | **yes** |
| `bdseq` | the Sparkplug BIRTH/DEATH counter | yes (a stale BIRTH is silently ignored) |
| `emqx-data.tar` | broker data volume | non-critical (sessions are transient) |
| `MANIFEST.txt` | time, schema, pending count at backup | for verification |

`pipeline backup` never stops the system. It uses a consistent `pg_dump`
snapshot, so the queue is captured as of one moment.

## Take a backup

```bash
cd /opt/mqtt-sparkplug-vtscada
./deploy/pipeline backup                 # → backups/<UTC timestamp>/
./deploy/pipeline backup /mnt/usb/backups # → a chosen directory
```

The directory and files are created mode `0700`/`0600`. **They contain your
data** — protect them like the database itself, and do not put them anywhere
world-readable.

Verify immediately (a backup you have not opened is not a backup):

```bash
head -5 <dir>/queue.sql          # should show PostgreSQL dump header
grep -c "COPY pipeline.samples" <dir>/queue.sql
cat <dir>/MANIFEST.txt
```

Copy the directory off the Pi (encrypted disk or a protected share). A backup on
the same failing disk is not a backup.

## Restore

> **Read this before typing.** Restore **replaces** the current `pipeline`
> schema. Any samples queued since the backup are lost. If the current queue
> still matters, take a fresh backup first.

1. Stop the stack (keeps volumes):

   ```bash
   ./deploy/pipeline stop
   ```

2. Restore:

   ```bash
   ./deploy/pipeline restore /path/to/backup
   ```

   It refuses to run against a running stack, recreates `timescaledb`, drops the
   old `pipeline` schema, loads `queue.sql`, and restores `bdseq`.

3. Verify the restored queue:

   ```bash
   ./deploy/pipeline start
   ./deploy/pipeline status          # pending should match MANIFEST.txt
   ./deploy/pipeline test            # delivered rows must still reconcile
   ```

   A restore is successful when `pending` matches the manifest **and**
   `pipeline test` still passes. If `pending` is lower than the manifest, the
   dump was incomplete — do not start delivery; restore again from a good copy.

## Recovery without a backup

If the Pi is lost and there is no backup:

* Data already **delivered** is safe in the VTScada historian.
* Data still **pending** is gone. It was never confirmed, so the historian never
  had it.
* Replace the Pi, install, and point it at the same PLC and VTScada. Delivery
  resumes with new samples. There is no way to recover the lost pending window.

This is the argument for a scheduled backup: it is the only way to recover
undelivered readings after hardware loss.

## What never to do

* `docker compose down -v` — deletes all volumes. `pipeline stop` does not.
* Delete `timescale-data` "to clear space" — lower `RETENTION_DAYS` instead.
* Copy a live PostgreSQL data directory with `cp` — use `pipeline backup`.
* Restore while the stack is running.
