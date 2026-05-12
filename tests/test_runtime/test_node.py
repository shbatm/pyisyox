"""Tests for :class:`pyisyox.runtime.Node` and command-send wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
from pyisyox.runtime import Node, NodeCommandError
from pyisyox.runtime._commands import encode_command_params
from pyisyox.schema import Profile
from pyisyox.schema.cmd import Command, CommandParameter
from pyisyox.schema.nodedef import NodeCommands, NodeDef
from tests.test_client.conftest import FakeSession

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"
BASE = "https://eisy.local"


@pytest.fixture(scope="module")
def real_profile() -> Profile:
    """The captured fixture covers Insteon thermostat (multi-param command),
    KeypadDimmer (single-param command), Flume (no controllable + plugin
    button verbs) — enough to exercise every send_command branch."""
    raw = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())
    return Profile.load_from_json(raw)


def _make_record(
    address: str = "3D 7D 87 1",
    name: str = "Test",
    nodedef_id: str = "KeypadDimmer_ADV",
    family_id: str = "1",
    instance_id: str = "1",
    properties: dict[str, NodePropertyValue] | None = None,
) -> NodeRecord:
    return NodeRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        type="1.65.69.0",
        properties=properties or {},
    )


def _make_client(session: FakeSession) -> IoXClient:
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    # Pretend the connect-time authenticate has already happened so
    # send_command goes straight to GET without queuing /api/login.
    client._authenticated = True
    return client


# --- introspection -------------------------------------------------------


def test_node_exposes_record_fields(real_profile: Profile) -> None:
    record = _make_record(properties={"ST": NodePropertyValue(id="ST", value="0", formatted="Off")})
    session = FakeSession(BASE)
    node = Node.from_record(record, real_profile, _make_client(session))

    assert node.address == "3D 7D 87 1"
    assert node.nodedef_id == "KeypadDimmer_ADV"
    assert node.nodedef is not None
    assert node.nodedef.id == "KeypadDimmer_ADV"
    assert node.properties["ST"].formatted == "Off"


def test_node_unresolved_nodedef_returns_none(real_profile: Profile) -> None:
    record = _make_record(nodedef_id="NoSuchType", family_id="1", instance_id="1")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    assert node.nodedef is None


# --- send_command — happy paths ------------------------------------------


@pytest.mark.asyncio
async def test_send_command_dof_no_params(real_profile: Profile) -> None:
    """KeypadDimmer accepts DOF with no parameters → URL has no /N suffix."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/DOF", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("DOF")

    method, path, _ = session.calls[0]
    assert method == "GET"
    assert path == "/rest/nodes/3D%207D%2087%201/cmd/DOF"


@pytest.mark.asyncio
async def test_send_command_don_with_optional_level(real_profile: Profile) -> None:
    """KeypadDimmer DON command takes an optional I_OL parameter (0-100).
    Verify the level appears as the URL's last path segment."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/DON/75", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("DON", 75)

    _, path, _ = session.calls[0]
    assert path.endswith("/cmd/DON/75")


@pytest.mark.asyncio
async def test_send_command_thermostat_clismode_via_enum_name(real_profile: Profile) -> None:
    """Thermostat CLIMD uses I_TSTAT_MODE — accept the string name 'Heat'
    and translate it via the editor codec to the integer 1."""
    record = _make_record(nodedef_id="Thermostat", family_id="1", instance_id="1")
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/CLIMD/1", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("CLIMD", "Heat")

    _, path, _ = session.calls[0]
    assert path.endswith("/cmd/CLIMD/1")


@pytest.mark.asyncio
async def test_send_command_plugin_verb_no_params(real_profile: Profile) -> None:
    """Flume controller accepts DISCOVER (no parameters)."""
    record = _make_record(
        address="n010_controller",
        nodedef_id="controller",
        family_id="10",
        instance_id="10",
    )
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/n010_controller/cmd/DISCOVER", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("DISCOVER")

    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/n010_controller/cmd/DISCOVER"


# --- send_command — validation failures ----------------------------------


@pytest.mark.asyncio
async def test_send_command_unknown_command_id_raises(real_profile: Profile) -> None:
    record = _make_record()
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="not accepted by nodedef"):
        await node.send_command("BOGUS_CMD")


@pytest.mark.asyncio
async def test_send_command_no_nodedef_raises(real_profile: Profile) -> None:
    record = _make_record(nodedef_id="NoSuchType")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="no nodedef resolved"):
        await node.send_command("DON")


@pytest.mark.asyncio
async def test_send_command_rejects_subset_invalid_value(real_profile: Profile) -> None:
    """I_TSTAT_MODE has subset='0-3,5-7' — Fan Only (4) is in names but not
    in subset. Encode must reject it before any HTTP call fires."""
    record = _make_record(nodedef_id="Thermostat")
    session = FakeSession(BASE)  # no routes — would error if we hit the wire
    node = Node.from_record(record, real_profile, _make_client(session))

    with pytest.raises(NodeCommandError, match="not in subset"):
        await node.send_command("CLIMD", 4)
    assert session.calls == [], "validation must short-circuit before HTTP"


@pytest.mark.asyncio
async def test_send_command_rejects_unknown_enum_name(real_profile: Profile) -> None:
    record = _make_record(nodedef_id="Thermostat")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="not a recognised name"):
        await node.send_command("CLIMD", "Bogus")


@pytest.mark.asyncio
async def test_send_command_too_many_params_raises(real_profile: Profile) -> None:
    """DOF takes zero parameters — passing one should raise."""
    record = _make_record()
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="accepts 0 parameter"):
        await node.send_command("DOF", 5)


@pytest.mark.asyncio
async def test_send_command_omits_optional_param(real_profile: Profile) -> None:
    """KeypadDimmer DON's I_OL_PARAM is optional — omitting it sends
    the URL without a level segment instead of raising."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/DON", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("DON")

    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/3D%207D%2087%201/cmd/DON"


@pytest.mark.asyncio
async def test_send_command_missing_required_param_raises(real_profile: Profile) -> None:
    """Thermostat CLIMD's I_TSTAT_MODE parameter is non-optional —
    omitting it must raise before any HTTP call fires."""
    record = _make_record(nodedef_id="Thermostat", family_id="1", instance_id="1")
    session = FakeSession(BASE)
    node = Node.from_record(record, real_profile, _make_client(session))

    with pytest.raises(NodeCommandError, match="requires parameter 0"):
        await node.send_command("CLIMD")
    assert session.calls == [], "validation must short-circuit before HTTP"


@pytest.mark.asyncio
async def test_send_command_missing_editor_raises() -> None:
    """If a command parameter references an editor id that doesn't exist
    in the profile, encoding raises rather than sending a malformed URL."""
    nodedef = NodeDef(
        id="synthetic",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(
            accepts=[Command(id="DO_THING", parameters=[CommandParameter(editor_id="NOPE")])],
        ),
    )
    empty_profile = Profile()

    with pytest.raises(NodeCommandError, match="editor 'NOPE' not found"):
        encode_command_params(
            nodedef=nodedef,
            profile=empty_profile,
            family_id="1",
            instance_id="1",
            command_id="DO_THING",
            params=[1],
            target_label="node 'X'",
        )


# --- IoXClient.send_node_command url construction ------------------------


@pytest.mark.asyncio
async def test_client_send_node_command_url_quoting() -> None:
    """Insteon addresses contain spaces — verify quote(safe='') escapes them."""
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/DON/75", 200, "<ok/>")
    client = _make_client(session)
    await client.send_node_command("3D 7D 87 1", "DON", 75)
    _, path, _ = session.calls[0]
    assert "%20" in path


@pytest.mark.asyncio
async def test_client_send_node_command_no_params() -> None:
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/n010_x/cmd/DISCOVER", 200, "<ok/>")
    client = _make_client(session)
    await client.send_node_command("n010_x", "DISCOVER")
    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/n010_x/cmd/DISCOVER"


@pytest.mark.asyncio
async def test_client_send_node_command_multiple_params() -> None:
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/A/cmd/X/1/2/3", 200, "<ok/>")
    client = _make_client(session)
    await client.send_node_command("A", "X", 1, 2, 3)
    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/A/cmd/X/1/2/3"


# --- enable / disable ----------------------------------------------------


@pytest.mark.asyncio
async def test_node_set_enabled_disable(real_profile: Profile) -> None:
    """``set_enabled(False)`` → GET /rest/nodes/{addr}/disable; the local
    record's ``enabled`` flag flips on success."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/disable", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))
    assert node.enabled is True

    await node.set_enabled(False)

    method, path, _ = session.calls[0]
    assert method == "GET"
    assert path == "/rest/nodes/3D%207D%2087%201/disable"
    assert node.enabled is False


@pytest.mark.asyncio
async def test_node_set_enabled_enable(real_profile: Profile) -> None:
    """``set_enabled(True)`` → GET /rest/nodes/{addr}/enable."""
    record = _make_record()
    record.enabled = False
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/enable", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.set_enabled(True)

    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/3D%207D%2087%201/enable"
    assert node.enabled is True


@pytest.mark.asyncio
async def test_client_set_node_enabled_quotes_address() -> None:
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/disable", 200, "<ok/>")
    client = _make_client(session)
    await client.set_node_enabled("3D 7D 87 1", False)
    _, path, _ = session.calls[0]
    assert path == "/rest/nodes/3D%207D%2087%201/disable"
