"""Coverage for ``pyisyox/__init__.py`` — namely the
``__version__`` resolution fallback that fires when the package isn't
installed (e.g. running from a source checkout without ``pip install -e``)."""

from __future__ import annotations

import importlib
import importlib.metadata as _md

import pytest

import pyisyox


def test_version_is_a_string() -> None:
    """Whatever path resolves it (real metadata or the fallback), the
    public ``__version__`` is always a string."""
    assert isinstance(pyisyox.__version__, str)
    assert pyisyox.__version__


def test_version_falls_back_to_unknown_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``importlib.metadata.version`` raises
    ``PackageNotFoundError`` (the package isn't installed under that
    name), the fallback path sets ``__version__ = "unknown"``.

    Patch the source — ``importlib.metadata.version`` — so reload-time
    re-import of the symbol still picks up the patched callable.
    """

    def _raise(*_: object, **__: object) -> str:
        raise _md.PackageNotFoundError("pyisyox")

    monkeypatch.setattr(_md, "version", _raise)
    reloaded = importlib.reload(pyisyox)
    try:
        assert reloaded.__version__ == "unknown"
    finally:
        # Restore the real metadata-driven version so other tests
        # observing ``pyisyox.__version__`` aren't polluted.
        monkeypatch.undo()
        importlib.reload(pyisyox)
