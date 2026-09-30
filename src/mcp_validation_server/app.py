"""The server instance.

Kept in its own module so that :mod:`mcp_validation_server.tools` can import it
and register tools with the ``@mcp.tool()`` decorator without a circular import.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

SERVER_NAME = "mcp-validation-server"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
Deterministic data-validation tools. Use these when a claim about a dataset needs
to be checked rather than asserted: to validate rows against a column contract, to
confirm that a period of arithmetic closes, to count distinct entities rather than
rows, or to surface suspected duplicates for review.

Every tool returns a JSON result with the same envelope: `ok`, `verdict`,
`findings`, and `guidance`. A validation failure is a RESULT, not an error. When a
tool returns `ok: false` the call succeeded — the data failed. Read the `findings`
array, which gives a stable `code`, a human-readable `message`, and a `locator`
naming the exact row (and column) for each problem. The `guidance` field says what
to do next.

Nothing here drops, merges, repairs, or overwrites data. Rows that fail are
quarantined and reported; suspected duplicates are returned for a human to decide.
Do not describe a dataset as validated unless the tool returned `ok: true`.
"""

mcp = MCPServer(
    name=SERVER_NAME,
    title="Data Validation",
    version=SERVER_VERSION,
    instructions=INSTRUCTIONS,
)

__all__ = ["INSTRUCTIONS", "SERVER_NAME", "SERVER_VERSION", "mcp"]
