#requires -Version 5.1
<#
    QQBot supervisor + network watchdog.

    Why this exists
    ---------------
    On 2026-10-11 the bot died at ~01:54 and stayed down for 2m41s.  The Python
    process produced NO stderr output and nothing restarted it, so neither the
    cause nor the outage was ever recorded.  This script closes both gaps:

      * restarts the bot automatically when it exits, logging the exit code and
        the tail of stderr so an OOM kill / crash can be told apart;
      * samples network health (gateway + DNS + OneBot WebSocket) and writes a
        transition log, so the "night disconnect" gets a timestamp and a cause;
      * never touches NapCat or QQ - only this project's Python process.

    Run:  powershell -ExecutionPolicy Bypass -File .\scripts\lifecycle\bot-supervisor.ps1
    Stop: delete run\supervisor.stop  (or kill this PowerShell process)
#>

$ErrorActionPreference = 'Continue'
# 仓库根 = 向上第一个含 bot.py 的目录：脚本再挪目录也不会失效（见 scripts/README.md）。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$python       = Join-Path $root '.venv\Scripts\python.exe'
$logs         = Join-Path $root 'logs'
$runDir       = Join-Path $root 'run'
$pidFile      = Join-Path $runDir 'bot.pid'
$stopFlag     = Join-Path $runDir 'supervisor.stop'
$watchLog     = Join-Path $logs 'supervisor.log'
$envPath      = Join-Path $root '.env'

New-Item -ItemType Directory -Force -Path $logs, $runDir | Out-Null

if (-not (Test-Path -LiteralPath $python)) { throw "Python venv missing: $python" }
if (-not (Test-Path -LiteralPath $envPath)) { throw "Missing .env" }
if (Test-Path -LiteralPath $stopFlag) { Remove-Item -LiteralPath $stopFlag -Force }

$env:PYTHONIOENCODING = 'utf-8'

function Write-Watch([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $watchLog -Value $line -Encoding UTF8
    Write-Host $line
}

# --- network probes --------------------------------------------------------
$gateway = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric | Select-Object -First 1).NextHop
$dnsProbe = 'q1.qlogo.cn'

function Test-NetworkState {
    $gw = $false
    if ($gateway) { $gw = Test-Connection -ComputerName $gateway -Count 1 -Quiet -ErrorAction SilentlyContinue }
    $dns = $false
    try { $dns = [bool](Resolve-DnsName -Name $dnsProbe -Type A -QuickTimeout -ErrorAction Stop) } catch { $dns = $false }
    $wlan = (Get-NetAdapter -Name 'WLAN' -ErrorAction SilentlyContinue).Status -eq 'Up'
    return [pscustomobject]@{ Gateway = $gw; Dns = $dns; Wlan = $wlan }
}

function Test-OneBotConnected {
    # The bot holds an established loopback session with NapCat on 8080.
    $c = Get-NetTCPConnection -LocalPort 8080 -State Established -ErrorAction SilentlyContinue
    return [bool]$c
}

Write-Watch '=== supervisor starting ==='
Write-Watch ("gateway={0} python={1}" -f $gateway, $python)

$netState  = $null
$oneBotUp  = $false
$restarts  = 0

while (-not (Test-Path -LiteralPath $stopFlag)) {

    # --- 1. network sampling (runs regardless of bot health) --------------
    $now = Test-NetworkState
    if ($null -eq $netState) {
        $netState = $now
        Write-Watch ("network baseline: gateway={0} dns={1} wlan={2}" -f $now.Gateway, $now.Dns, $now.Wlan)
    } elseif ($now.Gateway -ne $netState.Gateway -or $now.Dns -ne $netState.Dns -or $now.Wlan -ne $netState.Wlan) {
        Write-Watch ("NETWORK CHANGE: gateway {0}->{1}  dns {2}->{3}  wlan {4}->{5}" -f `
            $netState.Gateway, $now.Gateway, $netState.Dns, $now.Dns, $netState.Wlan, $now.Wlan)
        $netState = $now
    }

    $conn = Test-OneBotConnected
    if ($conn -ne $oneBotUp) {
        if ($conn) { Write-Watch 'ONEBOT RECONNECTED (loopback session on 8080 established)' }
        else       { Write-Watch ("ONEBOT LOST (no established session on 8080; network gw={0} dns={1})" -f $now.Gateway, $now.Dns) }
        $oneBotUp = $conn
    }

    # --- 2. is the bot process alive? -------------------------------------
    # 重启请求：`run\bot.restart` 存在就杀掉现有 bot 并强制走重启分支。
    # 为什么要这个文件而不是直接 kill：计划任务用 `/rl highest` 启动，bot 是**提权进程**，
    # 普通（非提权）shell 的 taskkill 会被拒绝（Access is denied）。看门狗本身是提权的，
    # 由它来杀就合法——于是"重启"变成一个不需要提权的操作：写文件即可。
    $restartFlag = Join-Path $runDir 'bot.restart'
    if (Test-Path -LiteralPath $restartFlag) {
        Remove-Item -LiteralPath $restartFlag -Force -ErrorAction SilentlyContinue
        Write-Watch 'RESTART REQUESTED (run\bot.restart)'
        foreach ($target in (Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*$python*" })) {
            Write-Watch ("killing python pid={0} for restart" -f $target.ProcessId)
            & taskkill /PID $target.ProcessId /T /F 2>&1 | Out-Null
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    # 停机请求：`run\bot.stop` 存在就杀掉 bot 并让看门狗自己退出（不再拉起）。
    # 同样是为了绕开"提权进程普通 shell 杀不掉"的限制（见上面的说明）。
    $stopBotFlag = Join-Path $runDir 'bot.stop'
    if (Test-Path -LiteralPath $stopBotFlag) {
        Remove-Item -LiteralPath $stopBotFlag -Force -ErrorAction SilentlyContinue
        Write-Watch 'STOP REQUESTED (run\bot.stop)'
        foreach ($target in (Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*$python*" })) {
            Write-Watch ("killing python pid={0} to stop" -f $target.ProcessId)
            & taskkill /PID $target.ProcessId /T /F 2>&1 | Out-Null
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Set-Content -LiteralPath $stopFlag -Value 'stop requested via run\bot.stop' -Encoding utf8
        break
    }

    $alive = $null
    if (Test-Path -LiteralPath $pidFile) {
        $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
        $botPid = 0
        if ([int]::TryParse(($raw -as [string]).Trim(), [ref]$botPid)) {
            $proc = Get-Process -Id $botPid -ErrorAction SilentlyContinue
            if ($proc -and $proc.Path -eq $python) { $alive = $proc }
        }
    }

    if (-not $alive) {
        # Reap any orphan worker still holding the port before restarting.
        foreach ($orphan in (Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*$python*" })) {
            Write-Watch ("killing orphan python pid={0}" -f $orphan.ProcessId)
            & taskkill /PID $orphan.ProcessId /T /F 2>&1 | Out-Null
        }

        $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stdout = Join-Path $logs "bot-$stamp.log"
        $stderr = Join-Path $logs "bot-$stamp.err.log"
        $restarts++
        Write-Watch ("BOT DOWN -> restart #{0}  net(gw={1} dns={2})" -f $restarts, $now.Gateway, $now.Dns)
        $p = Start-Process -FilePath $python -ArgumentList 'bot.py' -WorkingDirectory $root `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru
        $p.Id | Set-Content -LiteralPath $pidFile -Encoding ascii
        Start-Sleep -Seconds 8
        $p.Refresh()
        if ($p.HasExited) {
            Write-Watch ("BOT EXITED IMMEDIATELY code={0}" -f $p.ExitCode)
            $errTail = (Get-Content -LiteralPath $stderr -Tail 25 -ErrorAction SilentlyContinue) -join ' | '
            Write-Watch ("stderr: {0}" -f $errTail)
            Start-Sleep -Seconds 15
        } else {
            Write-Watch ("BOT STARTED pid={0} (log {1})" -f $p.Id, (Split-Path $stdout -Leaf))
        }
    }

    Start-Sleep -Seconds 15
}

Write-Watch '=== supervisor stopped ==='
