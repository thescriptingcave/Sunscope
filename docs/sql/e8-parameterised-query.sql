-- ==========================================================================
-- E8. Parameterised query
-- ==========================================================================
-- Tier       : Expert — self-joins, correlated subqueries, change-point detection
-- Demonstrates: core SELECT fundamentals
--
-- NOTE: this query uses $name placeholders, which is the point -- it is the
--       parameter binding that makes ad-hoc SQL injection-safe. Bindings
--       travel as a JSON field and are never spliced into the SQL text.
--       The CLI has no flag for them, so run this one through the API:
--
--         TOKEN=...            # see README, 'Verify it yourself'
--         curl -sG http://127.0.0.1:8000/api/explore \
--           -H "Authorization: Bearer $TOKEN" \
--           --data-urlencode "sql=$(sed 's/^--.*//' this-file.sql)" \
--           --data-urlencode 'params={"site":"mojave", ...}'
--
-- Full discussion: docs/06-sql-examples.md (E8)
-- ==========================================================================
SELECT time, inverter_id, ac_power_w
FROM inverter_telemetry
WHERE site = $site
  AND time >= $start_time
  AND time <  $end_time
  AND ac_power_w >= $min_power
ORDER BY time;
