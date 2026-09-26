-- ==========================================================================
-- I2. Hourly totals, only where the farm was actually producing
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: time bucketing
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I2)
-- ==========================================================================
SELECT date_bin(INTERVAL '1 hour', time) AS time,
       SUM(ac_power_w) AS total_ac_power_w,
       AVG(efficiency) AS avg_efficiency
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY 1
HAVING SUM(ac_power_w) > 0
ORDER BY 1;
