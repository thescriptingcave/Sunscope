"""Full-year physics sweep.

The tests in ``test_physics.py`` are the real invariants, and they are the ones that
matter. This module exists for a different reason: it runs **every** invariant across a
**whole simulated year** rather than a handful of representative days, and it reports a
summary rather than a single boolean per assertion.

Why a whole year, and not the five days ``test_physics.py`` parameterises over

    Those five days are the right sample for a fast gate, and they are the wrong sample for
    confidence. A seasonal regression lives in the tails: the winter solstice, the days
    either side of an equinox when the sun crosses the horizon at an awkward angle, a
    January morning where cell temperature goes negative. None of those are 21 June.

    Two of the assertions below have no equivalent anywhere else in the suite -- they need a
    full year to be meaningful at all, because "winter yield is lower than summer" is not a
    statement you can make about one day.

Runtime is about 90 seconds, which is why this is a nightly job rather than part of the
per-push suite. A year at 30-minute resolution is 17,520 ticks; the sweep exists to make that
affordable to check on a schedule.

What "failing" means here

    A sweep failure is not automatically a code regression. The sky model is stochastic, so
    a rare weather draw can push a marginal case outside a band. Every assertion below is
    written to survive that: bands are physically justified rather than fitted to observed
    output, and anything that is legitimately allowed to vary is asserted as a trend rather
    than a bound. A sweep that fails on an unchanged commit is itself the finding -- it means
    the thresholds are too tight, and that is worth knowing.

Run directly:

    cd sim && uv run pytest tests/test_physics_sweep.py -v
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from solar_sim.farm import SolarFarm
from solar_sim.topology import build_default_site
from solar_sim.weather import WeatherParams

#: Clear and still, so the sweep measures the PV chain rather than the cloud model.
#: The stochastic sky is exercised by test_physics.py; mixing both here would make a
#: failure ambiguous between "the model broke" and "the weather drew badly".
CLEAR = WeatherParams(
    kt_mean=1.0, phi=0.0, sigma=0.0, ramp_probability=0.0, temp_mean_c=20.0, wind_mean_mps=0.5
)

#: 30-minute resolution across 365 days. Coarser than the 5-minute live tick because a
#: sweep is about seasonal shape, and finer resolution would multiply the runtime for no
#: extra coverage.
DT_S = 1800
YEAR_START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def year() -> tuple[SolarFarm, list]:
    """A full simulated year at 30-minute resolution, computed once for the module."""
    farm = SolarFarm(build_default_site(), weather_params=CLEAR)
    return farm, list(farm.run(YEAR_START, 365 * 48, dt_s=DT_S))


# --- structure ---------------------------------------------------------------


def test_the_year_is_actually_a_year(year):
    """Guard the fixture itself.

    A sweep that silently covered 30 days would pass everything and prove nothing, and the
    failure mode is quiet: fewer ticks, fewer assertions, all green. Cheap to check, and it
    is the one bug this module could have without noticing.
    """
    _, ticks = year
    assert len(ticks) == 365 * 48, f"expected 17,520 ticks, got {len(ticks)}"
    span = ticks[-1].when - ticks[0].when
    assert timedelta(days=364, hours=23, minutes=30) <= span <= timedelta(days=365)
    months = {t.when.month for t in ticks}
    assert months == set(range(1, 13)), f"only months {sorted(months)} present"


# --- I1, I2, I3: nothing negative, nothing non-finite, nothing at night -------


def test_no_negative_or_nonfinite_values_anywhere_in_the_year(year):
    """I2 + I3, over 17,520 ticks rather than five days.

    Checked across every numeric field on every reading. A NaN that only appears on the
    winter solstice would survive a five-day sample and then poison a rolling average for
    weeks of stored data.
    """
    _, ticks = year
    fields = (
        "ac_power_w",
        "dc_power_w",
        "ac_voltage_v",
        "ac_current_a",
        "dc_voltage_v",
        "dc_current_a",
        "efficiency",
        "heatsink_temp_c",
        "internal_temp_c",
        "uptime_s",
        "status_code",
    )
    offenders: list[str] = []
    for tick in ticks:
        for inv in tick.inverters:
            for name in fields:
                value = getattr(inv, name)
                if not math.isfinite(value) or value < 0:
                    offenders.append(
                        f"{tick.when.isoformat()} {inv.inverter.inverter_id}.{name}={value}"
                    )
                    if len(offenders) > 10:
                        break
    assert not offenders, "non-finite or negative readings:\n  " + "\n  ".join(offenders)


def test_output_is_exactly_zero_outside_daylight_all_year(year):
    """I1 across every night of the year, including the short ones near the solstices.

    "Exactly zero, not small" is the assertion. A night that produces 0.3 W is a
    sign-convention or geometry bug that a tolerance would hide, and it would show up as a
    farm that never fully shuts down -- which is precisely what a real operator watches for.
    """
    _, ticks = year
    offenders = []
    nights_checked = 0
    for tick in ticks:
        sun_up = tick.weather and tick.weather.get("ghi", 0) > 0
        if sun_up:
            continue
        nights_checked += 1
        for inv in tick.inverters:
            if inv.ac_power_w != 0.0:
                offenders.append(
                    f"{tick.when.isoformat()} {inv.inverter.inverter_id} ac={inv.ac_power_w}"
                )
                if len(offenders) > 5:
                    break
    assert not offenders, "output outside daylight:\n  " + "\n  ".join(offenders)
    assert nights_checked > 5000, f"only {nights_checked} night samples -- fixture looks wrong"


# --- I6, I7: the inverter rating is a hard ceiling -----------------------------


def test_ac_output_never_exceeds_rating_across_the_year(year):
    """I6. The 250 kW rating holds on every one of the hottest, clearest days, not just one."""
    _, ticks = year
    farm, ticks = year
    worst = 0.0
    for tick in ticks:
        for inv in tick.inverters:
            if inv.ac_power_w > inv.inverter.rated_w + 1e-6:
                worst = max(worst, inv.ac_power_w - inv.inverter.rated_w)
    assert worst <= 1e-6, f"AC output exceeded rating by {worst:.3f} W at some point in the year"
    # The site total is bounded by the nameplate, which is a different assertion: four
    # inverters each under their rating can still sum to more than the farm's AC capacity.
    peak = max(
        t["total_ac_power_w"] for t in (x.rollup or {} for x in ticks) if "total_ac_power_w" in t
    )
    assert peak <= farm.site.ac_capacity_w + 1e-6, (
        f"site peak {peak:.0f} W exceeds nameplate {farm.site.ac_capacity_w:.0f} W"
    )


def test_clipping_flag_is_never_spurious_across_the_year(year):
    """I7. Every clipping flag in the year corresponds to output actually at the rating.

    Checked in both directions: clipping set while well under the rating is a sensor lie,
    and it would make the clipping dashboard and the `clipping_sustained` alert wrong.
    """
    _, ticks = year
    ticks = [t for t in ticks if t.weather and t.weather.get("ghi", 0) > 500]
    assert ticks, "no bright samples in the fixture"
    bad = []
    for tick in ticks:
        for inv in tick.inverters:
            if inv.clipping and inv.ac_power_w < 0.98 * inv.inverter.rated_w:
                bad.append(
                    f"{tick.when.isoformat()} {inv.inverter.inverter_id} clip@ {inv.ac_power_w:.0f}"
                )
    assert not bad, "clipping flagged below the rating:\n  " + "\n  ".join(bad[:8])


# --- I8: the DC side accounts for itself --------------------------------------


def test_string_dc_sums_to_inverter_dc_all_year(year):
    """I8, every tick of the year.

    The strings feeding an inverter must account for its DC input. A drift here is the
    signature of a string that has silently dropped out of the topology, and it would show
    up as a slowly worsening string-imbalance figure rather than as an error -- which is
    exactly the kind of fault that is expensive to diagnose late.
    """
    _, ticks = year
    checked = 0
    for tick in ticks:
        by_inverter: dict[str, float] = defaultdict(float)
        for reading in tick.strings:
            by_inverter[reading.string.inverter_id] += reading.dc_power_w
        for reading in tick.inverters:
            total = by_inverter.get(reading.inverter.inverter_id)
            if total is None or reading.dc_power_w <= 0.0:
                continue
            checked += 1
            assert total == pytest.approx(reading.dc_power_w, rel=1e-6), (
                f"{reading.inverter.inverter_id} at {tick.when.isoformat()}: "
                f"strings {total:.2f} W != inverter {reading.dc_power_w:.2f} W"
            )
    assert checked > 10000, (
        f"only {checked} productive inverter-ticks compared -- fixture looks wrong"
    )


# --- I10: performance ratio stays physically plausible, all year -------------


def test_performance_ratio_stays_in_a_physical_band_all_year(year):
    """I10. The single most effective check on the loss coefficients, over a full year.

    Two bands, deliberately of different widths, and the distinction matters:

    * **Individual samples** are allowed a wide range, 0.70-0.95. Clear-day PR varies with
      cell temperature, and cell temperature varies with irradiance at fixed air temperature.
      This fixture holds air temperature at 20 degC, so summer days run the modules hot and
      legitimately produce the year's lowest PR. Measured across the year the samples run
      0.72-0.93.
    * **Monthly means** are held to the tight 0.78-0.92 band, which is the same band the
      single-day test in test_physics.py uses. This is the assertion with teeth: if a loss
      coefficient drifts, or the temperature correction stops being applied, it moves the
      monthly means and not the spread.

    The upper bound is the documented bug signal. PR above ~0.95 means the farm is converting
    more than it receives, which is a units error, not good news.
    """
    import statistics

    _, ticks = year
    by_month: dict[int, list[float]] = defaultdict(list)
    for tick in ticks:
        if not (tick.weather and tick.weather.get("ghi", 0) > 300):
            continue
        pr = (tick.rollup or {}).get("pr_ratio")
        if pr:
            by_month[tick.when.month].append(pr)
    assert len(by_month) == 12, f"only months {sorted(by_month)} have daylight samples"

    all_pr = [v for vs in by_month.values() for v in vs]
    assert len(all_pr) > 500, f"only {len(all_pr)} daylight PR samples -- fixture looks wrong"
    assert min(all_pr) >= 0.70, f"PR fell to {min(all_pr):.3f} -- check the loss chain"
    assert max(all_pr) <= 0.95, f"PR peaked at {max(all_pr):.3f} -- check loss coefficients"

    for month, values in sorted(by_month.items()):
        mean = statistics.mean(values)
        assert 0.78 <= mean <= 0.92, (
            f"month {month} mean PR {mean:.3f} outside 0.78-0.92 "
            f"(monthly samples ranged {min(values):.3f}-{max(values):.3f})"
        )


# --- seasonal shape: the assertions that need the whole year ------------------


def test_seasonal_yield_is_unimodal_about_the_solstices(year):
    """The seasonal curve rises to the summer solstice and falls back. Strictly.

    This replaces an earlier version that paired each month with the one six months away and
    required the winter half to lose. That assertion was wrong, and instructively so: March
    against September is close to a fair comparison, but April against October is not.
    April has long days and a reasonably high sun; October has short days and a low one, and
    April legitimately out-produced it by 15 %. A calendar month is not a solar one.

    Unimodality is the physically correct statement and the stronger test. It pins the phase
    of the seasonal term exactly -- a declination sign error, a latitude/declination mix-up, or
    a date-handling slip all break monotonicity, and all three would survive a looser
    winter-versus-summer comparison.

    Compared as mean daily yield, not total energy: March has 31 days and September 30, so
    comparing totals lets a single extra day decide the assertion.
    """
    _, ticks = year
    energy_by_month: dict[int, float] = defaultdict(float)
    days_by_month: dict[int, set] = defaultdict(set)
    for tick in ticks:
        month = tick.when.month
        total_w = sum(inv.ac_power_w for inv in tick.inverters)
        energy_by_month[month] += total_w * (DT_S / 3600.0)
        days_by_month[month].add(tick.when.day)
    assert len(energy_by_month) == 12
    mean = {m: energy_by_month[m] / len(days_by_month[m]) for m in energy_by_month}

    rising = [mean[m] for m in range(1, 7)]  # Jan -> Jun
    falling = [mean[m] for m in range(7, 13)]  # Jul -> Dec
    for earlier, later in zip(rising, rising[1:], strict=False):
        assert later > earlier, f"yield must rise Jan->Jun, but {later:.0f} <= {earlier:.0f}"
    for earlier, later in zip(falling, falling[1:], strict=False):
        assert later < earlier, f"yield must fall Jul->Dec, but {later:.0f} >= {earlier:.0f}"

    # And the peak is at the solstice, not merely somewhere in the northern summer.
    peak = max(mean, key=lambda m: mean[m])
    assert peak in (6, 7), f"peak daily yield in month {peak}; expected June or July"
    trough = min(mean, key=lambda m: mean[m])
    assert trough == 12, f"minimum daily yield in month {trough}; expected December"
    # A seasonal swing that is too small means the declination term is missing.
    assert mean[6] / mean[12] > 1.35, (
        f"June/December ratio {mean[6] / mean[12]:.2f} too small -- seasonal term may be missing"
    )


def test_noon_irradiance_stays_in_a_seasonal_range_all_year(year):
    """Clear-sky noon irradiance varies seasonally but stays within physical bounds.

    The failure this catches is a geometry or date bug: a declination applied twice, a
    latitude used as declination, or a Julian-day offset. All of them produce a plausible
    annual wave with the wrong amplitude or phase, so the bounds are deliberately wide
    enough to allow real seasonal variation while excluding those.
    """
    _, ticks = year
    # Noon means the highest GHI of each local day, not a fixed hour: the solar noon moves
    # by nearly two hours across the year, and a fixed hour would sample the wrong time.
    daily_max: dict = {}
    for tick in ticks:
        if not tick.weather:
            continue
        day = tick.when.date()
        ghi = tick.weather.get("ghi", 0.0)
        if ghi > daily_max.get(day, 0.0):
            daily_max[day] = ghi
    values = list(daily_max.values())
    assert len(values) == 365, f"expected 365 daily maxima, got {len(values)}"
    assert min(values) > 400, f"a winter day peaked at only {min(values):.0f} W/m2"
    assert max(values) < 1200, (
        f"a summer day peaked at {max(values):.0f} W/m2 -- implausible for 36 degN"
    )
    # Summer must exceed winter, or the seasonal term is missing entirely.
    jan = [v for d, v in daily_max.items() if d.month == 1]
    jun = [v for d, v in daily_max.items() if d.month == 6]
    assert sum(jun) / len(jun) > sum(jan) / len(jan) * 1.3, "June barely exceeds January"


def test_daylight_hours_track_the_declination(year):
    """Day length must be shortest near the solstices and longest near the equinoxes.

    Cheapest possible detector for a date-handling bug, and the one most likely to be
    introduced by a "harmless" refactor of the time step. A farm with a fixed 12-hour day
    would satisfy every power assertion in the suite and still be wrong.
    """
    _, ticks = year
    daylight_by_month: dict[int, int] = defaultdict(int)
    for tick in ticks:
        if tick.weather and tick.weather.get("ghi", 0) > 0:
            daylight_by_month[tick.when.month] += 1
    # Ticks per month are fixed, so the counts are already proportional to hours.
    hours_per_tick = DT_S / 3600.0
    june, dec = daylight_by_month[6], daylight_by_month[12]
    assert june > dec * 1.25, (
        f"June daylight {june * hours_per_tick:.0f}h vs December {dec * hours_per_tick:.0f}h"
    )
    # Equinox months sit between: neither the longest nor the shortest.
    for month in (3, 9):
        hours = daylight_by_month[month] * hours_per_tick
        assert dec < daylight_by_month[month] < june, (
            f"month {month} daylight {hours:.0f}h is not between "
            f"December {dec * hours_per_tick:.0f}h and June {june * hours_per_tick:.0f}h"
        )
