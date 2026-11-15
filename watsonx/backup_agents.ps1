# Back up the current Pocket CFO agents from watsonx Orchestrate BEFORE importing changes.
# Run from the folder that contains `watsonx`:   .\watsonx\backup_agents.ps1
# Creates watsonx\backups\<timestamp>\<agent>.zip (agent YAML + dependencies). These files contain
# your agent instructions only - no API keys - but backups/ is git-ignored anyway.
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$agents = @(
  "Untitled_Agent_1_2610ZN",  # Pocket CFO Supervisor
  "Untitled_Agent_1_0599WM",  # Financial Analyst Agent
  "Untitled_Agent_1_0931g5",  # Budget Agent
  "Untitled_Agent_1_1216qX",  # Debt Management Agent
  "Untitled_Agent_1_7664A5",  # Savings Agent
  "Untitled_Agent_1_4588pq",  # Investment Education Agent
  "Untitled_Agent_1_6535qK"   # Financial Coach Agent
)
$dir = Join-Path $PSScriptRoot ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
foreach ($a in $agents) {
  orchestrate agents export -n $a -k native -o (Join-Path $dir "$a.zip")
  if ($LASTEXITCODE -ne 0) { throw "Export failed for $a" }
}
Write-Host "Backup saved to $dir"
Write-Host "To roll back one agent: unzip it and run  orchestrate agents import -f <the agent .yaml inside>"
