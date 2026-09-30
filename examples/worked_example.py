"""The worked example from the README, run for real.

Starts the server over stdio, calls ``validate_rows`` on deliberately broken data,
and prints the exact arguments sent and the exact result returned. The output of
this script is what the README shows.

It also writes the result to ``examples/worked_example_output.json`` so the
README's quoted output can be checked against a file in the repository.

    python3 examples/worked_example.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "examples" / "worked_example_output.json"

ARGUMENTS = {
    "rows": [
        {"invoice": "INV-1001", "amount": "100.50", "date": "2026-04-01"},
        {"invoice": "", "amount": "20", "date": "2026-04-02"},
        {"invoice": "INV-1003", "amount": "-5", "date": "2026-04-03"},
        {"invoice": "INV-1004", "amount": "abc", "date": "04/05/2026"},
    ],
    "columns": [
        {"name": "invoice", "type": "string", "required": True, "pattern": r"INV-\d+"},
        {"name": "amount", "type": "number", "min": 0},
        {"name": "date", "type": "date", "required": True},
    ],
    "row_key": "invoice",
}


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_validation_server"],
        cwd=str(REPO_ROOT),
    )

    print("CALL tools/call")
    print("  name: validate_rows")
    print("  arguments:")
    for line in json.dumps(ARGUMENTS, indent=4).splitlines():
        print(f"    {line}")

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("validate_rows", ARGUMENTS)

    print()
    print(f"RESULT is_error={result.is_error}")
    print(json.dumps(result.structured_content, indent=2))

    OUTPUT_FILE.write_text(
        json.dumps(
            {"arguments": ARGUMENTS, "is_error": result.is_error, "result": result.structured_content},
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
