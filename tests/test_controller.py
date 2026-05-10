"""Tests for :class:`pyisyox.controller.Controller` — the top-level glue."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from pyisyox.auth import LocalAuth, PortalAuth
from pyisyox.controller import Controller, ControllerNotConnectedError
from pyisyox.runtime.events import Event, NodeLifecycleAction, NodeLifecycleEvent
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
    session.set_route(
        "GET",
        "/rest/nodes",
        200,
        '<?xml version="1.0"?><nodes><root/></nodes>',
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
    # Status listener registration requires the WS; should raise.
    with pytest.raises(ControllerNotConnectedError, match="WebSocket"):
        controller.add_status_listener(lambda _s: None)

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

    # Re-fetch the runtime Node — properties dict reflects the WS update.
    node_after = controller.nodes["3D 7D 87 1"]
    assert node_after.properties["ST"].formatted == "On"
    assert node_after.properties["ST"].value == "255"

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
    await resources["3"].run()
    assert any(c[1] == "/rest/networking/resources/3" for c in session.calls)

    await controller.stop()
