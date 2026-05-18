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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp

from pyisyox.auth import AuthError
from pyisyox.constants import EventStreamStatus
from pyisyox.logging import LOG_VERBOSE
from pyisyox.paths import SUBSCRIBE_PATH

if TYPE_CHECKING:
    from pyisyox.client import IoXClient
    from pyisyox.runtime.events import EventDispatcher

_LOGGER = logging.getLogger(__name__)


#: Backoff schedule applied between reconnect attempts (seconds).
#: After the last entry the reader stays at the cap (60 s).
_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

#: After the socket opens the controller replays every node's current
#: status. The stream stays ``SYNCING`` until that burst goes quiet for
#: ``_SYNC_QUIET_SECONDS`` (no frame), then flips to ``CONNECTED``.
#: ``_SYNC_MAX_SECONDS`` is a hard cap so a chatty controller can never
#: stall the stream in ``SYNCING`` forever. Module-level so tests can
#: monkeypatch them small.
_SYNC_QUIET_SECONDS: float = 1.0
_SYNC_MAX_SECONDS: float = 10.0


StatusListener = Callable[[EventStreamStatus], None]


class WebSocketEventStream:
    """Background reader that feeds frames into an :class:`EventDispatcher`.

    Lifecycle:

    1. :meth:`start` schedules the read task and returns immediately.
    2. The task connects, dispatches frames, reconnects on transport
       errors, and pumps :class:`EventStreamStatus` notifications to
       any registered status listener. On each connect it holds
       ``SYNCING`` (not ``CONNECTED``) until the controller's initial
       status replay drains, so consumers don't treat the replay as
       live events.
    3. :meth:`stop` cancels the task and closes any active WS.

    The class deliberately keeps its surface narrow — the consumer is
    expected to be the top-level ``ISY`` glue object that owns both the
    :class:`IoXClient` and the dispatcher.
    """

    __slots__ = (
        "_backoff_idx",
        "_client",
        "_dispatcher",
        "_frame_count",
        "_last_event_at",
        "_path",
        "_status",
        "_status_listeners",
        "_stop_requested",
        "_sync_task",
        "_task",
        "_ws",
    )

    def __init__(
        self,
        client: IoXClient,
        dispatcher: EventDispatcher,
        path: str = SUBSCRIBE_PATH,
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
        self._status: EventStreamStatus = EventStreamStatus.NOT_STARTED
        self._last_event_at: datetime | None = None
        #: Bumped on every text frame; the sync watcher samples it to
        #: tell whether the post-connect status replay has gone quiet.
        self._frame_count = 0
        self._sync_task: asyncio.Task[None] | None = None

    # --- public API ----------------------------------------------------

    @property
    def status(self) -> EventStreamStatus:
        """Most-recent stream status.

        Updated on every transition (initialise / connect /
        reconnect / disconnect / lost). Defaults to
        :attr:`EventStreamStatus.NOT_STARTED` before :meth:`start`.
        Useful for system-health pages that want a single
        readable status string without subscribing to every
        notification.
        """
        return self._status

    @property
    def connected(self) -> bool:
        """``True`` while the stream is in the ``CONNECTED`` state.

        Convenience over comparing :attr:`status` directly. Note
        that ``connected`` flipping ``False`` doesn't mean the
        reader has given up — it may be reconnecting, or in
        :attr:`EventStreamStatus.SYNCING` (socket open but the
        controller's initial status replay hasn't drained yet —
        intentionally *not* "connected" so event consumers don't
        treat the replay as live changes).
        """
        return self._status == EventStreamStatus.CONNECTED

    @property
    def last_event_at(self) -> datetime | None:
        """UTC timestamp of the most recent text frame, or ``None``
        if no frame has been received this lifetime.

        The eisy emits a heartbeat ``<control>_0</control>`` frame
        every 30 seconds even when nothing else changes, so a
        stale ``last_event_at`` (more than ~60 s ago) is a
        reasonable signal that the connection is broken even
        when the WS handshake hasn't returned an error yet.
        """
        return self._last_event_at

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
                except Exception:  # pylint: disable=broad-except
                    # asyncio.CancelledError is a BaseException, so this
                    # `except Exception` does not catch it — stop() can
                    # cancel the task cleanly without the loop swallowing
                    # the cancellation.
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
            # Socket is open, but the controller now replays every
            # node's current status — a burst of ST/DON/DOF frames
            # that are NOT live changes. Hold SYNCING until that burst
            # drains so event consumers don't fire on the replay
            # (spurious automation triggers on every connect/restart).
            self._frame_count = 0
            self._notify(EventStreamStatus.SYNCING)
            self._sync_task = asyncio.create_task(self._promote_when_quiet(), name="pyisyox-ws-sync")
            async for msg in self._ws:
                if self._stop_requested:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_event_at = datetime.now(UTC)
                    self._frame_count += 1
                    if _LOGGER.isEnabledFor(LOG_VERBOSE):
                        _LOGGER.log(LOG_VERBOSE, "WS frame: %s", msg.data)
                    self._dispatcher.feed(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning("WS message error: %s", self._ws.exception())
                    break
                # BINARY/PING/PONG — ignore.
        finally:
            if self._sync_task is not None:
                self._sync_task.cancel()
                with _suppress_cancellation():
                    await self._sync_task
                self._sync_task = None
            current = self._ws
            self._ws = None
            if current is not None and not current.closed:
                with _suppress_aiohttp_close():
                    await current.close()

    async def _promote_when_quiet(self) -> None:
        """Flip ``SYNCING`` → ``CONNECTED`` once the post-connect status
        replay goes quiet (or the hard cap elapses).

        Sampled rather than event-driven so the hot read loop stays a
        plain ``async for``: every ``_SYNC_QUIET_SECONDS`` we check
        whether any frame arrived since the last sample. The first idle
        window means the replay drained (a silent controller promotes
        after one window). ``_SYNC_MAX_SECONDS`` caps the wait so a
        perpetually chatty controller still goes live. Cancelled by
        :meth:`_connect_and_read`'s ``finally`` if the socket drops
        first, so a connection that never settles never reports
        ``CONNECTED``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SYNC_MAX_SECONDS
        seen = self._frame_count
        while not self._stop_requested:
            await asyncio.sleep(_SYNC_QUIET_SECONDS)
            # Local copy — mypy narrows ``self._stop_requested`` to its
            # loop-entry value across the await; the read loop / stop()
            # may flip it during the sleep (same idiom as ``_run``).
            stopping: bool = self._stop_requested
            if stopping:
                return
            quiet = self._frame_count == seen
            seen = self._frame_count
            if quiet or loop.time() >= deadline:
                break
        if not self._stop_requested and self._status == EventStreamStatus.SYNCING:
            self._notify(EventStreamStatus.CONNECTED)

    async def _auth_kwargs(self) -> dict:
        """Authenticate (if not already) and gather aiohttp kwargs."""
        # _authenticate_once is intentionally accessed across the client
        # boundary — the WS reader needs the same lazy-auth handshake the
        # HTTP fan-out uses, and exposing it as a public method would
        # invite consumers to call it directly.
        await self._client._authenticate_once()  # pylint: disable=protected-access
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
        """Fan a status update out to listeners; suppress listener errors.

        Logs every lifecycle transition at DEBUG (``pyisyox.runtime.ws``)
        so the connect → SYNCING → CONNECTED → reconnect sequence is
        visible in consumer debug logs without attaching a listener.
        Only real changes are logged — a status re-notified with the
        same value (e.g. ``INITIALIZING`` each reconnect attempt) is not
        repeated.
        """
        if status != self._status:
            _LOGGER.debug("WS stream status: %s -> %s", self._status, status)
        self._status = status
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
