<!--
A WORKED EXAMPLE: one real deployment, written up step by step. Addresses,
hostnames and the MAC have been replaced with placeholders.
The generic, reusable installation guide is docs/INSTALL.md — follow that first.
-->

# Deploying to the Pi — step by step

Written to be followed by someone who has not done this before. Every command is
here in full; nothing is assumed. If a step does not behave the way this document
says, stop and read [When something is wrong](#when-something-is-wrong) before
carrying on.

Throughout, "the Pi" means `your-pi` and "the SCADA PC" means the Windows machine
running VTScada.

---

## 1. What you are deploying, in plain language

Five pieces, four of which run on the Pi:

| Piece | Where | What it does, in one line |
|---|---|---|
| **EMQX** | Pi, container | The message broker. Everything talks through it. Holds unacknowledged messages for `MQTT_SESSION_EXPIRY_SEC` — 7 days by default — if the collector is not listening. |
| **TimescaleDB** | Pi, container | A database. Holds the queue of readings waiting to be delivered. |
| **collector** | Pi, container | Listens to the PLC, writes each reading into the database, and only tells the broker "got it" once the reading is safely stored. |
| **bridge** | Pi, container | Takes readings out of the database, packages them the way VTScada wants, sends them, then **reads them back to prove they arrived**. |
| **VTScada** | SCADA PC, already installed | The historian. It is not part of this deployment; the Pi talks to it over the tailnet. |

Data path:

```
 CLICK PLUS ──MQTT──► EMQX ──► collector ──► TimescaleDB ──► bridge ──► VTScada
   (the PLC)          (Pi)        (Pi)            (Pi)          (Pi)      (SCADA PC)
                                                                │
                                                   reads back to prove it ─┘
```

**The one idea worth holding onto:** nothing is ever marked "delivered" just
because it was sent. The bridge sends a batch, then asks VTScada "do you have
these readings, at these exact times, with these exact values?" Only readings
that come back confirmed are marked delivered. If VTScada is down, nothing is
marked delivered, nothing is lost, and the queue simply grows until it comes
back.

---

## 2. Before you start

You need:

* the Pi, powered on and reachable (it is on the tailnet as `your-pi`,
  `100.64.1.10`)
* the SCADA PC with VTScada installed **and the application actually running** —
  a running VTScada *program* is not the same as a running *application*; if the
  Application Manager is open but the application is not started, the REST
  interface will not answer
* the VTScada REST password (see step 4)
* a terminal. On Windows, the one that comes with Git for Windows (called
  **Git Bash**) is what these commands are written for.

### The addresses, and where to check them

Write these down before you start. Everything below refers back to this table.

> The values in the "Value here" column are **this specific deployment's**, kept
> because a worked example is easier to follow than a placeholder. Substitute your
> own — the commands to find each one are in the middle column.

| What | How to find it | Value here |
|---|---|---|
| The Pi | `tailscale status` on any tailnet machine | `100.64.1.10` (`your-pi`) |
| The SCADA PC | `tailscale status`, look for your Windows desktop | `100.64.1.20` (`your-scada-pc`) |
| The broker, from a container on the Pi | always the service name | `emqx` |
| The broker, from the PLC | the Pi's address on the PLC's network | see step 8 |
| The database, from a container on the Pi | always the service name | `timescaledb` |

Quick check that the Pi is alive:

```bash
ssh root@100.64.1.10 'hostname; uptime'
```

It should print `your-pi` and an uptime. If it asks for a password, the tailnet
SSH policy is not letting you in as that user — try `ssh <user>@100.64.1.10`
instead.

---

## 3. Put the code on the Pi

The repository is public, so a plain HTTPS clone is enough and the Pi needs no
key:

```bash
cd /root
git clone https://github.com/ummsosma/mqtt-sparkplug-vtscada.git
cd mqtt-sparkplug-vtscada
```

**Optional: a deploy key.** A **deploy key** is a key that belongs to the Pi, can
only read this one repository, and cannot touch anything else in your GitHub
account — worth it if you want the Pi to hold the narrowest possible credential.

**On the Pi**, make a key:

```bash
ssh root@100.64.1.10
mkdir -p /root/.ssh && chmod 700 /root/.ssh
ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N "" -C "your-pi deploy key"
cat /root/.ssh/deploy_key.pub
```

Copy the line it prints (starts with `ssh-ed25519`). **On the SCADA PC or any
machine with `gh` logged in**, add it to the repository:

```bash
gh repo deploy-key add /dev/stdin --repo ummsosma/mqtt-sparkplug-vtscada \
  --title "your-pi" --allow-read <<< "<paste the ssh-ed25519 line here>"
```

Then **back on the Pi**, tell SSH to use that key for GitHub and clone:

```bash
cat >> /root/.ssh/config <<'EOF'
Host github.com
  IdentityFile /root/.ssh/deploy_key
  IdentitiesOnly yes
EOF

cd /root
git clone git@github.com:ummsosma/mqtt-sparkplug-vtscada.git
cd mqtt-sparkplug-vtscada
ls
```

You should see `README.md`, `docker-compose.yml`, `src`, `tools`, `tests`.

> If you would rather not set up a key yet, you can copy the folder across
> instead: on the SCADA PC run
> `scp -r C:/Users/<you>/mqtt-sparkplug-vtscada root@100.64.1.10:/root/`.
> The deploy key is worth the five minutes though, because it makes updating a
> one-line `git pull`.

---

## 4. Write the settings file

The stack is configured entirely by one file, `.env`. It is not in the
repository (it holds passwords), so you create it from the template.

```bash
cd /root/mqtt-sparkplug-vtscada
cp .env.example .env
```

Now edit it — `nano .env` is the simplest editor on a fresh Pi (save with
Ctrl-O, Enter, then exit with Ctrl-X). Only these lines need your attention:

```ini
# The VTScada REST password. This is the ONE value you must supply.
VTS_REST_PASSWORD=<the VTScada admin password>

# Where VTScada answers. This is the SCADA PC's tailnet address.
VTS_REST_URL=http://100.64.1.20:443
```

Leave everything else at its default to begin with. Two defaults worth knowing
about because they look alarming and are not:

* `DB_PASSWORD=emqx` — fine for a test Pi. Change it before this is anything
  other than a test, and note that changing it after the first start means the
  database volume has to be recreated.
* `EMQX_DASHBOARD_PASSWORD=public1234` — the password for the broker's web page
  at `http://100.64.1.10:18083` (user `admin`). Also fine for a test Pi.

### Check that VTScada is actually reachable from the Pi

Do this **before** starting anything. It saves the confusion of debugging the
pipeline when the problem is the network.

```bash
curl -s -o /dev/null -w "VTScada answered: %{http_code}\n" \
  "http://100.64.1.20:443/odbc/REST/SQLQuery?query=GET+Tables"
```

* `401` — good. It is answering and asking for credentials, which is correct for
  a request that did not send any.
* `000` or a timeout — VTScada is not reachable. Check that the **application**
  is running on the SCADA PC (not just the VTScada program), and that its REST
  interface has been enabled (Application Configuration → Thin Client/Server
  Setup, with a realm containing the application).

---

## 5. Start it

```bash
cd /root/mqtt-sparkplug-vtscada
docker compose up -d --build
```

> **One-time prerequisite, already done on this Pi.** `docker compose up --build`
> needs the `buildx` plugin to be reasonably current — compose v5 asks for 0.17.0
> or later, and Debian's own package is 0.13.1, which fails with
> `compose build requires buildx 0.17.0 or later`. If you ever rebuild this Pi
> from scratch, install a current one first:
>
> ```bash
> TAG=$(curl -sL https://api.github.com/repos/docker/buildx/releases/latest \
>       | grep -m1 '"tag_name"' | cut -d'"' -f4)
> mkdir -p /usr/local/lib/docker/cli-plugins
> curl -sL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
>   "https://github.com/docker/buildx/releases/download/${TAG}/buildx-${TAG}.linux-arm64"
> chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
> docker buildx version    # must report 0.17.0 or later
> ```

The first run takes a few minutes: it downloads EMQX and TimescaleDB and builds
the pipeline image. You are looking for four containers reported as `Started` or
`Running`.

Check:

```bash
docker compose ps
```

You want `emqx`, `timescaledb`, `collector` and `bridge` all up, with
`timescaledb` showing `(healthy)`.

Now watch the two pipeline services talk:

```bash
docker compose logs -f collector bridge
```

Good output looks like:

```
[collector] connected to emqx:1883, subscribing to plc/#
[bridge]    NBIRTH published (bdSeq=42) topic=spBv1.0/SFTest/NBIRTH/sf-pi
[bridge]    warm-up probe disabled; starting delivery
[bridge]    heartbeat backlog=0 counters={...} stall=0s
```

Press Ctrl-C to stop watching — that does **not** stop the services.

If `bridge` says `STANDBY entered — VTScada historian unreachable`, that is not a
crash. It means it cannot reach VTScada, so it is deliberately publishing nothing
and letting readings pile up in the database. Go back to the `curl` check in
step 4.

---

## 6. Prove it works, without the PLC

The repository includes a fake PLC. Start it:

```bash
docker compose --profile sim up -d simulator
```

Within a few seconds you should see the bridge confirming data:

```bash
docker compose logs -f bridge
```

```
[bridge] batch=... published=50 confirmed=50 unmatched=0 backlog=0 payload=...B
```

`confirmed=50` out of `published=50` is the thing you are looking for. Now prove
it landed in VTScada's own historian — not just that we sent it:

```bash
docker compose exec bridge python tools/verify_history.py reconcile
```

```
AI01: 194 delivered, 0 missing, 0 mismatched, 197 rows in historian
DI01: 194 delivered, 0 missing, 0 mismatched, 196 rows in historian

TOTAL 388 delivered | missing 0 | mismatched 0
VERDICT: PASS -- every delivered row is present in the historian at its source time
```

That is the whole pipeline verified end to end. Stop the fake PLC:

```bash
docker compose --profile sim stop simulator
```

---

## 7. Prove it survives VTScada going away

This is the reason the design exists, so it is worth seeing once.

```bash
# Stop the fake PLC from publishing (only if it is still running)
docker compose --profile sim stop simulator

# Start it again, so there is live data flowing
docker compose --profile sim up -d simulator
```

Now, **on the SCADA PC**, freeze VTScada (this is reversible and needs no
administrator rights — see `tests/vt_suspend.py` for how):

```bash
python tests/vt_suspend.py suspend
```

Watch the Pi. The bridge stops confirming, and the queue grows:

```bash
docker compose logs -f bridge
# [bridge] STANDBY entered -- VTScada historian unreachable: ...
# [bridge] STANDBY 61s: backlog=192 (publishing nothing; rows stay pending)
```

Check the queue directly:

```bash
docker compose exec timescaledb psql -U emqx -d emqx -c \
  "SELECT delivery_state, count(*) FROM pipeline.samples GROUP BY 1;"
```

`pending` climbing, `delivered` flat. Nothing is lost — it is all in the queue.

Bring VTScada back:

```bash
python tests/vt_suspend.py resume
```

The bridge re-registers its tags and drains the backlog:

```
[bridge] VTScada historian reachable again after 61.2s
[bridge] resuming delivery
[bridge] batch=... published=50 confirmed=50 unmatched=0 backlog=...
```

And finally, the honest check:

```bash
docker compose exec bridge python tools/verify_history.py reconcile
```

Everything that was queued during the outage must now be in the historian at its
original timestamp and value, with `missing 0` and `mismatched 0`.

---

## 8. Point it at the real CLICK PLUS

### 8a. Get the PLC onto a network the Pi can reach

The PLC is connected to the Pi by an Ethernet cable, but right now **neither end
has an address**, so they cannot talk. The Pi's `eth0` is set to ask for an
address from a DHCP server, and there is no DHCP server on that cable. The PLC is
in the same position: it asked, got nothing, and fell back to a self-assigned
address (`169.254.x.x`), which the Pi cannot route to.

The fix is to make the Pi *be* the DHCP server on that cable. NetworkManager can
do this in one command:

```bash
nmcli connection modify netplan-eth0 ipv4.method shared
nmcli connection up netplan-eth0
```

What this does: the Pi takes the address `10.42.0.1` on `eth0` and hands out
addresses in `10.42.0.x` to anything plugged into it. Verify:

```bash
ip -br addr show eth0          # expect 10.42.0.1/24
```

Give the PLC a few seconds, then look for it:

```bash
sudo apt-get install -y arp-scan
arp-scan --interface=eth0 --localnet
```

You are looking for the PLC's MAC address, `aa:bb:cc:dd:ee:ff` (a JTEKT device,
which is the hardware inside an AutomationDirect CLICK). It should come back with
an address like `10.42.0.x`. That address is now stable and is the one to give
the PLC when you configure it.

> This changes only `eth0`. Your WiFi (`wlan0`) and the tailnet are untouched, so
> you will not lose your connection to the Pi by doing this.

### 8a-bis. Making the PLC reachable from your PC, not just from the Pi

If you enable the Pi as a Tailscale subnet router (`--advertise-routes=10.42.0.0/24`)
and approve the route, the PLC is still unreachable from your PC — the ping comes
back `Reply from <pi>: Destination port unreachable`, and nothing ever appears on
`eth0`. This is not a Tailscale problem and the route is fine. It is
NetworkManager.

`ipv4.method shared` installs its own firewall table, and the last two rules in it
reject anything being *forwarded out* to the cable:

```
table ip nm-shared-eth0 {
  chain filter_forward {
    ...
    iifname "eth0" reject
    oifname "eth0" reject      <-- this kills the subnet route
  }
}
```

Shared mode is designed for NAT'd clients going *out*, not for reaching hosts *on*
the shared subnet. So the accept has to be inserted ahead of the reject — and
because NetworkManager rebuilds that table on every activation, it has to be
re-inserted every time. That is what `deploy/90-allow-plc-subnet` is for:

```bash
cp deploy/90-allow-plc-subnet /etc/NetworkManager/dispatcher.d/
chmod 755 /etc/NetworkManager/dispatcher.d/90-allow-plc-subnet
chown root:root /etc/NetworkManager/dispatcher.d/90-allow-plc-subnet

# apply it now without waiting for a reconnect
nft insert rule ip nm-shared-eth0 filter_forward \
    ip saddr { 100.64.0.0/10, 172.16.0.0/12 } oifname "eth0" accept
```

Verify from your PC:

```bash
ping 10.42.0.206
# Reply from 10.42.0.206: bytes=32 time=54ms TTL=126
```

Confirm it survives a restart of the interface — the script logs when it acts:

```bash
nmcli connection up netplan-eth0
journalctl -t plc-subnet -n 3
# plc-subnet[...]: allowed tailnet -> eth0: PLC subnet is now reachable remotely
```

You do **not** need `route add` on Windows. Tailscale installs the route itself
(you can see it with `netstat -rn`: `10.42.0.0/24` via `100.100.100.100` on the
Tailscale interface). A manual `route add` fails without elevation and is
unnecessary anyway.

**Confirmed working on this PLC:** port `502` is open and answers Modbus TCP — a
function `0x11` (Report Server ID) request returns a valid response, so the
PLC's Ethernet stack is live and it is reachable at `10.42.0.206`.

### 8b. Tell the PLC where the broker is

This part is done in the CLICK programming software on a Windows PC, and it is
the one step that cannot be scripted from here. In the PLC's Ethernet/MQTT
settings you need:

| Setting | Value |
|---|---|
| Broker / server address | `10.42.0.1` (the Pi, on the cable you just set up) |
| Broker port | `1883` |
| Username / password | leave blank — this broker is currently anonymous |
| Topic to publish on | something starting `plc/`, e.g. `plc/line1/AI01` or `plc/plc1` |

### 8c. Tell the pipeline what the PLC is sending

**This is the part we cannot finish until we see a real message.** The pipeline
does not know your PLC's message format, and guessing it would be worse than
waiting. So: get the PLC publishing, then look at what actually arrives.

Watch the raw traffic on the broker, from the Pi:

```bash
docker compose exec emqx emqx ctl trace start client "plc*" --topic "plc/#"
docker compose logs -f emqx | tail -40
```

Or subscribe to everything on the PLC's topic from a machine with `mosquitto`
installed:

```bash
mosquitto_sub -h 100.64.1.10 -t 'plc/#' -v
```

Once you have one real message, the shape tells you which setting to change. On
the CLICK PLUS used here the answer turned out to be this, and it is worth using
as your template because it needs almost no configuration:

```
topic:   plc/1/testVal
payload: -16165
```

Read it as three parts, and let the topic do the naming:

```
plc  /  1  /  testVal
 |      |      |
prefix  |      tag name  -> becomes the SCADA tag
        device number
```

That is exactly the `plc/<device>/<tag>` shape, so the last two topic levels are
read as device and tag and **nothing else needs configuring**:

```ini
PLC_TOPIC=plc/#
PLC_DECODER=auto
PLC_TOPIC_TAG_LEVELS=2
VTS_TAGS={"testVal": "double"}
```

If your topic is less helpful — for example a bare `plc/1` with no tag name in it
— say what the number means with a mapping instead:

```ini
PLC_TOPIC_TAG_LEVELS=1
PLC_DEVICE_ID=click1
PLC_TAG_MAP={"plc/1": "DS1"}
VTS_TAGS={"DS1": "double"}
```

The general rule: whatever name ends up on the sample **must also appear in
`VTS_TAGS`**, which is what decides its datatype and whether it is published to
VTScada at all. A tag that is not listed is reported and skipped, never guessed:

```
[bridge] ALARM 1 pending tag(s) are not in VTS_TAGS and will never be delivered: ['1']
```

That message means the PLC is publishing something you have not told the pipeline
about. Either add it to `VTS_TAGS`, or stop the PLC publishing it — but note those
rows stay in the queue, so they will accumulate until you deal with one or the
other. It is also the fastest way to spot a **second** publish action left behind
on the PLC: if you repoint a topic and the old one keeps arriving, you get this
message for the old name.

Two things about this particular PLC worth knowing:

* **It sends no timestamp.** Only the value arrives, so the pipeline records the
  time it received the message rather than the time the PLC measured it. At 1 Hz
  over a LAN the difference is milliseconds, which is fine. It would not be fine
  over a slow or queued link.
* **The value is a bare number** — no name, no unit, no quality flag. If you later
  need to distinguish "the PLC is off" from "the value is zero", that has to come
  from the value itself or from a second tag; the protocol carries no such
  information.

Anything the pipeline genuinely cannot read is not thrown away: it goes into a
quarantine table with the reason and the original message.

```bash
docker compose exec timescaledb psql -U emqx -d emqx -c \
  "SELECT created_at, reason, topic, payload FROM pipeline.quarantine ORDER BY id DESC LIMIT 10;"
```

After changing `.env`, apply it:

```bash
docker compose up -d --force-recreate collector bridge
```

> **Check it took effect.** `--force-recreate` is required — a plain
> `docker compose restart` reuses the existing container and its old environment,
> and the change silently does nothing. Confirm the running container has what you
> wrote:
>
> ```bash
> docker inspect bridge --format '{{range .Config.Env}}{{println .}}{{end}}' | grep VTS_TAGS
> ```

---

## 9. Everyday operations

Everything below is run from `/root/mqtt-sparkplug-vtscada` on the Pi.

| I want to… | Command |
|---|---|
| See what is running | `docker compose ps` |
| Watch the pipeline | `docker compose logs -f collector bridge` |
| See how many readings are waiting | `docker compose exec timescaledb psql -U emqx -d emqx -c "SELECT count(*) FROM pipeline.samples WHERE delivery_state='pending';"` |
| Check the historian really has the data | `docker compose exec bridge python tools/verify_history.py reconcile` |
| See the last few readings for one tag | `docker compose exec bridge python tools/verify_history.py show AI01 --limit 20` |
| See what could not be read | see the quarantine query in step 8c |
| Restart just the pipeline | `docker compose restart collector bridge` |
| Apply a settings change | `docker compose up -d --force-recreate collector bridge` |
| Update to the latest code | `git pull && docker compose up -d --build` |
| Stop everything (keeps all data) | `docker compose down` |
| Stop everything and delete the data | `docker compose down -v` ← **this destroys the queue and the database** |

### Reading the logs

Both services log a heartbeat so a problem is visible rather than silent.

```
[collector] counters={'received': 281, 'persisted': 280, 'duplicate_input': 0,
                      'quarantined': 0, 'persist_failed': 0, 'acknowledged': 280}
[bridge]    heartbeat backlog=4 counters={...} stall=0s
```

| What you see | What it means |
|---|---|
| `confirmed` == `published` | Normal. Every reading was proven to land. |
| `unmatched` non-zero, briefly | Normal at an outage boundary. Those rows stay queued and are retried. |
| `STANDBY entered` | VTScada is unreachable. Nothing is published; the queue grows. |
| `ALARM ... not in VTS_TAGS` | The PLC is sending a tag that is not listed in `VTS_TAGS`, so it can never be delivered. Add it, or fix the PLC. |
| `quarantined` climbing | The PLC's message format is not what the decoder expects. Look at the quarantine table. |
| `ALARM no delivery progress for Ns` | The bridge has been running but confirming nothing for 5 minutes. Check VTScada. |
| `heartbeat ... stall=NNNs` climbing | Same thing, seen from the collector side. |

---

## Before this is anything more than a test

The stack as shipped is deliberately open, because that is what makes it easy to
get working. Three things to close before it faces anything real:

1. **The broker has no authentication.** Anything that can reach port 1883 can
   publish onto `plc/#` — i.e. inject readings — or subscribe to `spBv1.0/#` and
   read everything the bridge sends. Set `MQTT_USERNAME` / `MQTT_PASSWORD` and
   configure EMQX to require them.
2. **The database password is `emqx` and port 5432 is published.** Change
   `DB_PASSWORD` (and recreate the volume, since it is set when the container is
   first created), and drop the `ports:` entry for `timescaledb` unless something
   outside the stack genuinely needs to reach it.
3. **`EMQX_DASHBOARD_PASSWORD` is `public1234`.** The dashboard on port 18083 is
   reachable from anything that can route to the Pi.

None of these are exotic, but none of them are on by default either — and a
blank-slate reader deserves to be told rather than left to notice.

## When something is wrong

Work down this list before changing anything. Each of these looks identical —
"data is not arriving" — and each has a different fix.

1. **Is VTScada's *application* running?** Not the VTScada program — the
   application. Check from the Pi:
   `curl -s -o /dev/null -w "%{http_code}\n" "http://100.64.1.20:443/odbc/REST/SQLQuery?query=GET+Tables"`
   — you want `401`. Anything else means the problem is on the SCADA side.
2. **Does the bridge think VTScada is up?**
   `docker compose logs bridge | grep STANDBY`
3. **Is data arriving from the PLC at all?** The collector's `received` counter
   in its heartbeat should be climbing. If it is flat, the PLC is not publishing,
   or it is publishing to a topic the collector is not subscribed to (`PLC_TOPIC`).
4. **Is it arriving but not being read?** Check the quarantine table.
5. **Is it queued but not delivered?** Check `VTS_GROUP`, `VTS_EDGE` and
   `VTS_TAGS` against the tag addresses in VTScada's own tag files
   (`<appfolder>\Tags\<Type>_<n>\*.tag`, plain text — look for `ReadAddress`).
   If the topic the bridge publishes to does not exactly match the topic in the
   tag's address, VTScada drops it silently with no error anywhere.
6. **Only then** read the historian back and see what actually landed:
   `docker compose exec bridge python tools/verify_history.py show AI01 --limit 20`

The full list of known silent-failure causes, with the evidence behind each, is
in [vtscada-wire-contract.md](vtscada-wire-contract.md) — section 6 in
particular is the debugging order.

---

## What is running where, in one picture

```
┌─────────────────────── your-pi (100.64.1.10) ────────────────────────┐
│                                                                        │
│  ┌────────────┐   ┌──────────────┐   ┌───────────┐   ┌──────────────┐  │
│  │   emqx     │◄──│  collector   │──►│timescaledb│◄──│    bridge    │  │
│  │  :1883     │   │  (durable    │   │  queue in │   │  (encodes +  │  │
│  │  :18083    │   │   queue)     │   │  schema   │   │   verifies)  │  │
│  └─────▲──────┘   └──────────────┘   │ 'pipeline'│   └──────┬───────┘  │
│        │                             └───────────┘          │          │
│        │ MQTT                                                │          │
│  ┌─────┴──────┐                                        readback        │
│  │ CLICK PLUS │                                        over REST        │
│  │ 10.42.0.x  │                                              │          │
│  │  via eth0  │                                              │          │
│  └────────────┘                                              │          │
└──────────────────────────────────────────────────────────────┼──────────┘
                                                               │ tailnet
                                                    ┌──────────▼──────────┐
                                                    │  your-scada-pc      │
                                                    │  100.64.1.20        │
                                                    │  ┌───────────────┐  │
                                                    │  │ VTScada       │  │
                                                    │  │ + historian   │  │
                                                    │  └───────────────┘  │
                                                    └─────────────────────┘
```
