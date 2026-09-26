-- ==========================================================================
-- I7. Recent errors and warnings
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I7)
-- ==========================================================================
SELECT time, severity, source, code, message, value, threshold
FROM events
WHERE site = 'mojave'
  AND severity IN ('warning', 'critical')
  AND time >= now() - INTERVAL '24 hours'
ORDER BY time DESC;
