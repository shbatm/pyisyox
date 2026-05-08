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
from pyisyox.runtime.events import Event
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
