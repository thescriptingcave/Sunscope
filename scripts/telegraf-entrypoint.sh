#!/bin/sh
# Telegraf entrypoint wrapper.
#
# The InfluxDB token is read from a file rather than passed as an environment
# variable, so it is not visible via `docker inspect` to anyone with access to
# the Docker socket.
set -e

TOKEN_FILE="${INFLUX_WRITE_TOKEN_FILE:-/run/secrets/influx_token}"

if [ ! -r "$TOKEN_FILE" ]; then
  echo "telegraf: cannot read token file $TOKEN_FILE" >&2
  echo "telegraf: run 'docker compose up influx-init' first" >&2
  exit 1
fi

INFLUX_WRITE_TOKEN="$(cat "$TOKEN_FILE")"
export INFLUX_WRITE_TOKEN

# Telegraf expands ${VAR} in the config file at parse time.
exec telegraf --config /etc/telegraf/telegraf.conf "$@"
