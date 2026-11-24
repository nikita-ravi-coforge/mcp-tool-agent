"""Agent tests: a scripted fake model and canned tool results. No network, no subprocess."""

import asyncio
import itertools
import json
from types import SimpleNamespace as NS

import pytest

from equipment import agent


def step(action, observation, is_error=False, args=None):
    return {"thought": "", "action": action, "args": args or {}, "observation": json.dumps(observation), "is_error": is_error}


EMPLOYEE = step("get_employee_info", {"found": True, "employee_id": "BC-2102", "hire_date": "2020-03-02"})
APPROVED = step("check_request_eligibility", {"decision": "approve", "rule": 14, "remaining_budget_usd": 1500.0})
DENIED = step("check_request_eligibility", {"decision": "deny", "rule": 11, "eligible_date": "2027-03-01"})
FLAGGED = step("flag_for_human_review", {"ticket_id": "ESC-0001", "status": "queued"})
REQUEST = "I need a new monitor, the $300 one."


def draft(decision, response="Done."):
    return {"decision": decision, "response": response}


# --- verifier -------------------------------------------------------------------------------------------------------


def test_verifier_passes_a_draft_that_matches_the_eligibility_decision():
    assert agent.verify(draft("approve"), [EMPLOYEE, APPROVED], REQUEST) == []


def test_verifier_rejects_a_decision_that_contradicts_eligibility():
    problems = agent.verify(draft("approve"), [EMPLOYEE, DENIED], REQUEST)
    assert len(problems) == 1 and "deny" in problems[0]


def test_verifier_uses_the_most_recent_successful_eligibility_check():
    failed_recheck = step("check_request_eligibility", "invalid request_type", is_error=True)
    assert agent.verify(draft("deny"), [APPROVED, DENIED, failed_recheck], REQUEST) == []
    assert agent.verify(draft("approve"), [APPROVED, DENIED, failed_recheck], REQUEST) != []


def test_verifier_always_allows_escalate_even_when_policy_would_approve():
    assert agent.verify(draft("escalate"), [APPROVED, FLAGGED], REQUEST) == []


def test_verifier_allows_only_escalate_when_eligibility_was_never_checked():
    errored = step("check_request_eligibility", "boom", is_error=True)
    assert agent.verify(draft("deny"), [EMPLOYEE, errored], REQUEST) != []
    assert agent.verify(draft("escalate"), [EMPLOYEE, errored, FLAGGED], REQUEST) == []


def test_verifier_treats_an_unreadable_eligibility_result_as_no_decision():
    garbled = step("check_request_eligibility", {}) | {"observation": "<html>gateway timeout</html>"}
    assert agent.verify(draft("approve"), [garbled], REQUEST) != []
    assert agent.verify(draft("escalate"), [garbled, FLAGGED], REQUEST) == []


def test_verifier_accepts_dates_and_amounts_found_in_observations_or_the_request():
    response = "You have $1,500 left; the $300.00 monitor fits. You were hired on 2020-03-02."
    assert agent.verify(draft("approve", response), [EMPLOYEE, APPROVED], REQUEST) == []


def test_verifier_normalises_dollar_formats():
    for written in ("$1,500", "$1500.00", "$1500", "$1,500.00."):
        assert agent.verify(draft("approve", f"Budget left: {written}"), [APPROVED], REQUEST) == [], written


def test_verifier_rejects_an_unsupported_date():
    problems = agent.verify(draft("approve", "It will arrive on 2026-12-01."), [EMPLOYEE, APPROVED], REQUEST)
    assert len(problems) == 1 and "2026-12-01" in problems[0]


def test_verifier_rejects_an_unsupported_amount():
    problems = agent.verify(draft("approve", "You have $2,000 left."), [EMPLOYEE, APPROVED], REQUEST)
    assert len(problems) == 1 and "$2,000" in problems[0]


def test_verifier_does_not_mistake_parts_of_a_date_or_an_id_for_an_amount():
    # 2020, 3 and 2 appear inside the hire date, and 2102 inside the employee ID, but none is a dollar figure.
    assert agent.verify(draft("approve", "That costs $2020."), [EMPLOYEE, APPROVED], REQUEST) != []
    assert agent.verify(draft("approve", "That costs $2102."), [EMPLOYEE, APPROVED], REQUEST) != []


def test_verifier_requires_exactly_one_successful_flag_on_escalate():
    failed_flag = step("flag_for_human_review", "reason must not be empty", is_error=True)
    assert agent.verify(draft("escalate"), [APPROVED], REQUEST) != []
    assert agent.verify(draft("escalate"), [APPROVED, failed_flag], REQUEST) != []
    assert agent.verify(draft("escalate"), [APPROVED, failed_flag, FLAGGED], REQUEST) == []
    assert agent.verify(draft("escalate"), [APPROVED, FLAGGED, FLAGGED], REQUEST) != []


def test_verifier_rejects_a_flag_on_approve_or_deny():
    assert agent.verify(draft("approve"), [APPROVED, FLAGGED], REQUEST) != []
    assert agent.verify(draft("deny"), [DENIED, FLAGGED], REQUEST) != []


def test_verifier_reports_a_malformed_draft_instead_of_crashing():
    assert agent.verify({}, [APPROVED], REQUEST) != []
    assert agent.verify({"decision": "approve"}, [APPROVED], REQUEST) != []
    assert agent.verify(draft("approve", "  "), [APPROVED], REQUEST) != []


# --- scripted model and canned tools --------------------------------------------------------------------------------

_ids = itertools.count(1)


def thinking(text):
    return NS(type="thinking", thinking=text, signature="sig")


def note(text):
    return NS(type="text", text=text)


def call(name, **args):
    return NS(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=args)


def submit(decision, response="Done."):
    return call("submit_draft", decision=decision, response=response)


def turn(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop)


def verdict(value, feedback=""):
    return turn(note(json.dumps({"verdict": value, "feedback": feedback})), stop="end_turn")


class FakeModel:
    """Plays back scripted assistant turns; reflection calls (those with a schema) come from a separate script."""

    def __init__(self, turns, verdicts=()):
        self.turns, self.verdicts = iter(turns), iter(verdicts)
        self.requests, self.reflections = [], []

    async def __call__(self, system, messages, tools, schema=None):
        if schema:
            self.reflections.append(messages)
            return next(self.verdicts)
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return next(self.turns)


class FakeTools:
    """Canned results per tool name. A list is consumed one result per call; a str is an error result."""

    def __init__(self, **results):
        self.results, self.calls = results, []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        result = self.results[name]
        if isinstance(result, list):
            result = result.pop(0)
        return (result, True) if isinstance(result, str) else (json.dumps(result), False)

    def names(self):
        return [name for name, _ in self.calls]


TOOL_DEFS = [{"name": "get_employee_info", "description": "Look up an employee.", "input_schema": {"type": "object"}}]
EMPLOYEE_INFO = {"found": True, "employee_id": "BC-2102", "role": "site_reliability_engineer"}
APPROVE = {"decision": "approve", "rule": 14, "reason": "within budget", "remaining_budget_usd": 1500.0}
DENY = {"decision": "deny", "rule": 11, "reason": "refresh not due", "eligible_date": "2027-03-01"}
TICKET = {"ticket_id": "ESC-0001", "status": "queued"}
LOOKUP = call("get_employee_info", employee_id="BC-2102")
CHECK = call("check_request_eligibility", employee_id="BC-2102", item="monitor", request_type="standard")


def run(model, tools, request=REQUEST):
    return asyncio.run(agent.run_agent("BC-2102", request, model, TOOL_DEFS, tools))


def tool_results(request):
    """The tool_result blocks in the last user message of a recorded model request."""
    return request["messages"][-1]["content"]


# --- ReAct loop -----------------------------------------------------------------------------------------------------


def test_clean_approve_path_records_each_step_and_never_flags():
    approve = submit("approve", "Approved: the monitor fits your remaining budget of $1,500.")
    first = turn(thinking("I need the employee record first."), LOOKUP)
    model = FakeModel(
        [first, turn(thinking(""), note("Now check the policy."), CHECK), turn(approve)], [verdict("pass", "Grounded.")]
    )
    tools = FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=APPROVE)

    result = run(model, tools)

    assert (result.decision, result.response) == ("approve", approve.input["response"])
    assert result.steps == [
        {
            "thought": "I need the employee record first.",
            "action": "get_employee_info",
            "args": {"employee_id": "BC-2102"},
            "observation": json.dumps(EMPLOYEE_INFO),
            "is_error": False,
        },
        {
            "thought": "Now check the policy.",
            "action": "check_request_eligibility",
            "args": CHECK.input,
            "observation": json.dumps(APPROVE),
            "is_error": False,
        },
    ]
    assert tools.names() == ["get_employee_info", "check_request_eligibility"]
    assert result.rounds == [{"draft": approve.input, "problems": [], "reflection": "pass", "feedback": "Grounded."}]


def test_loop_offers_the_mcp_tools_plus_a_hand_in_tool_and_replays_assistant_content_unchanged():
    # A block type the loop has no use for (here, the marker the API adds when a fallback model took over).
    first = turn(NS(type="fallback"), thinking("Look up the employee."), LOOKUP)
    model = FakeModel([first, turn(CHECK), turn(submit("approve"))], [verdict("pass")])
    run(model, FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=APPROVE))

    assert [t["name"] for t in model.requests[0]["tools"]] == ["get_employee_info", "submit_draft"]
    opening = model.requests[0]["messages"]
    assert len(opening) == 1 and "BC-2102" in opening[0]["content"] and REQUEST in opening[0]["content"]
    # The thinking block must go back exactly as it came, followed by the result for the call it made.
    assert model.requests[1]["messages"][1] == {"role": "assistant", "content": first.content}
    assert tool_results(model.requests[1]) == [
        {"type": "tool_result", "tool_use_id": LOOKUP.id, "content": json.dumps(EMPLOYEE_INFO), "is_error": False}
    ]
    assert len({r["system"] for r in model.requests}) == 1


def test_escalate_path_where_the_model_flags_and_parallel_results_share_one_message():
    flag = call("flag_for_human_review", employee_id="BC-2102", request=REQUEST, reason="lost laptop")
    model = FakeModel(
        [
            turn(thinking("Record first."), LOOKUP, thinking("Then policy."), CHECK),
            turn(flag),
            turn(submit("escalate", "A reviewer will pick this up.")),
        ],
        [verdict("pass")],
    )
    escalate = {"decision": "escalate", "rule": 4, "reason": "security incident"}
    tools = FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=escalate, flag_for_human_review=TICKET)

    result = run(model, tools)

    assert result.decision == "escalate"
    assert [s["thought"] for s in result.steps] == ["Record first.", "Then policy.", ""]
    assert [r["tool_use_id"] for r in tool_results(model.requests[1])] == [LOOKUP.id, CHECK.id]
    assert tools.names().count("flag_for_human_review") == 1


def test_tool_error_is_fed_back_and_the_model_recovers():
    bad = call("check_request_eligibility", employee_id="BC-2102", item="monitor", request_type="new")
    model = FakeModel([turn(bad), turn(CHECK), turn(submit("approve"))], [verdict("pass")])
    tools = FakeTools(check_request_eligibility=["invalid request_type 'new'", APPROVE])

    result = run(model, tools)

    assert result.decision == "approve"
    assert [(s["observation"], s["is_error"]) for s in result.steps] == [
        ("invalid request_type 'new'", True),
        (json.dumps(APPROVE), False),
    ]
    assert tool_results(model.requests[1])[0]["is_error"] is True


def test_model_that_stops_without_handing_in_is_asked_to_use_the_hand_in_tool():
    model = FakeModel([turn(CHECK), turn(note("Approved!"), stop="end_turn"), turn(submit("approve"))], [verdict("pass")])
    result = run(model, FakeTools(check_request_eligibility=APPROVE))

    assert result.decision == "approve"
    assert "submit_draft" in model.requests[2]["messages"][-1]["content"]


# --- reflection and revision ----------------------------------------------------------------------------------------


def test_reflection_sees_the_request_the_observations_and_the_draft():
    model = FakeModel([turn(CHECK), turn(submit("approve", "Approved, enjoy the monitor."))], [verdict("pass")])
    run(model, FakeTools(check_request_eligibility=APPROVE))

    (messages,) = model.reflections
    shown = messages[0]["content"]
    assert REQUEST in shown and "within budget" in shown and "Approved, enjoy the monitor." in shown


def test_reflection_catches_an_unsupported_promise_and_the_revised_draft_passes():
    promise = submit("approve", "Approved. It will be on your desk within a week.")
    honest = submit("approve", "Approved: the monitor is within your budget.")
    model = FakeModel(
        [turn(CHECK), turn(promise), turn(honest)],
        [verdict("fail", "No observation mentions delivery time."), verdict("pass")],
    )
    result = run(model, FakeTools(check_request_eligibility=APPROVE))

    assert result.response == honest.input["response"]
    assert [(r["reflection"], r["feedback"]) for r in result.rounds] == [
        ("fail", "No observation mentions delivery time."),
        ("pass", ""),
    ]
    (rejection,) = tool_results(model.requests[2])
    assert rejection["tool_use_id"] == promise.id and rejection["is_error"] is True
    assert "No observation mentions delivery time." in rejection["content"]


def test_verifier_catches_an_invented_date_before_reflection_is_spent_on_it():
    invented = submit("deny", "Not yet: you become eligible on 2027-06-01.")
    model = FakeModel(
        [turn(CHECK), turn(invented), turn(submit("deny", "Not yet: you become eligible on 2027-03-01."))],
        [verdict("pass")],
    )
    result = run(model, FakeTools(check_request_eligibility=DENY))

    assert result.decision == "deny" and "2027-03-01" in result.response
    assert "2027-06-01" in result.rounds[0]["problems"][0] and result.rounds[0]["reflection"] == "skipped"
    assert len(model.reflections) == 1
    assert "2027-06-01" in tool_results(model.requests[2])[0]["content"]


def test_model_can_flag_during_a_revision_to_fix_an_escalate_draft():
    flag = call("flag_for_human_review", employee_id="BC-2102", request=REQUEST, reason="unclear item")
    model = FakeModel(
        [turn(submit("escalate", "Sent for review.")), turn(flag), turn(submit("escalate", "Sent for review."))],
        [verdict("pass")],
    )
    tools = FakeTools(flag_for_human_review=TICKET)

    result = run(model, tools)

    assert result.decision == "escalate" and result.response == "Sent for review."
    assert len(result.rounds) == 2 and tools.names() == ["flag_for_human_review"]


@pytest.mark.parametrize(
    "unreadable",
    [
        turn(note("Looks fine to me."), stop="end_turn"),
        turn(note(json.dumps({"verdict": "probably", "feedback": "hm"})), stop="end_turn"),
        turn(note(json.dumps(["pass"])), stop="end_turn"),
        turn(stop="refusal"),
        turn(thinking("..."), stop="end_turn"),
    ],
)
def test_reflection_fails_closed_when_the_verdict_cannot_be_read(unreadable):
    model = FakeModel([turn(CHECK), turn(submit("approve")), turn(submit("approve"))], [unreadable, verdict("pass")])
    result = run(model, FakeTools(check_request_eligibility=APPROVE))

    assert [r["reflection"] for r in result.rounds] == ["fail", "pass"]


# --- fallback escalation --------------------------------------------------------------------------------------------


def assert_safe_escalation(result):
    assert result.decision == "escalate" and result.response == agent.SAFE_RESPONSE
    assert not agent.DATE.search(result.response) and "$" not in result.response


def test_revision_budget_exhausted_escalates_from_code_with_a_safe_response():
    promise = "Approved. It will be on your desk within a week."
    model = FakeModel(
        [turn(CHECK), *(turn(submit("approve", promise)) for _ in range(4))],
        [verdict("fail", "Delivery time is unsupported.") for _ in range(4)],
    )
    tools = FakeTools(check_request_eligibility=APPROVE, flag_for_human_review=TICKET)

    result = run(model, tools)

    assert_safe_escalation(result)
    assert len(result.rounds) == 3 and len(model.requests) == 4  # one draft plus two revisions, then stop asking
    name, args = tools.calls[-1]
    assert name == "flag_for_human_review" and tools.names().count(name) == 1
    assert args["employee_id"] == "BC-2102" and args["request"] == REQUEST
    assert args["reason"].startswith("could not verify response") and "Delivery time is unsupported." in args["reason"]
    assert result.steps[-1]["action"] == "flag_for_human_review" and result.steps[-1]["observation"] == json.dumps(TICKET)


def test_code_does_not_flag_again_when_the_model_already_flagged():
    flag = call("flag_for_human_review", employee_id="BC-2102", request=REQUEST, reason="not sure")
    model = FakeModel([turn(CHECK, flag), *(turn(submit("approve")) for _ in range(3))])
    tools = FakeTools(check_request_eligibility=APPROVE, flag_for_human_review=TICKET)

    result = run(model, tools)

    assert_safe_escalation(result)
    assert tools.names().count("flag_for_human_review") == 1


def test_turn_cap_stops_a_model_that_never_hands_in():
    model = FakeModel(turn(call("get_employee_info", employee_id="BC-2102")) for _ in range(50))
    tools = FakeTools(get_employee_info=EMPLOYEE_INFO, flag_for_human_review=TICKET)

    result = run(model, tools)

    assert_safe_escalation(result)
    assert len(model.requests) == agent.MAX_TURNS == 8
    assert tools.names() == ["get_employee_info"] * 8 + ["flag_for_human_review"]
    assert result.rounds == []


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_or_truncation_escalates_without_running_that_turns_tools(stop):
    truncated = [CHECK] if stop == "max_tokens" else []
    model = FakeModel([turn(LOOKUP), turn(*truncated, stop=stop)])
    tools = FakeTools(get_employee_info=EMPLOYEE_INFO, flag_for_human_review=TICKET)

    result = run(model, tools)

    assert_safe_escalation(result)
    assert tools.names() == ["get_employee_info", "flag_for_human_review"]
    assert stop in tools.calls[-1][1]["reason"]


def test_escalation_still_returns_when_the_code_side_flag_itself_fails():
    model = FakeModel([turn(stop="refusal")])
    result = run(model, FakeTools(flag_for_human_review="escalations file is read-only"))

    assert_safe_escalation(result)
    assert result.steps[-1]["is_error"] is True


# --- trace ----------------------------------------------------------------------------------------------------------


def test_format_trace_shows_steps_then_verification_rounds_then_the_outcome():
    promise = submit("approve", "Approved. It arrives within a week.")
    bad = call("check_request_eligibility", employee_id="BC-2102", item="monitor", request_type="new")
    model = FakeModel(
        [turn(thinking("Check the policy."), bad), turn(CHECK), turn(promise), turn(submit("approve", "Approved."))],
        [verdict("fail", "Delivery time is unsupported."), verdict("pass", "All supported.")],
    )
    tools = FakeTools(check_request_eligibility=["invalid request_type 'new'", APPROVE])

    lines = agent.format_trace(run(model, tools)).splitlines()

    assert lines[:3] == [
        "1. Thought: Check the policy.",
        f"   Action: check_request_eligibility({json.dumps(bad.input)})",
        "   Observation (error): invalid request_type 'new'",
    ]
    assert lines[3] == "2. Thought: (none returned)" and lines[5] == f"   Observation: {json.dumps(APPROVE)}"
    assert lines[6:] == [
        "Round 1 draft: approve - Approved. It arrives within a week.",
        "   Verifier: pass",
        "   Reflection: fail - Delivery time is unsupported.",
        "Round 2 draft: approve - Approved.",
        "   Verifier: pass",
        "   Reflection: pass - All supported.",
        "Final decision: approve",
        "Response: Approved.",
    ]


def test_format_trace_shows_verifier_problems_and_a_code_side_escalation():
    model = FakeModel([turn(submit("approve", "Approved, $50 credit added."))] * 3)
    lines = agent.format_trace(run(model, FakeTools(flag_for_human_review=TICKET))).splitlines()

    assert "   Verifier: check_request_eligibility never succeeded, so the only allowed decision is escalate." in lines
    assert "   Verifier: The amount $50 is not in any tool result or in the request." in lines
    assert lines.count("   Reflection: skipped") == 3
    assert lines[0].startswith("1. Thought: (agent code, not the model) could not verify response: ")
    assert lines[-3].startswith("Escalated by code: could not verify response: ")
    assert lines[-2:] == ["Final decision: escalate", f"Response: {agent.SAFE_RESPONSE}"]


# --- adapters -------------------------------------------------------------------------------------------------------


def test_request_params_respect_what_the_default_model_accepts(monkeypatch):
    monkeypatch.delenv("EQUIPMENT_MODEL", raising=False)
    params = agent.request_params("sys", [{"role": "user", "content": "hi"}], [agent.SUBMIT])

    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"]["type"] == "adaptive" and params["thinking"]["display"] != "omitted"  # readable Thoughts
    assert params.get("tool_choice", {"type": "auto"}) == {"type": "auto"}  # forced tool use is a 400
    assert params["tools"] == [agent.SUBMIT] and "format" not in params["output_config"]


def test_request_params_for_reflection_ask_for_schema_constrained_json_and_honour_the_model_override(monkeypatch):
    monkeypatch.setenv("EQUIPMENT_MODEL", "claude-haiku-5-5")
    params = agent.request_params("sys", [], [], agent.VERDICT_SCHEMA)

    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": agent.VERDICT_SCHEMA}
    assert "tools" not in params


def test_mcp_adapters_list_and_call_tools_on_the_real_server_in_process():
    from mcp import Client

    from equipment.server import mcp

    async def go():
        async with Client(mcp) as client:
            call_tool = agent.mcp_caller(client)
            good = await call_tool("get_employee_info", {"employee_id": "BC-2102"})
            return await agent.mcp_tools(client), good, await call_tool("get_employee_info", {})

    tools, (text, is_error), (error_text, error_flag) = asyncio.run(go())

    lookup = next(t for t in tools if t["name"] == "get_employee_info")
    assert set(lookup) == {"name", "description", "input_schema"} and "employee" in lookup["description"]
    assert lookup["input_schema"]["required"] == ["employee_id"]
    assert is_error is False and json.loads(text)["employee_id"] == "BC-2102"
    assert error_flag is True and "employee_id" in error_text


def test_one_thought_stated_for_several_calls_in_a_turn_is_shown_on_each_of_them():
    model = FakeModel(
        [turn(note("I'll look the employee up and check the policy."), LOOKUP, CHECK), turn(submit("approve"))],
        [verdict("pass")],
    )
    result = run(model, FakeTools(get_employee_info=EMPLOYEE_INFO, check_request_eligibility=APPROVE))

    assert [s["thought"] for s in result.steps] == ["I'll look the employee up and check the policy."] * 2
