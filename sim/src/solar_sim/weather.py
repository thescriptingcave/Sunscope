"""Weather: cloudiness, ramps, ambient temperature, and wind.

The interesting part is the clearness index ``kt``. Irradiance is
``GHI = kt * GHI_clear``, and ``kt`` is modelled as an AR(1) process rather than
white noise.

That choice matters more than it looks. Independent per-sample noise produces a
jagged signal on which no realistic alerting rule would ever fire, because every
excursion is immediately undone. A persistent AR(1) process produces the
sustained dimming and gradual recovery that real cloud shadows exhibit, which is
what the alert debouncing and the performance-ratio dashboards are actually
designed around.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class WeatherParams:
    """Cloud model parameters for a desert site."""

    #: Mean clearness index. 0.78 is plausible for a clear desert site.
    kt_mean: float = 0.78
    #: AR(1) autocorrelation. High, because cloud cover is persistent.
    phi: float = 0.92
    #: Innovation standard deviation.
    sigma: float = 0.09
    #: Probability per timestep of a cloud ramp event starting.
    ramp_probability: float = 0.0015
    #: Clearness during a ramp.
    ramp_kt: float = 0.25
    ramp_min_minutes: int = 1
    ramp_max_minutes: int = 4

    # Ambient temperature
    temp_mean_c: float = 28.0
    #: Peak-to-mean daily swing, degC.
    temp_swing_c: float = 12.0
    #: Fraction of the swing that lags solar noon (afternoon maximum).
    temp_lag_fraction: float = 0.35

    wind_mean_mps: float = 2.5
    humidity: float = 0.20


class CloudModel:
    """AR(1) clearness index with occasional ramp events."""

    def __init__(self, params: WeatherParams | None = None, seed: int = 42) -> None:
        self.params = params or WeatherParams()
        self._rng = np.random.default_rng(seed)

    def clearness_index(self, times: pd.DatetimeIndex) -> pd.Series:
        """Generate kt in [0, 1] for the given timestamps.

        The AR(1) recursion is seeded from the stationary distribution, so the
        series does not start with a transient regardless of the first value.
        """
        p = self.params
        n = len(times)
        if n == 0:
            return pd.Series(dtype=float)

        noise = self._rng.normal(0.0, p.sigma, n)
        # Stationary start: mean + stationary stddev * z
        stationary_sd = p.sigma / np.sqrt(1.0 - p.phi**2)
        kt = np.empty(n)
        kt[0] = p.kt_mean + stationary_sd * self._rng.normal()

        for i in range(1, n):
            kt[i] = p.kt_mean + p.phi * (kt[i - 1] - p.kt_mean) + noise[i]
        kt = np.clip(kt, 0.02, 1.0)

        kt = self._apply_ramps(kt)
        return pd.Series(kt, index=times, name="clearness_index")

    def _apply_ramps(self, kt: np.ndarray) -> np.ndarray:
        """Overlay fast cloud-edge events: sharp drop, then recovery.

        These are the events that stress-test alert debouncing, and they are the
        reason ramp handling is a separate code path rather than a magic number.
        """
        p = self.params
        if len(kt) < 2:
            return kt

        i = 0
        while i < len(kt):
            if self._rng.random() >= p.ramp_probability:
                i += 1
                continue
            length = int(self._rng.integers(p.ramp_min_minutes, p.ramp_max_minutes + 1))
            end = min(i + length, len(kt))
            # Ramp in over one sample, hold, then recover over one sample.
            kt[i] = min(kt[i], p.ramp_kt)
            if i + 1 < end:
                kt[i + 1 : end] = p.ramp_kt
            if end < len(kt):
                kt[end] = min(kt[end], (kt[end] + p.kt_mean) / 2.0)
            i = end + 1
        return kt


def ambient_temperature(
    times: pd.DatetimeIndex, poa: pd.Series, params: WeatherParams
) -> pd.Series:
    """Air temperature, lagging solar noon.

    Temperature follows irradiance with a lag, so the daily maximum occurs in
    the afternoon rather than at solar noon. This is a real effect and it
    matters: it is why peak power and peak temperature do not coincide, and why
    a naive performance-ratio dashboard misattributes thermal losses.
    """
    t = pd.DatetimeIndex(times)
    seconds = (t - t.normalize()).total_seconds()
    day_seconds = 86400.0
    # Fraction through the day, shifted so the peak lands after solar noon.
    phase = (seconds / day_seconds - 0.5 + params.temp_lag_fraction) % 1.0
    diurnal = np.cos(2.0 * np.pi * phase)
    temp = params.temp_mean_c + (params.temp_swing_c / 2.0) * diurnal
    return pd.Series(temp, index=t, name="air_temp_c")


def wind_speed(times: pd.DatetimeIndex, params: WeatherParams) -> pd.Series:
    """Wind speed with a slow random walk, clipped to a plausible range."""
    t = pd.DatetimeIndex(times)
    n = len(t)
    if n == 0:
        return pd.Series(dtype=float, name="wind_speed_mps")
    rng = np.random.default_rng(7)
    steps = rng.normal(0.0, 0.35, n)
    w = np.empty(n)
    w[0] = params.wind_mean_mps
    for i in range(1, n):
        w[i] = max(0.2, 0.7 * w[i - 1] + 0.3 * params.wind_mean_mps + steps[i])
    return pd.Series(np.clip(w, 0.1, 12.0), index=t, name="wind_speed_mps")
