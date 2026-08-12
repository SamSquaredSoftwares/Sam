<#
.SYNOPSIS
    Remove Sam Squared managed subagents from the Claude Code managed-settings
    directory on Windows.

.DESCRIPTION
    Removes only the agent files shipped in .\agents from
    "C:\Program Files\ClaudeCode\.claude\agents". Any other files in that
    directory, and backups created by install.ps1 (*.backup-*), are left in place.

    Run from an elevated (Administrator) PowerShell prompt.

.PARAMETER DryRun
    Show what would be removed; make no changes.

.PARAMETER ManagedDir
    Override the managed-settings directory (advanced/testing).

.EXAMPLE
    .\uninstall.ps1

.EXAMPLE
    .\uninstall.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$ManagedDir = "$env:ProgramFiles\ClaudeCode"
)

$ErrorActionPreference = 'Stop'

$ScriptDir = $PSScriptRoot
$AgentsSrc = Join-Path $ScriptDir 'agents'
$Dest      = Join-Path $ManagedDir '.claude\agents'

function Write-Info { param([string]$Msg) Write-Host "  $Msg" }

if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
    Write-Host "Nothing to do: managed agents directory does not exist ($Dest)."
    exit 0
}

$Shipped = @(Get-ChildItem -LiteralPath $AgentsSrc -Filter '*.md' -File)
if ($Shipped.Count -eq 0) {
    Write-Error "no shipped agent files found in $AgentsSrc; cannot determine what to remove."
    exit 1
}

Write-Host 'Removing Sam Squared managed subagents from:'
Write-Host "  $Dest"
if ($DryRun) { Write-Host '  mode: DRY RUN (no changes will be made)' }
Write-Host ''

$removed = 0
foreach ($f in $Shipped) {
    $target = Join-Path $Dest $f.Name
    if (Test-Path -LiteralPath $target) {
        Write-Info "remove $($f.Name)"
        if (-not $DryRun) { Remove-Item -LiteralPath $target -Force }
        $removed++
    } else {
        Write-Info "skip   $($f.Name) (not installed)"
    }
}

# Remove the agents directory if it is now empty.
if (-not $DryRun) {
    if (@(Get-ChildItem -LiteralPath $Dest -Force).Count -eq 0) {
        Remove-Item -LiteralPath $Dest -Force
    }
}

Write-Host ''
if ($DryRun) {
    Write-Host "Dry run complete. $removed agent(s) would be removed."
} else {
    Write-Host "Done. Removed $removed managed subagent(s)."
}
