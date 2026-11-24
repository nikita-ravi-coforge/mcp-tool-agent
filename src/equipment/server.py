"""MCP server: thin wrappers that expose the functions in tools.py. Run with `python -m equipment.server`."""

from mcp.server.mcpserver import MCPServer

from equipment import tools

mcp = MCPServer("beehive-equipment")


@mcp.tool()
def get_employee_info(employee_id: str) -> dict:
    """Look up an employee by ID (e.g. BC-2102). Returns role, tenure in months, employment status,
    on-call flag, equipment on file and peripheral spend. Unknown IDs return found=false."""
    return tools.get_employee_info(employee_id)


if __name__ == "__main__":
    mcp.run()
