-- ==========================================================================
-- A10. Current device state from the Last Value Cache
-- ==========================================================================
-- Tier       : Advanced — CTEs, window functions and frames
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (A10)
-- ==========================================================================
SELECT *
FROM last_cache('inverter_telemetry', 'inverter_current');
