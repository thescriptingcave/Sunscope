# SQL examples, one file each

**Generated from [docs/06-sql-examples.md](../06-sql-examples.md) — do not edit by hand.**

```bash
uv run python scripts/export-sql.py           # regenerate from the document
uv run python scripts/export-sql.py --check   # fail if out of date
uv run python scripts/export-sql.py --verify  # execute every file against InfluxDB
```

Every file is executed against the live database by `export-sql.py --verify`, and `bootstrap.sh test` fails if these files have drifted from the document they are generated from. Neither can rot unnoticed.

## Beginner — SELECT / WHERE / GROUP BY / ORDER BY

- **[`b1-recent-inverter-readings.sql`](b1-recent-inverter-readings.sql)** — Recent inverter readings · `—`
- **[`b2-average-power-per-inverter.sql`](b2-average-power-per-inverter.sql)** — Average power per inverter · `—`
- **[`b3-site-power-over-time-hourly.sql`](b3-site-power-over-time-hourly.sql)** — Site power over time, hourly · `time bucketing`
- **[`b4-which-devices-exist.sql`](b4-which-devices-exist.sql)** — Which devices exist · `—`
- **[`b5-hottest-inverters.sql`](b5-hottest-inverters.sql)** — Hottest inverters · `—`
- **[`b6-all-events-today.sql`](b6-all-events-today.sql)** — All events today · `time bucketing`

## Intermediate — CASE, joins, subqueries, aggregation

- **[`i1-classify-inverter-load-state.sql`](i1-classify-inverter-load-state.sql)** — Classify inverter load state · `—`
- **[`i2-hourly-totals-only-where-the-farm-was-actually-producing.sql`](i2-hourly-totals-only-where-the-farm-was-actually-producing.sql)** — Hourly totals, only where the farm was actually producing · `time bucketing`
- **[`i3-join-telemetry-to-weather.sql`](i3-join-telemetry-to-weather.sql)** — Join telemetry to weather · `—`
- **[`i4-below-average-inverters-subquery-in-where.sql`](i4-below-average-inverters-subquery-in-where.sql)** — Below-average inverters (subquery in `WHERE`) · `subquery`
- **[`i5-string-imbalance-per-inverter.sql`](i5-string-imbalance-per-inverter.sql)** — String imbalance per inverter · `CTE, subquery`
- **[`i6-weather-summary-by-day.sql`](i6-weather-summary-by-day.sql)** — Weather summary by day · `time bucketing`
- **[`i7-recent-errors-and-warnings.sql`](i7-recent-errors-and-warnings.sql)** — Recent errors and warnings · `—`
- **[`i8-device-inventory-with-per-device-summary.sql`](i8-device-inventory-with-per-device-summary.sql)** — Device inventory with per-device summary · `—`
- **[`i9-clipping-events-with-duration.sql`](i9-clipping-events-with-duration.sql)** — Clipping events with duration · `—`
- **[`i10-percentile-and-distribution-of-power.sql`](i10-percentile-and-distribution-of-power.sql)** — Percentile and distribution of power · `—`

## Advanced — CTEs, window functions and frames

- **[`a1-running-energy-per-inverter.sql`](a1-running-energy-per-inverter.sql)** — Running energy per inverter · `window function`
- **[`a2-hour-over-hour-change-cte-lag.sql`](a2-hour-over-hour-change-cte-lag.sql)** — Hour-over-hour change (CTE + `LAG`) · `CTE, window function, LAG, time bucketing, subquery`
- **[`a3-peak-power-per-inverter-per-hour-no-qualify.sql`](a3-peak-power-per-inverter-per-hour-no-qualify.sql)** — Peak power per inverter per hour (no `QUALIFY`) · `window function, ROW_NUMBER, time bucketing, subquery`
- **[`a4-moving-average.sql`](a4-moving-average.sql)** — Moving average · `window function, frame`
- **[`a5-time-based-moving-window.sql`](a5-time-based-moving-window.sql)** — Time-based moving window · `window function, frame`
- **[`a6-string-ranking-within-each-inverter.sql`](a6-string-ranking-within-each-inverter.sql)** — String ranking within each inverter · `window function, CUME_DIST, DENSE_RANK`
- **[`a7-percentile-buckets-across-the-fleet.sql`](a7-percentile-buckets-across-the-fleet.sql)** — Percentile buckets across the fleet · `window function, subquery`
- **[`a8-named-window.sql`](a8-named-window.sql)** — Named window · `frame`
- **[`a9-multi-cte-power-weather-and-specific-yield.sql`](a9-multi-cte-power-weather-and-specific-yield.sql)** — Multi-CTE: power, weather, and specific yield · `CTE, time bucketing, subquery`
- **[`a10-current-device-state-from-the-last-value-cache.sql`](a10-current-device-state-from-the-last-value-cache.sql)** — Current device state from the Last Value Cache · `—`

## Expert — self-joins, correlated subqueries, change-point detection

- **[`e1-performance-ratio-and-clipping-hourly.sql`](e1-performance-ratio-and-clipping-hourly.sql)** — Performance ratio and clipping, hourly · `CTE, time bucketing, subquery`
- **[`e2-anomaly-detection-against-a-prior-only-baseline.sql`](e2-anomaly-detection-against-a-prior-only-baseline.sql)** — Anomaly detection against a prior-only baseline · `CTE, window function, frame, time bucketing, subquery`
- **[`e3-self-join-to-compare-consecutive-intervals.sql`](e3-self-join-to-compare-consecutive-intervals.sql)** — Self-join to compare consecutive intervals · `—`
- **[`e4-correlated-subquery-worst-event-per-device.sql`](e4-correlated-subquery-worst-event-per-device.sql)** — Correlated subquery: worst event per device · `subquery`
- **[`e5-top-string-by-peak-power-per-inverter.sql`](e5-top-string-by-peak-power-per-inverter.sql)** — Top string by peak power, per inverter · `CTE, window function, RANK, ROW_NUMBER, subquery`
- **[`e6-carry-forward-the-last-known-value-gap-filling.sql`](e6-carry-forward-the-last-known-value-gap-filling.sql)** — Carry forward the last known value (gap filling) · `window function, LAST_VALUE, frame`
- **[`e7-current-state-and-cache-inventory.sql`](e7-current-state-and-cache-inventory.sql)** — Current state and cache inventory · `—`
- **[`e8-parameterised-query.sql`](e8-parameterised-query.sql)** — Parameterised query · `—`
- **[`e9-fleet-health-summary.sql`](e9-fleet-health-summary.sql)** — Fleet health summary · `CTE, time bucketing, subquery`
