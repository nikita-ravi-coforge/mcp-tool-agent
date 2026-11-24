"""Integration: a real MCP client talking to the real server process over stdio. No model involved."""

import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters

# The SDK starts the server with a minimal environment, so forward coverage's variables explicitly.
COVERAGE_ENV = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")}
SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "equipment.server"], env={"EQUIPMENT_AS_OF": "2026-11-23", **COVERAGE_ENV}
)


def call(tool: str, arguments: dict) -> dict:
    async def go():
        async with Client(SERVER) as client:
            result = await client.call_tool(tool, arguments)
            assert result.is_error is False
            return json.loads(result.content[0].text)

    return asyncio.run(go())


def list_tool_names() -> list[str]:
    async def go():
        async with Client(SERVER) as client:
            return [t.name for t in (await client.list_tools()).tools]

    return asyncio.run(go())


def test_server_registers_get_employee_info():
    assert "get_employee_info" in list_tool_names()


def test_get_employee_info_over_stdio():
    info = call("get_employee_info", {"employee_id": "BC-2102"})
    assert info["role"] == "site_reliability_engineer"
    assert info["tenure_months"] == 80


def test_unknown_employee_over_stdio_is_a_result_not_a_protocol_error():
    assert call("get_employee_info", {"employee_id": "BC-9999"})["found"] is False
