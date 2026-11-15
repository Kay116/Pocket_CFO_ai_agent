"""Pocket CFO tool: deterministic monthly cash-flow analysis.

Used by: Financial Analyst Agent, Budget Agent.
"""
import re
from typing import Dict, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool

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


@tool(name="calculate_cash_flow")
def calculate_cash_flow(monthly_income: float, expenses: Dict[str, float],
                        fixed_categories: Optional[list[str]] = None) -> dict:
    """Calculate monthly cash flow, spending breakdown, and fixed vs. variable expenses.

    Always call this tool instead of doing the arithmetic yourself. Only pass values
    the user actually provided; never invent missing numbers. Use the user's own category
    names; the tool recognises common wording (e.g. "Electric", "Dining out", "Car insurance").

    Args:
        monthly_income (float): The user's monthly take-home income in dollars.
        expenses (Dict[str, float]): Monthly expenses keyed by category name, e.g. {"Rent": 1200, "Groceries": 350}.
        fixed_categories (list[str], optional): Category names to treat as fixed costs, overriding the defaults.

    Returns:
        dict: totals (income, expenses, net cash flow, savings rate, fixed, variable, essential,
        discretionary), a per-category breakdown sorted by amount with its group, categories above
        common reference ranges, unrecognized_categories (ask the user whether these are essential),
        and debt_payment_categories (debt payments; if you later pass debts to another tool, these
        must not also be in expenses). Contains an "error" key if inputs are invalid.
    """
    if monthly_income is None or not monthly_income == monthly_income or monthly_income < 0:
        return {"error": "monthly_income is required and cannot be negative."}
    if not expenses:
        return {"error": "At least one expense category is required."}
    for cat, amt in expenses.items():
        if amt is None or not amt == amt or amt < 0:
            return {"error": f"Expense '{cat}' must be a non-negative number."}

    groups = {cat: classify_category(cat) for cat in expenses}
    override = {c.strip().lower() for c in fixed_categories} if fixed_categories else None

    def is_fixed(cat):
        if override is not None:
            return cat.strip().lower() in override
        return groups[cat] is not None and _RULES[groups[cat]][0]

    total = sum(expenses.values())
    fixed = sum(a for c, a in expenses.items() if is_fixed(c))
    essential = sum(a for c, a in expenses.items() if groups[c] and _RULES[groups[c]][1])
    net = monthly_income - total

    breakdown = []
    for cat, amt in sorted(expenses.items(), key=lambda kv: -kv[1]):
        breakdown.append({"category": cat, "group": groups[cat], "amount": round(amt, 2),
                          "share_of_expenses_pct": round(amt / total * 100, 2) if total else 0.0,
                          "share_of_income_pct": round(amt / monthly_income * 100, 2) if monthly_income else None})

    # Reference ranges are checked per group, so "Electric" + "Water" are judged together.
    flags = []
    if monthly_income:
        by_group: Dict[str, list] = {}
        for cat, amt in expenses.items():
            if groups[cat] and _RULES[groups[cat]][2] is not None:
                by_group.setdefault(groups[cat], []).append((cat, amt))
        for g, items in by_group.items():
            share = round(sum(a for _, a in items) / monthly_income * 100, 2)
            if share > _RULES[g][2]:
                flags.append({"category": " + ".join(c for c, _ in items), "group": g,
                              "share_of_income_pct": share, "reference_pct": _RULES[g][2]})

    return {
        "monthly_income": round(monthly_income, 2),
        "total_expenses": round(total, 2),
        "net_cash_flow": round(net, 2),
        "status": "surplus" if net > 0 else ("break_even" if net == 0 else "deficit"),
        "savings_rate_pct": round(net / monthly_income * 100, 2) if monthly_income else None,
        "fixed_expenses": round(fixed, 2),
        "variable_expenses": round(total - fixed, 2),
        "essential_expenses": round(essential, 2),
        "discretionary_expenses": round(total - essential, 2),
        "breakdown": breakdown,
        "above_reference_range": flags,
        "unrecognized_categories": [c for c in expenses if groups[c] is None],
        "debt_payment_categories": [c for c in expenses if groups[c] == "debt"],
    }
