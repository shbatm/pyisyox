"""Test builders that produce *real* pyisyox types.

For consumer test suites (and pyisyox's own tests) that need to drive
``Controller``-shaped state without an HTTP / WebSocket round trip:
record dataclass factories, a no-network ``Controller`` constructor,
event-firing helpers, and per-platform shortcuts backed by a bundled
anonymized eisy6 profile capture.

Why "real" types instead of fakes:

* the consumer's reads exercise the actual pyisyox attribute surface —
  if pyisyox renames or retypes a field, the consumer tests fail
  immediately instead of via a drifted parallel fake;
* introspection (``is_thermostat``, ``is_lock``, ``is_dimmable``,
  ``is_fan``) flows through the real classifier-on-Node path, which
  consults the resolved nodedef + editor codec from the bundled
  profile;
* the bundled profile means consumers don't ship their own — install
  pyisyox and the testing module + its data are right there.

Usage::

    from pyisyox.testing import (
        make_controller, make_load_result, make_node_record,
    )

    load = make_load_result(
        nodes={"3D 7D 87 1": make_node_record("3D 7D 87 1", "Lamp")},
    )
    controller = make_controller(load)
    # controller is a real pyisyox.Controller; .nodes / .groups /
    # .programs / .variables are populated; HTTP methods on its
    # _client are AsyncMock'd.
"""
# Reaches into Controller / IoXClient / EventDispatcher internals on
# purpose — the testing module's job is to short-circuit the
# connect()/HTTP path that would normally populate them.
# pylint: disable=protected-access

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock

from pyisyox.auth import Auth
from pyisyox.client import (
    ControllerConfig,
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
from pyisyox.runtime.events import EventDispatcher
from pyisyox.schema.editor import Editor
from pyisyox.schema.nodedef import NodeDef
from pyisyox.schema.profile import Family, Instance, Profile

DEFAULT_UUID = "aa:bb:cc:dd:ee:ff"
DEFAULT_HOST = "http://eisy.local:8080"

_PROFILE_RESOURCE = "_eisy6_profile.json"


def _read_bundled_profile() -> dict[str, Any]:
    """Decode the bundled eisy6 profile JSON via importlib.resources."""
    with resources.files(__package__).joinpath(_PROFILE_RESOURCE).open("r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    """Bundled anonymized eisy6 profile — contains the nodedefs the
    classifier resolves (DimmerLampSwitch / FanLincMotor / Thermostat /
    DoorLock / KeypadDimmer / etc.).

    Cached because the JSON blob is ~340 KB and parse cost shows up
    under pytest-xdist. Callers must not mutate the returned
    :class:`Profile`; use :func:`make_profile_with_cover_plugin` (and
    siblings) when a fresh-per-call profile is needed for grafting in
    plugin nodedefs.
    """
    return Profile.load_from_json(_read_bundled_profile())


# ---------------------------------------------------------------------------
# Record builders — wire-shape dataclasses.
# ---------------------------------------------------------------------------


def make_node_record(
    address: str,
    name: str,
    *,
    nodedef_id: str = "DimmerLampSwitch",
    family_id: str = "1",
    instance_id: str = "1",
    type_: str = "1.0.0.0",
    parent_address: str | None = None,
    pnode: str | None = None,
    enabled: bool = True,
    properties: dict[str, NodePropertyValue] | None = None,
    status_value: str = "0",
    status_uom: str = "100",
    status_formatted: str = "Off",
    status_precision: int = 0,
) -> NodeRecord:
    """Build a minimal :class:`NodeRecord`.

    ``status_*`` kwargs are a shortcut for the always-present ``ST``
    property. Override ``properties`` to take full control (e.g. plugin
    nodes that don't carry a status, or thermostat setpoint properties).

    ``pnode`` defaults to the **node's own address** when not supplied —
    that's the wire convention for Insteon device roots (the primary is
    the device itself). For sub-buttons of multi-button physicals
    (KeypadLinc, RemoteLinc, FanLinc), pass ``pnode=<primary_address>``
    explicitly. ``parent_address`` is the tree-hierarchy parent (folder
    containing the node) and is independent — leave it ``None`` unless
    you're specifically testing folder/tree behavior.
    """
    if properties is None:
        properties = {
            "ST": NodePropertyValue(
                id="ST",
                value=status_value,
                formatted=status_formatted,
                uom=status_uom,
                name="Status",
                precision=status_precision,
            ),
        }
    # Native Insteon nodes carry an ERR (comms-error counter) property
    # on the wire — consumers surface it as the diagnostic
    # ``device_communication_errors`` ("…responding") sensor. Seed it
    # for any family-1 record (default ST-only AND callers that supply
    # ``properties=``) so the diagnostic appears on every Insteon
    # fixture. Z-Wave (family "4") / plugin (family "100"+) nodes don't
    # carry ERR and intentionally skip this.
    if family_id == "1" and "ERR" not in properties:
        properties["ERR"] = NodePropertyValue(
            id="ERR",
            value="0",
            formatted="0",
            uom="0",
            name="Responding",
            precision=0,
        )
    return NodeRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        type=type_,
        parent_address=parent_address,
        pnode=pnode or address,
        enabled=enabled,
        properties=properties,
    )


def make_group_record(
    address: str,
    name: str,
    *,
    nodedef_id: str = "InsteonDimmer",
    family_id: str = "6",
    instance_id: str = "1",
    member_addresses: tuple[str, ...] = (),
    controller_addresses: tuple[str, ...] = (),
) -> GroupRecord:
    return GroupRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        member_addresses=member_addresses,
        controller_addresses=controller_addresses,
    )


def make_folder_record(address: str, name: str, *, parent_address: str | None = None) -> FolderRecord:
    return FolderRecord(address=address, name=name, parent_address=parent_address)


def make_program_folder_record(
    address: str,
    name: str,
    *,
    path: str = "",
    parent_address: str | None = None,
) -> ProgramRecord:
    """A folder-shaped :class:`ProgramRecord` (``is_folder=True``).

    Program folders live in the same ``programs`` dict as programs
    themselves — the runtime distinguishes them via ``is_folder``.
    Use this with :func:`make_load_result`'s ``program_folders=`` kwarg
    to keep folders mentally separate from programs at the test seam.

    ``path`` defaults to ``""`` (root-level folder); pass it
    explicitly with the slash-joined ancestry (excluding the synthetic
    ``"My Programs"`` root) for nested-folder tests.
    """
    return ProgramRecord(
        address=address,
        name=name,
        path=path,
        parent_address=parent_address,
        is_folder=True,
        status=False,
    )


def make_program_record(
    address: str,
    name: str,
    *,
    path: str = "",
    status: bool = False,
    enabled: bool | None = True,
    is_folder: bool = False,
    parent_address: str | None = None,
    run_at_startup: bool | None = None,
    running: str | None = None,
    last_run_time: str | None = None,
    last_finish_time: str | None = None,
    next_scheduled_run_time: str | None = None,
) -> ProgramRecord:
    """Build a :class:`ProgramRecord` for tests.

    The runtime fields (``run_at_startup`` / ``running`` / the three
    timestamps) default to ``None`` so existing call sites stay
    unchanged. Pass them explicitly when the entity under test reads
    those properties off the wrapper — e.g. consumers exposing
    ``Program.next_scheduled_run_time`` as a sensor.
    """
    return ProgramRecord(
        address=address,
        name=name,
        path=path,
        status=status,
        enabled=enabled,
        is_folder=is_folder,
        parent_address=parent_address,
        run_at_startup=run_at_startup,
        running=running,
        last_run_time=last_run_time,
        last_finish_time=last_finish_time,
        next_scheduled_run_time=next_scheduled_run_time,
    )


def make_network_resource_record(address: str, name: str) -> NetworkResourceRecord:
    return NetworkResourceRecord(address=address, name=name)


def make_variable_record(
    type_id: str,
    id_: str,
    name: str,
    *,
    value: int = 0,
    init: int = 0,
    precision: int = 0,
    ts: str = "",
) -> VariableRecord:
    return VariableRecord(
        type_id=type_id,
        id=id_,
        name=name,
        value=value,
        init=init,
        precision=precision,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# Controller wiring.
# ---------------------------------------------------------------------------


def make_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
    groups: dict[str, GroupRecord] | None = None,
    folders: dict[str, FolderRecord] | None = None,
    programs: dict[str, ProgramRecord] | None = None,
    program_folders: dict[str, ProgramRecord] | None = None,
    variables: dict[str, dict[str, VariableRecord]] | None = None,
    network_resources: dict[str, NetworkResourceRecord] | None = None,
) -> LoadResult:
    """Assemble a :class:`LoadResult` shaped like a real
    ``IoXClient.connect()`` output — but populated directly without
    HTTP.

    The profile is shared (the bundled anonymized capture) so node
    introspection (``is_thermostat`` / ``is_lock`` / ``is_dimmable``)
    and editor-codec command validation work the same way they do at
    runtime.

    ``program_folders`` is a convenience overlay onto ``programs`` —
    program folders live in the same dict at runtime (distinguished by
    ``ProgramRecord.is_folder``); passing them here keeps the test seam
    parallel to ``folders=`` for nodes. Use :func:`make_program_folder_record`
    to build the entries.
    """
    merged_programs: dict[str, ProgramRecord] = {**(programs or {}), **(program_folders or {})}
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=load_profile(),
        nodes=nodes or {},
        groups=groups or {},
        folders=folders or {},
        programs=merged_programs,
        triggers=[],
        variables=variables or {"1": {}, "2": {}},
        network_resources=network_resources or {},
    )


def make_controller(
    load_result: LoadResult,
    *,
    host: str = DEFAULT_HOST,
) -> Controller:
    """Return a real :class:`Controller` with ``_loaded`` +
    ``_dispatcher`` pre-populated — ``connect()`` is a no-op so the
    test never touches the network.

    The ``_client`` is set to a real :class:`IoXClient` shape (so
    ``isinstance(client, IoXClient)`` holds and method signatures stay
    typed); HTTP-dispatching coroutines are replaced with
    ``AsyncMock``s that succeed silently. Tests that assert on call
    shape patch / mock the client methods they care about.

    ``websocket`` stays None (matches ``start_websocket=False`` loads);
    set ``controller._ws`` directly if a test needs the WS-health rows.
    """
    auth_stub = MagicMock(spec=Auth)
    session_stub = MagicMock()
    controller = Controller(host, auth=auth_stub, session=session_stub)
    controller._loaded = load_result

    client = _build_fake_client(host, auth_stub, session_stub)
    controller._client = client
    controller._dispatcher = EventDispatcher(
        load_result.nodes,
        programs=load_result.programs,
        variables=load_result.variables,
    )
    return controller


#: Mutation methods on :class:`IoXClient` whose calls are captured by the
#: recording fake. Reads stay un-stubbed (the load already happened, and
#: there's no production code path that re-issues them at runtime against
#: the same client) so a test that accidentally trips one fails loudly
#: rather than silently returning ``None``.
_RECORDED_CLIENT_METHODS: tuple[str, ...] = (
    "send_node_command",
    "post_node_update",
    "post_variable_update",
    "create_variable",
    "delete_variable",
    "run_program_command",
    "run_network_resource",
    "set_node_enabled",
    "get_zwave_parameter",
    "set_zwave_parameter",
    "set_zwave_lock_code",
    "delete_zwave_lock_code",
)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One captured wire call on the testing fake :class:`IoXClient`.

    ``method`` is the :class:`IoXClient` method name (e.g.
    ``"run_program_command"``); ``args`` / ``kwargs`` are the argument
    tuple as the consumer code passed them. Wrapper layers above
    (:class:`Program.enable` etc.) collapse to a known ``method`` +
    ``args`` shape — asserting on this is the cleanest way for a
    consumer test to pin "the entity issued the right wire command"
    without monkey-patching the wrapper itself.
    """

    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _build_fake_client(host: str, auth: Any, session: Any) -> IoXClient:
    """A real :class:`IoXClient` with HTTP methods stubbed.

    Keeps the real class so ``isinstance(client, IoXClient)`` holds and
    method signatures stay typed. Each mutation method is replaced with
    an ``AsyncMock`` whose ``side_effect`` appends a :class:`RecordedCall`
    to ``client.calls`` — preserves the existing
    ``client.<method>.assert_awaited_once_with(...)`` style **and**
    enables wire-shape assertions via :func:`recorded_calls` /
    :func:`recorded_calls_for`. To programme a return value, set it on
    the mock the usual way: ``client.create_variable.return_value = ...``.
    """
    client = IoXClient(host, auth, session)
    client._authenticated = True

    calls: list[RecordedCall] = []
    client.calls = calls  # type: ignore[attr-defined]

    def _make_recorder(method_name: str) -> Callable[..., Awaitable[Any]]:
        async def _record(*args: Any, **kwargs: Any) -> Any:
            calls.append(RecordedCall(method_name, args, kwargs))
            # ``unittest.mock.DEFAULT`` tells the wrapping ``AsyncMock``
            # to fall through to its ``return_value`` — without this, an
            # async ``side_effect`` returning ``None`` would mask any
            # ``client.<method>.return_value = ...`` override the test
            # set up (e.g. ``create_variable`` echoing the new record).
            return DEFAULT

        return _record

    for method_name in _RECORDED_CLIENT_METHODS:
        setattr(
            client,
            method_name,
            AsyncMock(return_value=None, side_effect=_make_recorder(method_name)),
        )

    return client


def recorded_calls(controller: Controller) -> list[RecordedCall]:
    """Return every wire call captured on ``controller``'s fake client,
    in invocation order. The list is the live storage — consumers may
    ``.clear()`` between assertions when a test exercises multiple
    phases."""
    _, client = _loaded_and_client(controller)
    calls: list[RecordedCall] | None = getattr(client, "calls", None)
    assert calls is not None, "controller's client wasn't built via make_controller — no recording fake"
    return calls


def recorded_calls_for(controller: Controller, method: str) -> list[RecordedCall]:
    """Subset of :func:`recorded_calls` filtered to one client method
    name (e.g. ``"run_program_command"``)."""
    return [c for c in recorded_calls(controller) if c.method == method]


# ---------------------------------------------------------------------------
# Event firing helpers — drive listeners on a real Controller's
# dispatcher.
#
# The real ``EventDispatcher`` keeps three listener lists (events,
# lifecycle, program-status). Tests that want to assert on the
# consumer's dispatch logic synthesise ``Event`` /
# ``NodeLifecycleEvent`` / ``ProgramStatusEvent`` instances and route
# them to the dispatcher's listeners via the helpers below. We hit the
# dispatcher's internal lists directly because the public ``feed`` path
# requires raw XML frames, which would force every test to round-trip
# its synthetic events through pyisyox's parser. The shape contract is
# locked by pyisyox's own test suite — these helpers just fan the
# dataclass out to whatever listeners the consumer registered.
# ---------------------------------------------------------------------------


def _dispatcher(controller: Controller) -> EventDispatcher:
    """Resolve ``controller._dispatcher`` for the firing helpers, asserting
    it has been wired (it always is on a :func:`make_controller` instance)."""
    dispatcher = controller._dispatcher
    assert dispatcher is not None, "controller has no dispatcher — was it built via make_controller?"
    return dispatcher


def fire_event(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.Event`) to every event listener
    on ``controller``'s dispatcher."""
    for listener in tuple(_dispatcher(controller)._listeners):
        listener(event)


def fire_lifecycle(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.NodeLifecycleEvent`) to every
    lifecycle listener on ``controller``'s dispatcher."""
    for listener in tuple(_dispatcher(controller)._lifecycle_listeners):
        listener(event)


def fire_program_status(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.runtime.events.ProgramStatusEvent`)
    to every program-status listener on ``controller``'s dispatcher."""
    for listener in tuple(_dispatcher(controller)._program_status_listeners):
        listener(event)


def fire_variable_table_change(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.VariableTableChangeEvent`) to
    every variable-table-change listener on ``controller``'s
    dispatcher."""
    for listener in tuple(_dispatcher(controller)._variable_table_change_listeners):
        listener(event)


# ---------------------------------------------------------------------------
# Per-platform node shortcuts.
#
# Native introspection (``is_thermostat`` / ``is_lock`` / ``is_dimmable``
# / ``is_fan``) is derived from the resolved nodedef + editor codec on
# the bundled profile. These shortcuts pin a nodedef id that produces
# the expected classification, so consumer tests don't need to know
# pyisyox's introspection internals.
# ---------------------------------------------------------------------------

#: Nodedef ids in the bundled eisy6 profile that classify cleanly to
#: each native platform. ``RelayLampSwitch_ADV`` is the non-dimmable
#: keypad sub-button shape consumer sub-button suppression rules
#: target.
NODEDEF_FOR_PLATFORM: dict[str, str] = {
    "climate": "Thermostat",
    "lock": "DoorLock",
    "light": "DimmerLampOnly",
    "fan": "FanLincMotor",
    "switch": "RelayLampOnly",
    "subbutton": "RelayLampSwitch_ADV",
    "subdimmer": "DimmerLampSwitch_ADV",
}


# ---------------------------------------------------------------------------
# Plugin cover nodedef — synthetic, injected on demand.
#
# The bundled eisy6 profile is a real anonymized capture from a stock
# eisy 6.x; it carries no PG3 plugins. To exercise a cover-platform
# path (``pyisyox.classify`` returning ``ControllablePlatform.COVER``
# when accepts has ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` and no ``DON`` /
# ``DOF``), :func:`make_profile_with_cover_plugin` returns a fresh
# profile derived from the bundled one with a synthetic plugin family
# slot grafted in.
#
# The plugin slot id (``"100"``) deliberately stays outside the
# documented native family ids so ``Node.protocol`` returns
# ``"node_server"`` — the consumer's switch case for "defer to the
# pyisyox classifier" instead of "use native is_dimmable / is_lock /
# is_fan introspection".
# ---------------------------------------------------------------------------

PLUGIN_COVER_FAMILY_ID = "100"
PLUGIN_COVER_INSTANCE_ID = "1"
PLUGIN_COVER_NODEDEF_ID = "BlindShade"


def _build_plugin_cover_nodedef() -> NodeDef:
    """Construct a PG3-shape cover nodedef.

    Accepts ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` (and ``QUERY``) but not
    ``DON`` / ``DOF``, so the classifier picks
    ``ControllablePlatform.COVER`` rather than light / switch. One
    ``ST`` property using the standard on-level editor — enough surface
    for a consumer's cover entity to read a value off ``node.status``.
    """
    return NodeDef.from_json(
        {
            "id": PLUGIN_COVER_NODEDEF_ID,
            "nls": "blind",
            "properties": [
                {"id": "ST", "editor": "I_OL", "name": "Status"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "FDUP", "name": "Open"},
                    {"id": "FDDOWN", "name": "Close"},
                    {"id": "FDSTOP", "name": "Stop"},
                    {"id": "QUERY", "name": "Query"},
                ],
            },
        },
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
    )


def make_profile_with_cover_plugin() -> Profile:
    """Return a fresh :class:`Profile` (loaded from the bundled eisy6
    capture) with a synthetic PG3-shape cover nodedef injected under
    plugin slot ``"100"``.

    Built fresh per call — the LRU-cached :func:`load_profile` returns
    a shared instance, and we mustn't mutate it.
    """
    profile = Profile.load_from_json(_read_bundled_profile())

    nodedef = _build_plugin_cover_nodedef()
    instance = Instance(id=PLUGIN_COVER_INSTANCE_ID, name="Blind Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_COVER_FAMILY_ID, name="Blind Plugin")
    family.instances[PLUGIN_COVER_INSTANCE_ID] = instance
    profile.families[PLUGIN_COVER_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_cover_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the cover-plugin-augmented
    profile.

    Use with a cover :class:`NodeRecord` built via
    :func:`make_plugin_cover_node_record` so the classifier resolves
    the nodedef and routes the node onto a cover-shaped entity.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_cover_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_cover_node_record(
    address: str = "n100_blind1",
    name: str = "Living Room Blind",
    *,
    status_value: str = "0",
) -> NodeRecord:
    """Build a :class:`NodeRecord` shaped like a PG3 cover plugin's
    blind / shade — family slot ``"100"``, instance ``"1"``, nodedef
    ``BlindShade`` (matches :func:`_build_plugin_cover_nodedef`).
    """
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        type_="",
        status_value=status_value,
        status_uom="100",
        status_formatted="0%" if status_value == "0" else "Open",
    )


# --- plugin "hub" nodedef: no controllable, zero-arg accept verbs -----
#
# Models a PG3 controller-style node (Flume / Harmony hub shape):
# accepts a couple of zero-arg verbs (``DISCOVER`` parameterless,
# ``BEEP`` with one *optional* level param) plus the implicit
# ``QUERY``, and carries a status property. pyisyox's classifier
# returns no controllable, two ``buttons``, one reading — so a
# consumer surfaces a Query button (root scaffold) plus Discover +
# Beep buttons.

PLUGIN_HUB_FAMILY_ID = "101"
PLUGIN_HUB_INSTANCE_ID = "1"
PLUGIN_HUB_NODEDEF_ID = "PluginHub"


def _build_plugin_hub_nodedef() -> NodeDef:
    """PG3-shape hub nodedef — no ``DON`` / ``DOF`` (no controllable
    platform), zero-arg accept verbs, one ``ST`` property."""
    return NodeDef.from_json(
        {
            "id": PLUGIN_HUB_NODEDEF_ID,
            "nls": "hub",
            "properties": [
                {"id": "ST", "editor": "I_OL", "name": "Status"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "DISCOVER", "name": "Discover"},
                    {
                        "id": "BEEP",
                        "name": "Beep",
                        "parameters": [{"id": "", "editor": "I_OL", "optional": True}],
                    },
                    {"id": "QUERY", "name": "Query"},
                ],
            },
        },
        family_id=PLUGIN_HUB_FAMILY_ID,
        instance_id=PLUGIN_HUB_INSTANCE_ID,
    )


def make_profile_with_hub_plugin() -> Profile:
    """Bundled eisy6 profile with the synthetic ``PluginHub`` nodedef
    grafted under plugin slot ``"101"``. Built fresh per call (the
    cached :func:`load_profile` instance must not be mutated)."""
    profile = Profile.load_from_json(_read_bundled_profile())

    nodedef = _build_plugin_hub_nodedef()
    instance = Instance(id=PLUGIN_HUB_INSTANCE_ID, name="Hub Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_HUB_FAMILY_ID, name="Hub Plugin")
    family.instances[PLUGIN_HUB_INSTANCE_ID] = instance
    profile.families[PLUGIN_HUB_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_hub_plugin_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the hub-plugin-augmented profile.

    Use with :func:`make_plugin_hub_node_record` so the classifier
    resolves the nodedef and fans its zero-arg accepts into
    button-shaped commands.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_hub_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_hub_node_record(
    address: str = "n101_hub",
    name: str = "Plugin Hub",
    *,
    status_value: str = "0",
) -> NodeRecord:
    """A :class:`NodeRecord` shaped like a PG3 hub/controller node —
    family slot ``"101"``, instance ``"1"``, nodedef ``PluginHub``."""
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_HUB_NODEDEF_ID,
        family_id=PLUGIN_HUB_FAMILY_ID,
        instance_id=PLUGIN_HUB_INSTANCE_ID,
        type_="",
        status_value=status_value,
        status_uom="100",
        status_formatted="0%",
    )


# --- plugin "trigger source" nodedef: only cmds.sends, no controllable --
#
# Models a PG3 sensor/doorbell-style node that emits verbs but accepts
# none — pyisyox's classifier returns no controllable, no readings,
# and two ``triggers``. Consumers wire it onto an event-shape entity
# with event types derived from the sent commands' names.

PLUGIN_TRIGGER_FAMILY_ID = "102"
PLUGIN_TRIGGER_INSTANCE_ID = "1"
PLUGIN_TRIGGER_NODEDEF_ID = "PluginTriggerSource"


def _build_plugin_trigger_nodedef() -> NodeDef:
    """PG3-shape trigger-source nodedef — ``cmds.sends`` only, no
    accepts."""
    return NodeDef.from_json(
        {
            "id": PLUGIN_TRIGGER_NODEDEF_ID,
            "nls": "trigger",
            "properties": [],
            "cmds": {
                "sends": [
                    {"id": "DOORBELL_PRESS", "name": "Doorbell Press"},
                    {"id": "MOTION_ON", "name": "Motion On"},
                ],
                "accepts": [],
            },
        },
        family_id=PLUGIN_TRIGGER_FAMILY_ID,
        instance_id=PLUGIN_TRIGGER_INSTANCE_ID,
    )


def make_profile_with_trigger_plugin() -> Profile:
    """Bundled eisy6 profile with the synthetic ``PluginTriggerSource``
    nodedef grafted under plugin slot ``"102"``. Built fresh per
    call."""
    profile = Profile.load_from_json(_read_bundled_profile())

    nodedef = _build_plugin_trigger_nodedef()
    instance = Instance(id=PLUGIN_TRIGGER_INSTANCE_ID, name="Trigger Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_TRIGGER_FAMILY_ID, name="Trigger Plugin")
    family.instances[PLUGIN_TRIGGER_INSTANCE_ID] = instance
    profile.families[PLUGIN_TRIGGER_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_trigger_plugin_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the trigger-plugin-augmented
    profile.

    Use with :func:`make_plugin_trigger_node_record` so the classifier
    resolves the nodedef and consumers route the node onto an event
    entity.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_trigger_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_trigger_node_record(
    address: str = "n102_bell",
    name: str = "Front Doorbell",
) -> NodeRecord:
    """A :class:`NodeRecord` shaped like a PG3 trigger-source node —
    family slot ``"102"``, instance ``"1"``, nodedef
    ``PluginTriggerSource``, no status property."""
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_TRIGGER_NODEDEF_ID,
        family_id=PLUGIN_TRIGGER_FAMILY_ID,
        instance_id=PLUGIN_TRIGGER_INSTANCE_ID,
        type_="",
        properties={},
    )


# --- plugin "dimmer" nodedef: light controllable + editor-driven aux setters
#
# Models a PG3 dimmer that, beyond DON/DOF, accepts two parameterised
# setters whose *editors* decide the consumer's platform routing: a
# pure-enum editor (``names``, no numeric bounds) → SELECT; the
# generic ``INTEGER`` editor → NUMBER. The bundled eisy6 capture
# carries no PG3 editors, so both editors are grafted into the plugin
# instance alongside the nodedef.

PLUGIN_DIMMER_FAMILY_ID = "103"
PLUGIN_DIMMER_INSTANCE_ID = "1"
PLUGIN_DIMMER_NODEDEF_ID = "PluginDimmer"

# Pure-enum editor: ``names`` with no min/max → SELECT.
_PG_LEVEL_ENUM_EDITOR = {
    "id": "PG_LEVEL_ENUM",
    "ranges": [{"uom": "56", "names": {"0": "Low", "1": "Medium", "2": "High"}}],
}
# Generic numeric editor: editor id ``INTEGER`` → NUMBER (no UOM
# guessing).
_PG_INTEGER_EDITOR = {
    "id": "INTEGER",
    "ranges": [{"uom": "25", "prec": 0, "min": 0, "max": 1000}],
}
# Generic bool editor: editor id ``BOOL`` → SWITCH (writable).
_PG_BOOL_EDITOR = {
    "id": "BOOL",
    "ranges": [{"uom": "2", "subset": "0,1", "names": {"0": "False", "1": "True"}}],
}


def _build_plugin_dimmer_nodedef() -> NodeDef:
    """PG3-shape dimmer nodedef — ``DON``/``DOF`` (light controllable),
    a ``SETMODE`` setter on a pure-enum editor (→ SELECT) and a
    ``THRESHOLD`` setter on the ``INTEGER`` editor (→ NUMBER)."""
    return NodeDef.from_json(
        {
            "id": PLUGIN_DIMMER_NODEDEF_ID,
            "nls": "dimmer",
            "properties": [{"id": "ST", "editor": "I_OL", "name": "Status"}],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "DON", "name": "On"},
                    {"id": "DOF", "name": "Off"},
                    {"id": "BRT", "name": "Brighten"},
                    {"id": "DIM", "name": "Dim"},
                    {"id": "QUERY", "name": "Query"},
                    {
                        "id": "SETMODE",
                        "name": "Set Mode",
                        "parameters": [{"id": "", "editor": "PG_LEVEL_ENUM"}],
                    },
                    {
                        "id": "THRESHOLD",
                        "name": "Threshold",
                        "parameters": [{"id": "", "editor": "INTEGER"}],
                    },
                    {
                        "id": "INVERT",
                        "name": "Invert",
                        "parameters": [{"id": "", "editor": "BOOL"}],
                    },
                ],
            },
        },
        family_id=PLUGIN_DIMMER_FAMILY_ID,
        instance_id=PLUGIN_DIMMER_INSTANCE_ID,
    )


def make_profile_with_dimmer_plugin() -> Profile:
    """Bundled eisy6 profile with the synthetic ``PluginDimmer``
    nodedef and its two editors grafted under plugin slot ``"103"``.
    Built fresh per call (the cached :func:`load_profile` instance must
    not be mutated)."""
    profile = Profile.load_from_json(_read_bundled_profile())

    nodedef = _build_plugin_dimmer_nodedef()
    instance = Instance(id=PLUGIN_DIMMER_INSTANCE_ID, name="Dimmer Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    instance.editors["PG_LEVEL_ENUM"] = Editor.from_json(_PG_LEVEL_ENUM_EDITOR)
    instance.editors["INTEGER"] = Editor.from_json(_PG_INTEGER_EDITOR)
    instance.editors["BOOL"] = Editor.from_json(_PG_BOOL_EDITOR)
    family = Family(id=PLUGIN_DIMMER_FAMILY_ID, name="Dimmer Plugin")
    family.instances[PLUGIN_DIMMER_INSTANCE_ID] = instance
    profile.families[PLUGIN_DIMMER_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_dimmer_plugin_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the dimmer-plugin-augmented
    profile."""
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_dimmer_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_dimmer_node_record(
    address: str = "n103_lamp",
    name: str = "Studio Lamp",
    *,
    status_value: str = "0",
) -> NodeRecord:
    """A :class:`NodeRecord` shaped like a PG3 dimmer node — family
    slot ``"103"``, instance ``"1"``, nodedef ``PluginDimmer``."""
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_DIMMER_NODEDEF_ID,
        family_id=PLUGIN_DIMMER_FAMILY_ID,
        instance_id=PLUGIN_DIMMER_INSTANCE_ID,
        type_="",
        status_value=status_value,
        status_uom="100",
        status_formatted="0%",
    )


def make_classified_node_record(
    address: str,
    name: str,
    *,
    target: str,
    pnode: str | None = None,
    family_id: str | None = None,
    properties: dict[str, NodePropertyValue] | None = None,
    **status_kwargs: Any,
) -> NodeRecord:
    """Shortcut for :func:`make_node_record` that picks a real nodedef
    id for the requested target platform.

    ``target`` is one of the keys in :data:`NODEDEF_FOR_PLATFORM`. The
    family default is ``"4"`` (Z-Wave) for ``target="lock"`` and
    ``"1"`` (Insteon) for everything else. Pass ``family_id=`` to
    override (e.g. drive an Insteon lock through this shortcut).

    Pass ``pnode=<primary_address>`` for sub-buttons of multi-button
    devices (KeypadLinc, RemoteLinc, FanLinc).
    """
    if family_id is None:
        family_id = "4" if target == "lock" else "1"
    return make_node_record(
        address,
        name,
        nodedef_id=NODEDEF_FOR_PLATFORM[target],
        family_id=family_id,
        pnode=pnode,
        properties=properties,
        **status_kwargs,
    )


# ---------------------------------------------------------------------------
# High-level wrappers — load_result + controller in one call.
# ---------------------------------------------------------------------------


def _loaded_and_client(controller: Controller) -> tuple[LoadResult, IoXClient]:
    """Resolve a make_controller-built controller's loaded state + client,
    asserting both are present (always true for a make_controller instance)."""
    loaded = controller._loaded
    client = controller._client
    msg = "controller is missing _loaded / _client — was it built via make_controller?"
    assert loaded is not None, msg
    assert client is not None, msg
    return loaded, client


def make_node(record: NodeRecord, controller: Controller) -> Node:
    """Real :class:`Node` resolved against the controller's profile +
    client."""
    loaded, client = _loaded_and_client(controller)
    return Node.from_record(record, loaded.profile, client)


def make_group(
    record: GroupRecord,
    controller: Controller,
    nodes: dict[str, NodeRecord] | None = None,
) -> Group:
    """Real :class:`Group` bound to the controller's profile + client.

    Pass ``nodes`` to enable the ``group_all_on`` / ``group_any_on``
    aggregates (the real ``Group`` walks the registry on access).
    Default uses the controller's loaded node registry.
    """
    loaded, client = _loaded_and_client(controller)
    return Group.from_record(
        record,
        loaded.profile,
        client,
        nodes=nodes if nodes is not None else loaded.nodes,
    )


def make_program(record: ProgramRecord, controller: Controller) -> Program:
    _, client = _loaded_and_client(controller)
    return Program(record, client)


def make_folder(record: FolderRecord) -> Folder:
    return Folder(record)


def make_network_resource(record: NetworkResourceRecord, controller: Controller) -> NetworkResource:
    _, client = _loaded_and_client(controller)
    return NetworkResource(record, client)


def make_variable(record: VariableRecord, controller: Controller) -> Variable:
    _, client = _loaded_and_client(controller)
    return Variable.from_record(record, client)


# ---------------------------------------------------------------------------
# Insteon binary-sensor families.
#
# Real Insteon sensors (leak / door / motion / climate) use the bundled
# ``BinaryAlarm`` nodedef and pyisyox's classifier sees them as plain
# SENSORs — the hacs-udi-iox consumer routes them to ``BINARY_SENSOR``
# via two paths:
#
# 1. user-forced override (``"sensor"`` substring in the node's name);
# 2. ``binary_sensor._detect_device_type_and_class`` matching the
#    Insteon ``type`` triple's ``family.category`` prefix
#    (``16.8.*`` = leak, ``16.9.*`` = door, ``16.1.*`` = motion,
#    ``5.16.*`` = climate / thermostat).
#
# Real devices ship with a subnode tree (negative / heartbeat /
# dusk-dawn / low-battery / tamper / disabled / cool / heat) that the
# consumer's ``async_setup_entry`` enumerates by parsing the last hex
# byte of the subnode's address. These helpers produce that full tree
# so consumer tests can drive the orchestration end-to-end. ``name``
# defaults include ``"sensor"`` so the user-override route fires
# without callers having to remember.
# ---------------------------------------------------------------------------

#: Subnode address index for the "negative" status mirror on Insteon
#: leak / door sensors (firmware-dependent extra status node).
INSTEON_BSENSOR_SUBNODE_NEGATIVE = 2

#: Heartbeat subnode index — fires periodically to prove the device is
#: still alive (consumer surfaces it as its own binary_sensor).
INSTEON_BSENSOR_SUBNODE_HEARTBEAT = 4

#: Motion-sensor extras.
INSTEON_BSENSOR_SUBNODE_DUSK_DAWN = 2
INSTEON_BSENSOR_SUBNODE_LOW_BATTERY = 3
INSTEON_BSENSOR_SUBNODE_TAMPER = 10  # firmware variants also emit 16
INSTEON_BSENSOR_SUBNODE_DISABLED = 13  # firmware variants also emit 19

#: Insteon Thermostat subnodes for the cool / heat indicator sensors.
INSTEON_THERMOSTAT_SUBNODE_COOL = 2
INSTEON_THERMOSTAT_SUBNODE_HEAT = 3


def _insteon_subnode_address(primary: str, subnode: int) -> str:
    """Replace the trailing byte of a wire address with ``subnode`` (hex).

    Wire convention: ``"AA BB CC 1"`` primary → ``"AA BB CC 2"`` first
    subnode. The consumer parses the last byte with ``int(.., 16)`` so
    the value is rendered uppercase with no zero-padding (``A`` not
    ``0A``) to match how the eisy reports addresses.
    """
    head, _, _ = primary.rpartition(" ")
    if not head:
        raise ValueError(f"primary address must have a trailing byte: {primary!r}")
    return f"{head} {subnode:X}"


def _make_insteon_subnode(
    primary_address: str,
    primary_name: str,
    subnode: int,
    *,
    type_: str,
    label: str,
    status_uom: str = "2",
) -> NodeRecord:
    """Build one Insteon-binary-sensor subnode record.

    Subnodes inherit ``type_`` from the primary so the consumer's type-
    triple device-class detection sees the same family.category prefix.
    UOM ``"2"`` is the on/off binary type the consumer routes to
    BINARY_SENSOR for the aux-property fan-out path.
    """
    address = _insteon_subnode_address(primary_address, subnode)
    return make_node_record(
        address,
        f"{primary_name} {label}",
        nodedef_id="BinaryAlarm",
        type_=type_,
        pnode=primary_address,
        status_uom=status_uom,
    )


def make_leak_sensor_records(
    address: str = "30 30 30 1",
    name: str = "Garage Leak Sensor",
    *,
    type_: str = "16.8.1.0",
) -> dict[str, NodeRecord]:
    """A leak sensor primary + heartbeat subnode.

    Returns a dict keyed by address, ready to splat into
    :func:`make_load_result`. Default ``type_`` matches the
    ``MOISTURE`` device-class prefix; override per-test if exercising
    a non-default sub-category. Default name contains ``"sensor"`` so
    the consumer's user-string-match override routes the primary to
    BINARY_SENSOR without further fixture wiring.
    """
    primary = make_node_record(address, name, nodedef_id="BinaryAlarm", type_=type_, status_uom="2")
    heartbeat = _make_insteon_subnode(
        address,
        name,
        INSTEON_BSENSOR_SUBNODE_HEARTBEAT,
        type_=type_,
        label="Heartbeat",
    )
    return {primary.address: primary, heartbeat.address: heartbeat}


def make_door_sensor_records(
    address: str = "31 31 31 1",
    name: str = "Front Door Sensor",
    *,
    type_: str = "16.9.1.0",
) -> dict[str, NodeRecord]:
    """A door / opening sensor primary + negative-mirror subnode."""
    primary = make_node_record(address, name, nodedef_id="BinaryAlarm", type_=type_, status_uom="2")
    negative = _make_insteon_subnode(
        address,
        name,
        INSTEON_BSENSOR_SUBNODE_NEGATIVE,
        type_=type_,
        label="Negative",
    )
    return {primary.address: primary, negative.address: negative}


def make_motion_sensor_records(
    address: str = "32 32 32 1",
    name: str = "Hallway Motion Sensor",
    *,
    type_: str = "16.1.1.0",
) -> dict[str, NodeRecord]:
    """A motion sensor primary + every documented Insteon-motion subnode.

    Includes the dusk/dawn (light), low-battery, heartbeat, tamper, and
    disabled subnodes — exercising every branch in the consumer's
    motion-sensor sub-tree handler.
    """
    primary = make_node_record(address, name, nodedef_id="BinaryAlarm", type_=type_, status_uom="2")
    subnodes = {
        INSTEON_BSENSOR_SUBNODE_DUSK_DAWN: "Dusk/Dawn",
        INSTEON_BSENSOR_SUBNODE_LOW_BATTERY: "Low Battery",
        INSTEON_BSENSOR_SUBNODE_HEARTBEAT: "Heartbeat",
        INSTEON_BSENSOR_SUBNODE_TAMPER: "Tamper",
        INSTEON_BSENSOR_SUBNODE_DISABLED: "Disabled",
    }
    records: dict[str, NodeRecord] = {primary.address: primary}
    for sub, label in subnodes.items():
        record = _make_insteon_subnode(address, name, sub, type_=type_, label=label)
        records[record.address] = record
    return records


def make_thermostat_binary_records(
    address: str = "33 33 33 1",
    name: str = "Living Thermostat Sensor",
    *,
    type_: str = "5.16.0.0",
) -> dict[str, NodeRecord]:
    """An Insteon thermostat primary + cool / heat indicator subnodes.

    The ``"sensor"`` substring routes the primary to BINARY_SENSOR via
    the consumer's user-override; the consumer's binary_sensor
    orchestration then attaches the cool / heat subnodes as
    ``BinarySensorDeviceClass.COLD`` / ``HEAT`` indicators.
    """
    primary = make_node_record(address, name, nodedef_id="BinaryAlarm", type_=type_, status_uom="2")
    cool = _make_insteon_subnode(
        address,
        name,
        INSTEON_THERMOSTAT_SUBNODE_COOL,
        type_=type_,
        label="Cool",
    )
    heat = _make_insteon_subnode(
        address,
        name,
        INSTEON_THERMOSTAT_SUBNODE_HEAT,
        type_=type_,
        label="Heat",
    )
    return {primary.address: primary, cool.address: cool, heat.address: heat}


def make_insteon_binary_sensor_records() -> dict[str, NodeRecord]:
    """One-call shortcut: every Insteon binary-sensor family in one dict.

    Returns a single ``{address: NodeRecord}`` covering the leak / door
    / motion / thermostat fixtures with default addresses + names. Use
    when a test wants the *full* binary-sensor surface in one go;
    compose the per-family helpers when you only need one or two.
    """
    records: dict[str, NodeRecord] = {}
    records.update(make_leak_sensor_records())
    records.update(make_door_sensor_records())
    records.update(make_motion_sensor_records())
    records.update(make_thermostat_binary_records())
    return records


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_UUID",
    "INSTEON_BSENSOR_SUBNODE_DISABLED",
    "INSTEON_BSENSOR_SUBNODE_DUSK_DAWN",
    "INSTEON_BSENSOR_SUBNODE_HEARTBEAT",
    "INSTEON_BSENSOR_SUBNODE_LOW_BATTERY",
    "INSTEON_BSENSOR_SUBNODE_NEGATIVE",
    "INSTEON_BSENSOR_SUBNODE_TAMPER",
    "INSTEON_THERMOSTAT_SUBNODE_COOL",
    "INSTEON_THERMOSTAT_SUBNODE_HEAT",
    "NODEDEF_FOR_PLATFORM",
    "PLUGIN_COVER_FAMILY_ID",
    "PLUGIN_COVER_INSTANCE_ID",
    "PLUGIN_COVER_NODEDEF_ID",
    "PLUGIN_DIMMER_FAMILY_ID",
    "PLUGIN_DIMMER_INSTANCE_ID",
    "PLUGIN_DIMMER_NODEDEF_ID",
    "PLUGIN_HUB_FAMILY_ID",
    "PLUGIN_HUB_INSTANCE_ID",
    "PLUGIN_HUB_NODEDEF_ID",
    "PLUGIN_TRIGGER_FAMILY_ID",
    "PLUGIN_TRIGGER_INSTANCE_ID",
    "PLUGIN_TRIGGER_NODEDEF_ID",
    "RecordedCall",
    "fire_event",
    "fire_lifecycle",
    "fire_program_status",
    "fire_variable_table_change",
    "load_profile",
    "make_classified_node_record",
    "make_controller",
    "make_cover_load_result",
    "make_dimmer_plugin_load_result",
    "make_door_sensor_records",
    "make_folder",
    "make_folder_record",
    "make_group",
    "make_group_record",
    "make_hub_plugin_load_result",
    "make_insteon_binary_sensor_records",
    "make_leak_sensor_records",
    "make_load_result",
    "make_motion_sensor_records",
    "make_network_resource",
    "make_network_resource_record",
    "make_node",
    "make_node_record",
    "make_plugin_cover_node_record",
    "make_plugin_dimmer_node_record",
    "make_plugin_hub_node_record",
    "make_plugin_trigger_node_record",
    "make_profile_with_cover_plugin",
    "make_profile_with_dimmer_plugin",
    "make_profile_with_hub_plugin",
    "make_profile_with_trigger_plugin",
    "make_program",
    "make_program_folder_record",
    "make_program_record",
    "make_thermostat_binary_records",
    "make_trigger_plugin_load_result",
    "make_variable",
    "make_variable_record",
    "recorded_calls",
    "recorded_calls_for",
]
