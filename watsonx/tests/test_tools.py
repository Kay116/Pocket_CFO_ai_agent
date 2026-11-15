"""Tests for the watsonx Orchestrate Python tools (run: pytest watsonx/tests)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from calculate_cash_flow import calculate_cash_flow  # noqa: E402
from compound_interest_scenarios import compound_interest_scenarios  # noqa: E402
from plan_surplus_allocation import DebtInput as AllocDebt, plan_surplus_allocation  # noqa: E402
from savings_goal_timeline import savings_goal_timeline  # noqa: E402
from simulate_debt_payoff import DebtInput, simulate_debt_payoff  # noqa: E402

DEMO_EXPENSES = {"Rent": 1200, "Groceries": 350, "Transportation": 150, "Restaurants": 300,
                 "Shopping": 200, "Utilities": 150, "Entertainment": 100, "Other": 250}


def run(tool_fn, *args, **kwargs):
    """Decorated ADK tools return a ToolResponse when called locally; unwrap it."""
    out = tool_fn(*args, **kwargs)
    return out.content if hasattr(out, "content") else out


CARD = dict(name="Credit card", balance=8000, apr=22, minimum_payment=240)


def test_demo_cash_flow():
    r = run(calculate_cash_flow, 3500, DEMO_EXPENSES)
    assert r["total_expenses"] == 2700
    assert r["net_cash_flow"] == 800
    assert r["fixed_expenses"] == 1350
    assert r["essential_expenses"] == 1850
    assert {f["category"] for f in r["above_reference_range"]} == {"Restaurants", "Other"}


def test_cash_flow_rejects_negative_and_missing():
    assert "error" in run(calculate_cash_flow, 3500, {"Rent": -5})
    assert "error" in run(calculate_cash_flow, None, DEMO_EXPENSES)


def test_deficit_status():
    assert run(calculate_cash_flow, 1000, {"Rent": 1200})["status"] == "deficit"


def test_demo_allocation_adds_up():
    a = run(plan_surplus_allocation, 800, [AllocDebt(**CARD)], 1000, 5000)
    assert a["total_debt_payment"] == 632 and a["savings_contribution"] == 168
    assert a["sum_check"] is True


def test_allocation_shortfall():
    a = run(plan_surplus_allocation, 200, [AllocDebt(**CARD)], 0, 5000)
    assert a["status"] == "shortfall"


def test_demo_debt_payoff():
    r = run(simulate_debt_payoff, [DebtInput(**CARD)], 632)
    assert r["avalanche"]["months"] == 15
    assert abs(r["avalanche"]["total_interest"] - 1187.26) < 0.01
    assert r["minimum_payments_only"]["months"] == 52
    assert r["interest_saved_vs_minimum_only"] > 3000


def test_multiple_debts_and_zero_interest():
    debts = [DebtInput(name="Card", balance=3000, apr=24, minimum_payment=90),
             DebtInput(name="Family loan", balance=1000, apr=0, minimum_payment=50)]
    r = run(simulate_debt_payoff, debts, 400)
    assert r["avalanche"]["payoff_order"][0]["name"] == "Card"
    assert r["snowball"]["payoff_order"][0]["name"] == "Family loan"
    assert r["avalanche"]["total_interest"] <= r["snowball"]["total_interest"]


def test_payment_below_minimum_is_infeasible():
    r = run(simulate_debt_payoff, [DebtInput(**CARD)], 100)
    assert r["avalanche"]["feasible"] is False


def test_savings_with_rollover():
    r = run(savings_goal_timeline, 1000, 5000, 168, 1850, extra_contribution_after_month=15,
                              extra_contribution_amount=632)
    assert r["months_to_goal"] == 17
    assert r["emergency_fund_targets"]["three_months"] == 5550


def test_compound_interest():
    r = run(compound_interest_scenarios, 100, annual_rates_pct=[0], years=[10])
    assert r["scenarios"][0]["future_value"] == 12000
