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
    parse_rest_nodes_groups_folders,
)
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
    groups, folders = parse_rest_nodes_groups_folders(xml)

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
    groups, _ = parse_rest_nodes_groups_folders(xml)
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
    groups, _ = parse_rest_nodes_groups_folders(xml)
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
    groups, _ = parse_rest_nodes_groups_folders(xml)
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
    _, folders = parse_rest_nodes_groups_folders(xml)
    assert folders["10"].parent_address is None
    assert folders["20"].parent_address == "10"


def test_parse_rest_nodes_handles_empty_or_missing_xml() -> None:
    assert parse_rest_nodes_groups_folders("") == ({}, {})
    assert parse_rest_nodes_groups_folders('<?xml version="1.0"?><nodes/>') == ({}, {})


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
