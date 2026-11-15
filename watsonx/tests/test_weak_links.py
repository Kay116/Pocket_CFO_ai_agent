"""Regression tests for weak links found in review (2026-11-15).

2. Debt payments listed as expenses AND as debts were double-counted.
3. Category matching was exact, so "Electric", "Food", "Car insurance" were not essential.
4. Two debts with the same name were merged in the payoff simulation.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_financial_plan as bfp_mod  # noqa: E402
import calculate_cash_flow as ccf_mod  # noqa: E402
import pocket_cfo_data_tools as dt  # noqa: E402
from build_financial_plan import build_financial_plan  # noqa: E402
from calculate_cash_flow import calculate_cash_flow  # noqa: E402
from simulate_debt_payoff import simulate_debt_payoff  # noqa: E402
from test_data_tools import ALICE, cloud, ctx, run  # noqa: E402,F401

EXP = {"Rent": 1200, "Groceries": 400, "Utilities": 150, "Transportation": 200,
       "Restaurants": 250, "Shopping": 200, "Entertainment": 100, "Other": 200}
CARD = {"name": "Credit card", "balance": 8000, "apr": 22, "minimum_payment": 240}


# ----------------------------------------------------------------- 2. double counting
@pytest.mark.parametrize("debt_name,extra", [
    ("Credit card", {"Credit card payment": 240}),   # debt keyword
    ("Visa", {"Visa": 240}),                         # same name as the debt, no keyword
    ("Credit card", {"Card minimum": 240}),          # keyword + minimum
    ("Credit card", {"Student loan": 300}),          # a debt that should be in debts instead
])
def test_debt_payment_in_expenses_is_flagged(debt_name, extra):
    r = run(build_financial_plan, 3500, dict(EXP, **extra), 1000, [dict(CARD, name=debt_name)], 5000)
    assert r["error"] == "possible_double_count"
    assert r["categories"] == list(extra)


@pytest.mark.parametrize("expense", ["Car insurance", "Gas", "Car gas", "Phone", "Netflix"])
def test_lookalike_living_costs_are_not_flagged(expense):
    debts = [{"name": "Car loan", "balance": 9000, "apr": 6, "minimum_payment": 300}]
    r = run(build_financial_plan, 3500, dict(EXP, **{expense: 120}), 1000, debts, 5000)
    assert "error" not in r, r


def test_mortgage_listed_twice_is_flagged():
    debts = [{"name": "Mortgage", "balance": 200000, "apr": 6.5, "minimum_payment": 1400}]
    r = run(build_financial_plan, 6000, {"Mortgage": 1400, "Groceries": 500}, 2000, debts, 10000)
    assert r["error"] == "possible_double_count" and r["categories"] == ["Mortgage"]


def test_confirmation_overrides_flag():
    r = run(build_financial_plan, 3500, dict(EXP, **{"Credit card payment": 240}), 1000, [CARD], 5000,
            confirm_expenses_exclude_debt_payments=True)
    assert r["allocation"]["status"] == "ok"


def test_no_debts_means_no_double_count_check():
    r = run(build_financial_plan, 3500, dict(EXP, **{"Car loan": 300}), 1000, [], 5000)
    assert "error" not in r


def test_demo_plan_unchanged():
    r = run(build_financial_plan, 3500, EXP, 1000, [CARD], 5000)
    assert r["allocation"]["total_debt_payment"] == 632 and r["allocation"]["savings_contribution"] == 168
    assert r["debt"]["months_to_debt_free"] == 15 and r["savings"]["goal_reached_month"] == 17


# ----------------------------------------------------------------- 3. category matching
@pytest.mark.parametrize("name,group", [
    ("Rent/Mortgage", "housing"), ("Electric", "utilities"), ("Gas & electric", "utilities"),
    ("Water bill", "utilities"), ("Food", "groceries"), ("Dining out", "restaurants"),
    ("Food delivery", "restaurants"), ("Car insurance", "insurance"), ("Gas", "transportation"),
    ("MetroCard", "transportation"), ("Cell phone", "phone_internet"), ("Daycare", "childcare"),
    ("Prescriptions", "health"), ("Netflix", "subscriptions"), ("Clothes", "shopping"),
    ("Gym", "entertainment"), ("Vacation", "travel"), ("Misc", "other"), ("Student loan", "debt"),
    ("Dog food", "pets"), ("Barber", None), ("Gifts", None),
])
def test_classification(name, group):
    assert ccf_mod.classify_category(name) == group


def test_synonyms_count_as_essential():
    r = run(calculate_cash_flow, 4000, {"Rent/Mortgage": 1500, "Electric": 100, "Food": 400, "Gas": 150,
                                         "Car insurance": 120, "Dining out": 500, "Gifts": 50})
    assert r["essential_expenses"] == 2270
    assert r["fixed_expenses"] == 1720
    assert {f["group"] for f in r["above_reference_range"]} == {"housing", "restaurants"}
    assert r["unrecognized_categories"] == ["Gifts"]


def test_reference_range_is_checked_per_group():
    r = run(calculate_cash_flow, 3000, {"Restaurants": 100, "Takeout": 100, "Rent": 900})
    flags = r["above_reference_range"]
    assert len(flags) == 1 and flags[0]["category"] == "Restaurants + Takeout" and flags[0]["share_of_income_pct"] == 6.67


def test_essential_totals_agree_between_tools():
    exp = {"Rent/Mortgage": 1500, "Electric": 100, "Food": 400, "Car insurance": 120, "Dining out": 500}
    a = run(calculate_cash_flow, 4000, exp)["essential_expenses"]
    b = run(build_financial_plan, 4000, exp, 0, [], 6000)["cash_flow"]["essential_expenses"]
    assert a == b == 2120


def test_classifier_copies_are_identical():
    assert ccf_mod.CATEGORY_RULES == bfp_mod.CATEGORY_RULES
    for name in ["Rent", "Car insurance", "Food delivery", "Hobbies", "Gas & electric", "xyz"]:
        assert ccf_mod.classify_category(name) == bfp_mod.classify_category(name)


def test_nan_income_rejected():
    assert "error" in run(calculate_cash_flow, float("nan"), {"Rent": 1})


# ----------------------------------------------------------------- 4. duplicate debt names
DUPES = [{"name": "Card", "balance": 3000, "apr": 25, "minimum_payment": 90},
         {"name": "Card", "balance": 5000, "apr": 18, "minimum_payment": 150}]
DISTINCT = [DUPES[0], dict(DUPES[1], name="Card 2")]


def test_duplicate_names_simulate_like_distinct_names():
    dup, ok = run(simulate_debt_payoff, DUPES, 600), run(simulate_debt_payoff, DISTINCT, 600)
    for strategy in ("avalanche", "snowball", "minimum_payments_only"):
        assert dup[strategy]["months"] == ok[strategy]["months"]
        assert dup[strategy]["total_interest"] == ok[strategy]["total_interest"]
    assert [p["name"] for p in dup["avalanche"]["payoff_order"]] == ["Card", "Card (2)"]
    assert dup["avalanche"]["months"] == 16


def test_duplicate_names_in_full_plan():
    r = run(build_financial_plan, 4000, {"Rent": 1500, "Groceries": 400}, 0, DUPES, None)
    ok = run(build_financial_plan, 4000, {"Rent": 1500, "Groceries": 400}, 0, DISTINCT, None)
    assert r["debt"]["months_to_debt_free"] == ok["debt"]["months_to_debt_free"]
    assert r["debt"]["interest_paid"] == ok["debt"]["interest_paid"]
    assert len(r["debt"]["payoff_order"]) == 2


def test_duplicate_names_are_kept_apart_in_saved_profile(cloud):
    r = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True,
            debts=[dt.DebtInput(**d) for d in DUPES])
    assert [d["name"] for d in r["profile"]["debts"]] == ["Card", "Card (2)"]


# ----------------------------------------------------------------- 5. one strategy everywhere
import yaml  # noqa: E402

MULTI = [
    [{"name": "Small", "balance": 500, "apr": 15, "minimum_payment": 25},
     {"name": "Big", "balance": 6000, "apr": 16, "minimum_payment": 150}],          # snowball nearly free
    [{"name": "Card", "balance": 3000, "apr": 24, "minimum_payment": 90},
     {"name": "Family loan", "balance": 1000, "apr": 0, "minimum_payment": 50}],
    [CARD],
]


@pytest.mark.parametrize("debts", MULTI)
def test_debt_agent_and_plan_report_the_same_numbers(debts):
    plan = run(build_financial_plan, 4000, {"Rent": 1500, "Groceries": 400}, 0, debts, None)
    sim = run(simulate_debt_payoff, debts, plan["allocation"]["total_debt_payment"])
    chosen = sim[sim["recommended_strategy"]]
    assert sim["recommended_strategy"] == "avalanche"
    assert chosen["months"] == plan["debt"]["months_to_debt_free"]
    assert chosen["total_interest"] == plan["debt"]["interest_paid"]
    assert sim["interest_saved_vs_minimum_only"] == plan["debt"]["interest_saved_vs_minimum_only"]


def test_snowball_reported_as_low_cost_alternative():
    sim = run(simulate_debt_payoff, MULTI[0], 500)
    assert sim["recommended_strategy"] == "avalanche"
    assert sim["snowball_is_low_cost_alternative"] is True
    assert sim["snowball_extra_interest_vs_avalanche"] == round(
        sim["snowball"]["total_interest"] - sim["avalanche"]["total_interest"], 2)


# ----------------------------------------------------------------- 6. instructions agree
def _agent(name):
    p = Path(__file__).resolve().parents[1] / "agents" / f"{name}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def test_supervisor_has_one_calculation_tool():
    tools = _agent("pocket_cfo_supervisor")["tools"]
    assert "build_financial_plan" in tools
    assert "calculate_cash_flow" not in tools and "plan_surplus_allocation" not in tools


def test_no_agent_refers_to_a_tool_it_cannot_reach():
    for name in ["pocket_cfo_supervisor", "budget_agent", "debt_management_agent", "savings_agent",
                 "financial_coach_agent"]:
        assert "plan_surplus_allocation" not in _agent(name)["instructions"], name


# ----------------------------------------------------------------- 7. shortfall
def test_shortfall_reports_the_gap():
    r = run(build_financial_plan, 3500, EXP, 1000, [dict(CARD, minimum_payment=900)], 5000)
    assert r["plan_status"] == "shortfall" and r["cash_flow"]["status"] == "surplus"
    a = r["allocation"]
    assert a["required_minimum_payments"] == 900 and a["minimum_payments_covered"] is False
    assert a["monthly_gap"] == 100
    assert r["debt"]["feasible"] is False and r["debt"]["infeasible_reason"] == "payment_below_minimums"
    assert r["milestones"] == []


def test_deficit_gap_includes_minimums():
    r = run(build_financial_plan, 2500, EXP, 1000, [CARD], 5000)  # expenses 2700
    assert r["plan_status"] == "deficit" and r["allocation"]["monthly_gap"] == 440


def test_ok_plan_has_no_gap():
    a = run(build_financial_plan, 3500, EXP, 1000, [CARD], 5000)["allocation"]
    assert a["monthly_gap"] == 0 and a["minimum_payments_covered"] is True


def test_supervisor_has_shortfall_guideline():
    g = {x["display_name"]: x for x in _agent("pocket_cfo_supervisor")["guidelines"]}
    assert "shortfall" in g["Cannot cover minimum payments"]["condition"]
    assert "monthly_gap" in g["Cannot cover minimum payments"]["action"]


def test_allocation_tools_agree_on_gap():
    from plan_surplus_allocation import plan_surplus_allocation
    for net, mins in [(800, 900), (-200, 240), (800, 240)]:
        ref = run(plan_surplus_allocation, net, [dict(CARD, minimum_payment=mins)], 1000, 5000)
        exp = {"Rent": 3500 - net}
        mine = run(build_financial_plan, 3500, exp, 1000, [dict(CARD, minimum_payment=mins)], 5000)["allocation"]
        assert ref["status"] == mine["status"] and ref["monthly_gap"] == mine["monthly_gap"]
