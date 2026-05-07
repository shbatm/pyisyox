"""Editor dataclasses and bidirectional codec for IoX profile editors.

In IoX 6's profile schema an *editor* is referenced by id (e.g. ``"I_OL"``,
``"I_TSTAT_MODE"``, ``"GALLONS"``) from both nodedef properties and
command parameters. The editor is **not just display metadata** — it
defines a bidirectional contract:

- Read-side: how a raw integer value reported by the controller decodes
  to a display string and unit.
- Write-side: which integer values are *valid* to send as a command
  parameter, and how a user-provided enum name maps back to the integer
  the controller expects.

Three write-side fields beyond UOM/min/max/prec:

* ``min``/``max`` plus ``prec`` (decimal precision) — slider bounds and
  outbound validation.
* ``subset`` — narrower than min/max. ``"0-3,5-7"`` excludes 4. Outbound
  commands MUST respect this.
* ``names`` — enum option list AND int↔string mapping both directions.

The same editor commonly applies to both a property and a related command
parameter (``I_TSTAT_MODE`` covers reading ``CLIMD`` state and writing
``CLIMD`` setpoints), so editor-handling code is one shared codec.

Source schema: ``/rest/profiles`` ``editors[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _parse_subset(spec: str) -> set[int]:
    """Expand a subset spec like ``"0-3,5-7"`` into ``{0,1,2,3,5,6,7}``."""
    out: set[int] = set()
    for raw_piece in spec.split(","):
        piece = raw_piece.strip()
        if not piece:
            continue
        if "-" in piece:
            lo_s, hi_s = piece.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            out.update(range(lo, hi + 1))
        else:
            out.add(int(piece))
    return out


@dataclass(slots=True)
class EditorRange:
    """One range entry within an editor.

    An editor may carry multiple ranges (e.g., a temperature editor with
    Fahrenheit and Celsius variants), each tied to a distinct UOM.

    Attributes:
        uom: Unit-of-measure identifier (string, indexes into the IoX UOM
            table).
        min: Lower numeric bound for raw values (inclusive). ``None`` when
            the range is purely enumerative (subset only).
        max: Upper numeric bound (inclusive).
        prec: Decimal precision applied to raw values (e.g., raw ``6839``
            with ``prec=4`` displays as ``0.6839``).
        subset: Resolved set of valid raw integers, narrower than
            ``[min, max]``. Empty when the full ``[min, max]`` range is valid.
        names: Mapping of raw integer → display name for enumerated values
            (e.g., ``{0: "Off", 1: "Heat", 2: "Cool"}``).
    """

    uom: str
    min: float | None = None
    max: float | None = None
    prec: int = 0
    subset: set[int] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict) -> EditorRange:
        """Build a range from a JSON object."""
        subset_raw = raw.get("subset")
        names_raw = raw.get("names", {}) or {}
        return cls(
            uom=str(raw.get("uom", "0")),
            min=raw.get("min"),
            max=raw.get("max"),
            prec=int(raw.get("prec", 0)),
            subset=_parse_subset(subset_raw) if isinstance(subset_raw, str) else set(),
            names={int(k): v for k, v in names_raw.items()},
        )

    def is_valid(self, raw_value: int) -> bool:
        """True if ``raw_value`` is acceptable for outbound commands."""
        if self.subset:
            return raw_value in self.subset
        if self.min is not None and raw_value < self.min:
            return False
        return not (self.max is not None and raw_value > self.max)


class EditorCodecError(ValueError):
    """Raised when an editor codec cannot encode or decode a value."""


@dataclass(slots=True)
class Editor:
    """A profile editor — bidirectional codec for property and parameter values.

    Encoding direction (``encode``): user input (int or enum name) → raw int
    suitable to send to the controller, with subset/range validation.

    Decoding direction (``decode``): raw int from the controller → display
    string (enum name if known, else formatted number with prec/uom).

    For multi-range editors the codec selects the range whose UOM matches a
    caller-supplied ``uom`` hint, falling back to the first range. Most
    editors carry a single range.
    """

    id: str
    ranges: list[EditorRange] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict) -> Editor:
        """Build an :class:`Editor` from a JSON object."""
        ranges = [EditorRange.from_json(r) for r in raw.get("ranges", [])]
        return cls(id=raw["id"], ranges=ranges)

    def range_for(self, uom: str | None = None) -> EditorRange:
        """Pick the range matching ``uom``, or the first range if no hint."""
        if not self.ranges:
            raise EditorCodecError(f"Editor {self.id!r} has no ranges")
        if uom is not None:
            for r in self.ranges:
                if r.uom == uom:
                    return r
        return self.ranges[0]

    def decode(self, raw_value: float, uom: str | None = None) -> str:
        """Decode a raw value to its display string.

        Enum lookup first (when ``names`` covers the value), otherwise a
        precision-aware numeric string. Does not append the unit — callers
        format the unit separately based on the range's ``uom``.
        """
        rng = self.range_for(uom)
        if isinstance(raw_value, int) or (isinstance(raw_value, float) and raw_value.is_integer()):
            ival = int(raw_value)
            if ival in rng.names:
                return rng.names[ival]
        if rng.prec:
            return f"{raw_value / (10**rng.prec):.{rng.prec}f}"
        return str(raw_value)

    def encode(self, value: float | str, uom: str | None = None) -> int:
        """Encode user input to a raw integer the controller will accept.

        Accepts:
            * int/float — taken as the raw value directly (after rounding).
            * str — first looked up in the range's ``names`` (case-insensitive),
              else parsed as int.

        Raises :class:`EditorCodecError` if the resulting value falls outside
        the range's ``subset`` or ``[min, max]``.
        """
        rng = self.range_for(uom)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise EditorCodecError(f"Editor {self.id!r}: empty input")
            lowered = stripped.lower()
            inverse = {n.lower(): k for k, n in rng.names.items()}
            if lowered in inverse:
                raw = inverse[lowered]
            else:
                try:
                    raw = int(stripped)
                except ValueError as exc:
                    valid = sorted(rng.names.values())
                    raise EditorCodecError(
                        f"Editor {self.id!r}: {value!r} is not a recognised name (valid: {valid})"
                    ) from exc
        else:
            raw = round(value)
        if not rng.is_valid(raw):
            raise EditorCodecError(
                f"Editor {self.id!r}: value {raw} is not valid "
                f"(subset={sorted(rng.subset) if rng.subset else None}, "
                f"min={rng.min}, max={rng.max})"
            )
        return raw
