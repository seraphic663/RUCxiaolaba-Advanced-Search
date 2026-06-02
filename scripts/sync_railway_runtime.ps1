param(
    [Parameter(Mandatory = $true)]
    [string]$Volume,

    [string]$Project = "",
    [string]$Environment = "",
    [string]$Service = "",

    [string]$OutDir = "data\railway_sync",

    [switch]$Archive,
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

$latestFeedback = Join-Path $OutDir "feedback.latest.jsonl"
$latestCheckin = Join-Path $OutDir "checkin_count.latest.json"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Invoke-RailwayVolumeDownload "/feedback.jsonl" $latestFeedback ""
Invoke-RailwayVolumeDownload "/checkin_count.json" $latestCheckin '{"count":0}'

if ($IncludeLogs) {
    $logPath = Join-Path $OutDir "railway_logs.latest.txt"
    Invoke-RailwayLogs $logPath
}

if ($Archive) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $syncDir = Join-Path $OutDir $timestamp
    New-Item -ItemType Directory -Force -Path $syncDir | Out-Null
    Copy-Item -LiteralPath $latestFeedback -Destination (Join-Path $syncDir "feedback.jsonl") -Force
    Copy-Item -LiteralPath $latestCheckin -Destination (Join-Path $syncDir "checkin_count.json") -Force
    if ($IncludeLogs) {
        Copy-Item -LiteralPath (Join-Path $OutDir "railway_logs.latest.txt") -Destination (Join-Path $syncDir "railway_logs.txt") -Force
    }
}

Write-Host ""
Write-Host "Done."
Write-Host "Latest feedback: $latestFeedback"
Write-Host "Latest check-in: $latestCheckin"
if ($IncludeLogs) {
    Write-Host "Latest logs: $(Join-Path $OutDir 'railway_logs.latest.txt')"
}
if ($Archive) {
    Write-Host "Archive: $syncDir"
}
