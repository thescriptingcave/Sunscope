-- ==========================================================================
-- E6. Carry forward the last known value (gap filling)
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: window function, LAST_VALUE, frame
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E6)
-- ==========================================================================
SELECT time,
       inverter_id,
       ac_power_w,
       last_value(ac_power_w) OVER (
         PARTITION BY inverter_id
         ORDER BY time
         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS ac_power_w_filled
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
ORDER BY inverter_id, time;
