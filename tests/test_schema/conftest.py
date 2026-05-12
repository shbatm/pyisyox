"""Shared fixtures for schema tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.schema import Profile

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"


@pytest.fixture(scope="session")
def profile_raw() -> dict:
    """Raw ``/rest/profiles`` JSON captured from a live eisy with native
    Insteon/Z-Wave nodes plus a Flume PG3 plugin slot installed.
    """
    return json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())


@pytest.fixture(scope="session")
def profile(profile_raw: dict) -> Profile:
    """Parsed :class:`Profile` from the captured fixture."""
    return Profile.load_from_json(profile_raw)
