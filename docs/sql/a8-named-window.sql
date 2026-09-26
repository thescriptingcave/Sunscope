-- ==========================================================================
-- A8. Named window
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: frame
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A8)
-- ==========================================================================
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
