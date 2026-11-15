#!/usr/bin/env bash
# Import Pocket CFO (connection, tools, agents) into your active watsonx Orchestrate environment.
#
# Prerequisites (once):
#   pip install --upgrade ibm-watsonx-orchestrate
#   orchestrate env add -n pocket-cfo -u <your-instance-url> --type ibm_iam
#   orchestrate env activate pocket-cfo            # paste your watsonx API key when prompted
#   python db/setup_cloudant.py                    # creates the Cloudant databases (see README)
#
# Secrets are read from environment variables so they never land in files or git history:
#   export CLOUDANT_URL="https://<account>.cloudantnosqldb.appdomain.cloud"
#   export CLOUDANT_RUNTIME_APIKEY="<service ID key with Writer role only>"
#   export USER_KEY_PEPPER="$(openssl rand -hex 32)"   # generate ONCE, store in a password manager
set -euo pipefail
cd "$(dirname "$0")"

APP_ID=pocket_cfo_cloudant
: "${CLOUDANT_URL:?set CLOUDANT_URL}"
: "${CLOUDANT_RUNTIME_APIKEY:?set CLOUDANT_RUNTIME_APIKEY}"
: "${USER_KEY_PEPPER:?set USER_KEY_PEPPER}"

echo "==> Connection ${APP_ID} (team credentials, draft + live)"
orchestrate connections add -a "$APP_ID" 2>/dev/null || echo "   (connection already exists)"
for ENV in draft live; do
  orchestrate connections configure -a "$APP_ID" --env "$ENV" --type team --kind key_value
  orchestrate connections set-credentials -a "$APP_ID" --env "$ENV" \
    -e "CLOUDANT_URL=${CLOUDANT_URL}" \
    -e "CLOUDANT_APIKEY=${CLOUDANT_RUNTIME_APIKEY}" \
    -e "USER_KEY_PEPPER=${USER_KEY_PEPPER}"
done

echo "==> Calculation tools"
for t in build_financial_plan calculate_cash_flow plan_surplus_allocation simulate_debt_payoff savings_goal_timeline compound_interest_scenarios; do
  orchestrate tools import -k python -f "tools/${t}.py" -r tools/requirements.txt
done

echo "==> Data tools (Cloudant)"
orchestrate tools import -k python -f tools/pocket_cfo_data_tools.py -r tools/requirements.txt -a "$APP_ID"

echo "==> Specialist agents"
for a in financial_analyst_agent budget_agent debt_management_agent savings_agent investment_education_agent financial_coach_agent; do
  orchestrate agents import -f "agents/${a}.yaml"
done

echo "==> Supervisor (last, because it references the specialists)"
orchestrate agents import -f agents/pocket_cfo_supervisor.yaml

echo "Done. Test in Draft Preview (see README test script), then Create version -> Deploy."
