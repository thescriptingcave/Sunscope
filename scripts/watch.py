"""A live terminal dashboard, reading the same data path as the PWA.

The PWA is the real interface, but a terminal view is useful in its own right:
over SSH, in a container, on a machine with no browser, and when you want to
watch a fault develop without alt-tabbing.

It subscribes to MQTT directly over the same WebSocket listener the browser
uses, so what you see here is the live path, not a database read. The alert
figures come from the API, since the rule engine lives there.

    uv run --project api --with paho-mqtt python scripts/watch.py
    uv run --project api --with paho-mqtt python scripts/watch.py --once

Ctrl-C to exit. ANSI colour is dropped automatically when stdout is not a TTY,
so piping to a file gives clean text.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

HOST = os.environ.get("MQTT_WS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MQTT_WS_PORT", "8083"))
SITE = os.environ.get("ALERT_SITE", "mojave")
API = os.environ.get("API_BASE", "http://127.0.0.1:8000")

INTERVALS = {
    "inverter_telemetry": f"solar/{SITE}/block/+/inverter/+/telemetry",
    "site_rollup": f"solar/{SITE}/rollup",
    "weather": f"solar/{SITE}/weather/+/telemetry",
}
RATED_W = 250_000.0

_state: dict[str, Any] = {"readings": {}, "rollup": None, "weather": None}
_updated = 0.0
_lock = threading.Lock()
_stop = threading.Event()


class C:
    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s):    return self._w("1", s)
    def dim(self, s):     return self._w("2", s)
    def red(self, s):     return self._w("31", s)
    def green(self, s):   return self._w("32", s)
    def yellow(self, s):  return self._w("33", s)
    def blue(self, s):    return self._w("36", s)
    def mag(self, s):     return self._w("35", s)


def on_connect(client, userdata, flags, reason_code, properties=None):
    for f in INTERVALS.values():
        client.subscribe(f, qos=0)


def on_message(client, userdata, message):
    global _updated
    try:
        payload = json.loads(message.payload)
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    with _lock:
        if message.topic.endswith("/rollup"):
            _state["rollup"] = payload
        elif "/weather/" in message.topic:
            _state["weather"] = payload
        elif message.topic.endswith("/telemetry") and payload.get("inverter_id"):
            _state["readings"][payload["inverter_id"]] = payload
        _updated = time.time()


def bar(fraction: float, width: int = 28, colour=None) -> str:
    """A proportional bar. Clipped, not scaled, so overload is visible."""
    filled = max(0, min(width, round(fraction * width)))
    body = "█" * filled + "·" * (width - filled)
    return colour(body) if colour else body


def fetch_json(path: str, token: str) -> dict | None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(API + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def read_token() -> str:
    """Log in once so alerts can be shown. Silent if it fails."""
    import urllib.request

    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    password = ""
    if os.path.exists(env):
        for line in open(env):
            if line.startswith("API_ADMIN_PASSWORD="):
                password = line.split("=", 1)[1].strip()
    if not password:
        return ""
    body = json.dumps({"username": "admin", "password": password}).encode()
    req = urllib.request.Request(
        API + "/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310
            return json.loads(r.read())["token"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


def render(col: C, token: str, once: bool) -> None:
    with _lock:
        readings = dict(_state["readings"])
        rollup = _state["rollup"]
        weather = _state["weather"]
        updated = _updated

    out = []
    out.append(col.bold(f"  SOLAR FARM — {SITE}   1.0 MWac / 1.19 MWp"))
    age = time.time() - updated if updated else None
    if age is None:
        out.append(col.dim("  waiting for telemetry…"))
    elif age > 45:
        out.append(col.red(f"  no data for {age:.0f}s — the publisher may have stopped"))
    else:
        out.append(col.dim(f"  live over MQTT/WebSocket, updated {age:.0f}s ago"))
    out.append("")

    # --- site KPIs ---------------------------------------------------------
    if rollup:
        total = rollup.get("total_ac_power_w") or 0.0
        pr = rollup.get("pr_ratio") or 0.0
        yield_kwh = rollup.get("daily_yield_kwh") or 0.0
        online = rollup.get("inverters_online", 0)
        strings = rollup.get("strings_online", 0)

        out.append(col.bold("  SITE"))
        out.append(f"    {'power':<11}{total/1000:8.1f} kW  "
                   f"{bar(total/1_000_000, colour=col.blue)}")
        pr_colour = col.green if pr >= 0.85 else col.yellow if pr >= 0.7 else col.red
        out.append(f"    {'perf.ratio':<11}{pr_colour(f'{pr:8.3f}')}  "
                   f"{bar(pr, colour=pr_colour)}")
        out.append(f"    {'yield':<11}{yield_kwh:8.1f} kWh today")
        out.append(f"    {'online':<11}{online} inverters, {strings} strings")
        out.append("")

    # --- weather -----------------------------------------------------------
    if weather:
        ghi = weather.get("ghi") or 0.0
        out.append(col.bold("  WEATHER"))
        out.append(f"    {'GHI':<11}{ghi:8.1f} W/m²  {bar(ghi/1100, colour=col.yellow)}")
        out.append(f"    {'air':<11}{(weather.get('air_temp_c') or 0):8.1f} °C      "
                   f"wind {(weather.get('wind_speed_mps') or 0):.1f} m/s")
        out.append("")

    # --- inverters ---------------------------------------------------------
    if readings:
        out.append(col.bold("  INVERTERS"))
        for inv_id in sorted(readings):
            r = readings[inv_id]
            ac = r.get("ac_power_w") or 0.0
            eff = r.get("efficiency") or 0.0
            temp = r.get("heatsink_temp_c") or 0.0
            status = r.get("status_code", 0)
            clipping = bool(r.get("clipping"))

            # Heatsink turns amber past the derating onset at 70 C.
            temp_colour = col.red if temp >= 90 else col.yellow if temp >= 70 else col.dim
            bcol = col.yellow if clipping else col.blue
            tag = col.yellow("CLIP") if clipping else (
                col.red("FAULT") if status == 5 else col.dim(str(status)))

            out.append(
                f"    {inv_id:<11}{ac/1000:7.1f} kW "
                f"{bar(ac/RATED_W, 18, colour=bcol)} "
                f"eff {eff*100:5.1f}%  {temp_colour(f'{temp:5.1f}°C')}  {tag}"
            )
        out.append("")

    # --- alerts ------------------------------------------------------------
    alerts = fetch_json("/api/alerts", token) if token else None
    stats = fetch_json("/api/alert-stats", token) if token else None
    out.append(col.bold("  ALERTS"))
    if stats is None:
        out.append(col.dim("    (API not reachable)"))
    else:
        if not stats["engine_connected"]:
            out.append(col.red("    ENGINE OFFLINE — nothing is being watched for"))
        elif alerts and alerts["count"]:
            for a in alerts["alerts"]:
                c = {"critical": col.red, "warning": col.yellow}.get(a["severity"], col.dim)
                out.append(f"    {c('● ' + a['severity'].upper())} "
                           f"{a['rule_id']}  {a['subject']}  "
                           f"value={a['value']} threshold={a['threshold']}")
        else:
            out.append(col.green(f"    all clear  ({stats['received']} readings evaluated, "
                                 f"{stats['errors']} errors)"))
    out.append("")

    print("\033[2J\033[H" if not once else "", end="")
    print("\n".join(out), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="render a single frame and exit")
    parser.add_argument("--no-colour", "--no-color", action="store_true")
    args = parser.parse_args()

    col = C(not args.no_colour and sys.stdout.isatty())

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1, client_id="solar-watch", transport="websockets"
    )
    client.ws_set_options(path="/mqtt")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(HOST, PORT, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach the broker at {HOST}:{PORT} — {exc}", file=sys.stderr)
        return 1
    client.loop_start()

    token = read_token()
    if not token:
        print(col.dim("  (no API login; alerts will be unavailable)"), file=sys.stderr)

    # Wait for the first inverter readings before rendering. Without this,
    # `--once` snapshots a fraction of a second after connecting and shows an
    # empty dashboard, because the simulator publishes on a 30 s interval.
    # The wait is on *readings* specifically: the site rollup is published at a
    # different point in the tick, so accepting it alone renders a frame with a
    # summary and no per-inverter detail.
    deadline = time.time() + 45
    while time.time() < deadline and not _stop.is_set():
        with _lock:
            ready = len(_state["readings"]) >= 4
        if ready:
            break
        time.sleep(0.5)

    def stop(_s, _f):
        _stop.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while not _stop.is_set():
            render(col, token, args.once)
            if args.once:
                return 0
            for _ in range(10):
                if _stop.is_set():
                    return 0
                time.sleep(0.1)
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
