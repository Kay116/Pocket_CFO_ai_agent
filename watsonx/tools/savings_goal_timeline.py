"""Pocket CFO tool: emergency fund targets and savings-goal timeline.

Used by: Savings Agent.
"""
import math
from typing import Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool

MAX_MONTHS = 600


def _all_finite(*values) -> bool:
    """Reject NaN / infinity, which would otherwise flow through the math silently."""
    return all(v is None or (isinstance(v, (int, float)) and math.isfinite(v)) for v in values)


@tool(name="savings_goal_timeline")
def savings_goal_timeline(current_savings: float, goal_amount: float, monthly_contribution: float,
                          essential_monthly_expenses: Optional[float] = None,
                          annual_interest_rate: float = 0.0,
                          extra_contribution_after_month: Optional[int] = None,
                          extra_contribution_amount: float = 0.0) -> dict:
    """Estimate how many months it takes to reach a savings goal, plus emergency fund targets.

    Use extra_contribution_after_month / extra_contribution_amount to model money that rolls into
    savings later (for example the debt payment once a debt is paid off).

    Args:
        current_savings (float): Amount already saved, in dollars.
        goal_amount (float): Target amount, in dollars.
        monthly_contribution (float): Amount saved every month, in dollars.
        essential_monthly_expenses (float, optional): Essential monthly costs (housing, utilities, groceries, transport) used to suggest 3- and 6-month emergency fund targets.
        annual_interest_rate (float): Savings account interest in percent per year (default 0).
        extra_contribution_after_month (int, optional): Month after which an additional amount is saved each month.
        extra_contribution_amount (float): The additional monthly amount saved after that month.

    Returns:
        dict: months to goal (or null if unreachable), remaining amount, a short balance path,
        and suggested emergency fund targets when essential expenses are given.
    """
    if not _all_finite(*([current_savings, goal_amount, monthly_contribution, essential_monthly_expenses, annual_interest_rate, extra_contribution_amount])):
        return {"error": "All amounts must be real numbers (no NaN or infinity)."}
    if current_savings is None or goal_amount is None or monthly_contribution is None:
        return {"error": "current_savings, goal_amount and monthly_contribution are required."}
    if min(current_savings, goal_amount, monthly_contribution, extra_contribution_amount) < 0:
        return {"error": "Amounts cannot be negative."}

    r = annual_interest_rate / 1200
    bal, month, path = current_savings, 0, []
    reached = current_savings >= goal_amount
    while not reached:
        month += 1
        if month > MAX_MONTHS:
            month = None
            break
        contrib = monthly_contribution
        if extra_contribution_after_month is not None and month > extra_contribution_after_month:
            contrib += extra_contribution_amount
        if contrib <= 0 and r == 0:
            month = None
            break
        bal = bal * (1 + r) + contrib
        if month <= 6 or month % 6 == 0:
            path.append({"month": month, "balance": round(bal, 2)})
        reached = bal >= goal_amount - 0.005
    if reached and month:
        path.append({"month": month, "balance": round(bal, 2)})

    out = {
        "current_savings": round(current_savings, 2),
        "goal_amount": round(goal_amount, 2),
        "remaining": round(max(goal_amount - current_savings, 0), 2),
        "progress_pct": round(min(current_savings / goal_amount * 100, 100), 1) if goal_amount else 100.0,
        "months_to_goal": 0 if current_savings >= goal_amount else month,
        "balance_path": path,
    }
    if essential_monthly_expenses is not None:
        out["emergency_fund_targets"] = {
            "essential_monthly_expenses": round(essential_monthly_expenses, 2),
            "three_months": round(essential_monthly_expenses * 3, 2),
            "six_months": round(essential_monthly_expenses * 6, 2),
        }
    return out
