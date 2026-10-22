# The VTScada Sparkplug B wire contract

Everything here was established experimentally against a real VTScadaLIGHT
12.2.31 install (application `testApp`), not read off a specification. Each rule
has an observation behind it, because every one of these failures is **silent**:
the payload goes out, the broker accepts it, and nothing arrives.

`src/pipeline/bridge.py` implements this contract. If you change it, change it
with the evidence below in front of you.

---

## 1. The supported historical-write mechanism

**VTScada's MQTT Client Driver in Sparkplug B mode, plus the tag's History
Address.**

With `AutoCreateTags = 1` the driver creates an I/O tag per metric and sets its
addresses itself. Read out of `<appfolder>\Tags\IO_<n>\*.tag`:

| parameter | value |
|---|---|
| ReadAddress | `spBv1.0/SFTest/NDATA/sf-pi++AI01` |
| HistoryAddress | `spBv1.0/SFTest/NDATA/sf-pi++AI01$$History$$` |
| WriteAddress | `spBv1.0/SFTest/NCMD/sf-pi++AI01` |

Grammar: `<topic>++<metric>`, with `$$History$$` appended for the history address.
Read and History addresses must differ.

### The "array" is a repeated metric name

This is the part that is not obvious and is not in the protocol docs. The
historical data VTScada ingests is **not** a JSON blob with timestamps inside it.
It is one payload whose `metrics` contains N entries **all with the same name**,
each carrying its own `timestamp` (ms UTC) and value:

```python
p = spb.Payload()
for ts_ms, value in samples:
    m = p.metrics.add()
    m.name = "AI01"          # the same name, N times
    m.alias = 1
    m.timestamp = ts_ms      # each element keeps its own source timestamp
    m.datatype = 10          # Double
    m.double_value = value
```

VTScada then writes **every element into its historian at its own timestamp with
its own value**, in one go. A payload with one metric per name updates only the
current value and writes no history.

Evidence: `spb_proof.py --edge sf-proof1 --count 10`, read back through REST:

```
ai 1000-base: 10/10 landed_exact, 0 missing, 0 value mismatch
ai 2000-base: 10/10 landed_exact, 0 missing, 0 value mismatch
di 1000-base:  9/10  (last element, value 0, missing)
di 2000-base:  9/10  (last element, value 0, missing)
duplicate_timestamps: {}
```

Rows read back for one analog tag: `1792022851.389 → 810`, `...852.389 → 811`,
`...853.389 → 812` — exactly the published source timestamps and values, unmoved.

### Datatype enums

| Sparkplug type | enum |
|---|---|
| Int64 | 4 |
| Double | 10 |
| Boolean | 11 |

Writing the correct value field with the **wrong** enum makes VTScada ignore the
metric with no error anywhere. An early probe sent `datatype = 2` (Int16)
together with `double_value`, and that was one of two reasons nothing landed.

Do not reference enum members as `spb.Payload.Metric.Double`: that shape varies
between protobuf runtime majors and crashes at runtime. Assign raw integers.

---

## 2. Measured behavioural limits

**A trailing Boolean element whose value is 0/false is not written.** A Boolean
tag with N=6 in an alternating pattern lands 5/6 — the final `false` never
appears. With the last element `true`, all 6 land. Analog arrays do not show this.

*Workaround, verified:* append one extra duplicate element after the last real
element. With it, N/N real elements land at their exact timestamps and values; the
duplicate is dropped or coalesced and never corrupts a real record. `bridge.py`
does this for every tag group, including for analog, because it is harmless there.

**Warm-up after BIRTH.** An array published ~0.5 s after the NBIRTH that registers
the metrics is dropped. Allow a few seconds, then confirm by reading history back
before trusting delivery. The bridge publishes a distinctive probe (value 4242)
and polls until that exact `(timestamp, value)` appears.

**MQTT publish success proves nothing.** `publish().rc == 0` and QoS 1 acks say
nothing about historian insertion. The only accepted proof is reading the
`History` table back.

**`bdSeq` must persist across restarts.** A host treats a BIRTH whose `bdSeq` is
less than or equal to the last one seen as stale and ignores it — after which
every following DATA message is dropped for sequence mismatch. This produces
exactly "the tags exist but the values never update". The bridge keeps the counter
in `STATE_DIR/bdseq` and increments it, wrapping at 255.

**Topic and address must match.** Publishing to a topic that matches no tag's
address is dropped without error. The payload must go to the topic named in the
tag's Read/History address — node metrics on
`spBv1.0/<GROUP>/NDATA/<EDGE_NODE>`.

**Set both `name` and `alias`** on every metric. Host address schemes are
name-based; alias-only is spec-legal but not universally resolved.

**History lives in a proprietary store** (`<appfolder>\Data\History`), readable
only through VTScada. Never touch it directly, and never judge delivery from files
on disk — a header-only table proves registration, not flow.

---

## 3. The verification oracle: REST `SQLQuery`

```
http://<host>:443/<realm>/REST/SQLQuery?query=<url-encoded SQL>
```

* The function name is fixed and case-sensitive: `SQLQuery`. An invented name
  (`TagValueTable`, `query`, `SQL`) answers
  `{"errorMessage":"Unknown REST Function"}` with **HTTP 200** — a wrong-function
  error, not a server fault.
* Auth: HTTP Basic, or `X-VTScadaAuth: <base64 user:pass>` to stop browsers
  replaying credentials from the URL. The account needs Remote Data Access plus
  Remote Tag Value/History Retrieve.
* REST is **not enabled by default**. Enable it via Application Configuration →
  Thin Client/Server Setup: add the machine under Server Setup (port 5780), then
  create a realm (e.g. `odbc`) containing the application. Port 5780 closes plain
  non-REST paths when no realm is configured — expected, not a fault.
* `GET Tables` enumerates what exists: `History`, `History_1m/1h/1D/1W`,
  `AlarmHistory`, `ActiveAlarms`, …

`History` is **wide format**: one `Timestamp` column (UTC epoch seconds) plus one
column per logged variable, named `'<Full\Tag\Path>:Value'`. There is no
`Name`/`Time`/`Value` column, so a tag path only works as a quoted column literal
and bare `SELECT *` / `COUNT(*)` fail:

```sql
SELECT Timestamp, 'MQTT Context\MQTT-SB\MQTT-DRIVER\SFTest\sf-pi\AI01:Value'
FROM History WHERE Timestamp >= 1792033051 AND Timestamp <= 1792033111
ORDER BY Timestamp
```

Limits: 64 columns, 10 000 rows per page (paginate with the returned
`nextPageToken` resubmitted as `PageToken=`), request ≤ 32 KB
(`SQLQueryMaxRequestLength` in `SETUP.INI` raises it). A batch of 50 samples is
nowhere near the page limit, which is why `vtclient.py` does not paginate.

### Ordering and duplicates

Elements land at their own source timestamps, out of order if you send them out
of order. Re-sending the same timestamp **coalesces onto the existing record**
rather than appending a second one — which is what makes retries after an
interruption safe.

---

## 4. Interfaces that exist but are not used

* `\HistorianManager\WriteHistory(TagObj, TimestampArrayUTC, DataArray)` —
  script-only, blocking, and its return value of `0` means only "parameter sizes
  matched", not "data was written". Reachable from outside the application only
  through an in-app module registering
  `\RESTServicesInterface\RegisterCustomFunction(Name, Self())`. This is the
  fallback for a tag that has no MQTT address.
* `HistorianConnect` / `HistorianWriteRecords` / `HistorianReadRecords` /
  `HistorianGetData` — engine-level, "advanced programmers only".
* The Sparkplug `DataSet` datatype is not supported by VTScada's implementation.

---

## 5. Simulating an outage

VTScada has no start-from-CLI switch for an application, so killing it is not
reversible headlessly. **Suspend the process instead** — `tests/vt_suspend.py`
does this with `OpenProcess(PROCESS_SUSPEND_RESUME)` + `NtSuspendProcess` /
`NtResumeProcess`, needing no administrator rights for a process you own:

```bash
python tests/vt_suspend.py status
python tests/vt_suspend.py suspend      # stops ingestion AND REST
python tests/vt_suspend.py resume
python tests/vt_suspend.py test --secs 5
```

While suspended the application answers neither MQTT ingestion nor REST, which is
exactly what a dead SCADA host looks like — and on resume REST answers again
within ~0.02 s. Killing it instead is *not* reversible without the GUI; note that
starting `VTSLauncher.exe` will bring the Application Manager up and auto-start
the application if it is configured to, which is how a suspended-then-killed
install gets recovered.

---

## 6. Debug order for "tags exist but values are 0/Invalid"

Work down this list before changing any publisher code — each cause has a
different fix and they all look identical:

1. Read the IO tag file for the actual `ReadAddress`; compare with the topic and
   metric name/alias the publisher actually sends.
2. Check `bdSeq`/`seq` discipline (a stale BIRTH is ignored, then everything is).
3. Confirm the datatype enum matches the value field.
4. Confirm the tag has a History address, and that it differs from the Read
   address.
5. Remember the warm-up window and the trailing-Boolean quirk.
6. Only then read the historian back over REST to see what actually landed.
