"""Agent tests: a scripted fake model and canned tool results. No network, no subprocess."""

import json

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
