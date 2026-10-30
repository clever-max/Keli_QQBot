# 开发规范

本文是 `<repo>` 仓库的开发规范。**这里的规则是硬性的**：改动代码前先读一遍，
提交前对照「提交前检查清单」逐条确认。

适用范围：本仓库所有代码、文档、脚本改动。运行与部署见 [operations.md](operations.md)，
文档索引与文档约定见 [README.md](README.md)。

---

## 1. 每次改动都要提交并推送（硬性规则）

**规则：每当做出改动并通过验证后，必须按 conventional-commit 前缀规范提交，
并 push 到远端 `origin/main`。**

不允许「改完先放着」「只在本地留着」。可运行的迭代必须在 `main` 上有对应提交。

### 1.1 提交信息前缀

| 前缀 | 用于 | 例 |
|---|---|---|
| `feat` | 新增功能、新命令、新行为 | `feat(bottle): 新增「漂流瓶 相册」只抽相册` |
| `fix` | 修正缺陷 | `fix(album): 统一坏图键空间，坏图不再被反复重试` |
| `refactor` | 不改变外部行为的重构 | `refactor(recall): 抽出 RecallRequest 投递目标` |
| `docs` | 只改文档 | `docs: 建立规范与运维文档` |
| `chore` | 工具链、脚本、依赖、配置 | `chore(git): 持久化 HTTP/1.1 与大 postBuffer` |
| `test` | 只改测试 | `test: 把 recall_verify 套件收进 tests/` |
| `perf` | 性能 | `perf(essence): 相册列表并发拉取` |
| `style` | 格式，不改语义 | `style: 统一行宽` |
| `build` / `ci` | 构建、流水线 | `ci: 增加验证步骤` |
| `revert` | 回滚 | `revert: 撤回 xxx` |

可带作用域：`fix(album): ...`。`scripts/dev/commit-iteration.ps1` 会按前缀推断 CHANGELOG 里的小节标题
（`feat`→新增、`fix`→修复、`docs`→文档、`chore`/`build`/`ci`→工具链，其余见脚本 `Get-SectionTitle`），
认不出来就归到「变更」。

### 1.2 推送目标与已知网络问题

远端固定为 **HTTPS 到 GitHub**：

```
origin  https://github.com/clever-max/qqbot.git   （分支 main）
```

#### 真实根因：本机把 GitHub 域名劫持到了 Steam++ 加速器

2026-10-15 实测定位（不是"校园网 HTTP/2 有问题"这种猜测）：

1. `C:\Windows\System32\drivers\etc\hosts` 第 39–68 行把 `github.com`、`api.github.com`、
   `raw.githubusercontent.com` 等约 30 个 GitHub 域名全部指向 `127.0.0.1`。
2. **`Steam++.Accelerator`（Watt Toolkit / Steam++）** 正在 `127.0.0.1` 监听 **80 与 443**，
   作为 GitHub 的本地反向代理。
3. 它转发普通 HTTPS GET 正常（`https://github.com` → 200），但
   **`git-receive-pack` 端点返回 401**，于是 `git push` 间歇性失败：

   ```
   error: RPC failed; HTTP 408 curl 22 The requested URL returned error: 408
   fatal: unable to access '...': The requested URL returned error: 502
   send-pack: unexpected disconnect while reading sideband packet
   ```

**关键判断：`git fetch` / `git ls-remote` 能通而 `git push` 不能，几乎一定是这个加速器造成的**，
与 `http.version`、`postBuffer` 之类参数无关。本仓库下列配置**已确认不足以解决**，保留它们只是无害：

| 配置项 | 值 |
|---|---|
| `http.version` | `HTTP/1.1` |
| `http.postBuffer` | `524288000` |
| `http.lowSpeedLimit` / `http.lowSpeedTime` | `1000` / `60` |
| `http.sslBackend` | `schannel` |

#### 处置顺序

1. **首选：在 Steam++ 里关掉 GitHub 加速**（或退出加速），再推送。必要时把 hosts 里那批
   `127.0.0.1 github.com` 条目注释掉——但注意**校园网可能直连不了 GitHub，这才是当初装加速器的原因**，
   所以关掉之后要确认 `git ls-remote origin` 仍然可用。
2. **次选：多重试几次。** 该链路是间歇性的，实测有一次 `72be0aa..0eb8426` 是重试后成功的。
3. 仍不行就换网络（手机热点）后重试，或改用 SSH 远端（需要另配密钥；注意 hosts 同样会把
   `github.com` 解析到 127.0.0.1，SSH 也会被带偏）。

**推送没成功时不要说「已完成」**：要明确写成「已本地提交（含哈希），推送因网络失败，待重试」。

---

## 2. 「验证 → 提交 → 登记 CHANGELOG」流程

仓库既有约定：**每个可运行的迭代在 `main` 上提交一次，并登记到根目录 `CHANGELOG.md`**。
这三步由 `scripts/dev/commit-iteration.ps1` 一条命令串起来。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 -Message "feat: 增加 xxx"
```

本机同时装有 Windows PowerShell 5.1（命令是 `powershell`）和 PowerShell 7（命令是 `pwsh`，
当前 7.6.8）；两者都能跑这些脚本，示例默认写 `powershell` 以兼容没装 7 的机器，
把它换成 `pwsh` 即可。本机默认禁止运行脚本，所以要 `-ExecutionPolicy Bypass`
（或先 `Set-ExecutionPolicy -Scope Process Bypass`）。

### 2.1 参数

| 参数 | 用途 |
|---|---|
| `-Message <字符串>` | 提交信息（单行、或不含引号的多行） |
| `-MessageFile <文件>` | 从 UTF-8 文件读提交信息；**多行或含引号时用这个** |
| `-Note <字符串>` | 追加到 CHANGELOG 记录里的补充要点，一行一条 |
| `-Push` | 登记提交完成后，推送当前分支到 `origin` |
| `-Record <提交>` | **只补登记**一个已存在的提交：不验证、不产生代码提交 |
| `-SkipVerify` | 跳过两项检查（记录里会写明「已跳过」） |
| `-Section <标题>` | 覆盖记录里的 `### 小节` 标题（如 新增/修复/文档/重构/工具链） |
| `-VerifySummary <字符串>` | 覆盖记录里「可运行验证」那句话 |

`-Message`、`-MessageFile`、`-Record` 至少要给一个，否则脚本直接报错退出。

### 2.2 为什么多行提交信息必须用 `-MessageFile`

用 `powershell -File`（或 `pwsh -File`）调用脚本时，参数会被重新拼成一条命令行：直接内联带引号的多行字符串
会被拆坏——实测会把消息后半段当成位置参数，报出「补登记已有提交 pwsh」这类莫名其妙的错。
把信息写进 UTF-8 文件再交给 `-MessageFile` 最稳，文件本身不受 git 跟踪也不影响提交内容：

```powershell
Set-Content -Path run\iteration-message.txt -Value "fix: 修正 xxx`n`n细节说明" -Encoding UTF8
powershell -ExecutionPolicy Bypass -File .\scripts\dev\commit-iteration.ps1 `
  -MessageFile run\iteration-message.txt -Note "影响范围：撤回记录" -Push
```

`run\` 已在 `.gitignore` 里，不会混进提交。

### 2.3 为什么登记会成为紧随其后的 `docs(changelog)` 提交

CHANGELOG 记录里要写下**这次迭代的提交哈希**，而哈希只有提交之后才知道。所以流程是：

1. 验证；
2. 提交本次迭代（得到哈希）；
3. 用这个哈希写一条记录，**作为紧随其后的 `docs(changelog): 登记迭代 N（hash）` 提交**存在。

这条记录提交**不再单独登记**，否则会无限套娃。因此一次迭代在 `git log` 里是相邻两条提交。

记录插入位置是 `CHANGELOG.md` 里的 `<!-- ITERATION-INSERT-POINT ... -->` 标记下方
（最新的在最上面），迭代号是文件内的顺序号。相关约定见 [CHANGELOG.md](../CHANGELOG.md) 的「登记约定」。

退出码：`0` 成功；`1` 验证失败 / 参数不对 / git 报错——**此时不会产生任何提交或登记**。

### 2.4 编码纪律

- `scripts/*.ps1` **必须保持 UTF-8 带 BOM**：仓库仍要在 Windows PowerShell 5.1（ANSI 代码页 936）
  下可运行，没有 BOM 的 UTF-8 脚本会被按 GBK 解读，中文全变乱码、个别字节还会直接造成语法错误。
  PowerShell 7 能读无 BOM 的脚本，但为了 5.1 兼容仍要求带 BOM。
- `CHANGELOG.md` 等 Markdown **不要 BOM**。
- 用 PowerShell 改写含中文的 UTF-8 文件时，用 `[System.IO.File]::ReadAllText/WriteAllText`
  配 `UTF8Encoding($false)`；**不要**用 `Get-Content | Set-Content` 往返（会按 ANSI 读写而损坏中文）。

---

## 3. 验证方式

### 3.1 「可运行」的两项判据（脚本自动执行）

| 检查 | 命令 | 证明什么 |
|---|---|---|
| 语法 | `python -m compileall -q plugins` | 语法能过 |
| 加载 | `nonebot.init()` + `load_plugins("plugins")` | 插件真的被 NoneBot 加载 |

第 2 项由脚本生成一个临时冒烟脚本执行。注意 `load_plugins()` **会吞掉导入异常**、
只返回成功加载的那部分（导入失败时安静地返回空集合），所以冒烟脚本会**自己断言
确实加载到了 `group_bot`**（打印 `PLUGINS_LOADED: [...]`、断言失败则退出码 2），
否则这个检查会变成假绿灯。

它证明的是「能起来」，**不代替真机联调**（扫码登录、在群里发命令）。

### 3.2 更强的回归：17 个套件（**已在仓库 `tests/`**）

`scripts/dev/commit-iteration.ps1` 只证明能起来。行为改动另有一套逐项断言，位于
**`%TEMP%\recall_verify`**（即 `%TEMP%\recall_verify`），
每个套件用独立临时 SQLite 库、以真实数据库 + 真实处理器的方式跑，全绿才算通过：

| 套件 | 覆盖范围 |
|---|---|
| `verify_recall.py` | 撤回记录命令解析、权限助手、私聊目标群推导、`RecallRequest` 投递方向 |
| `verify_delivery.py` | 撤回卡片投递路由（进群 / 走私聊 / 私聊发起的命令）、空队列提示、条数上限只标记真正画出的记录 |
| `verify_switch.py` | 私聊投递开关的解析、SQLite 持久化与跨连接读取、权限模型、文案 |
| `verify_gate.py` | 处理器级的开关拦截：群里只能关、非主人不能开、关闭时拒绝且**不回落进群**、私聊静默与放行 |
| `verify_permissions.py` | 撤回记录开放给全群、全群 120 秒冷却与主人豁免、`clear` 仅主人、help 不再有超管段、跨群点名需被点名 |
| `verify_nogroupid.py` | 回复文本不含任何群号、`status` 报告改汇总且只发私聊 |
| `verify_bottle.py` | 相册返回结构的容错解析、双池抽取与降级回退、丢漂流瓶榜合并计数、连续同类补偿、`漂流瓶 相册` 的四种失败与退次数 |
| `verify_media.py` | 图片不放大且保留原图画质、卡片图框宽度、相册优先取原图键、视频识别与直接转发、视频转发失败退次数、文件类精华（含 `.mp4`）走原文件 + 归档副本/URL 下载、上传失败退次数 |
| `verify_font.py` | 缺字回退链的构成、变体选择符（`U+FE0F`）必须被跳过（`❤️` 曾变成两个空框）、空格保留宽度且不误报、典型缺字字符各由谁承担、**归档全部 2593 个字符无法绘制数为 0**、多段文本的锚点对齐 |
| `verify_face.py` | QQ 内置表情：`face.id/faceText` → `[表情:id:名字]` 标记、卡片正文「文字 + 表情图」混排且折行不拆标记、本机 `sysface_res` 图源与 64px 缓存、404 只探测一次、缺图退回 `[名字]` 文字 |
| `verify_group_scope.py` | 分群隔离：新增群的授权只按自己的名单判定（开放群的 `*` 不串群）、陌生人在新群仍被拒、`WATER_EXCLUDED_GROUP_IDS` 只排除指定群、`STATUS_EXCLUDED_GROUP_IDS` 从所有聚合查询里剔除该群且命令静默 |
| `verify_drawcount.py` | 被抽到次数的语义（跨天累加、同一天不重复累加、超保留期的行被清掉）、`label` 落库与 upsert（改 label 但不动 `drawn_at`）、榜单按次数倒序与 `album:` 前缀分类、卡片副标题与提示文案都带次数、**预留的被抽到榜没有挂成命令** |
| `verify_repeat_sampling.py` | 近期防重复：`preferred_order` 只是**排序**不是禁抽（结果集合不变）、严格模式下按"越旧越前"、K=5 时队首大概率落在最旧一侧但仍有随机性、`mode=off`/`K=1` 回到随机、空池与 K>池子不报错、近 20 次内零重复且明显优于纯随机、`draw_history` 按逻辑日期而非落库时刻、两个池子的选取真的接上了它、**当天不重复仍优先于它** |
| `verify_voice.py` | 语音点歌：曲库只认音频后缀、缺省优先默认那首、精确匹配优先于模糊匹配、点不到的歌给提示、**发出的是 `record` 段（不是图片/文字）**、发送失败补说明并提到 60 秒限制、空曲库提示投放路径、命令解析与 help 可见 |
| `verify_voice_audio.py` | 语音截断：MP3 帧解析与时长、超限才截、截到正好 ≤ 上限（误差 <1 帧）、ID3 头保留、缓存命中与源文件变更后失效、上限可配、`.silk`/坏文件原样返回不抛异常 |
| `verify_archive.py` | 多媒体永久归档：内容寻址与去重（同图转发两次只占一份）、四种媒体类型、超限与不可达**必须留痕**（不能当"没有多媒体"）、无多媒体消息也标记进度、补归档扫描幂等 |
| `verify_config_docs.py` | **配置三处一致性**：`config.py` 读取的每个键都必须出现在 `.env.example` 与 `docs/configuration.md`；自带"检测器自检"（故意缺键必须被报出来，防止假绿）。加新配置项时它会挡住漏更新文档 |

运行方式（每个套件独立，输出以「全部通过」结尾、失败则退出码 1）：

```powershell
& <repo>\.venv\Scripts\python.exe "$env:TEMP\recall_verify\verify_bottle.py"
```

**欠账（需要有人收尾）**：这 15 个套件目前只存在于 `%TEMP%`，**不在仓库里**——
全部套件已收进仓库：`tests/`（`tests/README.md` 有清单）。`tests/run_all.py` 一条命令跑完，\n`commit-iteration.ps1` 在提交前自动执行它（失败即中止提交）。

（历史上它们在 `%TEMP%`，机器一换就丢；2026-10-18 已全部收进仓库。）应当把它们收进仓库的 `tests/`
（并让 `scripts/dev/commit-iteration.ps1` 或 CI 一并运行），这一步尚未完成。在收进去之前，
**行为改动的验证结果只存在于当次会话**，请务必在提交信息与 CHANGELOG 的 `-Note` 里写清跑了哪些套件。

### 3.3 给套件打桩时，要打在"库里实际查找该名字的模块"上

2026-10-17 把 `essence.py`（1,514 行）拆成 `bottle_pool` / `bottle_stats` / `bottle_render` /
`bottle_flow` 四个模块后，`essence.py` 只剩再导出。这时套件里原本的写法会**静默失效**：

```python
gb_essence.choose_bottle = fake      # ← 只改了再导出层的属性
# bottle_flow.send_bottle 调用的是它自己命名空间里的 choose_bottle，仍是真函数
```

规则：**打桩要打在"实现该调用的模块"里**。本项目的对应关系：

| 你想替换的 | 打成 |
|---|---|
| `choose_bottle` / `choose_essence` / `choose_album_photo` / `fetch_*` / `materialize_*` / `random` / `BOTTLE_*` 常量 | `bottle_pool.*` |
| `claim_essence_draw` / `record_draw`* / `render_bottle_card` / `cache_avatar` | `bottle_flow.*` |
| `render_card_page` / `resolve_drawn_content` 用到的 `fetch_*`、`materialize_*` | `bottle_render.*` |

\* `record_draw` 定义在 `bottle_pool`，但被 `bottle_flow.send_bottle` 调用，所以按"调用方"打。

判断方法一句话：**看哪个函数会调用它，就打那个函数所在的模块。**

### 3.3b 给 NapCat 传本地文件：必须绝对路径或 `file://` URI

2026-10-17 踩到：语音发送一直失败，NapCat 返回

```
ActionFailed(retcode=1200, message='文件处理失败: 识别URL失败, uri= data\voice\.trimmed\让风告诉你-...-60s.mp3')
```

根因是 **路径是相对的**——`config.DB_PATH` 来自 `.env`（`data/messages.sqlite3`），
一路 `DB_PATH.parent / "voice"` 拼下来全是相对路径。修法两条一起上：

1. 目录常量在模块导入时就 `resolve()`（`VOICE_DIR = (DB_PATH.parent / "voice").resolve()`）；
2. 发送时传 **`file://` URI**（图片一直这么发，是最稳的形式），失败再回退绝对路径。

配套经验：
- **缓存/临时文件名用纯 ASCII**（`voice-<sha1>-<mtime>-<size>-60s.mp3`），别把中文路径
  这个变量引进来；
- 失败时**必须把 NapCat 的原始 `message` 记进日志**——我第一版只抛了句
  `RuntimeError("OneBot failed to send the voice message")`，把真正的错误信息丢掉了，
  白查了一轮。

**结论**：本项目里任何"把本地文件交给 NapCat"的地方（图片、语音、将来的视频/文件），
都用 `Path(...).resolve().as_uri()`。

### 3.4 改代码时别用 PowerShell 处理文件内容（三次事故记录）

在这个仓库里，**用 PowerShell 改文件内容已经连续出过三次事故**，都是它把「文本」当「表达式」处理：

| 事故 | 现象 | 原因 |
|---|---|---|
| BOM 丢失 | 改完 `.ps1` 后 `commit-iteration.ps1` 直接语法报错，日志里中文变成 `宸ュ叿閾?` | 编辑工具写回时丢了 UTF-8 BOM，Windows PowerShell 5.1 于是按 ANSI 代码页读文件 |
| 竖线被抹掉 | 一次性"替换 321 处"，`docs/configuration.md` 所有表格结构损坏 | 命令里含 Markdown 反引号，被 PowerShell 当成转义符 |
| 引号被吃掉 | 写进文件的 `label=""` 变成 `label="`，Python 报 `unterminated string literal` | 双引号在传参/字符串处理中被折叠 |

**纪律**：

1. 改代码与 Markdown **一律用编辑工具**（`edit` / `write`），不要用 `Get-Content` / `Set-Content` / 字符串 `Replace` 拼；
2. 确实需要脚本批量改时：用 **Python** 读写（显式 `encoding='utf-8'`），且**命令里不要出现反引号和双引号**（用 `chr(34)` 构造）；
3. 任何批量替换后**必须核对替换计数**——预期 1 处却报 300 处，就是被解析成单字符了；
4. 改完 `.ps1` 检查 BOM（`EF BB BF`）、改完 Markdown 检查表格竖线数与 `HEAD` 一致。

### 3.5 `store.connect()` 的语义（2026-10-18 起）：成功自动提交

以前 `connect()` 只是"开一条连接，用完关掉"，**不提交**。所以任何忘了 `await db.commit()`
的写入都会被静默丢弃——归档进度标记就这么丢过一次，表现是"补归档扫描反复扫同一条消息"。

现在改成单写者（一条常驻连接 + 一把锁），并且：

- **成功退出 `async with connect()` 就提交**，异常则回滚。写操作不再需要手动 `commit()`
  （写了也无害，只是冗余）。
- **嵌套调用复用外层事务**（同一个任务里再进 `connect()` 不会死锁、也不会另开事务）。
  因此"内层中途失败"不会单独回滚内层——需要局部回滚就自己用 `SAVEPOINT`。
- **aiosqlite 的工作线程原本是非守护线程**：换成常驻连接后，**任何**用到 store 的脚本都会
  在退出时卡在 `threading._shutdown`（当时 15 个套件全挂在退出阶段，而套件本身全绿）。
  现在由 `store._force_daemon_worker_threads()` 换成守护线程，并在 `on_shutdown` 正常关闭。
  写新脚本如果发现"跑完了不退出"，先怀疑这类非守护线程。

---

## 4. 敏感文件纪律

`.gitignore` 已忽略下列路径（**不要**用 `-f` 强行加入）：

| 路径 | 内容 |
|---|---|
| `.env` | 访问令牌、模型 API Key、群号与授权名单 |
| `data/` | SQLite 归档、头像与图片缓存 |
| `logs/` | 运行日志（含群消息正文） |
| `run/` | PID 文件、supervisor 停止标记、临时提交信息文件 |
| `.setup/`、`NapCat/`、`.venv/`、`__pycache__/`、`*.py[cod]` | 安装产物与依赖 |

例外：`oicq-native/vendor/*.zip` 被显式保留跟踪（上游 OICQ 源码存档，用于 diff 修改过的
vendor 树），见 `oicq-native/README.md`。

因为 `scripts/dev/commit-iteration.ps1` 内部用 `git add -A` 暂存**全部**改动，提交前必须自己确认：

```powershell
git status --porcelain
```

确认输出里**没有** `.env`、`data/`、`logs/`、`run/` 下的文件，再提交。日志与数据库里
含群消息原文与群号，一旦推上远端就无法收回。

---

## 5. 文档同步（状态型文档）

本仓库的文档分两类，改动功能时的义务不同，详见 [README.md](README.md) 的「文档约定」：

- **状态型文档**（`architecture` / `commands` / `configuration` / `operations` 等）
  描述**当前实际行为**，改功能就**必须同步更新**对应文档，不能留成过期描述。
- **`CHANGELOG.md` 只追加历史**，不改写既有条目。

---

## 6. 提交前检查清单

1. `git status --porcelain` 里没有 `.env` / `data/` / `logs/` / `run/`。
2. `python -m compileall plugins` 通过，插件能被 `load_plugins` 加载到 `group_bot`
   （直接用 `scripts/dev/commit-iteration.ps1` 即自动执行）。
3. 行为改动跑过对应的行为套件（见 3.2），并在 `-Note` 里写明跑了哪些。
4. 状态型文档已同步更新。
5. 提交信息带正确的 conventional-commit 前缀；多行用 `-MessageFile`。
6. 推送成功（`-Push`），或明确记录推送失败。**推送没成功不算完成。**
