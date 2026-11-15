"""Phase 4 tests: monthly check-in (progress since the last snapshot and versus its plan).

Uses the in-memory Cloudant fake from test_data_tools and a controllable clock, so the
check-in can be tested months apart without waiting.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pocket_cfo_data_tools as dt  # noqa: E402
from simulate_debt_payoff import DebtInput as SimDebt, simulate_debt_payoff  # noqa: E402
from test_data_tools import ALICE, BOB, DEMO, cloud, ctx, run  # noqa: E402,F401  (cloud is a fixture)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
CARD = "Credit card"


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(dt, "_utcnow", lambda: now["t"])

    def advance(days):
        now["t"] = now["t"] + timedelta(days=days)
    return advance


def card(balance):
    return [dt.DebtInput(name=CARD, balance=balance, apr=22, minimum_payment=240)]


def baseline(clock_):
    """Golden demo user: profile + plan snapshot at T0."""
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, **DEMO)
    snap = run(dt.save_analysis_snapshot, ctx(ALICE), plan_summary="Avalanche: $632 to card, $168 to savings")
    assert snap["snapshot"]["allocation"]["total_debt_payment"] == 632
    clock_(1)  # profile edits after the snapshot are strictly later
    return snap["snapshot"]


def update(debt, savings, **extra):
    r = run(dt.save_financial_profile, ctx(ALICE), consent_to_store=False,
            debts=card(debt) if debt is not None else None, savings_balance=savings, **extra)
    assert r.get("saved"), r


# ----------------------------------------------------------------- readiness
def test_no_profile(cloud, clock):
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["ready"] is False and r["reason"] == "no_profile"


def test_no_baseline_snapshot(cloud, clock):
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, **DEMO)
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["ready"] is False and r["reason"] == "no_baseline"


def test_snapshot_stores_debt_detail(cloud, clock):
    s = baseline(clock)
    assert s["debts"] == [{"name": CARD, "balance": 8000, "apr": 22, "minimum_payment": 240}]


# ----------------------------------------------------------------- golden month 1
def test_golden_first_month_on_track(cloud, clock):
    baseline(clock)
    clock(30)
    update(7514.67, 1168)
    r = run(dt.monthly_check_in, ctx(ALICE))
    since, plan = r["since_last_snapshot"], r["plan_comparison"]
    assert r["ready"] and r["check_in_due"] and r["balances_updated_since_snapshot"]
    assert since["months_elapsed"] == 1
    assert since["debt_paid_down"] == 485.33 and since["total_debt_change"] == -485.33
    assert since["savings_added"] == 168
    assert since["debts"] == [{"name": CARD, "previous_balance": 8000, "current_balance": 7514.67, "paid_down": 485.33}]
    assert plan["planned_monthly_debt_payment"] == 632 and plan["planned_monthly_savings"] == 168
    assert plan["expected_total_debt"] == 7514.67 and plan["expected_savings_balance"] == 1168
    assert plan["overall_status"] == "on_track"
    assert r["replan_recommended"] is False


def test_behind_plan_when_only_minimum_paid(cloud, clock):
    baseline(clock)
    clock(30)
    update(7906.67, 1000)  # 8000 + 1 month interest - $240 minimum, nothing saved
    r = run(dt.monthly_check_in, ctx(ALICE))
    plan = r["plan_comparison"]
    assert plan["debt_status"] == "behind" and plan["debt_vs_plan"] == -392.0
    assert plan["savings_status"] == "behind" and plan["savings_vs_plan"] == -168
    assert plan["overall_status"] == "behind" and r["replan_recommended"] is True


def test_ahead_of_plan(cloud, clock):
    baseline(clock)
    clock(30)
    update(7300, 1168)
    plan = run(dt.monthly_check_in, ctx(ALICE))["plan_comparison"]
    assert plan["debt_status"] == "ahead" and plan["debt_vs_plan"] == 214.67
    assert plan["savings_status"] == "on_track" and plan["overall_status"] == "ahead"


def test_small_difference_counts_as_on_track(cloud, clock):
    baseline(clock)
    clock(30)
    update(7530, 1160)  # $15 more debt and $8 less saved than planned
    assert run(dt.monthly_check_in, ctx(ALICE))["plan_comparison"]["overall_status"] == "on_track"


# ----------------------------------------------------------------- golden long horizon
@pytest.mark.parametrize("days,months,exp_debt,exp_savings", [
    (15 * 30.44, 15, 0, 3520),     # debt-free in month 15
    (16 * 30.44, 16, 0, 4320),     # emergency fund not yet reached
    (17 * 30.44, 17, 0, 5120),     # emergency fund ($5,000) reached in month 17
])
def test_golden_plan_projection(cloud, clock, days, months, exp_debt, exp_savings):
    baseline(clock)
    clock(days - 1)
    update(0, exp_savings)
    plan = run(dt.monthly_check_in, ctx(ALICE))["plan_comparison"]
    assert plan["months_compared"] == months
    assert plan["expected_total_debt"] == exp_debt and plan["expected_savings_balance"] == exp_savings
    assert plan["overall_status"] == "on_track"


def test_projection_matches_simulate_debt_payoff():
    cases = [
        ([dict(name=CARD, balance=8000, apr=22, minimum_payment=240)], 632),
        ([dict(name="Card", balance=3000, apr=24, minimum_payment=90),
          dict(name="Family loan", balance=1000, apr=0, minimum_payment=50)], 400),
        ([dict(name="Car", balance=9000, apr=4, minimum_payment=200)], 260),
    ]
    for debts, payment in cases:
        sim = run(simulate_debt_payoff, [SimDebt(**d) for d in debts], payment)["avalanche"]
        mine = dt._project_plan(debts, payment, 0, 0, sim["months"])
        assert mine["expected_debt_free_month"] == sim["months"], debts
        before = dt._project_plan(debts, payment, 0, 0, sim["months"] - 1)
        assert before["expected_debt_free_month"] is None and before["expected_total_debt"] > 0


# ----------------------------------------------------------------- edge cases
def test_less_than_a_month(cloud, clock):
    baseline(clock)
    clock(10)
    update(7800, 1050)
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["check_in_due"] is False and r["plan_comparison"] is None
    assert r["plan_comparison_unavailable_reason"] == "less_than_a_month_since_snapshot"
    assert r["since_last_snapshot"]["debt_paid_down"] == 200


def test_balances_not_updated_is_flagged(cloud, clock):
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=True, **DEMO)
    clock(1)
    run(dt.save_analysis_snapshot, ctx(ALICE), plan_summary="plan")
    clock(30)
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["balances_updated_since_snapshot"] is False


def test_old_snapshot_without_debt_detail(cloud, clock):
    baseline(clock)
    snap = next(v for k, v in cloud.dbs["pocket_cfo"].items() if ":snap-" in k)
    del snap["debts"]  # snapshots saved before phase 4
    clock(30)
    update(7514.67, 1168)
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["plan_comparison"] is None
    assert r["plan_comparison_unavailable_reason"] == "snapshot_has_no_debt_detail"
    assert r["since_last_snapshot"]["debt_paid_down"] == 485.33


def test_new_debt_and_income_change_recommend_replan(cloud, clock):
    baseline(clock)
    clock(30)
    run(dt.save_financial_profile, ctx(ALICE), consent_to_store=False, monthly_income=3300,
        debts=card(7514.67) + [dt.DebtInput(name="Store card", balance=500, apr=28, minimum_payment=25)])
    r = run(dt.monthly_check_in, ctx(ALICE))
    since = r["since_last_snapshot"]
    assert since["monthly_income_change"] == -200 and since["net_cash_flow_change"] == -200
    assert since["total_debt_change"] == 14.67 and since["debt_paid_down"] == 0
    assert {"name": "Store card", "previous_balance": None, "current_balance": 500, "paid_down": None} in since["debts"]
    assert r["replan_recommended"] is True


def test_uses_latest_snapshot_as_baseline(cloud, clock):
    baseline(clock)
    clock(30)
    update(7514.67, 1168)
    run(dt.save_analysis_snapshot, ctx(ALICE), plan_summary="Check-in 1")
    clock(31)
    update(7022.43, 1336)
    since = run(dt.monthly_check_in, ctx(ALICE))["since_last_snapshot"]
    assert since["debt_paid_down"] == 492.24 and since["savings_added"] == 168


def test_check_in_is_isolated_and_read_only(cloud, clock):
    baseline(clock)
    clock(30)
    update(7514.67, 1168)
    assert run(dt.monthly_check_in, ctx(BOB))["reason"] == "no_profile"
    before = {k: dict(v) for k, v in cloud.dbs["pocket_cfo"].items()}
    run(dt.monthly_check_in, ctx(ALICE))
    assert cloud.dbs["pocket_cfo"] == before
    assert dt.monthly_check_in.__tool_spec__.permission == dt.ToolPermission.READ_ONLY


def test_storage_failure_is_safe(cloud, clock):
    cloud.fail = True
    r = run(dt.monthly_check_in, ctx(ALICE))
    assert r["error"].startswith("Pocket CFO storage is temporarily unavailable")


# ----------------------------------------------------------------- agent wiring
def _agents():
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (ROOT / "agents").glob("*.yaml")}


def test_agent_yamls_are_valid_specs():
    from ibm_watsonx_orchestrate.agent_builder.agents.types import AgentSpec
    for name, spec in _agents().items():
        AgentSpec(**spec)  # raises on an invalid field


def test_every_agent_tool_exists():
    import importlib
    defined = set()
    for p in (ROOT / "tools").glob("*.py"):
        mod = importlib.import_module(p.stem)
        defined |= {v.__tool_spec__.name for v in vars(mod).values() if hasattr(v, "__tool_spec__")}
    for name, spec in _agents().items():
        missing = set(spec.get("tools") or []) - defined
        assert not missing, f"{name} references unknown tools {missing}"


def test_supervisor_has_check_in_flow():
    sup = _agents()["pocket_cfo_supervisor"]
    assert "monthly_check_in" in sup["tools"]
    assert "MONTHLY CHECK-IN" in sup["instructions"]
    assert "wxo_email_id" in sup["context_variables"]
    titles = [p["title"] for p in sup["starter_prompts"]["prompts"]]
    assert "Monthly check-in" in titles
