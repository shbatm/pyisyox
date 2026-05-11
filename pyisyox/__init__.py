"""PyISYoX — async Python client for Universal Devices' eisy / Polisy controllers running IoX 6.0.0+.

The public surface is intentionally small. Most consumers want:

* :class:`pyisyox.Controller` — the top-level handle (connect, query
  nodes, subscribe to events).
* :class:`pyisyox.PortalAuth` / :class:`pyisyox.LocalAuth` — auth
  strategies. Portal mode (JWT) is the recommended default; Local
  mode (HTTP basic) exists as a feature-degraded fallback.
* :class:`pyisyox.Node` — runtime device handle with
  editor-validated :meth:`Node.send_command`.

Example::

    from pyisyox import Controller, PortalAuth

    async def main():
        controller = Controller("https://eisy.local:443", PortalAuth("me@x.com", "pw"))
        await controller.connect()
        try:
            await controller.nodes["3D 7D 87 1"].send_command("DON", 75)
        finally:
            await controller.stop()

The library targets eisy/Polisy on IoX 6+. Original ISY-994 hardware is
out of scope; consumers needing that should use the upstream ``pyisy``
(v3.x) library.
"""

from importlib.metadata import PackageNotFoundError, version

from pyisyox.auth import Auth, AuthError, LocalAuth, PortalAuth
from pyisyox.classifier import (
    ClassificationResult,
    ControllablePlatform,
    Reading,
    ReadingPlatform,
    classify,
)
from pyisyox.client import (
    ClientError,
    ControllerConfig,
    FolderRecord,
    GroupRecord,
    HTTPError,
    IoXClient,
    LoadResult,
    NetworkResourceRecord,
    NodePropertyValue,
    NodeRecord,
    NodeType,
    ProgramRecord,
    VariableField,
    VariableRecord,
)
from pyisyox.controller import Controller, ControllerNotConnectedError
from pyisyox.exceptions import (
    ISYConnectionError,
    ISYInvalidAuthError,
    ISYMaxConnections,
    ISYResponseParseError,
    ISYStreamDataError,
    ISYStreamDisconnected,
)
from pyisyox.helpers.session import TLSVersionError, build_sslcontext
from pyisyox.logging import LOG_VERBOSE
from pyisyox.runtime import (
    Event,
    EventDispatcher,
    EventListener,
    Folder,
    Group,
    NetworkResource,
    Node,
    NodeCommandError,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    NodeLifecycleListener,
    Program,
    ProgramCommand,
    ProgramFolder,
    ProgramStatusEvent,
    ProgramStatusListener,
    StatusListener,
    SystemEventControl,
    TriggerAction,
    Variable,
    WebSocketEventStream,
)
from pyisyox.schema.profile import Profile, ProfileMergeResult

try:
    __version__ = version("pyisyox")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "LOG_VERBOSE",
    "Auth",
    "AuthError",
    "ClassificationResult",
    "ClientError",
    "ControllablePlatform",
    "Controller",
    "ControllerConfig",
    "ControllerNotConnectedError",
    "Event",
    "EventDispatcher",
    "EventListener",
    "Folder",
    "FolderRecord",
    "Group",
    "GroupRecord",
    "HTTPError",
    "ISYConnectionError",
    "ISYInvalidAuthError",
    "ISYMaxConnections",
    "ISYResponseParseError",
    "ISYStreamDataError",
    "ISYStreamDisconnected",
    "IoXClient",
    "LoadResult",
    "LocalAuth",
    "NetworkResource",
    "NetworkResourceRecord",
    "Node",
    "NodeCommandError",
    "NodeLifecycleAction",
    "NodeLifecycleEvent",
    "NodeLifecycleListener",
    "NodePropertyValue",
    "NodeRecord",
    "NodeType",
    "PortalAuth",
    "Profile",
    "ProfileMergeResult",
    "Program",
    "ProgramCommand",
    "ProgramFolder",
    "ProgramRecord",
    "ProgramStatusEvent",
    "ProgramStatusListener",
    "Reading",
    "ReadingPlatform",
    "StatusListener",
    "SystemEventControl",
    "TLSVersionError",
    "TriggerAction",
    "Variable",
    "VariableField",
    "VariableRecord",
    "WebSocketEventStream",
    "build_sslcontext",
    "classify",
]
__author__ = "shbatm"
__email__ = "support@shbatm.com"
