$ErrorActionPreference = 'Stop'
# 仓库根 = 向上第一个含 bot.py 的目录：脚本再挪目录也不会失效（见 scripts/README.md）。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$envFile = Join-Path $root '.env'
$python = Join-Path $root '.venv\Scripts\python.exe'
$pidFile = Join-Path $root 'run\bot.pid'

# 日志轮转放在 bot.py 里，不放这里：看门狗重启 bot 时是直接
# Start-Process python bot.py，会绕过这个脚本；只有入口才能保证两条路径都执行到。


if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env. Copy .env.example to .env and fill the group and authorized QQ IDs first.'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment is missing. Recreate .venv and install requirements.txt.'
}
$config = Get-Content -LiteralPath $envFile
$groupLine = $config | Where-Object { $_ -match '^\s*ALLOWED_GROUP_IDS\s*=' } | Select-Object -First 1
if (-not $groupLine) { $groupLine = $config | Where-Object { $_ -match '^\s*ALLOWED_GROUP_ID\s*=' } | Select-Object -First 1 }
$usersLine = $config | Where-Object { $_ -match '^\s*AUTHORIZED_USERS\s*=' } | Select-Object -First 1
$groupIds = if ($groupLine) { ($groupLine -split '=', 2)[1].Split(',') | ForEach-Object { $_.Trim() } } else { @() }
if (-not $groupIds.Count -or @($groupIds | Where-Object { $_ -notmatch '^[1-9]\d*$' }).Count -gt 0) {
    throw 'Fill ALLOWED_GROUP_IDS in .env with comma-separated target group numbers.'
}
if (-not $usersLine -or $usersLine -match '^\s*AUTHORIZED_USERS\s*=\s*$') {
    $groupUsersLine = $config | Where-Object { $_ -match '^\s*GROUP_AUTHORIZED_USERS\s*=' } | Select-Object -First 1
    if (-not $groupUsersLine -or $groupUsersLine -match '^\s*GROUP_AUTHORIZED_USERS\s*=\s*$') {
        throw 'Fill AUTHORIZED_USERS or GROUP_AUTHORIZED_USERS in .env with at least one QQ number.'
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $existingId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$existingId)) {
        $existing = Get-Process -Id $existingId -ErrorAction SilentlyContinue
        if ($existing -and $existing.Path -eq $python) {
            Write-Output "Bot is already running (PID $existingId)."
            return
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
}

# Group messages contain emoji; without this the redirected stdout/stderr fall
# back to the ANSI code page and loguru raises UnicodeEncodeError on every one.
$env:PYTHONIOENCODING = 'utf-8'

$logs = Join-Path $root 'logs'
$run = Join-Path $root 'run'
New-Item -ItemType Directory -Force -Path $logs, $run | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logs "bot-$stamp.log"
$stderr = Join-Path $logs "bot-$stamp.err.log"
$process = Start-Process -FilePath $python -ArgumentList 'bot.py' -WorkingDirectory $root `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
$process.Id | Set-Content -LiteralPath $pidFile -Encoding ascii
Start-Sleep -Seconds 2
$process.Refresh()
if ($process.HasExited) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw "Bot exited during startup. Inspect $stderr and $stdout."
}
Write-Output "Bot started in the background (PID $($process.Id))."
Write-Output "Logs: $logs"
