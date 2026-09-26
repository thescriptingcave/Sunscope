-- ==========================================================================
-- I5. String imbalance per inverter
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: CTE, subquery
--
-- The naive version of this query is wrong, and wrong in the worst
-- possible direction. It is worth showing both, because the failure is
-- silent and the number looks plausible.
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
-- Full discussion: docs/06-sql-examples.md (I5)
-- ==========================================================================
-- WRONG. Do not ship this.
SELECT inverter_id,
       COUNT(DISTINCT string_id) AS string_count,
       MIN(dc_power_w) AS min_dc_power_w,
       MAX(dc_power_w) AS max_dc_power_w,
       ROUND((MAX(dc_power_w) - MIN(dc_power_w)) / NULLIF(MAX(dc_power_w), 0), 4)
         AS imbalance_ratio
FROM string_telemetry
WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
GROUP BY inverter_id
ORDER BY imbalance_ratio DESC;

WITH per_sample AS (
    SELECT time,
           inverter_id,
           COUNT(DISTINCT string_id) AS strings_present,
           MAX(dc_power_w)           AS strongest_w,
           MIN(dc_power_w)           AS weakest_w
    FROM string_telemetry
    WHERE site = 'mojave' AND time >= now() - INTERVAL '24 hours'
    GROUP BY time, inverter_id          -- the fix: time is part of the grain
),
scored AS (
    SELECT inverter_id, time, strongest_w, weakest_w,
           (strongest_w - weakest_w) / NULLIF(strongest_w, 0) AS imbalance_ratio
    FROM per_sample
    WHERE strings_present >= 3 AND strongest_w > 0
)
SELECT inverter_id,
       3                        AS string_count,
       ROUND(MIN(weakest_w), 1)  AS min_dc_power_w,
       ROUND(MAX(strongest_w), 1) AS max_dc_power_w,
       ROUND(MAX(imbalance_ratio), 4) AS imbalance_ratio
FROM scored
GROUP BY inverter_id
ORDER BY imbalance_ratio DESC;
