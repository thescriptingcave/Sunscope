# 01 — Design

## 1. Purpose

Build a simulator that produces **physically plausible** solar farm telemetry, publishes it over
MQTT, stores it in InfluxDB 3, and surfaces it through Grafana and a real-time web/mobile app.

The design goal is **not** to be a game. It is to be a system where the numbers hold up to
inspection: power follows the sun, inverters clip at their rating, cell temperature drives
efficiency loss, and strings on the same inverter disagree slightly because real strings do.
A simulator that emits a smooth bell curve teaches you nothing about how to build a real
monitoring system, because real data is messy and the mess is the interesting part.

## 2. Domain model

### 2.1 Hierarchy

```
Site (mojave)
 ├── Block (BLK-A, BLK-B)
 │    ├── Inverter (INV-01 … INV-04)      250 kWac rated
 │    │    └── String (STR-01 … STR-12)   3 strings per inverter
 │    └── WeatherStation (WS-01)          plane-of-array + ambient
 └── Rollup                               site-level aggregate
```

Telemetry flows **up** the hierarchy: strings → inverters → site. Every level is independently
queryable, and the rollup is computed by the simulator, not by a database aggregation, so it
can carry values (daily yield, performance ratio) that are not pure sums.

### 2.2 Why a 1 MW site

Deliberately small. 4 inverters × 3 strings = 12 strings is the smallest topology that still
exercises every interesting case:

- **String-level imbalance** is visible (12 series, not 4).
- **Inverter-level clipping** is visible (4 devices that can independently saturate).
- **Partial outage** is expressible (lose `STR-07` and lose 8.3% of DC, not 25%).
- **Dashboard legibility** — 12 heatmap cells is one readable grid; 1200 is not.

It is also configurable, so this is a starting point rather than a ceiling.

## 3. Topology specification

| Parameter | Value |
|---|---|
| Site AC capacity | 1 000 000 W (4 × 250 kWac) |
| Module | 550 Wp mono-Si, 25% panel efficiency |
| Modules per string | 180 |
| String DC capacity | 99.0 kWp |
| Total DC capacity | 1 188 000 Wp (1.19 MWp) |
| Strings per inverter | 3 (297 kWp DC into 250 kWac AC) |
| DC/AC ratio | 1.19 |
| Strings per block | 6 (3 per inverter × 2 inverters) |

**Why DC/AC = 1.19 matters.** With a ratio near 1.0 the inverter never clips and the clipping
logic is dead code. At 1.19, clipping begins when plane-of-array irradiance is roughly
850–900 W/m² on a cool day, and earlier on a hot one. This gives a realistic, recurring
clipping window around solar noon that exercises the alert rules and the Grafana panel
every clear day. It is also a realistic number for a modern Nevada utility-scale site.

### 3.1 Site geometry

| Parameter | Value |
|---|---|
| Latitude / Longitude | 36.2° N, 115.1° W |
| Timezone | America/Los_Angeles |
| Array tilt | 30° |
| Array azimuth | 180° (due south) |
| Ground albedo | 0.25 (desert) |
| Tracker | None (fixed tilt) |

Latitude 36° N with 30° tilt is close to the annual optimum for fixed-tilt PV, so the diurnal
curve is nicely symmetric and annual seasonality is strong — good for validating that the
seasonal model works.

## 4. Physics model

The chain below is the heart of the simulator. Each stage is a separate module with its own
tests, so a bad number can be localised rather than guessed at.

```
   ┌─ clock / time-of-day ─────────────────────────────────┐
   │                                                        │
   ▼                                                        │
1. Solar position  ── NREL SPA ────────────────────────────▶  solar zenith, azimuth, airmass
   │
   ▼
2. Clear-sky irradiance  ── Ineichen/Perez ────────────────▶  GHI_clear, DNI_clear, DHI_clear
   │
   ▼
3. Cloud modulation  ── AR(1) clearness index ─────────────▶  kt, GHI, DNI, DHI
   │
   ▼
4. Plane-of-array irradiance  ── transposition ────────────▶  POA global
   │
   ▼
5. Cell temperature  ── Faiman / NOCT, wind-dependent ─────▶  T_cell
   │
   ▼
6. DC power per string  ── pvlib single-diode / PVWatts ──▶  P_dc, V_dc, I_dc
   │
   ▼
7. Inverter conversion  ── efficiency curve + clipping ────▶  P_ac, status, temps
   │
   ▼
8. Site aggregation  ─────────────────────────────────────▶  rollup, PR, yield, capacity factor
```

### 4.1 Solar position (NREL SPA)

`pvlib.solarposition.get_solarposition` with the NREL SPA algorithm. Accurate to ~0.0003°
over the relevant range. Produces zenith and azimuth angles plus the airmass mass.

Below the horizon, output is forced to exactly zero with a hysteresis band so that a
twilight-adjacent value does not chatter between zero and a few watts.

### 4.2 Clear-sky irradiance

`pvlib.irradiance.get_clearsky` with the Ineichen model, using Linke turbidity. Produces
clear-sky GHI, DNI, and DHI. Ineichen is a good default for a mid-turbidity desert site and
degrades gracefully.

### 4.3 Cloud modulation — the clearness index

Real irradiance is `GHI = kt × GHI_clear`, where `kt` is the clearness index in [0, 1].

Rather than random noise, `kt` follows an **AR(1) process**:

```
kt[t] = μ + φ · (kt[t-1] - μ) + ε[t]
```

- `μ` ≈ 0.78 mean clearness for a desert site
- `φ` ≈ 0.92 — high autocorrelation, because cloud cover is persistent
- `ε` ~ N(0, σ) with σ ≈ 0.09

Autocorrelation matters. Independent per-sample noise produces a jagged, unrealistic signal that
makes no realistic alerting rule ever fire. A persistent AR(1) process produces the sustained
dimming and gradual recovery that real cloud shadows exhibit.

On top of AR(1), occasional **ramp events** are injected: a fast drop to `kt ≈ 0.25` lasting
1–4 minutes with a fast recovery. These are the events that stress-test alert debouncing, and
they are the reason ramp handling is a distinct code path rather than a magic number.

### 4.4 Plane-of-array irradiance

Transposes GHI/DNI/DHI onto the tilted array plane:

```
POA = DNI · cos(AOI) + DHI · (1 + cos(tilt)) / 2 + GHI · albedo · (1 - cos(tilt)) / 2
```

where `AOI` is the angle of incidence derived from solar zenith, solar azimuth, tilt, and
array azimuth. Plus a small soiling factor (0.98) representing accumulated dust.

### 4.5 Cell temperature

Faiman-style model, which is wind-dependent and therefore more realistic than the simpler
NOCT-only form:

```
T_cell = T_ambient + POA / 800 · (NOCT - 20)
        + wind_correction
```

Wind reduces the rise above ambient, so a windy day yields better efficiency than a still
day at identical irradiance. That correlation is real and worth modelling, because it is
exactly the kind of thing an operator's performance-ratio dashboard needs to not be confused by.

### 4.6 DC power per string

`pvlib.pvsystem` with a single-diode model (or PVWatts as a lighter alternative), plus:

- **Temperature coefficient** — power falls ~0.35 %/°C above 25 °C
- **Soiling loss** — 2 %
- **Mismatch loss** — 2 %
- **Wiring loss** — 2 %

Each string gets a small persistent **manufacturing/soiling bias** drawn once at startup in the
range ±3 %, plus a slow random-walk drift. This is why `STR-04` is always slightly weaker than
`STR-03`, and it is what makes string-level heatmap comparison meaningful rather than noise.

### 4.7 Inverter conversion

Three behaviours, in order:

1. **Efficiency curve** — output efficiency varies with load fraction, peaking around
   20–30 % load and falling off at very low load. Below ~1 % load the inverter is in
   standby and reports zero.
2. **Clipping** — output is hard-capped at 250 000 W. When the DC input would exceed what the
   AC can deliver, the excess is clipped. `clipping = true` is published so the dashboard and
   alert rules can distinguish clipping from poor performance — these are completely different
   conditions and conflating them is the most common mistake in PV monitoring.
3. **Thermal derating** — above ~70 °C heatsink temperature, output is progressively reduced.
   On a hot still day this can precede clipping.

Inverter internal temperature is modelled as first-order lag on the load, so it does not track
power instantaneously — a real thermal mass effect, and visible as a phase shift in the
temperature curve.

### 4.8 Derived metrics

| Metric | Definition |
|---|---|
| Performance Ratio (PR) | `(P_ac / P_ac,rated) ÷ (POA / 1000)` |
| Daily yield | Integral of `P_ac` over the day, in kWh |
| Capacity factor | `Energy_today / (Rated_kW × 24)` |
| String imbalance | `(max − min) / max` DC power across strings on one inverter |
| Clipping loss | DC energy available minus AC energy delivered |

PR is the single most useful health number in PV operations, and it is the metric that exposes
soiling, degradation, downtime, and thermal losses all at once. It gets first-class treatment
in the dashboards.

## 5. Fault scenarios

A clean farm never triggers an alert. These scenarios exist so the alerting path, the Grafana
panels, and the offline detection are all genuinely exercised.

| Scenario | Trigger | Effect | Detected by |
|---|---|---|---|
| **Inverter offline** | Scheduled or random | Stops publishing; LWT fires; `status` retained message goes to `offline` | MQTT Last Will + status topic + missing telemetry |
| **String underperformance** | Scheduled | One string's output scaled to 40–70 % with a distinct degradation signature | String imbalance ratio |
| **Sensor drift** | Scheduled | `module_temp_c` biased +8 °C, `ghi` biased −12 % | PR drops, temp looks wrong |
| **Comms loss** | Scheduled | Telemetry stops but LWT does *not* fire (network partition, not power loss) | Gap in time series with no offline signal — deliberately the hard case |
| **Power clipping** | Natural | Inverter clips at 250 kW around solar noon | `clipping = true` |
| **Cloud ramp** | Natural | `kt` drops to ~0.25 for 1–4 minutes | Alert debouncing logic |
| **Inverter overheat** | Hot + still day | Progressive derating above 70 °C | Heatsink temperature threshold |
| **Wildfire / smoke haze** | Manual | `kt` decays to a sustained 0.15 over 30 min | Sustained low PR |

**Comms loss is the important one.** Power loss trips the Last Will, so it is detected in
seconds. A network partition does not — the device keeps its session, stays "connected", and
simply stops sending data. The only signal is a gap in the time series. This is precisely the
class of failure that a naive simulator can never surface, and the alerting design must include
a **staleness check** (no data for N intervals → alert) alongside the offline check.

Scenarios are driven by a declarative config so they can be scripted per test run:

```yaml
scenarios:
  - name: string_underperformance
    start: "2026-09-25T13:00:00-07:00"
    duration: 45m
    target: STR-07
    effect: { scale_dc_power: 0.55 }
```

## 6. Time control

Real-time by default. A `--speed` factor multiplies elapsed time, so `--speed 60` runs a full
diurnal cycle in about 24 seconds.

This is not a toy feature. Waiting 12 hours to see a power curve is the single biggest
iteration-speed problem in this kind of work, and it means most people never validate the
night-time behaviour of their system at all. Accelerated time also compresses seasonal
behaviour: `--speed 3600 --days 7` covers a week in seven seconds, which is how seasonal
trends get sanity-checked.

Telemetry timestamps always reflect **simulated** time, not wall-clock time, so accelerated
runs produce a coherent, correctly-spaced series in InfluxDB.

## 7. Acceptance criteria

The simulator is done when all of these hold:

1. **Noon power on a clear summer solstice day** reaches clipping on at least one inverter.
2. **Output is exactly zero** outside daylight hours, with no negative values at any point.
3. **A clear day's power curve is unimodal** — one peak, no spurious secondary maxima.
4. **PR on a clear day** lands in a plausible band (0.80–0.92 for this configuration). If PR
   exceeds ~0.95 something is wrong — usually an efficiency or loss coefficient set to 1.0.
5. **PR is materially lower on a hazy day** than a clear one, and the drop is visible without
   reading numbers off the chart.
6. **String imbalance** is under 5 % in normal operation and exceeds 20 % during the
   underperformance scenario.
7. **An inverter going offline** is reflected in the UI within one publish interval plus
   debounce, via the Last Will.
8. **A comms-loss scenario** is detected by the staleness rule, not missed.
9. **The full pipeline** — simulator → EMQX → Telegraf → InfluxDB → Grafana — shows the same
   numbers at each hop, verified by a test that compares them numerically.
10. **No negative or NaN values** are ever written to any field.

Criteria 2, 3, 4 and 10 are the ones most likely to be quietly violated by a physics model
and are covered explicitly in the [Testing](./05-testing.md) document.
