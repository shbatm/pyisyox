"""Tests for NodeLifecycleEvent + dispatcher emission."""

from __future__ import annotations

import pytest

from pyisyox.client import NodeRecord
from pyisyox.runtime.events import (
    DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS,
    NODE_LIFECYCLE_EVENT_INFO_TAGS,
    EventDispatcher,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    describe_system_event,
)


@pytest.mark.parametrize(
    ("control", "action", "expected"),
    [
        ("_5", "0", "system_status = not_busy"),
        ("_5", "1", "system_status = busy"),
        ("_5", "2", "system_status = idle"),
        ("_5", "9", "system_status = 9"),  # unknown status value passes through
        ("_1", "0", "trigger = program_status"),
        ("_1", "1", "trigger = get_status"),
        ("_1", "6", "trigger = variable_value"),
        ("_1", "7", "trigger = variable_init"),
        ("_1", "8", "trigger = key"),
        ("_1", "99", "trigger = 99"),  # unknown trigger action passes through
        ("_3", "WH", "node_lifecycle = pending_device_op"),
        ("_3", "WD", "node_lifecycle = programming_device"),
        ("_3", "CE", "node_lifecycle = node_error_cleared"),
        ("_3", "PI", "node_lifecycle = power_info_changed"),
        ("_3", "ZZ", "node_lifecycle = ZZ"),  # unknown verb passes through
        ("_4", "5", "system_config = batch_mode_updated"),
        ("_6", "1", "internet_access = enabled"),
        ("_7", "2.1", "progress = device_adder_info"),
        ("_8", "AW", "security_system = armed_away"),
        ("_20", "2", "device_linker = cleared"),
        ("_0", "90", "heartbeat = 90"),  # action = seconds; not enumerated
        ("_23", "1", "portal = 1"),  # control known, no action enum
        ("_28", "1.3", "matter_status = 1.3"),  # no enum for matter status
        ("_26", "2", "_26 = 2"),  # unknown control — both halves verbatim
    ],
)
def test_describe_system_event(control: str, action: str, expected: str) -> None:
    assert describe_system_event(control, action) == expected


def test_lifecycle_event_info_tags_cover_every_verb() -> None:
    """Every NodeLifecycleAction member has an eventInfo-tag entry, and
    the table doesn't reference verbs that aren't in the enum."""
    assert set(NODE_LIFECYCLE_EVENT_INFO_TAGS) == set(NodeLifecycleAction)
    # Spot-check a few documented payloads.
    assert NODE_LIFECYCLE_EVENT_INFO_TAGS[NodeLifecycleAction.NODE_RENAMED] == ("newName",)
    assert NODE_LIFECYCLE_EVENT_INFO_TAGS[NodeLifecycleAction.GROUP_ADDED] == ("groupName", "groupType")
    assert NODE_LIFECYCLE_EVENT_INFO_TAGS[NodeLifecycleAction.POWER_INFO_CHANGED] == (
        "deviceClass",
        "wattage",
        "dcPeriod",
    )
    assert NODE_LIFECYCLE_EVENT_INFO_TAGS[NodeLifecycleAction.PROGRAMMING_DEVICE] == ()


def test_device_write_progress_codes_are_underscore_prefixed() -> None:
    """The _7-frame sub-codes are documented but kept out of
    NodeLifecycleAction (they arrive on PROGRESS frames, not _3)."""
    assert set(DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS) == {"_7A", "_7M"}
    for code in DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS:
        assert code.startswith("_")
        assert code not in set(NodeLifecycleAction)


def _node_added_frame() -> str:
    """Captured shape: <control>_3</control><action>ND</action>
    <node>n009_harmonyctrl</node><eventInfo><node ...>...</node></eventInfo>"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Event seqnum="42" sid="uuid:1" '
        'timestamp="2026-05-08T08:06:34.961736-07:00">'
        "<control>_3</control><action>ND</action>"
        "<node>n009_harmonyctrl</node>"
        '<eventInfo><node flag="128" nodeDefId="HarmonyController">'
        "<address>n009_harmonyctrl</address>"
        "<name>HarmonyHub Controller</name>"
        '<family instance="9">10</family>'
        "<hint>0.0.0.0</hint>"
        "<type>1.1.0.0</type>"
        "<enabled>true</enabled>"
        "<pnode>n009_harmonyctrl</pnode>"
        "</node></eventInfo></Event>"
    )


def _node_removed_frame() -> str:
    return (
        '<Event seqnum="43"><control>_3</control><action>NR</action>'
        "<node>n009_harmonyctrl</node><eventInfo></eventInfo></Event>"
    )


def _property_event_frame() -> str:
    """Plain property frame to verify lifecycle listeners ignore non-_3 events."""
    return '<Event seqnum="100"><control>ST</control><action>1</action><node>3D 7D 87 1</node></Event>'


# --- detection ----------------------------------------------------------


def test_lifecycle_listener_fires_on_node_add() -> None:
    nodes: dict[str, NodeRecord] = {}
    dispatcher = EventDispatcher(nodes)
    received: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(received.append)

    dispatcher.feed(_node_added_frame())

    assert len(received) == 1
    ev = received[0]
    assert ev.action is NodeLifecycleAction.NODE_ADDED
    assert ev.node_address == "n009_harmonyctrl"
    assert ev.requires_reload is True
    assert ev.node_xml is not None
    assert "HarmonyController" in ev.node_xml


def test_lifecycle_listener_unwraps_action_to_typed_enum() -> None:
    nodes: dict[str, NodeRecord] = {}
    dispatcher = EventDispatcher(nodes)
    received: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(received.append)

    dispatcher.feed(_node_removed_frame())

    assert received[0].action is NodeLifecycleAction.NODE_REMOVED
    assert received[0].requires_reload is True
    assert received[0].node_xml is None  # no <eventInfo><node> on removes


def test_lifecycle_listener_passes_through_unknown_actions_as_strings() -> None:
    """A future control-3 verb that isn't in NodeLifecycleAction comes
    through with action=raw_string so consumers can still react."""
    frame = '<Event seqnum="1"><control>_3</control><action>XQ</action><node>X</node></Event>'
    dispatcher = EventDispatcher({})
    received: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(received.append)

    dispatcher.feed(frame)
    assert received[0].action == "XQ"
    assert received[0].raw_action == "XQ"
    assert received[0].requires_reload is False


def test_lifecycle_listener_ignores_property_events() -> None:
    dispatcher = EventDispatcher({"3D 7D 87 1": _stub_record("3D 7D 87 1")})
    received: list[NodeLifecycleEvent] = []
    dispatcher.add_lifecycle_listener(received.append)
    dispatcher.feed(_property_event_frame())
    assert received == []


def test_lifecycle_unsubscribe_stops_delivery() -> None:
    dispatcher = EventDispatcher({})
    received: list[NodeLifecycleEvent] = []
    unsubscribe = dispatcher.add_lifecycle_listener(received.append)
    unsubscribe()
    dispatcher.feed(_node_added_frame())
    assert received == []


def test_general_event_listener_still_fires_on_lifecycle_frames() -> None:
    """Lifecycle frames go through both the general and lifecycle channels."""
    dispatcher = EventDispatcher({})
    general: list = []
    lifecycle: list[NodeLifecycleEvent] = []
    dispatcher.add_listener(general.append)
    dispatcher.add_lifecycle_listener(lifecycle.append)

    dispatcher.feed(_node_added_frame())

    assert len(general) == 1
    assert len(lifecycle) == 1
    assert general[0].control == "_3"


def test_lifecycle_requires_reload_taxonomy() -> None:
    """Reload-worthy verbs invalidate the cached node registry; soft
    signals are informational. Action codes are pinned to UDI's
    canonical wire codes — see the ``NodeLifecycleAction`` docstring
    for the source-of-truth mapping."""
    reload_actions = {
        NodeLifecycleAction.NODE_ADDED,  # ND
        NodeLifecycleAction.NODE_REMOVED,  # NR
        NodeLifecycleAction.NODE_RENAMED,  # NN — not RG
        NodeLifecycleAction.NODE_REMOVED_FROM_GROUP,  # RG (scene-edit)
        NodeLifecycleAction.NODE_ENABLED,  # EN — covers both directions
        NodeLifecycleAction.NODE_REVISED,  # RV
        NodeLifecycleAction.NODE_DISCOVERY_COMPLETE,  # SC — new nodes may have appeared
        NodeLifecycleAction.FOLDER_ADDED,  # FD
        NodeLifecycleAction.FOLDER_REMOVED,  # FR
        NodeLifecycleAction.FOLDER_RENAMED,  # FN
        NodeLifecycleAction.GROUP_ADDED,  # GD
        NodeLifecycleAction.GROUP_REMOVED,  # GR
        NodeLifecycleAction.GROUP_RENAMED,  # GN
    }
    soft_actions = {
        NodeLifecycleAction.NODE_MOVED,  # MV (added to scene)
        NodeLifecycleAction.LINK_CHANGED,  # CL — not supported
        NodeLifecycleAction.PARENT_CHANGED,  # PC
        NodeLifecycleAction.POWER_INFO_CHANGED,  # PI
        NodeLifecycleAction.DEVICE_ID_CHANGED,  # DI — not implemented
        NodeLifecycleAction.DEVICE_PROPERTY_CHANGED,  # DP — UPB only
        NodeLifecycleAction.PENDING_DEVICE_OP,  # WH
        NodeLifecycleAction.PROGRAMMING_DEVICE,  # WD — a property event follows
        NodeLifecycleAction.DISCOVERING_NODES,  # SN — wait for SC
        NodeLifecycleAction.NODE_ERROR_CLEARED,  # CE — comm error cleared
        NodeLifecycleAction.NODE_ERROR,  # NE — comm error, no shape change
        NodeLifecycleAction.NET_RENAMED,  # WR — networking resource, not nodes
    }
    # The two sets together must cover every verb — keeps this test
    # honest when new verbs are added.
    assert reload_actions | soft_actions == set(NodeLifecycleAction)
    for act in reload_actions:
        ev = NodeLifecycleEvent(action=act, node_address="X", raw_action=act, seqnum=0)
        assert ev.requires_reload is True, f"{act} should be a reload-worthy signal"
    for act in soft_actions:
        ev = NodeLifecycleEvent(action=act, node_address="X", raw_action=act, seqnum=0)
        assert ev.requires_reload is False, f"{act} should be a soft signal"


def _stub_record(addr: str) -> NodeRecord:
    return NodeRecord(
        address=addr,
        name="t",
        nodedef_id="x",
        family_id="1",
        instance_id="1",
    )
