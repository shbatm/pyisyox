"""Module for connecting to and interacting with the ISY."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from pyisyox.clock import Clock
from pyisyox.configuration import ConfigurationData
from pyisyox.connection import Connection, ISYConnectionInfo
from pyisyox.constants import (
    CMD_X10,
    URL_QUERY,
    X10_COMMANDS,
    EventStreamStatus,
    Protocol,
    SystemStatus,
)
from pyisyox.events.websocket import WebSocketClient
from pyisyox.exceptions import ISYNotInitializedError
from pyisyox.helpers.events import EventEmitter
from pyisyox.logging import _LOGGER, enable_logging
from pyisyox.networking import NetworkResources
from pyisyox.nodes import Nodes
from pyisyox.programs import Programs
from pyisyox.variables import Variables


class ISY:
    """This is the main class that handles interaction with the ISY device."""

    _connected: bool = False
    args: argparse.Namespace | None = None
    background_tasks: set[asyncio.Task] = set()
    clock: Clock
    conn: Connection
    connection_events: EventEmitter
    connection_info: ISYConnectionInfo
    loop: asyncio.AbstractEventLoop
    networking: NetworkResources
    nodes: Nodes
    programs: Programs
    status_events: EventEmitter
    system_status: SystemStatus = SystemStatus.BUSY
    variables: Variables
    diagnostics: ISYDiagnosticInfo

    # These must be set as part of initialization or an error will be thrown
    config: ConfigurationData = None  # type: ignore[assignment]
    websocket: WebSocketClient = None  # type: ignore[assignment]

    def __init__(
        self,
        connection_info: ISYConnectionInfo,
        args: argparse.Namespace | None = None,
    ) -> None:
        """Initialize the primary ISY Class."""
        self.args = args  # Store command-line args
        if len(_LOGGER.handlers) == 0:
            enable_logging(add_null_handler=True)

        self.diagnostics = ISYDiagnosticInfo()

        # Initialize connection info and connection
        self.connection_info = connection_info
        self.conn = Connection(connection_info, args)

        # WebSocket is the only event-stream transport on IoX 6+.
        self.websocket = WebSocketClient(self, connection_info)

        # Initialize platforms
        self.clock = Clock(self)
        self.networking = NetworkResources(self)
        self.variables = Variables(self)
        self.programs = Programs(self)
        self.nodes = Nodes(self)

        # Setup event emitters and loop
        self.connection_events = EventEmitter()
        self.status_events = EventEmitter()
        self.loop = asyncio.get_running_loop()

    async def initialize(
        self,
        nodes: bool = True,
        clock: bool = True,
        programs: bool = True,
        variables: bool = True,
        networking: bool = True,
    ) -> None:
        """Initialize the connection with the ISY."""
        self.config = await self.conn.test_connection()

        isy_setup_tasks: list[Awaitable[Any]] = []
        if nodes:
            isy_setup_tasks.append(self.nodes.initialize())

        if clock:
            isy_setup_tasks.append(self.clock.update())

        if programs:
            isy_setup_tasks.append(self.programs.update())

        if variables:
            isy_setup_tasks.append(self.variables.update())

        if networking and (self.config.networking or self.config.portal):
            isy_setup_tasks.append(self.networking.update())

        await asyncio.gather(*isy_setup_tasks)

        self._connected = True

    async def shutdown(self) -> None:
        """Cleanup connections and prepare for exit."""
        if self.websocket:
            self.websocket.stop()
        await self.conn.close()

    @property
    def connected(self) -> bool:
        """Return the status of the connection."""
        return self._connected

    @property
    def auto_update(self) -> bool:
        """Return whether the WebSocket event stream is connected."""
        return self.websocket is not None and self.websocket.status == EventStreamStatus.CONNECTED

    @property
    def hostname(self) -> str | None:
        """Return the hostname."""
        return self.connection_info.parsed_url.hostname

    @property
    def protocol(self) -> str:
        """Return the protocol for this entity."""
        return Protocol.ISY

    @property
    def uuid(self) -> str:
        """Return the ISY's uuid."""
        if self.config is None:
            raise ISYNotInitializedError(
                "Module connection to ISY must first be initialized with isy.initialize()"
            )
        return self.config.uuid

    async def query(self, address: str | None = None) -> bool:
        """Query all the nodes or a specific node if an address is provided .

        Args:
            address (string, optional): Node Address to query. Defaults to None.

        Returns:
            boolean: Returns `True` on successful command, `False` on error.

        """
        req_path = [URL_QUERY]
        if address is not None:
            req_path.append(address)
        req_url = self.conn.compile_url(req_path)
        if not await self.conn.request(req_url):
            _LOGGER.warning("Error performing query.")
            return False
        _LOGGER.debug("Query requested successfully.")
        return True

    async def send_x10_cmd(self, address: str, cmd: str) -> None:
        """Send an X10 command.

        address: String of X10 device address (Ex: A10)
        cmd: String of command to execute. Any key of x10_commands can be used
        """
        if not (command := X10_COMMANDS.get(cmd)):
            raise ValueError(f"Invalid X10 command: {cmd}")

        req_url = self.conn.compile_url([CMD_X10, address, str(command)])
        if not await self.conn.request(req_url):
            _LOGGER.error("Failed to send X10 Command: %s To: %s", cmd, address)
            return
        _LOGGER.info("Sent X10 Command: %s To: %s", cmd, address)

    def system_status_changed_received(self, action: Any) -> None:
        """Handle System Status events from an event stream message."""
        if not action:
            return
        self.system_status = SystemStatus(action)
        self.status_events.notify(self.system_status)


@dataclass
class ISYDiagnosticInfo:
    """Diagnostic properties for the ISY."""

    batch_mode: bool = False
    write_updates_to_battery_nodes: bool = True
    portal_status: dict[str, bool] = field(default_factory=dict)
    zmatter: dict[str, Any] = field(default_factory=dict)
