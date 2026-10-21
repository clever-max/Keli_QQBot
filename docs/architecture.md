# 架构与模块职责

> **本文档的用途**：说明 `plugins/group_bot/` 的模块划分、依赖方向、SQLite 表结构、数据流与图片/视频链路，供改动代码前建立全局认识。
> **什么时候需要更新它**：新增或删除模块、改变模块间依赖、增删数据库表或列、改变图片/视频处理链路、改变命令路由顺序时。

## 1. 运行形态

| 组件 | 说明 |
|---|---|
| 登录端 | NapCat Desktop（NTQQ 协议实现），只负责登录与 OneBot v11 接口 |
| 通信 | NapCat 作为**反向 WebSocket 客户端**连到 NoneBot 的 `ws://127.0.0.1:8080/onebot/v11/ws` |
| 机器人 | NoneBot 2 + `nonebot-adapter-onebot` v11；入口 `bot.py`（`nonebot.init()` → 注册 Adapter → `load_plugins("plugins")` → `nonebot.run()`） |
| 存储 | 本地 SQLite；图片缓存与头像在 `data/` 下，全部是本地文件 |
| 守护 | `scripts/lifecycle/bot-supervisor.ps1` 负责自动重启与网络采样；`scripts/lifecycle/start-all.ps1` 负责把 OneBot 配置写进 NapCat |
| 不回补历史 | **不从 QQ 服务器补抓离线期间的消息**：只有插件加载且 OneBot 已连接时收到的消息才会进归档 |

> **协议端的演进**：早期曾试过两套 Node 生态的协议端（`oicq-native` / `icqq-native`），
> 都没走通；2026-10 起统一用 **NapCat**。那两个目录已于 2026-10-17 从工作区移除
> （曾占仓库跟踪字节的 91%），需要考古时见 git 历史。

> **目录结构（2026-10-18 起）**：模块按职责收进 `core/` `common/` `media/` `bottle/` `recall/` `runtime/` 六个子包，顶层只保留 `__init__.py`（包导入顺序）与 `handlers.py`（唯一入口层）。下表的模块名对应的包见各包 `__init__.py` 的说明。

## 2. 模块职责

| 模块 | 职责 | 关键符号 |
|---|---|---|
| `config` | 读取 `.env` 全部静态配置；权限判定函数集合。零内部依赖 | `_setting()`、`ALLOWED_GROUP_IDS`、`GROUP_AUTHORIZED_USERS`、`is_authorized()`、`is_super_admin()`、`is_named_admin()`、`is_named_in_group()`、`group_allows_everyone()`、`is_status_user()` |
| `state` | 共享可变运行时状态（缓存 / 锁 / 任务字典），避免模块间循环导入 | `_essence_cache`、`_essence_locks`、`_essence_broken`、`_album_cache`、`_album_locks`、`_album_broken`、`_recall_locks`、`_avatar_warm_tasks`、`_member_names`、`mark_bot_connected()`。注意**没有**「最近抽过的」容器了：近期防重复改成由数据库派生的无状态排序 |
| `safety` | 处理器兜底：统一"记日志 + 回一句人话"，不让 matcher 炸掉 | `guard()`、`guard_group()` |
| `bot_info` | 读群名（事件 → OneBot → 空串交给调用方兜"本群"） | `group_name_for()` |
| `store` | **单写者** SQLite：一条常驻连接 + 一把锁；建表/迁移；`on_startup` / `on_shutdown` 钩子 | `connect()`（成功自动提交、异常自动回滚、嵌套可重入）、`initialize_database()`、`close_connection()` |
| `settings` | 运行时开关读写（落 `bot_settings` 表），与 `.env` 静态配置分工 | `RECALL_PRIVATE_DELIVERY`、`is_switch_on()`、`set_switch()` |
| `cooldown` | 两套冷却：每群每人 120s（普通 @）与每群 120s（撤回记录） | `COOLDOWN_SECONDS`、`GROUP_RECALL_COOLDOWN_SECONDS`、`take_non_admin_mention_slot()`、`take_group_recall_slot()` |
| `ingest` | 归档群消息、标记撤回；落库后触发多媒体归档 | `message_data()`、`save_message()`、`mark_recalled()` |
| `archive` | **多媒体永久归档**：内容寻址落盘、`message_media` 索引、失败留痕、启动/定时补扫 | `ARCHIVE_ROOT`、`archive_message()`、`archive_pending()`、`schedule_archive()`、`start_archive_sweeper()` |
| `voice` | 语音点歌：`data/voice/` 目录即曲库，按歌名匹配并**以 `record` 段发送**；失败不静默 | `VOICE_DIR`、`list_voices()`、`match_voice()`、`send_voice()`、`safe_send_voice()`、`*_text()` |
| `media` | **所有**图片网络 I/O 与缓存；头像预热与绘制；失败一律不抛异常 | `read_local_image()`、`read_remote_image()`、`image_bytes_from_segment()`、`normalize_cached_image()`、`cache_message_images()`、`cache_avatar()`、`warm_group_avatars()`、`draw_avatar()`、`cached_image_paths_from_json()` |
| `media` | **所有**图片网络 I/O 与缓存；头像预热与绘制；失败一律不抛异常 | `read_local_image()`、`read_remote_image()`、`image_bytes_from_segment()`、`normalize_cached_image()`、`cache_message_images()`、`cache_avatar()`、`warm_group_avatars()`、`draw_avatar()`、`cached_image_paths_from_json()` |
【2026-10-18】media_cache 只是派生缓存（可由 media_archive 重建）
| `album` | 群相册拉取、防御式字段解析、缓存；产出照片或视频 | `AlbumPhoto`（含 `kind`）、`AlbumUnavailable`、`parse_media_item()`、`fetch_album_photos()`、`materialize_photo()`、`mark_broken()` |
| `render` | 纯文本渲染，不碰网络与数据库；表情段写成 `[表情:id:名字]` 标记 | `render_message()`、`clean_model_text()`、`cap_text()` |
| `faces` | QQ 内置表情的图源解析与缓存：本机 QQ 客户端 → 腾讯 CDN → 文字兜底；结果落 `data/face_cache/` | `face_token()`、`face_name()`、`image_path()`、`image()`、`scaled_image()` |
| `textimg` | 字体加载与文本换行 | `font()`、`wrap_text()` |
| `richtext` | 卡片正文混排：文字 + 内联表情图，表情标记作为整体参与折行与测宽 | `Run`、`parse()`、`wrap()`、`measure()`、`draw()` |
| `card` | 卡片几何与绘制；调用方只描述画什么（正文支持内联 QQ 表情图） | `CardEntry`、`render_card_page()`、`paginate()`、`page_size_for()`、`fitted_size()`、`bubble_size()` |
| `essence` | **再导出层**：只把下面四个 `bottle_*` 模块的符号重新导出，保证 `handlers.py` 与测试的导入路径不变 | 见下四行；**不要在测试里对 `essence.X` 打桩**（打不中，见 `development.md`） |
| `bottle_pool` | 漂流瓶的池子与选取：拉取/缓存、键空间、抽样与防重复、内容提取、抽取记录 | `ChosenBottle`、`fetch_essence_list()`、`choose_bottle()`、`choose_essence()`、`choose_album_photo()`、`preferred_order()`、`draw_history()`、`drawn_message_ids()`、`record_draw()`、`essence_text()`、`essence_video_ref()`、`pool_sizes()` |
| `bottle_stats` | 被抽到次数与被抽到榜的数据层（只读 `essence_draws`） | `DrawnContent`、`draw_times()`、`draw_times_after()`、`drawn_content_leaderboard()`、`drawn_content_leaderboard_text()`、`content_label()`、`drawn_rank_label()` |
| `bottle_render` | 卡片渲染与「榜单键 → 现行内容」回查 | `DrawnContentCard`、`render_bottle_card()`、`render_drawn_leaderboard_card()`、`resolve_drawn_content()` |
| `bottle_flow` | 抽取流程编排与用户可见文案（唯一同时依赖 pool/stats/render 的层） | `DrawVerdict`、`claim_essence_draw()`、`send_bottle()`、`send_drawn_content_leaderboard()`、`send_bottle_leaderboard()`、`safe_send_*()`、全部 `*_text()` |
| `summary` | 昨日总结：本地统计 + 可选模型正文 | `day_facts()`、`detect_repeat()`、`summary_digest()`、`render_daily_report()`、`claim_summary_slot()`、`send_yesterday_summary()` |
| `llm` | **唯一**模型调用入口（只有昨日总结会走到） | `chat_completion()`、`report_body()`、`closing_line()` |
| `status` | 本机体检报告；只发私聊、不含群号 | `collect()`、`render_status()`、`send_status()`、`safe_send_status()` |
| `handlers` | 全部 matcher 注册与命令路由；包内唯一的入口层 | `parse_bottle_command()`、`parse_recall_command()`、`parse_toggle_command()`、`is_recognized_command()`、`serve_recall_query()`、`handle_group_message()`、`handle_private_message()` |

## 3. 依赖方向

依赖是**单向**的，逐层向上：

1. 无内部依赖：`config`、`state`、`textimg`
2. 依赖第 1 层：`store`(config)、`llm`(config, render)、`settings`(store)、`cooldown`(store)、`faces`(config)、`render`(faces)
3. 混排与媒体：`richtext`(faces, textimg)、`media`(config, state, textimg)、`album`(config, media, state)
4. 卡片：`card`(config, media, richtext)
5. 业务数据：`ingest`(config, media, render, store)、`summary`(config, llm, render, state, store)
6. 功能模块：`recall`(card, config, media, render, state, store)、`status`(state, config, store)
7. 漂流瓶四层（**严格单向**）：
   `bottle_pool`(album, config, media, render, state, store)
   → `bottle_stats`(bottle_pool)
   → `bottle_render`(bottle_pool, bottle_stats, card, config, media, state)
   → `bottle_flow`(bottle_pool, bottle_stats, bottle_render, config, media, state, store)
   → `essence` 只做再导出，不写逻辑
8. 最上层：`handlers` 依赖以上几乎全部模块

**`__init__.py` 的导入顺序为什么重要**（见其模块 docstring）：

| 顺序 | 原因 |
|---|---|
| `config` / `state` 最先 | 它们在**导入时**就读取设置、创建共享状态，而设置来自 NoneBot 的 config 对象，因此必须在 `nonebot.init()` 之后 |
| `store` 紧随其后 | 它在导入时用 `@driver.on_startup` 注册建表钩子，必须先于任何查询者注册 |
| 功能模块居中 | 它们从 `config` / `state` / `store` 取值，形成上述单向依赖 |
| `handlers` 最后 | 它注册 matcher（`on_message` / `on_notice`）与 `@driver.on_bot_connect` 钩子；放最后才能保证**恰好注册一次** |

## 4. SQLite 表结构

`connect()` 借出**一条常驻连接**（单写者）：`WAL` + `busy_timeout=5000` + `synchronous=NORMAL`
+ `journal_size_limit=64 MiB`，`row_factory=aiosqlite.Row`。**成功自动提交、异常自动回滚**，
同一个任务里嵌套调用会复用外层事务（contextvar 判断）。表全部由 `initialize_database()` 用
`CREATE TABLE IF NOT EXISTS` 建立。

| 表 | 主键 | 用途 |
|---|---|---|
| `messages` | **`(group_id, message_id)`** | 消息归档：发送者、`message_json`（OneBot 段数组）、`plain_text`、`sent_at`、撤回后的 `recalled_at` / `recalled_by`、归档进度 `archived_at`。索引 `idx_messages_group_time(group_id, sent_at DESC)` 与 `idx_messages_message_id(message_id)`（后者必须有：复合主键的索引帮不上"只按 message_id 查"的 JOIN，见 `store.migrate_messages_primary_key` 的注释） |
| `recall_events` | `event_id`（自增），`UNIQUE(group_id, message_id)` | 每条撤回事件一行；`summoned_at` 记录是否已画进卡片。索引 `idx_recall_events_group_event`、`idx_recall_events_pending(group_id, summoned_at, event_id)` |
| `recall_cursor` | `group_id` | 每群的增量游标：`last_event_id`、`last_summoned_at` |
| `mention_cooldowns` | `(group_id, user_id)` | 普通 @ 的每群每人冷却截止时间 |
| `essence_usage` | `(group_id, user_id, day)` | 漂流瓶每人每天额度：`used`、`last_draw_at`、`limit_notified`、`cooldown_notified_at`（通知只发一次） |
| `essence_draws` | `(group_id, day, message_id)` | 当天已捞走的对象；**`message_id` 是键空间而非真实消息号**，见下节。这张表同时是**抽取次数**的来源（`COUNT(*)`），并带索引 `idx_essence_draws_content(group_id, message_id)` 供计数与「被抽到榜」查询。`label` 是内容的人类可读名字——精华列表是实时从 QQ 拉的，原消息可能早就没了，只留键就说不清榜上是什么 |
| `summary_runs` | `(group_id, day)` | 昨日总结的每日配额与结果：`covered_day`、`state`、正文、收尾句 |
| `bot_settings` | `name` | 运行时开关（`value` 存 `"0"`/`"1"`），带 `updated_at` |
| `group_cooldowns` | `group_id` | 撤回记录的全群 120s 窗口：`cooldown_until`、`notified` |

迁移：旧库的 `recall_events` 缺 `summoned_at` 会补列并把游标之前的记录标记为已看过；`essence_usage` 缺 `cooldown_notified_at` 会补列；启动时还会为每个 `ALLOWED_GROUP_IDS` 补一行 `recall_cursor`。

### 键空间约定（重要）

`essence_draws.message_id` 一列同时承载两个池子的键：精华消息原样存它的 `message_id`（`_essence_key()`），群相册存 `album:<media_id>`（`_album_key()`）。`kind_of_drawn_key()` 靠 `album:` 前缀还原类别，`recent_drawn_kinds()` 据此读出「连续同类」记录。

**坏图集合 `_album_broken` 必须用同一套键**（`album.mark_broken()` 接收的就是这个带前缀的键）：两处键空间一旦不一致，坏图就永远筛不掉、每次抽到都会重新下载一遍。

另一处命名沿用旧拼写是**有意**的：模块 `essence.py`、表 `essence_draws` / `essence_usage`、配置键 `ESSENCE_*` 都保留「精华」字样，避免一次没有收益的数据迁移；它们只控制缓存与配额，与用户看到的叫法无关。

## 5. 数据流

**① 群消息 → 归档 / 命令路由**（`handle_group_message`）：白名单群过滤 → `save_message()`（含图片缓存）→ `!recalled` 纯文本快捷路径 → @ 检测（无 @ 则结束）→ `status`（仅 `STATUS_USERS`）→ 漂流瓶（含 `相册` 形式）→ `is_recognized_command()` 过滤未知指令（静默）→ `撤回记录` → 非管理员 120s 冷却闸门 → 昨日总结 / `help` / 丢漂流瓶榜 / 私聊投递开关 / `clear`。

**② 撤回通知 → 标记**（`handle_group_recall` → `mark_recalled()`）：先 `UPDATE messages SET recalled_at, recalled_by WHERE message_id=? AND group_id=?`；只有 `rowcount` 非零（即这条消息确实在归档里）才 `INSERT OR IGNORE INTO recall_events`。这样撤回事件天然与本地归档对齐，不依赖 QQ 侧的撤回后历史查询。

**③ 漂流瓶抽取 → 选池 → 物化 → 卡片或视频转发**（`send_bottle`）：`claim_essence_draw()` 判定冷却/配额 → `choose_bottle()` 决定类别与对象 → 物化 → 投递 → `record_draw()` → `bottle_hint_text()` 补一条文字。

- 选池顺序：`漂流瓶 相册` 显式指定 → 相册被配置关闭则只抽精华 → `preferred_kind_order()` 连续同类补偿 → 按 `BOTTLE_ALBUM_SHARE` 随机；首选那类没内容时回退另一类（显式相册形式不回退）
- 物化：精华走 `materialize_images()`（图片段全部可下载才采用），相册走 `materialize_photo()`；失败的对象记进坏图集合并换下一个候选
- 投递：`ChosenBottle.video_ref` 非空时**直接转发视频**（`MessageSegment("video", ...)`，不下载、不渲染卡片）；否则 `render_bottle_card()` 出图并用 `MessageSegment.image()` 发送，发送后删除临时文件

## 6. 图片与视频链路

| 环节 | 实现 |
|---|---|
| 取字节与放行 | `image_bytes_from_segment()`：`base64://` 直接解码 → OneBot `get_image`（先试 `file`、再试 `url`）→ 段内 `url` 兜底。`read_remote_image()` 只接受 **https**，默认后缀 `.qq.com` / `.qq.com.cn` / `.qpic.cn` / `.qlogo.cn`，`extra_suffixes` 只为单条链路放开：精华 `ESSENCE_IMAGE_SUFFIXES`、相册 `ALBUM_IMAGE_SUFFIXES` |
| 原图保留与落盘 | `normalize_cached_image()`：最长边 ≤ `IMAGE_CACHE_MAX_DIMENSION` 且字节数 ≤ `MAX_CACHED_IMAGE_BYTES` 时**原样落盘**，只有超限才 `thumbnail()` 缩放并按 `IMAGE_CACHE_JPEG_QUALITY` 重编码（RGBA/LA 先合成白底）。路径为 `media_cache/<sha256(group_id:message_id)[:32]>_<index>.jpg`，回写进段的 `data.cached_path`；读取走 `cached_image_paths_from_json()`，带 `relative_to(MEDIA_CACHE_DIR)` 越界防护 |
| 头像 | `cache_avatar()` 取 `q1.qlogo.cn` s=100 后存 128×128 PNG，按 `AVATAR_CACHE_MAX_AGE` 判过期；`warm_group_avatars()` 在连接时用 `get_group_member_list` 预热并填充 `_member_names` |
| 卡片 | 图片框取 `max(64, min(RECALL_THUMB_SIZE, body_width))`；`fitted_size()` 的 `scale = min(max_w/宽, max_h/高, 1.0)`——**绝不放大**，小图按原尺寸显示 |
| 视频 | 不下载、不做卡片。精华用 `essence_video_ref()` 从 `video` 段取可播放引用；相册由 `parse_media_item()` 先判视频再判图片（`kind="video"`），转发失败会退还次数 |

## 7. 读代码时需要留意的既有约束

- `messages.message_id` 是**全局主键**（不是 `(group_id, message_id)`），写入用 `INSERT OR IGNORE`。OneBot 的消息号实际上全局唯一，所以一直没出问题，但跨群撞号会被静默忽略。
- `media.py` 的所有失败路径都返回 `None` 或只记日志，不抛异常——这是网络抖动不会拖垮机器人的原因，改这个模块时请保持。
- 两套冷却时长（`COOLDOWN_SECONDS`、`GROUP_RECALL_COOLDOWN_SECONDS`）是**代码常量**而非 `.env` 项，见 [configuration.md](configuration.md)。
