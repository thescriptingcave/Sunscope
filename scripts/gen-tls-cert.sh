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
# Appending to the bundle is idempotent: the cert is stripped if already there.
if [[ -f "$TLS_DIR/system-ca.crt" ]]; then
  grep -v -F -f <(awk '/BEGIN CERT/{n++} n{print}' "$TLS_DIR/server.crt") \
      "$TLS_DIR/system-ca.crt" > "$TLS_DIR/ca-bundle.crt" || true
  cat "$TLS_DIR/server.crt" >> "$TLS_DIR/ca-bundle.crt"
  echo "==> Combined bundle: $TLS_DIR/ca-bundle.crt"
  grep -c 'BEGIN CERTIFICATE' "$TLS_DIR/ca-bundle.crt" | sed 's/^/    certs in bundle: /'
fi

echo "==> Wrote:"
ls -l "$TLS_DIR"
echo
echo "Consumers must now use https:// and skip verification, since the cert is"
echo "self-signed and signed for 'localhost'/'influxdb' only."
