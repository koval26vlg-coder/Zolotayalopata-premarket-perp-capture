[CmdletBinding()]
param(
    [switch]$ScheduledTick,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'src\announcement_discovery.py'
$bundledPython = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = if ($env:PREMARKET_ANNOUNCEMENT_PYTHON) {
    $env:PREMARKET_ANNOUNCEMENT_PYTHON
} elseif (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

function Write-TerminalJson {
    param([Parameter(Mandatory = $true)][object]$Payload)
    $Payload | ConvertTo-Json -Compress -Depth 32
}

if (-not $ScheduledTick) {
    Write-TerminalJson ([ordered]@{
        status = 'DISCOVERY_NOT_RUN_SCHEDULED_TICK_REQUIRED'
        announcement_requests = 0
        appended_candidates = 0
        capture_authorized = $false
    })
    exit 2
}

Push-Location $projectRoot
try {
    & $python $runtime --scheduled-tick --json
    exit $LASTEXITCODE
} catch {
    Write-TerminalJson ([ordered]@{
        status = 'RETRY_NEXT_INTERVAL'
        reason = $_.Exception.Message
        announcement_requests = 0
        appended_candidates = 0
        pending_retry = $true
        capture_authorized = $false
    })
    exit 2
} finally {
    Pop-Location
}
