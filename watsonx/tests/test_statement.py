"""Tests for summarize_statement: bank / credit-card statements (CSV and PDF) -> monthly totals.

Expected numbers are worked out by hand from the sample statements below.
"""
import json
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_financial_plan as bfp_mod  # noqa: E402
import calculate_cash_flow as ccf_mod  # noqa: E402
import summarize_statement as st  # noqa: E402
from build_financial_plan import build_financial_plan  # noqa: E402
from summarize_statement import summarize_statement_bytes as summarize  # noqa: E402
from test_data_tools import run  # noqa: E402

# ----------------------------------------------------------------- sample statements
BANK_CSV = b"""Details,Posting Date,Description,Amount,Type,Balance
CREDIT,01/02/2026,ACME CORP PAYROLL DIRECT DEP,1750.00,ACH_CREDIT,2750.00
DEBIT,01/03/2026,RENT PAYMENT LANDLORD LLC,-1200.00,ACH_DEBIT,1550.00
DEBIT,01/05/2026,CON ED ONLINE PAYMENT,-85.00,ACH_DEBIT,1465.00
DEBIT,01/06/2026,WHOLE FOODS MKT #10234,-120.00,DEBIT_CARD,1345.00
DEBIT,01/07/2026,STARBUCKS STORE 04567,-6.50,DEBIT_CARD,1338.50
DEBIT,01/08/2026,MTA*NYCT PAYGO,-34.00,DEBIT_CARD,1304.50
DEBIT,01/09/2026,NETFLIX.COM,-15.49,DEBIT_CARD,1289.01
DEBIT,01/10/2026,CHASE CREDIT CRD AUTOPAY 1234567,-300.00,ACH_DEBIT,989.01
DEBIT,01/11/2026,ONLINE TRANSFER TO SAV ...9876,-200.00,ACCT_XFER,789.01
CREDIT,01/12/2026,ZELLE FROM JANE ROOMMATE,400.00,QUICKPAY_CREDIT,1189.01
DEBIT,01/13/2026,ATM WITHDRAWAL 000123 BROADWAY,-60.00,ATM,1129.01
DEBIT,01/14/2026,DEBIT CARD PURCHASE XYZ BOUTIQUE 99887766,-45.00,DEBIT_CARD,1084.01
CREDIT,01/15/2026,AMAZON.COM REFUND,20.00,MISC_CREDIT,1104.01
CREDIT,01/16/2026,ACME CORP PAYROLL DIRECT DEP,1750.00,ACH_CREDIT,2854.01
DEBIT,01/30/2026,UBER EATS ORDER,-25.00,DEBIT_CARD,2829.01
"""

# Card export with purchases POSITIVE and the payment negative (Discover-style).
CARD_CSV = b"""Trans. Date,Post Date,Description,Amount,Category
02/01/2026,02/02/2026,TRADER JOE'S #540,82.50,Supermarkets
02/03/2026,02/04/2026,SPOTIFY USA,11.99,Services
02/05/2026,02/06/2026,INTERNET PAYMENT - THANK YOU,-400.00,Payments and Credits
02/10/2026,02/11/2026,SHELL OIL 57444,40.00,Gasoline
02/12/2026,02/13/2026,TARGET 00012345,-15.00,Merchandise
02/20/2026,02/21/2026,TARGET 00012345,60.00,Merchandise
02/27/2026,02/28/2026,AMC THEATRES,18.00,Entertainment
"""

# Card export with separate Debit / Credit columns (Capital One-style).
DEBIT_CREDIT_CSV = b"""Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-03-02,2026-03-03,1234,CHIPOTLE 1234,Dining,14.25,
2026-03-05,2026-03-06,1234,CAPITAL ONE MOBILE PYMT,Payment/Credit,,250.00
2026-03-09,2026-03-10,1234,T-MOBILE,Phone/Cable,65.00,
2026-03-28,2026-03-29,1234,PETCO 1234,Merchandise,30.00,
"""

BANK_PDF_TEXT = """ACME BANK  Statement Period: January 1, 2026 through January 31, 2026
Beginning Balance 1,000.00
Date Description Amount Balance
01/03 PAYROLL DIRECT DEP ACME 1,750.00 2,750.00
01/05 RENT PAYMENT LANDLORD LLC 1,200.00 1,550.00
01/07 KEY FOOD 0123 BROOKLYN 64.20 1,485.80
01/09 VERIZON WIRELESS 70.00 1,415.80
01/12 ZELLE FROM JOHN 50.00 1,465.80
01/20 APPLECARD GSBANK PAYMENT 250.00 1,215.80
01/22 TOTAL WINE & MORE 30.00 1,185.80
01/31 Ending Balance 1,185.80"""

SECTIONS_PDF_TEXT = """CHECKING SUMMARY January 2026
DEPOSITS AND ADDITIONS
01/02 Direct Deposit ACME PAYROLL 2,000.00
01/16 Direct Deposit ACME PAYROLL 2,000.00
Total Deposits and Additions 4,000.00
ELECTRONIC WITHDRAWALS
01/03 GEICO AUTO INSURANCE 150.00
01/04 NATIONAL GRID 90.00
01/10 SHELL OIL 57444 45.00
ATM & DEBIT CARD WITHDRAWALS
01/11 TRADER JOE'S #540 88.10
01/18 PLANET FITNESS 24.99"""

CARD_PDF_TEXT = """Minimum Payment Due $35.00  Payment Due Date 02/25/2026  New Balance $812.45
Trans Date Post Date Description Amount
12/28 12/29 AMAZON.COM*AB12CD 45.99
12/30 12/31 STARBUCKS 800-782-7282 6.75
01/03 01/04 INTERNET PAYMENT - THANK YOU -500.00
01/05 01/06 SPOTIFY USA 11.99
01/08 01/09 UBER *TRIP 23.40
01/10 01/11 TARGET 00012345 (10.00)
01/15 01/16 INTEREST CHARGE ON PURCHASES 14.21"""


def from_text(text, year=2026):
    rows, _ = st._rows_from_text(text)
    return st._summarize(rows, text, "pdf", year)


# ----------------------------------------------------------------- CSV
def test_bank_csv_golden():
    r = summarize(BANK_CSV, "chase.csv")
    assert r["statement_type"] == "bank_account" and r["months_covered"] == 1
    assert r["income_by_source"] == {"paychecks": 3500, "peer_to_peer (Zelle/Venmo/PayPal)": 400}
    assert r["average_monthly_income"] == 3900
    assert r["monthly_expenses_by_category"] == {
        "Rent/Housing": 1200, "Groceries": 120, "Utilities": 85, "Cash/ATM withdrawals": 60,
        "Uncategorized": 45, "Transportation": 34, "Restaurants": 31.5, "Subscriptions": 15.49}
    assert r["average_monthly_spending"] == 1590.99 == round(sum(r["monthly_expenses_by_category"].values()), 2)
    assert r["average_monthly_net"] == 2309.01
    assert r["excluded_monthly"] == {"credit_card_payments": 300, "transfers_between_own_accounts": 200}
    assert r["refunds_without_matching_spending_monthly"] == 20


def test_utility_online_payment_is_not_a_card_payment():
    r = summarize(BANK_CSV, "chase.csv")
    assert r["monthly_expenses_by_category"]["Utilities"] == 85  # "CON ED ONLINE PAYMENT"


def test_uber_eats_is_food_not_transport():
    r = summarize(BANK_CSV, "chase.csv")
    assert r["monthly_expenses_by_category"]["Restaurants"] == 31.5  # 6.50 Starbucks + 25 Uber Eats


def test_account_numbers_are_masked_and_no_transactions_returned():
    r = summarize(BANK_CSV, "chase.csv")
    assert r["top_uncategorized"] == [{"merchant": "DEBIT CARD PURCHASE XYZ BOUTIQUE ####", "monthly_amount": 45}]
    dump = json.dumps(r)
    assert "99887766" not in dump and "1234567" not in dump and "JANE" not in dump
    assert "transactions" not in r


def test_bank_warnings_ask_the_right_questions():
    w = " ".join(summarize(BANK_CSV, "chase.csv")["warnings"])
    assert "Credit card payments were left out" in w and "Zelle" in w and "categorised" in w


def test_card_csv_with_positive_purchases():
    r = summarize(CARD_CSV, "discover.csv")
    assert r["statement_type"] == "credit_card" and r["average_monthly_income"] == 0
    assert r["monthly_expenses_by_category"] == {"Groceries": 82.5, "Shopping": 45, "Transportation": 40,
                                                 "Entertainment": 18, "Subscriptions": 11.99}
    assert r["excluded_monthly"] == {"credit_card_payments": 400}
    assert any("credit card statement" in w for w in r["warnings"])


def test_card_csv_with_debit_credit_columns():
    r = summarize(DEBIT_CREDIT_CSV, "capitalone.csv")
    assert r["statement_type"] == "credit_card"
    assert r["monthly_expenses_by_category"] == {"Phone & internet": 65, "Pets": 30, "Restaurants": 14.25}
    assert r["excluded_monthly"] == {"credit_card_payments": 250}


def test_csv_without_needed_columns():
    r = summarize(b"foo,bar\n1,2\n", "x.csv")
    assert r["error"] == "csv_columns_not_found" and "CSV" in r["message"]


# ----------------------------------------------------------------- PDF text
def test_bank_pdf_direction_from_running_balance():
    r = from_text(BANK_PDF_TEXT)
    assert r["income_by_source"] == {"paychecks": 1750, "peer_to_peer (Zelle/Venmo/PayPal)": 50}
    assert r["monthly_expenses_by_category"] == {"Rent/Housing": 1200, "Phone & internet": 70,
                                                 "Groceries": 64.2, "Uncategorized": 30}
    assert r["excluded_monthly"] == {"credit_card_payments": 250}


def test_summary_lines_are_not_transactions_but_total_wine_is():
    r = from_text(BANK_PDF_TEXT)
    assert r["transactions_read"] == 7  # "Ending Balance" skipped; "TOTAL WINE & MORE" kept
    assert r["top_uncategorized"][0]["merchant"] == "TOTAL WINE & MORE"


def test_printed_statement_period_is_used():
    r = from_text(BANK_PDF_TEXT)
    assert r["period"] == {"from": "2026-01-01", "to": "2026-01-31", "days": 31}
    assert not any("less than a month" in w for w in r["warnings"])


def test_bank_pdf_direction_from_section_headings():
    r = from_text(SECTIONS_PDF_TEXT)
    assert r["average_monthly_income"] == 4000
    assert r["monthly_expenses_by_category"] == {"Insurance": 150, "Utilities": 90, "Groceries": 88.1,
                                                 "Transportation": 45, "Entertainment": 24.99}


def test_card_pdf_with_year_wrap_and_parenthesised_refund():
    r = from_text(CARD_PDF_TEXT)
    assert r["statement_type"] == "credit_card"
    assert r["period"]["from"] == "2025-12-28" and r["period"]["to"] == "2026-01-15"
    assert r["monthly_expenses_by_category"] == {"Shopping": 35.99, "Transportation": 23.4,
                                                 "Bank fees & interest": 14.21, "Subscriptions": 11.99,
                                                 "Restaurants": 6.75}
    assert r["excluded_monthly"] == {"credit_card_payments": 500}


def test_scanned_pdf(monkeypatch):
    monkeypatch.setattr(st, "_text_from_pdf", lambda raw: "   ")
    assert summarize(b"%PDF-1.4 ...", "scan.pdf")["error"] == "scanned_pdf"


def test_password_protected_pdf(monkeypatch):
    def locked(raw):
        raise ValueError("pdf_password_protected")
    monkeypatch.setattr(st, "_text_from_pdf", locked)
    assert summarize(b"%PDF-1.4 ...", "locked.pdf")["error"] == "pdf_password_protected"


def test_pdf_without_pypdf_installed(monkeypatch):
    def missing(raw):
        raise ImportError("pypdf")
    monkeypatch.setattr(st, "_text_from_pdf", missing)
    assert summarize(b"%PDF-1.4 ...", "a.pdf")["error"] == "pdf_support_missing"


def test_pdf_bytes_route_through_text_parser(monkeypatch):
    monkeypatch.setattr(st, "_text_from_pdf", lambda raw: SECTIONS_PDF_TEXT)
    assert summarize(b"%PDF-1.4 ...", "jan.pdf")["average_monthly_income"] == 4000


# ----------------------------------------------------------------- a real PDF file (needs pypdf)
def _make_pdf(lines):
    """Minimal one-page PDF with Helvetica text lines, written by hand (no PDF library needed)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 9 Tf 40 800 Td 12 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    stream = zlib.compress(content.encode("latin-1"))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_real_pdf_file_end_to_end():
    pytest.importorskip("pypdf")
    r = summarize(_make_pdf(SECTIONS_PDF_TEXT.splitlines()), "statement.pdf")
    assert r["source"] == "pdf" and r["average_monthly_income"] == 4000
    assert r["average_monthly_spending"] == 398.09


# ----------------------------------------------------------------- fits the rest of Pocket CFO
def test_classifier_copy_is_identical():
    assert st.CATEGORY_RULES == ccf_mod.CATEGORY_RULES == bfp_mod.CATEGORY_RULES


def test_category_names_classify_back_to_their_group():
    for group, name in st.GROUP_NAMES.items():
        assert ccf_mod.classify_category(name) == group, name


def test_output_feeds_build_financial_plan():
    r = summarize(BANK_CSV, "chase.csv")
    plan = run(build_financial_plan, r["average_monthly_income"], r["monthly_expenses_by_category"], 1000, [], 5000)
    assert plan["cash_flow"]["net_cash_flow"] == r["average_monthly_net"]
    # rent, utilities, groceries and transportation are recognised as essentials
    assert plan["cash_flow"]["essential_expenses"] == 1200 + 85 + 120 + 34


def test_tool_takes_an_uploaded_file():
    spec = st.summarize_statement.__tool_spec__
    prop = spec.input_schema.properties["statement"]
    assert prop.format == "wxo-file"
    assert spec.input_schema.required == ["statement"]


# ----------------------------------------------------------------- live test 2026-11-15: one cell per line
# watsonx returned "no transactions" for a fictional statement whose table pypdf extracts one cell
# per line (date / description / type / amount). Data below is from that fictional test PDF.
CELLS = [("2026-10-02", "PAYROLL TEST DEPOSIT", "Deposit", "$1,800.00"),
         ("2026-10-03", "GREENWAY MARKET - TEST", "Withdrawal", "-$64.25"),
         ("2026-10-05", "NORTHSTAR UTILITIES - TEST", "Withdrawal", "-$123.40"),
         ("2026-10-07", "SYNTHETIC TRANSFER IN", "Deposit", "$750.00"),
         ("2026-10-09", "CITY TRANSIT - TEST", "Withdrawal", "-$48.17"),
         ("2026-10-11", "HARBOR BOOKS - TEST", "Withdrawal", "-$315.00"),
         ("2026-10-13", "MONTHLY SERVICE FEE - TEST", "Fee", "-$6.00"),
         ("2026-10-15", "PINE PHARMACY - TEST", "Withdrawal", "-$89.99"),
         ("2026-10-18", "SYNTHETIC REFUND", "Deposit", "$100.00"),
         ("2026-10-21", "CLOUD NINE CAFE - TEST", "Withdrawal", "-$42.50"),
         ("2026-10-25", "EASTSIDE HARDWARE - TEST", "Withdrawal", "-$187.11"),
         ("2026-10-31", "MONTHLY SERVICE FEE - TEST", "Fee", "-$6.00"),
         ("2026-10-31", "SYNTHETIC TRANSFER IN", "Deposit", "$1,200.00"),
         ("2026-10-31", "COMMUNITY ARTS - TEST", "Withdrawal", "-$225.00")]
HEADER = ["PAPER MOON COMMUNITY BANK", "Statement period: October 1-31, 2026", "OPENING BALANCE", "$2,450.00",
          "DEPOSITS", "$3,850.00", "WITHDRAWALS", "$1,095.42", "FEES", "$12.00", "CLOSING BALANCE", "$5,192.58",
          "DATE", "DESCRIPTION", "TYPE", "AMOUNT"]
CELL_TEXT = "\n".join(HEADER + [c for row in CELLS for c in row])
ROW_TEXT = "\n".join(HEADER[:2] + [f"  {d}       {desc:<60}{t:<14}{amt:>12}" for d, desc, t, amt in CELLS])
# Unsigned amounts with only the Type column to give the direction.
UNSIGNED_ROW_TEXT = ROW_TEXT.replace("-$", "$")


def _check_october(r):
    assert r["transactions_read"] == 14 and r["months_covered"] == 1
    assert r["period"] == {"from": "2026-10-01", "to": "2026-10-31", "days": 31}
    assert r["income_by_source"] == {"paychecks": 1800}
    assert r["excluded_monthly"] == {"transfers_between_own_accounts": 1950}
    assert r["monthly_expenses_by_category"] == {
        "Shopping": 502.11, "Uncategorized": 125, "Utilities": 123.4, "Health": 89.99, "Groceries": 64.25,
        "Transportation": 48.17, "Restaurants": 42.5, "Bank fees & interest": 12}
    # matches the statement's own reconciliation: 1,095.42 withdrawals + 12.00 fees - 100.00 refund
    assert r["average_monthly_spending"] == 1007.42
    assert r["by_month"] == [{"month": "2026-10", "income": 1800, "spending": 1107.42, "refunds": 100}]
    assert any("transfer in is actually income" in w for w in r["warnings"])


@pytest.mark.parametrize("text", [CELL_TEXT, ROW_TEXT, UNSIGNED_ROW_TEXT],
                         ids=["one-cell-per-line", "rows-with-type-column", "unsigned-with-type-column"])
def test_paper_moon_statement_layouts(monkeypatch, text):
    monkeypatch.setattr(st, "_text_from_pdf", lambda raw: [text])
    _check_october(summarize(b"%PDF-1.4 ...", "statement.pdf"))


def test_best_layout_wins_when_one_extraction_reads_nothing(monkeypatch):
    monkeypatch.setattr(st, "_text_from_pdf", lambda raw: ["garbled header only " * 5, CELL_TEXT])
    _check_october(summarize(b"%PDF-1.4 ...", "statement.pdf"))


def test_real_pdf_one_cell_per_line_end_to_end():
    pytest.importorskip("pypdf")
    _check_october(summarize(_make_pdf(CELL_TEXT.splitlines()), "statement.pdf"))
