-- ==========================================================================
-- I1. Classify inverter load state
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I1)
-- ==========================================================================
SELECT time, inverter_id, ac_power_w,
       CASE
         WHEN ac_power_w > 200000 THEN 'high'
         WHEN ac_power_w > 100000 THEN 'nominal'
         WHEN ac_power_w > 0      THEN 'low'
         ELSE 'offline'
       END AS load_state
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '3 hours'
ORDER BY time;
