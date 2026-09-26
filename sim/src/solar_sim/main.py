"""Simulator entry point.

Defaults to starting at **solar noon**, because a first run that shows an empty
dashboard is a bad first run: there is nothing to look at, and the temptation is
to conclude the pipeline is broken. Starting near the daily peak means data is
visibly flowing within seconds, and ``--realtime`` switches to wall-clock.

The other default worth arguing for is ``--speed``. Waiting twelve hours to see
a power curve is the single biggest iteration-speed problem in this kind of
work, and it means most people never validate their night-time behaviour at all.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

import pandas as pd

from .farm import SolarFarm
from .mqtt_publisher import FarmPublisher
from .scenarios import load_scenarios
from .topology import build_default_site
from .weather import WeatherParams

log = logging.getLogger("solar_sim")

#: Events emitted when a scenario is detected, so the alerting path is
#: exercised rather than assumed.
EVENT_THRESHOLD_W = 250_000.0


def _solar_noon(when: datetime, tz: str, lat: float, lon: float) -> datetime:
    """Time of peak solar elevation on the local day containing ``when``.

    Two subtleties, both learned by getting it wrong:

    * **Convert, do not localise, an aware timestamp.** pandas raises
      ``TypeError`` on ``tz_localize`` for an aware value, and this is the
      default startup path, so the error hit every run without --start.
    * **Scan one local calendar day, not 24 hours forward from ``when``.** A
      forward range straddles two local days, and the midpoint of its daylight
      samples is neither solar noon nor even reliably daytime -- it can land
      after sunset.

    Peak elevation is used rather than the midpoint of daylight, because it is
    the actual definition of solar noon and is robust to any geometry error.
    """
    import pvlib

    stamp = pd.Timestamp(when)
    stamp = stamp.tz_localize(tz) if stamp.tz is None else stamp.tz_convert(tz)
    day_start = stamp.normalize()
    index = pd.date_range(day_start, periods=1440, freq="1min")

    pos = pvlib.solarposition.get_solarposition(index, lat, lon, method="nrel_numpy")
    if pos["elevation"].isna().all() or float(pos["elevation"].max()) <= 0:
        # Polar night, or a date where the sun never rises. Fall back to the
        # caller's timestamp rather than failing.
        return stamp.to_pydatetime()
    return pos["elevation"].idxmax().to_pydatetime()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solar-sim",
        description="Physics-based solar farm simulator publishing over MQTT",
    )
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883"))
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SIM_INTERVAL_SECONDS", "30")),
        help="seconds of simulated time per tick (default 30)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.environ.get("SIM_SPEED", "1")),
        help="time acceleration. 1 = real time, 60 = 1 day in 24 min, 3600 = 1 day in 24 s",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="ISO start time. Naive values are interpreted as site-local time.",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="start at the current wall-clock time instead of solar noon",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=0,
        help="stop after N ticks. 0 runs until interrupted.",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help="YAML scenario file. See config/scenarios/ for the format.",
    )
    parser.add_argument(
        "--clear-sky",
        action="store_true",
        help="disable cloud modulation; useful for validating a clean day",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    site = build_default_site()
    log.info(
        "site %s: %.0f kWac AC, %.1f kWp DC, DC/AC ratio %.3f, %d inverters, %d strings",
        site.site,
        site.ac_capacity_w / 1000,
        site.dc_capacity_w / 1000,
        site.dc_ac_ratio,
        len(site.inverters),
        len(site.pv_strings),
    )

    if args.clear_sky:
        params = WeatherParams(
            kt_mean=1.0, phi=0.0, sigma=0.0, ramp_probability=0.0,
            temp_mean_c=22.0, wind_mean_mps=0.8,
        )
    else:
        params = WeatherParams()

    if args.start:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    elif args.realtime:
        start = datetime.now(UTC)
    else:
        # Start at solar noon on the current local day, so the dashboard has
        # something to show immediately.
        start = _solar_noon(
            datetime.now(UTC), site.timezone, site.latitude, site.longitude
        )

    farm = SolarFarm(site, weather_params=params, seed=args.seed)

    # Loaded after `start` is resolved, because scenarios may use
    # `start_offset:` relative to it. Loading first made relative timing
    # impossible, which is why the demo file had to hardcode absolute dates and
    # then quietly stopped firing.
    scenarios = (
        load_scenarios(args.scenarios, tz=site.timezone, anchor=start)
        if args.scenarios
        else []
    )
    if scenarios:
        log.info("loaded %d scenario(s) from %s", len(scenarios), args.scenarios)
    farm.scenarios = scenarios

    start = farm.to_utc(start)
    local = pd.Timestamp(start).tz_convert(site.timezone)
    log.info("starting at %s", local.strftime("%Y-%m-%d %H:%M:%S %Z"))

    publisher = FarmPublisher(site, host=args.host, port=args.port)
    publisher.connect(start)

    stopping = False

    def _handle(signum, _frame):  # noqa: ANN001
        nonlocal stopping
        log.info("signal %s received; shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    sim_dt = args.interval
    wall_dt = sim_dt / max(args.speed, 1e-9)
    log.info(
        "interval=%.0fs simulated, speed=%.1fx, wall clock step=%.2fs",
        sim_dt, args.speed, wall_dt,
    )

    when = start
    step = 0
    exit_code = 0
    try:
        while not stopping:
            tick = farm.step(when, sim_dt)
            counts = publisher.publish_tick(tick)
            if step % 10 == 0 or step == 1:
                log.info(
                    "%s  GHI=%6.1f  POA=%6.1f  total=%8.1f kW  PR=%.3f  [%s]",
                    pd.Timestamp(when).tz_convert(site.timezone).strftime("%H:%M:%S"),
                    tick.weather["ghi"],
                    tick.poa_w_m2,
                    tick.rollup["total_ac_power_w"] / 1000,
                    tick.rollup["pr_ratio"],
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                )
            # Emit an event for each inverter that is clipping, so the events
            # table is populated and the alert path has real input.
            for reading in tick.inverters:
                if reading.clipping:
                    publisher.publish_event(
                        when, "info", reading.inverter.inverter_id,
                        "CLIPPING", f"{reading.inverter.inverter_id} clipped at rating",
                        value=reading.ac_power_w, threshold=EVENT_THRESHOLD_W,
                    )
            for inverter_id in sorted(tick.faults.inverter_offline):
                publisher.publish_event(
                    when, "critical", inverter_id, "DEVICE_OFFLINE",
                    f"{inverter_id} offline", value=0.0, threshold=1.0,
                )
            for inverter_id in sorted(tick.faults.inverter_silent):
                publisher.publish_event(
                    when, "critical", inverter_id, "COMMS_LOST",
                    f"{inverter_id} stopped reporting (session still alive)",
                    value=0.0, threshold=1.0,
                )

            when += timedelta(seconds=sim_dt)
            step += 1
            if args.steps and step >= args.steps:
                log.info("reached --steps %d", args.steps)
                break
            # Pace against wall clock. Skip the wait entirely when running fast.
            if wall_dt < 0.05:
                continue
            target = time.monotonic() + wall_dt
            while (remaining := target - time.monotonic()) > 0 and not stopping:
                time.sleep(min(remaining, 0.25))
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        log.exception("fatal error")
        exit_code = 1
    finally:
        publisher.disconnect()
        log.info("published %d ticks", step)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
