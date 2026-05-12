"""Tests for the HA platform classifier against real captured nodedefs."""

from __future__ import annotations

from functools import partial

import pytest

from pyisyox.classifier import (
    ClassificationResult,
    ControllablePlatform,
    ReadingPlatform,
    classify,
)
from pyisyox.schema import Editor, Profile
from pyisyox.schema.cmd import Command, CommandParameter
from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty


def _resolver(profile: Profile, family: str, instance: str):
    """Editor resolver scoped to one family/instance, suitable for classify()."""
    return partial(profile.find_editor, family_id=family, instance_id=instance)


def _result_for(profile: Profile, nodedef_id: str, family: str, instance: str) -> ClassificationResult:
    nd = profile.find_nodedef(nodedef_id, family, instance)
    assert nd is not None, f"missing nodedef {nodedef_id} {family}/{instance}"
    return classify(nd, find_editor=_resolver(profile, family, instance))


def test_flume2_is_pure_sensor_device(profile: Profile) -> None:
    """flume2 accepts only QUERY, has no sends, has 7 gallon properties + 1 bool."""
    res = _result_for(profile, "flume2", "10", "10")
    assert res.controllable is None
    assert res.controllable_command_ids == frozenset()
    assert res.triggers == []
    assert res.buttons == []
    assert len(res.readings) == 9

    by_id = {r.property.id: r for r in res.readings}
    assert by_id["GV1"].platform is ReadingPlatform.SENSOR  # gallons
    assert by_id["GV1"].is_enum is False
    assert by_id["GV8"].platform is ReadingPlatform.BINARY_SENSOR  # bool — Leak Detected
    assert by_id["ST"].platform is ReadingPlatform.BINARY_SENSOR  # bool — Node Status


def test_flume_controller_buttons_plus_enum_sensors(profile: Profile) -> None:
    """controller accepts QUERY/DISCOVER/SETFAILED only (not DON/DOF); sends DON/DOF.
    Result: no controllable (it's not user-controllable), 2 buttons, 2 enum sensors,
    DON/DOF as triggers."""
    res = _result_for(profile, "controller", "10", "10")
    assert res.controllable is None

    button_ids = {c.id for c in res.buttons}
    assert button_ids == {"DISCOVER", "SETFAILED"}, "plugin verbs become buttons; QUERY excluded"

    trigger_ids = {c.id for c in res.triggers}
    assert trigger_ids == {"DON", "DOF"}, "sends always surface as triggers"

    # No controllable -> ST stays as a reading (enum sensor via the cst editor).
    by_id = {r.property.id: r for r in res.readings}
    assert {"ST", "GV1"} <= set(by_id)
    assert by_id["GV1"].platform is ReadingPlatform.SENSOR
    assert by_id["GV1"].is_enum is True


def test_flume1_hub_is_single_binary_sensor(profile: Profile) -> None:
    """flume1 has one bool property, no commands."""
    res = _result_for(profile, "flume1", "10", "10")
    assert res.controllable is None
    assert res.triggers == []
    assert res.buttons == []
    assert len(res.readings) == 1
    assert res.readings[0].property.id == "ST"
    assert res.readings[0].platform is ReadingPlatform.BINARY_SENSOR


def test_insteon_thermostat_is_climate(profile: Profile) -> None:
    """Thermostat accepts CLISPH/CLISPC/CLIMD/CLIFS plus BEEP/SETTIME/WDU.
    BRT/DIM are setpoint-up/down (folded into climate), not light dimming.
    """
    res = _result_for(profile, "Thermostat", "1", "1")
    assert res.controllable is ControllablePlatform.CLIMATE
    assert {"CLISPC", "CLISPH", "CLIMD", "CLIFS"} <= res.controllable_command_ids
    assert {"BRT", "DIM"} <= res.controllable_command_ids, "thermostat BRT/DIM = setpoint nudge"

    # BEEP carries one *optional* level param, so it's still pressable with
    # zero args → stays a button alongside the parameterless SETTIME/WDU.
    button_ids = {c.id for c in res.buttons}
    assert button_ids == {"BEEP", "SETTIME", "WDU"}, "zero-arg thermostat verbs become buttons"
    assert res.parameterized_commands == [], "no thermostat accept has a required param"


def test_insteon_dimmer_is_light_with_filtered_state(profile: Profile) -> None:
    """A nodedef with DON/DOF + OL property classifies as light;
    ST/OL/RR are filtered from readings."""
    nd = profile.find_nodedef("KeypadDimmer_ADV", "1", "1")
    assert nd is not None
    res = classify(nd, find_editor=_resolver(profile, "1", "1"))
    assert res.controllable is ControllablePlatform.LIGHT

    reading_ids = {r.property.id for r in res.readings}
    assert "ST" not in reading_ids
    assert "OL" not in reading_ids
    assert "RR" not in reading_ids


def test_insteon_oncontrol_is_pure_trigger_source(profile: Profile) -> None:
    """OnOffControl accepts nothing, sends DON/DOF — a paddle/remote, not a controllable."""
    res = _result_for(profile, "OnOffControl", "1", "1")
    assert res.controllable is None
    assert res.buttons == []
    trigger_ids = {c.id for c in res.triggers}
    assert trigger_ids == {"DON", "DOF"}
    # OnOffControl carries ST + ERR properties — both surface as readings since no controllable absorbs them.
    reading_ids = {r.property.id for r in res.readings}
    assert "ST" in reading_ids


def test_classify_works_without_editor_resolver(profile: Profile) -> None:
    """Without an editor resolver, every reading defaults to sensor.is_enum=False —
    callers can still render entities, just without device-class hints."""
    nd = profile.find_nodedef("flume2", "10", "10")
    assert nd is not None
    res = classify(nd)  # no resolver
    assert all(r.platform is ReadingPlatform.SENSOR for r in res.readings)
    assert all(r.is_enum is False for r in res.readings)


def test_query_is_never_a_button() -> None:
    """QUERY is implicit on every node — never surfaces as a HA button."""

    nd = NodeDef(
        id="bare",
        family_id="99",
        instance_id="1",
        cmds=NodeCommands(accepts=[Command(id="QUERY", name="Query")]),
    )
    res = classify(nd)
    assert res.buttons == []
    assert res.controllable is None


def test_plugin_verb_with_no_controllable_still_becomes_button() -> None:
    """A plugin nodedef that accepts only RESET (a non-standard verb) classifies as
    no-controllable + 1 button."""

    nd = NodeDef(
        id="reset_only",
        family_id="20",
        instance_id="3",
        cmds=NodeCommands(accepts=[Command(id="RESET", name="Reset"), Command(id="QUERY", name="Query")]),
    )
    res = classify(nd)
    assert res.controllable is None
    assert [c.id for c in res.buttons] == ["RESET"]
    assert res.parameterized_commands == []


def test_zero_arg_vs_required_param_accept_split() -> None:
    """Accept commands split on whether they're sendable with zero args.
    Parameterless verbs and ones whose parameters are *all* ``optional``
    (controller applies defaults — BEEP-style) become plain ``buttons``;
    a command with at least one *required* parameter lands in
    ``parameterized_commands`` (its editor would drive an input entity —
    not in scope for a button)."""

    nd = NodeDef(
        id="mixed_cmds",
        family_id="20",
        instance_id="5",
        cmds=NodeCommands(
            accepts=[
                Command(id="RESET", name="Reset"),  # parameterless
                Command(
                    id="BEEP",
                    name="Beep",
                    parameters=[CommandParameter(editor_id="I_BEEP_255", optional=True)],
                ),  # all-optional → still a button
                Command(
                    id="SET_LEVEL",
                    name="Set Level",
                    parameters=[CommandParameter(editor_id="I_PCT")],  # required
                ),
                Command(id="QUERY", name="Query"),
            ]
        ),
    )
    res = classify(nd)
    assert res.controllable is None
    assert {c.id for c in res.buttons} == {"RESET", "BEEP"}
    assert [c.id for c in res.parameterized_commands] == ["SET_LEVEL"]


def test_button_node_with_send_and_accept_overlap() -> None:
    """Synthetic case: a node that both accepts DON/DOF (LED control) and sends them
    (physical press). Should classify as switch (controllable) AND emit triggers."""

    nd = NodeDef(
        id="kpl_button",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(
            accepts=[Command(id="DON"), Command(id="DOF")],
            sends=[Command(id="DON"), Command(id="DOF")],
        ),
    )
    res = classify(nd)
    assert res.controllable is ControllablePlatform.SWITCH  # no OL property
    trigger_ids = {c.id for c in res.triggers}
    assert trigger_ids == {"DON", "DOF"}, "sends always surface as triggers, even when also accepted"


def test_resolver_returns_none_gracefully() -> None:
    """If the editor resolver can't find an editor, reading classification falls
    back to plain sensor instead of crashing."""

    nd = NodeDef(
        id="x",
        family_id="99",
        instance_id="1",
        properties={"P1": NodeProperty(id="P1", editor_id="MISSING", name="Phantom")},
        cmds=NodeCommands(),
    )

    def bad_resolver(_id: str) -> Editor | None:
        return None

    res = classify(nd, find_editor=bad_resolver)
    assert len(res.readings) == 1
    assert res.readings[0].platform is ReadingPlatform.SENSOR


@pytest.mark.parametrize(
    ("nodedef_id", "family", "instance", "expected"),
    [
        ("flume2", "10", "10", None),
        ("flume1", "10", "10", None),
        ("controller", "10", "10", None),
        ("Thermostat", "1", "1", ControllablePlatform.CLIMATE),
        ("OnOffControl", "1", "1", None),
        ("KeypadDimmer_ADV", "1", "1", ControllablePlatform.LIGHT),
    ],
)
def test_controllable_assignment_matrix(
    profile: Profile,
    nodedef_id: str,
    family: str,
    instance: str,
    expected: ControllablePlatform | None,
) -> None:
    res = _result_for(profile, nodedef_id, family, instance)
    assert res.controllable is expected
