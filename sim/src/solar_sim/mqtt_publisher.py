"""MQTT 5 publisher, with the Last Will that makes offline detection work.

Two design points are load-bearing:

* **One client per inverter, not one for the farm.** A single connection cannot
  express "inverter 2 is down" -- the whole site would look offline. Per-device
  connections are what make the Last Will meaningful.
* **The Will is registered per connection.** EMQX publishes it automatically
  when that connection drops unexpectedly, which detects power loss in seconds
  without any polling.

The failure this does *not* catch is a network partition, where the session
stays alive and nothing is published. That is detected by a staleness rule in
the alert engine, not here. See docs/03-data-flow.md section 5.4.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import paho.mqtt.client as mqtt

from . import metrics as M
from .farm import Tick
from .topology import Inverter, Site

log = logging.getLogger(__name__)

#: Retained so a subscriber that connects mid-session learns current state
#: immediately, without waiting for the next publish.
STATUS_RETAIN = True
ROLLUP_RETAIN = True
LWT_RETAIN = True


def _now_iso(when: datetime) -> str:
    """RFC 3339 in UTC with a Z suffix.

    Unambiguous on purpose: a naive local timestamp would be ambiguous twice a
    year, and the InfluxDB row timestamp is taken from this string.
    """
    return when.astimezone(UTC).strftime(M.TIMESTAMP_FORMAT)


class FarmPublisher:
    """Publishes one tick of telemetry to MQTT."""

    def __init__(
        self,
        site: Site,
        host: str = "127.0.0.1",
        port: int = 1883,
        keepalive: int = 30,
        client_prefix: str = "solar",
    ) -> None:
        self.site = site
        self.host = host
        self.port = port
        self.keepalive = keepalive
        self.client_prefix = client_prefix
        self._clients: dict[str, mqtt.Client] = {}
        self._connected: set[str] = set()

    # -- connection management ----------------------------------------------

    def _status_payload(self, inverter: Inverter, when: datetime, state: str) -> dict:
        return M.build_payload(
            "status",
            _now_iso(when),
            **self.site.identity(inverter),
            state=state,
            status_code=M.STATUS_CODES[state],
            last_seen=_now_iso(when),
            firmware=inverter.firmware,
        )

    def connect(self, when: datetime) -> None:
        """Open one connection per inverter and register each Last Will."""
        for inverter in self.site.inverters:
            client_id = f"{self.client_prefix}-{inverter.inverter_id}"
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv5,
            )
            # The Will: what EMQX publishes if this connection dies without a
            # clean disconnect. Retained, so a late subscriber still sees it.
            client.will_set(
                M.topic_for(
                    "status", **self.site.identity(inverter)
                ),
                json.dumps(
                    self._status_payload(inverter, when, M.STATE_OFFLINE)
                ),
                qos=1,
                retain=LWT_RETAIN,
            )
            client.connect(self.host, self.port, self.keepalive)
            client.loop_start()
            self._clients[inverter.inverter_id] = client
            self._connected.add(inverter.inverter_id)
            log.info("connected %s -> %s:%s", client_id, self.host, self.port)

    def disconnect(self) -> None:
        """Close every connection cleanly.

        A clean disconnect does NOT fire the Will, so shutdown must publish
        ``offline`` explicitly -- otherwise a deliberate stop would leave every
        inverter looking alive until the retained status message expired.
        """
        for inverter in self.site.inverters:
            client = self._clients.get(inverter.inverter_id)
            if client is None:
                continue
            try:
                client.publish(
                    M.topic_for("status", **self.site.identity(inverter)),
                    json.dumps(
                        self._status_payload(
                            inverter, datetime.now(UTC), M.STATE_OFFLINE
                        )
                    ),
                    qos=1,
                    retain=LWT_RETAIN,
                )
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.exception("error disconnecting %s", inverter.inverter_id)
        self._clients.clear()
        self._connected.clear()

    # -- publishing ----------------------------------------------------------

    def _publish(self, measurement: str, identity: dict[str, str], payload: dict) -> None:
        spec = M.CONTRACT[measurement]
        client = self._clients.get(identity.get(M.INVERTER_ID))
        if client is None:
            log.warning("no client for %s; dropping %s", identity, measurement)
            return
        client.publish(
            M.topic_for(measurement, **identity),
            json.dumps(payload, separators=(",", ":")),
            qos=spec["qos"],
            retain=spec["retain"],
        )

    def publish_tick(self, tick: Tick) -> dict[str, int]:
        """Publish every measurement for one tick. Returns a count per measurement."""
        counts: dict[str, int] = {}
        stamp = _now_iso(tick.when)

        for reading in tick.inverters:
            inverter = reading.inverter
            identity = self.site.identity(inverter)

            payload = M.build_payload(
                "inverter_telemetry",
                stamp,
                **identity,
                model=inverter.model,
                ac_power_w=reading.ac_power_w,
                dc_power_w=reading.dc_power_w,
                ac_voltage_v=reading.ac_voltage_v,
                ac_current_a=reading.ac_current_a,
                dc_voltage_v=reading.dc_voltage_v,
                dc_current_a=reading.dc_current_a,
                efficiency=reading.efficiency,
                heatsink_temp_c=reading.heatsink_temp_c,
                internal_temp_c=reading.internal_temp_c,
                uptime_s=reading.uptime_s,
                status_code=reading.status_code,
                clipping=reading.clipping,
            )
            self._publish("inverter_telemetry", identity, M.coerce("inverter_telemetry", payload))
            counts["inverter_telemetry"] = counts.get("inverter_telemetry", 0) + 1

            status = M.build_payload(
                "status",
                stamp,
                **identity,
                state=reading.state,
                status_code=reading.status_code,
                last_seen=stamp,
                firmware=inverter.firmware,
            )
            client = self._clients.get(inverter.inverter_id)
            if client is not None:
                client.publish(
                    M.topic_for("status", **identity),
                    json.dumps(status, separators=(",", ":")),
                    qos=1,
                    retain=STATUS_RETAIN,
                )

        for reading in tick.strings:
            identity = self.site.string_identity(reading.string)
            payload = M.build_payload(
                "string_telemetry",
                stamp,
                **identity,
                dc_power_w=reading.dc_power_w,
                dc_voltage_v=reading.dc_voltage_v,
                dc_current_a=reading.dc_current_a,
                module_temp_c=reading.module_temp_c,
            )
            self._publish("string_telemetry", identity, M.coerce("string_telemetry", payload))
            counts["string_telemetry"] = counts.get("string_telemetry", 0) + 1

        if tick.weather is not None:
            weather = M.build_payload(
                "weather_station", stamp, **M.coerce("weather_station", tick.weather)
            )
            client = next(iter(self._clients.values()), None)
            if client is not None:
                client.publish(
                    M.topic_for(
                        "weather_station",
                        site=self.site.site,
                        station_id=self.site.weather_station_id,
                    ),
                    json.dumps(weather, separators=(",", ":")),
                    qos=0,
                    retain=False,
                )
                counts["weather_station"] = counts.get("weather_station", 0) + 1

        if tick.rollup is not None:
            rollup = M.build_payload(
                "site_rollup", stamp, **M.coerce("site_rollup", tick.rollup)
            )
            client = next(iter(self._clients.values()), None)
            if client is not None:
                client.publish(
                    M.topic_for("site_rollup", site=self.site.site),
                    json.dumps(rollup, separators=(",", ":")),
                    qos=1,
                    retain=ROLLUP_RETAIN,
                )
                counts["site_rollup"] = counts.get("site_rollup", 0) + 1

        return counts

    def publish_event(
        self, when: datetime, severity: str, source: str, code: str, message: str,
        value: float | None = None, threshold: float | None = None,
    ) -> None:
        if severity not in M.SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        payload = M.build_payload(
            "events",
            _now_iso(when),
            site=self.site.site,
            severity=severity,
            source=source,
            code=code,
            message=message,
            value=value,
            threshold=threshold,
        )
        client = next(iter(self._clients.values()), None)
        if client is None:
            log.warning("no clients connected; dropping event %s", code)
            return
        client.publish(
            M.topic_for("events", site=self.site.site),
            json.dumps(M.coerce("events", payload), separators=(",", ":")),
            qos=1,
            retain=False,
        )
