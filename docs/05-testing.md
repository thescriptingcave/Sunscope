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

### 2.5 Full-year sweep (nightly)

`sim/tests/test_physics_sweep.py`, ten tests, ~90 s, scheduled daily at 06:17 UTC.

The tests above parameterise over five days — the equinoxes, the solstices and a shoulder
day. That is the right sample for a fast gate and the wrong sample for confidence, because a
seasonal regression lives in the tails.

What the sweep adds that the single-day suite cannot:

| Assertion | Why it needs a year |
|---|---|
| PR monthly means in 0.78–0.92 | Individual clear-day samples range 0.72–0.93 with cell temperature; only the *mean* is tight enough to catch a drifting loss coefficient |
| Yield strictly unimodal about the solstices | Pins the phase of the seasonal term exactly. A declination sign error or a date slip breaks monotonicity and survives every other test |
| Day length between December and June, equinoxes in between | Cheapest detector for a time-step refactor that would leave a fixed 12-hour day |
| Peak noon GHI bounded 400–1200 W/m² | Catches declination applied twice, or latitude used as declination |
| Every invariant across all 17,520 ticks | A NaN appearing only on the winter solstice would poison weeks of stored data |

Two of its own first-draft assertions were wrong and are recorded in the module docstring,
because a sweep that fails on correct code is worse than no sweep:

- Pairing each month with the one six months away. March vs September is roughly fair; April
  vs October is not, and April legitimately out-produced October by 15 % — long days and a
  decent sun against short days and a low one. A calendar month is not a solar one. Replaced
  with strict unimodality, which is both correct and stronger.
- Comparing monthly *total* energy rather than mean daily yield, which let March's 31st day
  out-produce all of September.

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
| Schema creation | `influx-init.sh` produces the declared schema |
| Last Value Cache | `last_cache()` returns the most recent value per series |
| InfluxDB outage | Telegraf buffers; data recovered after restart |
| Live relay | `GET /api/live` streams frames from the MQTT subscription |
| Backup round-trip | Restore into a scratch database, assert row counts match |


There is deliberately **no retention test**, because retention is not implemented: InfluxDB 3
Core has no row-level delete and never reclaims disk, so "out-of-range data is gone" is not an
assertion this engine can satisfy. The constraint, and the only reclaim path that does work, are
documented in [Retention](./10-retention.md). What is tested instead is that a backup can be
restored — a backup that cannot be restored is not a backup, and the restore path (recreate the
schema from the manifest, rebuild line protocol from CSV) is the non-trivial half.

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

The SQL layer is tested in two separate ways, because "does the query run" and "is the query
correct" fail independently.

### 5.1 The builders: injection defence and dialect rules

`api/tests/test_sql.py`, 23 tests, no database required. These are the more important half:
the allowlists are the only thing between an HTTP client and an **admin-scoped** InfluxDB
token, because InfluxDB 3 Core has no permission-scoped database tokens. A builder that
concatenates a caller-supplied value into the SQL text is a token-disclosure bug, not a style
problem.

```python
def test_user_values_never_appear_in_the_sql_text():
    """A binding must be a parameter, never spliced into the statement."""
    query, params = sqlmod.build_series_query(table="inverter_telemetry", ...)
    assert "mojave" not in query.sql          # the site comes from params
    assert params["site"] == "mojave"
```

Also pinned: unknown table, metric, interval and dimension are all rejected before any SQL is
built; identifiers come only from the allowlists; bucketed queries use ordinal `GROUP BY`; the
`raw` interval emits no `date_bin`. Each of those is a DataFusion constraint that fails
subtly — it still returns data, just wrong.

### 5.2 Every documented query must execute

`docs/06-sql-examples.md` is the source of truth for SQL teaching material, and
[Retention](./10-retention.md) §1 is a standing reminder that this dialect rejects things
standard SQL accepts. A documented example that does not run is worse than no example, so:

- `scripts/export-sql.py` **generates** `docs/sql/*.sql` from the document. The 35 files are
  build output, never hand-edited.
- `export-sql.py --check` fails if they have drifted from the document.
- `export-sql.py --verify` executes every one of the 40 statements against the live database.
- `scripts/check-doc-sql.py` additionally runs **every** ```` ```sql ```` block in **every**
  document, so prose in another doc cannot drift either.

40 statements, 39 executable, 12 deliberately skipped (parameterised queries, and panel SQL
carrying Grafana macros that only Grafana expands). A green run is a hard gate in
`bootstrap.sh test`.

Semantic assertions sit on top in the document itself: the comments say what each query
should prove, not just that it parses.

## 5.3 Authentication and roles

`api/tests/test_users.py` (26 tests) and `api/tests/test_rbac.py` (9 tests). These are
small and they are the most important tests in the API, because the thing they protect is
the only one where a single mistake hands over the database.

The split is deliberate. `test_users.py` covers the store and the role algebra — hashing,
per-user salts, default-deny, a malformed file failing closed. `test_rbac.py` covers the
HTTP boundary against a **real** user file written to disk, because the property under
test is precisely that a password is compared against a stored digest; mocking the store
would test the mock.

What is pinned, and why each one is not obvious:

| Assertion | The mistake it prevents |
|---|---|
| A viewer gets **403**, not 401, on `/api/explore` | A 401 makes a correct client log the user out and re-authenticate forever against a policy it cannot change |
| A wrong password and an unknown user return **byte-identical** responses | A distinct error per failure mode is a username oracle |
| A token with **no** role claim is treated as `viewer` | Tokens minted before roles existed have no such claim; defaulting to admin hands the raw-SQL surface to every token already in the wild |
| The store cannot be bypassed with `API_ADMIN_PASSWORD` while a user file exists | Otherwise rotating a digest cannot actually lock anyone out |
| `PRODUCTION_ITERATIONS >= 600_000`, **and** that 600k iterations is measurably slow | Lowering it is the quiet way to make an offline crack cheap. Asserting the constant alone would pass if someone replaced the hash function with a `sleep` |
| The legacy-token test signs its JWT **by hand** | Today's `issue_token` always sets the claim, so the code under test cannot produce the token the test is about |
| The CLI refuses to append to a file with no `users:` key | It yields a file that refuses to load. Loud, but a command that only works against a file it made itself is a trap for whoever is handed the printed hint |
| The CLI's `users:` check is a **regex**, not a prefix test | Found by running the documented command against a real generated file: every such file opens with a comment header, so a prefix test rejects all of them |

One test encodes a documented trade rather than an ideal: a role change applies at the
*next login*, not immediately, because the role is a JWT claim. Re-reading the user file
per request would make a demotion instant at the cost of a file read per API call. Pinning
it means changing the trade is a deliberate act rather than a drive-by.

## 6. Frontend and E2E

There is **no frontend unit-test framework**. `web/` has no Vitest, no React Testing Library
and no jsdom; adding one for a thin view layer would mostly assert implementation details.
What exists instead is two headless-Chromium checks that render the real thing and assert on
the rendered DOM:

| Check | Script | Asserts |
|---|---|---|
| PWA | `scripts/browser/check-ui.js` | Login renders; live feed connects; KPI tiles, inverter cards, chart series and string cells are present; **no console errors and no failed requests**; desktop and 375 px phone viewports |
| Grafana | `scripts/browser/check-grafana.js` | Both dashboards render; no panel shows a query error; no panel is empty (see below for the one exemption); the `$inverter` template variable expands to the full fleet |

Both exit non-zero on failure and both run in `bootstrap.sh test` and in CI.

Rendering is the assertion rather than a proxy for it. A green query API is not sufficient:
the datasource can return frames that Grafana still fails to plot, and a template variable can
fail to expand while every query reports success. Both bugs shipped here and were caught only
by looking at the rendered page.

The check also asserts the thing that is hardest to spot by hand and was in fact broken during
development: **a silent live-feed failure looks identical to a working dashboard showing stale
data.** The PWA check therefore requires the live banner to read `Live`, not merely for the
page to render.

The live relay itself is covered at the unit level in `api/tests/test_live_socket.py`, which
pins the properties that make a ticket worth having: single use, and a 30-second life.

### 6.1 How the PWA check decides the dashboard has data

`check-ui.js` has to answer one question before it can assert anything: *has data arrived
yet?* Getting that wrong in either direction is a bad time — too eager and it asserts
against an empty page, too patient and it fails a healthy system.

**Power cannot answer it.** The site-power tile is legitimately `0 W` whenever the simulator
is at night in its own timeline, and that timeline is not the wall clock: the simulator
starts at solar noon by default and free-runs, with `--speed` to accelerate and `--realtime`
to opt into wall-clock time. CI has run at 23:15 UTC with the simulator logging `12:31
GHI=854` — sun up, in simulation time. So no calculation over the host clock can tell 0 W
from no data. An earlier attempt did exactly that, computing daylight from the site's own
latitude and longitude, and it was wrong twice over: it waived the requirement when the sun
was genuinely up in simulation time, and it accepted the empty page's pre-data `0 W`
immediately, after which the chart assertion failed on a dashboard that simply had not been
given data yet. That attempt is reverted; this section is the replacement.

**The signal is the `Inverters online` tile.** It reads `N / 4`, which can only appear once
real device rows have reached the API. An empty dashboard renders it as `--`. That separates
a populated dashboard from an empty one at any hour, without needing to know the time.

Two details that are easy to get wrong and fail *silently*:

- The tile is located by **label text**, not by a class name. The tiles are `tile`,
  `tile-accent` and `tile-${tone}`; there is no per-metric class, so a selector such as
  `.tile-online` matches nothing and the wait can never be satisfied. A bad selector here
  looks exactly like a dead feed.
- The wait requires `N > 0`. Zero inverters online would be a legitimate fleet-wide outage,
  which is a different failure and should be reported as one.

### 6.2 One Grafana panel is allowed to be empty

`check-grafana.js` fails any panel that renders "no data", because a query that executes
and returns nothing is exactly the failure mode this check exists to catch. That rule is
right for almost every panel and wrong for one: **"Events by severity and source (CTE)"**
reads the event log, and an event log for a healthy plant with no faults in the window is
*supposed* to be empty. CI hit exactly that — a fresh database whose 24 h backfill happened
to contain no fault events, so the panel was correct and the check failed.

It is an explicit list of exact panel titles, not a heuristic, and the distinction it draws
is narrow:

| | Exempt panel | Every other panel |
|---|---|---|
| Shows a **query error** | **fails** | fails |
| Shows **no data** | not asserted | fails |

An error is a defect for every panel, exempt or not. Only "no rows" is waived. Matching on
title rather than on a keyword is deliberate: a heuristic would silently exempt the next
panel someone adds with "events" in its name, including one that is genuinely broken.

The exemption is printed in the check's output (`drawn (may be empty; not asserted)`), so a
green run never implies a panel was held to the no-data bar when it was not. A bare skip
would be invisible, and an invisible skip is worse than no exemption at all.

This is the same shape of mistake as the site-power tile in §6.1: an assertion on a quantity
whose value is legitimately "nothing" at some times. Two so far, in two different checks.

### 6.3 The PWA check logs what it fetched

Each `/api` response is logged with its status and shape:

```
200 /api/series?table=inverter_telemetry&metric=ac_power_w&interval=1h&... -> 4304B
200 /api/now -> devices=4
```

This exists because "no errors, no failed requests" is true and useless on its own. A
request that *succeeds with an empty body* is a passing check and an empty chart, and
nothing in the output said which request came back empty. When a failure like that is
genuinely unexplained, this log is the difference between one line of evidence and a
guess.

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

## 9. Verification scripts

Eight checks that are neither unit tests nor browser tests. Each one catches a class of failure
the other suites structurally cannot, and each is a hard gate in `bootstrap.sh test`.

| Script | Asserts | Why the rest cannot |
|---|---|---|
| `check-pwa-contract.py` | Every endpoint the PWA calls, called exactly as the browser calls it, with the real JWT flow | A contract drift between two codebases that each pass their own tests |
| `check-doc-sql.py` | Every ```` ```sql ```` block in **every** document executes | Docs are not compiled, so nothing else notices a query that stopped working |
| `check-live-ws.py` | EMQX's WebSocket listener carries real MQTT frames | Telegraf uses plain TCP on 1883, so no other component exercises the listener |
| `check-exposure.py` | Nothing is reachable off-loopback, and the security model holds; `--test` additionally starts a throwaway instance and probes it from a real LAN address | Every other test connects over loopback, which is exactly where the original raw-SQL guard was wrong — see [04-security §4.4a](04-security.md) |
| `gen-postman.py --check` | The Postman collection matches the live OpenAPI schema | A request for an endpoint that no longer exists is still a *valid* Postman request, so nothing complains until a reader runs it and gets a 404 |
| `export-sql.py --check` | `docs/sql/*.sql` matches the document it was generated from | Generated files drift silently |
| `export-sql.py --verify` | All 40 documented statements execute against the live database | See §5.2 |
| `backup.py` round-trip | A backup restores into a scratch database with matching row counts | A backup that cannot be restored is not a backup |

`check-live-ws.py` deserves a note, because its scope changed. It used to be "the browser's
path", when the dashboard connected straight to the broker. It no longer is — the browser goes
through the API relay — so it now checks the transport *underneath* the relay, and says so.
Leaving the old description in place would have pointed the next person at an architecture that
no longer exists.

### Running them individually

```bash
uv run --project api python scripts/check-pwa-contract.py
uv run --project api python scripts/check-doc-sql.py
uv run --project api --with paho-mqtt python scripts/check-live-ws.py
uv run python scripts/export-sql.py --check
```

## 10. CI pipeline

Two jobs, in `.github/workflows/ci.yml`. Triggers on every push to `main` and on
every pull request.

```yaml
jobs:
  python:                       # no services needed
    - ruff check sim/ api/ scripts/*.py
    - tsc --noEmit (via npm run build)
    - pytest sim                # 65
    - pytest api --ignore=test_live.py   # 191
  integration:                  # brings up the whole stack
    - ./scripts/bootstrap.sh up
    - pytest api                # 202, including the live-dialect tests
    - check-pwa-contract.py, check-doc-sql.py, check-live-ws.py
    - export-sql.py --check and --verify
    - gen-postman.py --check    # the collection matches the live OpenAPI schema
    - check-exposure.py         # nothing reachable off-loopback; --test also probes a LAN address
    - check-ui.js, check-grafana.js      # headless browser
    - backup.py --backup then --restore into a scratch database
```

Two jobs rather than one because a genuine code failure should not be masked by
a broken container. Only `test_live.py` is excluded from the unit job: it talks to
a real InfluxDB and has no skip guard, so it would fail rather than skip.

**Every command runs from its own project directory.** `--project` selects the uv
environment but does *not* change the working directory, and `[tool.pytest.ini_options]`
lives in each `pyproject.toml` — so a command run from the repository root
collects both `sim/tests` and `api/tests` and cannot import either package. This
cost three failed runs before it was found.

`LOGIN_RATE_LIMIT` is raised for the run: the browser checks perform real logins
and the API rate-limits logins to 10 per 5 minutes, which several checks in one run
would otherwise trip — and the symptom is a login rejection, which reads like a
real failure.

### What CI caught that local testing never could

All of these passed on macOS and failed on the runner:

| Bug | Symptom on Linux | Why macOS hid it |
|---|---|---|
| Secrets at mode 600 | InfluxDB would not start: `Permission denied` | Docker Desktop is lenient about ownership across the VM boundary; containers run as uid 1500 |
| Token written under a timestamped filename | 11 live tests 401 | Nothing looks for `api-read-<epoch>.token`, so the API fell back to a bogus token |
| Tests needing a developer's `.env` | 55 errors, `SystemExit(1)` | The lifespan calls `get_settings()` directly, so `dependency_overrides` cannot reach it |

The general lesson: **a green local suite is evidence about one machine.** Anything
that depends on uid, file modes, or a file that happens to exist on your disk is
untested until CI runs it.

### Not implemented

- **Nightly physics sweep.** No scheduled workflow exists. The intent — check the
  invariants across a full simulated year, because a seasonal regression should not
  reach a release — is sound and is the most valuable test still missing.
- **ESLint.** Not configured. `tsc --noEmit` is the only frontend type check.
- **Coverage measurement.** No tooling, so §10's targets are aspirations, not
  measurements.

## 11. Coverage

| Area | Target | Status |
|---|---|---|
| Physics modules | ≥ 95 % | aspiration, not measured |
| MQTT publisher | ≥ 90 % | aspiration, not measured |
| Alert rules | ≥ 95 % | aspiration, not measured |
| SQL builders | ≥ 90 % | aspiration, not measured |
| React components | ≥ 70 % | **no tooling** — `web/` has no unit-test framework |
| Rendered UIs | 2 checks, both headless | **implemented**: `check-ui.js`, `check-grafana.js` |

**These are targets, not measurements.** No coverage tool is wired up, so nothing here is
enforced. What is enforced, and what has actually caught bugs: 267 tests, six verification
scripts, two headless render checks, a docs/SQL consistency gate, and a backup round-trip —
all run by `./scripts/bootstrap.sh test` and by CI on every push.

Coverage is a floor, not a goal. The invariant tests and the PVWatts cross-validation are worth
more than any coverage percentage, because they check the *numbers* rather than the *lines*.
