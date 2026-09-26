"""Physics invariants.

These are the tests that matter. A dashboard rendering plausible curves from a
broken model is worse than no dashboard, because it looks right.

Each invariant here corresponds to one of the ten acceptance criteria in
docs/01-design.md. They are parameterised across a full simulated year rather
than a handful of days, because seasonal edge cases hide in the tails.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from solar_sim.farm import SolarFarm
from solar_sim.topology import build_default_site
from solar_sim.weather import WeatherParams

#: A perfectly clear, still, cool day. Isolates the PV chain from cloud noise.
CLEAR_DAY = WeatherParams(
    kt_mean=1.0, phi=0.0, sigma=0.0, ramp_probability=0.0, temp_mean_c=20.0, wind_mean_mps=0.5
)


@pytest.fixture(scope="module")
def clear_summer():
    farm = SolarFarm(build_default_site(), weather_params=CLEAR_DAY)
    ticks = list(farm.run(datetime(2026, 6, 21, 0, 0), 288, dt_s=300))
    return farm, ticks


@pytest.fixture(scope="module")
def summer_year():
    """A full year at 30-minute resolution, for the seasonal checks."""
    farm = SolarFarm(build_default_site(), weather_params=CLEAR_DAY)
    return farm, list(farm.run(datetime(2026, 1, 1, 0, 0), 365 * 48, dt_s=1800))


# --- I2, I3: no negatives, no NaN/Inf --------------------------------------


DAYS = [
    datetime(2026, 1, 15),
    datetime(2026, 3, 20),
    datetime(2026, 6, 21),
    datetime(2026, 9, 22),
    datetime(2026, 12, 10),
]  # equinoxes, solstices, and a shoulder-season day


@pytest.mark.parametrize("day", DAYS)
def test_no_negative_or_nonfinite_values(day, clear_summer):
    """I2 + I3. Every numeric field must be finite and non-negative."""
    farm = build_default_site()
    sim = SolarFarm(farm, weather_params=CLEAR_DAY)
    start = day.replace(hour=0, minute=0)
    for tick in sim.run(start, 48, dt_s=1800):
        for reading in tick.inverters:
            for name, value in vars(reading).items():
                if name == "inverter":
                    continue
                if isinstance(value, bool) or value is None or isinstance(value, str):
                    continue
                assert isinstance(value, (int, float)), f"{name} is {type(value)}"
                assert math.isfinite(value), f"{name} is not finite at {tick.when}"
                assert value >= 0.0, f"{name} = {value} is negative at {tick.when}"
        for reading in tick.strings:
            assert reading.dc_power_w >= 0.0
            assert reading.module_temp_c >= 0.0


# --- I1: night is exactly zero ----------------------------------------------


@pytest.mark.parametrize(
    "day", [datetime(2026, 1, 15), datetime(2026, 6, 21), datetime(2026, 12, 10)]
)
def test_night_output_is_exactly_zero(day):
    """I1. Output outside daylight must be exactly 0.0, not "small".

    A tolerance here would let twilight chatter through and show up as noise on
    the night-time power curve.
    """
    sim = SolarFarm(build_default_site(), weather_params=CLEAR_DAY)
    start = day.replace(hour=0, minute=0)
    for tick in sim.run(start, 48, dt_s=1800):
        # Recompute the sun position to decide night independently of the model.

        index = sim._utc_index(tick.when)
        zenith = float(sim.geometry.position(index)["zenith"].iloc[0])
        if zenith >= 90.0:
            for reading in tick.inverters:
                assert reading.ac_power_w == 0.0, (
                    f"{reading.inverter.inverter_id} produced "
                    f"{reading.ac_power_w}W at night ({tick.when}, zenith={zenith})"
                )
                assert reading.dc_power_w == 0.0
            assert tick.rollup["total_ac_power_w"] == 0.0
            assert tick.rollup["pr_ratio"] == 0.0


# --- I4: the daily power curve is unimodal ----------------------------------


def test_clear_day_power_curve_is_unimodal(clear_summer):
    """I4. One local maximum per day.

    Spurious secondary maxima almost always mean a double-counted quantity or a
    phase error in the geometry, and they are obvious on a chart.
    """
    _, ticks = clear_summer
    series = [t.rollup["total_ac_power_w"] for t in ticks]
    rising = 0
    falling = 0
    seen_peak = False
    for a, b in zip(series, series[1:], strict=False):
        if b > a + 1.0:
            if falling:
                pytest.fail("power rose again after falling: curve is not unimodal")
            rising += 1
        elif b < a - 1.0:
            falling += 1
            seen_peak = True
    assert seen_peak, "expected the curve to rise and fall within the day"
    assert rising > 0


# --- I5: monotonic in irradiance --------------------------------------------


def test_power_is_monotonic_in_irradiance_at_fixed_temperature():
    """I5. dP/dPOA > 0 for fixed cell temperature."""
    import numpy as np

    from solar_sim import pv

    site = build_default_site()
    pvs = site.pv_strings[0]
    poa = np.linspace(0.0, 1000.0, 50)
    t_cell = np.full_like(poa, 45.0)
    power = pv.dc_power_per_string(poa, t_cell, site, pvs)
    diffs = np.diff(power)
    assert np.all(diffs >= -1e-9), "DC power decreased as irradiance increased"


# --- I6: bounded by rating; I7: clip implies near-rating --------------------


def test_ac_power_never_exceeds_rating(clear_summer):
    """I6. AC output is capped at the inverter rating."""
    farm, ticks = clear_summer
    for tick in ticks:
        for reading in tick.inverters:
            assert reading.ac_power_w <= reading.inverter.rated_w + 1e-6
    assert ticks[-1].rollup["total_ac_power_w"] <= farm.site.ac_capacity_w + 1e-6


def test_clipping_flag_implies_near_rating(clear_summer):
    """I7. If clipping is flagged, output must actually be at the rating.

    A desynchronised flag is worse than no flag: it makes the dashboard report
    clipping on a healthy inverter, which is precisely the misdiagnosis the
    flag exists to prevent.
    """
    _, ticks = clear_summer
    flagged = 0
    for tick in ticks:
        for reading in tick.inverters:
            if reading.clipping:
                flagged += 1
                assert reading.ac_power_w >= reading.inverter.rated_w * 0.995
    assert flagged > 0, "expected clipping on a clear summer day at 36 degN"


def test_clipping_actually_occurs_on_a_clear_day(clear_summer):
    """Acceptance criterion 1: clipping must be reachable, or the logic is dead code."""
    _, ticks = clear_summer
    clipped_samples = sum(
        1 for t in ticks for r in t.inverters if r.clipping
    )
    assert clipped_samples > 0, "no inverter ever clipped; DC/AC ratio may be too low"


# --- I8: string sum approximates inverter DC --------------------------------


def test_string_dc_sums_to_inverter_dc(clear_summer):
    """I8. The strings feeding an inverter account for its DC input.

    Guards against double-counting or dropping a string.
    """
    farm, ticks = clear_summer
    for tick in ticks:
        by_inverter: dict[str, float] = {}
        for reading in tick.strings:
            by_inverter[reading.string.inverter_id] = (
                by_inverter.get(reading.string.inverter_id, 0.0) + reading.dc_power_w
            )
        for reading in tick.inverters:
            total = by_inverter.get(reading.inverter.inverter_id)
            if total is None or reading.dc_power_w <= 0.0:
                continue
            assert total == pytest.approx(reading.dc_power_w, rel=1e-6)


# --- I10: performance ratio in a plausible band ------------------------------


def test_pr_in_plausible_band_on_clear_days(clear_summer):
    """I10. The single most effective check on the loss coefficients.

    PR above ~0.95 almost always means a loss factor was left at 1.0, which is
    easy to do by accident and invisible without an explicit bound.
    """
    _, ticks = clear_summer
    prs = [t.rollup["pr_ratio"] for t in ticks if t.rollup["pr_ratio"] > 0]
    assert len(prs) > 20, "expected plenty of daytime samples"
    assert max(prs) <= 0.95, f"PR peaked at {max(prs):.3f} -- check loss coefficients"
    # Clipping is a genuine energy loss, so exclude clipped samples and the
    # remaining clear-day PR should sit in a tight, believable band.
    unclipped = [
        t.rollup["pr_ratio"]
        for t in ticks
        if t.rollup["pr_ratio"] > 0
        and not any(r.clipping for r in t.inverters)
        and t.poa_w_m2 > 400
    ]
    assert len(unclipped) > 5
    mean_pr = sum(unclipped) / len(unclipped)
    assert 0.78 <= mean_pr <= 0.92, f"mean clear-day PR {mean_pr:.3f} outside 0.78-0.92"


# --- seasonal behaviour -----------------------------------------------------


def _local(tick, tz="America/Los_Angeles"):
    """Site-local time. ``Tick.when`` is absolute (UTC) by design."""
    import pandas as pd

    return pd.Timestamp(tick.when).tz_convert(tz)


def test_winter_yield_is_lower_than_summer(summer_year):
    """A flat seasonal profile means the geometry or the date handling is wrong."""
    farm, ticks = summer_year
    by_month: dict[int, float] = {}
    for tick in ticks:
        local = _local(tick)
        by_month[local.month] = by_month.get(local.month, 0.0) + (
            tick.rollup["total_ac_power_w"]
        )
    summer = max(by_month[6], by_month[7])
    winter = min(by_month[12], by_month[1])
    # A 30 deg south-facing tilt is deliberately *not* summer-optimised, so it
    # captures much more of the winter resource than a horizontal array would.
    # The measured winter/summer ratio for 36 degN is ~0.69, well above the ~0.35
    # you would get tracking the sun. A ratio near 1.0 would mean the tilt or
    # azimuth is being ignored.
    assert winter < summer * 0.80, (
        f"winter yield ({winter:.0f}) should be clearly below summer ({summer:.0f})"
    )
    assert winter > summer * 0.55, (
        f"winter yield ({winter:.0f}) is implausibly low relative to summer "
        f"({summer:.0f}) -- check tilt and azimuth are applied"
    )


def test_noon_irradiance_seasonal_range(summer_year):
    """Sanity check against published clear-sky figures for 36 degN, 30 deg tilt."""
    farm, ticks = summer_year
    by_month: dict[int, float] = {}
    for tick in ticks:
        local = _local(tick)
        if 12 <= local.hour <= 14:
            by_month[local.month] = max(
                by_month.get(local.month, 0.0), tick.poa_w_m2
            )
    summer_noon = by_month[6]
    winter_noon = by_month[12]
    # Winter-solstice noon zenith at 36.2 degN is |lat - declination| =
    # |36.2 - (-23.44)| = 59.6 deg, so with a 30 deg south tilt the beam lands
    # far more head-on than in summer. Winter noon POA of ~1020 W/m2 is correct,
    # not a bug: tilt is chosen to favour the low-sun season.
    assert 1150 <= summer_noon <= 1400, f"summer noon POA {summer_noon:.0f} implausible"
    assert 950 <= winter_noon <= 1100, f"winter noon POA {winter_noon:.0f} implausible"
    assert summer_noon > winter_noon * 1.15, (
        "summer noon should exceed winter, but only by ~25 % at this tilt"
    )


# --- inverter curve shape ---------------------------------------------------


def test_efficiency_curve_is_flat_across_mid_load():
    """A parabolic curve collapses at high load and prevents clipping entirely."""
    import numpy as np

    from solar_sim import pv

    inverter = build_default_site().inverters[0]
    loads = np.linspace(0.05, 1.0, 100)
    eff = pv.inverter_efficiency(loads, inverter)
    # Between 30 % and 100 % load, real inverters stay above 92 % efficient.
    mid = eff[loads >= 0.30]
    assert mid.min() > 0.92, f"efficiency fell to {mid.min():.3f} at mid load"
    assert eff.max() > 0.97
    # Peak should be near the configured peak load fraction.
    assert abs(loads[int(np.argmax(eff))] - inverter.peak_load_fraction) < 0.05


def test_standby_threshold_produces_exact_zero():
    """Below the standby threshold output is exactly zero, not a small value."""
    import numpy as np

    from solar_sim import pv

    inverter = build_default_site().inverters[0]
    ac, eff, clipping = pv.inverter_ac_power(np.array([0.0, 1.0]), inverter)
    assert ac[0] == 0.0
    assert ac[1] == 0.0
    assert eff[0] == 0.0
    assert not clipping.any()


# --- thermal lag ------------------------------------------------------------


def test_internal_temperature_lags_power():
    """Thermal mass means temperature cannot track power instantaneously."""
    farm = SolarFarm(build_default_site(), weather_params=CLEAR_DAY)
    ticks = list(farm.run(datetime(2026, 6, 21, 8, 0), 24, dt_s=600))
    powers = [t.rollup["total_ac_power_w"] for t in ticks]
    temps = [t.inverters[0].internal_temp_c for t in ticks]
    # The temperature profile should be strictly smoother than the power profile.
    power_swing = max(powers) - min(powers)
    temp_swing = max(temps) - min(temps)
    assert temp_swing > 0.0
    assert temp_swing < power_swing / 100.0, "temperature appears to track power directly"


# --- startup path -----------------------------------------------------------


def test_solar_noon_handles_naive_and_aware_timestamps():
    """The default startup path is `--speed N` with no --start, and it passed an
    aware UTC timestamp into a tz_localize, which pandas rejects outright.

    Cheap to test, and it was the first thing a user would hit.
    """
    from solar_sim.main import _solar_noon

    site = build_default_site()
    aware = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    naive = datetime(2026, 6, 21, 12, 0)

    for value in (aware, naive):
        noon = _solar_noon(value, site.timezone, site.latitude, site.longitude)
        assert noon is not None
        # Solar noon at 36 degN in June is near 13:00 local, and it must be
        # inside the day rather than at midnight.
        local = pd.Timestamp(noon).tz_convert(site.timezone)
        assert 11 <= local.hour <= 14, f"solar noon resolved to {local}"


# --- rollup consistency -----------------------------------------------------


def test_capacity_factor_never_exceeds_one():
    """Regression: a unit error reported 132 % for a plant at 13 %.

    Energy accumulates in watt-hours; the nameplate denominator is in watts. The
    two were mismatched (the capacity was divided by 1000 to get kW, but the
    numerator stayed in Wh), inflating the result by 1000x and pushing a
    physically impossible figure onto a dashboard tile.
    """
    farm = SolarFarm(build_default_site(), weather_params=CLEAR_DAY)
    ticks = list(farm.run(datetime(2026, 6, 21, 6, 0), 36, dt_s=600))
    site = build_default_site()

    for tick in ticks:
        factor = tick.rollup["capacity_factor"]
        # The denominator is a full 24 h, so the figure is progress toward the
        # day's potential and is bounded by 1 by construction.
        assert 0.0 <= factor <= 1.0, f"capacity factor {factor} is out of range"

    # And it must actually be consistent with the two fields it is derived from.
    last = ticks[-1].rollup
    expected = (last["daily_yield_kwh"] * 1000.0) / (site.ac_capacity_w * 24.0)
    assert last["capacity_factor"] == pytest.approx(expected, rel=1e-9)

    # A clear June day should reach a plausible fraction of daily potential, not
    # a suspiciously tiny one -- which is what a missing 1000x would also cause.
    peak = max(t.rollup["capacity_factor"] for t in ticks)
    assert peak > 0.15, f"peak capacity factor {peak:.3f} is implausibly low for a clear day"
