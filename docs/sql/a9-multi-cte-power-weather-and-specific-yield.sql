-- ==========================================================================
-- A9. Multi-CTE: power, weather, and specific yield
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: CTE, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A9)
-- ==========================================================================
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
