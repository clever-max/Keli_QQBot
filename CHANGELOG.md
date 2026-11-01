# 变更记录（CHANGELOG）

本文件登记**每一个可运行的迭代**：只要项目达到「机器人能起来、插件能被 NoneBot 加载」的状态，
就在 `main` 上提交，并把这次迭代追加到这里。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，日期为本地日期（Asia/Shanghai），
提交哈希对应本仓库 `main` 上的真实提交。

## 登记约定

1. **每个迭代只登记一个「迭代提交」哈希**，也就是承载这次迭代代码/文档的那一条提交。
   为了让 CHANGELOG 能写下这个哈希，记录本身作为紧随其后的 `docs(changelog): ...` 提交存在；
   这条记录提交不再单独登记，否则会无限套娃。
2. **「可运行验证」只写真正跑过的检查**，不写推断。当前项目的检查就两项：
   - `python -m compileall plugins`——语法能过；
   - `nonebot.init()` + `nonebot.load_plugins("plugins")`——插件真的被 NoneBot 加载。
   它证明的是「能起来」，**不代替**真机联调（扫码登录、在群里发命令）。
3. **一条命令完成 验证 → 提交 → 登记**：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\commit-iteration.ps1 -Message "feat: ..."
   ```

   本机只装了 Windows PowerShell 5.1（命令是 `powershell`），**没有 `pwsh`**；装了 PowerShell 7
   的话把 `powershell` 换成 `pwsh` 即可。本机默认禁止运行脚本，所以要么加 `-ExecutionPolicy Bypass`，
   要么先 `Set-ExecutionPolicy -Scope Process Bypass`。详细用法见脚本头部注释。
   **多行提交信息请用 `-MessageFile <文件>`**（把信息写成 UTF-8 文件，例如放在已被忽略的 `run\` 下）：
   `powershell -File` 调用时参数要重新拼成一条命令行，直接内联带引号的多行字符串会被拆坏。
4. **迭代 1–8 是 2026-10-14 回填的**：内容取自 `git log`，并且每一条都在当天用上面那两项检查
   重新跑过一遍，全部通过。逐条输出见文末「回填复验记录」。
5. 迭代号只是本文件内的顺序号，与 `git` 提交顺序一致；**不要重排历史**。

---

<!-- ITERATION-INSERT-POINT 新迭代追加在这行下面，最新的在最上面 -->


## [迭代 35] - 2026-11-01 — `d4dce28`

可运行验证：`python -m compileall plugins` 通过；24/24 行为套件通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- 只开漂流瓶的群只保留漂流瓶系列命令（`BOTTLE_ONLY_GROUP_IDS`）。
- 主人私聊 `撤回记录 历史`：跨群返回所有已撤回消息（含已看过的），不标记未读。
- 字体回退补泰文/拉丁语音扩展，归档 3,732 个字符覆盖率 100%。

### 优化

- 成功抽取不再调用 `pool_sizes`，只使用已拉取池子的 `pool_total`。
- 连接后后台预热精华/相册缓存，重连时替换旧 Bot 的预热任务。
- 图片归档查询按消息批量；相册照片缓存复用。
- 归档 sha256 移出事件循环、临时文件原子写、共享 HTTP client。
- 新增 `messages(archived_at)` 部分索引，补扫查询 17 ms → 0.007 ms。
- 慢抽取记录 choose/select/render/send 分段日志。

### 文档

- 合并 8 份旧规划/评估文档，并标记本轮完成状态。
- 同步 README、docs 索引、架构、命令、配置、开发、运维中的链接与过期数据。

本迭代由 4 个解耦提交组成：`d4dce28`（功能）、`2efcc23`（性能）、`7d92b74`（字体）、随后一条 `docs` 提交。

## [迭代 34] - 2026-10-24 — `373717d`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 文件类精华/撤回原样返回并修复私聊撤回入口
- 全量 23/23 通过；recall_private_delivery 已置 1（运行时数据，不在提交内）

## [迭代 33] - 2026-10-23 — `b629c4a`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat: 撤回记录返回原视频
- 从永久媒体归档发送撤回视频，并在视频发送成功后标记召回

## [迭代 32] - 2026-10-23 — `f2a424a`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat: 完善撤回与漂流瓶消息展示
- 增加回复与合并转发预览、媒体摘要和分页幂等标记

## [迭代 31] - 2026-10-21 — `4603399`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(face): 卡片正文按 QQ 原样渲染内置表情
- 覆盖归档中 82 个表情 id 的 81 个（464「马上到」无图，退回文字）；全量行为套件 21/21 通过；卡片是静态 PNG，动图取第一帧

## [迭代 30] - 2026-10-21 — `05d2d39`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(summary): 增量总结（上次至今 / 回退 4 小时）+ NapCat 本地 OCR 读图

## [迭代 29] - 2026-10-20 — `cbbf141`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(essence): 支持文件类精华与多图精华，并附全库扫描结果

## [迭代 28] - 2026-10-18 — `ca151fa`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(water): 加「喝水提醒 测试」，立刻发一条示例提醒验证通道

## [迭代 27] - 2026-10-18 — `f45b03a`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(water): 喝水提醒（私聊 + 群，19 个时间点，主人私聊可开关）

## [迭代 26] - 2026-10-18 — `8209cd6`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 修复

- fix(gate): 手打「@可莉 命令」也认，修复"漂流瓶没反应"

## [迭代 25] - 2026-10-18 — `17225c2`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 测试

- test: 行为套件收进仓库 tests/ 并接进提交流程

## [迭代 24] - 2026-10-17 — `894b12f`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 重构

- refactor: 清废第一批（移除废弃协议端、死代码、退役配置，加日志轮转与依赖锁）

## [迭代 23] - 2026-10-17 — `320311f`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 被抽到榜改为显示漂流瓶里的实际内容

## [迭代 22] - 2026-10-17 — `ac16655`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 新增「被抽到榜」命令，TOP10 以卡片图片返回

## [迭代 21] - 2026-10-17 — `00fdf64`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 近期防重复改为随机化「最久未抽优先」，退役内存 deque

## [迭代 20] - 2026-10-17 — `4c5f955`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 显示内容被捞到的次数，并预留「被抽到榜」接口

## [迭代 19] - 2026-10-15 — `d3cf5a1`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 新增

- feat(bottle): 每人每天的漂流瓶次数由 5 提到 10

## [迭代 18] - 2026-10-15 — `ee6559b`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 修复

- fix(card): 补上缺字回退链，昵称与正文里的 emoji 不再显示成「口口」

## [迭代 17] - 2026-10-15 — `06f6fe0`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 重构

- refactor(scripts): 脚本按功能分为四类，根目录推断改为与深度无关

## [迭代 16] - 2026-10-15 — `36d589f`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 文档

- docs: 建立文档系统，根 README 瘦身为入口

## [迭代 15] - 2026-10-15 — `c142722`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 修复

- fix(media): 保留原图画质、禁止拉伸，视频改为直接转发

## [迭代 14] - 2026-10-14 — `aafcbc4`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 修复

- fix(changelog): 修正用法示例并用 -MessageFile 解决多行提交信息被拆坏的问题

## [迭代 13] - 2026-10-14 — `41ea7fd`

可运行验证：`python -m compileall plugins` 通过；`nonebot.init()` + `load_plugins("plugins")` 成功加载 ['group_bot']。

### 工具链

- chore(git): 加入 CHANGELOG 迭代登记约定与 commit-iteration.ps1
- CHANGELOG.md 与脚本均为 UTF-8 编写；只有 .ps1 需要 BOM（Markdown 不要 BOM）
- 本机默认禁止运行脚本：需 powershell -ExecutionPolicy Bypass -File .\scripts\commit-iteration.ps1

## [迭代 12] - 2026-10-14 — `15dc236`

可运行验证：只改文档；`compileall plugins` + `load_plugins("plugins")` 通过。

### 文档

- README 重写「使用」一节：漂流瓶与漂流瓶榜、`撤回记录` 的时间窗与条数上限、群内 `!recalled`、
  全群 120 秒冷却、私聊投递开关、`status` 报告、回复文本不出现群号的约定，
  以及精华取图链路（`*.myqcloud.com` 只在这一条链路上放行）等边界说明。
- `.env.example` 补齐 `RECALL_WINDOW_MINUTES`、`RECALL_MAX_COUNT`、`ESSENCE_COOLDOWN_SECONDS`、
  `ESSENCE_DAILY_LIMIT`、`ESSENCE_CACHE_SECONDS`、`ESSENCE_RECENT_MEMORY`、`BOTTLE_ALBUM_*`、
  `STATUS_USERS`。

## [迭代 11] - 2026-10-14 — `362edb9`

可运行验证：`compileall plugins` 通过；`load_plugins("plugins")` 成功加载 `group_bot`。

### 重构

- `plugins/group_bot.py`（1533 行）按职责拆成 `plugins/group_bot/` 包：`config` / `state` /
  `store` / `settings` / `cooldown` / `ingest` / `media` / `render` / `textimg` / `card` /
  `summary` / `llm` / `recall` / `status` / `essence` / `album` / `handlers`。
  `__init__.py` 固定导入顺序，保证建表钩子与各 matcher 只注册一次。

### 新增

- **漂流瓶**：群精华 + 群相册混合池（`BOTTLE_ALBUM_SHARE`，默认各半），取不回的坏图整条跳过并记住，
  当天不重复（`essence_draws`），`丢漂流瓶榜` 分「被丢进瓶子里最多的人」与「最爱丢漂流瓶的人」两榜。
- **`status` 本机体检报告**：只发主人私聊、报告内不出现任何群号、全部本地统计不调用模型。
- **私聊投递开关**（`bot_settings.recall_private_delivery`，默认开）：主人可开可关，群管理员只能关。
- **全群 120 秒冷却**（`group_cooldowns`）：超级管理员与私聊不受限，被权限拒绝的尝试不占用冷却。

### 变更

- `撤回记录` 默认只看最近 `RECALL_WINDOW_MINUTES`（30）分钟、上限 `RECALL_MAX_COUNT`（100）条；
  群内新增 `!recalled` 别名；「是否已召回」改为按单条事件记录（`recall_events.summoned_at`）。
- 回复文本统一改用 `<群号>` 占位符，不再输出真实群号。

### 修复

- `summary.py` 补回 `REPEAT_MIN_COUNT` 导入。拆包时漏掉这个常量，导致 2026-10-12 该群
  「昨日消息总结」无任何回复、且当天配额被静默消耗；经过见
  内部故障记录。

## [迭代 10] - 2026-10-14 — `2fc5177`

可运行验证：只加脚本，未改运行时代码；`compileall plugins` + `load_plugins("plugins")` 通过。

### 新增

- `scripts/bot-supervisor.ps1` 看门狗：进程退出后自动重启，并记录退出码与 stderr 末尾，
  便于区分 OOM 被杀与代码崩溃；定时采样网关 / DNS / OneBot WebSocket 健康度并记录状态变化。
  只托管本项目的 Python 进程，不碰 NapCat 与 QQ。
- 背景：2026-10-11 约 01:54 机器人挂掉、停了 2 分 41 秒，没有留下任何 stderr，也没有东西把它拉起来。

## [迭代 9] - 2026-10-14 — `e0a170e`

可运行验证：只改文档；`compileall plugins` + `load_plugins("plugins")` 通过。

### 文档

- 新增 `docs/功能规划-消息统计与群精华.md`：本地归档实测数据（3041 条 / 27 人 / 全表扫描 56 ms）、
  NapCat 群精华与群相册接口实测结果，以及统计功能的两阶段实现规划。
- 新增 `docs/故障记录-20261012-昨日总结无响应.md`：复盘与证据（日志、`summary_runs` 配额痕迹）。
- 更新 `docs/开发路线与需求清单.md` 的需求状态。

## [迭代 8] - 2026-10-10 — `4abda22`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 变更

- 停止跟踪本地数据备份 `qqbot-data.zip`（3.2 MB），`.gitignore` 增加 `*.zip`。

## [迭代 7] - 2026-10-10 — `4a5d66a`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 修复

- 修正 loguru 占位符写法，日志行不再丢掉参数。

## [迭代 6] - 2026-10-10 — `4fb444b`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 新增

- 昨日总结改版：DeepSeek 叙述、话题归纳、复读检测、每群每天一次的配额（`summary_runs`）。

### 文档

- `.env.example`、README、开发路线清单同步更新。

## [迭代 5] - 2026-10-10 — `122d55a`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 变更

- 重建 `.venv` 基线；`start-all.ps1` 大幅扩充：自动把 OneBot 反向 WebSocket 配置写进 NapCat、
  VC++ 运行库修复、日志与数据库路径整理。

### 修复

- `scripts/verify-vps.ps1` 相应调整。

### 备注

- 本次提交误把本地数据备份 `qqbot-data.zip` 一并提交，已在迭代 8 撤销跟踪。

## [迭代 4] - 2026-10-02 — `6a1baff`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 修复

- 召回处理遇到数据库 / 缓存 / 图片渲染错误时回落到失败提示，不再静默无响应。

## [迭代 3] - 2026-10-02 — `8db2de6`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 修复

- 修正 `scripts/verify-vps.ps1` 的检查逻辑。

## [迭代 2] - 2026-10-02 — `1ed980d`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 新增

- `scripts/verify-vps.ps1`：VPS 部署完成后的整套自检脚本，README 补充用法。

## [迭代 1] - 2026-10-02 — `61d1658`

可运行验证：`compileall plugins` + `load_plugins("plugins")` 通过。

### 新增

- 初始提交：`bot.py`（NoneBot + OneBot v11 入口）与 `plugins/group_bot.py`，
  已含多群白名单、授权用户 @ 触发、SQLite 消息归档、撤回事件标记与撤回记录图片卡片、
  昨日总结雏形、成员头像缓存，以及可选的 OpenAI 兼容模型回复。
- PowerShell 启停脚本（`start-all.ps1` / `start.ps1` / `stop.ps1`）、NapCat 安装辅助脚本、
  `Dockerfile` / `compose.yaml`、需求清单文档。
- 两个与主链路隔离的实验目录：`icqq-native/` 与 `oicq-native/`
  （OICQ 原生合并转发实验，vendor 源码固定到 `dmlgzs/oicq@726958d`）。

---

## 回填复验记录

2026-10-14 回填迭代 1–8 时，对**每一个**提交都用 `git archive` 导出到临时目录、
复制 `.env`、再用本机 `.venv\Scripts\python.exe` 跑了一遍加载检查，结果如下：

```
PASS  61d1658  loaded=[group_bot]  Initial QQ bot project
PASS  1ed980d  loaded=[group_bot]  Add VPS verification script
PASS  8db2de6  loaded=[group_bot]  Fix VPS verification script
PASS  6a1baff  loaded=[group_bot]  Add recall failure fallback
PASS  122d55a  loaded=[group_bot]  baseline: rebuild venv, auto OneBot reverse-WS config, VC++ runtime fix
PASS  4fb444b  loaded=[group_bot]  Daily summary: DeepSeek narrative, topics, repeat detection, per-day quota
PASS  4a5d66a  loaded=[group_bot]  Fix loguru placeholders so log lines keep their arguments
PASS  4abda22  loaded=[group_bot]  Stop tracking the local data backup archive
```

迭代 9–12 是同一天新提交的，验证在提交前于工作区直接完成，同样全部通过。

