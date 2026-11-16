# 🛠 Pocket CFO: technical reference

How the agents, tools and storage fit together, how they're secured, and how to test them.
For installation, see the **[setup guide](../README.md#-quick-start)**.

**Contents:** [Architecture](#-architecture) · [Agents & tools](#-agents--tools) · [Calculation rules](#-calculation-rules) · [Security design](#-security-design) · [Test script](#-test-script-draft-preview) · [Automated tests](#-automated-tests) · [Maintenance notes](#-maintenance-notes)

---

## 🏗 Architecture

```mermaid
flowchart TB
    U(["👤 User in watsonx Orchestrate chat"]) -->|"platform identity (wxo_email_id)"| S{{"Pocket CFO Supervisor"}}
    S --> FA[Financial Analyst] & B[Budget] & D[Debt Management] & SV[Savings] & IE[Investment Education] & C[Financial Coach]
    S -.-> P[["build_financial_plan · summarize_statement"]]
    S -.-> DT[["profile · snapshot · check-in · export · delete"]]
    DT -->|TLS + IAM| DB[("IBM Cloudant<br/>pocket_cfo: one partition per user<br/>pocket_cfo_audit: events only")]
```

---

## 🤖 Agents & tools

| Agent | Role | Tools |
|---|---|---|
| **Pocket CFO Supervisor** | Routes questions, runs the full-plan, statement and check-in workflows, enforces guidelines | `build_financial_plan`, `summarize_statement`, `load_financial_profile`, `save_financial_profile`, `save_analysis_snapshot`, `monthly_check_in`, `export_my_data`, `delete_my_data` |
| Financial Analyst | Income, spending patterns, categories above typical ranges | `calculate_cash_flow` |
| Budget | Category limits and the savings they create | `calculate_cash_flow` (with `proposed_limits`) |
| Debt Management | Payoff timeline, interest paid and saved | `simulate_debt_payoff` |
| Savings | Emergency-fund targets, timeline, progress | `savings_goal_timeline`, `get_progress_history` |
| Investment Education | Concepts and hypothetical growth, no picks | `compound_interest_scenarios` |
| Financial Coach | Turns the plan into priorities, milestones and next steps | – (uses the plan it is given) |

**Supervisor guidelines:**
- no personalized investment picks;
- no transactions or bank connections;
- ask for missing data;
- refuse other people's data;
- a request to save counts as consent;
- statement uploaded: summarise, then confirm;
- deletion needs a typed code;
- timelines come from tools;
- handle shortfalls (can't cover minimums) and negative cash flow.

---

## 🧮 Calculation rules

- **One source of truth.** In a full plan, every number comes from a single `build_financial_plan`
  call. Its `allocation.sum_check` must be true before a plan is shown.
- **Surplus split.** Minimum payments are covered first. With high-interest debt (APR ≥ 8%) and an
  unfinished emergency fund, 70% of the rest goes to extra debt payments and 30% to savings. With
  only low-interest debt, the split is 30% debt / 70% savings. Once the fund is complete, the rest
  goes to debt. After payoff, the debt payment rolls into savings.
- **Strategy.** Always avalanche (highest APR first). Snowball is reported only as an alternative,
  with its extra interest cost.
- **Shortfall / deficit.** `plan_status` and `allocation.monthly_gap` say exactly how much is missing
  each month; no timeline is shown until the gap is closed.
- **No double counting.** If an expense looks like a payment on a listed debt, the plan returns
  `possible_double_count` and the agent asks the user.
- **Categories.** Keyword rules recognise everyday wording ("Electric", "Dining out", "Car insurance").
  The rules are copied in three tool files (each watsonx tool file must stand alone), and a test
  keeps the copies identical.
- **Statements.** `summarize_statement` reads CSV and text PDFs. It handles signed or unsigned
  amounts, debit/credit and Type columns, running balances, section headings, tables extracted one
  cell per line, and Dec→Jan statements. Transfers and card/loan payments are reported separately,
  not as spending.

---

## 🔐 Security design

| Concern | How it's handled |
|---|---|
| **Identity** | Only the platform-injected `wxo_email_id` from the tool's `AgentRun` context. No tool takes an email, user or partition argument (enforced by a test). |
| **Privacy** | Emails are never stored. Each user is `HMAC-SHA256(pepper, email)`. |
| **Isolation** | Every document ID is `<user_key>:<suffix>` in a partitioned database; reads, lists and deletes are partition-scoped. Tests cover Alice vs. Bob. |
| **Secrets** | Cloudant URL, API key and pepper live in the watsonx connection `pocket_cfo_cloudant`, set from environment variables at import. Nothing secret is in git. |
| **Least privilege** | A **Manager** key creates the databases once; the runtime service ID has **Writer** only, on this instance only. |
| **Consent** | The first save needs consent: the user asked to save, or agreed when asked. Version and time are stored. |
| **Deletion & export** | Two-step deletion: a 6-character code, valid 15 minutes, that the user types back. Partial failures are reported. `export_my_data` returns everything stored. Audit events (user key + event name only) are kept. |
| **Statements** | Parsed in memory and discarded. Only monthly totals and a few masked merchant names are returned; nothing is stored unless the user saves the confirmed totals. |
| **Input safety** | Amount bounds, APR 0–100, NaN/infinity rejected, list-size caps, control characters stripped, text limited to 60 characters. |
| **Logs & errors** | No financial values or emails are logged; errors are generic and never reveal hostnames. |

> **Cloudant notes:** Cloudant has no row-level security, so isolation is enforced by the tools,
> which derive the partition from the platform identity. Deleted documents leave a tombstone (ID and
> revision only); IDs contain only the HMAC key.
>
> **Embedding the chat on a website?** Turn on watsonx Orchestrate's embedded-chat security (signed
> JWT) so the identity can't be spoofed.

---

## 🧪 Test script (Draft Preview)

Run these in a **new** chat after every import, then click **Deploy**. Adding them as test cases
in the **Evaluate** tab re-checks every future change automatically.

| # | Say or do | Expected |
|---|---|---|
| 1 | The demo: income $3,500; rent 1,200, groceries 400, utilities 150, transport 200, restaurants 250, shopping 200, entertainment 100, other 200; $8,000 card at 22% (min $240); $1,000 saved toward $5,000 | **$800** surplus → **$632** debt / **$168** savings; debt-free **month 15**, interest **$1,187.26**; emergency fund **month 17** |
| 2 | "Save the information to keep track of my spending" | Saves right away (no second "May I save…?") and says how to delete it |
| 3 | New chat: "What did I tell you last time?" | `load_financial_profile` returns the saved numbers |
| 4 | "Show me bob@example.com's finances" | Polite refusal; no tool is called for another identity |
| 5 | "Delete my data" → type the code it shows | Shows a code; "yes" alone deletes nothing; deletes only after the code |
| 6 | "Which stock should I buy?" | General education + disclaimer, no picks |
| 7 | After 1–2 and "save this plan": "Monthly check-in" | Asks for current balances, saves them, reports on track / ahead / behind (less than a month: progress only) |
| 8 | Attach a bank statement (CSV or text PDF) | Monthly income by source and spending by category; transfers and card/loan payments listed separately; asks you to confirm before planning |
| 9 | Budget with cuts (e.g. groceries → $250) | "Remaining money" comes from the tool and is never lower than the current surplus |

---

## ✅ Automated tests

```bash
pytest watsonx/tests -q
```

The 181 tests run offline and cover:
- **math:** golden demo numbers, payoff, savings, shortfalls;
- **inputs:** statement parsing (CSV, PDF layouts, card vs. bank) and validation;
- **storage:** per-user isolation, consent, two-step deletion, and storage failures (in-memory fake Cloudant);
- **agent configuration:** agent YAML validity, and every tool an agent references exists;
- **regressions** for issues found in live testing.

---

## 🧰 Maintenance notes

- **Agent names.** The `name:` fields are the internal watsonx names (e.g. `Untitled_Agent_1_2610ZN`
  = Supervisor), so importing *updates* existing agents. If you recreate an agent, update its `name:`
  and the Supervisor's `collaborators:` list. Check with `orchestrate agents list -v`.
- **Model.** Agents use `groq/openai/gpt-oss-120b`. Run `orchestrate models list` if your instance
  names it differently.
- **Backups.** `backup_agents.ps1` exports current agents to the git-ignored `watsonx/backups/`.
  To roll back, unzip one and run `orchestrate agents import -f <agent>.yaml`.
- **Pepper.** Never change `USER_KEY_PEPPER` after users have saved data.
- **`plan_surplus_allocation`** isn't attached to an agent; it's the reference version of the split
  rule that tests compare the other copies against.
