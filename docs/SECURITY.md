# Security

This system moves industrial process data across a network and stores a queue of
it. Treat the Pi, the broker and the database as plant equipment, not as a hobby
project.

## Secure baseline (what the installer does for you)

`deploy/install.sh` applies these on a fresh install:

* **Unique generated passwords** for the database and the EMQX dashboard. The
  shipped defaults (`emqx`, `public1234`) are never used.
* **`VTS_REST_PASSWORD` is required.** The installer refuses to proceed without
  it, because nothing can be confirmed delivered without REST.
* **The database port 5432 is not published** to the host or the network. Only
  the collector and bridge reach it, over the Compose network.
* **`.env` is written with mode `0600`** and secrets are never echoed to the
  terminal or to `pipeline status`.
* **Broker authentication is offered** (optional — see below).
* **No service is exposed to the public internet**, and the installer never
  weakens existing firewall rules.

## Broker authentication (optional by decision)

The installer can generate a broker username/password and configure EMQX to
require it. It is optional because some CLICK PLUS programs cannot supply
credentials, and forcing it would make the system undeployable at those sites.

**If you leave it anonymous**, anything that can reach port 1883 can:

* publish onto `plc/#` — inject false readings into the historian;
* subscribe to `spBv1.0/#` — read everything the bridge sends.

On an isolated plant LAN behind a firewall that may be acceptable. On any network
you do not fully control, enable authentication. This is a site decision, not a
default the installer makes for you silently.

## Network exposure

| Port | Service | Exposure |
|---|---|---|
| 1883 | MQTT | needed by the PLC; restrict to the PLC subnet and the Pi |
| 18083 | EMQX dashboard | **management** — bind to localhost or the tailnet only; never the public internet |
| 5432 | PostgreSQL | **not published**; do not re-add it |
| 443 | VTScada REST | outbound from the Pi to the SCADA PC only |

⚠️ `docker-compose.yml` as shipped maps `18083:18083`, which publishes the
dashboard on **every interface of the host**. The row above is a requirement for
the site, not something the Compose file enforces: restrict it with a host
firewall, or bind the mapping to loopback with an override such as
`"127.0.0.1:18083:18083"`, if the Pi is not on a segment you fully control. The
database port is the one that *is* restricted in the file — it is never published.

Use a host firewall (or the site's) to allow 1883 only from the PLC's address.
Tailscale is the supported remote-access path; do not port-forward the Pi.

## Credentials

* Live only in `.env` (mode 0600) and the VTScada REST account. Never in Git.
* The values in `.env.example` (`emqx`, `public1234`, `admin`) are convenience
  defaults for a local run, **not** production credentials. The installer
  generates unique ones. A guard, `tests/check_no_secrets.py`, keeps a real
  secret or a committed `.env` out of the tracked tree and prints these defaults
  as a reminder.
* Use a VTScada REST account with read-only historian access, not an admin
  account (`VTS_REST_USER`).
* Never paste them into a ticket, chat or log. `pipeline status` redacts.
* Rotating the **database** password requires recreating the volume, which
  destroys the queue — back up first.
* Rotating the **broker** password requires updating both `.env` and the PLC.
* Rotating the **VTScada** password requires only `.env` + `pipeline restart`.

## TLS and certificates

`MQTT_TLS=false` by default. Enable it (`MQTT_TLS=true`, port 8883) when the
broker link crosses an untrusted network. When enabled, the client validates the
broker certificate using the system trust store; a private CA requires mounting
its certificate into the container and pointing the client at it — this is not
automated today and must be documented per site.

VTScada REST is HTTP by default. It carries the REST password in a header
(`X-VTScadaAuth`, base64), so on an untrusted path it is only as private as the
network. Use the tailnet (encrypted) rather than a raw WAN link.

## Backups are sensitive

A backup contains every queued reading and the audit trail. `pipeline backup`
creates `0700` directories and `0600` files; keep them on encrypted media and
restrict who can read them. Do not email them.

## Test vs production

Keep them visibly separate:

* a test install uses its own `.env.test` and the `sim` profile;
* never point a test install at the production broker or historian;
* the simulator publishes to the configured `PLC_TOPIC` — set a distinct root
  (e.g. `sf-sim/#`) so simulated data cannot be mistaken for plant data.

## Incident response

| Symptom | First action |
|---|---|
| Unexpected tags or values in the historian | Check broker auth and who can reach 1883; review `pipeline.samples` for `topic` values you do not recognise. |
| Dashboard login from an unknown address | Change the dashboard password, restrict 18083, review EMQX logs. |
| `.env` or a backup exposed | Rotate the VTScada password and broker credentials; recreate the DB volume from a clean backup. |
| Suspected tampering with the queue | Stop the bridge (`pipeline stop`), preserve a backup **before** changing anything, then investigate. |

## What this baseline does NOT do

* It does not force broker authentication (by owner decision, ADR-005).
* It does not provision TLS certificates for you.
* It does not manage OS patching or SSH hardening — that is the site's
  responsibility and should be in the site's own maintenance plan.

## Reporting a security issue

This repository is public. Report a vulnerability through GitHub's private
vulnerability reporting — Security tab → **Report a vulnerability** (the form is
`.github/ISSUE_TEMPLATE/VULNERABILITY_REPORT.yml`). That reaches the owner
privately and can become a security advisory with a fix and a CVE.

Do not open a public issue with exploit details, and do not disclose a
vulnerability in a pull request.

Supported versions: `main` and the latest release tag; older tags are fixed on a
best-effort basis. Private vulnerability reporting is a repository *setting*, not
a file, so if the Security tab shows no report button the owner must enable it:
Settings → Security and quality → Private vulnerability reporting.
