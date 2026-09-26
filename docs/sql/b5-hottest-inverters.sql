-- ==========================================================================
-- B5. Hottest inverters
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B5)
-- ==========================================================================
SELECT inverter_id,
       MAX(heatsink_temp_c) AS max_temp_c,
       AVG(heatsink_temp_c) AS avg_temp_c
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY max_temp_c DESC;
