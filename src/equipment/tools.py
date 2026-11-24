"""Plain-Python business logic behind the MCP tools. No MCP imports here, so it is unit-testable on its own."""

import json
import os
from datetime import date
from functools import cache
from pathlib import Path

REQUEST_TYPES = ("standard", "broken", "lost", "additional", "keep_after_leaving")
TIER_RANK = {"standard": 0, "pro": 1, "eng": 2}

DATA_DIR = Path(os.environ.get("EQUIPMENT_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")


@cache
def _load(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text())


def today() -> date:
    """Today's date, or EQUIPMENT_AS_OF when set so date maths are reproducible."""
    frozen = os.environ.get("EQUIPMENT_AS_OF")
    return date.fromisoformat(frozen) if frozen else date.today()


def months_between(start: date, end: date) -> int:
    """Completed calendar months from start to end."""
    return (end.year - start.year) * 12 + (end.month - start.month) - (end.day < start.day)


def add_months(start: date, months: int) -> date:
    """The first date on which `months` completed calendar months have passed since start."""
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    try:
        return date(year, month + 1, start.day)
    except ValueError:  # e.g. the 31st in a 30-day month: the month completes on the 1st of the next
        return add_months(start.replace(day=1), months + 1)


def get_employee_info(employee_id: str, as_of: date | None = None) -> dict:
    """Role, tenure, status and equipment on file for one employee."""
    employee_id = employee_id.strip().upper()
    record = _load("employees")["employees"].get(employee_id)
    if record is None:
        return {"found": False, "employee_id": employee_id, "error": f"No employee with ID {employee_id}."}
    hired = date.fromisoformat(record["hire_date"])
    return {
        "found": True,
        "employee_id": employee_id,
        **record,
        "tenure_months": months_between(hired, as_of or today()),
    }


def get_policy_limits(role: str) -> dict:
    """What a role is entitled to: laptop tier, refresh interval, peripheral budgets, security key cap."""
    role = role.strip().lower()
    roles = _load("policies")["roles"]
    if role not in roles:
        return {"found": False, "role": role, "error": f"No policy for role {role}.", "known_roles": list(roles)}
    return {"found": True, "role": role, **roles[role]}


def _remaining_budget(employee: dict, limits: dict, as_of: date) -> int:
    """Rule 14: first-year budget minus all spend, or annual budget minus the last 12 months' spend."""
    spend = employee["peripheral_spend"]
    if employee["tenure_months"] < 12:
        return limits["peripheral_budget_first_year_usd"] - sum(s["amount_usd"] for s in spend)
    recent = [s for s in spend if months_between(date.fromisoformat(s["date"]), as_of) < 12]
    return limits["peripheral_budget_annual_usd"] - sum(s["amount_usd"] for s in recent)


def check_request_eligibility(
    employee_id: str, item: str, request_type: str = "standard", as_of: date | None = None
) -> dict:
    """Apply policy rules 1-14 in order; the first rule that decides wins."""
    request_type = request_type.strip().lower()
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"request_type must be one of: {', '.join(REQUEST_TYPES)}. Got {request_type!r}.")
    as_of = as_of or today()
    catalog = _load("catalog")
    by_sku = {i["sku"]: i for i in catalog["items"]}
    by_category = {i["category"]: i for i in catalog["items"]}
    item = item.strip().upper() if item.strip().upper() in by_sku else item.strip().lower()
    employee = get_employee_info(employee_id, as_of)

    def result(decision: str, rule: int, reason: str, **computed) -> dict:
        return {
            "decision": decision,
            "rule": rule,
            "reason": reason,
            "employee_id": employee["employee_id"],
            "item": item,
            "request_type": request_type,
            **computed,
        }

    # 1. Employee exists
    if not employee["found"]:
        return result("escalate", 1, "Employee ID not found.")

    # 2. Employment status
    status = employee["status"]
    if status == "terminated":
        return result("deny", 2, "Employee is terminated.")
    if status == "on_leave":
        return result("escalate", 2, "Employee is on leave; requests are held for review.")
    if status == "offboarding" and request_type != "keep_after_leaving":
        return result("deny", 2, "Employee is offboarding; no new equipment is issued.")

    # 3. Item is known
    if item in catalog["not_allowed"]:
        return result("deny", 3, catalog["not_allowed"][item])
    product = by_sku.get(item) or by_category.get(item)
    if product is None:
        return result("escalate", 3, f"'{item}' is not in the equipment catalog.")
    category = product["category"]
    active = [e for e in employee["equipment"] if e["status"] == "active"]
    laptop = next((e for e in active if e["category"] == "laptop"), None)
    laptop_months = months_between(date.fromisoformat(laptop["in_use_since"]), as_of) if laptop else None

    # 4-6. Lost, broken and additional devices go to a human
    if request_type == "lost" and category in ("laptop", "mobile_phone"):
        return result(
            "escalate",
            4,
            "A lost laptop or phone is a security incident: report it to IT and your manager in writing within "
            "24 hours, file a police report within 72 hours if stolen, and IT remote-wipes the device.",
        )
    if request_type == "broken" and category == "laptop":
        return result("escalate", 5, "Broken laptops go through the replacement program; IT assesses repair vs replace.")
    if request_type == "additional" and category == "laptop":
        return result("escalate", 6, "A second laptop needs a business justification plus manager and IT approval.")

    # 7. Keeping the laptop when leaving
    if request_type == "keep_after_leaving":
        if status != "offboarding":
            return result("deny", 7, "Keeping a laptop applies only to employees who are offboarding.")
        if category != "laptop" or laptop is None:
            return result("escalate", 7, "The keep option covers only a company laptop on file for the employee.")
        if employee["offboarding_for_cause"]:
            return result("deny", 7, "The keep option is void when an employee leaves for cause.")
        used = f"Laptop in use since {laptop['in_use_since']} ({laptop_months} months"
        if laptop_months >= 12:
            return result("approve", 7, f"{used}, at least 12). Kept at no cost after IT wipes it.")
        return result("deny", 7, f"{used}, under 12). It cannot be kept free, but may be bought at current net value.")

    # 8. Contractor laptops
    role = employee["role"]
    if role == "contractor" and category == "laptop":
        return result("escalate", 8, "Contractor laptops are an exception needing IT + Security approval.")

    # 9. Entitlement
    limits = get_policy_limits(role)
    if category == "laptop":
        entitled = limits["laptop_tier"] is not None
    elif category == "security_key":
        entitled = limits["security_keys_max"] > 0
    elif category == "mobile_phone":
        entitled = employee["on_call"]
    else:
        scope = limits["peripheral_scope"]
        entitled = scope == "all" or (scope == "electronic_only" and category in catalog["electronic_peripherals"])
    if not entitled:
        why = "company phones are for on-call staff" if category == "mobile_phone" else f"not covered for role {role}"
        return result("deny", 9, f"Not entitled to {category}: {why}.")

    if category == "laptop":
        # 10. Laptop spec
        tier = limits["laptop_tier"]
        if item in by_sku and TIER_RANK[product["tier"]] > TIER_RANK[tier]:
            return result(
                "escalate",
                10,
                f"{item} is {product['tier']} tier, above the {tier} tier for role {role}; "
                "a higher spec needs prior approval.",
            )
        # 11. Laptop refresh
        if laptop is None:
            return result("approve", 11, f"No active laptop on file; new issue at the {tier} tier.")
        refresh = limits["laptop_refresh_months"]
        used = f"In use since {laptop['in_use_since']} ({laptop_months} months); refresh is every {refresh} months."
        if laptop_months >= refresh:
            return result("approve", 11, used)
        eligible = add_months(date.fromisoformat(laptop["in_use_since"]), refresh).isoformat()
        return result("deny", 11, f"{used} Eligible from {eligible}.", eligible_date=eligible)

    # 12. Security key
    if category == "security_key":
        keys = [e for e in employee["equipment"] if e["category"] == "security_key"]
        if request_type == "lost" and not any(k["status"] == "reported_lost" for k in keys):
            return result(
                "escalate",
                12,
                "No key on file is marked reported_lost. The lost key must be reported and revoked before "
                "a replacement ships.",
            )
        active_keys = sum(k["status"] == "active" for k in keys)
        cap = limits["security_keys_max"]
        counts = f"{active_keys} active security key(s) on file; the maximum for role {role} is {cap}."
        return result("approve" if active_keys < cap else "deny", 12, counts)

    # 13. Mobile phone
    if category == "mobile_phone":
        if any(e["category"] == "mobile_phone" for e in active):
            return result("deny", 13, "Employee already has an active company phone.")
        return result("approve", 13, "Employee is on call and has no active company phone on file.")

    # 14. Peripheral budget
    remaining = _remaining_budget(employee, limits, as_of)
    price = product["unit_cost_usd"]
    if price <= remaining:
        reason = f"{product['sku']} costs ${price}; ${remaining} of the peripheral budget remains."
        return result("approve", 14, reason, remaining_budget_usd=remaining)
    reason = f"{product['sku']} costs ${price} but ${remaining} of the peripheral budget remains; needs manager approval."
    return result("escalate", 14, reason, remaining_budget_usd=remaining)
