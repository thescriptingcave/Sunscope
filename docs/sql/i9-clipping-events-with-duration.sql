-- ==========================================================================
-- I9. Clipping events with duration
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I9)
-- ==========================================================================
SELECT inverter_id,
       COUNT(*) AS clipped_samples,
       COUNT(*) * 60 AS approx_clipped_seconds
FROM inverter_telemetry
WHERE site = 'mojave'
  AND clipping = true
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY clipped_samples DESC;
