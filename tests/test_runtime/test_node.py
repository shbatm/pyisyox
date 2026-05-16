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


def test_node_properties_normalize_byte_to_percent(real_profile: Profile) -> None:
    """A DimmerLampSwitch reports ``OL``/``ST`` as a UOM-100 0-255 byte,
    but the ``I_OL`` editor (and the /cmd surface) speak UOM-51 0-100% —
    ``Node.properties`` surfaces the normalised percentage."""
    record = _make_record(
        nodedef_id="DimmerLampSwitch",
        properties={
            "OL": NodePropertyValue(id="OL", value="191", formatted="75%", uom="100"),
            "ST": NodePropertyValue(id="ST", value="255", formatted="On", uom="100"),
        },
    )
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))

    assert node.properties["OL"].value == "75"
    assert node.properties["OL"].uom == "51"
    assert node.properties["ST"].value == "100"
    assert node.status is not None
    assert node.status.value == "100"
    # underlying record is left untouched (normalisation is on read)
    assert record.properties["OL"].value == "191"


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
    """KeypadDimmer DON takes an optional I_OL_PARAM parameter (0-100%,
    UOM 51). Verify the value and its UOM trail the URL as ``/75/51``."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/DON/75/51", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("DON", 75)

    _, path, _ = session.calls[0]
    assert path.endswith("/cmd/DON/75/51")


@pytest.mark.asyncio
async def test_send_command_thermostat_clismode_via_enum_name(real_profile: Profile) -> None:
    """Thermostat CLIMD uses I_TSTAT_MODE — accept the string name 'Heat'
    and translate it via the editor codec to the integer 1."""
    record = _make_record(nodedef_id="Thermostat", family_id="1", instance_id="1")
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/CLIMD/1/98", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.send_command("CLIMD", "Heat")

    _, path, _ = session.calls[0]
    assert path.endswith("/cmd/CLIMD/1/98")


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
async def test_send_command_no_nodedef_passes_through(real_profile: Profile) -> None:
    """A node with no resolved nodedef (e.g. a dynamic Z-Wave node whose
    ``UZW*`` nodedef isn't published in ``/rest/profiles``) still issues
    the command — params verbatim, numeric coerced to int, no UOM."""
    record = _make_record(
        address="ZW003_1", nodedef_id="UZW0009", family_id="4", instance_id="1"
    )
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/ZW003_1/cmd/DON/100", 200, "<ok/>")
    session.set_route("GET", "/rest/nodes/ZW003_1/cmd/DOF", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))
    assert node.nodedef is None

    await node.send_command("DON", 100)
    await node.send_command("DOF")

    assert [p for _, p, _ in session.calls] == [
        "/rest/nodes/ZW003_1/cmd/DON/100",
        "/rest/nodes/ZW003_1/cmd/DOF",
    ]


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


# --- Z-Wave parameter wire surface ---------------------------------------


@pytest.mark.asyncio
async def test_client_set_zwave_parameter_uses_legacy_path() -> None:
    """Family ``4`` writes go to ``/rest/zwave/.../parameters/set/...``."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW003_1/config/set/24/1/1",
        200,
        "<RestResponse status='200'/>",
    )
    client = _make_client(session)
    await client.set_zwave_parameter("ZW003_1", 24, 1, 1)
    _, path, _ = session.calls[0]
    assert path == "/rest/zwave/node/ZW003_1/config/set/24/1/1"


@pytest.mark.asyncio
async def test_client_set_zwave_parameter_zmatter_path() -> None:
    """``zmatter=True`` routes the same write to the zmatter prefix."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zmatter/zwave/node/ZW100/config/set/3/255/2",
        200,
        "<ok/>",
    )
    client = _make_client(session)
    await client.set_zwave_parameter("ZW100", 3, 255, 2, zmatter=True)
    _, path, _ = session.calls[0]
    assert path == "/rest/zmatter/zwave/node/ZW100/config/set/3/255/2"


@pytest.mark.asyncio
async def test_client_get_zwave_parameter_uses_legacy_path() -> None:
    session = FakeSession(BASE)
    session.set_route(
        "GET", "/rest/zwave/node/ZW003_1/config/query/7", 200, "<ok/>"
    )
    client = _make_client(session)
    await client.get_zwave_parameter("ZW003_1", 7)
    _, path, _ = session.calls[0]
    assert path == "/rest/zwave/node/ZW003_1/config/query/7"


@pytest.mark.asyncio
async def test_node_set_zwave_parameter_picks_path_by_family(
    real_profile: Profile,
) -> None:
    """A family-``4`` node uses the legacy path; a family-``12`` (Z-Matter)
    node uses the ``/rest/zmatter/...`` prefix. Both go through the same
    runtime method — caller doesn't pass a ``zmatter`` flag."""
    legacy_session = FakeSession(BASE)
    legacy_session.set_route(
        "GET",
        "/rest/zwave/node/ZW003_1/config/set/24/1/1",
        200,
        "<RestResponse succeeded='true'/>",
    )
    legacy_record = _make_record(
        address="ZW003_1", nodedef_id="UZW000F", family_id="4"
    )
    legacy_node = Node.from_record(
        legacy_record, real_profile, _make_client(legacy_session)
    )
    await legacy_node.set_zwave_parameter(24, 1, 1)
    assert legacy_session.calls[0][1] == (
        "/rest/zwave/node/ZW003_1/config/set/24/1/1"
    )

    zmatter_session = FakeSession(BASE)
    zmatter_session.set_route(
        "GET",
        "/rest/zmatter/zwave/node/ZW100/config/set/3/255/2",
        200,
        "<RestResponse succeeded='true'/>",
    )
    zmatter_record = _make_record(
        address="ZW100", nodedef_id="UZW000F", family_id="12"
    )
    zmatter_node = Node.from_record(
        zmatter_record, real_profile, _make_client(zmatter_session)
    )
    await zmatter_node.set_zwave_parameter(3, 255, 2)
    assert zmatter_session.calls[0][1] == (
        "/rest/zmatter/zwave/node/ZW100/config/set/3/255/2"
    )


@pytest.mark.asyncio
async def test_node_get_zwave_parameter_parses_config_response(
    real_profile: Profile,
) -> None:
    """A success ``<config paramNum size value/>`` is parsed into a
    ``{"parameter", "size", "value"}`` dict of ints — matches PyISY 3.x's
    structured-return shape so consumers don't have to parse the XML
    themselves."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW003_1/config/query/24",
        200,
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<config paramNum="24" size="1" value="2"/>',
    )
    record = _make_record(address="ZW003_1", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))

    result = await node.get_zwave_parameter(24)
    assert result == {"parameter": 24, "size": 1, "value": 2}


@pytest.mark.asyncio
async def test_node_get_zwave_parameter_raises_on_rest_failure(
    real_profile: Profile,
) -> None:
    """A ``<RestResponse succeeded="false"><status>404</status>...``
    body raises :class:`NodeCommandError` so the caller learns the
    write didn't land — not a silent ``None`` return."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW003_1/config/query/24",
        200,
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<RestResponse succeeded="false"><status>404</status></RestResponse>',
    )
    record = _make_record(address="ZW003_1", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))

    with pytest.raises(NodeCommandError, match="status=404"):
        await node.get_zwave_parameter(24)


@pytest.mark.asyncio
async def test_node_set_zwave_parameter_raises_on_rest_failure(
    real_profile: Profile,
) -> None:
    """Symmetric failure on the set path: ``succeeded="false"`` raises."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW003_1/config/set/24/1/1",
        200,
        '<RestResponse succeeded="false"><status>500</status></RestResponse>',
    )
    record = _make_record(address="ZW003_1", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))

    with pytest.raises(NodeCommandError, match="status=500"):
        await node.set_zwave_parameter(24, 1, 1)


@pytest.mark.asyncio
async def test_node_set_zwave_parameter_rejects_non_zwave_family(
    real_profile: Profile,
) -> None:
    """Insteon nodes (family ``"1"``) don't have a parameters surface;
    the helper raises before touching the wire."""
    record = _make_record(family_id="1")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="not a Z-Wave node"):
        await node.set_zwave_parameter(1, 0, 1)


@pytest.mark.asyncio
async def test_node_set_zwave_parameter_rejects_invalid_size(
    real_profile: Profile,
) -> None:
    """Z-Wave parameter byte size is constrained to 1, 2, or 4."""
    record = _make_record(address="ZW003_1", family_id="4", nodedef_id="UZW000F")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="size must be 1, 2, or 4"):
        await node.set_zwave_parameter(1, 0, 3)


# --- Z-Wave lock-code wire surface ---------------------------------------


@pytest.mark.asyncio
async def test_client_set_zwave_lock_code_uses_legacy_path() -> None:
    """Family-``4`` writes go to ``/rest/zwave/.../security/user/.../set/code/...``."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/set/code/1234",
        200,
        "<RestResponse succeeded='true'/>",
    )
    client = _make_client(session)
    await client.set_zwave_lock_code("ZW100", 3, 1234)
    _, path, _ = session.calls[0]
    assert path == "/rest/zwave/node/ZW100/security/user/3/set/code/1234"


@pytest.mark.asyncio
async def test_client_delete_zwave_lock_code_uses_legacy_path() -> None:
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/delete",
        200,
        "<RestResponse succeeded='true'/>",
    )
    client = _make_client(session)
    await client.delete_zwave_lock_code("ZW100", 3)
    _, path, _ = session.calls[0]
    assert path == "/rest/zwave/node/ZW100/security/user/3/delete"


@pytest.mark.asyncio
async def test_client_set_zwave_lock_code_zmatter_path() -> None:
    """``zmatter=True`` flips the prefix."""
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zmatter/zwave/node/ZW100/security/user/3/set/code/1234",
        200,
        "<RestResponse succeeded='true'/>",
    )
    client = _make_client(session)
    await client.set_zwave_lock_code("ZW100", 3, 1234, zmatter=True)
    _, path, _ = session.calls[0]
    assert path == "/rest/zmatter/zwave/node/ZW100/security/user/3/set/code/1234"


@pytest.mark.asyncio
async def test_node_set_zwave_lock_code_picks_path_by_family(
    real_profile: Profile,
) -> None:
    legacy_session = FakeSession(BASE)
    legacy_session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/set/code/1234",
        200,
        "<RestResponse succeeded='true'/>",
    )
    legacy_record = _make_record(
        address="ZW100", nodedef_id="UZW000F", family_id="4"
    )
    legacy_node = Node.from_record(
        legacy_record, real_profile, _make_client(legacy_session)
    )
    await legacy_node.set_zwave_lock_code(3, 1234)
    assert legacy_session.calls[0][1] == (
        "/rest/zwave/node/ZW100/security/user/3/set/code/1234"
    )

    zmatter_session = FakeSession(BASE)
    zmatter_session.set_route(
        "GET",
        "/rest/zmatter/zwave/node/ZW100/security/user/3/set/code/1234",
        200,
        "<RestResponse succeeded='true'/>",
    )
    zmatter_record = _make_record(
        address="ZW100", nodedef_id="UZW000F", family_id="12"
    )
    zmatter_node = Node.from_record(
        zmatter_record, real_profile, _make_client(zmatter_session)
    )
    await zmatter_node.set_zwave_lock_code(3, 1234)
    assert zmatter_session.calls[0][1] == (
        "/rest/zmatter/zwave/node/ZW100/security/user/3/set/code/1234"
    )


@pytest.mark.asyncio
async def test_node_delete_zwave_lock_code_uses_delete_path(
    real_profile: Profile,
) -> None:
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/delete",
        200,
        "<RestResponse succeeded='true'/>",
    )
    record = _make_record(address="ZW100", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))
    await node.delete_zwave_lock_code(3)
    assert session.calls[0][1] == "/rest/zwave/node/ZW100/security/user/3/delete"


@pytest.mark.asyncio
async def test_node_set_zwave_lock_code_raises_on_rest_failure(
    real_profile: Profile,
) -> None:
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/set/code/1234",
        200,
        '<RestResponse succeeded="false"><status>500</status></RestResponse>',
    )
    record = _make_record(address="ZW100", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))
    with pytest.raises(NodeCommandError, match="status=500"):
        await node.set_zwave_lock_code(3, 1234)


@pytest.mark.asyncio
async def test_node_delete_zwave_lock_code_raises_on_rest_failure(
    real_profile: Profile,
) -> None:
    session = FakeSession(BASE)
    session.set_route(
        "GET",
        "/rest/zwave/node/ZW100/security/user/3/delete",
        200,
        '<RestResponse succeeded="false"><status>404</status></RestResponse>',
    )
    record = _make_record(address="ZW100", nodedef_id="UZW000F", family_id="4")
    node = Node.from_record(record, real_profile, _make_client(session))
    with pytest.raises(NodeCommandError, match="status=404"):
        await node.delete_zwave_lock_code(3)


@pytest.mark.asyncio
async def test_node_lock_code_rejects_non_zwave_family(
    real_profile: Profile,
) -> None:
    """Insteon nodes have no ``/security/user/...`` surface."""
    record = _make_record(family_id="1")
    node = Node.from_record(record, real_profile, _make_client(FakeSession(BASE)))
    with pytest.raises(NodeCommandError, match="not a Z-Wave node"):
        await node.set_zwave_lock_code(1, 1234)
    with pytest.raises(NodeCommandError, match="not a Z-Wave node"):
        await node.delete_zwave_lock_code(1)


# --- set_on_level (percent + UOM via the editor codec) -------------------


@pytest.mark.asyncio
async def test_set_on_level_sends_percent_with_uom(real_profile: Profile) -> None:
    """``set_on_level`` routes through ``send_command(OL, …)`` — the
    ``I_OL`` editor (UOM 51) validates 0-100 and the value is sent as
    ``/cmd/OL/{val}/51``; the controller does the device-side scaling."""
    record = _make_record(nodedef_id="DimmerLampSwitch", family_id="1", instance_id="1")
    session = FakeSession(BASE)
    session.set_route("GET", "/rest/nodes/3D%207D%2087%201/cmd/OL/75/51", 200, "<ok/>")
    node = Node.from_record(record, real_profile, _make_client(session))

    await node.set_on_level(75)

    method, path, _ = session.calls[0]
    assert method == "GET"
    assert path == "/rest/nodes/3D%207D%2087%201/cmd/OL/75/51"


@pytest.mark.asyncio
async def test_set_on_level_rejects_out_of_range(real_profile: Profile) -> None:
    """The ``I_OL`` editor's ``max=100`` rejects an out-of-range value
    before any HTTP call fires."""
    record = _make_record(nodedef_id="DimmerLampSwitch", family_id="1", instance_id="1")
    session = FakeSession(BASE)
    node = Node.from_record(record, real_profile, _make_client(session))
    with pytest.raises(NodeCommandError, match="above max"):
        await node.set_on_level(300)
    assert session.calls == [], "must short-circuit before HTTP"


# --- control-id → accept-command resolution (init pairing) ---------------


def _node_with_nodedef(real_profile: Profile, nodedef: NodeDef) -> Node:
    """A Node whose nodedef is the supplied synthetic def."""
    return Node(_make_record(), nodedef, real_profile, _make_client(FakeSession(BASE)))


def test_resolve_control_id_to_init_paired_command(real_profile: Profile) -> None:
    """A coalesced control written by its *status* id resolves to the
    accept command whose parameter ``init`` names that status.

    Covers the PG3 ``virtualtemp`` shape (``setTemp`` param
    ``init="ST"`` ⇄ ``ST`` status) and the i3 ``GV0`` shape (accept
    ``GV0`` param ``init="ST"``) — and that a direct accept id, the
    Insteon dual-purposed ``OL``, and an unknown id are unchanged.
    """
    nd = NodeDef(
        id="virtualtemp",
        family_id="10",
        instance_id="4",
        properties={},
        cmds=NodeCommands(
            accepts=[
                Command(id="resetStats"),
                Command(
                    id="setTemp",
                    parameters=[CommandParameter(editor_id="temp", init="ST")],
                ),
                # Insteon dual-purposes the id: accept "OL" *is* the
                # command — a direct match must win, no redirect.
                Command(
                    id="OL",
                    parameters=[CommandParameter(editor_id="I_OL", init="OL")],
                ),
            ]
        ),
    )
    node = _node_with_nodedef(real_profile, nd)

    assert node._resolve_accept_command_id("ST") == "setTemp"
    assert node._resolve_accept_command_id("setTemp") == "setTemp"
    assert node._resolve_accept_command_id("OL") == "OL"
    assert node._resolve_accept_command_id("resetStats") == "resetStats"
    # No status/command pairing → unchanged (the not-accepted error
    # still surfaces downstream).
    assert node._resolve_accept_command_id("ZZZ") == "ZZZ"

    i3 = NodeDef(
        id="I3PaddleFlags",
        family_id="1",
        instance_id="1",
        properties={},
        cmds=NodeCommands(
            accepts=[
                Command(
                    id="GV0",
                    parameters=[CommandParameter(editor_id="I3_RELAY_DIM", init="ST")],
                )
            ]
        ),
    )
    i3_node = _node_with_nodedef(real_profile, i3)
    assert i3_node._resolve_accept_command_id("ST") == "GV0"
    assert i3_node._resolve_accept_command_id("GV0") == "GV0"
