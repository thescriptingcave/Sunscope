# 05 — Testing

## 1. Strategy

The physics model is the part most likely to be subtly, silently wrong. A dashboard that renders
plausible-looking curves from a broken model is worse than no dashboard, because it looks
right. So testing effort is weighted toward **numeric correctness of the physics**, with the
usual UI-test-as-substitute-for-real-testing anti-pattern explicitly avoided.

Layers, by weight:

| Layer | Share | What it protects | Runs on |
|---|---|---|---|
| **Physics unit** | ~40 % | The model is correct | Every commit |
| **Contract / schema** | ~20 % | Sim, broker, Telegraf, DB agree | Every commit |
| **Integration** | ~20 % | The pipeline actually moves data | Every commit |
| **SQL regression** | ~10 % | Queries stay correct as schema evolves | Every commit |
| **E2E / UI** | ~5 % | The user-visible path works | Every commit (headless) |
| **Load / soak** | ~5 % | It holds up under stress | Nightly / pre-release |

## 2. Physics validation

The core question: **is the number right?** Three independent approaches, because any single one
can be fooled.

### 2.1 Analytic invariants

Properties that must hold for *any* correct PV model, checked over a full simulated year:

| # | Invariant | Assertion | Catches |
|---|---|---|---|
| I1 | Night is zero | `ac_power_w == 0` whenever solar zenith > 90° | Twilight chatter, negative values |
| I2 | No negatives | Every field ≥ 0 at every timestep | Sign errors, bad loss coefficients |
| I3 | No NaN/Inf | `math.isfinite()` on all outputs | Division by zero at night |
| I4 | Unimodal | Power curve has exactly one local maximum per day | Double-counting, phase errors |
| I5 | Monotonic in irradiance | `dP/dPOA > 0` for fixed temperature | Inverted or clipped logic |
| I6 | Bounded by rating | `ac_power_w <= rated_w + ε` | Missing clip |
| I7 | Clip ⇒ near rating | `clipping == true` ⟹ `ac_power_w ≈ rated_w` | Clip flag desync |
| I8 | String sum ≈ inverter DC | `Σ string_dc ≈ inverter_dc` within 2 % | Double-counting DC |
| I9 | Energy conservation | `∫P dt` matches Σ of samples | Integration errors |
| I10 | PR in band | 0.75 ≤ PR ≤ 0.95 on clear days | Coefficient mistakes |

I1–I3 and I10 are the highest-value checks. A PR above 0.95 almost always means an efficiency
coefficient or a loss factor is set to 1.0, which is easy to do by accident and invisible
without an explicit bound.

### 2.2 Cross-validation against an independent model

Run the same site and weather through both our pvlib model **and** the NREL **PVWatts** model
(`pvlib.pvwatts`), then compare.

Acceptance: hourly AC power within **±5 %** of PVWatts on clear days, and PR values agreeing
within 0.03.

Two implementations deriving from the same physical inputs but different code paths agreeing
is strong evidence. A single implementation compared against itself proves nothing.

### 2.3 Published reference cases

Spot-check against known figures:

| Case | Expected | Tolerance |
|---|---|---|
| Clear summer solstice noon, 36°N, 30° tilt | POA 950–1000 W/m² | ±5 % |
| Same, winter solstice noon | POA 780–830 W/m² | ±5 % |
| Cell temp rise at 1000 W/m², still air, 25 °C ambient | +30 °C | ±4 °C |
| Clear-day PR, this configuration | 0.80–0.92 | ±0.03 |
| Inverter peak efficiency | 0.98–0.99 at 25–30 % load | ±0.01 |

### 2.4 Test implementation

```python
@pytest.mark.parametrize("day", pd.date_range("2026-01-01", "2026-12-31", freq="D"))
def test_night_is_exactly_zero(day, site):
    """I1 — output must be identically zero outside daylight."""
    times = pd.date_range(day, periods=1440, freq="1min")
    for inverter in site.inverters:
        series = [inverter.ac_power_w(t, site) for t in times]
        for t, p in zip(times, series):
            if site.solar_position(t).zenith > 90.0:
                assert p == 0.0, f"{inverter.id} produced {p}W at night ({t})"


def test_pr_in_plausible_band(site, clear_day):
    """I10 — the single most effective sanity check on loss coefficients."""
    pr = site.performance_ratio(clear_day)
    assert 0.75 <= pr <= 0.95, f"PR={pr:.3f} out of band — check loss coefficients"
```

Parameterising over a full year rather than a handful of days is deliberate: seasonal
edge cases and polar-style low-sun conditions hide in the tails.

## 3. MQTT contract tests

The simulator and the database must agree on names and types, and tag immutability means a
mismatch discovered at runtime is expensive.

### 3.1 Schema contract

Single source of truth in `sim/src/solar_sim/metrics.py`, asserted against three consumers:

| Assertion | Catches |
|---|---|
| Every topic parses to a known measurement | Renamed topics |
| Every tag is in the declared tag set | Telegraf emitting an extra tag |
| Every field name/type matches the InfluxDB schema | Type drift |
| No new tags without a schema migration | **Tag immutability violations** |
| Payload is valid JSON with a parseable `ts` | Malformed publishes |

The fourth assertion deserves emphasis. Because tag definitions are immutable in InfluxDB 3,
a single message carrying an unexpected tag can permanently corrupt a table. A test that
fails on schema drift is cheap insurance.

### 3.2 Publishing behaviour

| Test | Assertion |
|---|---|
| QoS and retain flags | Telemetry QoS 0 / retain false; status QoS 1 / retain true |
| Publish interval | Within configured tolerance of the target |
| LWT registered | Status topic carries the offline will at connect |
| Retained messages | A new subscriber receives current state without waiting |
| No publish when offline | Fault scenario stops the device cleanly |

### 3.3 Offline detection

```python
def test_inverter_offline_triggers_lwt(emqx, subscriber):
    """T3 — power loss must be visible within one keepalive cycle."""
    sub = subscribe(emqx, "solar/+/block/+/inverter/+/status")
    sim = Simulator(topology=one_inverter_site())
    sim.start()
    wait_for_message(sub, state="producing")

    sim.kill_inverter("INV-01", power_loss=True)

    msg = wait_for_message(sub, state="offline", timeout=60)
    assert msg is not None
    assert elapsed() < keepalive * 1.5 + 5
```

And the counterpart — comms loss must **not** trip the LWT, and must instead be caught by the
staleness rule:

```python
def test_comms_loss_not_trip_lwt_but_trips_staleness(...):
    sim = Simulator(...); sim.start()
    time.sleep(60)
    sim.kill_inverter("INV-01", power_loss=False)   # network partition
    time.sleep(90)

    assert no_message_received(state="offline")     # LWT correctly silent
    assert staleness_alert_fired("INV-01")          # staleness rule catches it
```

This pair of tests encodes the most important behavioural distinction in the whole system.

## 4. Integration tests

Real components in Docker, real wire protocols — no mocks below the network boundary.

| Test | Scope |
|---|---|
| End-to-end tick | Simulator → EMQX → Telegraf → InfluxDB, then query it back |
| Latency bound | Publish → queryable in InfluxDB within 30 s |
| Data fidelity | Values at each hop are numerically identical |
| Schema creation | `init-influx.sh` produces the declared schema |
| Last Value Cache | `last_cache()` returns the most recent value per series |
| InfluxDB outage | Telegraf buffers; data recovered after restart |
| Retention | Out-of-range data is gone |

Data fidelity is the one that catches mapping bugs:

```python
def test_values_survive_pipeline(published, queried):
    """Every numeric field must match to within float32 precision."""
    for field in NUMERIC_FIELDS:
        for p, q in zip(published[field], queried[field]):
            assert math.isclose(p, q, rel_tol=1e-6)
```

`rel_tol=1e-6` rather than exact equality: InfluxDB stores `float64` but Parquet and Arrow
round-trips are not bit-identical, and demanding exact equality produces a test that fails for
reasons unrelated to correctness.

## 5. SQL regression tests

Every query in [06 — SQL Examples](./06-sql-examples.md) is a test. DataFusion's dialect differs
from standard SQL in ways that only surface at runtime, and a documented example that does not
run is worse than no example.

```python
@pytest.mark.parametrize("query_id", [
    "beginner_01", "beginner_02",      # ...
    "advanced_03", "expert_05",
])
def test_documented_query_runs(query_id, seeded_db):
    sql = load_sql(f"docs/examples/{query_id}.sql")
    result = execute(sql, database="solar_test")
    assert result is not None
```

Plus semantic assertions, because a query that returns no rows passes an execution test
while being completely wrong:

| Test | Assertion |
|---|---|
| LAG correctness | `lag` output equals the previous row's value |
| Running total | Monotonically non-decreasing |
| Moving average | Matches a hand-computed NumPy result |
| `RANK` vs `ROW_NUMBER` vs `DENSE_RANK` | Differ correctly on ties |
| Frame bounds | `ROWS 3 PRECEDING` averages exactly 4 rows |
| Time bucketing | `date_bin` bins align and are non-overlapping |
| Null handling | `count` excludes NULLs when filtered |
| Round-trip | CTE result equals the equivalent subquery form |

The moving-average test is the most valuable — off-by-one errors in window frames are the
single most common window-function bug, and they produce numbers that look reasonable.

```python
def test_moving_average_matches_numpy(seeded_db):
    sql = """
    SELECT time, ac_power_w,
           AVG(ac_power_w) OVER (ORDER BY time
                                 ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS ma
    FROM inverter_telemetry WHERE inverter_id = 'INV-01' ORDER BY time
    """
    got = dataframe_from_sql(sql)
    expected = got["ac_power_w"].rolling(4, min_periods=1).mean()
    pd.testing.assert_series_equal(got["ma"], expected, check_names=False)
```

## 6. Frontend and E2E

| Test | Tool | Scope |
|---|---|---|
| Component | Vitest + React Testing Library | Tiles, heatmap, chart data transforms |
| Hooks | Vitest | MQTT reconnect, subscription lifecycle, offline handling |
| E2E | Playwright | Login → live tile updates → history chart → alert acknowledged |
| Responsive | Playwright | 375 px, 768 px, 1440 px viewports |
| Offline | Playwright | Broker unreachable → reconnect banner, then recovery |

Deliberately light. The frontend is a thin view over two data sources, and a large UI test
suite would mostly be asserting implementation details. The one behaviour genuinely worth an E2E
test is **MQTT reconnect**, because a silent reconnect failure looks identical to a working
dashboard showing stale data — and that failure is very hard to spot by hand.

## 7. Load and soak

### 7.1 Load

Simulate 60 days of data in compressed time and measure ingestion.

| Metric | Threshold |
|---|---|
| Sustained ingest rate | ≥ 50 k points/s |
| InfluxDB p99 write | < 250 ms |
| Query p99 (24 h range, 4 inverters) | < 1 s |
| Query p99 (30 d range) | < 5 s |
| Memory, 10 M points | < 2 GB |
| Telegraf buffer overflow | Zero under expected load |

Also verify **graceful degradation**: with the database down, the live path must stay
unaffected. If the PWA goes stale when InfluxDB is unavailable, the two-path architecture in
Architecture §5.2 has been broken.

### 7.2 Soak

24 hours, `--speed 3600`, with faults injected. Watch for:

- Memory growth in the simulator (unbounded caches are the usual culprit)
- Memory growth in EMQX and InfluxDB
- Clock drift between simulated and wall time
- Leaked MQTT sessions — client count must stay at 18
- Float precision drift in the AR(1) process over many iterations
- InfluxDB WAL growth without compaction

## 8. Fault scenario tests

Every scenario in [Design §5](./01-design.md#5-fault-scenarios) has an end-to-end test
asserting **detection**, not just that the fault applied.

| Scenario | Must be detected by | Test assertion |
|---|---|---|
| Inverter offline | LWT + status topic | `offline` within 1.5 × keepalive |
| Comms loss | Staleness rule | Alert after 3 × interval |
| String underperformance | Imbalance rule | Ratio > 20 % within 2 intervals |
| Sensor drift | PR deviation | PR drop detected, flagged as sensor not weather |
| Overheat | Temperature rule | Alert above 70 °C |
| Wildfire smoke | Sustained low PR | Alert after 30 min, not before |

The sensor-drift test is the subtle one: drift makes PR fall, which looks identical to bad
weather. The test asserts the alert is **attributed correctly** — comparing `clearness_index`
against an independent reference separates "the sky is hazy" from "the sensor is lying."

## 9. CI pipeline

```yaml
stages:
  - lint:       ruff, eslint, tsc --noEmit
  - unit:       pytest -m "not integration and not load"    # < 2 min
  - contract:   pytest -m contract                          # < 1 min
  - integration: pytest -m integration                      # < 8 min
  - sql:        pytest tests/test_sql_regression.py          # < 3 min
  - e2e:        playwright test                             # < 6 min
  - build:      docker build
  # nightly
  - load, soak, physics-sweep
```

**The physics sweep** runs nightly across a full simulated year, checking all ten invariants.
It is nightly because it is slow, and because a seasonal regression should never reach a
release unnoticed.

## 10. Coverage

| Area | Target | Rationale |
|---|---|---|
| Physics modules | ≥ 95 % | The core value; untested physics is worthless |
| MQTT publisher | ≥ 90 % | Failure modes are subtle |
| Alert rules | ≥ 95 % | A missed alert is a real miss |
| SQL builders | ≥ 90 % | Injection surface |
| React components | ≥ 70 % | Thin view layer |
| E2E | 8–12 critical paths | Not exhaustive |

Coverage is a floor, not a goal. The invariant tests and the PVWatts cross-validation are worth
more than any coverage percentage, because they check the *numbers* rather than the *lines*.
