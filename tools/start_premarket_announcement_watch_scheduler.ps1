[CmdletBinding()]
param(
    [switch]$ScheduledTick,
    [switch]$Status,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'src\announcement_watch_scheduler.py'
$python = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Pinned Python runtime not found: $python"
}
$resolvedPython = (Resolve-Path -LiteralPath $python).Path
if (-not [string]::Equals(
    $resolvedPython,
    $python,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Pinned Python runtime resolved unexpectedly: $resolvedPython"
}

if ($ScheduledTick.IsPresent -eq $Status.IsPresent) {
    throw 'Choose exactly one mode: -ScheduledTick or -Status.'
}

$runtimeArgs = @()
if ($ScheduledTick) { $runtimeArgs += '--scheduled-tick' }
if ($Status) { $runtimeArgs += '--status' }
if ($Json) { $runtimeArgs += '--json' }

Push-Location $projectRoot
try {
    & $python $runtime @runtimeArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
