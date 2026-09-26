"""Tests for the alert rule engine.

The state machine is the part of this system most likely to be subtly wrong, and
a subtle bug here is invisible in normal operation: a rule that never fires, a
rule that fires once per message, or an alert that never clears all look like
"no alerts" on a healthy farm. So the transitions are driven explicitly with a
fake clock rather than left to timing.
"""

from __future__ import annotations

import pytest

from solar_api.alerts import Condition, Rule, RuleConfigError, RuleEngine


class Clock:
    """A manually advanced clock, so debounce needs no real waiting."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def heating(deg: float, **extra) -> dict:
    return {
        "ac_power_w": 200_000.0,
        "dc_power_w": 240_000.0,
        "efficiency": 0.83,
        "heatsink_temp_c": deg,
        "internal_temp_c": deg + 5,
        "status_code": 3,
        "clipping": False,
        "uptime_s": 1000.0,
        "ghi": 800.0,
        **extra,
    }


def heatsink_rule(debounce_s: float = 90.0, threshold: float = 70.0) -> Rule:
    return Rule(
        id="heatsink_high",
        description="heatsink above the derating onset",
        scope="inverter",
        severity="warning",
        debounce_s=debounce_s,
        conditions=(Condition("heatsink_temp_c", ">", threshold),),
    )


# --- configuration validation ------------------------------------------------


def test_threshold_rule_requires_a_condition():
    with pytest.raises(RuleConfigError, match="at least one condition"):
        Rule(id="r", description="d", scope="inverter", severity="warning")


def test_unknown_severity_is_rejected():
    with pytest.raises(RuleConfigError, match="unknown severity"):
        Rule(
            id="r",
            description="d",
            scope="inverter",
            severity="catastrophic",
            conditions=(Condition("x", ">", 1),),
        )


def test_staleness_rule_requires_a_positive_threshold():
    with pytest.raises(RuleConfigError, match="positive stale_after_s"):
        Rule(id="r", description="d", scope="inverter", severity="critical", kind="staleness")


def test_duplicate_rule_ids_are_rejected():
    with pytest.raises(RuleConfigError, match="duplicate rule id"):
        RuleEngine([heatsink_rule(), heatsink_rule()])


# --- condition evaluation ---------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "threshold", "value", "expected"),
    [
        (">", 70.0, 71.0, True),
        (">", 70.0, 70.0, False),
        (">=", 70.0, 70.0, True),
        ("<", 0.75, 0.74, True),
        ("<", 0.75, 0.75, False),
        ("<=", 0.75, 0.75, True),
        ("==", 3.0, 3.0, True),
        ("!=", 3.0, 3.0, False),
    ],
)
def test_operators(operator: str, threshold: float, value: float, expected: bool):
    condition = Condition("m", operator, threshold)
    assert condition.evaluate({"m": value}) is expected


def test_missing_field_is_unknown_not_false_positive():
    """A field absent from the payload must not be treated as a match.

    InfluxDB fields are optional and json_v2 drops points missing a required
    one, so partial payloads are normal. Treating a missing field as "condition
    met" would alert on every partial message.
    """
    assert Condition("heatsink_temp_c", ">", 0.0).evaluate({}) is False
    assert Condition("heatsink_temp_c", ">", 0.0).evaluate({"heatsink_temp_c": None}) is False
    # A bool is an int in Python; `clipping: true` must not read as 1 > 0.
    assert Condition("clipping", ">", 0.0).evaluate({"clipping": True}) is False
    assert Condition("clipping", ">", 0.0).evaluate({"clipping": "yes"}) is False


# --- debounce ---------------------------------------------------------------


def test_condition_must_persist_for_the_debounce_window():
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=90)], clock=clock)

    # Hot, but not yet for long enough.
    assert engine.observe("INV-01", "inverter", heating(75)) == []
    assert engine.state_of("heatsink_high", "INV-01") == "pending"

    clock.advance(89)
    assert engine.observe("INV-01", "inverter", heating(75)) == []
    assert engine.state_of("heatsink_high", "INV-01") == "pending"

    clock.advance(2)
    fired = engine.observe("INV-01", "inverter", heating(75))
    assert len(fired) == 1
    alert = fired[0]
    assert alert.rule_id == "heatsink_high"
    assert alert.subject == "INV-01"
    assert alert.severity == "warning"
    assert alert.value == 75.0
    assert alert.threshold == 70.0
    assert alert.active and not alert.is_resolution


def test_a_transient_spike_never_alerts():
    """The cloud-ramp case: brief threshold crossing, no alert.

    This is the whole reason debouncing exists. Without it a 1-4 minute cloud
    ramp produces an alert, and an operator learns to ignore the feed.
    """
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=90)], clock=clock)

    for _ in range(10):
        assert engine.observe("INV-01", "inverter", heating(75)) == []
        clock.advance(20)
        assert engine.observe("INV-01", "inverter", heating(60)) == []
        clock.advance(10)
    assert engine.active_alerts() == []
    assert engine.state_of("heatsink_high", "INV-01") == "ok"


def test_heating_past_the_window_alerts_once_not_per_message():
    """A sustained fault must not emit an alert on every subsequent message."""
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=90)], clock=clock)

    engine.observe("INV-01", "inverter", heating(75))
    clock.advance(100)
    assert len(engine.observe("INV-01", "inverter", heating(75))) == 1

    for _ in range(50):
        clock.advance(30)
        assert engine.observe("INV-01", "inverter", heating(78)) == []
    assert len(engine.active_alerts()) == 1


# --- resolution and re-arming ----------------------------------------------


def test_alert_resolves_once_when_the_condition_clears():
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=60)], clock=clock)

    engine.observe("INV-01", "inverter", heating(75))
    clock.advance(70)
    engine.observe("INV-01", "inverter", heating(75))

    fired = engine.observe("INV-01", "inverter", heating(60))
    assert len(fired) == 1
    resolution = fired[0]
    assert resolution.is_resolution
    assert not resolution.active
    assert resolution.resolved_at == clock.now
    assert engine.active_alerts() == []
    assert engine.state_of("heatsink_high", "INV-01") == "ok"


def test_flapping_does_not_produce_an_alert_storm():
    """A condition that crosses back and forth must not emit an alert per crossing.

    The invariant is that the debounce window is measured across *consecutive*
    satisfied evaluations, so a spell shorter than the window never alerts at
    all no matter how often it recurs. Using a 30 s publish cadence and a 90 s
    debounce, every hot spell here is 30 s and must be swallowed.
    """
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=90)], clock=clock)

    for _ in range(20):
        clock.advance(30)
        engine.observe("INV-01", "inverter", heating(75))
        clock.advance(30)
        engine.observe("INV-01", "inverter", heating(60))
    assert engine.active_alerts() == [], "a 30 s hot spell must not satisfy a 90 s debounce"


def test_debounce_survives_a_slow_publisher():
    """Debounce must be time-based, not message-count-based.

    A device publishing every 5 minutes against a 3 minute debounce would never
    emit if promotion only happened on message arrival. The tick() path exists so
    that a pending rule still fires once enough wall time has passed.
    """
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=180)], clock=clock)

    engine.observe("INV-01", "inverter", heating(75))
    assert engine.state_of("heatsink_high", "INV-01") == "pending"

    clock.advance(200)
    # No further message from the device at all; only the timer runs.
    fired = engine.tick()
    assert len(fired) == 1
    assert fired[0].rule_id == "heatsink_high"


def test_a_sustained_fault_emits_exactly_one_alert_and_one_resolution():
    """The count of events must track the number of episodes, not the messages."""
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=30)], clock=clock)

    for _ in range(10):  # sustained fault, 30 s cadence
        clock.advance(30)
        engine.observe("INV-01", "inverter", heating(75))
    assert len(engine.active_alerts()) == 1

    for _ in range(10):  # recovered
        clock.advance(30)
        engine.observe("INV-01", "inverter", heating(60))
    assert engine.active_alerts() == []


# --- gating -----------------------------------------------------------------


def test_gating_conditions_prevent_nonsense_alerts():
    """An inverter at 0 % efficiency at night is correct, not faulty.

    Gating is what stops the farm alarming itself at every dusk.
    """
    rule = Rule(
        id="efficiency_low",
        description="efficiency collapsed",
        scope="inverter",
        severity="warning",
        debounce_s=0,
        conditions=(
            Condition("efficiency", "<", 0.75),
            Condition("ac_power_w", ">", 12_500.0),
            Condition("status_code", "==", 3.0),
        ),
    )
    clock = Clock()
    engine = RuleEngine([rule], clock=clock)

    # Night: no output, so the gate holds even though efficiency is zero.
    engine.observe("INV-01", "inverter", {"efficiency": 0.0, "ac_power_w": 0.0, "status_code": 1})
    assert engine.active_alerts() == []

    # Producing at full clip, still fine.
    engine.observe(
        "INV-01", "inverter", {"efficiency": 0.96, "ac_power_w": 200_000.0, "status_code": 3}
    )
    assert engine.active_alerts() == []

    # Producing, but the converter is failing.
    engine.observe(
        "INV-01", "inverter", {"efficiency": 0.60, "ac_power_w": 200_000.0, "status_code": 3}
    )
    assert len(engine.active_alerts()) == 1


def test_zero_output_in_sunlight_is_caught_but_zero_output_at_night_is_not():
    """The failure a status code cannot express.

    The inverter is online, publishing, reporting status 3, and producing
    nothing. Only cross-referencing the weather station reveals it.
    """
    rule = Rule(
        id="power_zero_in_sunlight",
        description="zero output in sunlight",
        scope="inverter",
        severity="critical",
        debounce_s=0,
        conditions=(Condition("ac_power_w", "<", 1000.0), Condition("ghi", ">", 200.0)),
    )
    clock = Clock()
    engine = RuleEngine([rule], clock=clock)

    engine.observe("INV-01", "inverter", {"ac_power_w": 0.0, "ghi": 5.0, "status_code": 1})
    assert engine.active_alerts() == [], "must not alert at night"

    engine.observe("INV-01", "inverter", {"ac_power_w": 0.0, "ghi": 800.0, "status_code": 3})
    assert len(engine.active_alerts()) == 1, "zero output under 800 W/m2 is a real fault"


# --- staleness --------------------------------------------------------------


def staleness_rule(stale_after_s: float = 120.0) -> Rule:
    return Rule(
        id="telemetry_stale",
        description="no telemetry received",
        kind="staleness",
        scope="inverter",
        severity="critical",
        stale_after_s=stale_after_s,
        forget_after_s=600.0,
    )


def test_staleness_fires_without_any_incoming_message():
    """The comms-loss case: the session is alive, so no status message arrives.

    This is the whole reason staleness is a separate rule kind evaluated on a
    timer. A rule that only reacts to data it receives can never detect the
    failure where the data stops -- which is the failure that costs the most,
    because nothing anywhere reports an error.
    """
    clock = Clock()
    engine = RuleEngine([staleness_rule(120)], clock=clock)

    engine.observe("INV-01", "inverter", heating(60))
    assert engine.active_alerts() == []

    clock.advance(119)
    assert engine.tick() == []
    assert engine.active_alerts() == []

    # No observe() call: the device is silent, which is the point.
    clock.advance(2)
    fired = engine.tick()
    assert len(fired) == 1
    alert = fired[0]
    assert alert.rule_id == "telemetry_stale"
    assert alert.severity == "critical"
    # The reported value is the age, because that is what an operator needs.
    assert alert.value == pytest.approx(121.0, abs=1.0)
    assert alert.threshold == 120.0


def test_staleness_fires_only_once_and_resolves_on_return():
    clock = Clock()
    engine = RuleEngine([staleness_rule(120)], clock=clock)

    engine.observe("INV-01", "inverter", heating(60))
    clock.advance(200)
    assert len(engine.tick()) == 1
    # Still silent, so no re-fire -- but stay well inside forget_after_s (600s),
    # since past that the subject is dropped and the alert goes with it.
    for _ in range(5):
        clock.advance(30)
        assert engine.tick() == [], "must not re-fire while still silent"
    assert len(engine.active_alerts()) == 1

    # Data returns: the alert clears.
    engine.observe("INV-01", "inverter", heating(60))
    assert engine.active_alerts() == []


def test_a_silent_device_does_not_fire_forever_after_the_simulator_stops():
    """Memory must stay bounded across repeated restarts.

    `forget_after_s` drops subjects that have been gone long enough, so a
    simulator started and stopped many times cannot grow the engine forever.
    """
    clock = Clock()
    engine = RuleEngine([staleness_rule(120)], clock=clock)
    engine.observe("INV-01", "inverter", heating(60))
    clock.advance(200)
    engine.tick()
    assert len(engine.active_alerts()) == 1

    clock.advance(1000)
    engine.tick()
    assert engine.active_alerts() == [], "subject should be forgotten, not alerting forever"
    assert engine.state_of("telemetry_stale", "INV-01") == "ok"


# --- multiple subjects and scopes ------------------------------------------


def test_alerts_are_tracked_per_device():
    clock = Clock()
    engine = RuleEngine([heatsink_rule(debounce_s=30)], clock=clock)

    engine.observe("INV-01", "inverter", heating(75))
    engine.observe("INV-02", "inverter", heating(60))
    clock.advance(40)
    assert len(engine.observe("INV-01", "inverter", heating(75))) == 1
    assert engine.observe("INV-02", "inverter", heating(60)) == []
    assert [a.subject for a in engine.active_alerts()] == ["INV-01"]


def test_site_rules_do_not_evaluate_against_inverters():
    rule = Rule(
        id="pr_low",
        description="performance ratio low",
        scope="site",
        severity="warning",
        debounce_s=0,
        conditions=(Condition("pr_ratio", "<", 0.7),),
    )
    clock = Clock()
    engine = RuleEngine([rule], clock=clock)
    # An inverter payload with a low pr_ratio must not trigger a site rule.
    engine.observe("INV-01", "inverter", {"pr_ratio": 0.3})
    assert engine.active_alerts() == []
    engine.observe("mojave", "site", {"pr_ratio": 0.3})
    assert len(engine.active_alerts()) == 1


def test_active_alerts_are_ordered_most_severe_first():
    rules = [
        heatsink_rule(debounce_s=0),
        Rule(
            id="telemetry_stale",
            description="d",
            kind="staleness",
            scope="inverter",
            severity="critical",
            stale_after_s=1.0,
            forget_after_s=100.0,
        ),
    ]
    clock = Clock()
    engine = RuleEngine(rules, clock=clock)
    engine.observe("INV-01", "inverter", heating(75))
    clock.advance(5)
    engine.tick()
    assert [a.severity for a in engine.active_alerts()] == ["critical", "warning"]
