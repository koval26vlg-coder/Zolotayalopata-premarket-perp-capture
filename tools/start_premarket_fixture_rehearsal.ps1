param(
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'src\fixture_rehearsal.py'
$riskGate = Join-Path $projectRoot 'src\risk_gate.py'
$python = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Pinned Python runtime not found: $python"
}
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    throw "Fixture rehearsal runtime not found: $runtime"
}
if (-not (Test-Path -LiteralPath $riskGate -PathType Leaf)) {
    throw "Risk gate runtime not found: $riskGate"
}

$runtimeArgs = @()
if ($Json) { $runtimeArgs += '--json' }

Push-Location $projectRoot
try {
    $null = & $python $riskGate --plan-check
    if ($LASTEXITCODE -ne 0) { throw 'PlanOnly verification failed' }
    $null = & $python $riskGate --capability-scan
    if ($LASTEXITCODE -ne 0) { throw 'Capability scan failed' }
    & $python $runtime @runtimeArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
