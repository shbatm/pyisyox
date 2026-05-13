"""Runtime ``NetworkResource`` — fire-trigger wrapper for the IoX networking module.

Network resources are user-defined HTTP / TCP / UDP fire-triggers
configured in the IoX admin UI. They have no live state and no
parameters: the only operation is "fire by id". The wrapper exposes
the structural fields (``address``, ``name``) and a single
:meth:`run` coroutine.

Sourced from the legacy ``GET /rest/networking/resources`` XML
endpoint (``<NetConfig><NetRule>...``). Modern IoX 6 firmware keeps
this endpoint for compatibility; no ``/api/networking`` equivalent
has been observed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, NetworkResourceRecord


class NetworkResource:
    """User-facing handle for one networking module resource."""

    __slots__ = ("_client", "_record")

    def __init__(self, record: NetworkResourceRecord, client: IoXClient) -> None:
        """Wrap a parsed :class:`NetworkResourceRecord`."""
        self._record = record
        self._client = client

    @property
    def address(self) -> str:
        """Resource id (string for symmetry with node / group records)."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned label."""
        return self._record.name

    async def run(self) -> None:
        """Fire this network resource.

        Wire shape: ``GET /rest/networking/resources/{id}``. The
        controller acknowledges receipt only — the response doesn't
        carry the result of the underlying HTTP / TCP / UDP fire,
        and there's no progress event on the WebSocket. Treat this
        as fire-and-forget.
        """
        await self._client.run_network_resource(self._record.address)

    def to_dict(self) -> dict[str, Any]:
        """Flatten this resource to a JSON-compatible dict."""
        return asdict(self._record)

    def __repr__(self) -> str:
        return f"NetworkResource(address={self.address!r}, name={self.name!r})"
