"""Tests for the HA platform classifier against real captured nodedefs."""

from __future__ import annotations

from functools import partial

import pytest

from pyisyox.classifier import (
    AuxControl,
    AuxPlatform,
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

    # The light entity only claims DON/DOF. "Fast on/off" and the
    # momentary paddle verbs have no HA light equivalent, so they fall
    # through to buttons instead of being swallowed by the platform.
    assert res.controllable_command_ids == frozenset({"DON", "DOF"})
    button_ids = {c.id for c in res.buttons}
    assert {"DFON", "DFOF", "BRT", "DIM", "FDUP", "FDDOWN", "FDSTOP"} <= button_ids
    assert "DON" not in button_ids and "DOF" not in button_ids and "QUERY" not in button_ids

    reading_ids = {r.property.id for r in res.readings}
    assert "ST" not in reading_ids
    assert "OL" not in reading_ids
    assert "RR" not in reading_ids


def _virtualgeneric_nodedef(*, don_param: bool) -> NodeDef:
    """The Virtual node-server ``virtualgeneric`` shape (issue #64 /
    UniversalDevicesInc-PG3/Virtual#11): multilevel ``ST``/``OL``, the
    dimmer hint verbs ``BRT``/``DIM``, level set via ``SETST``/``SETOL``
    — and a ``DON`` that takes no level parameter. ``don_param`` flips
    only whether ``DON`` declares the on-level (the pivotal signal)."""
    don = Command(id="DON", name="On")
    if don_param:
        don = Command(
            id="DON",
            name="On",
            parameters=[CommandParameter(editor_id="value", init="ST", optional=True)],
        )
    return NodeDef(
        id="virtualgeneric",
        family_id="10",
        instance_id="4",
        properties={
            "ST": NodeProperty(id="ST", editor_id="value"),
            "OL": NodeProperty(id="OL", editor_id="value"),
        },
        cmds=NodeCommands(
            accepts=[
                don,
                Command(id="DOF", name="Off"),
                Command(id="BRT", name="Brighten"),
                Command(id="DIM", name="Dim"),
                Command(
                    id="SETST",
                    name="Set Status",
                    parameters=[CommandParameter(editor_id="value", init="ST")],
                ),
                Command(
                    id="SETOL",
                    name="Set On Level",
                    parameters=[CommandParameter(editor_id="value", init="OL")],
                ),
            ]
        ),
    )


def test_parameterless_don_classifies_as_switch_not_light() -> None:
    """``virtualgeneric`` has an ``OL`` property and ``BRT``/``DIM`` hints
    (both would say "dimmer") but a *parameterless* ``DON`` — HA drives
    brightness with ``DON <level>``, so it can't actually be dimmed via
    the light platform. It must degrade to SWITCH, with the level-set
    commands still surfaced for the consumer (issue #64 / Virtual#11)."""
    res = classify(_virtualgeneric_nodedef(don_param=False))
    assert res.controllable is ControllablePlatform.SWITCH
    assert res.controllable_command_ids == frozenset({"DON", "DOF"})
    param_ids = {c.id for c in res.parameterized_commands}
    assert {"SETST", "SETOL"} <= param_ids
    assert {"BRT", "DIM"} <= {c.id for c in res.buttons}


def test_parameterized_don_same_shape_stays_light() -> None:
    """Pivot test: the *only* difference from the SWITCH case above is
    that ``DON`` declares the on-level param — that alone makes it a
    real, HA-drivable dimmer → LIGHT."""
    res = classify(_virtualgeneric_nodedef(don_param=True))
    assert res.controllable is ControllablePlatform.LIGHT
    assert res.controllable_command_ids == frozenset({"DON", "DOF"})


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


# --- aux_controls (coalesced read/write controls, issue #160) -------------


def _aux_by_id(res: ClassificationResult) -> dict[str, AuxControl]:
    by_id: dict[str, AuxControl] = {}
    for a in res.aux_controls:
        assert a.id not in by_id, f"duplicate aux control id {a.id!r}"
        by_id[a.id] = a
    return by_id


def test_aux_controls_i3_flags_coalesce_via_init(profile: Profile) -> None:
    """The Insteon i3 ``I3PaddleFlags`` config sub-node: each ``GVx``
    write command coalesces with the status it is ``init``-synchronized
    with into ONE read/write control — no duplicate read-only sensor +
    writable switch pair (issue #160 / #67).

    Crucially ``GV0``'s param ``init="ST"`` (cmd-id != status-id): it
    must pair with the ``ST`` "Mode" status, proving the key is
    ``param.init``, not naive id matching.
    """
    res = _result_for(profile, "I3PaddleFlags", "1", "1")
    assert res.controllable is None  # no DON/DOF on the flags sub-node
    aux = _aux_by_id(res)

    mode = aux["ST"]  # keyed by the status id, not the command id
    assert mode.command is not None and mode.command.id == "GV0"
    assert mode.property is not None and mode.property.id == "ST"
    assert mode.readable and mode.writable
    assert mode.candidate_platform is AuxPlatform.SWITCH
    assert "GV0" not in aux  # not a separate write-only control

    for gv in ("GV1", "GV2", "GV3", "GV4", "GV5", "GV7"):
        c = aux[gv]
        assert c.readable and c.writable
        assert c.command is not None and c.command.id == gv
        assert c.property is not None and c.property.id == gv
        assert c.candidate_platform is AuxPlatform.SWITCH

    assert aux["WDU"].writable and not aux["WDU"].readable
    assert aux["WDU"].candidate_platform is AuxPlatform.BUTTON
    assert aux["ERR"].readable and not aux["ERR"].writable
    assert aux["ERR"].candidate_platform is AuxPlatform.SENSOR


def test_aux_controls_exclude_controllable_owned(profile: Profile) -> None:
    """A climate nodedef's setpoint/mode commands+status are owned by
    the controllable platform — they must not surface as aux controls."""
    res = _result_for(profile, "Thermostat", "1", "1")
    assert res.controllable is ControllablePlatform.CLIMATE
    ids = {a.id for a in res.aux_controls}
    assert ids.isdisjoint({"ST", "CLISPH", "CLISPC", "CLIMD", "CLIFS"})


def test_aux_controls_light_pairs_setters_with_props(profile: Profile) -> None:
    """On a light, ``ST`` is controllable-owned (no aux), but the
    ``OL``/``RR`` setters still pair with their properties → readable +
    writable aux controls (a paired writer reads its status back even
    when the property is controllable-filtered from standalone
    readings)."""
    res = _result_for(profile, "KeypadDimmer", "1", "1")
    assert res.controllable is ControllablePlatform.LIGHT
    aux = _aux_by_id(res)
    assert "ST" not in aux
    assert aux["OL"].readable and aux["OL"].writable
    assert aux["OL"].candidate_platform is AuxPlatform.NUMBER
    assert aux["RR"].readable and aux["RR"].writable
    assert aux["RR"].candidate_platform is AuxPlatform.SELECT
    # Backlight has no backing property → write-only.
    assert aux["BL"].writable and not aux["BL"].readable


def _ed(raw: dict) -> Editor:
    return Editor.from_json(raw)


def test_aux_control_candidate_platform_matrix() -> None:
    """Editor shape → candidate platform, for writable controls, plus
    a write-only (no ``init``) and a read-only (no writer) control."""
    editors = {
        "BOOLED": _ed(
            {"id": "BOOLED", "ranges": [{"uom": "2", "subset": "0,1", "names": {"0": "Off", "1": "On"}}]}
        ),
        "NUMED": _ed({"id": "NUMED", "ranges": [{"uom": "56", "min": 0, "max": 100}]}),
        "ENUMED": _ed({"id": "ENUMED", "ranges": [{"uom": "25", "names": {"0": "A", "1": "B"}}]}),
        "PCTED": _ed({"id": "PCTED", "ranges": [{"uom": "51", "min": 0, "max": 100}]}),
    }
    nd = NodeDef(
        id="synthetic",
        family_id="99",
        instance_id="1",
        properties={
            "GV0": NodeProperty(id="GV0", editor_id="BOOLED"),
            "GV1": NodeProperty(id="GV1", editor_id="NUMED"),
            "RO": NodeProperty(id="RO", editor_id="BOOLED"),
        },
        cmds=NodeCommands(
            accepts=[
                Command(
                    id="GV0", name="Toggle", parameters=[CommandParameter(editor_id="BOOLED", init="GV0")]
                ),
                Command(id="GV1", name="Level", parameters=[CommandParameter(editor_id="NUMED", init="GV1")]),
                Command(id="PICK", name="Pick", parameters=[CommandParameter(editor_id="ENUMED")]),
                Command(id="PCT", name="Pct", parameters=[CommandParameter(editor_id="PCTED")]),
                Command(id="GO", name="Go", parameters=[CommandParameter(editor_id="NUMED", optional=True)]),
            ]
        ),
    )
    res = classify(nd, find_editor=editors.get)
    aux = _aux_by_id(res)

    assert aux["GV0"].readable and aux["GV0"].writable
    assert aux["GV0"].candidate_platform is AuxPlatform.SWITCH
    assert aux["GV1"].candidate_platform is AuxPlatform.NUMBER
    # No init → write-only, keyed by command id.
    assert aux["PICK"].writable and not aux["PICK"].readable
    assert aux["PICK"].candidate_platform is AuxPlatform.SELECT
    assert aux["PCT"].candidate_platform is AuxPlatform.NUMBER
    # All-optional param → button-shaped.
    assert aux["GO"].candidate_platform is AuxPlatform.BUTTON
    assert aux["GO"].writable and not aux["GO"].readable
    # Property with no writer → read-only.
    assert aux["RO"].readable and not aux["RO"].writable
    assert aux["RO"].candidate_platform is AuxPlatform.BINARY_SENSOR


def test_aux_controls_is_additive_legacy_unchanged(profile: Profile) -> None:
    """``aux_controls`` is purely additive — the legacy
    readings/parameterized_commands/buttons split is still populated."""
    res = _result_for(profile, "KeypadDimmer", "1", "1")
    assert res.aux_controls  # new field populated
    assert res.readings  # legacy still present
    assert res.parameterized_commands or res.buttons
