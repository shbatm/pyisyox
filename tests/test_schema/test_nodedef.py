"""Nodedef shape tests covering native and plugin nodes uniformly."""

from __future__ import annotations

from pyisyox.schema import Profile


def test_flume2_properties_and_commands(profile: Profile) -> None:
    nd = profile.find_nodedef("flume2", "10", "10")
    assert nd is not None
    assert set(nd.properties) == {"ST", "GV1", "GV2", "GV3", "GV4", "GV5", "GV6", "GV7", "GV8"}
    assert nd.properties["GV1"].editor_id == "GALLONS"
    assert nd.properties["GV1"].name == "Current"
    assert nd.properties["GV8"].editor_id == "bool"
    assert nd.properties["GV8"].name == "Leak Detected"

    assert [c.id for c in nd.cmds.sends] == []
    assert [c.id for c in nd.cmds.accepts] == ["QUERY"]
    assert nd.nls_key == "flume2"


def test_flume_controller_has_plugin_buttons(profile: Profile) -> None:
    nd = profile.find_nodedef("controller", "10", "10")
    assert nd is not None
    accept_ids = [c.id for c in nd.cmds.accepts]
    send_ids = [c.id for c in nd.cmds.sends]
    assert "DON" in send_ids and "DOF" in send_ids
    assert {"QUERY", "DISCOVER", "SETFAILED"} <= set(accept_ids)


def test_thermostat_command_parameters_reference_editors(profile: Profile) -> None:
    nd = profile.find_nodedef("Thermostat", "1", "1")
    assert nd is not None
    by_id = {c.id: c for c in nd.cmds.accepts}
    clispc = by_id["CLISPC"]
    assert clispc.parameters
    assert clispc.parameters[0].editor_id == "I_CLISPC_DEG"
    climd = by_id["CLIMD"]
    assert climd.parameters[0].editor_id == "I_TSTAT_MODE"


def test_nodedef_uniform_shape_native_vs_plugin(profile: Profile) -> None:
    """Native and plugin nodedefs share the same dataclass — no plugin-only fields."""
    native = profile.find_nodedef("KeypadDimmer_ADV", "1", "1")
    plugin = profile.find_nodedef("flume2", "10", "10")
    assert native is not None
    assert plugin is not None
    assert type(native) is type(plugin)
    for field_name in ("id", "family_id", "instance_id", "properties", "cmds", "links"):
        assert hasattr(native, field_name)
        assert hasattr(plugin, field_name)
