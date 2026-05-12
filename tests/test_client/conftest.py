"""Shared test helpers — a fake aiohttp ClientSession that records every
request and returns scripted responses keyed on (method, path)."""

from __future__ import annotations

import json
from typing import Any

import pytest


class FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Pretend aiohttp ClientSession.

    Routes are queued by ``(method, path)``; each call pops the head of
    that route's response list. Use :meth:`set_route` to script an
    endpoint with one response that's reused, or :meth:`queue` to set a
    sequence (e.g. 401 then 200 for retry tests).
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._routes: dict[tuple[str, str], list[FakeResponse]] = {}
        self._defaults: dict[tuple[str, str], FakeResponse] = {}

    # --- scripting -----------------------------------------------------

    def set_route(self, method: str, path: str, status: int, body: str | dict | None = None) -> None:
        """Set a single default response for ``method path``. Always returns
        this response unless overridden via :meth:`queue` first."""
        self._defaults[(method.upper(), path)] = FakeResponse(status, _to_body(body))

    def queue(self, method: str, path: str, status: int, body: str | dict | None = None) -> None:
        """Append a one-shot response to the queue for ``method path``.

        Queued responses are consumed in FIFO order before the default
        kicks in — useful for tests that need ``[401, 200]`` to verify
        retry behaviour.
        """
        self._routes.setdefault((method.upper(), path), []).append(FakeResponse(status, _to_body(body)))

    # --- aiohttp.ClientSession surface ---------------------------------

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("POST", url, kwargs)

    # --- internals -----------------------------------------------------

    def _dispatch(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        path = url.removeprefix(self.base_url)
        self.calls.append((method, path, kwargs))
        key = (method, path)
        queued = self._routes.get(key)
        if queued:
            return queued.pop(0)
        if key in self._defaults:
            return self._defaults[key]
        raise AssertionError(f"no scripted response for {method} {path}")


def _to_body(body: str | dict | None) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    return json.dumps(body)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession(base_url="https://eisy.local")
