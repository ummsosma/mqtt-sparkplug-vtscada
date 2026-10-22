# Install

Audience: an industrial technician who has not used Docker, Python, Linux, MQTT
or PostgreSQL. Follow the steps in order. Every command is complete; nothing is
assumed. If a step does not behave as described, stop and read
`docs/TROUBLESHOOTING.md` before continuing.

You will end up with four containers running on a Raspberry Pi, starting
automatically after reboot, and one command (`pipeline status`) that tells you
whether the system is healthy.

---

## 1. What you need

**Hardware**
* A Raspberry Pi 4 or 5, 4 GB RAM or more, with a 32 GB+ SSD/SD card. A Pi 4 with
  2 GB works but leaves little headroom for the database and broker.
* Ethernet to the CLICK PLUS (the Pi can serve DHCP on that cable — see §6).
* Network access from the Pi to the SCADA PC (direct, LAN, or Tailscale).

**Software**
* Raspberry Pi OS (64-bit, Bookworm or later) or Debian/Ubuntu. The installer
  refuses other operating systems on purpose.
* Internet access **during installation only** (Docker images are pulled).
  Routine operation needs no internet.

**On the SCADA PC (not done by this installer)**
* VTScada installed **and the application running** — a running VTScada program is
  not the same as a running application.
* The MQTT Client Driver in Sparkplug B mode with tag auto-creation on.
* The REST interface enabled: Application Configuration → Thin Client/Server
  Setup, a realm (e.g. `odbc`) containing the application, and an account with
  Remote Data Access + Remote Tag Value/History Retrieve.

> **REST access is how delivery is *proved*, and it is the default.** In the
> default `readback` mode everything the system promises depends on reading each
> sample back out of the historian: with no REST, the pipeline collects and queues
> data but can never prove delivery, and it stays in STANDBY rather than pretend
> (see `docs/DECISIONS.md` ADR-003).
>
> If a site will not enable REST, set `BRIDGE_CONFIRM_MODE=publish` instead: the
> bridge then marks rows delivered on MQTT publish success, with no REST at all.
> That is a deliberate, weaker trade — nothing proves the historian stored the
> data, and there is no STANDBY/backlog protection. See
> `docs/CONFIGURATION.md` §2.

---

## 2. Check VTScada is reachable *before* installing

From the Pi (replace the address with your SCADA PC's):

```bash
curl -s -o /dev/null -w "VTScada answered: %{http_code}\n" \
  "http://100.64.1.20:443/odbc/REST/SQLQuery?query=GET+Tables"
```

| Answer | Meaning |
|---|---|
| `401` | **Good.** REST is answering and asking for credentials. |
| `000` / timeout | Not reachable. VTScada application not running, REST not enabled, or the network path is wrong. Fix this first. |
| `404` | Reachable but the realm path is wrong. Check `VTS_REST_REALM`. |

Write down the address, realm, user and password. The installer asks for them.

---

## 3. Put the code on the Pi

The repository is public, so either copy a release archive or clone it directly.
Pick one. A deploy key is no longer needed (the repo is no longer private); if a
site prefers one it still works.

**Option A — release archive (no GitHub account needed on the Pi)**

```bash
sudo mkdir -p /opt
sudo tar -xzf mqtt-sparkplug-vtscada-<version>.tar.gz -C /opt
sudo mv /opt/mqtt-sparkplug-vtscada-<version> /opt/mqtt-sparkplug-vtscada
```

**Option B — clone**

```bash
sudo git clone https://github.com/ummsosma/mqtt-sparkplug-vtscada.git /opt/mqtt-sparkplug-vtscada
```

No credentials are needed: the repository is public. If a site wants the install to
run under a read-only deploy key anyway, generate one
(`ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N ""`), add the public key at
GitHub → Settings → Deploy keys, and clone with `git@github.com:` instead.

Either way you should see `docker-compose.yml`, `src`, `deploy`, `docs`.

---

## 4. Install

```bash
cd /opt/mqtt-sparkplug-vtscada
sudo ./deploy/install.sh
```

The installer:

1. checks the OS, Docker, the Compose plugin, buildx ≥ 0.17 and free disk;
2. asks only for what it cannot know (VTScada address/password, PLC topic shape,
   retention, optional broker auth);
3. **generates** the database and dashboard passwords (it never prints them);
4. validates the configuration and refuses to start on a real error;
5. writes `/opt/mqtt-sparkplug-vtscada/.env` with mode `0600`;
6. builds and starts the stack;
7. installs and enables a boot unit so it starts after reboot.

To check prerequisites only and change nothing:

```bash
sudo ./deploy/install.sh --check
```

Re-running is safe. If `.env` already exists it offers to keep it; it never
overwrites an existing configuration without consent, and it never deletes data.

---

## 5. Verify

```bash
cd /opt/mqtt-sparkplug-vtscada
./deploy/pipeline status
```

Read the **delivery (VTScada REST)** line first:

| It says | What to do |
|---|---|
| `ok` | Good. Continue. |
| `credentials-rejected` | Wrong `VTS_REST_USER` / `VTS_REST_PASSWORD`. Nothing is delivered while this is wrong; the queue is safe. |
| `unreachable` | Network or VTScada application not running. Return to §2. |
| `not-permitted` | REST is refused without a credential error. Ask the site to enable it, or confirm the port/realm. |

Then prove the pipeline works **without the PLC**, using the built-in simulator:

```bash
docker compose --profile sim up -d simulator
./deploy/pipeline logs -f
```

You want batches with `confirmed` equal to `published` and `unmatched=0`. Now the
honest check — read it back out of the historian:

```bash
./deploy/pipeline test
```

`VERDICT: PASS` means every delivered row is present at its source timestamp.
Stop the simulator:

```bash
docker compose --profile sim stop simulator
```

---

## 6. Point it at the real CLICK PLUS

**6a. Give the PLC an address.** If it is on a bare cable to the Pi, make the Pi
the DHCP server on that interface:

```bash
sudo nmcli connection modify netplan-eth0 ipv4.method shared
sudo nmcli connection up netplan-eth0
ip -br addr show eth0          # expect 10.42.0.1/24
```

To reach that PLC from other machines over Tailscale, install the dispatcher
hook (see `deploy/90-allow-plc-subnet` for why it is needed):

```bash
sudo cp deploy/90-allow-plc-subnet /etc/NetworkManager/dispatcher.d/
sudo chmod 755 /etc/NetworkManager/dispatcher.d/90-allow-plc-subnet
sudo chown root:root /etc/NetworkManager/dispatcher.d/90-allow-plc-subnet
```

**6b. Tell the PLC where the broker is** (in the CLICK programming software; this
is the one step that cannot be scripted):

| Setting | Value |
|---|---|
| Broker address | the Pi's address on the PLC cable, e.g. `10.42.0.1` |
| Port | `1883` |
| Username/password | blank unless you enabled broker auth in the installer |
| Topic | something starting `plc/`, e.g. `plc/1/testVal` |

**6c. Confirm what it actually publishes.** Do not guess:

```bash
docker compose exec emqx emqx ctl trace start client "plc*" --topic "plc/#"
docker compose logs -f emqx | tail -40
```

The CLICK PLUS used at this site publishes:

```
topic:   plc/1/testVal
payload: -16165
```

That is `plc/<device>/<tag>` with a bare value. Set it in `.env`:

```ini
PLC_TOPIC=plc/#
PLC_DECODER=auto
PLC_TOPIC_TAG_LEVELS=2
VTS_TAGS={"testVal": "double"}
```

Apply and confirm it took:

```bash
docker compose up -d --force-recreate collector bridge
docker inspect bridge --format '{{range .Config.Env}}{{println .}}{{end}}' | grep VTS_TAGS
```

> **This PLC sends no timestamp.** The pipeline records arrival time instead, so
> the PLC's original measurement time is **not** preserved. At 1 Hz over a LAN the
> difference is milliseconds. It would not be acceptable over a slow link. Never
> describe this deployment as preserving PLC timestamps for this device.

Anything undecodable is quarantined, not dropped:

```bash
docker compose exec timescaledb psql -U "$DB_USER" -d "$DB_NAME" -c \
  "SELECT created_at, reason, topic, payload FROM pipeline.quarantine ORDER BY id DESC LIMIT 10;"
```

---

## 7. Finish

* Confirm `./deploy/pipeline status` shows `ok` and `pending` is small or zero.
* Record, in your site notes: the Pi's address, the SCADA PC's address, the
  VTScada realm, where `.env` lives, and the date of the first successful
  `pipeline test`.
* Set up a backup schedule (`docs/BACKUP_RESTORE.md`).
* Read `docs/OPERATIONS.md` for the everyday commands.

To remove the system later, see `docs/UPGRADING.md` and `pipeline uninstall`.
It keeps your data unless you explicitly confirm deletion twice.
