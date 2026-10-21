$ErrorActionPreference = 'Stop'
# 仓库根 = 向上第一个含 bot.py 的目录：脚本再挪目录也不会失效（见 scripts/README.md）。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$envPath = Join-Path $root '.env'
$desktopExe = Join-Path $root 'NapCat\NapCatQQ-Desktop.exe'
$expectedWsUrl = 'ws://127.0.0.1:8080/onebot/v11/ws'

if (-not (Test-Path -LiteralPath $envPath)) { throw 'Missing project .env.' }
$envLines = Get-Content -LiteralPath $envPath


function Get-EnvValue([string]$Name) {
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*='
    $line = $envLines | Where-Object { $_ -match $pattern } | Select-Object -First 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim()
}


$token = Get-EnvValue 'ONEBOT_ACCESS_TOKEN'
if (-not $token) { throw 'ONEBOT_ACCESS_TOKEN is missing from .env.' }

# --- Locate the NapCat component managed by NapCat Desktop -------------------
$candidates = @()
$configuredRoot = Get-EnvValue 'NAPCAT_COMPONENT_DIR'
if ($configuredRoot) { $candidates += $configuredRoot }
$candidates += (Join-Path $env:ProgramData 'NapCatQQ Desktop\components\NapCatQQ')

$napcatRoot = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath (Join-Path $candidate 'NapCatWinBootMain.exe')) { $napcatRoot = $candidate; break }
}
if (-not $napcatRoot) {
    throw "NapCat component not found (NapCatWinBootMain.exe). Checked: $($candidates -join '; ')"
}
$napcatExe = Join-Path $napcatRoot 'NapCatWinBootMain.exe'
$configDir = Join-Path $napcatRoot 'config'
if (-not (Test-Path -LiteralPath $configDir)) { throw "NapCat config directory not found: $configDir" }

# --- Work out which QQ account NapCat will log in as -------------------------
# Optional NAPCAT_UIN in .env wins; otherwise reuse the account of an existing
# onebot11_<uin>.json written by NapCat Desktop.
$accountId = Get-EnvValue 'NAPCAT_UIN'
if (-not $accountId) {
    $detected = Get-ChildItem -LiteralPath $configDir -Filter 'onebot11_*.json' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.BaseName -match '^onebot11_(\d+)$' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($detected) { $accountId = [regex]::Match($detected.BaseName, '^onebot11_(\d+)$').Groups[1].Value }
}

# --- Write the OneBot11 reverse WebSocket settings ---------------------------
# NapCat reads config\onebot11_<login uin>.json and falls back to the shared
# config\onebot11.json when that file does not exist yet. Keeping the shared file
# correct means a freshly created QQ account connects without any manual WebUI setup.
function New-ReverseWsClient([string]$Url, [string]$Token) {
    [pscustomobject]@{
        name              = 'nonebot'
        enable            = $true
        url               = $Url
        messagePostFormat = 'array'
        reportSelfMessage = $false
        reconnectInterval = 5000
        token             = $Token
        debug             = $false
        heartInterval     = 30000
    }
}


function Sync-OneBotConfig([string]$Path, [string]$Url, [string]$Token) {
    $existed = Test-Path -LiteralPath $Path
    $config = $null
    if ($existed) {
        try { $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { $config = $null }
        if (-not $config) { throw "Cannot parse existing NapCat config: $Path" }
    }
    if (-not $config) { $config = [pscustomobject]@{} }
    if (-not $config.PSObject.Properties['network']) {
        $config | Add-Member -NotePropertyName network -NotePropertyValue ([pscustomobject]@{})
    }
    if (-not $config.network.PSObject.Properties['websocketClients']) {
        $config.network | Add-Member -NotePropertyName websocketClients -NotePropertyValue @()
    }

    $clients = @($config.network.websocketClients)
    if (@($clients | Where-Object { $_.enable -and $_.url -eq $Url -and $_.token -eq $Token }).Count -gt 0) {
        return 'current'
    }

    # Reuse a client that already targets this URL, or one this script created before.
    $reusable = @($clients | Where-Object { $_.url -eq $Url -or $_.name -eq 'nonebot' }) | Select-Object -First 1
    if ($reusable) {
        $reusable.enable = $true
        $reusable.url = $Url
        $reusable.token = $Token
        if (-not $reusable.PSObject.Properties['messagePostFormat']) {
            $reusable | Add-Member -NotePropertyName messagePostFormat -NotePropertyValue 'array'
        }
        $action = 'repaired'
    } else {
        $config.network.websocketClients = @($clients) + (New-ReverseWsClient -Url $Url -Token $Token)
        $action = 'added'
    }

    if ($existed) { Copy-Item -LiteralPath $Path -Destination "$Path.bak" -Force }
    $json = $config | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    return $action
}


function Test-OneBotConfig([string]$Path, [string]$Url, [string]$Token) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try { $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $false }
    return @($config.network.websocketClients | Where-Object { $_.enable -and $_.url -eq $Url -and $_.token -eq $Token }).Count -gt 0
}


# NapCat Desktop keeps the authoritative OneBot11 settings in its own bot.json and
# rewrites config\onebot11_<uin>.json from it every time the bot is started, so the
# reverse WebSocket client has to live there to survive a Desktop-side restart.
function Sync-DesktopBotConfig([string]$DataRoot, [string]$Url, [string]$Token) {
    $path = Join-Path $DataRoot 'config\bot.json'
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    try { $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { throw "Cannot parse $path" }
    if (-not $state.PSObject.Properties['bots']) { return @() }

    $touched = @()
    foreach ($entry in @($state.bots)) {
        if (-not $entry.PSObject.Properties['connect'] -or -not $entry.connect) { continue }
        if (-not $entry.connect.PSObject.Properties['websocketClients']) {
            $entry.connect | Add-Member -NotePropertyName websocketClients -NotePropertyValue @()
        }
        $clients = @($entry.connect.websocketClients)
        if (@($clients | Where-Object { $_.enable -and $_.url -eq $Url -and $_.token -eq $Token }).Count -gt 0) { continue }

        $reusable = @($clients | Where-Object { $_.url -eq $Url -or $_.name -eq 'nonebot' }) | Select-Object -First 1
        if ($reusable) {
            $reusable.enable = $true
            $reusable.url = $Url
            $reusable.token = $Token
        } else {
            $entry.connect.websocketClients = @($clients) + (New-ReverseWsClient -Url $Url -Token $Token)
        }
        $label = if ($entry.PSObject.Properties['bot'] -and $entry.bot) { $entry.bot.name } else { 'bot' }
        $touched += "bot.json: $label"
    }

    if ($touched.Count -gt 0) {
        Copy-Item -LiteralPath $path -Destination "$path.bak" -Force
        $json = $state | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
    return $touched
}


$changed = @()
$dataRoot = Split-Path -Parent (Split-Path -Parent $napcatRoot)
$desktopChanges = @(Sync-DesktopBotConfig -DataRoot $dataRoot -Url $expectedWsUrl -Token $token)
$changed += $desktopChanges

$defaultConfig = Join-Path $configDir 'onebot11.json'
$action = Sync-OneBotConfig -Path $defaultConfig -Url $expectedWsUrl -Token $token
if ($action -ne 'current') { $changed += "$defaultConfig ($action)" }

$accountConfig = ''
if ($accountId) {
    $accountConfig = Join-Path $configDir "onebot11_$accountId.json"
    $action = Sync-OneBotConfig -Path $accountConfig -Url $expectedWsUrl -Token $token
    if ($action -ne 'current') { $changed += "$accountConfig ($action)" }
}

$effectiveConfig = if ($accountConfig -and (Test-Path -LiteralPath $accountConfig)) { $accountConfig } else { $defaultConfig }
if (-not (Test-OneBotConfig -Path $effectiveConfig -Url $expectedWsUrl -Token $token)) {
    throw "OneBot11 reverse WebSocket client is not configured correctly in $effectiveConfig"
}

if ($changed.Count -gt 0) {
    Write-Output 'OneBot11 reverse WebSocket configuration written:'
    $changed | ForEach-Object { Write-Output "  $_" }
} else {
    Write-Output 'OneBot11 reverse WebSocket configuration already up to date.'
}
Write-Output "Effective OneBot11 config: $effectiveConfig"

# A running Desktop keeps bot.json in memory and can write its own copy back, so
# tell the user when the change it just made may not stick.
$desktopRunning = @(Get-CimInstance Win32_Process -Filter "Name='NapCatQQ-Desktop.exe'") | Select-Object -First 1
if ($desktopRunning -and $desktopChanges.Count -gt 0) {
    Write-Output 'NapCat Desktop is running and did not have these settings yet: restart the Desktop and start the bot so it reloads bot.json.'
}

if (-not (Test-Path -LiteralPath $desktopExe) -or -not (Test-Path -LiteralPath $napcatExe)) {
    throw 'NapCat Desktop or its managed NapCat component is missing.'
}

$napcatAlreadyRunning = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessName -eq 'NapCatWinBootMain.exe' -and $_.ExecutablePath -eq $napcatExe
} | Select-Object -First 1
if ($napcatAlreadyRunning -and $changed.Count -gt 0) {
    Write-Output 'NapCat is already running: restart it (or restart QQ) so the new network settings take effect.'
}

& (Join-Path $root 'scripts\lifecycle\start.ps1')
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'Bot startup failed.' }
Start-Sleep -Seconds 2

if ($napcatAlreadyRunning) {
    Write-Output "NapCat is already running (PID $($napcatAlreadyRunning.ProcessId)); no duplicate instance started."
} else {
    $desktop = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessName -eq 'NapCatQQ-Desktop.exe' -and $_.ExecutablePath -eq $desktopExe
    } | Select-Object -First 1
    if (-not $desktop) {
        Start-Process -FilePath $desktopExe -WorkingDirectory (Split-Path -Parent $desktopExe) | Out-Null
        Start-Sleep -Seconds 8
        $napcatAlreadyRunning = Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessName -eq 'NapCatWinBootMain.exe' -and $_.ExecutablePath -eq $napcatExe
        } | Select-Object -First 1
    }
    if ($napcatAlreadyRunning) {
        Write-Output "NapCat Desktop started its managed instance (PID $($napcatAlreadyRunning.ProcessId))."
    } else {
        Write-Output 'NapCat Desktop is open. Start the local NapCat bot from its dashboard once; do not launch NapCatWinBootMain.exe directly.'
    }
}
