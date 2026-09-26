-- ==========================================================================
-- B1. Recent inverter readings
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B1)
-- ==========================================================================
SELECT time, inverter_id, ac_power_w, efficiency
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 50;
