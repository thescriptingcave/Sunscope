"""Solar geometry, clear-sky irradiance, and transposition to the array plane.

pvlib 0.15 reorganised these functions out of ``pvlib.irradiance`` into
``pvlib.clearsky``, ``pvlib.atmosphere`` and ``pvlib.solarposition``. The older
``get_clearsky`` / ``get_linke_turbidity`` names no longer exist, the SPA method
is ``nrel_numpy`` rather than ``nrel_spa``, and ``aoi`` became a pure-geometry
function taking solar zenith/azimuth rather than latitude/longitude/time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib
import pvlib.atmosphere as atm
import pvlib.clearsky as clearsky
import pvlib.solarposition as sp

from .topology import Site

#: Mojave sits around 1200 m. Airmass depends on pressure, and using sea level
#: for a site at altitude biases clear-sky DNI noticeably.
DEFAULT_ALTITUDE_M = 1200.0


class SolarGeometry:
    """Wraps pvlib for the parts of the chain that are pure geometry."""

    def __init__(self, site: Site, altitude_m: float = DEFAULT_ALTITUDE_M) -> None:
        self.site = site
        self.altitude_m = altitude_m

    def position(self, times: pd.DatetimeIndex) -> pd.DataFrame:
        """Solar zenith and azimuth via the NREL SPA, plus airmass.

        ``nrel_numpy`` is the SPA implementation. The other valid names in
        pvlib 0.15 are ``nrel_numba`` and ``nrel_c`` -- not ``nrel_spa``.
        """
        return sp.get_solarposition(
            times, self.site.latitude, self.site.longitude, method="nrel_numpy"
        )

    def clear_sky(self, times: pd.DatetimeIndex) -> pd.DataFrame:
        """Clear-sky GHI/DNI/DHI via the Ineichen/Perez model.

        Ineichen takes *apparent* zenith and *absolute* airmass rather than the
        true values, and needs Linke turbidity, which in pvlib 0.15 comes from
        ``pvlib.clearsky.lookup_linke_turbidity``. It returns all three
        components: DHI is derived internally as ``GHI - DNI * cos(zenith)``.

        (``clearsky.haurwitz`` is a *different* model -- a GHI-only clear-sky
        estimator taking just apparent zenith. It is not the diffuse-split
        helper the old ``irradiance.disc`` provided.)
        """
        position = self.position(times)
        pressure = atm.alt2pres(self.altitude_m)
        # get_absolute_airmass takes *relative* airmass, not a zenith angle.
        # Passing apparent_zenith here silently yields airmass ~13 instead of
        # ~4, which cuts clear-sky DNI by a factor of five and looks like a
        # plausible hazy day rather than an error.
        airmass_relative = atm.get_relative_airmass(position["zenith"])
        airmass_absolute = atm.get_absolute_airmass(airmass_relative, pressure)
        linke = clearsky.lookup_linke_turbidity(
            times, self.site.latitude, self.site.longitude
        )
        dni_extra = pvlib.irradiance.get_extra_radiation(times)
        out = clearsky.ineichen(
            position["apparent_zenith"],
            airmass_absolute,
            linke,
            altitude=self.altitude_m,
            dni_extra=dni_extra,
        )
        # Below the horizon these models can return small non-zero or negative
        # values. The caller zeroes output there anyway, but negatives in the
        # weather payload would be wrong in their own right.
        for column in ("ghi", "dni", "dhi"):
            out[column] = out[column].clip(lower=0.0)
        return out

    def angle_of_incidence(self, position: pd.DataFrame) -> np.ndarray:
        """Angle of incidence in degrees, from surface and solar geometry."""
        return pvlib.irradiance.aoi(
            self.site.tilt_deg,
            self.site.azimuth_deg,
            np.asarray(position["zenith"], dtype=float),
            np.asarray(position["azimuth"], dtype=float),
        )

    def plane_of_array(
        self,
        times: pd.DatetimeIndex,
        ghi,
        dni,
        dhi,
        position: pd.DataFrame | None = None,
    ) -> pd.Series:
        """Transpose GHI/DNI/DHI onto the fixed-tilt array plane.

        Returns total POA irradiance in W/m^2. Uses pvlib's own
        ``poa_components`` so the sky and ground-reflected models match the
        rest of the library rather than being re-derived here.
        """
        if position is None:
            position = self.position(times)
        aoi = self.angle_of_incidence(position)
        components = pvlib.irradiance.poa_components(
            aoi,
            np.asarray(dni, dtype=float),
            np.asarray(dhi, dtype=float),
            np.asarray(ghi, dtype=float) * self.site.albedo,
        )
        poa = np.asarray(components["poa_global"], dtype=float)
        return pd.Series(np.clip(poa, 0.0, None), index=times, name="poa")


def sun_is_up(position: pd.DataFrame) -> pd.Series:
    """True when the sun is above the horizon.

    No hysteresis band here on purpose: the acceptance criteria require output
    to be *exactly* zero outside daylight, and a band would leave a few watts
    of twilight output that shows up as noise on the night-time curve.
    """
    return position["zenith"] < 90.0
