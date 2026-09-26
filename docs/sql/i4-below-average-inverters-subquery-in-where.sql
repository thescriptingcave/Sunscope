-- ==========================================================================
-- I4. Below-average inverters (subquery in WHERE)
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I4)
-- ==========================================================================
SELECT time, inverter_id, ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
  AND ac_power_w < (
        SELECT AVG(ac_power_w)
        FROM inverter_telemetry
        WHERE site = 'mojave'
          AND time >= now() - INTERVAL '6 hours'
      )
ORDER BY time;
