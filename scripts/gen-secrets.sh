#!/usr/bin/env bash
# Generate real secrets and write them to .env.
# Safe to re-run: only fills in values still set to a "change-me" placeholder.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "==> .env not found, creating from .env.example"
  cp .env.example .env
  chmod 600 .env
fi

set_if_placeholder() {
  local key="$1" value="$2"
  if grep -qE "^${key}=change-me" .env; then
    if grep -qE "^${key}=" .env; then
      # BSD/GNU sed -i differ; write via temp file for portability.
      local tmp
      tmp=$(mktemp)
      sed "s|^${key}=.*|${key}=${value}|" .env > "$tmp"
      mv "$tmp" .env
    else
      printf '%s=%s\n' "$key" "$value" >> .env
    fi
    echo "    set ${key}"
  fi
}

# InfluxDB tokens must literally begin with `apiv3_` or the server rejects them
# with "Invalid token format". That format is specific to InfluxDB.
influx_token() { printf 'apiv3_%s' "$(openssl rand -hex 32)"; }

# Everything else gets its own generator, so a dashboard password cannot be
# mistaken for a database credential. They used to all come from one `token()`
# helper, which meant every secret in .env was prefixed `apiv3_` -- including
# the API admin password and the JWT signing key. Nothing was shared or derived
# across trust boundaries, but a secret that looks like a token invites being
# pasted into the wrong field, and reading one as a token when it is not.
# 32 hex bytes = 128 bits, comfortably above any local-development need.
password() { openssl rand -hex 16; }
secret_key() { openssl rand -hex 32; }

echo "==> Generating secrets"
set_if_placeholder INFLUX_ADMIN_TOKEN       "$(influx_token)"
set_if_placeholder EMQX_DASHBOARD_PASSWORD  "$(password)"
set_if_placeholder GRAFANA_ADMIN_PASSWORD   "$(password)"
set_if_placeholder API_SECRET_KEY           "$(secret_key)"
set_if_placeholder API_ADMIN_PASSWORD       "$(password)"

# The InfluxDB server reads its offline admin token from a file, so the token
# never appears in the container environment.
#
# The file must be JSON, and the token must start with "apiv3_". The exact
# schema is {"token": ..., "name": ..., "expiration": ...}, where expiration
# may be omitted. See the comment in docker-compose.yml.
echo "==> Writing secrets/admin-token"
mkdir -p secrets
# Always rewrite from .env rather than only when absent. Skipping this when the
# file already exists lets the file and .env drift apart, which produces a 401
# that is genuinely hard to trace back to this script.
admin=$(grep -E '^INFLUX_ADMIN_TOKEN=' .env | cut -d= -f2-)
printf '{"token":"%s","name":"solar-admin","expiration":null}\n' "$admin" > secrets/admin-token
# The server warns (and refuses on some builds) if the file is group/world readable.
chmod 600 secrets/admin-token

# VAPID key pair for Web Push, if pywebpush/nacl is available.
if grep -qE '^VAPID_PRIVATE_KEY=$' .env; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import nacl' 2>/dev/null; then
    keys=$(python3 - <<'PY'
from nacl.signing import SigningKey
from base64 import urlsafe_b64encode
sk = SigningKey.generate()
def b64(b): return urlsafe_b64encode(b).decode().rstrip("=")
print(b64(bytes(sk.verify_key)), b64(bytes(sk)))
PY
)
    pub=$(echo "$keys" | cut -d' ' -f1)
    priv=$(echo "$keys" | cut -d' ' -f2)
    set_if_placeholder VAPID_PUBLIC_KEY  "$pub"
    set_if_placeholder VAPID_PRIVATE_KEY "$priv"
  else
    echo "    pynacl not installed; VAPID keys left empty (Web Push disabled)"
  fi
fi

chmod 600 .env
echo "==> .env ready (mode 600). Do not commit it."

# --- the user file -----------------------------------------------------------
# API_ADMIN_PASSWORD above is the *fallback* account: a plaintext password compared in
# constant time, and reported by the API at startup as a degraded mode. Hashing it into
# api/config/users.yaml is the fix, so do it here rather than leaving it as a step nobody
# ever performs.
#
# Created only when absent, never overwritten. Re-hashing on every run would write a fresh
# digest for whatever API_ADMIN_PASSWORD currently says, and since the file is
# authoritative once it exists, that silently reinstates a password an operator may have
# deliberately rotated out of it.
USERS_FILE=api/config/users.yaml
ADD_USER_HINT="cd api && uv run python -m solar_api.users --add config/users.yaml --user NAME --role viewer"

if [[ -f "$USERS_FILE" ]]; then
  echo "==> $USERS_FILE exists; leaving it alone"
  echo "    add a user with: $ADD_USER_HINT"
elif ! command -v uv >/dev/null 2>&1; then
  echo "==> uv not found; skipping $USERS_FILE. The .env fallback account still works."
else
  echo "==> Creating $USERS_FILE with a hashed admin account"
  mkdir -p api/config
  api_password=$(grep -E '^API_ADMIN_PASSWORD=' .env | cut -d= -f2-)

  # The header is written inline rather than kept as a template file, so the explanation
  # of the role boundary lives at the point someone edits the file. A headerless
  # users.yaml is how a plaintext password ends up back in one.
  #
  # `users:` and the entry are both produced here, and the entry comes from the CLI with
  # --add - so the hash and its formatting have exactly one owner. Piped rather than
  # passed as an argument: an argument is visible in `ps` and lands in shell history.
  {
    cat <<'HEADER'
# API users. Generated by scripts/gen-secrets.sh.
#
# Passwords here are PBKDF2-HMAC-SHA256 digests, never plaintext. To add or change one:
#
HEADER
    printf '#   %s\n' "$ADD_USER_HINT"
    cat <<'HEADER'
#
# Roles:
#
#   admin   everything, including /api/explore
#   viewer  read telemetry, alerts and the live feed. No raw SQL.
#
# /api/explore is the boundary. It runs arbitrary SQL through an admin-scoped InfluxDB
# token, because InfluxDB 3 Core issues no permission-scoped tokens
# (docs/04-security.md 4.4). A viewer can watch the farm all day without being able to
# run a query against it.
#
# Read on each login and cached on mtime, so an edit applies without a restart. A role
# change takes effect at the next login, or immediately for every session if
# API_SECRET_KEY is rotated.
#
# This file is gitignored, because it holds digests of real passwords and a digest is
# offline-crackable material. api/config/users.yaml.example is the committed documentation.
#
# The file is authoritative: while it exists, API_ADMIN_PASSWORD is ignored, so rotating a
# digest here cannot be undone by falling back to a stale .env password.
users:
HEADER
    printf '%s' "$api_password" \
      | (cd api && uv run python -m solar_api.users --user admin --role admin --add -)
  } >"$USERS_FILE"

  echo "    created. API_ADMIN_PASSWORD is now ignored by the API; the file is authoritative."
  echo "    add a read-only account with: $ADD_USER_HINT"
fi
