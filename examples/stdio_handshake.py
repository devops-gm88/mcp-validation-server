"""Smoke test: start the server over stdio and speak real MCP to it.

This is a client, not a test double. It spawns the server exactly as an MCP
client does — as a subprocess on stdin/stdout — performs the initialize
handshake, lists the registered tools, and calls one of them.

    python3 examples/stdio_handshake.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_validation_server"],
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            handshake = await session.initialize()
            print("server   :", handshake.server_info.name, handshake.server_info.version)
            print("protocol :", handshake.protocol_version)
            print()

            listing = await session.list_tools()
            print(f"tools    : {len(listing.tools)}")
            for tool in listing.tools:
                first_line = (tool.description or "").strip().splitlines()[0]
                print(f"  - {tool.name}: {first_line}")
            print()

            result = await session.call_tool(
                "validate_rows",
                {
                    "rows": [
                        {"invoice": "INV-1001", "amount": "100.50", "date": "2026-04-01"},
                        {"invoice": "", "amount": "20", "date": "2026-04-02"},
                        {"invoice": "INV-1003", "amount": "-5", "date": "2026-04-03"},
                    ],
                    "columns": [
                        {"name": "invoice", "type": "string", "required": True, "pattern": r"INV-\d+"},
                        {"name": "amount", "type": "number", "min": 0},
                        {"name": "date", "type": "date", "required": True},
                    ],
                    "row_key": "invoice",
                },
            )

            print("call_tool: validate_rows")
            print("is_error :", result.is_error)
            structured = result.structured_content or {}
            print("ok       :", structured.get("ok"))
            print("verdict  :", structured.get("verdict"))
            print("summary  :", json.dumps(structured.get("summary")))
            print("quarantine:")
            for item in structured.get("quarantine", []):
                locator = item["locator"]
                print(f"  row {locator.get('row_number')}: {item['reasons']}")
            print()
            print("guidance :", structured.get("guidance"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
