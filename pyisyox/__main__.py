"""Command-line entry point: connect to an eisy / Polisy, print a node
summary, and (by default) hold the WebSocket event stream open.

Run with ``python3 -m pyisyox <url> <email|admin> <password>``. The
URL determines the auth mode — port ``:443`` triggers PortalAuth (JWT
bearer); port ``:8443`` triggers LocalAuth (HTTP basic). Pass
``--no-events`` to skip starting the WebSocket reader. Pass
``--dump <path>`` to write a full controller snapshot
(:meth:`pyisyox.Controller.to_dict`) to disk as JSON. Logging defaults
to ``INFO``; ``-d/--debug`` adds parsed event frames + the lifecycle /
reconnect chatter, ``-v/--verbose`` additionally dumps raw WS frames
and full ``/api/*`` payloads.

It's a thin wrapper over the library — handy for connecting to a
controller from the shell, watching the event stream, or sanity-
checking credentials. Applications embedding pyisyox (Home Assistant,
hacs-udi-iox) construct :class:`pyisyox.Controller` directly rather
than shelling out to this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pyisyox import (
    Controller,
    LocalAuth,
    PortalAuth,
)
from pyisyox.logging import LOG_VERBOSE, enable_logging

if TYPE_CHECKING:
    from pyisyox.auth import Auth

_LOGGER = logging.getLogger("pyisyox.cli")


def _build_auth(url: str, username: str, password: str) -> Auth:
    """Pick PortalAuth (email) vs LocalAuth (admin) based on the username."""
    if "@" in username:
        return PortalAuth(username, password)
    return LocalAuth(username, password)


def _write_snapshot(controller: Controller, path: Path) -> None:
    """Write ``Controller.to_dict()`` to ``path`` as pretty-printed JSON.

    Parent dirs auto-create (mirrors v1-beta ``util/output.write_to_file``
    convenience). ``default=str`` is a guardrail for any datetime /
    unexpected object that slips through ``to_dict()``. Extracted into a
    sync helper so :func:`main` can off-load it via
    :func:`asyncio.to_thread` and stay friendly to async-only lint rules
    that ban blocking I/O in coroutines.
    """
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(controller.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


async def main(args: argparse.Namespace) -> int:
    """Connect, print a one-line summary per node, optionally hold open
    the event stream until Ctrl-C."""
    auth = _build_auth(args.url, args.username, args.password)
    controller = Controller(
        args.url,
        auth,
        tls_version=args.tls_version,
        verify_ssl=args.verify_ssl,
    )
    try:
        try:
            await controller.connect(start_websocket=args.events)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to connect — check URL, credentials, and network")
            return 1

        _LOGGER.info(
            "Connected to %s (uuid=%s, version=%s)",
            args.url,
            controller.config.uuid,
            controller.config.version,
        )
        _LOGGER.info("Loaded %d node(s)", len(controller.nodes))
        for address, node in controller.nodes.items():
            nodedef_id = node.nodedef_id or "—"
            prop_summary = ", ".join(
                f"{pid}={prop.formatted or prop.value}" for pid, prop in list(node.properties.items())[:3]
            )
            _LOGGER.info("  [%s] %s (%s) %s", address, node.name, nodedef_id, prop_summary)

        if args.dump is not None:
            path = Path(args.dump)
            await asyncio.to_thread(_write_snapshot, controller, path)
            _LOGGER.info("Wrote controller snapshot to %s", path)

        if args.events:
            _LOGGER.info("Event stream running — press Ctrl-C to exit")
            try:
                while True:
                    await asyncio.sleep(60)
            except (KeyboardInterrupt, asyncio.CancelledError):
                _LOGGER.info("Caught Ctrl-C; shutting down")
        return 0
    finally:
        await controller.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to an eisy / Polisy, print a node summary, and watch the event stream",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="Controller URL (e.g. https://eisy.local:443)")
    parser.add_argument("username", help="Portal email or local admin username")
    parser.add_argument("password", help="Portal or local admin password")
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Debug logging (parsed event frames, lifecycle, reconnects)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging — raw WS frames + full /api/* payloads; implies --debug",
    )
    parser.add_argument(
        "-q",
        "--no-events",
        dest="events",
        action="store_false",
        help="Skip the WebSocket event stream",
    )
    parser.add_argument(
        "--tls-version",
        type=float,
        default=None,
        choices=[1.2, 1.3],
        help="Pin TLS version (default: auto-negotiate)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Enforce SSL certificate verification (default: off; eisy ships self-signed)",
    )
    parser.add_argument(
        "--dump",
        metavar="PATH",
        default=None,
        help="Write Controller.to_dict() to PATH as pretty-printed JSON after load",
    )
    return parser.parse_args()


def _resolve_log_level(args: argparse.Namespace) -> int:
    """Pick the log level from the parsed args (verbose > debug > info)."""
    if args.verbose:
        return LOG_VERBOSE
    if args.debug:
        return logging.DEBUG
    return logging.INFO


def run() -> int:
    """CLI entry point: parse args, configure logging, run :func:`main`.

    Extracted from the ``if __name__ == "__main__"`` block so the
    bootstrap is exercisable from tests.
    """
    cli_args = parse_args()
    enable_logging(_resolve_log_level(cli_args))
    return asyncio.run(main(cli_args))


if __name__ == "__main__":
    sys.exit(run())
