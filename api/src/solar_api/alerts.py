"""Threshold and staleness alert rules, and the engine that evaluates them.

This module is deliberately free of I/O. It takes readings and a clock, and
returns alerts. Everything that touches MQTT or InfluxDB lives in
``alert_service.py``. That split is what makes the interesting part -- the
debounce and hysteresis state machine -- testable without a broker, and it is
the part that is genuinely easy to get subtly wrong.

The three behaviours that matter, and why each is here:

**Debounce.** A cloud passing over the array drops POA hard for one to four
minutes. A rule that fires the instant a threshold is crossed turns every
passing cloud into an alert, and an operator who is trained to ignore alerts
stops reading them. So a condition must hold continuously for ``debounce_s``
before it becomes an alert. Pending time is tracked, not assumed.

**Hysteresis / re-arm.** A condition that flaps across its threshold would
otherwise emit a stream of alerts. An alert fires once, on the transition into
the firing state, and stays firing until the condition clears -- at which point
it emits a single ``resolved`` event and re-arms. It does not re-fire while the
condition persists, and it does not need the condition to be false for any
longer than a single evaluation.

**Staleness.** A network partition does not trip the MQTT Last Will, because
the device keeps its session and stays "connected" while publishing nothing.
There is no status message to react to; the only evidence is the *absence* of
messages. This is why the engine has an explicit staleness rule type and a
``tick()`` that advances on a timer: a rule that only reacts to data it
receives can never detect the failure where the data stops.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

#: Severities, matching ``solar_sim.metrics.SEVERITIES``. Kept as a tag column,
#: so the set is small on purpose: every extra value multiplies series count.
SEVERITIES: Final = ("info", "warning", "critical")

#: Display priority, most severe first. Separate from SEVERITIES because that
#: tuple is ordered ascending for the tag column, and reusing it for sorting put
#: warnings above criticals in the feed -- the opposite of what an operator
#: scanning a list needs.
SEVERITY_PRIORITY: Final = {"critical": 0, "warning": 1, "info": 2}

#: Comparison operators available in rule definitions.
OPERATORS: Final = (">", ">=", "<", "<=", "==", "!=")

#: Rule scopes. `inverter` rules are keyed per device, `site` rules once.
SCOPES: Final = ("inverter", "site")

#: Engine states for a single (rule, subject) pair.
OK: Final = "ok"
PENDING: Final = "pending"
FIRING: Final = "firing"


class RuleConfigError(ValueError):
    """A rule definition that cannot be evaluated as written."""


@dataclass(frozen=True)
class Condition:
    """One ``metric <operator> threshold`` test.

    Conditions are combined with AND: every one must hold for the rule to
    match. That is all this needs -- OR across metrics is better expressed as
    two rules, which keeps each rule independently readable and testable.
    """

    metric: str
    operator: str
    threshold: float

    def evaluate(self, payload: Mapping[str, Any]) -> bool:
        raw = payload.get(self.metric)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            # A missing or non-numeric field is not a pass and not a failure. It
            # is unknown, and treating unknown as "condition met" would alert on
            # every payload that omits an optional field.
            return False
        value = float(raw)
        threshold = self.threshold
        match self.operator:
            case ">":
                return value > threshold
            case ">=":
                return value >= threshold
            case "<":
                return value < threshold
            case "<=":
                return value <= threshold
            case "==":
                return value == threshold
            case "!=":
                return value != threshold
        raise RuleConfigError(f"unknown operator {self.operator!r}")


@dataclass(frozen=True)
class Rule:
    """A single alert rule.

    ``kind="threshold"`` compares metrics in a payload. ``kind="staleness"``
    fires when a subject stops publishing, and is evaluated by ``tick()``
    against elapsed wall time rather than by any incoming payload.
    """

    id: str
    description: str
    scope: str
    severity: str
    debounce_s: float = 60.0
    conditions: tuple[Condition, ...] = ()
    kind: str = "threshold"
    stale_after_s: float = 0.0
    #: Seconds after which a subject with no data is dropped entirely. Without
    #: this the engine grows without bound as devices come and go.
    forget_after_s: float = 3600.0

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise RuleConfigError(f"rule {self.id!r}: unknown scope {self.scope!r}")
        if self.severity not in SEVERITIES:
            raise RuleConfigError(f"rule {self.id!r}: unknown severity {self.severity!r}")
        if self.kind not in ("threshold", "staleness"):
            raise RuleConfigError(f"rule {self.id!r}: unknown kind {self.kind!r}")
        if self.kind == "threshold" and not self.conditions:
            raise RuleConfigError(
                f"rule {self.id!r}: a threshold rule needs at least one condition"
            )
        if self.kind == "staleness" and self.stale_after_s <= 0:
            raise RuleConfigError(
                f"rule {self.id!r}: a staleness rule needs a positive stale_after_s"
            )

    def matches(self, payload: Mapping[str, Any]) -> bool:
        return all(condition.evaluate(payload) for condition in self.conditions)


@dataclass
class Alert:
    """An alert instance, active or freshly resolved.

    ``fired_at`` and ``resolved_at`` are epoch seconds so they can be written
    straight to InfluxDB and rendered without further conversion.
    """

    rule_id: str
    subject: str
    scope: str
    severity: str
    message: str
    value: float | None
    threshold: float | None
    fired_at: float
    since: float
    resolved_at: float | None = None
    #: True for the single event emitted when an alert clears.
    is_resolution: bool = False

    @property
    def key(self) -> str:
        return f"{self.rule_id}:{self.subject}"

    @property
    def active(self) -> bool:
        return self.resolved_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "subject": self.subject,
            "scope": self.scope,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "fired_at": self.fired_at,
            "since": self.since,
            "resolved_at": self.resolved_at,
            "active": self.active,
        }


@dataclass
class _State:
    """Engine bookkeeping for one (rule, subject) pair."""

    state: str = OK
    #: When the condition first became true, for debounce accounting.
    since: float = 0.0
    #: When this subject last published anything, for staleness.
    last_seen: float = 0.0
    #: The value that triggered the rule, reported with the alert.
    value: float | None = None
    alert: Alert | None = None


@dataclass
class _Subject:
    """Everything the engine knows about one device or the site."""

    scope: str
    last_seen: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    states: dict[str, _State] = field(default_factory=dict)


class RuleEngine:
    """Evaluates rules against incoming readings.

    The clock is injectable so debounce and staleness can be tested by
    advancing time directly, instead of sleeping through real intervals.
    """

    def __init__(
        self,
        rules: Iterable[Rule],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._rules: tuple[Rule, ...] = tuple(rules)
        seen: set[str] = set()
        for rule in self._rules:
            if rule.id in seen:
                raise RuleConfigError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        self._subjects: dict[str, _Subject] = {}

    # -- introspection -------------------------------------------------------

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def active_alerts(self) -> list[Alert]:
        """Currently firing alerts, most severe first, then oldest first."""
        alerts = [
            state.alert
            for subject in self._subjects.values()
            for state in subject.states.values()
            if state.alert is not None and state.alert.active
        ]
        alerts.sort(key=lambda a: (SEVERITY_PRIORITY.get(a.severity, 99), a.fired_at))
        return alerts

    def state_of(self, rule_id: str, subject: str) -> str:
        entry = self._subjects.get(subject)
        if entry is None:
            return OK
        state = entry.states.get(rule_id)
        return state.state if state else OK

    # -- ingestion -----------------------------------------------------------

    def observe(self, subject: str, scope: str, payload: Mapping[str, Any]) -> list[Alert]:
        """Feed one reading. Returns alerts that started or resolved here.

        ``subject`` is the inverter id, or the site name for site-scoped rules.
        """
        now = self._clock()
        entry = self._subjects.get(subject)
        if entry is None:
            entry = self._subjects[subject] = _Subject(scope=scope)
        entry.last_seen = now
        entry.payload = dict(payload)

        fired: list[Alert] = []

        # A message from this subject is proof it is alive, so any staleness
        # alert resolves here rather than waiting for the next tick(). Waiting
        # would leave a device looking broken in the feed for a few seconds
        # after it demonstrably came back.
        for rule in self._rules:
            if rule.scope != scope or rule.kind != "staleness":
                continue
            state = entry.states.get(rule.id)
            if state is not None and state.state == FIRING and state.alert is not None:
                fired.append(self._build_resolution(state.alert, now))
                state.alert = None
                state.state = OK

        for rule in self._rules:
            if rule.scope != scope or rule.kind != "threshold":
                continue
            state = entry.states.setdefault(rule.id, _State())
            state.last_seen = now
            met = rule.matches(payload)
            value = self._trigger_value(rule, payload)

            if met:
                state.value = value
                if state.state == OK:
                    state.state = PENDING
                    state.since = now
                if state.state == PENDING and now - state.since >= rule.debounce_s:
                    state.state = FIRING
                    state.alert = self._build(rule, subject, scope, value, now)
                    fired.append(state.alert)
            elif state.state == FIRING:
                # The condition cleared: emit exactly one resolution and re-arm.
                if state.alert is not None:
                    resolution = self._build_resolution(state.alert, now)
                    state.alert = None
                    fired.append(resolution)
                state.state = OK
                state.since = 0.0
            elif state.state == PENDING:
                # Cleared during the debounce window, so it never alerted at all.
                state.state = OK
                state.since = 0.0
        return fired

    def tick(self) -> list[Alert]:
        """Advance time-based rules. Call on a timer, not per message.

        This is the only way a staleness rule can fire: if nothing is being
        published there are no messages to react to, so the engine has to be
        woken up independently.
        """
        now = self._clock()
        fired: list[Alert] = []
        for subject, entry in list(self._subjects.items()):
            age = now - entry.last_seen

            # Promote any pending threshold rule that has now held long enough.
            # Doing this here as well as in observe() makes the debounce purely
            # time-based, so it does not depend on the publish cadence lining up
            # with the debounce window. Without it, a rule could be starved by a
            # device that publishes less often than the debounce is long.
            for rule in self._rules:
                if rule.scope != entry.scope or rule.kind != "threshold":
                    continue
                state = entry.states.get(rule.id)
                if state is None or state.state != PENDING:
                    continue
                if now - state.since < rule.debounce_s or not rule.matches(entry.payload):
                    continue
                state.state = FIRING
                state.alert = self._build(rule, subject, entry.scope, state.value, now)
                fired.append(state.alert)

            for rule in self._rules:
                if rule.scope != entry.scope or rule.kind != "staleness":
                    continue
                state = entry.states.setdefault(rule.id, _State())
                if age >= rule.stale_after_s:
                    if state.state != FIRING:
                        state.state = FIRING
                        state.since = now
                        state.alert = Alert(
                            rule_id=rule.id,
                            subject=subject,
                            scope=entry.scope,
                            severity=rule.severity,
                            message=(
                                f"{subject} stopped reporting for {age:.0f}s "
                                f"(threshold {rule.stale_after_s:.0f}s)"
                            ),
                            # The value is the age, because that is the number an
                            # operator needs to judge severity.
                            value=round(age, 1),
                            threshold=rule.stale_after_s,
                            fired_at=now,
                            since=now,
                        )
                        fired.append(state.alert)
                elif state.state == FIRING:
                    if state.alert is not None:
                        fired.append(self._build_resolution(state.alert, now))
                    state.alert = None
                    state.state = OK

            # Forget subjects that have been gone long enough, so a simulator
            # that is stopped and restarted many times cannot grow the engine
            # without bound. The threshold is the longest any applicable rule
            # would still care about, plus a margin, so nothing is dropped while
            # a rule could still legitimately fire.
            forget_after = max(
                (r.forget_after_s for r in self._rules if r.scope == entry.scope),
                default=0.0,
            )
            if forget_after and age > forget_after:
                del self._subjects[subject]
        return fired

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _trigger_value(rule: Rule, payload: Mapping[str, Any]) -> float | None:
        """The metric value that decided the match, for reporting.

        The *first* matching condition is used, since with AND semantics the
        first is the one whose threshold is most naturally quoted back.
        """
        for condition in rule.conditions:
            if condition.evaluate(payload):
                raw = payload.get(condition.metric)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    return float(raw)
        return None

    @staticmethod
    def _build(rule: Rule, subject: str, scope: str, value: float | None, now: float) -> Alert:
        return Alert(
            rule_id=rule.id,
            subject=subject,
            scope=scope,
            severity=rule.severity,
            message=f"{subject}: {rule.description}",
            value=value,
            threshold=rule.conditions[0].threshold if rule.conditions else None,
            fired_at=now,
            since=now,
        )

    @staticmethod
    def _build_resolution(alert: Alert, now: float) -> Alert:
        return Alert(
            rule_id=alert.rule_id,
            subject=alert.subject,
            scope=alert.scope,
            severity=alert.severity,
            message=f"{alert.subject}: resolved -- {alert.message.split(': ', 1)[-1]}",
            value=alert.value,
            threshold=alert.threshold,
            fired_at=alert.fired_at,
            since=alert.since,
            resolved_at=now,
            is_resolution=True,
        )
