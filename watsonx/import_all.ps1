# Import Pocket CFO (connection, tools, agents) into the ACTIVE watsonx Orchestrate environment.
# Windows PowerShell version of import_all.sh. Run from the folder that contains `watsonx`:
#
#   orchestrate env activate <your-env-name>      # if the token expired (see: orchestrate env list)
#   (or run .\watsonx\deploy_pocket_cfo.ps1 -EnvName <your-env-name>, which does all of this for you)
#   $env:CLOUDANT_URL            = "https://<account>.cloudantnosqldb.appdomain.cloud"
#   $env:CLOUDANT_RUNTIME_APIKEY = "<service ID key with Writer role>"
#   $env:USER_KEY_PEPPER         = "<64-char pepper saved in your password manager>"
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\watsonx\backup_agents.ps1                    # recommended first
#   .\watsonx\import_all.ps1
#
# Secrets are read from environment variables only; nothing is written to disk.
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location $PSScriptRoot

foreach ($v in "CLOUDANT_URL", "CLOUDANT_RUNTIME_APIKEY", "USER_KEY_PEPPER") {
  if (-not [Environment]::GetEnvironmentVariable($v)) { throw "Set `$env:$v first (see the top of this script)." }
}
if ($env:USER_KEY_PEPPER.Length -lt 32) { throw "USER_KEY_PEPPER looks too short; generate a 64-character value." }

function Run($cmd) {
  Write-Host "   > $($cmd -replace [regex]::Escape($env:CLOUDANT_RUNTIME_APIKEY), '***' -replace [regex]::Escape($env:USER_KEY_PEPPER), '***')"
  Invoke-Expression $cmd
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE)" }
}

$app = "pocket_cfo_cloudant"
Write-Host "==> Connection $app (team credentials, draft + live)"
orchestrate connections add -a $app *> $null   # *> hides the CLI's "already exists" [ERROR] line
if ($LASTEXITCODE -ne 0) { Write-Host "   (connection already exists - updating its credentials; this is normal)" }
foreach ($envName in "draft", "live") {
  Run "orchestrate connections configure -a $app --env $envName --type team --kind key_value"
  orchestrate connections set-credentials -a $app --env $envName `
    -e "CLOUDANT_URL=$($env:CLOUDANT_URL)" `
    -e "CLOUDANT_APIKEY=$($env:CLOUDANT_RUNTIME_APIKEY)" `
    -e "USER_KEY_PEPPER=$($env:USER_KEY_PEPPER)"
  if ($LASTEXITCODE -ne 0) { throw "set-credentials failed for $envName" }
  Write-Host "   > credentials set for $envName (values hidden)"
}

Write-Host "==> Calculation tools"
foreach ($t in "build_financial_plan", "summarize_statement", "calculate_cash_flow", "plan_surplus_allocation", "simulate_debt_payoff", "savings_goal_timeline", "compound_interest_scenarios") {
  Run "orchestrate tools import -k python -f tools/$t.py -r tools/requirements.txt"
}

Write-Host "==> Data tools (Cloudant)"
Run "orchestrate tools import -k python -f tools/pocket_cfo_data_tools.py -r tools/requirements.txt -a $app"

Write-Host "==> Specialist agents (updates your existing agents by internal name)"
foreach ($a in "financial_analyst_agent", "budget_agent", "debt_management_agent", "savings_agent", "investment_education_agent", "financial_coach_agent") {
  Run "orchestrate agents import -f agents/$a.yaml"
}

Write-Host "==> Supervisor (last, because it references the specialists)"
Run "orchestrate agents import -f agents/pocket_cfo_supervisor.yaml"

Write-Host ""
Write-Host "Done. Open Pocket CFO Supervisor in watsonx, run the README test script in Draft Preview,"
Write-Host "then Create version -> Deploy."
