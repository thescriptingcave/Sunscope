-- ==========================================================================
-- A4. Moving average
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: window function, frame
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A4)
-- ==========================================================================
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
