<#
.SYNOPSIS
    Deploy Sam Squared managed subagents into the Claude Code managed-settings
    directory on Windows.

.DESCRIPTION
    Copies the agent definitions in .\agents into
    "C:\Program Files\ClaudeCode\.claude\agents". Managed subagents are picked up
    by every Claude Code session on this machine and take precedence over project
    (.claude\agents) and user (~\.claude\agents) subagents with the same name.

    Run from an elevated (Administrator) PowerShell prompt, since the default
    target lives under Program Files.

.PARAMETER DryRun
    Show what would happen; make no changes.

.PARAMETER Force
    Overwrite existing managed agents without backing them up first.

.PARAMETER ManagedDir
    Override the managed-settings directory (advanced/testing).

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$ManagedDir = "$env:ProgramFiles\ClaudeCode"
)

$ErrorActionPreference = 'Stop'

$ScriptDir  = $PSScriptRoot
$AgentsSrc  = Join-Path $ScriptDir 'agents'
$Validator  = Join-Path $ScriptDir 'scripts\validate_agents.py'
$Dest       = Join-Path $ManagedDir '.claude\agents'

function Write-Info { param([string]$Msg) Write-Host "  $Msg" }

# --- Verify source -----------------------------------------------------------
if (-not (Test-Path -LiteralPath $AgentsSrc -PathType Container)) {
    Write-Error "agents source directory not found: $AgentsSrc"
    exit 1
}
$AgentFiles = @(Get-ChildItem -LiteralPath $AgentsSrc -Filter '*.md' -File)
if ($AgentFiles.Count -eq 0) {
    Write-Error "no agent .md files found in $AgentsSrc"
    exit 1
}

Write-Host 'Sam Squared - managed subagents installer'
Write-Host "  source : $AgentsSrc ($($AgentFiles.Count) agents)"
Write-Host "  target : $Dest"
if ($DryRun) { Write-Host '  mode   : DRY RUN (no changes will be made)' }
Write-Host ''

# --- Admin check -------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    Write-Warning 'Not running as Administrator. Writing under Program Files will likely fail.'
    Write-Warning 'Re-run this script from an elevated PowerShell prompt.'
}

# --- Pre-flight validation ---------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($python -and (Test-Path -LiteralPath $Validator)) {
    Write-Host 'Validating agent definitions...'
    & $python.Source $Validator $AgentsSrc
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'validation failed; aborting install.'
        exit 1
    }
    Write-Host ''
} else {
    Write-Info 'python/validator unavailable - skipping deep validation.'
}

# --- Backup existing managed agents ------------------------------------------
if ((Test-Path -LiteralPath $Dest) -and -not $Force) {
    $existing = @(Get-ChildItem -LiteralPath $Dest -Filter '*.md' -File)
    if ($existing.Count -gt 0) {
        $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = "$Dest.backup-$stamp"
        Write-Host "Backing up $($existing.Count) existing managed agent(s) -> $backup"
        if (-not $DryRun) {
            New-Item -ItemType Directory -Path $backup -Force | Out-Null
            Copy-Item -LiteralPath $existing.FullName -Destination $backup
        }
    }
}

# --- Install -----------------------------------------------------------------
Write-Host "Installing $($AgentFiles.Count) managed subagent(s):"
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
}
foreach ($f in $AgentFiles) {
    Write-Info $f.Name
    if (-not $DryRun) {
        Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $Dest $f.Name) -Force
    }
}

Write-Host ''
if ($DryRun) {
    Write-Host 'Dry run complete. No changes were made.'
} else {
    Write-Host "Done. $($AgentFiles.Count) managed subagent(s) installed to:"
    Write-Host "  $Dest"
    Write-Host ''
    Write-Host 'Verify from any project with the Claude Code CLI:'
    Write-Host '  claude   # then run /agents, or ask "which subagents are available?"'
    Write-Host ''
    Write-Host 'These managed agents override project and user agents of the same name.'
}
