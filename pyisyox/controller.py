"""High-level handle for an IoX 6+ controller.

:class:`Controller` is the single user-facing entry point that
composes the lower layers:

* :class:`pyisyox.auth.Auth` — credentials and token lifecycle.
* :class:`pyisyox.client.IoXClient` — JSON-first HTTP client with the
  initial-load orchestrator.
* :class:`pyisyox.runtime.EventDispatcher` — parses ``/rest/subscribe``
  frames and overlays property updates onto the node registry.
* :class:`pyisyox.runtime.WebSocketEventStream` — runs the WS read
  loop with auto-reconnect.
* :class:`pyisyox.runtime.Node` — user handles for individual devices,
  with editor-validated :meth:`Node.send_command`.

A typical consumer (HA Core, hacs-isy994, a CLI) constructs one
``Controller``, ``await``s :meth:`connect`, then drives nodes through
``controller.nodes[address].send_command(...)`` and subscribes to
event/status callbacks. WebSocket frames mutate
``controller.nodes[...].properties`` in place, so attribute reads
always reflect the latest controller state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp

from pyisyox.client import IoXClient
from pyisyox.runtime.events import EventDispatcher
from pyisyox.runtime.node import Node
from pyisyox.runtime.ws import WebSocketEventStream

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyisyox.auth import Auth
    from pyisyox.client import ControllerConfig, LoadResult
    from pyisyox.runtime.events import Event, EventListener
    from pyisyox.runtime.ws import StatusListener
    from pyisyox.schema.profile import Profile

_LOGGER = logging.getLogger(__name__)


class ControllerNotConnectedError(RuntimeError):
    """Raised when accessing live data before :meth:`Controller.connect`
    has populated it."""


class Controller:
    """Top-level handle for one IoX 6+ controller (eisy / Polisy).

    Construction is cheap and synchronous; the network round-trips
    happen in :meth:`connect`. Disconnect is symmetric: :meth:`stop`
    closes the WebSocket and (if the controller owns the aiohttp
    session) closes that too.

    Threading: this class is async-only. The WS reader runs as a
    background ``asyncio.Task``; do not block the event loop in event
    or status callbacks — schedule heavier work on a separate task.
    """

    __slots__ = (
        "_auth",
        "_base_url",
        "_client",
        "_dispatcher",
        "_loaded",
        "_owns_session",
        "_session",
        "_ws",
        "_ws_path",
    )

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        session: aiohttp.ClientSession | None = None,
        ws_path: str = "/rest/subscribe",
    ) -> None:
        """Configure the controller.

        Args:
            base_url: Scheme + host + port (no trailing slash). Use
                ``"https://eisy.local:443"`` for portal mode or
                ``"https://eisy.local:8443"`` for local mode.
            auth: A configured :class:`pyisyox.auth.Auth` instance —
                typically :class:`pyisyox.auth.PortalAuth` (default,
                JWT bearer) or :class:`pyisyox.auth.LocalAuth` (HTTP
                basic, feature-degraded).
            session: An aiohttp ``ClientSession`` to reuse. When
                ``None``, the controller creates and owns one and
                will close it on :meth:`stop`.
            ws_path: WebSocket path. Default ``/rest/subscribe`` works
                under both auth modes; ``/api/events/subscribe`` is
                an opt-in JSON envelope path that requires portal
                JWT auth and an initial frame handshake.
        """
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._session = session
        self._owns_session = session is None
        self._ws_path = ws_path
        self._client: IoXClient | None = None
        self._dispatcher: EventDispatcher | None = None
        self._ws: WebSocketEventStream | None = None
        self._loaded: LoadResult | None = None

    # --- lifecycle -----------------------------------------------------

    async def connect(self, *, start_websocket: bool = True) -> None:
        """Authenticate, run the initial load, and (optionally) open the WS.

        Builds the IoXClient, calls :meth:`IoXClient.connect` to fetch
        ``/api/config``, ``/rest/profiles``, ``/api/nodes``,
        ``/rest/status``, programs/triggers/variables in parallel, and
        merges the status overlay. Then constructs the
        :class:`EventDispatcher` over the same node registry the
        runtime :class:`Node` instances will read from, so WebSocket
        frames mutate properties in place.

        Args:
            start_websocket: When ``True`` (default), the WS reader
                starts in the background after the initial load
                completes. Pass ``False`` for one-shot reads (CLI
                tools, tests) where the consumer doesn't need live
                updates.

        Raises:
            Any error from :meth:`IoXClient.connect` (auth failure,
            HTTP failure, malformed payload) propagates unchanged.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        self._client = IoXClient(self._base_url, self._auth, self._session)
        self._loaded = await self._client.connect()
        # Bind the dispatcher to the same dict the LoadResult holds —
        # so runtime Nodes (which read from LoadResult.nodes) see live
        # updates without an explicit notification path.
        self._dispatcher = EventDispatcher(self._loaded.nodes)

        if start_websocket:
            self._ws = WebSocketEventStream(self._client, self._dispatcher, path=self._ws_path)
            self._ws.start()

    async def stop(self) -> None:
        """Stop the WebSocket and (if we own it) close the session.

        Idempotent — safe to call from cleanup paths even if
        :meth:`connect` partially failed.
        """
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
        # Drop the loaded snapshot so accessing properties after stop
        # raises a clear error instead of returning stale data.
        self._loaded = None
        self._dispatcher = None
        self._client = None

    # --- accessors -----------------------------------------------------

    @property
    def connected(self) -> bool:
        """True between :meth:`connect` returning successfully and
        :meth:`stop` being called."""
        return self._loaded is not None

    @property
    def base_url(self) -> str:
        """The controller URL passed to ``__init__``."""
        return self._base_url

    @property
    def config(self) -> ControllerConfig:
        """Decoded ``/api/config`` slice (uuid, version, portalHost)."""
        return self._loaded_or_raise().config

    @property
    def profile(self) -> Profile:
        """The decoded ``/rest/profiles`` blob with built nodedef lookup."""
        return self._loaded_or_raise().profile

    @property
    def nodes(self) -> dict[str, Node]:
        """Map of node address → runtime :class:`Node`.

        Built lazily on first access from the loaded :class:`NodeRecord`
        registry; subsequent accesses return the cached dict so
        identity is stable across calls (consumers can hold references
        to specific nodes safely).
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover — connect() sets both
            raise ControllerNotConnectedError("controller has no client")
        return {
            address: Node.from_record(record, loaded.profile, client)
            for address, record in loaded.nodes.items()
        }

    @property
    def programs(self) -> list[dict]:
        """Raw ``/api/programs`` data list (typed wrappers TBD)."""
        return self._loaded_or_raise().programs

    @property
    def triggers(self) -> list[dict]:
        """Raw ``/api/triggers`` data list — program AST as JSON."""
        return self._loaded_or_raise().triggers

    @property
    def variables(self) -> dict[str, list[dict]]:
        """Raw variables, keyed by type id (``"1"`` integer, ``"2"`` state)."""
        return self._loaded_or_raise().variables

    # --- subscriptions -------------------------------------------------

    def add_event_listener(self, callback: EventListener) -> Callable[[], None]:
        """Subscribe to every parsed WebSocket event.

        The dispatcher applies the property update *before* calling
        listeners, so callbacks observing a property event can read
        the new value via ``controller.nodes[address].properties[id]``
        synchronously.

        Returns:
            An unsubscribe function. Calling it removes ``callback``.

        Raises:
            ControllerNotConnectedError: When called before
                :meth:`connect` or after :meth:`stop`.
        """
        if self._dispatcher is None:
            raise ControllerNotConnectedError("add_event_listener requires connect() to have completed")
        return self._dispatcher.add_listener(callback)

    def add_status_listener(self, callback: StatusListener) -> Callable[[], None]:
        """Subscribe to WebSocket lifecycle status changes.

        Returns:
            An unsubscribe function.

        Raises:
            ControllerNotConnectedError: When called before
                :meth:`connect` (or after :meth:`stop`), or when
                :meth:`connect` was called with ``start_websocket=False``.
        """
        if self._ws is None:
            raise ControllerNotConnectedError(
                "add_status_listener requires the WebSocket reader to be started"
            )
        return self._ws.add_status_listener(callback)

    # --- testing seams -------------------------------------------------

    def feed_event_frame(self, raw_frame: str) -> Event | None:
        """Inject a raw frame into the dispatcher.

        Useful in tests and CLIs replaying captured WebSocket data.
        Production code paths drive the dispatcher through the
        :class:`WebSocketEventStream` reader.
        """
        if self._dispatcher is None:
            raise ControllerNotConnectedError("feed_event_frame requires connect() to have completed")
        return self._dispatcher.feed(raw_frame)

    # --- internals -----------------------------------------------------

    def _loaded_or_raise(self) -> LoadResult:
        if self._loaded is None:
            raise ControllerNotConnectedError(
                "controller is not connected; call await controller.connect() first"
            )
        return self._loaded
