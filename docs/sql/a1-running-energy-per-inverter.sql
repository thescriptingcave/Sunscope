-- ==========================================================================
-- A1. Running energy per inverter
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: window function
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A1)
-- ==========================================================================
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
