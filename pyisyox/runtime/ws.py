"""WebSocket reader loop for the eisy event stream.

Opens ``wss://{host}/rest/subscribe`` (or another configured path) using
the same :class:`pyisyox.auth.Auth` strategy the HTTP client uses, then
runs a read loop that feeds every frame to an
:class:`~pyisyox.runtime.events.EventDispatcher`. Reconnects with
exponential backoff on transport errors; refreshes auth tokens on a
401-class WebSocket handshake failure.

Auth integration:

* :class:`LocalAuth` returns ``{"auth": aiohttp.BasicAuth(...)}`` from
  ``request_kwargs`` — aiohttp's ``ws_connect`` accepts ``auth``
  directly, so the upgrade carries an ``Authorization: Basic`` header.
* :class:`PortalAuth` returns ``{"headers": {"Authorization": "Bearer
  ..."}}``. ``ws_connect`` passes ``headers`` through verbatim, so the
  bearer rides on the upgrade.

The loop is intentionally split from the parsing/dispatch logic in
:mod:`pyisyox.runtime.events` so the dispatcher can be unit-tested
without WebSocket plumbing and the reader can be unit-tested without a
real WS server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import aiohttp

from pyisyox.auth import AuthError
from pyisyox.constants import EventStreamStatus

if TYPE_CHECKING:
    from pyisyox.client import IoXClient
    from pyisyox.runtime.events import EventDispatcher

_LOGGER = logging.getLogger(__name__)


#: Backoff schedule applied between reconnect attempts (seconds).
#: After the last entry the reader stays at the cap (60 s).
_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)


StatusListener = Callable[[EventStreamStatus], None]


class WebSocketEventStream:
    """Background reader that feeds frames into an :class:`EventDispatcher`.

    Lifecycle:

    1. :meth:`start` schedules the read task and returns immediately.
    2. The task connects, dispatches frames, reconnects on transport
       errors, and pumps :class:`EventStreamStatus` notifications to
       any registered status listener.
    3. :meth:`stop` cancels the task and closes any active WS.

    The class deliberately keeps its surface narrow — the consumer is
    expected to be the top-level ``ISY`` glue object that owns both the
    :class:`IoXClient` and the dispatcher.
    """

    __slots__ = (
        "_backoff_idx",
        "_client",
        "_dispatcher",
        "_path",
        "_status_listeners",
        "_stop_requested",
        "_task",
        "_ws",
    )

    def __init__(
        self,
        client: IoXClient,
        dispatcher: EventDispatcher,
        path: str = "/rest/subscribe",
    ) -> None:
        """Bind to a client + dispatcher.

        Args:
            client: The HTTP client whose session and auth strategy
                drive the WS upgrade. The reader does not own the
                session lifecycle — caller (typically the ISY glue)
                does.
            dispatcher: Where parsed frames flow.
            path: WS path. Default is ``/rest/subscribe`` (works under
                both auth modes). ``/api/events/subscribe`` is
                opt-in for portal mode and requires sending a
                ``{"auth": {"token": ...}}`` initial frame; that path
                is not supported here yet.
        """
        self._client = client
        self._dispatcher = dispatcher
        self._path = path
        self._task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop_requested = False
        self._backoff_idx = 0
        self._status_listeners: list[StatusListener] = []

    # --- public API ----------------------------------------------------

    def add_status_listener(self, callback: StatusListener) -> Callable[[], None]:
        """Register a callback for stream-status changes.

        Returns:
            An unsubscribe function.
        """
        self._status_listeners.append(callback)

        def _unsubscribe() -> None:
            try:
                self._status_listeners.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def start(self) -> asyncio.Task[None]:
        """Start the background read loop. Idempotent — calling twice
        returns the existing task."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_requested = False
        self._backoff_idx = 0
        self._task = asyncio.create_task(self._run(), name="pyisyox-ws-reader")
        return self._task

    async def stop(self) -> None:
        """Stop the read loop and close any active WebSocket."""
        self._stop_requested = True
        if self._ws is not None and not self._ws.closed:
            with _suppress_aiohttp_close():
                await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with _suppress_cancellation():
                await self._task
            self._task = None
        self._notify(EventStreamStatus.DISCONNECTED)

    # --- read loop -----------------------------------------------------

    async def _run(self) -> None:
        """Connect, read, reconnect — the main coroutine."""
        try:
            while not self._stop_requested:
                self._notify(EventStreamStatus.INITIALIZING)
                try:
                    await self._connect_and_read()
                except AuthError:
                    _LOGGER.warning("WS auth failed and could not be recovered; giving up")
                    self._notify(EventStreamStatus.RECONNECT_FAILED)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("WS read loop hit unexpected error; will reconnect")
                # Read into a local so mypy doesn't narrow `_stop_requested`
                # to its loop-entry value across the await suspension above.
                stopping: bool = self._stop_requested
                if stopping:
                    break
                self._notify(EventStreamStatus.LOST_CONNECTION)
                await self._sleep_with_backoff()
                self._notify(EventStreamStatus.RECONNECTING)
        except asyncio.CancelledError:
            # stop() requested; falls through to the DISCONNECTED notify
            # in stop() itself.
            pass

    async def _connect_and_read(self) -> None:
        """One connect-read cycle. Returns when the WS closes."""
        url = self._ws_url()
        kwargs = await self._auth_kwargs()
        try:
            self._ws = await self._client.session.ws_connect(url, **kwargs)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                # Auth-side recovery (token refresh / re-login) and one retry.
                if not await self._client.auth.handle_unauthorized(
                    self._client.session, self._client.base_url
                ):
                    raise AuthError(f"WS auth could not recover from 401 on {url}") from exc
                kwargs = await self._auth_kwargs()
                self._ws = await self._client.session.ws_connect(url, **kwargs)
            else:
                raise

        try:
            self._backoff_idx = 0  # successful connect resets backoff
            self._notify(EventStreamStatus.CONNECTED)
            async for msg in self._ws:
                if self._stop_requested:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatcher.feed(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning("WS message error: %s", self._ws.exception())
                    break
                # BINARY/PING/PONG — ignore.
        finally:
            current = self._ws
            self._ws = None
            if current is not None and not current.closed:
                with _suppress_aiohttp_close():
                    await current.close()

    async def _auth_kwargs(self) -> dict:
        """Authenticate (if not already) and gather aiohttp kwargs."""
        await self._client._authenticate_once()
        return await self._client.auth.request_kwargs(self._client.session, self._client.base_url)

    def _ws_url(self) -> str:
        """Translate the HTTP base URL into the ``wss://`` form."""
        base = self._client.base_url
        if base.startswith("https://"):
            return f"wss://{base[len('https://') :]}{self._path}"
        if base.startswith("http://"):
            return f"ws://{base[len('http://') :]}{self._path}"
        return base + self._path

    async def _sleep_with_backoff(self) -> None:
        """Wait between reconnects, advancing through ``_BACKOFF_SCHEDULE``."""
        delay = _BACKOFF_SCHEDULE[min(self._backoff_idx, len(_BACKOFF_SCHEDULE) - 1)]
        self._backoff_idx += 1
        _LOGGER.debug("WS reconnect in %.1fs (attempt %d)", delay, self._backoff_idx)
        await asyncio.sleep(delay)

    def _notify(self, status: EventStreamStatus) -> None:
        """Fan a status update out to listeners; suppress listener errors."""
        for listener in tuple(self._status_listeners):
            try:
                listener(status)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("WS status listener raised; suppressing")


# --- small context-manager helpers ---------------------------------------


class _suppress_aiohttp_close:
    """Swallow exceptions from a defensive ``ws.close()``.

    Closing an already-closed WS is a no-op in current aiohttp, but
    older versions raise; suppressing keeps the cleanup path safe
    across versions without needing a precise version pin.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)  # type: ignore[arg-type]


class _suppress_cancellation:
    """Swallow ``asyncio.CancelledError`` from awaiting a cancelled task."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is asyncio.CancelledError
