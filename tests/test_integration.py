"""End to end minus the network: a scripted model drives the real agent loop against the real MCP server over stdio."""

import asyncio
import json

from test_agent import FakeModel, call, note, submit, turn, verdict

from equipment import agent, tools


def handle(monkeypatch, tmp_path, employee_id, text, model):
    monkeypatch.setenv("EQUIPMENT_AS_OF", "2026-11-23")
    monkeypatch.setenv("EQUIPMENT_ESCALATIONS_FILE", str(tmp_path / "escalations.jsonl"))
    return asyncio.run(agent.handle(employee_id, text, model))


def escalations(tmp_path):
    file = tmp_path / "escalations.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def test_denied_refresh_uses_the_servers_real_answer_and_raises_no_ticket(monkeypatch, tmp_path):
    model = FakeModel(
        [
            turn(note("Look the employee up."), call("get_employee_info", employee_id="BC-2101")),
            turn(note("Check the refresh."), call("check_request_eligibility", employee_id="BC-2101", item="laptop")),
            turn(submit("deny", "Your laptop is not due for refresh yet. You are eligible from 2027-02-05.")),
        ],
        [verdict("pass")],
    )
    result = handle(monkeypatch, tmp_path, "BC-2101", "Laptop has been feeling slow lately, can I get a new one?", model)

    assert result.decision == "deny" and "2027-02-05" in result.response
    assert [s["action"] for s in result.steps] == ["get_employee_info", "check_request_eligibility"]
    assert json.loads(result.steps[1]["observation"])["rule"] == 11
    assert escalations(tmp_path) == []
    offered = [t["name"] for t in model.requests[0]["tools"]]
    assert offered == [
        "get_employee_info",
        "get_policy_limits",
        "check_request_eligibility",
        "flag_for_human_review",
        "submit_draft",
    ]


def test_lost_laptop_is_escalated_with_one_ticket_written_by_the_server(monkeypatch, tmp_path):
    text = "I think I left my laptop in an Uber last night. Can I get a new one?"
    check = call("check_request_eligibility", employee_id="BC-2109", item="laptop", request_type="lost")
    flag = call("flag_for_human_review", employee_id="BC-2109", request=text, reason="Lost laptop: security incident.")
    model = FakeModel(
        [turn(check), turn(flag), turn(submit("escalate", "This has been passed to IT as a security incident."))],
        [verdict("pass")],
    )
    result = handle(monkeypatch, tmp_path, "BC-2109", text, model)

    assert result.decision == "escalate" and result.fallback == ""
    (ticket,) = escalations(tmp_path)
    assert ticket["employee_id"] == "BC-2109" and ticket["reason"] == "Lost laptop: security incident."


def test_invalid_request_type_comes_back_as_a_tool_error_the_model_can_correct(monkeypatch, tmp_path):
    bad = call("check_request_eligibility", employee_id="BC-2101", item="laptop", request_type="upgrade")
    good = call("check_request_eligibility", employee_id="BC-2101", item="laptop", request_type="standard")
    model = FakeModel([turn(bad), turn(good), turn(submit("deny", "Not due yet."))], [verdict("pass")])
    result = handle(monkeypatch, tmp_path, "BC-2101", "New laptop please", model)

    assert [s["is_error"] for s in result.steps] == [True, False]
    assert "standard" in result.steps[0]["observation"]  # the error names the valid values
    assert result.decision == "deny"


def test_employee_with_a_role_that_has_no_policy_is_escalated_not_crashed(monkeypatch):
    from datetime import date

    from test_tools import patch_data

    patch_data(monkeypatch, "employees", lambda d: d["employees"]["BC-2101"].update(role="astronaut"))
    result = tools.check_request_eligibility("BC-2101", "monitor", as_of=date(2026, 11, 23))
    assert (result["decision"], result["rule"]) == ("escalate", 9)
    assert "astronaut" in result["reason"]
