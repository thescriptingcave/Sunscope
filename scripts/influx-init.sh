#!/usr/bin/env bash
# One-shot InfluxDB 3 Core initialisation.
#
# Runs inside the influxdb image as the `influx-init` compose service, before
# Telegraf starts. This ordering is deliberate: InfluxDB 3 tag column
# definitions are IMMUTABLE once a table exists, so the schema must be created
# explicitly before the first write rather than being inferred from data.
#
# Idempotent: safe to re-run. Existing objects are left alone.
set -euo pipefail

DB="${INFLUX_DB:-solar}"
URL="${INFLUX_URL:-http://localhost:8181}"
ADMIN_TOKEN="${INFLUX_ADMIN_TOKEN:?INFLUX_ADMIN_TOKEN is required}"
SECRETS_DIR="${SECRETS_DIR:-/etc/influxdb3/secrets}"

# --tls-no-verify: the certificate is self-signed. Replace with --tls-ca
# pointing at secrets/tls/server.crt for anything beyond local development.
influx() { influxdb3 "$@" --host "$URL" --token "$ADMIN_TOKEN" --tls-no-verify; }

# The CLI has no `show tables`; table discovery goes through SQL.
sql() {
  influxdb3 query --host "$URL" --token "$ADMIN_TOKEN" --database "$1" \
    --tls-no-verify "$2" 2>/dev/null
}
table_exists() {
  sql "$DB" "SELECT table_name FROM information_schema.tables WHERE table_schema = 'iox' AND table_name = '$1'" \
    | grep -q "$1"
}

echo "==> Waiting for InfluxDB at ${URL}"
for _ in $(seq 1 60); do
  curl -fsSk "${URL}/ping" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsSk "${URL}/ping" >/dev/null || { echo "    TIMEOUT" >&2; exit 1; }
echo "    ready"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
echo "==> Database '${DB}'"
# `show databases` renders an ASCII table whose single column is named
# `iox::database`, and each row is "| name |". Match the row, not $1.
if influx show databases 2>/dev/null | grep -qE "^\|[[:space:]]*${DB}[[:space:]]*\|$"; then
  echo "    exists"
else
  influx create database "$DB"
  echo "    created"
fi

# ---------------------------------------------------------------------------
# Tables
#
# --tags form the primary key, in this order, and are immutable.
# --fields is a single list of name:type; valid types are
# int64, uint64, float64, utf8, bool.
# ---------------------------------------------------------------------------
create_table() {
  local table="$1" tags="$2" fields="$3"
  if table_exists "$table"; then
    echo "    ${table} exists"
    return
  fi
  influx create table --database "$DB" --tags ${tags} --fields "$fields" "$table"
  echo "    created ${table}"
}

echo "==> Tables"
create_table inverter_telemetry "site block inverter_id model" \
  "ac_power_w:float64,dc_power_w:float64,ac_voltage_v:float64,ac_current_a:float64,dc_voltage_v:float64,dc_current_a:float64,efficiency:float64,heatsink_temp_c:float64,internal_temp_c:float64,uptime_s:int64,status_code:int64,clipping:bool"

create_table string_telemetry "site block inverter_id string_id" \
  "dc_power_w:float64,dc_voltage_v:float64,dc_current_a:float64,module_temp_c:float64"

create_table weather_station "site station_id" \
  "ghi:float64,dni:float64,dhi:float64,air_temp_c:float64,wind_speed_mps:float64,relative_humidity:float64,clearness_index:float64"

create_table site_rollup "site" \
  "total_ac_power_w:float64,daily_yield_kwh:float64,pr_ratio:float64,capacity_factor:float64,inverters_online:int64,strings_online:int64"

# `rule` is a tag, not just a field, and that matters more than it looks.
# A point's primary key is (measurement, tag set, timestamp). Two alerts on the
# same device with the same severity that resolve in the same evaluation carry
# identical site/severity/source and an identical nanosecond timestamp, so
# without `rule` in the tag set the second write silently overwrites the first
# and one of the two resolutions is lost. That happened.
create_table events "site severity source rule" \
  "code:utf8,message:utf8,value:float64,threshold:float64"

# ---------------------------------------------------------------------------
# Last Value Cache -- in-memory, backs GET /api/now for the PWA cold load.
# Keyed on site + inverter_id only; every extra key column multiplies
# cardinality and memory for no benefit here.
# ---------------------------------------------------------------------------
echo "==> Last Value Cache"
# `influxdb3 show system` takes a different argument order and rejects
# --host/--token there, so detect the cache through SQL instead.
if sql "$DB" "SELECT name FROM system.last_caches WHERE name = 'inverter_current'" | grep -q inverter_current; then
  echo "    inverter_current exists"
else
  influx create last_cache --database "$DB" \
    --table inverter_telemetry \
    --key-columns site,inverter_id \
    --count 1 \
    --ttl 30m \
    inverter_current
  echo "    created inverter_current"
fi

# ---------------------------------------------------------------------------
# Component tokens
#
# IMPORTANT: InfluxDB 3 Core 3.11 does NOT support permission-scoped database
# tokens. `influxdb3 create token` only offers --admin, and the HTTP API
# exposes only admin-token endpoints. Every token created here is therefore an
# ADMIN token.
#
# Separate named tokens still buy real value: per-component attribution in the
# token table, and the ability to revoke one component without rotating the
# others. They do NOT provide least privilege. See docs/04-security.md.
# ---------------------------------------------------------------------------
echo "==> Component tokens"
mkdir -p "$SECRETS_DIR"

# A token file can outlive the token catalog: `docker compose down -v` wipes the
# InfluxDB volume but leaves secrets/ on the host, so the file looks present
# while the server has never heard of it. Always verify before reusing, and
# regenerate on mismatch.
token_works() {
  local tok="$1"
  curl -fsSk -o /dev/null \
    -H "Authorization: Bearer ${tok}" \
    "${URL}/api/v3/query_sql?db=${DB}&q=SELECT%201" 2>/dev/null
}

for name in telegraf-write grafana-read api-read; do
  out="${SECRETS_DIR}/${name}.token"

  if [[ -s "$out" ]] && token_works "$(cat "$out")"; then
    echo "    ${name} exists and is valid"
    continue
  fi
  if [[ -s "$out" ]]; then
    echo "    ${name} present but rejected; regenerating"
  fi
  # If the old name is still in the catalog, its plaintext is unrecoverable
  # (InfluxDB stores only a hash), so create under a fresh name.
  if influx show tokens 2>/dev/null | grep -q "$name"; then
    name="${name}-$(date +%s)"
    out="${SECRETS_DIR}/${name}.token"
  fi

  # The token must be created ONLINE so the server registers it.
  #
  # `create token --admin --offline` looks attractive because it can write the
  # token straight to a file, but it mints a token the server never learns
  # about: every subsequent request with it returns 401. Verified against
  # influxdb:3.11-core. So the token is read from stdout instead, which means
  # stripping the ANSI colour codes the CLI wraps around it.
  created=$(influxdb3 create token --admin --name "$name" --host "$URL" \
    --token "$ADMIN_TOKEN" --tls-no-verify 2>/dev/null)
  printf '%s' "$created" | grep -oE 'apiv3_[A-Za-z0-9_-]+' | head -1 > "$out"

  if [[ ! -s "$out" ]]; then
    echo "    FAILED to create token for ${name}" >&2
    exit 1
  fi
  if ! token_works "$(cat "$out")"; then
    echo "    FAILED: new token for ${name} was rejected by the server" >&2
    exit 1
  fi
  chmod 600 "$out"
  echo "    created ${name} (verified)"
done

echo
echo "==> Tables now present:"
sql "$DB" "SELECT table_name FROM information_schema.tables WHERE table_schema = 'iox' ORDER BY table_name"
echo "==> Init complete"
echo
echo "NOTE: Grafana and FastAPI authenticate with INFLUX_ADMIN_TOKEN from .env."
echo "      InfluxDB 3 Core 3.11 has no permission-scoped database tokens, so"
echo "      the per-component token files here provide attribution and"
echo "      independent revocation, not least privilege. See docs/04-security.md."
