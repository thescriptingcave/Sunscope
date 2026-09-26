"""Tests for the SQL layer: the injection defence and the dialect rules.

These matter more than endpoint tests. The allowlists are the only thing
between a client and an admin-scoped InfluxDB token, and the dialect
constraints (no ``QUALIFY``, ordinal ``GROUP BY``, parameters only in ``WHERE``)
are easy to get subtly wrong in a way that still returns data.
"""

from __future__ import annotations

import pytest

from solar_api import sql as sqlmod
from solar_api.sql import QueryError

# --- allowlists -------------------------------------------------------------


def test_unknown_table_is_rejected():
    with pytest.raises(QueryError, match="unknown table"):
        sqlmod.build_series_query(
            table="information_schema.tables", metric="ac_power_w",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        )


def test_unknown_metric_is_rejected():
    with pytest.raises(QueryError, match="unknown metric"):
        sqlmod.build_series_query(
            table="inverter_telemetry", metric="; DROP TABLE site_rollup",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        )


def test_unknown_interval_is_rejected():
    with pytest.raises(QueryError, match="unknown interval"):
        sqlmod.build_series_query(
            table="inverter_telemetry", metric="ac_power_w", interval="1 hour'; --",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        )


def test_unknown_dimension_is_rejected():
    with pytest.raises(QueryError, match="unknown dimension"):
        sqlmod.build_series_query(
            table="inverter_telemetry", metric="ac_power_w",
            group_by="1) UNION SELECT * FROM site_rollup --",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        )


# --- values are bound, not interpolated -------------------------------------


def test_user_values_never_appear_in_the_sql_text():
    """The core invariant: a hostile value must not reach the SQL string."""
    hostile = "x' OR 1=1 --"
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w",
        start=hostile, end=hostile, site=hostile,
    )
    assert hostile not in built.sql
    assert "$site" in built.sql
    assert "$start_time" in built.sql
    assert built.params["site"] == hostile
    assert built.params["start_time"] == hostile


def test_identifiers_come_only_from_allowlists():
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", group_by="inverter_id",
        start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
    )
    # Table, column, bucket and dimension are all ours, so they can be literal.
    assert "FROM inverter_telemetry" in built.sql
    assert 'AVG("ac_power_w")' in built.sql
    assert '"inverter_id"' in built.sql
    assert "INTERVAL '5 minutes'" in built.sql


# --- dialect rules ----------------------------------------------------------


def test_bucketed_query_uses_ordinal_group_by():
    """Grouping by the `time` alias would group by the raw column instead.

    InfluxDB 3 cannot group by a SELECT alias whose name matches the underlying
    column, so an expression must be grouped by ordinal. Getting this wrong
    produces wrong results with no error.
    """
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", interval="15m",
        start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
    )
    assert "date_bin(INTERVAL '15 minutes', time) AS time" in built.sql
    assert "GROUP BY 1" in built.sql
    assert "GROUP BY time" not in built.sql


def test_raw_interval_has_no_date_bin():
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", interval="raw",
        start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
    )
    assert "date_bin" not in built.sql
    assert 'SELECT time, "ac_power_w" AS value' in built.sql


def test_limit_is_an_integer_not_a_string():
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", limit=100,
        start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
    )
    assert "LIMIT 100" in built.sql


def test_no_qualify_anywhere():
    """InfluxDB 3 has no QUALIFY clause. Top-N-per-group must use a derived table."""
    statements = [
        sqlmod.build_series_query(
            table="inverter_telemetry", metric="ac_power_w",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        ).sql,
        sqlmod.build_fleet_summary()[0],
        sqlmod.build_string_imbalance()[0],
        sqlmod.build_event_feed()[0],
    ]
    for sql in statements:
        assert "qualify" not in sql.lower()


def test_type_is_not_used_as_an_identifier():
    """`type` is a reserved word in this dialect."""
    for name, metrics in sqlmod.METRICS.items():
        assert "type" not in metrics, f"{name} uses reserved word 'type'"


def test_string_imbalance_compares_within_a_single_timestamp():
    """Guards the shape of the fix, not just its output.

    A whole-window MIN/MAX looks correct and is not: it reports the day/night
    cycle as string imbalance. The collapse to per-timestamp spread has to
    happen before the outer aggregate, so the assertion is on the query text --
    the live test covers the numbers, this covers the cause.
    """
    sql = sqlmod.build_string_imbalance()[0]
    assert "GROUP BY time, inverter_id" in sql
    # The inner CTE must not aggregate away `time`, or every string would be
    # pooled back into one number before the spread is computed.
    inner = sql.split("scored AS")[0]
    assert "GROUP BY time, inverter_id" in inner
    # Samples missing a string are dropped rather than scored: a partial sample
    # is indistinguishable from a dead string.
    assert "strings_present >= $expected_strings" in sql
    # Worst case in the window, not the mean -- this is an alarm.
    assert "MAX(imbalance_ratio)" in sql


# --- last value cache -------------------------------------------------------


def test_now_query_uses_string_literals():
    """last_cache() args must be string literals; parameters are not accepted there."""
    built = sqlmod.build_now_query()
    assert "last_cache('inverter_telemetry', 'inverter_current')" in built


def test_now_query_rejects_a_table_without_a_cache():
    with pytest.raises(QueryError, match="no Last Value Cache"):
        sqlmod.build_now_query("site_rollup")


# --- read-only guard -------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "  select   *   from   site_rollup  ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SHOW TABLES",
        "SELECT * FROM site_rollup;",
    ],
)
def test_read_only_guard_allows_reads(statement: str):
    assert sqlmod.build_read_only_sql(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE site_rollup",
        "DELETE FROM site_rollup",
        "INSERT INTO site_rollup VALUES (1)",
        "UPDATE site_rollup SET pr_ratio = 1",
        "ALTER TABLE site_rollup ADD COLUMN x INT",
        "CREATE TABLE evil (a int)",
        "SELECT 1; DROP TABLE site_rollup",
        "SELECT 1 -- ok\n; DROP TABLE site_rollup",
        "GRANT ALL ON solar TO x",
    ],
)
def test_read_only_guard_rejects_writes(statement: str):
    with pytest.raises(QueryError):
        sqlmod.build_read_only_sql(statement)


def test_read_only_guard_does_not_trip_on_column_names():
    """A column called updated_at must not be mistaken for UPDATE."""
    assert sqlmod.build_read_only_sql("SELECT updated_at FROM events")
    assert sqlmod.build_read_only_sql("SELECT created_at, delete_flag FROM events")


def test_read_only_guard_rejects_a_leading_paren():
    """A leading comment or paren must not smuggle a write past the prefix test."""
    with pytest.raises(QueryError):
        sqlmod.build_read_only_sql("(SELECT 1); DROP TABLE site_rollup")


# --- regressions: both of these were live bugs, caught by tests/test_live.py ---


def test_boolean_metric_is_not_averaged():
    """AVG rejects booleans outright in this engine.

    "Avg does not support inputs of type Boolean" is a planning-time error, so
    a bool in the allowlist means every bucketed request for it 400s.
    """
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="clipping", interval="15m",
        start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
    )
    assert 'MAX("clipping")' in built.sql
    assert "AVG" not in built.sql


def test_numeric_metrics_are_averaged():
    for metric in ("ac_power_w", "uptime_s"):
        built = sqlmod.build_series_query(
            table="inverter_telemetry", metric=metric, interval="1h",
            start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        )
        assert f'AVG("{metric}")' in built.sql


def test_every_allowlisted_metric_has_a_usable_aggregate():
    """Sweeping the allowlist is the point: a typo in one entry is otherwise invisible."""
    for table, metrics in sqlmod.METRICS.items():
        for metric, (_, sql_type) in metrics.items():
            assert sql_type in sqlmod.AGGREGATE_FOR_TYPE, (
                f"{table}.{metric} has type {sql_type!r} with no aggregate defined"
            )


def test_severity_filter_uses_scalar_parameters_not_a_list():
    """JSON arrays are rejected: only null, boolean, number and string bind.

    So `IN ($severity)` with a list value fails with "JSON arrays are not
    supported as query parameters". One placeholder per severity instead.
    """
    sql, params = sqlmod.build_event_feed(["warning", "critical"])
    assert "$severity)" not in sql
    assert "$severity_0" in sql and "$severity_1" in sql
    assert params["severity_0"] == "warning"
    assert params["severity_1"] == "critical"
    # No value in params is a list.
    assert not any(isinstance(v, list) for v in params.values())


def test_severity_filter_rejects_an_unknown_severity():
    with pytest.raises(QueryError, match="unknown severity"):
        sqlmod.build_event_feed(["warning", "'; DROP TABLE events; --"])
