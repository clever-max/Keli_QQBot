# 配置项说明

> **本文档的用途**：逐个说明 `.env` 里的配置项（含义、默认值、钳制范围）以及存在 SQLite 里的运行时开关，作为改配置时的对照表。
> **什么时候需要更新它**：新增/删除/改名任何 `.env` 键、调整默认值或钳制范围、新增运行时开关时。

## 1. 两类配置的分工

| 类别 | 存放位置 | 读取方式 | 生效时机 | 适用场景 |
|---|---|---|---|---|
| **静态配置** | `.env` | `config.py` 的 `_setting()` / `_int_setting()`，经 NoneBot config 对象读取 | 进程启动时读一次 | 群白名单、授权名单、配额、路径、模型地址 |
| **运行时开关** | SQLite `bot_settings` 表 | `settings.py` 的 `is_switch_on()` / `set_switch()` | 每次用到时读库 | 需要在运行中翻转、且**必须跨重启保留**的开关 |

原则：值来自 `.env` 就放 `config`；需要在运行中改、且不能因为重启而丢，就放 `settings`。当前 `settings` 里只有 `RECALL_PRIVATE_DELIVERY`（撤回记录私聊投递），默认**开**。

## 2. `.env` 配置项

`_int_setting()` 读入的整型值会被 `min`/`max` **钳制**：写超范围的值得不到预期效果，也不会报错。

### 2.1 群与权限

| 键 | 默认值 | 含义与取值范围 |
|---|---|---|
| `ALLOWED_GROUP_IDS` | 空 | 允许归档与响应命令的群号，逗号分隔。留空即"失败关闭"（不响应任何群），`scripts/lifecycle/start.ps1` 会拒绝启动。旧版单数键 `ALLOWED_GROUP_ID` 仍作为回退被读取，但它**未收录进 `.env.example`**，建议只用复数形式 |
| `AUTHORIZED_USERS` | 空 | 全局授权名单，逗号分隔；这里列出的账号同时是**超级管理员（主人）**。可用 `*` 表示允许任何人；群级授权**不会**让人变成主人 |
| `GROUP_AUTHORIZED_USERS` | 空 | 按群的授权名单，格式 `群号:qq1,qq2;群号2:*`。`*` 表示该群全体成员。**在 `ALLOWED_GROUP_IDS` 里但没写在这里的群，回退到全局 `AUTHORIZED_USERS`** |
| `STATUS_USERS` | `100000009` | 允许使用 `status` 体检报告的 QQ 号，逗号分隔，可用 `*`。报告只发私聊 |

### 2.2 归档与撤回记录

| 键 | 默认值 | 含义 | 取值范围 |
|---|---|---|---|
| `RECALL_WINDOW_MINUTES` | `30` | `撤回记录` 默认回看的时间窗（分钟） | 1–1440 |
| `RECALL_MAX_COUNT` | `100` | 单次 `撤回记录` 最多画多少条 | 1–500 |
| `DATABASE_PATH` | `data/messages.sqlite3` | SQLite 路径（相对项目根） | 其父目录同时决定 `avatars/`、`media_cache/`、`card_images/`、`face_cache/` 的位置 |

复读判定所需的最少参与人数 `REPEAT_MIN_SENDERS`（值 2）是**代码常量**，不可通过 `.env` 配置。

### 2.3 图片缓存

| 键 | 默认值 | 含义 | 取值范围 |
|---|---|---|---|
| `IMAGE_CACHE_MAX_DIMENSION` | `2560` | 缓存图片最长边上限。**不超过**它就原样保存，不重编码 | 768–8192 |
| `IMAGE_CACHE_JPEG_QUALITY` | `92` | 只有超过上面上限、必须重编码时才使用的 JPEG 质量 | 70–100 |
| `RECALL_THUMB_SIZE` | `1200` | 卡片里图片框的上限；实际框宽还会被卡片内容宽度压住（取两者较小值） | 256–2400 |
| `QQ_FACE_DIR` | 空 | QQ 内置表情（小黄脸）的图源目录，指向客户端的 `sysface_res`（或其父目录）。留空时自动探测「文档\Tencent Files」，探测不到退回腾讯表情 CDN | 目录路径 |

### 2.4 漂流瓶（精华池）

键名沿用 `ESSENCE_*`，**改版后数值语义完全不变**，这样已有 `.env` 不需要改动。

| 键 | 默认值 | 含义 | 取值范围 |
|---|---|---|---|
| `ESSENCE_COOLDOWN_SECONDS` | `60` | 每人每群的漂流瓶冷却 | 10–3600 |
| `ESSENCE_DAILY_LIMIT` | `10` | 每人每天的漂流瓶次数（两个池子共用）。2026-10-15 由 5 提到 10 | 1–100 |
| `ESSENCE_CACHE_SECONDS` | `600` | 精华列表的内存缓存时长 | 30–86400 |

### 2.5 群相册（漂流瓶的第二个来源）

| 键 | 默认值 | 含义 | 取值范围 / 注意 |
|---|---|---|---|
| `BOTTLE_ALBUM_ENABLED` | `1` | 是否把群相册当作来源 | 假值集合：`0` / `false` / `no` / `off`。关掉即回到"只抽精华" |
| `BOTTLE_ALBUM_CACHE_SECONDS` | `600` | 相册内容列表的内存缓存时长 | 30–86400 |
| `BOTTLE_ALBUM_SHARE` | `50` | 一次抽取有多大比例来自相册 | 0–100。**`0` 等于关闭相册**：`album_pool_enabled()` 为假，`漂流瓶 相册` 也会拒绝，连续同类补偿同样不生效 |
| `BOTTLE_SWITCH_STREAK` | `3` | 连续这么多次捞到同一类时，下一次强制换另一类 | 0–20，`0` = 关闭补偿 |
| `BOTTLE_ALBUM_MAX_PAGES` | `3` | 单次拉取相册时最多翻几页 | 1–20，防止分页异常时无限翻 |
| `BOTTLE_ALBUM_MAX_PHOTOS` | `400` | 单次拉取最多收多少条相册内容 | 1–5000 |
| `BOTTLE_REPEAT_MODE` | `lru` | 近期防重复策略：`lru` = 从随机的 K 个候选里挑最久没被抽到的；`off` = 纯随机（回滚开关） | `lru` / `off`（也认 `0`/`false`/`no`）。两种模式下"当天不重复"都保留 |
| `BOTTLE_REPEAT_PICK_K` | `5` | 候选采样宽度 K：1 = 纯随机，越大越接近严格轮转 | 1–50。实测 K=5 已能把"近 100 次内不同内容数"从 77 提到 97 |
| `BOTTLE_HISTORY_DAYS` | `30` | 抽取历史保留天数，同时决定"最久未抽"能看多远（一轮 ≈ 池子大小 / 每日抽取次数） | 1–365。**提高它不影响"当天去重"**（那只查今天），只是多存几行；`essence_draws` 现在是百行量级 |

### 2.6 昨日总结与模型

| 键 | 默认值 | 含义 | 取值范围 / 注意 |
|---|---|---|---|
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 | 尾部 `/` 会被去掉。**只有昨日总结会调用模型** |
| `LLM_API_KEY` | 空 | 模型 API Key | 只从 `.env` 读；不写进日志、群消息或图片 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 | — |
| `SUMMARY_USE_LLM` | `1` | 是否让模型写总结正文 | 假值集合同 `BOTTLE_ALBUM_ENABLED`；设为假时正文退化为本地精选片段 |
| `SUMMARY_DIGEST_CHARS` | `6000` | 交给模型的聊天记录字符预算 | 1000–20000 |
| `SUMMARY_MAX_CHARS` | `600` | 整条报告的硬上限 | 200–2000 |
| `REPEAT_MIN_COUNT` | `4` | 连续相同文本达到这个长度才算"复读" | 2–50，且需要至少 2 个不同的人参与 |
| `VOICE_MAX_SECONDS` | `60` | 语音点歌的时长上限：**超过它的歌自动截成前 N 秒**再发（不重新编码，在 MP3 帧边界切；结果缓存到 `data/voice/.trimmed/`） | 0–300，`0` = 不截断（交给 NapCat，失败时有兜底提示） |
| `ARCHIVE_ENABLED` | `1` | 多媒体永久归档开关：开 = 收到消息就把图片/视频/语音/文件存进 `data/media_archive/`（内容寻址、去重、**永不清理**）；关 = 退回"只在查撤回时按需下载"的旧行为（QQ 的媒体 URL 过期后就拿不回来了） | `1` / `0` |
| `ARCHIVE_MAX_BYTES` | `104857600`（100 MiB） | **单个文件**的归档上限。超过的只写一行带原因的记录、不落盘 | 0–∞，`0` = 不限制；这是上限不是清理阈值 |

### 2.7 分群功能开关

白名单群默认拥有全部功能；这两项用来把个别群从某一项功能里排除（群本身仍在 `ALLOWED_GROUP_IDS` 里，归档与其它命令不受影响）。

| 键 | 默认值 | 含义 | 取值范围 / 注意 |
|---|---|---|---|
| `WATER_EXCLUDED_GROUP_IDS` | 空 | 不接收喝水提醒（含 `喝水提醒 测试`）的群，逗号分隔 | 群号；留空 = 所有白名单群都收 |
| `STATUS_EXCLUDED_GROUP_IDS` | 空 | 不计入 `status` 报告、且在这些群里发 `status` 也完全静默的群，逗号分隔 | 群号；留空 = 所有白名单群都计入 |
| `BOTTLE_ONLY_HELP_GROUP_IDS` | 空 | `help` 只返回漂流瓶用法、不出现撤回记录/昨日总结/管理员说明的群，逗号分隔 | 群号；留空 = 所有白名单群都用完整 help |

### 2.8 不由 `config.py` 读取的键

这些键在 `.env.example` 里，但由 NoneBot 或启动脚本消费：

| 键 | 消费者 | 说明 |
|---|---|---|
| `DRIVER` | NoneBot（`nonebot.init()`） | 默认 `~fastapi` |
| `HOST` / `PORT` | NoneBot（`nonebot.run()`） | 反向 WebSocket 的监听地址与端口，默认 `0.0.0.0` / `8080` |
| `ONEBOT_ACCESS_TOKEN` | `scripts/lifecycle/start-all.ps1` | 写进 NapCat 的 OneBot 配置，作为反向 WS 的凭据 |
| `NAPCAT_UIN` | `scripts/lifecycle/start-all.ps1` | 指定要写配置的账号；留空则从已有的 `onebot11_*.json` 探测 |
| `NAPCAT_COMPONENT_DIR` | `scripts/lifecycle/start-all.ps1` | 覆盖 NapCat 组件目录（默认已注释） |

## 3. 运行时开关（`bot_settings`）

| 键名（`name`） | 默认 | 语义 | 谁能改 |
|---|---|---|---|
| `recall_private_delivery` | 开 | 撤回记录的私聊投递（`撤回记录 私聊` 与私聊命令入口） | 关：群里或私聊的管理员；**开：只有主人在私聊** |

存储形式为 `value` 列里的字符串 `"0"` / `"1"`，并带 `updated_at`。`set_switch()` 会返回"值是否真的发生变化"，因此回复文案能区分"已经关掉了"与"本来就是关着的"。表中**没有行**时按默认值（开）处理。

## 4. 代码常量（想调也调不了）

| 常量 | 位置 | 值 | 说明 |
|---|---|---|---|
| `COOLDOWN_SECONDS` | `cooldown.py` | 120 | 非管理员普通 @ 的每群每人冷却 |
| `GROUP_RECALL_COOLDOWN_SECONDS` | `cooldown.py` | 120 | 撤回记录的全群共用窗口；主人豁免 |
| `GROUP_TIMEZONE` | `config.py` | UTC+8 | 所有"本地日"与展示时间的时区 |
| `MAX_CACHED_IMAGE_BYTES` / `AVATAR_CACHE_MAX_AGE` | `config.py` | 12 MiB / 7 天 | 单张缓存图上限；头像缓存有效期 |
| `ESSENCE_IMAGE_ATTEMPTS` / `ESSENCE_DRAW_RETENTION_DAYS` | `essence.py` | 6 / 7 | 单次抽取最多尝试几个候选；`essence_draws` 保留天数 |
| `ESSENCE_IMAGE_SUFFIXES` / `ALBUM_IMAGE_SUFFIXES` | `essence.py` / `album.py` | `.myqcloud.com` 等 | 精华与相册取图额外放行的域名后缀 |
| `CARD_*` 几何常量 | `card.py` | 见文件 | 卡片宽度、边距、缩略图列数等排版参数 |

## 5. 已知的配置层不一致

| 项 | 现状 |
|---|---|
| `ALLOWED_GROUP_ID`（单数） | 代码仍支持这个旧键作为回退，但 `.env.example` 只写了复数形式。建议只用复数 |
| 遗留命名 | `config.py` 的 `CARD_IMAGE_DIR`、`store.py` 的 `essence_usage` 注释仍写"群精华"；模块 `essence.py`、表 `essence_draws` / `essence_usage`、配置键 `ESSENCE_*` 也保留"精华"字样。这属于**刻意**决定（避免无收益的数据迁移），只影响读代码时的观感，与用户看到的"漂流瓶"叫法无关 |
| `.env` 变更的生效时机 | 除运行时开关外，所有键都在**进程启动时读一次**，改完需要重启机器人（`stop.ps1` + `scripts/lifecycle/start.ps1`，或让 supervisor 拉起） |
