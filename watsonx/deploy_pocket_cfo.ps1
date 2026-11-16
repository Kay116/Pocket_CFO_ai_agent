# Guided deploy of Pocket CFO to watsonx Orchestrate (Windows, PowerShell 7).
#
#   pwsh -NoProfile -ExecutionPolicy Bypass -File .\watsonx\deploy_pocket_cfo.ps1 -EnvName <your-env-name>
#
# <your-env-name> is the name you gave with `orchestrate env add -n <name> ...` (see `orchestrate env list`).
# For each secret the script asks you to COPY it, then press Enter; it reads the clipboard.
# Secrets are never printed, written to disk or stored in your command history.
param(
  [Parameter(Mandatory = $true, HelpMessage = "Your orchestrate environment name (see: orchestrate env list)")]
  [string]$EnvName
)
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$here = $PSScriptRoot   # the watsonx folder this script lives in

function Ask($what, $check, $hint) {
  while ($true) {
    $null = Read-Host "`nCOPY your $what now, then press Enter here"   # $null: don't return the Enter as a value
    $v = ((Get-Clipboard -Raw) -as [string]).Trim()
    if ((& $check $v) -eq $true) { Write-Host "   OK ($what received, length $($v.Length))" -ForegroundColor Green; return $v }
    Write-Host "   That doesn't look right: $hint. Try again." -ForegroundColor Yellow
  }
}

function Test-IamKey($v) {
  # Ask IBM directly whether this key exists. Prints IBM's verdict; never prints the key.
  if ($v.Length -lt 20 -or $v -match "\s") { Write-Host "   Not a key (too short or has spaces)." -ForegroundColor Yellow; return $false }
  if ($v -like "ApiKey-*") { Write-Host "   This is the key's ID, not the key. Copy the key from the Create pop-up." -ForegroundColor Yellow; return $false }
  try {
    Invoke-RestMethod -Method Post -Uri "https://iam.cloud.ibm.com/identity/token" `
      -Body @{ grant_type = "urn:ibm:params:oauth:grant-type:apikey"; apikey = $v } | Out-Null
    Write-Host "   IBM says: key is VALID." -ForegroundColor Green
    return $true
  } catch {
    $m = $null; try { $m = $_.ErrorDetails.Message | ConvertFrom-Json } catch {}
    if ($m) { Write-Host "   IBM says: REJECTED - $($m.errorCode) $($m.errorMessage)" -ForegroundColor Yellow }
    else { Write-Host "   Could not reach IBM: $($_.Exception.Message)" -ForegroundColor Yellow }
    return $false
  }
}

Write-Host "STEP 1 of 4: log in to watsonx Orchestrate (environment '$EnvName')" -ForegroundColor Cyan
Write-Host "   Key: IBM Cloud -> Manage -> Access (IAM) -> API keys -> Create, then Copy in the pop-up"
$key = Ask "IBM Cloud API key" { param($v) Test-IamKey $v } "create a NEW key in IBM Cloud -> Manage -> Access (IAM) -> API keys -> Create, click Copy in the pop-up, then press Enter again"
$out = & orchestrate env activate $EnvName "--api-key=$key" 2>&1 | Out-String
$code = $LASTEXITCODE
$safe = $out.Replace($key, "***")   # never show the key, even inside an error message from the CLI
Remove-Variable key
if ($code -ne 0) {
  Write-Host ($safe.Trim().Split("`n") | Select-Object -Last 6 | Out-String) -ForegroundColor Yellow
  throw "Login failed (details above, key hidden). Check the environment name with 'orchestrate env list'."
}
Write-Host "   Logged in." -ForegroundColor Green

Write-Host "`nSTEP 2 of 4: Cloudant secrets" -ForegroundColor Cyan
$env:CLOUDANT_URL = Ask "Cloudant URL (External endpoint)" { param($v) $v -match "^https://\S+$" } "it should start with https://"
$env:CLOUDANT_RUNTIME_APIKEY = Ask "Cloudant RUNTIME API key" { param($v) $v.Length -ge 20 -and $v -notmatch "\s" -and $v -ne $env:CLOUDANT_URL } "it should be one long string, not the URL"
$env:USER_KEY_PEPPER = Ask "USER_KEY_PEPPER (from your password manager; the SAME one every time)" { param($v) $v.Length -ge 32 -and $v -ne $env:CLOUDANT_RUNTIME_APIKEY -and $v -ne $env:CLOUDANT_URL } "it should be your 64-character pepper, different from the key"

Set-Location $here
Write-Host "`nSTEP 3 of 4: backing up the current agents (to the git-ignored backups folder)" -ForegroundColor Cyan
try { & .\backup_agents.ps1 }
catch { Write-Host "   No backup made (normal on a first import, when the agents don't exist yet)." -ForegroundColor Yellow }

Write-Host "`nSTEP 4 of 4: importing tools and agents" -ForegroundColor Cyan
& .\import_all.ps1

Write-Host "`nDONE. Open watsonx Orchestrate, start a NEW chat in Draft Preview and test." -ForegroundColor Green
