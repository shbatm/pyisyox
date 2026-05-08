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
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from pyisyox.client import NodePropertyValue

if TYPE_CHECKING:
    from pyisyox.client import NodeRecord

_LOGGER = logging.getLogger(__name__)


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


class EventDispatcher:
    """Routes parsed :class:`Event` instances into a node registry +
    listener callbacks.

    The dispatcher is intentionally not coupled to the WebSocket
    transport — :meth:`feed` accepts a raw frame and does the parse +
    route + emit dance. The actual WS read loop lives in
    :mod:`pyisyox.runtime.ws`; tests can drive the dispatcher directly
    with synthetic frames.
    """

    __slots__ = ("_listeners", "_nodes")

    def __init__(self, nodes: dict[str, NodeRecord]) -> None:
        """Bind to a node registry.

        Args:
            nodes: The same ``dict[str, NodeRecord]`` that
                :class:`IoXClient.LoadResult` produces. The dispatcher
                mutates ``record.properties`` in place when an event
                targets a known node; events for unknown addresses
                are dropped silently (typically nodes that joined
                after the initial load — re-run ``IoXClient.connect``
                or refresh the registry on node-add events).
        """
        self._nodes = nodes
        self._listeners: list[EventListener] = []

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
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("event listener raised; suppressing to keep loop alive")
        return event

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
