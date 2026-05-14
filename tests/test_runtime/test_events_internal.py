"""Coverage for the ``runtime/events`` private helpers + defensive
branches in the dispatcher.

The headline tests in :mod:`tests.test_runtime.test_events` cover the
happy paths through the dispatcher. This file fills the gaps: the
``label()`` classmethods on each system-event StrEnum, the rendering
helpers (``_scalar`` / ``_xml_to_obj`` / ``_compact_event_info`` /
``_log_system_event``), the WS-frame parsing fallbacks, and the
defensive returns inside ``_apply_variable_table_change`` /
``_emit_lifecycle``.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from pyisyox.runtime.events import (
    DeviceLinkerAction,
    DeviceWriteAction,
    EventDispatcher,
    InternetAccessStatus,
    NodeLifecycleAction,
    ProgressAction,
    SecuritySystemAction,
    SystemConfigAction,
    SystemEventControl,
    TriggerAction,
    VariableTableChangeEvent,
    _compact_event_info,
    _escape_stray_ampersands,
    _log_system_event,
    _maybe_unwrap_json_envelope,
    _parse_lifecycle_enabled,
    _scalar,
    _xml_to_obj,
)

# ---------------------------------------------------------------------------
# StrEnum .label() classmethods.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_cls", "known_value"),
    [
        (TriggerAction, TriggerAction.VARIABLE_TABLE_CHANGED),
        (ProgressAction, ProgressAction.UPDATE),
        (SystemConfigAction, SystemConfigAction.TIME_CHANGED),
        (InternetAccessStatus, InternetAccessStatus.ENABLED),
        (SecuritySystemAction, SecuritySystemAction.DISARMED),
        (DeviceLinkerAction, DeviceLinkerAction.STATUS),
        (NodeLifecycleAction, NodeLifecycleAction.NODE_ENABLED),
    ],
)
def test_enum_label_known_returns_friendly_name(enum_cls: type, known_value: str) -> None:
    """A known wire-code resolves to its lower-cased canonical enum-
    member name (``"variable_table_changed"`` etc.). The exact label
    text is the responsibility of ``_enum_label`` — here we just
    require a non-empty string that isn't the raw wire code itself."""
    label = enum_cls.label(known_value)
    assert isinstance(label, str)
    assert label
    assert label != known_value


@pytest.mark.parametrize(
    "enum_cls",
    [
        TriggerAction,
        ProgressAction,
        SystemConfigAction,
        InternetAccessStatus,
        SecuritySystemAction,
        DeviceLinkerAction,
        NodeLifecycleAction,
    ],
)
def test_enum_label_unknown_passes_through_raw_value(enum_cls: type) -> None:
    assert enum_cls.label("__bogus__") == "__bogus__"


def test_device_write_action_label_round_trips_unknown() -> None:
    assert DeviceWriteAction.label("__bogus__") == "__bogus__"


# ---------------------------------------------------------------------------
# _scalar — text-leaf coercion for log rendering.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("false", False),
        ("False", False),
        ("hello", "hello"),
        # No int-guessing: zero-padded numeric strings stay strings.
        ("007", "007"),
        ("", ""),
    ],
)
def test_scalar_coerces_booleans_only(text: str, expected: object) -> None:
    assert _scalar(text) == expected


# ---------------------------------------------------------------------------
# _xml_to_obj — recursive XML element → JSON-friendly object.
# ---------------------------------------------------------------------------


def test_xml_to_obj_text_leaf_bool() -> None:
    el = ET.fromstring("<v>true</v>")  # noqa: S314 — test fixture
    assert _xml_to_obj(el) is True


def test_xml_to_obj_text_leaf_string() -> None:
    el = ET.fromstring("<v>hello</v>")  # noqa: S314 — test fixture
    assert _xml_to_obj(el) == "hello"


def test_xml_to_obj_self_closing_is_presence_flag() -> None:
    """``<on/>`` and similar bare presence markers come back as ``True``."""
    el = ET.fromstring("<on/>")  # noqa: S314 — test fixture
    assert _xml_to_obj(el) is True


def test_xml_to_obj_attributes_and_children_share_keyspace() -> None:
    el = ET.fromstring('<root a="1"><child>v</child></root>')  # noqa: S314 — test fixture
    assert _xml_to_obj(el) == {"a": "1", "child": "v"}


def test_xml_to_obj_text_alongside_children_lands_under_text_key() -> None:
    el = ET.fromstring("<root>hi<child>v</child></root>")  # noqa: S314 — test fixture
    assert _xml_to_obj(el) == {"#text": "hi", "child": "v"}


# ---------------------------------------------------------------------------
# _compact_event_info — render <eventInfo> contents for log lines.
# ---------------------------------------------------------------------------


def test_compact_event_info_returns_none_for_empty() -> None:
    assert _compact_event_info("") is None


def test_compact_event_info_renders_xml_as_json_blob() -> None:
    rendered = _compact_event_info("<loglevel>0</loglevel><connected>true</connected>")
    assert rendered is not None
    parsed = json.loads(rendered)
    assert parsed == {"loglevel": "0", "connected": True}


def test_compact_event_info_falls_back_to_collapsed_text_for_non_xml() -> None:
    """``_7`` controller logs carry CDATA / non-XML text. A payload
    that fails ``ET.fromstring`` after the ``<eventInfo>...`` wrap
    falls through to whitespace-collapsed text rendering."""
    # Stray ``<`` inside the payload makes the wrapped fragment
    # malformed — drives the ParseError path (lines 546-548).
    rendered = _compact_event_info("plain    <unclosed   text")
    assert rendered == "plain <unclosed text"


def test_compact_event_info_returns_none_for_whitespace_only() -> None:
    """Whitespace-only payload parses to an empty root with no
    children and no text — the trailing branch returns ``None`` so the
    caller can skip the log line entirely."""
    assert _compact_event_info("   ") is None


# ---------------------------------------------------------------------------
# _log_system_event — DEBUG line shape for unrouted system events.
# ---------------------------------------------------------------------------


class _Frame:
    """Minimal Event-shaped object with the four attrs the helper reads."""

    def __init__(
        self,
        *,
        control: str,
        action: str = "",
        node_address: str = "",
        event_info: str = "",
    ) -> None:
        self.control = control
        self.action = action
        self.node_address = node_address
        self.event_info = event_info


def test_log_system_event_heartbeat(caplog: pytest.LogCaptureFixture) -> None:
    """Heartbeat frames take a special-case path with the next-within
    delay; the generic system-event line is suppressed."""
    caplog.set_level(logging.DEBUG, logger="pyisyox.runtime.events")
    _log_system_event(_Frame(control=SystemEventControl.HEARTBEAT, action="60"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("ISY heartbeat" in m and "60s" in m for m in msgs)


def test_log_system_event_renders_node_and_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="pyisyox.runtime.events")
    _log_system_event(
        _Frame(
            control=SystemEventControl.SYSTEM_CONFIG,
            action=SystemConfigAction.BATCH_MODE_UPDATED,
            node_address="root",
            event_info="<status>1</status>",
        )
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("System event:" in m and "node=root" in m and "status" in m for m in msgs)


# ---------------------------------------------------------------------------
# _parse_lifecycle_enabled — defensive XML parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_info", "expected"),
    [
        ("<enabled>true</enabled>", True),
        ("<enabled>1</enabled>", True),
        ("<enabled>false</enabled>", False),
        ("<enabled>0</enabled>", False),
        ("<enabled>?</enabled>", None),
        ("<other>x</other>", None),
        ("", None),
        ("<unclosed", None),
    ],
)
def test_parse_lifecycle_enabled(event_info: str, expected: bool | None) -> None:
    assert _parse_lifecycle_enabled(event_info) is expected


# ---------------------------------------------------------------------------
# _maybe_unwrap_json_envelope — WS frame envelope handling.
# ---------------------------------------------------------------------------


def test_unwrap_envelope_returns_xml_passthrough() -> None:
    payload = "<Event><control>ST</control></Event>"
    assert _maybe_unwrap_json_envelope(payload) == payload


def test_unwrap_envelope_extracts_event_data() -> None:
    raw = json.dumps({"type": "event", "data": "<Event>x</Event>"})
    assert _maybe_unwrap_json_envelope(raw) == "<Event>x</Event>"


def test_unwrap_envelope_returns_none_for_subscription_envelope() -> None:
    """A non-event envelope (subscription / spolisy / unknown) is
    explicitly filtered out — this drives line 864's
    ``envelope.get("type") != "event"`` branch."""
    raw = json.dumps({"type": "subscription", "key": "xyz"})
    assert _maybe_unwrap_json_envelope(raw) is None


def test_unwrap_envelope_returns_none_for_non_dict_json() -> None:
    """A bare JSON value (list / number) shouldn't be treated as an
    envelope."""
    assert _maybe_unwrap_json_envelope("[1, 2, 3]") is None


def test_unwrap_envelope_returns_none_for_neither_xml_nor_json() -> None:
    assert _maybe_unwrap_json_envelope("just some text") is None


def test_unwrap_envelope_returns_none_for_invalid_json() -> None:
    assert _maybe_unwrap_json_envelope("{not: valid json}") is None


# ---------------------------------------------------------------------------
# WS frame XML parse failure recovery.
#
# ``feed`` first tries plain ``ET.fromstring``; if that fails it tries
# again with ampersands escaped (eisy sometimes embeds ``NUM=1&VAL=2``
# into eventInfo without escaping). Lines 746-748 are the path where
# even the repaired payload still fails to parse.
# ---------------------------------------------------------------------------


def test_dispatcher_drops_frame_when_payload_unfixable_no_ampersands() -> None:
    """Initial parse fails, ampersand-repair can't change anything (no
    stray ``&``), so the dispatcher drops via the ``else`` branch
    (lines 749-751)."""
    dispatcher = EventDispatcher({})
    assert dispatcher.feed("<Event><control>ST<unbalanced") is None


def test_dispatcher_drops_frame_when_repaired_payload_still_fails() -> None:
    """Initial parse fails AND the ampersand-repair pass changes the
    payload — but the repaired payload still doesn't parse. Drives
    the inner ParseError branch (lines 746-748)."""
    dispatcher = EventDispatcher({})
    # Stray ``&`` triggers the repair pass; the unbalanced tag keeps
    # the repaired payload from parsing too.
    assert dispatcher.feed("<Event>raw&value<unbalanced") is None


def test_escape_stray_ampersands_only_touches_unescaped() -> None:
    raw = "a&b but &amp; stays"
    assert _escape_stray_ampersands(raw) == "a&amp;b but &amp; stays"


# ---------------------------------------------------------------------------
# variable-table-change unsubscribe + defensive paths.
# ---------------------------------------------------------------------------


def test_variable_table_change_unsubscribe_idempotent() -> None:
    """Calling the unsubscribe twice (or removing a listener that was
    already removed manually) must not raise — exercises the
    ValueError suppression in the unsubscribe closure."""
    dispatcher = EventDispatcher({})
    listener: Any = lambda evt: None  # noqa: E731
    unsubscribe = dispatcher.add_variable_table_change_listener(listener)
    unsubscribe()
    unsubscribe()  # second call hits the ValueError path


def _trigger_frame(event_info: str = "") -> str:
    """Build a raw WS frame for control=_1 / action=9 (VARIABLE_TABLE_CHANGED)."""
    info_block = f"<eventInfo>{event_info}</eventInfo>" if event_info else ""
    return (
        "<?xml version='1.0'?>"
        "<Event>"
        f"<control>{SystemEventControl.TRIGGER}</control>"
        f"<action>{TriggerAction.VARIABLE_TABLE_CHANGED}</action>"
        "<node></node>"
        f"{info_block}"
        "</Event>"
    )


def test_variable_table_change_no_listener_logs_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When no listener is registered, the dispatcher logs at DEBUG and
    short-circuits — does not parse the payload."""
    caplog.set_level(logging.DEBUG, logger="pyisyox.runtime.events")
    dispatcher = EventDispatcher({})
    dispatcher.feed(_trigger_frame('<var type="1"/>'))
    assert any(
        "Variable table change (no listener)" in r.getMessage() for r in caplog.records
    )


def test_variable_table_change_empty_event_info_drops() -> None:
    """No payload → nothing to dispatch."""
    received: list[VariableTableChangeEvent] = []
    dispatcher = EventDispatcher({})
    dispatcher.add_variable_table_change_listener(received.append)
    dispatcher.feed(_trigger_frame(""))
    assert received == []


def test_variable_table_change_malformed_xml_drops() -> None:
    """Malformed eventInfo (unquoted attribute) is rejected by expat at
    the *outer* ``feed()`` parse — there's no ``&`` for the
    ampersand-repair pass to fix, so the dispatcher drops the frame
    before ``_apply_variable_table_change`` ever runs. End result is
    the same (no listener fires) regardless of which parse rejected
    it; this asserts the observable outcome."""
    received: list[VariableTableChangeEvent] = []
    dispatcher = EventDispatcher({})
    dispatcher.add_variable_table_change_listener(received.append)
    dispatcher.feed(_trigger_frame("<var type=1></var>"))
    assert received == []


def test_variable_table_change_missing_var_element_drops() -> None:
    received: list[VariableTableChangeEvent] = []
    dispatcher = EventDispatcher({})
    dispatcher.add_variable_table_change_listener(received.append)
    dispatcher.feed(_trigger_frame("<other/>"))
    assert received == []


def test_variable_table_change_missing_type_drops() -> None:
    received: list[VariableTableChangeEvent] = []
    dispatcher = EventDispatcher({})
    dispatcher.add_variable_table_change_listener(received.append)
    dispatcher.feed(_trigger_frame("<var/>"))
    assert received == []


def test_variable_table_change_listener_exception_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A listener that raises must be logged but mustn't break the
    dispatcher loop."""
    caplog.set_level(logging.ERROR, logger="pyisyox.runtime.events")

    def boom(evt: VariableTableChangeEvent) -> None:
        raise RuntimeError("boom")

    dispatcher = EventDispatcher({})
    dispatcher.add_variable_table_change_listener(boom)
    dispatcher.feed(_trigger_frame('<var type="1"/>'))
    assert any(
        "Variable table change listener raised" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Dispatcher debug-log paths in feed / _emit_lifecycle.
# ---------------------------------------------------------------------------


def test_unrouted_system_event_logs_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A system frame that doesn't match a routed handler hits
    ``_log_system_event`` when the logger is at DEBUG."""
    caplog.set_level(logging.DEBUG, logger="pyisyox.runtime.events")
    dispatcher = EventDispatcher({})
    raw = (
        "<?xml version='1.0'?>"
        "<Event>"
        f"<control>{SystemEventControl.SYSTEM_CONFIG}</control>"
        f"<action>{SystemConfigAction.TIME_CHANGED}</action>"
        "<node></node>"
        "</Event>"
    )
    dispatcher.feed(raw)
    assert any("System event:" in r.getMessage() for r in caplog.records)


def test_lifecycle_event_includes_compact_event_info_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_emit_lifecycle`` adds the rendered ``<eventInfo>`` blob to its
    DEBUG log line."""
    caplog.set_level(logging.DEBUG, logger="pyisyox.runtime.events")
    dispatcher = EventDispatcher({})
    raw = (
        "<?xml version='1.0'?>"
        "<Event>"
        f"<control>{SystemEventControl.NODE_LIFECYCLE}</control>"
        f"<action>{NodeLifecycleAction.NODE_ENABLED}</action>"
        "<node>1A 2B 3C 1</node>"
        "<eventInfo><enabled>true</enabled></eventInfo>"
        "</Event>"
    )
    dispatcher.feed(raw)
    assert any("Node lifecycle:" in r.getMessage() for r in caplog.records)
