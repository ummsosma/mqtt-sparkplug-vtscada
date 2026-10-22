# Upgrading

## Before any upgrade

1. `./deploy/pipeline backup` and verify it (see `docs/BACKUP_RESTORE.md`).
2. Note the current version: `git -C /opt/mqtt-sparkplug-vtscada describe --tags`
   (or the commit hash).
3. Read `CHANGELOG.md` for migration notes.
4. Check `pipeline status` is healthy **before** you change anything. Do not
   upgrade a broken system — you will not know which change caused what.

## Normal upgrade

```bash
cd /opt/mqtt-sparkplug-vtscada
./deploy/pipeline backup
./deploy/pipeline update
```

`update` pulls the release, rebuilds the image, recreates the services, and runs
`pipeline test`. Docker volumes — the queue, broker data and `bdseq` — are
preserved.

Confirm afterwards:

```bash
./deploy/pipeline status      # delivery ok, pending not climbing
./deploy/pipeline test        # must PASS
```

## What can and cannot be rolled back

| Change | Rollback |
|---|---|
| Application code / docs | `git checkout <previous-commit>` then `pipeline update` |
| A setting in `.env` | restore the old value, `pipeline restart` |
| Database **schema** change | **not** automatically reversible. Restore the pre-upgrade backup. |
| A Docker volume / database password change | **not** reversible without the backup. |

Schema changes are applied idempotently at startup (`db.apply_schema()`), and the
only historical migration is `payload: jsonb → text`, which is lossless. There is
no down-migration. If a release changes the schema, the changelog will say so and
the backup is the rollback.

## Downgrading

There is no supported downgrade path for a schema change. If a downgrade is
required:

```bash
./deploy/pipeline stop
git checkout <previous-commit>
./deploy/pipeline restore <pre-upgrade-backup>
./deploy/pipeline start
```

Note that restoring the backup also discards anything queued after it.

## Offline / air-gapped sites

The first `update` needs the release artifact and the Docker images. On a site
with no internet, pre-load them:

```bash
# on a connected machine
docker save emqx/emqx-enterprise:6.3.1 timescale/timescaledb:2.30.2-pg16 \
  mqtt-sparkplug-vtscada:local -o pipeline-images.tar
# on the Pi
docker load -i pipeline-images.tar
```

Then run the update with the release archive rather than `git pull`.

## Release checklist (maintainer)

The full gate list, with the current pass/fail status, is
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). In short: unit tests, a live
`pipeline test`, an outage → recovery → reconcile run, a verified backup/restore,
a clean install, the security checks, and a dated changelog + tag.
