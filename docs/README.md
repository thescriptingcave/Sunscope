# Sunscope — Design Documentation

Sunscope: a physics-based solar farm simulator with a real-time web and mobile dashboard.

**Stack:** Python (pvlib) → MQTT (EMQX) → Telegraf → InfluxDB 3 Core → Grafana + React PWA + FastAPI

## Documents

| # | Document | Purpose |
|---|----------|---------|
| 1 | [Design](./01-design.md) | Domain model, PV physics, site topology, fault scenarios, acceptance criteria |
| 2 | [Architecture](./02-architecture.md) | Components, technology choices, port map, repo layout, why-not decisions |
| 3 | [Data Flow](./03-data-flow.md) | MQTT topics, JSON payloads, InfluxDB schema, end-to-end sequences, latency budget |
| 4 | [Security](./04-security.md) | Threat model, per-tier controls, the MQTT-WebSocket exposure cliff |
| 5 | [Testing](./05-testing.md) | Test strategy, pyramid, physics validation, MQTT contract tests, SQL regression, load tests |
| 6 | [SQL Examples](./06-sql-examples.md) | Beginner → Expert InfluxDB 3 Core SQL, including CTEs and window functions |
| 7 | [Alerting](./07-alerting.md) | Rules, debounce and hysteresis, the staleness check, alert persistence |

## Canonical identifiers

Used consistently across every document. Changing any of these requires updating all docs.

| Entity | Values |
|---|---|
| Database | `solar` |
| Site tag | `mojave` (Nevada, ~36.2°N, 115.1°W) |
| Blocks | `BLK-A`, `BLK-B` |
| Inverters | `INV-01` … `INV-04` (250 kWac each) |
| Strings | `STR-01` … `STR-12` |
| Weather station | `WS-01` |

## Decisions of record

| Area | Choice | Rationale |
|---|---|---|
| Transport | MQTT (EMQX 5) | IoT-native; topic hierarchy mirrors site/block/inverter/string; retained messages + Last Will give instant offline detection |
| Simulator | Python + `pvlib` | NREL SPA solar position, clear-sky models, real inverter efficiency curves |
| Site model | 1 MWac / 1.19 MWp DC | DC/AC ratio 1.19 so clipping genuinely occurs on hot clear days |
| Ingest | Telegraf `inputs.mqtt_consumer` | Decoupled failure domain, batching, retry, replay |
| Time series | InfluxDB 3 Core (`influxdb:3.11-core`) | `latest` now resolves to 3 Core, so images are pinned |
| Grafana | 12.2+, **SQL** query language | Flux is not supported on InfluxDB 3.x |
| Web + mobile | Single React PWA | One codebase, installable to iOS/Android, live data over MQTT/WebSocket |
| Backend | FastAPI + JWT | Queries InfluxDB, evaluates alert rules, sends Web Push |
| Faults | Included | Otherwise alert rules never fire and dashboard panels stay empty |
| HTTPS | Localhost only (initially) | Web Push deferred until a tunnel or domain exists — see Security doc |

## Verified platform constraints

Confirmed against InfluxDB 3 Core documentation. These shape the schema, the SQL, and the
Grafana setup. Full detail and source links in [SQL Examples](./06-sql-examples.md#platform-constraints).

- The SQL engine is **Apache Arrow DataFusion**, not a traditional row-store engine.
- **Flux is not supported.** Grafana must use SQL (Flight SQL over HTTP/2) or InfluxQL.
- **Tag column definitions are immutable per table.** The schema must be correct on first write.
- **`QUALIFY` is not supported.** Use a derived table in `FROM` plus `WHERE rn = 1`.
- **`WITH RECURSIVE` is not supported.** Plain `WITH` with multiple CTEs is supported.
- **`CROSS JOIN`, `OFFSET`, and the `TOP` clause** are not supported.
- `last_cache()` and `distinct_cache()` are **`FROM`-clause table functions** taking string literals.
- Parameterized queries use **`$name` only**, in **`WHERE` predicates only** — not in `SELECT`,
  `GROUP BY`, function arguments, or `INTERVAL` literals. They are text substitution, not
  prepared statements.
- `GROUP BY` requires an aggregate or selector in `SELECT`; the `SELECT` list may contain only
  `GROUP BY` columns or aggregates. Use ordinals (`GROUP BY 1`) to group by expressions.
- `RANGE` window frames require `ORDER BY` with **exactly one** column.
- `count(expr)` **includes** NULL values.

## Build order

1. **Scaffold** — compose, healthchecks, pinned images
2. **Simulator** — topology, physics, faults, MQTT publisher with LWT
3. **Ingest** — Telegraf, pre-created schema, Last Value Cache
4. **Grafana** — SQL datasource + dashboards
5. **FastAPI** — auth, `/api/now`, `/api/series`, push subscription
6. **PWA** — live tiles over MQTT/WebSocket
7. **Alerting** — threshold rules + in-app feed
8. **Polish** — scenarios, README
