$ErrorActionPreference = 'Stop'
# 重启机器人（**不需要提权**）。
#
# 背景：开机自启的计划任务的 Action 带 `/rl highest`，所以 bot 是提权进程；普通 shell 的
# taskkill / Stop-Process 一律 Access is denied（2026-10-18 实测）。看门狗是提权的，能杀。
# 因此这里只写一个请求文件，由看门狗在下一轮（≤15 秒）里杀旧进程并拉起新的：
#   run\bot.restart  ->  看门狗看到后 kill bot，然后按正常流程重启
# 若看门狗没在跑（比如刚重启机器还没登录），退化为提示用计划任务拉起。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$runDir = Join-Path $root 'run'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$flag = Join-Path $runDir 'bot.restart'

# 不能靠 Get-CimInstance 的 CommandLine 判断：提权进程的 CommandLine 对非提权 shell 是空的
# （实测），那样会误报"看门狗没在运行"。计划任务状态不需要提权就能读。
$taskState = schtasks /query /tn 'QQBotSupervisor' /fo LIST 2>$null | Out-String
$supervisor = ($taskState -match 'Running' -or $taskState -match '正在运行')

if (-not $supervisor) {
    Write-Output '看门狗没在运行：先启动它（计划任务），它会自己把机器人拉起来：'
    Write-Output '  schtasks /run /tn "QQBotSupervisor"'
    exit 1
}

Set-Content -LiteralPath $flag -Value ('restart requested at ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding utf8
Write-Output '已写入重启请求（run\bot.restart）。看门狗会在 15 秒内重启机器人。'
Write-Output '查看进度：Get-Content logs\supervisor.log -Tail 5'
