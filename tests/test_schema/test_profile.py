"""Profile loader and lookup-table tests against the captured eisy fixture."""

from __future__ import annotations

from pyisyox.schema import Profile


def test_loads_all_families(profile: Profile) -> None:
    assert len(profile.families) == 13
    assert "common" in profile.families
    assert "1" in profile.families
    assert "10" in profile.families


def test_insteon_family_has_expected_volume(profile: Profile) -> None:
    insteon = profile.families["1"]
    assert "1" in insteon.instances
    inst = insteon.instances["1"]
    assert len(inst.nodedefs) == 64
    assert len(inst.editors) == 60
    assert len(inst.linkdefs) == 26


def test_flume_plugin_slot_parsed(profile: Profile) -> None:
    plugin = profile.families["10"]
    assert "10" in plugin.instances
    inst = plugin.instances["10"]
    assert set(inst.nodedefs) == {"controller", "flume1", "flume2"}
    assert {"GALLONS", "I_AUTH", "bool", "cst"} <= set(inst.editors)


def test_lookup_resolves_native_and_plugin(profile: Profile) -> None:
    insteon_def = profile.find_nodedef("KeypadDimmer_ADV", "1", "1")
    assert insteon_def is not None
    assert insteon_def.lookup_key == ("KeypadDimmer_ADV", "1", "1")

    flume_def = profile.find_nodedef("flume2", "10", "10")
    assert flume_def is not None
    assert flume_def.lookup_key == ("flume2", "10", "10")


def test_lookup_misses_return_none(profile: Profile) -> None:
    assert profile.find_nodedef("flume2", "1", "1") is None
    assert profile.find_nodedef("nonexistent", "10", "10") is None


def test_editor_scope_is_per_instance(profile: Profile) -> None:
    plugin_bool = profile.find_editor("bool", "10", "10")
    assert plugin_bool is not None
    assert plugin_bool.range_for("2").names == {0: "False", 1: "True"}
    assert profile.find_editor("bool", "1", "1") is None


def test_timestamp_preserved(profile: Profile) -> None:
    assert profile.timestamp
    assert "T" in profile.timestamp
