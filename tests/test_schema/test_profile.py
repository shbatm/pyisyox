"""Profile loader and lookup-table tests against the captured eisy fixture."""

from __future__ import annotations

from pyisyox.schema import Profile
from pyisyox.schema.nodedef import NodeDef


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


def test_find_editor_falls_back_to_common_family(profile: Profile) -> None:
    """UDI's ``common`` family carries a shared editor set
    (``_sys_notify_full``, ``_sys_notify_short``) that plugin nodedefs
    can reference. find_editor should locate them regardless of which
    family/instance the caller passes."""
    # Direct lookup against the common family works.
    direct = profile.find_editor("_sys_notify_full", "common", "1")
    assert direct is not None

    # A plugin (family 10, instance 10) referencing the same id resolves
    # via the common-family fallback.
    via_fallback = profile.find_editor("_sys_notify_full", "10", "10")
    assert via_fallback is direct, "fallback must return the same Editor instance"


def test_find_editor_returns_none_when_not_in_either_scope(profile: Profile) -> None:
    assert profile.find_editor("not_a_real_editor", "10", "10") is None


def test_find_editor_decodes_encoded_ids(profile: Profile) -> None:
    """An encoded editor id is decoded on the spot, regardless of scope —
    that's how the dynamic Z-Wave nodedefs spell most of their editors.
    A leading ``_`` that *doesn't* parse as an encoding (UDI's
    ``_sys_notify_full``) still falls through to the table lookup."""
    ed = profile.find_editor("_51_0_R_0_101_N_IX_DIM_REP", "4", "1")
    assert ed is not None
    assert ed.id == "_51_0_R_0_101_N_IX_DIM_REP"
    assert (ed.ranges[0].uom, ed.ranges[0].min, ed.ranges[0].max) == ("51", 0, 101)
    # _sys_notify_full begins with "_" but isn't an encoding — table lookup wins.
    assert profile.find_editor("_sys_notify_full", "10", "10") is not None
    # A "_"-prefixed string that's neither a valid encoding nor a known id.
    assert profile.find_editor("_17_x", "10", "10") is None


def test_register_nodedefs_adds_to_scope_and_lookup(profile: Profile) -> None:
    """register_nodedefs() folds a batch of nodedefs into a family/instance
    (creating it if needed) and updates the join-key lookup — this is how
    the dynamic Z-Wave nodedefs land in the live profile post-load."""
    nd = NodeDef(id="UZW9999", family_id="4", instance_id="1")
    assert profile.find_nodedef("UZW9999", "4", "1") is None
    profile.register_nodedefs("4", "1", {"UZW9999": nd})
    assert profile.find_nodedef("UZW9999", "4", "1") is nd
    assert profile.families["4"].instances["1"].nodedefs["UZW9999"] is nd


def test_find_editor_local_match_takes_precedence_over_common(profile: Profile) -> None:
    """If a family-local editor with the same id exists, it wins —
    common is a fallback, not a shadow."""
    # The Flume slot defines its own "bool" editor. Common doesn't.
    plugin_bool = profile.find_editor("bool", "10", "10")
    assert plugin_bool is not None
    # Insteon (family 1) doesn't define "bool"; should miss → fall back
    # to common, which also doesn't have it → None.
    assert profile.find_editor("bool", "1", "1") is None
