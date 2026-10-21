$ErrorActionPreference = 'Stop'
# 仓库根 = 向上第一个含 bot.py 的目录：脚本再挪目录也不会失效（见 scripts/README.md）。
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
$installer = Join-Path $root '.setup\napcat\NapCatQQ-Desktop-3.3.0-x64.msi'
if (-not (Test-Path -LiteralPath $installer)) {
    throw 'Verified NapCat Desktop installer is missing. Re-download it from the official NapCatQQ Desktop GitHub release.'
}
Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', "`"$installer`"")
Write-Output 'NapCatQQ Desktop setup opened. After installation, create the local NapCat bot and sign in to QQ yourself.'
