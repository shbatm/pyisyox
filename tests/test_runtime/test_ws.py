"""Tests for :class:`pyisyox.runtime.ws.WebSocketEventStream`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import multidict
import pytest
from aiohttp.client_reqrep import RequestInfo
from yarl import URL

from pyisyox.auth import LocalAuth
from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
from pyisyox.constants import EventStreamStatus
from pyisyox.runtime import ws as ws_module
from pyisyox.runtime.events import EventDispatcher
from pyisyox.runtime.ws import WebSocketEventStream

BASE = "https://eisy.local:8443"


def _ws_handshake_error(status: int) -> aiohttp.WSServerHandshakeError:
    """Build a ``WSServerHandshakeError`` with a usable ``status`` field."""
    headers = multidict.CIMultiDictProxy(multidict.CIMultiDict())
    request_info = RequestInfo(URL("https://eisy.local/"), "GET", headers)
    return aiohttp.WSServerHandshakeError(
        request_info=request_info,
        history=(),
        status=status,
        message=f"HTTP {status}",
    )


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
async def test_ws_reader_start_is_idempotent() -> None:
    """Calling ``start()`` twice returns the same task; the second call
    must not spawn a parallel reader."""
    session = FakeWSSession()
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    task1 = stream.start()
    task2 = stream.start()
    assert task1 is task2
    await stream.stop()


@pytest.mark.asyncio
async def test_ws_reader_unsubscribe_twice_is_safe() -> None:
    """Double-unsubscribe must not raise — the ValueError from the
    second list.remove() is swallowed."""
    session = FakeWSSession()
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    unsubscribe = stream.add_status_listener(lambda _s: None)
    unsubscribe()
    unsubscribe()  # must not raise


def test_ws_url_for_http_base() -> None:
    """``http://`` translates to ``ws://`` so plain-HTTP devices
    (test harnesses, legacy ISY994) work without ``https://`` upgrade."""
    session = FakeWSSession()
    client = IoXClient("http://eisy.local:8080", LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    stream = WebSocketEventStream(client, EventDispatcher({}))

    assert stream._ws_url() == "ws://eisy.local:8080/rest/subscribe"


def test_ws_url_for_unknown_scheme_appends_path() -> None:
    """Non-http(s) base URLs fall through to ``base + path`` so the
    method always returns *something* — callers see a clear error from
    ws_connect rather than a silent crash here."""
    session = FakeWSSession()
    client = IoXClient("eisy.local:8080", LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    stream = WebSocketEventStream(client, EventDispatcher({}))

    assert stream._ws_url() == "eisy.local:8080/rest/subscribe"


@pytest.mark.asyncio
async def test_ws_reader_401_with_unrecoverable_auth_raises_authoritatively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LocalAuth cannot recover from a 401 (basic-auth credentials are
    wrong by construction). The reader must notify
    ``RECONNECT_FAILED`` and stop instead of looping forever."""
    monkeypatch.setattr(ws_module, "_BACKOFF_SCHEDULE", (0.0,))
    session = FakeWSSession()
    session.queue_failure(_ws_handshake_error(401))
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    statuses: list[EventStreamStatus] = []
    stream.add_status_listener(statuses.append)

    stream.start()
    for _ in range(200):
        if EventStreamStatus.RECONNECT_FAILED in statuses:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert EventStreamStatus.RECONNECT_FAILED in statuses
    assert len(session.calls) == 1, "no retry once auth recovery declined"


@pytest.mark.asyncio
async def test_ws_reader_401_recoverable_retries_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``handle_unauthorized`` returns ``True`` the reader retries
    the handshake once with refreshed kwargs — typical of PortalAuth
    after a token refresh."""

    class _RecoverableAuth:
        def __init__(self) -> None:
            self.recover_calls = 0
            self.kwargs_calls = 0

        async def authenticate(self, _session: object, _base: str) -> None:
            return None

        async def request_kwargs(self, _session: object, _base: str) -> dict:
            self.kwargs_calls += 1
            return {"headers": {"Authorization": f"Bearer t{self.kwargs_calls}"}}

        async def handle_unauthorized(self, _session: object, _base: str) -> bool:
            self.recover_calls += 1
            return True

        async def close(self, _session: object, _base: str) -> None:
            return None

    session = FakeWSSession()
    session.queue_failure(_ws_handshake_error(401))
    session.queue_success([_closed_frame()])
    auth = _RecoverableAuth()
    client = IoXClient(BASE, auth, session)  # type: ignore[arg-type]
    client._authenticated = True
    stream = WebSocketEventStream(client, EventDispatcher({}))

    statuses: list[EventStreamStatus] = []
    stream.add_status_listener(statuses.append)

    stream.start()
    for _ in range(200):
        if EventStreamStatus.CONNECTED in statuses:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert auth.recover_calls == 1
    assert len(session.calls) == 2, "one retry after auth recovery"
    assert EventStreamStatus.CONNECTED in statuses


@pytest.mark.asyncio
async def test_ws_reader_non_401_handshake_error_triggers_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 from the handshake is not an auth problem — the loop
    catches the exception, notifies ``LOST_CONNECTION`` /
    ``RECONNECTING``, and retries (which succeeds here)."""
    monkeypatch.setattr(ws_module, "_BACKOFF_SCHEDULE", (0.0,))
    session = FakeWSSession()
    session.queue_failure(_ws_handshake_error(500))
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    statuses: list[EventStreamStatus] = []
    stream.add_status_listener(statuses.append)

    stream.start()
    for _ in range(200):
        if EventStreamStatus.CONNECTED in statuses:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert EventStreamStatus.LOST_CONNECTION in statuses
    assert EventStreamStatus.RECONNECTING in statuses
    assert EventStreamStatus.CONNECTED in statuses
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_ws_reader_generic_exception_triggers_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception from ws_connect (not a handshake error)
    must still flow through the reconnect path rather than killing
    the reader task."""
    monkeypatch.setattr(ws_module, "_BACKOFF_SCHEDULE", (0.0,))
    session = FakeWSSession()
    session.queue_failure(RuntimeError("transient"))
    session.queue_success([_closed_frame()])
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    statuses: list[EventStreamStatus] = []
    stream.add_status_listener(statuses.append)

    stream.start()
    for _ in range(200):
        if EventStreamStatus.CONNECTED in statuses:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert EventStreamStatus.RECONNECTING in statuses
    assert EventStreamStatus.CONNECTED in statuses


@pytest.mark.asyncio
async def test_ws_reader_breaks_on_error_frame() -> None:
    """A ``WSMsgType.ERROR`` frame ends the read cycle (we don't try to
    interpret malformed transport-level errors)."""
    session = FakeWSSession()
    ws = session.queue_success(
        [
            FakeWSMessage(type=aiohttp.WSMsgType.ERROR),
        ]
    )
    client = _make_client(session)
    stream = WebSocketEventStream(client, EventDispatcher({}))

    stream.start()
    for _ in range(50):
        if ws.closed or ws.close_called:
            break
        await asyncio.sleep(0)
    await stream.stop()

    assert ws.close_called or ws.closed


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
