Event Pipeline
==============

The eisy emits state changes as ``<Event>`` XML frames over a single
WebSocket connection. PyISYoX parses each frame, applies the update
to the appropriate record in :class:`pyisyox.LoadResult`, and then
fans out to three listener channels via the
:class:`~pyisyox.EventDispatcher`. This document describes the
event taxonomy, the dispatcher contract, and the WebSocket health
surface.

Transport
---------

Two WebSocket paths exist:

* ``/rest/subscribe`` — the legacy path. Raw XML frames. **Default**
  for both PortalAuth and LocalAuth.
* ``/api/events/subscribe`` — the modern path. JSON envelope
  (``{"type": "event", "data": "<xml>"}``) and adds a ``"spolisy"``
  side channel for PG3 service status. **Opt-in for PortalAuth only.**

Both deliver the same underlying ``<Event seqnum=... sid=...
timestamp=...>`` XML payload, so :func:`pyisyox.runtime.parse_event_frame`
accepts either shape and returns a single :class:`~pyisyox.Event`
(or ``None`` for keep-alive nulls and non-event JSON frames like
``"spolisy"`` PG3 status updates).

To opt in to the JSON-envelope path, pass ``ws_path="/api/events/subscribe"``
to :class:`pyisyox.Controller`.

Event control codes
-------------------

Every frame carries a ``<control>`` element. The dispatcher routes
on this value:

Property updates
~~~~~~~~~~~~~~~~

When ``<control>`` is a property id (``"ST"``, ``"OL"``, ``"GV1"``,
etc.) and ``<node>`` is populated, the frame is a property update.
The dispatcher updates ``LoadResult.nodes[address].properties[control]``
in place — building a :class:`~pyisyox.NodePropertyValue` from the
``<action>`` raw value plus optional ``uom`` / ``prec`` attributes
and ``<fmtAct>`` / ``<fmtName>`` siblings.

System control codes
~~~~~~~~~~~~~~~~~~~~

Codes starting with an underscore carry system-wide signals. The
dispatcher special-cases two:

* ``_3`` — **node-lifecycle**. ``<action>`` is a lifecycle verb
  (see :class:`~pyisyox.NodeLifecycleAction`); the dispatcher emits
  a :class:`~pyisyox.NodeLifecycleEvent` to lifecycle listeners.
* ``_1`` — **program / variable / system**:
    - ``<action>0</action>`` is a program-status update — the
      matching :class:`~pyisyox.ProgramRecord` is mutated in place
      and a :class:`~pyisyox.ProgramStatusEvent` is emitted to
      program-status listeners.
    - ``<action>6</action>`` is a variable-value update — the
      matching :class:`~pyisyox.VariableRecord` has its ``value``
      and timestamp mutated.
    - ``<action>7</action>`` is a variable-init update — the
      matching :class:`~pyisyox.VariableRecord` has its ``init``
      mutated.
    - ``<action>3</action>`` and other freeform actions are
      surfaced as plain :class:`~pyisyox.Event` instances; consumers
      that care can parse the ``event_info`` payload themselves.

Other system codes (``_5`` driver state, ``_7`` controller logs,
``_28`` Matter status, ...) flow through as plain
:class:`~pyisyox.Event` instances. Consumers that want their
structured payload parse the ``event_info`` string themselves.

Node lifecycle events
---------------------

The full set of lifecycle verbs is on
:class:`~pyisyox.NodeLifecycleAction`. The most important property is
:attr:`NodeLifecycleEvent.requires_reload`: ``True`` for verbs that
invalidate the cached node registry (add / remove / rename /
enabled-toggle / revised / removed-from-group), ``False`` for softer
signals (added-to-scene, parent-changed, pending-op, PG3 property /
config reports, comm errors).

HA Core's intended UX is to register a Repair issue on the first
lifecycle event with ``requires_reload=True`` and clear it once the
user-initiated reload completes. PyISYoX does **not** auto-merge
these into the live registry — consumers decide when to call
:meth:`~pyisyox.Controller.refresh`.

For ``ND`` (added) frames, the inner ``<node>`` element is preserved
verbatim in :attr:`NodeLifecycleEvent.node_xml`; consumers can pass
that to :func:`pyisyox.runtime.events.parse_lifecycle_node_xml` to
get a structured shape.

Subscribing
-----------

Three listener channels are exposed on :class:`pyisyox.Controller`,
each returning an unsubscribe function:

.. code-block:: python

    def on_event(ev):                          # every parsed frame
        print(ev.seqnum, ev.control, ev.action, ev.node_address)

    def on_lifecycle(ev):                      # _3 frames only
        if ev.requires_reload:
            schedule_reload()

    def on_program_status(ev):                 # _1/0 frames only
        print("program", ev.address, ev.status)

    def on_ws_status(status):                  # ws lifecycle
        print("ws:", status)

    unsub_event     = controller.add_event_listener(on_event)
    unsub_lifecycle = controller.add_node_lifecycle_listener(on_lifecycle)
    unsub_program   = controller.add_program_status_listener(on_program_status)
    unsub_status    = controller.add_status_listener(on_ws_status)

The dispatcher applies the property / program / variable update
*before* calling listeners, so a callback observing a property event
can read the new value via
``controller.nodes[address].properties[control]`` synchronously.

Listener exceptions are isolated: an exception raised by one
listener is logged at warning level but does not prevent the other
listeners from running, and does not crash the read loop.

WebSocket health
----------------

:class:`pyisyox.WebSocketEventStream` exposes three readable
properties for surfacing connection health to the user:

* ``status`` — :class:`pyisyox.constants.EventStreamStatus` enum
  (``CONNECTING``, ``CONNECTED``, ``RECONNECTING``,
  ``DISCONNECTED``).
* ``connected`` — bool shortcut for ``status == CONNECTED``.
* ``last_event_at`` — ``datetime`` (UTC) of the most recently
  received frame, or ``None`` if no frame has arrived yet.

Access via ``controller.websocket`` (``None`` if the controller was
started with ``start_websocket=False`` or after ``stop()``):

.. code-block:: python

    ws = controller.websocket
    if ws is None:
        # one-shot read, or already stopped
        ...
    else:
        print(ws.status, ws.connected, ws.last_event_at)

Reconnection
------------

On transport error or unexpected close, the reader backs off through
a fixed schedule (1s → 2s → 5s → 10s → 30s → 60s, capped at 60s
thereafter) before reconnecting. The schedule resets after a
successful read. Status listeners see ``RECONNECTING`` while we're in
the backoff loop and ``CONNECTED`` once a fresh handshake succeeds.

A 401 during the WebSocket handshake triggers a token refresh via the
auth strategy before the next attempt — PortalAuth refreshes; LocalAuth
returns ``False`` from ``handle_unauthorized`` so the next attempt's
basic-auth header carries the (possibly updated) credentials.

Testing without a live controller
---------------------------------

The dispatcher is decoupled from the WebSocket transport. Tests
can inject synthetic frames via :meth:`~pyisyox.Controller.feed_event_frame`:

.. code-block:: python

    raw = """<?xml version="1.0"?>
        <Event seqnum="1" sid="uuid:42" timestamp="2026-05-11T00:00:00Z">
          <control>ST</control>
          <action prec="0" uom="51">100</action>
          <node>3D 7D 87 1</node>
          <fmtAct>On</fmtAct>
        </Event>"""
    controller.feed_event_frame(raw)
    assert controller.nodes["3D 7D 87 1"].properties["ST"].value == "100"

This is the same path the WebSocket reader exercises, so listener
contracts behave identically.

Reference
---------

.. autoclass:: pyisyox.Event
    :members:
    :no-index:

.. autoclass:: pyisyox.EventDispatcher
    :members:
    :no-index:

.. autoclass:: pyisyox.NodeLifecycleAction
    :members:
    :no-index:

.. autoclass:: pyisyox.NodeLifecycleEvent
    :members:
    :no-index:

.. autoclass:: pyisyox.ProgramStatusEvent
    :members:
    :no-index:

.. autoclass:: pyisyox.WebSocketEventStream
    :members:
    :no-index:

.. autofunction:: pyisyox.runtime.parse_event_frame
