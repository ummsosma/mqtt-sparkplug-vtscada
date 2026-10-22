# Protocols and message contracts

Three interfaces: PLC → broker, bridge → VTScada (Sparkplug B over MQTT), and
bridge → VTScada (REST, for readback). The VTScada quirks are established
experimentally and recorded with evidence in
[`vtscada-wire-contract.md`](vtscada-wire-contract.md) — that file is the
authority; this one summarises and links.

## 1. PLC → broker (ingest)

The collector subscribes to `PLC_TOPIC` (default `plc/#`) at QoS 1 and withholds
PUBACK until the row is committed.

`PLC_DECODER` selects how a message becomes a sample:

| Decoder | Shape | Timestamp |
|---|---|---|
| `json` | object with configurable field paths (`PLC_JSON_*_FIELD`) | required; missing = undecodable |
| `topic_tag` | `plc/<device>/<tag>` with a bare value (or `{value,ts}`) | optional; **arrival time** if absent |
| `sparkplug` | already-encoded Sparkplug B payload | metric timestamp; arrival time if absent |
| `auto` | route on topic (`spBv1.0/`), then body (`{`), then `topic_tag` | as above |

Deduplication: `seq` comes from the payload when present, otherwise from the
source timestamp. `UNIQUE(device_id, seq, tag)` then makes a replay idempotent.

**The real CLICK PLUS at this site:**

```
topic:   plc/1/testVal
payload: -16165
```

`PLC_TOPIC_TAG_LEVELS=2` reads `<device>/<tag>` (`1` / `testVal`). There is **no
timestamp and no tag name in the payload**; the tag comes from the topic and the
time is arrival time. Documented limitation, not a bug.

## 2. Bridge → VTScada (Sparkplug B)

Topic: `spBv1.0/<VTS_GROUP>/NDATA/<VTS_EDGE>` (BIRTH `NBIRTH`, DEATH `NDEATH`).

The contract that VTScada actually accepts (all established experimentally):

* **History is a repeated metric name**, not a JSON blob: one payload containing N
  metrics all named `AI01`, each with its own `timestamp` (ms UTC) and value.
* Datatypes: Int64 = 4, Double = 10, Boolean = 11, with the matching value field.
  The right value with the wrong enum is ignored silently.
* Set both `name` and `alias`.
* One **trailing duplicate element** per tag group (VTScada drops a trailing
  Boolean `false`); it coalesces and never corrupts a real record.
* A payload immediately after the NBIRTH that creates the tags is dropped, hence
  the post-BIRTH wait and optional warm-up probe.
* Re-sending the same timestamp **coalesces** in the historian — this is what
  makes retries safe.
* A topic that matches no tag's address is dropped **without an error**. The tag's
  `ReadAddress` must be `spBv1.0/<G>/NDATA/<E>++<tag>` and its `HistoryAddress`
  the same plus `$$History$$`.

`bdSeq` persists across restarts (a stale BIRTH causes every following message to
be ignored) and the recovery re-BIRTH reuses the same value.

## 3. Bridge → VTScada (REST readback)

```
GET {VTS_REST_URL}/{VTS_REST_REALM}/REST/SQLQuery?query=<url-encoded SQL>
Auth: X-VTScadaAuth: base64(user:password)
```

* `SQLQuery` is fixed and case-sensitive; a wrong function answers HTTP 200 with
  `{"errorMessage":"Unknown REST Function"}`.
* `History` is **wide**: one `Timestamp` column (UTC epoch seconds) plus one
  column per logged variable named `'<VTS_TAG_ROOT>\<GROUP>\<EDGE>\<TAG>:Value'`.
  There is no Name/Time/Value column.
* Pagination: 10 000 rows/page via `nextPageToken` → `PageToken=`; `vtclient`
  follows it.
* A `401` means credentials are wrong — **not** that the historian is down. The
  `pipeline status` classifier distinguishes this.

Delivery is proven only by reading back the exact `(timestamp, value)`. An MQTT
publish `rc=0` proves nothing.

## Boundaries and honesty

* Exactly-once is **not** claimed. A retry can re-publish an event; VTScada
  coalesces it. If a future historian does not coalesce, retries create
  duplicates.
* Ordering is per tag and per batch (`ORDER BY tag, seq`), not global across tags.
* For a PLC with no timestamp, source time = arrival time; the original
  measurement time is lost.
