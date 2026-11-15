"""Golden and edge-case tests for build_financial_plan (the Supervisor's one-call plan)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from build_financial_plan import DebtInput, build_financial_plan  # noqa: E402
from plan_surplus_allocation import DebtInput as AllocDebt, plan_surplus_allocation  # noqa: E402


def run(fn, *a, **k):
    out = fn(*a, **k)
    return out.content if hasattr(out, "content") else out


EXP = {"Rent": 1200, "Groceries": 350, "Transportation": 150, "Restaurants": 300,
       "Shopping": 200, "Utilities": 150, "Entertainment": 100, "Other": 250}
CARD = [DebtInput(name="Credit card", balance=8000, apr=22, minimum_payment=240)]


def test_golden_plan():
    p = run(build_financial_plan, 3500, EXP, 1000, CARD, 5000)
    assert p["cash_flow"]["net_cash_flow"] == 800
    a = p["allocation"]
    assert (a["minimum_debt_payments"], a["extra_debt_payment"], a["savings_contribution"]) == (240, 392, 168)
    assert a["total_debt_payment"] == 632 and a["sum_check"] is True
    d = p["debt"]
    assert d["months_to_debt_free"] == 15 and d["interest_paid"] == 1187.26
    assert d["minimum_only_months"] == 52 and d["minimum_only_interest_paid"] == 4476.88
    assert d["interest_saved_vs_minimum_only"] == 3289.62
    s = p["savings"]
    assert s["goal_reached_month"] == 17 and s["three_month_essentials_target"] == 5550
    assert [m["month"] for m in p["milestones"]] == [15, 17]


def test_allocation_matches_policy_tool():
    for net_inc, debts, sav, tgt in [(3500, CARD, 1000, 5000), (3500, [], 1000, 5000),
                                     (3500, CARD, 6000, 5000), (2900, CARD, 0, 5000)]:
        p = run(build_financial_plan, net_inc, EXP, sav, debts, tgt)["allocation"]
        ref = run(plan_surplus_allocation, net_inc - 2700,
                  [AllocDebt(**d.model_dump()) for d in debts], sav, tgt)
        assert p["status"] == ref["status"]
        assert p["total_debt_payment"] == ref["total_debt_payment"]
        assert p["savings_contribution"] == ref["savings_contribution"]


def test_deficit_and_no_debt():
    p = run(build_financial_plan, 2500, EXP, 0, [], 5000)
    assert p["cash_flow"]["status"] == "deficit" and p["allocation"]["status"] == "deficit"
    assert p["savings"]["goal_reached_month"] is None and p["debt"] is None


def test_zero_interest_multiple_debts():
    debts = [DebtInput(name="Card", balance=3000, apr=24, minimum_payment=90),
             DebtInput(name="Family loan", balance=1000, apr=0, minimum_payment=50)]
    p = run(build_financial_plan, 3500, EXP, 1000, debts, 5000)
    assert p["debt"]["payoff_order"][0]["name"] == "Card"
    assert p["debt"]["feasible"] is True


def test_invalid_inputs():
    assert "error" in run(build_financial_plan, -1, EXP, 0)
    assert "error" in run(build_financial_plan, 3500, {"Rent": -1}, 0)
    assert "error" in run(build_financial_plan, 3500, EXP, 0, [DebtInput(name="x", balance=1, apr=150, minimum_payment=1)])
