"""Regression tests for the second review batch (2026-11-15).

8.  No emergency fund goal: suggest essentials-based goals instead of silently skipping savings.
9.  savings_balance and the emergency fund goal's current_amount cannot drift apart.
12. Deletion needs a server-issued code the user types back.
14. Partial bulk-delete failures are reported; NaN/infinity rejected by every calculation tool.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pocket_cfo_data_tools as dt  # noqa: E402
from build_financial_plan import build_financial_plan  # noqa: E402
from compound_interest_scenarios import compound_interest_scenarios  # noqa: E402
from plan_surplus_allocation import plan_surplus_allocation  # noqa: E402
from savings_goal_timeline import savings_goal_timeline  # noqa: E402
from simulate_debt_payoff import simulate_debt_payoff  # noqa: E402
from test_data_tools import ALICE, BOB, DEMO, cloud, ctx, run  # noqa: E402,F401

EXP = {"Rent": 1200, "Groceries": 400, "Utilities": 150, "Transportation": 200,
       "Restaurants": 250, "Shopping": 200, "Entertainment": 100, "Other": 200}
CARD = {"name": "Credit card", "balance": 8000, "apr": 22, "minimum_payment": 240}
NAN, INF = float("nan"), float("inf")


# ----------------------------------------------------------------- 8. missing goal
def test_missing_goal_suggests_essentials_targets():
    s = run(build_financial_plan, 3500, EXP, 1000, [CARD], None)["savings"]
    assert s["goal"] is None and s["goal_missing"] is True
    assert s["suggested_goal_three_months_essentials"] == 5850   # essentials 1950 x 3
    assert s["suggested_goal_six_months_essentials"] == 11700
    assert "goal_reached_month" not in s


def test_missing_goal_without_essentials_suggests_nothing():
    s = run(build_financial_plan, 3000, {"Restaurants": 300}, 0, [], None)["savings"]
    assert s["suggested_goal_three_months_essentials"] is None


def test_supervisor_asks_for_goal():
    p = Path(__file__).resolve().parents[1] / "agents" / "pocket_cfo_supervisor.yaml"
    text = yaml.safe_load(p.read_text(encoding="utf-8"))["instructions"]
    assert "suggested_goal_three_months_essentials" in text and "Never choose a goal" in text


# ----------------------------------------------------------------- 9. one savings number
def _ef(profile):
    return next(g for g in profile["savings_goals"] if g["is_emergency_fund"])


def test_goal_mirrors_savings_balance(cloud):
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, **DEMO)
    p = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=False, savings_balance=1500)["profile"]
    assert p["savings_balance"] == 1500 and _ef(p)["current_amount"] == 1500


def test_savings_balance_wins_over_goal_amount(cloud):
    p = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, savings_balance=900,
            savings_goals=[dt.GoalInput(name="EF", target_amount=5000, current_amount=2000, is_emergency_fund=True)])["profile"]
    assert p["savings_balance"] == 900 and _ef(p)["current_amount"] == 900


def test_goal_amount_used_when_no_balance_saved(cloud):
    p = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True,
            savings_goals=[dt.GoalInput(name="EF", target_amount=5000, current_amount=700, is_emergency_fund=True)])["profile"]
    assert p["savings_balance"] == 700


def test_only_one_emergency_fund(cloud):
    r = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, savings_goals=[
        dt.GoalInput(name="A", target_amount=1, is_emergency_fund=True),
        dt.GoalInput(name="B", target_amount=1, is_emergency_fund=True)])
    assert "error" in r and cloud.dbs["pocket_cfo"] == {}


# ----------------------------------------------------------------- 12. two-step deletion
def _saved(cloud_):
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, **DEMO)
    run(dt.save_analysis_snapshot, ctx(ALICE), plan_summary="x")


def test_delete_without_request_deletes_nothing(cloud):
    _saved(cloud)
    r = run(dt.delete_my_data, ctx(ALICE), confirmation_code="ABC123")
    assert r["error"] == "invalid_or_expired_code" and r["deleted"] == 0
    assert run(dt.load_financial_profile, ctx(ALICE))["found"]


def test_wrong_code_deletes_nothing(cloud):
    _saved(cloud)
    code = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    assert len(code) == 6 and not set(code) & set("01IO")
    assert run(dt.delete_my_data, ctx(ALICE), confirmation_code="ZZZZZZ")["deleted"] == 0
    assert run(dt.load_financial_profile, ctx(ALICE))["found"]


def test_expired_code_deletes_nothing(cloud, monkeypatch):
    _saved(cloud)
    code = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    later = datetime.now(timezone.utc) + timedelta(minutes=dt.DELETE_CODE_MINUTES + 1)
    monkeypatch.setattr(dt, "_utcnow", lambda: later)
    assert run(dt.delete_my_data, ctx(ALICE), confirmation_code=code)["error"] == "invalid_or_expired_code"
    assert run(dt.load_financial_profile, ctx(ALICE))["found"]


def test_new_request_replaces_old_code(cloud):
    _saved(cloud)
    old = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    new = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    if old != new:
        assert run(dt.delete_my_data, ctx(ALICE), confirmation_code=old)["deleted"] == 0
    assert run(dt.delete_my_data, ctx(ALICE), confirmation_code=f"  {new.lower()} ")["deleted"] == 2
    assert cloud.dbs["pocket_cfo"] == {}  # the request document is removed too


def test_one_users_code_cannot_delete_another_user(cloud):
    _saved(cloud)
    code = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    assert run(dt.delete_my_data, ctx(BOB), confirmation_code=code)["deleted"] == 0
    assert run(dt.load_financial_profile, ctx(ALICE))["found"]


def test_partial_bulk_delete_is_reported(cloud, monkeypatch):
    _saved(cloud)
    code = run(dt.delete_my_data, ctx(ALICE))["confirmation_code"]
    real = cloud.post_bulk_docs

    def flaky(db, bulk_docs):
        real(db, bulk_docs)
        snap = next(d for d in bulk_docs.docs if ":snap-" in d.id)
        return type("R", (), {"get_result": lambda self: [{"id": snap.id, "error": "conflict"}]})()
    monkeypatch.setattr(cloud, "post_bulk_docs", flaky)
    r = run(dt.delete_my_data, ctx(ALICE), confirmation_code=code)
    assert r["failed"] == 1 and r["deleted"] == 1 and "error" in r


def test_delete_request_not_in_export_or_history(cloud):
    _saved(cloud)
    run(dt.delete_my_data, ctx(ALICE))
    e = run(dt.export_my_data, ctx(ALICE))
    assert len(e["snapshots"]) == 1 and e["profile"]["type"] == "profile"
    assert run(dt.get_progress_history, ctx(ALICE))["count"] == 1


# ----------------------------------------------------------------- 14. NaN / infinity
@pytest.mark.parametrize("call", [
    lambda: run(build_financial_plan, NAN, EXP, 1000, [CARD], 5000),
    lambda: run(build_financial_plan, 3500, dict(EXP, Rent=INF), 1000, [CARD], 5000),
    lambda: run(build_financial_plan, 3500, EXP, 1000, [dict(CARD, apr=NAN)], 5000),
    lambda: run(simulate_debt_payoff, [CARD], NAN),
    lambda: run(simulate_debt_payoff, [dict(CARD, balance=INF)], 600),
    lambda: run(plan_surplus_allocation, NAN, [CARD], 1000, 5000),
    lambda: run(savings_goal_timeline, 1000, INF, 100),
    lambda: run(compound_interest_scenarios, 100, 0.0, [NAN]),
])
def test_non_finite_numbers_rejected(call):
    assert "error" in call()


def test_allocation_apr_bound():
    assert "error" in run(plan_surplus_allocation, 800, [dict(CARD, apr=150)], 1000, 5000)


# ----------------------------------------------------------------- consent wording (live test 2026-11-15)
def test_user_request_to_save_counts_as_consent():
    p = Path(__file__).resolve().parents[1] / "agents" / "pocket_cfo_supervisor.yaml"
    text = yaml.safe_load(p.read_text(encoding="utf-8"))["instructions"]
    assert "that IS consent. Do not ask again" in text
    assert "Do not ask again if they already" in dt.save_financial_profile.__tool_spec__.description


# ----------------------------------------------------------------- budget numbers (live test 2026-11-15)
# The Budget Agent said "about $830 left" after cuts while the cash flow was $900: cuts can only raise
# what is left. The proposed budget is now computed by the tool.
from calculate_cash_flow import calculate_cash_flow  # noqa: E402

LIVE = {"Rent": 1000, "Groceries": 300, "Utilities": 100, "Insurance": 200}


def test_proposed_budget_from_live_scenario():
    r = run(calculate_cash_flow, 2500, LIVE, proposed_limits={"Groceries": 250, "Utilities": 80})
    assert r["net_cash_flow"] == 900
    p = r["proposed_budget"]
    assert p["changes"] == [
        {"category": "Groceries", "current": 300, "proposed": 250, "difference": -50},
        {"category": "Utilities", "current": 100, "proposed": 80, "difference": -20}]
    assert p["total_monthly_savings"] == 70 and p["proposed_total_expenses"] == 1530
    assert p["proposed_net_cash_flow"] == 970 and p["current_net_cash_flow"] == 900
    assert p["proposed_status"] == "surplus"


def test_proposed_limits_must_use_existing_names():
    r = run(calculate_cash_flow, 2500, LIVE, proposed_limits={"Food": 250})
    assert "error" in r and "Groceries" in r["error"]


@pytest.mark.parametrize("bad", [-1, NAN, INF])
def test_proposed_limits_validated(bad):
    assert "error" in run(calculate_cash_flow, 2500, LIVE, proposed_limits={"Groceries": bad})


def test_no_proposal_means_no_proposed_budget():
    assert run(calculate_cash_flow, 2500, LIVE)["proposed_budget"] is None


def test_budget_agent_uses_tool_for_remaining_money():
    p = Path(__file__).resolve().parents[1] / "agents" / "budget_agent.yaml"
    text = yaml.safe_load(p.read_text(encoding="utf-8"))["instructions"]
    assert "proposed_limits" in text and "proposed_net_cash_flow" in text
    assert "double-check" not in text  # no arithmetic left to the model
