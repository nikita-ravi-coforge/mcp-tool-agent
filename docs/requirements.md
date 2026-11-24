# IT Equipment Request Handler: Requirements

Company: BEEHIVE Cloud, Inc. (fictional). Policy: IT-EQ-100 rev A. All data is synthetic (`data/`).

## 1. What the system does

An employee sends a free-text equipment request. The system looks up the employee, applies the policy,
and returns one of three outcomes with a written response:

| Outcome    | Meaning                                                                 |
|------------|-------------------------------------------------------------------------|
| `approve`  | The request is clearly inside policy.                                   |
| `deny`     | The request is clearly outside policy, and policy says no.              |
| `escalate` | Policy cannot decide alone. A human reviewer gets a ticket with a reason. |

## 2. Data a request needs

| Field          | Source                    | Notes |
|----------------|---------------------------|-------|
| `employee_id`  | given with the request    | e.g. `BC-2102` |
| request text   | the employee              | free text |
| `item`         | read from the text        | a category (`laptop`, `monitor`, `security_key`, `mobile_phone`, ...), a catalog SKU (`LAP-PRO16-64`), or `personal_laptop` |
| `request_type` | read from the text        | `standard`, `broken`, `lost`, `additional`, `keep_after_leaving` |
| role, status, tenure, on-call, equipment, spend | `employees.json` | never taken from the request text |
| limits per role | `policies.json`          | laptop tier, refresh interval, peripheral budgets, security key cap |
| price, tier    | `catalog.json`            | |
| today's date   | `EQUIPMENT_AS_OF` or the clock | frozen to 2026-11-23 in tests |

## 3. Policy rules

Rules run in order. The first rule that decides wins. "Months" are completed calendar months.
Request types `broken`, `lost` and `additional` change the outcome only where a rule names them; otherwise
the request is treated as `standard`.

| #  | Check | Outcome when it applies |
|----|-------|-------------------------|
| 1  | Employee ID not found | escalate |
| 2  | Status `terminated` | deny |
|    | Status `on_leave` | escalate |
|    | Status `offboarding` and request type is not `keep_after_leaving` | deny |
| 3  | Item is `personal_laptop` | deny |
|    | Item is not in the catalog | escalate |
| 4  | `lost` laptop or phone | escalate (security incident) |
| 5  | `broken` laptop | escalate (IT assesses repair vs replace) |
| 6  | `additional` laptop | escalate (needs justification, manager and IT approval) |
| 7  | `keep_after_leaving`: not offboarding | deny |
|    | ... item is not a laptop, or no laptop is on file | escalate |
|    | ... left for cause | deny |
|    | ... laptop in use 12 months or more | approve |
|    | ... laptop in use under 12 months | deny (may buy at net value) |
| 8  | Contractor asks for a laptop | escalate (IT + Security exception) |
| 9  | Role has no entry in the policy | escalate |
|    | Not entitled: role has no laptop tier; peripheral outside role scope; security key cap is 0; phone and not on call | deny |
| 10 | Specific laptop SKU above the role's tier | escalate |
| 11 | Laptop: none on file | approve |
|    | ... in use for at least the refresh interval (36 months) | approve |
|    | ... otherwise | deny, with `eligible_date` |
| 12 | Security key, `lost`, but no key on file is marked `reported_lost` | escalate |
|    | ... active keys below the role cap | approve |
|    | ... otherwise | deny |
| 13 | Phone: already has an active company phone | deny; otherwise approve |
| 14 | Peripheral: price is within remaining budget | approve, with `remaining_budget_usd` |
|    | ... over budget | escalate (manager approval) |

Budget for rule 14: under 12 months' tenure, first-year budget minus all spend since hire. Otherwise,
annual budget minus spend dated in the 12 months before today (a purchase exactly 12 months ago no longer counts).

## 4. What counts as ambiguous (always escalate, never guess)

1. **Policy says a human must decide**: any rule above whose outcome is `escalate`.
2. **The request cannot be pinned down**: the text does not identify one item or one request type, or
   asks for several things at once. The agent escalates with that as the reason.
3. **The tools could not answer**: a tool errored or returned something the agent cannot interpret.
4. **The draft cannot be verified**: the response still fails verification after two revisions (section 6).

## 5. Tool contracts

Each tool is a plain function in `equipment/tools.py`, wrapped unchanged in `equipment/server.py`.
All return JSON objects. Unknown IDs and roles are normal results, not errors.

| Tool | Returns |
|------|---------|
| `get_employee_info(employee_id)` | `{found: true, employee_id, name, role, status, on_call, hire_date, tenure_months, equipment[], peripheral_spend[], ...}` or `{found: false, employee_id, error}` |
| `get_policy_limits(role)` | `{found: true, role, laptop_tier, laptop_refresh_months, peripheral_budget_first_year_usd, peripheral_budget_annual_usd, peripheral_scope, security_keys_max, note?}` or `{found: false, role, error, known_roles[]}` |
| `check_request_eligibility(employee_id, item, request_type="standard")` | `{decision, rule, reason, employee_id, item, request_type, eligible_date?, remaining_budget_usd?}`. An invalid `request_type` raises an error so the caller can correct it. |
| `flag_for_human_review(employee_id, request, reason)` | Appends one line to `escalations.jsonl` and returns `{ticket_id, status: "queued"}`. An empty `reason` raises an error. |

## 6. Agent behaviour

1. **ReAct loop.** The agent connects to the server as an MCP client. Each step records a Thought, an
   Action (tool name and arguments) and an Observation (tool result). At most 8 model turns, revisions included.
2. **Draft.** The agent produces `{decision, response}`.
3. **Deterministic verifier (code).** The draft passes only if all hold:
   - its decision equals the decision last returned by `check_request_eligibility` (a draft of `escalate`
     is always allowed, and is the only decision allowed if that check never succeeded);
   - every date and dollar amount in the response appears in a tool observation;
   - on `escalate`, `flag_for_human_review` was called exactly once; on `approve` or `deny`, never.
4. **Reflection (model).** Only if the verifier passed. A second call judges whether the response commits to anything the
   observations do not support. It returns pass, or fail with feedback.
5. **Revise.** A failure from step 3 or 4 goes back to the agent, at most twice. If the draft still
   fails, the code raises the ticket itself with reason "could not verify response" and sends a reply that
   states no policy facts. The same happens if the model refuses or runs out of turns.

## 7. Acceptance

- Unit tests cover every rule row in section 3 and the edge cases in section 5.
- Integration tests call all four tools through a real MCP client over stdio.
- The rule engine reproduces `expected` for all 21 cases in `data/test_requests.json`.
- Agent tests use a scripted fake model; no network in CI.
- CI fails under 90% branch coverage.
