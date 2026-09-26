"""Scenarios: deliberate faults, so the alerting path is actually exercised.

A clean farm never triggers an alert, which means dashboards stay empty and the
detection logic is never tested. The scenarios below exist to make both real.

The one that matters most is **comms loss**. Power loss trips the MQTT Last
Will, so it is detected in seconds. A network partition does not: the session
stays alive, the device looks healthy, and the only evidence is a gap in the
time series. That is precisely the failure class a naive simulator can never
surface, and it is why the alerting design needs a staleness check alongside the
offline check.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yaml


@dataclass
class Scenario:
    """One fault, active over a time window."""

    name: str
    start: datetime
    duration: timedelta
    target: str | None = None
    effect: dict[str, Any] = field(default_factory=dict)

    def active_at(self, when: datetime) -> bool:
        """True when ``when`` falls inside this scenario's window.

        Both sides are normalised to aware UTC first. Comparing a naive and an
        aware datetime raises rather than returning a wrong answer, and a
        scenario that cannot be evaluated must not take the simulation down with
        it.
        """
        start = _as_utc(self.start)
        moment = _as_utc(when)
        return start <= moment < start + self.duration


@dataclass
class ActiveFaults:
    """The faults in force at one instant."""

    inverter_offline: set[str] = field(default_factory=set)
    #: Devices that keep their MQTT session but stop publishing. Not detectable
    #: by Last Will -- only by a staleness rule.
    inverter_silent: set[str] = field(default_factory=set)
    #: string_id -> remaining output scale in (0, 1].
    string_scale: dict[str, float] = field(default_factory=dict)
    #: string_id -> additive temperature bias in degC.
    string_temp_bias: dict[str, float] = field(default_factory=dict)
    #: Additive bias on the weather station's irradiance, as a fraction.
    ghi_bias: float = 0.0
    ghi_temp_bias: float = 0.0
    #: Sustained clearness override, for wildfire / smoke haze.
    kt_override: float | None = None

    def is_silent(self, inverter_id: str) -> bool:
        return inverter_id in self.inverter_offline or inverter_id in self.inverter_silent


def load_scenarios(path: str, tz: str = "UTC", anchor: datetime | None = None) -> list[Scenario]:
    """Read scenario definitions from YAML.

    Two time forms are accepted, and the distinction matters:

    * ``start: "2026-09-25T11:00:00"`` -- an absolute local wall-clock time,
      localised to ``tz``.
    * ``start_offset: "0s"`` -- relative to ``anchor`` (the simulation start).

    Relative is almost always what you want. An absolute date means the file
    silently stops producing faults the day after it was written, which is
    exactly what happened to ``config/scenarios/demo.yaml``.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    out: list[Scenario] = []
    for entry in raw.get("scenarios", []):
        duration = entry.get("duration", "0s")
        if "start_offset" in entry:
            if anchor is None:
                raise ValueError(
                    f"scenario {entry['name']!r} uses start_offset but no anchor "
                    "was given; pass the simulation start time"
                )
            start = anchor + _parse_duration(entry["start_offset"])
        else:
            start = _parse_time(entry["start"], tz)
        out.append(
            Scenario(
                name=entry["name"],
                start=start,
                duration=_parse_duration(duration),
                target=entry.get("target"),
                effect=entry.get("effect", {}),
            )
        )
    return out


def _parse_time(value: str, tz: str = "UTC") -> datetime:
    """Parse a scenario start time, always returning an aware datetime.

    The simulator's clock is UTC and aware, so a naive scenario time here made
    ``active_at`` raise ``TypeError: can't compare offset-naive and offset-aware
    datetimes`` -- which meant the whole scenario feature crashed on first use.
    Naive values are site-local, as the file header documents.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(ZoneInfo("UTC"))
    return parsed.replace(tzinfo=ZoneInfo(tz))


def _parse_duration(value: str) -> timedelta:
    """Parse a duration: ``45s``, ``30m``, ``2h``, ``1d``, or compound ``1h30m``.

    Compound forms are needed because relative offsets are naturally written
    that way (``start_offset: "+1h0m"``), and a leading sign is allowed.
    """
    text = value.strip()
    # The sign is significant: `start_offset: "-30m"` starts a scenario before
    # the simulation begins. Stripping it without reapplying it silently turned
    # that into +30m.
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    if not text:
        raise ValueError(f"empty duration: {value!r}")

    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0.0
    matched = 0
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd])", text):
        total += float(number) * factors[unit]
        matched += len(number) + 1
    # Every character must have been consumed, so "5x" or "1h30" is an error
    # rather than a silently-truncated duration.
    if not matched or matched != len(text):
        raise ValueError(f"cannot parse duration {value!r}")
    return timedelta(seconds=sign * total)


def _as_utc(value: datetime) -> datetime:
    """Coerce to an aware UTC datetime, treating naive values as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("UTC"))


def resolve(scenarios: list[Scenario], when: datetime) -> ActiveFaults:
    """Fold every scenario active at ``when`` into one fault set.

    Later scenarios win on conflicting scalar effects, which keeps precedence
    obvious when reading a scenario file.
    """
    faults = ActiveFaults()
    for scenario in scenarios:
        if not scenario.active_at(when):
            continue
        effect = scenario.effect
        target = scenario.target
        match effect:
            case {"inverter_offline": True}:
                if target:
                    faults.inverter_offline.add(target)
            case {"inverter_silent": True}:
                if target:
                    faults.inverter_silent.add(target)
            case {"scale_dc_power": float() as scale}:
                if target:
                    faults.string_scale[target] = scale
            case {"module_temp_bias_c": float() as bias}:
                if target:
                    faults.string_temp_bias[target] = bias
            case {"ghi_bias": float() as bias, "air_temp_bias_c": float() as temp_bias}:
                faults.ghi_bias = bias
                faults.ghi_temp_bias = temp_bias
            case {"sustained_kt": float() as kt}:
                faults.kt_override = kt
    return faults


def iter_faults(
    scenarios: list[Scenario], times
) -> Iterator[tuple[datetime, ActiveFaults]]:
    """Yield the fault set for each timestamp, for batch computation."""
    for when in times:
        yield when, resolve(scenarios, when)
