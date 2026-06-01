param(
    [Parameter(Mandatory = $true)]
    [string]$Volume,

    [string]$Project = "",
    [string]$Environment = "",
    [string]$Service = "",

    [string]$OutDir = "data\railway_sync",

    [switch]$IncludeLogs,
    [int]$LogLines = 500
)

$ErrorActionPreference = "Stop"

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command '$Name' not found. Install Railway CLI first, then run: railway login; railway link"
    }
}

function Invoke-RailwayVolumeDownload($RemotePath, $LocalPath, $DefaultContent = "") {
    $args = @("volume", "files", "--volume", $Volume)
    if ($Project) { $args += @("-p", $Project) }
    if ($Environment) { $args += @("-e", $Environment) }
    if ($Service) { $args += @("-s", $Service) }
    $args += @("download", $RemotePath, $LocalPath, "--overwrite")

    Write-Host "Downloading $RemotePath -> $LocalPath"
    & railway @args
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not download $RemotePath. Creating a local placeholder instead."
        $DefaultContent | Out-File -FilePath $LocalPath -Encoding utf8
    }
}

function Invoke-RailwayLogs($LocalPath) {
    $args = @("logs", "--lines", "$LogLines")
    if ($Environment) { $args += @("-e", $Environment) }
    if ($Service) { $args += @("-s", $Service) }

    Write-Host "Saving last $LogLines Railway log lines -> $LocalPath"
    & railway @args | Out-File -FilePath $LocalPath -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        throw "railway logs failed"
    }
}

Require-Command "railway"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$syncDir = Join-Path $OutDir $timestamp
New-Item -ItemType Directory -Force -Path $syncDir | Out-Null

$feedbackPath = Join-Path $syncDir "feedback.jsonl"
$checkinPath = Join-Path $syncDir "checkin_count.json"

Invoke-RailwayVolumeDownload "/feedback.jsonl" $feedbackPath ""
Invoke-RailwayVolumeDownload "/checkin_count.json" $checkinPath '{"count":0}'

$latestFeedback = Join-Path $OutDir "feedback.latest.jsonl"
$latestCheckin = Join-Path $OutDir "checkin_count.latest.json"
Copy-Item -LiteralPath $feedbackPath -Destination $latestFeedback -Force
Copy-Item -LiteralPath $checkinPath -Destination $latestCheckin -Force

if ($IncludeLogs) {
    $logPath = Join-Path $syncDir "railway_logs.txt"
    Invoke-RailwayLogs $logPath
    Copy-Item -LiteralPath $logPath -Destination (Join-Path $OutDir "railway_logs.latest.txt") -Force
}

Write-Host ""
Write-Host "Done."
Write-Host "Archive: $syncDir"
Write-Host "Latest feedback: $latestFeedback"
Write-Host "Latest check-in: $latestCheckin"
if ($IncludeLogs) {
    Write-Host "Latest logs: $(Join-Path $OutDir 'railway_logs.latest.txt')"
}
