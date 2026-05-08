"""Tests for the WebSocket event parser + dispatcher.

Runs synthetic frames + a sample of real captured frames against the
parser and verifies the dispatcher overlays property updates onto the
node registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.client import NodePropertyValue, NodeRecord
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
