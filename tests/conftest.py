"""Shared pytest configuration for the pyisyox suite.

Pin the system timezone to UTC so dispatcher tests that round-trip
WS controller-local timestamps through ``datetime.astimezone()`` get
deterministic results regardless of the host's tz. CI runners default
to UTC; this guards against "passes on CI, fails on developer laptop
in CDT" surprises.
"""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture(autouse=True, scope="session")
def _force_utc_tz() -> None:
    os.environ["TZ"] = "UTC"
    time.tzset()
