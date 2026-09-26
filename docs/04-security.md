# 04 — Security

## 1. Scope and honest risk assessment

This is a **simulator running on localhost** with synthetic data. It is not a production system
and holds no real assets. Security engineering effort should be proportionate to that.

The threat model is therefore narrow and specific:

> **Primary risk: the stack gets exposed to a network, and the MQTT broker — the one component
> shipped with no authentication enabled by default — becomes an open, writeable data plane.**

This is not hypothetical. The planned path to making Web Push work on iOS is to put the PWA
behind HTTPS, which typically means adding a tunnel (Cloudflare, ngrok) or a reverse proxy. The
moment that happens, **every port that was bound to `127.0.0.1` may become reachable**, and
EMQX on `:8083` is an unauthenticated MQTT broker by default. Anyone who finds it can publish
arbitrary data into the pipeline, subscribe to everything, or flood the broker.

Secondary risks are ordinary: SQL injection through the API, a leaked database token, an
unauthenticated PWA backend, and secrets committed to git.

## 2. Threat model

| # | Threat | Vector | Impact | Likelihood (localhost) | Likelihood (exposed) |
|---|---|---|---|---|---|
| T1 | **Unauthenticated MQTT access** | EMQX `:1883` / `:8083` | Read all telemetry; inject fake data; flood broker | Low | **High** |
| T2 | **InfluxDB unauthenticated access** | `:8181` | Read/write/delete all telemetry | Low | High |
| T3 | **SQL injection** | FastAPI `/api/series` | Read arbitrary data; DoS | Medium | Medium |
| T4 | **Token / secret leakage** | `.env` in git, logs, client bundle | Full DB access | Low | Medium |
| T5 | **Unauthenticated API access** | FastAPI `:8000` | Read history; subscribe to push | Low | Medium |
| T6 | **Grafana default credentials** | `admin/admin` | Full admin, datasource access | Low | Medium |
| T7 | **Denial of service** | MQTT flood, expensive SQL | Dashboard unusable | Low | Medium |
| T8 | **XSS via telemetry** | Event `message` rendered in PWA | Session theft | Low | Medium |
| T9 | **PII in telemetry** | Log/URL leakage | Low — synthetic data only | n/a | Low |
| T10 | **Supply chain** | Unpinned images, dependencies | Arbitrary code | Low | Low |

T1, T2, T5 and T6 all share a single root cause: **the compose file binds ports to `0.0.0.0` by
default.** Every one of them is fixed by one line per service.

## 3. Current posture — localhost

As designed, with no additional hardening:

| Control | Status |
|---|---|
| Compose ports bound to `127.0.0.1` | Required — the single most important control |
| EMQX authentication | **Disabled** — acceptable only while bound to loopback |
| InfluxDB authentication | Enabled with a generated admin token |
| EMQX console credentials | `admin` / `admin` — **must be changed** |
| Grafana credentials | `admin` / `admin` — **must be changed** |
| FastAPI JWT auth | Enabled, single user from env |
| Secrets in `.env` | Yes, gitignored |
| TLS | None (loopback) |
| Web Push VAPID keys | Generated at setup, stored in `.env` |

**The loopback binding is the entire security model right now.** Everything else is defence in
depth. If that one control is lost, T1 becomes trivially exploitable.

## 4. Controls

### 4.1 Network binding (addresses T1, T2, T5, T6)

```yaml
services:
  emqx:
    ports:
      - "127.0.0.1:1883:1883"
      - "127.0.0.1:8083:8083"
      - "127.0.0.1:18083:18083"
  influxdb:
    ports:
      - "127.0.0.1:8181:8181"
  grafana:
    ports:
      - "127.0.0.1:3000:3000"
```

Never publish these to the LAN, even on a trusted network. A solar farm simulator on a shared
network is a fun neighbour.

### 4.2 EMQX authentication (addresses T1)

Enable built-in authentication with per-client credentials. Each simulator device gets its own
username so publishes can be attributed and a single device can be revoked.

```hocon
# emqx/emqx.conf
authentication = [
  {
    mechanism = password_based
    backend   = built_in_database
    user_id_type = username
  }
]

authorization = [
  {
    mechanism = acl_file
    acl_file  = "/opt/emqx/etc/acl.conf"
  }
]
```

```hocon
# acl.conf
%% Read-only telemetry for the PWA and Grafana
{allow, {username, "grafana"}, all, ["solar/#"]}.
%% Simulator may publish telemetry and status, subscribe to nothing
{allow, {username, "sim-##"}, publish, ["solar/+/block/+/inverter/+/telemetry",
                                         "solar/+/block/+/inverter/+/status",
                                         "solar/+/block/+/inverter/+/string/+/telemetry",
                                         "solar/+/block/+/weather/+/telemetry",
                                         "solar/+/rollup",
                                         "solar/+/events"]}.
```

That last rule is the important one: the simulator is **publish-only**. Compromising it must not
grant the ability to read the whole farm or to inject events that trigger alerts.

### 4.3 InfluxDB tokens and least privilege (addresses T2, T4)

**Correction: the planned design does not work as written.** This section originally specified
separate read-only and write-only tokens, assuming `influxdb3 create token` supports permission
scoping. It does not, in InfluxDB 3 Core 3.11. Verified against a live `influxdb:3.11-core`:

| Attempted | Result |
|---|---|
| `influxdb3 create token --read database` | Option does not exist |
| `influxdb3 create token` (bare) | No options except `--admin` |
| `POST /api/v2/authorizations` | 404 |
| `POST /api/v3/authorizations` | 404 |
| `POST /api/v3/configure/token/named_admin` | 201, but the token is an **admin** token |

**Every token InfluxDB 3 Core can create is an admin token.** The `permissions` column in
`influxdb3 show tokens` reads `*:*:*` for all of them.

Separate named tokens still buy three real things:

- **Attribution** — the token table shows which component authenticated
- **Independent revocation** — rotate one component without touching the others
- **Credential separation** — a leak in one component does not hand over the others

What they do **not** buy is least privilege. If Grafana is compromised, the attacker has full
admin on InfluxDB. That reduces blast radius; it is not access control and should not be
described as such.

| Principal | Credential | Notes |
|---|---|---|
| InfluxDB server | `secrets/admin-token` (JSON offline file) | Written by `scripts/gen-secrets.sh` |
| Telegraf | `secrets/telegraf-write.token` | Read from a file by the entrypoint, not from the environment |
| Grafana | `INFLUX_ADMIN_TOKEN` from `.env` | Provisioning expands env vars and cannot read files |
| FastAPI | `secrets/api-read.token` | Mounted as a file, same pattern as Telegraf |

**If least privilege is genuinely required**, the options are InfluxDB 3 Enterprise, InfluxDB
Cloud, or a proxy in front of InfluxDB that enforces the boundary. That is a change in scope,
not a config tweak.

> **Verified trap.** `influxdb3 create token --admin --offline` mints a token the server never
> registers. It looks ideal because it writes the token straight to a file, but every request
> with it returns 401. Tokens must be created *online* and read from stdout, stripping the CLI's
> ANSI colour codes. `scripts/influx-init.sh` does this, then verifies the new token works
> before it continues.

### 4.4 SQL injection prevention (addresses T3)

InfluxDB 3 Core supports `$name` parameters, but with two sharp restrictions that are easy to
get wrong:

- `WHERE` predicates **only** — not `SELECT`, `GROUP BY`, function arguments, or `INTERVAL`
- Substitution happens **before** planning; it is not a prepared statement

Therefore:

**Do — parameterise values:**

```python
sql = """
SELECT time, inverter_id, ac_power_w
FROM inverter_telemetry
WHERE site = $site AND time >= $start_time AND ac_power_w >= $min_power
"""
params = {"site": site, "start_time": start, "min_power": min_power}
```

**Do — allowlist identifiers (tables, columns, sort direction):**

```python
ALLOWED_METRICS = {
    "ac_power_w", "dc_power_w", "efficiency",
    "heatsink_temp_c", "internal_temp_c",
}
ALLOWED_SORTS = {"asc", "desc"}

if metric not in ALLOWED_METRICS:
    raise HTTPException(400, "unknown metric")
order = sort if sort in ALLOWED_SORTS else "desc"   # never interpolate raw input
```

**Do not — string-format user input into SQL:**

```python
# WRONG — classic injection
sql = f"SELECT * FROM inverter_telemetry WHERE site = '{site}'"
```

Verified against a live `influxdb:3.11-core`: posting `x' OR 1=1 --` as a `$site` parameter
returns an empty result set — not an error, and not anyone else's data. The value is treated as
a literal.

Because parameters cannot be used in `INTERVAL` literals, time ranges need a small extra care:
take the range as **timestamps** (`$start_time`, `$end_time`) and compute any bucket size
server-side from an allowlisted enum, rather than accepting an `INTERVAL` string from the client.

### 4.5 Secrets handling (addresses T4)

- `.env` is gitignored; `.env.example` holds placeholders only
- Generate tokens and VAPID keys at setup, never commit real values
- Grafana and EMQX passwords come from `.env`, not from compose defaults
- **Never log the InfluxDB token or the JWT secret**, including in exception handlers
- The VAPID private key must stay server-side; only the public key reaches the PWA

```bash
# .gitignore
.env
*.pem
*.key
```

### 4.6 API auth (addresses T5)

- JWT with an expiry, HS256, secret from env
- Password hashed with argon2 or bcrypt — never stored or compared in plaintext
- Tokens in `Authorization: Bearer`, **not** in query strings, so they do not land in access logs
- CORS restricted to the PWA's exact origin
- Rate limiting on `/api/auth/login` (a brute-force vector) and on `/api/series` (DoS via
  expensive range scans)

### 4.7 XSS protection (addresses T8)

Event `message` and `source` strings originate from the simulator but must be treated as
untrusted — a compromised or buggy device could publish arbitrary text.

- React escapes by default; **never** use `dangerouslySetInnerHTML` for telemetry values
- Enforce a Content Security Policy
- Do not interpolate telemetry into `href` or `src` without validation

### 4.8 Denial of service (addresses T7)

| Vector | Control |
|---|---|
| MQTT message flood | EMQX rate limiting per client; `max_message_rate` |
| Inexpensive-appearing but expensive SQL | Cap time range; allowlist `date_bin` intervals; statement timeout |
| Grafana dashboard spam | Auth required; provisioning read-only where possible |
| InfluxDB write flood | Write token scoped to one database; rate limit at the proxy |

### 4.9 Supply chain (addresses T10)

- **Pin every image to an explicit version.** `influxdb:latest` now resolves to InfluxDB 3
  Core — a floating tag that silently changes major versions.
- Generate a lockfile (`uv.lock`, `package-lock.json`) and commit it.
- Run `uv lock --check` and `npm audit` in CI.
- Scan images before use in anything beyond a laptop.

## 5. Posture comparison

| Control | Localhost | LAN-exposed | Internet (tunnelled) |
|---|---|---|---|
| Port binding | `127.0.0.1` | `0.0.0.0` + firewall | Reverse proxy only |
| EMQX auth | Optional | **Required** | **Required + TLS** |
| EMQX ACL | Optional | **Required** | **Required** |
| InfluxDB auth | **Required** | **Required** | **Required + TLS** |
| Separate read/write tokens | Recommended | **Required** | **Required** |
| Grafana password | **Change from default** | **Required** | **Required + TLS** |
| FastAPI JWT | **Required** | **Required** | **Required** |
| TLS | No | Yes | **Yes — mandatory** |
| Rate limiting | No | Yes | **Yes** |
| VAPID keys | Generated | Generated | Generated |
| Realistic verdict | Acceptable | Workable | Needs a real threat model |

## 6. The exposure cliff — read before adding a tunnel

The plan to make Web Push work on iOS requires a **secure context**, because browsers block
Web Push outside HTTPS (and outside `localhost`). The usual fix is a tunnel.

**What a tunnel does to this stack:**

1. `https://your-tunnel.trycloudflare.com` terminates TLS publicly
2. The PWA becomes reachable from the internet
3. If the tunnel also proxies other ports — or if anything rebinds from `127.0.0.1` to
   `0.0.0.0` — then **EMQX `:8083` may be exposed with no authentication**

`:8083` is the dangerous one. It is an unauthenticated MQTT broker that accepts both
**publish** and **subscribe**. An attacker who reaches it can subscribe to every topic (full
telemetry read) and publish arbitrary messages (inject fake alarms, corrupt dashboards, exhaust
disk). MQTT has no built-in authorisation to fall back on.

### Safe way to add a tunnel

Expose **only the PWA** and put the API behind it. Do not proxy the broker.

```
Internet ──TLS──▶ reverse proxy ──▶ FastAPI :8000   (PWA + /api, with auth)
                             └──▶ static PWA build

FastAPI (server-side only) ──▶ EMQX :8083           (MQTT over WS)
FastAPI (server-side only) ──▶ InfluxDB :8181
```

**This requires changing the PWA's live-data path.** §5.2 of the Architecture doc has the PWA
connecting directly to EMQX over WebSocket, which is the right design on localhost and the wrong
one on the internet. When tunnelling, the backend must proxy or fan out the MQTT stream instead,
and the broker must never be directly reachable.

Minimum before tunnelling:

1. Enable EMQX authentication **and** ACLs (§4.2) — non-negotiable
2. Proxy only `:8000` and the static assets
3. Serve the PWA over HTTPS with HSTS
4. Keep `1883`, `8083`, `8181`, `18083`, `3000` on `127.0.0.1`
5. Rate limit the login endpoint
6. Rotate every default password

## 7. Pre-exposure checklist

Run through this before anything leaves localhost:

- [ ] Every compose port bound to `127.0.0.1`
- [ ] EMQX authentication enabled
- [ ] EMQX ACLs enabled; simulator is publish-only
- [ ] InfluxDB read and write tokens are separate
- [ ] FastAPI holds a read-only InfluxDB token
- [ ] Grafana and EMQX passwords changed from defaults
- [ ] `.env` gitignored; no secrets committed; `git log -p | grep -i token` is clean
- [ ] No secrets in application logs
- [ ] CORS restricted to the exact PWA origin
- [ ] Rate limiting on login and series endpoints
- [ ] Only `:8000` and static assets proxied
- [ ] TLS terminated at the proxy; HSTS set
- [ ] VAPID private key server-side only
- [ ] CSP header set; no `dangerouslySetInnerHTML` on telemetry values
- [ ] All images pinned to explicit versions
- [ ] Time-range and interval parameters validated and allowlisted

## 8. Known gaps

Stated plainly rather than papered over:

| Gap | Why | Mitigation |
|---|---|---|
| No TLS locally | Loopback does not need it | Add at the proxy when exposed |
| Single shared JWT secret | Single-user system | Acceptable here; per-user secrets if it grows |
| No audit log | Simulator, not a production system | Add if it ever holds real data |
| EMQX ACLs use wildcards | Simplifies topology changes | Review when topology changes, not before |
| No secret rotation tooling | Manual `.env` | Add if tokens are ever shared |
| Last Value Cache is unauthenticated internally | Same process boundary | Non-issue while colocated; revisit if split |

**If this system ever handles real telemetry from a real site, none of the "acceptable here"
judgements above hold**, and it needs a proper threat model, per-device PKI, and audit logging.
