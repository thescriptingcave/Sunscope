# 11. Glossary

Terms used across this project, defined once. Where a term has a non-obvious
meaning here — because the platform means something different from the textbook
one — that difference is called out, because that is where the misunderstandings
come from.

Numbers and formulas are quoted from the code, not from convention. If they ever
disagree with the source, the source is right.

---

## 1. The plant

| Term | Meaning here |
|---|---|
| **Site** | The whole plant. The only site is `mojave`, and the identifier appears as the `site` tag on every row. |
| **Block** | A physical grouping of inverters sharing a collector. Two: `BLK-A` and `BLK-B`. A tag, not a table. |
| **Inverter** | Converts the strings' DC to grid AC. Four: `INV-01`…`INV-04`, 250 kW each. |
| **String** | A series chain of PV modules feeding one inverter input. Three per inverter, 12 in total. The highest-cardinality thing measured. |
| **1 MWac** | Nameplate **AC** output. DC nameplate is higher — the DC/AC ratio is a derived property (`topology.py:118`). PR and capacity factor are both referenced to the AC figure. |
| **POA** | Plane-of-array irradiance: the light actually landing on the tilted, reflecting module surface. Not a raw weather reading — see §2. |
| **Cell temperature** | The module's own temperature, hotter than air. Drives the temperature correction in PR (§3). |

Topology is fixed in code, not configuration: 4 inverters × 250 kW, 3 strings
each. There is no farm config file, so a change to the plant shape is a code
change.

## 2. Irradiance and weather

| Term | Meaning |
|---|---|
| **GHI** (Global Horizontal Irradiance) | Total sunlight on a horizontal surface. W/m². The broadest number. |
| **DNI** (Direct Normal Irradiance) | The beam component, arriving perpendicular to the sun. What a tracker would aim at. |
| **DHI** (Diffuse Horizontal Irradiance) | The scattered component. Present even under a clear sky. |
| **POA** (Plane of Array) | GHI rotated onto the module's tilt. The number the PV model actually uses. |
| **Clearness index** | Measured ÷ expected GHI for a clear sky. Below ~1 means cloud. |
| **Air temperature** | Ambient, from the weather station. |
| **Module temperature** | Measured at the module. Differs from cell temperature, which the model derives. |
| **Sun up / `sun_up`** | Whether the sun is above the horizon. Gates PR and several rules. |

All of these come from [pvlib](https://pvlib-python.readthedocs.io/) via a
linkedom-style clear-sky or stochastic sky model. Columns: `ghi`, `dni`, `dhi`,
`poa`, `air_temp_c`, `module_temp_c`, `clearness_index`, `relative_humidity`,
`wind_speed_mps` in `weather_station`.

## 3. Electrical and plant performance

| Term | Meaning here |
|---|---|
| **AC power** | Grid-side output, watts. The number everyone looks at. |
| **DC power** | String-side input. Always ≥ AC; the gap is conversion loss. |
| **Efficiency** | AC ÷ DC, 0–1. A field, not a derived-by-the-UI value. |
| **Clipping** | The inverter hit its AC rating and discarded excess DC. `true`/`false`, and the most valuable field in the dataset. |
| **Derating** | The inverter is deliberately limiting output, usually on heatsink temperature. A status, not a fault. |
| **Heatsink temperature** | The inverter's cooling surface. The thermal proxy the whole thermal-derating story hangs on. |
| **Internal temperature** | Inside the case, hotter than the heatsink. |
| **Uptime** | Seconds since the inverter last started. Distinguishes "been off an hour" from "been off a week". |
| **Status code** | `0` Offline, `1` Standby, `3` Producing, `4` Derating, `5`+ fault states. Rendered by `statusLabel()` in `web/src/format.ts`. |
| **PR** (performance ratio) | Temperature-corrected AC ÷ expected AC from POA. See below. |
| **Capacity factor** | Energy so far today ÷ (nameplate × **24 h**). Not the textbook definition — see below. |
| **Daily yield** | Accumulated energy today, kWh. |
| **Inverters online / strings online** | Counts, in `site_rollup`. |

### PR: two gates that are not optional

From `farm.py:359`:

- **Gated at 200 W/m² POA.** Below that the ratio is dominated by inverter
  overhead and the diffuse fraction, not system performance. Ungated, PR reads
  *above 1.0* at dawn and dusk, which makes the whole metric look broken. Real PR
  reporting always gates on irradiance.
- **Temperature-corrected** at ~0.35 %/°C of cell temperature. Without it, PR just
  tracks afternoon temperature and the number describes the weather rather than
  the equipment — which defeats its purpose.

**PR above ~0.95 is a bug signal, not good news.** It almost always means a loss
coefficient was left at 1.0. A healthy plant sits around 0.85–0.93.

### Capacity factor is deliberately non-standard

`farm.py:341` divides by nameplate × **24 h**, not by hours elapsed. The
comment there is emphatic, and it is right: a textbook capacity factor
(energy ÷ capacity ÷ hours elapsed) is near-meaningless at 08:00 and swings wildly
on a partly cloudy day, which makes it useless as a live tile. This one reads as
*progress toward the day's potential* and is stable early on.

There was also a units bug here once — energy in Wh against a kW nameplate
inflated the result 10×, reporting 132 % for a plant producing 13 %.

## 4. Telemetry and transport

| Term | Meaning |
|---|---|
| **Topic** | The MQTT address a reading is published to. Structured, not arbitrary: see below. |
| **LWT** (Last Will and Testament) | The message a broker publishes for a client that vanishes without disconnecting. How an inverter's `status` goes to Offline. |
| **Line protocol** | InfluxDB's text ingest format. `measurement,tag=val field=1i 123456789`. Timestamp last, in nanoseconds when `precision=ns`. |
| **Measurement** | In line protocol, the table name. Here the table name *is* the measurement: `inverter_telemetry` and nothing else. |
| **Tag** | An indexed string. Part of the primary key. In the SQL schema a tag's type is `Dictionary(Int32, Utf8)` — **that is how you tell a tag from a field.** |
| **Field** | The measured value. Typed. Not part of the key. |
| **LVC** (Last Value Cache) | An in-memory "latest value per series" structure. What makes `/api/now` instant instead of a scan over hours of Parquet. |
| **Parquet** | Columnar storage. What InfluxDB actually keeps on disk. Also a dead end as an *export* format here — see §7. |
| **Partition** | A chunk of Parquet files on a time/series boundary. The unit of storage. |
| **Flight SQL** | gRPC. How Grafana queries InfluxDB 3. Requires TLS, and is why Grafana needed fixing. |
| **EMQX** | The MQTT broker. |
| **Telegraf** | Subscribes to MQTT, converts line protocol, writes to InfluxDB. |

### Topic structure

```
solar/{site}/block/{block}/inverter/{inverter_id}/telemetry
solar/{site}/block/{block}/inverter/{inverter_id}/string/{string_id}/telemetry
solar/{site}/block/{block}/inverter/{inverter_id}/status
solar/{site}/weather/{station_id}/telemetry
solar/{site}/rollup
```

Site-scoped, and the path *is* the metadata. The weather topic is scoped the
same way, which is the reason the API can reject another site's traffic by shape
rather than by content.

## 5. Alerting

| Term | Meaning |
|---|---|
| **Rule** | A declarative threshold in `api/config/alerts.yaml`. Id, severity, metric, operator, threshold, debounce. |
| **Threshold rule** | Fires when a metric crosses a value. Most rules. |
| **Staleness rule** | Fires when data *stops arriving*. Has no metric. Needs its own timer, because nothing arrives to react to. |
| **Debounce** | The condition must hold continuously for `debounce_s` before firing. Prevents a single noisy sample causing an alert. |
| **PENDING / OK / FIRING** | The per-rule, per-subject state machine. PENDING is inside the debounce window. |
| **Re-arm** | After a resolution, the rule returns to OK and can fire again. Without it, one alert suppresses the rule forever. |
| **Resolution event** | A synthetic alert written when a firing rule clears. Suffixed `_RESOLVED`, at `info` severity, so a resolved fault stops drawing attention. |
| **Severity** | `critical`, `warning`, `info`. |
| **Live vs historical alerts** | `/api/alerts` is the engine's **in-memory current state**; `/api/events` is the **persisted history** in InfluxDB. They are not the same thing, and `/api/alerts` deliberately does not touch the database. |

### "Hysteresis" — a word this codebase over-uses

`docs/07-alerting.md` and an `alerts.py` docstring both call the mechanism
"debounce and hysteresis". There is **no hysteresis in the numeric sense**: no
`clear_threshold`, no separate clearing condition. A rule clears as soon as
`rule.matches()` stops being true.

The state machine does prevent flapping — a condition that clears inside the
debounce window never alerted at all — and that is worth having. But calling it
hysteresis overstates it. The mechanisms actually implemented are **debounce**
and **re-arm**.

### The 12 rules

| Rule | Severity | Fires when |
|---|---|---|
| `heatsink_high` | warning | Heatsink above derating onset |
| `heatsink_critical` | critical | Heatsink above derating end, output collapsing |
| `efficiency_low` | warning | Conversion efficiency collapsed while producing |
| `power_zero_in_sunlight` | critical | Zero output reported while irradiance is high |
| `inverter_derating` | warning | Inverter reports active derating |
| `inverter_fault` | critical | Inverter status code indicates a fault |
| `device_offline` | critical | No telemetry from a device that should be publishing |
| `clipping_sustained` | info | Clipping persists — energy being thrown away |
| `telemetry_stale` | critical | Per-device telemetry stopped |
| `site_rollup_stale` | critical | The site rollup stopped |
| `performance_ratio_low` | warning | PR below threshold |
| `inverters_missing` | critical | Fewer inverters online than the fleet size |

A clean farm fires **none** of the first eleven. That is the design: if the
baseline system raised alerts, the alerting would be untestable. Use
`scripts/inject-fault.py` to make something fire.

## 6. The user interfaces

| Term | Meaning |
|---|---|
| **PWA** | The React dashboard. Operator-facing, phone-first. See [§12](./12-dashboards.md). |
| **Grafana** | Provisioned dashboards over Flight SQL. Analyst-facing. See [§12](./12-dashboards.md). |
| **KPI tile** | A single large number with a label. Four of them at the top of the PWA. |
| **Series** | A named line of time-ordered points. One per inverter, per metric, per `group_by`. |
| **Bucket / bin** | Aggregating raw samples into a coarser interval (`5m`, `1h`). Reduces point count and hides 30-second noise. |
| **`group_by`** | Splitting one metric into one series per value of a dimension. |
| **Allowlist** | The closed set of legal `table`/`metric`/`dimension` values, enforced before any SQL is built. `GET /api/meta` publishes it so the UI cannot drift from it. |

## 7. Operations

| Term | Meaning |
|---|---|
| **Backfill** | Publishing historical data as if it had just happened, so a fresh database is not empty. 24 h by default. |
| **Backup** | Per-table CSV plus a manifest, via `scripts/backup.py`. Not Parquet — see below. |
| **Restore** | Recreates the schema from the manifest, then rebuilds line protocol. |
| **Compaction** | The rebuild-the-volume operation that actually reclaims disk. There is no other. |
| **Tombstone** | A dropped InfluxDB table: marked deleted, still occupying disk, and **not removable**. |
| **Retain** | *Not implemented*, and cannot be. See [§10](./10-retention.md). |

### Parquet cannot be restored

`influxdb3 query --format parquet` produces a valid file and Parquet is
InfluxDB's internal format, so it looks like the obvious backup format. It is
not: 3.11 exposes no Parquet write endpoint. `/api/v3/write_parquet` and
`/api/v3/write` both 404, and the only write path is `/api/v3/write_lp`. Hence
CSV out, line protocol in.

## 8. Traps

Things that look like bugs and are not — or are, and fail quietly.

| Looks like | Actually |
|---|---|
| An empty time range in a query | The simulator's timestamps do not track wall-clock time. A `now() - 6 hours` window can contain no data at all. |
| A dropped table coming back as `name-20260926T181521` | A dropped table is tombstoned. The next write recreates it under a suffixed name, and the original name then returns **nothing**. |
| Disk not shrinking after deleting data | Nothing reclaims it. Disk use is monotonic for the life of the volume. |
| PR of 1.05 | Either the 200 W/m² gate or the temperature correction is missing. |
| Capacity factor rising slowly at 06:00 | Correct. It is progress toward the day's potential, not a rate. |
| `403` on `/api/explore` over loopback | Docker rewrites the source address, so the API sees the bridge gateway (`172.22.0.1`), not `127.0.0.1`. |
| A 401 from the live tests | The token file was written under a timestamped name, or left mode 0600. See [§10](./10-retention.md) and the init script. |
| `ECONNRESET` from `fetch()` on 8181 or 1883 | The port does not speak HTTP. 8181 is TLS-only, 1883 is raw MQTT; the peer closes the socket on a payload it cannot parse. Nothing is down. |
