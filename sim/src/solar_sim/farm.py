"""The site state machine: run the physics and produce one tick of telemetry."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import metrics as M
from . import pv
from .scenarios import ActiveFaults, Scenario, resolve
from .solar import SolarGeometry
from .topology import Inverter, Site, build_default_site
from .weather import CloudModel, WeatherParams, ambient_temperature, wind_speed


@dataclass
class InverterReading:
    inverter: Inverter
    state: str
    ac_power_w: float
    dc_power_w: float
    ac_voltage_v: float
    ac_current_a: float
    dc_voltage_v: float
    dc_current_a: float
    efficiency: float
    heatsink_temp_c: float
    internal_temp_c: float
    uptime_s: int
    status_code: int
    clipping: bool

    def payload(self, site: Site) -> dict:
        return M.build_payload(
            "inverter_telemetry",
            M.TIMESTAMP_KEY,  # placeholder, replaced by the publisher
            **site.identity(self.inverter),
            model=self.inverter.model,
            ac_power_w=self.ac_power_w,
            dc_power_w=self.dc_power_w,
            ac_voltage_v=self.ac_voltage_v,
            ac_current_a=self.ac_current_a,
            dc_voltage_v=self.dc_voltage_v,
            dc_current_a=self.dc_current_a,
            efficiency=self.efficiency,
            heatsink_temp_c=self.heatsink_temp_c,
            internal_temp_c=self.internal_temp_c,
            uptime_s=self.uptime_s,
            status_code=self.status_code,
            clipping=self.clipping,
        )


@dataclass
class StringReading:
    string: object
    dc_power_w: float
    dc_voltage_v: float
    dc_current_a: float
    module_temp_c: float

    def payload(self, site: Site) -> dict:
        return M.build_payload(
            "string_telemetry",
            M.TIMESTAMP_KEY,
            **site.string_identity(self.string),
            dc_power_w=self.dc_power_w,
            dc_voltage_v=self.dc_voltage_v,
            dc_current_a=self.dc_current_a,
            module_temp_c=self.module_temp_c,
        )


@dataclass
class Tick:
    """Everything produced for one instant."""

    when: datetime
    inverters: list[InverterReading] = field(default_factory=list)
    strings: list[StringReading] = field(default_factory=list)
    #: Plane-of-array irradiance in W/m^2. Not on the wire; kept for tests
    #: and for the PR calculation.
    poa_w_m2: float = 0.0
    weather: dict | None = None
    rollup: dict | None = None
    events: list[dict] = field(default_factory=list)
    faults: ActiveFaults = field(default_factory=ActiveFaults)


class SolarFarm:
    """Holds the mutable state (thermal lag, daily energy) across ticks."""

    def __init__(
        self,
        site: Site | None = None,
        weather_params: WeatherParams | None = None,
        scenarios: list[Scenario] | None = None,
        seed: int = 42,
    ) -> None:
        self.site = site or build_default_site()
        self.geometry = SolarGeometry(self.site)
        self.weather_params = weather_params or WeatherParams()
        self.cloud = CloudModel(self.weather_params, seed=seed)
        self.scenarios = scenarios or []

        self._internal_temp = {i.inverter_id: 25.0 for i in self.site.inverters}
        self._heatsink_temp = {i.inverter_id: 25.0 for i in self.site.inverters}
        self._uptime = {i.inverter_id: 0 for i in self.site.inverters}
        self._day_energy_wh = 0.0
        self._day_date: date | None = None
        self._was_up = False
        #: Performance ratio is not reported below this POA. See
        #: performance_ratio() for why reporting it looks broken.
        self.pr_min_poa_w_m2 = 200.0

    # -- physics ------------------------------------------------------------

    def _utc(self, when: datetime) -> datetime:
        """Normalise a timestamp to an aware UTC instant.

        A naive datetime is interpreted as *site-local* time. Aware datetimes are
        converted. Everything downstream works in absolute time, because solar
        geometry is a function of absolute time and local time is only for
        display.

        This also sidesteps daylight saving entirely: stepping a naive local
        clock through the spring transition lands on 02:00, which does not
        exist, and pandas raises rather than guessing. ``run`` therefore keeps
        the clock in UTC and never adds to a local timestamp.
        """
        stamp = pd.Timestamp(when)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(self.site.timezone)
        return stamp.tz_convert("UTC").to_pydatetime()

    def to_utc(self, when: datetime) -> datetime:
        """Public wrapper for :meth:`_utc`, for callers outside the model."""
        return self._utc(when)

    def _utc_index(self, when: datetime) -> pd.DatetimeIndex:
        """UTC-aware DatetimeIndex for a single instant, for pvlib."""
        return pd.DatetimeIndex([pd.Timestamp(self._utc(when))])

    @property
    def local_timezone(self) -> str:
        return self.site.timezone

    def step(self, when: datetime, dt_s: float = 30.0) -> Tick:
        """Advance the model by one timestep and return the readings."""
        times = self._utc_index(when)
        faults = resolve(self.scenarios, when)

        position = self.geometry.position(times)
        clear = self.geometry.clear_sky(times)
        zenith = float(position["zenith"].iloc[0])
        sun_up = zenith < 90.0

        kt = (
            np.array([faults.kt_override])
            if faults.kt_override is not None
            else self.cloud.clearness_index(times).to_numpy()
        )[0]

        # Night: everything is exactly zero. Not "small", not "rounded down".
        if sun_up:
            ghi = float(clear["ghi"].iloc[0]) * kt * (1.0 + faults.ghi_bias)
            dni = float(clear["dni"].iloc[0]) * kt
            dhi = float(clear["dhi"].iloc[0]) * kt
        else:
            ghi = dni = dhi = 0.0
            kt = 0.0

        poa = float(
            self.geometry.plane_of_array(times, ghi, dni, dhi, position).iloc[0]
        )
        air_temp = float(
            ambient_temperature(times, pd.Series([poa], index=times), self.weather_params).iloc[0]
        ) + faults.ghi_temp_bias
        wind = float(wind_speed(times, self.weather_params).iloc[0])

        # Reset daily energy at local midnight.
        today = when.date()
        if self._day_date != today:
            self._day_date = today
            self._day_energy_wh = 0.0

        tick = Tick(when=when, faults=faults, poa_w_m2=poa)

        total_ac = 0.0
        total_poa_w = 0.0
        online = 0
        strings_online = 0
        cell_temps: list[float] = []

        for inverter in self.site.inverters:
            pvs = self.site.strings_for(inverter.inverter_id)
            offline = inverter.inverter_id in faults.inverter_offline

            dc_total = 0.0
            dc_v = 0.0
            dc_a = 0.0
            module_temps: list[float] = []

            for s in pvs:
                scaled_poa = poa
                if sun_up:
                    scale = faults.string_scale.get(s.string_id, 1.0)
                    t_cell = float(
                        pv.cell_temperature(
                            [scaled_poa * scale], [air_temp], [wind], s.module
                        )[0]
                    )
                    t_cell += faults.string_temp_bias.get(s.string_id, 0.0)
                    p_dc = float(
                        pv.dc_power_per_string([poa], [t_cell], self.site, s)[0]
                    )
                    # A string fault scales output, not irradiance.
                    p_dc *= scale
                else:
                    t_cell = air_temp
                    p_dc = 0.0

                if p_dc > 0.0:
                    # ~1.45 V per module at operating point, scaled for temperature.
                    v = s.modules_in_series * 41.0 * (
                        1.0 + 0.0003 * (t_cell - 25.0) * 60.0 / 41.0
                    )
                    a = p_dc / max(v, 1.0)
                else:
                    v = a = 0.0

                dc_total += p_dc
                dc_v += v
                dc_a += a
                module_temps.append(t_cell)
                cell_temps.append(t_cell)

                if not offline and not faults.is_silent(inverter.inverter_id):
                    tick.strings.append(
                        StringReading(s, p_dc, v, a, t_cell)
                    )
                    if p_dc > 0.0:
                        strings_online += 1

            heatsink = self._heatsink_temp[inverter.inverter_id]
            if offline or not sun_up:
                ac = np.array([0.0])
                eff = np.array([0.0])
                clipping = np.array([False])
            else:
                load = np.clip(dc_total, 0.0, inverter.rated_w) / inverter.rated_w
                steady_hs = 25.0 + 55.0 * load
                heatsink += (1.0 - math.exp(-dt_s / 1800.0)) * (steady_hs - heatsink)
                ac, eff, clipping = pv.inverter_ac_power(
                    np.array([dc_total]), inverter, np.array([heatsink])
                )

            self._internal_temp[inverter.inverter_id] = pv.update_internal_temperature(
                self._internal_temp[inverter.inverter_id], ac, inverter, dt_s
            )[0]

            ac_w = float(ac[0])
            efficiency = float(eff[0])
            is_clipping = bool(clipping[0])

            if offline:
                # Power loss. The MQTT Last Will also reports this, which is how
                # it is normally detected.
                state = M.STATE_OFFLINE
            elif not sun_up or ac_w <= 0.0:
                state = M.STATE_STANDBY
            elif heatsink > inverter.derate_start_c:
                state = M.STATE_DERATING
            else:
                state = M.STATE_PRODUCING

            if not offline:
                self._uptime[inverter.inverter_id] += int(dt_s)
                online += 1
                total_ac += ac_w

            ac_v = 0.0
            ac_a = 0.0
            if ac_w > 0.0:
                ac_v = 480.0
                ac_a = ac_w / ac_v

            tick.inverters.append(
                InverterReading(
                    inverter=inverter,
                    state=state,
                    ac_power_w=ac_w,
                    dc_power_w=dc_total,
                    ac_voltage_v=ac_v,
                    ac_current_a=ac_a,
                    dc_voltage_v=dc_v,
                    dc_current_a=dc_a,
                    efficiency=efficiency,
                    heatsink_temp_c=heatsink,
                    internal_temp_c=self._internal_temp[inverter.inverter_id],
                    uptime_s=self._uptime[inverter.inverter_id],
                    status_code=M.STATUS_CODES[state],
                    clipping=is_clipping,
                )
            )

            if sun_up:
                total_poa_w += poa * len(pvs) * inverter.rated_w / 1000.0

        self._day_energy_wh += total_ac * dt_s / 3600.0

        mean_cell_temp = sum(cell_temps) / len(cell_temps) if cell_temps else air_temp

        tick.weather = {
            M.SITE: self.site.site,
            M.STATION_ID: self.site.weather_station_id,
            "ghi": max(ghi, 0.0),
            "dni": max(dni, 0.0),
            "dhi": max(dhi, 0.0),
            "air_temp_c": air_temp,
            "wind_speed_mps": wind,
            "relative_humidity": self.weather_params.humidity,
            "clearness_index": float(kt),
        }

        # Energy is accumulated in watt-hours and nameplate is in watts, so the
        # denominator is capacity * 24 h in Wh. Converting the capacity to kW
        # here instead was a unit error that inflated the result 10x, reporting
        # 132 % for a plant that had actually produced 13 %.
        #
        # The denominator is a full 24 h rather than the hours elapsed, so this
        # reads as "progress toward the day's potential" and is deliberately
        # stable early in the day. A textbook capacity factor (energy / capacity
        # / hours elapsed) is near-meaningless at 08:00 and swings wildly on
        # partly cloudy days, which makes it useless as a live tile.
        capacity_factor = (
            self._day_energy_wh / (self.site.ac_capacity_w * 24.0)
            if self.site.ac_capacity_w
            else 0.0
        )
        pr = self.performance_ratio(total_ac, poa, mean_cell_temp, sun_up)

        tick.rollup = {
            M.SITE: self.site.site,
            "total_ac_power_w": total_ac,
            "daily_yield_kwh": self._day_energy_wh / 1000.0,
            "pr_ratio": pr,
            "capacity_factor": capacity_factor,
            "inverters_online": online,
            "strings_online": strings_online,
        }
        return tick

    def performance_ratio(
        self, ac_power_w: float, poa_w_m2: float, cell_temp_c: float, sun_up: bool
    ) -> float:
        """Temperature-corrected performance ratio.

        Two things matter here, and both were learned the hard way.

        **A gate on irradiance.** Below roughly 200 W/m^2 the ratio of actual to
        expected power is dominated by inverter overhead and the diffuse
        fraction, not by system performance. Reporting it produces PR values
        above 1.0 at dawn and dusk, which makes the whole metric look broken.
        Real PR reporting always gates on irradiance.

        **A temperature correction.** Cell temperature costs roughly
        0.35 %/degC. Without correcting for it, PR tracks the afternoon
        temperature and the number says more about the weather than about the
        equipment. Corrected, a falling PR points at soiling, degradation,
        downtime or a fault -- which is the entire point of the metric.

        PR above ~0.95 is a bug signal rather than good news: it almost always
        means a loss coefficient was left at 1.0.
        """
        if not sun_up or poa_w_m2 < self.pr_min_poa_w_m2:
            return 0.0
        if self.site.ac_capacity_w <= 0:
            return 0.0
        temp_coeff = self.site.pv_strings[0].module.temp_coeff_per_c
        thermal = 1.0 + temp_coeff * (cell_temp_c - 25.0)
        # Reference DC capacity, NOT AC rating. With a DC/AC ratio of 1.19,
        # referencing the AC rating makes PR exceed 1.0 whenever the array is
        # below clipping -- which is most of the day, and exactly when an
        # operator would be looking at the number. PR is conventionally
        # normalised to array nameplate DC.
        expected = self.site.dc_capacity_w * (poa_w_m2 / 1000.0) * max(thermal, 0.1)
        if expected <= 0:
            return 0.0
        return min(ac_power_w / expected, 1.5)

    def run(self, start: datetime, steps: int, dt_s: float = 30.0) -> Iterator[Tick]:
        """Yield ``steps`` ticks, advancing absolute time by ``dt_s`` each step.

        ``start`` may be naive (interpreted as site-local) or aware. The clock is
        carried in UTC internally so a daylight saving transition cannot produce
        a nonexistent local time.
        """
        when = self._utc(start)
        for _ in range(steps):
            yield self.step(when, dt_s)
            when += timedelta(seconds=dt_s)
