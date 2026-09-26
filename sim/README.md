# Solar farm simulator

Physics-based PV model that publishes telemetry over MQTT.

## Run

```bash
uv sync

# Defaults to solar noon so the dashboard has data immediately.
uv run solar-sim

# Real time, wall clock.
uv run solar-sim --realtime

# A full day in 24 seconds (interval 30 s, 60x acceleration).
uv run solar-sim --speed 60

# A specific moment, a clean cloudless day, then stop.
uv run solar-sim --start 2026-09-25T12:00:00 --clear-sky --steps 3

# With faults injected.
uv run solar-sim --scenarios config/scenarios/demo.yaml --speed 60
```

Options: `--host --port --interval --speed --start --realtime --steps --scenarios
--clear-sky --seed -v`.

## Tests

```bash
uv run pytest              # 36 tests
uv run ruff check src/ tests/
```

| File | What it protects |
|---|---|
| `test_physics.py` | Ten physics invariants from `docs/01-design.md` §7 — night is exactly zero, no negatives or NaN, unimodal curve, monotonic in irradiance, bounded by rating, clipping reachable, string/inverter DC agreement, PR in a plausible band, seasonal behaviour |
| `test_contract.py` | The wire contract against the *real* `telegraf/telegraf.conf` and `docs/03-data-flow.md`. Catches tag and type drift, which is the failure that permanently corrupts an InfluxDB 3 schema |

`test_contract.py` is the one that earns its keep. Three independent expressions of the
contract — this simulator, the Telegraf config, and the documentation — drift apart
silently otherwise, and because InfluxDB 3 tag definitions are immutable, the cost of
catching that late is rebuilding a table.

## Layout

| Module | Responsibility |
|---|---|
| `metrics.py` | The wire contract. Topics, tags, field names and types, declared once. |
| `topology.py` | Site, blocks, inverters, strings, module specs, per-string degradation |
| `solar.py` | Solar position, clear-sky irradiance, transposition to the array plane |
| `weather.py` | AR(1) clearness index, cloud ramps, ambient temperature, wind |
| `pv.py` | Cell temperature, DC power, inverter efficiency, clipping, derating |
| `scenarios.py` | Fault definitions and resolution |
| `farm.py` | The state machine: runs the chain, produces one tick |
| `mqtt_publisher.py` | One MQTT 5 client per inverter, with the Last Will |
| `main.py` | CLI and the run loop |

## Notes for anyone changing this

**The physics has sharp edges that look like bugs but are not.**

- *Inverter efficiency is piecewise, not parabolic.* A parabola fitted to the low-load and
  peak points collapses to ~60 % at 110 % load, which means the inverter never reaches its
  rating, never clips, and quietly deflates every power curve. There is a test for this.
- *Performance ratio references DC capacity, not AC rating.* With a DC/AC ratio of 1.19,
  referencing the AC rating makes PR exceed 1.0 whenever the array is below clipping —
  which is most of the day. PR is also gated below 200 W/m² and temperature-corrected;
  without those it tracks the afternoon weather instead of the equipment.
- *The clock is UTC.* Naive local time breaks across the daylight saving transition, where
  02:00 does not exist. `Tick.when` is absolute; local time is for display only.
- *Night output is exactly 0.0.* No tolerance, no hysteresis. A few watts of twilight
  chatter is visible as noise on the night curve.

**pvlib 0.15 moved things.** `get_clearsky` → `pvlib.clearsky.ineichen`,
`get_linke_turbidity` → `pvlib.clearsky.lookup_linke_turbidity`, SPA method `nrel_spa` →
`nrel_numpy`, and `aoi` is now pure geometry taking solar zenith/azimuth rather than
latitude/longitude/time.

**`get_absolute_airmass` takes relative airmass, not a zenith angle.** Passing zenith gives
airmass ~13 instead of ~4, which cuts clear-sky DNI by a factor of five and looks like a
plausible hazy day rather than an error.
