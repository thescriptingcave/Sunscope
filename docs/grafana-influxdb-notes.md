# Grafana + InfluxDB 3 Core: what actually works

Findings from building this stack, all verified against a live
`grafana/grafana:12.2.0` and `influxdb:3.11-core`. Read this before debugging
the datasource: sections 1–3 are the TLS traps that cost hours, section 4 is the
`database` header that looks like a Grafana bug and is not, and section 6 is how
to write panels that actually show data.

## 1. Flight SQL requires TLS. This is not optional.

Grafana's InfluxDB datasource queries InfluxDB 3 Core over **Flight SQL**, which
is gRPC. gRPC will not speak to a plaintext server. Against an InfluxDB with no
TLS, every query fails with:

```
flightsql: rpc error: code = Unavailable desc = connection error:
desc = "transport: authentication handshake failed:
        tls: first record does not look like a TLS handshake"
```

`scripts/gen-tls-cert.sh` generates a self-signed certificate with the right
SANs, and InfluxDB is started with `--tls-cert` / `--tls-key`.

## 2. The InfluxDB docs are wrong about "Insecure Connection"

The docs say you can run SQL without TLS by enabling **Insecure Connection** under
Advanced Database Settings. That does not work for the gRPC path.

Every plausible provisioning key was tried; the transport stayed on Flight SQL
and kept failing:

| Key | Result |
|---|---|
| `jsonData.insecureConnection` | still Flight SQL |
| `jsonData.insecureSkipVerify` | still Flight SQL |
| `jsonData.allowInsecure` | still Flight SQL |
| `jsonData.insecureGrpc` | still Flight SQL |
| `jsonData.queryLanguage` / `qlVersion` / `lang` | still Flight SQL |
| `jsonData.product` = `InfluxDB` / `InfluxDB v2` | still Flight SQL |

The honest workaround is to give the server real TLS.

## 3. Grafana's gRPC client ignores datasource TLS settings entirely

This is the non-obvious one. Grafana's **HTTP** client honours the datasource
TLS fields. The **gRPC** client honours none of them:

| Key | Honoured by HTTP client | Honoured by gRPC/Flight SQL |
|---|---|---|
| `jsonData.insecureSkipVerify` | yes | **no** |
| `jsonData.tlsSkipVerify` | yes | **no** |
| `jsonData.tlsCACert` | yes | **no** |
| `jsonData.tlsCAData` | yes | **no** |
| `secureJsonData.tlsSkipVerify` | yes | **no** |

The gRPC stack is Go, and Go honours the **`SSL_CERT_FILE`** environment
variable. That is the only lever available without rebuilding the image or
running the container as root:

```yaml
environment:
  SSL_CERT_FILE: /run/secrets/tls/ca-bundle.crt
```

`ca-bundle.crt` is the container's system CA bundle with the InfluxDB cert
appended, built by `scripts/gen-tls-cert.sh`.

Telegraf, by contrast, *does* support TLS properly — use `tls_ca` rather than
disabling verification:

```toml
tls_ca = "/run/secrets/tls/server.crt"
```

## 4. Resolved: the database header is a top-level field

With TLS working, the next error was:

```
flightsql: rpc error: code = InvalidArgument desc = no 'database' header in request
```

That is an *application* error from InfluxDB, which proves gRPC is now
connecting successfully — but the Flight SQL request carried no database.

The cause is that Grafana sends `database` as a **gRPC metadata header**, and it
reads that field from the **top level** of the datasource, not from `jsonData`.
Setting only `jsonData.database` leaves the header unset. The provisioning needs
both, because they are not aliases:

```yaml
datasources:
  - name: InfluxDB-3
    uid: influxdb3-solar
    type: influxdb
    url: https://influxdb:8181
    database: solar          # <- sent as the gRPC `database` header
    jsonData:
      version: SQL
      database: solar        # <- used by the HTTP/InfluxQL path
```

Verified, not assumed:

- `GET /api/datasources/uid/influxdb3-solar/health` → `{"message":"OK"}`
- `POST /api/ds/query` with a real `SELECT` → rows returned
- after a full `docker compose restart grafana`, so the result comes from the
  provisioning file and not from state an earlier UI session left behind

This was originally filed as a blocker requiring a one-time manual step in the
Grafana UI. **No manual step is needed.** The dashboards in
`grafana/dashboards/` are provisioned from code like everything else.

## 5. Flight SQL *is* the SQL path — there is nothing to switch away from

A common misreading: `jsonData.version: SQL` sounds like it should select a
non-gRPC transport, and InfluxQL sounds like the escape hatch from all this TLS
and header trouble. It is the reverse.

Flight SQL is how Grafana queries InfluxDB 3 with SQL. Choosing SQL is what puts
the datasource on gRPC, and gRPC is what requires TLS. InfluxQL would use
HTTP/1.1 and sidestep all of section 1, but InfluxQL is not supported by
InfluxDB 3 Core, so that is not an option — it is a different product.

`jsonData.product` must be set to **`InfluxDB Enterprise`** even on Core. There is
no Core-specific entry in the dropdown, and this is expected, not a mistake.

## 6. Writing panels against InfluxDB 3 SQL

Three things that are not obvious and each cost a debugging cycle.

**Use `$__timeFilter(time)`, not `now() - INTERVAL`.** The simulator publishes
simulated timestamps that do not track wall-clock time, so a hardcoded
`now() - INTERVAL '6 hours'` can select a window that contains no data at all —
and an empty panel is indistinguishable from a broken query. `$__timeFilter`
expands to the dashboard's time-picker range, so the user controls the window and
the panels are honest about what they cover.

**Use `${var:sqlstring}` for multi-value variables.** A bare `$inverter` is
interpolated by Grafana's generic formatter, which does not produce a quoted SQL
list. The datasource then plans the query against a bare identifier:

```
Schema error: No field named inv. Valid fields are inverter_telemetry.ac_power_w, ...
```

Correct form, which expands to `'INV-01','INV-02',…`:

```sql
WHERE inverter_id IN (${inverter:sqlstring})
```

**Tags are `Dictionary(Int32, Utf8)`.** A column's SQL type is how you tell a
tag from a field, which matters for anything that reconstructs line protocol.
`information_schema.columns.data_type` gives the answer:

| `data_type` | Meaning |
|---|---|
| `Dictionary(Int32, Utf8)` | tag — part of the primary key, immutable |
| `Float64`, `Int64`, `Utf8`, `Boolean` | field |

## Summary of what each component needs

| Component | TLS trust mechanism | Works |
|---|---|---|
| InfluxDB server | `--tls-cert` / `--tls-key` | yes |
| `influxdb3` CLI | `--tls-no-verify` (or `--tls-ca`) | yes |
| Telegraf | `tls_ca = <path>` | yes |
| FastAPI | `httpx` with the CA bundle | yes |
| Grafana HTTP client | `jsonData.insecureSkipVerify` | yes |
| **Grafana Flight SQL client** | **`SSL_CERT_FILE` only** | **yes, with a top-level `database`** |

## Health-check caveat

Test an actual query, not just the health endpoint. The health check does not
carry a database, so during the failure in section 4 it reported the Flight SQL
error; conversely it is not a substitute for confirming a panel renders, because
a datasource can return frames that Grafana still fails to plot.

`scripts/browser/check-grafana.js` does both: it logs in, renders each
provisioned dashboard in headless Chromium, and fails on a panel-level query
error, an empty panel, or a template variable that did not expand. It is wired
into `./scripts/bootstrap.sh test`.
