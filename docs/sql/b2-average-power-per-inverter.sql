-- ==========================================================================
-- B2. Average power per inverter
-- ==========================================================================
-- Tier       : Beginner — SELECT / WHERE / GROUP BY / ORDER BY
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (B2)
-- ==========================================================================
SELECT inverter_id,
       AVG(ac_power_w) AS avg_ac_power_w,
       MAX(ac_power_w) AS peak_ac_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '6 hours'
GROUP BY inverter_id
ORDER BY avg_ac_power_w DESC;
