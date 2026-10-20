"""同一个人、同一句话:SingMic 录的和手机自带录音机录的,高频到底差多少。

★ 用途:分清「离麦远」和「浏览器把高频削了」。
   - 两份频谱形状接近 → 是距离/音量的事,按 2~3cm 唱就行;
   - SingMic 那份在 3k 以上一路低 10dB 以上 → 是采集路径,换采集方式。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\compare_capture.py 甲.wav 乙.mp3
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BANDS = ((100, 300), (300, 1000), (1000, 3000), (3000, 5000),
         (5000, 7000), (7000, 10000), (10000, 14000), (14000, 20000))


def profile(path: str) -> tuple[np.ndarray, float]:
    """返回 (相对 1–3k 的各频段电平, 总时长秒)。"""
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    x = y[:, 0].astype(np.float64)
    w = int(0.1 * sr)
    hop = w // 2
    frames = []
    for i in range(0, len(x) - w, hop):
        seg = x[i:i + w]
        frames.append((float(np.sqrt(np.mean(seg * seg))), seg))
    if not frames:
        raise SystemExit(f"{path}:太短了")
    frames.sort(key=lambda t: t[0])
    # 取最响的 30 帧 —— 唱得响的地方才代表「采集能拿到什么」
    loud = np.mean([f[1] for f in frames[-30:]], axis=0)
    S = np.abs(np.fft.rfft(loud * np.hanning(len(loud)))) ** 2
    f = np.fft.rfftfreq(len(loud), 1.0 / sr)

    def band(lo: float, hi: float) -> float:
        m = (f >= lo) & (f < hi)
        return 10 * np.log10(float(S[m].mean()) + 1e-30)

    ref = band(1000, 3000)
    return np.array([band(lo, hi) - ref for lo, hi in BANDS]), len(x) / sr


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = sys.argv[1], sys.argv[2]
    pa, da = profile(a)
    pb, db = profile(b)
    print(f"A = {Path(a).name}   ({da:.1f}s)")
    print(f"B = {Path(b).name}   ({db:.1f}s)")
    print()
    print(f"{'频段':>12} {'A':>8} {'B':>8} {'A-B':>8}")
    for (lo, hi), x, y in zip(BANDS, pa, pb):
        print(f"{lo:5d}-{hi:5d} {x:8.1f} {y:8.1f} {x - y:8.1f}")
    print()
    def avg(v, lo, hi):
        return float(np.mean([v[i] for i, (a_, b_) in enumerate(BANDS)
                              if a_ >= lo and b_ <= hi]))
    d = avg(pa, 5000, 20000) - avg(pb, 5000, 20000)
    print(f"5k 以上平均差:{d:+.1f} dB")
    if d < -10:
        print("  ⇒ A 的高频明显少。两份是同距离同句的话,问题在**采集路径**。")
    elif d > 10:
        print("  ⇒ A 反而更亮。B 那份可能有自带的处理/AGC。")
    else:
        print("  ⇒ 两份差不多。高频没丢,之前听到的差别是距离或唱法。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
