"""Pocket CFO data tools: secure per-user storage in IBM Cloudant.

Security model
--------------
* Identity comes ONLY from the platform-injected context variable ``wxo_email_id``
  (read from ``AgentRun.request_context``). No tool accepts a user id, email or
  partition key as an argument, so a prompt can never redirect a tool to another
  user's data.
* The email is never stored. Each user is identified by
  ``user_key = HMAC-SHA256(USER_KEY_PEPPER, lowercase(email))``.
* Data lives in a *partitioned* Cloudant database; the partition key is the
  user_key and every read/write/delete goes through partition-scoped calls built
  from that key, so one user's documents are never listed alongside another's.
* Credentials (URL, IAM API key, pepper) come from the watsonx Orchestrate
  key-value connection ``pocket_cfo_cloudant``; nothing secret is in code.
* Financial values are never logged. Audit events (event name + user_key only)
  go to a separate, non-partitioned audit database.

Connection keys (key_value):
    CLOUDANT_URL       https://<account>.cloudantnosqldb.appdomain.cloud
    CLOUDANT_APIKEY    IAM API key of a service ID with Writer on this instance only
    USER_KEY_PEPPER    long random secret (e.g. `openssl rand -hex 32`); never change it
    CLOUDANT_DB        optional, default "pocket_cfo"
    CLOUDANT_AUDIT_DB  optional, default "pocket_cfo_audit"
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import BaseModel, Field

APP_ID = "pocket_cfo_cloudant"
CREDS = [{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
CONSENT_VERSION = "2026-11"
HIGH_INTEREST_APR = 8.0
MAX_AMOUNT = 10_000_000
MAX_EXPENSES, MAX_DEBTS, MAX_GOALS, MAX_SNAPSHOTS_RETURNED = 30, 20, 10, 24
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
DELETE_CODE_MINUTES = 15
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I mix-ups


# --------------------------------------------------------------------------- input models
class DebtInput(BaseModel):
    name: str = Field(description="Debt name, e.g. 'Credit card'")
    balance: float = Field(description="Current balance in dollars")
    apr: float = Field(description="Annual interest rate in percent, e.g. 22 for 22%")
    minimum_payment: float = Field(description="Required minimum monthly payment in dollars")


class GoalInput(BaseModel):
    name: str = Field(description="Goal name, e.g. 'Emergency fund'")
    target_amount: float = Field(description="Target amount in dollars")
    current_amount: float = Field(0.0, description="Amount saved toward this goal so far")
    is_emergency_fund: bool = Field(False, description="True if this goal is the emergency fund")


def _as_models(items, model):
    """watsonx passes nested objects as plain dicts at runtime; accept both."""
    return [i if isinstance(i, model) else model.model_validate(i) for i in (items or [])]


class ToolInputError(ValueError):
    pass


# --------------------------------------------------------------------------- identity & store
def _settings() -> dict:
    creds = connections.key_value(APP_ID) or {}
    missing = [k for k in ("CLOUDANT_URL", "CLOUDANT_APIKEY", "USER_KEY_PEPPER") if not creds.get(k)]
    if missing:
        raise RuntimeError(f"Connection '{APP_ID}' is missing: {', '.join(missing)}")
    return creds


def _user_key(context: AgentRun, pepper: str) -> str:
    rc = context.request_context if context is not None else None
    email = rc.get("wxo_email_id") if rc is not None else None
    if not email or "@" not in str(email):
        raise PermissionError("No signed-in user was provided by the platform.")
    return hmac.new(pepper.encode(), str(email).strip().lower().encode(), hashlib.sha256).hexdigest()


def _make_client(settings: dict):  # replaced in tests
    from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
    from ibmcloudant.cloudant_v1 import CloudantV1

    client = CloudantV1(authenticator=IAMAuthenticator(settings["CLOUDANT_APIKEY"]))
    client.set_service_url(settings["CLOUDANT_URL"])
    return client


class _UserStore:
    """All data access for ONE user. Every document id is '<user_key>:<suffix>'."""

    def __init__(self, context: AgentRun):
        s = _settings()
        self.key = _user_key(context, s["USER_KEY_PEPPER"])
        self.db = s.get("CLOUDANT_DB") or "pocket_cfo"
        self.audit_db = s.get("CLOUDANT_AUDIT_DB") or "pocket_cfo_audit"
        self.client = _make_client(s)

    def _id(self, suffix: str) -> str:
        return f"{self.key}:{suffix}"

    def get(self, suffix: str) -> Optional[dict]:
        from ibm_cloud_sdk_core import ApiException
        try:
            return self.client.get_document(db=self.db, doc_id=self._id(suffix)).get_result()
        except ApiException as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise

    def put(self, suffix: str, body: dict, rev: Optional[str] = None) -> None:
        from ibmcloudant.cloudant_v1 import Document
        doc = dict(body, _id=self._id(suffix))
        if rev:
            doc["_rev"] = rev
        self.client.put_document(db=self.db, doc_id=doc["_id"], document=Document.from_dict(doc))

    def list(self, prefix: str = "") -> List[dict]:
        res = self.client.post_partition_all_docs(
            db=self.db, partition_key=self.key, include_docs=True,
            start_key=self._id(prefix), end_key=self._id(prefix) + "￰",
        ).get_result()
        return [r["doc"] for r in res.get("rows", []) if r.get("doc")]

    def delete_all(self) -> tuple:
        """Delete every document in the user's partition. Returns (deleted, failed) counts of
        profile/snapshot documents, checking Cloudant's per-document results."""
        from ibmcloudant.cloudant_v1 import BulkDocs, Document
        docs = self.list("")
        if not docs:
            return 0, 0
        res = self.client.post_bulk_docs(db=self.db, bulk_docs=BulkDocs(
            docs=[Document(id=d["_id"], rev=d["_rev"], deleted=True) for d in docs])).get_result() or []
        failed_ids = {r.get("id") for r in res if isinstance(r, dict) and r.get("error")}
        user_docs = [d for d in docs if d.get("type") != "delete_request"]
        failed = sum(d["_id"] in failed_ids for d in user_docs)
        return len(user_docs) - failed, failed

    def audit(self, event: str) -> None:
        """Best-effort audit record: user_key + event name only, no financial data."""
        from ibmcloudant.cloudant_v1 import Document
        try:
            self.client.post_document(db=self.audit_db, document=Document.from_dict(
                {"user_key": self.key, "event": event, "at": _now()}))
        except Exception:  # auditing must never break the user's request
            pass


# --------------------------------------------------------------------------- helpers
def _utcnow() -> datetime:  # replaced in tests to simulate months passing
    return datetime.now(timezone.utc)


def _now() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _clean_text(value: str, field: str, max_len: int = 60) -> str:
    text = _CTRL.sub("", str(value or "")).strip()
    if not text:
        raise ToolInputError(f"{field} cannot be empty.")
    return text[:max_len]


def _money(value, field: str, allow_none: bool = False) -> Optional[float]:
    if value is None:
        if allow_none:
            return None
        raise ToolInputError(f"{field} is required.")
    v = float(value)
    if v != v or v < 0 or v > MAX_AMOUNT:
        raise ToolInputError(f"{field} must be between 0 and {MAX_AMOUNT:,}.")
    return round(v, 2)


def _public(doc: Optional[dict]) -> Optional[dict]:
    """Strip Cloudant internals (ids contain the user_key) before returning to the agent."""
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _error_code(exc: Exception) -> str:
    """Short, non-sensitive diagnostic code (no hostnames, keys or user data)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__
    text = str(exc).lower()
    if name == "ApiException" and status:
        if "api key" in text or "iam" in text or "token" in text:
            return f"iam_auth_failed_http_{status}"
        return f"cloudant_http_{status}"
    if isinstance(exc, ImportError):
        return "dependency_missing"
    if "connection" in name.lower() or "timeout" in name.lower() or "ssl" in name.lower():
        return f"network_{name}"
    return f"unexpected_{name}"


def _error(exc: Exception) -> dict:
    if isinstance(exc, (ToolInputError, PermissionError)):
        return {"error": str(exc)}
    if isinstance(exc, RuntimeError):
        return {"error": "Pocket CFO storage is not configured. Ask the administrator to check the connection.",
                "error_code": "connection_settings_missing"}
    return {"error": "Pocket CFO storage is temporarily unavailable. Please try again.",
            "error_code": _error_code(exc)}


def _allocation(net: float, debts: List[dict], savings: float, target: Optional[float]) -> dict:
    """Same policy as plan_surplus_allocation (kept in sync by tests)."""
    active = [d for d in debts if d["balance"] > 0]
    mins = round(sum(d["minimum_payment"] for d in active), 2)
    if net <= 0:
        return {"status": "deficit", "total_debt_payment": 0.0, "savings_contribution": 0.0}
    if net < mins:
        return {"status": "shortfall", "total_debt_payment": round(net, 2), "savings_contribution": 0.0}
    remaining = net - mins
    done = target is not None and savings >= target
    high = any(d["apr"] >= HIGH_INTEREST_APR for d in active)
    share = (0.7 if high else 0.3 if active else 0.0) if not done else (1.0 if active else 0.0)
    extra = float(round(remaining * share))
    save = round(remaining - extra, 2) if (not done or active) else 0.0
    return {"status": "ok", "total_debt_payment": round(mins + extra, 2), "savings_contribution": save}


DAYS_PER_MONTH = 365.25 / 12


def _project_plan(debts: List[dict], payment: float, savings: float, savings_contribution: float,
                  months: int) -> dict:
    """Where the plan says balances should be after `months` months.

    Same month-by-month avalanche simulation as simulate_debt_payoff (interest, minimums, then the
    rest to the highest APR), with the debt payment rolling into savings after the debt-free month,
    as in build_financial_plan. Balances are tracked by position, so duplicate names are safe.
    """
    order = sorted((d for d in debts if d["balance"] > 0.005), key=lambda d: (-d["apr"], d["balance"]))
    bal = [float(d["balance"]) for d in order]
    debt_free = 0 if not order else None
    for month in range(1, months + 1):
        if debt_free is None:
            for i, d in enumerate(order):
                if bal[i] > 0.005:
                    bal[i] += bal[i] * d["apr"] / 1200
            budget = payment
            for i, d in enumerate(order):
                if bal[i] > 0.005:
                    p = min(d["minimum_payment"], bal[i], budget)
                    bal[i] -= p
                    budget -= p
            for i in range(len(order)):
                if budget <= 0.005:
                    break
                p = min(budget, bal[i])
                bal[i] -= p
                budget -= p
            bal = [0.0 if b <= 0.005 else b for b in bal]
            savings += savings_contribution
            if not any(bal):
                debt_free = month
        else:
            savings += savings_contribution + (payment if order else 0.0)
    return {"expected_total_debt": round(sum(bal), 2), "expected_savings_balance": round(savings, 2),
            "expected_debt_free_month": debt_free}


def _vs_plan_status(diff: float, planned_monthly: float) -> str:
    """diff > 0 means better than planned. Small gaps (max $25 or 10% of a month) count as on track."""
    tolerance = max(25.0, 0.10 * planned_monthly)
    return "ahead" if diff > tolerance else "behind" if diff < -tolerance else "on_track"


def _debt_changes(old: Optional[List[dict]], new: List[dict]) -> Optional[List[dict]]:
    if old is None:
        return None
    prev = {d["name"]: d["balance"] for d in old}
    out = []
    for d in new:
        p = prev.pop(d["name"], None)
        out.append({"name": d["name"], "previous_balance": p, "current_balance": d["balance"],
                    "paid_down": round(p - d["balance"], 2) if p is not None else None})
    for name, p in prev.items():  # in the snapshot but no longer in the profile
        out.append({"name": name, "previous_balance": p, "current_balance": None, "paid_down": None,
                    "removed_from_profile": True})
    return out


# --------------------------------------------------------------------------- tools
@tool(name="save_financial_profile", permission=ToolPermission.READ_WRITE, expected_credentials=CREDS)
def save_financial_profile(context: AgentRun, consent_to_store: bool,
                           monthly_income: Optional[float] = None,
                           savings_balance: Optional[float] = None,
                           expenses: Optional[Dict[str, float]] = None,
                           debts: Optional[List[DebtInput]] = None,
                           savings_goals: Optional[List[GoalInput]] = None) -> dict:
    """Save or update the signed-in user's financial profile so it is remembered next time.

    Only fields that are passed are changed; omitted fields keep their saved values. Passing a
    list (expenses, debts, savings_goals) replaces that whole list, so always pass every debt.
    savings_balance is the emergency fund balance; the emergency fund goal's current_amount is
    kept equal to it automatically. The FIRST save needs the user's consent: either the user
    asked you to save or remember their details, or you asked "May I save your financial details
    so I can remember them next time?" and they clearly agreed. Do not ask again if they already
    asked you to save. Never ask for or pass the user's email or identity; the platform provides it.

    Args:
        consent_to_store (bool): True only if the user asked to save their data or explicitly agreed to it.
        monthly_income (float, optional): Monthly take-home income in dollars.
        savings_balance (float, optional): Current savings balance in dollars.
        expenses (Dict[str, float], optional): Monthly expenses by category, e.g. {"Rent": 1200}.
        debts (List[DebtInput], optional): All debts with balance, APR and minimum payment.
        savings_goals (List[GoalInput], optional): Savings goals, including the emergency fund.

    Returns:
        dict: the saved profile (without internal identifiers) or an "error" message.
    """
    try:
        try:
            debts = _as_models(debts, DebtInput) if debts is not None else None
            savings_goals = _as_models(savings_goals, GoalInput) if savings_goals is not None else None
        except Exception:
            raise ToolInputError("Each debt needs name, balance, apr and minimum_payment; "
                                 "each goal needs name and target_amount.")
        store = _UserStore(context)
        existing = store.get("profile")
        if existing is None and not consent_to_store:
            return {"error": "consent_required",
                    "message": "Nothing was saved. If the user already asked you to save, store, remember or "
                               "keep track of their details, that is consent: call again now with "
                               "consent_to_store=true, without asking again. Otherwise ask them once."}
        profile = _public(existing) or {}
        if consent_to_store:
            profile["consent"] = {"version": CONSENT_VERSION, "granted_at": _now()}
        if monthly_income is not None:
            profile["monthly_income"] = _money(monthly_income, "monthly_income")
        if savings_balance is not None:
            profile["savings_balance"] = _money(savings_balance, "savings_balance")
        if expenses is not None:
            if len(expenses) > MAX_EXPENSES:
                raise ToolInputError(f"At most {MAX_EXPENSES} expense categories.")
            profile["expenses"] = {_clean_text(k, "category"): _money(v, f"expense '{k}'") for k, v in expenses.items()}
        if debts is not None:
            if len(debts) > MAX_DEBTS:
                raise ToolInputError(f"At most {MAX_DEBTS} debts.")
            cleaned = []
            for d in debts:
                if d.apr < 0 or d.apr > 100:
                    raise ToolInputError(f"APR for '{d.name}' must be between 0 and 100.")
                cleaned.append({"name": _clean_text(d.name, "debt name"), "balance": _money(d.balance, "balance"),
                                "apr": round(float(d.apr), 2), "minimum_payment": _money(d.minimum_payment, "minimum_payment")})
            used = set()  # same rule as the calculation tools: duplicates become "Visa (2)"
            for d in cleaned:
                base, n = d["name"], 1
                while d["name"].lower() in used:
                    n += 1
                    d["name"] = f"{base} ({n})"
                used.add(d["name"].lower())
            profile["debts"] = cleaned
        if savings_goals is not None:
            if len(savings_goals) > MAX_GOALS:
                raise ToolInputError(f"At most {MAX_GOALS} savings goals.")
            profile["savings_goals"] = [{"name": _clean_text(g.name, "goal name"),
                                         "target_amount": _money(g.target_amount, "target_amount"),
                                         "current_amount": _money(g.current_amount, "current_amount"),
                                         "is_emergency_fund": bool(g.is_emergency_fund)} for g in savings_goals]
            if sum(g["is_emergency_fund"] for g in profile["savings_goals"]) > 1:
                raise ToolInputError("Only one savings goal can be the emergency fund.")
        # savings_balance is the single source of truth for the emergency fund; the goal mirrors it.
        ef = next((g for g in profile.get("savings_goals", []) if g.get("is_emergency_fund")), None)
        if ef is not None:
            if savings_balance is None and savings_goals is not None and profile.get("savings_balance") is None:
                profile["savings_balance"] = ef["current_amount"]  # only the goal's amount was given
            if profile.get("savings_balance") is not None:
                ef["current_amount"] = profile["savings_balance"]
        profile["type"] = "profile"
        profile["updated_at"] = _now()
        store.put("profile", profile, rev=existing.get("_rev") if existing else None)
        store.audit("profile_saved")
        return {"saved": True, "profile": profile}
    except Exception as exc:
        return _error(exc)


@tool(name="load_financial_profile", permission=ToolPermission.READ_ONLY, expected_credentials=CREDS)
def load_financial_profile(context: AgentRun) -> dict:
    """Load the signed-in user's saved financial profile (income, expenses, debts, savings goals).

    Call this at the start of a planning conversation. If "found" is false, ask the user for
    their numbers. Values that are missing in the profile are unknown; never assume them.

    Returns:
        dict: {"found": bool, "profile": {...}} or an "error" message.
    """
    try:
        doc = _UserStore(context).get("profile")
        return {"found": doc is not None, "profile": _public(doc)}
    except Exception as exc:
        return _error(exc)


@tool(name="save_analysis_snapshot", permission=ToolPermission.READ_WRITE, expected_credentials=CREDS)
def save_analysis_snapshot(context: AgentRun, plan_summary: str) -> dict:
    """Save a dated snapshot of the user's finances after a full plan, for progress tracking.

    The numbers are recalculated from the saved profile by this tool, so they are always
    consistent; you only provide a short summary of the plan the user agreed to.

    Args:
        plan_summary (str): One or two sentences describing the agreed plan (max 500 characters).

    Returns:
        dict: the stored snapshot, or an "error" message (for example if no profile is saved).
    """
    try:
        store = _UserStore(context)
        profile = _public(store.get("profile"))
        if not profile or profile.get("monthly_income") is None or "expenses" not in profile:
            raise ToolInputError("Save the user's income and expenses with save_financial_profile first.")
        income = profile["monthly_income"]
        total_exp = round(sum(profile["expenses"].values()), 2)
        debts = profile.get("debts", [])
        savings = profile.get("savings_balance") or 0.0
        ef = next((g for g in profile.get("savings_goals", []) if g.get("is_emergency_fund")), None)
        net = round(income - total_exp, 2)
        snap = {
            "type": "snapshot", "taken_at": _now(),
            "monthly_income": income, "total_expenses": total_exp, "net_cash_flow": net,
            "total_debt": round(sum(d["balance"] for d in debts), 2),
            "debts": [{k: d[k] for k in ("name", "balance", "apr", "minimum_payment")} for d in debts],
            "savings_balance": savings,
            "emergency_fund_target": ef["target_amount"] if ef else None,
            "allocation": _allocation(net, debts, savings, ef["target_amount"] if ef else None),
            "plan_summary": _CTRL.sub(" ", plan_summary or "").strip()[:500],
        }
        stamp = _utcnow().strftime("%Y%m%dT%H%M%S")
        store.put(f"snap-{stamp}-{secrets.token_hex(3)}", snap)
        store.audit("snapshot_saved")
        return {"saved": True, "snapshot": snap}
    except Exception as exc:
        return _error(exc)


@tool(name="get_progress_history", permission=ToolPermission.READ_ONLY, expected_credentials=CREDS)
def get_progress_history(context: AgentRun, limit: int = 6) -> dict:
    """Return the signed-in user's most recent snapshots and the change since the previous one.

    Use this to tell the user how their debt, savings and cash flow changed over time. Report
    only the numbers returned here.

    Args:
        limit (int): How many recent snapshots to return (1-24, default 6).

    Returns:
        dict: {"snapshots": [...newest first], "change_since_previous": {...} or null}.
    """
    try:
        limit = max(1, min(int(limit), MAX_SNAPSHOTS_RETURNED))
        snaps = sorted((_public(d) for d in _UserStore(context).list("snap-")),
                       key=lambda s: s["taken_at"], reverse=True)[:limit]
        change = None
        if len(snaps) >= 2:
            new, old = snaps[0], snaps[1]
            change = {k: round(new[k] - old[k], 2) for k in ("total_debt", "savings_balance", "net_cash_flow", "total_expenses")}
            change["from"], change["to"] = old["taken_at"], new["taken_at"]
        return {"count": len(snaps), "snapshots": snaps, "change_since_previous": change}
    except Exception as exc:
        return _error(exc)


@tool(name="monthly_check_in", permission=ToolPermission.READ_ONLY, expected_credentials=CREDS)
def monthly_check_in(context: AgentRun) -> dict:
    """Compare the signed-in user's current saved balances with their last plan snapshot.

    Call this when the user wants a progress check-in. First ask for their current debt balances
    and savings balance and save them with save_financial_profile; then call this tool. Report
    only the numbers it returns. It does not change any data.

    Returns:
        dict: ready (false with a reason if there is no profile or no snapshot yet), months since
        the snapshot, check_in_due, balances_updated_since_snapshot, since_last_snapshot (change in
        debt, savings, income and cash flow, per debt), plan_comparison (where the plan expected the
        balances to be now, and on_track / ahead / behind) or the reason it is unavailable, and
        replan_recommended with the reasons; or an "error" message.
    """
    try:
        store = _UserStore(context)
        profile = _public(store.get("profile"))
        if not profile:
            return {"ready": False, "reason": "no_profile"}
        snaps = sorted((_public(d) for d in store.list("snap-")), key=lambda s: s["taken_at"], reverse=True)
        if not snaps:
            return {"ready": False, "reason": "no_baseline"}
        snap = snaps[0]

        days = (_utcnow() - datetime.fromisoformat(snap["taken_at"])).total_seconds() / 86400
        months = max(int(days / DAYS_PER_MONTH), 0)
        debts = profile.get("debts", [])
        income = profile.get("monthly_income")
        expenses = round(sum(profile.get("expenses", {}).values()), 2)
        net = round(income - expenses, 2) if income is not None else None
        total_debt = round(sum(d["balance"] for d in debts), 2)
        savings = profile.get("savings_balance") or 0.0
        debt_change = round(total_debt - snap["total_debt"], 2)

        def change(new, old):
            return round(new - old, 2) if new is not None and old is not None else None

        since = {
            "from": snap["taken_at"], "to": _now(), "months_elapsed": months,
            "total_debt_change": debt_change, "debt_paid_down": round(max(-debt_change, 0.0), 2),
            "savings_added": round(savings - (snap.get("savings_balance") or 0.0), 2),
            "monthly_income_change": change(income, snap.get("monthly_income")),
            "total_expenses_change": change(expenses, snap.get("total_expenses")),
            "net_cash_flow_change": change(net, snap.get("net_cash_flow")),
            "debts": _debt_changes(snap.get("debts"), debts),
        }

        plan, unavailable = None, None
        alloc = snap.get("allocation") or {}
        if months < 1:
            unavailable = "less_than_a_month_since_snapshot"
        elif "debts" not in snap:
            unavailable = "snapshot_has_no_debt_detail"
        elif alloc.get("status") != "ok":
            unavailable = "snapshot_plan_had_no_surplus"
        else:
            pay, save = alloc["total_debt_payment"], alloc["savings_contribution"]
            proj = _project_plan(snap["debts"], pay, snap.get("savings_balance") or 0.0, save, months)
            debt_vs = round(proj["expected_total_debt"] - total_debt, 2)
            save_vs = round(savings - proj["expected_savings_balance"], 2)
            ds, ss = _vs_plan_status(debt_vs, pay), _vs_plan_status(save_vs, save)
            plan = {"months_compared": months, "planned_monthly_debt_payment": pay,
                    "planned_monthly_savings": save, **proj,
                    "actual_total_debt": total_debt, "actual_savings_balance": savings,
                    "debt_vs_plan": debt_vs, "debt_status": ds,
                    "savings_vs_plan": save_vs, "savings_status": ss,
                    "overall_status": "behind" if "behind" in (ds, ss) else "ahead" if "ahead" in (ds, ss) else "on_track"}

        reasons = []
        if plan and plan["overall_status"] == "behind":
            reasons.append("behind_plan")
        if since["monthly_income_change"] or since["total_expenses_change"]:
            reasons.append("income_or_expenses_changed")
        if since["debts"] and any(d["previous_balance"] is None for d in since["debts"]):
            reasons.append("new_debt")

        return {"ready": True, "check_in_due": months >= 1,
                "balances_updated_since_snapshot": profile.get("updated_at", "") > snap["taken_at"],
                "baseline_plan_summary": snap.get("plan_summary"),
                "since_last_snapshot": since, "plan_comparison": plan,
                "plan_comparison_unavailable_reason": unavailable,
                "replan_recommended": bool(reasons), "replan_reasons": reasons}
    except Exception as exc:
        return _error(exc)


@tool(name="export_my_data", permission=ToolPermission.READ_ONLY, expected_credentials=CREDS)
def export_my_data(context: AgentRun) -> dict:
    """Export everything Pocket CFO has stored for the signed-in user (profile and snapshots).

    Returns:
        dict: {"profile": {...} or null, "snapshots": [...], "exported_at": "..."}.
    """
    try:
        store = _UserStore(context)
        docs = [_public(d) for d in store.list("")]
        store.audit("data_exported")
        return {"exported_at": _now(),
                "profile": next((d for d in docs if d.get("type") == "profile"), None),
                "snapshots": sorted((d for d in docs if d.get("type") == "snapshot"), key=lambda s: s["taken_at"])}
    except Exception as exc:
        return _error(exc)


@tool(name="delete_my_data", permission=ToolPermission.READ_WRITE, expected_credentials=CREDS)
def delete_my_data(context: AgentRun, confirmation_code: Optional[str] = None) -> dict:
    """Permanently delete everything Pocket CFO has stored for the signed-in user. Two steps.

    Step 1: call WITHOUT confirmation_code. You get a short code. Tell the user exactly what will
    be deleted (profile and all progress snapshots), that it cannot be undone, show the code, and
    ask them to type it to confirm. Step 2: only when the user's own latest message contains the
    code, call again with confirmation_code set to what the user typed. Never pass a code the
    user did not type. Codes expire after 15 minutes.

    Args:
        confirmation_code (str, optional): The code exactly as the user typed it back.

    Returns:
        dict: step 1: {"confirmation_required": true, "confirmation_code": "..."}; step 2:
        {"deleted": n} or an "error" (wrong or expired code, or a partial failure).
    """
    try:
        store = _UserStore(context)
        if not confirmation_code:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
            req = store.get("delete-request")
            store.put("delete-request", {"type": "delete_request", "code": code, "issued_at": _now()},
                      rev=req.get("_rev") if req else None)
            store.audit("delete_requested")
            return {"deleted": 0, "confirmation_required": True, "confirmation_code": code,
                    "expires_in_minutes": DELETE_CODE_MINUTES,
                    "message": "Explain what will be permanently deleted, show this code, and ask the user "
                               "to type it to confirm. Do not call delete_my_data again until they do."}
        req = store.get("delete-request")
        issued = datetime.fromisoformat(req["issued_at"]) if req else None
        if (req is None or _utcnow() - issued > timedelta(minutes=DELETE_CODE_MINUTES)
                or not hmac.compare_digest(str(confirmation_code).strip().upper(), req["code"])):
            return {"deleted": 0, "error": "invalid_or_expired_code",
                    "message": "The code is wrong or expired. Nothing was deleted. Start again without a code."}
        deleted, failed = store.delete_all()
        if failed:
            store.audit("data_delete_partial")
            return {"deleted": deleted, "failed": failed,
                    "error": "Some data could not be deleted. Nothing more was changed; please try again."}
        store.audit("data_deleted")
        return {"deleted": deleted}
    except Exception as exc:
        return _error(exc)
