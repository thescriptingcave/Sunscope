"""Site topology: blocks, inverters, strings, and the weather station."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import metrics as M


@dataclass(frozen=True)
class Module:
    """One PV module's nameplate and thermal behaviour."""

    nameplate_w: float = 550.0
    efficiency: float = 0.25
    noct_c: float = 45.0
    #: Fractional power loss per degC above 25 C. -0.0035 => -0.35 %/degC.
    temp_coeff_per_c: float = -0.0035


@dataclass(frozen=True)
class Inverter:
    """AC-side conversion device.

    ``rated_w`` is the hard clipping ceiling. The DC/AC ratio of the strings
    feeding it is deliberately above 1 so clipping actually occurs on hot clear
    days, which is what makes the clipping flag meaningful.
    """

    inverter_id: str
    block: str
    model: str
    rated_w: float
    max_efficiency: float = 0.985
    #: Load fraction at which the inverter is most efficient (~25-30 %).
    peak_load_fraction: float = 0.27
    #: Fractional efficiency lost between zero load and the peak. Real devices
    #: lose ~10 % here to converter overhead.
    low_load_drop: float = 0.10
    #: Fractional efficiency lost between the peak and full load. Small: real
    #: curves are near-flat across the mid range.
    high_load_drop: float = 0.05
    #: Below this load fraction the inverter is in standby and reports zero.
    standby_load_fraction: float = 0.01
    #: Heatsink temperature above which output is progressively derated.
    derate_start_c: float = 70.0
    derate_end_c: float = 90.0
    firmware: str = "4.2.1"
    #: First-order thermal lag on internal temperature, in seconds.
    thermal_tau_s: float = 900.0

    @property
    def state(self) -> str:
        return M.STATE_PRODUCING


@dataclass
class PVString:
    """A series string of modules feeding one inverter input."""

    string_id: str
    inverter_id: str
    block: str
    modules_in_series: int
    module: Module = field(default_factory=Module)
    #: Persistent per-string weakness, drawn once. This is what makes string
    #: comparison meaningful instead of pure noise -- real strings differ.
    degradation_factor: float = 1.0

    @property
    def dc_capacity_w(self) -> float:
        return self.modules_in_series * self.module.nameplate_w


@dataclass
class Site:
    """A whole solar farm."""

    site: str
    latitude: float
    longitude: float
    timezone: str
    tilt_deg: float
    azimuth_deg: float
    albedo: float
    blocks: dict[str, list[Inverter]]
    strings: dict[str, PVString]
    weather_station_id: str = "WS-01"
    #: Soiling, mismatch and DC wiring losses, as fractions.
    soiling_loss: float = 0.02
    mismatch_loss: float = 0.02
    wiring_loss: float = 0.02

    # -- derived topology ---------------------------------------------------

    @property
    def inverters(self) -> list[Inverter]:
        return [i for group in self.blocks.values() for i in group]

    @property
    def pv_strings(self) -> list[PVString]:
        return list(self.strings.values())

    def inverter(self, inverter_id: str) -> Inverter:
        for inv in self.inverters:
            if inv.inverter_id == inverter_id:
                return inv
        raise KeyError(inverter_id)

    def strings_for(self, inverter_id: str) -> list[PVString]:
        return [s for s in self.strings.values() if s.inverter_id == inverter_id]

    def block_of(self, inverter_id: str) -> str:
        return self.inverter(inverter_id).block

    @property
    def ac_capacity_w(self) -> float:
        return sum(i.rated_w for i in self.inverters)

    @property
    def dc_capacity_w(self) -> float:
        return sum(s.dc_capacity_w for s in self.pv_strings)

    @property
    def dc_ac_ratio(self) -> float:
        return self.dc_capacity_w / self.ac_capacity_w

    # -- identity helpers ---------------------------------------------------

    def identity(self, inverter: Inverter) -> dict[str, str]:
        return {
            M.SITE: self.site,
            M.BLOCK: inverter.block,
            M.INVERTER_ID: inverter.inverter_id,
        }

    def string_identity(self, s: PVString) -> dict[str, str]:
        return {
            M.SITE: self.site,
            M.BLOCK: s.block,
            M.INVERTER_ID: s.inverter_id,
            M.STRING_ID: s.string_id,
        }


def build_default_site(rng: random.Random | None = None) -> Site:
    """The 1 MWac / 1.19 MWp Mojave site from docs/01-design.md.

    4 inverters x 250 kWac, 12 strings x 180 modules x 550 Wp = 1.19 MWp.
    Each string gets a persistent degradation factor in +/-3 %, which is what
    makes the string heatmap show a stable ranking instead of noise.
    """
    rng = rng or random.Random(20260925)

    inverters: list[Inverter] = []
    strings: dict[str, PVString] = {}
    modules = Module()

    for block_index, block in enumerate(("BLK-A", "BLK-B"), start=1):
        for position in range(2):
            n = (block_index - 1) * 2 + position + 1
            inv = Inverter(
                inverter_id=f"INV-{n:02d}",
                block=block,
                model="SG250CX",
                rated_w=250_000.0,
            )
            inverters.append(inv)
            for string_index in range(3):
                s = n * 10 + string_index + 1
                strings[f"STR-{s:02d}"] = PVString(
                    string_id=f"STR-{s:02d}",
                    inverter_id=inv.inverter_id,
                    block=block,
                    modules_in_series=180,
                    module=modules,
                    degradation_factor=1.0 + rng.uniform(-0.03, 0.03),
                )

    return Site(
        site="mojave",
        latitude=36.2,
        longitude=-115.1,
        timezone="America/Los_Angeles",
        tilt_deg=30.0,
        azimuth_deg=180.0,
        albedo=0.25,
        blocks={"BLK-A": inverters[:2], "BLK-B": inverters[2:]},
        strings=strings,
    )
