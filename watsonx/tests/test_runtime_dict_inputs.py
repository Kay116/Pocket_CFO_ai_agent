"""watsonx passes nested objects (debts, goals) to tools as plain dicts at runtime.
Found in live testing 2026-11-15: AttributeError 'dict' object has no attribute 'balance'.
These tests call every tool the way the platform does."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_financial_plan import build_financial_plan  # noqa: E402
from plan_surplus_allocation import plan_surplus_allocation  # noqa: E402
from simulate_debt_payoff import simulate_debt_payoff  # noqa: E402
import pocket_cfo_data_tools as dt  # noqa: E402
from test_data_tools import FakeCloudant, SETTINGS, ctx  # noqa: E402


def run(fn, *a, **k):
    out = fn(*a, **k)
    return out.content if hasattr(out, "content") else out


CARD = {"name": "Credit card", "balance": 8000, "apr": 22, "minimum_payment": 240}
EXP = {"Rent": 1200, "Groceries": 350, "Transportation": 150, "Restaurants": 300,
       "Shopping": 200, "Utilities": 150, "Entertainment": 100, "Other": 250}


def test_build_plan_with_dicts():
    p = run(build_financial_plan, monthly_income=3500, expenses=EXP, savings_balance=1000,
            debts=[CARD], emergency_fund_target=5000)
    assert p["debt"]["months_to_debt_free"] == 15
    assert p["savings"]["goal_reached_month"] == 17


def test_allocation_with_dicts():
    a = run(plan_surplus_allocation, net_cash_flow=800, debts=[CARD], savings_balance=1000,
            emergency_fund_target=5000)
    assert a["total_debt_payment"] == 632 and a["savings_contribution"] == 168


def test_simulate_with_dicts():
    r = run(simulate_debt_payoff, debts=[CARD], monthly_payment=632)
    assert r["avalanche"]["months"] == 15


@pytest.mark.parametrize("fn,kwargs", [
    (build_financial_plan, dict(monthly_income=3500, expenses=EXP, savings_balance=0, debts=[{"name": "x"}])),
    (plan_surplus_allocation, dict(net_cash_flow=800, debts=[{"name": "x"}], savings_balance=0)),
    (simulate_debt_payoff, dict(debts=[{"name": "x"}], monthly_payment=100)),
])
def test_incomplete_debt_dict_returns_error(fn, kwargs):
    assert "error" in run(fn, **kwargs)


def test_save_profile_with_dicts(monkeypatch):
    fake = FakeCloudant()
    monkeypatch.setattr(dt.connections, "key_value", lambda app_id: dict(SETTINGS))
    monkeypatch.setattr(dt, "_make_client", lambda s: fake)
    r = run(dt.save_financial_profile, ctx("alice@example.com"), consent_to_store=True,
            monthly_income=3500, savings_balance=1000, expenses=EXP, debts=[CARD],
            savings_goals=[{"name": "Emergency fund", "target_amount": 5000, "current_amount": 1000,
                            "is_emergency_fund": True}])
    assert r["saved"] is True and r["profile"]["debts"][0]["balance"] == 8000
    snap = run(dt.save_analysis_snapshot, ctx("alice@example.com"), plan_summary="ok")["snapshot"]
    assert snap["allocation"]["total_debt_payment"] == 632
