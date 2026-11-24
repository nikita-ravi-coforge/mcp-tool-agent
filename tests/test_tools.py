from datetime import date

from equipment import tools

AS_OF = date(2026, 11, 23)


def test_get_employee_info_returns_role_tenure_and_equipment():
    info = tools.get_employee_info("BC-2102", as_of=AS_OF)
    assert info["found"] is True
    assert info["employee_id"] == "BC-2102"
    assert info["role"] == "site_reliability_engineer"
    assert info["status"] == "active"
    assert info["on_call"] is True
    assert info["tenure_months"] == 80  # hired 2020-03-02
    assert [e["category"] for e in info["equipment"]] == ["laptop", "security_key", "security_key"]


def test_get_employee_info_tenure_counts_only_completed_months():
    # hired 2026-08-03 -> 3 full months on 2026-11-23, still 3 the day before the 4th
    assert tools.get_employee_info("BC-9001", as_of=AS_OF)["tenure_months"] == 3
    assert tools.get_employee_info("BC-9001", as_of=date(2026, 12, 2))["tenure_months"] == 3
    assert tools.get_employee_info("BC-9001", as_of=date(2026, 12, 3))["tenure_months"] == 4


def test_get_employee_info_unknown_id_is_not_found_rather_than_an_error():
    info = tools.get_employee_info("BC-9999", as_of=AS_OF)
    assert info == {"found": False, "employee_id": "BC-9999", "error": "No employee with ID BC-9999."}


def test_get_employee_info_normalises_whitespace_and_case():
    assert tools.get_employee_info("  bc-2102 ", as_of=AS_OF)["found"] is True


# --- get_policy_limits ---


def test_get_policy_limits_returns_the_limits_for_a_role():
    assert tools.get_policy_limits("product_designer") == {
        "found": True,
        "role": "product_designer",
        "laptop_tier": "pro",
        "laptop_refresh_months": 36,
        "peripheral_budget_first_year_usd": 2500,
        "peripheral_budget_annual_usd": 500,
        "peripheral_scope": "all",
        "security_keys_max": 2,
    }


def test_get_policy_limits_includes_the_note_only_where_the_role_has_one():
    contractor = tools.get_policy_limits(" Contractor ")
    assert contractor["laptop_tier"] is None
    assert "IT + Security" in contractor["note"]
    assert "note" not in tools.get_policy_limits("software_engineer")


def test_get_policy_limits_unknown_role_lists_the_known_roles():
    limits = tools.get_policy_limits("astronaut")
    assert limits["found"] is False
    assert limits["role"] == "astronaut"
    assert "astronaut" in limits["error"]
    assert "intern" in limits["known_roles"] and len(limits["known_roles"]) == 9
