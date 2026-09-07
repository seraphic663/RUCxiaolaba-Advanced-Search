[CmdletBinding()]
param(
    [int]$ListenPort = 8899,
    [int]$WebPort = 8900,
    [string]$MitmwebPath = "D:\application\mitmproxy-env\Scripts\mitmweb.exe",
    [ValidateSet("new", "old")]
    [string]$Mode = "new",
    [switch]$NoInstall,
    [int]$TimeoutSeconds = 300,
    [int]$PollSeconds = 1,
    [switch]$KeepOpen,
    [switch]$NoPanel
)

$ErrorActionPreference = "Stop"

$script:ProxyKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$script:ProxyServer = "127.0.0.1:$ListenPort"
$script:PanelUrl = "http://127.0.0.1:$WebPort/"
$script:BackupPath = Join-Path $env:TEMP "rucxiaolaba-mitmproxy-backup.json"
$script:ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:CookieFilePath = if ($Mode -eq "old") {
    Join-Path $script:ProjectRoot "data\config.txt"
} else {
    Join-Path $script:ProjectRoot "data\config_small.txt"
}
$script:CandidatePath = "$($script:CookieFilePath).candidate"
$script:StatusPath = Join-Path $script:ProjectRoot "data\capture_status.json"
$script:ProxySnapshot = $null
$script:ProxyChanged = $false
$script:MitmProcess = $null
$script:PanelProcess = $null
$script:PanelProfilePath = Join-Path $env:TEMP ("rucxiaolaba-mitm-panel-" + [Guid]::NewGuid().ToString("N"))
$script:PanelUsesDedicatedProfile = $false
$script:PreviousStatusEnvironment = $null
$script:PreviousCookieEnvironment = $null
$script:PreviousCandidateEnvironment = $null
$script:PreviousLabelEnvironment = $null

function Get-RegistryValueSnapshot {
    param([string]$Name)

    $properties = Get-ItemProperty -Path $script:ProxyKey -ErrorAction SilentlyContinue
    if ($null -eq $properties) {
        return [pscustomobject]@{ present = $false; value = $null }
    }
    $property = $properties.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return [pscustomobject]@{ present = $false; value = $null }
    }
    return [pscustomobject]@{ present = $true; value = $property.Value }
}

function Get-ProxySnapshot {
    return [pscustomobject][ordered]@{
        version = 1
        target_proxy = $script:ProxyServer
        proxy_enable = Get-RegistryValueSnapshot "ProxyEnable"
        proxy_server = Get-RegistryValueSnapshot "ProxyServer"
        proxy_override = Get-RegistryValueSnapshot "ProxyOverride"
        auto_config_url = Get-RegistryValueSnapshot "AutoConfigURL"
        auto_detect = Get-RegistryValueSnapshot "AutoDetect"
    }
}

function Get-CurrentProxyServer {
    $properties = Get-ItemProperty -Path $script:ProxyKey -ErrorAction SilentlyContinue
    if ($null -eq $properties) {
        return $null
    }
    return [pscustomobject]@{
        enabled = [int]($properties.ProxyEnable -as [int])
        server = [string]$properties.ProxyServer
    }
}

function Notify-InternetSettingsChanged {
    if (-not ("RucXlbWinInet" -as [type])) {
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class RucXlbWinInet {
    [DllImport("wininet.dll", SetLastError = true)]
    public static extern bool InternetSetOption(
        IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
}
"@
    }
    [RucXlbWinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null
    [RucXlbWinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null
}

function Set-ProxyValue {
    param(
        [string]$Name,
        [object]$Value
    )
    if ($null -eq $Value) {
        Remove-ItemProperty -Path $script:ProxyKey -Name $Name -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -Path $script:ProxyKey -Name $Name -Value $Value
    }
}

function Restore-ProxySnapshot {
    param([object]$Snapshot)

    if ($null -eq $Snapshot) {
        return
    }
    foreach ($item in @(
        @{ name = "ProxyEnable"; state = $Snapshot.proxy_enable },
        @{ name = "ProxyServer"; state = $Snapshot.proxy_server },
        @{ name = "ProxyOverride"; state = $Snapshot.proxy_override },
        @{ name = "AutoConfigURL"; state = $Snapshot.auto_config_url },
        @{ name = "AutoDetect"; state = $Snapshot.auto_detect }
    )) {
        if ($item.state.present) {
            Set-ProxyValue $item.name $item.state.value
        } else {
            Remove-ItemProperty -Path $script:ProxyKey -Name $item.name -ErrorAction SilentlyContinue
        }
    }
    Notify-InternetSettingsChanged
}

function Recover-PreviousProxy {
    if (-not (Test-Path -LiteralPath $script:BackupPath)) {
        return
    }

    try {
        $previous = Get-Content -LiteralPath $script:BackupPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $current = Get-CurrentProxyServer
        $matches = (
            $null -ne $current -and
            $current.enabled -eq 1 -and
            $current.server -eq [string]$previous.target_proxy
        )
        if (-not $matches) {
            throw "检测到上一次抓包的代理备份，但当前代理已被手动修改；为避免覆盖你的新设置，暂不自动恢复。备份文件：$script:BackupPath"
        }
        Write-Host "发现上次未正常退出的抓包代理，先恢复原代理设置..." -ForegroundColor Yellow
        Restore-ProxySnapshot $previous
        Remove-Item -LiteralPath $script:BackupPath -Force
    } catch {
        throw $_
    }
}

function Find-Python {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        return [pscustomobject]@{ path = $py.Source; prefix = @("-3") }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return [pscustomobject]@{ path = $python.Source; prefix = @() }
    }
    return $null
}

function Find-Mitmweb {
    if ($MitmwebPath -and (Test-Path -LiteralPath $MitmwebPath)) {
        return (Resolve-Path -LiteralPath $MitmwebPath).Path
    }

    $command = Get-Command mitmweb.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $python = Find-Python
    if ($null -eq $python) {
        return $null
    }
    $scriptPaths = & $python.path @($python.prefix) -c "import os, site, sysconfig; paths = [sysconfig.get_path('scripts')]; paths.append(sysconfig.get_path('scripts', scheme='nt_user')); paths.append(os.path.join(site.getuserbase(), 'Scripts')); print('\\n'.join(p for p in paths if p))" 2>$null
    foreach ($scripts in @($scriptPaths)) {
        $scripts = ([string]$scripts).Trim()
        if (-not $scripts) {
            continue
        }
        $candidate = Join-Path $scripts "mitmweb.exe"
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Ensure-Mitmweb {
    $mitmweb = Find-Mitmweb
    if ($null -ne $mitmweb) {
        return $mitmweb
    }
    if ($NoInstall) {
        throw "找不到 mitmweb。请先安装 mitmproxy，或不要使用 -NoInstall。"
    }

    $python = Find-Python
    if ($null -eq $python) {
        throw "找不到 Python。请先安装 Python 3，再重新运行本脚本。"
    }

    Write-Host "未找到 mitmweb，正在为当前用户安装 mitmproxy..." -ForegroundColor Yellow
    & $python.path @($python.prefix) -m pip install --user mitmproxy
    if ($LASTEXITCODE -ne 0) {
        throw "mitmproxy 安装失败，pip exit code=$LASTEXITCODE"
    }

    $mitmweb = Find-Mitmweb
    if ($null -eq $mitmweb) {
        throw "mitmproxy 已安装，但仍找不到 mitmweb.exe。请重新打开 PowerShell 后再试。"
    }
    return $mitmweb
}

function Set-CaptureProxy {
    $script:ProxySnapshot = Get-ProxySnapshot
    $script:ProxySnapshot | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $script:BackupPath -Encoding UTF8

    Set-ProxyValue "ProxyEnable" 1
    Set-ProxyValue "ProxyServer" $script:ProxyServer
    Set-ProxyValue "ProxyOverride" "<local>"
    Set-ProxyValue "AutoDetect" 0
    Remove-ItemProperty -Path $script:ProxyKey -Name "AutoConfigURL" -ErrorAction SilentlyContinue
    Notify-InternetSettingsChanged
    $script:ProxyChanged = $true
}

function Test-CaptureValidated {
    if (-not (Test-Path -LiteralPath $script:StatusPath)) {
        return $false
    }
    try {
        $status = Get-Content -LiteralPath $script:StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($status.state -eq "validated" -and $status.validated -eq $true)
    } catch {
        return $false
    }
}

function Remove-CaptureCandidate {
    Remove-Item -LiteralPath $script:CandidatePath -Force -ErrorAction SilentlyContinue
}

function Find-Chrome {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    return $null
}

function Open-CapturePanel {
    if ($NoPanel) {
        Write-Host "已跳过自动打开面板（-NoPanel）。"
        return
    }
    $chrome = Find-Chrome
    if ($null -eq $chrome) {
        Start-Process $script:PanelUrl | Out-Null
        Write-Warning "找不到 Chrome，面板已用系统默认浏览器打开；脚本无法安全自动关闭这个标签页。"
        return
    }

    New-Item -ItemType Directory -Path $script:PanelProfilePath -Force | Out-Null
    $panelArguments = @(
        "--app=`"$($script:PanelUrl)`"",
        "--user-data-dir=`"$($script:PanelProfilePath)`"",
        "--no-first-run",
        "--no-default-browser-check"
    )
    $script:PanelProcess = Start-Process `
        -FilePath $chrome `
        -ArgumentList $panelArguments `
        -PassThru `
        -WindowStyle Normal
    $script:PanelUsesDedicatedProfile = $true
}

function Close-CapturePanel {
    if ($null -ne $script:PanelProcess) {
        try {
            if (-not $script:PanelProcess.HasExited) {
                Stop-Process -Id $script:PanelProcess.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Warning "关闭本次抓包面板失败：$($_.Exception.Message)"
        }
        $script:PanelProcess = $null
    }
    if ($script:PanelUsesDedicatedProfile) {
        Remove-Item -LiteralPath $script:PanelProfilePath -Recurse -Force -ErrorAction SilentlyContinue
        $script:PanelUsesDedicatedProfile = $false
    }
}

function Restore-CaptureProxy {
    if (-not $script:ProxyChanged) {
        return
    }
    Write-Host "正在恢复原来的 Windows 代理设置..." -ForegroundColor Yellow
    Restore-ProxySnapshot $script:ProxySnapshot
    Remove-Item -LiteralPath $script:BackupPath -Force -ErrorAction SilentlyContinue
    $script:ProxyChanged = $false
    Write-Host "代理设置已恢复。" -ForegroundColor Green
}

try {
    Write-Host "============================================"
    Write-Host "  mitmproxy - WeChat Mini Program Capture"
    Write-Host "============================================"
    Write-Host ""

    Recover-PreviousProxy
    $mitmweb = Ensure-Mitmweb
    $filter = Join-Path $PSScriptRoot "mitm_filter.py"
    if (-not (Test-Path -LiteralPath $filter)) {
        throw "找不到过滤脚本：$filter"
    }

    Remove-CaptureCandidate
    Remove-Item -LiteralPath $script:StatusPath -Force -ErrorAction SilentlyContinue
    $script:PreviousStatusEnvironment = [Environment]::GetEnvironmentVariable(
        "RUC_CAPTURE_STATUS_FILE",
        "Process"
    )
    $script:PreviousCookieEnvironment = [Environment]::GetEnvironmentVariable(
        "RUC_CAPTURE_COOKIE_FILE",
        "Process"
    )
    $script:PreviousCandidateEnvironment = [Environment]::GetEnvironmentVariable(
        "RUC_CAPTURE_CANDIDATE_FILE",
        "Process"
    )
    $script:PreviousLabelEnvironment = [Environment]::GetEnvironmentVariable(
        "RUC_CAPTURE_LABEL",
        "Process"
    )
    $env:RUC_CAPTURE_STATUS_FILE = $script:StatusPath
    $env:RUC_CAPTURE_COOKIE_FILE = $script:CookieFilePath
    $env:RUC_CAPTURE_CANDIDATE_FILE = $script:CandidatePath
    $env:RUC_CAPTURE_LABEL = $Mode

    Set-CaptureProxy
    Write-Host "代理已临时设置为 $script:ProxyServer" -ForegroundColor Green
    Write-Host "面板：$script:PanelUrl"
    Write-Host "检测到成功 API 响应后，$Mode cookie 自动保存到 $script:CookieFilePath"
    Write-Host "请现在在微信中打开小程序并刷新一次列表或详情页。"
    if ($KeepOpen) {
        Write-Host "当前为手动模式：保持窗口打开，关闭 mitmweb 后恢复代理。"
    } else {
        Write-Host "默认将在验证成功后自动停止；超时 ${TimeoutSeconds} 秒。"
    }
    Write-Host "请只在自己的微信账号和自己的设备上观察请求。"

    $arguments = @(
        "--listen-port", "$ListenPort",
        "--web-port", "$WebPort",
        "--web-host", "127.0.0.1",
        "--set", "block_global=false",
        "-s", ('"{0}"' -f $filter)
    )
    $script:MitmProcess = Start-Process -FilePath $mitmweb -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -PassThru -NoNewWindow
    Start-Sleep -Seconds 2
    Open-CapturePanel

    $deadline = $null
    if ($TimeoutSeconds -gt 0) {
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    }
    $autoStopped = $false
    while (-not $script:MitmProcess.HasExited) {
        if (-not $KeepOpen -and (Test-CaptureValidated)) {
            Write-Host "已验证成功，正在关闭抓包并恢复代理。" -ForegroundColor Green
            Start-Sleep -Seconds 2
            if (-not $script:MitmProcess.HasExited) {
                Stop-Process -Id $script:MitmProcess.Id -Force -ErrorAction SilentlyContinue
            }
            $autoStopped = $true
            break
        }
        if ($null -ne $deadline -and [DateTime]::UtcNow -ge $deadline) {
            throw "抓包超时：没有观察到成功 API 响应。请确认微信小程序已经刷新，并检查证书/代理设置。"
        }
        Start-Sleep -Seconds ([Math]::Max(1, $PollSeconds))
    }
    if (-not $autoStopped -and -not (Test-CaptureValidated)) {
        Write-Warning "抓包结束，但没有验证成功的会话；原来的 $Mode cookie 文件未被替换。"
    }
} finally {
    if ($null -ne $script:MitmProcess -and -not $script:MitmProcess.HasExited) {
        Stop-Process -Id $script:MitmProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Close-CapturePanel
    Remove-CaptureCandidate
    if ($null -ne $script:PreviousStatusEnvironment) {
        $env:RUC_CAPTURE_STATUS_FILE = $script:PreviousStatusEnvironment
    } else {
        Remove-Item Env:RUC_CAPTURE_STATUS_FILE -ErrorAction SilentlyContinue
    }
    foreach ($item in @(
        @{ name = "RUC_CAPTURE_COOKIE_FILE"; value = $script:PreviousCookieEnvironment },
        @{ name = "RUC_CAPTURE_CANDIDATE_FILE"; value = $script:PreviousCandidateEnvironment },
        @{ name = "RUC_CAPTURE_LABEL"; value = $script:PreviousLabelEnvironment }
    )) {
        if ($null -ne $item.value) {
            Set-Item -Path ("Env:" + $item.name) -Value $item.value
        } else {
            Remove-Item -Path ("Env:" + $item.name) -ErrorAction SilentlyContinue
        }
    }
    Restore-CaptureProxy
}
