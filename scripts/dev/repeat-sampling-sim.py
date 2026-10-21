"""漂流瓶「减少重复抽样」的机制对比模拟器。

用途：在真实池子规模与抽取节奏下，比较几种抽样机制"近一段时间内重复"的程度，
用来复跑参数、比较方案。

用法（本机虚拟环境）：
    .\\.venv\\Scripts\\python.exe .\\scripts\\dev\\repeat-sampling-sim.py

模拟的前提与生产一致：保留「当天不重复」的硬约束（同一内容当天抽过就不再出现），
每 36 分钟抽一次（约 40 次/天），20~30 组随机种子取均值。

结论摘要（详见规划文档）：
- 时间衰减加权、按抽取间隔加权都会让重复率**变高**——压制"刚抽过"等于把抽样挤到
  "最久没抽"的尾巴子集上，有效池子变小；
- 「随机化的最久未抽优先」（power of k choices，k=5）在近 100 次窗口内能覆盖 97/100
  条不同内容，且实现无状态（完全由 essence_draws 派生），这就是线上采用的方案。

第一轮的两个教训（保留备查）：
- `distinct_per_100` 曾写成「不同内容数 / 总抽取数」，随总次数增加必然趋于 0，是无意义指标；
- 时间衰减 H=24h 太弱（24 小时才衰减到一半），而「当天不重复」的硬约束已经把
  短窗口重复压得很低，所以时间衰减几乎只剩噪声。
"""

from __future__ import annotations

import random
import statistics
from collections import deque

DAY = 24 * 3600


def simulate(strategy, n_items: int, draws: int, interval: float, seed: int) -> list[int]:
    rng = random.Random(seed)
    keys = list(range(n_items))
    last_time: dict[int, float] = {}
    last_index: dict[int, int] = {}
    count: dict[int, int] = {k: 0 for k in keys}
    drawn_today: set[int] = set()
    day_index = -1
    recent: deque[int] = deque(maxlen=5)
    deck: list[int] = []
    history: list[int] = []
    now = 0.0

    for step in range(draws):
        now = step * interval
        today = int(now // DAY)
        if today != day_index:
            day_index = today
            drawn_today.clear()
        pool = [k for k in keys if k not in drawn_today]
        if not pool:
            continue  # 当天抽干：生产中提示"明天再来"并退还次数

        kind = strategy[0] if isinstance(strategy, tuple) else strategy
        if kind == 'uniform':
            pick = rng.choice(pool)
        elif kind == 'recent5':
            candidates = [k for k in pool if k not in recent] or pool
            pick = rng.choice(candidates)
        elif kind == 'time':
            half_life = strategy[1]
            weights = []
            for key in pool:
                if key in last_time:
                    hours = (now - last_time[key]) / 3600.0
                    weights.append(max(0.02, 2 ** (-hours / half_life)))
                else:
                    weights.append(1.0)
            pick = rng.choices(pool, weights=weights, k=1)[0]
        elif kind == 'gap':
            # 按「距上次被抽到过了多少次抽取」加权：与群活跃度自动同尺度，
            # 不需要知道真实时间。
            beta = strategy[1]
            weights = []
            for key in pool:
                if key in last_index:
                    gap = step - last_index[key]
                    weights.append(1.0 / (1.0 + gap) ** beta)
                else:
                    weights.append(1.0)
            pick = rng.choices(pool, weights=weights, k=1)[0]
        elif kind == 'deck':
            # 发牌式：一轮内不重复，抽干后重洗（等价于"尽可能摊开"，但周期边界会露馅）
            deck = [k for k in deck if k in pool]
            if not deck:
                deck = list(pool)
                rng.shuffle(deck)
            pick = deck.pop()
        elif kind == 'powerk':
            # 随机取 k 个候选，挑其中最久没被抽到的（"多个选择的力量"）。
            # 不需要维护牌堆，天然适应池子增删，且靠 drawn_at 就能持久化。
            k = min(len(pool), strategy[1])
            cands = rng.sample(pool, k)
            pick = min(cands, key=lambda key: last_time.get(key, -1.0))
        elif kind == 'oldestq':
            # 在最久没被抽到的那一档里随机挑一个，避免完全确定的轮转。
            ordered = sorted(pool, key=lambda key: last_time.get(key, -1.0))
            upto = max(1, int(len(ordered) * strategy[1]))
            pick = rng.choice(ordered[:upto])
        else:
            raise SystemExit('unknown strategy')

        drawn_today.add(pick)
        recent.append(pick)
        last_time[pick] = now
        last_index[pick] = step
        count[pick] += 1
        history.append(pick)

    return history


def metrics(history: list[int]) -> dict[str, float]:
    total = len(history)
    out: dict[str, float] = {}
    for window in (5, 10, 20, 50):
        repeats = sum(
            1
            for index in range(window, total)
            if history[index] in history[index - window:index]
        )
        out[f'repeat{window}'] = 100 * repeats / max(1, total - window)
    # 滑动 100 次窗口内的不同内容数（反映"近一段时间摊得开不开"）
    if total > 100:
        out['distinct100'] = statistics.mean(
            len(set(history[i:i + 100])) for i in range(0, total - 100, 7)
        )
    else:
        out['distinct100'] = len(set(history))
    gaps: list[int] = []
    last_seen: dict[int, int] = {}
    for index, key in enumerate(history):
        if key in last_seen:
            gaps.append(index - last_seen[key])
        last_seen[key] = index
    out['mean_gap'] = statistics.mean(gaps) if gaps else float('nan')
    return out


def compare() -> None:
    print('节奏：每 36 分钟一次（约 40 次/天），保留「当天不重复」硬约束')
    strategies = [
        ('uniform', 'uniform'),
        ('recent5（现状）', 'recent5'),
        ('time H=6h', ('time', 6)),
        ('time H=24h', ('time', 24)),
        ('gap b=1', ('gap', 1)),
        ('deck（发牌式）', 'deck'),
        ('powerk k=3', ('powerk', 3)),
        ('powerk k=5', ('powerk', 5)),
        ('powerk k=8', ('powerk', 8)),
        ('oldest 20% 随机', ('oldestq', 0.2)),
    ]
    for n_items in (60, 130):
        print()
        print(f'--- 池子 {n_items} 个内容 ---')
        print(f'{"策略":<22}{"重复@5":>8}{"@10":>7}{"@20":>7}{"@50":>7}{"近100次内不同数":>16}{"平均间隔":>9}')
        for label, strategy in strategies:
            runs = [metrics(simulate(strategy, n_items, 3000, 2160, seed)) for seed in range(15)]
            avg = {k: statistics.mean(r[k] for r in runs) for k in runs[0]}
            print(f'{label:<22}{avg["repeat5"]:>7.2f}%{avg["repeat10"]:>6.2f}%'
                  f'{avg["repeat20"]:>6.2f}%{avg["repeat50"]:>6.2f}%'
                  f'{avg["distinct100"]:>16.1f}{avg["mean_gap"]:>9.1f}')


if __name__ == '__main__':
    compare()
