-- ==========================================================================
-- A7. Percentile buckets across the fleet
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: window function, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A7)
-- ==========================================================================
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
