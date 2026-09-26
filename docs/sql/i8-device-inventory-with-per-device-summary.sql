-- ==========================================================================
-- I8. Device inventory with per-device summary
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I8)
-- ==========================================================================
SELECT inverter_id,
       COUNT(*)            AS sample_count,
       MAX(time)           AS last_report,
       AVG(ac_power_w)     AS avg_ac_power_w,
       MAX(ac_power_w)     AS peak_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY last_report DESC;
