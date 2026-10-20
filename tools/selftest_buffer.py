"""抖动缓冲水位自适应 —— 纯逻辑,不用手机也不用声卡。

★ 它盯的是「延迟」这一项:目标水位必须能**自己降下去**,遇到欠载又要**自己涨回来**。
  写死一个数的话,要么延迟永远白高,要么偶尔咔一下;这两件事都不会报错。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_buffer.py
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_engine import BUF_HI_MS, BUF_LO_MS, Engine   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def age(e: Engine, seconds: float) -> None:
    """把「上一次出事 / 上一次调整 / 上一次撑不住」往前挪,假装已经过了这么久。"""
    e._tune_t -= seconds
    e._tune_last_bad -= seconds
    e._tune_up_t -= seconds
    e._open_t -= seconds          # 预热期也跟着过去
    if e._tune_hold_t:
        e._tune_hold_t -= seconds
    # ★ 欠载的时刻也往前挪 —— 不然「10 秒内反复欠载」那个窗口永远不成立,
    #   测试就变成在测一条永远不会走的路径。
    e._und_times = deque((t - seconds for t in e._und_times), maxlen=16)


def spill(e: Engine, n: int = 1) -> None:
    """来 n 次欠载并跑一次自适应。"""
    e.voice.underruns += n
    e._tune_buffer()


def main() -> int:
    e = Engine()
    n = e.block
    # ★ 上下限是**毫秒**(2026-10-18 改的)。以前按「几块」写,块长一变
    #   「3 块」就从 30ms 变 15ms —— 那种跟着块长漂的常数最容易悄悄改坏。
    # ★ 跟引擎里的算法**一字不差**:毫秒地板,但至少装得下两块。
    lo = max(int(round(BUF_LO_MS / 1000.0 * e.sr)), 2 * n)
    hi = int(round(BUF_HI_MS / 1000.0 * e.sr))
    print(f"块 = {n} 样本({1000 * n / e.sr:.0f}ms),地板 = {lo} 样本({1000 * lo / e.sr:.0f}ms),"
          f"上限 = {hi} 样本({1000 * hi / e.sr:.0f}ms),初始目标 = {e.voice.target} 样本")

    # 1) 一直稳 → 一路降,但不会降穿地板
    for _ in range(10):
        age(e, 30.0)
        e._tune_buffer()
    check("稳定后会降到底噪地板(15ms)", e.voice.target == lo,
          f"{e.voice.target} 样本 = {e.voice.target / e.sr * 1000:.0f}ms")

    # 2) ★ 单次离群**不许**抬水位。
    #    手机 JS 线程偶尔卡一下就是 60~70ms 的空洞(实测 arr_max 67.9ms),
    #    20ms 挡不住、40ms 也照样挡不住 —— 为它永久抬水位就是「延迟白高,咔照旧」。
    spill(e)
    check("单次离群不抬水位", e.voice.target == lo, f"{e.voice.target} 样本")

    # 2b) 15 秒内反复欠载(5 次)→ 才认定水位真的不够,加一块
    for _ in range(5):
        spill(e)
    check("15 秒内欠 5 次 → 加一块", e.voice.target == lo + n, f"{e.voice.target} 样本")
    check("漂移校正跟着改目标", e.dc.target == e.voice.target, f"dc.target={e.dc.target}")

    # 3) 连着欠载 → 涨到 80ms 就封顶。★ 每涨一块至少隔 2 秒(见 _tune_buffer):
    #    一次系统卡顿会连着欠载好几次,不设间隔会一口气顶到 80ms。
    for _ in range(20):
        spill(e, 5)
        age(e, 3.0)
    check("上限 80ms", e.voice.target == hi, f"{e.voice.target} 样本")

    # 3b) 同一秒里反复欠载,也**只涨一块**(加块至少隔 2 秒)
    e3 = Engine()
    e3.voice.target = lo
    e3._tune_und = 0
    for _ in range(6):
        spill(e3)
    check("2 秒内只涨一块", e3.voice.target == lo + n, f"{e3.voice.target} 样本")

    # 4) 刚出过事,不许马上往下探(否则会来回抖)
    age(e, 5.0)                      # 只稳了 5 秒
    e._tune_buffer()
    check("刚出事 20 秒内不往下探", e.voice.target == hi, f"{e.voice.target} 样本")

    # 5) 稳够之后 → 一次只降一块,不跳
    #    (这里要过 5 分钟:刚撑不住的那个水位不许马上再探,规矩在 test 9)
    age(e, 400.0)
    e._tune_buffer()
    check("一次只降一块", e.voice.target == hi - n, f"{e.voice.target} 样本")

    # 6) 没在推流的时候不许乱涨(手机没连,underruns 不涨,目标就在地板)
    #    ★ 从 80ms 降回 15ms 要 13 步,一次只降一块 —— 所以这里得多来几轮。
    for _ in range(40):
        age(e, 30.0)
        e._tune_buffer()
    check("没声音时目标停在地板", e.voice.target == lo, f"{e.voice.target} 样本")

    # 7) 手机重连时 voice.reset() 把欠载清零 —— 那不算「又出事了」,不许涨
    e.voice.underruns += 1
    e._tune_buffer()
    before = e.voice.target
    e.voice.underruns = 0                  # ← 重连(voice.reset())
    age(e, 1.0)
    e._tune_buffer()
    check("重连清零不会把目标顶上去", e.voice.target == before, f"{e.voice.target} 样本")

    # 8) 块大小变了(设备采样率不同)→ **地板还是 15ms**,不跟着块长漂
    e.block = 441
    e.voice.target = 3 * e.block
    for _ in range(20):
        age(e, 400.0)
        e._tune_buffer()
    lo441 = max(int(round(BUF_LO_MS / 1000.0 * e.sr)), 2 * e.block)
    check("地板不跟着块长漂(至少两块)", e.voice.target == lo441,
          f"块 441 → 停在 {e.voice.target} 样本 = {e.voice.target / e.sr * 1000:.0f}ms")

    # 9) 「撑不住的那个水位」5 分钟内不许反复去探 —— 不然就是每 20 秒咔一声
    e2 = Engine()
    e2.voice.target = lo               # 正好在地板
    e2._tune_und = 0
    e2._tune_t = time.time()
    e2._tune_last_bad = time.time()
    spill(e2, 5)                       # 地板上确实撑不住 = 反复欠载
    check("地板上撑不住 → 涨一块", e2.voice.target == lo + e2.block, f"{e2.voice.target} 样本")
    age(e2, 30.0)
    e2._tune_buffer()
    check("5 分钟内不回到撑不住的水位", e2.voice.target == lo + e2.block, f"{e2.voice.target} 样本")
    age(e2, 400.0)
    e2._tune_buffer()
    check("5 分钟后再探一次", e2.voice.target == lo, f"{e2.voice.target} 样本")

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
