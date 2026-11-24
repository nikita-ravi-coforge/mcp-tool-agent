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


def test_get_policy_limits_over_stdio():
    limits = call("get_policy_limits", {"role": "intern"})
    assert limits["peripheral_scope"] == "electronic_only"
    assert limits["peripheral_budget_first_year_usd"] == 925
    assert call("get_policy_limits", {"role": "astronaut"})["found"] is False


def test_check_request_eligibility_over_stdio_uses_the_frozen_date():
    result = call("check_request_eligibility", {"employee_id": "BC-2101", "item": "laptop"})
    assert (result["decision"], result["rule"], result["eligible_date"]) == ("deny", 11, "2027-02-05")
    assert result["request_type"] == "standard"
    lost = call("check_request_eligibility", {"employee_id": "BC-2109", "item": "laptop", "request_type": "lost"})
    assert (lost["decision"], lost["rule"]) == ("escalate", 4)


def test_invalid_request_type_over_stdio_is_a_tool_error_naming_the_valid_values():
    async def go():
        async with Client(SERVER) as client:
            return await client.call_tool(
                "check_request_eligibility", {"employee_id": "BC-2101", "item": "laptop", "request_type": "upgrade"}
            )

    result = asyncio.run(go())
    assert result.is_error is True
    assert "keep_after_leaving" in result.content[0].text


def test_tool_schemas_do_not_expose_as_of():
    async def go():
        async with Client(SERVER) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(go())
    assert {t.name for t in tools} == {
        "get_employee_info",
        "get_policy_limits",
        "check_request_eligibility",
        "flag_for_human_review",
    }
    assert all("as_of" not in t.input_schema["properties"] for t in tools)


def test_flag_for_human_review_over_stdio_writes_the_ticket_to_the_configured_file(tmp_path, monkeypatch):
    path = tmp_path / "escalations.jsonl"
    env = {**SERVER.env, "EQUIPMENT_ESCALATIONS_FILE": str(path)}
    monkeypatch.setattr(sys.modules[__name__], "SERVER", SERVER.model_copy(update={"env": env}))

    ticket = call(
        "flag_for_human_review",
        {"employee_id": "BC-2109", "request": "I left my laptop in an Uber", "reason": "Lost laptop"},
    )
    assert ticket["status"] == "queued"
    [line] = path.read_text().splitlines()
    assert json.loads(line)["ticket_id"] == ticket["ticket_id"]

    async def blank_reason():
        async with Client(SERVER) as client:
            return await client.call_tool(
                "flag_for_human_review", {"employee_id": "BC-2109", "request": "lost laptop", "reason": " "}
            )

    error = asyncio.run(blank_reason())
    assert error.is_error is True
    assert "reason must not be empty" in error.content[0].text
    assert len(path.read_text().splitlines()) == 1
