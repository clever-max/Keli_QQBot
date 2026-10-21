import nonebot
from nonebot.adapters.onebot.v11 import Adapter

# 注意：这个文件在 `nonebot.init()` 之前只做"与配置无关"的事。


def prune_old_logs(keep: int = 30) -> None:
    """只保留最近 ``keep`` 个 bot 日志，防止日志目录无限堆积。

    放在入口而不是启动脚本里：手动启动走 ``start.ps1``，但看门狗重启是直接
    ``Start-Process python bot.py``，会绕过所有 PowerShell 启动脚本——只有这里
    两条路径都会执行。一次启动一个文件，不清理时一年会攒下一千多个。
    """
    import pathlib

    log_dir = pathlib.Path(__file__).resolve().parent / "logs"
    if not log_dir.is_dir():
        return
    for pattern in ("bot-*.log", "bot-*.err.log"):
        files = [
            path
            for path in log_dir.glob(pattern)
            # `bot-*.log` 也会匹配 `bot-x.err.log`（都以 .log 结尾），这里按后缀分开，
            # 否则两类日志会互相挤占配额（实测会变成各 15 个而不是各 30 个）。
            if path.name.endswith(".err.log") == pattern.endswith(".err.log")
        ]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass


nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    prune_old_logs()
    nonebot.run()
