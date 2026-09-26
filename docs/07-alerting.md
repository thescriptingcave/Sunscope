# Alerting

How faults become alerts, and why the design is shaped the way it is. Read
[01-design.md](./01-design.md) §fault scenarios first: this document is about
detecting the failures that document describes.

## 1. The problem

A monitoring system has one job that matters more than the rest: **not being
quiet when something is wrong.** Everything below follows from taking that
seriously, because the two ways this usually goes wrong are both quiet failures.

**Too sensitive.** Cloud ramps last one to four minutes. A rule that fires the
instant a threshold is crossed turns every passing cloud into an alert. An
operator who is trained to ignore alerts stops reading them, and then the one
that mattered is the one they skim past. An alert nobody trusts is worse than no
alerting, because it manufactures confidence.

**Too trusting.** The obvious way to know a device is down is to watch for its
Last Will. That works for power loss and crashes, and it silently does *not*
work for the failure that costs the most: a network partition, where the device
keeps its session open, stays "connected", and publishes nothing. There is no
message to react to. A system built only on status messages reports all-clear
through an outage.

The gap between those two is the whole design.

## 2. Shape

```
MQTT ──▶ AlertService ──▶ RuleEngine ──▶ active alerts ──▶ GET /api/alerts ──▶ PWA
            │                  │
            │                  └─▶ transitions ──▶ events table (InfluxDB)
            └─▶ injects weather ghi as rule context
```

Three modules, split along the line that makes the interesting part testable:

| Module | Responsibility | I/O |
|---|---|---|
| `alerts.py` | rules, conditions, the debounce/re-arm state machine | none |
| `alert_config.py` | read and validate `config/alerts.yaml` | file |
| `alert_service.py` | MQTT subscription, context injection, persistence | broker, database |

`alerts.py` takes readings and a clock and returns alerts. The clock is
injectable, so debounce and staleness are tested by advancing time directly
rather than by sleeping. That matters because a subtle bug in this state machine
is invisible on a healthy farm: a rule that never fires, a rule that fires once
per message, and a correct rule all look identical when nothing is wrong.

The API subscribes to MQTT itself rather than reusing the PWA's browser
connection. The browser's live path stops at the edge and depends on someone
having a dashboard open; alerting cannot depend on that.

## 3. Rules

Defined in [`api/config/alerts.yaml`](../api/config/alerts.yaml), so thresholds
can be tuned during commissioning without a code change. A malformed file is a
**hard startup failure** — an engine that silently loaded nothing would report
"all clear" forever, which is the most dangerous possible state for the component
whose entire job is to say something is wrong.

### Threshold rules

| id | scope | severity | condition | debounce |
|---|---|---|---|---|
| `heatsink_high` | inverter | warning | `heatsink_temp_c > 70` | 90 s |
| `heatsink_critical` | inverter | critical | `heatsink_temp_c > 90` | 60 s |
| `efficiency_low` | inverter | warning | `efficiency < 0.75` | 180 s |
| `power_zero_in_sunlight` | inverter | critical | `ac_power_w < 1000` | 60 s |
| `inverter_derating` | inverter | warning | `status_code == 4` | 60 s |
| `inverter_fault` | inverter | critical | `status_code == 5` | 30 s |
| `device_offline` | inverter | critical | `status_code == 0` | 15 s |
| `clipping_sustained` | inverter | info | `clipping == true` | 900 s |
| `performance_ratio_low` | site | warning | `pr_ratio < 0.70` | 300 s |
| `inverters_missing` | site | critical | `inverters_online < 4` | 60 s |

### Staleness rules

| id | scope | severity | fires when |
|---|---|---|---|
| `telemetry_stale` | inverter | critical | no telemetry for 120 s |
| `site_rollup_stale` | site | critical | no rollup for 180 s |

Staleness rules have no metric. They are evaluated by `RuleEngine.tick()` on a
10 s timer against elapsed wall time, and they are the only way to detect a
comms partition. `docs/01-design.md` calls this out as the case a naive
simulator can never surface; it is the reason this subsystem exists.

### Gating

A rule can carry `when:` conditions, ANDed with its main condition. They exist
to stop rules firing on data that is meaningless in context:

- `efficiency_low` requires `ac_power_w > 12.5 kW`. At night every inverter
  reads a meaningless efficiency, and without the gate the farm alarms itself at
  every dusk.
- `power_zero_in_sunlight` requires `ghi > 200 W/m²`. Zero output under a firm
  solar-noon reading is a fault; zero output at midnight is Tuesday.
- `performance_ratio_low` requires `ghi > 200` **and** `clipping < 0.5`. PR is
  meaningless when the output is limited by the inverter rather than the array.

`ghi` is not part of the telemetry payload. The service injects it from the
site's weather station before evaluation, so
`sim/src/solar_sim/metrics.py` stays the single source of truth for the wire
contract.

### `power_zero_in_sunlight` is the one worth reading twice

The failure a status code cannot express. The inverter is online, keeps
publishing, cheerfully reports `status_code: 3` (producing), and outputs
nothing. Every per-device rule that reads the status code says the device is
fine. Only cross-referencing the weather station reveals it.

## 4. Debounce and re-arm

Three distinct behaviours, all in `RuleEngine.observe()`.

**Debounce.** A condition must hold *continuously* for `debounce_s` before it
becomes an alert. Pending time is tracked, not assumed. Debounce lengths are
chosen from how transient each condition actually is: 15 s for a Last Will,
which is unambiguous and has nothing to debounce; 900 s for clipping, which on a
1.19 MWp DC / 1.0 MWac plant is normal every clear afternoon and would otherwise
alert daily.

Debounce is **time-based, not message-count-based**. A pending rule is promoted
in `tick()` as well as in `observe()`, so a device publishing less often than the
debounce is long still gets its alert.

**Re-arm.** An alert fires once, on the transition into firing, and stays firing
until the condition clears. At that point it emits exactly one `resolved` event
and re-arms. A condition that flaps across its threshold produces one alert and
one resolution per episode, never one per crossing.

Worth being precise about the vocabulary, because the name is a common
over-claim. This section was previously headed "debounce, hysteresis, re-arm",
and there is **no hysteresis here in the numeric sense**: no `clear_threshold`,
no separate clearing condition. A rule clears as soon as `matches()` stops
being true. The state machine does suppress flapping -- one alert and one
resolution per *episode* rather than one per crossing -- but that follows from
firing on transitions and re-arming, not from a deadband. If a future change
introduces a real clear threshold, this section is where it belongs.

**Cancellation.** A condition that clears during the debounce window never
alerted at all, and leaves no trace. A 40 s spike against a 90 s debounce is
invisible, which is the point.

## 5. Reading alerts

Two endpoints, and the distinction is deliberate.

`GET /api/alerts` is the **live** view, held in the engine's memory. It includes
alerts not yet flushed to storage, and it keeps working when InfluxDB is
unreachable. That is not a convenience: the staleness rules exist to notice that
data stopped arriving, so a feed that itself requires a successful database round
trip could not be the thing that reports the outage.

`GET /api/events` is the **historical** record, read back from the `events`
table. It survives a restart and is what Grafana charts.

`GET /api/alert-stats` exposes engine counters. `errors` is the number worth
watching: a rising count means the engine is running but something inside the
loop is failing, which is a different problem from `engine_connected` being
false and considerably easier to miss.

The PWA shows engine health next to the alert list, because a disconnected
engine is the failure that looks like good news — the panel goes empty and reads
as "all clear" when in fact nothing is watching. Silently blank is the one
unacceptable outcome.

## 6. Persistence

Every transition is written to the InfluxDB `events` table, sharing the table
and the `severity`/`source` tag columns with the simulator's own events, so one
query covers both writers. Resolutions are recorded at `info` severity with a
`_RESOLVED` code suffix, timestamped at `resolved_at`, which preserves the record
of how serious the fault had been and when it actually ended.

The `rule` tag is load-bearing, not decorative. See §7.

Writes go through a bounded queue drained by a background task, not a
`create_task` per alert. A bare task can be garbage collected before it runs,
silently dropping the alert, and an unbounded burst of tasks would let a slow
database build a backlog with no limit. When the queue is full the **oldest**
entry is dropped: the newest alert is the one an operator is waiting to see.

A write failure is counted and logged, never propagated. Losing the historical
record of one alert is bad; crashing the subscription loop loses every future
alert too. `dropped` in the stats makes the lossy case visible rather than
silent.

## 7. Verified behaviour

Confirmed against the running stack, not inferred.

| Scenario | Result |
|---|---|
| Healthy farm, clear sky | 0 active alerts, 0 errors, ~1500 readings evaluated |
| Clipping every clear afternoon | no alert — 900 s debounce, and it is expected behaviour |
| Injected 96 °C overheat, held 120 s | `heatsink_critical` at +60 s, `heatsink_high` at +90 s — each at its own debounce |
| Same overheat, then cooled | both rules resolve in the *same* evaluation, same nanosecond |
| Two same-instant resolutions | both persist — `rule` is a tag, so they are separate series |
| `pkill -9` on the simulator | `device_offline` on all 4 inverters within ~15 s (the Last Will *does* fire on an abrupt socket close) |
| Simulator silent for 120 s | `telemetry_stale` on all 4 inverters, `value` = measured silence age |
| Simulator restarts | all alerts resolve, resolutions persisted, feed returns to all-clear |
| `/api/v3/write` (wrong endpoint) | HTTP 404; the correct path is `/api/v3/write_lp` |

Two bugs in this table were found by running the scenarios, not by reading the
code, and both are worth knowing about because neither is visible without them:

**The `rule` tag.** A point's primary key is (measurement, tag set, timestamp).
Two rules on one device that resolve in the same evaluation share
`site`/`severity`/`source` *and* the nanosecond timestamp, so the second write
silently replaced the first and one resolution vanished from the history. The
fix is `rule` in the tag set. This is the kind of defect that looks fine for
weeks, because it only drops data — and it drops the *second* of any pair, which
is never the one being looked at.

**Resolution timestamps.** Resolutions were written with `fired_at`, so every
one landed at the moment its fault *started*. The history claimed faults cleared
the instant they were detected. Now written with `resolved_at`.

The `pkill -9` case is worth being precise about, because it is *not* a comms
partition: SIGKILL closes the TCP socket, the broker sees a dropped connection
and publishes the Last Will. A true partition — a firewall drop or a network
black hole, where the socket stays open — fires no Last Will at all. That is the
case `telemetry_stale` exists for, and it is the one the simulator's `COMMS_LOST`
scenario models.

## 8. Tuning

Thresholds are in `api/config/alerts.yaml`, mounted read-only into the API
container. Edit and restart the API; no rebuild.

`GET /api/alert-rules` returns the loaded set, so a change can be confirmed
without reading logs. Two rules are worth re-checking after any change to the
simulator's physics: `clipping_sustained` and `performance_ratio_low` both depend
on values the model produces, so a physics change can move them across their
thresholds.

To see the whole system react, run the simulator's fault scenarios:

```bash
cd sim && uv run solar-sim --scenarios config/scenarios/demo.yaml
```

## 9. Not implemented: Web Push

[Architecture §2.6](./02-architecture.md) originally scoped this component to
fire Web Push as well as maintain the in-app feed. **Web Push is not built**, and
the scope line above has been corrected rather than the feature quietly treated
as done.

It was cut for a reason worth recording. Push needs VAPID keypair management, a
`ServiceWorker` push handler, and a subscription store keyed per browser — and in
this single-operator, single-user setup it would notify exactly one person, who is
already looking at the dashboard. The in-app feed delivers the same information
to the same audience with none of that surface area.

If it is added later, the seams already exist: `AlertService._queue_write()` is
the single point every transition passes through, so a push dispatcher belongs
beside the InfluxDB writer rather than inside the engine. That placement is
deliberate — the engine stays free of I/O so its state machine stays testable.
