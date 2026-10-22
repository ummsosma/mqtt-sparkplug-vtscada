# Architecture decisions

One record per decision that is expensive to reverse or that a future maintainer
would otherwise re-litigate. Each records the decision, the reason, and the
alternatives rejected. Decisions marked **(owner)** were taken by the project
owner during the Phase 1 review gate.

---

## ADR-001 — Keep Docker Compose as the runtime **(owner)**

**Decision.** The stack stays a `docker-compose.yml` deployment (broker,
database, collector, bridge). Routine Docker operations are hidden behind a small
documented command set; a technician never types `docker` by hand.

**Why.** The Compose stack is already proven end to end against a real VTScada
historian (see `DEPLOYMENT_AUDIT.md`, `verify-pass`). It pins the broker and
database images, gives restart-on-failure for free, and keeps durable data in
named volumes separate from the images. The gap is the human interface and the
safe defaults, not the runtime.

**Rejected.** Bare-metal Python + systemd (reintroduces pip/venv maintenance and
loses pinned images); a single all-in-one container (merges broker, DB and
pipeline into one failure domain and violates durable-data separation);
Kubernetes/Swarm/message bus (unjustified at this size, and explicitly excluded
by the assignment); a custom installer framework (no benefit over a script).

---

## ADR-002 — Keep `emqx/emqx-enterprise:6.3.1` as pinned; single node only **(owner, corrected)**

**Decision.** Keep the pinned image. Do not switch to `emqx/emqx`, and do not
obtain a commercial licence.

**Why (corrected).** Since EMQX **5.9.0** the Open Source and Enterprise editions
were consolidated into one image with the full feature set, under the Business
Source License (BSL) 1.1. The Enterprise image ships with a bundled **single-node
Community License** that permits limited commercial use, so a single-node
deployment needs no licence key. This is the owner's actual situation: one node,
not a cluster. The original audit entry (F19) assumed a commercial dependency that
does not exist for this deployment.

**Constraints this creates (must be documented, not assumed away).**
* The deployment must remain **single-node**. Running it as a cluster, or for
  "full commercial usage", would require a Commercial License.
* The pin matters: BSL 1.1 is not OSI-open-source. Record the Change Date and the
  licence in `docs/MAINTENANCE.md` so a future maintainer knows the terms and can
  wait out the Change Date to a fully open version if needed.
* Durable sessions (`EMQX_DURABLE_SESSIONS__ENABLE`, `MESSAGE_RETENTION_PERIOD`)
  are present because the feature set is unified — no capability gap to verify.

---

## ADR-003 — Never fake delivery when the REST oracle is unavailable **(owner)**

**Decision.** The readback oracle stays mandatory. If a site does not permit REST
access to VTScada, the system does **not** fall back to "mark delivered on MQTT
publish". Instead it fails loudly and safely: a startup capability preflight, a
distinct log/status message, and a bridge that holds in STANDBY while the queue
grows (invariant 4).

**Why.** Marking a row delivered on the strength of an MQTT ack is the exact
failure the whole design exists to prevent. The owner confirmed some sites restrict
REST access and asked that the system be able to *detect* this rather than assume
it away.

**What is added.** (a) A preflight check that distinguishes "REST not enabled / not
permitted" from "host down" and from "credentials rejected", surfaced by the
installer and the status command. (b) Documentation that REST access is a hard
deployment prerequisite. (c) If a site genuinely cannot enable REST, the documented
alternative is an in-application VTScada function
(`\HistorianManager\WriteHistory` via `\RESTServicesInterface\RegisterCustomFunction`,
noted in `vtscada-wire-contract.md` §4) — a separate project, out of scope here.

**Rejected.** A `--trust-publish` degraded mode (silently weakens the integrity
guarantee); reading VTScada's proprietary history files directly (unsupported, and
"header-only proves registration, not flow").

---

## ADR-004 — Retention is configurable, defaulting to a finite window **(owner)**

**Decision.** The installer collects a retention period for the queue/audit
history. Delivered rows older than the window are eligible for pruning; `pending`
rows are never pruned.

**Why.** Today the tables grow forever "by design" (audit F2). The owner asked for
the window to be a deliberate install-time choice. A default is required so an
installer who does not care still gets bounded growth.

**Design.** A single `RETENTION_DAYS` setting (default to be agreed — proposal:
`90`) plus a periodic prune in the bridge at startup and once a day, pruned in
bounded batches so it never holds a long transaction. `pipeline.attempts` /
`pipeline.batches` / `pipeline.quarantine` follow the same window.
**This needs new code (Phase 5), not just configuration.**

---

## ADR-005 — MQTT authentication is optional, generated at install **(owner)**

**Decision.** The installer offers broker username/password. It is **not**
mandatory, but when chosen it generates credentials, configures EMQX to require
them, and writes them into both the pipeline's `.env` and the PLC configuration
steps.

**Why.** The owner wants authentication available without forcing it (the CLICK
PLUS program must be able to supply credentials; some sites will not). The default
must stop being *anonymous with no path to authentication*, which is what the stack
ships today (audit F1).

**Note.** The database port 5432 will stop being published regardless — that is a
Required fix, not optional (audit F1).

---

## ADR-006 — The repository stays private; install ships as a self-contained artifact

> **Superseded on 2026-10-20** by the public release. The condition below ("until
> the owner has confirmed no identifying information, secrets or keys are
> present") was met: the full history of every file ever added was scanned and no
> credential, key or site identifier was ever committed, so the repo went public.
> The second half of this decision still stands — installation and update must not
> depend on live GitHub access for a field technician, so the release archive
> remains the supported path and a plain `git clone` is the alternative.

**Decision.** The GitHub repo remains private until the owner has confirmed no
identifying information, secrets or keys are present. Installation and update must
therefore not depend on live GitHub access for a field technician; a release
artifact (git archive tarball, or the repo copied with the deploy key as today) is
the supported path.

**Why.** F5 (install depends on a deploy key + `git pull`) and the owner's
instruction to keep the repo private. Internet is available at install time (Q5),
so images may be pulled then, but the *source* must be deliverable without
credentials.

---

## ADR-007 — Do not claim exactly-once; document retry semantics

**Decision.** Keep the current honesty: no end-to-end exactly-once claim. Safe
retry depends on VTScada coalescing equal timestamps; where a retry can duplicate
a publication, the deduplication mechanism and its limit are documented.

**Why.** Audit F18 / invariant 12. Being honest about the boundary is worth more
than a marketing guarantee a 20-year maintainer cannot verify.
