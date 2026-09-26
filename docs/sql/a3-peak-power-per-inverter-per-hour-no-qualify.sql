-- ==========================================================================
-- A3. Peak power per inverter per hour (no QUALIFY)
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: window function, ROW_NUMBER, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A3)
-- ==========================================================================
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
