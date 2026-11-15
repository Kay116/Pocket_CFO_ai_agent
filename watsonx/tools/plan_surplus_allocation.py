"""Pocket CFO tool: split monthly surplus between debt and savings.

This is the Supervisor's conflict-resolution policy between the Debt Management
and Savings agents, implemented deterministically so the split always adds up.
Used by: Pocket CFO Supervisor, Financial Coach Agent.
"""
import math
from typing import List, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from pydantic import BaseModel, Field

HIGH_INTEREST_APR = 8.0


class DebtInput(BaseModel):
    name: str = Field(description="Debt name, e.g. 'Credit card'")
    balance: float = Field(description="Current balance in dollars")
    apr: float = Field(description="Annual interest rate in percent, e.g. 22 for 22%")
    minimum_payment: float = Field(description="Required minimum monthly payment in dollars")


def _as_models(items, model):
    """watsonx passes nested objects as plain dicts at runtime; accept both."""
    return [i if isinstance(i, model) else model.model_validate(i) for i in (items or [])]


def _all_finite(*values) -> bool:
    """Reject NaN / infinity, which would otherwise flow through the math silently."""
    return all(v is None or (isinstance(v, (int, float)) and math.isfinite(v)) for v in values)


@tool(name="plan_surplus_allocation")
def plan_surplus_allocation(net_cash_flow: float, debts: List[DebtInput], savings_balance: float,
                            emergency_fund_target: Optional[float] = None) -> dict:
    """Split the monthly surplus between debt payments and savings using Pocket CFO's policy.

    Policy: (1) minimum debt payments first; (2) if high-interest debt (APR >= 8%) exists and the
    emergency fund is incomplete, 70% of the remainder goes to extra debt payments and 30% to savings;
    (3) low-interest debt only: 70% savings / 30% debt; (4) emergency fund complete: remainder to debt.
    The returned amounts always add up exactly to net_cash_flow.

    Args:
        net_cash_flow (float): Monthly income minus monthly expenses (from calculate_cash_flow).
        debts (List[DebtInput]): The user's debts. Pass an empty list if the user has no debt.
        savings_balance (float): Current emergency savings balance in dollars.
        emergency_fund_target (float, optional): The user's emergency fund goal in dollars.

    Returns:
        dict: status (ok, shortfall or deficit), minimum_debt_payments, extra_debt_payment,
        total_debt_payment, savings_contribution, unallocated, a check that the parts sum to
        the surplus, and the rationale.
    """
    try:
        debts = _as_models(debts, DebtInput)
    except Exception:
        return {"error": "Each debt needs a name, balance, apr and minimum_payment."}
    if not _all_finite(*([x for d in debts for x in (d.balance, d.apr, d.minimum_payment)] + [net_cash_flow, savings_balance, emergency_fund_target])):
        return {"error": "All amounts must be real numbers (no NaN or infinity)."}
    for d in debts:
        if d.balance < 0 or d.apr < 0 or d.apr > 100 or d.minimum_payment < 0:
            return {"error": f"Debt '{d.name}' has an invalid balance, APR or minimum payment."}
    if savings_balance is None or savings_balance < 0:
        return {"error": "savings_balance is required and cannot be negative."}

    active = [d for d in debts if d.balance > 0]
    mins = round(sum(d.minimum_payment for d in active), 2)
    high = any(d.apr >= HIGH_INTEREST_APR for d in active)

    def result(status, min_pay, extra, save, unalloc, notes):
        return {"status": status, "available": round(net_cash_flow, 2),
                "minimum_debt_payments": round(min_pay, 2), "extra_debt_payment": round(extra, 2),
                "total_debt_payment": round(min_pay + extra, 2), "savings_contribution": round(save, 2),
                "unallocated": round(unalloc, 2),
                "sum_check": round(min_pay + extra + save + unalloc, 2) == round(net_cash_flow, 2),
                "required_minimum_payments": mins, "minimum_payments_covered": status == "ok",
                "monthly_gap": round(max(mins - net_cash_flow, 0.0), 2) if status != "ok" else 0.0,
                "rationale": notes}

    if net_cash_flow <= 0:
        return result("deficit", 0, 0, 0, net_cash_flow,
                      ["Expenses meet or exceed income, so there is no surplus. Reducing spending comes first."])
    if net_cash_flow < mins:
        return result("shortfall", net_cash_flow, 0, 0, 0,
                      [f"Surplus ${net_cash_flow:,.2f} does not cover minimum payments of ${mins:,.2f}."])

    remaining = net_cash_flow - mins
    fund_done = emergency_fund_target is not None and savings_balance >= emergency_fund_target
    notes = [f"Minimum debt payments of ${mins:,.2f} are covered first."] if mins else []
    if not fund_done and high:
        share = 0.7
        notes.append("High-interest debt present: 70% of the remaining surplus to extra debt payments, 30% to the emergency fund.")
    elif not fund_done and active:
        share = 0.3
        notes.append("Only low-interest debt: 70% of the remaining surplus builds the emergency fund first.")
    elif not fund_done:
        share = 0.0
        notes.append("No debt: the full surplus builds the emergency fund.")
    elif active:
        share = 1.0
        notes.append("Emergency fund complete: the remaining surplus accelerates debt payoff.")
    else:
        notes.append("Emergency fund complete and no debt: surplus is free for other goals or long-term investing.")
        return result("ok", 0, 0, 0, remaining, notes)

    extra = float(round(remaining * share))
    return result("ok", mins, extra, remaining - extra, 0, notes)
