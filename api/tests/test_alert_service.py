"""Tests for rule loading and the alert service wiring.

The engine's behaviour is covered in test_alerts.py. What is tested here is
everything between the YAML file and the running service: that a malformed rule
is rejected loudly, that MQTT topics are routed to the right subject, and that
the weather gate is actually injected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solar_api.alert_config import load_rules
from solar_api.alert_service import TOPICS, AlertService
from solar_api.alerts import RuleEngine
from solar_api.config import Settings

# --- the shipped rule file --------------------------------------------------


def test_shipped_rules_load_and_validate():
    """The real config must parse, or the API refuses to start."""
    rules = load_rules()
    assert rules, "the shipped rule file yielded no rules"
    ids = [rule.id for rule in rules]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    for rule in rules:
        assert rule.severity in ("info", "warning", "critical")
        assert rule.scope in ("inverter", "site")


def test_every_documented_failure_mode_has_a_rule():
    """A gap here is a gap in coverage, not in configuration.

    These are the failures docs/01-design.md commits to detecting. The comms-loss
    and overheat rules in particular are the reason the alerting design exists.
    """
    ids = {rule.id for rule in load_rules()}
    required = {
        "telemetry_stale",          # comms loss: no Last Will, gap in the series
        "device_offline",           # power loss: Last Will fires
        "heatsink_high",            # overheat
        "heatsink_critical",
        "power_zero_in_sunlight",   # online, publishing, producing nothing
        "performance_ratio_low",    # soiling, drift, widespread string loss
        "site_rollup_stale",        # the whole site went quiet
        "clipping_sustained",
    }
    assert required <= ids, f"missing rules: {sorted(required - ids)}"


def test_staleness_rules_exist_and_have_a_threshold():
    staleness = [r for r in load_rules() if r.kind == "staleness"]
    assert staleness, "without a staleness rule a comms partition is undetectable"
    for rule in staleness:
        assert rule.stale_after_s > 0
        assert not rule.conditions, "a staleness rule has no metric to compare"


def test_noisy_rules_are_debounced_longer_than_quiet_ones():
    """Debounce must reflect how transient each condition is.

    Cloud ramps last 1-4 minutes, so anything irradiance-driven needs a debounce
    measured in minutes. Clipping at solar noon is expected on a 1.19 MWp plant
    and needs a long one, or it alerts every clear day.
    """
    by_id = {rule.id: rule for rule in load_rules()}
    assert by_id["performance_ratio_low"].debounce_s >= 300
    assert by_id["clipping_sustained"].debounce_s >= 900
    # The Last Will is unambiguous, so there is nothing to debounce.
    assert by_id["device_offline"].debounce_s <= 30


# --- malformed configuration -------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "alerts.yaml"
    path.write_text(body)
    return path


def test_missing_file_is_an_error(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    with pytest.raises(RuleConfigError, match="not found"):
        load_rules(tmp_path / "nope.yaml")


def test_empty_rules_list_is_an_error(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    with pytest.raises(RuleConfigError, match="list found"):
        load_rules(write(tmp_path, "version: 1\nrules: []\n"))


def test_unknown_operator_is_rejected(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    path = write(
        tmp_path,
        "rules:\n"
        "  - id: r\n    scope: inverter\n    severity: warning\n"
        "    metric: ac_power_w\n    operator: '~='\n    threshold: 1\n",
    )
    with pytest.raises(RuleConfigError, match="unknown operator"):
        load_rules(path)


def test_non_numeric_threshold_is_rejected(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    path = write(
        tmp_path,
        "rules:\n"
        "  - id: r\n    scope: inverter\n    severity: warning\n"
        "    metric: ac_power_w\n    operator: '>'\n    threshold: hot\n",
    )
    with pytest.raises(RuleConfigError, match="not numeric"):
        load_rules(path)


def test_bad_severity_is_rejected(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    path = write(
        tmp_path,
        "rules:\n"
        "  - id: r\n    scope: inverter\n    severity: apocalyptic\n"
        "    metric: ac_power_w\n    operator: '>'\n    threshold: 1\n",
    )
    with pytest.raises(RuleConfigError, match="unknown severity"):
        load_rules(path)


def test_missing_id_is_rejected(tmp_path: Path):
    from solar_api.alerts import RuleConfigError

    path = write(
        tmp_path,
        "rules:\n  - scope: inverter\n    severity: warning\n"
        "    metric: x\n    operator: '>'\n    threshold: 1\n",
    )
    with pytest.raises(RuleConfigError, match="no .id."):
        load_rules(path)


# --- service routing --------------------------------------------------------


class FakeInflux:
    def __init__(self) -> None:
        self.written: list[object] = []

    async def write_event(self, alert, site: str = "mojave") -> None:
        self.written.append(alert)


def service_with(rules=None):
    from solar_api.alerts import Condition, Rule

    rules = rules or [
        Rule(
            id="power_zero_in_sunlight",
            description="zero output in sunlight",
            scope="inverter",
            severity="critical",
            debounce_s=0,
            conditions=(
                Condition("ac_power_w", "<", 1000.0),
                Condition("ghi", ">", 200.0),
            ),
        )
    ]
    influx = FakeInflux()
    return AlertService(RuleEngine(rules), influx), influx


def telemetry(topic: str, **payload) -> tuple[str, bytes]:
    return topic, json.dumps(payload).encode()


def test_telemetry_topic_routes_to_the_inverter_id():
    service, _ = service_with()
    service._handle(*telemetry(
        "solar/mojave/block/BLK-A/inverter/INV-01/telemetry",
        ac_power_w=200_000.0, status_code=3,
    ))
    assert service.active_alerts() == []  # producing, so the gate holds
    assert service.counters["received"] == 1


def test_weather_ghi_is_injected_so_night_is_not_a_fault():
    """The gate depends on cross-referencing the weather station.

    Without the injection the `ghi` condition evaluates as missing, the rule
    never matches, and the fault it exists to catch goes unnoticed.
    """
    service, _ = service_with()
    topic = "solar/mojave/block/BLK-A/inverter/INV-01/telemetry"

    # Night first: no alert.
    service._handle(*telemetry("solar/mojave/weather/WS-01/telemetry", ghi=3.0))
    service._handle(*telemetry(topic, ac_power_w=0.0, status_code=1))
    assert service.active_alerts() == [], "zero output at night is correct"

    # Then full sun with the same zero output: now it is a fault.
    service._handle(*telemetry("solar/mojave/weather/WS-01/telemetry", ghi=850.0))
    service._handle(*telemetry(topic, ac_power_w=0.0, status_code=3))
    assert [a.rule_id for a in service.active_alerts()] == ["power_zero_in_sunlight"]


def test_status_topic_updates_status_code():
    from solar_api.alerts import Condition, Rule

    rule = Rule(
        id="device_offline", description="offline", scope="inverter",
        severity="critical", debounce_s=0,
        conditions=(Condition("status_code", "==", 0.0),),
    )
    service, _ = service_with([rule])
    service._handle(*telemetry(
        "solar/mojave/block/BLK-A/inverter/INV-02/status", status_code=0,
    ))
    assert [a.rule_id for a in service.active_alerts()] == ["device_offline"]


def test_rollup_routes_to_the_site_subject():
    from solar_api.alerts import Condition, Rule

    rule = Rule(
        id="pr_low", description="pr low", scope="site", severity="warning",
        debounce_s=0, conditions=(Condition("pr_ratio", "<", 0.7),),
    )
    service, _ = service_with([rule])
    service._handle(*telemetry("solar/mojave/rollup", pr_ratio=0.42, inverters_online=4))
    assert [a.subject for a in service.active_alerts()] == ["mojave"]


def test_another_site_is_ignored():
    """Only the configured site is evaluated.

    A second site on the same broker must not raise alerts that this API's
    operator has no way to act on.
    """
    service, _ = service_with()
    service._handle(*telemetry(
        "solar/other/block/BLK-A/inverter/INV-01/telemetry",
        ac_power_w=0.0, status_code=5,
    ))
    assert service.counters["received"] == 0


def test_malformed_json_is_counted_not_raised():
    service, _ = service_with()
    service._handle("solar/mojave/rollup", b"{not json")
    assert service.counters["errors"] == 1
    assert service.counters["received"] == 0


def test_subscriptions_cover_everything_the_rules_need():
    """A rule whose data is not subscribed to can never fire."""
    joined = " ".join(TOPICS)
    assert "/telemetry" in joined, "inverter telemetry not subscribed"
    assert "/rollup" in joined, "site rollup not subscribed"
    assert "/weather/" in joined, "weather station not subscribed; ghi gates would never open"
    assert "mojave" in joined


def test_line_protocol_escaping_prevents_tag_injection():
    """A hostile device id must not be able to add tag columns.

    Unescaped, a comma or space in the source would create new tags, silently
    changing the shape of the write.
    """
    from solar_api.influx import InfluxClient

    assert InfluxClient.escape_tag("a,b") == r"a\,b"
    assert InfluxClient.escape_tag("a=b") == r"a\=b"
    assert InfluxClient.escape_tag("a b") == r"a\ b"
    assert InfluxClient.escape_field('say "hi"') == r"say \"hi\""


# --- counters and persistence ----------------------------------------------


def test_staleness_alerts_are_counted_like_threshold_alerts():
    """Regression: `alerts_fired` read 0 while staleness alerts were on screen.

    The two paths into the engine -- an incoming message and the staleness tick --
    tallied separately, and only the message path incremented the counter. Every
    staleness alert, which is the whole point of the staleness rules, was
    invisible in /api/alert-stats.
    """
    from solar_api.alerts import Condition, Rule

    rules = [
        Rule(
            id="inverter_fault", description="d", scope="inverter",
            severity="critical", debounce_s=0,
            conditions=(Condition("status_code", "==", 5.0),),
        ),
        Rule(
            id="telemetry_stale", description="d", kind="staleness",
            scope="inverter", severity="critical", stale_after_s=1.0,
        ),
    ]
    service, _ = service_with(rules)

    service._handle(
        "solar/mojave/block/BLK-A/inverter/INV-01/telemetry",
        json.dumps({"ac_power_w": 0.0, "status_code": 5}).encode(),
    )
    assert service.counters["alerts_fired"] == 1

    # Go quiet, then drive the tick the way _tick_forever does.
    service._engine._subjects["INV-01"].last_seen -= 10
    for alert in service._engine.tick():
        service._count(alert)
        service._queue_write(alert)
    assert service.counters["alerts_fired"] == 2, "a staleness alert must be counted too"
    assert len(service.active_alerts()) == 2


async def test_events_are_persisted_to_the_events_table():
    """An alert must reach InfluxDB, or the feed is lost on restart.

    Written through the real InfluxClient against a stub transport, so the
    line protocol and the endpoint are both covered -- the endpoint being the
    part that was wrong (`/api/v3/write` does not exist in 3.11; it is
    `/api/v3/write_lp`).
    """
    import httpx

    from solar_api.alerts import Alert
    from solar_api.influx import InfluxClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = InfluxClient(
        Settings(INFLUX_API_TOKEN="test-token", INFLUX_DB="solar"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://influxdb:8181"
        ),
    )
    alert = Alert(
        rule_id="telemetry_stale", subject="INV-01", scope="inverter",
        severity="critical", message="INV-01 stopped reporting for 130s (threshold 120s)",
        value=130.0, threshold=120.0, fired_at=1_790_000_000.5, since=1_790_000_000.5,
    )
    await client.write_event(alert)

    assert len(seen) == 1
    request = seen[0]
    # The endpoint that actually exists in InfluxDB 3 Core 3.11.
    assert request.url.path == "/api/v3/write_lp"
    body = request.content.decode()
    assert body.startswith(
        "events,site=mojave,severity=critical,source=INV-01,rule=telemetry_stale "
    )
    assert 'code="telemetry_stale"' in body
    assert "value=130.0,threshold=120.0" in body
    # Nanosecond precision, as requested by precision=ns.
    assert body.endswith(" 1790000000500000000")


async def test_a_resolution_is_recorded_as_its_own_event():
    """A resolved alert must be queryable, not silently overwritten.

    Recorded at info severity with a _RESOLVED suffix so the record of how
    serious the fault had been survives.
    """
    import httpx

    from solar_api.alerts import Alert
    from solar_api.influx import InfluxClient

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(204)

    client = InfluxClient(
        Settings(INFLUX_API_TOKEN="test-token", INFLUX_DB="solar"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://influxdb:8181"
        ),
    )
    await client.write_event(Alert(
        rule_id="heatsink_high", subject="INV-02", scope="inverter", severity="warning",
        message="resolved", value=0.0, threshold=70.0,
        fired_at=1_790_000_000.0, since=1_790_000_000.0,
        resolved_at=1_790_000_060.0, is_resolution=True,
    ))
    assert "severity=info" in seen[0]
    assert 'code="heatsink_high_RESOLVED"' in seen[0]
    # Timestamped when the fault *ended*. Writing fired_at here put every
    # resolution at the same instant as its own firing, so the event history
    # claimed faults cleared the moment they started.
    assert seen[0].endswith(" 1790000060000000000"), seen[0]
    assert not seen[0].endswith(" 1790000000000000000")


async def test_a_write_failure_does_not_stop_alerting():
    """A failing database must not take the subscription down with it.

    Losing the historical record of one alert is bad; losing every future alert
    is far worse.
    """
    import httpx

    from solar_api.alerts import Alert, Condition, Rule
    from solar_api.influx import InfluxClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    influx = InfluxClient(
        Settings(INFLUX_API_TOKEN="test-token", INFLUX_DB="solar"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://influxdb:8181"
        ),
    )
    rule = Rule(
        id="inverter_fault", description="d", scope="inverter",
        severity="critical", debounce_s=0,
        conditions=(Condition("status_code", "==", 5.0),),
    )
    service = AlertService(RuleEngine([rule]), influx)
    await service._record(Alert(
        rule_id="inverter_fault", subject="INV-01", scope="inverter", severity="critical",
        message="m", value=5.0, threshold=5.0, fired_at=1.0, since=1.0,
    ))
    assert service.counters["errors"] == 1

    # Evaluation still works, which is the part that must not be lost.
    service._handle(
        "solar/mojave/block/BLK-A/inverter/INV-01/telemetry",
        json.dumps({"status_code": 5}).encode(),
    )
    assert len(service.active_alerts()) == 1


async def test_two_alerts_resolving_together_do_not_overwrite_each_other():
    """Regression: same-instant resolutions silently destroyed one another.

    A point's primary key is (measurement, tag set, timestamp). Two rules on the
    same inverter, resolving in the same evaluation, share site/severity/source
    *and* the nanosecond timestamp -- so without `rule` in the tag set the second
    write replaced the first and the event history lost one of them.

    This asserts the two writes differ in their tag set, which is what keeps them
    in separate series.
    """
    import httpx

    from solar_api.alerts import Alert
    from solar_api.influx import InfluxClient

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(204)

    client = InfluxClient(
        Settings(INFLUX_API_TOKEN="test-token", INFLUX_DB="solar"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://influxdb:8181"
        ),
    )
    resolved_at = 1_790_000_100.0
    for rule_id in ("heatsink_critical", "heatsink_high"):
        await client.write_event(Alert(
            rule_id=rule_id, subject="INV-03", scope="inverter", severity="critical",
            message="m", value=96.0, threshold=90.0,
            fired_at=1_790_000_000.0, since=1_790_000_000.0,
            resolved_at=resolved_at, is_resolution=True,
        ))

    assert len(seen) == 2
    stamps = {line.rsplit(" ", 1)[1] for line in seen}
    assert len(stamps) == 1, "the timestamps are meant to be identical"
    tag_sets = {line.split(" ")[0] for line in seen}
    assert len(tag_sets) == 2, "identical tag sets would overwrite each other"
    assert any("rule=heatsink_critical" in line for line in seen)
    assert any("rule=heatsink_high" in line for line in seen)
