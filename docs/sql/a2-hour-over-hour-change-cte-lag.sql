-- ==========================================================================
-- A2. Hour-over-hour change (CTE + LAG)
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: CTE, window function, LAG, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A2)
-- ==========================================================================
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
