"""The MCP SDK creates the spans; these tests check our setup makes them visible and joined into one trace."""

import asyncio
import os
import sys

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from equipment import tracing
from equipment.server import mcp

exporter = InMemorySpanExporter()
tracing.setup_tracing("test-service", processor=SimpleSpanProcessor(exporter))


def call_tool_inside_parent_span() -> None:
    async def go():
        with trace.get_tracer("test").start_as_current_span("agent.request"):
            async with Client(mcp) as client:
                await client.call_tool("get_employee_info", {"employee_id": "BC-2102"})

    exporter.clear()
    asyncio.run(go())


def test_tool_call_produces_a_server_span_named_after_the_tool():
    call_tool_inside_parent_span()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    tool_span = spans["tools/call get_employee_info"]
    assert tool_span.attributes["gen_ai.tool.name"] == "get_employee_info"
    assert tool_span.resource.attributes["service.name"] == "test-service"


def test_tool_span_belongs_to_the_callers_trace():
    call_tool_inside_parent_span()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["tools/call get_employee_info"].context.trace_id == spans["agent.request"].context.trace_id


def test_one_line_format_shows_name_duration_and_trace():
    call_tool_inside_parent_span()
    span = next(s for s in exporter.get_finished_spans() if s.name == "agent.request")
    line = tracing.one_line(span)
    assert line.startswith("[trace ") and "agent.request" in line and line.endswith("ms\n")


def test_exporter_choice_follows_environment(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.default_exporter() is None
    monkeypatch.setenv("EQUIPMENT_TRACE", "console")
    assert type(tracing.default_exporter()).__name__ == "ConsoleSpanExporter"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    assert type(tracing.default_exporter()).__name__ == "OTLPSpanExporter"


def test_trace_id_crosses_the_process_boundary_over_stdio(tmp_path):
    """Agent and server are separate processes; the server's span must still carry the caller's trace ID."""
    coverage_env = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")}
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "equipment.server"], env={"EQUIPMENT_TRACE": "console", **coverage_env}
    )
    server_stderr = tmp_path / "server.err"

    async def go() -> int:
        with trace.get_tracer("test").start_as_current_span("agent.request") as parent:
            with server_stderr.open("w") as errlog:
                async with Client(stdio_client(server, errlog=errlog)) as client:
                    await client.call_tool("get_employee_info", {"employee_id": "BC-2102"})
            return parent.get_span_context().trace_id

    trace_id = asyncio.run(go())
    assert f"[trace {trace_id:032x}] tools/call get_employee_info" in server_stderr.read_text()


def test_each_react_step_is_one_span_with_thought_action_and_observation():
    import json

    from test_agent import APPROVE, CHECK, EMPLOYEE_INFO, LOOKUP, REQUEST, TOOL_DEFS, FakeModel, FakeTools, note, submit, turn, verdict

    from equipment import agent

    model = FakeModel(
        [turn(note("Look the employee up."), LOOKUP), turn(note("Now the policy."), CHECK), turn(submit("approve"))],
        [verdict("pass")],
    )
    fake_tools = FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=APPROVE)
    exporter.clear()
    asyncio.run(agent.run_agent("BC-2102", REQUEST, model, TOOL_DEFS, fake_tools))

    spans = {s.name: s for s in exporter.get_finished_spans()}
    step = spans["step 2: check_request_eligibility"]
    assert json.loads(step.attributes["input.value"]) == {
        "thought": "Now the policy.",
        "action": "check_request_eligibility",
        "arguments": CHECK.input,
    }
    assert json.loads(step.attributes["output.value"])["decision"] == "approve"  # the observation
    assert "step 1: get_employee_info" in spans


def test_server_side_tool_span_records_its_arguments_and_result():
    call_tool_inside_parent_span()
    span = next(s for s in exporter.get_finished_spans() if s.name == "tools/call get_employee_info")
    assert span.attributes["input.value"] == '{"employee_id": "BC-2102"}'
    assert '"role": "site_reliability_engineer"' in span.attributes["output.value"]


def test_draft_check_span_records_the_draft_the_verifier_result_and_the_reflection_verdict():
    import json

    from test_agent import APPROVE, CHECK, EMPLOYEE_INFO, LOOKUP, REQUEST, TOOL_DEFS, FakeModel, FakeTools, submit, turn, verdict

    from equipment import agent

    model = FakeModel(
        [turn(LOOKUP, CHECK), turn(submit("approve", "Ships 2031-01-01.")), turn(submit("approve", "Approved."))],
        [verdict("pass")],
    )
    fake_tools = FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=APPROVE)
    exporter.clear()
    asyncio.run(agent.run_agent("BC-2102", REQUEST, model, TOOL_DEFS, fake_tools))

    first, second = [s for s in exporter.get_finished_spans() if s.name == "check draft"]
    assert json.loads(first.attributes["input.value"])["response"] == "Ships 2031-01-01."
    rejected = json.loads(first.attributes["output.value"])
    assert "2031-01-01" in rejected["verifier"][0] and rejected["reflection"] == "skipped"
    assert json.loads(second.attributes["output.value"]) == {"verifier": "pass", "reflection": "pass", "feedback": ""}
