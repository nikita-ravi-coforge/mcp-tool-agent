import copy
import json
from datetime import date

import pytest

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


# --- check_request_eligibility ---

CASES = json.loads((tools.DATA_DIR / "test_requests.json").read_text())["requests"]


def check(employee_id, item, request_type="standard", as_of=AS_OF):
    return tools.check_request_eligibility(employee_id, item, request_type, as_of=as_of)


def outcome(*args, **kwargs):
    result = check(*args, **kwargs)
    return result["decision"], result["rule"]


def patch_data(monkeypatch, name, change):
    """Run the rules against a modified copy of one data file, for situations no real record covers."""
    data = copy.deepcopy(tools._load(name))
    change(data)
    real_load = tools._load
    monkeypatch.setattr(tools, "_load", lambda n: data if n == name else real_load(n))


@pytest.mark.parametrize("case", CASES, ids=[c["request_id"] for c in CASES])
def test_rule_engine_reproduces_the_expected_outcome_for_every_sample_request(case):
    expected = case["expected"]
    result = check(case["employee_id"], expected["item"], expected["request_type"])
    assert (result["decision"], result["rule"]) == (expected["decision"], expected["rule"])
    for computed in ("eligible_date", "remaining_budget_usd"):
        assert result.get(computed) == expected.get(computed)


def test_all_21_sample_requests_are_covered():
    assert len(CASES) == 21


def test_result_echoes_the_normalised_inputs_and_explains_itself():
    result = check(" bc-2101 ", " Laptop ")
    assert result == {
        "decision": "deny",
        "rule": 11,
        "reason": result["reason"],
        "employee_id": "BC-2101",
        "item": "laptop",
        "request_type": "standard",
        "eligible_date": "2027-02-05",
    }
    assert "2024-02-05" in result["reason"] and "2027-02-05" in result["reason"]


def test_invalid_request_type_raises_so_the_caller_can_correct_it():
    with pytest.raises(ValueError, match="standard, broken, lost, additional, keep_after_leaving"):
        check("BC-2101", "laptop", "upgrade")


def test_rule_2_terminated_employee_is_denied(monkeypatch):
    patch_data(monkeypatch, "employees", lambda d: d["employees"]["BC-2101"].update(status="terminated"))
    assert outcome("BC-2101", "monitor") == ("deny", 2)


def test_rule_2_offboarding_employee_is_denied_anything_but_keeping_the_laptop():
    assert outcome("BC-2112", "monitor") == ("deny", 2)
    assert outcome("BC-2112", "laptop", "broken") == ("deny", 2)


def test_rule_3_item_not_in_catalog_escalates():
    assert outcome("BC-2101", "espresso_machine") == ("escalate", 3)
    assert outcome("BC-2101", "LAP-PRO99-128") == ("escalate", 3)


def test_rule_3_personal_laptop_is_denied_whatever_the_request_type():
    assert outcome("BC-2101", "Personal_Laptop", "additional") == ("deny", 3)


def test_rule_4_lost_phone_is_a_security_incident():
    assert outcome("BC-2110", "mobile_phone", "lost") == ("escalate", 4)


def test_rules_4_to_6_apply_when_the_laptop_is_named_by_sku():
    assert outcome("BC-2109", "LAP-PRO16-64", "lost") == ("escalate", 4)
    assert outcome("BC-2110", "lap-pro16-64", "broken") == ("escalate", 5)
    assert outcome("BC-2111", "LAP-PRO16-64", "additional") == ("escalate", 6)


def test_rule_7_keep_after_leaving_is_denied_when_not_offboarding():
    assert outcome("BC-2101", "laptop", "keep_after_leaving") == ("deny", 7)


def test_rule_7_keep_turns_on_twelve_completed_months_of_use():
    # BC-2113's laptop went into use on 2026-02-10
    assert outcome("BC-2113", "laptop", "keep_after_leaving", as_of=date(2027, 2, 9)) == ("deny", 7)
    assert outcome("BC-2113", "laptop", "keep_after_leaving", as_of=date(2027, 2, 10)) == ("approve", 7)


def test_rule_7_keeping_something_other_than_a_laptop_on_file_goes_to_a_human(monkeypatch):
    assert outcome("BC-2112", "monitor", "keep_after_leaving") == ("escalate", 7)
    patch_data(monkeypatch, "employees", lambda d: d["employees"]["BC-2112"].update(equipment=[]))
    assert outcome("BC-2112", "laptop", "keep_after_leaving") == ("escalate", 7)


def test_rule_8_contractor_laptop_by_sku_escalates():
    assert outcome("BC-9001", "LAP-AIR15-24") == ("escalate", 8)


def test_rule_9_contractor_has_no_peripheral_scope():
    assert outcome("BC-9001", "monitor") == ("deny", 9)


def test_rule_9_role_with_key_cap_zero_is_denied_a_security_key():
    assert outcome("BC-2103", "security_key") == ("deny", 9)


def test_rule_9_role_without_a_laptop_tier_is_denied_a_laptop(monkeypatch):
    patch_data(monkeypatch, "policies", lambda d: d["roles"]["intern"].update(laptop_tier=None))
    assert outcome("BC-2106", "laptop") == ("deny", 9)


def test_rule_9_intern_may_have_electronic_peripherals():
    result = check("BC-2106", "headset")  # first-year budget 925, 380 spent
    assert (result["decision"], result["rule"], result["remaining_budget_usd"]) == ("approve", 14, 545)


def test_rule_10_sku_at_or_below_the_role_tier_falls_through_to_refresh():
    result = check("BC-2103", " lap-air15-24 ")  # standard tier, laptop in use since 2024-09-16
    assert (result["decision"], result["rule"]) == ("deny", 11)
    assert result["item"] == "LAP-AIR15-24"
    assert result["eligible_date"] == "2027-09-16"
    assert outcome("BC-2102", "LAP-PRO14-36") == ("approve", 11)  # eng tier asking for pro


def test_rule_11_no_laptop_on_file_is_a_new_issue(monkeypatch):
    patch_data(monkeypatch, "employees", lambda d: d["employees"]["BC-2101"].update(equipment=[]))
    assert outcome("BC-2101", "laptop") == ("approve", 11)


def test_rule_11_refresh_opens_on_the_eligible_date():
    assert outcome("BC-2101", "laptop", as_of=date(2027, 2, 4)) == ("deny", 11)
    assert outcome("BC-2101", "laptop", as_of=date(2027, 2, 5)) == ("approve", 11)


def test_add_months_gives_the_first_day_the_months_are_complete():
    assert tools.add_months(date(2024, 2, 5), 36) == date(2027, 2, 5)
    assert tools.add_months(date(2024, 11, 30), 3) == date(2025, 3, 1)  # no 30 February
    assert tools.months_between(date(2024, 11, 30), date(2025, 2, 28)) == 2
    assert tools.months_between(date(2024, 11, 30), date(2025, 3, 1)) == 3


def test_rule_12_key_cap_reached_is_denied():
    assert outcome("BC-2102", "security_key") == ("deny", 12)


def test_rule_12_lost_key_with_no_key_on_file_escalates():
    assert outcome("BC-9001", "security_key", "lost") == ("escalate", 12)


def test_rule_13_second_company_phone_is_denied():
    assert outcome("BC-2110", "mobile_phone") == ("deny", 13)


def test_rule_14_annual_budget_counts_only_spend_in_the_last_12_months():
    # BC-2105 bought a $120 webcam on 2026-03-10; annual budget is $500
    assert check("BC-2105", "MON-27-4K", as_of=date(2027, 3, 9))["remaining_budget_usd"] == 380
    assert check("BC-2105", "MON-27-4K", as_of=date(2027, 3, 10))["remaining_budget_usd"] == 500


def test_rule_14_budget_switches_from_first_year_to_annual_at_12_months_tenure():
    # BC-2104 was hired 2026-05-04 and spent $1,980 in the first five months; a webcam costs $120
    before = check("BC-2104", "webcam", as_of=date(2027, 5, 3))  # 2500 - 1980
    assert (before["decision"], before["remaining_budget_usd"]) == ("approve", 520)
    after = check("BC-2104", "webcam", as_of=date(2027, 5, 4))  # 500 - 1980, all within 12 months
    assert (after["decision"], after["remaining_budget_usd"]) == ("escalate", -1480)


# --- flag_for_human_review ---


@pytest.fixture
def escalations(tmp_path, monkeypatch):
    path = tmp_path / "escalations.jsonl"
    monkeypatch.setenv("EQUIPMENT_ESCALATIONS_FILE", str(path))
    return path


def test_flag_for_human_review_appends_one_json_line_per_ticket(escalations):
    first = tools.flag_for_human_review(" bc-2109 ", "I left my laptop in an Uber", "Lost laptop: security incident")
    second = tools.flag_for_human_review("BC-2110", "Screen cracked", "Broken laptop")
    assert first["status"] == second["status"] == "queued"
    assert first["ticket_id"] != second["ticket_id"]

    lines = [json.loads(line) for line in escalations.read_text().splitlines()]
    assert [line["ticket_id"] for line in lines] == [first["ticket_id"], second["ticket_id"]]
    assert lines[0]["employee_id"] == "BC-2109"
    assert lines[0]["request"] == "I left my laptop in an Uber"
    assert lines[0]["reason"] == "Lost laptop: security incident"
    assert lines[0]["status"] == "queued"


def test_flag_for_human_review_defaults_to_escalations_jsonl_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("EQUIPMENT_ESCALATIONS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    tools.flag_for_human_review("BC-2109", "lost laptop", "security incident")
    assert len((tmp_path / "escalations.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("reason", ["", "   \n"])
def test_flag_for_human_review_without_a_reason_raises_and_writes_nothing(escalations, reason):
    with pytest.raises(ValueError, match="reason"):
        tools.flag_for_human_review("BC-2109", "lost laptop", reason)
    assert not escalations.exists()
