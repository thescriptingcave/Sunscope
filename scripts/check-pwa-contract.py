"""Verify every endpoint the PWA calls, exactly as the browser will.

Run: uv run --project api python scripts/check-pwa-contract.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")


def env(name: str) -> str:
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def get(path: str, token: str | None = None) -> object:
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())


def post(path: str, body: dict, token: str | None = None) -> object:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    failures: list[str] = []

    token = post("/api/auth/login", {"username": "admin", "password": env("API_ADMIN_PASSWORD")})
    print(f"login            OK  (JWT {len(token['token'])} chars, expires_in={token['expires_in']})s")

    # --- /api/now: the Last Value Cache path ---------------------------------
    now = get("/api/now", token["token"])
    print(f"/api/now         source={now['source']} count={now['count']}")
    if not now["devices"]:
        failures.append("/api/now returned 0 devices (LVC empty; simulator idle?)")
    for device in now["devices"]:
        print(
            f"  {device['inverter_id']:>4}  {device['ac_power_w']:>9.1f} W  "
            f"eff={device['efficiency']:.4f}  heatsink={device['heatsink_temp_c']:.1f}C  "
            f"status={device['status_code']}  clip={device['clipping']}"
        )
        # The PWA renders these straight into a tile; a string here becomes "[object]".
        for key in ("ac_power_w", "dc_power_w", "efficiency", "heatsink_temp_c"):
            if not isinstance(device[key], (int, float)):
                failures.append(f"/api/now {key} is {type(device[key]).__name__}, expected number")

    # --- /api/series: the chart ----------------------------------------------
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    series = get(
        f"/api/series?table=inverter_telemetry&metric=ac_power_w"
        f"&interval=1h&group_by=inverter_id&start={rfc3339(start)}&end={rfc3339(end)}",
        token["token"],
    )
    print(f"/api/series      count={series['count']} interval={series['interval']} group_by={series['group_by']}")
    if not series["points"]:
        failures.append("/api/series returned no points; the chart would render empty")
    for point in series["points"][:4]:
        # App.tsx does `new Date(point.time).getTime()` and skips NaN.
        stamp = datetime.fromisoformat(str(point["time"]).replace("Z", "+00:00"))
        print(f"  {stamp:%H:%M}  {point.get('inverter_id'):>4}  {float(point['value']):>9.1f} W")
    for point in series["points"]:
        try:
            float(point["value"])
        except (TypeError, ValueError):
            failures.append(f"/api/series value is not numeric: {point['value']!r}")

    # --- /api/strings: the heatmap -------------------------------------------
    strings = get("/api/strings", token["token"])
    print(f"/api/strings     {len(strings['inverters'])} inverters")
    for row in strings["inverters"]:
        print(
            f"  {row['inverter_id']:>4}  strings={row['string_count']:>2}  "
            f"min={row['min_dc_power_w']:.0f} W  max={row['max_dc_power_w']:.0f} W  "
            f"imbalance={row['imbalance_ratio'] * 100:.2f}%"
        )
        if not isinstance(row["imbalance_ratio"], (int, float)):
            failures.append(f"/api/strings imbalance_ratio is {row['inverter_id']}: {row['imbalance_ratio']!r}")

    # --- /api/summary: the cold-load KPI fallback ---------------------------
    summary = get("/api/summary", token["token"])
    print(f"/api/summary     rollup_time={summary['rollup_time']}")
    for key, value in (summary["rollup"] or {}).items():
        flag = ""
        if not isinstance(value, (int, float)):
            # KpiTiles guards this with a typeof check, but a non-number here
            # means the declared `Record<string, number>` type is a lie.
            flag = "  <-- NOT A NUMBER"
            failures.append(f"/api/summary rollup[{key}] is {type(value).__name__}: {value!r}")
        print(f"  {key:<22} = {value!r}{flag}")
    if summary["rollup_time"] is not None and not isinstance(summary["rollup_time"], str):
        failures.append("/api/summary rollup_time must be a string or null")

    # Physical invariants. These are what a dashboard tile must never show as an
    # impossible number -- a unit error in the simulator shipped a 132 %
    # "capacity factor" straight through to the UI before this was checked.
    rollup = summary["rollup"] or {}
    for key, limit in (("capacity_factor", 1.0), ("pr_ratio", 1.0)):
        value = rollup.get(key)
        if isinstance(value, (int, float)) and not 0.0 <= value <= limit:
            failures.append(f"/api/summary {key} = {value} is outside [0, {limit}]")
    efficiency = rollup.get("pr_ratio")
    if isinstance(efficiency, (int, float)) and efficiency > 1.0:
        print("  note: PR above 1.0 is legitimate (it is POA-relative, not nameplate-relative)")

    # --- /api/events: the feed ----------------------------------------------
    events = get("/api/events", token["token"])
    print(f"/api/events      {len(events['events'])} events")
    for event in events["events"][:4]:
        print(f"  {event['time']}  {event['severity']:<8} {event['source']:<8} {event['message'][:60]}")
    for event in events["events"]:
        if event["severity"] not in ("info", "warning", "critical"):
            failures.append(f"unknown severity {event['severity']!r} would render an unstyled pill")
        try:
            datetime.fromisoformat(str(event["time"]).replace("Z", "+00:00"))
        except ValueError:
            failures.append(f"/api/events time is unparseable by formatClock: {event['time']!r}")

    # --- /api/meta: used by the explore view --------------------------------
    meta = get("/api/meta", token["token"])
    print(f"/api/meta        tables={meta['tables']}")

    # --- alerting -----------------------------------------------------------
    stats = get("/api/alert-stats", token["token"])
    print(
        f"/api/alert-stats connected={stats['engine_connected']} "
        f"received={stats['received']} active={stats['active']} "
        f"fired={stats['alerts_fired']} errors={stats['errors']} dropped={stats['dropped']}"
    )
    if not stats["engine_connected"]:
        failures.append(
            "alert engine is not connected to MQTT; alerting is silently dead"
        )
    if stats["received"] == 0:
        failures.append("alert engine has received no messages; rules cannot fire")
    # A rising error count means the engine is running but something inside the
    # loop is failing, which is much easier to miss than a disconnected engine.
    if stats["errors"] > 0:
        failures.append(f"alert engine reported {stats['errors']} error(s)")
    if stats["dropped"] > 0:
        failures.append(f"alert engine dropped {stats['dropped']} writes (InfluxDB too slow?)")

    alerts = get("/api/alerts", token["token"])
    print(f"/api/alerts      {alerts['count']} active  counts={alerts['counts']}")
    for alert in alerts["alerts"]:
        print(
            f"  [{alert['severity']:<8}] {alert['rule_id']:<24} "
            f"{alert['subject']:<8} value={alert['value']} threshold={alert['threshold']}"
        )
    for alert in alerts["alerts"]:
        if alert["severity"] not in ("info", "warning", "critical"):
            failures.append(f"unknown severity {alert['severity']!r} renders an unstyled pill")
        if alert["value"] is not None and not isinstance(alert["value"], (int, float)):
            failures.append(f"alert value is not numeric: {alert['value']!r}")

    rules = get("/api/alert-rules", token["token"])
    kinds = [rule["kind"] for rule in rules["rules"]]
    print(f"/api/alert-rules {rules['count']} rules ({kinds.count('staleness')} staleness)")
    # Without a staleness rule a network partition is undetectable: the Last Will
    # only fires on an ungraceful disconnect, not on a device that holds its
    # session open and publishes nothing.
    if "staleness" not in kinds:
        failures.append("no staleness rule loaded; a comms partition would go unnoticed")
    for rule in rules["rules"]:
        if rule["kind"] == "threshold" and not rule["conditions"]:
            failures.append(f"rule {rule['id']} is a threshold rule with no conditions")

    print()
    if failures:
        print(f"{len(failures)} problem(s) the PWA would hit:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All PWA-facing endpoints return renderable data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
