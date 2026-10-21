"""跑仓库里的全部行为套件。

用法：
    .venv\\Scripts\\python.exe tests\\run_all.py            # 全部
    .venv\\Scripts\\python.exe tests\\run_all.py verify_bottle.py  # 只跑指定的

判据：每个脚本独立进程运行，输出以「全部通过」结尾且退出码 0 才算过。
为什么独立进程：套件会各自 `nonebot.init()` 并改全局状态，同进程跑会互相污染。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PYTHON = TESTS.parent / ".venv" / "Scripts" / "python.exe"


def main() -> int:
    names = sys.argv[1:] or sorted(p.name for p in TESTS.glob("verify_*.py"))
    failures: list[str] = []
    for name in names:
        path = TESTS / name
        if not path.is_file():
            print(f"FAIL 找不到 {name}")
            failures.append(name)
            continue
        started = time.perf_counter()
        # 子进程是 Python：强制它用 UTF-8 写 stdout，否则在中文 Windows 上会是 GBK，
        # 而我们按 UTF-8 解码就会得到 None（这个坑在 commit-iteration.ps1 里暴露过）。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [str(PYTHON), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        elapsed = time.perf_counter() - started
        ok = result.returncode == 0 and "全部通过" in (result.stdout or "")
        print(f"{'ok  ' if ok else 'FAIL'} {name:<28}{elapsed:6.1f}s")
        if not ok:
            failures.append(name)
            tail = "\n".join(((result.stdout or "") + (result.stderr or "")).splitlines()[-8:])
            print("     " + tail.replace("\n", "\n     "))

    print("-" * 60)
    total = len(names)
    print(f"通过 {total - len(failures)} / {total}")
    if failures:
        print("失败: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
