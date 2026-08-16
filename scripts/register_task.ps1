<#
.SYNOPSIS
    Registers the STWDO watchdog as a Windows scheduled task that starts at logon.

.DESCRIPTION
    Runs `stwdo watch` from this project's virtual environment. Dry run by
    default: pass -Live only after you have reviewed a dry-run screenshot and
    set application.live_enabled: true in config.yaml.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Live

.NOTES
    Remove with:  Unregister-ScheduledTask -TaskName "STWDO Room Watchdog" -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = "STWDO Room Watchdog",
    [switch]$Live
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found at $python. Create it first:`n  py -m venv .venv`n  .venv\Scripts\activate`n  pip install -e .`n  playwright install chromium"
}

$arguments = "-m stwdo.cli watch"
if ($Live) {
    Write-Warning "LIVE MODE: this task may submit your one and only application."
    Write-Warning "STWDO deletes ALL applications from anyone who submits more than one."
    $answer = Read-Host "Type LIVE to confirm"
    if ($answer -ne "LIVE") {
        Write-Host "Aborted. Task not registered."
        exit 1
    }
    $arguments += " --live"
}

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn

# The watchdog is a long-running loop: no execution time limit, restart on failure,
# and it must keep polling on battery (rooms are published at any time).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing the existing task '$TaskName'."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Watches stwdo.de for free student rooms and applies to the best match." | Out-Null

Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "Mode: $(if ($Live) { 'LIVE — may submit an application' } else { 'dry run — will never submit' })"
Write-Host "Start now with:  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Logs:            $projectRoot\data\watchdog.log"
