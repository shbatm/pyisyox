"""Tests for Group + Folder runtime wrappers + the /rest/nodes XML parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import (
    FolderRecord,
    GroupRecord,
    IoXClient,
    NodePropertyValue,
    NodeRecord,
    parse_rest_nodes_groups_folders,
)
from pyisyox.constants import INSTEON_STATELESS_NODEDEFID
from pyisyox.runtime import Folder, Group
from pyisyox.schema import Profile
from tests.test_client.conftest import FakeSession

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"
BASE = "https://eisy.local"


# --- /rest/nodes XML parser ---------------------------------------------


def test_parse_rest_nodes_extracts_groups_and_folders() -> None:
    """The captured fixture has 94 groups + folders; the parser keeps them
    separated and ignores ``<node>`` entries (those come from /api/nodes)."""
    xml = (FIXTURE_DIR / "rest_nodes.xml").read_text()
    groups, folders, _ = parse_rest_nodes_groups_folders(xml)

    # The fixture has 94 <group> entries; one is the "ISY" controller-self
    # group (flag="12") which is filtered out — leaving 93 user scenes.
    assert len(groups) == 93
    assert len(folders) >= 5  # several user-named folders


def test_parse_rest_nodes_filters_isy_self_group() -> None:
    """flag="12" is the special controller-self group whose address is
    the eisy MAC. It must not appear in the groups dict."""
    xml = """<?xml version="1.0"?>
<nodes>
  <group flag="12" nodeDefId="InsteonDimmer">
    <address>00:21:b9:00:00:00</address>
    <name>The eisy</name>
    <family>6</family>
    <members/>
  </group>
  <group flag="132" nodeDefId="InsteonDimmer">
    <address>1234</address>
    <name>Real Scene</name>
    <family>6</family>
    <pnode>1234</pnode>
    <members>
      <link type="0">3D 7D 87 1</link>
    </members>
  </group>
</nodes>"""
    groups, _, _ = parse_rest_nodes_groups_folders(xml)
    assert "00:21:b9:00:00:00" not in groups
    assert "1234" in groups


def test_parse_rest_nodes_captures_member_addresses_in_order() -> None:
    xml = """<?xml version="1.0"?>
<nodes>
  <group flag="132" nodeDefId="InsteonDimmer">
    <address>5000</address>
    <name>Living</name>
    <family>6</family>
    <members>
      <link type="0">A1</link>
      <link type="0">A2</link>
      <link type="0">A3</link>
    </members>
  </group>
</nodes>"""
    groups, _, _ = parse_rest_nodes_groups_folders(xml)
    assert groups["5000"].member_addresses == ("A1", "A2", "A3")


def test_parse_rest_nodes_handles_empty_members() -> None:
    """Empty groups (e.g. ``~zAuto DR[i]`` placeholders) parse with
    ``member_addresses == ()`` — not None, not missing."""
    xml = """<?xml version="1.0"?>
<nodes>
  <group flag="132" nodeDefId="X">
    <address>EMPTY</address>
    <name>placeholder</name>
    <family>6</family>
    <members/>
  </group>
</nodes>"""
    groups, _, _ = parse_rest_nodes_groups_folders(xml)
    assert groups["EMPTY"].member_addresses == ()


def test_parse_rest_nodes_records_folder_parent() -> None:
    xml = """<?xml version="1.0"?>
<nodes>
  <folder flag="0">
    <address>10</address>
    <name>Top</name>
    <family>13</family>
  </folder>
  <folder flag="0">
    <address>20</address>
    <name>Child</name>
    <family>13</family>
    <parent type="3">10</parent>
  </folder>
</nodes>"""
    _, folders, _ = parse_rest_nodes_groups_folders(xml)
    assert folders["10"].parent_address is None
    assert folders["20"].parent_address == "10"


def test_parse_rest_nodes_handles_empty_or_missing_xml() -> None:
    assert parse_rest_nodes_groups_folders("") == ({}, {}, "")
    assert parse_rest_nodes_groups_folders('<?xml version="1.0"?><nodes/>') == (
        {},
        {},
        "",
    )


def test_parse_rest_nodes_captures_root_group_name() -> None:
    """The root group (``flag="12"`` = ``ROOT | IS_A_GROUP``) is filtered
    out of the groups registry but its user-assigned ``<name>`` is
    surfaced as the third return value so consumers can use the
    friendly controller name for device labels (the same value the
    legacy ``/rest/config`` ``<configuration><root><name>`` path
    carried in PyISY 3.x)."""
    xml = (
        '<?xml version="1.0"?><nodes>'
        '<group flag="12"><address>00:21:b9:01:23:45</address>'
        "<name>Main eisy</name></group>"
        '<group flag="132"><address>0030</address>'
        "<name>Living Room</name><members/></group>"
        "</nodes>"
    )
    groups, folders, root_name = parse_rest_nodes_groups_folders(xml)
    assert root_name == "Main eisy"
    # Root group is filtered; only the user-facing scene survives.
    assert "00:21:b9:01:23:45" not in groups
    assert "0030" in groups
    assert folders == {}


# --- Folder runtime wrapper ---------------------------------------------


def test_folder_exposes_record_fields() -> None:
    record = FolderRecord(address="10", name="Entry", family_id="13", parent_address=None)
    folder = Folder(record)
    assert folder.address == "10"
    assert folder.name == "Entry"
    assert folder.parent_address is None
    assert folder.family_id == "13"
    assert "Entry" in repr(folder)


# --- Group runtime wrapper ----------------------------------------------


def _make_client(session: FakeSession) -> IoXClient:
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    return client


def _profile() -> Profile:
    raw = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())
    return Profile.load_from_json(raw)


@pytest.mark.asyncio
async def test_group_send_command_routes_to_node_endpoint() -> None:
    """Group commands hit /rest/nodes/{group_addr}/cmd/{cmd} — the same
    endpoint as nodes; the controller broadcasts to members."""
    record = GroupRecord(
        address="55090",
        name="Driveway",
        nodedef_id="InsteonDimmer",
        family_id="1",
        instance_id="1",
        member_addresses=("3D 7D 87 1", "40 4E 68 1"),
    )
    profile = _profile()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/55090/cmd/DON", 200, "<ok/>")
    group = Group.from_record(record, profile, _make_client(session))

    await group.send_command("DON")

    method, path, _ = session.calls[0]
    assert method == "GET"
    assert path == "/rest/nodes/55090/cmd/DON"


@pytest.mark.asyncio
async def test_group_send_command_with_level() -> None:
    """``Group.send_command("DON", 75)`` puts the level in the URL path —
    consumers pre-encode (groups don't validate via editor codec)."""
    record = GroupRecord(
        address="55090",
        name="Driveway",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
    )
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/55090/cmd/DON/75", 200, "<ok/>")
    group = Group.from_record(record, _profile(), _make_client(session))

    await group.send_command("DON", 75)

    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/55090/cmd/DON/75"


@pytest.mark.asyncio
async def test_group_send_command_no_validation() -> None:
    """Group send-path is intentionally unvalidated — `nodeDefId` on a
    group is a scene-class label, not a profile nodedef. Sending an
    unknown command id passes through to the controller; the eisy
    decides whether to honour it."""
    record = GroupRecord(
        address="X",
        name="Y",
        nodedef_id="WhateverScene",
        family_id="6",
        instance_id="1",
    )
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/X/cmd/CUSTOM_VERB", 200, "<ok/>")
    group = Group.from_record(record, _profile(), _make_client(session))

    # Should not raise — group commands aren't editor-validated.
    await group.send_command("CUSTOM_VERB")
    assert session.calls[0][1] == "/rest/nodes/X/cmd/CUSTOM_VERB"


def test_group_exposes_record_fields() -> None:
    record = GroupRecord(
        address="55090",
        name="Driveway",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        parent_address="36485",
        pnode="55090",
        member_addresses=("A1", "A2", "A3"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)))
    assert group.address == "55090"
    assert group.name == "Driveway"
    assert group.nodedef_id == "InsteonDimmer"
    assert group.family_id == "6"
    assert group.parent_address == "36485"
    assert group.member_addresses == ("A1", "A2", "A3")
    assert "Driveway" in repr(group)
    assert "members=3" in repr(group)


# --- controller_addresses + group_all_on (added 2026-05-09) -------------


def test_parse_rest_nodes_separates_controllers_from_responders() -> None:
    """``<link type="16">`` is the IoX 0x10 'controller' flag; everything
    else is a responder. Both populate ``member_addresses``; only
    type=16 entries appear in ``controller_addresses``."""
    xml = """<?xml version="1.0"?>
<nodes>
  <group flag="132" nodeDefId="InsteonDimmer">
    <address>5000</address>
    <name>Mixed Scene</name>
    <family>6</family>
    <members>
      <link type="16">CTRL 1</link>
      <link type="0">RESP 1</link>
      <link type="0">RESP 2</link>
      <link type="16">CTRL 2</link>
    </members>
  </group>
</nodes>"""
    groups, _, _ = parse_rest_nodes_groups_folders(xml)
    rec = groups["5000"]
    assert rec.member_addresses == ("CTRL 1", "RESP 1", "RESP 2", "CTRL 2")
    assert rec.controller_addresses == ("CTRL 1", "CTRL 2")


def test_parse_rest_nodes_controller_addresses_empty_when_none() -> None:
    xml = """<?xml version="1.0"?>
<nodes>
  <group flag="132" nodeDefId="InsteonDimmer">
    <address>5000</address>
    <name>No Controllers</name>
    <family>6</family>
    <members>
      <link type="0">A1</link>
      <link type="0">A2</link>
    </members>
  </group>
</nodes>"""
    groups, _, _ = parse_rest_nodes_groups_folders(xml)
    assert groups["5000"].controller_addresses == ()


def test_group_controller_addresses_passthrough() -> None:
    record = GroupRecord(
        address="X",
        name="Y",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2", "M3"),
        controller_addresses=("M1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)))
    assert group.controller_addresses == ("M1",)


# group_all_on test scaffolding -----------------------------------------


def _record_with_st(addr: str, st_value: str) -> NodeRecord:
    """Build a NodeRecord with just enough state for the group_all_on
    derivation — ``ST`` value is the only field consulted."""
    return NodeRecord(
        address=addr,
        name=addr,
        nodedef_id="DimmerLampSwitch",
        family_id="1",
        instance_id="1",
        properties={"ST": NodePropertyValue(id="ST", value=st_value, formatted="")},
    )


def test_group_all_on_true_when_every_member_is_on() -> None:
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "255"),
        "M2": _record_with_st("M2", "100"),
    }
    record = GroupRecord(
        address="G",
        name="All On",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is True


def test_group_all_on_false_when_a_member_is_off() -> None:
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "255"),
        "M2": _record_with_st("M2", "0"),  # off
    }
    record = GroupRecord(
        address="G",
        name="One Off",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is False


def test_group_all_on_false_when_member_missing_from_registry() -> None:
    """Defensive: a member that's been dropped from the registry (perhaps
    because the controller deleted it post-load and we haven't surfaced
    the lifecycle event yet) makes the group not-all-on."""
    nodes: dict[str, NodeRecord] = {"M1": _record_with_st("M1", "255")}
    record = GroupRecord(
        address="G",
        name="Stale",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2_MISSING"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is False


def test_group_all_on_false_when_member_has_no_st() -> None:
    """Plugin nodes / battery sensors don't expose ST. Treat them as
    not-on so a scene of mixed device types reports honestly."""
    no_st = NodeRecord(
        address="M1",
        name="M1",
        nodedef_id="X",
        family_id="1",
        instance_id="1",
        properties={"BATLVL": NodePropertyValue(id="BATLVL", value="80", formatted="80%")},
    )
    nodes: dict[str, NodeRecord] = {"M1": no_st}
    record = GroupRecord(
        address="G",
        name="No ST",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is False


def test_group_all_on_false_when_constructed_without_nodes_ref() -> None:
    """Without the controller's nodes registry the property can't compute
    — return False rather than raise. Consumers using Group purely for
    command-issuing don't need the nodes ref."""
    record = GroupRecord(
        address="G",
        name="No Nodes",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)))  # nodes=None
    assert group.group_all_on is False


def test_group_all_on_false_when_no_members() -> None:
    """An empty group can't be 'all on'."""
    record = GroupRecord(
        address="EMPTY",
        name="No Members",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=(),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes={})
    assert group.group_all_on is False


# group_any_on test scaffolding -----------------------------------------
#
# Mirrors the group_all_on cases above but with the inverted aggregation:
# "at least one member on" is what HA's scene-switch ``is_on`` wants, and
# what the legacy ``pyisy.Group.status`` returned.


def test_group_any_on_true_when_at_least_one_member_is_on() -> None:
    """Mixed on/off members — any-on aggregation is True."""
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "255"),
        "M2": _record_with_st("M2", "0"),
    }
    record = GroupRecord(
        address="G",
        name="Mixed",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_any_on is True


def test_group_any_on_false_when_every_member_is_off() -> None:
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "0"),
        "M2": _record_with_st("M2", "0"),
    }
    record = GroupRecord(
        address="G",
        name="All Off",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_any_on is False


def test_group_any_on_skips_missing_members_instead_of_short_circuiting() -> None:
    """Unlike ``group_all_on``, a member dropped from the registry doesn't
    flip the aggregation to False — present-and-on members still count."""
    nodes: dict[str, NodeRecord] = {"M1": _record_with_st("M1", "255")}
    record = GroupRecord(
        address="G",
        name="Stale",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "M2_MISSING"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_any_on is True


def test_group_any_on_false_when_member_has_no_st() -> None:
    no_st = NodeRecord(
        address="M1",
        name="M1",
        nodedef_id="X",
        family_id="1",
        instance_id="1",
        properties={"BATLVL": NodePropertyValue(id="BATLVL", value="80", formatted="80%")},
    )
    nodes: dict[str, NodeRecord] = {"M1": no_st}
    record = GroupRecord(
        address="G",
        name="No ST",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_any_on is False


def test_group_any_on_false_when_constructed_without_nodes_ref() -> None:
    record = GroupRecord(
        address="G",
        name="No Nodes",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)))
    assert group.group_any_on is False


def test_group_any_on_false_when_no_members() -> None:
    record = GroupRecord(
        address="EMPTY",
        name="No Members",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=(),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes={})
    assert group.group_any_on is False


# Stateless-member handling -------------------------------------------
#
# Battery / stateless members (motion sensors, RemoteLincs, binary-alarm
# nodedefs — see INSTEON_STATELESS_NODEDEFID) don't carry a persistent ST,
# so they're excluded from both aggregations.


def _stateless_record(addr: str, st_value: str) -> NodeRecord:
    return NodeRecord(
        address=addr,
        name=addr,
        nodedef_id=INSTEON_STATELESS_NODEDEFID[0],
        family_id="1",
        instance_id="1",
        properties={"ST": NodePropertyValue(id="ST", value=st_value, formatted="")},
    )


def test_group_all_on_ignores_stateless_member_off() -> None:
    """A stateless member reading 0 (or nothing) must not drag the
    scene's all-on aggregation to False."""
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "255"),
        "S1": _stateless_record("S1", "0"),
    }
    record = GroupRecord(
        address="G",
        name="Lamp + Motion Sensor",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "S1"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is True


def test_group_all_on_false_when_only_stateless_members() -> None:
    """A scene with no stateful members can't be 'all on' — don't return
    True vacuously."""
    nodes: dict[str, NodeRecord] = {"S1": _stateless_record("S1", "255")}
    record = GroupRecord(
        address="G",
        name="Sensors Only",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("S1",),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_all_on is False


def test_group_any_on_ignores_stateless_member_on() -> None:
    """A momentarily-on stateless member doesn't make the scene 'any on'."""
    nodes: dict[str, NodeRecord] = {
        "M1": _record_with_st("M1", "0"),
        "S1": _stateless_record("S1", "255"),
    }
    record = GroupRecord(
        address="G",
        name="Off Lamp + Triggered Sensor",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
        member_addresses=("M1", "S1"),
    )
    group = Group.from_record(record, _profile(), _make_client(FakeSession(BASE)), nodes=nodes)
    assert group.group_any_on is False


@pytest.mark.asyncio
async def test_group_rename_posts_name_and_type_group() -> None:
    """``Group.rename`` posts ``{"name", "nodeType": "group"}`` to
    ``/api/nodes/{address}`` — same endpoint as Node.rename, but the
    server requires the type field to dispatch through the scene
    registry instead of the node registry."""
    record = GroupRecord(
        address="55090",
        name="Driveway",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
    )
    session = FakeSession(BASE)
    session.set_route("POST", "/api/nodes/55090", 200, '{"successful": true, "data": null}')
    group = Group.from_record(record, _profile(), _make_client(session))

    await group.rename("Front Yard")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/nodes/55090")
    assert kwargs["json"] == {"name": "Front Yard", "nodeType": "group"}
