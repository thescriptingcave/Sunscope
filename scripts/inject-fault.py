"""Inject a fault and watch the alert engine catch it.

The most convincing way to verify alerting is to break something on purpose and
watch it get noticed. This does that without editing the simulator or waiting
for a scenario file.

    uv run --project api --with paho-mqtt python scripts/inject-fault.py --list
    uv run --project api --with paho-mqtt python scripts/inject-fault.py overheat
    uv run --project api --with paho-mqtt python scripts/inject-fault.py comms-loss
    uv run --project api --with paho-mqtt python scripts/inject-fault.py dead-inverter
    uv run --project api --with paho-mqtt python scripts/inject-fault.py recover

Each scenario runs until interrupted (Ctrl-C), except `recover`, which restores
healthy readings for ~90 s. Watch the API logs in another terminal:

    docker compose logs -f api | grep ALERT

Or poll the API:

    curl -s localhost:8000/api/alerts -H "Authorization: Bearer $TOKEN"
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt

HOST = "127.0.0.1"
PORT = 1883
SITE = "mojave"
INTERVAL_S = 5.0

#: A healthy reading, from the shape in sim/src/solar_sim/metrics.py. Values are
#: a clear-sky afternoon: clipping, good efficiency, cool heatsink.
HEALTHY: dict[str, Any] = {
    "ac_power_w": 250_000.0,
    "dc_power_w": 300_000.0,
    "ac_voltage_v": 480.0,
    "ac_current_a": 520.8,
    "efficiency": 0.8333,
    "heatsink_temp_c": 26.0,
    "internal_temp_c": 45.0,
    "uptime_s": 86_400.0,
    "status_code": 3,
    "clipping": True,
}

INVERTERS = ("INV-01", "INV-02", "INV-03", "INV-04")

SCENARIOS = {
    "overheat": {
        "inverter": "INV-02",
        "what": "heatsink 96 C, held past both debounces (60 s critical, 90 s warning)",
        "expect": "heatsink_critical then heatsink_high, ~30 s apart",
        "patch": {"heatsink_temp_c": 96.0, "internal_temp_c": 78.0, "ac_power_w": 180_000.0,
                  "dc_power_w": 216_000.0, "clipping": False},
    },
    "dead-inverter": {
        "inverter": "INV-03",
        "what": "online and publishing, reports status 3, outputs nothing",
        "expect": "power_zero_in_sunlight (needs ghi > 200, so the weather station must be up)",
        "patch": {"ac_power_w": 0.0, "dc_power_w": 0.0, "efficiency": 0.0, "clipping": False},
    },
    "comms-loss": {
        "inverter": "INV-04",
        "what": "telemetry simply stops, with no offline status and no Last Will",
        "expect": "telemetry_stale after 120 s -- the case a Last Will cannot catch",
        "patch": None,  # silence, rather than a bad value
    },
    "hot-weather": {
        "inverter": "INV-01",
        "what": "heatsink 84 C: hot but under the critical threshold",
        "expect": "heatsink_high only, no heatsink_critical",
        "patch": {"heatsink_temp_c": 84.0, "internal_temp_c": 70.0},
    },
}


def reading(inverter_id: str, patch: dict[str, Any] | None, when: str) -> str:
    block = "BLK-A" if inverter_id in ("INV-01", "INV-02") else "BLK-B"
    return json.dumps({
        "ts": when,
        "site": SITE,
        "block": block,
        "inverter_id": inverter_id,
        "model": "SG250CX",
        **HEALTHY,
        **(patch or {}),
    })


def topic_for(inverter_id: str) -> str:
    block = "BLK-A" if inverter_id in ("INV-01", "INV-02") else "BLK-B"
    return f"solar/{SITE}/block/{block}/inverter/{inverter_id}/telemetry"


def weather(ghi: float) -> str:
    return json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "site": SITE, "station_id": "WS-01",
        "ghi": ghi, "dni": ghi * 0.85, "dhi": ghi * 0.15,
        "air_temp_c": 28.0, "wind_speed_mps": 2.0,
        "relative_humidity": 0.2, "clearness_index": 1.0,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=[*SCENARIOS, "recover", "list"])
    parser.add_argument("--duration", type=float, default=0, help="seconds to run (0 = until Ctrl-C)")
    args = parser.parse_args()

    if args.scenario == "list":
        print("Scenarios:\n")
        for name, spec in SCENARIOS.items():
            print(f"  {name:<16} {spec['inverter']}  {spec['what']}")
            print(f"  {'':<16} expect: {spec['expect']}\n")
        print("  recover          restore healthy readings for ~90 s, so alerts clear")
        return 0

    if args.scenario == "recover":
        target, patch, stop_after = "all", None, 90.0
    else:
        spec = SCENARIOS[args.scenario]
        target, patch, stop_after = spec["inverter"], spec["patch"], args.duration

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fault-injector")
    client.connect(HOST, PORT, 60)
    client.loop_start()

    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False
        print("\nstopped")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"injecting into {target}. Ctrl-C to stop.")
    if patch is not None:
        print(f"  patch: {patch}")
    else:
        print("  sending nothing for this inverter (simulated comms loss)")
    print("  watching: docker compose logs -f api | grep ALERT")
    if target != "all":
        print(
            "\n  NOTE: stop the real simulator first. If it is running it keeps\n"
            "  publishing for this inverter, and the fault will never appear:\n"
            "    pkill -9 -f solar-sim\n"
        )
    print()

    started = time.monotonic()
    while running:
        elapsed = time.monotonic() - started
        if stop_after and elapsed >= stop_after:
            print(f"done after {elapsed:.0f}s")
            break
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Keep the weather station sunny so irradiance-gated rules can open.
        client.publish(f"solar/{SITE}/weather/WS-01/telemetry", weather(850.0), qos=0)
        for inverter_id in INVERTERS:
            # Feed *every* inverter, so a scenario isolates one device instead of
            # silencing the whole farm. Only the deliberately-silent one is
            # skipped, and only when this scenario is a comms loss.
            silent = patch is None and inverter_id == target
            if silent:
                continue
            client.publish(
                topic_for(inverter_id),
                reading(inverter_id, patch if inverter_id == target else None, stamp),
                qos=0,
            )
        time.sleep(INTERVAL_S)

    # Leave the broker clean: restore healthy readings so alerts resolve.
    print("restoring healthy readings…")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for inverter_id in INVERTERS:
        client.publish(topic_for(inverter_id), reading(inverter_id, None, stamp), qos=0)
    time.sleep(2)
    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
