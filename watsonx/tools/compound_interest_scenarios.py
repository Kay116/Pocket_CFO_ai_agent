"""Pocket CFO tool: illustrative compound-interest scenarios (education only).

Used by: Investment Education Agent.
"""
from typing import List, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool


@tool(name="compound_interest_scenarios")
def compound_interest_scenarios(monthly_contribution: float, starting_amount: float = 0.0,
                                annual_rates_pct: Optional[List[float]] = None,
                                years: Optional[List[int]] = None) -> dict:
    """Show hypothetical compound-growth scenarios for financial education.

    These are illustrations with constant assumed rates, not predictions or recommendations.
    Real investments fluctuate and can lose value.

    Args:
        monthly_contribution (float): Hypothetical amount invested each month, in dollars.
        starting_amount (float): Hypothetical starting balance, in dollars (default 0).
        annual_rates_pct (List[float], optional): Assumed annual returns in percent (default [4, 6, 8]).
        years (List[int], optional): Time horizons in years (default [5, 10, 20, 30]).

    Returns:
        dict: for each rate and horizon, the future value, total contributed and growth,
        plus a disclaimer.
    """
    if monthly_contribution is None or monthly_contribution < 0 or starting_amount < 0:
        return {"error": "Amounts must be non-negative."}
    rates = annual_rates_pct or [4.0, 6.0, 8.0]
    horizons = years or [5, 10, 20, 30]
    if any(y < 0 or y > 60 for y in horizons) or any(r < -50 or r > 50 for r in rates):
        return {"error": "Years must be 0-60 and rates between -50% and 50%."}

    scenarios = []
    for rate in rates:
        r = rate / 1200
        for y in horizons:
            n = y * 12
            fv = starting_amount + monthly_contribution * n if r == 0 else \
                starting_amount * (1 + r) ** n + monthly_contribution * (((1 + r) ** n - 1) / r)
            contributed = starting_amount + monthly_contribution * n
            scenarios.append({"annual_rate_pct": rate, "years": y, "future_value": round(fv, 2),
                              "total_contributed": round(contributed, 2), "growth": round(fv - contributed, 2)})
    return {
        "scenarios": scenarios,
        "disclaimer": "Hypothetical illustration with a constant assumed return, monthly compounding, "
                      "no fees or taxes. Not a prediction or personalized investment advice.",
    }
