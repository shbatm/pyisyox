"""Tests for ``pyisyox.testing`` — the consumer-facing test builders.

These exercise the public API the way downstream test suites do:
build records, wire a Controller, resolve real ``Node`` / ``Group`` /
``Variable`` wrappers off the bundled eisy6 profile, and synthesise
events through the dispatcher seam. If a future refactor breaks the
shape these factories produce, the failure shows up here before any
consumer's pin starts breaking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pyisyox.client import (
    FolderRecord,
    GroupRecord,
    IoXClient,
    LoadResult,
    NetworkResourceRecord,
    NodePropertyValue,
    NodeRecord,
    ProgramRecord,
    VariableRecord,
)
from pyisyox.controller import Controller
from pyisyox.runtime import (
    Folder,
    Group,
    NetworkResource,
    Node,
    Program,
    Variable,
)
from pyisyox.schema.profile import Profile
from pyisyox.testing import (
    DEFAULT_HOST,
    DEFAULT_UUID,
    INSTEON_BSENSOR_SUBNODE_DISABLED,
    INSTEON_BSENSOR_SUBNODE_DUSK_DAWN,
    INSTEON_BSENSOR_SUBNODE_HEARTBEAT,
    INSTEON_BSENSOR_SUBNODE_LOW_BATTERY,
    INSTEON_BSENSOR_SUBNODE_NEGATIVE,
    INSTEON_BSENSOR_SUBNODE_TAMPER,
    INSTEON_THERMOSTAT_SUBNODE_COOL,
    INSTEON_THERMOSTAT_SUBNODE_HEAT,
    NODEDEF_FOR_PLATFORM,
    PLUGIN_COVER_FAMILY_ID,
    PLUGIN_DIMMER_FAMILY_ID,
    PLUGIN_HUB_FAMILY_ID,
    PLUGIN_TRIGGER_FAMILY_ID,
    RecordedCall,
    fire_event,
    fire_lifecycle,
    fire_program_status,
    fire_variable_table_change,
    load_profile,
    make_classified_node_record,
    make_controller,
    make_cover_load_result,
    make_dimmer_plugin_load_result,
    make_door_sensor_records,
    make_folder,
    make_folder_record,
    make_group,
    make_group_record,
    make_hub_plugin_load_result,
    make_insteon_binary_sensor_records,
    make_leak_sensor_records,
    make_load_result,
    make_motion_sensor_records,
    make_network_resource,
    make_network_resource_record,
    make_node,
    make_node_record,
    make_plugin_cover_node_record,
    make_plugin_dimmer_node_record,
    make_plugin_hub_node_record,
    make_plugin_trigger_node_record,
    make_profile_with_cover_plugin,
    make_profile_with_dimmer_plugin,
    make_profile_with_hub_plugin,
    make_profile_with_trigger_plugin,
    make_program,
    make_program_record,
    make_thermostat_binary_records,
    make_trigger_plugin_load_result,
    make_variable,
    make_variable_record,
    recorded_calls,
    recorded_calls_for,
)

# ---------------------------------------------------------------------------
# Bundled profile.
# ---------------------------------------------------------------------------


def test_load_profile_returns_real_profile() -> None:
    profile = load_profile()
    assert isinstance(profile, Profile)
    # The bundled capture has the native Insteon, Z-Wave, Universal +
    # Polyglot families.
    assert "1" in profile.families  # Insteon
    assert "4" in profile.families  # Z-Wave


def test_load_profile_is_cached() -> None:
    """LRU-cached so parse cost doesn't compound under pytest-xdist."""
    assert load_profile() is load_profile()


def test_native_classifier_resolves_for_each_platform_shortcut() -> None:
    """Every entry in ``NODEDEF_FOR_PLATFORM`` resolves to a real
    nodedef in the bundled profile."""
    profile = load_profile()
    known_ids = {nd.id for nd in profile.nodedef_lookup.values()}
    for nodedef_id in NODEDEF_FOR_PLATFORM.values():
        assert nodedef_id in known_ids, nodedef_id


# ---------------------------------------------------------------------------
# Record builders.
# ---------------------------------------------------------------------------


def test_make_node_record_seeds_status_and_err() -> None:
    rec = make_node_record("1A 2B 3C 1", "Lamp")
    assert isinstance(rec, NodeRecord)
    assert rec.address == "1A 2B 3C 1"
    assert rec.pnode == "1A 2B 3C 1"  # defaults to address
    # Insteon (family "1") records auto-seed ERR alongside ST.
    assert set(rec.properties) == {"ST", "ERR"}
    assert rec.properties["ST"].value == "0"
    assert rec.properties["ST"].uom == "100"


def test_make_node_record_skips_err_for_non_insteon() -> None:
    rec = make_node_record("AA BB CC 1", "ZW", family_id="4")
    assert "ERR" not in rec.properties


def test_make_node_record_pnode_explicit() -> None:
    rec = make_node_record("AA BB CC 2", "Sub", pnode="AA BB CC 1")
    assert rec.pnode == "AA BB CC 1"


def test_make_node_record_custom_properties_keeps_err_seed() -> None:
    """Caller-supplied ``properties`` still get ERR appended on family
    "1" so the consumer's diagnostic always shows up."""
    custom = {
        "OL": NodePropertyValue(id="OL", value="100", formatted="100%", uom="51", name="On Level"),
    }
    rec = make_node_record("1A 2B 3C 4", "x", properties=custom)
    assert set(rec.properties) == {"OL", "ERR"}


def test_record_factories_return_typed_records() -> None:
    assert isinstance(make_group_record("g1", "G"), GroupRecord)
    assert isinstance(make_folder_record("f1", "F"), FolderRecord)
    assert isinstance(make_program_record("p1", "P"), ProgramRecord)
    assert isinstance(make_network_resource_record("nr1", "NR"), NetworkResourceRecord)
    assert isinstance(make_variable_record("1", "5", "V"), VariableRecord)


# ---------------------------------------------------------------------------
# Controller wiring.
# ---------------------------------------------------------------------------


def test_make_load_result_defaults() -> None:
    lr = make_load_result()
    assert isinstance(lr, LoadResult)
    assert lr.config.uuid == DEFAULT_UUID
    assert lr.profile is load_profile()
    assert lr.nodes == {}
    # Variables default seeds both type tables.
    assert set(lr.variables) == {"1", "2"}


def test_make_controller_no_network() -> None:
    """``connect()`` is unnecessary — _loaded / _dispatcher / _client
    are pre-populated; the client's HTTP coroutines are AsyncMock'd."""
    rec = make_node_record("1A 2B 3C 1", "Lamp")
    lr = make_load_result(nodes={rec.address: rec})
    controller = make_controller(lr)

    assert isinstance(controller, Controller)
    assert controller.base_url == DEFAULT_HOST
    assert controller._loaded is lr
    assert isinstance(controller._client, IoXClient)
    # HTTP-dispatching methods should be AsyncMocks.
    assert isinstance(controller._client.send_node_command, AsyncMock)
    assert isinstance(controller._client.post_variable_update, AsyncMock)


@pytest.mark.asyncio
async def test_controller_send_command_does_not_hit_network() -> None:
    rec = make_node_record("1A 2B 3C 1", "Lamp")
    lr = make_load_result(nodes={rec.address: rec})
    controller = make_controller(lr)
    node = make_node(rec, controller)
    await node.send_command("DON", 75)
    controller._client.send_node_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_recorded_calls_captures_wire_shape() -> None:
    """``recorded_calls`` exposes the actual ``(method, args)`` issued by
    the wrapper layer — without monkey-patching the wrapper itself."""
    rec = make_node_record("1A 2B 3C 1", "Lamp")
    controller = make_controller(make_load_result(nodes={rec.address: rec}))
    node = make_node(rec, controller)

    await node.send_command("DON", 75)
    await node.send_command("DOF")

    calls = recorded_calls(controller)
    assert calls == [
        RecordedCall("send_node_command", ("1A 2B 3C 1", "DON", 75, "51"), {}),
        RecordedCall("send_node_command", ("1A 2B 3C 1", "DOF"), {}),
    ]
    assert recorded_calls_for(controller, "send_node_command") == calls
    assert recorded_calls_for(controller, "post_variable_update") == []


@pytest.mark.asyncio
async def test_recorded_calls_coexists_with_assert_awaited_once_with() -> None:
    """The recording side-effect doesn't break the existing AsyncMock
    ``.assert_awaited_*`` interface — both styles work on the same
    fake."""
    rec = make_node_record("1A 2B 3C 1", "Lamp")
    controller = make_controller(make_load_result(nodes={rec.address: rec}))
    node = make_node(rec, controller)

    await node.send_command("DOF")

    controller._client.send_node_command.assert_awaited_once_with("1A 2B 3C 1", "DOF")
    assert recorded_calls_for(controller, "send_node_command") == [
        RecordedCall("send_node_command", ("1A 2B 3C 1", "DOF"), {}),
    ]


@pytest.mark.asyncio
async def test_fake_client_return_value_override_takes_effect() -> None:
    """The recording side-effect propagates ``unittest.mock.DEFAULT`` so a
    test can still set ``client.<method>.return_value = ...`` to programme
    a real return shape (e.g. ``create_variable`` echoing the new
    record). Without that, the side-effect's implicit ``None`` would
    mask the override."""
    controller = make_controller(make_load_result())
    expected = {"successful": True, "data": {"id": "5", "name": "x"}}
    controller._client.create_variable.return_value = expected

    result = await controller._client.create_variable("1", "x")

    assert result == expected
    assert recorded_calls_for(controller, "create_variable") == [
        RecordedCall("create_variable", ("1", "x"), {}),
    ]


def test_recorded_calls_is_live_and_clearable() -> None:
    """The list returned by :func:`recorded_calls` is the live storage —
    clearing it between phases is the documented way to scope assertions."""
    controller = make_controller(make_load_result())
    calls = recorded_calls(controller)
    assert calls == []
    calls.append(RecordedCall("synthetic", (), {}))
    assert recorded_calls(controller) == [RecordedCall("synthetic", (), {})]
    calls.clear()
    assert recorded_calls(controller) == []


# ---------------------------------------------------------------------------
# Real-wrapper resolution off the bundled profile.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "attr"),
    [
        ("climate", "is_thermostat"),
        ("lock", "is_lock"),
        ("fan", "is_fan"),
        ("light", "is_dimmable"),
    ],
)
def test_make_classified_node_resolves_introspection(target: str, attr: str) -> None:
    rec = make_classified_node_record(f"AA BB {target[:2]} 1", target, target=target)
    controller = make_controller(make_load_result(nodes={rec.address: rec}))
    node = make_node(rec, controller)
    assert isinstance(node, Node)
    assert getattr(node, attr) is True


def test_make_group_resolves_against_controller() -> None:
    n1 = make_node_record("1A 2B 3C 1", "Lamp")
    n2 = make_node_record("1A 2B 3C 2", "Lamp 2")
    group_rec = make_group_record("g1", "Scene", member_addresses=(n1.address, n2.address))
    controller = make_controller(
        make_load_result(
            nodes={n1.address: n1, n2.address: n2},
            groups={group_rec.address: group_rec},
        )
    )
    group = make_group(group_rec, controller)
    assert isinstance(group, Group)
    # No nodes are on, so the aggregate is False (not raising).
    assert group.group_any_on is False


def test_make_program_folder_variable_network_resource_wrappers() -> None:
    controller = make_controller(make_load_result())

    prog_rec = make_program_record("p1", "Bedtime", path="/Programs/Bedtime")
    prog = make_program(prog_rec, controller)
    assert isinstance(prog, Program)
    assert prog.name == "Bedtime"

    folder_rec = make_folder_record("f1", "Lights")
    folder = make_folder(folder_rec)
    assert isinstance(folder, Folder)
    assert folder.name == "Lights"

    var_rec = make_variable_record("1", "5", "Mode", value=3)
    var = make_variable(var_rec, controller)
    assert isinstance(var, Variable)
    assert var.value == 3

    nr_rec = make_network_resource_record("nr1", "Doorbell")
    nr = make_network_resource(nr_rec, controller)
    assert isinstance(nr, NetworkResource)
    assert nr.name == "Doorbell"


# ---------------------------------------------------------------------------
# Plugin profile augmentations.
# ---------------------------------------------------------------------------


def test_cover_plugin_profile_grafts_family_100() -> None:
    profile = make_profile_with_cover_plugin()
    assert PLUGIN_COVER_FAMILY_ID in profile.families
    # Loading the bundled profile separately must not see the graft —
    # confirms we're not mutating the cached :func:`load_profile`.
    assert PLUGIN_COVER_FAMILY_ID not in load_profile().families


def test_make_cover_load_result_routes_to_classifier() -> None:
    cover_rec = make_plugin_cover_node_record()
    lr = make_cover_load_result(nodes={cover_rec.address: cover_rec})
    controller = make_controller(lr)
    node = make_node(cover_rec, controller)
    # Plugin family slots produce node_server protocol — the consumer's
    # cue to defer to pyisyox.classify rather than native introspection.
    assert node.protocol == "node_server"


def test_button_plugin_profile_grafts_family_101() -> None:
    profile = make_profile_with_hub_plugin()
    assert PLUGIN_HUB_FAMILY_ID in profile.families
    hub_rec = make_plugin_hub_node_record()
    lr = make_hub_plugin_load_result(nodes={hub_rec.address: hub_rec})
    assert PLUGIN_HUB_FAMILY_ID in lr.profile.families


def test_trigger_plugin_profile_grafts_family_102() -> None:
    profile = make_profile_with_trigger_plugin()
    assert PLUGIN_TRIGGER_FAMILY_ID in profile.families
    rec = make_plugin_trigger_node_record()
    # Trigger-source nodes carry no status.
    assert rec.properties == {}
    lr = make_trigger_plugin_load_result(nodes={rec.address: rec})
    assert PLUGIN_TRIGGER_FAMILY_ID in lr.profile.families


def test_dimmer_plugin_profile_grafts_family_103_with_editors() -> None:
    profile = make_profile_with_dimmer_plugin()
    assert PLUGIN_DIMMER_FAMILY_ID in profile.families
    # The two synthetic editors must land in the plugin instance.
    instance = profile.families[PLUGIN_DIMMER_FAMILY_ID].instances["1"]
    assert "PG_LEVEL_ENUM" in instance.editors
    assert "INTEGER" in instance.editors
    assert "BOOL" in instance.editors

    rec = make_plugin_dimmer_node_record()
    lr = make_dimmer_plugin_load_result(nodes={rec.address: rec})
    assert PLUGIN_DIMMER_FAMILY_ID in lr.profile.families


# ---------------------------------------------------------------------------
# Event-firing helpers.
# ---------------------------------------------------------------------------


def test_fire_event_invokes_registered_listeners() -> None:
    controller = make_controller(make_load_result())
    received: list[object] = []
    controller.add_event_listener(received.append)

    sentinel = object()
    fire_event(controller, sentinel)
    assert received == [sentinel]


def test_fire_lifecycle_invokes_registered_listeners() -> None:
    controller = make_controller(make_load_result())
    received: list[object] = []
    controller.add_node_lifecycle_listener(received.append)

    sentinel = object()
    fire_lifecycle(controller, sentinel)
    assert received == [sentinel]


def test_fire_program_status_invokes_registered_listeners() -> None:
    controller = make_controller(make_load_result())
    received: list[object] = []
    controller.add_program_status_listener(received.append)

    sentinel = object()
    fire_program_status(controller, sentinel)
    assert received == [sentinel]


def test_fire_variable_table_change_invokes_registered_listeners() -> None:
    """``Controller`` doesn't expose a public registrar for this
    listener type yet; consumers register directly on the dispatcher.
    The fire helper still belongs in the testing surface so when the
    public registrar lands, the firing seam is already there."""
    controller = make_controller(make_load_result())
    received: list[object] = []
    assert controller._dispatcher is not None
    controller._dispatcher._variable_table_change_listeners.append(received.append)

    sentinel = object()
    fire_variable_table_change(controller, sentinel)
    assert received == [sentinel]


# ---------------------------------------------------------------------------
# Regressions for issues caught in PR #134 review.
# ---------------------------------------------------------------------------


def test_make_controller_dispatcher_carries_variable_registry() -> None:
    """``make_controller`` must wire ``load_result.variables`` into the
    dispatcher — otherwise variable property events arriving via
    ``feed_event_frame`` silently miss their record."""
    var_rec = make_variable_record("1", "5", "Mode", value=3)
    lr = make_load_result(variables={"1": {"5": var_rec}, "2": {}})
    controller = make_controller(lr)
    assert controller._dispatcher is not None
    # Same dict object as the load result — so a record overlay applied
    # by EventDispatcher mutates the wrapper consumers read from too.
    assert controller._dispatcher._variables is lr.variables


def test_make_classified_node_record_lock_default_family_is_zwave() -> None:
    rec = make_classified_node_record("AA BB CC 1", "L", target="lock")
    assert rec.family_id == "4"


def test_make_classified_node_record_lock_family_override_honored() -> None:
    """Caller passing ``family_id="1"`` for a lock target must keep the
    Insteon family — the docstring promises override; the impl had been
    silently clobbering it."""
    rec = make_classified_node_record(
        "AA BB CC 1", "L", target="lock", family_id="1"
    )
    assert rec.family_id == "1"


@pytest.mark.parametrize(
    "method_name",
    [
        "set_node_enabled",
        "get_zwave_parameter",
        "set_zwave_parameter",
        "set_zwave_lock_code",
        "delete_zwave_lock_code",
    ],
)
def test_fake_client_stubs_zwave_and_enable_methods(method_name: str) -> None:
    """Beyond the five primary dispatch verbs, the fake client must
    also stub the Z-Wave + enable/disable methods so a consumer test
    that drives a Z-Wave lock or toggles ``Node.set_enabled`` doesn't
    fall through to a real ``aiohttp`` call on the MagicMock session."""
    controller = make_controller(make_load_result())
    method = getattr(controller._client, method_name)
    assert isinstance(method, AsyncMock)


# --- make_program_record runtime-field kwargs --------------------------------


def test_make_program_record_runtime_fields_default_to_none() -> None:
    """The new runtime kwargs preserve back-compat: omit them and the
    record's runtime fields stay ``None``."""
    record = make_program_record("0010", "Sunset Lights", path="Lighting/Sunset")
    assert record.run_at_startup is None
    assert record.running is None
    assert record.last_run_time is None
    assert record.last_finish_time is None
    assert record.next_scheduled_run_time is None


def test_make_program_record_accepts_runtime_fields() -> None:
    """Every runtime field can now be supplied via the helper instead
    of forcing consumers to ``dataclasses.replace`` after the fact."""
    record = make_program_record(
        "0010",
        "Sunset Lights",
        path="Lighting/Sunset",
        run_at_startup=True,
        running="idle",
        last_run_time="2026-05-13T18:42:11.000Z",
        last_finish_time="2026-05-13T18:42:13.000Z",
        next_scheduled_run_time="2026-05-14T18:42:00.000Z",
    )
    assert record.run_at_startup is True
    assert record.running == "idle"
    assert record.last_run_time == "2026-05-13T18:42:11.000Z"
    assert record.last_finish_time == "2026-05-13T18:42:13.000Z"
    assert record.next_scheduled_run_time == "2026-05-14T18:42:00.000Z"


# --- Insteon binary-sensor families ------------------------------------------


def test_make_leak_sensor_records_returns_primary_plus_heartbeat() -> None:
    """Two records: primary plus heartbeat subnode 4."""
    records = make_leak_sensor_records()
    assert len(records) == 2
    primary = next(r for r in records.values() if r.pnode == r.address)
    heartbeat = next(r for r in records.values() if r.pnode != r.address)
    assert primary.type.startswith("16.8.")
    assert "sensor" in primary.name.lower(), "name must trip the consumer override"
    assert heartbeat.pnode == primary.address
    assert int(heartbeat.address.split(" ")[-1], 16) == INSTEON_BSENSOR_SUBNODE_HEARTBEAT


def test_make_door_sensor_records_returns_primary_plus_negative() -> None:
    """Door / opening: primary plus the negative-mirror subnode (id 2)."""
    records = make_door_sensor_records()
    primary = next(r for r in records.values() if r.pnode == r.address)
    negative = next(r for r in records.values() if r.pnode != r.address)
    assert primary.type.startswith("16.9.")
    assert int(negative.address.split(" ")[-1], 16) == INSTEON_BSENSOR_SUBNODE_NEGATIVE
    assert negative.pnode == primary.address


def test_make_motion_sensor_records_covers_every_documented_subnode() -> None:
    """Motion: primary + dusk/dawn + low-battery + heartbeat + tamper +
    disabled. Pin the addresses each subnode index resolves to so the
    consumer's hex-byte parse stays in sync."""
    records = make_motion_sensor_records()
    primary = next(r for r in records.values() if r.pnode == r.address)
    assert primary.type.startswith("16.1.")
    sub_ids = {
        int(r.address.split(" ")[-1], 16)
        for r in records.values()
        if r.pnode != r.address
    }
    assert sub_ids == {
        INSTEON_BSENSOR_SUBNODE_DUSK_DAWN,
        INSTEON_BSENSOR_SUBNODE_LOW_BATTERY,
        INSTEON_BSENSOR_SUBNODE_HEARTBEAT,
        INSTEON_BSENSOR_SUBNODE_TAMPER,
        INSTEON_BSENSOR_SUBNODE_DISABLED,
    }


def test_make_thermostat_binary_records_has_cool_and_heat_subnodes() -> None:
    """Insteon thermostat: primary + cool (subnode 2) + heat (subnode 3)."""
    records = make_thermostat_binary_records()
    primary = next(r for r in records.values() if r.pnode == r.address)
    assert primary.type.startswith("5.16.")
    sub_ids = {
        int(r.address.split(" ")[-1], 16)
        for r in records.values()
        if r.pnode != r.address
    }
    assert sub_ids == {
        INSTEON_THERMOSTAT_SUBNODE_COOL,
        INSTEON_THERMOSTAT_SUBNODE_HEAT,
    }


def test_make_insteon_binary_sensor_records_combines_all_families() -> None:
    """The one-call shortcut returns every family's records in one dict."""
    combined = make_insteon_binary_sensor_records()
    expected_size = (
        len(make_leak_sensor_records())
        + len(make_door_sensor_records())
        + len(make_motion_sensor_records())
        + len(make_thermostat_binary_records())
    )
    assert len(combined) == expected_size
    # Address space distinct across families — no key collisions.
    primary_types = {
        r.type
        for r in combined.values()
        if r.pnode == r.address
    }
    assert {"16.8.1.0", "16.9.1.0", "16.1.1.0", "5.16.0.0"} <= primary_types
