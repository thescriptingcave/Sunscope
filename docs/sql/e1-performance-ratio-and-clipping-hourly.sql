-- ==========================================================================
-- E1. Performance ratio and clipping, hourly
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: CTE, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E1)
-- ==========================================================================
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
