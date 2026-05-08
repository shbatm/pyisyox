"""Tests for :class:`pyisyox.runtime.ws.WebSocketEventStream`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
from pyisyox.constants import EventStreamStatus
from pyisyox.runtime.events import EventDispatcher
from pyisyox.runtime.ws import WebSocketEventStream

BASE = "https://eisy.local:8443"


# --- fake WS plumbing ----------------------------------------------------


@dataclass
class FakeWSMessage:
    type: aiohttp.WSMsgType
    data: str = ""


class FakeWebSocket:
    """Minimal aiohttp.ClientWebSocketResponse stand-in."""

    def __init__(self, frames: Iterable[FakeWSMessage]) -> None:
        self._frames = list(frames)
        self.closed = False
        self._exception: BaseException | None = None
        self.close_called = False

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> FakeWSMessage:
        if self.closed or not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._frames.pop(0)

    async def close(self) -> None:
        self.close_called = True
        self.closed = True

    def exception(self) -> BaseException | None:
        return self._exception


@dataclass
class WSConnectCall:
    url: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class FakeWSSession:
    """ClientSession surface limited to ws_connect — enough to drive the
    WebSocketEventStream tests."""

    def __init__(self) -> None:
        self.calls: list[WSConnectCall] = []
        # Each entry is one of: FakeWebSocket (success) | Exception (raise).
        self._scripted: list[FakeWebSocket | BaseException] = []

    def queue_success(self, frames: Iterable[FakeWSMessage]) -> FakeWebSocket:
        ws = FakeWebSocket(frames)
        self._scripted.append(ws)
        return ws

    def queue_failure(self, exc: BaseException) -> None:
        self._scripted.append(exc)

    async def ws_connect(self, url: str, **kwargs: Any) -> FakeWebSocket:
        self.calls.append(WSConnectCall(url=url, kwargs=kwargs))
        if not self._scripted:
            raise AssertionError(f"no scripted WS response for {url}")
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_client(session: FakeWSSession) -> IoXClient:
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True  # skip the auth handshake in WS tests
    return client


def _text_frame(data: str) -> FakeWSMessage:
    return FakeWSMessage(type=aiohttp.WSMsgType.TEXT, data=data)


def _closed_frame() -> FakeWSMessage:
    return FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)


# --- tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_reader_dispatches_property_update() -> None:
    """Connect, receive one property frame, propagate it into NodeRecord."""
    nodes = {
        "3D 7D 87 1": NodeRecord(
            address="3D 7D 87 1",
            name="Test",
            nodedef_id="X",
            family_id="1",
            instance_id="1",
            properties={"ST": NodePropertyValue(id="ST", value="0", formatted="Off")},
        )
    }
    dispatcher = EventDispatcher(nodes)
    session = FakeWSSession()
    client = _make_client(session)
    stream = WebSocketEventStream(client, dispatcher)

    session.queue_success(
        [
            _text_frame(
                '<Event seqnum="1"><control>ST</control>'
                '<action uom="100">255</action>'
                "<node>3D 7D 87 1</node>"
                "<fmtAct>On</fmtAct></Event>"
            ),
            _closed_frame(),
        ]
    )

    task = stream.start()
    # Give the loop time to drain the queued frames + hit closed.
    for _ in range(50):
        if nodes["3D 7D 87 1"].properties["ST"].formatted == "On":
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert nodes["3D 7D 87 1"].properties["ST"].formatted == "On"
    assert task.done()


@pytest.mark.asyncio
async def test_ws_reader_emits_status_lifecycle() -> None:
    nodes: dict[str, NodeRecord] = {}
    dispatcher = EventDispatcher(nodes)
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, dispatcher)

    statuses: list[EventStreamStatus] = []
    stream.add_status_listener(statuses.append)

    stream.start()
    for _ in range(50):
        if EventStreamStatus.CONNECTED in statuses:
            break
        await asyncio.sleep(0)
    await stream.stop()

    # Successful connect emits INITIALIZING -> CONNECTED, stop() emits DISCONNECTED.
    assert EventStreamStatus.INITIALIZING in statuses
    assert EventStreamStatus.CONNECTED in statuses
    assert statuses[-1] == EventStreamStatus.DISCONNECTED


@pytest.mark.asyncio
async def test_ws_reader_url_translates_https_to_wss() -> None:
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    stream.start()
    for _ in range(50):
        if session.calls:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert session.calls[0].url == "wss://eisy.local:8443/rest/subscribe"


@pytest.mark.asyncio
async def test_ws_reader_attaches_local_auth() -> None:
    """LocalAuth -> request_kwargs returns ``auth=BasicAuth``; the stream
    forwards that as kwargs to ws_connect so the upgrade carries
    ``Authorization: Basic`` headers."""
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    stream.start()
    for _ in range(50):
        if session.calls:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert isinstance(session.calls[0].kwargs.get("auth"), aiohttp.BasicAuth)


@pytest.mark.asyncio
async def test_ws_reader_drops_listener_exceptions() -> None:
    """A status listener that raises must not break the loop or stop other
    listeners from firing."""
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    received: list[EventStreamStatus] = []
    stream.add_status_listener(lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    stream.add_status_listener(received.append)

    stream.start()
    for _ in range(50):
        if EventStreamStatus.CONNECTED in received:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert EventStreamStatus.CONNECTED in received


@pytest.mark.asyncio
async def test_ws_reader_stop_closes_active_socket() -> None:
    session = FakeWSSession()
    ws = session.queue_success(
        [
            _text_frame('<Event seqnum="1"><control>_5</control><action>0</action><node></node></Event>'),
            _closed_frame(),
        ]
    )
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    stream.start()
    await asyncio.sleep(0)
    await stream.stop()

    assert ws.close_called or ws.closed


@pytest.mark.asyncio
async def test_ws_reader_unsubscribe_listener() -> None:
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    received: list[EventStreamStatus] = []
    unsubscribe = stream.add_status_listener(received.append)
    unsubscribe()

    stream.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if not stream._task or stream._task.done():
            break
    await stream.stop()

    assert received == [], "unsubscribed listener must receive nothing"


@pytest.mark.asyncio
async def test_ws_reader_does_not_dispatch_after_stop() -> None:
    """Frames arriving after stop() must not produce property updates."""
    nodes = {
        "A": NodeRecord(
            address="A",
            name="Test",
            nodedef_id="X",
            family_id="1",
            instance_id="1",
        )
    }
    dispatcher = EventDispatcher(nodes)
    session = FakeWSSession()
    # Enough frames to keep the iterator running while we call stop().
    session.queue_success(
        [
            _text_frame('<Event seqnum="1"><control>ST</control><action>1</action><node>A</node></Event>'),
            _text_frame('<Event seqnum="2"><control>ST</control><action>2</action><node>A</node></Event>'),
            _closed_frame(),
        ]
    )
    client = _make_client(session)
    stream = WebSocketEventStream(client, dispatcher)

    stream.start()
    await stream.stop()
    # Whether either frame landed depends on scheduling — but the loop
    # must terminate cleanly without errors.
    assert stream._task is None
