-- ==========================================================================
-- E2. Anomaly detection against a prior-only baseline
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: CTE, window function, frame, time bucketing, subquery
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E2)
-- ==========================================================================
WITH hourly AS (
  SELECT date_bin(INTERVAL '1 hour', time) AS time,
         inverter_id,
         AVG(ac_power_w) AS avg_power_w,
         STDDEV(ac_power_w) AS sd_power_w
  FROM inverter_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '7 days'
  GROUP BY 1, inverter_id
),
baseline AS (
  SELECT time,
         inverter_id,
         avg_power_w,
         sd_power_w,
         AVG(avg_power_w) OVER (
           PARTITION BY inverter_id
           ORDER BY time
           ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING
         ) AS prior_mean
  FROM hourly
)
SELECT time,
       inverter_id,
       ROUND(avg_power_w, 1) AS avg_power_w,
       ROUND(prior_mean, 1)  AS prior_mean,
       ROUND(avg_power_w - prior_mean, 1) AS delta_w
FROM baseline
WHERE prior_mean IS NOT NULL
  AND avg_power_w < prior_mean * 0.7
ORDER BY time;
