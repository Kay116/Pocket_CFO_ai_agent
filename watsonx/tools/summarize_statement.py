"""Pocket CFO tool: turn an uploaded bank or credit-card statement (CSV or PDF) into monthly totals.

The model never adds up transactions: this code parses the file, decides money in vs. out,
categorises each line, and returns monthly averages that build_financial_plan can use.

Privacy: individual transactions are read in memory, summarised and discarded. Only totals and
a short, masked list of uncategorised merchant names are returned. Nothing is stored or logged.

Used by: Pocket CFO Supervisor.
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

from ibm_watsonx_orchestrate.agent_builder.tools import WXOFile, tool

MAX_BYTES = 10 * 1024 * 1024
MAX_TRANSACTIONS = 5000


# --- category classification (keep identical in calculate_cash_flow.py; a test checks this) ---
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


# Expense names returned to the agent. Each one classifies back to the same group in
# build_financial_plan, so essentials and reference ranges keep working.
GROUP_NAMES = {"housing": "Rent/Housing", "utilities": "Utilities", "phone_internet": "Phone & internet",
               "groceries": "Groceries", "restaurants": "Restaurants", "transportation": "Transportation",
               "insurance": "Insurance", "health": "Health", "childcare": "Childcare", "education": "Education",
               "subscriptions": "Subscriptions", "shopping": "Shopping", "entertainment": "Entertainment",
               "travel": "Travel", "pets": "Pets", "other": "Other"}

# Merchant names seen on statements, checked before the generic keywords (so "Uber Eats" is food,
# not transport). Matched as whole words/phrases in the lowercased description.
MERCHANT_HINTS = [
    ("restaurants", ["uber eats", "ubereats", "doordash", "grubhub", "seamless", "starbucks", "dunkin",
                     "mcdonald", "chipotle", "sweetgreen", "pret a manger", "chick fil a", "panera",
                     "shake shack", "domino", "pizza", "cafe", "deli", "bagel"]),
    ("groceries", ["whole foods", "trader joe", "stop & shop", "shoprite", "kroger", "safeway", "aldi",
                   "publix", "wegmans", "key food", "foodtown", "fairway", "instacart", "h mart", "c-town",
                   "food bazaar", "fresh direct", "freshdirect", "western beef", "market", "supermarket",
                   "grocer", "grocery"]),
    ("utilities", ["con ed", "coned", "consolidated edison", "national grid", "pseg", "duke energy",
                   "pg&e", "water dept"]),
    ("phone_internet", ["t-mobile", "tmobile", "verizon", "at&t", "spectrum", "comcast", "xfinity",
                        "optimum", "mint mobile", "cricket wireless", "visible"]),
    ("subscriptions", ["netflix", "spotify", "hulu", "disney+", "disney plus", "hbo max", "max.com",
                       "apple.com/bill", "youtube premium", "amazon prime", "paramount+", "peacock",
                       "chatgpt", "openai", "icloud", "patreon"]),
    ("transportation", ["mta", "omny", "nyct", "metrocard", "lyft", "uber", "shell", "exxon", "mobil",
                        "chevron", "sunoco", "bp#", "citgo", "e-zpass", "ezpass", "amtrak", "nj transit",
                        "lirr", "citi bike", "parking"]),
    ("insurance", ["geico", "progressive", "state farm", "allstate", "lemonade", "liberty mutual"]),
    ("health", ["cvs", "walgreens", "duane reade", "rite aid", "pharmacy", "urgent care", "dental"]),
    ("shopping", ["amazon", "amzn", "target", "walmart", "costco", "best buy", "ikea", "uniqlo", "h&m",
                  "zara", "macy", "tj maxx", "marshalls", "sephora", "ulta", "etsy", "ebay", "shein",
                  "temu", "nike", "apple store", "books", "bookstore", "barnes & noble", "hardware",
                  "home depot", "lowe's", "lowes"]),
    ("entertainment", ["planet fitness", "equinox", "blink fitness", "crunch", "steam", "playstation",
                       "xbox", "nintendo", "amc", "regal", "ticketmaster", "fandango", "eventbrite"]),
    ("travel", ["airbnb", "delta air", "jetblue", "united air", "american air", "southwest", "spirit air",
                "expedia", "booking.com", "hotels.com", "marriott", "hilton"]),
    ("pets", ["petco", "petsmart", "chewy"]),
    ("housing", ["rent", "landlord", "property mgmt", "apartments"]),
]

# Money that is not income or spending, reported separately so the agent can ask about it.
TRANSFER_HINTS = ["transfer", "xfer", "to savings", "from savings", "to checking", "from checking",
                  "from chk", "to chk", "from sav", "to sav", "internal transfer"]
CARD_PAYMENT_HINTS = ["thank you", "autopay payment", "payment received", "credit card payment",
                      "card payment", "crcardpmt", "card services payment"]
CARD_ISSUERS = ["chase credit crd", "chase card", "amex", "american express", "discover", "capital one",
                "citi card", "citicard", "applecard", "apple card", "barclaycard", "synchrony", "credit card",
                "bank of america card", "bkofamerica card", "wells fargo card"]
PAYMENT_WORDS = ["payment", "pmt", "pymt", "autopay", "epay", "e-payment", "epayment", "bill pay"]
BOILERPLATE = ["debit card purchase", "pos purchase", "pos debit", "card purchase", "purchase authorized on",
               "recurring payment", "recurring", "checkcard", "visa direct", "debit card", "purchase", "pos"]
LOAN_PAYMENT_HINTS = ["student loan", "navient", "nelnet", "sallie mae", "mohela", "aidvantage", "dept of ed",
                      "car loan", "auto loan", "auto pay loan", "toyota financial", "honda financial",
                      "ally auto", "loan pmt", "loan payment", "sofi loan", "upstart", "lendingclub"]
REFUND_HINTS = ["refund", "return", "reversal", "chargeback", "credit adjustment", "merchandise credit"]
PAYROLL_HINTS = ["payroll", "direct dep", "dir dep", "salary", "adp", "paychex", "gusto", "workday",
                 "employer", "wages", "paycheck"]
P2P_HINTS = ["zelle", "venmo", "cash app", "cashapp", "paypal"]
INTEREST_HINTS = ["interest paid", "interest earned", "int earned", "dividend"]
CASH_HINTS = ["atm", "cash withdrawal", "withdrawal atm"]
FEE_HINTS = ["fee", "overdraft", "nsf", "service charge", "interest charge", "late charge", "finance charge"]
CARD_STATEMENT_MARKERS = ["minimum payment due", "credit limit", "payment due date", "new balance",
                          "available credit", "previous balance"]
INCOME_SECTION = ["deposits", "additions", "credits", "money in", "paid in"]
SPENDING_SECTION = ["withdrawals", "debits", "checks paid", "purchases", "subtractions", "money out",
                    "paid out", "card transactions", "atm & debit", "electronic payments", "fees charged"]

SUMMARY_LINES = ["ending balance", "closing balance", "new balance", "daily balance", "balance",
                 "subtotal", "summary", "total deposits", "total withdrawals", "total fees", "total purchases",
                 "total payments", "total credits", "total debits", "total checks", "total interest",
                 "total additions", "total subtractions", "total for this period", "totals"]
_PERIOD_DATE = r"(?:[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
_PERIOD_RE = re.compile(rf"(?P<a>{_PERIOD_DATE})\s*(?:through|thru|to|-|–)\s*(?P<b>{_PERIOD_DATE})", re.I)
_PERIOD_SAME_MONTH_RE = re.compile(r"(?P<m>[A-Za-z]{3,9})\.? (?P<d1>\d{1,2})\s*[-–]\s*(?P<d2>\d{1,2}),? (?P<y>\d{4})")

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                         "nov", "dec"], start=1)}


def _has(text: str, phrases: List[str]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text) for p in phrases)


def _norm(description: str) -> str:
    return re.sub(r"\s+", " ", description.lower()).strip()


def _mask(description: str) -> str:
    """Hide card/account/reference numbers before a merchant name is shown to the agent."""
    text = re.sub(r"\d{4,}", "####", description)
    return re.sub(r"\s+", " ", text).strip()[:40]


# --------------------------------------------------------------------------- amounts and dates
_AMOUNT_RE = re.compile(r"^(?P<neg1>[-−])?\s*(?P<paren>\()?\s*(?P<neg2>[-−])?\s*\$?\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                        r"\s*\)?\s*(?P<trail>-|CR|DR|cr|dr)?$")


def _parse_amount(text: str) -> Optional[Tuple[float, Optional[int]]]:
    """'1,234.56' -> (1234.56, None); '-12.00' / '(12.00)' / '12.00 DR' -> (12.0, -1); '5.00 CR' -> (5.0, +1)."""
    if text is None:
        return None
    m = _AMOUNT_RE.match(str(text).strip())
    if not m:
        return None
    value = float(m.group("num").replace(",", ""))
    trail = (m.group("trail") or "").upper()
    if m.group("neg1") or m.group("neg2") or m.group("paren") or trail in ("-", "DR"):
        sign = -1
    elif trail == "CR":
        sign = 1
    else:
        sign = None
    return value, sign


def _parse_date(text: str, default_year: int) -> Optional[date]:
    t = str(text).strip().lower().replace(",", "")
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", t)
        if m:
            y = int(m[3]) if m[3] else default_year
            return date(y + 2000 if y < 100 else y, int(m[1]), int(m[2]))
        m = re.match(r"^([a-z]{3})[a-z]*\.? (\d{1,2})(?: (\d{4}))?$", t)
        if m and m[1] in _MONTHS:
            return date(int(m[3]) if m[3] else default_year, _MONTHS[m[1]], int(m[2]))
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- CSV
_COLS = {
    "date": ["transaction date", "trans date", "trans. date", "posting date", "posted date", "post date", "date"],
    "description": ["description", "merchant", "payee", "name", "details", "memo", "transaction"],
    "amount": ["amount", "amount (usd)", "transaction amount"],
    "debit": ["debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "charge", "outflow"],
    "credit": ["credit", "credits", "deposit", "deposits", "money in", "paid in", "inflow"],
}


def _find_columns(header: List[str]) -> Optional[Dict[str, int]]:
    h = [_norm(c) for c in header]
    found = {}
    for key, names in _COLS.items():
        for name in names:  # names are in priority order
            if name in h and h.index(name) not in found.values():
                found[key] = h.index(name)
                break
    if "date" in found and "description" in found and ("amount" in found or "debit" in found or "credit" in found):
        return found
    return None


def _rows_from_csv(raw: bytes) -> Tuple[List[dict], List[str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    for i, row in enumerate(rows[:30]):  # some banks put account info above the header
        cols = _find_columns(row)
        if cols:
            break
    else:
        raise ValueError("csv_columns_not_found")
    out = []
    for row in rows[i + 1:]:
        if len(row) <= max(cols.values()):
            continue
        d = row[cols["date"]].strip()
        desc = row[cols["description"]].strip()
        if not d or not desc:
            continue
        if "amount" in cols:
            a = _parse_amount(row[cols["amount"]])
            if not a:
                continue
            out.append({"date": d, "desc": desc, "amount": a[0], "sign": a[1] if a[1] is not None else 1,
                        "explicit": True})
        else:
            debit = _parse_amount(row[cols["debit"]]) if "debit" in cols else None
            credit = _parse_amount(row[cols["credit"]]) if "credit" in cols else None
            if debit and debit[0] > 0:
                out.append({"date": d, "desc": desc, "amount": debit[0], "direction": "out"})
            elif credit and credit[0] > 0:
                out.append({"date": d, "desc": desc, "amount": credit[0], "direction": "in"})
    return out, []


# --------------------------------------------------------------------------- PDF
_DATE_PAT = r"(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2}|[A-Za-z]{3}[a-z]*\.? \d{1,2}(?:,? \d{4})?)"
_AMT_PAT = r"[-−]?\(?\$?\s?\d{1,3}(?:,\d{3})*\.\d{2}\)?(?:\s?(?:CR|DR|-))?"
_LINE_RE = re.compile(rf"^\s*(?P<date>{_DATE_PAT})\s+(?:{_DATE_PAT}\s+)?(?P<desc>.+?)\s+(?P<amt>{_AMT_PAT})"
                      rf"(?:\s+(?P<bal>{_AMT_PAT}))?\s*$")


def _text_from_pdf(raw: bytes) -> List[str]:
    """Return candidate texts: layout mode (keeps table rows on one line) and plain mode."""
    from pypdf import PdfReader  # imported here so CSV statements work without it
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("pdf_password_protected")
        except Exception:
            raise ValueError("pdf_password_protected")
    texts = []
    for mode in ("layout", "plain"):
        try:
            texts.append("\n".join(p.extract_text(extraction_mode=mode) or "" for p in reader.pages))
        except Exception:
            continue
    return texts


# A "Type" column on many statements; it tells money in from money out when amounts have no sign.
TYPE_WORDS = {"deposit": "in", "deposits": "in", "credit": "in", "withdrawal": "out", "debit": "out",
              "fee": "out", "purchase": "out", "check": "out", "atm": "out", "pos": "out", "charge": "out"}
_DATE_ONLY = re.compile(rf"^\s*{_DATE_PAT}\s*$")
_AMT_ONLY = re.compile(rf"^\s*{_AMT_PAT}\s*$")


def _split_type(desc: str) -> Tuple[str, Optional[str]]:
    """'GREENWAY MARKET   Withdrawal' -> ('GREENWAY MARKET', 'out')."""
    parts = re.split(r"\s{2,}", desc.strip())
    if len(parts) > 1 and _norm(parts[-1]) in TYPE_WORDS:
        return " ".join(parts[:-1]).strip(), TYPE_WORDS[_norm(parts[-1])]
    return desc.strip(), None


def _rows_from_cells(text: str) -> List[dict]:
    """Tables extracted one cell per line: a date line, then description (and type) lines, then the amount."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out, i = [], 0
    while i < len(lines):
        if not _DATE_ONLY.match(lines[i]):
            i += 1
            continue
        date_text, j, parts = lines[i], i + 1, []
        if j < len(lines) and _DATE_ONLY.match(lines[j]):
            j += 1  # a second (posting) date
        while j < len(lines) and len(parts) < 4 and not _AMT_ONLY.match(lines[j]) and not _DATE_ONLY.match(lines[j]):
            parts.append(lines[j])
            j += 1
        if j >= len(lines) or not _AMT_ONLY.match(lines[j]) or not parts:
            i += 1
            continue
        amount, sign = _parse_amount(lines[j])
        type_dir = next((TYPE_WORDS[_norm(p)] for p in parts if _norm(p) in TYPE_WORDS), None)
        desc = " ".join(p for p in parts if _norm(p) not in TYPE_WORDS)
        i = j + 1
        if i < len(lines) and _AMT_ONLY.match(lines[i]):
            i += 1  # a running-balance cell
        if not desc or _has(_norm(desc), SUMMARY_LINES):
            continue
        row = {"date": date_text, "desc": desc, "amount": amount}
        if sign is not None:
            row.update(sign=sign, explicit=True)
        else:
            row["direction"] = type_dir
        out.append(row)
    return out


def _rows_from_text(text: str) -> Tuple[List[dict], List[str]]:
    """Parse statement text (from a PDF) line by line. Direction comes from, in order: an explicit
    sign, the running balance, the section heading, or the description."""
    out, notes, section, prev_bal = [], [], None, None
    for line in text.splitlines():
        low = _norm(line)
        m = _LINE_RE.match(line)
        if _has(low, ["beginning balance", "opening balance", "starting balance", "balance forward",
                      "previous balance"]):
            amounts = re.findall(_AMT_PAT, line)
            if amounts:
                b = _parse_amount(amounts[-1])
                prev_bal = b[0] * (b[1] or 1) if b else prev_bal
            continue
        if m and _has(_norm(m["desc"]), SUMMARY_LINES):
            continue  # "Ending Balance", "Total Deposits"... are not transactions
        if not m:
            if low and len(low) < 60 and not re.search(r"\d+\.\d{2}", low):  # a heading, not a transaction
                if _has(low, SPENDING_SECTION):
                    section = "out"
                elif _has(low, INCOME_SECTION):
                    section = "in"
            continue
        amount, sign = _parse_amount(m["amt"])
        bal = _parse_amount(m["bal"]) if m["bal"] else None
        desc, type_dir = _split_type(m["desc"])
        row = {"date": m["date"], "desc": desc, "amount": amount}
        if sign is not None:
            row.update(sign=sign, explicit=True)
        elif type_dir:
            row["direction"] = type_dir
        elif bal is not None and prev_bal is not None and abs(abs(bal[0] - prev_bal) - amount) < 0.011:
            row["direction"] = "in" if bal[0] > prev_bal else "out"
        elif section:
            row["direction"] = section
        else:
            row["direction"] = None  # decided later from the description
        if bal is not None:
            prev_bal = bal[0] * (bal[1] or 1)
        out.append(row)
    return out, notes


# --------------------------------------------------------------------------- direction and categories
def _is_card_statement(rows: List[dict], text: str) -> bool:
    """Card statements show purchases as positive and payments as negative; bank accounts the reverse."""
    if _has(_norm(text), CARD_STATEMENT_MARKERS):
        return True
    credits = [_norm(r["desc"]) for r in rows if r.get("direction") == "in"]
    if credits and not any(_has(d, PAYROLL_HINTS) for d in credits):
        # debit/credit columns: on a card, the credits are payments to the card and refunds
        if sum(_is_card_payment(d, False, "in") or _has(d, REFUND_HINTS) for d in credits) > len(credits) / 2:
            return True
    signed = [r for r in rows if r.get("explicit")]
    money_in = [r for r in signed if _has(_norm(r["desc"]), PAYROLL_HINTS + CARD_PAYMENT_HINTS + REFUND_HINTS)]
    if money_in:
        return sum(r["sign"] < 0 for r in money_in) > len(money_in) / 2
    return bool(signed) and sum(r["sign"] > 0 for r in signed) > 0.6 * len(signed)


def _direction(row: dict, card: bool) -> str:
    if row.get("direction"):
        return row["direction"]
    if row.get("explicit"):
        positive = row["sign"] > 0
        return ("out" if positive else "in") if card else ("in" if positive else "out")
    d = _norm(row["desc"])
    return "in" if _has(d, PAYROLL_HINTS + P2P_HINTS + INTEREST_HINTS + REFUND_HINTS + ["deposit"]) else "out"


def _is_card_payment(d: str, card: bool, direction: str) -> bool:
    if _has(d, CARD_PAYMENT_HINTS):
        return True
    if card and direction == "in" and _has(d, PAYMENT_WORDS):
        return True  # on a card statement, a payment line is money paying the card off
    return _has(d, CARD_ISSUERS) and _has(d, PAYMENT_WORDS)


def _strip_boilerplate(d: str) -> str:
    for b in BOILERPLATE:
        d = re.sub(r"(?<![a-z0-9])" + re.escape(b) + r"(?![a-z0-9])", " ", d)
    return re.sub(r"\s+", " ", d).strip()


def _spending_group(desc: str) -> str:
    d = _strip_boilerplate(_norm(desc))
    for group, phrases in MERCHANT_HINTS:
        if _has(d, phrases):
            return group
    if _has(d, CASH_HINTS):
        return "cash"
    if _has(d, FEE_HINTS):
        return "fees"
    g = classify_category(d)
    return g if g and g != "debt" else "uncategorized"


def _summarize(rows: List[dict], text: str, source: str, default_year: int) -> dict:
    if not rows:
        return {"error": "no_transactions_found",
                "message": "No transactions could be read from this file. Ask the user for a CSV export "
                           "of the statement, or for their monthly income and expenses."}
    if len(rows) > MAX_TRANSACTIONS:
        return {"error": "too_many_transactions", "message": f"The file has more than {MAX_TRANSACTIONS} transactions; "
                "ask the user to upload one to three months at a time."}
    card = _is_card_statement(rows, text)
    by_month = defaultdict(lambda: defaultdict(float))
    spend = defaultdict(float)
    income_src = defaultdict(float)
    excluded = defaultdict(float)
    uncategorized = defaultdict(float)
    dates, skipped = [], 0
    parsed = [_parse_date(r["date"], default_year) for r in rows]
    no_year = [not re.search(r"\d{4}|\d/\d{1,2}/\d{2}$", str(r["date"]).strip()) for r in rows]
    months_seen = {p.month for p, ny in zip(parsed, no_year) if p and ny}
    if 1 in months_seen and 12 in months_seen:  # a Dec-Jan statement printed without years
        parsed = [p.replace(year=p.year - 1) if p and ny and p.month >= 7 else p for p, ny in zip(parsed, no_year)]
    for r, dt in zip(rows, parsed):
        if dt is None:
            skipped += 1
            continue
        dates.append(dt)
        month = f"{dt.year:04d}-{dt.month:02d}"
        d, amt, direction = _norm(r["desc"]), round(r["amount"], 2), _direction(r, card)
        if _is_card_payment(d, card, direction):
            excluded["credit_card_payments"] += amt
        elif _has(d, LOAN_PAYMENT_HINTS) and direction == "out":
            excluded["loan_payments"] += amt
        elif _has(d, TRANSFER_HINTS) and not _has(d, P2P_HINTS):
            excluded["transfers_between_own_accounts"] += amt
        elif direction == "in" and (card or _has(d, REFUND_HINTS)):  # card credits are refunds, not income
            g = _spending_group(r["desc"])
            spend[g] -= amt
            by_month[month]["refunds"] += amt
        elif direction == "in":
            src = ("paychecks" if _has(d, PAYROLL_HINTS) else "peer_to_peer (Zelle/Venmo/PayPal)" if _has(d, P2P_HINTS)
                   else "interest" if _has(d, INTEREST_HINTS) else "other_deposits")
            income_src[src] += amt
            by_month[month]["income"] += amt
        else:
            g = _spending_group(r["desc"])
            spend[g] += amt
            by_month[month]["spending"] += amt
            if g == "uncategorized":
                uncategorized[_mask(r["desc"])] += amt
    if not dates:
        return {"error": "no_dates_found", "message": "Transaction dates could not be read. Ask for a CSV export."}

    months = sorted(by_month) or sorted({f"{x.year:04d}-{x.month:02d}" for x in dates})
    start, end = min(dates), max(dates)
    pm, a, b = _PERIOD_RE.search(text or ""), None, None
    if pm:
        a, b = _parse_date(pm["a"], default_year), _parse_date(pm["b"], default_year)
    else:
        pm = _PERIOD_SAME_MONTH_RE.search(text or "")  # "October 1-31, 2026"
        if pm:
            a = _parse_date(f"{pm['m']} {pm['d1']} {pm['y']}", default_year)
            b = _parse_date(f"{pm['m']} {pm['d2']} {pm['y']}", default_year)
    if pm:  # the statement's printed period is better than first/last transaction dates
        if a and b and a <= start and b >= end and (b - a).days < 400:
            start, end = a, b
    span_days = (end - start).days + 1
    n = max(1, round(span_days / 30.44))  # Jan 5 - Feb 4 is one month, not two
    names = dict(GROUP_NAMES, cash="Cash/ATM withdrawals", fees="Bank fees & interest", uncategorized="Uncategorized")
    expenses = {names[g]: round(v / n, 2) for g, v in sorted(spend.items(), key=lambda kv: -kv[1]) if round(v / n, 2) > 0}
    # A refund with no matching purchase in the file is reported, not netted, so the category
    # amounts always add up to average_monthly_spending.
    unmatched_refunds = round(-sum(v for v in spend.values() if v < 0) / n, 2)
    income = round(sum(income_src.values()) / n, 2)
    spending = round(sum(expenses.values()), 2)

    warnings = []
    if span_days < 25:
        warnings.append("The file covers less than a month, so monthly averages may be off.")
    if card:
        warnings.append("This looks like a credit card statement: income is not on it. Ask for the bank "
                        "statement or the user's monthly take-home pay.")
    if excluded.get("credit_card_payments") and not card:
        warnings.append("Credit card payments were left out of spending. Ask about the card balance, APR and "
                        "minimum payment (a debt), or for the card statement to see what was bought.")
    if excluded.get("transfers_between_own_accounts"):
        warnings.append("Transfers were left out of income and spending, assuming they move money between "
                        "the user's own accounts. Ask whether any transfer in is actually income.")
    if excluded.get("loan_payments"):
        warnings.append("Loan payments were left out of spending. Enter those loans as debts with balance, APR "
                        "and minimum payment.")
    if income_src.get("peer_to_peer (Zelle/Venmo/PayPal)"):
        warnings.append("Some income came through Zelle/Venmo/PayPal. Ask whether it is regular income "
                        "or reimbursements.")
    if spend.get("uncategorized"):
        warnings.append("Some spending could not be categorised; ask the user about the top uncategorised merchants.")
    if skipped:
        warnings.append(f"{skipped} line(s) had unreadable dates and were skipped.")

    return {
        "source": source,
        "statement_type": "credit_card" if card else "bank_account",
        "period": {"from": start.isoformat(), "to": end.isoformat(), "days": span_days},
        "months_covered": n,
        "transactions_read": len(dates),
        "average_monthly_income": income,
        "income_by_source": {k: round(v / n, 2) for k, v in income_src.items()},
        "average_monthly_spending": spending,
        "average_monthly_net": round(income - spending, 2),
        "monthly_expenses_by_category": expenses,
        "excluded_monthly": {k: round(v / n, 2) for k, v in excluded.items()},
        "refunds_without_matching_spending_monthly": unmatched_refunds,
        "by_month": [{"month": m, "income": round(by_month[m]["income"], 2),
                      "spending": round(by_month[m]["spending"], 2),
                      "refunds": round(by_month[m]["refunds"], 2)} for m in months],
        "top_uncategorized": [{"merchant": k, "monthly_amount": round(v / n, 2)}
                              for k, v in sorted(uncategorized.items(), key=lambda kv: -kv[1])[:8]],
        "warnings": warnings,
        "next_step": ("Show the user these monthly figures and ask them to confirm or correct them before "
                      "using them. Then pass average_monthly_income as monthly_income and "
                      "monthly_expenses_by_category as expenses to build_financial_plan."),
    }


def summarize_statement_bytes(raw: bytes, file_name: str = "", content_type: str = "",
                              default_year: Optional[int] = None) -> dict:
    """Pure function behind the tool (no platform calls), so it can be tested directly."""
    year = default_year or date.today().year
    if not raw:
        return {"error": "empty_file", "message": "The file is empty."}
    if len(raw) > MAX_BYTES:
        return {"error": "file_too_large", "message": "The file is larger than 10 MB. Ask for a shorter period."}
    is_pdf = raw[:5] == b"%PDF-" or file_name.lower().endswith(".pdf") or "pdf" in (content_type or "")
    try:
        if is_pdf:
            texts = _text_from_pdf(raw)
            texts = [texts] if isinstance(texts, str) else texts
            texts = [t for t in texts if t and len(t.strip()) >= 50]
            if not texts:
                return {"error": "scanned_pdf",
                        "message": "This PDF has no readable text (it is probably a scanned image). Ask the user "
                                   "to download the statement as a CSV, or as a PDF from online banking."}
            years = re.findall(r"\b(20\d{2})\b", texts[0])
            if years:
                year = int(max(set(years), key=years.count))  # the statement's year, for dates like 01/05
            # Statements are laid out in different ways: try whole-row lines first, then one cell per
            # line, on each extracted text, and keep whichever reads the most transactions.
            best_rows, best_text = [], texts[0]
            for t in texts:
                for rows in (_rows_from_text(t)[0], _rows_from_cells(t)):
                    if len(rows) > len(best_rows):
                        best_rows, best_text = rows, t
            return _summarize(best_rows, best_text, "pdf", year)
        rows, _ = _rows_from_csv(raw)
        return _summarize(rows, "", "csv", year)
    except ImportError:
        return {"error": "pdf_support_missing", "message": "PDF reading is not installed for this tool. Ask "
                "the user for a CSV export instead, and tell the administrator to add pypdf."}
    except ValueError as exc:
        code = str(exc)
        msg = {"csv_columns_not_found": "The CSV needs date, description and amount (or debit/credit) columns. "
                                        "Ask the user to export transactions as CSV from online banking.",
               "pdf_password_protected": "The PDF is password-protected. Ask the user to download it again "
                                         "without a password, or as a CSV."}.get(code)
        return {"error": code if msg else "unreadable_file", "message": msg or "The file could not be read. Ask for a CSV export."}
    except Exception:
        return {"error": "unreadable_file", "message": "The file could not be read. Ask for a CSV export."}


@tool(name="summarize_statement")
def summarize_statement(statement: WXOFile) -> dict:
    """Read an uploaded bank or credit-card statement (CSV or PDF) and return monthly totals.

    Call this whenever the user attaches a statement or transaction export. It returns average
    monthly income, spending by category, and money left out of spending (transfers between own
    accounts, credit card payments, loan payments). Never add up transactions yourself. Always show
    the totals to the user and ask them to confirm before using them in build_financial_plan.
    Individual transactions are not stored or returned.

    Args:
        statement (WXOFile): The statement file the user uploaded (.csv or .pdf).

    Returns:
        dict: period, months_covered, average_monthly_income, income_by_source,
        average_monthly_spending, monthly_expenses_by_category, excluded_monthly,
        top_uncategorized, warnings and next_step; or an "error" with a message.
    """
    try:
        raw = WXOFile.get_content(statement)
        name = WXOFile.get_file_name(statement) or ""
        ctype = WXOFile.get_file_type(statement) or ""
    except Exception:
        return {"error": "file_unavailable", "message": "The uploaded file could not be opened. Ask the user to upload it again."}
    return summarize_statement_bytes(raw, name, ctype)
