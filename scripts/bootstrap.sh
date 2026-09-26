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

  wait_for_container solar-telegraf "running" 120 \
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
  ( cd sim && uv run pytest -q ) || die "simulator tests failed"
  ( cd api && uv run pytest -q ) || die "API tests failed"
  ok "lint"
  ( cd sim && uv run ruff check src/ tests/ --output-format=concise ) || die "sim lint failed"
  ( cd api && uv run ruff check src/ tests/ --output-format=concise ) || die "api lint failed"
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

  # docs/sql is generated from docs/06-sql-examples.md, so a stale file means the
  # runnable copies and the teaching document have diverged.
  uv run python scripts/export-sql.py --check \
    || die "docs/sql is out of date; run: uv run python scripts/export-sql.py"
}

usage() {
  cat <<'USAGE'
solar farm simulator — bootstrap

  up         bring the whole system up (default). Idempotent.
  down       stop the containers and the simulator, keep all data
  reset      destroy everything including the database and secrets
  status     show what is running, and the endpoints
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
  test)       run_tests ;;
  sim:start)  start_simulator ;;
  sim:stop)   stop_simulator ;;
  help|-h|--help) usage ;;
  *)          usage; die "unknown command: $1" ;;
esac
