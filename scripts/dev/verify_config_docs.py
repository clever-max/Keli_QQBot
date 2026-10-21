"""配置三处一致性：`config.py` 读取的每个键，必须同时出现在 `.env.example` 与
`docs/configuration.md`。

为什么要有它：配置分散在三处（代码默认值、示例文件、文档表格），只靠人肉同步一定会漂移——
2026-10-17 评估时就发现 `ALLOWED_GROUP_ID` 被 `config.py` 回退读取却不在 `.env.example` 里。
这个脚本把它变成可执行的约束，并且**接进了 `scripts/dev/commit-iteration.ps1`**，
每次提交前自动跑。

另外自带一个"自检"：故意构造一份缺键的文本，断言检测器真的能报出来——否则一个永远通过的
检查等于没有检查（这类"假绿"比没测试更危险）。

用法：`python scripts/dev/verify_config_docs.py`（退出码 0 = 通过）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "plugins" / "group_bot" / "core" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
DOC = ROOT / "docs" / "configuration.md"

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def config_keys(text: str) -> set[str]:
    """config.py 里真正被读取的键（`_setting(` / `_int_setting(` 的第一个参数）。"""
    keys = set(re.findall(r"_int_setting\(\s*[\"']([A-Z][A-Z0-9_]*)", text))
    keys |= set(re.findall(r"_setting\(\s*[\"']([A-Z][A-Z0-9_]*)", text))
    return keys


def env_keys(text: str) -> set[str]:
    """`.env.example` 里的键；注释掉的（`# KEY=`）也算——那表示"这里说明过这个键"。"""
    return set(re.findall(r"^[#\s]*([A-Z][A-Z0-9_]*)\s*=", text, re.M))


def doc_keys(text: str) -> set[str]:
    """`configuration.md` 里以行内代码形式提到的键（表格里都是这种写法）。"""
    return set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text))


def find_missing(keys: set[str], haystack: set[str]) -> list[str]:
    return sorted(keys - haystack)


def main() -> int:
    for path in (CONFIG, ENV_EXAMPLE, DOC):
        if not path.exists():
            print(f"FAIL 缺少文件 {path}")
            return 1

    keys = config_keys(CONFIG.read_text(encoding="utf-8"))
    env = env_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    doc = doc_keys(DOC.read_text(encoding="utf-8"))

    print("=== 1. 键的数量 ===")
    print(
        f"     config.py 读取 {len(keys)} 个键；.env.example {len(env)} 行键；"
        f"configuration.md 提到 {len(doc)} 个"
    )
    check_true("config.py 至少能抽到 20 个键（防止解析失效导致假绿）", len(keys) >= 20)

    print("\n=== 2. 三处一致 ===")
    check("config.py 的键都在 .env.example 里", find_missing(keys, env), [])
    check("config.py 的键都在 configuration.md 里", find_missing(keys, doc), [])

    print("\n=== 3. 自检：检测器真的会报缺键 ===")
    fake_keys = {"ALPHA_SETTING", "BETA_SETTING"}
    check("缺键能被检出", find_missing(fake_keys, {"ALPHA_SETTING"}), ["BETA_SETTING"])
    check("齐全时为空", find_missing(fake_keys, fake_keys), [])
    check("注释键算说明过", "COMMENTED_KEY" in env_keys("# COMMENTED_KEY=1\n"), True)
    check("纯注释不会被误认成键", "NOT_A_KEY" in env_keys("# NOT_A_KEY 只是文字\n"), False)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
