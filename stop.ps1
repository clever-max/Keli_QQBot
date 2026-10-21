$ErrorActionPreference = 'Stop'
# 停下机器人（**不需要提权**）。
#
# 背景：开机自启的计划任务带 `/rl highest`，bot 是提权进程；普通 shell 的 Stop-Process /
# taskkill 会被拒绝（Access is denied，2026-10-18 实测）。看门狗是提权的，能杀，所以这里
# 写一个请求文件让它来做：
#   run\bot.stop  ->  看门狗杀掉 bot，然后自己退出（不会重新拉起）
#
# 若看门狗没在跑（刚重启、还没登录），退化为直接按 PID 杀——那种情况下 bot 也不是提权的，
# 普通权限就够。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$runDir  = Join-Path $root 'run'
$pidFile = Join-Path $runDir 'bot.pid'
$python  = Join-Path $root '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

# "看门狗是否在跑"不能靠 Get-CimInstance 的 CommandLine：提权进程的 CommandLine 对非提权
# shell 是空的（实测），改用计划任务状态（schtasks /query 不需要提权）。
$taskState = schtasks /query /tn 'QQBotSupervisor' /fo LIST 2>$null | Out-String
$supervisor = ($taskState -match 'Running' -or $taskState -match '正在运行')

if ($supervisor) {
    Set-Content -LiteralPath (Join-Path $runDir 'bot.stop') -Value ('stop requested at ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding utf8
    Write-Output '已写入停机请求（run\bot.stop），由看门狗执行。'
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $conn = Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue
        if (-not $conn) { Write-Output 'Bot stopped (port 8080 released).'; exit 0 }
    }
    Write-Output '等待超时：机器人可能仍在关闭中，请查看 logs\supervisor.log。'
    exit 1
}

# 没有看门狗：按 PID 直接停（此时 bot 与当前 shell 同权限，通常可行）。
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'No bot PID file found; the bot may already be stopped.'
    exit 0
}
$botProcessId = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$botProcessId)) {
    throw 'Invalid PID file. Inspect run\bot.pid before removing it.'
}
$process = Get-Process -Id $botProcessId -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Output 'Bot process was already stopped.'
    Remove-Item -LiteralPath $pidFile -Force
    exit 0
}
try {
    Stop-Process -Id $botProcessId -Force -ErrorAction Stop
    Write-Output "Bot stopped (PID $botProcessId)."
} catch {
    Write-Output "无法结束 PID $botProcessId（$($_.Exception.Message)）。"
    Write-Output '它可能是计划任务以提权方式启动的：请用管理员的 PowerShell 执行'
    Write-Output '  schtasks /end /tn "QQBotSupervisor"'
    Write-Output '或直接重启机器；日常更推荐 scripts\lifecycle\restart.ps1（不需要提权）。'
    exit 1
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
