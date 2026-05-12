.. _tutorial:

Quickstart
==========

This is a guided tour of the public API. Every example assumes you are
inside an ``async def`` and that ``pyisyox`` is installed.

Installation
------------

.. code-block:: shell

    pip install pyisyox

PyISYoX needs **Python 3.11+** and connects to an eisy or Polisy
controller running IoX 6.0.0+.

Pick an auth mode
-----------------

The controller exposes two HTTP endpoints with different auth shapes
and different feature sets. Pick once at construction time:

**Portal mode (recommended)** — port ``:443`` with the
``my.universal-devices.com`` portal email and password. PyISYoX trades
those credentials for a short-lived JWT (``POST /api/login``) and
refreshes it automatically. The eisy validates the portal credentials
and signs the JWT *locally*; there is no my.isy.io round-trip during
steady-state operation. Unlocks the modern ``/api/*`` JSON endpoints
(triggers AST, variable names, program metadata).

.. code-block:: python

    from pyisyox import PortalAuth
    auth = PortalAuth("me@example.com", "portal-password")

**Local mode** — port ``:8443`` with the local admin username and
password. HTTP basic on every request; no login round-trip. No portal
account required, but feature-degraded: no ``/api/triggers`` AST,
no ``/api/variables`` names; must fall back to the legacy
``/rest/nodes`` XML for structure. Recommended only if the user
refuses to use a portal account.

.. code-block:: python

    from pyisyox import LocalAuth
    auth = LocalAuth("admin", "admin-password")

Connect
-------

:class:`pyisyox.Controller` is the one entry point. Construction is
cheap and synchronous; network round-trips happen in :meth:`~pyisyox.Controller.connect`.

.. code-block:: python

    from pyisyox import Controller, PortalAuth

    controller = Controller(
        "https://eisy.local:443",          # :443 for portal, :8443 for local
        PortalAuth("me@example.com", "pw"),
    )
    await controller.connect()             # auth + 7 parallel HTTP calls + WS upgrade
    try:
        ...                                # use controller.nodes, .programs, etc.
    finally:
        await controller.stop()            # symmetric: cancels WS, closes session

``connect()`` does three things in order:

1. ``GET /api/config`` to retrieve uuid / version / portal host.
2. Authenticate (login + cache JWT, or no-op for LocalAuth).
3. Run a parallel fan-out across ``/rest/profiles``, ``/api/nodes``,
   ``/rest/status``, ``/api/programs``, ``/api/triggers``,
   ``/api/variables/1``, ``/api/variables/2`` and merge the status
   overlay into the node registry.

By default it then opens a WebSocket against ``/rest/subscribe`` and
runs a background reader that mutates the same node / program /
variable records in place as events arrive. Pass
``start_websocket=False`` for one-shot reads (CLI tools, snapshot
tests).

See :doc:`connection-flow` for the full sequence (endpoints, retries,
event routing).

What you can read after connect
-------------------------------

Once :meth:`~pyisyox.Controller.connect` returns, the public accessors are
populated:

* ``controller.config`` — :class:`pyisyox.ControllerConfig` (uuid,
  version, portal_host).
* ``controller.nodes`` — ``dict[str, Node]`` keyed by wire address.
* ``controller.groups`` — ``dict[str, Group]`` (IoX scenes).
* ``controller.folders`` — organisational folders (no command surface).
* ``controller.programs`` / ``controller.program_folders`` — typed
  Program / ProgramFolder wrappers, keyed by 4-character hex id.
* ``controller.variables`` — ``dict[str, dict[str, Variable]]``;
  outer key is type id (``"1"`` integer, ``"2"`` state), inner key
  is variable id.
* ``controller.network_resources`` — network-module fire triggers.
* ``controller.triggers`` — raw ``/api/triggers`` JSON list (program
  AST). Programs themselves are wrapped; the AST stays raw for
  consumers that want to introspect the rule logic.
* ``controller.profile`` — the decoded :class:`pyisyox.Profile`
  (nodedefs + editors + linkdefs) with lookup helpers.

Accessing any of these before ``connect()`` (or after ``stop()``)
raises :class:`pyisyox.ControllerNotConnectedError`.

Controlling a node
------------------

Use the wire address (the value of the ``<address>`` element in
``/api/nodes``). Insteon addresses look like ``"3D 7D 87 1"``; Z-Wave
addresses are short hex; plugin addresses are the plugin slot prefix
(``"n010_84dd4c2c24c3b7"``).

.. code-block:: python

    node = controller.nodes["3D 7D 87 1"]
    await node.send_command("DON")            # turn fully on
    await node.send_command("DON", 75)        # turn on at 75 %
    await node.send_command("DOF")            # turn off

:meth:`~pyisyox.Node.send_command` validates the parameters through the
editor codec on the node's nodedef *before* hitting HTTP — out-of-range
levels, unknown command ids, or wrong parameter counts raise
:class:`pyisyox.NodeCommandError` with no traffic sent. Enum names
work alongside integers:

.. code-block:: python

    thermostat = controller.nodes["1A 2B 3C 1"]
    await thermostat.send_command("CLIMD", "Heat")     # accepts enum name
    await thermostat.send_command("CLISPC", 72.0)      # codec scales by precision

Thin ergonomic wrappers are available for the common Insteon /
Z-Wave commands — :meth:`~pyisyox.Node.set_climate_mode`,
:meth:`~pyisyox.Node.set_climate_setpoint_heat`, :meth:`~pyisyox.Node.set_on_level`,
:meth:`~pyisyox.Node.set_ramp_rate`, :meth:`~pyisyox.Node.secure_lock`, etc. — each is
a one-liner over :meth:`~pyisyox.Node.send_command` with the wire-level command id
baked in.

Reading live state
~~~~~~~~~~~~~~~~~~

Every node carries a ``properties`` dict (keyed by property id —
``"ST"``, ``"OL"``, ``"RR"``, ...) that the WebSocket dispatcher
updates in place:

.. code-block:: python

    node = controller.nodes["3D 7D 87 1"]
    st = node.status                          # shortcut for properties["ST"]
    print(st.value, st.formatted, st.uom)     # raw, display, UOM id

Derived introspection helpers — :attr:`~pyisyox.Node.protocol`,
:attr:`~pyisyox.Node.is_dimmable`, :attr:`~pyisyox.Node.is_thermostat`,
:attr:`~pyisyox.Node.is_lock`, :attr:`~pyisyox.Node.is_fan`,
:attr:`~pyisyox.Node.is_battery_node` — let consumers branch on capability
without hardcoding type strings.

Sub-buttons (KeypadLinc, RemoteLinc, FanLinc) carry the device
primary's address in :attr:`~pyisyox.Node.primary_address`; primaries return
``None``. ``node.primary_address or node.address`` gives the
canonical device-grouping key.

Controlling a scene (group)
---------------------------

Scenes use the same wire shape as nodes (``GET /rest/nodes/{addr}/cmd/...``);
addresses are typically 5-digit integer strings. Group commands are
**not** editor-validated (there's no nodedef-level codec for scene
commands), so encode parameters as integers up-front:

.. code-block:: python

    scene = controller.groups["28614"]
    await scene.send_command("DON")
    await scene.send_command("DON", 100)
    await scene.send_command("DOF")

Programs and program folders
----------------------------

Both share the controller's flat program list and the
``/rest/programs/{id}/{command}`` endpoint, but folders only support a
subset of verbs (``run`` / ``stop`` / ``enable`` / ``disable``). The
typed split means consumers can branch on ``isinstance`` rather than
an ``is_folder`` flag:

.. code-block:: python

    from pyisyox import ProgramCommand

    program = controller.programs["005E"]
    await program.run()                                # → ProgramCommand.RUN
    await program.run_then()                           # → ProgramCommand.RUN_THEN
    await program.send_command(ProgramCommand.STOP)

    folder = controller.program_folders["0061"]
    await folder.run()                                 # whole folder

Variables
---------

Variable type ``1`` is integer; type ``2`` is state. Both are typed
:class:`pyisyox.Variable` wrappers with three mutation coroutines
(``set_value`` / ``set_init`` / ``rename``) that route through
``POST /api/variables/{type}/{id}``. The wrapper's record is mutated in
place after a successful write, so reads reflect the new value without
waiting for the corresponding WebSocket frame:

.. code-block:: python

    var = controller.variables["2"]["14"]
    await var.set_value(6)
    print(var.value)        # 6

    state_var = controller.variables["1"]["3"]
    await state_var.set_init(0)

Network resources
-----------------

Network resources are user-defined HTTP / TCP / UDP fire-triggers on
the controller. Fire by id:

.. code-block:: python

    await controller.network_resources["5"].run()
    # equivalently:
    await controller.run_network_resource(5)

The controller acknowledges receipt only — it does not report the
result of the underlying fire.

Subscribing to events
---------------------

The WebSocket reader pushes every parsed frame through three
listener channels. Each ``add_*_listener`` call returns an
unsubscribe function:

.. code-block:: python

    def on_event(ev):
        print(ev.seqnum, ev.control, ev.action, ev.node_address)

    def on_status(status):
        print("WS:", status)

    def on_node_lifecycle(ev):
        if ev.requires_reload:
            print("reload needed:", ev.action, ev.node_address)

    unsub_event = controller.add_event_listener(on_event)
    unsub_status = controller.add_status_listener(on_status)
    unsub_life = controller.add_node_lifecycle_listener(on_node_lifecycle)
    # ...later
    unsub_event()

Dispatcher semantics: property updates are applied to the underlying
node record **before** listeners fire, so callbacks can read
``controller.nodes[address].properties[id]`` synchronously and see the
new value. The same applies to program-status and variable-change
frames — the record is mutated first, then listeners are notified.

Node-tree changes (add / remove / rename) are *not* auto-merged into
the live registry. The dispatcher emits a
:class:`pyisyox.NodeLifecycleEvent`; consumers decide whether to call
:meth:`~pyisyox.Controller.refresh` to absorb the change. See :doc:`events`
for the full taxonomy.

CLI smoke test
--------------

The package ships a small CLI for connectivity checks:

.. code-block:: shell

    python3 -m pyisyox https://eisy.local:443 me@example.com portal-password
    python3 -m pyisyox https://eisy.local:8443 admin admin-password --no-events

The URL determines the auth mode — port ``:443`` triggers PortalAuth,
``:8443`` triggers LocalAuth (the choice is also keyed on whether the
username looks like an email). Pass ``--no-events`` to skip the
WebSocket reader; useful when you only want a one-shot inventory.

Cleanly shutting down
---------------------

:meth:`~pyisyox.Controller.stop` is idempotent and symmetric. It:

1. Cancels the WebSocket reader and closes the underlying connection.
2. Best-effort posts ``/api/logout`` to invalidate the portal session
   (so the long-lived refresh token can't be reused — LocalAuth no-ops
   here).
3. Closes the aiohttp session if the controller owns it (when
   ``session=None`` was passed to the constructor; consumers that
   inject their own session are responsible for closing it).

It is safe to call from cleanup paths even if ``connect()`` partially
failed.

What's next
-----------

* :doc:`library` — full reference of the public types in this module.
* :doc:`connection-flow` — endpoint-by-endpoint connect sequence,
  retry logic, and the WebSocket state machine.
* :doc:`events` — the event taxonomy, dispatcher contract, and
  WebSocket health surface.
* :doc:`api/index` — the auto-generated module-level API reference.
