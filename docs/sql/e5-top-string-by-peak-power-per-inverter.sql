-- ==========================================================================
-- E5. Top string by peak power, per inverter
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: CTE, window function, RANK, ROW_NUMBER, subquery
--
-- NOTE: this file holds more than one statement, and that is the point. The
--       first is the approach that looks right and is wrong; the second is
--       the fix. The CLI accepts ONE statement per invocation, so run them
--       separately:
--
--         uv run python scripts/export-sql.py --verify
--
-- Run:  docker compose exec -T influxdb influxdb3 query \
--          --host https://localhost:8181 --tls-no-verify \
--          --database solar --token "$TOK" < this-file.sql
-- Full discussion: docs/06-sql-examples.md (E5)
-- ==========================================================================
SELECT time          AS peak_time,
       inverter_id,
       string_id,
       dc_power_w    AS peak_power_w
FROM (
  SELECT time,
         inverter_id,
         string_id,
         dc_power_w,
         ROW_NUMBER() OVER (
           PARTITION BY inverter_id
           ORDER BY dc_power_w DESC
         ) AS rn
  FROM string_telemetry
  WHERE site = 'mojave'
    AND time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY inverter_id;

WITH ranked AS (
  SELECT time, inverter_id, string_id, dc_power_w,
         RANK() OVER (PARTITION BY inverter_id ORDER BY dc_power_w DESC) AS rnk
  FROM string_telemetry
  WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
)
SELECT time, inverter_id, string_id, dc_power_w
FROM ranked WHERE rnk = 1 ORDER BY inverter_id;
