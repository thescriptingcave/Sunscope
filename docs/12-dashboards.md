# 12. The dashboards

Two interfaces over the same data, for two different jobs:

- **Grafana** — for analysis. Ad-hoc SQL, time-range control, window functions.
  Desktop, mouse, keyboard.
- **PWA** — for operations. "Is it broken right now, and by how much." Phone,
  glanceable, live.

Neither is redundant. Grafana cannot be used on a phone in a plant room, and the
PWA cannot answer "which inverter is in the worst percentile over 12 hours".

Terms used here are in the [glossary](./11-glossary.md).

---

# Part 1 — Grafana

Two provisioned dashboards, twelve panels, every query returning data. Both live
under `grafana/dashboards/` as code; Grafana re-reads them every 30 s. Dashboard
UIDs are stable, so bookmarks survive edits.

Queries go over **Flight SQL** (gRPC) to InfluxDB 3, which is why TLS and the
`database` header matter at all — see
[the Grafana notes](./grafana-influxdb-notes.md) if you touch the datasource.

## 1.1 Sunscope — Overview

Eight panels, top to bottom. Four stats, two time series, a bar gauge, a table.

### Site power — `stat`

```sql
SELECT "total_ac_power_w" FROM site_rollup
WHERE site = 'mojave' ORDER BY time DESC LIMIT 1
```

Current AC output, whole farm, watts. `ORDER BY time DESC LIMIT 1` reads the tail
of the window rather than aggregating history — this is a "what is it now"
number, and treating it as an average would be the wrong question.

**Read it as:** 0 at night is normal. A drop to 0 while the sun is up is the
`power_zero_in_sunlight` alert.

### Performance ratio — `stat`

```sql
SELECT "pr_ratio" FROM site_rollup WHERE site = 'mojave' ORDER BY time DESC LIMIT 1
```

Temperature-corrected, gated at 200 W/m². Thresholds are set so the colour
means something: red below 0.70, amber to 0.85, green above.

**Read it as:** 0.85–0.93 is healthy. **Above 0.95 is a bug**, not a triumph —
it means a loss coefficient is 1.0. Below 0.70 for a sustained period is
`performance_ratio_low`.

### Energy today — `stat`

```sql
SELECT "daily_yield_kwh" FROM site_rollup WHERE site = 'mojave' ORDER BY time DESC LIMIT 1
```

Accumulated kWh since local midnight. Monotonic within a day; resets at midnight.

**Read it as:** compare against yesterday, not against a number in your head.

### Inverters online — `stat`

```sql
SELECT "inverters_online", "strings_online" FROM site_rollup ORDER BY time DESC LIMIT 1
```

Two counts from one row. Green only at 4 / 4. Anything less is
`inverters_missing`.

### AC power — all inverters — `timeseries`

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
SELECT date_bin(INTERVAL '5 minutes', time) AS time, inverter_id,
       AVG("ac_power_w") AS "ac power"
FROM inverter_telemetry
WHERE site = 'mojave' AND $__timeFilter(time)
GROUP BY 1, inverter_id ORDER BY 1
```

One line per inverter, binned to 5 minutes. This is the main diagnostic chart:
flat, parallel lines mean a balanced fleet; a line that drops while the others
hold is an inverter; all of them dropping together is irradiance, weather, or a
grid problem.

**Read it as:** the *shape* is the signal. Binning to 5 min is deliberate — at
30-second resolution the 4-inverter comparison is unreadable.

### GHI and air temperature — `timeseries`

Two queries, one panel, dual axis: `AVG("ghi")` on the left in W/m²,
`AVG("air_temp_c")` on the right in °C.

The pairing is the point. It separates *the weather changed* from *the plant
degraded*: if power falls while GHI is flat, the plant is at fault; if both fall
together, it is the sun.

**Read it as:** always check this chart before believing a power drop.

### String balance per inverter — `bargauge`

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
SELECT inverter_id, ROUND(MAX(imbalance), 4) AS "imbalance" FROM (
  SELECT time, inverter_id,
         (MAX("dc_power_w") - MIN("dc_power_w")) / NULLIF(MAX("dc_power_w"), 0) AS imbalance
  FROM string_telemetry
  WHERE site = 'mojave' AND $__timeFilter(time)
  GROUP BY time, inverter_id
  HAVING COUNT(DISTINCT string_id) >= 3
) GROUP BY inverter_id ORDER BY "imbalance" DESC
```

Spread between the strongest and weakest string, worst case over the window.
Amber at 5 %, red at 20 %.

The inner query groups by `time` as well as `inverter_id` on purpose. Taking
`MAX`/`MIN` across the whole window without the time grouping would compare the
fleet's overall peaks across different times of day and produce a number that
means nothing.

**Read it as:** under 5 % is normal. Over 20 % is a degrading or failed string.

### Alert feed — `table`

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
SELECT time, severity, source, code, message FROM events
WHERE site = 'mojave' AND $__timeFilter(time)
ORDER BY time DESC LIMIT 50
```

Severity is colour-mapped. Resolutions appear as `<code>_RESOLVED` at `info`.

**Read it as:** `info` + `clipping_sustained` is not a fault — it is energy being
thrown away, which is a money problem rather than an availability one.

## 1.2 Sunscope — Analysis

Four panels, the window-function set. This is the dashboard for "why", and it is
where `docs/06-sql-examples.md` earns its place — each panel is one of the
documented examples.

### Power with a 4-tick moving average (`ROWS`)

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
SELECT time, "ac_power_w" AS raw,
       AVG("ac_power_w") OVER (ORDER BY time ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS smoothed
FROM inverter_telemetry
WHERE site = 'mojave' AND inverter_id IN ($inverter) AND $__timeFilter(time)
ORDER BY time
```

Raw in grey, smoothed in colour. `ROWS` counts **rows**, so the window is 4
samples wide regardless of how much time passed — which is what you want when
comparing across a window where the sample interval is constant.

### Period-over-period change (`LAG`)

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
WITH binned AS (
  SELECT date_bin(INTERVAL '15 minutes', time) AS time, inverter_id,
         AVG("ac_power_w") AS avg_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave' AND inverter_id IN ($inverter) AND $__timeFilter(time)
  GROUP BY 1, inverter_id
)
SELECT time, inverter_id, avg_power_w,
       avg_power_w - LAG(avg_power_w) OVER (PARTITION BY inverter_id ORDER BY time) AS change_w
FROM binned ORDER BY time
```

First difference of the 15-minute mean. The CTE bins first so the `LAG` compares
equal intervals — binning after the `LAG` would compare 15 minutes against 30
sometimes and 5 other times.

**Read it as:** the first place a sudden loss shows up, *before* it moves the
daily total.

### Fleet ranking by mean efficiency (`CUME_DIST`)

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
SELECT inverter_id, ROUND(AVG("efficiency"), 4) AS mean_eff,
       ROUND(CUME_DIST() OVER (ORDER BY AVG("efficiency")), 3) AS percentile
FROM inverter_telemetry
WHERE site = 'mojave' AND inverter_id IN ($inverter) AND $__timeFilter(time)
GROUP BY inverter_id ORDER BY mean_eff DESC
```

Not just who is best, but how far ahead. A fleet spread above ~0.01 in mean
efficiency is worth investigating; below that it is measurement noise.

### Events by severity and source (CTE)

```sql
-- not runnable: contains Grafana macros ($__timeFilter, $inverter) that
-- Grafana expands at render time. See docs/06-sql-examples.md for the
-- same queries as plain InfluxDB SQL.
WITH recent AS (
  SELECT time, severity, source, code FROM events
  WHERE site = 'mojave' AND $__timeFilter(time)
)
SELECT severity, source, COUNT(*) AS n, MAX(time) AS latest
FROM recent GROUP BY severity, source ORDER BY n DESC
```

Which asset generates the most events. Persistent `critical` on one source is a
repair ticket, not an alert to acknowledge.

## 1.3 Grafana gotchas that will cost you an hour

- **Use the time picker, not hardcoded windows.** Panels use
  `$__timeFilter(time)`, which expands to the picker's range. A hardcoded
  `now() - INTERVAL '6 hours'` can select a window containing no data, because
  the simulator's timestamps do not track wall-clock time. An empty panel then
  looks exactly like a broken query.
- **Multi-value variables need `${var:sqlstring}`.** A bare `$inverter` is not
  interpolated into a quoted SQL list, and the datasource plans `... IN inv`,
  failing with `No field named inv`.
- **`metric` must belong to the `table`.** `dc_power_w` is valid on
  `string_telemetry` and not on `site_rollup`. `GET /api/meta` lists them.
- **Health check is not a query.** `GET /api/datasources/uid/.../health` did not
  carry a database during the original breakage. Test a real query.

---

# Part 2 — The PWA

React, installable, served by the API at `/` so it is a single origin. Built for
an operator with a phone, not an analyst with a keyboard.

## 2.1 The live feed

A banner at the top reports the live connection in one word:

| State | Meaning |
|---|---|
| **Live** | Streaming from MQTT |
| **Reconnecting** | The socket dropped; retrying with backoff up to 15 s |
| **Offline** | No live connection. Cached state and history still render. |

**The browser does not talk to the broker.** It opens a same-origin WebSocket to
the API at `/api/live`, and the API relays frames from the MQTT subscription it
already holds for the alert engine. This is deliberate: a direct `ws://` connection
to EMQX is blocked as mixed content the moment the page is served over HTTPS, and
there is no client-side workaround.

The socket authenticates with a **single-use 30-second ticket** from
`POST /api/live-ticket`, not the JWT — a WebSocket handshake cannot carry an
`Authorization` header, and the usual workaround (a token in the query string)
leaks an hours-long credential into access logs and browser history.

## 2.2 The page, top to bottom

### Header

Site name and a logout button. The identity of what you are looking at, so a
screenshot is self-describing.

### Four KPI tiles

| Tile | Sub-label | Source |
|---|---|---|
| **Site power** | "AC, all inverters" | `site_rollup.total_ac_power_w` |
| **Performance ratio** | "temp-corrected" | `site_rollup.pr_ratio` |
| **Energy today** | — | `site_rollup.daily_yield_kwh` |
| **Inverters online** | — | `count / 4` |

These update from the **live feed**, not from polling, so they move as the
simulator publishes. A tile that renders `--` means the value was not a number —
never a raw string, by design.

### Alerts

The live, in-memory alert state, polled every **15 s** — faster than the rest,
because a fault that resolves should stop drawing attention quickly and a fresh
one should appear without waiting a minute.

States: `all clear`, `N active`, or `engine offline`. That last one is
distinguished deliberately: an engine that is disconnected is not the same as an
engine that is connected and has nothing to say.

Below the list, the engine's counters. **`errors` is the number to watch** — a
rising count means the engine is running but failing internally, which is a very
different problem from `engine_connected: false` and much easier to miss.

### Power chart — last 24 hours

One line per inverter, `interval: 1h`, `group_by: inverter_id`, polled every
**5 minutes**. The 1-hour bin is coarse on purpose: this is a 24-hour trend
overview, not a 30-second trace.

### Inverter cards

One card per inverter, showing:

- Status pill — Offline / Standby / Producing / Derating / fault
- AC power (large)
- Efficiency
- Heatsink temperature
- DC input
- Clipping yes/no

Each field prefers the **live reading** and falls back to the Last Value Cache.
The combination is what makes the page useful during an incident: power, thermal
state, conversion efficiency and clipping together on one card is enough to
distinguish "the inverter is fine but its strings are weak" from "the inverter is
throttling".

### String balance

A bar per inverter showing the spread between its strongest and weakest string.
Under 5 % is normal; over 20 % is a degrading string. The bar width is scaled
against 25 % so the colours land where the thresholds are.

### Events

The persisted history from `events`, newest first. Distinct from the Alerts
panel above: this one is read back from InfluxDB, that one is the engine's
current state.

### Rules

All twelve rules with their thresholds, polled every **5 minutes** — rarely,
because they do not change. The point is auditability: you can see what the system
is watching for without reading `alerts.yaml`, and you can confirm a threshold
change took effect without restarting anything.

## 2.3 How the PWA decides what is fresh

| Data | Mechanism | Interval |
|---|---|---|
| KPI tiles, inverter cards, string heatmap | Live WebSocket | As published (~30 s) |
| Alerts, engine stats | HTTP poll | 15 s |
| Power chart, rules | HTTP poll | 5 min |

Slow-moving things are polled; fast-moving things stream. Polling the power chart
every 30 s would be indistinguishable from streaming it and would multiply the
query load for nothing.

---

# Part 3 — Which one, and how to exercise it

## Choosing

| Question | Use |
|---|---|
| "Is it broken right now?" | PWA |
| "Show me on my phone" | PWA |
| "Which inverter is in the worst percentile over 12 h?" | Grafana Analysis |
| "How long was the farm clipped, and what did it cost?" | `/api/explore` or Grafana |
| "What is this metric called exactly?" | `/api/meta` |

## Neither UI shows a fault until you cause one

A clean farm fires no alerts. That is the design — if the baseline raised
alerts, the alerting would be untestable. To see the alerting work:

```bash
./scripts/bootstrap.sh sim:stop
uv run --project api --with paho-mqtt python scripts/inject-fault.py overheat
docker compose logs -f api | grep ALERT
```

Then watch the PWA's Alerts panel go from `all clear` to `N active`. The
`device_offline` and `telemetry_stale` rules will fire on their own, because
stopping the simulator is itself a fault.

Available faults: `overheat`, `hot-weather`, `dead-inverter`, `comms-loss`,
`recover`.

## When a panel is empty

In order of likelihood:

1. **Wrong time window.** The simulator's clock does not track wall-clock time.
   Widen the range before assuming a broken query.
2. **Metric/table mismatch.** Check `GET /api/meta`.
3. **Nothing has been published yet.** Start the simulator.
4. **Grafana only:** the datasource. `docker compose restart grafana`, then check
   `/api/datasources/uid/influxdb3-solar/health` *and* run a real query.

Both interfaces are covered by headless checks that fail on a panel-level error,
an empty panel, or a template variable that did not expand:

```bash
node scripts/browser/check-ui.js        # PWA, including the live feed
node scripts/browser/check-grafana.js   # both Grafana dashboards
```

Both run as part of `./scripts/bootstrap.sh test`. A green query API is not
sufficient — the datasource can return frames that Grafana still fails to plot —
so these assert on rendered DOM.
