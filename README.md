# QQ 群机器人

基于 **NoneBot2 + NapCat（OneBot v11）+ SQLite** 的 QQ 群机器人。业务逻辑、消息归档与图片
生成全部在本机完成：消息进本地 SQLite，撤回可召回，卡片图本地渲染后发送。

当前功能概览：

| 功能 | 一句话 |
|---|---|
| 归档与召回 | 白名单群的消息进本地 SQLite；被撤回的消息可由机器人翻出来渲染成卡片图 |
| 私聊投递 | 召回结果可改投到发起人的私聊 / 群临时会话，群里只留一句回执；带总开关与全群冷却 |
| 漂流瓶 | 从群精华与群相册里随机捞一个（也可只抽相册）；配「丢漂流瓶榜」 |
| 昨日总结 | 昨日群聊的本地统计，正文交给模型归纳（可用 `SUMMARY_USE_LLM=0` 关掉） |
| status | 本机体检报告，只发主人私聊，且不含任何群号 |

命令、权限与冷却的完整说明见 [docs/commands.md](docs/commands.md)。

## 快速开始

需要 Windows 与 Python（本机用的是 32 位 3.13）。QQ 登录由 NapCat Desktop 负责，
机器人通过 OneBot 11 **反向** WebSocket 连到 `127.0.0.1:8080`。

**1. 建 Python 环境**

```powershell
py -3.13-32 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**2. 配置**

复制 `.env.example` 为 `.env` 并填写：允许的群号、授权名单、`ONEBOT_ACCESS_TOKEN`，
模型 Key 可选（只有昨日总结会用）。每个配置项的说明见
[docs/configuration.md](docs/configuration.md)。

> **不要把 `.env` 提交到 Git。** 它含访问令牌与模型 API Key；`data/`、`logs/`、`run/`
> 同样已被 `.gitignore` 忽略（里面有群消息原文与群号）。

**3. 启动**

```powershell
.\start-all.cmd      # 一键：写好 NapCat 反向 WebSocket 配置并打开 NapCat Desktop
```

或分步：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\lifecycle\start-all.ps1    # 机器人 + NapCat
.\stop.ps1                           # 只停机器人；NapCat/QQ 请从 Desktop 关
```

只想前台跑机器人：`.\.venv\Scripts\python.exe bot.py`。仓库内另有 `Dockerfile` 与
`compose.yaml`（Compose 只绑定 `127.0.0.1:8080`）。

两点注意：

- `.venv` **不能**从别的机器或目录复制过来——`pyvenv.cfg` 指向原机器的 Python 路径，
  路径不存在时 `python.exe` 直接起不来；换机器请按上面命令重建。
- 机器人需要机器保持开机不休眠；`start-all.cmd` 本身**不会**配置 Windows 开机自启。
- 8080 只服务本机的 NapCat，**不要**把这个端口暴露到公网。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | **文档索引与文档约定**（先看这个） |
| [docs/commands.md](docs/commands.md) | 命令与权限总表：命令、别名、权限、冷却与文案 |
| [docs/architecture.md](docs/architecture.md) | 架构与模块职责、依赖方向、数据表、数据流 |
| [docs/configuration.md](docs/configuration.md) | 配置项说明：`.env` 与 SQLite 运行时开关 |
| [docs/development.md](docs/development.md) | 开发规范：提交/推送规则、验证方式、提交前清单 |
| [docs/operations.md](docs/operations.md) | 运行与运维：启动/看门狗、路径产物、排障、备份 |
| [scripts/README.md](scripts/README.md) | **脚本目录**：按功能分类的脚本清单、用法与改动约定 |
| [CHANGELOG.md](CHANGELOG.md) | 每个可运行迭代的追加式登记 |
