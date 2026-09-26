"""SQL construction with strict allowlists.

Every value a client can influence reaches InfluxDB as a bound parameter, never
as interpolated text. The one exception is the Last Value Cache function, whose
arguments must be string literals; those are validated against the allowlists
here.

InfluxDB 3 Core restrictions that shape this module, all verified against
``influxdb:3.11-core`` and documented in ``docs/06-sql-examples.md``:

* Parameters are ``$name`` and work in ``WHERE`` predicates **only** -- not in
  ``SELECT``, ``GROUP BY``, function arguments, identifiers, or ``INTERVAL``
  literals. That is why the time range is passed as two timestamp parameters and
  the bucket size is an allowlisted string, not a parameter.
* Parameter binding is text substitution performed before planning. It is not a
  prepared statement and gets no caching benefit.
* ``GROUP BY`` requires an aggregate or selector in ``SELECT``, and the
  ``SELECT`` list may only contain ``GROUP BY`` columns or aggregates. Grouping
  by an expression therefore uses an ordinal, ``GROUP BY 1``.
* ``QUALIFY`` does not exist. "Top N per group" is a derived table plus
  ``ROW_NUMBER()`` and a ``WHERE`` filter on the outer query.
* ``type`` is a reserved word, so the schema uses ``status_code``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------
# Allowlists. Anything not here cannot be interpolated, ever.
# --------------------------------------------------------------------------

#: InfluxDB tables this API is willing to read.
TABLES: frozenset[str] = frozenset(
    {
        "inverter_telemetry",
        "string_telemetry",
        "weather_station",
        "site_rollup",
        "events",
    }
)

#: Last Value Caches, which are addressed as literal function arguments.
CACHES: frozenset[str] = frozenset({"inverter_current"})

#: Quantisation steps offered to clients. A closed set, because the interval is
#: interpolated into an INTERVAL literal and parameters cannot be used there.
INTERVALS: dict[str, str] = {
    "raw": "",  # no date_bin at all
    "1m": "INTERVAL '1 minute'",
    "5m": "INTERVAL '5 minutes'",
    "15m": "INTERVAL '15 minutes'",
    "1h": "INTERVAL '1 hour'",
    "6h": "INTERVAL '6 hours'",
    "1d": "INTERVAL '1 day'",
}

SORT_ORDERS: dict[str, str] = {"asc": "ASC", "desc": "DESC"}

#: Aggregate to use per column type when bucketing.
#:
#: AVG is right for float and int, but the engine rejects it outright for
#: booleans -- "Avg does not support inputs of type Boolean" -- so `clipping`
#: uses MAX, which reads naturally: was any sample in this bucket clipped?
AGGREGATE_FOR_TYPE: dict[str, str] = {"float": "AVG", "int": "AVG", "bool": "MAX"}

#: Metrics the ``/api/series`` endpoint exposes, per table. Values are
#: (column, sql_type) so the API can coerce JSON values to the column's type.
#:
#: This doubles as documentation of the schema: adding a column here is the only
#: way a client can ask for it.
METRICS: dict[str, dict[str, tuple[str, str]]] = {
    "inverter_telemetry": {
        "ac_power_w": ("ac_power_w", "float"),
        "dc_power_w": ("dc_power_w", "float"),
        "ac_voltage_v": ("ac_voltage_v", "float"),
        "ac_current_a": ("ac_current_a", "float"),
        "dc_voltage_v": ("dc_voltage_v", "float"),
        "dc_current_a": ("dc_current_a", "float"),
        "efficiency": ("efficiency", "float"),
        "heatsink_temp_c": ("heatsink_temp_c", "float"),
        "internal_temp_c": ("internal_temp_c", "float"),
        "uptime_s": ("uptime_s", "int"),
        "status_code": ("status_code", "int"),
        "clipping": ("clipping", "bool"),
    },
    "string_telemetry": {
        "dc_power_w": ("dc_power_w", "float"),
        "dc_voltage_v": ("dc_voltage_v", "float"),
        "dc_current_a": ("dc_current_a", "float"),
        "module_temp_c": ("module_temp_c", "float"),
    },
    "weather_station": {
        "ghi": ("ghi", "float"),
        "dni": ("dni", "float"),
        "dhi": ("dhi", "float"),
        "air_temp_c": ("air_temp_c", "float"),
        "wind_speed_mps": ("wind_speed_mps", "float"),
        "relative_humidity": ("relative_humidity", "float"),
        "clearness_index": ("clearness_index", "float"),
    },
    "site_rollup": {
        "total_ac_power_w": ("total_ac_power_w", "float"),
        "daily_yield_kwh": ("daily_yield_kwh", "float"),
        "pr_ratio": ("pr_ratio", "float"),
        "capacity_factor": ("capacity_factor", "float"),
        "inverters_online": ("inverters_online", "int"),
        "strings_online": ("strings_online", "int"),
    },
}

#: Group-by dimensions, also allowlisted because they are interpolated.
DIMENSIONS: dict[str, dict[str, str]] = {
    "inverter_telemetry": {
        "inverter_id": "inverter_id",
        "block": "block",
    },
    "string_telemetry": {
        "inverter_id": "inverter_id",
        "string_id": "string_id",
    },
    "weather_station": {"station_id": "station_id"},
    "site_rollup": {},
}

#: The names the simulator publishes, so the API can render friendly labels.
EVENT_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "critical"})


class QueryError(ValueError):
    """A request was rejected before any SQL was built."""


@dataclass(frozen=True)
class SeriesQuery:
    sql: str
    params: dict[str, Any]
    metric: str
    column: str
    sql_type: str
    interval: str
    group_by: str | None


# ruff: noqa: S608 -- file-level justification:
# Every SQL string in this module is assembled from closed allowlists declared
# above (TABLES, METRICS, INTERVALS, DIMENSIONS). No caller-supplied value is
# ever interpolated: sites and time bounds become bound parameters, and
# identifiers are looked up in a dict before use. See tests/test_sql.py, which
# asserts a hostile value never reaches the SQL text.
def build_series_query(
    *,
    table: str,
    metric: str,
    start: str,
    end: str,
    interval: str = "5m",
    group_by: str | None = None,
    site: str = "mojave",
    limit: int = 5000,
) -> SeriesQuery:
    """Build a series query for one metric.

    Every caller-supplied value is validated against an allowlist and then bound
    as a parameter. The SQL text is assembled only from literals this module
    controls.

    Two shapes, because the dialect constrains them differently:

    * **bucketed** -- ``date_bin`` plus ``AVG``, grouped by ordinal 1. The
      ordinal is required: grouping by the ``time`` alias would silently group by
      the raw column instead, giving wrong buckets with no error.
    * **raw** -- the stored points, with **no GROUP BY at all**. The dialect
      requires an aggregate or selector alongside ``GROUP BY``, so a non-aggregate
      query cannot group. Grouping raw points would also collapse distinct rows,
      which is not what "raw" should mean.
    """
    if table not in TABLES:
        raise QueryError(f"unknown table {table!r}")
    metrics = METRICS.get(table)
    if not metrics:
        raise QueryError(f"table {table!r} has no series endpoint")
    if metric not in metrics:
        raise QueryError(f"unknown metric {metric!r} for {table!r}")
    if interval not in INTERVALS:
        raise QueryError(f"unknown interval {interval!r}")

    column, sql_type = metrics[metric]
    bucket = INTERVALS[interval]
    aggregate = AGGREGATE_FOR_TYPE[sql_type]

    # _dimension() already returns a quoted identifier; do not quote it again.
    dimension = _dimension(table, group_by) if group_by else ""
    group_column = f' AS "{group_by}"' if group_by else ""

    if bucket:
        select = (
            f"date_bin({bucket}, time) AS time, {aggregate}(\"{column}\") AS value"
            + (f", {dimension}{group_column}" if dimension else "")
        )
        # Ordinal 1 is the bucketed time; the dimension is named, not ordinal.
        group_clause = f"GROUP BY 1, {dimension}" if dimension else "GROUP BY 1"
    else:
        select = (
            f'time, "{column}" AS value'
            + (f", {dimension}{group_column}" if dimension else "")
        )
        group_clause = ""

    sql = (
        f"SELECT {select} FROM {table} "
        "WHERE site = $site AND time >= $start_time AND time < $end_time "
        f"{group_clause} ORDER BY 1 ASC "
        f"LIMIT {int(limit)}"
    )
    params = {"site": site, "start_time": start, "end_time": end}
    return SeriesQuery(
        sql=sql,
        params=params,
        metric=metric,
        column=column,
        sql_type=sql_type,
        interval=interval,
        group_by=group_by,
    )


def _dimension(table: str, group_by: str | None) -> str:
    """Resolve and quote a group-by column from the allowlist."""
    if not group_by:
        raise QueryError("group_by not supported for this table")
    dims = DIMENSIONS.get(table, {})
    if group_by not in dims:
        raise QueryError(f"unknown dimension {group_by!r} for {table!r}")
    return f'"{dims[group_by]}"'


def build_now_query(table: str = "inverter_telemetry") -> str:
    """Current state for every series, from the Last Value Cache.

    The cache is an in-memory structure on the server, which is the entire
    reason it exists: this answers in milliseconds instead of aggregating hours
    of Parquet, which is what lets the PWA show real numbers immediately on a
    cold load.
    """
    if table not in TABLES:
        raise QueryError(f"unknown table {table!r}")
    if table != "inverter_telemetry":
        # Only one cache exists today. Say so plainly rather than failing later
        # with a confusing SQL error.
        raise QueryError("no Last Value Cache is defined for that table")
    return "SELECT * FROM last_cache('inverter_telemetry', 'inverter_current')"


def build_fleet_summary() -> tuple[str, dict[str, Any]]:
    """Latest site rollup plus per-inverter aggregate, in one round trip.

    Uses a CTE, which InfluxDB 3 Core supports, with the parameterised time
    window in the ``WHERE`` clause of each branch.
    """
    sql = """
WITH latest_rollup AS (
    SELECT time, total_ac_power_w, daily_yield_kwh, pr_ratio,
           capacity_factor, inverters_online, strings_online
    FROM site_rollup
    WHERE site = $site AND time < $end_time
    ORDER BY time DESC
    LIMIT 1
),
per_inverter AS (
    SELECT inverter_id,
           AVG("ac_power_w") AS avg_ac_power_w,
           MAX("ac_power_w") AS peak_ac_power_w,
           MAX("heatsink_temp_c") AS max_heatsink_temp_c,
           AVG("efficiency") AS avg_efficiency
    FROM inverter_telemetry
    WHERE site = $site AND time >= $start_time AND time < $end_time
    GROUP BY 1
),
clipping AS (
    SELECT inverter_id, COUNT(*) AS clipped_samples
    FROM inverter_telemetry
    WHERE site = $site AND time >= $start_time AND time < $end_time AND clipping
    GROUP BY 1
)
SELECT r.time AS rollup_time,
       r.total_ac_power_w, r.daily_yield_kwh, r.pr_ratio,
       r.capacity_factor, r.inverters_online, r.strings_online,
       p.inverter_id, p.avg_ac_power_w, p.peak_ac_power_w,
       p.max_heatsink_temp_c, p.avg_efficiency,
       COALESCE(c.clipped_samples, 0) AS clipped_samples
FROM per_inverter p
LEFT JOIN clipping c ON p.inverter_id = c.inverter_id
LEFT JOIN latest_rollup r ON 1 = 1
ORDER BY p.inverter_id
"""
    return sql, {"site": "mojave"}


def build_string_imbalance(
    min_ratio: float = 0.0, expected_strings: int = 3
) -> tuple[str, dict[str, Any]]:
    """Per-inverter string spread, the fault signature for a bad string.

    **Strings must be compared at the same instant.** The obvious query --
    ``MIN(dc_power_w)`` and ``MAX(dc_power_w)`` over the whole window -- is
    wrong, and wrong in the worst possible direction: it reports the *day/night
    cycle* as string imbalance. Over a 24 h window the weakest reading is dawn
    and the strongest is noon, so a perfectly healthy farm scores 25-50 % and
    every inverter lights up as faulty. The per-timestamp collapse below fixes
    that by never comparing across times.

    Three details that matter:

    * ``NULLIF`` guards the divide when an inverter is fully offline and every
      string reads zero.
    * Only samples where *every* string reported are scored. A partial sample is
      indistinguishable from a dead string, so scoring it would manufacture an
      imbalance out of a dropped message.
    * The reported figure is the **worst** per-sample ratio in the window, not
      the mean. This is an alarm: a bad string is bad for part of the day, and
      averaging it away is how a real fault gets missed.
    """
    sql = """
WITH per_sample AS (
    SELECT time,
           inverter_id,
           COUNT(DISTINCT string_id) AS strings_present,
           MAX("dc_power_w")           AS strongest_w,
           MIN("dc_power_w")           AS weakest_w
    FROM string_telemetry
    WHERE site = $site AND time >= $start_time AND time < $end_time
    GROUP BY time, inverter_id
),
scored AS (
    SELECT inverter_id,
           time,
           strongest_w,
           weakest_w,
           (strongest_w - weakest_w) / NULLIF(strongest_w, 0) AS imbalance_ratio
    FROM per_sample
    WHERE strings_present >= $expected_strings AND strongest_w > 0
)
SELECT inverter_id,
       $expected_strings                AS string_count,
       ROUND(MIN(weakest_w), 1)          AS min_dc_power_w,
       ROUND(MAX(strongest_w), 1)        AS max_dc_power_w,
       ROUND(MAX(imbalance_ratio), 4)    AS imbalance_ratio
FROM scored
GROUP BY inverter_id
HAVING MAX(imbalance_ratio) >= $min_ratio
ORDER BY imbalance_ratio DESC
"""
    return sql, {
        "site": "mojave",
        "min_ratio": min_ratio,
        "expected_strings": expected_strings,
    }


def build_event_feed(severities: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    """Recent events, optionally filtered by severity.

    ``severity`` is a tag, so filtering happens in SQL against a bound
    parameter list rather than in Python after the fact.
    """
    params: dict[str, Any] = {"site": "mojave", "start_time": "", "end_time": ""}
    sql = """
SELECT time, severity, source, code, message, value, threshold
FROM events
WHERE site = $site AND time >= $start_time AND time < $end_time
ORDER BY time DESC
LIMIT 200
"""
    if severities:
        unknown = [s for s in severities if s not in EVENT_SEVERITIES]
        if unknown:
            raise QueryError(f"unknown severity in {unknown!r}")
        # One scalar parameter per severity. `IN ($severity)` with a list is
        # impossible here: the engine accepts only null, boolean, number or
        # string parameters and rejects a JSON array outright with
        # "JSON arrays are not supported as query parameters".
        placeholders = []
        for index, severity in enumerate(severities):
            key = f"severity_{index}"
            placeholders.append(f"${key}")
            params[key] = severity
        sql = sql.replace(
            "AND time < $end_time",
            f"AND time < $end_time AND severity IN ({', '.join(placeholders)})",
            1,
        )
    return sql, params


def build_read_only_sql(sql: str) -> str:
    """Reject anything that is not a single read-only statement.

    Guards the exploration endpoint. This is a **deny-list**, which is weaker
    than an allow-list, and that is a deliberate, disclosed trade: the token is
    admin-scoped because InfluxDB 3 Core offers no read-only tokens, so this
    function is the only thing standing between a client and a full admin token.

    Strengthen it before exposing the endpoint beyond localhost.
    """
    normalised = " ".join(sql.split()).strip().rstrip(";")
    lowered = normalised.lower()

    if not lowered.startswith(("select", "with", "show")):
        raise QueryError("only SELECT, WITH and SHOW statements are allowed")

    forbidden = (
        "insert", "update", "delete", "drop", "alter", "create",
        "truncate", "grant", "revoke", "attach", "copy", "call",
        "merge into", "upsert",
    )
    # Checked as whole words so a column called `updated_at` does not trip it.
    tokens = {t.strip("(),;'\"") for t in lowered.replace("(", " ").replace(")", " ").split()}
    hits = sorted(tokens & set(forbidden))
    if hits:
        raise QueryError(f"statement contains forbidden keyword(s): {hits}")

    # A stacked statement is the classic way past a naive check.
    if ";" in normalised:
        raise QueryError("multiple statements are not allowed")
    return normalised
