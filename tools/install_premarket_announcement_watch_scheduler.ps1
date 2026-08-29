[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Status,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$taskPath = '\ZolotyayLopata\'
$taskName = 'PremarketAnnouncementWatch'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot 'tools\start_premarket_announcement_watch_scheduler.ps1'
$pwsh = 'C:\Program Files\PowerShell\7\pwsh.exe'
$python = 'C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

function Ensure-ScheduledTaskFolder {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $folderPath = $Path.TrimEnd('\')
    if ($folderPath -ne '\ZolotyayLopata') {
        throw "Unexpected scheduled-task folder: $Path"
    }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    try {
        $null = $service.GetFolder($folderPath)
        return
    } catch [System.Runtime.InteropServices.COMException] {
        $root = $service.GetFolder('\')
        try {
            $null = $root.CreateFolder('ZolotyayLopata')
        } catch [System.Runtime.InteropServices.COMException] {
            # A concurrent installer may have created the exact folder.
            $null = $service.GetFolder($folderPath)
            return
        }
        $null = $service.GetFolder($folderPath)
    }
}

function Get-TaskPayload {
    $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [ordered]@{
            status = 'NOT_INSTALLED'
            task_path = $taskPath
            task_name = $taskName
            installed = $false
            python_executable = $python
        }
    }
    $info = Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $taskName
    return [ordered]@{
        status = 'INSTALLED'
        task_path = $taskPath
        task_name = $taskName
        installed = $true
        enabled = [bool]$task.Settings.Enabled
        hidden = [bool]$task.Settings.Hidden
        state = [string]$task.State
        execute = [string]$task.Actions[0].Execute
        arguments = [string]$task.Actions[0].Arguments
        working_directory = [string]$task.Actions[0].WorkingDirectory
        repetition_interval = [string]$task.Triggers[0].Repetition.Interval
        next_run_time = $info.NextRunTime.ToUniversalTime().ToString('o')
        last_task_result = $info.LastTaskResult
        python_executable = $python
    }
}

if ($Install.IsPresent -eq $Status.IsPresent) {
    throw 'Choose exactly one mode: -Install or -Status.'
}

if ($Install) {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Scheduler launcher not found: $launcher"
    }
    if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) {
        throw "Pinned PowerShell runtime not found: $pwsh"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Pinned Python runtime not found: $python"
    }
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '" -ScheduledTick'
    $action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval ([TimeSpan]::FromMinutes(5)) -RepetitionDuration ([TimeSpan]::FromDays(3650))
    $settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Ensure-ScheduledTaskFolder -Path $taskPath
    Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
}

$payload = Get-TaskPayload
if ($Json) {
    $payload | ConvertTo-Json -Compress -Depth 16
} else {
    $payload
}
