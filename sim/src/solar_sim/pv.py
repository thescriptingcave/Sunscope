"""PV physics: cell temperature, DC power, and the inverter's AC conversion.

The chain is: POA irradiance -> cell temperature -> DC power per string ->
inverter efficiency and clipping -> AC power. Each stage is separate so a wrong
number can be localised rather than guessed at.
"""

from __future__ import annotations

import numpy as np

from .topology import Inverter, PVString, Site


def cell_temperature(
    poa_w_m2: np.ndarray,
    air_temp_c: np.ndarray,
    wind_mps: np.ndarray,
    module,
) -> np.ndarray:
    """Faiman-style cell temperature, degC.

    ``T_cell = T_air + POA/800 * (NOCT - 20) - wind_correction``

    The wind term is why a windy day yields better efficiency than a still day at
    identical irradiance. That correlation is real, and it is exactly the kind
    of thing an operator's performance-ratio dashboard must not misread as a
    fault.
    """
    poa = np.asarray(poa_w_m2, dtype=float)
    air = np.asarray(air_temp_c, dtype=float)
    wind = np.asarray(wind_mps, dtype=float)

    rise = poa / 800.0 * (module.noct_c - 20.0)
    # Wind removes heat; diminishing returns above ~5 m/s.
    wind_correction = 3.0 * (1.0 - np.exp(-0.15 * np.maximum(wind, 0.0)))
    return air + rise - wind_correction


def dc_power_per_string(
    poa_w_m2: np.ndarray,
    t_cell_c: np.ndarray,
    site: Site,
    pvs: PVString,
) -> np.ndarray:
    """DC power for one string, in W, including all DC-side losses."""
    poa = np.asarray(poa_w_m2, dtype=float)
    t_cell = np.asarray(t_cell_c, dtype=float)

    # Thermal derating relative to STC (25 degC).
    delta_t = t_cell - 25.0
    thermal = 1.0 + pvs.module.temp_coeff_per_c * delta_t

    # DC collection efficiency.
    collection = (
        (1.0 - site.soiling_loss)
        * (1.0 - site.mismatch_loss)
        * (1.0 - site.wiring_loss)
        * pvs.degradation_factor
    )

    # Reference: nameplate is measured at 1000 W/m^2, 25 degC.
    p = (pvs.dc_capacity_w * np.clip(poa, 0.0, None) / 1000.0) * np.clip(thermal, 0.0, None)
    return p * collection


def inverter_efficiency(load_fraction: np.ndarray, inverter: Inverter) -> np.ndarray:
    """Conversion efficiency as a function of load fraction.

    The shape matters, and a parabola is the wrong one. A parabola fitted to the
    low-load and peak points collapses to about 60 % at 110 % load, which would
    mean the inverter never reaches its rating, never clips, and quietly deflates
    every power curve. Real inverter curves are close to flat from roughly 20 %
    load up to the rating, with a soft knee at very low load where converter
    overhead dominates, and a gentle decline approaching 100 %.

    So the curve is piecewise: a low-load knee rising to the peak, then a gentle
    decline toward the rating, beyond which clipping takes over.
    """
    x = np.clip(np.asarray(load_fraction, dtype=float), 0.0, 2.0)
    peak = inverter.max_efficiency
    x_peak = max(inverter.peak_load_fraction, 1e-6)
    span = max(1.0 - x_peak, 1e-6)

    low = peak * (1.0 - inverter.low_load_drop * (1.0 - np.minimum(x, x_peak) / x_peak) ** 2)
    high = peak * (1.0 - inverter.high_load_drop * ((x - x_peak) / span) ** 2)
    eff = np.where(x <= x_peak, low, high)
    return np.clip(eff, 0.0, peak)


def inverter_ac_power(
    dc_power_w: np.ndarray,
    inverter: Inverter,
    heatsink_temp_c: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert DC power to AC power.

    Returns ``(ac_power_w, efficiency, clipping)``.

    Three behaviours, applied in order:

    1. **Standby** below ``standby_load_fraction``. Output is exactly zero -- not
       a small positive number, which would make the night-time curve noisy.
    2. **Clipping** at ``rated_w``. The excess is discarded, and the flag is
       raised so the dashboard can distinguish clipping (a healthy inverter on a
       sunny day) from poor performance (a fault). Conflating the two is the
       most common mistake in PV monitoring.
    3. **Thermal derating** above ``derate_start_c``, progressive to
       ``derate_end_c``. On a hot, still day this can bite before clipping does.
    """
    dc = np.clip(np.asarray(dc_power_w, dtype=float), 0.0, None)
    zero = np.zeros_like(dc)

    # 1. Standby
    active = dc >= inverter.standby_load_fraction * inverter.rated_w

    # 2. Efficiency + clipping
    load_fraction = dc / inverter.rated_w
    efficiency = inverter_efficiency(load_fraction, inverter)
    ac = dc * efficiency

    clipping = ac >= inverter.rated_w
    ac = np.minimum(ac, inverter.rated_w)

    # 3. Thermal derating
    if heatsink_temp_c is not None:
        temp = np.asarray(heatsink_temp_c, dtype=float)
        span = max(inverter.derate_end_c - inverter.derate_start_c, 1e-6)
        derate_factor = np.clip(1.0 - (temp - inverter.derate_start_c) / span, 0.0, 1.0)
        derating = (temp > inverter.derate_start_c) & active
        ac = np.where(derating, ac * derate_factor, ac)
        efficiency = np.where(active, ac / np.maximum(dc, 1e-9), 0.0)

    ac = np.where(active, ac, zero)
    efficiency = np.where(active, np.clip(efficiency, 0.0, 1.0), 0.0)
    return ac, efficiency, clipping & active


def update_internal_temperature(
    previous_c: float, ac_power_w: np.ndarray, inverter: Inverter, dt_s: float
) -> np.ndarray:
    """First-order thermal lag on internal temperature.

    Internal temperature does not track power instantaneously: the device has
    thermal mass. The visible consequence is a phase shift between the power and
    temperature curves, which is a real and easily-misread effect.
    """
    tau = max(inverter.thermal_tau_s, 1.0)
    alpha = 1.0 - np.exp(-dt_s / tau)
    load = np.clip(np.asarray(ac_power_w, dtype=float), 0.0, None) / inverter.rated_w
    steady = 25.0 + 45.0 * load
    return previous_c + alpha * (steady - previous_c)
