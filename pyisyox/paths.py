"""REST and WebSocket endpoint paths for IoX 6 controllers.

Centralised so the wire-level path strings appear in exactly one place
each. Fixed paths are exported as string constants; parametric paths
are templates the caller fills with ``.format(...)``.

Examples::

    from pyisyox.paths import CONFIG_PATH, NODE_COMMAND_PATH

    await client._get_json(CONFIG_PATH)
    await client._get_text(NODE_COMMAND_PATH.format(address=enc_addr, command="DON"))

Auth-flow paths (``/api/login``, ``/api/jwt/refresh``, ``/api/jwt/logout``)
live on :class:`pyisyox.auth.PortalAuth` as class attributes because
they're tied to that strategy's behaviour, not the general wire surface.
"""

from __future__ import annotations

#: ``GET /api/config`` — uuid / version / portal host. Unauthenticated.
CONFIG_PATH = "/api/config"

#: ``GET /api/nodes`` — JSON node structure (family/instance, addresses,
#: parent/pnode, flags). Plugin nodes have no ``property[]`` field.
NODES_PATH = "/api/nodes"

#: ``GET /api/programs`` — programs and program-folders.
PROGRAMS_PATH = "/api/programs"

#: ``GET /api/triggers`` — program AST as JSON.
TRIGGERS_PATH = "/api/triggers"

#: ``GET /rest/profiles?include=nodedefs,editors,linkdefs`` — the
#: ~117 KB profile blob with every nodedef + editor + linkdef.
PROFILES_PATH = "/rest/profiles?include=nodedefs,editors,linkdefs"

#: ``GET /rest/nodes`` — legacy XML surface for the node + group +
#: folder tree. **No longer used by the default load fan-out** —
#: ``/api/nodes`` JSON carries ``data.nodes.{node, group, folder}``
#: with every field the connector needs (verified against captures on
#: eisy IoX 6+; member ``type="16"`` controller marker matches). The
#: constant + the legacy parser (:func:`parse_rest_nodes_groups_folders`)
#: stay exported for LocalAuth flows on ``:8443`` (which doesn't
#: expose ``/api/*``) and for external consumers that prefer the XML.
REST_NODES_PATH = "/rest/nodes"

#: ``GET /rest/zwave/node/{address}/def/get`` — the *dynamically
#: generated* Z-Wave (family ``4``) nodedefs, in the legacy
#: ``<nodeDefs>`` XML shape. Use ``"0"`` for ``address`` to get every
#: Z-Wave nodedef in one call. These ``UZW*`` nodedefs are **not**
#: carried by ``/rest/profiles`` (only their ``ZW_*`` editors are), so
#: pyisyox fetches this on connect when there are unresolved Z-Wave
#: nodes. 404 tolerated (no Z-Wave radio / older firmware).
ZWAVE_NODEDEFS_PATH = "/rest/zwave/node/{address}/def/get"

#: ``GET /rest/zmatter/zwave/node/{address}/def/get`` — as
#: :data:`ZWAVE_NODEDEFS_PATH` but for the Z-Matter (800-series /
#: family ``12``) radio. Not yet confirmed against hardware; tried
#: best-effort for unresolved family-``12`` nodes.
ZMATTER_ZWAVE_NODEDEFS_PATH = "/rest/zmatter/zwave/node/{address}/def/get"

#: ``GET /rest/zwave/node/{address}/config/query/{number}`` — fetch one
#: Z-Wave configuration parameter. The HTTP body is a
#: ``<config paramNum="N" size="SZ" value="V"/>`` response on success
#: (PyISY 3.x verified shape); the device's underlying poll reply also
#: arrives on the WebSocket stream. ``address`` is URL-quoted.
ZWAVE_PARAMETER_GET_PATH = "/rest/zwave/node/{address}/config/query/{number}"

#: ``GET /rest/zwave/node/{address}/config/set/{number}/{value}/{size}`` —
#: set one Z-Wave parameter. ``size`` is the parameter's byte size (1/2/4)
#: as defined by the device; the controller forwards it on the wire so
#: multi-byte parameters land correctly.
ZWAVE_PARAMETER_SET_PATH = "/rest/zwave/node/{address}/config/set/{number}/{value}/{size}"

#: ``GET /rest/zmatter/zwave/node/{address}/config/query/{number}`` — as
#: :data:`ZWAVE_PARAMETER_GET_PATH` but for the Z-Matter (family ``12``)
#: radio. Not yet confirmed against hardware; mirrors the nodedef-path
#: zmatter quirk and is exercised through :meth:`Node.get_zwave_parameter`
#: when ``family_id == "12"``.
ZMATTER_ZWAVE_PARAMETER_GET_PATH = "/rest/zmatter/zwave/node/{address}/config/query/{number}"

#: ``GET /rest/zmatter/zwave/node/{address}/config/set/{number}/{value}/{size}`` —
#: zmatter-radio counterpart of :data:`ZWAVE_PARAMETER_SET_PATH`. Same
#: hardware-not-verified caveat as above.
ZMATTER_ZWAVE_PARAMETER_SET_PATH = "/rest/zmatter/zwave/node/{address}/config/set/{number}/{value}/{size}"

#: ``GET /rest/zwave/node/{address}/security/user/{user_num}/set/code/{code}`` —
#: program a Z-Wave lock's user-code slot. PyISY 3.x verified shape on
#: the legacy radio; reused untouched on IoX 6+ (no captures show the
#: surface changed). Lock devices vary in how many slots they expose;
#: pyisyox doesn't enumerate that — callers pass the slot they want.
ZWAVE_LOCK_CODE_SET_PATH = "/rest/zwave/node/{address}/security/user/{user_num}/set/code/{code}"

#: ``GET /rest/zwave/node/{address}/security/user/{user_num}/delete`` —
#: clear a Z-Wave lock's user-code slot. PyISY 3.x verified shape.
ZWAVE_LOCK_CODE_DELETE_PATH = "/rest/zwave/node/{address}/security/user/{user_num}/delete"

#: ``GET /rest/zmatter/zwave/node/{address}/security/user/{user_num}/set/code/{code}`` —
#: zmatter-radio counterpart of :data:`ZWAVE_LOCK_CODE_SET_PATH`. Not
#: yet confirmed against hardware (same caveat as the zmatter parameter
#: paths); needs a tester with an 800-series lock.
ZMATTER_ZWAVE_LOCK_CODE_SET_PATH = (
    "/rest/zmatter/zwave/node/{address}/security/user/{user_num}/set/code/{code}"
)

#: ``GET /rest/zmatter/zwave/node/{address}/security/user/{user_num}/delete`` —
#: zmatter-radio counterpart of :data:`ZWAVE_LOCK_CODE_DELETE_PATH`.
ZMATTER_ZWAVE_LOCK_CODE_DELETE_PATH = "/rest/zmatter/zwave/node/{address}/security/user/{user_num}/delete"

#: ``GET /rest/status`` — XML property table. Merged into ``/api/nodes``
#: records to fill missing property values (especially for plugin nodes).
REST_STATUS_PATH = "/rest/status"

#: ``GET /rest/profiles/family/{family}/profile/{instance}/download/nls/en_US.txt``
#: — the per-family NLS string table (flat ``KEY = VALUE`` text). Family
#: ``-1`` is GLOBAL (radio-independent command / status labels); a
#: per-radio family (``4`` Z-Wave, ``12`` Z-Matter) overlays it with
#: device-class overrides + editor enum names. Only fetched for
#: dynamically-loaded Z-Wave nodedefs, whose ``UZW*`` commands arrive
#: label-less (``/rest/profiles`` bakes its families' labels inline).
#: 404 tolerated.
NLS_PATH = "/rest/profiles/family/{family}/profile/{instance}/download/nls/en_US.txt"

#: ``GET /rest/networking/resources`` — optional networking module.
#: 404 / 503 tolerated; load doesn't abort if the module is absent.
NETWORKING_RESOURCES_PATH = "/rest/networking/resources"

#: ``wss://.../rest/subscribe`` — default WebSocket event path.
#: Works under both PortalAuth (JWT bearer) and LocalAuth (HTTP basic).
SUBSCRIBE_PATH = "/rest/subscribe"

#: ``wss://.../api/events/subscribe`` — modern JSON-envelope WS path.
#: Opt-in for PortalAuth only; adds a ``"spolisy"`` side channel for
#: PG3 service status.
SUBSCRIBE_JSON_PATH = "/api/events/subscribe"

# --- templated paths (call .format(...)) ----------------------------------

#: ``GET /api/variables/{type_id}`` — variable list by type. Use
#: ``"1"`` (integer) or ``"2"`` (state).
VARIABLES_TYPE_PATH = "/api/variables/{type_id}"

#: ``POST /api/variables/{type_id}/{var_id}`` — variable mutation
#: (value / init / name). See :class:`pyisyox.runtime.variable.Variable`.
VARIABLE_ITEM_PATH = "/api/variables/{type_id}/{var_id}"

#: ``GET /rest/nodes/{address}/cmd/{command}[/...]`` — legacy node
#: command endpoint. ``address`` is URL-quoted by the caller because
#: Insteon addresses contain spaces. Optional parameter slots are
#: appended as ``/{p1}/{p2}/...`` after the command id.
NODE_COMMAND_PATH = "/rest/nodes/{address}/cmd/{command}"

#: ``POST /api/nodes/{address}`` — node metadata mutation
#: (rename, etc.). ``address`` is URL-quoted.
NODE_ITEM_PATH = "/api/nodes/{address}"

#: ``GET /rest/nodes/{address}/enable`` — re-enable a node the
#: controller had disabled. ``address`` is URL-quoted. Legacy ``/rest/``
#: surface (no ``/api/*`` equivalent in captures), like ``/cmd/``.
NODE_ENABLE_PATH = "/rest/nodes/{address}/enable"

#: ``GET /rest/nodes/{address}/disable`` — disable a node (the
#: controller stops polling / commanding it; it stays in the table).
NODE_DISABLE_PATH = "/rest/nodes/{address}/disable"

#: ``GET /rest/programs/{program_id}/{command}`` — program command
#: (run / stop / enable / disable etc.). See
#: :class:`pyisyox.runtime.program.ProgramCommand`.
PROGRAM_COMMAND_PATH = "/rest/programs/{program_id}/{command}"

#: ``GET /rest/networking/resources/{resource_id}`` — fire one
#: network resource. The controller acknowledges receipt only.
NETWORK_RESOURCE_ITEM_PATH = "/rest/networking/resources/{resource_id}"
