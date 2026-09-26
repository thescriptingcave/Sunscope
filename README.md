# Solar Farm Simulator

Physics-based solar farm simulator with a real-time web/mobile dashboard.

**Python (pvlib) → MQTT (EMQX) → Telegraf → InfluxDB 3 Core → Grafana + React PWA + FastAPI**

Full design documentation is in [`docs/`](./docs/).

## Quick start

```bash
./scripts/bootstrap.sh up
```

That is the whole thing. From a clean checkout it checks prerequisites, generates secrets and
a TLS certificate, builds the PWA, starts the stack, waits for it to be healthy, and launches
the simulator. First run takes a few minutes while images build; later runs are fast.

Then open **http://127.0.0.1:8000/** and sign in as `admin` with the `API_ADMIN_PASSWORD`
value from `.env`.

`make up` does the same thing if you prefer. The full verb set:

| Command | Effect |
|---|---|
| `./scripts/bootstrap.sh up` | Bring everything up. Idempotent — safe to re-run |
| `./scripts/bootstrap.sh status` | What is running, and every endpoint |
| `./scripts/bootstrap.sh test` | All test suites plus the three verification scripts |
| `./scripts/bootstrap.sh sim:stop` | Stop the simulator (the stack keeps running) |
| `./scripts/bootstrap.sh down` | Stop everything, keep the database |
| `./scripts/bootstrap.sh reset` | Destroy the database, secrets and TLS material |

`up` will not clobber existing secrets — `gen-secrets.sh` only fills values still set to a
`change-me` placeholder — and it never touches the database.

<details>
<summary>The manual equivalent, if you would rather drive the pieces yourself</summary>

```bash
./scripts/gen-secrets.sh          # tokens, .env (gitignored), secrets/
./scripts/gen-tls-cert.sh         # self-signed cert; InfluxDB will not start without it
(cd web && npm install && npm run build)
docker compose up -d              # influx-init runs automatically, before Telegraf
(cd sim && uv run solar-sim)
```

The ordering is not arbitrary. InfluxDB is given `--tls-cert` on the command line, so the
certificate must exist before the container starts. The API mounts `web/dist`, so the PWA
must be built first. And `influx-init` must finish before Telegraf writes anything, because
InfluxDB 3 tag definitions are immutable once a table exists — the schema has to be created
explicitly rather than inferred from the first write. Compose enforces the container
ordering; the script enforces the rest.

</details>

### Services

| Service | URL | Purpose |
|---|---|---|
| Dashboard | http://127.0.0.1:8000 | The PWA, served same-origin by the API |
| EMQX console | http://127.0.0.1:18083 | Inspect live topics and retained messages |
| MQTT (TCP) | `127.0.0.1:1883` | Simulator publishes here |
| MQTT (WebSocket) | `ws://127.0.0.1:8083/mqtt` | The PWA subscribes here for live tiles |
| InfluxDB 3 | https://127.0.0.1:8181 | SQL only — Flux is not supported on 3.x. Self-signed TLS |
| Grafana | http://127.0.0.1:3000 | Dashboards (datasource blocked, see docs/grafana-influxdb-notes.md) |
| API | http://127.0.0.1:8000 | Backend. OpenAPI docs at `/docs` |

Credentials are in `.env` (gitignored, mode 600). `secrets/` holds token files and is also
gitignored.

### Send a test message by hand

```bash
docker run --rm --network solar-sim_default eclipse-mosquitto:2 \
  mosquitto_pub -h emqx -q 0 \
  -t 'solar/mojave/block/BLK-A/inverter/INV-01/telemetry' \
  -m '{"ts":"2026-09-25T12:01:00Z","site":"mojave","block":"BLK-A",
       "inverter_id":"INV-01","model":"SG250CX","ac_power_w":248913.4,
       "efficiency":0.8552,"uptime_s":287412,"clipping":true}'
```

Then query it:

```bash
docker compose exec influxdb influxdb3 query \
  --host http://localhost:8181 --token "$(python3 -c "import json;print(json.load(open('secrets/admin-token'))['token'])")" \
  --database solar \
  "SELECT time, inverter_id, ac_power_w, uptime_s, clipping FROM inverter_telemetry"
```

> The MQTT payload repeats the identity that the topic already carries. This is deliberate: see
> [docs/03-data-flow.md](./docs/03-data-flow.md) for why `topic_parsing` is not used.

## Repository layout

```
docs/                 design documentation (9 documents)
sim/                  Python simulator        (host-run, 62 tests)
api/                  FastAPI backend         (in compose, 146 tests)
api/config/alerts.yaml declarative alert rules
web/                  React PWA              (built to web/dist, served by the API at /)
telegraf/             MQTT -> InfluxDB config
grafana/provisioning/ datasource + dashboard provisioning
scripts/              bootstrap, gen-secrets, gen-tls-cert, influx-init, telegraf-entrypoint,
                      check-pwa-contract, check-live-ws, check-doc-sql, inject-fault,
                      watch (live terminal dashboard)
scripts/browser/      check-ui: headless browser render of the PWA
Makefile              thin wrapper over scripts/bootstrap.sh
```

## Build order

| # | Component | State |
|---|---|---|
| 1 | Scaffold: compose, healthchecks, pinned images | DONE |
| 2 | InfluxDB schema: 5 tables + Last Value Cache, created before first write | DONE |
| 3 | Ingest: Telegraf `mqtt_consumer` -> InfluxDB, verified end to end | DONE |
| 4 | Simulator: pvlib physics, fault scenarios, per-inverter MQTT client with LWT | DONE (38 tests) |
| 5 | FastAPI: auth, `/api/now` off the LVC, `/api/series`, `/api/explore` | DONE (145 tests) |
| 6 | PWA: live tiles over MQTT/WebSocket | DONE |
| 7 | Alerting: threshold + staleness rules, in-app feed, event persistence | DONE |
| 8 | Grafana dashboards | BLOCKED - see below |

### The Grafana blocker

Grafana's InfluxDB datasource queries over Flight SQL (gRPC), which requires TLS
and which Grafana will not send a `database` header for. Reproduced on 12.2.0 and
12.4.0. TLS and certificate trust are solved; the header is not. The fix is one
manual step in the UI, which writes something the provisioning API does not:

```
http://127.0.0.1:3000/connections/datasources/edit/influxdb3-solar
```

Full findings, including everything tried, are in
[docs/grafana-influxdb-notes.md](./docs/grafana-influxdb-notes.md). `/api/explore`
covers the exploration role meanwhile, over plain HTTPS with no gRPC.

## Services

| Service | URL | Purpose |
|---|---|---|
| EMQX console | http://127.0.0.1:18083 | Inspect live topics and retained messages |
| MQTT (TCP) | `127.0.0.1:1883` | Simulator publishes here |
| MQTT (WebSocket) | `ws://127.0.0.1:8083/mqtt` | The PWA subscribes here for live tiles |
| InfluxDB 3 | http://127.0.0.1:8181 | SQL only — Flux is not supported on 3.x |
| Grafana | http://127.0.0.1:3000 | Dashboards (datasource blocked, see docs/grafana-influxdb-notes.md) |
| API | http://127.0.0.1:8000 | Backend. OpenAPI docs at `/docs` |
| API docs | http://127.0.0.1:8000/docs | Interactive OpenAPI |

Credentials are in `.env` (gitignored, mode 600). `secrets/` holds token files and is also
gitignored.

### Send a test message by hand

```bash
docker run --rm --network solar-sim_default eclipse-mosquitto:2 \
  mosquitto_pub -h emqx -q 0 \
  -t 'solar/mojave/block/BLK-A/inverter/INV-01/telemetry' \
  -m '{"ts":"2026-09-25T12:01:00Z","site":"mojave","block":"BLK-A",
       "inverter_id":"INV-01","model":"SG250CX","ac_power_w":248913.4,
       "efficiency":0.8552,"uptime_s":287412,"clipping":true}'
```

Then query it:

```bash
docker compose exec influxdb influxdb3 query \
  --host http://localhost:8181 --token "$(python3 -c "import json;print(json.load(open('secrets/admin-token'))['token'])")" \
  --database solar \
  "SELECT time, inverter_id, ac_power_w, uptime_s, clipping FROM inverter_telemetry"
```

> The MQTT payload repeats the identity that the topic already carries. This is deliberate: see
> [docs/03-data-flow.md](./docs/03-data-flow.md) for why `topic_parsing` is not used.

## Repository layout

```
docs/                 design documentation (7 documents)
sim/                  Python simulator        (not built yet)
api/                  FastAPI backend         (in compose, 74 tests)
web/                  React PWA              (not built yet)
telegraf/             MQTT -> InfluxDB config
grafana/provisioning/ datasource + dashboard provisioning
scripts/              gen-secrets, influx-init, telegraf-entrypoint
```

## Build order

1. DONE **Scaffold** - compose, healthchecks, pinned images, schema init
2. DONE **InfluxDB schema** - 5 tables + Last Value Cache, created before first write
3. DONE **Ingest** - Telegraf `mqtt_consumer` -> InfluxDB, verified end to end
4. DONE **Simulator** - pvlib physics, fault scenarios, per-inverter MQTT client with LWT, 36 tests
5. TODO **Grafana dashboards** - datasource is provisioned; dashboards not written yet
6. TODO **FastAPI** - auth, `/api/now`, `/api/series`
7. TODO **PWA** - live tiles over MQTT/WebSocket
8. TODO **Alerting** - threshold rules + in-app feed

## Services

Everything comes up with `docker compose up -d`. The **simulator** is the one
component that runs on the host, because that is where iteration happens.

| Component | Where | Notes |
|---|---|---|
| EMQX, InfluxDB, Telegraf, Grafana, **API** | Docker Compose | source bind-mounted for the API, so it reloads on edit |
| Simulator | host, via `uv run` | fast physics iteration |
| API (alternative) | host, via `uv run solar-api` | escape hatch for working on the API alone |

## Run the simulator

```bash
cd sim && uv sync

uv run solar-sim                      # starts at solar noon, so data is visible at once
uv run solar-sim --speed 60           # a full day in 24 minutes
uv run solar-sim --clear-sky --steps 3 --start 2026-09-25T12:00:00
uv run solar-sim --scenarios config/scenarios/demo.yaml
```

Then query it:

```bash
TOK=$(python3 -c "import json;print(json.load(open('secrets/admin-token'))['token'])")
docker compose exec influxdb influxdb3 query --host http://localhost:8181 --token "$TOK" \
  --database solar \
  "SELECT time, inverter_id, ac_power_w, clipping FROM inverter_telemetry ORDER BY time DESC LIMIT 8"
```

Tests: `cd sim && uv run pytest` (37 tests) and `uv run ruff check src/ tests/`.

## The dashboard

The PWA lives in `web/`. The API serves the built bundle at `/`, so the whole app is a
single origin — no CORS in production, one port to expose, and the service worker scope
covers everything.

```bash
cd web && npm install && npm run build   # -> web/dist, mounted into the API container
docker compose up -d api
open http://127.0.0.1:8000/              # sign in as admin
```

For HMR, run Vite on :5173 instead. It proxies `/api` to :8000, so there is still one
origin as far as the browser is concerned:

```bash
cd web && npm run dev
```

Two data paths, and the split is deliberate:

| Data | Path | Why |
|---|---|---|
| Live tiles | browser → `ws://localhost:8083/mqtt` | sub-second, and stays up when the API does not |
| Cold load, history, events | browser → `/api/now`, `/api/series`, `/api/strings`, `/api/events` | Last Value Cache answers in ms instead of aggregating Parquet |

Live tiles come straight from the broker. This works because `localhost` is a secure context;
**it will not work over HTTPS**, where the browser blocks a `ws://` connection as mixed
content. Moving the fan-out server-side is the fix, and `web/src/mqtt/live.ts` is the seam
where that swap belongs. See [docs/04-security.md](./docs/04-security.md).

`mqtt.js` is loaded via dynamic `import()`, which keeps the initial bundle at **52 kB
gzipped** instead of 158 kB — the first paint does not wait on the broker client.

### Alerting

The API subscribes to MQTT and evaluates 12 rules, declared in
[`api/config/alerts.yaml`](./api/config/alerts.yaml). Two of them are
**staleness** rules: they fire when data stops arriving, which is the only way to
catch a network partition. A device that holds its MQTT session open never
trips a Last Will, so a system built only on status messages reports all-clear
through an outage.

```bash
cd api && uv run solar-api          # or via compose; the engine starts with it
curl -s localhost:8000/api/alert-stats   -H "Authorization: Bearer $TOKEN"
```

Two endpoints, deliberately distinct: `/api/alerts` is the live in-memory view
and works even when InfluxDB is unreachable; `/api/events` is the persisted
history. Full rationale, rule reference and verified behaviour in
[docs/07-alerting.md](./docs/07-alerting.md).

Watch `/api/alert-stats` → `errors`. A rising count means the engine is running
but something inside the loop is failing — a different problem from
`engine_connected` being false, and much easier to miss.

### Verify it yourself

```bash
# 1. Automated checks (all should pass)
uv run --project api python scripts/check-pwa-contract.py
uv run --project api --with paho-mqtt python scripts/check-live-ws.py
uv run --project api python scripts/check-doc-sql.py
(cd sim && uv run pytest -q) && (cd api && uv run pytest -q)

# 2. Break something and watch it get noticed
pkill -9 -f solar-sim        # the injector needs sole control of the broker
uv run --project api --with paho-mqtt python scripts/inject-fault.py list
uv run --project api --with paho-mqtt python scripts/inject-fault.py comms-loss --duration 200
# in another terminal, watch the alerts arrive:
docker compose logs -f api | grep ALERT
```

`inject-fault.py` feeds every inverter except the one under test, so a scenario
isolates a single device instead of silencing the fleet. It restores healthy
readings when it finishes, so the alerts clear on their own.

**Stop the real simulator first.** If it is running it keeps publishing for the
device you are trying to break, and the fault never appears. That is also true
of `comms-loss` conceptually: you cannot make a device go silent while something
else is speaking for it.

| Scenario | What it does | What you should see |
|---|---|---|
| `overheat` | INV-02 at 96 °C | `heatsink_critical` at +60 s, then `heatsink_high` at +90 s |
| `hot-weather` | INV-01 at 84 °C | `heatsink_high` only — proves the critical threshold is separate |
| `dead-inverter` | INV-03 online, status 3, 0 W | `power_zero_in_sunlight` — invisible to any status-code rule |
| `comms-loss` | INV-04 simply stops | `telemetry_stale` after 120 s, with no Last Will |

`comms-loss` is the one worth running. It is the failure that a Last Will cannot
detect, and the reason the staleness rules exist.

### Watch it live in a terminal

If you would rather not open a browser — over SSH, on a machine with no GUI, or
just to keep an eye on things while doing something else — there is a terminal
dashboard. It subscribes to the same MQTT/WebSocket path the PWA uses, so it
shows the live feed rather than a database read.

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

### Verify the dashboard's view of the data

```bash
./scripts/bootstrap.sh test          # all of the below, in order
```

| Script | Catches | Has caught |
|---|---|---|
| `check-pwa-contract` | wrong types, impossible numbers, unrenderable timestamps, a dead alert engine | a 132 % capacity factor from a unit error in the simulator |
| `check-live-ws` | a broken broker WebSocket path | — (path had no coverage at all) |
| `check-doc-sql` | docs that have drifted from the database | a query teaching a bug; a column copied from an unrelated project |
| `check-ui` | anything that only a real browser can see | the live feed throwing `mqttModule.connect is not a function` while all 208 tests passed |

`check-ui` earns its place. It logs in for real, waits for MQTT data to land in
the tiles, and fails if the feed never reports Live. Type checking could not
catch the bug it found: Vite bundles mqtt.js to a chunk exporting only
`default`, so `mod.connect` is undefined in the browser even though
`typeof import('mqtt')` declares it. The dashboard rendered perfectly and was
completely dead. It also writes screenshots to `shots/`.

```bash
npm --prefix scripts/browser install   # once
node scripts/browser/check-ui.js       # desktop + phone viewports
```

It is part of `./scripts/bootstrap.sh test`, so it runs in CI. Note that it
performs a real login, and the API rate-limits logins to 10 per 5 minutes —
repeated local runs will need `LOGIN_RATE_LIMIT` raised or a pause between them.

## Gotchas

Non-obvious behaviours that were verified by running them. Full list in
[docs/02-architecture.md §6.1](./docs/02-architecture.md#61-verified-platform-behaviours-worth-knowing).

- The `influxdb` image has no `python3` and no `jq`.
- `create token --admin --offline` mints a token the server rejects (401).
- InfluxDB 3 Core 3.11 cannot create permission-scoped tokens; all tokens are admin.
- Grafana needs `newInfluxDSConfigPageDesign` and the query language set to **SQL**.
- The Grafana "Product" dropdown has no Core option; choose "InfluxDB Enterprise 3.x".
- `docker compose down -v` wipes the InfluxDB volume but not `secrets/`; init detects and
  regenerates stale tokens.
- `MIN`/`MAX` over a time window measures the **day/night cycle**, not the spread between
  devices. String imbalance must be computed per timestamp; see
  [docs/06-sql-examples.md I5](./docs/06-sql-examples.md).
- `pd.date_range(when, ...)` on an aware timestamp followed by `tz_localize` raises. Use
  `tz_convert`, and scan one local calendar day rather than 24 h forward from `when`.
- **Every simulator run started with defaults begins at solar noon of the same day**, so two
  runs write identical simulated timestamps and collide. Two consequences: old rows can look
  "newest" by wall clock while being hours stale, and duplicate points appear at the same
  `(tags, timestamp)`. Pass an explicit `--start` for a run that should be distinguishable.
- Scenario times are `start_offset:` (relative to the sim start), never absolute dates. An
  absolute date silently stops firing the day after it is written — which is what `demo.yaml`
  did, and the whole feature was dead because of it.

