"""Live integration tests against a running stack.

Skipped unless the stack is up, because a stub cannot tell you that InfluxDB
*accepts* a query. The unit tests prove the SQL is injection-safe; these prove
it is also dialect-valid, which is a different failure and an easier one to
ship by accident.

    docker compose up -d
    cd api && uv run pytest tests/test_live.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from solar_api import sql as sqlmod
from solar_api.config import Settings
from solar_api.influx import InfluxClient


def _reachable() -> bool:
    """True when the stack is up, judged by whether the token file exists.

    A connection probe would be better but would slow the default (offline) test
    run; a missing token file is an equally reliable signal in practice, because
    scripts/gen-secrets.sh plus `docker compose up influx-init` create it.
    """
    token_file = os.environ.get("INFLUX_TOKEN_FILE", "../secrets/api-read.token")
    return os.path.exists(token_file)


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="stack not running; see docs/README.md"
)


@pytest.fixture
async def client():
    settings = Settings()
    c = InfluxClient(settings)
    try:
        yield c
    finally:
        await c.close()


def _window(hours: int = 48) -> tuple[str, str]:
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


async def test_ping_and_simple_select(client: InfluxClient):
    rows = await client.query("SELECT 1 AS one")
    assert rows and rows[0]["one"] == 1


async def test_last_value_cache_query_runs(client: InfluxClient):
    """The query behind /api/now, which is the PWA cold-load path."""
    rows = await client.query(sqlmod.build_now_query())
    assert isinstance(rows, list)


async def test_parameterised_where_clause_runs(client: InfluxClient):
    """$name binding in a WHERE predicate, which is the whole injection defence."""
    start, end = _window()
    sql, params = sqlmod.build_string_imbalance(0.0)
    params.update({"start_time": start, "end_time": end})
    rows = await client.query(sql, params)
    assert isinstance(rows, list)


async def test_string_imbalance_is_not_measuring_the_day_night_cycle(client: InfluxClient):
    """Regression: the imbalance query used MIN/MAX over the *whole window*.

    That measures the day/night cycle, not the spread between strings, and it
    reported 23-26 % for a farm with no string fault -- every inverter rendered
    red on the dashboard.

    The assertion is deliberately *relative* rather than an absolute "< 10 %"
    bound. A live test that hardcodes a healthy-farm threshold fails whenever a
    fault is legitimately injected, which is a test that cries wolf and gets
    ignored. Comparing against the naive window-wide query instead asks the
    question that actually matters: are the two numbers wildly different, which
    is what proves the per-timestamp collapse is happening?
    """
    start, end = _window()
    sql, params = sqlmod.build_string_imbalance(0.0)
    params.update({"start_time": start, "end_time": end})
    rows = await client.query(sql, params)
    if not rows:
        pytest.skip("no string telemetry in the window")

    naive_sql = """
SELECT inverter_id,
       ROUND((MAX("dc_power_w") - MIN("dc_power_w"))
             / NULLIF(MAX("dc_power_w"), 0), 4) AS naive_ratio
FROM string_telemetry
WHERE site = $site AND time >= $start_time AND time < $end_time
GROUP BY inverter_id
"""
    naive = {r["inverter_id"]: r["naive_ratio"] for r in await client.query(naive_sql, params)}

    for row in rows:
        inverter = row["inverter_id"]
        assert row["string_count"] == 3, (
            f"{inverter} reported {row['string_count']} strings, expected 3"
        )
        assert row["min_dc_power_w"] <= row["max_dc_power_w"]
        # A per-timestamp spread cannot exceed the whole-window spread, and on a
        # farm spanning a day/night cycle it is dramatically smaller. Equal
        # values would mean the query regressed to the naive form.
        if inverter in naive and naive[inverter]:
            assert row["imbalance_ratio"] < naive[inverter], (
                f"{inverter}: per-timestamp imbalance {row['imbalance_ratio']} is not "
                f"below the whole-window spread {naive[inverter]}"
            )


async def test_parameter_binding_is_not_interpolation(client: InfluxClient):
    """A hostile parameter must be treated as a literal and match nothing."""
    rows = await client.query(
        "SELECT count(*) AS n FROM inverter_telemetry WHERE site = $site",
        {"site": "x' OR 1=1 --"},
    )
    assert rows[0]["n"] == 0


async def test_bucketed_series_runs_with_group_by(client: InfluxClient):
    start, end = _window()
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", interval="1h",
        group_by="inverter_id", start=start, end=end,
    )
    rows = await client.query(built.sql, built.params)
    assert isinstance(rows, list)
    if rows:
        # The group key must be present, or the caller cannot tell the series apart.
        assert "inverter_id" in rows[0]


async def test_raw_series_runs(client: InfluxClient):
    start, end = _window()
    built = sqlmod.build_series_query(
        table="inverter_telemetry", metric="ac_power_w", interval="raw",
        start=start, end=end, limit=10,
    )
    assert "GROUP BY" not in built.sql
    rows = await client.query(built.sql, built.params)
    assert isinstance(rows, list)


async def test_fleet_summary_cte_runs(client: InfluxClient):
    start, end = _window()
    query, params = sqlmod.build_fleet_summary()
    params.update({"start_time": start, "end_time": end})
    rows = await client.query(query, params)
    assert isinstance(rows, list)


async def test_event_feed_runs_with_severity_filter(client: InfluxClient):
    start, end = _window()
    query, params = sqlmod.build_event_feed(["warning", "critical"])
    params.update({"start_time": start, "end_time": end})
    rows = await client.query(query, params)
    assert isinstance(rows, list)
    for row in rows:
        assert row["severity"] in {"warning", "critical"}


async def test_every_allowlisted_metric_executes(client: InfluxClient):
    """Sweep the whole allowlist so a typo cannot hide in an unexercised entry."""
    start, end = _window()
    checked = 0
    for table, metrics in sqlmod.METRICS.items():
        for metric in metrics:
            built = sqlmod.build_series_query(
                table=table, metric=metric, interval="1h",
                start=start, end=end, limit=5,
            )
            await client.query(built.sql, built.params)
            checked += 1
    assert checked >= 25, f"only swept {checked} metrics"


async def test_read_only_guard_matches_server_behaviour(client: InfluxClient):
    """The guard's allow-list must not contain anything the server rejects."""
    for statement in ("SELECT 1", "SELECT 1 AS one", "SHOW TABLES"):
        sqlmod.build_read_only_sql(statement)
        await client.query(statement)
