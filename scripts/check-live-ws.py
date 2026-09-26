"""Prove the PWA's live path works: MQTT over WebSocket, as the browser does it.

The dashboard subscribes to `ws://localhost:8083/mqtt` straight from the browser.
Nothing else in the stack exercises that path -- Telegraf speaks plain TCP on
1883 -- so a silent break here would leave the live tiles frozen while every
other health check stayed green.

Uses paho-mqtt over its WebSocket transport, which is the same shape of
connection `mqtt.js` makes in the browser: an RFC 6455 upgrade carrying MQTT 3.1.1
frames. A real client is used rather than a hand-rolled one so this checks the
*protocol* works, not that a bespoke encoder happens to agree with itself.

Run: uv run --project api --with paho-mqtt python scripts/check-live-ws.py
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time

import paho.mqtt.client as mqtt

HOST = "127.0.0.1"
PORT = 8083
SITE = "mojave"
WAIT_SECONDS = 60

# Exactly the topics web/src/mqtt/live.ts subscribes to.
SUBSCRIPTIONS = [
    (f"solar/{SITE}/block/+/inverter/+/telemetry", "telemetry"),
    (f"solar/{SITE}/rollup", "rollup"),
    (f"solar/{SITE}/block/+/inverter/+/status", "status"),
]

connected = threading.Event()
subscribed = threading.Event()
subacks = 0
received: dict[str, int] = {}
sample_topic = ""
sample_keys: list[str] = []


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print(f"  FAIL  CONNACK refused: {reason_code}")
        return
    print(f"  OK    connected over WebSocket to {HOST}:{PORT} (reason {reason_code})")
    for topic, _ in SUBSCRIPTIONS:
        client.subscribe(topic, qos=0)
    connected.set()


def on_subscribe(client, userdata, mid, granted_qos, properties=None):
    """One SUBACK per SUBSCRIBE, so count them rather than logging each."""
    global subacks
    subacks += 1
    if any(qos > 2 for qos in granted_qos):
        print(f"  FAIL  broker refused a subscription: {granted_qos}")
    elif subacks == len(SUBSCRIPTIONS):
        print(f"  OK    all {subacks} subscriptions granted at QoS 0")
    if subacks >= len(SUBSCRIPTIONS):
        subscribed.set()


def on_message(client, userdata, message):
    global sample_topic, sample_keys
    kind = next((k for filter_, k in SUBSCRIPTIONS if _wildcard_match(filter_, message.topic)), None)
    if kind is None:
        return
    received[kind] = received.get(kind, 0) + 1
    if not sample_topic:
        sample_topic = message.topic
        try:
            payload = json.loads(message.payload)
            sample_keys = [k for k in ("inverter_id", "total_ac_power_w") if k in payload]
        except json.JSONDecodeError:
            sample_keys = ["<non-JSON>"]


def _wildcard_match(filter_: str, topic: str) -> bool:
    """Match an MQTT topic filter, so a mis-typed wildcard cannot pass silently.

    Substituting a `+` is a regex character, which would quietly make the pattern
    match one character instead of one level.
    """
    pattern = "^" + re.escape(filter_).replace(r"\+", "[^/]+").replace(r"\#", ".*") + "$"
    return re.match(pattern, topic) is not None


def main() -> int:
    # CallbackAPIVersion.VERSION1 keeps the classic (client, userdata, flags,
    # rc) signature, which is stable across paho 1.x and 2.x.
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        client_id="pwa-contract-check",
        transport="websockets",
        protocol=mqtt.MQTTv311,
    )
    client.ws_set_options(path="/mqtt")
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(HOST, PORT, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  WebSocket connect failed: {exc}")
        return 1
    client.loop_start()

    if not connected.wait(15):
        print("  FAIL  no CONNACK within 15 s")
        client.loop_stop()
        return 1
    if not subscribed.wait(15):
        print("  FAIL  no SUBACK within 15 s")
        client.loop_stop()
        return 1

    # Keep listening briefly after the first message so the per-inverter
    # wildcard is exercised too, not just the literal rollup topic.
    print(f"  ..    waiting up to {WAIT_SECONDS}s for live telemetry")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and not received:
        time.sleep(0.5)
    while time.monotonic() < deadline and len(received) < len(SUBSCRIPTIONS):
        time.sleep(0.5)

    client.loop_stop()
    client.disconnect()

    if not received:
        print(f"  FAIL  subscribed but nothing arrived in {WAIT_SECONDS}s -- is the simulator running?")
        return 1

    print(f"  OK    live message on {sample_topic} carrying {sample_keys}")
    print(f"  OK    received {sum(received.values())} messages by kind: {received}")

    # The wildcard subscription is the one that feeds the inverter cards, so a
    # broken `+` would leave the dashboard showing only the site total.
    if "telemetry" not in received:
        print("  FAIL  the per-inverter wildcard matched nothing; inverter tiles would stay empty")
        return 1
    print("\nThe browser live path works: MQTT over WebSocket, with no API or database in the loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
