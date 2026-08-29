[CmdletBinding()]
param(
    [int]$ListenPort = 8899,
    [int]$WebPort = 8900,
    [string]$MitmwebPath = "D:\application\mitmproxy-env\Scripts\mitmweb.exe",
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

$script:ProxyKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$script:ProxyServer = "127.0.0.1:$ListenPort"
$script:PanelUrl = "http://127.0.0.1:$WebPort/"
$script:BackupPath = Join-Path $env:TEMP "rucxiaolaba-mitmproxy-backup.json"
$script:ProxySnapshot = $null
$script:ProxyChanged = $false
$script:MitmProcess = $null

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

    Set-CaptureProxy
    Write-Host "代理已临时设置为 $script:ProxyServer" -ForegroundColor Green
    Write-Host "面板：$script:PanelUrl"
    Write-Host "Cookie：data\config_small.txt（只在本机保存，不打印值）"
    Write-Host "关闭此窗口或停止 mitmweb 后，脚本会尝试恢复原代理。"
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
    Start-Process $script:PanelUrl | Out-Null
    Wait-Process -Id $script:MitmProcess.Id
} finally {
    if ($null -ne $script:MitmProcess -and -not $script:MitmProcess.HasExited) {
        Stop-Process -Id $script:MitmProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Restore-CaptureProxy
}
