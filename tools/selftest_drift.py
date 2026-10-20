"""时钟漂移校正的自测 —— 纯模拟,不需要手机、不需要声卡。

为什么要有这一条:
    真机上曾经看到 drift 在 +1.20% / -1.20% 之间来回撞,缓冲 28ms → 100ms 地跳。
    1% 的频率摆动 ≈ 17 音分 = 唱的时候能听出来的「颤」。
    根因是拿「瞬时水位」做校正,而 WebSocket 本来就是一阵一阵到的。

这一条把「手机按 20ms 一块发、声卡按 10ms 一块取」这个节奏原样模拟出来,
让控制器自己跑 60 秒,然后量三件事:
    1. 缓冲不许一路涨到上限(那是重采样复制声音那个老 bug 的样子)
    2. 缓冲不许见底(那是校正过头)
    3. drift 最后 20 秒不许来回摆(那就是颤音)

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_drift.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_engine import Engine  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR = 48000
OUT_BLOCK = 480          # 10ms 取一次
PHONE_BLOCK = 960        # 20ms 发一次
STEP_MS = 10


def simulate(phone_sps_ratio: float, seconds: float = 60.0, seed: int = 1,
             jitter_ms: float = 0.0):
    """phone_sps_ratio=0.99 表示手机每秒只发 47520 个样本(晶振慢 1%)。

    jitter_ms > 0 时,每个包再随机晚 0~jitter_ms 毫秒到 —— 模拟 USB/WebSocket
    那种一阵一阵的到达。这是抖动缓冲真正要挡的东西。
    """
    rs = np.random.RandomState(seed)
    eng = Engine()
    eng.set_phone_rate(SR)

    steps = int(seconds * 1000 / STEP_MS)          # 每步 = 10ms
    acc = 0.0
    in_flight: list[list] = []                     # [到期步, 样本数]
    fill_hist = np.zeros(steps)
    drift_hist = np.zeros(steps)

    for i in range(steps):
        acc += SR * phone_sps_ratio * (STEP_MS / 1000.0)
        # ★ 一次可能攒出不止一块;也可能攒不够(那就这一步不发)
        while acc >= PHONE_BLOCK:
            acc -= PHONE_BLOCK
            # 每个包按自己的随机延迟排队 —— 这才是真实网络的样子
            due = i + (int(rs.rand() * (jitter_ms / STEP_MS)) if jitter_ms else 0)
            in_flight.append([due, PHONE_BLOCK])

        # 到期的包一起交出去(同一时刻到了几个就是几个)
        ready = sum(n for due, n in in_flight if due <= i)
        if ready:
            in_flight = [p for p in in_flight if p[0] > i]
            eng.push_pcm16(np.zeros(int(ready), dtype="<i2").tobytes())

        fill_hist[i] = eng.voice.fill_samples()
        drift_hist[i] = eng.dc.d
        eng.voice.pull(OUT_BLOCK)                  # 声卡取走一块

    return eng, fill_hist, drift_hist


def run() -> int:
    print("时钟漂移校正 · 模拟 60 秒")
    print("=" * 70)
    bad = 0
    target = 4 * 480

    cases = (
        ("手机慢 1%", 0.99, 0.0),
        ("手机快 1%", 1.01, 0.0),
        ("两边一样", 1.0, 0.0),
        ("手机慢 1% + 30ms 抖动", 0.99, 30.0),
        ("手机快 1% + 30ms 抖动", 1.01, 30.0),
    )
    for label, ratio, jit in cases:
        eng, fill, drift = simulate(ratio, jitter_ms=jit)
        tail_fill = fill[-2000:]                   # 最后 20 秒
        tail_drift = drift[-2000:]
        mean_fill_ms = tail_fill.mean() / SR * 1000
        lo_ms = tail_fill.min() / SR * 1000
        hi_ms = tail_fill.max() / SR * 1000
        d_mean = tail_drift.mean() * 100
        d_pp = (tail_drift.max() - tail_drift.min()) * 100

        print(f"\n{label}(实际发 {SR*ratio:.0f} sp/s)")
        print(f"  缓冲:平均 {mean_fill_ms:5.1f}ms  范围 {lo_ms:.1f}~{hi_ms:.1f}ms"
              f"  (目标 {target/SR*1000:.0f}ms,上限 {eng.voice.limit/SR*1000:.0f}ms)")
        print(f"  校正:平均 {d_mean:+.2f}%  最后 20 秒摆动 {d_pp:.2f} 个百分点")
        print(f"  欠载 {eng.voice.underruns} 次,溢出 {eng.voice.overruns} 次")

        target_ms = target / SR * 1000
        checks = [
            ("缓冲没顶到上限", eng.voice.overruns == 0),
            ("缓冲没见底", lo_ms > 5.0),
            # ★ 这一条是这次加积分项的原因:只有比例项时,手机快 1%
            #   会让缓冲稳稳停在 87ms(目标 40ms),白多 47ms 延迟。
            ("稳态水位离目标不超过 20ms", abs(mean_fill_ms - target_ms) <= 20.0),
            ("校正量摆得够小(<0.3 个百分点)", d_pp < 0.3),
            # 手机慢 1%(ratio<1)⇒ 必须往负方向修,反之亦然
            ("校正方向对", d_mean * (ratio - 1.0) >= 0 or abs(ratio - 1.0) < 1e-9),
        ]
        for name, ok in checks:
            print(f"    {'✓' if ok else '✗'} {name}")
            if not ok:
                bad += 1

    print("\n" + "=" * 70)
    print("全部通过" if bad == 0 else f"★ {bad} 项没过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
