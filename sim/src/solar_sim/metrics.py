"""Single source of truth for the MQTT wire contract.

Every topic, tag and field name used by the simulator is declared here exactly
once, and the rest of the code refers to these constants. This module is the
reference implementation of the contract documented in
``docs/03-data-flow.md`` section 2.0.

Two facts make this worth centralising:

1. Identity is carried **twice** -- once in the topic (for routing and
   subscription) and once in the JSON payload (for ingestion). They are kept in
   lockstep by :func:`build_payload`, so the two can never drift apart silently.
2. InfluxDB 3 tag definitions are **immutable** once a table exists. A tag
   arriving that the table does not declare permanently alters the primary key.
   ``tests/test_contract.py`` asserts that these names match the tables created
   by ``scripts/influx-init.sh`` and the parsers in ``telegraf/telegraf.conf``.
"""

from __future__ import annotations

import re
from typing import Any, Final

# --------------------------------------------------------------------------
# Field type vocabulary. Mirrors the InfluxDB 3 column types.
# --------------------------------------------------------------------------
FLOAT: Final = "float"
INT: Final = "int"
STRING: Final = "string"
BOOL: Final = "bool"

#: Every payload carries this; it becomes the row timestamp.
TIMESTAMP_KEY: Final = "ts"
TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

SITE: Final = "site"
BLOCK: Final = "block"
INVERTER_ID: Final = "inverter_id"
STRING_ID: Final = "string_id"
STATION_ID: Final = "station_id"
MODEL: Final = "model"
SEVERITY: Final = "severity"
SOURCE: Final = "source"


# --------------------------------------------------------------------------
# Topic grammar
# --------------------------------------------------------------------------
# The root of every topic. Must match the ``servers``/``topics`` patterns in
# telegraf/telegraf.conf.
TOPIC_ROOT: Final = "solar"

INVERTER_TELEMETRY: Final = (
    f"{TOPIC_ROOT}/{{site}}/block/{{block}}/inverter/{{inverter_id}}/telemetry"
)
STRING_TELEMETRY: Final = (
    f"{TOPIC_ROOT}/{{site}}/block/{{block}}/inverter/{{inverter_id}}/string/{{string_id}}/telemetry"
)
# The weather station is a SITE asset, not a per-block one, so its topic
# carries no block segment. That keeps the invariant "every topic placeholder
# is also a payload tag" true for every measurement except events.
WEATHER_TELEMETRY: Final = f"{TOPIC_ROOT}/{{site}}/weather/{{station_id}}/telemetry"
INVERTER_STATUS: Final = f"{TOPIC_ROOT}/{{site}}/block/{{block}}/inverter/{{inverter_id}}/status"
SITE_ROLLUP: Final = f"{TOPIC_ROOT}/{{site}}/rollup"
SITE_EVENTS: Final = f"{TOPIC_ROOT}/{{site}}/events"


# --------------------------------------------------------------------------
# Per-measurement contracts
# --------------------------------------------------------------------------
# tags   -> InfluxDB tag columns, part of the immutable primary key. ALWAYS
#           present in the payload; a point without them is meaningless.
# fields -> InfluxDB columns. Optional in the payload.
#
# The names and types here are asserted against telegraf/telegraf.conf and
# against the live InfluxDB schema by tests/test_contract.py.
CONTRACT: Final[dict[str, dict[str, Any]]] = {
    "inverter_telemetry": {
        "topic": INVERTER_TELEMETRY,
        "qos": 0,
        "retain": False,
        "tags": (SITE, BLOCK, INVERTER_ID, MODEL),
        "fields": {
            "ac_power_w": FLOAT,
            "dc_power_w": FLOAT,
            "ac_voltage_v": FLOAT,
            "ac_current_a": FLOAT,
            "dc_voltage_v": FLOAT,
            "dc_current_a": FLOAT,
            "efficiency": FLOAT,
            "heatsink_temp_c": FLOAT,
            "internal_temp_c": FLOAT,
            "uptime_s": INT,
            "status_code": INT,
            "clipping": BOOL,
        },
    },
    "string_telemetry": {
        "topic": STRING_TELEMETRY,
        "qos": 0,
        "retain": False,
        "tags": (SITE, BLOCK, INVERTER_ID, STRING_ID),
        "fields": {
            "dc_power_w": FLOAT,
            "dc_voltage_v": FLOAT,
            "dc_current_a": FLOAT,
            "module_temp_c": FLOAT,
        },
    },
    "weather_station": {
        "topic": WEATHER_TELEMETRY,
        "qos": 0,
        "retain": False,
        "tags": (SITE, STATION_ID),
        "fields": {
            "ghi": FLOAT,
            "dni": FLOAT,
            "dhi": FLOAT,
            "air_temp_c": FLOAT,
            "wind_speed_mps": FLOAT,
            "relative_humidity": FLOAT,
            "clearness_index": FLOAT,
        },
    },
    "site_rollup": {
        "topic": SITE_ROLLUP,
        "qos": 1,
        "retain": True,
        "tags": (SITE,),
        "fields": {
            "total_ac_power_w": FLOAT,
            "daily_yield_kwh": FLOAT,
            "pr_ratio": FLOAT,
            "capacity_factor": FLOAT,
            "inverters_online": INT,
            "strings_online": INT,
        },
    },
    "events": {
        "topic": SITE_EVENTS,
        "qos": 1,
        "retain": False,
        # severity and source are tags that exist ONLY in the payload: the
        # events topic is `solar/{site}/events` and carries no device identity.
        "tags": (SITE, SEVERITY, SOURCE),
        "fields": {
            "code": STRING,
            "message": STRING,
            "value": FLOAT,
            "threshold": FLOAT,
        },
    },
}

# Retained status message. Not ingested by Telegraf -- this is broker state
# consumed directly by the PWA and the FastAPI alert engine. It still carries
# identity, and its identity must match the tags of inverter_telemetry.
STATUS_CONTRACT: Final[dict[str, Any]] = {
    "topic": INVERTER_STATUS,
    "qos": 1,
    "retain": True,
    "identity": (SITE, BLOCK, INVERTER_ID),
    "fields": {
        "state": STRING,
        "status_code": INT,
        "last_seen": STRING,
        "firmware": STRING,
    },
}

#: Inverter states, published on the status topic and in ``status_code``.
STATE_OFFLINE: Final = "offline"
STATE_STANDBY: Final = "standby"
STATE_PRODUCING: Final = "producing"
STATE_DERATING: Final = "derating"
STATE_FAULT: Final = "fault"

INVERTER_STATES: Final = frozenset(
    {STATE_OFFLINE, STATE_STANDBY, STATE_PRODUCING, STATE_DERATING, STATE_FAULT}
)

STATUS_CODES: Final[dict[str, int]] = {
    STATE_OFFLINE: 0,
    STATE_STANDBY: 1,
    STATE_PRODUCING: 3,
    STATE_DERATING: 4,
    STATE_FAULT: 5,
}

#: Event severities. A tag, so this set is deliberately small: every additional
#: value multiplies the series count of the events table.
SEVERITIES: Final = ("info", "warning", "critical")


def topic_for(measurement: str, **identity: str) -> str:
    """Render a topic, failing loudly on a missing or unexpected identity key.

    A silent mismatch here would publish to a topic that nobody subscribes to,
    which is indistinguishable from the simulator having stopped.
    """
    spec = STATUS_CONTRACT if measurement == "status" else CONTRACT[measurement]
    template = spec["topic"]
    # The topic's placeholders, not the full tag set. `model` is a tag on
    # inverter telemetry but has no topic segment, so requiring it here would
    # reject every valid inverter topic.
    expected = set(re.findall(r"\{([a-z_]+)\}", template))
    supplied = {k for k, v in identity.items() if v is not None}
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ValueError(
            f"{measurement}: identity mismatch (missing={missing}, unexpected={extra})"
        )
    return template.format(**identity)


def build_payload(measurement: str, ts: str, /, **values: Any) -> dict[str, Any]:
    """Assemble a payload, guaranteeing both contract invariants.

    * every declared tag is present -- a point without identity is meaningless
      and InfluxDB would reject it;
    * every declared field that the caller did not supply is omitted rather than
      sent as null, because Telegraf's json_v2 parses null as a typed value and
      would poison aggregations.
    """
    spec: dict[str, Any] = STATUS_CONTRACT if measurement == "status" else CONTRACT[measurement]
    tag_names: tuple[str, ...] = spec["identity"] if measurement == "status" else spec["tags"]

    for tag in tag_names:
        if values.get(tag) in (None, ""):
            raise ValueError(f"{measurement}: required tag {tag!r} is missing")

    payload: dict[str, Any] = {TIMESTAMP_KEY: ts}
    payload.update({tag: values[tag] for tag in tag_names})
    payload.update({k: v for k, v in values.items() if k not in payload})
    return payload


def coerce(measurement: str, values: dict[str, Any]) -> dict[str, Any]:
    """Coerce values to the contract's declared types.

    Guards the pipeline's most dangerous failure: a Python bool is an ``int``
    subclass, so ``True`` would serialise as ``1`` and land in an ``int64``
    column, or a numpy float64 would serialise in a form json_v2 reads as a
    string. Because the ``json`` parser silently coerces every number to float
    and discards booleans, a type error here is invisible until a dashboard
    looks wrong.
    """
    spec = STATUS_CONTRACT if measurement == "status" else CONTRACT[measurement]
    out = dict(values)
    for name, kind in spec["fields"].items():
        if name not in out or out[name] is None:
            continue
        v = out[name]
        if kind == FLOAT:
            out[name] = float(v)
        elif kind == INT:
            out[name] = int(v)
        elif kind == BOOL:
            out[name] = bool(v)
        elif kind == STRING:
            out[name] = str(v)
    return out
