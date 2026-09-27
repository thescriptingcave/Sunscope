#!/usr/bin/env bash
#
# Bootstrap the whole solar farm simulator from a clean checkout.
#
#   ./scripts/bootstrap.sh            # bring everything up (the default)
#   ./scripts/bootstrap.sh status     # what is running, and where
#   ./scripts/bootstrap.sh test       # all test suites and verification scripts
#   ./scripts/bootstrap.sh down       # stop containers, keep the data
#   ./scripts/bootstrap.sh reset      # destroy everything, including the database
#
# Every target is idempotent. `up` can be re-run safely: it will not clobber
# existing secrets, and it will not touch the database.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- output ------------------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step() { printf '\n%s==>%s %s%s\n' "$BLUE$BOLD" "$RESET$BOLD" "$*" "$RESET"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

SIM_PIDFILE="$ROOT/.sim.pid"
SIM_LOG="$ROOT/sim.log"

# --- prerequisites -----------------------------------------------------------
# Checked up front and all at once, so a missing tool is reported before any
# work starts rather than half way through a five-minute build.
require_tools() {
  step "Checking prerequisites"
  local missing=()
  for tool in docker uv node npm python3 curl; do
    if command -v "$tool" >/dev/null 2>&1; then
      ok "$tool $(command -v "$tool")"
    else
      missing+=("$tool")
    fi
  done
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose $(docker compose version --short 2>/dev/null || echo '')"
  else
    missing+=("docker compose (v2 plugin)")
  fi
  (( ${#missing[@]} == 0 )) || die "missing: ${missing[*]}
  install the tools above, then re-run. Nothing was changed."

  docker info >/dev/null 2>&1 || die "docker is installed but not running.
  start Docker Desktop (or your daemon) and re-run."
}

# --- wait helpers ------------------------------------------------------------

# Poll an HTTP endpoint until it answers or the deadline passes. Used instead of
# `sleep` so a slow start is tolerated and a real failure is reported, rather
# than being indistinguishable from a healthy-but-slow boot.
wait_for_http() {
  local url="$1" name="$2" timeout="${3:-90}" waited=0
  while (( waited < timeout )); do
    if curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      ok "$name is up (${waited}s)"
      return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

wait_for_container() {
  local name="$1" state="$2" timeout="${3:-120}" waited=0
  while (( waited < timeout )); do
    if [[ "$(docker inspect -f "{{.State.Status}}" "$name" 2>/dev/null)" == "$state" ]]; then
      ok "$name is $state (${waited}s)"
      return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# --- steps -------------------------------------------------------------------

prepare_secrets() {
  step "Secrets and TLS"
  # gen-secrets.sh only fills values still set to a "change-me" placeholder, so
  # re-running preserves an existing .env and its tokens.
  ./scripts/gen-secrets.sh >/dev/null || die "gen-secrets.sh failed"
  ok "secrets/ and .env present"

  # Must exist before InfluxDB starts: the server is given --tls-cert on the
  # command line and will not start without it.
  ./scripts/gen-tls-cert.sh >/dev/null || die "gen-tls-cert.sh failed"
  ok "TLS certificate and CA bundle generated"

  relax_secret_modes
}

# Make every container-readable secret actually readable by the containers.
#
# WHY THIS IS NEEDED
#
# The generator writes tokens at mode 600 and the TLS directory at 700, owned by
# the invoking user. That is correct for a secret on the host, and it works on
# macOS -- but only because Docker Desktop's file sharing is lenient about
# ownership across the VM boundary. On a native Linux runner the containers run
# as a different uid and cannot read them:
#
#   Failed to initialize admin token from file:
#   Token error: Failed to read admin token file: Permission denied (os error 13)
#
# The containers run as uid 1500 (influxdb3) and bind mounts preserve host modes,
# so the files have to be readable by "other" for the stack to start anywhere
# other than this Mac.
#
# The trade-off is deliberate and worth stating: these files become world-
# readable on the host. That is the standard requirement for bind-mounted
# secrets, and acceptable here because the tokens are local development
# credentials in a gitignored directory, not production secrets. If this ever
# held real credentials, the answer is Docker's top-level `secrets:` with an
# explicit mode, not a wider chmod.
relax_secret_modes() {
  # Two separate needs, and conflating them is how you end up with a 777 secrets
  # directory and no idea which part wanted it.
  #
  # READ: every container that mounts ./secrets runs as uid 1500, and bind mounts
  # preserve host modes, so directories must be traversable and files readable.
  # InfluxDB will not start without admin-token, and it warns (harmlessly) that
  # 644 is looser than it would like.
  find secrets -type d -exec chmod 755 {} + 2>/dev/null || true
  find secrets -type f -exec chmod 644 {} + 2>/dev/null || true

  # WRITE: influx-init mints the per-component tokens and writes them back into
  # this same directory. It truncates the existing *.token files, and when a
  # token is regenerated under a fresh timestamped name it creates a new file
  # outright -- so it needs write access to the files AND to the directory.
  #
  # Scoped to secrets/ and *.token only. secrets/tls/ is written by
  # gen-tls-cert.sh on the host and never by a container, so it stays 755.
  chmod 777 secrets 2>/dev/null || true
  find secrets -maxdepth 1 -name "*.token" -exec chmod 666 {} + 2>/dev/null || true

  # A file the init container creates is owned by uid 1500, and the host user
  # cannot chmod it. That is fine: only the container rewrites those, and only
  # the host ever reads them.

  # .env is read by bootstrap and by compose's variable substitution on the host
  # only, never by a container, so it keeps its tighter mode.
  [[ -f .env ]] && chmod 600 .env
  ok "secret modes set so the containers can read, and init can write, them"
}

build_web() {
  step "Building the PWA"
  if [[ ! -d web/node_modules ]]; then
    info "installing npm dependencies (first run only)"
    (cd web && npm install --no-fund --no-audit >/dev/null 2>&1) \
      || die "npm install failed; try running it in web/ to see the error"
  fi
  (cd web && npm run build >/dev/null 2>&1) \
    || die "npm run build failed; try running it in web/ to see the error"
  [[ -f web/dist/index.html ]] || die "web/dist/index.html missing after build"
  ok "web/dist built ($(du -sh web/dist | cut -f1))"
  info "the API serves this at /, so the app is a single origin"
}

start_infrastructure() {
  step "Starting the stack"
  # Ordering is enforced by compose, not by this script: influxdb must be
  # healthy before influx-init runs, and influx-init must complete before
  # Telegraf and the API start. The schema has to exist before the first write
  # because InfluxDB tag definitions are immutable once a table exists.
  docker compose up -d --build >/dev/null 2>&1 || die "docker compose up failed"

  wait_for_container sunscope-telegraf "running" 120 \
    || warn "telegraf is not running yet; check: docker compose logs telegraf"
  wait_for_http "http://127.0.0.1:8000/healthz" "api" 120 \
    || warn "api health endpoint not answering; check: docker compose logs api"

  # Scanned after a settle delay. Compose reports a container healthy only once
  # its healthcheck passes, which happens a moment *after* the port starts
  # answering -- so scanning immediately flags healthy services as "starting".
  sleep 5
  local unhealthy
  unhealthy="$(docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null \
    | awk '$2 != "healthy" && $2 != "" {print $1" ("$2")"}' || true)"
  if [[ -n "$unhealthy" ]]; then
    warn "not healthy yet: $unhealthy"
  else
    ok "all containers healthy"
  fi
}

sim_running() {
  [[ -f "$SIM_PIDFILE" ]] && kill -0 "$(cat "$SIM_PIDFILE")" 2>/dev/null
}

start_simulator() {
  step "Starting the simulator"
  if sim_running; then
    ok "already running (pid $(cat "$SIM_PIDFILE"))"
    return 0
  fi
  # Runs on the host, not in compose: this is where iteration happens, and the
  # physics loop benefits from a real filesystem and a fast restart.
  # Default settings start at solar noon so there is data to look at at once.
  #
  # The launch must happen *inside* sim/: `uv run` resolves the `solar-sim`
  # entry point from the project it is run in, and running it from the repo
  # root fails with "Failed to spawn: solar-sim".
  #
  # Detached deliberately. `setsid` puts the simulator in its own session so it
  # is not in this script's process group, and all three standard streams are
  # redirected. Without that it inherits the caller's stdout, and anything
  # piping this script's output (`bootstrap.sh up | tee log`) blocks forever on
  # a pipe the simulator is still holding open -- and in CI the simulator gets
  # killed along with the job.
  (cd sim && uv sync >/dev/null 2>&1) || die "uv sync failed in sim/"
  (
    cd sim
    if command -v setsid >/dev/null 2>&1; then
      setsid uv run solar-sim --host 127.0.0.1 --port 1883 \
        >"$SIM_LOG" 2>&1 </dev/null &
    else
      uv run solar-sim --host 127.0.0.1 --port 1883 \
        >"$SIM_LOG" 2>&1 </dev/null &
    fi
    echo $! >"$SIM_PIDFILE"
  )
  sleep 4
  sleep 4
  if sim_running; then
    ok "simulator running (pid $(cat "$SIM_PIDFILE")), logging to sim.log"
  else
    warn "the simulator exited immediately; last lines of sim.log:"
    tail -5 "$SIM_LOG" | sed 's/^/      /' >&2
    return 1
  fi
}

stop_simulator() {
  step "Stopping the simulator"
  if sim_running; then
    kill "$(cat "$SIM_PIDFILE")" 2>/dev/null || true
    sleep 1
    kill -9 "$(cat "$SIM_PIDFILE")" 2>/dev/null || true
    ok "stopped"
  else
    ok "not running"
  fi
  rm -f "$SIM_PIDFILE"
}

show_status() {
  step "Status"
  docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null || true
  if sim_running; then
    ok "simulator: running (pid $(cat "$SIM_PIDFILE"))"
  else
    warn "simulator: not running"
  fi

  step "Endpoints"
  info "dashboard   http://127.0.0.1:8000/  (sign in as admin)"
  info "API health  http://127.0.0.1:8000/healthz"
  info "Grafana     http://127.0.0.1:3000/"
  info "EMQX        http://127.0.0.1:18083/         (dashboard)"
  info "InfluxDB    https://127.0.0.1:8181/         (self-signed TLS)"
  info "MQTT        127.0.0.1:1883 (tcp), 127.0.0.1:8083 (websocket)"
}

run_tests() {
  step "Tests"
  # The full-year sweep is excluded here for the same reason as in CI: ~90 s is
  # affordable on a schedule and not on every run. Run it with `make sweep`.
  ( cd sim && uv run pytest -q --ignore=tests/test_physics_sweep.py ) \
    || die "simulator tests failed"
  ( cd api && uv run pytest -q ) || die "API tests failed"
  ok "lint"
  # scripts/ is linted too. It was not, for a while, and the gap hid 16
  # findings -- including a crash in backup.py whenever the backup directory
  # lived outside the repo, which is exactly what CI does.
  ( cd sim && uv run ruff check src/ tests/ --output-format=concise ) || die "sim lint failed"
  ( cd api && uv run ruff check src/ tests/ --output-format=concise ) || die "api lint failed"
  uv run --project api ruff check scripts/*.py --output-format=concise \
    || die "scripts lint failed"
  ( cd web && npx tsc --noEmit ) || die "web typecheck failed"

  step "Verification scripts"
  # These need the stack to be up. A failure here is a real finding, not a
  # broken environment, so they are reported rather than tolerated.
  uv run --project api python scripts/check-pwa-contract.py \
    || die "API contract check failed"
  uv run --project api python scripts/check-doc-sql.py \
    || die "documented SQL does not match the database"
  if docker compose ps --status running --services 2>/dev/null | grep -qx emqx; then
    uv run --project api --with paho-mqtt python scripts/check-live-ws.py \
      || die "the browser live path is broken"
  else
    warn "EMQX is not running; skipped the live WebSocket check"
  fi

  # The only check that can see a UI-level failure. Everything above is
  # structural, and all of it stayed green while the live feed was throwing in
  # the browser. Playwright is installed on demand.
  step "Browser check (renders the PWA in headless Chromium)"
  if [[ ! -d scripts/browser/node_modules ]]; then
    info "installing Playwright (first run only)"
    (cd scripts/browser && npm install --no-fund --no-audit >/dev/null 2>&1) \
      || die "npm install failed in scripts/browser/"
  fi
  node scripts/browser/check-ui.js \
    || die "the PWA did not render, or its live feed did not come up"

  # Grafana is the other UI, and it fails differently: the datasource can return
  # frames that Grafana still fails to plot, and a template variable can fail to
  # expand while every query reports success. Only rendering catches either.
  step "Grafana check (renders the provisioned dashboards)"
  if docker compose ps --status running --services 2>/dev/null | grep -qx grafana; then
    node scripts/browser/check-grafana.js \
      || die "a Grafana dashboard did not render, or a panel query failed"
  else
    warn "Grafana is not running; skipped the dashboard render check"
  fi

  # docs/sql is generated from docs/06-sql-examples.md, so a stale file means the
  # runnable copies and the teaching document have diverged. --check compares
  # them; --verify then executes every statement against the live database, which
  # is the only thing that catches a query that is still valid SQL but wrong for
  # this dialect.
  uv run python scripts/export-sql.py --check \
    || die "docs/sql is out of date; run: uv run python scripts/export-sql.py"
  uv run python scripts/export-sql.py --verify \
    || die "a documented SQL example does not run against the live database"

  # Backup/restore round-trip. A backup that cannot be restored is not a backup,
  # and the restore path is the non-trivial half: it recreates the schema from
  # the manifest and rebuilds line protocol from CSV. Restores into a scratch
  # database, never into `solar` -- a verification step has no business being
  # able to overwrite live data.
  step "Backup and restore round-trip"
  if docker compose ps --status running --services 2>/dev/null | grep -qx influxdb; then
    backup_roundtrip
  else
    warn "InfluxDB is not running; skipped the backup round-trip"
  fi
}

# --- backup round-trip --------------------------------------------------------
#
# Back up to a throwaway directory, restore into a scratch database, drop it.
#
# The cleanup state is global rather than local, and that is deliberate. A
# `trap ... RETURN` inside the function re-fires after the function's locals are
# gone, so with `set -u` it died on `scratch: unbound variable` — after the
# database had already been dropped, so the cleanup had worked and the trap then
# errored trying to repeat itself. Globals make the cleanup idempotent and
# independent of scope, and the trap is cleared explicitly on the happy path.
SCRATCH_DB=""
SCRATCH_DIR=""
SCRATCH_TOKEN=""

scratch_cleanup() {
  [[ -z "$SCRATCH_DB" && -z "$SCRATCH_DIR" ]] && return 0
  if [[ -n "$SCRATCH_DB" && -n "$SCRATCH_TOKEN" ]]; then
    docker compose exec -T influxdb influxdb3 delete database "$SCRATCH_DB" \
      --host https://localhost:8181 --tls-no-verify --token "$SCRATCH_TOKEN" -y \
      >/dev/null 2>&1 || true
  fi
  [[ -n "$SCRATCH_DIR" ]] && rm -rf "$SCRATCH_DIR"
  SCRATCH_DB=""; SCRATCH_DIR=""; SCRATCH_TOKEN=""
}

backup_roundtrip() {
  SCRATCH_DB="${BACKUP_SCRATCH_DB:-sunscope_backup_check}"
  SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/solar-backup.XXXXXX")"

  # The token is a credential, so it is read from the file rather than passed on
  # a command line, where it would show up in `ps` output.
  if [[ ! -f secrets/admin-token ]]; then
    warn "no secrets/admin-token; skipped the backup round-trip"
    scratch_cleanup
    return 0
  fi
  SCRATCH_TOKEN="$(python3 -c "import json;print(json.load(open('secrets/admin-token'))['token'])")"

  # EXIT rather than RETURN, so it also covers `die` exiting the whole script.
  trap scratch_cleanup EXIT

  local src rows
  uv run --project api python scripts/backup.py --backup --dir "$SCRATCH_DIR" \
    || die "backup failed"
  src="$(find "$SCRATCH_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [[ -n "$src" ]] || die "backup produced no directory"
  uv run --project api python scripts/backup.py --restore "$src" \
    --database "$SCRATCH_DB" --create-database \
    || die "restore failed — a backup that cannot be restored is not a backup"

  # A restore that "succeeds" but writes nothing is the failure mode worth
  # catching, so confirm the scratch database is actually readable.
  rows="$(docker compose exec -T influxdb influxdb3 query \
    'SELECT COUNT(*) AS n FROM inverter_telemetry' \
    --host https://localhost:8181 --tls-no-verify \
    --database "$SCRATCH_DB" --token "$SCRATCH_TOKEN" 2>/dev/null \
    | awk -F'|' 'NF>2 {gsub(/ /,"",$2); if ($2 ~ /^[0-9]+$/ && $2 != "n") print $2}' | tail -1)"
  if [[ -z "${rows:-}" || "$rows" == "0" ]]; then
    die "restore wrote no inverter_telemetry rows"
  fi
  ok "restored $rows inverter_telemetry rows into $SCRATCH_DB"

  scratch_cleanup
  trap - EXIT
}

influx_token() {
  [[ -f secrets/admin-token ]] || return 1
  python3 -c 'import json;print(json.load(open("secrets/admin-token"))["token"])' 2>/dev/null
}

# Returns the single data row of an aggregate query, with the header and the
# ASCII border discarded. Trimming here rather than at each call site is what
# keeps the callers correct: awk over the full output yields the column name on
# line one and the value on line two, so a caller that forgets `tail -1` gets a
# two-line string that fails a numeric test and silently reads as zero.
#
# Single-row only. A query returning several rows would be truncated.
query_influx() {
  docker compose exec -T influxdb influxdb3 query "$1" \
    --host https://localhost:8181 --tls-no-verify \
    --database "${INFLUX_DB:-solar}" --token "$(influx_token)" 2>/dev/null \
    | grep '^|' | tail -1
}

# --- disk usage and growth ----------------------------------------------------
#
# Reported rather than acted on, because InfluxDB 3 Core cannot delete rows and
# never reclaims disk (see docs/10-retention.md). The point of this command is
# that the "1.29 GB/year" figure in the docs stays a measured number rather than
# becoming folklore, and that nobody rediscovers the no-delete constraint by
# hitting it.
#
# Bytes come from system.parquet_files, which is the same accounting InfluxDB
# uses internally, rather than du on the volume: du includes the WAL, caches and
# metadata, which are fixed overhead and make real growth look worse than it is.
show_disk() {
  step "Disk usage"

  local volume rows bytes age
  volume="$(docker volume ls -q | grep -m1 'influx' || true)"
  if [[ -z "$volume" ]]; then
    warn "no influxdb volume found"
    return 0
  fi
  info "volume: $volume"

  local on_disk
  on_disk="$(docker compose exec -T influxdb sh -c 'du -sm /var/lib/influxdb3 2>/dev/null | cut -f1' 2>/dev/null | tr -d ' ')"
  [[ -n "$on_disk" ]] && info "on disk (incl. WAL/caches): ${on_disk} MB"

  # Row counts come from the tables themselves, not from system.parquet_files.
  # Parquet accounting lags badly on a fresh volume -- freshly written data sits
  # in the object store until a compaction pass, which can be many minutes, so
  # reading rows from there reports zero on a database that plainly has data.
  local counted=0 t n
  for t in inverter_telemetry string_telemetry site_rollup weather_station events; do
    n="$(query_influx "SELECT COUNT(*) AS n FROM $t" | awk -F'|' '{gsub(/ /,"",$2);print $2}')"
    [[ "$n" =~ ^[0-9]+$ ]] && counted=$((counted + n))
  done
  info "telemetry: ${counted} rows"

  # Bytes: prefer InfluxDB's own parquet accounting, which counts only data.
  #
  # When that is unavailable -- a fresh volume can go a long time before the
  # first compaction -- du on the volume is a poor substitute for a per-row
  # figure: it includes the WAL, caches and metadata, and dividing that overhead
  # across the row count understates bytes-per-row rather than overstating it.
  # So the fallback does not silently produce a number. It uses the rate measured
  # in docs/10-retention.md and says so.
  local measured_bpr=358
  if [[ "${bytes:-0}" -gt 0 ]]; then
    info "on disk: $(awk -v b="$bytes" 'BEGIN{printf "%.2f", b/1e6}') MB of Parquet data"
  else
    warn "system.parquet_files is empty: nothing has been compacted on this volume yet,"
    warn "so a real byte count is not available. Using the measured rate instead."
    bytes=$(( counted * measured_bpr ))
  fi

  # Growth needs a time span, so take it from the data rather than assuming the
  # container start time -- the volume outlives any single run.
  local age
  age="$(query_influx 'SELECT (EXTRACT(EPOCH FROM (MAX(time) - MIN(time))) / 3600.0) AS h FROM inverter_telemetry' \
    | awk -F'|' '{gsub(/ /,"",$2);print $2}')"

  if awk -v h="${age:-0}" 'BEGIN{exit !(h > 0.5)}' && [[ "$counted" -gt 0 ]]; then
    local per_year hours
    per_year="$(awk -v r="$counted" -v b="$bytes" -v h="$age" \
      'BEGIN{printf "%.0f", (r/h)*24*365*b/r/1e6}')"
    hours="$(awk -v h="$age" 'BEGIN{printf "%.1f", h}')"
    info "over ${hours} h of data => ~${per_year} MB/year"
    # Framed over ten years rather than "years to fill 1 GB": at ~1.3 GB/year a
    # 1 GB threshold reads as alarming when it is not a constraint at all, and a
    # ten-year horizon is the scale anyone actually cares about for capacity.
    awk -v yr="$per_year" -v mb="${on_disk:-0}" 'BEGIN{
      printf "    %d MB used => about %.1f GB over 10 years\n", mb, yr*10/1024
    }'
    if [[ "$per_year" -lt 10000 ]]; then
      echo "    not a concern for this workload; see docs/10-retention.md"
    fi
  else
    info "not enough data yet to project a growth rate"
  fi

  echo
  warn "InfluxDB 3 Core cannot DELETE rows and never reclaims disk."
  warn "Reclaiming means rebuilding the volume -- see docs/10-retention.md"
}

# --- full-year physics sweep --------------------------------------------------
#
# Simulates 365 days at 30-minute resolution and checks every physics invariant across
# the whole year, rather than the five representative days the fast suite uses. ~90 s,
# which is why it is a separate command and a nightly workflow rather than part of `test`.
run_sweep() {
  step "Full-year physics sweep"
  ( cd sim && uv run pytest tests/test_physics_sweep.py -v --durations=5 ) \
    || die "the full-year physics sweep failed"
}

# --- exposure audit ----------------------------------------------------------
#
# Everything here had only ever been exercised on 127.0.0.1, which hid a real
# vulnerability: the raw-SQL guard treated all of RFC 1918 as local, so any client on
# the LAN could run arbitrary SQL with an admin-scoped token. scripts/check-exposure.py
# found it by probing from a real non-loopback address.
# "$@" is forwarded so `exposure --test` actually probes from a LAN address. It used to be
# dropped here, which made the flag silently do nothing and print a hint telling you to
# try it -- the most annoying possible failure mode for a command whose whole job is to
# tell you whether you are exposed.
check_exposure() {
  step "Exposure audit${1:+ ($1)}"
  uv run --project api python scripts/check-exposure.py "$@" \
    || die "something is reachable off-loopback, or the security model does not hold"
  echo
  if [[ "${1:-}" != "--test" ]]; then
    info "add --test to also start a throwaway instance and probe it from a LAN address"
  fi
}

# --- accounts ----------------------------------------------------------------
#
# A thin wrapper over the CLI in api/src/solar_api/users.py. The CLI is the real
# interface and gen-secrets.sh uses it directly; this exists so the password is read
# with echo off and hashed in one step, rather than being assembled out of a pipeline
# that a reader has to trust.
add_user() {
  local username="$1" role="${2:-viewer}"
  case "$role" in
    admin|viewer) ;;
    *) die "role must be 'admin' or 'viewer', not '$role'" ;;
  esac
  [[ -f api/config/users.yaml ]] \
    || die "no api/config/users.yaml yet. Run './scripts/bootstrap.sh up' first."

  step "Adding $username ($role)"
  # Reading the password here rather than in the caller keeps it out of the process
  # table and the shell history, and getpass does not echo it.
  local password
  printf 'Password for %s: ' "$username" >&2
  read -r -s password
  printf '\n' >&2
  [[ -n "$password" ]] || die "empty password"

  printf '%s' "$password" \
    | (cd api && uv run python -m solar_api.users \
        --add config/users.yaml --user "$username" --role "$role") \
    || die "could not add $username"
  echo
  info "takes effect at the next login. Existing sessions keep their old role until"
  info "their token expires, or until API_SECRET_KEY is rotated."
}

usage() {
  cat <<'USAGE'
solar farm simulator — bootstrap

  up         bring the whole system up (default). Idempotent.
  down       stop the containers and the simulator, keep all data
  reset      destroy everything including the database and secrets
  status     show what is running, and the endpoints
  disk       storage used and the projected growth rate
  exposure   audit what is reachable off-loopback; --test probes it live
  sweep      full-year physics sweep (~90 s); runs nightly in CI
  user NAME [admin|viewer]
              add an account to the API user file (default: viewer)
  test       run every test suite and verification script
  sim:start  start the simulator on the host
  sim:stop   stop the simulator
  help       this message

First run: `up` generates secrets, builds the PWA, starts the stack, and
launches the simulator. It takes a few minutes while images build.
USAGE
}

cmd_up() {
  require_tools
  prepare_secrets
  build_web
  start_infrastructure
  start_simulator || warn "the stack is up but the simulator is not; check sim.log"
  show_status
  cat <<'DONE'

Next: open the dashboard and sign in.

  dashboard   http://127.0.0.1:8000/
  username    admin
  password    the API_ADMIN_PASSWORD value in .env

To see alerting catch a real fault:

  ./scripts/bootstrap.sh sim:stop
  uv run --project api --with paho-mqtt python scripts/inject-fault.py comms-loss
  docker compose logs -f api | grep ALERT
DONE
}

cmd_down() {
  stop_simulator
  step "Stopping containers"
  docker compose down >/dev/null 2>&1 || true
  ok "data volume kept; `reset` removes it"
}

cmd_reset() {
  warn "this destroys the database, all tokens and the TLS certificate"
  read -r -p "    type 'yes' to continue: " reply
  [[ "$reply" == "yes" ]] || { info "aborted"; return 0; }
  stop_simulator
  docker compose down -v >/dev/null 2>&1 || true
  rm -rf secrets .env web/dist sim.log .sim.pid
  ok "removed: database volume, secrets, .env, web/dist, sim.log"
  info "run './scripts/bootstrap.sh up' for a clean rebuild"
}

case "${1:-up}" in
  up)         cmd_up ;;
  down)       cmd_down ;;
  reset)      cmd_reset ;;
  status)     show_status ;;
  disk)       show_disk ;;
  exposure)   check_exposure "${@:2}" ;;
  sweep)      run_sweep ;;
  user)       [[ $# -ge 2 ]] || { usage; die "usage: bootstrap.sh user NAME [admin|viewer]"; }
             add_user "$2" "${3:-viewer}" ;;
  test)       run_tests ;;
  sim:start)  start_simulator ;;
  sim:stop)   stop_simulator ;;
  help|-h|--help) usage ;;
  *)          usage; die "unknown command: $1" ;;
esac
