# Solar Farm Simulator

A 1 MWac desert PV plant, simulated with [pvlib](https://pvlib-python.readthedocs.io/),
publishing telemetry over MQTT, stored in InfluxDB 3 Core, and surfaced through a
FastAPI backend and an installable React PWA — with threshold and staleness
alerting that catches faults you inject yourself.

```
pvlib simulator ──MQTT──▶ EMQX ──┬──▶ Telegraf ──▶ InfluxDB 3 ──▶ FastAPI ──▶ PWA
                                 │                              ▲
                                 └────────WebSocket──────────────┘
                                  (live tiles, straight to the browser)
```

---

## Prerequisites

You need four things. The bootstrap script checks for them and stops with a
clear message if one is missing, rather than failing three steps later.

| Tool | Why | Install |
|---|---|---|
| Docker + Compose v2 | runs 5 of the 6 components | [Docker Desktop](https://docs.docker.com/desktop/) (Mac/Windows) or [Docker Engine](https://docs.docker.com/engine/install/) + the compose plugin (Linux) |
| [uv](https://docs.astral.sh/uv/) | Python env and runner for the simulator | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js 20+ | builds the PWA | [nodejs.org](https://nodejs.org/) — or `brew install node` |
| Python 3.12+ | the helper scripts | usually already present; `brew install python@3.12` |

Roughly 4 GB of RAM for the containers, and about 2 GB of disk for images.

## Quick start

```bash
git clone <this-repo> && cd iot-telemetry
./scripts/bootstrap.sh up
```

One command. From a clean checkout it checks prerequisites, generates secrets
and a TLS certificate, builds the PWA, starts the stack, waits for it to be
healthy, and launches the simulator. First run takes a few minutes while Docker
images build; later runs take seconds.

`make up` does the same thing if you prefer.

Then open **http://127.0.0.1:8000/** and sign in:

- username `admin`
- password: the `API_ADMIN_PASSWORD` line in `.env` — `grep API_ADMIN_PASSWORD .env`

<details>
<summary>Driving the pieces yourself instead</summary>

```bash
./scripts/gen-secrets.sh          # tokens, .env (gitignored), secrets/
./scripts/gen-tls-cert.sh         # self-signed cert; InfluxDB will not start without it
(cd web && npm install && npm run build)
docker compose up -d              # influx-init runs automatically, before Telegraf
(cd sim && uv run solar-sim)
```

The ordering is not arbitrary. InfluxDB is given `--tls-cert` on the command
line, so the certificate must exist before the container starts. The API mounts
`web/dist`, so the PWA must be built first. And `influx-init` must finish before
Telegraf writes anything, because InfluxDB 3 tag definitions are immutable once
a table exists — the schema has to be created explicitly rather than inferred
from the first write. Compose enforces the container ordering; the script
enforces the rest.

</details>

### What you should see

If `up` worked, all of this is true:

```bash
./scripts/bootstrap.sh status
```

- five containers **healthy** (`api`, `emqx`, `grafana`, `influxdb`, `telegraf`)
- the simulator **running**, logging to `sim.log`
- the dashboard loads and the banner reads **Live** rather than "Connecting"
- four inverter cards, each around 250 kW at solar noon

No browser? `uv run --project api --with paho-mqtt python scripts/watch.py`
renders the same data in the terminal — see [Watch it live](#watch-it-live-in-a-terminal).

### Commands

| Command | Effect |
|---|---|
| `./scripts/bootstrap.sh up` | Bring everything up. Idempotent — safe to re-run |
| `./scripts/bootstrap.sh status` | What is running, and every endpoint |
| `./scripts/bootstrap.sh test` | All test suites plus four verification scripts |
| `./scripts/bootstrap.sh sim:stop` | Stop the simulator (the stack keeps running) |
| `./scripts/bootstrap.sh down` | Stop everything, keep the database |
| `./scripts/bootstrap.sh reset` | Destroy the database, secrets and TLS material |

`up` will not clobber existing secrets — `gen-secrets.sh` only fills values still
set to a `change-me` placeholder — and it never touches the database.

---

## Services

| Service | URL | Purpose |
|---|---|---|
| **Dashboard** | http://127.0.0.1:8000 | The PWA, served same-origin by the API |
| API docs | http://127.0.0.1:8000/docs | Interactive OpenAPI |
| EMQX console | http://127.0.0.1:18083 | Inspect live topics and retained messages |
| MQTT (TCP) | `127.0.0.1:1883` | Simulator publishes here |
| MQTT (WebSocket) | `ws://127.0.0.1:8083/mqtt` | The PWA subscribes here for live tiles |
| InfluxDB 3 | https://127.0.0.1:8181 | SQL only — Flux is not supported on 3.x. Self-signed TLS |
| Grafana | http://127.0.0.1:3000 | Dashboards (datasource blocked, see below) |

Every port is bound to `127.0.0.1`, so nothing is reachable from the network.
Credentials live in `.env` (gitignored, mode 600); token files are in `secrets/`
(also gitignored).

## Repository layout

```
docs/                 design documentation (9 documents)
sim/                  Python simulator        (host-run, 65 tests)
api/                  FastAPI backend         (in compose, 146 tests)
api/config/alerts.yaml declarative alert rules
web/                  React PWA              (built to web/dist, served by the API at /)
telegraf/             MQTT -> InfluxDB config
grafana/provisioning/ datasource + dashboard provisioning
scripts/              bootstrap, gen-secrets, gen-tls-cert, influx-init,
                      telegraf-entrypoint, check-*, inject-fault, watch
scripts/browser/      check-ui: headless browser render of the PWA
Makefile              thin wrapper over scripts/bootstrap.sh
```

Only the simulator runs on the host; the other five components run in Docker.

## Build order

| # | Component | State |
|---|---|---|
| 1 | Scaffold: compose, healthchecks, pinned images | DONE |
| 2 | InfluxDB schema: 5 tables + Last Value Cache, created before first write | DONE |
| 3 | Ingest: Telegraf `mqtt_consumer` → InfluxDB, verified end to end | DONE |
| 4 | Simulator: pvlib physics, fault scenarios, per-inverter MQTT client with LWT | DONE (65 tests) |
| 5 | FastAPI: auth, `/api/now` off the LVC, `/api/series`, `/api/explore` | DONE (146 tests) |
| 6 | PWA: live tiles over MQTT/WebSocket | DONE |
| 7 | Alerting: threshold + staleness rules, in-app feed, event persistence | DONE |
| 8 | Grafana dashboards | BLOCKED — see below |

### The Grafana blocker

Grafana's InfluxDB datasource queries over Flight SQL (gRPC), which requires TLS
and which Grafana will not send a `database` header for. Reproduced on 12.2.0 and
12.4.0. TLS and certificate trust are solved; the header is not. The fix is one
manual step in the UI, which writes something the provisioning API does not:

```
http://127.0.0.1:3000/connections/datasources/edit/influxdb3-solar
```

Full findings, including everything tried, are in
[docs/grafana-influxdb-notes.md](./docs/grafana-influxdb-notes.md). The PWA and
`/api/explore` cover the exploration role meanwhile, over plain HTTPS with no gRPC.

## Running the simulator

The simulator runs on the host, because that is where iteration happens.

```bash
cd sim && uv sync

uv run solar-sim                      # backfills 24 h, then starts at solar noon
uv run solar-sim --backfill 0         # start with an empty database
uv run solar-sim --speed 30           # 30× faster than real time
uv run solar-sim --realtime           # wall-clock time instead
uv run solar-sim --clear-sky          # no cloud model, useful for comparison
uv run solar-sim --scenarios config/scenarios/demo.yaml   # with faults
```

**Backfill.** On startup the simulator generates 24 h of history at 5-minute
resolution before entering the live loop. Without it a fresh database holds ten
minutes of data, the "last 24 hours" chart collapses to a single dot per
inverter, and daily yield reads in kilowatt-hours rather than megawatt-hours. A
real site has history when you connect to it. Pass `--backfill 0` to skip it.

History is stepped *forward*, never rewound — the cloud model is AR(1) and the
inverters carry thermal state, so the farm arrives at the start time already
warmed up. Events are not emitted during backfill, or the feed would open on
hundreds of identical clipping entries.

Then query it:

```bash
TOK=$(python3 -c "import json;print(json.load(open('secrets/admin-token'))['token'])")
docker compose exec influxdb influxdb3 query \
  --host https://localhost:8181 --tls-no-verify --token "$TOK" --database solar \
  "SELECT time, inverter_id, ac_power_w, clipping FROM inverter_telemetry ORDER BY time DESC LIMIT 8"
```

## The dashboard

The PWA lives in `web/`. The API serves the built bundle at `/`, so the whole app
is a single origin — no CORS in production, one port to expose, and the service
worker scope covers everything.

```bash
cd web && npm install && npm run build
docker compose up -d api
open http://127.0.0.1:8000/
```

For hot reload, run Vite on :5173 instead. It proxies `/api` to :8000, so it is
still one origin as far as the browser is concerned:

```bash
cd web && npm run dev
```

Two data paths, and the split is deliberate:

| Data | Path | Why |
|---|---|---|
| Live tiles | browser → `ws://localhost:8083/mqtt` | sub-second, and stays up when the API does not |
| Cold load, history, events | browser → `/api/now`, `/api/series`, `/api/strings`, `/api/events` | the Last Value Cache answers in ms instead of aggregating Parquet |

Live tiles come straight from the broker. That works because `localhost` is a
secure context; **it will not work over HTTPS**, where the browser blocks a `ws://`
connection as mixed content. Moving the fan-out server-side is the fix, and
`web/src/mqtt/live.ts` is the seam where that swap belongs. See
[docs/04-security.md](./docs/04-security.md).

`mqtt.js` is loaded with a dynamic `import()`, holding the initial bundle to
**53 kB gzipped** instead of 160 kB — first paint does not wait on the broker client.

## Alerting

The API subscribes to MQTT and evaluates 12 rules, declared in
[`api/config/alerts.yaml`](./api/config/alerts.yaml). Two of them are
**staleness** rules: they fire when data stops arriving, which is the only way to
catch a network partition. A device that holds its MQTT session open never trips
a Last Will, so a system built only on status messages reports all-clear through
an outage.

```bash
curl -s localhost:8000/api/alert-stats -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Two endpoints, deliberately distinct: `/api/alerts` is the live in-memory view and
works even when InfluxDB is unreachable; `/api/events` is the persisted history.
Full rationale, rule reference and verified behaviour in
[docs/07-alerting.md](./docs/07-alerting.md).

Watch `/api/alert-stats` → `errors`. A rising count means the engine is running
but something inside the loop is failing — a different problem from
`engine_connected` being false, and much easier to miss.

### Break something and watch it get noticed

```bash
./scripts/bootstrap.sh sim:stop      # the injector needs sole control of the broker
uv run --project api --with paho-mqtt python scripts/inject-fault.py list
uv run --project api --with paho-mqtt python scripts/inject-fault.py comms-loss --duration 200
# in a second terminal:
docker compose logs -f api | grep ALERT
```

`inject-fault.py` feeds every inverter except the one under test, so a scenario
isolates a single device instead of silencing the fleet. It restores healthy
readings when it finishes, so the alerts clear on their own.

**Stop the real simulator first.** If it is running it keeps publishing for the
device you are trying to break, and the fault never appears. That is also true of
`comms-loss` conceptually: you cannot make a device go silent while something else
is speaking for it.

| Scenario | What it does | What you should see |
|---|---|---|
| `overheat` | INV-02 at 96 °C | `heatsink_critical` at +60 s, then `heatsink_high` at +90 s |
| `hot-weather` | INV-01 at 84 °C | `heatsink_high` only — proves the critical threshold is separate |
| `dead-inverter` | INV-03 online, status 3, 0 W | `power_zero_in_sunlight` — invisible to any status-code rule |
| `comms-loss` | INV-04 simply stops | `telemetry_stale` after 120 s, with no Last Will |

`comms-loss` is the one worth running. It is the failure a Last Will cannot
detect, and the reason the staleness rules exist.

### Watch it live in a terminal

If you would rather not open a browser — over SSH, on a machine with no GUI, or
just to keep an eye on things while doing something else:

```bash
uv run --project api --with paho-mqtt python scripts/watch.py
```

```
  SOLAR FARM — mojave   1.0 MWac / 1.19 MWp
  live over MQTT/WebSocket, updated 0s ago

  SITE
    power         1000.0 kW  ████████████████████████████
    perf.ratio     0.836  ███████████████████████······
    yield         236.2 kWh today
    online        4 inverters, 12 strings

  WEATHER
    GHI           783.6 W/m²  ████████████████████········
    air            25.3 °C      wind 2.5 m/s

  INVERTERS
    INV-01       250.0 kW ██████████████████ eff  89.8%   25.9°C  CLIP
    INV-02       180.0 kW █████████████····· eff  83.3%   96.0°C  3
    INV-03       250.0 kW ██████████████████ eff  89.8%   25.9°C  CLIP
    INV-04       250.0 kW ██████████████████ eff  89.8%   25.9°C  CLIP

  ALERTS
    ● CRITICAL heatsink_critical  INV-02  value=96.0 threshold=90.0
    ● WARNING heatsink_high  INV-02  value=96.0 threshold=70.0
```

`--once` renders a single frame and exits, which is handy in a script.

## Verify it yourself

```bash
./scripts/bootstrap.sh test
```

211 tests, lint, type checking, and four verification scripts — including one
that loads the PWA in headless Chromium and fails if the live feed does not come
up. It writes screenshots to `shots/`.

| Check | Catches | Has caught |
|---|---|---|
| `check-pwa-contract` | wrong types, impossible numbers, a dead alert engine | a 132 % capacity factor from a unit error in the simulator |
| `check-live-ws` | a broken broker WebSocket path | — (the path had no coverage at all) |
| `check-doc-sql` | docs that have drifted from the database | a query teaching a bug; a column copied from an unrelated project |
| `check-ui` | anything only a real browser can see | the live feed throwing `mqttModule.connect is not a function` while all 208 tests passed |

`check-ui` earns its place. It logs in for real, waits for MQTT data to land in
the tiles, and fails if the feed never reports Live. Type checking could not
catch the bug it found: Vite bundles mqtt.js to a chunk exporting only
`default`, so `mod.connect` is undefined in the browser even though
`typeof import('mqtt')` declares it.

It performs a real login, and the API rate-limits logins to 10 per 5 minutes, so
repeated local runs will need `LOGIN_RATE_LIMIT` raised or a pause between them.

## Troubleshooting

**`bootstrap.sh up` says a port is in use, or a container will not start.**
Something else holds one of the loopback ports. `lsof -nP -iTCP:8000 -sTCP:LISTEN`
finds it. Every port is bound to `127.0.0.1`, so it cannot be a network service.

**`docker is installed but not running`.** Start Docker Desktop and re-run.

**The dashboard banner says "Connecting" and every tile is 0 W.**
The live feed is not up. `./scripts/bootstrap.sh status` shows whether the
simulator is running; start it with `sim:start`. If the banner says *Live* but
tiles are still zero, the simulator is publishing into a different simulated
time range than the API's window expects — see the sim-clock note under Gotchas.

**Login returns "rate limit exceeded".** The API allows 10 login attempts per
5 minutes. Wait, or set `LOGIN_RATE_LIMIT` in `.env` higher.

**`up` completes but the dashboard 404s on `/`.** The PWA was not built.
`cd web && npm install && npm run build`, then `docker compose restart api`.

**Queries return nothing.** The simulator starts at solar noon, so before it has
run for a while there is no data. Check `tail -5 sim.log`.

**Alert rules show 12 but `/api/alerts` is empty.** That is correct on a healthy
farm. Inject a fault to see them fire.

## Gotchas

Non-obvious behaviours, all verified by running them. Full list in
[docs/02-architecture.md §6.1](./docs/02-architecture.md#61-verified-platform-behaviours-worth-knowing).

- The `influxdb` image has no `python3` and no `jq`.
- `create token --admin --offline` mints a token the server rejects (401).
- InfluxDB 3 Core 3.11 cannot create permission-scoped tokens; all tokens are admin.
- Line protocol writes go to `/api/v3/write_lp`. `/api/v3/write` 404s on 3.11.
- A point's primary key is (measurement, tags, timestamp), so two alerts on one
  device resolving in the same evaluation collide unless they differ by tag.
- `MIN`/`MAX` over a time window measures the **day/night cycle**, not the spread
  between devices. String imbalance must be computed per timestamp; see
  [docs/06-sql-examples.md I5](./docs/06-sql-examples.md).
- `pd.date_range(when, ...)` on an aware timestamp followed by `tz_localize`
  raises. Use `tz_convert`, and scan one local calendar day rather than 24 h
  forward from `when`.
- **Every simulator run started with defaults begins at solar noon of the same
  day**, so two runs write identical simulated timestamps and collide. Old rows
  can look "newest" by wall clock while being hours stale. Pass an explicit
  `--start` for a run that should be distinguishable.
- Scenario times are `start_offset:` (relative to the sim start), never absolute
  dates. An absolute date silently stops firing the day after it is written.
- `docker compose down -v` wipes the InfluxDB volume but not `secrets/`; init
  detects and regenerates stale tokens.

## Design documentation

| # | Document | Purpose |
|---|---|---|
| 1 | [Design](./docs/01-design.md) | Domain model, PV physics, site topology, fault scenarios |
| 2 | [Architecture](./docs/02-architecture.md) | Components, choices, port map, **§6.1 verified platform behaviours** |
| 3 | [Data Flow](./docs/03-data-flow.md) | MQTT topics, JSON payloads, InfluxDB schema, latency budget |
| 4 | [Security](./docs/04-security.md) | Threat model, per-tier controls, the MQTT-WebSocket exposure cliff |
| 5 | [Testing](./docs/05-testing.md) | Test strategy, pyramid, contract tests, SQL regression |
| 6 | [SQL Examples](./docs/06-sql-examples.md) | Beginner → Expert InfluxDB SQL, all 35 executed against the live database |
| 7 | [Alerting](./docs/07-alerting.md) | Rules, debounce and hysteresis, the staleness check |
| — | [Grafana notes](./docs/grafana-influxdb-notes.md) | Everything tried on the Flight SQL blocker |
