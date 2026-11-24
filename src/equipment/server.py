"""MCP server: thin wrappers that expose the functions in tools.py. Run with `python -m equipment.server`."""

from typing import Literal

from mcp.server.mcpserver import MCPServer

from equipment import tools

mcp = MCPServer("beehive-equipment")


@mcp.tool()
def get_employee_info(employee_id: str) -> dict:
    """Look up an employee by ID (e.g. BC-2102). Returns role, tenure in months, employment status,
    on-call flag, equipment on file and peripheral spend. Unknown IDs return found=false."""
    return tools.get_employee_info(employee_id)


@mcp.tool()
def get_policy_limits(role: str) -> dict:
    """Look up what a role is entitled to under policy IT-EQ-100. `role` is the role string returned by
    get_employee_info (e.g. software_engineer, intern, contractor). Returns laptop_tier,
    laptop_refresh_months, first-year and annual peripheral budgets in USD, peripheral_scope
    (all | electronic_only | none), security_keys_max and sometimes a note. Use it to explain a limit
    to the employee; use check_request_eligibility to decide a request. Unknown roles return
    found=false with known_roles."""
    return tools.get_policy_limits(role)


@mcp.tool()
def check_request_eligibility(
    employee_id: str,
    item: str,
    request_type: Literal["standard", "broken", "lost", "additional", "keep_after_leaving"] = "standard",
) -> dict:
    """Decide one equipment request under policy IT-EQ-100. Call this for every request before answering;
    its decision is authoritative.

    employee_id: the requester's ID, e.g. BC-2102.
    item: exactly one of: a category (laptop, monitor, dock, headset, webcam, keyboard_mouse, laptop_stand,
        chair, standing_desk, security_key, mobile_phone); a catalog SKU when the employee names a specific
        model (e.g. LAP-PRO16-64); or personal_laptop when they ask to work on their own laptop.
    request_type: standard (new item or routine refresh), broken (replace a damaged item), lost (replace a
        lost or stolen item), additional (a second laptop alongside the current one), or keep_after_leaving
        (keep the laptop when leaving the company). Any other value is an error; retry with a valid one.

    Returns decision (approve | deny | escalate), the rule number that decided, a reason, and where relevant
    eligible_date (when a denied laptop refresh opens) or remaining_budget_usd (peripheral budget left).
    On escalate, also call flag_for_human_review."""
    return tools.check_request_eligibility(employee_id, item, request_type)


if __name__ == "__main__":
    mcp.run()
