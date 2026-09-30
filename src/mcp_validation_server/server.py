"""Server entry point: ``python3 -m mcp_validation_server``.

Also exposed as the ``mcp-validation-server`` console script. Both start the
same server over stdio, which is the transport an MCP client such as Claude
Desktop or Claude Code spawns as a subprocess and talks to on stdin/stdout.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import tools  # noqa: F401  — imported for its side effect: registers the tools
from .app import SERVER_NAME, SERVER_VERSION, mcp

__all__ = ["main", "serve_stdio"]

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def serve_stdio() -> None:
    """Run the server on stdio. Blocks until the client closes the connection."""
    mcp.run("stdio")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server.

    ``--transport`` selects the transport and defaults to ``stdio``, the one an
    MCP client spawns locally. ``--version`` prints the version and exits.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    if "--version" in args:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return 0

    if "--help" in args or "-h" in args:
        print(
            f"{SERVER_NAME} {SERVER_VERSION}\n"
            "\n"
            "Usage: python3 -m mcp_validation_server [--transport stdio|sse|streamable-http]\n"
            "\n"
            "Started with no arguments, this speaks MCP over stdin/stdout, which is what\n"
            "an MCP client expects when it spawns the server as a subprocess. Run it\n"
            "directly in a terminal and it will appear to hang: it is waiting for a\n"
            "client on stdin.\n"
        )
        return 0

    transport = "stdio"
    if "--transport" in args:
        position = args.index("--transport")
        if position + 1 >= len(args):
            print("error: --transport needs a value", file=sys.stderr)
            return 2
        transport = args[position + 1]

    if transport not in VALID_TRANSPORTS:
        print(
            f"error: unknown transport {transport!r}; expected one of {', '.join(VALID_TRANSPORTS)}",
            file=sys.stderr,
        )
        return 2

    mcp.run(transport)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
