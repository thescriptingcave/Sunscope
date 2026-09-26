# 02 — Architecture

## 1. System overview

```
┌──────────────────┐
│   Simulator      │   Python 3.14 + pvlib
│   (runs on host) │   physics model, fault scenarios
└────────┬─────────┘
         │ MQTT publish (TCP 1883)
         │ JSON payloads, 10–60 s interval
         ▼
┌──────────────────┐
│   EMQX 5         │   MQTT broker
│   :1883  :8083 WS│   retained messages, Last Will
│   :18083 console │   topic auth (see Security doc)
└────────┬─────────┘
         │ subscribe
         ▼
┌──────────────────┐
│   Telegraf       │   inputs.mqtt_consumer (json_v2)
│   :8094 metrics  │   batching, retry, buffering
└────────┬─────────┘
         │ line protocol (HTTP)
         ▼
┌──────────────────┐
│   InfluxDB 3     │   Core, :8181
│   5 tables       │   Parquet storage, Last Value Cache
└────────┬─────────┘
         │ SQL (Flight SQL over HTTP/2)
         ├──────────────────────┐
         ▼                      ▼
┌──────────────────┐   ┌──────────────────┐
│   Grafana        │   │   FastAPI :8000  │   JWT auth
│   :3000          │   │   /api/now       │   alert rules
│   SQL datasource │   │   /api/series    │   MQTT fan-out
│   dashboards     │   │   /api/live      │
└──────────────────┘   └────────┬─────────┘
                                  │ REST + JWT
                                  ▼
                        ┌──────────────────┐
                        │   React PWA      │   live tiles
                        │   web + mobile   │   installable
                        └────────┬─────────┘
                                 │ MQTT over WebSocket
                                 └──────────────▶ EMQX :8083
```

## 2. Components

### 2.1 Simulator — Python, `uv`, runs on the host

The physics engine and MQTT publisher. Chosen to run on the host rather than in a container so
the edit/run loop stays fast; `uv run` gives near-instant startup and dependency resolution.

Dependencies: `pvlib` (solar geometry and PV models), `numpy`, `paho-mqtt` (MQTT 5 client),
`pydantic` (config validation), `PyYAML` (scenario files).

### 2.2 EMQX 5 — MQTT broker

Not Mosquitto. The deciding factor is the web console on `:18083`, which lets you inspect live
topics, retained messages, and client sessions while debugging. When the simulator is
misbehaving, being able to see exactly what is being published is worth more than Mosquitto's
lower resource footprint. EMQX also gives retained messages, per-client credentials, and a
Last Will mechanism, all of which the offline-detection design depends on.

**MQTT 5** protocol version, which gives proper reason codes, shared subscriptions, and message
expiry.

### 2.3 Telegraf — MQTT → InfluxDB bridge

Chosen over writing a bespoke bridge because it gives batching, retry with backoff, disk
buffering, and metric tracking for free. A hand-rolled bridge would need all of that
reinvented, badly, and would be one more thing to keep running.

- `inputs.mqtt_consumer` with the `json_v2` parser
- `outputs.influxdb_v2` pointed at the InfluxDB 3 Core v1/v2 compatibility write API
- `metric_batch_size` tuned so a tick from all 12 strings plus 4 inverters lands in one batch

Telegraf is the component that decouples the simulator from the database. If InfluxDB is down,
Telegraf buffers to disk and the simulator keeps publishing.

### 2.4 InfluxDB 3 Core — `influxdb:3.11-core`

The time series store. Pinned to an explicit version because `latest` now resolves to 3 Core,
and a floating tag in a compose file is how a stack breaks six months later.

- SQL is the query interface (DataFusion over Parquet)
- **Last Value Cache** for instant current-state reads
- **Tag immutability** is the main schema constraint — see §5

### 2.5 Grafana 12.2+ — engineering and ops surface

For deeper analysis than the PWA provides: fleet-wide trends, PR history, string comparison
heatmaps, cross-inverter correlation.

Configured with the **SQL** query language. Requires Grafana 12.2+ **and** the
`newInfluxDSConfigPageDesign` feature toggle. The datasource "Product" dropdown has no
"Core" entry — select "InfluxDB Enterprise 3.x" against a Core instance. That is documented
InfluxData behaviour, not a misconfiguration.

Datasource and dashboards are provisioned as code from `grafana/provisioning/`, so the whole
stack comes up with dashboards already present.

### 2.6 FastAPI — backend API

Four jobs:

1. **Auth** — JWT login, single user from env
2. **History** — `/api/series` runs parameterised SQL against InfluxDB
3. **Current state** — `/api/now` reads the Last Value Cache
4. **Alerting** — subscribes to MQTT, evaluates threshold and staleness rules, and maintains a
   live in-app alert feed. Web Push is not implemented; see [Alerting §9](./07-alerting.md)

The PWA gets **live** data directly from EMQX over WebSocket, not through this API. The API
handles history and alerting only. Routing live data through the backend would add a hop for no
benefit and make the backend a bottleneck for the one thing that must feel instant.

### 2.7 React PWA — web and mobile

One codebase, installable to iOS and Android home screens and usable in a desktop browser.
Subscribes to `ws://localhost:8083/mqtt` for sub-second live updates. Vite + React, with
`mqtt.js` for the WebSocket client.

## 3. Port map

| Port | Service | Exposure |
|---|---|---|
| 1883 | EMQX MQTT (TCP) | localhost only |
| 8083 | EMQX MQTT over WebSocket | localhost only — **see Security doc** |
| 18083 | EMQX console | localhost only |
| 8181 | InfluxDB 3 | localhost only |
| 8094 | Telegraf metrics | internal |
| 3000 | Grafana | localhost |
| 8000 | FastAPI | localhost |
| 5173 | PWA dev server (Vite) | localhost |

**HTTP/2 note.** Grafana's SQL datasource uses Flight SQL over gRPC, which requires HTTP/2.
Grafana talks directly to InfluxDB over the Compose network with no proxy in between, so this
is satisfied by default. If a reverse proxy is ever inserted in that path, it must be
configured for h2c or every SQL query will fail to connect. The InfluxQL query language uses
HTTP/1.1 and is unaffected — useful to know when debugging.

## 4. Repository layout

```
.
├── docker-compose.yml            EMQX, InfluxDB, Telegraf, Grafana, API
├── Makefile                      thin wrapper over scripts/bootstrap.sh
├── .env.example                  all configuration, no secrets
├── .github/workflows/ci.yml      tests, lint, verification, browser checks
├── docs/                         these documents
├── sim/                          Python simulator
│   ├── pyproject.toml
│   ├── config/scenarios/demo.yaml
│   └── src/solar_sim/
│       ├── main.py               orchestration loop, backfill, --speed
│       ├── solar.py              pvlib clear-sky / stochastic sky
│       ├── pv.py                 cell temperature, DC model, losses, clipping
│       ├── topology.py           site model: 4 inverters, 12 strings
│       ├── farm.py               per-tick assembly, PR, capacity factor
│       ├── weather.py            weather-station fields
│       ├── scenarios.py          fault injection
│       ├── metrics.py            shared measurement/tag definitions
│       └── mqtt_publisher.py     MQTT 5 client, LWT, retained messages
├── telegraf/telegraf.conf        mqtt_consumer -> outputs.influxdb_v2
├── scripts/
│   ├── bootstrap.sh              the entry point: up/down/reset/status/test/disk
│   ├── gen-secrets.sh            .env + secrets/admin-token
│   ├── gen-tls-cert.sh           self-signed cert + CA bundle
│   ├── influx-init.sh            database, 5 tables, Last Value Cache, tokens
│   ├── telegraf-entrypoint.sh
│   ├── backup.py                 backup, restore, retention window (--since)
│   ├── export-sql.py             generates docs/sql/ from docs/06
│   ├── check-doc-sql.py          every documented SQL block must execute
│   ├── check-pwa-contract.py     API <-> PWA contract
│   ├── check-live-ws.py          the live relay, end to end
│   ├── inject-fault.py           overheat, hot-weather, dead-inverter, ...
│   ├── watch.py                  live terminal dashboard
│   └── browser/
│       ├── check-ui.js           headless render + live-feed assertion
│       └── check-grafana.js      headless render of both dashboards
├── api/                          FastAPI
│   ├── pyproject.toml
│   ├── config/alerts.yaml        the 12 declarative rules
│   ├── tests/conftest.py         pins runtime config so tests need no .env
│   └── src/solar_api/
│       ├── main.py               app, all routes, lifespan, PWA mount
│       ├── config.py             Settings, validate_runtime
│       ├── auth.py               JWT, login rate limit
│       ├── influx.py             InfluxDB 3 client over HTTPS
│       ├── sql.py                every SQL builder + the allowlists
│       ├── alerts.py             rule engine (pure, injectable clock)
│       ├── alert_config.py       loads and validates alerts.yaml
│       ├── alert_service.py      MQTT subscriber, fan-out, persistence
│       └── live_tickets.py       single-use tickets for GET /api/live
├── web/                          React PWA
│   ├── package.json
│   ├── vite.config.ts
│   ├── public/sw.js              service worker
│   └── src/
│       ├── App.tsx               layout, login, polling
│       ├── api/client.ts         typed REST client
│       ├── mqtt/live.ts          the same-origin live relay
│       ├── hooks/index.ts        auth + polling hooks
│       ├── format.ts             units, status labels
│       └── components/           panels.tsx, LineChart, ConnectionBanner
└── grafana/
    ├── provisioning/datasources/influxdb.yaml
    ├── provisioning/dashboards/solar.yaml
    └── dashboards/               the two dashboard JSON files
```

## 5. Key architectural decisions

### 5.1 Schema-before-data, because InfluxDB 3 tag immutability

In InfluxDB 3 Core, **a table's tag column definitions are immutable once created**. The
primary key is the ordered set of tags plus time, and tag definitions cannot be altered
afterwards. If the simulator publishes first with the wrong tag set, the table is permanently
wrong and must be dropped and re-ingested.

Mitigation: `scripts/influx-init.sh` creates all five tables with the correct tag and field
definitions **before** the simulator's first publish. Compose healthchecks enforce this
ordering. The schema is a deliberate artefact, not something that emerges from the data.

This single constraint is the most likely thing to cause a frustrating debugging session, and
it is why schema definitions live in one place (`sim/src/solar_sim/metrics.py`) and are shared
by the simulator, the init script, and the tests.

> **Verified: recovering from a wrong tag set is harder than "drop the table".**
> `influxdb3 delete table` without `--hard-delete now` is a **no-op** — the table keeps its data
> and its tag set, and re-creating it then fails with `409 already exists`. The soft delete
> renames the table to `inverter_telemetry-20260925T231231` rather than removing it.
>
> The working recovery is:
>
> ```bash
> influxdb3 delete table -y --hard-delete now --database solar <name>
> ```
>
> after which the original name is free and the corrected schema can be created. The tombstones
> (`<name>-<timestamp>`) remain in `information_schema` until compaction; they are inert but
> noisy. If a table has accumulated real data, delete and re-ingest the database instead.
>
> This was hit for real during the build: `inputs.mqtt_consumer` defaults `topic_tag` to
> `"topic"`, which added a `topic` tag to `inverter_telemetry` on the very first write. Setting
> `topic_tag = ""` fixes the config, but the table had to be hard-deleted and rebuilt.

### 5.2 Two read paths, deliberately

| Path | Used for | Latency | Why |
|---|---|---|---|
| MQTT/WebSocket | live tiles | < 1 s | The broker already has the data. No reason to route through the DB. |
| SQL via FastAPI | history, analytics | 100 ms – s | Aggregations over time ranges the broker does not retain. |

An earlier instinct was to poll InfluxDB for everything. That couples live UI latency to
database write latency, makes the DB a single point of failure for the dashboard, and wastes
the broker that is already sitting there holding exactly the right messages.

### 5.3 The Last Value Cache

InfluxDB 3 Core's LVC keeps the last N values per series in memory, queried with
`SELECT * FROM last_cache('inverter_telemetry', 'inverter_current')`. This gives the PWA a
correct "current state" immediately on cold load, before any history query returns, instead of
waiting for an aggregate over the last few hours.

Caveats worth remembering: it is **in-memory**, so it is flushed when the server stops and
repopulated from historical data on restart; and both arguments must be **string literals**.

### 5.4 Grafana *and* a PWA, not one or the other

Grafana is excellent at exploratory analysis and poor as a product surface — it cannot do
push notifications, has no concept of an alert feed, and its navigation is designed for
engineers, not operators checking a phone at 6am. The PWA is the opposite.

Keeping both is justified because they serve different users at different moments, and Grafana
also serves as the debugging surface when something looks wrong in the PWA.

### 5.5 Why not Kafka

Kafka was considered and rejected for the transport. It is the stronger choice for event
replay, long retention, and high-throughput stream processing — but this system has none of
those requirements. Telemetry is ~1 kB messages at 10–60 s intervals across 17 devices.

MQTT wins on: message size, built-in retained messages, Last Will offline detection that maps
directly onto the domain, topic hierarchy that mirrors the site structure, and a browser-native
WebSocket path the PWA can consume directly. Kafka has no browser story at all, which would
have forced the PWA through the backend for live data — undoing §5.2.

If the design later needs replay or much higher throughput, the topic and payload structure is
deliberately kept clean enough to bridge into Kafka without changing consumers.

## 6. Failure modes and behaviour

| Failure | Behaviour | Detected by |
|---|---|---|
| InfluxDB down | Telegraf buffers in memory (`metric_buffer_limit`); simulator unaffected | Telegraf metrics, InfluxDB healthcheck |
| EMQX down | Simulator publish fails, retries with backoff | Publish errors, simulator log |
| Simulator down | LWT fires → all devices `offline` within keepalive × 1.5 | Status topics, staleness alert |
| Telegraf down | Data published but not stored; **messages are lost** | Telegraf healthcheck |
| Grafana down | No effect on ingestion or PWA | Independent |
| FastAPI down | Live tiles keep working (direct MQTT); history and alerts stop | Healthcheck |

The one genuinely lossy path is Telegraf: MQTT is transient and a stopped consumer misses
messages. There is **no disk buffer** in this stack — `outputs.influxdb_v2` exposes no
`buffer_limit` and Telegraf 1.36 ships no `outputs.disk` plugin, so `metric_buffer_limit` covers
only an InfluxDB outage while Telegraf stays running. If Telegraf restarts, buffered metrics are
gone. The Last Value Cache partially mitigates this for current-state reads, and the staleness
alert covers the resulting gap in history. This is an accepted trade for the operational
simplicity of not running a durable log.

### 6.1 Verified platform behaviours worth knowing

Everything in this section was confirmed by running it, not inferred from documentation. Each
one cost real debugging time and would cost the same to the next person.

| Behaviour | Consequence |
|---|---|
| InfluxDB offline admin token file must be JSON: `{"token": "apiv3_…", "name": "…", "expiration": null}`, mode 0600 | A bare token file fails to parse and the server will not start |
| Tokens must begin with `apiv3_` | Otherwise "Invalid token format" |
| `create token --admin --offline` produces a token the server does not accept | Must create online and parse stdout |
| InfluxDB 3 Core 3.11 cannot create permission-scoped tokens | See [Security §4.4](./04-security.md#44-influxdb-tokens-and-least-privilege-addresses-t2-t4) |
| `influxdb3 show databases` renders an ASCII table; the column is `iox::database` | `awk '{print $1}'` does not work for existence checks |
| `influxdb3 show system` rejects `--host`/`--token` | Detect Last Value Caches via `SELECT … FROM system.last_caches` |
| `/ping` requires auth | Healthcheck needs `--disable-authz=health,ping` |
| The `influxdb` image has no `python3` and no `jq` | Shell + awk only |
| Docker bind-mounts a file by inode | Regenerating by delete+recreate leaves containers reading the old inode; mount the directory |
| `delete table` is a soft delete and appears to do nothing | Use `-y --hard-delete now`; see §5.1 |
| `topic_parsing` length validation is unreliable in Telegraf 1.36 | Use `name_override` + payload tags instead |
| `inputs.mqtt_consumer` defaults `topic_tag = "topic"` | Set `topic_tag = ""`, or it adds a tag column |
| json_v2 field paths are required by default | `optional = true` per field, or one missing field drops the point |
| `omit_hostname`, not `omit_host` | Otherwise the default `host` tag is added |
| `MIN`/`MAX` over a time window measures the day/night cycle, not the spread between devices | A healthy 1 MW farm reported 23–26 % string imbalance and rendered every inverter as faulty. Spread must be computed per timestamp; see [SQL Examples I5](./06-sql-examples.md) |
| Partial time samples are indistinguishable from dead devices | Filter on `COUNT(DISTINCT string_id) >= expected` before scoring, or a dropped message becomes a false fault |
| `pd.date_range()` on an aware timestamp + `tz_localize` raises `TypeError` | Use `tz_convert`. Also scan one *local calendar day*, not 24 h forward from `when` — a forward range straddles two days and its daylight midpoint can land after sunset |
| `influxdb3 delete` has no row-level predicate | Only `database`/`table`/`token`/`trigger`. Design queries to be robust to orphan rows rather than planning to delete them |
| `mqtt.js` is ~107 kB gzipped and not needed for first paint | Dynamic `import()` holds the initial bundle to 52 kB gzipped |
| `localhost` is a secure context, so `ws://` is allowed from an `http://localhost` page | The same code breaks over HTTPS (mixed content). Moving the fan-out server-side is the fix, not `wss://` on a public broker |
| Line protocol writes go to `/api/v3/write_lp`, not `/api/v3/write` | The latter 404s in InfluxDB 3 Core 3.11. `/api/v2/write` exists but is the 2.x API and demands a bucket |
| A point's primary key is (measurement, tag set, timestamp) | Two alerts on one device resolving in the same evaluation collided and one was silently overwritten. Anything that must survive co-timestamped writes needs its own tag — see [Alerting §7](./07-alerting.md) |
| aiomqtt's `message.topic` is a `Topic` object, not a `str` | `str()` it before string methods. An exception here escapes into the message loop and tears down the subscription, so a bad payload would cost the next seconds of alerting |
| `pkill -9` **does** trip the Last Will | SIGKILL closes the TCP socket, so the broker publishes the offline status. A true partition (firewall drop, black hole) fires no Last Will — only a staleness rule catches that, see [Alerting §7](./07-alerting.md) |
| `--speed N` runs the simulator's clock ahead of wall clock | After a few minutes at `--speed 30` the newest points are timestamped in the future, so `time < now()` queries (`/api/summary`, `/api/series`) lag behind the Last Value Cache view from `/api/now`. Correct behaviour, but it reads as stale data |
