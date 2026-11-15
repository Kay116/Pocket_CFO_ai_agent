"""Pocket CFO tool: month-by-month debt payoff simulation (avalanche vs. snowball).

Used by: Debt Management Agent.
"""
from typing import List

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from pydantic import BaseModel, Field

MAX_MONTHS = 600


class DebtInput(BaseModel):
    name: str = Field(description="Debt name, e.g. 'Credit card'")
    balance: float = Field(description="Current balance in dollars")
    apr: float = Field(description="Annual interest rate in percent, e.g. 22 for 22%")
    minimum_payment: float = Field(description="Required minimum monthly payment in dollars")


def _as_models(items, model):
    """watsonx passes nested objects as plain dicts at runtime; accept both."""
    return [i if isinstance(i, model) else model.model_validate(i) for i in (items or [])]


def _unique_names(debts: List[DebtInput]) -> List[DebtInput]:
    """Two debts called 'Visa' become 'Visa' and 'Visa (2)' so they are never merged."""
    used, out = set(), []
    for d in debts:
        base = d.name.strip() or "Debt"
        name, n = base, 1
        while name.lower() in used:
            n += 1
            name = f"{base} ({n})"
        used.add(name.lower())
        out.append(d.model_copy(update={"name": name}))
    return out


def _simulate(debts: List[DebtInput], payment: float, strategy: str, rollover: bool = True) -> dict:
    active = [d for d in debts if d.balance > 0.005]
    if not active:
        return {"feasible": True, "months": 0, "total_interest": 0.0, "payoff_order": []}
    mins = sum(d.minimum_payment for d in active)
    if payment + 1e-9 < mins:
        return {"feasible": False, "reason": f"Payment ${payment:,.2f} is below total minimums ${mins:,.2f}."}
    key = (lambda d: (-d.apr, d.balance)) if strategy == "avalanche" else (lambda d: (d.balance, -d.apr))
    order = sorted(active, key=key)
    bal = {d.name: d.balance for d in order}
    paid_off, interest, month = {}, 0.0, 0
    while any(b > 0.005 for b in bal.values()):
        month += 1
        if month > MAX_MONTHS:
            return {"feasible": False, "reason": "Payments do not outpace interest; the debt would not be repaid."}
        for d in order:
            if bal[d.name] > 0.005:
                i = bal[d.name] * d.apr / 1200
                bal[d.name] += i
                interest += i
        budget = payment if rollover else sum(d.minimum_payment for d in order if bal[d.name] > 0.005)
        for d in order:
            if bal[d.name] > 0.005:
                p = min(d.minimum_payment, bal[d.name], budget)
                bal[d.name] -= p
                budget -= p
        if rollover:
            for d in order:
                if budget <= 0.005:
                    break
                p = min(budget, bal[d.name])
                bal[d.name] -= p
                budget -= p
        for d in order:
            if bal[d.name] <= 0.005 and d.name not in paid_off:
                paid_off[d.name] = month
                bal[d.name] = 0.0
    return {"feasible": True, "months": month, "total_interest": round(interest, 2),
            "payoff_order": [{"name": n, "paid_off_month": m} for n, m in sorted(paid_off.items(), key=lambda kv: kv[1])]}


@tool(name="simulate_debt_payoff")
def simulate_debt_payoff(debts: List[DebtInput], monthly_payment: float) -> dict:
    """Simulate paying off debts month by month and compare avalanche, snowball and minimum-only.

    Always call this tool for payoff timelines and interest figures instead of estimating.
    Results are estimates that assume a fixed APR, no new charges, and the same payment every month.

    Args:
        debts (List[DebtInput]): Every debt with balance, APR (percent) and minimum payment. Ask the user for any missing value.
        monthly_payment (float): Total amount paid toward all debts each month (minimums plus extra).

    Returns:
        dict: total debt, total minimums, avalanche / snowball / minimum-only results (months to
        debt-free, total interest, payoff order), the recommended strategy (always avalanche, the
        same plan build_financial_plan uses), the interest saved versus paying minimums only, and
        how much extra interest snowball would cost (snowball_is_low_cost_alternative is true when
        it costs under $50 more, so it can be offered for motivation).
    """
    try:
        debts = _unique_names(_as_models(debts, DebtInput))
    except Exception:
        return {"error": "Each debt needs a name, balance, apr and minimum_payment."}
    if not debts:
        return {"error": "No debts provided."}
    for d in debts:
        if d.balance < 0 or d.apr < 0 or d.apr > 100 or d.minimum_payment < 0:
            return {"error": f"Debt '{d.name}' has an invalid balance, APR or minimum payment."}
    if monthly_payment is None or monthly_payment <= 0:
        return {"error": "monthly_payment must be greater than zero."}

    mins = sum(d.minimum_payment for d in debts if d.balance > 0)
    avalanche = _simulate(debts, monthly_payment, "avalanche")
    snowball = _simulate(debts, monthly_payment, "snowball")
    minimum_only = _simulate(debts, mins, "avalanche", rollover=False)

    # The plan is always avalanche, the same strategy build_financial_plan and monthly_check_in use,
    # so every agent reports the same timeline. Snowball is reported as an alternative with its cost.
    recommended = "avalanche"
    snowball_extra = None
    if avalanche.get("feasible") and snowball.get("feasible"):
        snowball_extra = round(snowball["total_interest"] - avalanche["total_interest"], 2)
    saved = None
    if avalanche.get("feasible") and minimum_only.get("feasible"):
        saved = round(minimum_only["total_interest"] - avalanche["total_interest"], 2)

    return {
        "total_debt": round(sum(d.balance for d in debts), 2),
        "total_minimum_payments": round(mins, 2),
        "monthly_payment": round(monthly_payment, 2),
        "avalanche": avalanche,
        "snowball": snowball,
        "minimum_payments_only": minimum_only,
        "recommended_strategy": recommended,
        "interest_saved_vs_minimum_only": saved,
        "snowball_extra_interest_vs_avalanche": snowball_extra,
        "snowball_is_low_cost_alternative": snowball_extra is not None and len(debts) > 1 and snowball_extra < 50,
        "assumptions": "Fixed APR, interest compounded monthly, no new charges, same payment each month.",
    }
