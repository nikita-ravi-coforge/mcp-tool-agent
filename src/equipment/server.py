"""MCP server: thin wrappers that expose the functions in tools.py. Run with `python -m equipment.server`."""

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


if __name__ == "__main__":
    mcp.run()
