-- ==========================================================================
-- E3. Self-join to compare consecutive intervals
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E3)
-- ==========================================================================
SELECT current.time                              AS time,
       current.site                              AS site,
       current.ac_power_w                        AS current_power_w,
       previous.ac_power_w                       AS previous_power_w,
       current.ac_power_w - previous.ac_power_w  AS change_w
FROM inverter_telemetry AS current
LEFT JOIN inverter_telemetry AS previous
  ON current.inverter_id = previous.inverter_id
 AND previous.time = current.time - INTERVAL '1 hour'
WHERE current.site = 'mojave'
  AND current.time >= now() - INTERVAL '6 hours'
ORDER BY current.time;
