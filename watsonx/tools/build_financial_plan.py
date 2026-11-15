"""Pocket CFO tool: the complete deterministic plan in ONE call.

Live testing (2026-11-14) showed the Supervisor model sometimes skips delegation and
invents timelines ("debt-free in ~14 months", "save ~$1,200"). This tool returns every
number a full plan needs (cash flow, allocation, debt payoff, savings timeline with the
debt payment rolling into savings, milestones) so the agents only have to explain them.

Used by: Pocket CFO Supervisor (and its numbers are passed to the specialists / Coach).
"""
import re
from typing import Dict, List, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from pydantic import BaseModel, Field

HIGH_INTEREST_APR = 8.0
MAX_MONTHS = 600
# --- category classification (keep identical in build_financial_plan.py; a test checks this) ---
# (group, keywords, fixed, essential, reference ceiling as % of income). The first group with a
# matching word wins, so the order matters ("Car insurance" -> insurance, "Food delivery" -> dining).
CATEGORY_RULES = [
    ("debt", {"loan", "debt", "card", "credit", "bnpl", "klarna", "afterpay", "affirm"}, True, True, None),
    ("insurance", {"insurance", "premium"}, True, True, None),
    ("restaurants", {"restaurant", "restaurants", "dining", "dine", "takeout", "eating", "delivery",
                     "doordash", "ubereats", "grubhub", "coffee", "bar", "bars", "fast", "fastfood", "lunch"},
     False, False, 6),
    ("housing", {"rent", "housing", "mortgage", "hoa", "lodging", "apartment"}, True, True, 35),
    ("utilities", {"utility", "utilities", "electric", "electricity", "power", "water", "heat", "heating",
                   "sewer", "trash", "energy"}, True, True, 8),
    ("phone_internet", {"phone", "cell", "mobile", "internet", "wifi", "broadband", "cable"}, True, True, None),
    ("pets", {"pet", "pets", "dog", "cat", "vet", "veterinary"}, False, False, None),
    ("groceries", {"grocery", "groceries", "food", "supermarket"}, False, True, 12),
    ("transportation", {"transportation", "transport", "transit", "gas", "gasoline", "fuel", "car", "auto",
                        "vehicle", "uber", "lyft", "metro", "metrocard", "bus", "train", "subway", "parking",
                        "toll", "commute"}, False, True, 15),
    ("health", {"health", "healthcare", "medical", "doctor", "dental", "dentist", "pharmacy", "prescription",
                "medicine", "medication", "therapy"}, False, True, None),
    ("childcare", {"childcare", "daycare", "babysitter", "babysitting", "nanny"}, True, True, None),
    ("education", {"tuition", "education", "school", "textbook", "textbooks"}, True, True, None),
    ("subscriptions", {"subscription", "subscriptions", "streaming", "netflix", "spotify", "hulu"}, True, False, 3),
    ("shopping", {"shopping", "clothes", "clothing", "amazon", "retail", "apparel"}, False, False, 6),
    ("entertainment", {"entertainment", "fun", "movie", "game", "gaming", "hobby", "hobbies", "concert",
                       "gym", "fitness"}, False, False, 5),
    ("travel", {"travel", "vacation", "trip", "flight", "hotel"}, False, False, 5),
    ("other", {"other", "misc", "miscellaneous"}, False, False, 6),
]
_RULES = {g: (fixed, essential, ref) for g, _, fixed, essential, ref in CATEGORY_RULES}


def _words(name: str) -> set:
    words = set(re.findall(r"[a-z]+", name.lower()))
    return words | {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}


def classify_category(name: str) -> Optional[str]:
    """Map a free-text expense name to a group, or None if it is not recognised."""
    words = _words(name)
    return next((g for g, keys, *_ in CATEGORY_RULES if words & keys), None)
# --- end of shared classification ---


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


_GENERIC = {"the", "my", "and", "of", "for", "bill", "bills", "monthly", "payment", "payments", "minimum"}
# Sharing a word with a debt ("Car insurance" vs. "Car loan") is not a payment on that debt.
_NOT_A_PAYMENT = {"insurance", "gas", "fuel", "parking", "toll", "tolls", "repair", "repairs",
                  "maintenance", "registration", "tax", "taxes"}
_PAYMENT_GROUPS = {None, "debt", "housing", "transportation", "education", "other"}


def _debt_like_expenses(expenses: Dict[str, float], debts: List[DebtInput]) -> List[str]:
    """Expense categories that look like a payment on a debt that is already in the debts list."""
    debt_words = set().union(*(_words(d.name) for d in debts)) - _GENERIC
    mins = {round(d.minimum_payment, 2) for d in debts if d.minimum_payment > 0}
    flagged = []
    for cat, amt in expenses.items():
        words, group = _words(cat), classify_category(cat)
        same_amount = round(amt, 2) in mins
        shares_name = bool(words & debt_words) and not (words & _NOT_A_PAYMENT) and group in _PAYMENT_GROUPS
        says_payment = bool(words & {"payment", "minimum"})
        if group == "debt" or shares_name or (same_amount and (says_payment or bool(words & debt_words))):
            flagged.append(cat)
    return flagged


def _simulate(debts: List[DebtInput], payment: float, rollover: bool = True) -> dict:
    active = [d for d in debts if d.balance > 0.005]
    if not active:
        return {"feasible": True, "months": 0, "total_interest": 0.0, "payoff_order": []}
    if payment + 1e-9 < sum(d.minimum_payment for d in active):
        return {"feasible": False, "reason": "payment_below_minimums"}
    order = sorted(active, key=lambda d: (-d.apr, d.balance))  # avalanche
    bal = {d.name: d.balance for d in order}
    paid, interest, month = {}, 0.0, 0
    while any(b > 0.005 for b in bal.values()):
        month += 1
        if month > MAX_MONTHS:
            return {"feasible": False, "reason": "never_pays_off"}
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
            if bal[d.name] <= 0.005 and d.name not in paid:
                paid[d.name] = month
                bal[d.name] = 0.0
    return {"feasible": True, "months": month, "total_interest": round(interest, 2),
            "payoff_order": [{"name": n, "paid_off_month": m} for n, m in sorted(paid.items(), key=lambda kv: kv[1])]}


@tool(name="build_financial_plan")
def build_financial_plan(monthly_income: float, expenses: Dict[str, float], savings_balance: float,
                         debts: Optional[List[DebtInput]] = None,
                         emergency_fund_target: Optional[float] = None,
                         confirm_expenses_exclude_debt_payments: bool = False) -> dict:
    """Compute the complete Pocket CFO plan: cash flow, surplus split, debt payoff and savings timeline.

    ALWAYS call this for any full plan or any question about how long it takes to become
    debt-free or reach a savings goal. Every number in your answer must come from this result;
    never estimate timelines or interest yourself. Only pass values the user provided.

    Debt minimum payments are taken from `debts`, so do NOT also list them in `expenses`.
    If the result has error "possible_double_count", ask the user about the listed categories.

    Args:
        monthly_income (float): Monthly take-home income in dollars.
        expenses (Dict[str, float]): Monthly living expenses by category, e.g. {"Rent": 1200}. Exclude debt payments.
        savings_balance (float): Current emergency savings in dollars.
        debts (List[DebtInput], optional): Every debt with balance, APR and minimum payment. Empty if none.
        emergency_fund_target (float, optional): The user's emergency fund goal in dollars.
        confirm_expenses_exclude_debt_payments (bool): Set true ONLY after the user confirmed that the
            categories flagged by a "possible_double_count" error are not payments on the listed debts.

    Returns:
        dict: plan_status ("ok"; "shortfall" = the surplus does not cover all minimum payments;
        "deficit" = expenses meet or exceed income), cash_flow, allocation (with sum_check, and
        monthly_gap = how much more per month is needed to cover expenses plus all minimums),
        debt (avalanche: months to debt-free, interest paid, interest saved vs. minimum-only),
        savings (month the goal is reached, with the debt payment rolling into savings after
        payoff), milestones and assumptions; or an "error".
    """
    try:
        debts = _unique_names(_as_models(debts, DebtInput))
    except Exception:
        return {"error": "Each debt needs a name, balance, apr and minimum_payment."}
    if monthly_income is None or monthly_income < 0:
        return {"error": "monthly_income is required and cannot be negative."}
    if not expenses or any(v is None or v < 0 for v in expenses.values()):
        return {"error": "Expenses are required and cannot be negative."}
    if savings_balance is None or savings_balance < 0:
        return {"error": "savings_balance is required and cannot be negative."}
    for d in debts:
        if d.balance < 0 or d.apr < 0 or d.apr > 100 or d.minimum_payment < 0:
            return {"error": f"Debt '{d.name}' has an invalid balance, APR or minimum payment."}
    active_debts = [d for d in debts if d.balance > 0]
    if active_debts and not confirm_expenses_exclude_debt_payments:
        flagged = _debt_like_expenses(expenses, active_debts)
        if flagged:
            return {"error": "possible_double_count", "categories": flagged,
                    "message": ("These expense categories look like payments on the listed debts: "
                                f"{', '.join(flagged)}. Debt minimum payments come from the debts list, so "
                                "counting them in expenses too would understate the surplus. Ask the user. "
                                "If a category is a debt payment, remove it from expenses (the debt itself "
                                "stays in debts with its balance, APR and minimum payment). If it is not, call "
                                "again with confirm_expenses_exclude_debt_payments=true.")}

    total = round(sum(expenses.values()), 2)
    net = round(monthly_income - total, 2)
    groups = {k: classify_category(k) for k in expenses}
    essential = round(sum(v for k, v in expenses.items() if groups[k] and _RULES[groups[k]][1]), 2)
    cash_flow = {"monthly_income": round(monthly_income, 2), "total_expenses": total, "net_cash_flow": net,
                 "status": "surplus" if net > 0 else ("break_even" if net == 0 else "deficit"),
                 "essential_expenses": essential,
                 "unrecognized_categories": [k for k in expenses if groups[k] is None]}

    # --- allocation (same policy as plan_surplus_allocation)
    active = [d for d in debts if d.balance > 0]
    mins = round(sum(d.minimum_payment for d in active), 2)
    high = any(d.apr >= HIGH_INTEREST_APR for d in active)
    fund_done = emergency_fund_target is not None and savings_balance >= emergency_fund_target
    if net <= 0:
        alloc = {"status": "deficit", "minimum_debt_payments": 0.0, "extra_debt_payment": 0.0, "savings_contribution": 0.0}
    elif net < mins:
        alloc = {"status": "shortfall", "minimum_debt_payments": net, "extra_debt_payment": 0.0, "savings_contribution": 0.0}
    else:
        remaining = net - mins
        if not fund_done:
            share = 0.7 if high else (0.3 if active else 0.0)
        else:
            share = 1.0 if active else 0.0
        extra = float(round(remaining * share))
        save = round(remaining - extra, 2) if (not fund_done or active) else 0.0
        alloc = {"status": "ok", "minimum_debt_payments": mins, "extra_debt_payment": extra, "savings_contribution": save}
    debt_payment = round(alloc["minimum_debt_payments"] + alloc["extra_debt_payment"], 2)
    alloc["total_debt_payment"] = debt_payment
    alloc["unallocated"] = round(max(net, 0) - debt_payment - alloc["savings_contribution"], 2) if alloc["status"] == "ok" else 0.0
    alloc["sum_check"] = alloc["status"] != "ok" or abs(debt_payment + alloc["savings_contribution"] + alloc["unallocated"] - net) < 0.01
    # How much more per month is needed to pay living costs AND every minimum (0 when status is ok).
    alloc["required_minimum_payments"] = mins
    alloc["minimum_payments_covered"] = alloc["status"] == "ok"
    alloc["monthly_gap"] = round(max(mins - net, 0.0), 2) if alloc["status"] != "ok" else 0.0

    # --- debt
    debt = None
    if active:
        plan = _simulate(debts, debt_payment) if debt_payment > 0 else {"feasible": False, "reason": "no_payment"}
        base = _simulate(debts, mins, rollover=False)
        saved = round(base["total_interest"] - plan["total_interest"], 2) if plan.get("feasible") and base.get("feasible") else None
        debt = {"total_debt": round(sum(d.balance for d in active), 2), "monthly_payment": debt_payment,
                "strategy": "avalanche (highest APR first)",
                "months_to_debt_free": plan.get("months"), "interest_paid": plan.get("total_interest"),
                "payoff_order": plan.get("payoff_order", []), "feasible": plan.get("feasible"),
                "infeasible_reason": None if plan.get("feasible") else plan.get("reason"),
                "minimum_only_months": base.get("months"), "minimum_only_interest_paid": base.get("total_interest"),
                "interest_saved_vs_minimum_only": saved}

    # --- savings (debt payment rolls into savings after payoff)
    savings = None
    if emergency_fund_target is not None:
        debt_free = debt["months_to_debt_free"] if debt and debt["feasible"] else (0 if not active else None)
        bal, month, goal_month = savings_balance, 0, (0 if savings_balance >= emergency_fund_target else None)
        while goal_month is None and month < MAX_MONTHS:
            month += 1
            contrib = alloc["savings_contribution"]
            if debt_free is not None and month > debt_free:
                contrib += debt_payment
            if contrib <= 0:
                break
            bal += contrib
            if bal >= emergency_fund_target - 0.005:
                goal_month = month
        savings = {"current_savings": round(savings_balance, 2), "goal": round(emergency_fund_target, 2),
                   "progress_pct": round(min(savings_balance / emergency_fund_target * 100, 100), 1) if emergency_fund_target else 100.0,
                   "monthly_contribution_now": alloc["savings_contribution"],
                   "monthly_contribution_after_debt_free": round(alloc["savings_contribution"] + (debt_payment if active else 0), 2),
                   "goal_reached_month": goal_month,
                   "three_month_essentials_target": round(essential * 3, 2),
                   "six_month_essentials_target": round(essential * 6, 2)}

    milestones = []
    if debt and debt.get("months_to_debt_free"):
        milestones.append({"month": debt["months_to_debt_free"], "milestone": "Debt-free (estimate)"})
    if savings and savings.get("goal_reached_month"):
        milestones.append({"month": savings["goal_reached_month"], "milestone": "Emergency fund goal reached (estimate)"})
    milestones.sort(key=lambda m: m["month"])

    return {"plan_status": alloc["status"], "cash_flow": cash_flow, "allocation": alloc, "debt": debt, "savings": savings,
            "milestones": milestones,
            "assumptions": "Fixed APR, monthly compounding, no new charges, same income and expenses each month, "
                           "0% interest on savings. Timelines are estimates, not guarantees."}
