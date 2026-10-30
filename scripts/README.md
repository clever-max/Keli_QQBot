# 脚本目录

本目录放本仓库的**全部 PowerShell 脚本**，按用途分四类。日常入口 `start-all.cmd` 与
停止脚本 `stop.ps1` 留在仓库根目录（双击 / 手敲都方便），其余一律归到下面的子目录。

| 分类 | 目录 | 管什么 |
|---|---|---|
| 启停与守护 | `lifecycle/` | 把机器人跑起来、停下来、掉了自动拉起 |
| 环境准备 | `setup/` | 一次性安装第三方组件 |
| 开发流程 | `dev/` | 验证、提交、登记 CHANGELOG、推送 |
| 运维与迁移 | `ops/` | 面向部署环境的检查脚本 |

---

## lifecycle —— 启停与守护

| 脚本 | 作用 | 用法 |
|---|---|---|
| `start-all.ps1` | 全套启动：校验 `.env` → 定位 NapCat 组件 → 把反向 WebSocket 配置写进 NapCat → 按需启动 NapCat Desktop → 调用 `start.ps1` 启动机器人 | `.\scripts\lifecycle\start-all.ps1`（或根目录双击 `start-all.cmd`） |
| `start.ps1` | **只启动机器人**：后台隐藏窗口运行 `bot.py`，PID 写入 `run\bot.pid`，日志 `logs\bot-<时间戳>.log` / `.err.log`。`.env` 或 `.venv` 缺失时拒绝启动 | `.\scripts\lifecycle\start.ps1` |
| `bot-supervisor.ps1` | 看门狗：15 秒轮询，机器人掉了就清理孤儿 python 进程并重启；同时采样网关 / DNS / WLAN 与 8080 回环会话，写 `logs\supervisor.log`（`NETWORK CHANGE` / `ONEBOT LOST` / `BOT DOWN` 等）。**只管本项目 python，不碰 NapCat 与 QQ** | `powershell -ExecutionPolicy Bypass -File .\scripts\lifecycle\bot-supervisor.ps1`；停止用 `New-Item -ItemType File -Force .\run\supervisor.stop` |
| `..\..\stop.ps1` | **留在仓库根**：按 `run\bot.pid` 停止机器人，并校验该 PID 确实是本项目的 `.venv\Scripts\python.exe`，否则拒绝执行 | `.\stop.ps1` |

> ⚠️ 看门狗**不是**开机自启的：本机没有计划任务、Run 键或启动项会拉起它。它一旦退出，
> 机器人再掉线就不会自愈——2026-10-15 因此停机约 48 分钟。详见
> [../docs/operations.md](../docs/operations.md) 的「看门狗自己也会死」。

## setup —— 环境准备

| 脚本 | 作用 | 用法 |
|---|---|---|
| `install-napcat.ps1` | 打开 `.setup\napcat\` 里那份**已核验**的 NapCat Desktop MSI 安装程序；文件缺失时直接报错，不会去网上随便下一个 | `.\scripts\setup\install-napcat.ps1` |

## dev —— 开发流程

| 脚本 | 作用 | 用法 |
|---|---|---|
| `commit-iteration.ps1` | 一条命令走完「验证 → 提交 → 登记 CHANGELOG → 可选推送」 | 见下 |

```powershell
# 常规：验证当前工作区 → 提交 → 登记 → 推送
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Message "feat: 增加 xxx" -Push

# 多行提交信息（必须用文件，别把多行字符串塞给 -Message）
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -MessageFile .\run\msg.txt

# 已经自己提交过了，只补登记
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Record 15dc236 -Note "补登记"
```

提交信息前缀（`feat` / `fix` / `docs` / `refactor` / `chore` …）与推送纪律见
[../docs/development.md](../docs/development.md) —— **那份文档是硬性规则所在**。

## ops —— 运维与迁移

| 脚本 | 作用 | 用法 |
|---|---|---|
| `verify-vps.ps1` | 部署环境自检：Python / 依赖 / `.env` / `bot.py` / 插件包 / 数据库 / `run\bot.pid` / 日志 / 端口等逐项 PASS / WARN / FAIL。`-Root` 默认指向 VPS 路径 `C:\Apps\QQBot`，**本机日常不用** | `.\scripts\ops\verify-vps.ps1 -Root C:\Apps\QQBot [-RequireOneBotConnected]` |

---

## 约定（新增或移动脚本前请读）

### 1. 仓库根目录靠「向上找 `bot.py`」推断，不靠层数

脚本一旦挪目录，`Split-Path -Parent` 这类按层数写死的写法就会指错地方。本目录所有需要
仓库根的脚本都统一用这段（**新增脚本请照抄**）：

```powershell
$root = $PSScriptRoot
while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'bot.py'))) {
    $parentDir = Split-Path -Parent $root
    if (-not $parentDir -or $parentDir -eq $root) { throw "找不到仓库根目录：$PSScriptRoot 之上没有 bot.py" }
    $root = $parentDir
}
```

`stop.ps1`（在根）走这段也会立刻停在根目录，所以留在哪一层都不影响。

### 2. 编码：`.ps1` 带 BOM，`.cmd` 与 `.md` 不带

- **`.ps1` 必须存成 UTF-8 带 BOM。** 少了 BOM，Windows PowerShell 5.1 会按 ANSI 代码页
  读文件，中文字符串字面量被解码成乱码，`switch` / 字符串分支会直接**语法报错**
  （实测：`commit-iteration.ps1` 的 `"chore" { return "工具链" }` 变成 `宸ュ叿閾?`）。
  PowerShell 7 能读无 BOM 的文件，但为了 5.1 兼容，这条要求不放松。
- **`.cmd` 不能带 BOM**：`cmd.exe` 会把 BOM 当成首行命令的一部分而报错。
- **Markdown 不带 BOM**（与仓库其余文档一致）。

用编辑器改完 `.ps1` 后，务必确认 BOM 还在：

```powershell
$b = [System.IO.File]::ReadAllBytes('.\scripts\lifecycle\start.ps1')
'{0:X2} {1:X2} {2:X2}' -f $b[0], $b[1], $b[2]   # 期望 EF BB BF
```

### 3. 移动脚本后的检查清单

1. 用 `git mv` 移动（保留历史）。
2. 确认脚本里的根目录推断仍是上面那段（不要按层数改 `..\..`）。
3. 全仓库搜索旧路径：`git grep -n "scripts\\<旧名>"`，更新 `docs/`、根 `README.md` 与本文件。
4. **不要改 `CHANGELOG.md`**：它只追加，历史条目里的旧路径属于历史。
5. 语法解析全部通过：
   ```powershell
   Get-ChildItem -Recurse -Filter *.ps1 | ForEach-Object {
       $e = $null
       [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$e) | Out-Null
       if ($e) { "$($_.Name): $($e[0].Message)" } else { "ok $($_.Name)" }
   }
   ```
6. 确认 BOM 没丢（见上）。

### 4. `verify-vps.ps1` 的那个旧引用已修

它原先检查 `plugins\group_bot.py`，而该文件在 `362edb9` 已被拆成包
（`plugins\group_bot\`），所以那条检查必然 FAIL。现已改为检查
`plugins\group_bot\__init__.py`。
