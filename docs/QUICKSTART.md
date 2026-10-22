# Quick Start — 10 minute setup (non-technical)

This is the shortest path from "nothing" to "PLC data landing in VTScada". No
Python writing, no deep Docker knowledge. You only need to be able to copy
files and answer a few questions.

It assumes:

  * a machine (a Raspberry Pi is ideal) running Raspberry Pi OS / Debian /
    Ubuntu, with internet access for the first install,
  * a VTScada server you can reach from that machine (same LAN is fine; the
    optional VPN / Tailscale path is at the end),
  * your PLC publishing over MQTT on topic `plc/#`.

If a step sounds like a lot, it is not — each one is a single command.
Stop and read `docs/INSTALL.md` only if a step fails.

## 1. Get the software onto the machine

On the Pi (or whatever machine will run it):

    sudo mkdir -p /opt
    cd /opt
    # copy or clone the repo here, then:
    sudo ./deploy/install.sh

The installer asks you a handful of plain-English questions:

  * VTScada REST URL and password — how the machine reaches VTScada.
  * PLC topic and how the data is shaped (choose **auto** unless you know).
  * The tags you want to send, e.g. `{"AI01":"double","DI01":"bool"}`.

Leave the default answer (the value in brackets) when you are unsure.

## 2. Confirm it is healthy

    sudo /opt/mqtt-sparkplug-vtscada/deploy/pipeline status

You want to see:

  * all containers up,
  * `delivery: ok` (VTScada REST reachable) — or the guidance it prints if not,
  * a pending count that falls toward its steady floor as data flows.

## 3. Watch it work (and change settings later)

    sudo /opt/mqtt-sparkplug-vtscada/deploy/pipeline logs -f     # live data
    sudo /opt/mqtt-sparkplug-vtscada/deploy/pipeline configure   # change settings

`configure` walks you through each important setting and validates what you
type, so you cannot break the config with a typo. After a change run
`pipeline restart`.

## Common variants

### "My PLC does not send JSON, just a topic + a number"

That is the `topic_tag` shape. In `pipeline configure`, set:

  * PLC decoder: `topic_tag`
  * PLC topic tag levels: `2`  (topic `plc/<device>/<tag>`) or `1` (`plc/<tag>`)

### "The payload shape is new to me and nothing decodes it"

Use a **payload mapping file** instead of writing code. Copy the example, edit
the tag names / field names / scaling, then tell the installer to use it:

    cp plc-mapping/example.json plc-mapping/plc.json
    $EDITOR plc-mapping/plc.json     # the tags your PLC sends, and any rename/scale
    sudo ./deploy/pipeline configure # choose 'mapping' when asked how to read a message

`configure` then asks for the mapping file path (the default is right) and
remembers it. Nothing else to set.

See docs/CONFIGURATION.md → "Mapping a new payload shape" for the full schema.

### "I cannot reach VTScada REST from the pipeline machine at all"

If the bridge cannot reach VTScada's REST interface (not configured, blocked,
or not licensed), set the confirm mode to `publish`:

    # in .env, or via `pipeline configure`
    BRIDGE_CONFIRM_MODE=publish

Rows are then marked delivered as soon as the MQTT publish succeeds, with no
REST readback. Read docs/CONFIGURATION.md first: this drops the guarantee that
data really landed in the historian.

### "VTScada is on a different machine, not on my LAN"

Give the bridge a reachable address for the VTScada box. On a normal LAN just
set `VTS_REST_URL=http://<that-machine's-LAN-IP>:443`. You do NOT need
Tailscale unless the two machines are on separate networks.

## Only read the rest of these when you hit a problem

  * `docs/INSTALL.md` — full clean install and every prerequisite.
  * `docs/TROUBLESHOOTING.md` — symptom → cause → fix.
  * `docs/BACKUP_RESTORE.md` — backing up the queue (`pipeline backup`).
  * `docs/CONFIGURATION.md` — every setting, alphabetically.
  * `docs/SECURITY.md` — credentials, exposure, incident response.
  * `docs/ARCHITECTURE.md` — how the pieces fit and why.

## The one-command summary

    pipeline status      health in one screen
    pipeline logs -f     watch data flow
    pipeline configure   change settings safely
    pipeline simulate    run a fake PLC so you can test with no hardware
    pipeline start/stop  start / stop (data is kept)
    pipeline test        prove every delivered row is in the historian
    pipeline backup      back up the queue

## Testing it with no PLC attached

    pipeline simulate        # publishes sample AI01/DI01 data to plc/#
    pipeline status          # the queue should start draining
    pipeline simulate stop

That exercises the whole chain — broker, collector, queue, bridge — without any
hardware. Use it to confirm the install before wiring up the real PLC.
