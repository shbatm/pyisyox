"""Editor dataclasses and bidirectional codec for IoX profile editors.

An *editor* (e.g. ``"I_OL"``, ``"I_TSTAT_MODE"``) is referenced by both
nodedef properties and command parameters and defines a bidirectional
contract — read-side decode and write-side validation/encode. Write-side
constraints beyond ``min``/``max``/``prec`` are the ``subset`` mask
(``"0-3,5-7"`` excludes 4) and the ``names`` enum option list.

Send-side scaling
-----------------

The controller does **all** device-side scaling itself, keyed off the
UOM appended to the ``/cmd`` URL — proven on hardware:
``/cmd/DON/100/100`` → 39 % (100 read as a UOM-100 0-255 byte) vs
``/cmd/DON/100/51`` → 100 % (100 read as UOM-51 percent). So the codec
**validates** input (enum-name resolution, ``min``/``max``, ``subset``)
but sends the *displayed* value verbatim with its range UOM; it does
**not** rewrite the number (no ``*10**prec`` rescale, no half-degree doubling).
The eisy web UI does the same (``/cmd/setTemp/10.4/17``). ``decode``
keeps its precision-aware formatting for display helpers, but the
property read path normalises by the wire UOM and never calls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: UOMs that use the legacy "raw is 2x displayed" half-degree encoding.
#: ``101`` is the IoX 6+ id; ``"degrees"`` is the ISY-v4 alias kept for
#: legacy profiles. Mirrored in :mod:`pyisyox.helpers` for the
#: ``/rest/status`` decode path.
_HALF_DEGREE_UOMS = frozenset({"101", "degrees"})


def _parse_subset(spec: str, lo_default: int | None = None, hi_default: int | None = None) -> set[int]:
    """Expand a subset spec like ``"0-3,5-7"`` into ``{0,1,2,3,5,6,7}``.

    Some IoX 6.x firmware (eisy 6.0.5) emits an open-ended bound —
    ``"5-"``, ``"-7"``, ``"-"`` — resolved against ``lo_default`` /
    ``hi_default`` (the editor's min/max or names-index extremes). A
    piece that still can't resolve (or is garbage) is skipped rather
    than raising and aborting the whole profile load.
    """
    out: set[int] = set()
    for raw_piece in spec.split(","):
        piece = raw_piece.strip()
        if not piece:
            continue
        try:
            if "-" in piece:
                lo_s, hi_s = (p.strip() for p in piece.split("-", 1))
                lo = int(lo_s) if lo_s else lo_default
                hi = int(hi_s) if hi_s else hi_default
                if lo is None or hi is None:
                    continue
                out.update(range(lo, hi + 1))
            else:
                out.add(int(piece))
        except ValueError:
            continue
    return out


def _decode_signed_bound(token: str) -> int:
    """Decode an encoded-editor-id numeric bound; a leading ``m`` = negative."""
    return -int(token[1:]) if token.startswith("m") else int(token)


def _subset_from_hex_masks(low_hex: str, high_hex: str | None) -> set[int]:
    """Decode the ``_S_`` bitmask form: bit *i* set ⇒ value *i* is valid.

    ``low_hex`` covers bits 0-31; the optional ``high_hex`` bits 32-63.
    e.g. ``"FF00FF00"`` → ``{8..15, 24..31}``.
    """
    out: set[int] = set()
    low = int(low_hex, 16)
    for i in range(32):
        if low & (1 << i):
            out.add(i)
    if high_hex is not None:
        high = int(high_hex, 16)
        for i in range(32):
            if high & (1 << i):
                out.add(32 + i)
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
        precision: Decimal precision applied to raw values (e.g., raw
            ``6839`` with ``precision=4`` displays as ``0.6839``). The
            wire keys it as ``"prec"``; Python attribute spells it out.
        subset: Resolved set of valid raw integers, narrower than
            ``[min, max]``. Empty when the full ``[min, max]`` range is valid.
        names: Mapping of raw integer → display name for enumerated values
            (e.g., ``{0: "Off", 1: "Heat", 2: "Cool"}``).
        step: Increment hint for numeric (slider-shaped) ranges, in
            *displayed* units — e.g. ``0.5`` on a half-degree setpoint
            editor. ``None`` when the editor doesn't specify one (callers
            then derive a step from ``precision``). Not used by the codec;
            it's a UI hint, surfaced for consumers that build number
            entities.
        nls_prefix: The NLS string-table prefix this range's enum option
            names live under (the ``_N_<nls>`` segment of an encoded
            editor id, or a named editor's index nls). ``names`` stays
            empty until something resolves it against an NLS table (the
            controller does it inline for ``/rest/profiles`` families;
            :meth:`Profile.find_editor` does it for encoded editors when
            the profile has an NLS table loaded).
    """

    uom: str
    min: float | None = None
    max: float | None = None
    precision: int = 0
    subset: set[int] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)
    step: float | None = None
    nls_prefix: str | None = None

    @classmethod
    def from_json(cls, raw: dict) -> EditorRange:
        """Build a range from a JSON object."""
        subset_raw = raw.get("subset")
        names_raw = raw.get("names", {}) or {}
        names = {int(k): v for k, v in names_raw.items()}
        step_raw = raw.get("step")
        # Open-ended-bound floor/ceiling: own min/max, else names extremes.
        rng_min, rng_max = raw.get("min"), raw.get("max")
        lo_default = int(rng_min) if isinstance(rng_min, (int, float)) else (min(names) if names else None)
        hi_default = int(rng_max) if isinstance(rng_max, (int, float)) else (max(names) if names else None)
        return cls(
            uom=str(raw.get("uom", "0")),
            min=rng_min,
            max=rng_max,
            precision=int(raw.get("prec", 0)),
            subset=(
                _parse_subset(subset_raw, lo_default, hi_default) if isinstance(subset_raw, str) else set()
            ),
            names=names,
            step=float(step_raw) if isinstance(step_raw, (int, float)) else None,
        )

    def is_valid(self, raw_value: int) -> bool:
        """True if ``raw_value`` is acceptable for outbound commands.

        Used for **subset** validation only (prec=0, enum-shaped editors).
        Numeric editors with ``prec>0`` validate the displayed value and
        send it as-is (no scaling — the controller scales device-side
        from the UOM; see :meth:`Editor.encode`). ``min``/``max`` in the
        IoX schema are stored in **displayed form** (e.g. ``min=5.0`` on
        a UOM-4 °C setpoint editor with ``prec=1`` means 5.0 °C, not raw
        5).
        """
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

    @classmethod
    def from_encoded_id(cls, editor_id: str) -> Editor | None:
        """Decode a self-describing *encoded editor id* into an :class:`Editor`.

        IoX lets a simple editor be referenced by an id that fully
        encodes its (single) range instead of pointing at a named
        ``<editor>`` element — handy for the dynamically-generated
        Z-Wave nodedefs where most editors are spelled inline. The
        grammar (see
        https://developer.isy.io/docs/API/IoX/editors#encoded-editor-id):

        * ``_<uom>_<prec>`` — implied bounds ``[-2147483647, 2147483647]``
        * optionally one of
          ``_R_<min>_<max>`` (numeric range; a leading ``m`` makes a
          bound negative — ``_17_2_R_m5_10`` => -5..10) or
          ``_S_<lowMask>[_<highMask>]`` (subset as a 32/64-bit hex
          bitmask — ``_17_1_S_FF00FF00`` ⇒ ``{8..15, 24..31}``)
        * optionally a trailing ``_N_<nls>`` NLS-prefix segment

        Returns ``None`` if ``editor_id`` doesn't parse as an encoding
        (so callers can fall back to a table lookup). The ``_N_<nls>``
        segment is captured as ``EditorRange.nls_prefix`` but not
        resolved here — :meth:`Profile.find_editor` fills ``names`` from
        it when the profile carries an NLS table. Range / subset
        validation works regardless.
        """
        if not editor_id.startswith("_"):
            return None
        parts = editor_id.split("_")[1:]  # drop the leading empty token
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        uom, prec_s, rest = parts[0], parts[1], parts[2:]
        # Peel an optional trailing ``_N_<nls>`` (nls may itself contain "_").
        nls_prefix: str | None = None
        if "N" in rest:
            nidx = rest.index("N")
            nls_prefix = "_".join(rest[nidx + 1 :]) or None
            rest = rest[:nidx]
        rng_min: float | None = None
        rng_max: float | None = None
        subset: set[int] = set()
        try:
            if not rest:
                pass
            elif rest[0] == "R" and len(rest) == 3:
                rng_min = _decode_signed_bound(rest[1])
                rng_max = _decode_signed_bound(rest[2])
            elif rest[0] == "S" and len(rest) in (2, 3):
                subset = _subset_from_hex_masks(rest[1], rest[2] if len(rest) == 3 else None)
            else:
                return None
        except ValueError:
            return None
        return cls(
            id=editor_id,
            ranges=[
                EditorRange(
                    uom=uom,
                    min=rng_min,
                    max=rng_max,
                    precision=int(prec_s),
                    subset=subset,
                    nls_prefix=nls_prefix,
                )
            ],
        )

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

        When ``uom`` isn't given and the editor has multiple ranges, the
        enum-name lookup scans every range (so e.g. an editor whose first
        range is a 0-100 % scale and whose second is a tiny ``{1: "Previous
        Value"}`` index still decodes ``1`` to its name).

        UOM-101 / "degrees" with ``prec=0`` halves the raw value (Insteon
        half-degree convention).
        """
        rng = self.range_for(uom)
        if isinstance(raw_value, int) or (isinstance(raw_value, float) and raw_value.is_integer()):
            ival = int(raw_value)
            if ival in rng.names:
                return rng.names[ival]
            if uom is None:
                for other in self.ranges:
                    if ival in other.names:
                        return other.names[ival]
        if rng.precision:
            return f"{raw_value / (10**rng.precision):.{rng.precision}f}"
        if rng.uom in _HALF_DEGREE_UOMS:
            return f"{raw_value / 2.0:.1f}"
        return str(raw_value)

    def encode(self, value: float | str, uom: str | None = None) -> int | float:
        """Validate user input and return the value to put on the wire.

        Two paths within a range:

        * **Enum name (str matching ``names``)** — returns the matching
          raw int verbatim. ``min``/``max`` don't apply.
        * **Numeric (int/float, or string parsed as float)** — the
          *displayed* value. Validated against ``[min, max]`` and the
          ``subset`` mask (both stored in displayed form), then returned
          **as-is** (int when integral, else float). The controller does
          device-side scaling from the appended UOM — the codec does not
          rewrite the number (no ``*10**prec`` rescale, no half-degree doubling).

        When ``uom`` is given, only that range is tried. Otherwise every
        range is tried in order and the first that accepts ``value`` wins
        — multi-range editors like ``ZW_DIM_PERCENT`` (range 0 is a tiny
        ``{1: "Previous Value"}`` index, range 1 is the 0-100 % scale)
        need this so a plain ``75`` lands in the percent range instead of
        being rejected by the index range.

        Raises :class:`EditorCodecError` if no range accepts ``value``.
        """
        return self.encode_param(value, uom)[0]

    def encode_param(self, value: float | str, uom: str | None = None) -> tuple[int | float, str]:
        """Like :meth:`encode`, but also returns the UOM of the range used.

        Command-send code appends each parameter as ``/{value}/{uom}``
        and the controller scales device-side from that UOM (proven:
        ``/cmd/DON/100/100`` → 39 %, ``/cmd/DON/100/51`` → 100 %), so the
        UOM has to be the one belonging to the range that actually
        accepted the value, not always ``ranges[0]`` — for a multi-range
        editor like ``ZW_DIM_PERCENT`` a plain ``75`` is encoded by the
        0-100 % range (uom ``51``), so ``/cmd/DON/75/51`` goes on the
        wire, not ``/cmd/DON/75/25``.
        """
        if uom is not None:
            rng = self.range_for(uom)
            return self._encode_in_range(rng, value), rng.uom
        ranges = self.ranges or [self.range_for()]  # range_for() raises if truly empty
        last_error: EditorCodecError | None = None
        for rng in ranges:
            try:
                return self._encode_in_range(rng, value), rng.uom
            except EditorCodecError as exc:
                last_error = exc
        raise last_error or EditorCodecError(f"Editor {self.id!r}: cannot encode {value!r}")

    def _encode_in_range(self, rng: EditorRange, value: float | str) -> int | float:
        """Validate ``value`` against a single range; return it as-is.

        Enum names resolve to their raw int. Numeric input is range- and
        subset-checked in *displayed* units and returned unchanged (int
        when integral so the URL stays ``/72`` not ``/72.0``, else float
        so a ``/cmd/setTemp/10.4/17`` survives). The controller does the
        precision / unit scaling from the appended UOM — see the module
        docstring.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise EditorCodecError(f"Editor {self.id!r}: empty input")
            lowered = stripped.lower()
            inverse = {n.lower(): k for k, n in rng.names.items()}
            if lowered in inverse:
                return inverse[lowered]
            try:
                numeric: float = float(stripped)
            except ValueError as exc:
                valid = sorted(rng.names.values())
                raise EditorCodecError(
                    f"Editor {self.id!r}: {value!r} is not a recognised name (valid: {valid})"
                ) from exc
        else:
            numeric = float(value)

        if rng.min is not None and numeric < rng.min:
            raise EditorCodecError(f"Editor {self.id!r}: {numeric} is below min={rng.min}")
        if rng.max is not None and numeric > rng.max:
            raise EditorCodecError(f"Editor {self.id!r}: {numeric} is above max={rng.max}")
        # Subset masks are discrete integer/index sets (prec-0 index
        # editors; never co-occur with prec>0). Reject non-integral
        # input outright — a fractional index is meaningless and must
        # not reach the wire.
        if rng.subset:
            if not numeric.is_integer():
                raise EditorCodecError(
                    f"Editor {self.id!r}: {numeric} is not a valid index (subset {sorted(rng.subset)})"
                )
            if int(numeric) not in rng.subset:
                raise EditorCodecError(
                    f"Editor {self.id!r}: value {int(numeric)} is not in subset {sorted(rng.subset)}"
                )
        return int(numeric) if numeric.is_integer() else numeric
