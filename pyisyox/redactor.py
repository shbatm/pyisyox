"""Log redactor for sensitive fields in eisy responses.

The ``POST /api/login`` response leaks the PG3 MQTT TLS keypair under
``data.ssl`` (verified in HAR captures, 2026-05-06). Access and refresh
tokens are also sensitive. Apply :func:`redact_sensitive` to any
debug-level dump of a JSON response body before logging.

Redactor walks the payload recursively so nested envelopes
(``{"successful": true, "data": {...}}``) are still scrubbed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: Keys whose values are replaced with ``<redacted>`` regardless of their
#: position in the payload tree.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "accessToken",
        "refreshToken",
        "ssl",
        "token",
        "clientToken",
        "password",
        "Authorization",
        "authorization",
        "Cookie",
        "cookie",
        "Set-Cookie",
        "setCookie",
    }
)

REDACTED = "<redacted>"


def redact_sensitive(payload: Any, *, sensitive_keys: frozenset[str] = SENSITIVE_KEYS) -> Any:
    """Return a deep copy of ``payload`` with sensitive values replaced.

    Walks dicts and lists recursively. Non-container values pass through
    unchanged. The original ``payload`` is not mutated.

    Args:
        payload: A JSON-shaped value (dict, list, or primitive). Most
            commonly a dict from an aiohttp response body.
        sensitive_keys: Override the default set of keys to redact. Use
            for tests or for redacting additional auth-domain keys.

    Returns:
        A deep copy with sensitive values replaced by :data:`REDACTED`.
    """
    cloned = deepcopy(payload)
    _walk(cloned, sensitive_keys)
    return cloned


def _walk(node: Any, sensitive_keys: frozenset[str]) -> None:
    """In-place redact pass over ``node`` and its descendants."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key in sensitive_keys:
                node[key] = REDACTED
            else:
                _walk(node[key], sensitive_keys)
    elif isinstance(node, list):
        for item in node:
            _walk(item, sensitive_keys)
