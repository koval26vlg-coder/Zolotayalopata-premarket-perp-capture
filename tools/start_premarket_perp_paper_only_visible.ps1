[CmdletBinding()]
param(
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$activePlan = Join-Path $projectRoot 'docs\plans\premarket-perp-capture-planonly-20260822-v34.json'
$paperRuntime = Join-Path $projectRoot 'src\paper_replay.py'
$registryRuntime = Join-Path $projectRoot 'src\event_registry.py'
$gateRuntime = Join-Path $projectRoot 'src\risk_gate.py'
$paperOutput = if ($env:PREMARKET_PAPER_OUTPUT_ROOT) {
    $env:PREMARKET_PAPER_OUTPUT_ROOT
} else {
    Join-Path $projectRoot 'docs\paper'
}

$bundledPython = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = if ($env:PREMARKET_PAPER_PYTHON) {
    $env:PREMARKET_PAPER_PYTHON
} elseif (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

function Write-TerminalJson {
    param([Parameter(Mandatory = $true)][object]$Payload)
    $Payload | ConvertTo-Json -Compress -Depth 32
}

if (-not (Test-Path -LiteralPath $activePlan -PathType Leaf)) {
    Write-TerminalJson ([ordered]@{
        status = 'PAPER_NOT_RUN_ACTIVE_PLAN_MISSING'
        virtual_positions_created = 0
        capture_started = $false
        paper_broker_execution = $false
        acceptance_capable = $false
    })
    exit 2
}

Push-Location $projectRoot
try {
    $null = & $python $gateRuntime --plan-check
    if ($LASTEXITCODE -ne 0) { throw 'PlanOnly verification failed' }
    $null = & $python $gateRuntime --capability-scan
    if ($LASTEXITCODE -ne 0) { throw 'capability scan failed' }

    $candidateRaw = & $python $registryRuntime --candidate-status
    if ($LASTEXITCODE -ne 0) { throw 'candidate-status verification failed' }
    $candidate = $candidateRaw | ConvertFrom-Json

    if ($candidate.status -eq 'NO_SECONDS_GRADE_CANDIDATE') {
        $paperRaw = $candidateRaw | & $python $paperRuntime `
            --candidate-stdin --output-dir $paperOutput --json
        $paperExit = $LASTEXITCODE
        $paper = $paperRaw | ConvertFrom-Json
        if ($paper.status -ne 'NO_ELIGIBLE_EVENT') {
            throw 'paper runtime did not preserve the no-event boundary'
        }
        Write-TerminalJson $paper
        exit $paperExit
    }

    # v34 remains capture-disabled. Even when discovery later yields a human-reviewed
    # official candidate, this launcher only evaluates already sealed evidence; it
    # never invokes market_data_capture, consumes a capture-token, or calls Start-Process.
    $paperRaw = $candidateRaw | & $python $paperRuntime `
        --candidate-stdin --output-dir $paperOutput --json
    $paperExit = $LASTEXITCODE
    $paper = $paperRaw | ConvertFrom-Json
    Write-TerminalJson $paper
    exit $paperExit
} catch {
    Write-TerminalJson ([ordered]@{
        status = 'PAPER_NOT_RUN_PREFLIGHT_FAILED'
        reason = $_.Exception.Message
        virtual_positions_created = 0
        capture_started = $false
        paper_broker_execution = $false
        acceptance_capable = $false
    })
    exit 2
} finally {
    Pop-Location
}
