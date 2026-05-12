"""Tests for :class:`Node` introspection helpers + ergonomic wrappers.

Introspection (``is_thermostat`` / ``is_lock`` / ``is_dimmable`` /
``is_battery_node`` / ``protocol``) is derived from the node's nodedef
+ properties + family id, with no consumer wire knowledge needed.

Wrappers (``set_climate_mode``, ``secure_lock``, ``set_on_level``, …)
are one-liners over :meth:`Node.send_command` that bake in the IoX
wire-convention command id; this file pins the URL each wrapper
produces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
from pyisyox.constants import NodeFlag, Protocol
from pyisyox.runtime import Node
from pyisyox.schema import Profile
from tests.test_client.conftest import FakeSession

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"
BASE = "https://eisy.local"


@pytest.fixture(scope="module")
def real_profile() -> Profile:
    raw = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())
    return Profile.load_from_json(raw)


def _make_record(
    nodedef_id: str = "DimmerSwitchOnly",
    family_id: str = "1",
    instance_id: str = "1",
    address: str = "AA BB CC 1",
    properties: dict[str, NodePropertyValue] | None = None,
    type_: str = "1.0.0.0",
    parent_address: str | None = None,
    pnode: str | None = None,
) -> NodeRecord:
    return NodeRecord(
        address=address,
        name="Test",
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        type=type_,
        properties=properties or {},
        parent_address=parent_address,
        pnode=pnode,
    )


def _make_node(record: NodeRecord, profile: Profile, session: FakeSession | None = None) -> Node:
    if session is None:
        session = FakeSession(BASE)
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    return Node.from_record(record, profile, client)


# --- protocol -------------------------------------------------------------


@pytest.mark.parametrize(
    ("family_id", "expected"),
    [
        ("1", Protocol.INSTEON),
        ("2", Protocol.UPB),  # family.xsd: "2" is UPB, not X10
        ("4", Protocol.ZWAVE),  # legacy attached Z-Wave radio
        ("12", Protocol.ZWAVE),  # Z-Matter radio as a Z-Wave controller
        ("15", Protocol.MATTER),  # Z-Matter radio as a Matter controller
        ("10", Protocol.NODE_SERVER),  # NODESERVER family / PG3 slot
        ("99", Protocol.NODE_SERVER),  # any id outside the core family set
        ("13", Protocol.UNKNOWN),  # folder family — recognised but no protocol
        ("3", Protocol.UNKNOWN),  # RCS — recognised core family, no mapping
        ("", Protocol.UNKNOWN),  # no family id
    ],
)
def test_protocol_classifies_by_family_id(real_profile: Profile, family_id: str, expected: Protocol) -> None:
    node = _make_node(_make_record(family_id=family_id), real_profile)
    result = node.protocol
    assert result is expected
    assert result == str(expected)  # StrEnum: still string-compatible


# --- is_thermostat / is_lock / is_dimmable / is_battery_node -------------


def test_is_thermostat_true_for_thermostat_nodedef(real_profile: Profile) -> None:
    """Thermostat nodedef accepts CLIMD + CLISPH — both trigger the
    thermostat flag via the cmds-based check."""
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile)
    assert node.is_thermostat is True
    assert node.is_lock is False


def test_is_lock_true_for_doorlock_nodedef(real_profile: Profile) -> None:
    """The captured fixture's ``DoorLock`` nodedef accepts DON/DOF/WDU
    only (no SECMD). The lock-detection fallback on the nodedef id
    pattern catches it."""
    node = _make_node(_make_record(nodedef_id="DoorLock"), real_profile)
    assert node.is_lock is True
    assert node.is_thermostat is False


def test_is_dimmable_true_for_dimmer_with_param_don(real_profile: Profile) -> None:
    """Dimmer nodedefs accept ``DON`` with an on-level parameter."""
    node = _make_node(_make_record(nodedef_id="DimmerLampSwitch"), real_profile)
    assert node.is_dimmable is True


def test_is_dimmable_false_for_relay_only(real_profile: Profile) -> None:
    """Relay-only switches accept ``DON`` with no parameter — pin that
    the helper distinguishes parameterised vs parameterless DON."""
    node = _make_node(_make_record(nodedef_id="RelayLampOnly"), real_profile)
    # DON without parameters → not dimmable
    assert node.is_dimmable is False


def test_is_fan_true_for_fanlinc_motor(real_profile: Profile) -> None:
    """``FanLincMotor`` is the Insteon FanLinc fan-side sub-node;
    its sibling light reports as ``DimmerLampOnly``.

    Note: ``is_dimmable`` returns False on FanLincMotor because the
    ``I_FLM_LVL`` editor pins the level to a 4-value subset
    (``{0, 25, 75, 100}``) without an explicit range max — the
    multilevel-range check requires a numeric max. ``is_fan`` is
    therefore the only positive classification signal for these
    nodes; today they fall through to ``SWITCH``."""
    node = _make_node(_make_record(nodedef_id="FanLincMotor"), real_profile)
    assert node.is_fan is True


def test_is_fan_false_for_dimmer(real_profile: Profile) -> None:
    """A plain dimmer must not be misclassified as a fan."""
    node = _make_node(_make_record(nodedef_id="DimmerLampOnly"), real_profile)
    assert node.is_fan is False


def test_is_fan_false_for_thermostat(real_profile: Profile) -> None:
    """The fan-mode editors on a thermostat are editor ids, not
    nodedef ids — the Thermostat nodedef must not match ``is_fan``."""
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile)
    assert node.is_fan is False


def test_is_battery_node_detects_batlvl_only_devices(real_profile: Profile) -> None:
    """Battery sensors expose BATLVL but no ST."""
    record = _make_record(properties={"BATLVL": NodePropertyValue(id="BATLVL", value="80", formatted="80%")})
    node = _make_node(record, real_profile)
    assert node.is_battery_node is True


def test_is_battery_node_false_when_st_present(real_profile: Profile) -> None:
    record = _make_record(
        properties={
            "ST": NodePropertyValue(id="ST", value="0", formatted="Off"),
            "BATLVL": NodePropertyValue(id="BATLVL", value="80", formatted="80%"),
        }
    )
    node = _make_node(record, real_profile)
    assert node.is_battery_node is False


def test_introspection_safe_when_nodedef_unresolved(real_profile: Profile) -> None:
    """Plugin nodes loaded before their profile is resolved still get a
    Node — introspection helpers must not crash, just return False."""
    node = _make_node(_make_record(nodedef_id="NoSuchType"), real_profile)
    assert node.nodedef is None
    assert node.is_thermostat is False
    assert node.is_lock is False
    assert node.is_fan is False
    assert node.is_dimmable is False


# --- shortcuts: status + primary_address + parent_address ----------------


def test_status_returns_st_property_when_present(real_profile: Profile) -> None:
    """``Node.status`` is a shortcut for ``properties["ST"]``."""
    st = NodePropertyValue(id="ST", value="100", formatted="On", uom="51")
    node = _make_node(_make_record(properties={"ST": st}), real_profile)
    assert node.status is st


def test_status_none_when_st_absent(real_profile: Profile) -> None:
    """Nodes without an ST reading (write-only controllers, plugin nodes
    that don't advertise it) return ``None`` — callers branch on it."""
    node = _make_node(_make_record(properties={}), real_profile)
    assert node.status is None


def test_primary_address_resolves_from_pnode(real_profile: Profile) -> None:
    """``primary_address`` derives from the IoX ``<pnode>`` element — the
    device-primary address for multi-button physicals (KeypadLinc,
    RemoteLinc, FanLinc). Returns the primary only when this node is a
    sub-button (``pnode != address``)."""
    sub_button = _make_node(_make_record(address="AA BB CC 2", pnode="AA BB CC 1"), real_profile)
    assert sub_button.primary_address == "AA BB CC 1"


def test_primary_address_none_for_device_root(real_profile: Profile) -> None:
    """The device primary has ``pnode == address`` — surface as ``None`` so
    consumers can use ``primary_address is not None`` as a sub-button
    indicator. ``pnode`` absent is treated the same way."""
    root_with_self_pnode = _make_node(_make_record(address="AA BB CC 1", pnode="AA BB CC 1"), real_profile)
    assert root_with_self_pnode.primary_address is None

    root_without_pnode = _make_node(_make_record(pnode=None), real_profile)
    assert root_without_pnode.primary_address is None


def test_parent_address_returns_tree_parent(real_profile: Profile) -> None:
    """``parent_address`` exposes the IoX ``<parent>`` element — the
    tree-hierarchy parent (folder/scene), independent of the device
    primary. The two concepts are orthogonal: a sub-button can be inside
    a folder while also being a sub-node of a device primary."""
    sub_button_in_folder = _make_node(
        _make_record(
            address="AA BB CC 2",
            parent_address="folder-id",  # tree parent
            pnode="AA BB CC 1",  # device primary (different concept)
        ),
        real_profile,
    )
    assert sub_button_in_folder.parent_address == "folder-id"
    assert sub_button_in_folder.primary_address == "AA BB CC 1"

    # Tree-root node, no folder parent, also a device primary
    node = _make_node(
        _make_record(
            address="AA BB CC 1",
            parent_address=None,
            pnode="AA BB CC 1",
        ),
        real_profile,
    )
    assert node.parent_address is None
    assert node.primary_address is None


# --- flag / has_flag -----------------------------------------------------


def test_flag_defaults_to_zero(real_profile: Profile) -> None:
    """Records constructed without a flag carry ``0`` — and ``has_flag``
    reports False for every bit, including a zero argument (no bits set
    can never satisfy any nonzero mask)."""
    node = _make_node(_make_record(), real_profile)
    assert node.flag == 0
    assert node.has_flag(NodeFlag.DEVICE_ROOT) is False
    assert node.has_flag(NodeFlag.NEW) is False


def test_has_flag_matches_individual_bit(real_profile: Profile) -> None:
    """``DEVICE_ROOT`` (0x80) is set on a record carrying ``flag=128``."""
    record = _make_record()
    record.flag = int(NodeFlag.DEVICE_ROOT)
    node = _make_node(record, real_profile)
    assert node.flag == 128
    assert node.has_flag(NodeFlag.DEVICE_ROOT) is True
    assert node.has_flag(NodeFlag.NEW) is False


def test_has_flag_combined_mask_requires_all_bits(real_profile: Profile) -> None:
    """An OR'd mask only matches when every bit in the mask is set —
    consumers asking for ``NEW | IN_ERR`` mean "both", not "either"."""
    record = _make_record()
    record.flag = int(NodeFlag.NEW | NodeFlag.IN_ERR | NodeFlag.DEVICE_ROOT)
    node = _make_node(record, real_profile)
    assert node.has_flag(NodeFlag.NEW | NodeFlag.IN_ERR) is True
    assert node.has_flag(NodeFlag.NEW) is True
    assert node.has_flag(NodeFlag.NEW | NodeFlag.TO_DELETE) is False


# --- ergonomic wrappers (URL pinning) ------------------------------------
#
# Each wrapper does the same thing: route through send_command with a
# specific cmd id. Pin the URL each one produces against a real captured
# nodedef so that, if a future codec change affects encode behaviour,
# the wire-level effect of these helpers stays correct.


def _pin_get(session: FakeSession, path: str) -> None:
    session.set_route("GET", path, 200, "<RestResponse succeeded='true'/>")


@pytest.mark.asyncio
async def test_set_climate_mode_routes_to_climd_with_enum(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/CLIMD/1/98")
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile, session)
    await node.set_climate_mode("Heat")
    assert any("/cmd/CLIMD/1/98" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_climate_mode_accepts_int(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/CLIMD/2/98")
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile, session)
    await node.set_climate_mode(2)
    assert any("/cmd/CLIMD/2/98" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_fan_mode_routes_to_clifs(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/CLIFS/8/99")
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile, session)
    await node.set_fan_mode("Auto")
    assert any("/cmd/CLIFS/" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_node_rename_posts_name_and_type_node(real_profile: Profile) -> None:
    """``Node.rename`` posts ``{"name", "nodeType": "node"}`` to
    ``/api/nodes/{address}``. URL encoding on the address is
    handled by the client's quote(safe="") — spaces become ``%20``."""
    session = FakeSession(BASE)
    session.set_route(
        "POST",
        "/api/nodes/AA%20BB%20CC%201",
        200,
        '{"successful": true, "data": null}',
    )
    node = _make_node(_make_record(), real_profile, session)
    await node.rename("New Name")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/nodes/AA%20BB%20CC%201")
    assert kwargs["json"] == {"name": "New Name", "nodeType": "node"}


#
# secure_lock / secure_unlock / start_manual_dimming / stop_manual_dimming
# can't pin wire-level — the captured fixture has no Z-Wave radio (no
# SECMD) and no Insteon BMAN/SMAN (deprecated in favor of FADE_*). Stub
# send_command on the instance and assert each wrapper delegates with
# the right wire-convention command id + parameter.


# --- record-backed property accessors (smoke) ----------------------------


def test_node_exposes_name_type_and_enabled_from_record(real_profile: Profile) -> None:
    """``name`` / ``type`` / ``enabled`` are simple record passthroughs —
    pin them so a future record-shape refactor doesn't silently change
    the consumer surface."""
    record = _make_record(type_="1.65.69.0")
    node = _make_node(record, real_profile)
    assert node.name == "Test"
    assert node.type == "1.65.69.0"
    assert node.enabled is True  # default on NodeRecord


# --- is_dimmable defensive guard ----------------------------------------


def test_is_dimmable_false_when_st_property_missing_from_nodedef(real_profile: Profile) -> None:
    """``DimmerSwitchOnly`` advertises only an ``ERR`` property — no ST
    means the editor lookup can't even start, so the helper returns False
    rather than crashing on ``st_prop.editor_id``."""
    node = _make_node(_make_record(nodedef_id="DimmerSwitchOnly"), real_profile)
    assert node.is_dimmable is False


# --- ergonomic wrappers: end-to-end via real fixture nodedefs ------------


@pytest.mark.asyncio
async def test_set_climate_setpoint_heat_routes_to_clisph(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile, session)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/CLISPH/70/14")
    await node.set_climate_setpoint_heat(70)
    assert any("/cmd/CLISPH/" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_climate_setpoint_cool_routes_to_clispc(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    node = _make_node(_make_record(nodedef_id="Thermostat"), real_profile, session)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/CLISPC/70/14")
    await node.set_climate_setpoint_cool(70)
    assert any("/cmd/CLISPC/" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_on_level_routes_to_ol(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    node = _make_node(_make_record(nodedef_id="DimmerLampSwitch"), real_profile, session)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/OL/50/51")
    await node.set_on_level(50)
    assert any("/cmd/OL/" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_ramp_rate_routes_to_rr(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    node = _make_node(_make_record(nodedef_id="DimmerLampSwitch"), real_profile, session)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/RR/5/25")
    await node.set_ramp_rate(5)
    assert any("/cmd/RR/" in path for _, path, _ in session.calls)


@pytest.mark.asyncio
async def test_set_backlight_routes_to_bl(real_profile: Profile) -> None:
    session = FakeSession(BASE)
    node = _make_node(_make_record(nodedef_id="DimmerLampSwitch"), real_profile, session)
    _pin_get(session, "/rest/nodes/AA%20BB%20CC%201/cmd/BL/50/51")
    await node.set_backlight(50)
    assert any("/cmd/BL/" in path for _, path, _ in session.calls)


# --- ergonomic wrappers: stubbed (cmds not in captured fixture) ----------
#
# secure_lock/unlock and start/stop_manual_dimming wrap send_command with
# constant cmd ids; the captured profile has no nodedef accepting SECMD /
# BMAN / SMAN, so we stub send_command on the instance and assert each
# wrapper delegates correctly.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper_method", "expected_cmd", "expected_params"),
    [
        ("secure_lock", "SECMD", (1,)),
        ("secure_unlock", "SECMD", (0,)),
        ("start_manual_dimming", "BMAN", ()),
        ("stop_manual_dimming", "SMAN", ()),
    ],
)
async def test_thin_wrapper_delegates_to_send_command(
    real_profile: Profile,
    monkeypatch: pytest.MonkeyPatch,
    wrapper_method: str,
    expected_cmd: str,
    expected_params: tuple,
) -> None:
    node = _make_node(_make_record(), real_profile)
    calls: list[tuple[str, tuple]] = []

    async def _record(self: Node, cmd_id: str, *params: float | str) -> None:
        calls.append((cmd_id, params))

    # ``Node`` uses __slots__ so instance attribute assignment is blocked;
    # patch at the class level instead.
    monkeypatch.setattr(Node, "send_command", _record)
    await getattr(node, wrapper_method)()

    assert calls == [(expected_cmd, expected_params)]
