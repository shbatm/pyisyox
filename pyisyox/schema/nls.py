"""IoX NLS (National Language Support) string-table parsing & lookup.

NLS tables are flat ``KEY = VALUE`` text files served per profile family at
``/rest/profiles/family/{family}/profile/{instance}/download/nls/en_US.txt``.
They carry the human-readable labels that the structured ``/rest/profiles``
JSON bakes inline for its families but the *dynamically* generated Z-Wave
``def/get`` XML does not — a ``UZW*`` nodedef's ``<cmd id="FDUP"/>`` arrives
with no name, and the label ("Fade Up") only exists in the NLS table.

Key shapes this module resolves (``<base>`` = the nodedef's numeric ``nls``
attribute, used for device-class overrides):

* ``CMD-[<base>-]<id>-NAME``    command label  (e.g. ``CMD-FDUP-NAME``)
* ``ST-[<base>-]<id>-NAME``     property label (e.g. ``ST-ST-NAME``)
* ``NDN-<base>-NAME``           nodedef display name
* ``<prefix>-<int>``            enum option for an editor that names an NLS
                                 prefix (encoded ``_N_<prefix>`` or a named
                                 editor) — e.g. ``IX_DIM_REP-0 = Off``

Family ``-1`` is the GLOBAL table (generic command / status names); a
per-radio family (``4`` = Z-Wave, ``12`` = Z-Matter) overlays it with
device-specific overrides and enum names.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Family id of the GLOBAL NLS table (generic, radio-independent labels).
GLOBAL_NLS_FAMILY_ID = "-1"


@dataclass(slots=True)
class NLSTable:
    """A parsed NLS string table — a flat ``key -> value`` map plus the
    handful of IoX key-shape lookups callers actually need.
    """

    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> NLSTable:
        """Parse ``KEY = VALUE`` text. Blank lines and ``#`` comments are
        skipped; the value keeps everything after the first ``=`` (so
        format strings containing ``=`` survive)."""
        entries: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, sep, value = stripped.partition("=")
            if not sep:
                continue
            key = key.strip()
            if key:
                entries[key] = value.strip()
        return cls(entries=entries)

    def overlay(self, other: NLSTable) -> NLSTable:
        """Return a new table with ``other``'s entries layered on top of
        this one's (``other`` wins on key collisions)."""
        merged = dict(self.entries)
        merged.update(other.entries)
        return NLSTable(entries=merged)

    def _first(self, *keys: str) -> str | None:
        for key in keys:
            value = self.entries.get(key)
            if value:
                return value
        return None

    def command_name(self, command_id: str, base: str | None = None) -> str | None:
        """Label for a command id, preferring the nodedef-scoped override."""
        if base:
            return self._first(f"CMD-{base}-{command_id}-NAME", f"CMD-{command_id}-NAME")
        return self._first(f"CMD-{command_id}-NAME")

    def property_name(self, property_id: str, base: str | None = None) -> str | None:
        """Label for a property id, preferring the nodedef-scoped override."""
        if base:
            return self._first(f"ST-{base}-{property_id}-NAME", f"ST-{property_id}-NAME")
        return self._first(f"ST-{property_id}-NAME")

    def nodedef_name(self, base: str) -> str | None:
        """Default display name for a nodedef by its ``nls`` base."""
        return self._first(f"NDN-{base}-NAME")

    def enum_names(self, prefix: str) -> dict[int, str]:
        """All ``<prefix>-<int> = label`` entries as an ``{int: label}`` map.

        Used to resolve the option labels of an editor that references an
        NLS prefix (e.g. the encoded ``_..._N_IX_DIM_REP`` editor's
        ``0 -> "Off"`` / ``101 -> "Unknown"``). Non-integer suffixes
        (``-NAME``, ``-FMT``, …) are ignored.
        """
        out: dict[int, str] = {}
        needle = f"{prefix}-"
        for key, value in self.entries.items():
            if not key.startswith(needle):
                continue
            try:
                out[int(key[len(needle) :])] = value
            except ValueError:
                continue
        return out
