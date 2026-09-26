# Grafana + InfluxDB 3 Core: what actually works

Findings from building this stack, all verified against a live
`grafana/grafana:12.2.0` and `influxdb:3.11-core`. Read this before debugging
the datasource, because two of these cost hours and neither is documented
accurately.

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

## 4. Open blocker: the database header is not sent

With TLS working, the error changes to:

```
flightsql: rpc error: code = InvalidArgument desc = no 'database' header in request
```

That is an *application* error from InfluxDB, which proves gRPC is now
connecting successfully. But the Flight SQL request carries no database.

Reproduced identically on **grafana/grafana 12.2.0 and 12.4.0**, and across
every combination of:

- `jsonData.database` = `solar`
- top-level `database` field = `solar`
- `jsonData.bucket` = `solar`
- per-query `database` field
- `user` set alongside `database`

The InfluxDB documentation configures this datasource **through the Grafana UI**,
and the UI evidently writes something the provisioning API does not.

### Resolution

Finish configuring the datasource once in the UI:

```
http://127.0.0.1:3000/connections/datasources/edit/influxdb3-solar
```

Set URL, Token, and Database, choose SQL, and save. After that, dashboards can
still be provisioned as code from `grafana/provisioning/dashboards/`, so this is
a one-time manual step rather than an ongoing one.

## 5. Selecting InfluxQL is not possible via provisioning either

InfluxQL uses HTTP/1.1 and would sidestep gRPC entirely, making the whole
problem disappear. But `jsonData.version` does not appear to select the query
language in Grafana 12.2 — setting it to `InfluxQL` left the transport on
Flight SQL. The same "configure it in the UI" caveat applies.

If Grafana remains blocked, two working alternatives for the same data:

- **InfluxDB 3 Explorer**, bundled with the server, queries SQL over plain HTTPS
  today and needs no Grafana.
- The **PWA** (phase 7), which reads the Last Value Cache and SQL directly
  through FastAPI and does not depend on Grafana at all.

## Summary of what each component needs

| Component | TLS trust mechanism | Works |
|---|---|---|
| InfluxDB server | `--tls-cert` / `--tls-key` | yes |
| `influxdb3` CLI | `--tls-no-verify` (or `--tls-ca`) | yes |
| Telegraf | `tls_ca = <path>` | yes |
| FastAPI (phase 6) | `httpx` / `influxdb3` client CA bundle | not yet built |
| Grafana HTTP client | `jsonData.insecureSkipVerify` | yes |
| **Grafana Flight SQL client** | **`SSL_CERT_FILE` only** | **TLS yes, queries no** |

## Health-check caveat

`GET /api/datasources/uid/<uid>/health` returns the Flight SQL error above even
when a *real query* might work, because the health check does not carry a
database either. Do not treat the health endpoint as authoritative for this
datasource — test an actual query.
