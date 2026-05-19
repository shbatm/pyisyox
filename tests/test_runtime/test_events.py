"""Tests for the WebSocket event parser + dispatcher.

Runs synthetic frames + a sample of real captured frames against the
parser and verifies the dispatcher overlays property updates onto the
node registry.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pyisyox.client import (
    GroupRecord,
    IoXClient,
    NodePropertyValue,
    NodeRecord,
    ProgramRecord,
    VariableRecord,
)
from pyisyox.runtime.events import (
    Event,
    EventDispatcher,
    NodeLifecycleEvent,
    ProgramEvalState,
    ProgramRunState,
    ProgramStatusEvent,
    SystemEventControl,
    VariableTableChangeEvent,
    _decode_program_status_byte,
    _extract_event_tz,
    _extract_lifecycle_node_xml,
    parse_event_frame,
)
from pyisyox.runtime.program import Program

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
    assert event.precision == 0
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
    assert event.precision == 4
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
    assert event.precision is None


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


def test_dispatcher_logs_property_update_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A node-property frame gets a DEBUG line (the raw WS frame itself
    only logs at VERBOSE) — using the controller's formatted value plus
    the raw value / uom for context."""
    nodes = {"3D 7D 87 1": _make_record("3D 7D 87 1")}
    dispatcher = EventDispatcher(nodes)
    xml = (
        '<Event seqnum="1"><control>OL</control>'
        '<action uom="100" prec="0">191</action>'
        "<node>3D 7D 87 1</node>"
        "<fmtAct>75%</fmtAct><fmtName>On Level</fmtName></Event>"
    )

    with caplog.at_level(logging.DEBUG, logger="pyisyox.runtime.events"):
        dispatcher.feed(xml)

    assert "Node 3D 7D 87 1 OL -> 75% (raw=191, uom=100)" in caplog.text


def test_dispatcher_propagates_prec_into_node_record() -> None:
    """``<action prec="...">`` flows through to ``NodePropertyValue.precision``
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
    assert prop.precision == 4
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
    assert nodes["X"].properties["ST"].precision == 0


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
    """Action ``"7"`` updates ``record.init`` in place; value is untouched.

    Real eisy firmware emits the new init value in the ``<init>`` element
    of the ``<var>`` payload (alongside ``<prec>``), **not** ``<val>`` —
    earlier dispatcher logic looked for ``<val>`` only and silently
    dropped every init frame the controller emitted. This fixture
    matches what was captured on a live eisy at FW 6.0."""
    record = _make_variable_record(type_id="2", id_="8", value=5, init=0)
    variables = {"1": {}, "2": {"8": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>7</action>'
        "<node></node>"
        '<eventInfo><var type="2" id="8"><init>81</init><prec>1</prec></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)

    assert record.init == 81
    assert record.value == 5  # untouched on action 7


def test_dispatcher_applies_variable_init_change_with_val_fallback() -> None:
    """A frame emitting ``<val>`` on an action-7 init change still works
    — older / alternative firmwares may reuse the value element name
    even for init changes. The dispatcher prefers ``<init>`` and falls
    back to ``<val>`` so both shapes round-trip."""
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
    assert record.value == 5


def test_dispatcher_applies_variable_float_change_to_record() -> None:
    """A float-valued variable change (modern API can store floats)
    parses as ``float`` rather than dropping the frame."""
    record = _make_variable_record(type_id="2", id_="8", value=0, init=0)
    variables = {"1": {}, "2": {"8": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node>"
        '<eventInfo><var type="2" id="8"><val>51.5</val></var></eventInfo>'
        "</Event>"
    )
    dispatcher.feed(frame)

    assert record.value == 51.5


def test_dispatcher_fires_variable_table_change_listener() -> None:
    """Action ``"9"`` is the controller's signal that a variable was
    added/removed or had its precision changed on a given type. The
    dispatcher fires registered listeners; it does **not** auto-refresh
    the registry (that's the consumer's call). Listeners receive the
    type id pulled from ``<var type="N">`` (attribute) or
    ``<var><type>N</type></var>`` (child-element) — both shapes have
    been observed across firmwares."""
    received: list[VariableTableChangeEvent] = []
    dispatcher = EventDispatcher({}, variables={"1": {}, "2": {}})
    dispatcher.add_variable_table_change_listener(received.append)

    # Attribute form.
    dispatcher.feed(
        '<Event seqnum="42"><control>_1</control><action>9</action>'
        "<node></node>"
        '<eventInfo><var type="2" id="0"/></eventInfo>'
        "</Event>"
    )
    # Child-element form.
    dispatcher.feed(
        '<Event seqnum="43"><control>_1</control><action>9</action>'
        "<node></node>"
        "<eventInfo><var><type>1</type><id>0</id></var></eventInfo>"
        "</Event>"
    )

    assert [(e.type_id, e.seqnum) for e in received] == [("2", 42), ("1", 43)]


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


# --- dispatcher: variable change — defensive paths -----------------------
#
# Same "the firmware sometimes sends partial shapes" pattern as the
# program-status defensive paths above; pin every short-circuit so a
# future refactor doesn't regress to a crash.


def test_variable_event_with_empty_event_info_is_dropped() -> None:
    """A ``_1/6`` frame with empty ``<eventInfo/>`` carries no decode
    target — the apply step short-circuits before touching the
    registry."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = '<Event seqnum="1"><control>_1</control><action>6</action><node></node><eventInfo/></Event>'
    dispatcher.feed(frame)
    assert record.value == 0


def test_variable_event_without_var_child_is_dropped() -> None:
    """``<eventInfo>`` present but missing the ``<var>`` child — drop."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node><eventInfo><other /></eventInfo></Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 0


@pytest.mark.parametrize(
    "var_attrs",
    [
        'id="5"',  # missing type
        'type="1"',  # missing id
        'type="" id="5"',  # empty type
        'type="1" id=""',  # empty id
    ],
)
def test_variable_event_without_type_or_id_attrs_is_dropped(var_attrs: str) -> None:
    """``<var>`` must carry both ``type`` and ``id`` to route to a record."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        f"<node></node><eventInfo><var {var_attrs}><val>42</val></var></eventInfo></Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 0


def test_variable_event_with_empty_val_is_dropped() -> None:
    """``<val/>`` (no text) means the firmware didn't actually carry a
    value — drop rather than coerce empty to 0."""
    record = _make_variable_record(type_id="1", id_="5", value=99)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        '<node></node><eventInfo><var type="1" id="5"><val></val></var></eventInfo></Event>'
    )
    dispatcher.feed(frame)
    assert record.value == 99  # unchanged


def test_variable_event_with_malformed_event_info_xml_is_dropped() -> None:
    """A pathological eventInfo containing escaped ``</eventInfo>`` text
    re-wraps to malformed XML when the apply step concatenates the tags
    back — the try/except guards against any such firmware oddities.
    Mirror test for program-status decode below."""
    record = _make_variable_record(type_id="1", id_="5", value=0)
    variables = {"1": {"5": record}}
    dispatcher = EventDispatcher({}, variables=variables)

    frame = (
        '<Event seqnum="1"><control>_1</control><action>6</action>'
        "<node></node><eventInfo>&lt;/eventInfo&gt;</eventInfo></Event>"
    )
    dispatcher.feed(frame)
    assert record.value == 0


def test_program_status_with_malformed_event_info_xml_is_dropped() -> None:
    """Same trick as the variable test above: escaped ``</eventInfo>``
    text in the outer frame survives outer-parse, then breaks the
    inner re-wrap. The try/except in ``_apply_program_status`` swallows
    it."""
    program = ProgramRecord(
        address="008D",
        name="Foo",
        path="",
        parent_address=None,
        is_folder=False,
        status=False,
    )
    dispatcher = EventDispatcher({}, programs={"008D": program})

    frame = (
        '<Event seqnum="1"><control>_1</control><action>0</action>'
        "<node></node><eventInfo>&lt;/eventInfo&gt;</eventInfo></Event>"
    )
    event = dispatcher.feed(frame)
    # feed must produce an Event (proves the dispatcher ran) and the
    # apply step must not have flipped status (proves the ParseError
    # branch in _apply_program_status caught the re-wrap failure).
    assert event is not None
    assert event.control == "_1"
    assert program.status is False


# --- parser: more permissive-decode edge cases --------------------------


@pytest.mark.parametrize(
    "frame",
    [
        "<Event seqnum='1'><control",  # malformed XML — ET.ParseError
        '{"type":"event","data":"<bad-xml"}',  # JSON envelope wrapping malformed XML
        "{not valid json",  # starts with `{` but isn't JSON — JSONDecodeError
        "[1,2,3]",  # valid JSON but not a dict
    ],
)
def test_parse_returns_none_on_broken_payloads(frame: str) -> None:
    """Every malformed-but-shaped frame must be dropped, never raise."""
    assert parse_event_frame(frame) is None


def test_parse_ignores_non_numeric_prec_attribute() -> None:
    """``prec="abc"`` must coerce to ``None`` rather than crash the parser."""
    xml = (
        '<Event seqnum="1"><control>ST</control><action uom="100" prec="abc">1</action><node>A</node></Event>'
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.precision is None


def test_parse_preserves_event_info_with_tail_text_between_children() -> None:
    """Mixed-content ``<eventInfo>`` (text + child + tail text) round-trips
    via the string-builder path; tails between children mustn't be lost."""
    xml = (
        '<Event seqnum="1"><control>_7</control><action>0</action><node></node>'
        "<eventInfo>head<inner/>tail-text<after/></eventInfo></Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert "head" in event.event_info
    assert "tail-text" in event.event_info


def test_parse_recovers_from_unescaped_ampersand_in_event_info() -> None:
    """eisy occasionally emits ``_7`` REST-log frames whose
    ``<eventInfo>`` echoes a query-string with unescaped ``&`` (e.g.
    a Z-Wave ``submitCmd(..., NUM.107=24&VAL.111=1)`` call). Strict
    XML rejects those, but the recovery path re-escapes stray
    ampersands and re-parses so the frame still becomes an ``Event``.
    """
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Event seqnum="74"><control>_7</control><action>1</action><node></node>'
        "<eventInfo>U7 Rest:  submitCmd([ZW003_1],[CONFIG],[NUM.107=24&VAL.111=1])"
        "</eventInfo></Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert event.control == "_7"
    assert event.action == "1"
    # The recovered ``eventInfo`` carries the original ampersand back
    # as part of the text payload — consumers reading it as a string
    # (not re-parsing) see the source form they expect.
    assert "NUM.107=24&VAL.111=1" in event.event_info


def test_parse_preserves_existing_entity_refs_when_recovering() -> None:
    """A frame that mixes a properly escaped ``&amp;`` with a stray
    ``&`` must round-trip the escaped one verbatim — the recovery
    regex skips known entity references so ``&amp;amp;`` doesn't
    creep in."""
    xml = (
        '<Event seqnum="1"><control>_7</control><action>0</action><node></node>'
        "<eventInfo>a &amp; b & c</eventInfo></Event>"
    )
    event = parse_event_frame(xml)
    assert event is not None
    assert "a & b & c" in event.event_info


# --- _extract_lifecycle_node_xml direct unit tests -----------------------
#
# These edge cases aren't reachable through ``dispatcher.feed`` because
# the dispatcher only calls the helper after a successful frame parse —
# the helper has its own defensive guards for the wider call surface.


def test_extract_lifecycle_node_xml_drops_non_event_json_envelope() -> None:
    """A non-event JSON envelope short-circuits before re-parsing."""
    assert _extract_lifecycle_node_xml('{"type":"spolisy","data":"x"}') is None


def test_extract_lifecycle_node_xml_drops_malformed_xml() -> None:
    """Malformed inner XML (post-envelope-unwrap) returns ``None`` rather
    than raising — defensive guard for future envelope shapes."""
    assert _extract_lifecycle_node_xml("<Event<broken") is None


def test_extract_lifecycle_node_xml_without_event_info_returns_none() -> None:
    assert _extract_lifecycle_node_xml("<Event><control>_3</control></Event>") is None


def test_extract_lifecycle_node_xml_without_inner_node_returns_none() -> None:
    """``eventInfo`` present but no inner ``<node>`` (rename / remove
    actions carry only the address)."""
    xml = "<Event><control>_3</control><eventInfo><addr>A</addr></eventInfo></Event>"
    assert _extract_lifecycle_node_xml(xml) is None


# --- _extract_event_tz direct unit tests ---------------------------------


def test_extract_event_tz_empty_returns_none() -> None:
    assert _extract_event_tz("") is None


def test_extract_event_tz_naive_iso_returns_none() -> None:
    """No offset → ``parsed.tzinfo`` is ``None``; caller falls back to local."""
    assert _extract_event_tz("2026-05-14T20:47:26.828098") is None


def test_extract_event_tz_malformed_returns_none() -> None:
    assert _extract_event_tz("not-a-timestamp") is None


def test_extract_event_tz_offset_bearing_returns_tzinfo() -> None:
    tz = _extract_event_tz("2026-05-14T20:47:26.828098-05:00")
    assert tz is not None
    assert tz.utcoffset(None) == timedelta(hours=-5)


# --- dispatcher: listener unsubscribe-twice + lifecycle / program errors --


def test_dispatcher_unsubscribe_twice_is_safe_for_every_listener_type() -> None:
    """Double-unsubscribe on each of the three listener registries
    suppresses the ValueError from the redundant ``list.remove``."""
    dispatcher = EventDispatcher({})
    for register in (
        dispatcher.add_listener,
        dispatcher.add_program_status_listener,
        dispatcher.add_lifecycle_listener,
    ):
        unsub = register(lambda _e: None)
        unsub()
        unsub()  # must not raise


def test_dispatcher_lifecycle_listener_exception_does_not_break_others() -> None:
    """A raising lifecycle listener must not stop subsequent listeners."""
    dispatcher = EventDispatcher({})
    received: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))
    dispatcher.add_lifecycle_listener(received.append)

    dispatcher.feed(
        '<Event seqnum="1"><control>_3</control><action>ND</action>'
        '<node>A</node><eventInfo><node id="A"><name>X</name></node></eventInfo></Event>'
    )
    assert len(received) == 1


# --- dispatcher: program-status decode edge cases ------------------------


def _make_program(address: str = "008D") -> ProgramRecord:
    return ProgramRecord(
        address=address,
        name="Foo",
        path="",
        parent_address=None,
        is_folder=False,
        status=False,
    )


def _program_frame(event_info: str, *, seqnum: int = 1, timestamp: str = "") -> str:
    ts_attr = f' timestamp="{timestamp}"' if timestamp else ""
    return (
        f'<Event seqnum="{seqnum}"{ts_attr}><control>_1</control><action>0</action><node></node>'
        f"<eventInfo>{event_info}</eventInfo></Event>"
    )


def test_program_status_with_empty_event_info_is_dropped() -> None:
    """A ``_1`` frame with an empty ``<eventInfo/>`` carries no decode
    target; the apply step short-circuits."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(
        '<Event seqnum="1"><control>_1</control><action>0</action><node></node><eventInfo/></Event>'
    )
    assert program.status is False, "no event_info means no status change"


def test_program_status_with_missing_id_is_dropped() -> None:
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})
    dispatcher.feed(_program_frame("<on /><s>21</s>"))
    assert program.status is False, "no <id> means we can't match a record"


def test_program_status_without_on_off_marker_preserves_enabled() -> None:
    """A frame without ``<on/>`` / ``<off/>`` (the enabled-flag elements)
    leaves ``record.enabled`` unchanged — the dispatcher only mutates
    when the wire actually carries a new value. ``<s>21</s>`` still
    drives status from its eval-state nibble (TRUE → True)."""
    program = _make_program()
    program.enabled = True
    program.status = False
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(_program_frame("<id>8D</id><weird /><s>21</s>"))
    assert program.enabled is True, "no on/off marker preserves prior enabled"
    assert program.status is True, "<s>21 has eval-state TRUE → status flips True"


def test_program_status_with_non_integer_running_is_dropped() -> None:
    """The ``<s>`` running-state value is best-effort-int; garbage there
    must not crash the apply step. With no decoded eval state, status
    carries forward; ``<on/>`` still updates the enabled flag."""
    program = _make_program()
    program.status = False
    program.enabled = False
    program.running = None
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(_program_frame("<id>8D</id><on /><s>not-a-number</s>"))
    assert program.status is False, "garbage <s> leaves status unchanged"
    assert program.enabled is True, "<on/> still flips enabled True"
    assert program.running is None, "non-int <s> leaves running unchanged"


def test_decode_program_status_byte_splits_nibbles() -> None:
    """Cookbook §8.5.3: ``<s>`` is a bitwise OR of RUN_* (low nibble)
    and ST_* (high nibble); decode each separately."""
    assert _decode_program_status_byte(0x21) == (
        ProgramRunState.IDLE,
        ProgramEvalState.TRUE,
    )
    assert _decode_program_status_byte(0x22) == (
        ProgramRunState.THEN,
        ProgramEvalState.TRUE,
    )
    assert _decode_program_status_byte(0x33) == (
        ProgramRunState.ELSE,
        ProgramEvalState.FALSE,
    )
    assert _decode_program_status_byte(0x11) == (
        ProgramRunState.IDLE,
        ProgramEvalState.UNKNOWN,
    )
    # NOT_LOADED has no run state — the program isn't running.
    assert _decode_program_status_byte(0xF0) == (None, ProgramEvalState.NOT_LOADED)
    # Missing wire field → both None.
    assert _decode_program_status_byte(None) == (None, None)
    # Unrecognised bit pattern → both None (defensive against future
    # firmware additions, doesn't raise).
    assert _decode_program_status_byte(0xAB) == (None, None)
    # Partial validity is decoded independently — a recognised low
    # nibble survives an unknown high nibble (and vice versa).
    assert _decode_program_status_byte(0xA3) == (ProgramRunState.ELSE, None)
    assert _decode_program_status_byte(0x2C) == (None, ProgramEvalState.TRUE)


def test_program_status_event_carries_decoded_run_and_eval_state() -> None:
    """The :class:`ProgramStatusEvent` exposes typed ``run_state`` and
    ``eval_state`` derived from the same ``<s>`` byte."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})
    received: list[ProgramStatusEvent] = []
    dispatcher.add_program_status_listener(received.append)

    # ELSE | FALSE → 0x33 (cookbook §8.5.3: wire byte is two hex digits)
    dispatcher.feed(_program_frame("<id>8D</id><off /><s>33</s>"))
    assert len(received) == 1
    event = received[0]
    assert event.running == 0x33
    assert event.run_state is ProgramRunState.ELSE
    assert event.eval_state is ProgramEvalState.FALSE


def test_program_status_event_run_state_is_none_when_not_loaded() -> None:
    """``ST_NOT_LOADED`` has no low-nibble run code; ``run_state`` is
    ``None`` while ``eval_state`` carries the NOT_LOADED marker."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})
    received: list[ProgramStatusEvent] = []
    dispatcher.add_program_status_listener(received.append)

    # NOT_LOADED = 0xF0 (cookbook §8.5.3: wire byte is two hex digits)
    dispatcher.feed(_program_frame("<id>8D</id><on /><s>F0</s>"))
    assert len(received) == 1
    event = received[0]
    assert event.run_state is None
    assert event.eval_state is ProgramEvalState.NOT_LOADED


def test_program_status_writes_timestamps_to_record() -> None:
    """``<r>`` / ``<f>`` are parsed from ``YYMMDD HH:MM:SS`` controller-
    local into UTC-suffixed ISO 8601 strings on the record (the test
    suite forces ``TZ=UTC`` so the local→UTC conversion is a no-op
    and the wall-clock survives). The typed :class:`Program` accessors
    decode either shape. This frame omits ``<nsr>``, so
    ``next_scheduled_run_time`` falls under the absent-field-preserves-
    prior-value path."""
    program = _make_program()
    program.last_run_time = "2026-05-01T00:00:00"
    program.last_finish_time = "2026-05-01T00:00:00"
    program.next_scheduled_run_time = "2026-05-15T18:00:00"
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(
        _program_frame("<id>8D</id><on /><nr /><r>260514 16:44:11 </r><f>260514 16:44:21 </f><s>21</s>")
    )

    assert program.last_run_time == "2026-05-14T16:44:11+00:00"
    assert program.last_finish_time == "2026-05-14T16:44:21+00:00"
    assert program.next_scheduled_run_time == "2026-05-15T18:00:00"


def test_program_status_uses_event_frame_tz_for_body_timestamps() -> None:
    """The eisy stamps every WS frame with its own local time + offset
    (e.g. ``-05:00``). The dispatcher uses that offset — not the host's
    system tz — when interpreting the naive ``YYMMDD HH:MM:SS`` body
    timestamps. This is the only correct path inside a ``TZ=UTC``
    container talking to an eisy in a different zone (the common
    devcontainer setup)."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})

    # Eisy in CDT: frame timestamp carries -05:00 offset; <r>/<f>
    # body strings are 20:42:42 *local CDT* → 01:42:42 *UTC* of the
    # next day.
    frame = _program_frame(
        "<id>8D</id><on /><r>260514 20:42:42 </r><f>260514 20:42:42 </f><s>21</s>",
        timestamp="2026-05-14T20:47:26.828098-05:00",
    )
    dispatcher.feed(frame)

    assert program.last_run_time == "2026-05-15T01:42:42+00:00"
    assert program.last_finish_time == "2026-05-15T01:42:42+00:00"


def test_program_status_falls_back_to_system_tz_when_event_timestamp_missing() -> None:
    """When the event frame has no timestamp attribute, the parser
    falls back to system-local tz (``TZ=UTC`` in the test suite, so the
    fall-back path is a no-op and the wall-clock survives)."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})

    # No ``timestamp=`` arg → no offset on the frame → fall-back path.
    dispatcher.feed(_program_frame("<id>8D</id><on /><r>260514 16:44:11 </r><s>21</s>"))

    assert program.last_run_time == "2026-05-14T16:44:11+00:00"


def test_program_status_writes_next_scheduled_from_nsr_partial_frame() -> None:
    """``<nsr>`` is the next-scheduled-run timestamp. Often arrives
    standalone — ``<id>`` + ``<nsr>`` with no other fields — when the
    controller plans the next run after a schedule fires. The dispatcher
    updates ``record.next_scheduled_run_time`` and leaves every other
    field untouched (absent-field-preserves-prior-value). Confirmed
    against live capture from real eisy hardware (2026-05-14)."""
    program = _make_program()
    program.last_run_time = "2026-05-13T10:00:00+00:00"
    program.enabled = True
    program.run_at_startup = True
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(_program_frame("<id>8D</id><nsr>260515 21:02:00 </nsr>"))

    assert program.next_scheduled_run_time == "2026-05-15T21:02:00+00:00"
    # Untouched siblings:
    assert program.last_run_time == "2026-05-13T10:00:00+00:00"
    assert program.enabled is True
    assert program.run_at_startup is True


def test_program_status_timestamp_round_trips_to_aware_datetime() -> None:
    """End-to-end check: the WS local-time wire shape parses through
    the dispatcher into a stored UTC ISO 8601 string, and the typed
    :class:`Program.last_run_time` accessor decodes back to a tz-aware
    :class:`datetime` whose UTC wall-clock matches the wire (under the
    test suite's forced UTC tz)."""
    program_record = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program_record})

    dispatcher.feed(
        _program_frame("<id>8D</id><on /><r>260514 16:44:11 </r><f>260514 16:44:21 </f><s>21</s>")
    )

    # ``IoXClient.__new__`` skips ``__init__`` to build a stub client
    # without an aiohttp session — safe here because the timestamp
    # accessors only read ``Program._record`` and never touch the
    # client. If a future ``Program`` accessor calls into the client,
    # this test will surface the missing init via AttributeError.
    program = Program(program_record, IoXClient.__new__(IoXClient))
    assert program.last_run_time == datetime(2026, 5, 14, 16, 44, 11, tzinfo=UTC)
    assert program.last_finish_time == datetime(2026, 5, 14, 16, 44, 21, tzinfo=UTC)


def test_program_status_garbage_timestamp_leaves_record_unchanged() -> None:
    """Unparsable ``<r>`` text doesn't crash and leaves the prior value."""
    program = _make_program()
    program.last_run_time = "2026-05-01T00:00:00"
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(_program_frame("<id>8D</id><on /><r>not-a-timestamp</r>"))

    assert program.last_run_time == "2026-05-01T00:00:00"


def test_program_status_run_at_startup_flag() -> None:
    """``<rr/>`` = run-at-reboot enabled, ``<nr/>`` = disabled. Mutually
    exclusive on the wire (every captured PROGRAM_STATUS frame carries
    exactly one). Confirmed against live captures from real eisy
    hardware."""
    program = _make_program()
    program.run_at_startup = False
    dispatcher = EventDispatcher({}, programs={"008D": program})

    # <rr/> → run_at_startup = True
    dispatcher.feed(_program_frame("<id>8D</id><on /><rr /><s>21</s>"))
    assert program.run_at_startup is True

    # <nr/> → run_at_startup = False
    dispatcher.feed(_program_frame("<id>8D</id><on /><nr /><s>21</s>"))
    assert program.run_at_startup is False


def test_program_status_omitted_run_at_startup_preserves_value() -> None:
    """A frame without either ``<rr/>`` or ``<nr/>`` leaves the flag
    unchanged — same "absent → preserve" pattern as the enabled flag."""
    program = _make_program()
    program.run_at_startup = True
    dispatcher = EventDispatcher({}, programs={"008D": program})

    dispatcher.feed(_program_frame("<id>8D</id><on /><s>21</s>"))

    assert program.run_at_startup is True


def test_program_status_event_run_state_is_none_when_running_absent() -> None:
    """When ``<s>`` is missing entirely, both typed fields are ``None``."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})
    received: list[ProgramStatusEvent] = []
    dispatcher.add_program_status_listener(received.append)

    dispatcher.feed(_program_frame("<id>8D</id><on />"))
    assert len(received) == 1
    event = received[0]
    assert event.running is None
    assert event.run_state is None
    assert event.eval_state is None


def test_program_status_listener_exception_does_not_break_others() -> None:
    """A raising program-status listener must not stop subsequent listeners."""
    program = _make_program()
    dispatcher = EventDispatcher({}, programs={"008D": program})
    received: list[ProgramStatusEvent] = []
    dispatcher.add_program_status_listener(lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))
    dispatcher.add_program_status_listener(received.append)

    dispatcher.feed(_program_frame("<id>8D</id><on /><s>21</s>"))
    assert len(received) == 1


# --- SystemEventControl public surface -----------------------------------


def test_system_event_control_documented_codes() -> None:
    """Pin the wire codes for each documented system event.

    Source: PyISY 3.x ``events/websocket.py`` (canonical legacy mapping)
    plus the pyisyox v6 dispatcher's own internal usage.
    """
    assert SystemEventControl.HEARTBEAT == "_0"
    assert SystemEventControl.TRIGGER == "_1"
    assert SystemEventControl.NODE_LIFECYCLE == "_3"
    assert SystemEventControl.SYSTEM_STATUS == "_5"
    assert SystemEventControl.PROGRESS == "_7"
    assert SystemEventControl.MATTER_STATUS == "_28"


def test_system_event_control_label_known_code_renders_lowercase_name() -> None:
    """Known codes render as their lowercased enum-name for log readability."""
    assert SystemEventControl.label("_3") == "node_lifecycle"
    assert SystemEventControl.label("_5") == "system_status"
    assert SystemEventControl.label("_28") == "matter_status"


def test_system_event_control_label_unknown_code_returns_raw() -> None:
    """Unknown system control codes pass through verbatim — the log line
    still identifies the wire code, callers can correlate against
    captures even when pyisyox hasn't enumerated the code."""
    assert SystemEventControl.label("_42") == "_42"
    assert SystemEventControl.label("_99") == "_99"


def test_system_event_control_label_handles_arbitrary_string() -> None:
    """``label`` accepts any ``str`` so consumers can pass
    ``event.control`` unguarded. Property-update controls (``"ST"``,
    ``"GV1"``, etc.) are non-system and pass through verbatim."""
    assert SystemEventControl.label("ST") == "ST"
    assert SystemEventControl.label("") == ""


# --- dispatcher: group status re-emit (pyisy-3.x parity) -----------------


def test_dispatcher_reemits_member_property_change_as_group_event() -> None:
    """A member node's ST change fans out a synthetic event addressed
    to each containing group — restoring the pyisy-3.x behaviour where
    Group re-published its own status when a member changed."""
    member = "3D 7D 87 1"
    nodes = {member: _make_record(member)}
    groups = {
        "5000": GroupRecord(
            address="5000",
            name="Living Room Scene",
            nodedef_id="InsteonDimmer",
            family_id="1",
            instance_id="1",
            member_addresses=(member, "40 4E 68 1"),
        )
    }
    dispatcher = EventDispatcher(nodes, groups=groups)
    seen: list[Event] = []
    dispatcher.add_listener(seen.append)

    xml = (
        '<Event seqnum="7"><control>ST</control>'
        '<action uom="100" prec="0">255</action>'
        f"<node>{member}</node>"
        "<fmtAct>On</fmtAct><fmtName>Status</fmtName></Event>"
    )
    dispatcher.feed(xml)

    # The real member event plus one synthetic group-addressed event.
    assert [e.node_address for e in seen] == [member, "5000"]
    group_event = seen[1]
    assert group_event.control == "ST"
    assert group_event.node_address == "5000"
    assert group_event.seqnum == 7


def test_dispatcher_reemits_to_every_containing_group() -> None:
    """A node in multiple scenes triggers one synthetic event per scene."""
    member = "AA BB CC 1"
    nodes = {member: _make_record(member)}
    groups = {
        "100": GroupRecord(
            address="100",
            name="G1",
            nodedef_id="InsteonDimmer",
            family_id="1",
            member_addresses=(member,),
        ),
        "200": GroupRecord(
            address="200",
            name="G2",
            nodedef_id="InsteonDimmer",
            family_id="1",
            member_addresses=("ZZ ZZ ZZ 1", member),
        ),
    }
    dispatcher = EventDispatcher(nodes, groups=groups)
    seen: list[str] = []
    dispatcher.add_listener(lambda e: seen.append(e.node_address))

    xml = f'<Event seqnum="1"><control>ST</control><action uom="100">0</action><node>{member}</node></Event>'
    dispatcher.feed(xml)

    assert seen[0] == member
    assert sorted(seen[1:]) == ["100", "200"]


def test_dispatcher_no_group_reemit_for_non_member_node() -> None:
    """A node that is in no scene produces no synthetic group event."""
    nodes = {"LONE 1": _make_record("LONE 1")}
    groups = {
        "300": GroupRecord(
            address="300",
            name="Other",
            nodedef_id="InsteonDimmer",
            family_id="1",
            member_addresses=("SOMEONE ELSE 1",),
        )
    }
    dispatcher = EventDispatcher(nodes, groups=groups)
    seen: list[str] = []
    dispatcher.add_listener(lambda e: seen.append(e.node_address))

    dispatcher.feed(
        '<Event seqnum="1"><control>ST</control><action uom="100">255</action><node>LONE 1</node></Event>'
    )

    assert seen == ["LONE 1"]


def test_dispatcher_without_groups_is_legacy_noop() -> None:
    """Omitting ``groups`` keeps the pre-fix behaviour: member events
    only, no synthetic group fan-out."""
    member = "3D 7D 87 1"
    dispatcher = EventDispatcher({member: _make_record(member)})
    seen: list[str] = []
    dispatcher.add_listener(lambda e: seen.append(e.node_address))

    dispatcher.feed(
        f'<Event seqnum="1"><control>ST</control><action uom="100">255</action><node>{member}</node></Event>'
    )

    assert seen == [member]


def test_dispatcher_group_reemit_skips_system_events() -> None:
    """System frames (no node address) never trigger a group re-emit
    even if the (empty) address somehow indexed."""
    member = "3D 7D 87 1"
    nodes = {member: _make_record(member)}
    groups = {
        "5000": GroupRecord(
            address="5000",
            name="S",
            nodedef_id="InsteonDimmer",
            family_id="1",
            member_addresses=(member,),
        )
    }
    dispatcher = EventDispatcher(nodes, groups=groups)
    seen: list[str] = []
    dispatcher.add_listener(lambda e: seen.append(e.node_address))

    # Heartbeat-style system event — empty node address.
    dispatcher.feed('<Event seqnum="1"><control>_5</control><action>0</action><node></node></Event>')

    assert seen == [""]
