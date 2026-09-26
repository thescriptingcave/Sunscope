"""The wire contract, asserted against the things that actually consume it.

The simulator, ``telegraf/telegraf.conf`` and the InfluxDB schema are three
independent expressions of the same contract. Nothing stops them drifting apart
except these tests, and the failure mode when they do is nasty: identity
mismatches are either rejected outright (loud) or, worse, create a tag column
that did not exist before, which is unfixable because InfluxDB 3 tag
definitions are immutable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from solar_sim import metrics as M

REPO = Path(__file__).resolve().parents[2]
TELEGRAF_CONF = REPO / "telegraf" / "telegraf.conf"
INIT_SCRIPT = REPO / "scripts" / "influx-init.sh"
DATA_FLOW_DOC = REPO / "docs" / "03-data-flow.md"

pytestmark = pytest.mark.skipif(
    not TELEGRAF_CONF.exists(), reason="telegraf config not present"
)


def _telegraf_blocks() -> dict[str, dict]:
    """Parse the mqtt_consumer blocks out of the real Telegraf config."""
    source = TELEGRAF_CONF.read_text()
    blocks: dict[str, dict] = {}
    for match in re.finditer(
        r"\[\[inputs\.mqtt_consumer\]\](.*?)(?=\n\[\[inputs|\n\[\[outputs|\Z)",
        source,
        re.S,
    ):
        body = match.group(1)
        name = re.search(r'name_override\s*=\s*"([^"]+)"', body)
        if not name:
            continue
        measurement = name.group(1)
        tags = tuple(
            re.findall(r'json_v2\.tag\]\]\s*\n\s*path\s*=\s*"([^"]+)"', body)
        )
        fields = dict(
            re.findall(
                r'json_v2\.field\]\]\s*\n\s*path\s*=\s*"([^"]+)"\s*\n\s*type\s*=\s*"([^"]+)"',
                body,
            )
        )
        topics = re.search(r"topics\s*=\s*\[(.*?)\]", body, re.S)
        topic_tag = re.search(r'topic_tag\s*=\s*"([^"]*)"', body)
        blocks[measurement] = {
            "tags": tags,
            "fields": fields,
            "topics": re.findall(r'"([^"]+)"', topics.group(1)) if topics else [],
            "topic_tag": topic_tag.group(1) if topic_tag else None,
        }
    return blocks


@pytest.fixture(scope="module")
def telegraf():
    return _telegraf_blocks()


# --- the simulator matches the ingest configuration -------------------------


def test_every_measurement_exists_in_telegraf(telegraf):
    for measurement in M.CONTRACT:
        assert measurement in telegraf, (
            f"{measurement} is declared by the simulator but no Telegraf input "
            f"block ingests it; its data would never reach InfluxDB"
        )


def test_tags_match_telegraf_exactly(telegraf):
    """A tag in the payload that Telegraf does not map is a new column.

    Either direction is a bug: a missing tag silently collapses series, and an
    extra one permanently alters the table's primary key.
    """
    for measurement, spec in M.CONTRACT.items():
        assert tuple(spec["tags"]) == telegraf[measurement]["tags"], (
            f"{measurement}: simulator tags {spec['tags']} != "
            f"telegraf tags {telegraf[measurement]['tags']}"
        )


def test_fields_and_types_match_telegraf(telegraf):
    for measurement, spec in M.CONTRACT.items():
        assert dict(spec["fields"]) == telegraf[measurement]["fields"], (
            f"{measurement}: field/type mismatch. A mismatch in *type* is the "
            f"dangerous one -- InfluxDB columns are typed and Telegraf's json "
            f"parser will silently coerce."
        )


def test_topics_match_telegraf(telegraf):
    for measurement, spec in M.CONTRACT.items():
        topic = spec["topic"]
        # Convert the template into a wildcard pattern the way a subscriber would.
        pattern = re.sub(r"\{[^}]+\}", "+", topic)
        assert pattern in telegraf[measurement]["topics"], (
            f"{measurement}: topic template {topic} -> {pattern} is not "
            f"subscribed by Telegraf (subscribes: {telegraf[measurement]['topics']})"
        )


def test_no_input_block_adds_a_topic_tag(telegraf):
    """`topic_tag` defaults to "topic" and would add an undeclared tag column.

    This exact mistake happened during the build and cost a table rebuild.
    """
    for measurement, block in telegraf.items():
        assert block["topic_tag"] == "", (
            f"{measurement}: topic_tag is {block['topic_tag']!r}; it must be \"\" "
            f"or a 'topic' tag column is added that is not in the schema"
        )


def test_agent_omits_hostname():
    """The default `host` tag is not in the schema either."""
    source = TELEGRAF_CONF.read_text()
    assert re.search(r"^\s*omit_hostname\s*=\s*true", source, re.M), (
        "omit_hostname must be true in [agent]; the default host tag would be an "
        "undeclared column and would multiply series cardinality"
    )


def test_events_qos_is_at_least_one(telegraf):
    """Losing an alarm is not acceptable, unlike a telemetry sample."""
    assert M.CONTRACT["events"]["qos"] >= 1


def test_retained_only_where_a_new_subscriber_needs_state(telegraf):
    """Retained status/rollup give the PWA instant state on a cold load."""
    assert M.STATUS_CONTRACT["retain"] is True
    assert M.CONTRACT["site_rollup"]["retain"] is True
    for measurement in ("inverter_telemetry", "string_telemetry", "weather_station"):
        assert M.CONTRACT[measurement]["retain"] is False, (
            f"{measurement} must not be retained; retaining high-rate telemetry "
            f"would hand every new subscriber a backlog"
        )


# --- identity is duplicated consistently ------------------------------------


def test_topic_and_payload_agree():
    """A topic/payload mismatch writes the wrong tags, silently.

    ``build_payload`` is the only place the two are combined, so testing the
    round trip is sufficient: if the identity keys required for a topic are
    present, the topic and payload cannot disagree.
    """
    payload = M.build_payload(
        "inverter_telemetry",
        "2026-09-25T12:00:00Z",
        site="mojave",
        block="BLK-A",
        inverter_id="INV-01",
        model="SG250CX",
        ac_power_w=1.0,
    )
    topic = M.topic_for(
        "inverter_telemetry", site="mojave", block="BLK-A", inverter_id="INV-01"
    )
    assert topic == "solar/mojave/block/BLK-A/inverter/INV-01/telemetry"
    assert payload["site"] == "mojave"
    assert payload["inverter_id"] == "INV-01"


def test_missing_identity_is_rejected_not_defaulted():
    """A silent default would publish a point nothing subscribes to."""
    with pytest.raises(ValueError, match="required tag"):
        M.build_payload("inverter_telemetry", "2026-09-25T12:00:00Z", site="mojave")
    with pytest.raises(ValueError, match="identity mismatch"):
        M.topic_for("inverter_telemetry", site="mojave")


def test_extra_identity_is_rejected():
    with pytest.raises(ValueError, match="unexpected"):
        M.topic_for(
            "inverter_telemetry", site="mojave", block="BLK-A",
            inverter_id="INV-01", bogus="x",
        )


def test_payload_is_json_serialisable_with_contract_types():
    payload = M.build_payload(
        "inverter_telemetry",
        "2026-09-25T12:00:00Z",
        site="mojave",
        block="BLK-A",
        inverter_id="INV-01",
        model="SG250CX",
        ac_power_w=1.5,
        uptime_s=10,
        clipping=True,
    )
    # bool must stay a JSON boolean, not become 1 via int coercion.
    assert json.loads(json.dumps(payload))["clipping"] is True


# --- coercion guards the type-mismatch failure mode -------------------------


def test_coerce_preserves_bool_through_int_subclassing():
    """Python bool is an int subclass; naive int() would turn True into 1."""
    out = M.coerce("inverter_telemetry", {"clipping": True, "uptime_s": 42})
    assert out["clipping"] is True
    assert out["uptime_s"] == 42


def test_coerce_preserves_float_that_integral_value():
    """A JSON float of 250.0 must not become int 250 and land in the wrong column."""
    out = M.coerce("inverter_telemetry", {"ac_power_w": 250.0})
    assert isinstance(out["ac_power_w"], float)


def test_coerce_leaves_none_alone():
    """None is dropped by the caller rather than serialised as null."""
    out = M.coerce("inverter_telemetry", {"ac_power_w": None})
    assert out["ac_power_w"] is None


# --- the documented payloads must match the contract -------------------------


def test_documented_payload_examples_satisfy_the_contract():
    """Every JSON example in the data-flow doc must be a valid payload.

    Documentation that drifts from the code is worse than no documentation,
    because it is trusted.
    """
    if not DATA_FLOW_DOC.exists():
        pytest.skip("data-flow doc not present")
    text = DATA_FLOW_DOC.read_text()
    section = text[text.index("### 2.1") : text.index("## 3. InfluxDB schema")]
    examples = re.findall(r"```json\n(.*?)\n```", section, re.S)
    assert len(examples) >= 5, "expected payload examples in the data-flow doc"

    checked = 0
    for raw in examples:
        data = json.loads(raw)
        measurement = _measurement_for(data)
        if measurement is None:
            continue
        spec = M.CONTRACT.get(measurement) or M.STATUS_CONTRACT
        identity = spec.get("identity") or spec["tags"]
        for tag in identity:
            assert tag in data, (
                f"documented {measurement} payload is missing the required "
                f"tag {tag!r}: {data}"
            )
        for key in data:
            declared = spec.get("fields", {})
            assert key in declared or key in identity or key == M.TIMESTAMP_KEY, (
                f"documented {measurement} payload has undeclared key {key!r}"
            )
        checked += 1
    assert checked >= 5, f"only checked {checked} documented payloads"


def _measurement_for(data: dict) -> str | None:
    if "total_ac_power_w" in data:
        return "site_rollup"
    if "severity" in data:
        return "events"
    if "ghi" in data:
        return "weather_station"
    # ac_power_w before dc_power_w: inverter telemetry has both.
    if "ac_power_w" in data:
        return "inverter_telemetry"
    if "dc_power_w" in data:
        return "string_telemetry"
    if "state" in data:
        return "status"
    return None
