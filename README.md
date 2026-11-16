# Pocket CFO

A multi-agent personal finance coach on **IBM watsonx Orchestrate**: one Supervisor and six
specialist agents (Financial Analyst, Budget, Debt Management, Savings, Investment Education,
Financial Coach). Python tools do every calculation, so the agents explain numbers instead of
guessing them. Users can type their figures or upload a bank / credit-card statement (CSV or PDF),
and can choose to save their profile in **IBM Cloudant**, isolated per user.

Design, security model and the full test script are in [watsonx/README.md](watsonx/README.md).

```
watsonx/
├─ agents/      7 agent definitions (YAML)
├─ tools/       Python tools: plan, cash flow, debt payoff, savings, statement reader, Cloudant data tools
├─ tests/       pytest suite (runs offline, no credentials needed)
├─ db/          one-time Cloudant setup and a connection check
├─ import_all.ps1 / import_all.sh    upload tools + agents to watsonx Orchestrate
├─ deploy_pocket_cfo.ps1             guided version of the above for Windows
└─ backup_agents.ps1                 download the current agents before you overwrite them
```

---

## Set it up on your computer

### What you need
- **Python 3.11** or newer, and **git**
- **Windows:** PowerShell 7 (`pwsh`). **macOS/Linux:** bash
- An **IBM Cloud** account with a **watsonx Orchestrate** instance and a **Cloudant** instance
  (the Cloudant Lite plan is enough). You only need these for step 4 onward.

### 1. Get the code
```bash
git clone https://github.com/Kay116/Pocket_CFO_ai_agent.git
cd Pocket_CFO_ai_agent
```

### 2. Create a virtual environment and install the packages
```bash
python -m venv .venv
# Windows:      .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install --upgrade ibm-watsonx-orchestrate ibmcloudant pypdf==5.1.0 pytest
```

### 3. Run the tests (no IBM account needed)
```bash
pytest watsonx/tests -q
```
All tests should pass. They use an in-memory fake of Cloudant and sample statements, so this is
the quickest way to check your setup.

### 4. Set up Cloudant (once)
1. In IBM Cloud, open your Cloudant instance → **Manage → Authentication method** → choose **IAM only**.
2. Copy its URL: **Manage → Overview → External endpoint** (starts with `https://`). Use the
   *external/public* endpoint, not a private one.
3. Create the databases with a key that has the **Manager** role (your own IBM Cloud API key works):
   ```bash
   # Windows PowerShell: $env:CLOUDANT_URL = "https://..." ; $env:CLOUDANT_APIKEY = "..."
   export CLOUDANT_URL="https://<your-account>.cloudantnosqldb.appdomain.cloud"
   export CLOUDANT_APIKEY="<setup key>"
   python watsonx/db/setup_cloudant.py
   ```
4. Create the **runtime** key the agents will use: IBM Cloud → **Manage → Access (IAM) → Service IDs**
   → create one (e.g. `pocket-cfo-runtime`) → give it **Cloudant → this instance → Writer** →
   **API keys → Create**. Optionally check it: set `CLOUDANT_RUNTIME_APIKEY` and run
   `python watsonx/db/check_cloudant.py`.
5. Generate a **pepper** once and keep it in a password manager. It turns users' emails into
   anonymous IDs; **if it ever changes, saved users can no longer find their data.**
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

### 5. Connect to watsonx Orchestrate (once)
1. Get your instance URL: watsonx Orchestrate → profile icon → **Settings → API details**.
2. Create an API key: IBM Cloud → **Manage → Access (IAM) → API keys → Create**, then click **Copy**
   in the pop-up. The key is shown only once. The `ApiKey-…` value in the list is its *ID*, not the key.
3. Register the environment (pick any name, e.g. `pocket-cfo`):
   ```bash
   orchestrate env add -n pocket-cfo -u <instance-url> --type ibm_iam
   ```

### 6. Upload the tools and agents
**Windows (guided, recommended).** It asks you to copy each secret and press Enter, checks the API key
with IBM, backs up any existing agents, then imports everything. Secrets are never printed:
```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\watsonx\deploy_pocket_cfo.ps1 -EnvName pocket-cfo
```

**macOS / Linux:**
```bash
orchestrate env activate pocket-cfo        # paste the API key when asked
export CLOUDANT_URL="https://<your-account>.cloudantnosqldb.appdomain.cloud"
export CLOUDANT_RUNTIME_APIKEY="<runtime key from step 4>"
export USER_KEY_PEPPER="<pepper from step 4>"
bash watsonx/import_all.sh
```

### 7. Try it, then publish
1. In watsonx Orchestrate, open **Pocket CFO Supervisor** and start a **new** chat in Draft Preview.
2. Run the test script in [watsonx/README.md](watsonx/README.md#test-script-draft-preview), for
   example the demo budget, "save my information", uploading a statement, and "delete my data".
3. When the draft behaves correctly, click **Deploy** so the live agent uses it.

After any change to `agents/` or `tools/`, run step 6 again and test in a **new** chat. An old
chat may keep the previous behaviour.

---

## Keep secrets out of git
- Secrets live only in environment variables and in the watsonx connection `pocket_cfo_cloudant`.
  Never put them in a file in this repo. [.env.example](.env.example) lists the names only.
- `.gitignore` already excludes `.env`, keys, agent backups, Python caches, editor/assistant
  folders, and statement files (`*.pdf`, `*.csv`), so a test statement can't be committed by accident.
- If a key ever appears in a terminal, chat or screenshot, **delete it in IBM Cloud and create a new one**.

## Troubleshooting
| Message | What it means / what to do |
|---|---|
| `BXNIM0415E Provided API key could not be found` | Not a valid key: usually the key's ID was copied, or the key was deleted. Create a new one and click **Copy** in the pop-up. |
| `Error getting IBM_IAM Token` | Same cause as above, or the key is from a different IBM Cloud account than the Orchestrate instance. |
| `The token found for environment ... is missing or expired` | Run `orchestrate env activate <name>` again (or the deploy script). |
| `A connection with the App ID 'pocket_cfo_cloudant' already exists` | Normal on a second import; the script updates its credentials and continues. |
| `.\watsonx\...ps1 is not recognized` | You are inside `watsonx` already: run `.\import_all.ps1` (without `watsonx\`). |
| Agent still behaves the old way | The change wasn't imported, or the chat is old. Re-run step 6, then open a new chat. |
| "Unable to read any transactions" from a PDF | Scanned (image) PDFs can't be read. Download the statement from online banking as CSV or as a text PDF. |

## Disclaimer
Pocket CFO gives general education and estimates, not personalized financial or investment advice.
