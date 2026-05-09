"""WebSocket event parsing and dispatch.

The eisy event stream sends ``<Event>`` XML frames over WebSocket. Two
transports exist:

* ``/rest/subscribe`` — legacy, raw XML frames. **Default** for both
  PortalAuth and LocalAuth modes.
* ``/api/events/subscribe`` — modern, JSON-wrapped:
  ``{"type": "event", "data": "<xml>"}``. Adds a ``"spolisy"`` side
  channel for PG3 service status. Opt-in for portal mode only.

Both wrap the same ``<Event seqnum=... sid=... timestamp=...>`` XML
payload, so :func:`parse_event_frame` accepts either shape and
returns a single :class:`Event` (or ``None`` for unparsable / non-
event frames like keep-alive nulls).

Event control ids:

* Property updates use the property id (e.g. ``"ST"``, ``"GV1"``) and
  populate ``node_address``.
* System events use a leading underscore (``"_5"`` driver state,
  ``"_28"`` Matter status, etc.) with empty ``node_address``.

This module is decoupled from the actual WebSocket reader so the
dispatcher can be tested with synthetic frames; the WS loop lives in
:mod:`pyisyox.runtime.ws`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from pyisyox.client import NodePropertyValue

if TYPE_CHECKING:
    from pyisyox.client import NodeRecord

_LOGGER = logging.getLogger(__name__)


#: Control code for node-lifecycle frames (add / remove / rename).
#: The action is the lifecycle verb; eventInfo carries the new node
#: element on add, just the address on remove/rename.
_NODE_LIFECYCLE_CONTROL = "_3"


class NodeLifecycleAction(StrEnum):
    """Verbs the eisy emits via ``<control>_3</control>`` events.

    The wire codes here are the canonical UDI set published in the
    IoX REST docs and the ISY-994's notification table — the
    upstream PyISY 3.x ``constants.py`` keeps the same mapping.
    Real captures additionally observe ``WD`` and ``CE`` on PG3
    nodes; they're undocumented but pyisyox surfaces them so
    consumers can react.

    ``EN`` carries an ``enabled`` boolean in ``<eventInfo>`` —
    there's no separate ``DI`` verb on the wire; the same code
    handles both transitions.
    """

    #: Node added (new device joined the controller).
    NODE_ADDED = "ND"
    #: Node removed (device deleted from the controller).
    NODE_REMOVED = "NR"
    #: Node renamed (display name changed).
    NODE_RENAMED = "NN"
    #: Node moved into a Scene.
    NODE_MOVED = "MV"
    #: Node removed from a Scene.
    NODE_REMOVED_FROM_GROUP = "RG"
    #: Parent (primary node) changed.
    PARENT_CHANGED = "PC"
    #: Node enabled/disabled — direction is in ``eventInfo.enabled``.
    NODE_ENABLED = "EN"
    #: Pending device operation (queued change awaiting commit).
    PENDING_DEVICE_OP = "WH"
    #: Node revised (UPB / firmware-style revision).
    NODE_REVISED = "RV"
    #: Node communication error (device unreachable).
    NODE_ERROR = "NE"
    #: Property reported — observed on PG3 plugin nodes; undocumented.
    PROPERTY_REPORTED = "WD"
    #: Configuration error — observed on PG3 plugin nodes; undocumented.
    CONFIG_ERROR = "CE"


@dataclass(slots=True, frozen=True)
class NodeLifecycleEvent:
    """A high-level summary of a ``<control>_3</control>`` lifecycle frame.

    Emitted alongside the raw :class:`Event` whenever the dispatcher
    sees one of the actions in :class:`NodeLifecycleAction`. Consumers
    subscribe via
    :meth:`pyisyox.controller.Controller.add_node_lifecycle_listener`
    to drive their own reload UX (HA Core's Repair issue, etc.).

    Attributes:
        action: The lifecycle verb (typed enum). Unknown verbs come
            through as a plain string via :attr:`raw_action`.
        node_address: Wire address of the affected node. Empty
            string only for system-wide signals (none observed yet).
        raw_action: The string action value verbatim, in case a new
            verb appears that isn't yet in :class:`NodeLifecycleAction`.
        seqnum: Sequence number of the underlying :class:`Event`.
        node_xml: For ``ND`` actions, the inner ``<node>`` element
            text from ``<eventInfo>``. ``None`` for verbs that don't
            include the full element. Consumers wanting the parsed
            shape can pass this to :func:`parse_lifecycle_node_xml`.
    """

    action: NodeLifecycleAction | str
    node_address: str
    raw_action: str
    seqnum: int
    node_xml: str | None = None

    @property
    def requires_reload(self) -> bool:
        """True for verbs that invalidate the cached node registry.

        Reload-worthy: ``ND`` (added), ``NR`` (removed), ``NN`` (renamed —
        the display name baked into the registry is now stale), ``EN``
        (enabled/disabled — the entity may need reloading because its
        properties change shape), ``RV`` (revised — UPB-style firmware
        update changes the property table), ``RG`` (removed from scene —
        scene membership changed).

        Softer signals — informational, don't trigger reload UX:
        ``MV`` (added to scene), ``PC`` (parent changed), ``WH`` (pending
        op), ``WD`` (PG3 property report), ``CE`` (PG3 config error),
        ``NE`` (comm error — device is just unreachable, no shape change).
        """
        return self.action in {
            NodeLifecycleAction.NODE_ADDED,
            NodeLifecycleAction.NODE_REMOVED,
            NodeLifecycleAction.NODE_RENAMED,
            NodeLifecycleAction.NODE_REMOVED_FROM_GROUP,
            NodeLifecycleAction.NODE_ENABLED,
            NodeLifecycleAction.NODE_REVISED,
        }


@dataclass(slots=True, frozen=True)
class Event:
    """One parsed event frame.

    Attributes:
        seqnum: Event sequence number from the eisy. Monotonic per
            connection; resets on reconnect.
        timestamp: ISO 8601 timestamp string from the frame
            (preserved verbatim — consumer parses if needed).
        control: Property id (``"ST"``, ``"GV1"``, ...) or system code
            (``"_5"``, ``"_28"``, ...).
        action: Raw value as reported (string form preserves the
            controller's precision representation).
        node_address: Wire address of the affected node, or empty
            string for system events.
        formatted_action: Human-readable display value (e.g.
            ``"0.6839 US gallons"``). Empty when the controller didn't
            supply one (system events typically don't).
        formatted_name: Display name of the property (e.g.
            ``"Current"``). Empty when not provided.
        uom: Unit-of-measure id from ``<action uom="...">``.
        prec: Decimal precision from ``<action prec="...">``, or
            ``None`` if absent.
    """

    seqnum: int
    timestamp: str
    control: str
    action: str
    node_address: str
    formatted_action: str = ""
    formatted_name: str = ""
    uom: str = ""
    prec: int | None = None

    @property
    def is_system(self) -> bool:
        """True for system control codes (``_5``, ``_28``, ...)."""
        return self.control.startswith("_")

    @property
    def is_node_property(self) -> bool:
        """True when this event should overlay onto a node's property dict."""
        return not self.is_system and bool(self.node_address) and bool(self.control)


def parse_event_frame(raw: str) -> Event | None:
    """Decode a single WebSocket frame to an :class:`Event`.

    Accepts either:

    * Raw XML — ``<?xml...?><Event...>...</Event>`` (legacy
      ``/rest/subscribe``).
    * JSON envelope — ``{"type": "event", "data": "<xml>"}`` (modern
      ``/api/events/subscribe``). Other ``type`` values (e.g.
      ``"spolisy"`` PG3 service status) return ``None`` — they're not
      property updates and the dispatcher ignores them.

    Returns ``None`` for keep-alive nulls, malformed XML, or non-event
    JSON envelopes. Does **not** raise on parse failures so a single
    bad frame can't crash the read loop.
    """
    if not raw:
        return None
    payload = _maybe_unwrap_json_envelope(raw)
    if payload is None:
        return None
    try:
        root = ET.fromstring(payload)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        _LOGGER.debug("WS frame XML parse failed (%s); frame=%r", exc, payload[:200])
        return None
    if root.tag != "Event":
        return None

    action_el = root.find("action")
    uom, prec = _decode_action_attrs(action_el)
    return Event(
        seqnum=_int_or(root.get("seqnum", "0"), default=0),
        timestamp=root.get("timestamp", ""),
        control=_text(root.find("control")),
        action=_text(action_el),
        node_address=_text(root.find("node")),
        formatted_action=root.findtext("fmtAct", default="") or "",
        formatted_name=root.findtext("fmtName", default="") or "",
        uom=uom,
        prec=prec,
    )


def _text(element: ET.Element | None) -> str:
    """Read an element's text safely, treating absent elements as empty."""
    if element is None:
        return ""
    return element.text or ""


def _int_or(raw: str, *, default: int) -> int:
    """Coerce a string to int; return ``default`` on failure."""
    try:
        return int(raw)
    except ValueError:
        return default


def _decode_action_attrs(action_el: ET.Element | None) -> tuple[str, int | None]:
    """Pull ``uom`` and ``prec`` attrs off an ``<action>`` element.

    ``prec`` is ``None`` when absent or non-numeric; legitimate negative
    values (rare but possible per the IoX spec) round-trip unchanged.
    """
    if action_el is None:
        return "", None
    uom = action_el.get("uom", "")
    prec_raw = action_el.get("prec")
    if prec_raw is None:
        return uom, None
    try:
        return uom, int(prec_raw)
    except ValueError:
        return uom, None


def _maybe_unwrap_json_envelope(raw: str) -> str | None:
    """Return the inner XML payload, or the raw string if unwrapped.

    Returns ``None`` when the frame is a non-event JSON envelope
    (e.g. ``"spolisy"`` PG3 status frames) or unparsable JSON that
    also isn't XML-shaped — the dispatcher should ignore those.
    """
    stripped = raw.lstrip()
    if stripped.startswith("<"):
        return raw
    if not stripped.startswith("{"):
        return None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("type") != "event":
        # spolisy / null / unknown — not a property update.
        return None
    data = envelope.get("data")
    return data if isinstance(data, str) else None


EventListener = Callable[[Event], None]
NodeLifecycleListener = Callable[[NodeLifecycleEvent], None]


def _extract_lifecycle_node_xml(raw_frame: str) -> str | None:
    """Pull the inner ``<node>...</node>`` element text out of a lifecycle
    frame's ``<eventInfo>``.

    The eisy emits the full new node element on ``ND`` actions (capture
    confirmed); other lifecycle verbs may follow the same pattern in
    eventInfo but haven't been observed yet. Returns ``None`` when no
    inner ``<node>`` is found — keeps the consumer code path simple.
    """
    payload = _maybe_unwrap_json_envelope(raw_frame)
    if payload is None:
        return None
    try:
        root = ET.fromstring(payload)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError:
        return None
    info = root.find("eventInfo")
    if info is None:
        return None
    node_el = info.find("node")
    if node_el is None:
        return None
    return ET.tostring(node_el, encoding="unicode")


class EventDispatcher:
    """Routes parsed :class:`Event` instances into a node registry +
    listener callbacks.

    The dispatcher is intentionally not coupled to the WebSocket
    transport — :meth:`feed` accepts a raw frame and does the parse +
    route + emit dance. The actual WS read loop lives in
    :mod:`pyisyox.runtime.ws`; tests can drive the dispatcher directly
    with synthetic frames.
    """

    __slots__ = ("_lifecycle_listeners", "_listeners", "_nodes")

    def __init__(self, nodes: dict[str, NodeRecord]) -> None:
        """Bind to a node registry.

        Args:
            nodes: The same ``dict[str, NodeRecord]`` that
                :class:`IoXClient.LoadResult` produces. The dispatcher
                mutates ``record.properties`` in place when an event
                targets a known node; events for unknown addresses
                are dropped silently (typically nodes that joined
                after the initial load — listen for node-add via
                :meth:`add_lifecycle_listener` and trigger a reload).
        """
        self._nodes = nodes
        self._listeners: list[EventListener] = []
        self._lifecycle_listeners: list[NodeLifecycleListener] = []

    def add_listener(self, callback: EventListener) -> Callable[[], None]:
        """Register ``callback`` to fire on every parsed event.

        Returns:
            An unsubscribe function. Calling it removes ``callback``
            from the listener list. Safe to call from inside a
            callback (the dispatcher iterates a snapshot).
        """
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def add_lifecycle_listener(self, callback: NodeLifecycleListener) -> Callable[[], None]:
        """Register ``callback`` to fire on every parsed
        :class:`NodeLifecycleEvent` (``<control>_3</control>`` frames).

        Use this to drive reload UX: HA Core typically registers a
        Repair issue when it sees a lifecycle event with
        ``requires_reload=True``, prompting the user to reload the
        integration when convenient. The dispatcher does **not**
        update the node registry on lifecycle events — consumers
        decide whether to call :meth:`pyisyox.controller.Controller.refresh`
        or live with a stale view until manual reload.

        Returns:
            An unsubscribe function.
        """
        self._lifecycle_listeners.append(callback)

        def _unsubscribe() -> None:
            try:
                self._lifecycle_listeners.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def feed(self, raw_frame: str) -> Event | None:
        """Parse one frame, apply the property update, fan out to listeners.

        Returns the parsed :class:`Event` for callers that want to
        peek (e.g. for sequence-number tracking), or ``None`` when the
        frame couldn't be parsed (malformed XML, non-event envelope,
        keep-alive null). Never raises on bad input — a single bad
        frame must not crash the read loop.
        """
        event = parse_event_frame(raw_frame)
        if event is None:
            return None
        if event.is_node_property:
            self._apply_property_update(event)
        # Lifecycle frames go through their own listener channel in
        # addition to the general event channel — the typed
        # NodeLifecycleEvent is more ergonomic for consumers driving
        # reload UX than re-parsing the raw frame.
        if event.control == _NODE_LIFECYCLE_CONTROL:
            self._emit_lifecycle(event, raw_frame)
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("event listener raised; suppressing to keep loop alive")
        return event

    def _emit_lifecycle(self, event: Event, raw_frame: str) -> None:
        """Build a :class:`NodeLifecycleEvent` and fan to lifecycle listeners."""
        if not self._lifecycle_listeners:
            return
        try:
            action: NodeLifecycleAction | str = NodeLifecycleAction(event.action)
        except ValueError:
            action = event.action
        node_xml = _extract_lifecycle_node_xml(raw_frame) if event.action == "ND" else None
        lifecycle = NodeLifecycleEvent(
            action=action,
            node_address=event.node_address,
            raw_action=event.action,
            seqnum=event.seqnum,
            node_xml=node_xml,
        )
        for listener in tuple(self._lifecycle_listeners):
            try:
                listener(lifecycle)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("lifecycle listener raised; suppressing to keep loop alive")

    def _apply_property_update(self, event: Event) -> None:
        """Overlay an event's value into the matching node's properties."""
        record = self._nodes.get(event.node_address)
        if record is None:
            _LOGGER.debug(
                "WS event for unknown node address %r — dropping (control=%s)",
                event.node_address,
                event.control,
            )
            return
        record.properties[event.control] = NodePropertyValue(
            id=event.control,
            value=event.action,
            formatted=event.formatted_action,
            uom=event.uom,
            name=event.formatted_name,
        )
