param(
    [string]$Root = 'C:\Apps\QQBot',
    [switch]$RequireOneBotConnected
)

$ErrorActionPreference = 'Continue'
$script:Failures = 0
$script:Warnings = 0

function Pass([string]$Message) {
    Write-Host "[PASS] $Message" -ForegroundColor Green
}

function Warn([string]$Message) {
    $script:Warnings++
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Fail([string]$Message) {
    $script:Failures++
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Has-Command([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

Write-Host "QQBot VPS verification" -ForegroundColor Cyan
Write-Host "Root: $Root"
Write-Host "Time: $([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss zzz'))"
Write-Host ''

if (-not (Test-Path -LiteralPath $Root)) {
    Fail "Project root does not exist: $Root"
    exit 1
}

$envPath = Join-Path $Root '.env'
$python = Join-Path $Root '.venv\Scripts\python.exe'
$botPath = Join-Path $Root 'bot.py'
# 插件在 362edb9 由单文件拆成了包，这里跟着改成检查包入口。
$pluginPath = Join-Path $Root 'plugins\group_bot\__init__.py'
$requirementsPath = Join-Path $Root 'requirements.txt'
$dbPath = Join-Path $Root 'data\messages.sqlite3'
$pidPath = Join-Path $Root 'run\bot.pid'
$logsPath = Join-Path $Root 'logs'

foreach ($required in @($envPath, $python, $botPath, $pluginPath, $requirementsPath, $dbPath)) {
    if (Test-Path -LiteralPath $required) { Pass "File exists: $required" }
    else { Fail "Missing required file: $required" }
}

if (-not (Test-Path -LiteralPath $envPath)) {
    Fail 'Cannot inspect .env because it is missing.'
    exit 1
}

$envLines = Get-Content -LiteralPath $envPath
$groupLine = $envLines | Where-Object { $_ -match '^\s*ALLOWED_GROUP_IDS\s*=' } | Select-Object -First 1
$authLine = $envLines | Where-Object { $_ -match '^\s*AUTHORIZED_USERS\s*=' } | Select-Object -First 1
$groupAuthLine = $envLines | Where-Object { $_ -match '^\s*GROUP_AUTHORIZED_USERS\s*=' } | Select-Object -First 1
$tokenLine = $envLines | Where-Object { $_ -match '^\s*ONEBOT_ACCESS_TOKEN\s*=' } | Select-Object -First 1

if ($groupLine -and $groupLine -match '^\s*ALLOWED_GROUP_IDS\s*=\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*$') {
    Pass "Allowed groups configured: $($Matches[1] -replace '\s','')"
} else {
    Fail 'ALLOWED_GROUP_IDS is missing or invalid.'
}

if ($authLine -and $authLine -notmatch '^\s*AUTHORIZED_USERS\s*=\s*$') {
    Pass 'Default authorized user list is configured.'
} elseif ($groupAuthLine -and $groupAuthLine -notmatch '^\s*GROUP_AUTHORIZED_USERS\s*=\s*$') {
    Pass 'Per-group authorized user list is configured.'
} else {
    Fail 'Neither AUTHORIZED_USERS nor GROUP_AUTHORIZED_USERS is configured.'
}

if ($tokenLine -and $tokenLine -notmatch '^\s*ONEBOT_ACCESS_TOKEN\s*=\s*(?:replace-with-a-long-random-secret)?\s*$') {
    Pass 'OneBot access token is present and masked from output.'
} else {
    Fail 'ONEBOT_ACCESS_TOKEN is missing or still uses the example value.'
}

if (Test-Path -LiteralPath $python) {
    $version = & $python --version 2>&1
    if ($LASTEXITCODE -eq 0) { Pass "Python: $version" } else { Fail 'Python executable could not run.' }

    $syntaxFiles = @($botPath, $pluginPath)
    foreach ($source in $syntaxFiles) {
        & $python -c "import ast, pathlib; ast.parse(pathlib.Path(r'$source').read_text(encoding='utf-8'))" 2>$null
        if ($LASTEXITCODE -eq 0) { Pass "Python syntax: $source" } else { Fail "Python syntax error: $source" }
    }

    & $python -c "import aiosqlite, httpx, PIL; print('dependencies-ok')" 2>$null
    if ($LASTEXITCODE -eq 0) { Pass 'Python dependencies: aiosqlite, httpx, Pillow' }
    else { Fail 'Python dependencies are incomplete.' }
}

if (Has-Command 'Get-NetTCPConnection') {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        Pass "Port 8080 is listening on: $($listeners.LocalAddress -join ', ')"
        if (@($listeners | Where-Object { $_.LocalAddress -in @('0.0.0.0', '::') }).Count -gt 0) {
            Warn 'Port 8080 is exposed on all interfaces. Set HOST=127.0.0.1 when NapCat runs on this VPS.'
        }
    } else {
        Fail 'Nothing is listening on TCP port 8080.'
    }
} else {
    Warn 'Get-NetTCPConnection is unavailable; port check skipped.'
}

$botProcess = $null
if (Test-Path -LiteralPath $pidPath) {
    $pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $botPid = 0
    if ([int]::TryParse($pidText, [ref]$botPid)) {
        $botProcess = Get-Process -Id $botPid -ErrorAction SilentlyContinue
        if ($botProcess -and $botProcess.Path -eq $python) {
            Pass "Bot process is running: PID $botPid"
        } else {
            Fail "PID file does not point to this project's Python process."
        }
    } else {
        Fail 'run\bot.pid is invalid.'
    }
} else {
    Fail 'run\bot.pid is missing; scripts\lifecycle\start.ps1 may not have started the bot.'
}

if (Test-Path -LiteralPath $logsPath) {
    $latestLog = Get-ChildItem -LiteralPath $logsPath -Filter 'bot-*.log' -File |
        Where-Object { $_.Name -notlike '*.err.log' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $latestErr = Get-ChildItem -LiteralPath $logsPath -Filter 'bot-*.err.log' -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1

    if ($latestErr -and $latestErr.Length -gt 0) {
        $errorText = Get-Content -LiteralPath $latestErr.FullName -Raw
        if ($errorText -match 'Traceback|Failed to import|IndentationError|SyntaxError') {
            Fail "Latest error log contains startup/runtime errors: $($latestErr.Name)"
        } else {
            Warn "Latest error log is non-empty: $($latestErr.Name)"
        }
    } else {
        Pass 'Latest bot error log is empty.'
    }

    $combinedText = ''
    if ($latestLog) { $combinedText += ((Get-Content -LiteralPath $latestLog.FullName -Tail 160 -ErrorAction SilentlyContinue) -join "`n") }
    if ($latestErr) { $combinedText += "`n" + ((Get-Content -LiteralPath $latestErr.FullName -Tail 160 -ErrorAction SilentlyContinue) -join "`n") }
    $pluginLoadedAt = $combinedText.LastIndexOf('Succeeded to load plugin "group_bot"')
    $pluginFailedAt = $combinedText.LastIndexOf('Failed to import "group_bot"')
    $tracebackAt = $combinedText.LastIndexOf('Traceback')
    if ($pluginLoadedAt -lt 0) {
        Fail 'Latest logs do not show successful loading of group_bot.'
    } elseif ($pluginFailedAt -gt $pluginLoadedAt -or $tracebackAt -gt $pluginLoadedAt) {
        Fail 'Latest logs contain plugin import or traceback errors.'
    } else {
        Pass 'Latest logs show group_bot loaded without later traceback errors.'
    }
    if ($combinedText -match 'OneBot V11.*connected|Bot .* connected|connection open') {
        Pass 'Recent logs show an active OneBot connection.'
    } elseif ($RequireOneBotConnected) {
        Fail 'No recent OneBot connected line found.'
    } else {
        Warn 'No recent OneBot connected line found; NapCat may be stopped or not connected yet.'
    }
} else {
    Fail "Logs directory does not exist: $logsPath"
}

if (Test-Path -LiteralPath $dbPath) {
    $dbScript = @'
import sqlite3
db = sqlite3.connect(r'''__DB_PATH__''')
required = {'messages', 'recall_events', 'recall_cursor', 'mention_cooldowns'}
tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
missing = required - tables
if missing:
    raise SystemExit('missing tables: ' + ','.join(sorted(missing)))
print('tables-ok')
for row in db.execute('select group_id, count(*) from messages group by group_id order by group_id'):
    print('messages', row[0], row[1])
for row in db.execute('select group_id, last_event_id from recall_cursor order by group_id'):
    print('cursor', row[0], row[1])
db.close()
'@
    $dbScript = $dbScript.Replace('__DB_PATH__', $dbPath.Replace('\', '\\'))
    # Run the snippet from a file: passing multi-line Python through -c loses the
    # embedded quotes when the shell hands the argument to python.exe.
    $dbScriptFile = Join-Path ([System.IO.Path]::GetTempPath()) "qqbot-verify-db-$PID.py"
    [System.IO.File]::WriteAllText($dbScriptFile, $dbScript, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $dbResult = & $python $dbScriptFile 2>&1
    } finally {
        Remove-Item -LiteralPath $dbScriptFile -Force -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -eq 0 -and ($dbResult -join "`n") -match 'tables-ok') {
        Pass 'SQLite schema and archive database are readable.'
        $dbResult | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkGray }
    } else {
        Fail "SQLite check failed: $($dbResult -join ' ')"
    }
}

Write-Host ''
Write-Host "Failures: $script:Failures  Warnings: $script:Warnings" -ForegroundColor Cyan
if ($script:Failures -gt 0) { exit 1 }
exit 0
