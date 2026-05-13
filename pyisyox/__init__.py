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
    DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS,
    NODE_LIFECYCLE_EVENT_INFO_TAGS,
    DeviceLinkerAction,
    DeviceWriteAction,
    Event,
    EventDispatcher,
    EventListener,
    Folder,
    Group,
    InternetAccessStatus,
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
    ProgressAction,
    SecuritySystemAction,
    StatusListener,
    SystemConfigAction,
    SystemEventControl,
    TriggerAction,
    Variable,
    VariableTableChangeEvent,
    VariableTableChangeListener,
    WebSocketEventStream,
    describe_system_event,
)
from pyisyox.schema.profile import Profile, ProfileMergeResult

try:
    __version__ = version("pyisyox")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS",
    "LOG_VERBOSE",
    "NODE_LIFECYCLE_EVENT_INFO_TAGS",
    "Auth",
    "AuthError",
    "ClassificationResult",
    "ClientError",
    "ControllablePlatform",
    "Controller",
    "ControllerConfig",
    "ControllerNotConnectedError",
    "DeviceLinkerAction",
    "DeviceWriteAction",
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
    "InternetAccessStatus",
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
    "ProgressAction",
    "Reading",
    "ReadingPlatform",
    "SecuritySystemAction",
    "StatusListener",
    "SystemConfigAction",
    "SystemEventControl",
    "TLSVersionError",
    "TriggerAction",
    "Variable",
    "VariableField",
    "VariableRecord",
    "VariableTableChangeEvent",
    "VariableTableChangeListener",
    "WebSocketEventStream",
    "build_sslcontext",
    "classify",
    "describe_system_event",
]
__author__ = "shbatm"
__email__ = "support@shbatm.com"
