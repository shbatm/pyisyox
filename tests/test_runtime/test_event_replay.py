"""Replay 41 anonymized WS frames captured against a real eisy and
assert each one parses + dispatches as expected.

Originated as PyISY 3.6.1's ``websocket_events.log`` fixture, which
covered a wider variety of control codes (``_0``/``_3``/``_4``/``_5``/
``_7``/``_22``/``_25``/``_26``/``_28`` plus ``ST``/``OL``/``RR``/
``ERR``) than our existing ``ws-frames.jsonl`` capture. The breadth
exposes parse-paths that synthetic frames don't exercise — empty
``<eventInfo>``, ``<action>`` with ``uom``+``prec`` attrs, system
events that have no ``<node>`` element, and so on."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from pyisyox.runtime.events import (
    EventDispatcher,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    parse_event_frame,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "eisy6" / "ws-frames-pyisy.log"


@pytest.fixture(scope="module")
def captured_frames() -> list[str]:
    """One ``<?xml...?><Event>...</Event>`` string per non-empty line."""
    return [line for line in FIXTURE.read_text().splitlines() if line.strip()]


def _by_control(events: list, code: str):
    return [e for e in events if e is not None and e.control == code]


# --- frame coverage -------------------------------------------------------


def test_capture_has_expected_control_variety(captured_frames: list[str]) -> None:
    """Pin the breadth of the imported capture so that, if someone
    re-anonymizes from a fresh log, they don't accidentally drop a
    control code our replay was relying on."""
    found = set(re.findall(r"<control>([^<]+)</control>", "\n".join(captured_frames)))
    expected = {"_0", "_1", "_3", "_4", "_5", "_7", "_22", "_25", "_26", "_28", "ERR", "OL", "RR", "ST"}
    assert expected <= found


def test_every_frame_parses(captured_frames: list[str]) -> None:
    """Real-controller frames must round-trip. Anything that returns
    ``None`` indicates a parser regression."""
    parsed = [parse_event_frame(f) for f in captured_frames]
    failures = [
        (i, raw[:120]) for i, (raw, ev) in enumerate(zip(captured_frames, parsed, strict=True)) if ev is None
    ]
    assert failures == [], f"{len(failures)} frame(s) failed to parse: {failures[:3]}"


# --- shape per control code ----------------------------------------------


def test_property_frames_have_node_address(captured_frames: list[str]) -> None:
    """``ST``/``OL``/``RR``/``ERR`` are property updates — they must
    carry a non-empty ``node_address`` and must classify as
    ``is_node_property``."""
    parsed = [parse_event_frame(f) for f in captured_frames]
    prop_codes = {"ST", "OL", "RR", "ERR"}
    for ev in parsed:
        if ev and ev.control in prop_codes:
            assert ev.node_address, f"{ev.control} frame has empty node_address: {ev}"
            assert ev.is_node_property is True
            assert ev.is_system is False


def test_system_frames_classify_correctly(captured_frames: list[str]) -> None:
    """Codes starting with ``_`` are system events; they must report
    ``is_system=True`` regardless of whether they have a node addr."""
    parsed = [parse_event_frame(f) for f in captured_frames]
    for ev in parsed:
        if ev and ev.control.startswith("_"):
            assert ev.is_system is True
            assert ev.is_node_property is False


def test_action_uom_and_prec_decoded(captured_frames: list[str]) -> None:
    """The capture has at least one ``<action uom="100" prec="0">``
    payload (an OL update). Confirm the parser extracts both attrs."""
    parsed = [parse_event_frame(f) for f in captured_frames]
    with_uom = [e for e in parsed if e and e.uom]
    assert with_uom, "expected at least one frame with a uom attribute"
    with_prec = [e for e in parsed if e and e.precision is not None]
    assert with_prec, "expected at least one frame with a prec attribute"


# --- dispatcher fan-out ---------------------------------------------------


def test_dispatcher_routes_every_parsed_event(captured_frames: list[str]) -> None:
    """All parsed events flow through the general listener; the
    counts should match exactly."""
    dispatcher = EventDispatcher(nodes={})
    received: list = []
    dispatcher.add_listener(received.append)
    parsed_count = 0
    for raw in captured_frames:
        if dispatcher.feed(raw) is not None:
            parsed_count += 1
    assert len(received) == parsed_count


def test_lifecycle_listener_only_sees_underscore_three(captured_frames: list[str]) -> None:
    """The dual-channel design means the lifecycle listener should
    *only* receive ``_3`` frames — never ``_5`` system status, never
    ``ST`` property updates."""
    dispatcher = EventDispatcher(nodes={})
    lifecycle: list[NodeLifecycleEvent] = []
    general: list = []
    dispatcher.add_listener(general.append)
    dispatcher.add_lifecycle_listener(lifecycle.append)
    for raw in captured_frames:
        dispatcher.feed(raw)

    underscore_three_count = len(_by_control(general, "_3"))
    assert len(lifecycle) == underscore_three_count
    # action is typed as ``NodeLifecycleAction | str`` — the dispatcher
    # falls back to a raw string for verbs not yet in the enum, and the
    # raw_action attr always carries the wire value verbatim.
    for lc in lifecycle:
        assert lc.raw_action  # always populated
        assert isinstance(lc.action, (NodeLifecycleAction, str))


def test_every_observed_lifecycle_action_is_in_enum(captured_frames: list[str]) -> None:
    """Every ``_3`` action code seen in real captures must resolve to a
    :class:`NodeLifecycleAction` member, not a fallback string. The
    enum is the source-of-truth for UDI's documented codes plus the
    PG3 codes (``WD``, ``CE``) we've observed in capture."""
    dispatcher = EventDispatcher(nodes={})
    lifecycle: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(lifecycle.append)
    for raw in captured_frames:
        dispatcher.feed(raw)
    for lc in lifecycle:
        assert isinstance(lc.action, NodeLifecycleAction), (
            f"observed lifecycle action {lc.raw_action!r} has no enum entry"
        )


# --- raw control-code distribution (regression guard) -------------------


def test_parsed_control_distribution(captured_frames: list[str]) -> None:
    """If the fixture is regenerated and a control disappears, the
    overall distribution shifts — this test pins a minimum so the
    coverage assertion above stays meaningful."""
    parsed = [e for e in (parse_event_frame(f) for f in captured_frames) if e is not None]
    counts = Counter(e.control for e in parsed)
    # The capture has multiple ST and OL events (live property updates),
    # so they should dominate. System codes appear once each.
    assert counts["ST"] >= 1
    assert counts["OL"] >= 1
