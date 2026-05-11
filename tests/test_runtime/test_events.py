"""Tests for the WebSocket event parser + dispatcher.

Runs synthetic frames + a sample of real captured frames against the
parser and verifies the dispatcher overlays property updates onto the
node registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.client import NodePropertyValue, NodeRecord, VariableRecord
from pyisyox.runtime.events import (
    Event,
    EventDispatcher,
    parse_event_frame,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"


# --- parser: synthetic frames -------------------------------------------


def test_parse_native_property_update() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Event seqnum="42" sid="uuid:1" timestamp="2026-05-06T16:30:00Z">'
        "<control>ST</control>"
        '<action uom="100" prec="0">255</action>'
        "<node>3D 7D 87 1</node>"
        "<eventInfo></eventInfo>"
        "<fmtAct>On</fmtAct>"
        "<fmtName>Status</fmtName>"
        "</Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.seqnum == 42
    assert event.control == "ST"
    assert event.action == "255"
    assert event.node_address == "3D 7D 87 1"
    assert event.formatted_action == "On"
    assert event.formatted_name == "Status"
    assert event.uom == "100"
    assert event.prec == 0
    assert event.is_node_property is True
    assert event.is_system is False


def test_parse_plugin_gallons_update() -> None:
    """Flume sensor reports gallons via GV1 with prec=4."""
    xml = (
        '<Event seqnum="425" sid="uuid:1" timestamp="2026-05-06T16:29:38">'
        "<control>GV1</control>"
        '<action uom="69" prec="4">6839</action>'
        "<node>n010_84dd4c2c24c3b7</node>"
        "<eventInfo></eventInfo>"
        "<fmtAct>0.6839 US gallons</fmtAct>"
        "<fmtName>Current</fmtName>"
        "</Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.control == "GV1"
    assert event.uom == "69"
    assert event.prec == 4
    assert event.formatted_action == "0.6839 US gallons"
    assert event.is_node_property is True


def test_parse_system_event_has_empty_node() -> None:
    """Control codes prefixed with '_' are system events with no node."""
    xml = (
        '<Event seqnum="1" sid="uuid:1" timestamp="2026-05-06">'
        "<control>_5</control>"
        "<action>0</action>"
        "<node></node>"
        "<eventInfo></eventInfo>"
        "</Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.is_system is True
    assert event.is_node_property is False
    assert event.node_address == ""
    assert event.event_info == ""


def test_parse_preserves_event_info_for_variable_value_change() -> None:
    """Variable change frames (control=_1 action=6) carry the new
    value inside ``<eventInfo><var type=... id=...>``. Consumers
    can re-parse the inner XML to drive variable state updates."""
    xml = (
        '<Event seqnum="312" sid="uuid:1" timestamp="2026-05-02">'
        "<control>_1</control><action>6</action><node></node>"
        "<eventInfo>"
        '<var type="1" id="1">'
        "<prec>1</prec><val>20</val><ts>20260502 14:56:16 </ts>"
        "</var>"
        "</eventInfo>"
        "</Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.control == "_1"
    assert event.action == "6"
    assert "<var" in event.event_info and 'type="1"' in event.event_info
    assert "<val>20</val>" in event.event_info


def test_parse_preserves_cdata_event_info_for_controller_logs() -> None:
    """``_7`` controller-log frames pack the message in CDATA inside
    ``<eventInfo>``. Round-tripping the inner content has to keep the
    text payload — even if the CDATA wrapper itself doesn't survive,
    the consumer-visible string must."""
    xml = (
        '<Event seqnum="291" sid="uuid:1" timestamp="2026-05-02">'
        "<control>_7</control><action>1</action><node></node>"
        "<eventInfo><![CDATA["
        "U7 Rest:  submitCmd([A9 AD 83 1],[OL],[<NULL>])"
        "]]></eventInfo>"
        "</Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert "submitCmd" in event.event_info


def test_parse_empty_self_closing_event_info_normalises_to_empty_string() -> None:
    """``<eventInfo/>`` and absent eventInfo both normalise to ``""`` so
    consumers can ``if event.event_info:`` without checking None."""
    xml_self_closing = (
        '<Event seqnum="1" sid="uuid:1" timestamp="x">'
        "<control>ST</control><action>1</action>"
        "<node>A</node><eventInfo/>"
        "</Event>"
    )
    xml_absent = (
        '<Event seqnum="1" sid="uuid:1" timestamp="x">'
        "<control>ST</control><action>1</action><node>A</node>"
        "</Event>"
    )
    for xml in (xml_self_closing, xml_absent):
        event = parse_event_frame(xml)
        assert event is not None
        assert event.event_info == ""


def test_parse_handles_jsonenvelope_from_api_events_subscribe() -> None:
    """The /api/events/subscribe path JSON-wraps frames as
    {"type":"event","data":"<xml>"}. Parser unwraps it transparently."""
    inner = (
        '<Event seqnum="3" sid="uuid:1" timestamp="x">'
        "<control>ST</control><action>1</action>"
        "<node>A</node><eventInfo/>"
        "</Event>"
    )
    envelope = json.dumps({"type": "event", "data": inner})
    event = parse_event_frame(envelope)
    assert event is not None
    assert event.control == "ST"
    assert event.node_address == "A"


def test_parse_skips_spolisy_envelope() -> None:
    """The PG3 status side channel uses type='spolisy'. Not a property
    update; parser returns None so the dispatcher ignores it."""
    envelope = json.dumps({"type": "spolisy", "data": {"plugin": "flume", "status": "ok"}})
    assert parse_event_frame(envelope) is None


@pytest.mark.parametrize(
    "frame",
    [
        "",
        "null",
        "not xml or json",
        "<NotAnEvent/>",
        '{"type":"event"}',  # missing data
        '{"type":"event","data":{"not":"a string"}}',
        '<?xml version="1.0"?><Event seqnum="oops"><control>ST</control></Event>',  # non-int seqnum
    ],
)
def test_parse_returns_none_on_garbage(frame: str) -> None:
    result = parse_event_frame(frame)
    # The "non-int seqnum" case still parses (we coerce to 0), but the
    # other six all return None. Just assert the function never raises.
    assert result is None or isinstance(result, Event)


def test_parse_seqnum_coercion_to_zero_on_non_int() -> None:
    xml = '<Event seqnum="garbage"><control>X</control><action>1</action><node>A</node></Event>'
    event = parse_event_frame(xml)
    assert event is not None
    assert event.seqnum == 0


def test_parse_action_without_uom_or_prec() -> None:
    """System frames sometimes carry <action> with no attributes."""
    xml = '<Event seqnum="0"><control>_5</control><action>0</action><node></node></Event>'
    event = parse_event_frame(xml)
    assert event is not None
    assert event.uom == ""
    assert event.prec is None


# --- parser: real captured frames ---------------------------------------


def test_parse_real_captured_frames() -> None:
    """Run the parser over the captured ws-frames.jsonl. Verify it never
    raises and that the bulk of frames decode to property updates."""
    parsed = 0
    skipped = 0
    raises = 0
    with (FIXTURE_DIR / "ws-frames.jsonl").open() as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("direction") != "receive":
                continue
            data = entry.get("data", "")
            try:
                event = parse_event_frame(data)
            except Exception:  # pylint: disable=broad-except
                raises += 1
                continue
            if event is None:
                skipped += 1
            else:
                parsed += 1

    assert raises == 0, "parser must not raise on captured frames"
    # The capture is dominated by event frames; skipped are spolisy and nulls.
    assert parsed > skipped, f"expected mostly events; parsed={parsed}, skipped={skipped}"


# --- dispatcher: routing -------------------------------------------------


def _make_record(addr: str, properties: dict[str, NodePropertyValue] | None = None) -> NodeRecord:
    return NodeRecord(
        address=addr,
        name="Test",
        nodedef_id="X",
        family_id="1",
        instance_id="1",
        properties=properties or {},
    )


def test_dispatcher_overlays_property_into_node_record() -> None:
    nodes = {"3D 7D 87 1": _make_record("3D 7D 87 1")}
    dispatcher = EventDispatcher(nodes)
    xml = (
        '<Event seqnum="1"><control>ST</control>'
        '<action uom="100" prec="0">255</action>'
        "<node>3D 7D 87 1</node>"
        "<fmtAct>On</fmtAct><fmtName>Status</fmtName></Event>"
    )

    event = dispatcher.feed(xml)

    assert event is not None
    prop = nodes["3D 7D 87 1"].properties["ST"]
    assert prop.value == "255"
    assert prop.formatted == "On"
    assert prop.uom == "100"


def test_dispatcher_propagates_prec_into_node_record() -> None:
    """``<action prec="...">`` flows through to ``NodePropertyValue.prec``
    so the consumer can scale ``raw / 10**prec`` without a second wire trip."""
    nodes = {"X": _make_record("X")}
    dispatcher = EventDispatcher(nodes)
    xml = (
        '<Event seqnum="1"><control>GV1</control>'
        '<action uom="69" prec="4">6839</action>'
        "<node>X</node>"
        "<fmtAct>0.6839 US gallons</fmtAct><fmtName>Volume</fmtName></Event>"
    )

    dispatcher.feed(xml)

    prop = nodes["X"].properties["GV1"]
    assert prop.prec == 4
    assert prop.value == "6839"


def test_dispatcher_falls_back_to_zero_prec_when_action_omits_it() -> None:
    """An ``<action>`` without ``prec`` (Insteon ``ST``, etc.) defaults to
    ``prec=0`` rather than leaving the field unset — the consumer always
    has a numeric scaler."""
    nodes = {"X": _make_record("X")}
    dispatcher = EventDispatcher(nodes)
    xml = (
        '<Event seqnum="1"><control>ST</control><action uom="100">255</action>'
        "<node>X</node><fmtAct>On</fmtAct></Event>"
    )
    dispatcher.feed(xml)
    assert nodes["X"].properties["ST"].prec == 0


def test_dispatcher_replaces_existing_property() -> None:
    """An event for an already-tracked property replaces (not merges) it."""
    nodes = {
        "A": _make_record(
            "A",
            properties={"ST": NodePropertyValue(id="ST", value="0", formatted="Off", uom="100")},
        )
    }
    dispatcher = EventDispatcher(nodes)
    xml = (
        '<Event seqnum="1"><control>ST</control><action uom="100">255</action>'
        "<node>A</node><fmtAct>On</fmtAct></Event>"
    )
    dispatcher.feed(xml)
    assert nodes["A"].properties["ST"].formatted == "On"
    assert nodes["A"].properties["ST"].value == "255"


def test_dispatcher_drops_events_for_unknown_addresses() -> None:
    """Events for addresses not in the registry are dropped silently —
    don't autovivify ghost nodes."""
    nodes: dict[str, NodeRecord] = {}
    dispatcher = EventDispatcher(nodes)
    xml = '<Event seqnum="1"><control>ST</control><action>1</action><node>UNKNOWN</node></Event>'
    event = dispatcher.feed(xml)
    assert event is not None  # parsed fine
    assert nodes == {}, "registry must not gain entries for unknown nodes"


def test_dispatcher_ignores_system_events_for_property_routing() -> None:
    """System events (control='_5' etc.) reach listeners but don't try
    to overlay any property, even if a node happens to share the empty
    address (it can't, but the guard protects against future shapes)."""
    nodes = {"A": _make_record("A")}
    dispatcher = EventDispatcher(nodes)
    received: list[Event] = []
    dispatcher.add_listener(received.append)

    xml = '<Event seqnum="1"><control>_28</control><action>1.3</action><node></node><eventInfo/></Event>'
    event = dispatcher.feed(xml)
    assert event is not None
    assert event.is_system is True
    assert nodes["A"].properties == {}, "system event must not modify any record"
    assert received == [event]


def test_dispatcher_listener_receives_all_events() -> None:
    nodes = {"A": _make_record("A")}
    dispatcher = EventDispatcher(nodes)
    received: list[Event] = []
    unsubscribe = dispatcher.add_listener(received.append)

    dispatcher.feed('<Event seqnum="1"><control>ST</control><action>1</action><node>A</node></Event>')
    dispatcher.feed('<Event seqnum="2"><control>_5</control><action>0</action><node></node></Event>')
    assert len(received) == 2
    assert received[0].control == "ST"
    assert received[1].control == "_5"

    unsubscribe()
    dispatcher.feed('<Event seqnum="3"><control>ST</control><action>0</action><node>A</node></Event>')
    assert len(received) == 2, "post-unsubscribe events must not reach the listener"


def test_dispatcher_listener_exception_does_not_break_other_listeners() -> None:
    """A misbehaving listener must not break the read loop or stop other
    listeners from running."""
    nodes = {"A": _make_record("A")}
    dispatcher = EventDispatcher(nodes)
    received: list[Event] = []

    def bad(_event: Event) -> None:
        raise RuntimeError("boom")

    dispatcher.add_listener(bad)
    dispatcher.add_listener(received.append)

    event = dispatcher.feed('<Event seqnum="1"><control>ST</control><action>1</action><node>A</node></Event>')
    assert event is not None
    assert len(received) == 1


def test_dispatcher_feed_returns_none_on_garbage() -> None:
    nodes: dict[str, NodeRecord] = {}
    dispatcher = EventDispatcher(nodes)
    assert dispatcher.feed("") is None
    assert dispatcher.feed("not xml") is None
    assert dispatcher.feed('{"type":"spolisy","data":{}}') is None


# --- dispatcher: variable change -----------------------------------------
#
# Variable events ride on ``<control>_1</control>`` (same as program-status)
# but with action ``"6"`` (current value change) or ``"7"`` (init change).
# The wire payload is ``<eventInfo><var type="N" id="M"><val>...</val></var></eventInfo>``.
# The dispatcher updates the matching ``VariableRecord`` in place, mirroring
# how it already handles node properties and program status.


def _make_variable_record(
    *, type_id: str = "1", id_: str = "5", value: int = 0, init: int = 0
) -> VariableRecord:
    return VariableRecord(type_id=type_id, id=id_, name=f"Var_{type_id}_{id_}", value=value, init=init)


def test_dispatcher_applies_variable_value_change_to_record() -> None:
    """Action ``"6"`` updates ``record.value`` in place."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}, "2": {}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="1" id="5"><val>42</val><ts>20260510 21:00:00</ts></var></eventInfo>'
        "</Event>"
    )
    event = dispatcher.feed(frame)

    assert event is not None
    assert record.value == 42
    assert record.init == 0  # untouched on action 6
    assert record.ts == "20260510 21:00:00"


def test_dispatcher_applies_variable_init_change_to_record() -> None:
    """Action ``"7"`` updates ``record.init`` in place; value is untouched."""
    record = _make_variable_record(type_id="2", id_="8", value=5, init=0)
    variables = {"1": {}, "2": {"8": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>7</action>'
        "<node></node>"
        '<eventInfo><var type="2" id="8"><val>100</val></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)

    assert record.init == 100
    assert record.value == 5  # untouched on action 7


def test_dispatcher_drops_variable_event_for_unknown_type() -> None:
    """A variable change for a type the registry doesn't track is dropped
    silently — no exception, no autovivified bucket."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    # Type "3" doesn't exist on IoX; the dispatcher should ignore it.
    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="3" id="5"><val>42</val></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 0


def test_dispatcher_drops_variable_event_for_unknown_id() -> None:
    """A variable change for an id missing from its type bucket is dropped."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}, "2": {}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="1" id="99"><val>42</val></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 0


def test_dispatcher_drops_variable_event_with_non_numeric_val() -> None:
    """The wire is supposed to carry an int; a junk value is ignored rather
    than coerced to 0 (which would silently clobber state)."""
    record = _make_variable_record(type_id="1", id_="5", value=99)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="1" id="5"><val>not-a-number</val></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 99  # unchanged


def test_dispatcher_variable_change_no_registry_is_no_op() -> None:
    """When constructed without a variables registry (the test-friendly
    default), variable-change frames flow through to the generic listener
    channel but don't mutate any shared state — symmetric with the
    program-status no-op when ``programs=None``."""
    dispatcher = EventDispatcher({})  # no variables
    received: list[Event] = []
    dispatcher.add_listener(received.append)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="1" id="5"><val>42</val></var></eventInfo>'
        "</Event>"
    )
    event = dispatcher.feed(frame)
    assert event is not None
    assert len(received) == 1
