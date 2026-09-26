-- ==========================================================================
-- E7. Current state and cache inventory
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E7)
-- ==========================================================================
-- All tables and their columns
SELECT table_name, column_name, data_type, is_nullable, ordinal_position
FROM information_schema.columns
WHERE table_schema = 'iox'
ORDER BY table_name, ordinal_position;

-- Just the tables
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'iox'
ORDER BY table_name;

-- Live server settings
SELECT * FROM information_schema.df_settings;

-- Active and recent queries
SELECT query_text, issue_time, execute_duration, success
FROM system.queries
ORDER BY issue_time DESC
LIMIT 20;
