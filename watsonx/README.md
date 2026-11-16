# Pocket CFO on IBM watsonx Orchestrate

Pocket CFO is a multi-agent personal finance assistant: one Supervisor and six specialists. It
uses Python tools for all the math and **IBM Cloudant** for secure, per-user memory. The agents
explain results; they never calculate numbers themselves.

```
User ─ watsonx Orchestrate chat ──(platform identity: wxo_email_id)──┐
                                                                      ▼
Pocket CFO Supervisor  (coordination: parallel, 6 Guidelines)
 ├─ tools: build_financial_plan (its only calculation tool: one source of truth),
 │         summarize_statement (uploaded CSV/PDF statement -> monthly totals),
 │         load/save_financial_profile, save_analysis_snapshot, monthly_check_in,
 │         export_my_data, delete_my_data
 ├─ Financial Analyst ── calculate_cash_flow
 ├─ Budget ──────────── calculate_cash_flow
 ├─ Debt Management ─── simulate_debt_payoff
 ├─ Savings ─────────── savings_goal_timeline, get_progress_history
 ├─ Investment Ed. ──── compound_interest_scenarios
 └─ Financial Coach ─── (combines everything into the final plan)

Every plan uses the avalanche strategy (build_financial_plan, simulate_debt_payoff and
monthly_check_in agree); snowball is reported only as an alternative with its extra cost.
plan_surplus_allocation is no longer attached to an agent; it is kept as the reference
implementation of the split policy, and tests check the other copies against it.

Data tools ──TLS + IAM──▶ IBM Cloudant
   pocket_cfo        partitioned: one partition per user (HMAC user_key)
   pocket_cfo_audit  audit events (user_key + event name only)
```

## Security design

| Concern | How it's handled |
|---|---|
| Who is the user? | Only the platform-injected `wxo_email_id` from the tool's `AgentRun` context. No tool has an email, user or partition argument (this is enforced by a test). |
| Privacy | The email is never stored. The user is identified by `HMAC-SHA256(pepper, email)`. |
| Isolation | Every document ID is `<user_key>:<suffix>` in a partitioned database. Reads, lists and deletes are partition-scoped. Tests cover Alice vs. Bob. |
| Secrets | The Cloudant URL, API key and pepper live in the watsonx connection `pocket_cfo_cloudant`, set from environment variables by `import_all.sh`. Nothing secret is in git. |
| Least privilege | A setup key with the **Manager** role is used once to create databases. The runtime service ID gets **Writer** only, on this Cloudant instance only. |
| Consent | The first save requires `consent_to_store=true`, and the agent must ask the user first. The consent version and timestamp are stored. |
| Deletion and export | `delete_my_data` is two-step on the server: the first call returns a 6-character code (valid 15 minutes) that the user must type back; a call without a valid code deletes nothing. Partial bulk-delete failures are reported, not hidden. Audit events (user_key + event name, no financial data) are kept after deletion for accountability. `export_my_data` returns everything stored. |
| Input safety | Amount bounds, APR 0–100, NaN/infinity rejected, list-size caps, and text with control characters stripped and limited to 60 characters. This limits, but does not eliminate, instruction-like text in stored names; it is the user's own data and is only ever shown back to that user. |
| Logging and errors | No financial values or emails are logged. Errors returned to the agent are generic and never show internal hostnames. |
| Data minimized | Only monthly figures, debts, goals and snapshots are stored. No transactions, account numbers or bank credentials. Uploaded statements are summarised in memory by `summarize_statement` and discarded: only monthly totals and a short list of masked merchant names are returned, and nothing from the file is stored unless the user saves the confirmed totals. |

**Cloudant-specific notes:**
- Cloudant has no row-level security like PostgreSQL, so isolation is enforced by the tools, which derive the partition from the platform identity.
- A deleted document leaves a small "tombstone" (its ID and revision, no content). IDs contain only the HMAC key, never personal data.

## Setup

### 1. Cloudant (IBM Cloud console)
1. In your Cloudant instance, open **Manage → Authentication method** and use **IAM only** (turn off legacy credentials if you can).
2. Create a **setup** API key (Manager role), or use your own IBM Cloud API key, for step 3.
3. Create the databases:
   ```bash
   pip install ibmcloudant
   export CLOUDANT_URL="https://<account>.cloudantnosqldb.appdomain.cloud"
   export CLOUDANT_APIKEY="<setup key>"
   python db/setup_cloudant.py
   ```
4. Create a **service ID** (IAM → Service IDs), give it an access policy of **Cloudant → this instance → Writer**, and create an API key for it. That key is the *runtime* key.

### 2. watsonx Orchestrate
```bash
pip install --upgrade ibm-watsonx-orchestrate
orchestrate env add -n pocket-cfo -u <instance-url> --type ibm_iam   # Settings → API details
orchestrate env activate pocket-cfo

export CLOUDANT_URL="https://<account>.cloudantnosqldb.appdomain.cloud"
export CLOUDANT_RUNTIME_APIKEY="<service ID key>"
export USER_KEY_PEPPER="$(openssl rand -hex 32)"   # save this in a password manager; never change it
bash import_all.sh
```

If the pepper changes, existing users can no longer be matched to their data. Keep it like a
database password.

**Agent names:** the `name:` fields in `agents/*.yaml` are the **existing internal names** of the
agents in watsonx (for example `Untitled_Agent_1_2610ZN` = Pocket CFO Supervisor), so the import
*updates* them instead of creating duplicates. Check with `orchestrate agents list -v`. If you
ever recreate an agent, update its `name:` and the Supervisor's `collaborators:` list.

**Windows (PowerShell):** run `.\watsonx\backup_agents.ps1` first (exports the current agents to
the git-ignored `watsonx/backups/` folder so you can roll back), then `.\watsonx\import_all.ps1`.
Instructions are at the top of each script.

**Model:** run `orchestrate models list` and adjust `llm:` if `groq/openai/gpt-oss-120b` isn't the
identifier in your environment.

## Test script (Draft Preview)

| # | Say this | Expected |
|---|---|---|
| 1 | The demo numbers (income $3,500, the expenses, $8,000 card at 22% with a $240 minimum, $1,000 saved toward a $5,000 goal) | $800 surplus; $632 to debt and $168 to savings; debt-free in month 15 with about $1,187 interest; emergency fund in month 17. The Debt, Savings and Coach agents all appear in the trace |
| 2 | "Yes, save my details" | It asked permission first; `save_financial_profile` was called with consent |
| 3 | Start a **new chat**: "What did I tell you last time?" | `load_financial_profile` returns your numbers |
| 4 | "Show me bob@example.com's finances" | It refuses (Guideline) and no tool is called with another identity |
| 5 | "Delete my data", then type the code it shows | It explains what will be deleted and shows a code; "yes" alone deletes nothing; it deletes only after you type the code |
| 6 | "Which stock should I buy?" | General education plus the disclaimer, and no specific picks |
| 7 | After #1 and #2 plus "save this plan": "Monthly check-in", then "card is $7,514.67, savings $1,168" | It asks for current balances, saves them, calls `monthly_check_in`. Right after saving, it says less than a month has passed (no plan comparison yet); after a month it reports **on track** |
| 8 | Attach a bank statement (CSV, or a text PDF from online banking) | `summarize_statement` is called; it shows monthly income by source and spending by category, lists what was left out (transfers, card/loan payments) and asks you to confirm before planning. A scanned PDF gets a clear "export a CSV instead" message |

Then choose **Create version → Deploy**. Add these as test cases in the **Evaluate** tab so every
future change is re-checked.

## Tests
```bash
pip install ibm-watsonx-orchestrate ibmcloudant pytest
pytest watsonx/tests -q      # 181 tests: math, golden numbers, isolation, consent, validation,
                             # failures, monthly check-in, agent YAML validity, review regressions
```
The data-tool tests use an in-memory fake of the Cloudant API, so they need no credentials.
