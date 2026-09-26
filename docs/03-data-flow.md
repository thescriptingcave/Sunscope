# 03 — Data Flow

## 1. MQTT topic structure

```
solar/{site}/block/{block}/inverter/{inverter_id}/telemetry
solar/{site}/block/{block}/inverter/{inverter_id}/string/{string_id}/telemetry
solar/{site}/block/{block}/inverter/{inverter_id}/status
solar/{site}/weather/{station_id}/telemetry
solar/{site}/rollup
solar/{site}/events
```

The topic hierarchy **is** the entity hierarchy. This is the main reason MQTT was chosen over a
flat topic scheme: a consumer subscribes to `solar/mojave/block/BLK-A/inverter/+/telemetry`
and gets exactly one inverter's data with no payload-level filtering required.

**Identity is carried twice** — once in the topic and once in the JSON payload. The topic is for
routing and subscription; the payload is the ingest contract. The duplication is required by the
ingest mechanism, not stylistic; §2.0 explains why and which one wins if they disagree.

| Property | Telemetry | Status | Rollup | Events |
|---|---|---|---|---|
| Retained | no | **yes** | **yes** | no |
| QoS | 0 | 1 | 1 | 1 |
| Publish interval | 10–60 s | on change | 10–60 s | on event |
| Ingested to InfluxDB | yes | **no** | yes | yes |
| Payload | JSON | JSON | JSON | JSON |

**Retained messages** on `status` and `rollup` mean a newly connected client immediately knows
current state without waiting for the next publish. This is what makes the PWA's cold start
instant.

**QoS 0** on high-rate telemetry is deliberate: a dropped telemetry sample is far less harmful
than added latency, and at 10–60 s intervals with no acknowledgement needed there is nothing to
gain from QoS 1's round trip. Status, rollup, and events use QoS 1 because losing a state change
or an alarm is unacceptable.

## 2. Payloads

### 2.0 Payload contract — read this before §2.1

**Every payload is self-describing: it repeats the identity that the topic already carries.**

```json
// topic: solar/mojave/block/BLK-A/inverter/INV-01/telemetry
{ "site": "mojave", "block": "BLK-A", "inverter_id": "INV-01", "model": "SG250CX", ... }
```

| Key | Present in | Rule |
|---|---|---|
| `ts` | all | RFC 3339 with offset. Becomes the row timestamp. **Required.** |
| identity fields | all | The same values as the topic segments. Become **tags**. |
| measurement fields | all | The columns of the target table. **Never a tag.** |

This duplication is deliberate, and it is load-bearing rather than stylistic:

- The **topic** is the subscription and routing mechanism. `solar/+/block/+/inverter/+/telemetry`
  selects exactly one table, and a human reading the broker sees the structure.
- The **payload** is the ingest contract. Telegraf's `topic_parsing` mechanism, which would map
  topic segments to tags, reports `measurement length does not equal topic length` for
  configurations that are provably correct — reproducibly, and inconsistently between otherwise
  identical files. The identity therefore travels in the payload, where `json_v2` reads it
  unambiguously.

Consequences worth internalising:

- **The payload is authoritative.** If a topic segment and a payload field disagree, the payload
  wins, because that is what determines the tags written to InfluxDB.
- **Identity fields are required, measurement fields are optional.** A point with no identity is
  meaningless and is rejected; a point missing one optional reading is still written. This is
  enforced in `telegraf.conf` by leaving `json_v2.tag` paths required and setting
  `optional = true` on every `json_v2.field`.
- **Never add an identity field to a payload without adding it to the table's tag set.** Tag
  definitions are immutable, and a tag arriving that the table does not declare permanently
  alters the primary key. See [Architecture §5.1](./02-architecture.md#51-schema-before-data-because-influxdb-3-tag-immutability).
- **`topic` is not a field.** `inputs.mqtt_consumer` defaults `topic_tag` to `"topic"`; every
  input block sets `topic_tag = ""` to prevent it. This was got wrong once already.

The authoritative list is the Telegraf config itself. These five tables are what actually runs,
verified end to end against a live InfluxDB:

| Payload | Tags (required) | Fields (optional) |
|---|---|---|
| inverter telemetry | `site`, `block`, `inverter_id`, `model` | `ac_power_w`, `dc_power_w`, `ac_voltage_v`, `ac_current_a`, `dc_voltage_v`, `dc_current_a`, `efficiency`, `heatsink_temp_c`, `internal_temp_c` (float) · `uptime_s`, `status_code` (int) · `clipping` (bool) |
| string telemetry | `site`, `block`, `inverter_id`, `string_id` | `dc_power_w`, `dc_voltage_v`, `dc_current_a`, `module_temp_c` (float) |
| weather station | `site`, `station_id` | `ghi`, `dni`, `dhi`, `air_temp_c`, `wind_speed_mps`, `relative_humidity`, `clearness_index` (float) |
| site rollup | `site` | `total_ac_power_w`, `daily_yield_kwh`, `pr_ratio`, `capacity_factor` (float) · `inverters_online`, `strings_online` (int) |
| events | `site`, `severity`, `source` | `code`, `message` (string) · `value`, `threshold` (float) |

`status` (§2.4) is not ingested by Telegraf — it is retained MQTT state for the PWA and FastAPI
— but it follows the same rule and repeats `site`, `block` and `inverter_id`.

**One deliberate asymmetry: `events` carries two tags that are not in the topic.** Its topic is
`…/events`, which contains no device identity, yet `severity` and `source` are both tags in the
`events` table. Any consumer of events — an alert feed, a Grafana variable, the PWA alert list —
must therefore read the payload to know which device an event came from. That is unavoidable
given the topic design, and it is why `source` is constrained to a small set of device ids rather
than being a free-form string: it is part of the primary key, and cardinality there multiplies
series without bound.

### 2.1 Inverter telemetry

Topic: `solar/mojave/block/BLK-A/inverter/INV-01/telemetry`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "block": "BLK-A",
  "inverter_id": "INV-01",
  "model": "SG250CX",
  "ac_power_w": 248913.4,
  "dc_power_w": 291044.2,
  "ac_voltage_v": 480.1,
  "ac_current_a": 318.4,
  "dc_voltage_v": 1187.3,
  "dc_current_a": 245.1,
  "efficiency": 0.8552,
  "heatsink_temp_c": 68.4,
  "internal_temp_c": 51.2,
  "uptime_s": 287412,
  "status_code": 3,
  "clipping": true
}
```

Note `ac_power_w` (248 913) sitting just under the 250 000 W rating with `clipping: true` — that
pair is how clipping is distinguished from a genuinely underperforming inverter.

### 2.2 String telemetry

Topic: `solar/mojave/block/BLK-A/inverter/INV-01/string/STR-01/telemetry`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "block": "BLK-A",
  "inverter_id": "INV-01",
  "string_id": "STR-01",
  "dc_power_w": 97012.8,
  "dc_voltage_v": 1187.3,
  "dc_current_a": 81.7,
  "module_temp_c": 58.9
}
```

### 2.3 Weather station

Topic: `solar/mojave/weather/WS-01/telemetry`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "station_id": "WS-01",
  "ghi": 981.4,
  "dni": 913.2,
  "dhi": 68.2,
  "air_temp_c": 34.8,
  "wind_speed_mps": 1.8,
  "relative_humidity": 0.19,
  "clearness_index": 0.94
}
```

`clearness_index` (kt) is published explicitly. It is the single most useful diagnostic for
distinguishing a cloudy day from a broken sensor, and having it as a field means Grafana does
not have to derive it.

### 2.4 Status (retained)

Topic: `solar/mojave/block/BLK-A/inverter/INV-01/status`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "block": "BLK-A",
  "inverter_id": "INV-01",
  "state": "producing",
  "status_code": 3,
  "last_seen": "2026-09-25T12:34:07Z",
  "firmware": "4.2.1"
}
```

`state` ∈ `offline` | `standby` | `producing` | `derating` | `fault`

The **Last Will and Testament** is registered as the same payload with `state: "offline"` and
`status_code: 0` at connect time. EMQX publishes it automatically if the connection drops
unexpectedly. This is the mechanism that detects inverter power loss in seconds without any
polling.

**Comms loss does not trip the LWT** — the session stays alive, so nothing is published. That
gap is detected by a staleness check instead, and the difference matters; see [Design §5](./01-design.md#5-fault-scenarios).

### 2.5 Site rollup (retained)

Topic: `solar/mojave/rollup`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "total_ac_power_w": 995653.6,
  "daily_yield_kwh": 6234.1,
  "pr_ratio": 0.8712,
  "capacity_factor": 0.5124,
  "inverters_online": 4,
  "strings_online": 12
}
```

### 2.6 Events

Topic: `solar/mojave/events`

```json
{
  "ts": "2026-09-25T12:34:07Z",
  "site": "mojave",
  "severity": "warning",
  "source": "INV-02",
  "code": "CLIPPING_HIGH",
  "message": "Inverter clipped for 47 minutes today",
  "value": 0.92,
  "threshold": 0.5
}
```

`severity` ∈ `info` | `warning` | `critical` — a **tag**, so a high-cardinality or unbounded value
here would multiply series. Keep the set small. `source` is the device or subsystem the event
came from, also a tag, and — uniquely among all five payloads — **neither is present in the
topic**. See §2.0.

## 3. InfluxDB schema

Database: `solar`. Five tables. **Tag sets are immutable** — created by
`scripts/init-influx.sh` before the first publish.

### 3.1 `inverter_telemetry`

| Kind | Columns |
|---|---|
| Tags | `site`, `block`, `inverter_id`, `model` |
| Fields | `ac_power_w`, `dc_power_w`, `ac_voltage_v`, `ac_current_a`, `dc_voltage_v`, `dc_current_a`, `efficiency`, `heatsink_temp_c`, `internal_temp_c`, `uptime_s`, `status_code`, `clipping` |

Primary key: (`site`, `block`, `inverter_id`, `model`, `time`) — in tag arrival order.

### 3.2 `string_telemetry`

| Kind | Columns |
|---|---|
| Tags | `site`, `block`, `inverter_id`, `string_id` |
| Fields | `dc_power_w`, `dc_voltage_v`, `dc_current_a`, `module_temp_c` |

### 3.3 `weather_station`

| Kind | Columns |
|---|---|
| Tags | `site`, `station_id` |
| Fields | `ghi`, `dni`, `dhi`, `air_temp_c`, `wind_speed_mps`, `relative_humidity`, `clearness_index` |

### 3.4 `site_rollup`

| Kind | Columns |
|---|---|
| Tags | `site` |
| Fields | `total_ac_power_w`, `daily_yield_kwh`, `pr_ratio`, `capacity_factor`, `inverters_online`, `strings_online` |

### 3.5 `events`

| Kind | Columns |
|---|---|
| Tags | `site`, `severity`, `source` |
| Fields | `code`, `message`, `value`, `threshold` |

### 3.6 Cardinality

| Table | Series | Points/day @ 60 s |
|---|---|---|
| `inverter_telemetry` | 4 | 5 760 |
| `string_telemetry` | 12 | 17 280 |
| `weather_station` | 1 | 1 440 |
| `site_rollup` | 1 | 1 440 |
| `events` | low | sparse |
| **Total** | **18** | **~25 920** |

~26 k points/day, ~9.5 M/year. Trivially small, which is the point — it keeps the inner loop fast
and makes accidental schema mistakes cheap to fix (drop table, re-ingest a day).

**Cardinality is deliberately minimal.** `model` is a tag because it is genuinely
identifying and low-cardinality (one value). It is *not* used for `status_code` or
`severity`-style enumerations on the hot path, because every extra tag multiplies series count
and, in the Last Value Cache, multiplies memory.

## 4. Telegraf mapping

One `[[inputs.mqtt_consumer]]` block per target table. The measurement comes from
`name_override`, identity comes from `json_v2.tag` entries reading the **payload**, and
`ts` becomes the row timestamp.

```toml
[[inputs.mqtt_consumer]]
  servers = ["tcp://emqx:1883"]
  topics = ["solar/+/block/+/inverter/+/telemetry"]
  qos = 0
  # mqtt_consumer defaults this to "topic", which would add a tag column that
  # is not in the immutable schema.
  topic_tag = ""
  client_id = "telegraf-inverter"
  name_override = "inverter_telemetry"
  data_format = "json_v2"

  [[inputs.mqtt_consumer.json_v2]]
    timestamp_path = "ts"
    timestamp_format = "2006-01-02T15:04:05Z07:00"

    # Required: identity. A point without it is meaningless and is rejected.
    [[inputs.mqtt_consumer.json_v2.tag]]
      path = "site"
    [[inputs.mqtt_consumer.json_v2.tag]]
      path = "block"
    [[inputs.mqtt_consumer.json_v2.tag]]
      path = "inverter_id"
    [[inputs.mqtt_consumer.json_v2.tag]]
      path = "model"

    # Optional: json_v2 paths are required by default, and a missing path
    # discards the ENTIRE point rather than just that field.
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "ac_power_w"
      type = "float"
      optional = true
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "uptime_s"
      type = "int"
      optional = true
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "clipping"
      type = "bool"
      optional = true

[[outputs.influxdb_v2]]
  urls = ["${INFLUX_URL}"]
  token = "${INFLUX_WRITE_TOKEN}"
  bucket = "${INFLUX_DB}"
  timeout = "10s"
```

Five input blocks, one per table, each subscribing only to its own topics. QoS is 0 for
telemetry and 1 for `events`, where losing an alarm is not acceptable.

Five rules govern the mapping, each forced by observed Telegraf 1.36 behaviour rather than by
preference:

1. **The measurement name comes from `name_override`.** Telegraf names a measurement after the
   input plugin, so without this every point lands in one `mqtt_consumer` table.
2. **Tags come from the payload, not the topic.** `topic_parsing` requires `tags` and
   `measurement` to be comma-separated lists whose length exactly matches the topic, and its
   validation reported `measurement length does not equal topic length` for configurations that
   were provably correct — reproducibly, and inconsistently between otherwise identical files.
   See the payload contract in §2.0 for the consequences.
3. **`data_format = "json_v2"`, never `"json"`.** The `json` parser coerces every number to
   float64 and **silently discards booleans**, which would break the `clipping:bool` and
   `status_code:int64` columns with no error anywhere.
4. **Every field needs `optional = true`.** Tags are left required on purpose: a point without
   identity should be rejected loudly rather than written with a null tag.
5. **`topic_tag = ""` and `omit_hostname = true`** in `[agent]`. Both defaults add tag columns
   that are not in the schema, and tag definitions are immutable.

There is **no disk buffer**: `outputs.influxdb_v2` exposes no `buffer_limit` and Telegraf 1.36 ships
no `outputs.disk` plugin. `metric_buffer_limit` in `[agent]` is in-memory only, so metrics survive
a database outage but not a Telegraf restart. Combined with MQTT being transient, this is the one
genuinely lossy hop — see [Architecture §6](./02-architecture.md#6-failure-modes-and-behaviour).

## 5. End-to-end sequences

### 5.1 Normal telemetry tick

| # | Component | Action | Latency |
|---|---|---|---|
| 1 | Simulator | Compute physics for all 17 devices at time `t` | ~5 ms |
| 2 | Simulator | Publish 17 telemetry messages to EMQX | < 10 ms |
| 3 | EMQX | Route to subscribers (Telegraf, PWA) | < 5 ms |
| 4a | Telegraf | Parse, buffer, batch | immediate |
| 4b | Telegraf | Flush batch to InfluxDB | ≤ 10 s |
| 5 | InfluxDB | Write to WAL, later compact to Parquet | < 100 ms |
| 6a | PWA | Receive over WebSocket, update tile | **< 1 s** |
| 6b | PWA | User opens history → FastAPI → SQL | 100 ms – s |

**Live latency to the PWA is under a second** and is bounded only by the publish interval and
the WebSocket hop. The 10 s Telegraf flush is deliberately *not* on the live path.

### 5.2 Cold PWA load

1. Connect to `ws://localhost:8083/mqtt`
2. Subscribe to `solar/+/+/status` and `solar/+/rollup` — **retained**, so current state arrives
   immediately, no waiting for the next publish
3. Subscribe to telemetry topics for live updates
4. In parallel, call `GET /api/now` → FastAPI → `SELECT * FROM last_cache(...)` for the full
   device inventory
5. Call `GET /api/series?…` for the history charts

Step 2 gives a populated screen in well under a second. Steps 4–5 fill in behind it.

### 5.3 Inverter power loss

| # | Component | Action | Detection time |
|---|---|---|---|
| 1 | Simulator | Stop publishing for that inverter | — |
| 2 | EMQX | TCP session drops, keepalive expires | keepalive × 1.5 (~45 s at 30 s keepalive) |
| 3 | EMQX | Publishes LWT: `state: offline` on status topic | with step 2 |
| 4 | PWA | Retained status message received → tile turns red | **< 1 s after step 3** |
| 5 | FastAPI | Alert rule sees `offline` → Web Push | < 5 s |
| 6 | InfluxDB | Telegraf has no new rows → time-series gap | next query |

### 5.4 Comms loss (the hard case)

The session stays alive, so no LWT fires. Telemetry simply stops.

1. Simulator stops publishing, connection stays open
2. No status change is published — **the device still looks healthy**
3. The staleness rule in the alert engine fires: no telemetry for `3 × publish_interval`
4. Web Push: "INV-03 not reporting for 3 minutes"
5. Time-series gap visible in both Grafana and the PWA

**This is why both an offline check and a staleness check are required.** A system with only
the offline check silently misses network failures, which are more common than power failures.

### 5.5 InfluxDB outage

1. InfluxDB stops accepting writes
2. Telegraf write fails, buffers to disk up to `metric_buffer_limit`
3. Simulator and PWA are **completely unaffected** — live path never touches the database
4. InfluxDB recovers, Telegraf drains the buffer
5. Grafana history shows a gap if the buffer overflowed

This is the clearest demonstration of why the live and history paths are separated.

## 6. Latency budget

| Path | Target | Notes |
|---|---|---|
| Simulator compute | < 50 ms | 17 devices, once per tick |
| Simulator → EMQX | < 10 ms | localhost |
| EMQX → PWA (live) | **< 1 s** | The UX-critical path |
| EMQX → Telegraf | < 10 ms | |
| Telegraf → InfluxDB | ≤ 10 s | Flush interval, not user-facing |
| InfluxDB write ack | < 100 ms | WAL |
| FastAPI `/api/now` (LVC) | < 50 ms | In-memory cache |
| FastAPI `/api/series` | < 2 s | Parquet scan, range-dependent |
| Alert rule → Web Push | < 5 s | Plus rule debounce |
