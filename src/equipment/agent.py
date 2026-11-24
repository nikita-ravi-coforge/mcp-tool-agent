"""ReAct agent over the equipment MCP tools: draft, verify in code, reflect with a second model call, revise.

    python -m equipment.agent BC-2102 "I'd like a second monitor"
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass

import anthropic
from mcp import Client, StdioServerParameters
from opentelemetry import trace

from equipment import tracing

tracer = trace.get_tracer("equipment.agent")

MAX_TURNS = 8  # model turns in the main conversation, revisions included
MAX_REVISIONS = 2
FLAG = "flag_for_human_review"
SAFE_RESPONSE = (
    "I wasn't able to complete this request automatically, so it needs to be reviewed by a person on the IT team. "
    "I can't tell you the outcome myself."
)

SYSTEM = f"""\
You handle IT equipment requests for employees of BEEHIVE Cloud. Each conversation gives you one employee ID and \
that employee's request in their own words. Your job is to work out what they are asking for, check it against \
company policy using the tools, and hand in a decision (approve, deny or escalate) with a short reply that will be \
sent to the employee as written.

The tools are your only source of facts. get_employee_info and get_policy_limits tell you about the person and \
their role; check_request_eligibility applies the policy rules and returns the decision, which you should adopt \
rather than re-derive, because the rule engine is the authority and the employee record often contradicts what a \
request claims. Before each tool call, write one short line saying what you are about to check and why, including \
when you make several calls in one turn; these lines are shown to the people who audit the decision.

Escalate whenever the outcome is not clear-cut: the eligibility check says escalate, the request does not pin down \
a single item and request type or asks for several things at once, or a tool fails or returns something you cannot \
interpret. A wrong approval or denial reaches the employee as if it were company policy, while an escalation only \
costs a reviewer a few minutes, so guessing is the more harmful mistake. To escalate, first call {FLAG} once with \
a reason a reviewer can act on, and only then hand in the escalate draft; without the flag no human ever sees the \
request. Do not flag requests you approve or deny.

When you are done, call submit_draft. Everything in the reply must come from a tool result or the request itself: \
dates, dollar amounts, delivery times and next steps that no tool returned are promises the company has not made. \
Your draft is checked before it is sent; if it comes back with problems, fix them and hand in a new draft."""

SUBMIT = {
    "name": "submit_draft",
    "description": "Hand in the final decision and the reply to the employee. Call this once you have the facts you "
    f"need (and, for an escalation, after {FLAG} has succeeded). This is the only way to finish the request.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "deny", "escalate"]},
            "response": {"type": "string", "description": "The reply sent to the employee."},
        },
        "required": ["decision", "response"],
        "additionalProperties": False,
    },
}

REFLECT_SYSTEM = """\
You review a drafted reply to an employee's IT equipment request before it is sent. You are given the request, \
every tool result the drafting agent saw, and the draft. Judge one thing: does the reply commit the company to \
anything, or state anything as fact, that the tool results do not support, or does it misstate them? Tone and \
style are not your concern. Answer "pass" if every claim is supported, otherwise "fail" with feedback naming each \
unsupported or misstated claim so the agent can fix it."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["pass", "fail"]}, "feedback": {"type": "string"}},
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}


@dataclass
class Result:
    decision: str
    response: str
    steps: list[dict]  # {thought, action, args, observation, is_error}
    rounds: list[dict]  # {draft, problems, reflection: pass|fail|skipped, feedback}
    fallback: str = ""  # why the code escalated on its own, if it did


async def run_agent(employee_id: str, request_text: str, model, tools: list[dict], call_tool) -> Result:
    """Handle one request. `model(system, messages, tools, schema=None)` returns an Anthropic Message;
    `call_tool(name, args)` returns `(text, is_error)`; `tools` are tool definitions in Anthropic's shape."""
    offered = [*tools, SUBMIT]
    messages = [{"role": "user", "content": f"Employee ID: {employee_id}\nRequest: {request_text}"}]
    steps, rounds = [], []
    failure = f"no draft was handed in within {MAX_TURNS} model turns"

    for _ in range(MAX_TURNS):
        reply = await model(SYSTEM, messages, offered)
        if reply.stop_reason in ("refusal", "max_tokens"):  # content is empty or cut off: do not act on it
            failure = f"the model stopped with {reply.stop_reason}"
            break
        messages.append({"role": "assistant", "content": reply.content})

        results, drafts, notes, thought = [], [], [], ""
        for block in reply.content:
            if block.type in ("thinking", "text"):
                notes.append(block.thinking if block.type == "thinking" else block.text)
            elif block.type == "tool_use" and block.name == SUBMIT["name"]:
                drafts.append(block)
            elif block.type == "tool_use":
                text, is_error = await call_tool(block.name, block.input)
                # A turn with several calls often states its reasoning once; that thought covers all of them.
                thought, notes = " ".join(filter(None, notes)) or thought, []
                steps.append(_step(thought, block.name, block.input, text, is_error))
                results.append(_tool_result(block.id, text, is_error))

        if drafts:
            draft = drafts[-1].input
            problems = verify(draft, steps, request_text)
            verdict, feedback = ("skipped", "") if problems else await reflect(model, request_text, steps, draft)
            rounds.append({"draft": draft, "problems": problems, "reflection": verdict, "feedback": feedback})
            if verdict == "pass":
                return Result(draft["decision"], draft["response"], steps, rounds)
            issues = problems or [feedback]
            if len(rounds) > MAX_REVISIONS:
                failure = "; ".join(issues)
                break
            rejection = "This draft was not accepted and nothing was sent to the employee:\n" + "\n".join(
                f"- {issue}" for issue in issues
            )
            rejection += "\nFix this (you may call tools again), then hand in a new draft with submit_draft."
            results += [_tool_result(block.id, rejection, True) for block in drafts]
        messages.append({"role": "user", "content": results or "Please finish by calling submit_draft."})

    reason = f"could not verify response: {failure}"
    if not any(s["action"] == FLAG and not s["is_error"] for s in steps):
        args = {"employee_id": employee_id, "request": request_text, "reason": reason}
        steps.append(_step("(agent code, not the model) " + reason, FLAG, args, *await call_tool(FLAG, args)))
    return Result("escalate", SAFE_RESPONSE, steps, rounds, fallback=reason)


async def reflect(model, request_text: str, steps: list[dict], draft: dict) -> tuple[str, str]:
    """Second, independent model call that judges the draft against the observations. Fails closed."""
    evidence = {
        "request": request_text,
        "tool_results": [{"tool": s["action"], "arguments": s["args"], "result": s["observation"]} for s in steps],
        "draft": draft,
    }
    reply = await model(REFLECT_SYSTEM, [{"role": "user", "content": json.dumps(evidence, indent=1)}], [], VERDICT_SCHEMA)
    try:
        if reply.stop_reason != "end_turn":
            raise ValueError(f"stop_reason {reply.stop_reason}")
        verdict = json.loads(next(b.text for b in reply.content if b.type == "text"))
        if verdict["verdict"] in ("pass", "fail"):
            return verdict["verdict"], verdict["feedback"]
        raise ValueError(f"unknown verdict {verdict['verdict']!r}")
    except (ValueError, KeyError, TypeError, StopIteration) as error:
        return "fail", f"The reviewer's verdict could not be read ({error!r}), so the draft is treated as unverified."


def _step(thought: str, action: str, args: dict, observation: str, is_error: bool) -> dict:
    return {"thought": thought, "action": action, "args": args, "observation": observation, "is_error": is_error}


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
# A bare number, but not a piece of a date (2026-11-23) or of an ID (BC-2102).
NUMBER = re.compile(r"(?<![\w-])\$?(\d[\d,]*(?:\.\d+)?)(?![\w-])")


def _amount(text: str) -> float:
    return float(text.replace(",", ""))


def verify(draft: dict, steps: list[dict], request_text: str) -> list[str]:
    """Deterministic checks on a draft. Returns the problems found; an empty list means it passes."""
    problems = []
    decision, response = draft.get("decision"), draft.get("response") or ""
    ok = [s for s in steps if not s["is_error"]]
    if not response.strip():
        problems.append("The response is empty.")

    checks = [s for s in ok if s["action"] == "check_request_eligibility"]
    if decision != "escalate":
        if not checks:
            problems.append("check_request_eligibility never succeeded, so the only allowed decision is escalate.")
        elif (policy := _decision_of(checks[-1])) != decision:
            problems.append(f"Draft decision {decision!r} differs from the eligibility decision {policy!r}.")

    sources = "\n".join([request_text, *(s["observation"] for s in steps)])
    known_amounts = {_amount(n) for n in NUMBER.findall(sources)}
    for date in DATE.findall(response):
        if date not in sources:
            problems.append(f"The date {date} is not in any tool result or in the request.")
    for match in DOLLARS.finditer(response):
        if _amount(match[1]) not in known_amounts:
            problems.append(f"The amount {match[0]} is not in any tool result or in the request.")

    flags = sum(s["action"] == FLAG for s in ok)
    if decision == "escalate" and flags != 1:
        problems.append(f"An escalate draft needs exactly one successful {FLAG} call; there were {flags}.")
    if decision != "escalate" and flags:
        problems.append(f"{FLAG} was called, so a human is already reviewing this; the decision must be escalate.")
    return problems


def _decision_of(step: dict):
    try:
        return json.loads(step["observation"])["decision"]
    except (ValueError, KeyError, TypeError):
        return None


def format_trace(result: Result) -> str:
    """The run as text: numbered Thought/Action/Observation steps, each verification round, then the outcome."""
    lines = []
    for number, s in enumerate(result.steps, 1):
        lines.append(f"{number}. Thought: {s['thought'] or '(none returned)'}")
        lines.append(f"   Action: {s['action']}({json.dumps(s['args'])})")
        lines.append(f"   Observation{' (error)' if s['is_error'] else ''}: {s['observation']}")
    for number, r in enumerate(result.rounds, 1):
        lines.append(f"Round {number} draft: {r['draft'].get('decision')} - {r['draft'].get('response')}")
        lines += [f"   Verifier: {problem}" for problem in r["problems"] or ["pass"]]
        lines.append(f"   Reflection: {r['reflection']}" + (f" - {r['feedback']}" if r["feedback"] else ""))
    if result.fallback:
        lines.append(f"Escalated by code: {result.fallback}")
    return "\n".join([*lines, f"Final decision: {result.decision}", f"Response: {result.response}"])


# --- adapters: the real model and the real MCP server ---------------------------------------------------------------


def request_params(system: str, messages: list, tools: list[dict], schema: dict | None = None) -> dict:
    """Keyword arguments for `client.beta.messages.create`. `schema` asks for JSON matching it (the reflection call)."""
    output_config = {"effort": "medium"}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    params = {
        "model": os.environ.get("EQUIPMENT_MODEL", "claude-opus-5-5"),
        "max_tokens": 16000,
        "system": system,
        "messages": messages,
        # Thinking cannot be turned off on this model; "summarized" makes the thinking blocks readable (the Thoughts).
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": output_config,
        # If a safety classifier declines the request, the API retries it on Anthropic's recommended fallback model.
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }
    if tools:
        params["tools"] = tools  # tool_choice is left at its default, auto: forcing a tool is rejected by this model
    return params


async def claude(system, messages, tools, schema=None):  # pragma: no cover - needs the network and an API key
    async with anthropic.AsyncAnthropic() as client:  # the SDK emits its own span for each call
        return await client.beta.messages.create(**request_params(system, messages, tools, schema))


async def mcp_tools(client) -> list[dict]:
    """The MCP server's tools as Anthropic tool definitions."""
    listed = (await client.list_tools()).tools
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed]


def mcp_caller(client):
    async def call_tool(name: str, args: dict) -> tuple[str, bool]:
        result = await client.call_tool(name, args)
        return "\n".join(c.text for c in result.content if c.type == "text"), bool(result.is_error)

    return call_tool


async def handle(employee_id: str, request_text: str, model=claude) -> Result:
    """Start the MCP server over stdio and run one request against it."""
    # The SDK starts the server with a minimal environment, so pass our settings and the tracing endpoint on.
    env = {k: v for k, v in os.environ.items() if k.startswith(("EQUIPMENT_", "OTEL_"))}
    server = StdioServerParameters(command=sys.executable, args=["-m", "equipment.server"], env=env)
    with tracer.start_as_current_span("agent.request", attributes={"employee_id": employee_id}) as span:
        async with Client(server) as client:
            result = await run_agent(employee_id, request_text, model, await mcp_tools(client), mcp_caller(client))
        span.set_attribute("decision", result.decision)
        return result


if __name__ == "__main__":
    tracing.setup_tracing("equipment-agent")
    print(format_trace(asyncio.run(handle(sys.argv[1], sys.argv[2]))))
