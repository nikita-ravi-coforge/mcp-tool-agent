"""Plain-Python business logic behind the MCP tools. No MCP imports here, so it is unit-testable on its own."""

import json
import os
from datetime import date
from functools import cache
from pathlib import Path

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
