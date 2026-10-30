#requires -Version 5.1
<#
    commit-iteration.ps1 — 可运行迭代的「验证 → 提交 → 登记」一条命令

    为什么要有它
    ------------
    项目约定：每拿到一个可运行的迭代就在 main 上提交一次，并把这次提交登记到根目录
    CHANGELOG.md。手工做这件事有两处容易漏：忘了先验证「能起来」，以及提交完忘了登记。
    这个脚本把三步连起来，并把验证结果一起写进记录里。

    三个约定
    --------
    1. 「可运行」的判据是两项检查，脚本每次都真跑：
         python -m compileall plugins                     语法能过
         nonebot.init() + load_plugins("plugins")         插件真的被 NoneBot 加载
       它只证明「能起来」，不代替真机联调（扫码登录、在群里发命令）。
    2. CHANGELOG 里要写这次迭代的提交哈希，而哈希只有提交之后才知道，所以登记本身
       作为紧随其后的 docs(changelog) 提交存在，并且这条记录提交不再单独登记。
    3. 新记录插在 CHANGELOG.md 的 ITERATION-INSERT-POINT 标记下面，最新的在最上面；
       小节标题按提交信息的 conventional-commit 前缀推断，可用 -Section 覆盖。

    用法
    ----
      这台机器同时装有 Windows PowerShell 5.1（命令是 powershell）和 PowerShell 7
      （命令是 pwsh，7.6.8）；两者都能跑，下面的示例用 powershell，换成 pwsh 即可。

      # 常规：验证当前工作区 → 提交 → 登记 → （可选）推送
      powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Message "feat: 增加 xxx"

      # 多行提交信息 + 记录里多写几条要点 + 提交后推送
      powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 `
        -Message "fix: 修正 xxx`n`n细节说明" -Note "影响范围：撤回记录" -Push

      # 已经自己提交过了，只补登记
      powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Record 15dc236 -Note "补登记"

      # 环境起不来时跳过验证（不推荐，记录里会写明跳过了）
      powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Message "docs: ..." -SkipVerify

    多行提交信息：不要直接把带引号的多行字符串塞给 -Message。用 powershell -File 调用时，
    参数要重新拼成一条命令行，信息里的引号和换行会被拆坏（实测会把消息后半段当成位置参数，
    报成"补登记已有提交 pwsh"这种莫名其妙的错）。把信息写进一个 UTF-8 文件再交给
    -MessageFile 最稳，文件本身不受 git 跟踪也不影响提交内容：

      Set-Content -Path run\iteration-message.txt -Value "fix: 修正 xxx`n`n细节" -Encoding UTF8
      powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -MessageFile run\iteration-message.txt

    （run\ 已在 .gitignore 里，不会混进提交。）

    退出码：0 成功；1 验证失败 / 参数不对 / git 报错（此时不会有任何提交或登记）。

    注意：本机默认禁止运行脚本，所以要用 powershell -ExecutionPolicy Bypass -File（或先
    Set-ExecutionPolicy -Scope Process Bypass）。

    编码：本文件必须保持「UTF-8 带 BOM」。本机的 powershell 是 Windows PowerShell 5.1、
    ANSI 代码页 936；没有 BOM 的 UTF-8 脚本会被按 GBK 解读——中文全变乱码，个别字节还会
    直接造成语法错误。带 BOM 的 UTF-8 在 PS 5.1 与 PS 7 下都能正确读取；用记事本或
    VS Code 另存时请保持 BOM。
#>

[CmdletBinding()]
param(
    # 提交信息。多行用 "第一行`n第二行" 或直接多行字符串。
    [string]$Message = "",

    # 从文件读提交信息（UTF-8）。多行、或信息里含引号时用这个，见头部「用法」里的说明。
    [string]$MessageFile = "",

    # 只补登记一个已经存在的提交，不做验证、不产生代码提交。
    [string]$Record = "",

    # 追加到记录里的补充要点，一行一条，会跟在提交标题后面。
    [string]$Note = "",

    # 覆盖记录里的「### 小节」标题，例如 新增 / 修复 / 文档 / 重构 / 工具链。
    [string]$Section = "",

    # 覆盖记录里的「可运行验证」那句话。
    [string]$VerifySummary = "",

    # 跳过两项检查（记录里会写明跳过）。
    [switch]$SkipVerify,

    # 登记提交完成后推送当前分支到 origin。
    [switch]$Push
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Fail([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

# 外部命令（git / python）的 stderr 在 PowerShell 里会被包成 ErrorRecord，
# 配合 $ErrorActionPreference=Stop 会直接终止脚本，所以这两个包装器内部临时放开。
function Invoke-External {
    param([string]$Exe, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Exe @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
    return [pscustomobject]@{ Output = $output; Code = $code }
}

function Invoke-Git {
    param([string[]]$Arguments)
    $result = Invoke-External -Exe "git" -Arguments (@("-C", $script:Root) + $Arguments)
    if ($result.Code -ne 0) {
        Write-Host $result.Output.Trim() -ForegroundColor Red
        Fail "git $($Arguments -join ' ') 失败（退出码 $($result.Code)）"
    }
    return $result.Output.Trim()
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Read-Utf8([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

# 按 conventional-commit 前缀决定记录里的小节标题；认不出来就归到「变更」。
function Get-SectionTitle([string]$Text) {
    $first = ($Text -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1)
    if ($first -match '^\s*([A-Za-z]+)(\([^)]*\))?!?:') {
        switch ($Matches[1].ToLower()) {
            "feat" { return "新增" }
            "fix" { return "修复" }
            "refactor" { return "重构" }
            "perf" { return "优化" }
            "docs" { return "文档" }
            "test" { return "测试" }
            "style" { return "格式" }
            "chore" { return "工具链" }
            "build" { return "工具链" }
            "ci" { return "工具链" }
            "revert" { return "回滚" }
        }
    }
    return "变更"
}

# ---------------------------------------------------------------- 0. 定位仓库
# 仓库根 = 向上第一个含 bot.py 的目录：脚本再挪目录也不会失效（见 scripts/README.md）。
$script:Root = $PSScriptRoot
while ($script:Root -and -not (Test-Path -LiteralPath (Join-Path $script:Root "bot.py"))) {
    $parentDir = Split-Path -Parent $script:Root
    if (-not $parentDir -or $parentDir -eq $script:Root) {
        Fail "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py"
        exit 1
    }
    $script:Root = $parentDir
}
if (-not (Test-Path (Join-Path $script:Root ".git"))) { Fail "找不到 git 仓库：$script:Root" }
$changelogPath = Join-Path $script:Root "CHANGELOG.md"
if (-not (Test-Path $changelogPath)) { Fail "找不到根目录 CHANGELOG.md，无法登记迭代。" }

if (-not [string]::IsNullOrWhiteSpace($MessageFile)) {
    $resolvedMessageFile = $MessageFile
    if (-not [System.IO.Path]::IsPathRooted($resolvedMessageFile)) {
        $resolvedMessageFile = Join-Path $script:Root $resolvedMessageFile
    }
    if (-not (Test-Path $resolvedMessageFile)) { Fail "找不到 -MessageFile 指定的文件：$resolvedMessageFile" }
    $Message = (Read-Utf8 $resolvedMessageFile).TrimEnd()
    if ([string]::IsNullOrWhiteSpace($Message)) { Fail "-MessageFile 里的内容是空的：$resolvedMessageFile" }
}

if ([string]::IsNullOrWhiteSpace($Message) -and [string]::IsNullOrWhiteSpace($Record)) {
    Fail '至少要给 -Message "提交信息"，或用 -MessageFile <文件>，或用 -Record <提交> 只补登记。'
}

$python = Join-Path $script:Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
    Write-Host "提示：没找到 .venv\Scripts\python.exe，改用 PATH 里的 python。" -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $script:Root ".env"))) {
    Write-Host "提示：仓库根目录没有 .env，插件可能因缺少配置而加载失败。" -ForegroundColor Yellow
}

$tempFiles = New-Object System.Collections.Generic.List[string]

try {
    $hash = ""
    $subject = ""
    $verifyText = ""
    $sectionTitle = ""

    if ([string]::IsNullOrWhiteSpace($Record)) {
        # ---------------------------------------------------- 1. 验证能起来
        if ($SkipVerify) {
            Write-Step "跳过可运行验证（-SkipVerify）"
            $verifyText = "已跳过（提交时用了 -SkipVerify，未跑检查）。"
        }
        else {
            Write-Step "验证：python -m compileall plugins"
            Push-Location $script:Root
            try { $compile = Invoke-External -Exe $python -Arguments @("-m", "compileall", "-q", "plugins") }
            finally { Pop-Location }
            if ($compile.Code -ne 0) {
                Write-Host $compile.Output.Trim() -ForegroundColor Red
                Fail "语法检查没通过，先修好再提交（本次没有产生任何提交）。"
            }
            Write-Ok "语法检查通过"

            Write-Step "验证：配置三处一致（config.py / .env.example / configuration.md）"
            Push-Location $script:Root
            try {
                $configCheck = Invoke-External -Exe $python -Arguments @("scripts\dev\verify_config_docs.py")
            }
            finally { Pop-Location }
            if ($configCheck.Code -ne 0) {
                Write-Host $configCheck.Output.Trim() -ForegroundColor Red
                Fail "配置三处不一致：新加的键要同时写进 .env.example 与 docs\configuration.md（本次没有产生任何提交）。"
            }
            Write-Ok "配置三处一致"

            Write-Step "验证：tests\run_all.py（行为套件）"
            Push-Location $script:Root
            try {
                $suites = Invoke-External -Exe $python -Arguments @("tests\run_all.py")
            }
            finally { Pop-Location }
            if ($suites.Code -ne 0) {
                Write-Host $suites.Output.Trim() -ForegroundColor Red
                Fail "行为套件没通过，先修好再提交（本次没有产生任何提交）。"
            }
            Write-Ok "行为套件全绿"

            Write-Step "验证：nonebot 加载 plugins"
            $smokePath = Join-Path $env:TEMP ("qqbot-smoke-" + [guid]::NewGuid().ToString("N") + ".py")
            $tempFiles.Add($smokePath)
            Write-Utf8NoBom $smokePath @'
import os
import sys

# NoneBot 是按模块名导入插件的，仓库根目录必须在 sys.path 上。这个冒烟脚本自己住在临时目录，
# 所以不能靠 sys.path[0]，否则 "plugins" 根本导入不进来。
root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
sys.path.insert(0, root)
os.chdir(root)

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)

# 关键：load_plugins 会把导入异常吞掉、只返回"成功加载的那部分"，导入失败时它安静地返回空集合。
# 所以这里必须自己断言确实加载到了预期的插件，否则这个检查会变成假绿灯。
loaded = nonebot.load_plugins("plugins")
names = sorted(getattr(plugin, "name", str(plugin)) for plugin in loaded)
print("PLUGINS_LOADED:", names)

EXPECTED = ["group_bot"]
missing = [name for name in EXPECTED if name not in names]
if missing:
    # 只用 ASCII：这段输出跑在 GBK 控制台代码页下，中文会变成乱码。
    print("SMOKE_FAIL: plugins not loaded:", missing)
    print("(load_plugins swallows import errors - see nonebot's ERROR log above for the cause.)")
    sys.exit(2)
print("SMOKE_OK")
'@
            Push-Location $script:Root
            try { $smoke = Invoke-External -Exe $python -Arguments @($smokePath, $script:Root) }
            finally { Pop-Location }
            if ($smoke.Code -ne 0 -or $smoke.Output -notmatch "SMOKE_OK") {
                Write-Host (($smoke.Output.Trim() -split "`r?`n" | Select-Object -Last 15) -join "`n") -ForegroundColor Red
                Fail "插件没能被 NoneBot 加载，先修好再提交（本次没有产生任何提交）。"
            }
            $loadedNames = ([regex]::Match($smoke.Output, "PLUGINS_LOADED: \[(.*?)\]")).Groups[1].Value
            if ([string]::IsNullOrWhiteSpace($loadedNames)) {
                Write-Host $smoke.Output.Trim() -ForegroundColor Red
                Fail "插件加载结果为空，按失败处理（本次没有产生任何提交）。"
            }
            Write-Ok "已加载插件：[$loadedNames]"
            $verifyText = "``python -m compileall plugins`` 通过；``nonebot.init()`` + ``load_plugins(""plugins"")`` 成功加载 [$loadedNames]。"
        }

        # ---------------------------------------------------- 2. 提交本次迭代
        Write-Step "提交本次迭代"
        $null = Invoke-Git @("add", "-A")
        $staged = Invoke-Git @("status", "--porcelain")
        if ([string]::IsNullOrWhiteSpace($staged)) {
            Write-Host "工作区本来就没有改动，没有可提交的内容。" -ForegroundColor Yellow
            $hash = Invoke-Git @("rev-parse", "--short", "HEAD")
            $subject = Invoke-Git @("log", "-1", "--format=%s")
            $sectionTitle = Get-SectionTitle $subject
            Write-Host "改为登记当前 HEAD：$hash" -ForegroundColor Yellow
        }
        else {
            $msgPath = Join-Path $env:TEMP ("qqbot-commit-msg-" + [guid]::NewGuid().ToString("N") + ".txt")
            $tempFiles.Add($msgPath)
            Write-Utf8NoBom $msgPath $Message
            $null = Invoke-Git @("commit", "-F", $msgPath)
            $hash = Invoke-Git @("rev-parse", "--short", "HEAD")
            $subject = Invoke-Git @("log", "-1", "--format=%s")
            $sectionTitle = Get-SectionTitle $Message
            Write-Ok "已提交 $hash  $subject"
        }
    }
    else {
        # ---------------------------------------------------- 只补登记已有提交
        Write-Step "补登记已有提交 $Record"
        $hash = Invoke-Git @("rev-parse", "--short", $Record)
        $subject = Invoke-Git @("log", "-1", "--format=%s", $Record)
        $sectionTitle = Get-SectionTitle $subject
        if ([string]::IsNullOrWhiteSpace($VerifySummary)) {
            $verifyText = "见该提交的说明（本次为补登记，未重跑检查）。"
        }
        Write-Ok "目标提交 $hash  $subject"
    }

    if (-not [string]::IsNullOrWhiteSpace($VerifySummary)) { $verifyText = $VerifySummary }
    if (-not [string]::IsNullOrWhiteSpace($Section)) { $sectionTitle = $Section }

    # -------------------------------------------------------- 3. 登记到 CHANGELOG
    Write-Step "登记到 CHANGELOG.md"
    $changelog = Read-Utf8 $changelogPath
    $eol = if ($changelog -match "`r`n") { "`r`n" } else { "`n" }

    $numbers = [regex]::Matches($changelog, "(?m)^##\s*\[迭代\s*(\d+)\]") | ForEach-Object { [int]$_.Groups[1].Value }
    $next = if ($numbers.Count -gt 0) { ($numbers | Measure-Object -Maximum).Maximum + 1 } else { 1 }

    $date = (Get-Date).ToString("yyyy-MM-dd")
    $entryLines = New-Object System.Collections.Generic.List[string]
    $entryLines.Add("## [迭代 $next] - $date — ``$hash``")
    $entryLines.Add("")
    if (-not [string]::IsNullOrWhiteSpace($verifyText)) {
        $entryLines.Add("可运行验证：$verifyText")
        $entryLines.Add("")
    }
    $entryLines.Add("### $sectionTitle")
    $entryLines.Add("")
    $entryLines.Add("- $subject")
    if (-not [string]::IsNullOrWhiteSpace($Note)) {
        foreach ($line in ($Note -split "`r?`n")) {
            if (-not [string]::IsNullOrWhiteSpace($line)) { $entryLines.Add("- " + $line.Trim()) }
        }
    }
    $entry = ($entryLines -join $eol)

    $markerMatch = [regex]::Matches($changelog, "(?m)^.*ITERATION-INSERT-POINT.*$") | Select-Object -First 1
    if (-not $markerMatch) { Fail "CHANGELOG.md 里找不到 ITERATION-INSERT-POINT 标记，无法插入新记录。" }

    $changelog = $changelog.Replace($markerMatch.Value, $markerMatch.Value + $eol + $eol + $entry)
    Write-Utf8NoBom $changelogPath $changelog
    Write-Ok "已写入 [迭代 $next]（提交 $hash）"

    $null = Invoke-Git @("add", "--", "CHANGELOG.md")
    $null = Invoke-Git @("commit", "-m", "docs(changelog): 登记迭代 $next（$hash）")
    $recordHash = Invoke-Git @("rev-parse", "--short", "HEAD")
    Write-Ok "记录提交 $recordHash"

    # -------------------------------------------------------- 4. 推送（可选）
    if ($Push) {
        Write-Step "推送到 origin"
        $branch = Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD")
        $null = Invoke-Git @("push", "origin", $branch)
        Write-Ok "已推送 $branch"
    }

    Write-Host ""
    Write-Host "完成：迭代 $next  ->  $hash  $subject" -ForegroundColor Green
    Write-Host "      记录提交  ->  $recordHash" -ForegroundColor Green
    if (-not $Push) { Write-Host "      还没推送：需要时运行 git push origin main" -ForegroundColor Yellow }
}
finally {
    foreach ($path in $tempFiles) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
}
