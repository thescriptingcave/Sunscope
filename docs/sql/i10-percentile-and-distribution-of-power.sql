-- ==========================================================================
-- I10. Percentile and distribution of power
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I10)
-- ==========================================================================
SELECT inverter_id,
       MEDIAN(ac_power_w)  AS median_power_w,
       STDDEV(ac_power_w)  AS sd_power_w,
       MIN(ac_power_w)     AS min_power_w,
       MAX(ac_power_w)     AS max_power_w
FROM inverter_telemetry
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '24 hours'
  AND ac_power_w > 0
GROUP BY inverter_id
ORDER BY median_power_w DESC;
