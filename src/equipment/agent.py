"""ReAct agent over the equipment MCP tools: draft, verify in code, reflect with a second model call, revise.

    python -m equipment.agent BC-2102 "I'd like a second monitor"
"""

import json
import re

FLAG = "flag_for_human_review"
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
