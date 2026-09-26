-- ==========================================================================
-- E4. Correlated subquery: worst event per device
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E4)
-- ==========================================================================
SELECT e.time,
       e.source,
       e.severity,
       e.code,
       e.message
FROM events AS e
WHERE e.site = 'mojave'
  AND e.time >= now() - INTERVAL '7 days'
  AND e.value = (
    SELECT MAX(inner_e.value)
    FROM events AS inner_e
    WHERE inner_e.source = e.source
      AND inner_e.time >= now() - INTERVAL '7 days'
  )
ORDER BY e.severity, e.time DESC;
