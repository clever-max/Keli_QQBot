# 行为套件（回归测试）

全部是**可执行断言脚本**，不是 pytest：每个文件独立进程运行、自己 `nonebot.init()`，
输出以「全部通过」结尾、失败退出码 1。

```powershell
.venv\Scripts\python.exe tests\run_all.py                # 跑全部
.venv\Scripts\python.exe tests\run_all.py verify_bottle.py # 只跑一个
```

`scripts\dev\commit-iteration.ps1` 在提交前会自动跑一遍（`-SkipVerify` 可跳过）。

| 套件 | 覆盖 |
|---|---|
| `verify_recall.py` | 撤回记录的分页、时间窗、去重、游标推进 |
| `verify_recall_enhancements.py` | 回复/合并转发预览、媒体标签、撤回竞态、视频/文件原文件召回、聊天记录不返回、分页失败幂等标记 |
| `verify_recall_history.py` | 主人私聊「撤回记录 历史」：跨群、包含已看过的、不标记未读、条数限制、非主人被拒、群里不触发 |
| `verify_delivery.py` | 群聊/私聊投递选择与兜底文案 |
| `verify_switch.py` | `bot_settings` 运行时开关的持久化与回读 |
| `verify_gate.py` | 权限闸门与冷却（非管理员 @ 冷却、主人豁免） |
| `verify_permissions.py` | 群/用户白名单、`*` 通配、临时会话 |
| `verify_nogroupid.py` | 未配置群号时的静默行为 |
| `verify_bottle.py` | 漂流瓶：池子混合比例、相册开关、内容提取 |
| `verify_media.py` | 图片/视频缓存、路径校验、发送兜底；精华视频直接转发、文件类精华发原文件 + 文字提示 |
| `verify_font.py` | 字体覆盖（机器人文案 0 缺字；线上数据只报覆盖率） |
| `verify_face.py` | QQ 表情渲染：id→名字、`[表情:id:名字]` 标记、混排不拆标记、本地图源/缓存、缺图退回文字 |
| `verify_group_scope.py` | 分群隔离：新增群只拿自己的授权、别的群的 `*` 不串群；喝水/status 的分群排除；bottle-only 群只放行漂流瓶 |
| `verify_drawcount.py` | 被抽到次数、榜单、卡片副标题 |
| `verify_repeat_sampling.py` | 近期防重复抽样（排序而非禁用） |
| `verify_config_docs.py` | 配置三处一致（也在 `scripts/dev/` 有同一份，供提交脚本调用） |
| `verify_voice.py` | 语音点歌：曲库、点歌、URI 发送与回退 |
| `verify_voice_audio.py` | 超过时长上限的自动截断（MP3 帧边界） |
| `verify_archive.py` | 多媒体永久归档：内容寻址、留痕、补扫幂等 |
| `verify_messages_pk.py` | `(group_id, message_id)` 主键迁移 |
| `verify_media_converge.py` | 渲染缓存从归档复制（不联网） |
