"""Wiring that connects the rule engine to real data.

Three responsibilities, deliberately separated from :mod:`alerts`:

* Subscribe to MQTT and feed readings to the engine.
* Inject the weather station's ``ghi`` into inverter payloads, so rules like
  ``power_zero_in_sunlight`` can tell "broken" from "dark". This does *not*
  change the wire contract in ``sim/src/solar_sim/metrics.py`` -- that stays the
  single source of truth -- it is context layered on at evaluation time.
* Persist every alert transition to the InfluxDB ``events`` table, so the feed
  survives a restart and Grafana can chart it.

The staleness tick runs on its own timer. It has to: when the failure is that
data stopped arriving, there are no messages to drive anything, so a purely
message-driven loop would simply never notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

import aiomqtt

from .alerts import Alert, RuleEngine
from .influx import InfluxClient

log = logging.getLogger("solar_api.alerts")

#: Site this process evaluates. Single-site by design; the API already pins
#: `site = 'mojave'` in its SQL allowlists.
SITE = os.environ.get("ALERT_SITE", "mojave")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

#: Topics the engine needs. Mirrors what web/src/mqtt/live.ts subscribes to,
#: plus the weather station for the irradiance gate.
TOPICS = (
    f"solar/{SITE}/block/+/inverter/+/telemetry",
    f"solar/{SITE}/block/+/inverter/+/status",
    f"solar/{SITE}/rollup",
    f"solar/{SITE}/weather/+/telemetry",
)

#: How often to run the staleness check. Must be well under the smallest
#: `stale_after_s` (120 s) or detection is needlessly delayed.
TICK_INTERVAL_S = float(os.environ.get("ALERT_TICK_INTERVAL_S", "10"))


class AlertService:
    """Owns the MQTT subscription and the rule engine for the process lifetime."""

    def __init__(self, engine: RuleEngine, influx: InfluxClient) -> None:
        self._engine = engine
        self._influx = influx
        self._task: asyncio.Task[None] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._connected = False
        #: Latest weather reading per station, keyed by station id. Shared with
        #: inverter payloads as `ghi`.
        self._weather: dict[str, dict[str, Any]] = {}
        # Bounded so a slow or unreachable database cannot grow the backlog
        # without limit. 256 is far more than a real alert rate.
        self._writes: asyncio.Queue[Alert] = asyncio.Queue(maxsize=256)
        #: Connected browsers, for the server-side live feed. See `subscribe`.
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._counters = {
            "received": 0, "alerts_fired": 0, "resolutions": 0, "errors": 0, "dropped": 0,
        }

    # -- live fan-out -------------------------------------------------------

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue[dict[str, Any]]:
        """Register a listener and return its queue.

        This is what makes the PWA work over HTTPS. The browser used to open its
        own WebSocket straight to the broker, which a browser blocks as mixed
        content the moment the page is served over https -- and the only ways
        around that were to expose EMQX publicly or to put a TLS terminator in
        front of it. Relaying through the API removes the browser's need to reach
        the broker at all.

        Reusing this one MQTT connection rather than opening a second per browser
        is deliberate: the broker sees one subscriber regardless of how many
        dashboards are open, and there is a single parse of each message.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(queue)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def _broadcast(self, frame: dict[str, Any]) -> None:
        """Fan one parsed message out to every connected browser.

        A browser that cannot keep up is dropped rather than allowed to block
        the MQTT loop: a slow client must not be able to stall rule evaluation.
        Dropping the oldest entry is right here, because the newest reading is
        the one the dashboard is waiting to draw.
        """
        for queue in list(self._listeners):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(frame)
                self._counters["dropped"] += 1


    # -- state ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def active_alerts(self) -> list[Alert]:
        return self._engine.active_alerts()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._stop.clear()
        self._writer = asyncio.create_task(self._write_forever(), name="alert-writer")
        self._task = asyncio.create_task(self._run(), name="alert-service")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._writer):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._writer = None
        self._connected = False

    async def _run(self) -> None:
        """Subscribe and stay subscribed.

        aiomqtt reconnects on its own for transport errors, but not for a broker
        that is simply absent at startup, so the whole thing sits in a retry
        loop. Alerting that silently stops because the broker was down when the
        API started would be the worst possible failure mode for this component.
        """
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=MQTT_HOST, port=MQTT_PORT, identifier="solar-alert-engine"
                ) as client:
                    for topic in TOPICS:
                        await client.subscribe(topic)
                    log.info(
                        "alert engine subscribed to %d topics on %s:%d",
                        len(TOPICS), MQTT_HOST, MQTT_PORT,
                    )
                    self._connected = True
                    self._ticker = asyncio.create_task(self._tick_forever(), name="alert-tick")
                    try:
                        async for message in client.messages:
                            # aiomqtt hands back a `Topic` object, not a str.
                            # Also isolated per message: an exception escaping
                            # here would close the subscription and force a
                            # reconnect, so one malformed payload would cost the
                            # next N seconds of alerting.
                            try:
                                self._handle(str(message.topic), message.payload)
                            except Exception:  # noqa: BLE001
                                self._counters["errors"] += 1
                                log.exception("failed to handle message on %s", message.topic)
                    finally:
                        self._ticker.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self._ticker
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._counters["errors"] += 1
                log.warning("alert engine MQTT connection failed (%s); retrying in 5s", exc)
            finally:
                self._connected = False
            if not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)

    async def _tick_forever(self) -> None:
        """Drive the staleness check on a timer, independent of traffic."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_INTERVAL_S)
                return
            except TimeoutError:
                pass
            try:
                for alert in self._engine.tick():
                    self._count(alert)
                    self._queue_write(alert)
            except Exception:  # noqa: BLE001
                self._counters["errors"] += 1
                log.exception("alert tick failed")

    # -- ingestion -----------------------------------------------------------

    def _handle(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            self._counters["errors"] += 1
            return
        if not isinstance(data, dict):
            return

        parts = topic.split("/")
        # solar/{site}/block/{block}/inverter/{id}/{telemetry|status}
        #   0      1       2        3         4        5       6
        if len(parts) == 7 and parts[1] == SITE and parts[2] == "block" and parts[4] == "inverter":
            self._counters["received"] += 1
            identity, kind = parts[5], parts[6]
            if kind == "telemetry":
                enriched = self._with_ghi(data)
                self._dispatch(identity, "inverter", enriched)
                self._broadcast({"type": "reading", "payload": enriched})
            elif kind == "status":
                # LWT/status messages carry only the state; a rule reading
                # status_code must still see a fresh value on this path.
                frame = {"status_code": data.get("status_code")}
                self._dispatch(identity, "inverter", frame)
                self._broadcast({"type": "status", "subject": identity, "payload": frame})
        # solar/{site}/rollup
        elif len(parts) == 3 and parts[1] == SITE and parts[2] == "rollup":
            self._counters["received"] += 1
            enriched = self._with_ghi(data)
            self._dispatch(SITE, "site", enriched)
            self._broadcast({"type": "rollup", "payload": enriched})
        # solar/{site}/weather/{station}/telemetry
        elif len(parts) == 5 and parts[1] == SITE and parts[2] == "weather":
            self._counters["received"] += 1
            self._weather[parts[3]] = dict(data)
            self._broadcast({"type": "weather", "payload": dict(data)})
        # Anything else is another site's traffic or a topic shape this version
        # does not understand; counting it as received would overstate coverage.

    def _with_ghi(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attach the site's current irradiance as rule context.

        Layered on at evaluation time rather than added to the telemetry
        contract, so ``sim/src/solar_sim/metrics.py`` stays the single source of
        truth for the wire format.
        """
        station = self._latest_weather()
        if station is None:
            return dict(payload)
        enriched = dict(payload)
        enriched["ghi"] = station.get("ghi")
        return enriched

    def _count(self, alert: Alert) -> None:
        """Tally and log a transition.

        Shared by the message path and the tick path, so an alert produced by the
        staleness check is counted exactly like one produced by a threshold rule.
        Counting them in only one of the two places made ``alerts_fired`` read 0
        while the staleness alerts were plainly on screen.
        """
        if alert.is_resolution:
            self._counters["resolutions"] += 1
            return
        self._counters["alerts_fired"] += 1
        log.warning(
            "ALERT %-8s %-24s %-8s value=%s threshold=%s",
            alert.severity, alert.rule_id, alert.subject, alert.value, alert.threshold,
        )

    def _dispatch(self, subject: str, scope: str, payload: dict[str, Any]) -> None:
        """Feed one reading and persist any resulting transitions.

        Synchronous on purpose: the persistence is queued rather than awaited so
        a slow or failing InfluxDB cannot back-pressure and stall rule
        evaluation, which is the part that must not miss messages.
        """
        try:
            alerts = self._engine.observe(subject, scope, payload)
        except Exception:  # noqa: BLE001
            self._counters["errors"] += 1
            log.exception("rule evaluation failed for %s", subject)
            return
        for alert in alerts:
            self._count(alert)
            self._queue_write(alert)

    def _latest_weather(self) -> dict[str, Any] | None:
        if not self._weather:
            return None
        return next(reversed(self._weather.values()))

    # -- persistence ---------------------------------------------------------

    def _queue_write(self, alert: Alert) -> None:
        """Hand the alert to the writer task.

        A queue rather than ``create_task`` per alert for two reasons: a bare
        task can be garbage collected before it runs, silently dropping the
        alert, and an unbounded burst of tasks would let a slow database build a
        backlog with no bound. ``put_nowait`` also works outside a running loop,
        which keeps the service testable without an event loop.
        """
        try:
            self._writes.put_nowait(alert)
        except asyncio.QueueFull:
            # The database is far behind. Dropping the oldest is right here: the
            # newest alert is the one an operator is waiting to see.
            self._counters["dropped"] += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self._writes.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._writes.put_nowait(alert)

    async def _write_forever(self) -> None:
        """Drain the write queue until stopped."""
        while True:
            alert = await self._writes.get()
            try:
                await self._record(alert)
            finally:
                self._writes.task_done()

    async def _record(self, alert: Alert) -> None:
        """Write one alert transition to the events table.

        A failure must not propagate: losing the historical record of an alert
        is bad, but crashing the subscription loop means losing every future
        alert too.
        """
        try:
            await self._influx.write_event(alert)
        except Exception:  # noqa: BLE001
            self._counters["errors"] += 1
            log.exception("failed to persist alert %s", alert.rule_id)
