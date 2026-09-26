-- ==========================================================================
-- E9. Fleet health summary
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: CTE, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E9)
-- ==========================================================================
WITH latest AS (
  SELECT *
  FROM last_cache('inverter_telemetry', 'inverter_current')
),
today AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         SUM(ac_power_w) AS ac_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
  GROUP BY 1
)
SELECT (SELECT MAX(time) FROM today)                    AS data_through,
       (SELECT COUNT(*) FROM latest)                    AS device_count,
       (SELECT COUNT(*) FROM latest WHERE clipping)      AS clipping_now,
       (SELECT ROUND(MIN(ac_power_w), 1) FROM today)    AS min_hourly_w,
       (SELECT ROUND(MAX(ac_power_w), 1) FROM today)    AS peak_hourly_w;
