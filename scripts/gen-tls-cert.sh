#!/usr/bin/env bash
# Generate a self-signed certificate for InfluxDB.
#
# WHY THIS EXISTS
#
# Grafana's InfluxDB datasource queries InfluxDB 3 Core over Flight SQL, which
# is gRPC, and gRPC **requires TLS**. Against a plaintext InfluxDB the datasource
# fails with:
#
#   flightsql: rpc error: code = Unavailable desc = connection error:
#   desc = "transport: authentication handshake failed:
#           tls: first record does not look like a TLS handshake"
#
# The InfluxDB documentation mentions an "Insecure Connection" option for
# running SQL without TLS. That option does not work for the gRPC path in
# Grafana 12.2.0 -- verified by trying every plausible provisioning key
# (insecureConnection, insecureSkipVerify, allowInsecure, insecureGrpc,
# queryLanguage, qlVersion, lang) and each product value; the transport stayed
# on Flight SQL every time.
#
# So the server gets real TLS. That is the supported configuration, and it also
# means the "internet exposed" column of the security posture table is reachable
# later without a second round of changes.
#
# This is a self-signed cert for local development. It is not a substitute for
# a real certificate if this is ever exposed.
set -euo pipefail

cd "$(dirname "$0")/.."
TLS_DIR="secrets/tls"
mkdir -p "$TLS_DIR"
chmod 700 "$TLS_DIR"

REGENERATE=0
if [[ -f "$TLS_DIR/server.crt" && "${FORCE:-0}" != "1" ]]; then
  echo "==> Certificate already present (FORCE=1 to regenerate)"
else
  REGENERATE=1
fi

# SANs matter: a cert with only a CN is rejected by modern Go clients, and
# Grafana's gRPC stack is Go.
if [[ "$REGENERATE" == "1" ]]; then
HOSTS="localhost,influxdb,127.0.0.1"
echo "==> Generating self-signed certificate for: ${HOSTS}"

openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$TLS_DIR/server.key" \
  -out    "$TLS_DIR/server.crt" \
  -subj "/CN=solar-influxdb" \
  -addext "subjectAltName=DNS:localhost,DNS:influxdb,IP:127.0.0.1" \
  2>/dev/null

chmod 600 "$TLS_DIR/server.key"
chmod 644 "$TLS_DIR/server.crt"
fi

# Build a combined trust bundle: the container's system CAs plus ours.
#
# Grafana's Flight SQL client does NOT read TLS settings from the datasource
# jsonData -- insecureSkipVerify, tlsSkipVerify, tlsCACert and tlsCAData are all
# ignored for the gRPC dial (verified by trying each). The gRPC stack uses Go's
# crypto/x509, which honours the SSL_CERT_FILE environment variable, so that is
# the only lever available without rebuilding the image or running as root.
#
# `system-ca.crt` must exist first. It used to be assumed present, and on a clean
# checkout it is not -- which meant the bundle was silently skipped, every
# InfluxDB consumer failed the TLS handshake, and the only symptom was an
# opaque ConnectError from the API. It is now sourced from the host CA store
# when missing.
if [[ ! -f "$TLS_DIR/system-ca.crt" ]]; then
  echo "==> Collecting host CA certificates"
  CA_STORE=""
  for candidate in \
    /etc/ssl/cert.pem \
    /etc/pki/tls/certs/ca-bundle.crt \
    /etc/ssl/certs/ca-certificates.crt \
    /usr/local/etc/openssl/cert.pem
  do
    if [[ -f "$candidate" ]]; then CA_STORE="$candidate"; break; fi
  done
  if [[ -z "$CA_STORE" ]]; then
    echo "    no host CA store found; bundle will hold only our cert" >&2
    : > "$TLS_DIR/system-ca.crt"
  else
    cp "$CA_STORE" "$TLS_DIR/system-ca.crt"
    echo "    from $CA_STORE ($(grep -c 'BEGIN CERTIFICATE' "$TLS_DIR/system-ca.crt") certs)"
  fi
fi

# Build the bundle, skipping the append if our cert is already in the system
# roots so re-running cannot accumulate duplicates.
#
# De-duplication is by SHA-256 fingerprint, not by matching certificate text.
# `grep -F` matches *substrings*, so a naive "strip our cert from the system
# file" removes the `-----BEGIN CERTIFICATE-----` and `-----END CERTIFICATE-----`
# lines of all 128 system roots and leaves a file containing no complete
# certificates at all. That still served InfluxDB, because only our own cert was
# then appended -- and quietly broke anything needing a public CA.
SERVER_FP="$(openssl x509 -in "$TLS_DIR/server.crt" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
cp "$TLS_DIR/system-ca.crt" "$TLS_DIR/ca-bundle.crt"

if [[ -n "$SERVER_FP" ]] && openssl crl2pkcs7 -nocrl -certfile "$TLS_DIR/system-ca.crt" 2>/dev/null \
    | openssl pkcs7 -print_certs -noout -fingerprint -sha256 2>/dev/null | grep -qF "$SERVER_FP"; then
  echo "==> Server certificate already present in the system roots; not appending"
else
  cat "$TLS_DIR/server.crt" >> "$TLS_DIR/ca-bundle.crt"
fi

# Postcondition, not optimism. The API, Telegraf and Grafana are each configured
# to trust exactly this file, so its absence is a total outage that otherwise
# surfaces much later as an unexplained connection error.
if [[ ! -s "$TLS_DIR/ca-bundle.crt" ]]; then
  echo "ERROR: $TLS_DIR/ca-bundle.crt was not created" >&2
  exit 1
fi
if ! openssl verify -CAfile "$TLS_DIR/ca-bundle.crt" "$TLS_DIR/server.crt" >/dev/null 2>&1; then
  echo "ERROR: ca-bundle.crt does not verify the server certificate" >&2
  exit 1
fi
# A bundle holding only our own cert still serves InfluxDB, so this failure is
# invisible until something needs a public CA. Assert the system roots survived.
SYSTEM_CERTS="$(grep -c 'BEGIN CERTIFICATE' "$TLS_DIR/system-ca.crt" || echo 0)"
BUNDLE_CERTS="$(grep -c 'BEGIN CERTIFICATE' "$TLS_DIR/ca-bundle.crt" || echo 0)"
if (( SYSTEM_CERTS > 1 && BUNDLE_CERTS < SYSTEM_CERTS )); then
  echo "ERROR: bundle has $BUNDLE_CERTS certs but the host CA store had $SYSTEM_CERTS;" >&2
  echo "       the system roots were stripped and would break any public-CA consumer" >&2
  exit 1
fi
echo "==> Combined bundle: $TLS_DIR/ca-bundle.crt"
echo "    certs in bundle: $BUNDLE_CERTS (system roots: $SYSTEM_CERTS)"

echo "==> Wrote:"
ls -l "$TLS_DIR"
echo
echo "Consumers must now use https:// and skip verification, since the cert is"
echo "self-signed and signed for 'localhost'/'influxdb' only."
