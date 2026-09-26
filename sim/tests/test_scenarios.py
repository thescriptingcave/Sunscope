"""Tests for fault scenarios.

This module had **no tests at all**, which is how it shipped broken: the
`active_at` comparison raised ``TypeError`` on the first real use, and the demo
file pointed at a string id the topology has never generated. Both faults were
invisible until someone ran ``--scenarios`` and watched it publish zero ticks.

The tests below are mostly about the two ways this feature fails *quietly*: a
scenario that cannot be evaluated, and a scenario aimed at something that does
not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from solar_sim.scenarios import Scenario, load_scenarios, resolve
from solar_sim.topology import build_default_site

SITE_TZ = "America/Los_Angeles"
DEMO = Path(__file__).resolve().parents[1] / "config" / "scenarios" / "demo.yaml"


# --- the regression that mattered ------------------------------------------


def test_a_scenario_can_be_evaluated_against_an_aware_clock():
    """Regression: comparing a naive scenario time to an aware sim clock raised.

    `_parse_time` returned a naive datetime while the simulator's clock is aware
    UTC, so `active_at` raised `TypeError` and the run published zero ticks. The
    feature had simply never worked.
    """
    scenario = Scenario(
        name="s",
        start=datetime(2026, 9, 25, 11, 0),  # naive, as the YAML gives it
        duration=timedelta(minutes=45),
    )
    assert scenario.active_at(datetime(2026, 9, 25, 11, 30, tzinfo=UTC)) is True
    assert scenario.active_at(datetime(2026, 9, 25, 12, 0, tzinfo=UTC)) is False


def test_naive_scenario_times_are_read_as_site_local(tmp_path: Path):
    """A bare timestamp in the YAML is site-local, as the file header says.

    Goes through the loader, because that is where the interpretation happens:
    `Scenario` itself carries an already-resolved aware time and treats anything
    naive as UTC.
    """
    path = tmp_path / "s.yaml"
    path.write_text('scenarios:\n  - name: a\n    start: "2026-09-25T11:00:00"\n    duration: 2h\n')
    (scenario,) = load_scenarios(path, tz=SITE_TZ)
    # 11:00 PDT is 18:00 UTC.
    assert scenario.start == datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
    assert scenario.active_at(datetime(2026, 9, 25, 19, 0, tzinfo=UTC)) is True
    assert scenario.active_at(datetime(2026, 9, 25, 17, 0, tzinfo=UTC)) is False


def test_window_is_half_open():
    """Active at the start, inactive at the exact end.

    Otherwise two adjacent scenarios both claim the boundary instant.
    """
    scenario = Scenario(
        name="s", start=datetime(2026, 1, 1, tzinfo=UTC), duration=timedelta(minutes=30)
    )
    assert scenario.active_at(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) is True
    assert scenario.active_at(datetime(2026, 1, 1, 0, 30, tzinfo=UTC)) is False
    assert scenario.active_at(datetime(2025, 12, 31, 23, 59, tzinfo=UTC)) is False


# --- loading ----------------------------------------------------------------


def test_demo_file_uses_relative_times(tmp_path: Path):
    """An absolute date means the file stops working the next day.

    `demo.yaml` was pinned to 2026-09-25, so by 2026-09-26 every scenario was
    permanently in the past. That is the failure this guards against.
    """
    raw = yaml.safe_load(DEMO.read_text())
    for entry in raw["scenarios"]:
        assert "start_offset" in entry, (
            f"{entry['name']} uses an absolute start; it will stop firing once "
            "that date passes"
        )
        assert "start" not in entry


def test_relative_scenarios_anchor_to_the_simulation_start():
    anchor = datetime(2026, 9, 26, 19, 0, tzinfo=UTC)
    scenarios = load_scenarios(DEMO, tz=SITE_TZ, anchor=anchor)
    assert scenarios
    assert all(s.start >= anchor for s in scenarios), "offsets must not precede the start"
    first = scenarios[0]
    assert first.start == anchor, "the first scenario should begin immediately"


def test_relative_offset_requires_an_anchor(tmp_path: Path):
    path = tmp_path / "s.yaml"
    path.write_text('scenarios:\n  - name: a\n    start_offset: "0s"\n    duration: 5m\n')
    with pytest.raises(ValueError, match="no anchor"):
        load_scenarios(path, tz=SITE_TZ)


def test_absolute_times_still_work(tmp_path: Path):
    path = tmp_path / "s.yaml"
    path.write_text('scenarios:\n  - name: a\n    start: "2026-09-25T11:00:00"\n    duration: 5m\n')
    scenarios = load_scenarios(path, tz=SITE_TZ)
    assert scenarios[0].start == datetime(2026, 9, 25, 18, 0, tzinfo=UTC)  # 11:00 PDT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("45s", 45), ("30m", 1800), ("2h", 7200), ("1d", 86400),
        # Compound and signed forms: relative offsets are written this way.
        ("1h30m", 5400), ("+1h0m", 3600), ("-30m", -1800), ("0s", 0),
    ],
)
def test_duration_formats(text: str, expected: int):
    from solar_sim.scenarios import _parse_duration

    assert _parse_duration(text) == timedelta(seconds=expected)


@pytest.mark.parametrize("text", ["5x", "1h30", "", "abc", "10"])
def test_a_malformed_duration_is_rejected_rather_than_truncated(text: str):
    """Silence here would mean a scenario that never fires, with no error."""
    from solar_sim.scenarios import _parse_duration

    with pytest.raises(ValueError):
        _parse_duration(text)


# --- targets must exist -----------------------------------------------------


def test_every_demo_target_exists_in_the_topology():
    """A target that does not exist makes the scenario a silent no-op.

    `string_underperformance` aimed at `STR-07`, which the topology has never
    generated, so it did nothing at all and still looked configured.
    """
    site = build_default_site()
    known = (
        {s.string_id for s in site.pv_strings}
        | {i.inverter_id for i in site.inverters}
        | {site.weather_station_id}
    )
    raw = yaml.safe_load(DEMO.read_text())
    for entry in raw["scenarios"]:
        target = entry.get("target")
        if target is not None:
            assert target in known, (
                f"scenario {entry['name']!r} targets {target!r}, which is not in "
                f"the topology; known ids: {sorted(known)}"
            )


# --- resolve ----------------------------------------------------------------


def test_resolve_folds_active_scenarios():
    when = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    scenarios = [
        Scenario(
            name="offline", start=when - timedelta(minutes=5),
            duration=timedelta(hours=1), target="INV-03",
            effect={"inverter_offline": True},
        ),
        Scenario(
            name="future", start=when + timedelta(hours=5),
            duration=timedelta(hours=1), effect={"sustained_kt": 0.15},
        ),
    ]
    faults = resolve(scenarios, when)
    assert "INV-03" in faults.inverter_offline
    assert faults.kt_override is None, "an inactive scenario must not contribute"


def test_comms_loss_is_distinct_from_power_loss():
    """The whole point of the scenario set.

    Power loss trips the Last Will; comms loss keeps the session alive, so only
    a staleness rule can catch it. Conflating them would mean never exercising
    the rule that matters.
    """
    when = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    silent = resolve(
        [Scenario(
            name="comms", start=when, duration=timedelta(hours=1),
            target="INV-04", effect={"inverter_silent": True},
        )],
        when,
    )
    offline = resolve(
        [Scenario(
            name="power", start=when, duration=timedelta(hours=1),
            target="INV-04", effect={"inverter_offline": True},
        )],
        when,
    )
    assert silent.inverter_silent == {"INV-04"} and not silent.inverter_offline
    assert offline.inverter_offline == {"INV-04"} and not offline.inverter_silent
    # Both are "not reporting", which is why `is_silent` covers each.
    assert silent.is_silent("INV-04") and offline.is_silent("INV-04")


def test_a_degrading_string_is_scaled_not_removed():
    when = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    faults = resolve(
        [Scenario(
            name="weak", start=when, duration=timedelta(hours=1),
            target="STR-12", effect={"scale_dc_power": 0.55},
        )],
        when,
    )
    assert faults.string_scale == {"STR-12": 0.55}
