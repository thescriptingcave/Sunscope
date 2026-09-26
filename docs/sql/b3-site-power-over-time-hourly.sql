-- ==========================================================================
-- B3. Site power over time, hourly
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: time bucketing
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B3)
-- ==========================================================================
SELECT date_bin(INTERVAL '1 hour', time) AS time,
       SUM(ac_power_w) AS total_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 1;
