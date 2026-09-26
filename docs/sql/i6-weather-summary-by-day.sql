-- ==========================================================================
-- I6. Weather summary by day
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: time bucketing
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (I6)
-- ==========================================================================
SELECT date_bin(INTERVAL '1 day', time) AS time,
       AVG(ghi)             AS avg_ghi,
       MAX(air_temp_c)      AS max_air_temp_c,
       AVG(clearness_index) AS avg_clearness_index,
       SUM(wind_speed_mps)  AS wind_sample_sum
FROM weather_station
WHERE site = 'mojave'
  AND time >= now() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1;
