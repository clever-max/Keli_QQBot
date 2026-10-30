# 运行与运维

本文描述 `<repo>` 在本机（Windows 10 专业版）上的**启动、停止、观测与排障**。
开发规范与提交流程见 [development.md](development.md)，文档索引见 [README.md](README.md)。

本机事实：同时装有 Windows PowerShell 5.1（命令是 `powershell`）和 PowerShell 7
（命令是 `pwsh`，当前 7.6.8），脚本两者都能跑；
Python 环境是仓库内的 `.venv`（注意 `.venv` 不能跨机器/跨目录复制，`pyvenv.cfg` 会指向原路径）。

---

## 1. 组成与数据流

```
NapCat Desktop  ──反向 WebSocket──▶  NoneBot（bot.py）
C:\ProgramData\      ws://127.0.0.1:8080
NapCatQQ Desktop\    /onebot/v11/ws
      │
      └─ 登录 QQ（QQNT 客户端）
```

- **NapCat Desktop**：QQ 登录端 + OneBot v11 实现，装在 `C:\ProgramData\NapCatQQ Desktop`。
  它主动**反向**连到 NoneBot 的 8080 端口，所以消息是 NapCat 推给 NoneBot 的。
- **NoneBot**：由 `bot.py` 启动，加载 `plugins/`（只有 `plugins/group_bot`），
  业务与数据全在本地。
- **SQLite**：`data/messages.sqlite3`，存消息归档、撤回事件、冷却与开关等状态。

---

## 2. 启动与停止

| 入口 | 作用 |
|---|---|
| `start-all.cmd` | 一键入口（留在仓库根，方便双击）。实际是 `powershell -ExecutionPolicy Bypass -File scripts\lifecycle\start-all.ps1`，失败时 `pause` |
| `scripts/lifecycle/start-all.ps1` | 校验 `.env` → 定位 NapCat 组件 → 写反向 WebSocket 配置 → 按需启动 NapCat Desktop |
| `scripts/lifecycle/start.ps1` | **只启动机器人进程**（后台、隐藏窗口），把 PID 写入 `run\bot.pid`，日志写入 `logs\bot-<时间戳>.log` / `.err.log` |
| `stop.ps1` | **停机**。看门狗在跑时只写 `run\bot.stop`（由提权的看门狗杀进程），不需要提权；看门狗没跑时才按 `run\bot.pid` 直接停 |
| `scripts/lifecycle/restart.ps1` | **重启**（日常用这个）：写 `run\bot.restart`，看门狗在 ≤15 秒内杀旧进程并拉起新的，**不需要提权** |
| `scripts\lifecycle\bot-supervisor.ps1` | 看门狗：机器人掉了自动拉起；响应 `run\bot.restart`（重启）与 `run\bot.stop`（停机并退出）；`run\supervisor.stop` 让看门狗自己退出 |
| `scripts\ops\verify-vps.ps1` | 面向 VPS 迁移的检查脚本（默认 `-Root C:\Apps\QQBot`），本机不使用 |

> ⚠️ **提权带来的操作限制（2026-10-18 起）**：开机自启的计划任务 `QQBotSupervisor` 用
> `/rl highest` 启动，所以机器人和看门狗都是**提权进程**。非提权 shell 既看不到它们的
> 命令行/路径，也**杀不掉**（`Access is denied`）。因此：
> - 重启 → `scripts\lifecycle\restart.ps1`（写文件让看门狗干活，无需提权）；
> - 停机 → `stop.ps1`（同上）；
> - 彻底结束计划任务 / 重建任务 → 需要管理员：`schtasks /end /tn "QQBotSupervisor"`；
> - **重启机器后必须登录一次**（任务触发器是 `onlogon`），否则机器人不会自己起来。

### 2.1 `scripts/lifecycle/start-all.ps1` 做了什么

1. 读 `.env`，缺 `ONEBOT_ACCESS_TOKEN` 直接抛错。
2. 定位 NapCat 组件目录：`.env` 的 `NAPCAT_COMPONENT_DIR` 优先，否则找
   `%ProgramData%\NapCatQQ Desktop\components\NapCatQQ`，要求存在 `NapCatWinBootMain.exe`。
3. 确定要登录的 QQ 号：`.env` 的 `NAPCAT_UIN` 优先，否则复用已有的 `config\onebot11_<uin>.json`。
4. 把反向 WebSocket 设置写进 NapCat 的 `config\onebot11.json` 与 `config\onebot11_<uin>.json`：
   `ws://127.0.0.1:8080/onebot/v11/ws`，token 取 `.env` 的 `ONEBOT_ACCESS_TOKEN`。
   注意 NapCat Desktop 以自己 `bot.json` 为准，并会在每次启动时重写 `onebot11_<uin>.json`。
5. NapCat 已在运行时，若配置有变化会提示**重启 NapCat / QQ** 让新设置生效；
   没运行则启动 `NapCat\NapCatQQ-Desktop.exe`。
   它**不会**直接启动 `NapCatWinBootMain.exe`——若提示需要手动操作，到 Desktop 面板里启动一次本机 Bot。

### 2.2 看门狗 `scripts\lifecycle\bot-supervisor.ps1`

- **15 秒轮询**一次。
- 读 `run\bot.pid` 判断机器人是否存活；掉了就先 `taskkill` 清理仍占着端口的孤儿 python 进程，
  再用新的时间戳日志重启，并把「退出码 + stderr 尾部」写进 `logs\supervisor.log`。
- 顺带采样网络健康：默认网关、DNS（`q1.qlogo.cn`）、WLAN 网卡状态，以及 8080 上是否有已建立的
  OneBot 回环连接；状态变化时记录 `NETWORK CHANGE` / `ONEBOT LOST` / `ONEBOT RECONNECTED`。
- 只管本项目的 Python 进程，**不碰 NapCat、不碰 QQ**。
- 停止方式：创建 `run\supervisor.stop`（或直接杀掉这个 PowerShell 进程）。

```powershell
# 启动
powershell -ExecutionPolicy Bypass -File .\scripts\lifecycle\bot-supervisor.ps1
# 停止
New-Item -ItemType File -Force .\run\supervisor.stop
```

#### ⚠️ 看门狗自己也会死，而且没有任何东西会拉起它

**2026-10-15 实测事故**：`logs\supervisor.log` 在当天 11:38 之后再没有任何记录（看门狗进程已不在），
机器人于 **19:23 死亡**后无人接管，服务中断约 **48 分钟**，直到人工重新启动看门狗。

已核实的事实：本机**没有**任何机制自动启动看门狗——计划任务里没有，`HKCU`/`HKLM` 的 `Run` 键没有，
启动文件夹里也没有。也就是说：

- 重启电脑后，NapCat/QQ、看门狗、机器人都需要**人工拉起**；
- 看门狗一旦退出，机器人再掉线就**不会**自愈，而 `supervisor.log` 里不会有任何"我退出了"的记录。

**正确的存活检查**（注意：下面这条命令的过滤字符串会匹配到**执行它的这个 shell 自身**，
必须排除 `$PID`，否则无论看门狗在不在都会"命中"）：

```powershell
$self = $PID
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*bot-supervisor.ps1*' -and $_.ProcessId -ne $self } |
  Select-Object ProcessId, CreationDate
```

判断机器人是否在线，以 `Get-NetTCPConnection -State Established -LocalPort 8080` 出现回环会话为准——
它同时说明 NoneBot 起来了、NapCat 也连上了。

**加固建议（尚未实施）**：建一个计划任务，触发器用「用户登录时」+「失败后每 5 分钟重试」，
操作为 `powershell -ExecutionPolicy Bypass -File <repo>\scripts\lifecycle\bot-supervisor.ps1`；
这样重启或看门狗意外退出后都能自愈。`start-all.cmd` 与 `scripts/lifecycle/start-all.ps1` 目前都**不**注册自启。

---

## 3. 本机路径与产物

| 位置 | 内容 | 说明 |
|---|---|---|
| `data\messages.sqlite3` | 消息归档、撤回事件、游标、冷却、开关、**多媒体索引** | WAL 模式，另有 `-wal` / `-shm` |
| `data\media_archive\<2位>\<2位>\<sha256>.<ext>` | **多媒体永久归档**（图片/视频/语音/文件） | 内容寻址、同内容只存一份；**永不清理**，见 §3.1 |
| `data\media_cache\` | **派生**的渲染缓存（消息图片、相册照片） | 相册照片按 `album:<media_id>` 的散列命名。**可随时整个删掉**：渲染时由 `media.cache_message_images` 优先从 `media_archive` 复制回来（复制不到才联网）。不再是唯一副本 |
| `data\voice\` | 语音点歌的曲库 | 文件名即歌名；`.trimmed\` 是自动截断后的副本（可删可重建） |
| `data\avatars\<群号>\` | 群成员头像缓存 | 机器人连接时预热，新成员加入时补缓存 |
| `data\card_images\` | 卡片临时图片 | **发送后立即删除**，正常应为空 |
| `data\face_cache\` | QQ 内置表情图缓存（64px PNG） | 从本机 QQ 客户端或腾讯 CDN 取的派生缓存，**可随时整个删掉重建**；`s<id>.missing` 表示该 id 确认没有图，删掉它会重新探测 |
| `logs\bot-<时间戳>.log` | 机器人 stdout | 事件与业务日志 |
| `logs\bot-<时间戳>.err.log` | 机器人 stderr | **插件加载失败、未捕获异常看这里** |
| `logs\supervisor.log` | 看门狗的追加日志 | 重启与网络状态变化的时间线 |
| `run\bot.pid` | 当前机器人 PID | `stop.ps1` / supervisor 依据 |
| `run\supervisor.stop` | 存在即让看门狗退出 | |

上述 `data\`、`logs\`、`run\`、`.env` 都在 `.gitignore` 里，**不要提交**。

### 3.1 数据生命周期：全部永久保存，没有任何清理作业

从 2026-10-18 起，"所有消息（含多媒体）永久保存"是**明确承诺**，因此：

| 数据 | 保留策略 |
|---|---|
| `messages` 行 | **永久**。没有任何 `DELETE FROM messages`；只有 `essence_draws`（抽取历史）按 `BOTTLE_HISTORY_DAYS` 过期，那是统计口径，不是消息 |
| 多媒体文件 | **永久**。收到消息即归档到 `data\media_archive\`，内容寻址去重；**没有 TTL、没有配额、没有清理脚本** |
| 头像 / 卡片图 | 头像可重建（缓存）；卡片图发送即删（临时产物） |
| 日志 | 各保留 30 个（`bot.py` 启动时轮转），不是业务数据 |

**归档的机制**（`plugins/group_bot/archive.py`）：消息落库后立刻起后台任务下载其中的
图片/视频/语音/文件，按 `sha256` 存一份，并在 `message_media` 表里记一行
（`group_id / message_id / part_index / kind / sha256 / size / suffix / source_url / error`）。
下载失败或超过 `ARCHIVE_MAX_BYTES` 也**留一行带原因的记录**，而不是假装"没有多媒体"。
崩溃或漏掉的会在启动后与每 10 分钟被扫一遍补齐（`messages.archived_at` 标记进度）。

**三条必须知道的边界**：

1. **历史媒体救不回来**。QQ 的媒体 URL 会过期。2026-10-18 实测：对库里旧消息做补归档，
   149 个媒体段里 **132 个失败**（URL 已失效）。所以"永久保存"的准确含义是
   **从这一刻起收到的消息**能永久保存；存量里只有当时恰好被缓存过的还活着。
2. **超过 `ARCHIVE_MAX_BYTES`（默认 100 MiB）的单文件不落盘**，只记原因。要改就设大它，
   别指望事后补救。
3. **磁盘会一直涨**。当前约 20 MB（新归档）+ 234 MB（旧 `media_cache`），`D:` 还有 680 GB。
   涨满时应当**加盘/迁移**，而不是删归档——删了就是违约。

**备份**：`messages.sqlite3` + `data\media_archive\` **必须一起备份**。只备数据库会丢失
媒体，只备媒体会丢失"哪条消息对应哪张图"的映射。`data\media_cache` 与 `data\avatars`
可以不备（渲染缓存/可重建）。

---

## 4. 常见故障与处置

### 4.1 插件加载失败

先看 **stderr**（`logs\bot-<时间戳>.err.log`）与 stdout 里的
`Failed to import "group_bot"` / `Traceback`。常见原因是语法或缩进错误、
`.env` 缺少必需配置（`ALLOWED_GROUP_IDS`、`AUTHORIZED_USERS`/`GROUP_AUTHORIZED_USERS`、
`ONEBOT_ACCESS_TOKEN`）。改完可以用脚本的两项检查快速确认再提交：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Message "fix: ..."
```

### 4.2 相册接口不可用

`漂流瓶` 的相册那一半依赖 NapCat 的 `get_qun_album_list` 与 `get_group_album_media_list`。
读不到时日志会出现：

```
get_qun_album_list failed for group ...
get_group_album_media_list failed for group ... album ...
```

**这是降级而非致命**：该来源按 0 条处理，抽取会只用精华；`漂流瓶 相册` 则会明确回一句
「相册被关掉了」/「相册里还没有照片」，不会静默也不给精华顶包。

### 4.3 `Album list shape` / `Album media shape` 两行日志

`get_group_album_media_list` 的返回结构在 NapCat 里**没有定型**（schema 是 `Any`），
所以 `plugins/group_bot/album.py` 的解析是防御式的：按优先级递归找字段，再兜底扫描图片 URL。
每轮拉取会把首个响应的**字段名**（不含值）记进日志：

```
Album list shape for group <群号>: album_list,attach_info,has_more
Album media shape for group <群号>: attach_info,media_list
```

**用途**：想知道真实返回长什么样、或相册照片抽不到时，抓这两行就能把防御式解析收窄成确定实现。
拿到新字段名后同步更新 `album.py` 的键名列表。

### 4.4 图片下载失败

下载失败的对象会被记进「坏图集合」，之后不再重试（避免每次抽到都重新下载一遍）：

- 精华：`state._essence_broken`（按 `message_id`）
- 相册：`state._album_broken`（按 `album:<media_id>` 这套键，与「今天已捞过」同一键空间）

集合在内存里，**重启即清空**——重启后会给坏图再一次机会。若某张图长期坏且一直占据抽取位置，
重启无效时可清理 `data\media_cache` 下对应的散列文件后重试。

### 4.5 机器人静默、没有任何回复

按顺序排查：

1. `logs\supervisor.log` 是否有 `BOT DOWN` / `ONEBOT LOST` —— 进程或连接问题；
2. 8080 上是否有已建立的连接：`Get-NetTCPConnection -LocalPort 8080 -State Established`；
3. NapCat 那侧 QQ 是否掉线（NapCat Desktop 面板）；
4. 是否撞上冷却：撤回记录是**全群共用 120 秒**（主人豁免），漂流瓶是每人每群 60 秒。
   冷却期内的重复尝试按设计**静默**，第一次被挡才会提示「还要等 N 秒」；
5. 是否是**未识别命令**：未知指令按设计完全静默（例如已删除的旧名 `精华`、`精华榜`）。

### 4.6 日志里的 `UnicodeEncodeError`

群消息含 emoji，若 stdout 重定向后回落到 ANSI 代码页会触发。`scripts/lifecycle/start.ps1` 已经设置
`PYTHONIOENCODING=utf-8`；手工前台运行时也要设这个环境变量。

---

## 5. 备份建议

| 对象 | 建议 |
|---|---|
| `data\messages.sqlite3` | **至少每天一次**。用 SQLite 在线备份（`VACUUM INTO` 或 `.backup`），或短暂停机后复制 `messages.sqlite3` + `-wal` + `-shm` 三个文件 |
| `data\avatars\`、`data\media_cache\`、`data\face_cache\` | 可单独压缩归档；属于可重建缓存，优先级低于数据库 |
| `.env` | 单独离线保管（含令牌），**不要**进任何代码仓库或聊天记录 |
| `data\card_images\` | 不需要备份（发送后即删） |

恢复顺序：停机器人（`stop.ps1`）→ 覆盖数据库与缓存 → 启动 → 检查每群召回游标、
冷却与开关状态（`bot_settings`）、以及最近消息时间是否正常。

备份产物不要只放在同一块盘：本机数据库与日志同盘，盘故障会一起丢。
