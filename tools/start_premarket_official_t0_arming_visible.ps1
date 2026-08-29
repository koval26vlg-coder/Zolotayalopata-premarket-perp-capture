[CmdletBinding()]
param(
    [switch]$Arm,
    [switch]$Status,
    [string]$RunId = '',
    [string]$EpisodeId = '',
    [string]$OfficialRecordHash = '',
    [string]$ExpectedOfficialT0 = '',
    [string]$ExpectedContract = '',
    [string]$ExpectedSpotSymbol = '',
    [string]$ExpectedCurrentArmingReceiptHash = '',
    [string]$ArmedBy = '',
    [switch]$AcknowledgeNoCaptureAuthority,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'src\official_t0_arming.py'
$bundledPython = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = if ($env:PREMARKET_OFFICIAL_T0_ARMING_PYTHON) {
    $env:PREMARKET_OFFICIAL_T0_ARMING_PYTHON
} elseif (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

function Write-TerminalJson {
    param([Parameter(Mandatory = $true)][object]$Payload)
    $Payload | ConvertTo-Json -Compress -Depth 32
}

if ($Arm -eq $Status) {
    Write-TerminalJson ([ordered]@{
        status = 'ARMING_NOT_RUN_EXACTLY_ONE_MODE_REQUIRED'
        capture_authorized = $false
        capture_token_issued = $false
        event_bound_plan_generated = $false
    })
    exit 2
}

if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    Write-TerminalJson ([ordered]@{
        status = 'ARMING_NOT_RUN_RUNTIME_MISSING'
        capture_authorized = $false
        capture_token_issued = $false
        event_bound_plan_generated = $false
    })
    exit 2
}

Push-Location $projectRoot
try {
    if ($Status) {
        $arguments = @($runtime, '--status', '--json')
        if ($EpisodeId.Trim()) {
            $arguments += @('--episode-id', $EpisodeId)
        }
        & $python @arguments
        exit $LASTEXITCODE
    }

    $required = [ordered]@{
        RunId = $RunId
        EpisodeId = $EpisodeId
        OfficialRecordHash = $OfficialRecordHash
        ExpectedOfficialT0 = $ExpectedOfficialT0
        ExpectedContract = $ExpectedContract
        ExpectedSpotSymbol = $ExpectedSpotSymbol
        ArmedBy = $ArmedBy
    }
    $missing = @(
        $required.GetEnumerator() |
            Where-Object { [string]::IsNullOrWhiteSpace([string]$_.Value) } |
            ForEach-Object { $_.Key }
    )
    if ($missing.Count -gt 0) {
        throw ('missing required arming arguments: ' + ($missing -join ', '))
    }
    if (-not $AcknowledgeNoCaptureAuthority) {
        throw 'AcknowledgeNoCaptureAuthority is required'
    }

    $arguments = @(
        $runtime,
        '--arm',
        '--run-id', $RunId,
        '--episode-id', $EpisodeId,
        '--official-record-hash', $OfficialRecordHash,
        '--expected-official-t0', $ExpectedOfficialT0,
        '--expected-contract', $ExpectedContract,
        '--expected-spot-symbol', $ExpectedSpotSymbol,
        '--armed-by', $ArmedBy,
        '--acknowledge-no-capture-authority',
        '--json'
    )
    if ($ExpectedCurrentArmingReceiptHash.Trim()) {
        $arguments += @(
            '--expected-current-arming-receipt-hash',
            $ExpectedCurrentArmingReceiptHash
        )
    }
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Write-TerminalJson ([ordered]@{
        status = 'ARMING_NOT_RUN_FAIL_CLOSED'
        reason = $_.Exception.Message
        capture_authorized = $false
        capture_token_issued = $false
        event_bound_plan_generated = $false
    })
    exit 2
} finally {
    Pop-Location
}
