-- ==========================================================================
-- I3. Join telemetry to weather
-- ==========================================================================
-- Tier       : Intermediate — CASE, joins, subqueries, aggregation
-- Demonstrates: core SELECT fundamentals
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
-- Full discussion: docs/06-sql-examples.md (I3)
-- ==========================================================================
SELECT i.time,
       i.inverter_id,
       i.ac_power_w,
       w.ghi,
       w.air_temp_c,
       w.clearness_index
FROM inverter_telemetry AS i
INNER JOIN weather_station AS w
  ON i.time = w.time
 AND i.site  = w.site
WHERE i.site = 'mojave'
  AND i.time >= now() - INTERVAL '2 hours'
ORDER BY i.time;

SELECT i.time, i.inverter_id, i.ac_power_w, w.ghi
FROM inverter_telemetry AS i
INNER JOIN weather_station AS w
  ON i.site = w.site
 AND w.time <= i.time
 AND w.time > i.time - INTERVAL '5 minutes'
ORDER BY i.time;
