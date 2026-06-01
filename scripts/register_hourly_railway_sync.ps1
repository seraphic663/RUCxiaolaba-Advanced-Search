param(
    [string]$TaskName = "RUCxiaolaba Railway Runtime Sync",
    [string]$Volume = "rucxiaolaba-advanced-search-volume",
    [string]$RepoDir = "D:\temp\RUCxiaolaba-Advanced-Search"
)

$ErrorActionPreference = "Stop"

$syncScript = Join-Path $RepoDir "scripts\sync_railway_runtime.ps1"
if (-not (Test-Path -LiteralPath $syncScript)) {
    throw "Sync script not found: $syncScript"
}

$start = (Get-Date).Date.AddMinutes(30)
if ($start -le (Get-Date)) {
    $start = $start.AddHours(1)
}

$actionArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$syncScript`"",
    "-Volume", $Volume
) -join " "

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $actionArgs `
    -WorkingDirectory $RepoDir

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Sync Railway feedback and check-in files every hour at minute 30." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "First run: $start"
Write-Host "Repeats: every 1 hour"
Write-Host "Command: powershell.exe $actionArgs"
