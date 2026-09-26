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
