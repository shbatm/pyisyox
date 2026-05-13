"""Runtime ``Variable`` — typed wrapper for IoX controller variables.

The IoX controller exposes two variable types — integer (``"1"``) and
state (``"2"``); each carries a current value, an init/restore-on-
startup value, decimal precision, a user-assigned name, and a last-
change timestamp. The wrapper surfaces those as read-only properties
plus three mutation coroutines (``set_value`` / ``set_init`` /
``rename``) that route through the controller's
``POST /api/variables/{type}/{id}`` endpoint.

Sourced from the parsed :class:`VariableRecord` in
:mod:`pyisyox.client`. Each :class:`Variable` instance shares the
underlying record with the controller's loaded state — local mutations
update the record in place, and WS variable-change frames (the
``<var>`` payload on ``_1`` action 6/7 events) likewise update the
record so reads always reflect the latest value.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from pyisyox.client import VariableField

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, VariableRecord


def _coerce_numeric(value: float | str) -> int | float:
    """Coerce a caller-supplied value to ``int | float`` for the wire.

    Pass-through for ``int`` / ``float``. Strings are parsed as
    ``float`` if they carry a decimal point, otherwise ``int`` — this
    matches the legacy PyISY 3.x contract that accepted string values
    from generic callers.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if "." in text or "e" in text.lower():
        return float(text)
    return int(text)


class Variable:
    """User-facing handle for one controller variable."""

    __slots__ = ("_client", "_record")

    def __init__(self, record: VariableRecord, client: IoXClient) -> None:
        """Bind a :class:`VariableRecord` to the controller's HTTP client."""
        self._record = record
        self._client = client

    @classmethod
    def from_record(cls, record: VariableRecord, client: IoXClient) -> Variable:
        """Construct a :class:`Variable` from a parsed record."""
        return cls(record=record, client=client)

    # --- introspection ------------------------------------------------

    @property
    def type_id(self) -> str:
        """Variable type — ``"1"`` (integer) or ``"2"`` (state)."""
        return self._record.type_id

    @property
    def id(self) -> str:
        """Variable id within its type (string for ergonomic joins)."""
        return self._record.id

    @property
    def address(self) -> str:
        """Composite ``"{type_id}.{id}"`` identifier."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned label."""
        return self._record.name

    @property
    def value(self) -> int | float:
        """Current value (wire field ``val``).

        Reads reflect the latest write — mutations via :meth:`set_value`
        update the underlying record in place after a successful POST.

        Type is ``int | float``: most variables read back as ``int`` from
        the wire (``/api/variables/{type}`` parses ``"val"`` as int), but
        a controller may surface ``float`` on a fresh write that posted a
        non-integer (the modern ``POST /api/variables/{type}/{id}``
        endpoint accepts floats and the wrapper stores whatever was sent
        on success).
        """
        return self._record.value

    @property
    def init(self) -> int | float:
        """Restore-on-startup value. Same int-or-float surface as :attr:`value`."""
        return self._record.init

    @property
    def precision(self) -> int:
        """Decimal precision. ``displayed = raw / 10**precision``."""
        return self._record.precision

    @property
    def ts(self) -> str:
        """Last-change timestamp as the controller emits it.

        ISO 8601 UTC string when present, ``""`` when the controller
        doesn't stamp the entry (e.g. freshly created variables before
        the first change).
        """
        return self._record.ts

    # --- mutation -----------------------------------------------------

    async def set_value(self, value: float) -> None:
        """Set the current value of this variable.

        Wire shape: ``POST /api/variables/{type}/{id}`` with body
        ``{"value": <number>}``. The modern endpoint accepts both
        ``int`` and ``float`` — for a ``precision > 0`` variable, send
        the *displayed* float (e.g. ``51.5``) and the controller
        applies the ``* 10**precision`` scale on store. Sending an
        ``int`` on the same variable means the controller stores it
        verbatim (no scale applied), which produces a mismatch
        between consumer-displayed and controller-internal values — so
        callers driving displayed-unit UIs should send floats.

        Strings are tolerated for legacy callers (parsed as float if
        they contain a decimal point, else int).

        Updates the underlying record on success so subsequent reads
        of :attr:`value` reflect the new state without waiting for a
        WS frame.
        """
        new_value = _coerce_numeric(value)
        await self._client.post_variable_update(
            self._record.type_id, self._record.id, {VariableField.VALUE: new_value}
        )
        self._record.value = new_value

    async def set_init(self, init: float) -> None:
        """Set the init / restore-on-startup value.

        Wire shape: ``POST /api/variables/{type}/{id}`` with
        ``{"init": <number>}``. Same int-or-float semantics as
        :meth:`set_value`.
        """
        new_init = _coerce_numeric(init)
        await self._client.post_variable_update(
            self._record.type_id, self._record.id, {VariableField.INIT: new_init}
        )
        self._record.init = new_init

    async def rename(self, name: str) -> None:
        """Rename this variable on the controller.

        Wire shape: ``POST /api/variables/{type}/{id}`` with
        ``{"name": "<str>"}``.
        """
        await self._client.post_variable_update(
            self._record.type_id, self._record.id, {VariableField.NAME: name}
        )
        self._record.name = name

    def to_dict(self) -> dict[str, Any]:
        """Flatten this variable to a JSON-compatible dict."""
        return asdict(self._record)

    def __repr__(self) -> str:
        return f"Variable(type_id={self.type_id!r}, id={self.id!r}, name={self.name!r}, value={self.value})"
