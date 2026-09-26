-- ==========================================================================
-- B4. Which devices exist
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B4)
-- ==========================================================================
SELECT DISTINCT inverter_id
FROM inverter_telemetry
WHERE site = 'mojave'
ORDER BY inverter_id;
