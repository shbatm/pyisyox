"""Compatibility shim — re-exports stdlib :class:`enum.StrEnum`.

Kept as a thin alias so existing imports of ``pyisyox.util.backports.StrEnum``
continue to work; with the 3.11 baseline the backport implementation is no
longer needed. Prefer importing :class:`enum.StrEnum` directly in new code.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["StrEnum"]
