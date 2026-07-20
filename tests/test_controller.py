"""Tests for :class:`pyisyox.controller.Controller` — the top-level glue."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from pyisyox.auth import LocalAuth, PortalAuth
from pyisyox.constants import EventStreamStatus
from pyisyox.controller import Controller, ControllerNotConnectedError
from pyisyox.runtime import ProgramCommand
from pyisyox.runtime.events import Event, EventDispatcher, NodeLifecycleAction, NodeLifecycleEvent
from pyisyox.runtime.ws import WebSocketEventStream
from tests.test_client.conftest import FakeSession as FakeHttpSession
from tests.test_runtime.test_ws import FakeWebSocket, FakeWSMessage

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "eisy6"
BASE = "https://eisy.local:8443"


# --- combined fake session (HTTP + WS) -----------------------------------


class FakeSession:
    """Stitches together the HTTP fake (from test_client) and a
    minimal ws_connect surface so the Controller's full
    connect-then-subscribe flow can run under one session object."""

    def __init__(self, base_url: str) -> None:
        self._http = FakeHttpSession(base_url)
        self._ws_responses: list[FakeWebSocket | BaseException] = []
        self.calls = self._http.calls
        self.ws_calls: list[tuple[str, dict[str, Any]]] = []

    def set_route(self, method: str, path: str, status: int, body: Any = None) -> None:
        self._http.set_route(method, path, status, body)

    def queue_ws(self, frames: list[FakeWSMessage]) -> FakeWebSocket:
        ws = FakeWebSocket(frames)
        self._ws_responses.append(ws)
        return ws

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._http.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._http.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self._http.put(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self._http.delete(url, **kwargs)

    async def ws_connect(self, url: str, **kwargs: Any) -> FakeWebSocket:
        self.ws_calls.append((url, kwargs))
        if not self._ws_responses:
            raise AssertionError(f"no scripted WS response for {url}")
        item = self._ws_responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        # Mimic aiohttp.ClientSession.close — no-op for the fake.
        return


def _profile_fixture() -> dict:
    return json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())


def _stub_responses(session: FakeSession) -> None:
    """Wire up the standard /api/* responses connect() needs."""
    session.set_route(
        "GET",
        "/api/config",
        200,
        {"successful": True, "data": {"uuid": "uuid-1", "version": "6.0.0"}},
    )
    session.set_route(
        "GET",
        "/rest/profiles?include=nodedefs,editors,linkdefs",
        200,
        _profile_fixture(),
    )
    session.set_route(
        "GET",
        "/api/nodes",
        200,
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "3D 7D 87 1",
                            "name": "Breezeway",
                            "nodeDefId": "KeypadDimmer_ADV",
                            "type": "1.65.69.0",
                            "enabled": "true",
                            "pnode": "3D 7D 87 1",
                            "property": [{"id": "ST", "value": "0", "formatted": "Off", "uom": "100"}],
                        }
                    ]
                }
            }
        },
    )
    session.set_route(
        "GET",
        "/rest/status",
        200,
        '<?xml version="1.0"?><nodes></nodes>',
    )
    session.set_route("GET", "/api/programs", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/triggers", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/variables/1", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/variables/2", 200, {"successful": True, "data": []})
    session.set_route(
        "GET",
        "/rest/networking/resources",
        200,
        '<?xml version="1.0"?><NetConfig/>',
    )


# --- error surfaces before connect() -------------------------------------


def test_pre_connect_property_access_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    assert controller.connected is False
    with pytest.raises(ControllerNotConnectedError):
        _ = controller.config
    with pytest.raises(ControllerNotConnectedError):
        _ = controller.profile
    with pytest.raises(ControllerNotConnectedError):
        _ = controller.nodes
    with pytest.raises(ControllerNotConnectedError):
        controller.feed_event_frame("<Event/>")


# --- connect() happy path -----------------------------------------------


@pytest.mark.asyncio
async def test_connect_populates_loadresult_and_starts_ws() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    session.queue_ws([FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)])
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]

    await controller.connect()

    assert controller.connected is True
    assert controller.config.uuid == "uuid-1"
    assert controller.config.version == "6.0.0"
    assert "3D 7D 87 1" in controller.nodes
    node = controller.nodes["3D 7D 87 1"]
    assert node.nodedef_id == "KeypadDimmer_ADV"
    assert node.nodedef is not None

    # WS upgrade is fired on the background task; pump the loop until
    # ws_connect runs.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not session.ws_calls:
        await asyncio.sleep(0)
    assert any("/rest/subscribe" in url for url, _ in session.ws_calls)

    await controller.stop()
    assert controller.connected is False


@pytest.mark.asyncio
async def test_connect_without_websocket_skips_ws_call() -> None:
    """Some consumers (CLI tools, snapshot tests) only need a one-shot
    initial load. start_websocket=False must not open a WS."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]

    await controller.connect(start_websocket=False)

    assert controller.connected is True
    assert session.ws_calls == []
    # ``websocket`` is None when the WS wasn't started — system_health
    # consumers branch on this.
    assert controller.websocket is None
    # Status listener registration requires the WS; should raise.
    with pytest.raises(ControllerNotConnectedError, match="WebSocket"):
        controller.add_status_listener(lambda _s: None)

    await controller.stop()


@pytest.mark.asyncio
async def test_websocket_status_and_connected_track_state() -> None:
    """``WebSocketEventStream.status`` mirrors the most recent
    ``EventStreamStatus``; ``connected`` is True only while in
    ``CONNECTED``. ``last_event_at`` advances when a frame arrives."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    # Construct a WS stream against the controller's client + a
    # fresh dispatcher; we don't start the read loop because we
    # want to drive status manually.
    ws = WebSocketEventStream(
        controller._client,  # type: ignore[arg-type]
        EventDispatcher(controller.nodes),
    )
    assert ws.status == EventStreamStatus.NOT_STARTED
    assert ws.connected is False
    assert ws.last_event_at is None

    ws._notify(EventStreamStatus.CONNECTED)  # type: ignore[attr-defined]
    assert ws.status == EventStreamStatus.CONNECTED
    assert ws.connected is True

    ws._notify(EventStreamStatus.LOST_CONNECTION)  # type: ignore[attr-defined]
    assert ws.connected is False
    assert ws.status == EventStreamStatus.LOST_CONNECTION

    await controller.stop()


@pytest.mark.asyncio
async def test_websocket_records_last_event_on_text_frame() -> None:
    """Each TEXT frame the read loop dispatches updates
    ``last_event_at`` to UTC now. Driven through the public
    surface by queueing a frame on the FakeWebSocket."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.queue_ws(
        [
            FakeWSMessage(
                type=aiohttp.WSMsgType.TEXT,
                data='<?xml version="1.0"?><Event seqnum="1" sid="x" timestamp="t">'
                "<control>_0</control><action>90</action><node></node>"
                "<eventInfo></eventInfo></Event>",
            ),
            FakeWSMessage(type=aiohttp.WSMsgType.CLOSED),
        ]
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and (
        controller.websocket is None or controller.websocket.last_event_at is None
    ):
        await asyncio.sleep(0)
    ws = controller.websocket
    assert ws is not None
    assert ws.last_event_at is not None

    await controller.stop()


# --- live updates --------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_event_frame_updates_node_property() -> None:
    """The dispatcher and runtime Nodes share the same NodeRecord
    registry — a frame fed in mutates the property visible via
    ``controller.nodes[address].properties``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    # Initial state from /api/nodes JSON.
    node_before = controller.nodes["3D 7D 87 1"]
    assert node_before.properties["ST"].formatted == "Off"

    event = controller.feed_event_frame(
        '<Event seqnum="1"><control>ST</control>'
        '<action uom="100">255</action>'
        "<node>3D 7D 87 1</node>"
        "<fmtAct>On</fmtAct></Event>"
    )
    assert event is not None

    # Re-fetch the runtime Node — properties dict reflects the WS update,
    # UOM-normalised to the nodedef editor's canonical unit: the frame
    # reported the raw UOM-100 byte 255, surfaced as UOM-51 100%.
    node_after = controller.nodes["3D 7D 87 1"]
    assert node_after.properties["ST"].formatted == "On"
    assert node_after.properties["ST"].value == "100"
    assert node_after.properties["ST"].uom == "51"

    await controller.stop()


@pytest.mark.asyncio
async def test_event_listener_fires_on_feed() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    received: list[Event] = []
    unsubscribe = controller.add_event_listener(received.append)

    controller.feed_event_frame(
        '<Event seqnum="1"><control>ST</control><action>1</action><node>3D 7D 87 1</node></Event>'
    )
    assert len(received) == 1
    unsubscribe()
    controller.feed_event_frame(
        '<Event seqnum="2"><control>ST</control><action>0</action><node>3D 7D 87 1</node></Event>'
    )
    assert len(received) == 1, "post-unsubscribe events must not reach the listener"

    await controller.stop()


# --- session ownership --------------------------------------------------


@pytest.mark.asyncio
async def test_externally_provided_session_not_closed() -> None:
    """When the consumer passes a session, the controller must not close
    it on stop() — HA Core shares one session across integrations."""

    class _ClosableFake(FakeSession):
        def __init__(self, base_url: str) -> None:
            super().__init__(base_url)
            self.close_called = False

        async def close(self) -> None:
            self.close_called = True

    session = _ClosableFake(BASE)
    _stub_responses(session)
    session.queue_ws([FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)])
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect()
    await controller.stop()

    assert session.close_called is False, "external session must outlive the controller"


# --- WS-driven property update ------------------------------------------


@pytest.mark.asyncio
async def test_ws_event_propagates_to_node_properties() -> None:
    """End-to-end: the WS reader receives a frame, the dispatcher applies
    it, and ``controller.nodes[address].properties`` reflects the new
    value without manual feed_event_frame intervention."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.queue_ws(
        [
            FakeWSMessage(
                type=aiohttp.WSMsgType.TEXT,
                data=(
                    '<Event seqnum="1"><control>ST</control>'
                    '<action uom="100">255</action>'
                    "<node>3D 7D 87 1</node>"
                    "<fmtAct>On</fmtAct></Event>"
                ),
            ),
            FakeWSMessage(type=aiohttp.WSMsgType.CLOSED),
        ]
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect()

    # Wait for the WS reader to drain the queued frame. asyncio.sleep(0)
    # yields enough scheduler ticks for FakeWebSocket.__anext__ to fire.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0)
        if controller.nodes["3D 7D 87 1"].properties["ST"].formatted == "On":
            break

    assert controller.nodes["3D 7D 87 1"].properties["ST"].formatted == "On"
    await controller.stop()


# --- typing / surface ---------------------------------------------------


@pytest.mark.asyncio
async def test_controller_accepts_either_auth_strategy() -> None:
    """Ensure the constructor isn't accidentally tied to one Auth subtype."""
    portal = PortalAuth("u@x", "p")
    Controller(BASE, portal)  # construct only — no network in __init__
    local = LocalAuth("admin", "p")
    Controller(BASE, local)


# --- dynamic profile reload ---------------------------------------------


@pytest.mark.asyncio
async def test_refresh_profile_merges_added_nodedef() -> None:
    """PG3 dynamic profile reload simulation: connect with one nodedef
    set, swap the /rest/profiles route to return a profile with an
    extra nodedef, call refresh_profile, verify the merge added the
    new entry without rebuilding the live Profile."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    base_profile = controller.profile
    assert base_profile.find_nodedef("flume2", "10", "10") is not None
    # After connect, swap the route to a payload that adds a new nodedef
    # in family 11. The original profile's nodedefs all stay.
    extended = {
        "families": [
            {
                "id": "11",
                "instances": [
                    {
                        "id": "11",
                        "editors": [],
                        "linkdefs": [],
                        "nodedefs": [
                            {
                                "id": "brand_new_nodedef",
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
    session.set_route("GET", "/rest/profiles?include=nodedefs,editors,linkdefs", 200, extended)

    result = await controller.refresh_profile()

    assert result.changed is True
    assert ("brand_new_nodedef", "11", "11") in result.nodedefs_added
    # The live Profile was mutated in place — same object, new entries.
    assert controller.profile is base_profile
    assert base_profile.find_nodedef("brand_new_nodedef", "11", "11") is not None
    # Original nodedefs survive.
    assert base_profile.find_nodedef("flume2", "10", "10") is not None

    await controller.stop()


@pytest.mark.asyncio
async def test_refresh_profile_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.refresh_profile()


# --- node lifecycle listener ---------------------------------------------


@pytest.mark.asyncio
async def test_node_lifecycle_listener_fires_on_node_add() -> None:
    """Feeding a lifecycle frame fans out to lifecycle listeners
    registered through the Controller surface."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    received: list[NodeLifecycleEvent] = []
    unsubscribe = controller.add_node_lifecycle_listener(received.append)

    controller.feed_event_frame(
        '<Event seqnum="1"><control>_3</control><action>ND</action>'
        "<node>n009_harmonyctrl</node>"
        '<eventInfo><node nodeDefId="HarmonyController">'
        "<address>n009_harmonyctrl</address><name>HarmonyHub</name>"
        '<family instance="9">10</family></node></eventInfo></Event>'
    )

    assert len(received) == 1
    assert received[0].action is NodeLifecycleAction.NODE_ADDED
    assert received[0].requires_reload is True

    unsubscribe()
    controller.feed_event_frame(
        '<Event seqnum="2"><control>_3</control><action>NR</action><node>X</node></Event>'
    )
    assert len(received) == 1, "post-unsubscribe events must not reach the listener"

    await controller.stop()


@pytest.mark.asyncio
async def test_add_node_lifecycle_listener_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        controller.add_node_lifecycle_listener(lambda _ev: None)


# --- refresh() ------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_replaces_node_registry_in_place() -> None:
    """Controller.refresh() re-runs the load fan-out and updates
    LoadResult.nodes in place — the dispatcher's binding stays valid."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    nodes_dict_id = id(controller._loaded.nodes)
    assert "3D 7D 87 1" in controller.nodes
    assert "extra_node" not in controller.nodes

    # Swap /api/nodes to add an extra entry, then refresh.
    session.set_route(
        "GET",
        "/api/nodes",
        200,
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "3D 7D 87 1",
                            "name": "Breezeway",
                            "nodeDefId": "KeypadDimmer_ADV",
                            "type": "1.65.69.0",
                            "enabled": "true",
                            "pnode": "3D 7D 87 1",
                            "property": [{"id": "ST", "value": "0", "formatted": "Off", "uom": "100"}],
                        },
                        {
                            "address": "extra_node",
                            "name": "Brand New",
                            "nodeDefId": "flume2",
                            "family": {"_": "10", "instance": "10"},
                            "type": "1.2.3.4",
                            "enabled": "true",
                            "pnode": "extra_node",
                        },
                    ]
                }
            }
        },
    )
    diff = await controller.refresh()

    assert "extra_node" in controller.nodes
    # Same dict object — dispatcher binding stays valid.
    assert id(controller._loaded.nodes) == nodes_dict_id
    # No profile change in this test; diff is empty.
    assert diff.changed is False

    await controller.stop()


@pytest.mark.asyncio
async def test_refresh_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.refresh()


# --- variable mutations --------------------------------------------------


@pytest.mark.asyncio
async def test_set_variable_value_posts_value_body() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/variables/2/8", 200, {"successful": True, "data": {}})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.set_variable_value(2, 8, 42)

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"value": 42}

    await controller.stop()


@pytest.mark.asyncio
async def test_set_variable_init_posts_init_body() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/variables/2/8", 200, {"successful": True, "data": {}})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.set_variable_init(2, 8, 1)

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"init": 1}

    await controller.stop()


@pytest.mark.asyncio
async def test_rename_variable_posts_name_body() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/variables/2/8", 200, {"successful": True, "data": {}})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.rename_variable(2, 8, "State_8_Renamed")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"name": "State_8_Renamed"}

    await controller.stop()


# --- Variable create / refresh / table-change auto-refresh ---------------


@pytest.mark.asyncio
async def test_create_variable_inserts_record_and_returns_wrapper() -> None:
    """``Controller.create_variable`` PUTs the request, parses the new
    id from the response, inserts a :class:`VariableRecord` into the
    loaded registry in place, and returns a typed :class:`Variable`."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "PUT",
        "/api/variables/1",
        200,
        {"successful": True, "data": {"id": "3", "name": "New Int Var", "prec": 1}},
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        before_bucket = controller._loaded.variables["1"]  # type: ignore[union-attr]
        wrapper = await controller.create_variable(1, "New Int Var", prec=1)

        assert wrapper.type_id == "1"
        assert wrapper.id == "3"
        assert wrapper.name == "New Int Var"
        assert wrapper.precision == 1
        # PUT silently drops init/value — wrapper reflects 0/0 even
        # though the request didn't send them in the first place.
        assert wrapper.value == 0
        assert wrapper.init == 0
        # Same dict object — dispatcher binding survives.
        assert controller._loaded.variables["1"] is before_bucket  # type: ignore[union-attr]
        assert before_bucket["3"].name == "New Int Var"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_create_variable_raises_when_response_missing_id() -> None:
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "PUT",
        "/api/variables/1",
        200,
        {"successful": True, "data": {"name": "X"}},  # no id
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        with pytest.raises(Exception, match="missing id"):
            await controller.create_variable(1, "X")
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_create_variable_raises_when_response_missing_data() -> None:
    """Defensive: a controller that returns ``{successful: true}`` with
    no ``data`` key at all should produce a clear error rather than an
    AttributeError downstream."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("PUT", "/api/variables/1", 200, {"successful": True})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        with pytest.raises(Exception, match="missing data"):
            await controller.create_variable(1, "X")
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_refresh_variables_mutates_bucket_in_place() -> None:
    """The dispatcher holds a reference to ``loaded.variables[type]``,
    so ``refresh_variables`` must clear+update the same dict — not
    swap it. Otherwise the dispatcher would route value/init updates
    into the orphaned old bucket."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        original_bucket = controller._loaded.variables["1"]  # type: ignore[union-attr]
        # Re-script the GET with a non-empty payload; refresh re-fetches.
        session.set_route(
            "GET",
            "/api/variables/1",
            200,
            {
                "successful": True,
                "data": [{"id": "9", "name": "Just Created", "val": 0, "init": 0, "prec": 0}],
            },
        )
        await controller.refresh_variables(1)

        assert controller._loaded.variables["1"] is original_bucket  # type: ignore[union-attr]
        assert "9" in original_bucket
        assert original_bucket["9"].name == "Just Created"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_variable_table_changed_triggers_auto_refresh() -> None:
    """End-to-end: feed a synthetic ``_1``/``9`` frame and assert the
    auto-wired listener re-fetches ``/api/variables/{type}`` and
    overlays the result onto the live registry. Issue #125 acceptance
    criterion — proves the precision-change path lands without an
    explicit consumer call."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        bucket = controller._loaded.variables["2"]  # type: ignore[union-attr]
        assert bucket == {}

        # Re-script the GET so the auto-refresh sees a new variable
        # appear; this stands in for "controller emitted a TABLE_CHANGED
        # because something happened on the wire that the listener
        # should react to".
        session.set_route(
            "GET",
            "/api/variables/2",
            200,
            {
                "successful": True,
                "data": [
                    {"id": "5", "name": "Boost Mode", "val": 1, "init": 0, "prec": 2}
                ],
            },
        )

        controller.feed_event_frame(
            "<?xml version='1.0'?>"
            "<Event>"
            "<control>_1</control>"
            "<action>9</action>"
            "<node></node>"
            "<eventInfo><var type=\"2\"/></eventInfo>"
            "</Event>"
        )

        # _on_variable_table_changed scheduled refresh as a Task; let
        # the loop run a tick so it can complete.
        for _ in range(5):
            if "5" in bucket:
                break
            await asyncio.sleep(0)

        assert "5" in bucket
        assert bucket["5"].precision == 2
        assert bucket["5"].name == "Boost Mode"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_auto_refresh_logs_when_get_fails(caplog: pytest.LogCaptureFixture) -> None:
    """If the auto-refresh GET fails after a TABLE_CHANGED frame, the
    failure must land in the logger — never in the dispatcher loop."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        # Re-script the GET to 500 so the auto-refresh raises.
        session.set_route("GET", "/api/variables/2", 500, "boom")
        caplog.set_level(logging.ERROR, logger="pyisyox.controller")

        controller.feed_event_frame(
            "<?xml version='1.0'?>"
            "<Event>"
            "<control>_1</control>"
            "<action>9</action>"
            "<node></node>"
            "<eventInfo><var type=\"2\"/></eventInfo>"
            "</Event>"
        )
        # Let the scheduled task run.
        for _ in range(5):
            await asyncio.sleep(0)
        assert any(
            "auto-refresh of variables[type=2] failed" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_add_variable_table_change_listener_fires(caplog: pytest.LogCaptureFixture) -> None:
    """Consumers can register their own listener on top of the
    auto-refresh — both fire on the same event."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    received: list[Any] = []
    try:
        controller.add_variable_table_change_listener(received.append)
        controller.feed_event_frame(
            "<?xml version='1.0'?>"
            "<Event>"
            "<control>_1</control>"
            "<action>9</action>"
            "<node></node>"
            "<eventInfo><var type=\"1\"/></eventInfo>"
            "</Event>"
        )
        # Give the auto-refresh task a chance to run / fail silently.
        await asyncio.sleep(0)
        assert len(received) == 1
        assert received[0].type_id == "1"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_add_variable_table_change_listener_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        controller.add_variable_table_change_listener(lambda evt: None)


@pytest.mark.asyncio
async def test_create_variable_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.create_variable(1, "x")


@pytest.mark.asyncio
async def test_refresh_variables_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.refresh_variables(1)


@pytest.mark.asyncio
async def test_rename_node_posts_name_and_type_node() -> None:
    """``rename_node`` posts ``{"name", "nodeType": "node"}`` to
    ``/api/nodes/{address}`` (URL-encoded). Wire shape verified
    against an eisy 6+ admin-UI capture; ``nodeType`` is required
    even though the address already disambiguates."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/nodes/3E%20FF%201F%204", 200, {"successful": True, "data": None})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.rename_node("3E FF 1F 4", "Test Remote - G-H - Test")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/nodes/3E%20FF%201F%204")
    assert kwargs["json"] == {
        "name": "Test Remote - G-H - Test",
        "nodeType": "node",
    }

    await controller.stop()


@pytest.mark.asyncio
async def test_rename_group_posts_name_and_type_group() -> None:
    """``rename_group`` is the scene-side variant — same endpoint,
    same address-encoding, but ``nodeType: "group"``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/nodes/12345", 200, {"successful": True, "data": None})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.rename_group("12345", "Hallway Scene")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/nodes/12345")
    assert kwargs["json"] == {"name": "Hallway Scene", "nodeType": "group"}

    await controller.stop()


@pytest.mark.asyncio
async def test_rename_folder_posts_name_and_type_folder() -> None:
    """``rename_folder`` posts ``nodeType: "folder"`` — the IoX
    admin UI distinguishes folders from groups even though both go
    through the same endpoint."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("POST", "/api/nodes/12345", 200, {"successful": True, "data": None})
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.rename_folder("12345", "Hallway")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/nodes/12345")
    assert kwargs["json"] == {"name": "Hallway", "nodeType": "folder"}

    await controller.stop()


@pytest.mark.asyncio
async def test_rename_node_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.rename_node("3E FF 1F 4", "any")


@pytest.mark.asyncio
async def test_set_variable_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.set_variable_value(2, 8, 1)


@pytest.mark.asyncio
async def test_run_network_resource_fires_legacy_endpoint() -> None:
    """``Controller.run_network_resource(5)`` issues
    ``GET /rest/networking/resources/5``. No modern ``/api/networking``
    equivalent has been observed; the legacy endpoint is the only
    supported path on both ISY-994 and IoX 6."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("GET", "/rest/networking/resources/5", 200, "<RestResponse status='200'/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.run_network_resource(5)

    method, path, _ = session.calls[-1]
    assert (method, path) == ("GET", "/rest/networking/resources/5")

    await controller.stop()


@pytest.mark.asyncio
async def test_run_network_resource_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.run_network_resource(1)


@pytest.mark.asyncio
async def test_run_network_resource_logs_debug_with_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fire-trigger calls leave a DEBUG breadcrumb — the controller
    only acknowledges receipt (the response doesn't carry the result
    of the underlying HTTP/TCP/UDP fire), so the client-side log line
    is the only evidence a user filing a bug can show that the call
    actually went out."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET", "/rest/networking/resources/5", 200, "<RestResponse status='200'/>"
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)
    try:
        with caplog.at_level(logging.DEBUG, logger="pyisyox.client"):
            await controller.run_network_resource(5)
    finally:
        await controller.stop()

    assert any(
        "Network resource fire" in msg and "/rest/networking/resources/5" in msg
        for msg in caplog.messages
    )


@pytest.mark.asyncio
async def test_network_resources_property_wraps_records() -> None:
    """``Controller.network_resources`` exposes parsed records as
    runtime ``NetworkResource`` wrappers; each one's ``run()`` fires
    the same wire endpoint as ``Controller.run_network_resource``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    # Override the default empty NetConfig with a populated payload.
    session.set_route(
        "GET",
        "/rest/networking/resources",
        200,
        '<?xml version="1.0"?><NetConfig>'
        "<NetRule><id>3</id><name>Notify</name></NetRule>"
        "<NetRule><id>7</id><name>Webhook</name></NetRule>"
        "</NetConfig>",
    )
    session.set_route("GET", "/rest/networking/resources/3", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    resources = controller.network_resources
    assert {addr: r.name for addr, r in resources.items()} == {
        "3": "Notify",
        "7": "Webhook",
    }
    # ``.address`` mirrors the dict key (string for symmetry with nodes /
    # groups). ``repr`` exposes both fields for log-line scanning.
    assert resources["3"].address == "3"
    assert repr(resources["3"]) == "NetworkResource(address='3', name='Notify')"
    await resources["3"].run()
    assert any(c[1] == "/rest/networking/resources/3" for c in session.calls)

    await controller.stop()


# --- programs -----------------------------------------------------------


def _programs_payload(*entries: dict) -> dict:
    return {"successful": True, "data": list(entries)}


@pytest.mark.asyncio
async def test_programs_property_wraps_records_and_separates_folders() -> None:
    """``Controller.programs`` exposes only programs (not folders);
    ``Controller.program_folders`` exposes only folders. Both are
    keyed on the 4-character hex id."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {"id": "0001", "name": "My Programs", "folder": True, "status": "true"},
            {
                "id": "0010",
                "name": "HA.switch",
                "folder": True,
                "status": "true",
                "parentId": "0001",
            },
            {
                "id": "0030",
                "name": "Foo Status",
                "folder": False,
                "status": "true",
                "enabled": True,
                "running": "idle",
                "parentId": "0010",
            },
        ),
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    assert set(controller.programs) == {"0030"}
    assert set(controller.program_folders) == {"0001", "0010"}

    program = controller.programs["0030"]
    assert program.name == "Foo Status"
    assert program.path == "HA.switch/Foo Status"
    assert program.status is True
    assert program.enabled is True

    await controller.stop()


@pytest.mark.asyncio
async def test_send_program_command_targets_legacy_endpoint() -> None:
    """``Controller.send_program_command(id, "runThen")`` issues
    ``GET /rest/programs/{id}/runThen``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("GET", "/rest/programs/0030/runThen", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.send_program_command("0030", "runThen")

    method, path, _ = session.calls[-1]
    assert (method, path) == ("GET", "/rest/programs/0030/runThen")

    await controller.stop()


@pytest.mark.asyncio
async def test_send_program_command_accepts_program_command_enum() -> None:
    """``Controller.send_program_command(id, ProgramCommand.RUN_THEN)``
    works equivalently to passing the bare string. The StrEnum members
    are themselves strings, so URL formatting keeps the wire value."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route("GET", "/rest/programs/0030/runThen", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.send_program_command("0030", ProgramCommand.RUN_THEN)

    _, path, _ = session.calls[-1]
    assert path == "/rest/programs/0030/runThen"
    await controller.stop()


@pytest.mark.asyncio
async def test_program_run_then_routes_through_wrapper() -> None:
    """``Program.run_then()`` is the ergonomic equivalent of
    ``Controller.send_program_command(id, "runThen")``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {
                "id": "0030",
                "name": "Foo",
                "folder": False,
                "status": "true",
                "enabled": True,
            },
        ),
    )
    session.set_route("GET", "/rest/programs/0030/runThen", 200, "<ok/>")
    session.set_route("GET", "/rest/programs/0030/runElse", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await controller.programs["0030"].run_then()
    await controller.programs["0030"].run_else()

    paths = [p for _, p, _ in session.calls if p.startswith("/rest/programs/")]
    assert paths == [
        "/rest/programs/0030/runThen",
        "/rest/programs/0030/runElse",
    ]

    await controller.stop()


_PROGRAM_VERBS = [
    ("run", "run"),
    ("stop", "stop"),
    ("enable", "enable"),
    ("disable", "disable"),
    ("run_then", "runThen"),
    ("run_else", "runElse"),
    ("run_if", "runIf"),
    ("enable_run_at_startup", "enableRunAtStartup"),
    ("disable_run_at_startup", "disableRunAtStartup"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method_name", "wire_verb"), _PROGRAM_VERBS)
async def test_program_command_wrappers_hit_legacy_endpoint(method_name: str, wire_verb: str) -> None:
    """Every command method on ``Program`` issues
    ``GET /rest/programs/{id}/{wire_verb}``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {
                "id": "0030",
                "name": "Foo",
                "folder": False,
                "status": "true",
                "enabled": True,
            },
        ),
    )
    session.set_route("GET", f"/rest/programs/0030/{wire_verb}", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await getattr(controller.programs["0030"], method_name)()

    method, path, _ = session.calls[-1]
    assert (method, path) == ("GET", f"/rest/programs/0030/{wire_verb}")
    await controller.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "wire_verb"),
    [("run", "run"), ("stop", "stop"), ("enable", "enable"), ("disable", "disable")],
)
async def test_program_folder_command_wrappers_hit_legacy_endpoint(method_name: str, wire_verb: str) -> None:
    """``ProgramFolder`` only carries the four shared verbs; each
    routes to ``GET /rest/programs/{folder_id}/{wire_verb}``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {"id": "0001", "name": "My Programs", "folder": True, "status": "true"},
            {
                "id": "0010",
                "name": "HA.switch",
                "folder": True,
                "status": "true",
                "parentId": "0001",
            },
        ),
    )
    session.set_route("GET", f"/rest/programs/0010/{wire_verb}", 200, "<ok/>")
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    await getattr(controller.program_folders["0010"], method_name)()

    method, path, _ = session.calls[-1]
    assert (method, path) == ("GET", f"/rest/programs/0010/{wire_verb}")
    await controller.stop()


@pytest.mark.asyncio
async def test_program_record_fields_surface_via_properties() -> None:
    """Every ``ProgramRecord`` field is exposed through the
    corresponding ``Program`` / ``ProgramFolder`` property, and
    both types render a useful ``repr``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {"id": "0001", "name": "My Programs", "folder": True, "status": "true"},
            {
                "id": "0010",
                "name": "HA.switch",
                "folder": True,
                "status": "true",
                "parentId": "0001",
            },
            {
                "id": "0030",
                "name": "Foo Status",
                "folder": False,
                "status": "true",
                "enabled": True,
                "runAtStartup": False,
                "running": "running then",
                "lastRunTime": "2026-05-10T14:49:53.000Z",
                "lastFinishTime": "2026-05-10T14:49:54.000Z",
                "nextScheduledRunTime": "2026-05-10T15:00:00.000Z",
                "parentId": "0010",
            },
        ),
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    program = controller.programs["0030"]
    assert program.address == "0030"
    assert program.parent_address == "0010"
    assert program.run_at_startup is False
    assert program.running == "running then"
    assert program.last_run_time == datetime(2026, 5, 10, 14, 49, 53, tzinfo=UTC)
    assert program.last_finish_time == datetime(2026, 5, 10, 14, 49, 54, tzinfo=UTC)
    assert program.next_scheduled_run_time == datetime(2026, 5, 10, 15, 0, 0, tzinfo=UTC)
    assert repr(program) == (
        "Program(address='0030', name='Foo Status', path='HA.switch/Foo Status', status=True)"
    )

    folder = controller.program_folders["0010"]
    assert folder.address == "0010"
    assert folder.parent_address == "0001"
    assert repr(folder) == "ProgramFolder(address='0010', name='HA.switch', path='HA.switch')"

    await controller.stop()


@pytest.mark.asyncio
async def test_send_program_command_before_connect_raises() -> None:
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError):
        await controller.send_program_command("0030", "run")


@pytest.mark.asyncio
async def test_program_status_event_updates_record_in_place() -> None:
    """Feeding a ``<control>_1</control>`` action ``"0"`` frame with a
    known program id mutates the matching record (status from the
    ``<s>`` eval-state nibble; enabled from ``<on/>`` / ``<off/>``;
    run-at-reboot from ``<rr/>`` / ``<nr/>``; last-run timestamps
    from ``<r>`` / ``<f>``) and fires any registered
    ``add_program_status_listener`` callbacks."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {
                "id": "008D",
                "name": "Foo",
                "folder": False,
                "status": "true",
                "enabled": False,
                "runAtStartup": False,
                "running": "idle",
            },
        ),
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    received: list = []
    controller.add_program_status_listener(received.append)

    # Wire frame: <id>8D</id> (unpadded), <on/> = enabled True,
    # <rr/> = run-at-reboot True, <s>31</s> = eval FALSE (status flips
    # False), <r>/<f> = last-run timestamps in controller-local time
    # (the test suite forces TZ=UTC so the conversion round-trips
    # cleanly).
    frame = (
        '<?xml version="1.0"?><Event seqnum="9" sid="x" timestamp="t">'
        "<control>_1</control><action>0</action><node></node>"
        "<eventInfo><id>8D</id><on /><rr /><r>260506 14:30:36 </r>"
        "<f>260506 14:31:42 </f><s>31</s></eventInfo></Event>"
    )
    controller.feed_event_frame(frame)

    assert received and received[0].address == "008D"
    # `<s>31</s>` → eval FALSE → status flips False (was True).
    assert received[0].status is False
    assert received[0].running == 0x31
    assert received[0].enabled is True
    assert received[0].run_at_startup is True
    # Record mutated in place — Program wrapper sees the new state.
    program = controller.programs["008D"]
    assert program.status is False
    assert program.enabled is True
    assert program.run_at_startup is True
    assert program.last_run_time == datetime(2026, 5, 6, 14, 30, 36, tzinfo=UTC)
    assert program.last_finish_time == datetime(2026, 5, 6, 14, 31, 42, tzinfo=UTC)

    await controller.stop()


@pytest.mark.asyncio
async def test_program_status_event_resolves_against_decimal_json_id() -> None:
    """A program loaded from a decimal JSON id (#193) is registered
    under its upconverted hex key; the WS dispatcher's unpadded-hex
    ``<id>8D</id>`` frame must still resolve against it."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {"id": 141, "name": "Foo", "folder": False, "status": "true", "enabled": False},
        ),
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    assert "008D" in controller.programs

    frame = (
        '<?xml version="1.0"?><Event seqnum="9" sid="x" timestamp="t">'
        "<control>_1</control><action>0</action><node></node>"
        "<eventInfo><id>8D</id><on /><s>21</s></eventInfo></Event>"
    )
    controller.feed_event_frame(frame)

    assert controller.programs["008D"].status is True

    await controller.stop()


@pytest.mark.asyncio
async def test_program_status_event_off_marker_flips_enabled_false() -> None:
    """``<off/>`` is the enabled-flag, not status. ``<s>21</s>`` carries
    eval-state TRUE so status flips True regardless of the on/off marker."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.set_route(
        "GET",
        "/api/programs",
        200,
        _programs_payload(
            {
                "id": "0011",
                "name": "Bar",
                "folder": False,
                "status": "false",
                "enabled": True,
            },
        ),
    )
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    frame = (
        '<?xml version="1.0"?><Event seqnum="1" sid="x" timestamp="t">'
        "<control>_1</control><action>0</action><node></node>"
        "<eventInfo><id>11</id><off /><s>21</s></eventInfo></Event>"
    )
    controller.feed_event_frame(frame)

    program = controller.programs["0011"]
    assert program.enabled is False, "<off/> sets enabled = False"
    assert program.status is True, "<s>21 → eval TRUE → status flips True"
    await controller.stop()


@pytest.mark.asyncio
async def test_program_status_event_for_unknown_id_drops_silently() -> None:
    """A frame with an id that isn't in the registry must not crash —
    just log + drop. Common during plugin reloads when the client
    hasn't refreshed yet."""
    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    frame = (
        '<?xml version="1.0"?><Event seqnum="1" sid="x" timestamp="t">'
        "<control>_1</control><action>0</action><node></node>"
        "<eventInfo><id>FFFF</id><on /></eventInfo></Event>"
    )
    # Should not raise.
    controller.feed_event_frame(frame)
    await controller.stop()


# --- coverage fills: listener pre-connect raises, property surface, owned session ---


def test_pre_connect_listener_registration_raises() -> None:
    """All listener-registration methods raise before ``connect()`` —
    they need the dispatcher / WS, which are built during connect."""
    controller = Controller(BASE, LocalAuth("admin", "p"))
    with pytest.raises(ControllerNotConnectedError, match="add_event_listener"):
        controller.add_event_listener(lambda _e: None)
    with pytest.raises(ControllerNotConnectedError, match="add_program_status_listener"):
        controller.add_program_status_listener(lambda _e: None)


@pytest.mark.asyncio
async def test_post_connect_property_surface() -> None:
    """Sweep the runtime-wrapping property accessors that aren't exercised
    elsewhere: ``base_url``, ``groups``, ``folders``, ``triggers``,
    ``variables``, plus the live-WS branch of ``add_status_listener``."""
    session = FakeSession(BASE)
    _stub_responses(session)
    session.queue_ws([FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)])
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]
    await controller.connect()

    assert controller.base_url == BASE
    # The default stubs return empty collections, but iterating still
    # forces the wrapping comprehension to run.
    assert controller.groups == {}
    assert controller.folders == {}
    assert controller.triggers == []
    assert controller.variables == {"1": {}, "2": {}}

    # WS is live (start_websocket=True by default) — registration returns
    # an unsubscribe callable rather than raising.
    unsubscribe = controller.add_status_listener(lambda _s: None)
    assert callable(unsubscribe)
    unsubscribe()

    await controller.stop()


@pytest.mark.asyncio
async def test_build_owned_session_uses_unsafe_jar_and_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_owned_session`` wires ``build_sslcontext`` into a
    TCPConnector and passes an unsafe cookie jar — the unsafe jar
    is load-bearing so cookies set on bare-IP LAN hosts survive."""
    captured: dict = {}

    def _fake_connector(ssl: object) -> str:
        captured["connector_ssl"] = ssl
        return "sentinel-connector"

    def _fake_session(**kwargs: object) -> str:
        captured["session_kwargs"] = kwargs
        return "sentinel-session"

    monkeypatch.setattr(aiohttp, "TCPConnector", _fake_connector)
    monkeypatch.setattr(aiohttp, "ClientSession", _fake_session)

    controller = Controller(BASE, LocalAuth("admin", "p"))
    result = controller._build_owned_session()

    assert result == "sentinel-session"
    kwargs = captured["session_kwargs"]
    assert kwargs["connector"] == "sentinel-connector"
    assert isinstance(kwargs["cookie_jar"], aiohttp.CookieJar)
    assert kwargs["cookie_jar"]._unsafe is True
    # https:// base → an SSL context is built and threaded into TCPConnector.
    assert captured["connector_ssl"] is not None


@pytest.mark.asyncio
async def test_owned_session_is_built_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the consumer doesn't inject a session, ``connect()`` builds
    one and ``stop()`` closes it. Patch the builder so the test doesn't
    touch real aiohttp internals."""
    fake = FakeSession(BASE)
    _stub_responses(fake)
    fake.queue_ws([FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)])

    close_calls = []

    async def _fake_close() -> None:
        close_calls.append(True)

    fake.close = _fake_close  # type: ignore[method-assign]

    monkeypatch.setattr(Controller, "_build_owned_session", lambda self: fake)
    controller = Controller(BASE, LocalAuth("admin", "p"))  # no session=...

    await controller.connect()
    await controller.stop()

    assert close_calls == [True], "owned session must be closed on stop()"


@pytest.mark.asyncio
async def test_stop_swallows_auth_close_error() -> None:
    """``auth.close()`` can raise during PortalAuth logout (network blip,
    server already invalidated the session). ``stop()`` logs and
    continues rather than letting the error escape — cleanup paths
    can't afford to propagate."""

    class _RaisingAuth:
        async def authenticate(self, _session: object, _base: str) -> None:
            return None

        async def request_kwargs(self, _session: object, _base: str) -> dict:
            return {}

        async def handle_unauthorized(self, _session: object, _base: str) -> bool:
            return False

        async def close(self, _session: object, _base: str) -> None:
            raise RuntimeError("logout failed")

    session = FakeSession(BASE)
    _stub_responses(session)
    controller = Controller(BASE, _RaisingAuth(), session=session)  # type: ignore[arg-type]
    await controller.connect(start_websocket=False)

    # Must not raise.
    await controller.stop()
    assert controller.connected is False
