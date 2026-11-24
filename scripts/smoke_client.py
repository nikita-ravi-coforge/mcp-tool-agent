"""Connectivity proof: start the server over stdio, list its tools, and call one for a known and an unknown employee.

    uv run python scripts/smoke_client.py
"""

import asyncio
import sys

from mcp import Client, StdioServerParameters

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "equipment.server"])


async def main() -> None:
    async with Client(SERVER) as client:
        print(f"connected to: {client.server_info.name}")
        tools = (await client.list_tools()).tools
        print(f"tools registered: {[t.name for t in tools]}")
        for employee_id in ("BC-2102", "BC-9999"):
            result = await client.call_tool("get_employee_info", {"employee_id": employee_id})
            print(f"\nget_employee_info({employee_id!r}) -> isError={result.is_error}")
            print(result.content[0].text[:600])


asyncio.run(main())
