-- ==========================================================================
-- A6. String ranking within each inverter
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: window function, CUME_DIST, DENSE_RANK
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A6)
-- ==========================================================================
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
