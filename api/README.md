# Solar Farm API

Read-only backend over the telemetry in InfluxDB 3. Backs both the PWA and the
alerting engine.

## Run

**Containerised, as part of the stack:**

```bash
docker compose up -d api       # from the repo root
```

Interactive docs at http://127.0.0.1:8000/docs

**On the host, for working on the API alone:**

```bash
uv sync
uv run solar-api               # 127.0.0.1:8000
```

The container bind-mounts `api/src` and runs uvicorn with `--reload`, so editing
source on the host reloads the service in a couple of seconds. The token and CA
bundle are mounted as files rather than passed as environment variables, so
neither is visible to `docker inspect`.

Inside the network the server is `https://influxdb:8181`. The certificate's SAN
already covers that name, so verification stays real — the container pins
`secrets/tls/ca-bundle.crt` rather than disabling TLS.

Tests run on the host either way: `Settings` resolves `.env` relative to the
source tree, which differs inside the container.

## Tests

```bash
uv run pytest                      # 74 tests
uv run pytest tests/test_live.py   # 10 more, need the stack running
uv run ruff check src/ tests/
```

`test_live.py` is the one that earns its keep. The unit tests prove the SQL is
injection-safe; only the live tests prove it is also *dialect-valid*, and those
are different failures. It has already caught two real bugs — see below.

## Why not the official InfluxDB client

`influxdb3-python` is deliberately not a dependency:

1. `InfluxDBClient3` exposes no `query_with_parameters`. Parameter binding is
   `$name` in `WHERE` predicates only, and that is the entire injection defence
   in `docs/04-security.md`. A client that cannot pass parameters cannot use it.
2. It queries over Flight SQL (gRPC) — the same transport that made Grafana fail
   against a plaintext server.

So this calls `POST /api/v3/query_sql` over HTTPS with httpx: parameterized,
CA-pinned, and no gRPC handshake to negotiate.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | no | Liveness |
| POST | `/api/auth/login` | no | Single-user login → JWT |
| GET | `/api/auth/me` | yes | Echo the subject |
| GET | `/api/now` | yes | Current state, from the Last Value Cache |
| GET | `/api/summary` | yes | Fleet rollup + per-inverter aggregates |
| GET | `/api/series` | yes | One metric, time-bucketed |
| GET | `/api/strings` | yes | Per-inverter string imbalance |
| GET | `/api/events` | yes | Recent alarms, optional severity filter |
| GET | `/api/explore` | yes | Read-only SQL (localhost only) |
| GET | `/api/meta` | yes | Allowlists, so the UI need not duplicate them |

```bash
TOK=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$API_ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

curl -s -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:8000/api/series?table=inverter_telemetry&metric=ac_power_w&interval=1h&group_by=inverter_id'
```

## SQL injection defence

Three layers, in order:

1. **Identifiers are allowlisted.** `table`, `metric`, `interval`, `group_by`
   and `severity` are looked up in a closed `dict`. An unknown value is a 400
   before any SQL exists. This is why `interval` is a fixed set rather than a
   free string: it is interpolated into an `INTERVAL` literal, and InfluxDB 3
   cannot parameterise one.
2. **Values are bound.** `site` and the time range become `$site`,
   `$start_time`, `$end_time`. `tests/test_sql.py` asserts a hostile value
   never appears in the SQL text.
3. **The one interpolation is allowlist-checked.** `last_cache()` takes string
   *literals* and rejects parameters, so `table` and `cache` are validated
   against closed sets immediately before use.

Verified live: seven injection attempts across the HTTP surface, all rejected
with 400, all tables intact afterwards.

## Known weaknesses

Disclosed rather than papered over. All of these are in the "localhost" column
of the posture table in `docs/04-security.md`.

- **`/api/explore` is a deny-list.** The token is admin-scoped because InfluxDB
  3 Core offers no read-only tokens, so this endpoint is the only boundary. It is
  restricted to loopback. Strengthen it before exposing it.
- **The rate limiter is per-process.** Multiple workers multiply the effective
  limit.
- **The password is compared, not hashed.** Fine for a credential in a local
  `.env`; wrong for anything multi-user.
- **`/api/series` returns untyped JSON.** `METRICS` carries the SQL type so the
  client can coerce, but the API does not currently enforce it.

## Bugs the live tests caught

Both were invisible to the stubbed unit tests, which is the argument for having
the live suite at all.

| Bug | Symptom | Cause |
|---|---|---|
| `AVG` on a boolean | Every bucketed request for `clipping` returned 400 | The engine rejects `avg(Boolean)` at plan time. `AGGREGATE_FOR_TYPE` now picks `MAX` for booleans. |
| `IN ($severity)` with a list | Severity filtering returned 400 | "JSON arrays are not supported as query parameters" — only null, boolean, number and string bind. Now one scalar parameter per severity. |

A third was found by probing the live HTTP surface: a malformed timestamp
escaped `fromisoformat` as a **500** rather than a 400, which misreports a
client mistake as a server fault.
