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
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import aiohttp

from pyisyox.client import IoXClient, NodeType, VariableField
from pyisyox.helpers.session import build_sslcontext
from pyisyox.paths import PROFILES_PATH, SUBSCRIBE_PATH
from pyisyox.runtime.events import EventDispatcher
from pyisyox.runtime.folder import Folder
from pyisyox.runtime.group import Group
from pyisyox.runtime.network_resource import NetworkResource
from pyisyox.runtime.node import Node
from pyisyox.runtime.program import Program, ProgramCommand, ProgramFolder
from pyisyox.runtime.variable import Variable
from pyisyox.runtime.ws import WebSocketEventStream
from pyisyox.schema.profile import Profile, ProfileMergeResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyisyox.auth import Auth
    from pyisyox.client import ControllerConfig, LoadResult
    from pyisyox.runtime.events import (
        Event,
        EventListener,
        NodeLifecycleListener,
        ProgramStatusListener,
    )
    from pyisyox.runtime.ws import StatusListener

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
        "_tls_version",
        "_verify_ssl",
        "_ws",
        "_ws_path",
    )

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        session: aiohttp.ClientSession | None = None,
        ws_path: str = SUBSCRIBE_PATH,
        tls_version: float | None = None,
        verify_ssl: bool = False,
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
                ``None``, the controller creates and owns one
                (configured via ``tls_version`` and ``verify_ssl``)
                and will close it on :meth:`stop`. When provided,
                ``tls_version`` and ``verify_ssl`` are ignored —
                consumers must configure SSL on their own session.
            ws_path: WebSocket path. Default ``/rest/subscribe`` works
                under both auth modes; ``/api/events/subscribe`` is
                an opt-in JSON envelope path that requires portal
                JWT auth and an initial frame handshake.
            tls_version: ``None`` (default) auto-negotiates TLS 1.2 or
                1.3. Pin to ``1.2`` or ``1.3`` for reproducible
                behaviour. Used only when the controller creates its
                own session.
            verify_ssl: ``False`` (default) accepts the eisy's
                self-signed certificate. ``True`` enforces strict
                verification — for users with their own CA. Used only
                when the controller creates its own session.
        """
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._session = session
        self._owns_session = session is None
        self._ws_path = ws_path
        self._tls_version = tls_version
        self._verify_ssl = verify_ssl
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
            self._session = self._build_owned_session()
            self._owns_session = True

        self._client = IoXClient(self._base_url, self._auth, self._session)
        self._loaded = await self._client.connect()
        # Bind the dispatcher to the same dict the LoadResult holds —
        # so runtime Nodes (which read from LoadResult.nodes) see live
        # updates without an explicit notification path.
        self._dispatcher = EventDispatcher(
            self._loaded.nodes,
            programs=self._loaded.programs,
            variables=self._loaded.variables,
        )

        if start_websocket:
            self._ws = WebSocketEventStream(self._client, self._dispatcher, path=self._ws_path)
            self._ws.start()

    async def stop(self) -> None:
        """Stop the WebSocket, log out, and (if we own it) close the session.

        Idempotent — safe to call from cleanup paths even if
        :meth:`connect` partially failed.
        """
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None
        # Best-effort logout to invalidate any server-side session
        # (PortalAuth posts /api/logout; LocalAuth no-ops). Run before
        # closing the session because PortalAuth needs it for the call.
        if self._session is not None:
            try:
                await self._auth.close(self._session, self._base_url)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("auth.close() raised; ignoring during shutdown", exc_info=True)
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
    def websocket(self) -> WebSocketEventStream | None:
        """The active WebSocket stream, or ``None``.

        Returns the live :class:`WebSocketEventStream` when
        :meth:`connect` was called with ``start_websocket=True`` and
        :meth:`stop` hasn't run yet. ``None`` for one-shot reads
        (CLI tools, snapshot tests) that opted out of the WS
        upgrade. Consumers polling stream health (HA system_health,
        diagnostics) read ``websocket.status`` /
        ``websocket.last_event_at`` directly.
        """
        return self._ws

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
    def groups(self) -> dict[str, Group]:
        """Map of group address → runtime :class:`Group` (IoX scenes).

        Sourced from ``/rest/nodes`` XML at connect time. The
        controller-self group (``flag="12"``) is filtered out.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover — connect() sets both
            raise ControllerNotConnectedError("controller has no client")
        return {
            address: Group.from_record(record, loaded.profile, client, nodes=loaded.nodes)
            for address, record in loaded.groups.items()
        }

    @property
    def folders(self) -> dict[str, Folder]:
        """Map of folder address → runtime :class:`Folder` (org tree only)."""
        loaded = self._loaded_or_raise()
        return {address: Folder(record) for address, record in loaded.folders.items()}

    @property
    def programs(self) -> dict[str, Program]:
        """Map of program id → runtime :class:`Program`.

        Folders share the same id space but live under
        :attr:`program_folders`; this map only contains executable
        programs (``is_folder=False``).
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        return {
            address: Program(record, client)
            for address, record in loaded.programs.items()
            if not record.is_folder
        }

    @property
    def program_folders(self) -> dict[str, ProgramFolder]:
        """Map of folder id → runtime :class:`ProgramFolder`.

        The synthetic root folder (``"My Programs"`` on stock eisy
        firmware) is included — consumers walking the tree from the
        controller can use it as the root anchor.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        return {
            address: ProgramFolder(record, client)
            for address, record in loaded.programs.items()
            if record.is_folder
        }

    @property
    def triggers(self) -> list[dict]:
        """Raw ``/api/triggers`` data list — program AST as JSON."""
        return self._loaded_or_raise().triggers

    @property
    def variables(self) -> dict[str, dict[str, Variable]]:
        """Map of variable type → id → typed :class:`Variable` wrapper.

        Outer key is ``"1"`` (integer) or ``"2"`` (state); inner key is
        the variable id within that type. Each :class:`Variable` shares
        its underlying :class:`VariableRecord` with the controller's
        loaded state — writes via the wrapper's mutation coroutines
        update the record in place so subsequent reads reflect the new
        value without waiting for a WS frame.

        Returns an empty inner dict for a type the controller has no
        variables in.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        return {
            type_id: {vid: Variable.from_record(record, client) for vid, record in records.items()}
            for type_id, records in loaded.variables.items()
        }

    @property
    def network_resources(self) -> dict[str, NetworkResource]:
        """Map of resource id → runtime :class:`NetworkResource`.

        Empty when the controller has no networking module enabled —
        the optional endpoint either 404s or returns an empty
        ``<NetConfig/>``, both flattened to ``{}`` here.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        return {
            address: NetworkResource(record, client) for address, record in loaded.network_resources.items()
        }

    # --- snapshot -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Flatten the full controller state to a JSON-compatible dict.

        Aggregates every loaded collection (nodes / groups / folders /
        programs / program_folders / variables / network_resources)
        plus the controller's own config + WebSocket health. Each
        nested object's structural fields come from its own
        :meth:`to_dict` so the same code path drives the
        ``pyisyox -m … --dump`` CLI flag and consumer diagnostics.
        Raises :class:`ControllerNotConnectedError` when called before
        :meth:`connect` (no loaded state to snapshot).
        """
        ws = self._ws
        return {
            "config": asdict(self.config),
            "connected": self.connected,
            "websocket": {
                "status": ws.status.value if ws is not None else None,
                "last_event_at": ws.last_event_at.isoformat()
                if ws is not None and ws.last_event_at is not None
                else None,
            },
            "profile": self.profile.to_dict(),
            "nodes": {addr: node.to_dict() for addr, node in self.nodes.items()},
            "groups": {addr: group.to_dict() for addr, group in self.groups.items()},
            "folders": {addr: folder.to_dict() for addr, folder in self.folders.items()},
            "programs": {addr: program.to_dict() for addr, program in self.programs.items()},
            "program_folders": {addr: folder.to_dict() for addr, folder in self.program_folders.items()},
            "variables": {
                type_id: {vid: var.to_dict() for vid, var in vars_.items()}
                for type_id, vars_ in self.variables.items()
            },
            "network_resources": {
                addr: resource.to_dict() for addr, resource in self.network_resources.items()
            },
        }

    # --- dynamic profile reload ---------------------------------------

    async def refresh_profile(self) -> ProfileMergeResult:
        """Re-fetch ``/rest/profiles`` and merge updates into the live profile.

        Designed for PG3 dynamic profile reload — when a plugin
        updates its nodedefs at runtime, consumers detect the
        controller-side signal (the WS event control code is plugin-
        + version-specific; capture it from a real reload to wire up
        an automatic listener) and call this method to absorb the
        change.

        The live :class:`Profile` is mutated in place: existing
        :class:`pyisyox.runtime.Node` instances that resolved against
        a NodeDef before the reload now see the new NodeDef on their
        next attribute access. The returned :class:`ProfileMergeResult`
        lists the lookup-key triples that were added vs replaced so
        consumers can re-classify or invalidate any caches keyed on
        nodedef.

        Returns:
            A :class:`ProfileMergeResult` summarising the diff. Empty
            (``result.changed is False``) when the controller's
            response was identical to what we had.

        Raises:
            ControllerNotConnectedError: When called before
                :meth:`connect`.
            ClientError / HTTPError / AuthError: As with any HTTP
                round-trip.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover — connect() sets both
            raise ControllerNotConnectedError("controller has no client")
        # _get_json is intentionally accessed across the client boundary —
        # the Controller is the only consumer that legitimately needs to
        # re-issue a load-time endpoint outside of the connect() flow.
        new_raw = await client._get_json(  # pylint: disable=protected-access
            PROFILES_PATH
        )
        incoming = Profile.load_from_json(new_raw)
        return loaded.profile.merge(incoming)

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

    def add_node_lifecycle_listener(self, callback: NodeLifecycleListener) -> Callable[[], None]:
        """Subscribe to node-tree lifecycle changes (add / remove / rename).

        The eisy emits ``<control>_3</control>`` frames when nodes
        appear or disappear (typically driven by PG3 plugin reloads).
        The dispatcher does **not** auto-update the live registry —
        consumers decide whether to call :meth:`refresh` or live with
        a stale view until the user manually reloads the integration.

        HA Core's intended UX is to register a Repair issue on the
        first lifecycle event with ``ev.requires_reload is True`` and
        clear it once the user-initiated reload completes.

        Returns:
            An unsubscribe function.

        Raises:
            ControllerNotConnectedError: When called before :meth:`connect`.
        """
        if self._dispatcher is None:
            raise ControllerNotConnectedError(
                "add_node_lifecycle_listener requires connect() to have completed"
            )
        return self._dispatcher.add_lifecycle_listener(callback)

    def add_program_status_listener(self, callback: ProgramStatusListener) -> Callable[[], None]:
        """Subscribe to program-status changes (the ``<control>_1</control>``
        action ``"0"`` frames).

        The dispatcher mutates the matching ``ProgramRecord.status`` /
        ``running`` in place before firing, so consumers reading
        ``controller.programs[id].status`` from the callback see the
        new value.

        Returns:
            An unsubscribe function.

        Raises:
            ControllerNotConnectedError: When called before :meth:`connect`.
        """
        if self._dispatcher is None:
            raise ControllerNotConnectedError(
                "add_program_status_listener requires connect() to have completed"
            )
        return self._dispatcher.add_program_status_listener(callback)

    # --- mutation -----------------------------------------------------

    async def refresh(self) -> ProfileMergeResult:
        """Re-run the parallel load fan-out and merge results into the
        live :class:`LoadResult`.

        Use after a :class:`NodeLifecycleEvent` with
        ``requires_reload=True`` to absorb the new node tree without
        re-authenticating. The live :class:`Profile` is mutated in
        place (see :meth:`Profile.merge`); the ``nodes`` /  ``groups``
        / ``folders`` / ``programs`` / ``triggers`` / ``variables``
        registries on the LoadResult are updated to match the fresh
        snapshot. The dispatcher's binding to ``LoadResult.nodes``
        survives because we mutate the dict in place.

        Returns:
            The :class:`ProfileMergeResult` from the schema merge —
            useful for tracking which nodedefs changed.

        Raises:
            ControllerNotConnectedError: When called before :meth:`connect`.
        """
        loaded = self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover — connect() sets both
            raise ControllerNotConnectedError("controller has no client")
        fresh = await client.load(loaded.config)

        diff = loaded.profile.merge(fresh.profile)

        # Mutate node registry in place so the EventDispatcher's
        # binding stays valid. Other registries can be replaced.
        loaded.nodes.clear()
        loaded.nodes.update(fresh.nodes)
        loaded.groups = fresh.groups
        loaded.folders = fresh.folders
        loaded.programs = fresh.programs
        loaded.triggers = fresh.triggers
        loaded.variables = fresh.variables
        loaded.network_resources = fresh.network_resources
        return diff

    async def send_program_command(self, program_id: str, command: ProgramCommand | str) -> None:
        """Send a program / folder command via the legacy REST endpoint.

        Wire shape: ``GET /rest/programs/{id}/{command}``. See
        :class:`pyisyox.runtime.ProgramCommand` for the typed command
        set; bare strings are accepted too (the StrEnum members are
        themselves strings, so ``ProgramCommand.RUN_THEN == "runThen"``
        — pass either form).

        Lower-level than :meth:`Program.run` etc.; useful for
        consumers that hold ids without a Program wrapper (e.g. an
        HA service receiving raw ids).
        """
        self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        await client.run_program_command(program_id, command)

    async def run_network_resource(self, resource_id: str | int) -> None:
        """Fire a network resource by id.

        Wire shape: ``GET /rest/networking/resources/{id}``. Treat as
        fire-and-forget — the controller acknowledges receipt only,
        not the result of the underlying HTTP / TCP / UDP fire.
        """
        self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        await client.run_network_resource(resource_id)

    async def set_variable_value(self, var_type: int | str, var_id: int | str, value: int) -> None:
        """Set the current value of a controller variable.

        Wire shape: ``POST /api/variables/{type}/{id}`` with body
        ``{"value": <int>}``.

        Args:
            var_type: ``1`` (integer) or ``2`` (state). Strings accepted.
            var_id: Variable id within the type.
            value: New value to write.

        Raises:
            ControllerNotConnectedError: When called before :meth:`connect`.
            HTTPError / ClientError: On wire failures.
        """
        await self._post_variable(var_type, var_id, {VariableField.VALUE: int(value)})

    async def set_variable_init(self, var_type: int | str, var_id: int | str, init: int) -> None:
        """Set the initial / restore-on-startup value of a variable.

        Wire shape: ``POST /api/variables/{type}/{id}`` with
        ``{"init": <int>}``.
        """
        await self._post_variable(var_type, var_id, {VariableField.INIT: int(init)})

    async def rename_variable(self, var_type: int | str, var_id: int | str, name: str) -> None:
        """Rename a variable.

        Wire shape: ``POST /api/variables/{type}/{id}`` with
        ``{"name": "<str>"}``.
        """
        await self._post_variable(var_type, var_id, {VariableField.NAME: name})

    async def _post_variable(self, var_type: int | str, var_id: int | str, body: dict) -> None:
        """Internal: route a variable mutation through the IoXClient."""
        self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        await client.post_variable_update(var_type, var_id, body)

    async def rename_node(self, address: str, name: str) -> None:
        """Rename a node.

        Wire shape: ``POST /api/nodes/{address}`` with
        ``{"name": "<str>", "nodeType": "node"}``.

        The ``nodeType`` field is required by the server. Use
        :meth:`rename_group` for scenes.
        """
        await self._post_node(address, {"name": name, "nodeType": NodeType.NODE})

    async def rename_group(self, address: str, name: str) -> None:
        """Rename a group / scene.

        Same endpoint as :meth:`rename_node` but with
        ``nodeType: "group"`` so the server applies the change
        through the scene registry.
        """
        await self._post_node(address, {"name": name, "nodeType": NodeType.GROUP})

    async def rename_folder(self, address: str, name: str) -> None:
        """Rename a folder (organisational container).

        Same endpoint as :meth:`rename_node` / :meth:`rename_group`
        but with ``nodeType: "folder"``. Folders are address-keyed
        like nodes/groups; their addresses are typically 5-digit
        integers (family ``"13"``).
        """
        await self._post_node(address, {"name": name, "nodeType": NodeType.FOLDER})

    async def _post_node(self, address: str, body: dict) -> None:
        """Internal: route a node mutation through the IoXClient."""
        self._loaded_or_raise()
        client = self._client
        if client is None:  # pragma: no cover
            raise ControllerNotConnectedError("controller has no client")
        await client.post_node_update(address, body)

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

    def _build_owned_session(self) -> aiohttp.ClientSession:
        """Construct an aiohttp session honouring our TLS settings.

        The cookie jar is set ``unsafe=True`` so cookies set on a bare
        IP host (typical LAN deployment) survive — aiohttp's default
        jar rejects them as a precaution that doesn't apply to a
        known-trusted LAN target.
        """
        use_https = self._base_url.startswith("https")
        context = build_sslcontext(
            use_https=use_https,
            tls_version=self._tls_version,
            verify_ssl=self._verify_ssl,
        )
        connector = aiohttp.TCPConnector(ssl=context) if context is not None else None
        return aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
