# 06 — SQL Examples

Runnable InfluxDB 3 Core SQL for the solar farm schema, in four difficulty tiers.

Every query here was checked against the InfluxDB 3 Core SQL reference. Where the dialect
differs from standard SQL, the difference is called out — several of these patterns exist
*because* a common SQL feature is unavailable.

> **Runnable copies.** Every query below is also exported to a standalone file in
> [`docs/sql/`](./sql/), each with a header giving the tier, the features it
> demonstrates and how to run it. Those files are generated from this document by
> `uv run python scripts/export-sql.py`, so the two cannot drift, and `--verify`
> executes each one against the live database.

## Schema reference

Database: `solar`

| Table | Tags | Key fields |
|---|---|---|
| `inverter_telemetry` | `site`, `block`, `inverter_id`, `model` | `ac_power_w`, `dc_power_w`, `efficiency`, `heatsink_temp_c`, `status_code`, `clipping` |
| `string_telemetry` | `site`, `block`, `inverter_id`, `string_id` | `dc_power_w`, `dc_voltage_v`, `module_temp_c` |
| `weather_station` | `site`, `station_id` | `ghi`, `dni`, `dhi`, `air_temp_c`, `wind_speed_mps`, `clearness_index` |
| `site_rollup` | `site` | `total_ac_power_w`, `daily_yield_kwh`, `pr_ratio`, `capacity_factor`, `inverters_online` |
| `events` | `site`, `severity`, `source` | `code`, `message`, `value`, `threshold` |

## Platform constraints

Read these before writing anything beyond the beginner tier. The engine is **Apache Arrow
DataFusion**, not a row-store SQL engine, and the differences are not academic.

| Feature | Status | Note |
|---|---|---|
| `WITH` (CTEs, multiple) | ✅ | Must be the first clause |
| `WITH RECURSIVE` | ❌ | Not supported |
| `ROW_NUMBER` / `RANK` / `DENSE_RANK` | ✅ | |
| `LAG` / `LEAD` | ✅ | |
| `FIRST_VALUE` / `LAST_VALUE` / `NTH_VALUE` | ✅ | |
| `NTILE` / `CUME_DIST` / `PERCENT_RANK` | ✅ | |
| Aggregates over `OVER` | ✅ | All of them |
| Named `WINDOW` clause | ✅ | |
| `ROWS` / `RANGE` / `GROUPS` frames | ✅ | `RANGE` needs single-column `ORDER BY` |
| `INNER` / `LEFT` / `RIGHT` / `FULL` JOIN | ✅ | |
| `CROSS JOIN` | ❌ | Not supported |
| Subqueries in `FROM` / `WHERE` / `SELECT` / `HAVING` | ✅ | `SELECT` = scalar only |
| `EXISTS` | ✅ | **Correlated only** |
| **`QUALIFY`** | ❌ | **Not supported** — use a derived table |
| `OFFSET` / `TOP` | ❌ | `LIMIT` only |
| `last_cache()` / `distinct_cache()` | ✅ | `FROM`-clause table functions, string literals |
| `information_schema` | ✅ | `.tables`, `.columns`, `.views`, `.schemata`, `.df_settings` |
| `$name` parameters | ✅ | `WHERE` only; text substitution, not prepared statements |
| `_influxdb3_catalog` | ❌ | Undocumented — use `information_schema` |

### Conventions used throughout

- **Double-quoted identifiers** always. Unquoted identifiers are case-insensitive and will match
  any column with the same characters regardless of case.
- **Half-open time ranges**: `time >= X AND time < Y`. The idiomatic upper bound throughout the
  InfluxDB docs — exclusive bounds avoid the double-counting you get from `<=`.
- **`now() - INTERVAL '1 hour'`** rather than a hard-coded timestamp, so examples stay runnable.
- **`INTERVAL '1 hour'`** string-literal form. Note parameters **cannot** be used in `INTERVAL`.
- `type` is a reserved word; the schema uses `status_code` and `severity` instead.

---

# Beginner

Selecting, filtering, aggregating. No CTEs, no window functions.

### B1 — Recent inverter readings

```sql
SELECT time, inverter_id, ac_power_w, efficiency
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 50;
```

### B2 — Average power per inverter

```sql
SELECT inverter_id,
       AVG(ac_power_w) AS avg_ac_power_w,
       MAX(ac_power_w) AS peak_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
GROUP BY inverter_id
ORDER BY avg_ac_power_w DESC;
```

`GROUP BY` here is unremarkable because every non-aggregate column in `SELECT` appears in
`GROUP BY` — which the dialect requires.

### B3 — Site power over time, hourly

```sql
SELECT date_bin(INTERVAL '1 hour', time) AS time,
       SUM(ac_power_w) AS total_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 1;
```

`date_bin` truncates timestamps to fixed buckets — far faster than `date_trunc` for charting.
`GROUP BY 1` groups by **ordinal position**, which is required when grouping by an expression:
the dialect cannot group by a `SELECT` alias whose name matches the underlying column, so
grouping by `time` directly would silently group by the raw column instead of the binned one.
**When you see wrong bucketed results, this is the cause.**

### B4 — Which devices exist

```sql
SELECT DISTINCT inverter_id
FROM inverter_telemetry
WHERE site = 'mojave'
ORDER BY inverter_id;
```

### B5 — Hottest inverters

```sql
SELECT inverter_id,
       MAX(heatsink_temp_c) AS max_temp_c,
       AVG(heatsink_temp_c) AS avg_temp_c
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY max_temp_c DESC;
```

### B6 — All events today

```sql
SELECT time, severity, source, code, message
FROM events
WHERE site = 'mojave'
  AND time >= date_trunc('day', now())
ORDER BY time DESC;
```

---

# Intermediate

`CASE`, `HAVING`, joins, subqueries, null handling, string and time functions.

### I1 — Classify inverter load state

```sql
SELECT time, inverter_id, ac_power_w,
       CASE
         WHEN ac_power_w > 200000 THEN 'high'
         WHEN ac_power_w > 100000 THEN 'nominal'
         WHEN ac_power_w > 0      THEN 'low'
         ELSE 'offline'
       END AS load_state
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '3 hours'
ORDER BY time;
```

### I2 — Hourly totals, only where the farm was actually producing

```sql
SELECT date_bin(INTERVAL '1 hour', time) AS time,
       SUM(ac_power_w) AS total_ac_power_w,
       AVG(efficiency) AS avg_efficiency
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY 1
HAVING SUM(ac_power_w) > 0
ORDER BY 1;
```

### I3 — Join telemetry to weather

```sql
SELECT i.time,
       i.inverter_id,
       i.ac_power_w,
       w.ghi,
       w.air_temp_c,
       w.clearness_index
FROM inverter_telemetry AS i
INNER JOIN weather_station AS w
  ON i.time = w.time
 AND i.site  = w.site
WHERE i.site = 'mojave'
  AND i.time >= now() - INTERVAL '2 hours'
ORDER BY i.time;
```

`time` exists in both tables, so it must be qualified **everywhere** — `SELECT`, `ON`, and
`ORDER BY`. An unqualified `time` is ambiguous and the query fails.

This join only works because the simulator publishes every device on the **same tick**. With
independent intervals you would need time bucketing on both sides first, or a range join:

```sql
SELECT i.time, i.inverter_id, i.ac_power_w, w.ghi
FROM inverter_telemetry AS i
INNER JOIN weather_station AS w
  ON i.site = w.site
 AND w.time <= i.time
 AND w.time > i.time - INTERVAL '5 minutes'
ORDER BY i.time;
```

### I4 — Below-average inverters (subquery in `WHERE`)

```sql
SELECT time, inverter_id, ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
  AND ac_power_w < (
        SELECT AVG(ac_power_w)
        FROM inverter_telemetry
        WHERE site = 'mojave'
          AND time >= now() - INTERVAL '6 hours'
      )
ORDER BY time;
```

### I5 — String imbalance per inverter

The naive version of this query is wrong, and wrong in the worst possible direction. It is
worth showing both, because the failure is silent and the number looks plausible.

```sql
-- WRONG. Do not ship this.
SELECT inverter_id,
       COUNT(DISTINCT string_id) AS string_count,
       MIN(dc_power_w) AS min_dc_power_w,
       MAX(dc_power_w) AS max_dc_power_w,
       ROUND((MAX(dc_power_w) - MIN(dc_power_w)) / NULLIF(MAX(dc_power_w), 0), 4)
         AS imbalance_ratio
FROM string_telemetry
WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY imbalance_ratio DESC;
```

`MIN` and `MAX` here collapse the **whole window** into two numbers, so they are not measuring
the spread between strings at all. They are measuring the day/night cycle: the weakest reading
is at dawn, the strongest is at noon. On a perfectly healthy farm this returned **23–26 %**
imbalance, which the dashboard rendered as every inverter being faulty. Widen the window and
the "imbalance" grows, which is the tell that you are measuring time and not hardware.

Strings must be compared **at the same instant**. Collapse per timestamp first, then take the
worst case across the window:

```sql
WITH per_sample AS (
    SELECT time,
           inverter_id,
           COUNT(DISTINCT string_id) AS strings_present,
           MAX(dc_power_w)           AS strongest_w,
           MIN(dc_power_w)           AS weakest_w
    FROM string_telemetry
    WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
    GROUP BY time, inverter_id          -- the fix: time is part of the grain
),
scored AS (
    SELECT inverter_id, time, strongest_w, weakest_w,
           (strongest_w - weakest_w) / NULLIF(strongest_w, 0) AS imbalance_ratio
    FROM per_sample
    WHERE strings_present >= 3 AND strongest_w > 0
)
SELECT inverter_id,
       3                        AS string_count,
       ROUND(MIN(weakest_w), 1)  AS min_dc_power_w,
       ROUND(MAX(strongest_w), 1) AS max_dc_power_w,
       ROUND(MAX(imbalance_ratio), 4) AS imbalance_ratio
FROM scored
GROUP BY inverter_id
ORDER BY imbalance_ratio DESC;
```

Three things to note, each of which is a separate trap:

- **`GROUP BY time, inverter_id` is the whole point.** Without `time` in the grain you are back
  to pooling the day into one number.
- **`strings_present >= 3` drops partial samples.** A sample where one string failed to arrive
  looks identical to a sample where that string is dead, so scoring it manufactures an
  imbalance out of a dropped message. This is also what silently immunised the query against
  two orphan rows left in the table by an earlier run of the simulator.
- **`MAX(imbalance_ratio)`, not `AVG`.** This is an alarm. A degrading string is bad for part
  of the day, and averaging it away is precisely how a real fault gets missed.

On the reference dataset this returns **1.3–5.5 %**, consistent with the documented healthy
baseline of under 5 %. `NULLIF` still guards the divide when an inverter is fully offline and
every string reads zero — in DataFusion that would otherwise surface as a query error rather
than a NULL row.

### I6 — Weather summary by day

```sql
SELECT date_bin(INTERVAL '1 day', time) AS time,
       AVG(ghi)             AS avg_ghi,
       MAX(air_temp_c)      AS max_air_temp_c,
       AVG(clearness_index) AS avg_clearness_index,
       SUM(wind_speed_mps)  AS wind_sample_sum
FROM weather_station
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1;
```

### I7 — Recent errors and warnings

```sql
SELECT time, severity, source, code, message, value, threshold
FROM events
WHERE site = 'mojave'
  AND severity IN ('warning', 'critical')
  AND time >= now() - INTERVAL '24 hours'
ORDER BY time DESC;
```

### I8 — Device inventory with per-device summary

```sql
SELECT inverter_id,
       COUNT(*)            AS sample_count,
       MAX(time)           AS last_report,
       AVG(ac_power_w)     AS avg_ac_power_w,
       MAX(ac_power_w)     AS peak_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY last_report DESC;
```

`last_report` is a cheap staleness signal straight from the data — a device whose latest row is
old is a device that stopped reporting, which is the comms-loss case from
[Data Flow §5.4](./03-data-flow.md#54-comms-loss-the-hard-case).

### I9 — Clipping events with duration

```sql
SELECT inverter_id,
       COUNT(*) AS clipped_samples,
       COUNT(*) * 60 AS approx_clipped_seconds
FROM inverter_telemetry
WHERE site = 'mojave'
  AND clipping = true
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY clipped_samples DESC;
```

### I10 — Percentile and distribution of power

```sql
SELECT inverter_id,
       MEDIAN(ac_power_w)  AS median_power_w,
       STDDEV(ac_power_w)  AS sd_power_w,
       MIN(ac_power_w)     AS min_power_w,
       MAX(ac_power_w)     AS max_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
  AND ac_power_w > 0
GROUP BY inverter_id
ORDER BY median_power_w DESC;
```

---

# Advanced

CTEs and window functions.

### A1 — Running energy per inverter

```sql
SELECT time,
       inverter_id,
       ac_power_w,
       SUM(ac_power_w) OVER (
         PARTITION BY inverter_id
         ORDER BY time
       ) AS cumulative_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
ORDER BY inverter_id, time;
```

`ORDER BY time` inside `OVER` gives a running total. This is the SQL replacement for a
cumulative aggregate — and note the outer query **still needs** its own `ORDER BY`; the window's
`ORDER BY` controls the frame, not the output order.

### A2 — Hour-over-hour change (CTE + `LAG`)

```sql
WITH binned AS (
  SELECT date_bin(INTERVAL '15 minutes', time) AS time,
         inverter_id,
         AVG(ac_power_w) AS avg_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1, inverter_id
)
SELECT time,
       inverter_id,
       avg_power_w,
       LAG(avg_power_w, 1) OVER (
         PARTITION BY inverter_id ORDER BY time
       ) AS prev_avg_power_w,
       avg_power_w - LAG(avg_power_w, 1) OVER (
         PARTITION BY inverter_id ORDER BY time
       ) AS delta_w
FROM binned
ORDER BY inverter_id, time;
```

Bin **first**, then apply `LAG`. Applying `LAG` to raw 60-second data and then binning gives a
different and much noisier answer.

### A3 — Peak power per inverter per hour (no `QUALIFY`)

```sql
SELECT time, inverter_id, peak_power_w
FROM (
  SELECT time,
         inverter_id,
         ac_power_w AS peak_power_w,
         ROW_NUMBER() OVER (
           PARTITION BY date_bin(INTERVAL '1 hour', time)
           ORDER BY ac_power_w DESC
         ) AS rn
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '12 hours'
)
WHERE rn = 1
ORDER BY time;
```

**`QUALIFY` does not exist in this dialect.** This derived-table pattern is the only way to
express "top N per group". Note that `date_bin(INTERVAL '1 hour', time)` appears inside
`PARTITION BY`, where window expressions are allowed even though `date_bin` is not an aggregate
in the `SELECT` list.

Use `RANK` instead of `ROW_NUMBER` if you want ties to all be returned.

### A4 — Moving average

```sql
SELECT time,
       ac_power_w,
       AVG(ac_power_w) OVER (
         ORDER BY time
         ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
       ) AS ma_4
FROM inverter_telemetry
WHERE site = 'mojave'
  AND inverter_id = 'INV-01'
  AND time >= now() - INTERVAL '6 hours'
ORDER BY time;
```

`ROWS BETWEEN 3 PRECEDING AND CURRENT ROW` averages **4** rows. Off-by-one here is the most
common window-function bug, and the result looks plausible either way — see
[Testing §5](./05-testing.md#5-sql-regression-tests) for the test that catches it.

### A5 — Time-based moving window

```sql
SELECT time,
       ac_power_w,
       AVG(ac_power_w) OVER (
         ORDER BY time
         RANGE BETWEEN INTERVAL '30 minutes' PRECEDING AND CURRENT ROW
       ) AS ma_30min
FROM inverter_telemetry
WHERE site = 'mojave'
  AND inverter_id = 'INV-01'
  AND time >= now() - INTERVAL '6 hours'
ORDER BY time;
```

`RANGE` with an `INTERVAL` offset gives a true **time-based** window, which is what you actually
want for irregularly-spaced telemetry. `ROWS` counts rows regardless of elapsed time. Two
constraints: `RANGE` requires `ORDER BY` with **exactly one** column, and the sort column must
be a timestamp.

### A6 — String ranking within each inverter

```sql
SELECT time,
       inverter_id,
       string_id,
       dc_power_w,
       DENSE_RANK() OVER (
         PARTITION BY inverter_id, time
         ORDER BY dc_power_w DESC
       ) AS rank_in_inverter,
       CUME_DIST() OVER (
         PARTITION BY inverter_id, time
         ORDER BY dc_power_w
       ) AS pctile_of_strings
FROM string_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '1 hour'
ORDER BY inverter_id, time, rank_in_inverter;
```

`CUME_DIST` gives the fraction of strings at or below the current value — a direct way to
quantify "this string is in the bottom decile", which is what the string heatmap shows.

### A7 — Percentile buckets across the fleet

```sql
SELECT time, inverter_id, ac_power_w, power_quartile
FROM (
  SELECT time, inverter_id, ac_power_w,
         NTILE(4) OVER (
           PARTITION BY time ORDER BY ac_power_w DESC
         ) AS power_quartile
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '6 hours'
)
WHERE power_quartile = 1
ORDER BY time;
```

### A8 — Named window

```sql
SELECT time,
       inverter_id,
       ac_power_w,
       AVG(ac_power_w) OVER fleet_window AS rolling_avg,
       COUNT(*)        OVER fleet_window AS sample_count
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '3 hours'
WINDOW fleet_window AS (
  PARTITION BY inverter_id
  ORDER BY time
  ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
)
ORDER BY inverter_id, time;
```

The named `WINDOW` clause is the readable way to reuse one frame across several functions —
without it you would repeat the identical `OVER` clause twice and risk the copies drifting apart.

### A9 — Multi-CTE: power, weather, and specific yield

```sql
WITH hourly_power AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         SUM(ac_power_w)  AS ac_power_w,
         AVG(efficiency)  AS avg_efficiency
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
),
hourly_weather AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         AVG(ghi)             AS ghi,
         AVG(air_temp_c)      AS air_temp_c,
         AVG(clearness_index) AS clearness_index
  FROM weather_station
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
)
SELECT p.time,
       p.ac_power_w,
       w.ghi,
       w.air_temp_c,
       w.clearness_index,
       ROUND(p.ac_power_w / NULLIF(w.ghi, 0), 2) AS watts_per_wm2
FROM hourly_power AS p
INNER JOIN hourly_weather AS w ON p.time = w.time
ORDER BY p.time;
```

Each CTE bins independently, **then** they join on the binned timestamp — safe even if the
underlying samples do not align. `watts_per_wm2` is specific yield, the standard
irradiance-normalised performance measure.

### A10 — Current device state from the Last Value Cache

```sql
SELECT *
FROM last_cache('inverter_telemetry', 'inverter_current');
```

`last_cache()` is a **`FROM`-clause table function** taking two string literals — there is no
scalar form. This is the query behind `GET /api/now`, giving the PWA current state in
milliseconds instead of aggregating hours of Parquet.

---

# Expert

Multi-CTE analytics, `FULL JOIN`, correlated subqueries, anomaly detection, gap filling,
schema introspection, parameterised queries.

### E1 — Performance ratio and clipping, hourly

```sql
WITH dc AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         SUM(dc_power_w) AS dc_power_w
  FROM string_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
),
ac AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         SUM(ac_power_w) AS ac_power_w,
         MAX(ac_power_w) AS peak_ac_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
),
wx AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         AVG(ghi)             AS ghi,
         AVG(air_temp_c)      AS air_temp_c
  FROM weather_station
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
)
SELECT COALESCE(a.time, d.time) AS time,
       d.dc_power_w,
       a.ac_power_w,
       a.peak_ac_power_w,
       w.ghi,
       w.air_temp_c,
       ROUND(a.ac_power_w / NULLIF(d.dc_power_w, 0), 4) AS inverter_efficiency,
       ROUND(a.ac_power_w / NULLIF(w.ghi, 0), 2)      AS watts_per_wm2,
       CASE WHEN a.peak_ac_power_w >= 249000 THEN true ELSE false END AS clipped
FROM ac AS a
FULL JOIN dc AS d ON a.time = d.time
FULL JOIN wx AS w ON COALESCE(a.time, d.time) = w.time
ORDER BY time;
```

Three independent aggregations joined on binned time. `FULL JOIN` keeps rows from either side
even where the other is missing — a missing weather row shows as NULL `ghi` rather than
silently dropping an hour of production data. `COALESCE` builds a join key that works when
either `a.time` or `d.time` is NULL.

The `inverter_efficiency` column is the cleanest single indicator of inverter health: it should
sit at 0.96–0.98 and any sustained departure points at a specific fault.

### E2 — Anomaly detection against a prior-only baseline

```sql
WITH hourly AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         inverter_id,
         AVG(ac_power_w) AS avg_power_w,
         STDDEV(ac_power_w) AS sd_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '7 days'
  GROUP BY 1, inverter_id
),
baseline AS (
  SELECT time,
         inverter_id,
         avg_power_w,
         sd_power_w,
         AVG(avg_power_w) OVER (
           PARTITION BY inverter_id
           ORDER BY time
           ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING
         ) AS prior_mean
  FROM hourly
)
SELECT time,
       inverter_id,
       ROUND(avg_power_w, 1) AS avg_power_w,
       ROUND(prior_mean, 1)  AS prior_mean,
       ROUND(avg_power_w - prior_mean, 1) AS delta_w
FROM baseline
WHERE prior_mean IS NOT NULL
  AND avg_power_w < prior_mean * 0.7
ORDER BY time;
```

**`ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING`** is the critical detail. A frame ending at
`CURRENT ROW` includes the row being tested, which pulls the mean toward the outlier and shrinks
the measured deviation — a self-defeating baseline. Excluding the current row makes this a
genuine change-point test. `prior_mean IS NOT NULL` filters the first six rows, where the frame
is incomplete.

The `0.7` threshold is the tunable: it should be validated against the sensor-drift and
string-underperformance scenarios so real faults fire and cloud transients do not.

### E3 — Self-join to compare consecutive intervals

```sql
SELECT current.time                              AS time,
       current.site                              AS site,
       current.ac_power_w                        AS current_power_w,
       previous.ac_power_w                       AS previous_power_w,
       current.ac_power_w - previous.ac_power_w  AS change_w
FROM inverter_telemetry AS current
LEFT JOIN inverter_telemetry AS previous
  ON current.inverter_id = previous.inverter_id
 AND previous.time = current.time - INTERVAL '1 hour'
WHERE current.site = 'mojave'
  AND current.time >= now() - INTERVAL '6 hours'
ORDER BY current.time;
```

The manual alternative to `LAG`, and instructive because it shows what `LAG` does internally.
`LEFT JOIN` is required, not `INNER` — the earliest row in each series has no predecessor, and
an inner join would silently drop it.

### E4 — Correlated subquery: worst event per device

```sql
SELECT e.time,
       e.source,
       e.severity,
       e.code,
       e.message
FROM events AS e
WHERE e.site = 'mojave'
  AND e.time >= now() - INTERVAL '7 days'
  AND e.value = (
    SELECT MAX(inner_e.value)
    FROM events AS inner_e
    WHERE inner_e.source = e.source
      AND inner_e.time >= now() - INTERVAL '7 days'
  )
ORDER BY e.severity, e.time DESC;
```

A correlated subquery: `inner_e.source = e.source` references the outer row, so the maximum is
computed **per device**. Correlated subqueries are also the only form `EXISTS` supports here.

### E5 — Top string by peak power, per inverter

```sql
SELECT time          AS peak_time,
       inverter_id,
       string_id,
       dc_power_w    AS peak_power_w
FROM (
  SELECT time,
         inverter_id,
         string_id,
         dc_power_w,
         ROW_NUMBER() OVER (
           PARTITION BY inverter_id
           ORDER BY dc_power_w DESC
         ) AS rn
  FROM string_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY inverter_id;
```

One level of nesting: the window is evaluated in the inner query, and the outer `WHERE` filters
on its result. This is the pattern that replaces `QUALIFY`.

With `RANK` instead of `ROW_NUMBER`, tied strings are all returned — which is usually what you
want here, since two strings peaking at the same instant is a meaningful observation rather than
an arbitrary tiebreak:

```sql
WITH ranked AS (
  SELECT time, inverter_id, string_id, dc_power_w,
         RANK() OVER (PARTITION BY inverter_id ORDER BY dc_power_w DESC) AS rnk
  FROM string_telemetry
  WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
)
SELECT time, inverter_id, string_id, dc_power_w
FROM ranked WHERE rnk = 1 ORDER BY inverter_id;
```

### E6 — Carry forward the last known value (gap filling)

```sql
SELECT time,
       inverter_id,
       ac_power_w,
       last_value(ac_power_w) OVER (
         PARTITION BY inverter_id
         ORDER BY time
         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS ac_power_w_filled
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
ORDER BY inverter_id, time;
```

For charting, a gap is often better shown as a flat carry-forward than as a break in the line.
Note the two columns are kept separate so the UI can distinguish real from held values —
a filled gap drawn identically to a real sample is misleading.

There are also built-in helpers: `locf()` (last observation carried forward) and
`interpolate()` (linear interpolation between points). Prefer them when a plain forward-fill
is what you want.

### E7 — Current state and cache inventory

```sql
-- All tables and their columns
SELECT table_name, column_name, data_type, is_nullable, ordinal_position
FROM information_schema.columns
WHERE table_schema = 'iox'
ORDER BY table_name, ordinal_position;

-- Just the tables
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'iox'
ORDER BY table_name;

-- Live server settings
SELECT * FROM information_schema.df_settings;

-- Active and recent queries
SELECT query_text, issue_time, execute_duration, success
FROM system.queries
ORDER BY issue_time DESC
LIMIT 20;
```

`table_schema = 'iox'` is user tables; `'system'` and `'information_schema'` are the other two.
This is the supported replacement for `_influxdb3_catalog`, which is undocumented and should not
be relied on.

### E8 — Parameterised query

```sql
SELECT time, inverter_id, ac_power_w
FROM inverter_telemetry
WHERE site = $site
  AND time >= $start_time
  AND time <  $end_time
  AND ac_power_w >= $min_power
ORDER BY time;
```

```json
{
  "db": "solar",
  "q": "SELECT time, inverter_id, ac_power_w FROM inverter_telemetry WHERE site = $site AND time >= $start_time AND time < $end_time AND ac_power_w >= $min_power ORDER BY time",
  "params": {
    "site": "mojave",
    "start_time": "2026-09-25T00:00:00Z",
    "end_time": "2026-09-26T00:00:00Z",
    "min_power": 100000.0
  }
}
```

Sent to `POST /api/v3/query_sql`. Four restrictions that matter for the API design:

1. **`WHERE` predicates only** — not `SELECT`, `GROUP BY`, function arguments, or identifiers
2. **`$name` only** — no `?`, `?1`, `:name`, or `@name`
3. **Text substitution before planning** — not a true prepared statement, so it gets no caching
   or planning benefit
4. **Timestamps are passed as strings**

Because parameters cannot go in `INTERVAL` literals, the API takes time ranges as timestamp
parameters and computes any bucket size server-side from an allowlist. See
[Security §4.5](./04-security.md#45-sql-injection-prevention-addresses-t3).

### E9 — Fleet health summary

```sql
WITH latest AS (
  SELECT *
  FROM last_cache('inverter_telemetry', 'inverter_current')
),
today AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         SUM(ac_power_w) AS ac_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
)
SELECT (SELECT MAX(time) FROM today)                    AS data_through,
       (SELECT COUNT(*) FROM latest)                    AS device_count,
       (SELECT COUNT(*) FROM latest WHERE clipping)      AS clipping_now,
       (SELECT ROUND(MIN(ac_power_w), 1) FROM today)    AS min_hourly_w,
       (SELECT ROUND(MAX(ac_power_w), 1) FROM today)    AS peak_hourly_w;
```

Scalar subqueries in the `SELECT` list, each returning exactly one row and one column —
the correct use of a subquery in `SELECT`. Pairing the Last Value Cache for present state with
a time-bucketed aggregate for the trailing day gives both in a single round trip.

---

## Quick reference

| Task | Pattern |
|---|---|
| Bucket time | `date_bin(INTERVAL '1 hour', time)` + `GROUP BY 1` |
| Group by expression | **Ordinals** (`GROUP BY 1`), never a colliding alias |
| Running total | `SUM(x) OVER (PARTITION BY k ORDER BY time)` |
| Row-to-row change | `LAG(x, 1) OVER (PARTITION BY k ORDER BY time)` |
| Row-count moving avg | `ROWS BETWEEN n PRECEDING AND CURRENT ROW` (n+1 rows) |
| Time-based moving avg | `RANGE BETWEEN INTERVAL '30 minutes' PRECEDING AND CURRENT ROW` |
| Top N per group | Derived table + `ROW_NUMBER()` + `WHERE rn = 1` (**no `QUALIFY`**) |
| Reuse a frame | Named `WINDOW w AS (...)` |
| Avoid divide-by-zero | `x / NULLIF(y, 0)` |
| Instant current state | `SELECT * FROM last_cache('table', 'cache')` |
| Discover schema | `information_schema.columns` / `.tables` |
| Safe user input | `$name` in `WHERE` only + identifier allowlists |
