"""Tests for ``Profile.merge`` (PG3 dynamic profile reload support)."""

from __future__ import annotations

from pyisyox.schema import Profile


def _profile_from_nodedef(nodedef_id: str, prop_id: str, prop_name: str) -> Profile:
    """Build a one-nodedef profile for merge tests."""
    return Profile.load_from_json(
        {
            "timestamp": "2026-05-08T00:00:00Z",
            "families": [
                {
                    "id": "10",
                    "name": "",
                    "instances": [
                        {
                            "id": "10",
                            "name": "",
                            "editors": [
                                {"id": "bool", "ranges": [{"uom": "2"}]},
                            ],
                            "linkdefs": [],
                            "nodedefs": [
                                {
                                    "id": nodedef_id,
                                    "nls": "x",
                                    "properties": [
                                        {"id": prop_id, "editor": "bool", "name": prop_name},
                                    ],
                                    "cmds": {"sends": [], "accepts": []},
                                    "links": {"ctl": [], "rsp": []},
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    )


# --- additive merge ------------------------------------------------------


def test_merge_adds_new_nodedef() -> None:
    """A nodedef in the incoming profile that's absent in the target is added."""
    base = _profile_from_nodedef("flume1", "ST", "Status")
    incoming = _profile_from_nodedef("flume2", "GV1", "Current")

    result = base.merge(incoming)

    assert result.nodedefs_added == [("flume2", "10", "10")]
    assert result.nodedefs_replaced == []
    assert result.changed is True
    assert base.find_nodedef("flume2", "10", "10") is not None
    # Original entry survives.
    assert base.find_nodedef("flume1", "10", "10") is not None


# --- replacement merge ---------------------------------------------------


def test_merge_replaces_existing_nodedef_in_place() -> None:
    """Same id → existing entry overwritten; runtime objects holding the
    Profile reference see the new property when they re-resolve."""
    base = _profile_from_nodedef("flume2", "GV1", "Old Name")
    incoming = _profile_from_nodedef("flume2", "GV1", "Current")

    result = base.merge(incoming)

    assert result.nodedefs_replaced == [("flume2", "10", "10")]
    assert result.nodedefs_added == []
    nd = base.find_nodedef("flume2", "10", "10")
    assert nd is not None
    assert nd.properties["GV1"].name == "Current"


def test_merge_does_not_remove_nodedefs_absent_from_incoming() -> None:
    """Merge is additive — nodedefs only in the target stay put. To
    remove, callers diff explicitly."""
    base = _profile_from_nodedef("flume1", "ST", "A")
    base_extra = _profile_from_nodedef("flume2", "GV1", "B")
    base.merge(base_extra)  # base now has both flume1 and flume2

    only_flume1 = _profile_from_nodedef("flume1", "ST", "A-updated")
    result = base.merge(only_flume1)

    assert base.find_nodedef("flume1", "10", "10") is not None
    assert base.find_nodedef("flume2", "10", "10") is not None
    assert result.nodedefs_replaced == [("flume1", "10", "10")]


# --- editor / linkdef tracking ------------------------------------------


def test_merge_tracks_editor_changes() -> None:
    """Editors merged from a second profile show up in the diff result."""
    base = _profile_from_nodedef("flume1", "ST", "S")
    # Build a profile that adds a NEW editor id to instance 10/10.
    new_editor_profile = Profile.load_from_json(
        {
            "families": [
                {
                    "id": "10",
                    "instances": [
                        {
                            "id": "10",
                            "editors": [
                                {"id": "GALLONS", "ranges": [{"uom": "69", "prec": 4}]},
                            ],
                            "linkdefs": [],
                            "nodedefs": [],
                        }
                    ],
                }
            ]
        }
    )
    result = base.merge(new_editor_profile)

    assert any(e[0] == "GALLONS" for e in result.editors_added)
    assert base.find_editor("GALLONS", "10", "10") is not None


# --- unrelated families remain untouched --------------------------------


def test_merge_adds_a_new_family() -> None:
    """An incoming profile carrying a family not in the target gets added wholesale."""
    base = _profile_from_nodedef("flume1", "ST", "S")
    new_family = Profile.load_from_json(
        {
            "families": [
                {
                    "id": "11",  # different family id
                    "instances": [
                        {
                            "id": "11",
                            "editors": [],
                            "linkdefs": [],
                            "nodedefs": [
                                {
                                    "id": "newplugin",
                                    "properties": [],
                                    "cmds": {"sends": [], "accepts": []},
                                    "links": {"ctl": [], "rsp": []},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    result = base.merge(new_family)

    assert "11" in base.families
    assert ("newplugin", "11", "11") in result.nodedefs_added


# --- empty merge --------------------------------------------------------


def test_merge_empty_profile_is_noop() -> None:
    """Merging an empty incoming profile changes nothing."""
    base = _profile_from_nodedef("flume2", "GV1", "Current")
    empty = Profile.load_from_json({"families": []})

    result = base.merge(empty)

    assert result.changed is False
    assert base.find_nodedef("flume2", "10", "10") is not None


def test_merge_updates_timestamp_when_present() -> None:
    base = _profile_from_nodedef("flume2", "GV1", "X")
    base.timestamp = "2026-05-01T00:00:00Z"
    incoming = _profile_from_nodedef("flume2", "GV1", "Y")
    incoming.timestamp = "2026-05-08T12:00:00Z"

    base.merge(incoming)
    assert base.timestamp == "2026-05-08T12:00:00Z"
