-- ==========================================================================
-- B6. All events today
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: time bucketing
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B6)
-- ==========================================================================
SELECT time, severity, source, code, message
FROM events
WHERE site = 'mojave'
  AND time >= date_trunc('day', now())
ORDER BY time DESC;
