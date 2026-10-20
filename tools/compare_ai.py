"""AI 降噪开 / 关两条录音的对照量具。

★ 只看四件事,别的都是干扰:
  1. 底噪 —— 停顿时还剩多少(p10);
  2. 人声 —— 唱的时候有没有被削(p90 和 3–5k 频段);
  3. 削顶 —— 有没有采样点顶到 ±1;
  4. 断音 —— 有没有 >=5ms 的整段全零(手机那头没送数据)。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\compare_ai.py AI开.wav AI关.wav
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BANDS = ((100, 300), (300, 1000), (1000, 3000), (3000, 5000),
         (5000, 7000), (7000, 10000), (10000, 14000))


def analyse(path: str) -> dict:
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    x = y[:, 0].astype(np.float64)

    # ---- 帧电平(50ms 一帧) ----
    w = int(0.05 * sr)
    n = len(x) // w
    frames = x[:n * w].reshape(n, w)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-12)

    # ---- 削顶 ----
    clips = int(np.sum(np.abs(x) >= 0.999))

    # ---- 断音(>=5ms 连续全零) ----
    z = np.abs(x) < 1e-9
    gaps, i = [], 0
    while i < len(z):
        if z[i]:
            j = i
            while j < len(z) and z[j]:
                j += 1
            if j - i >= int(0.005 * sr):
                gaps.append((i / sr, (j - i) / sr))
            i = j
        else:
            i += 1

    # ---- 频谱:最响的 30 帧 ----
    order = np.argsort(rms)
    loud = frames[order[-min(30, n):]].mean(axis=0)
    S = np.abs(np.fft.rfft(loud * np.hanning(len(loud)))) ** 2
    f = np.fft.rfftfreq(len(loud), 1.0 / sr)

    def band(lo, hi):
        m = (f >= lo) & (f < hi)
        return 10 * np.log10(float(S[m].mean()) + 1e-30)

    ref = band(1000, 3000)
    prof = {f"{lo}-{hi}": band(lo, hi) - ref for lo, hi in BANDS}

    # ---- 尖峰(和录音里「咔」同一把尺子) ----
    d = np.diff(x, prepend=x[0])
    hp = np.diff(d, prepend=d[0]) * 4
    ww = int(0.002 * sr)
    m = len(hp) // ww
    e = np.sqrt(np.mean(hp[:m * ww].reshape(m, ww) ** 2, axis=1))
    med = np.array([np.median(e[max(0, k - 25):k + 26]) for k in range(m)])
    score = e / (med + 1e-9)

    return {
        "sec": len(x) / sr,
        "peak": 20 * np.log10(float(np.max(np.abs(x))) + 1e-12),
        "p10": float(np.percentile(db, 10)),
        "p50": float(np.percentile(db, 50)),
        "p90": float(np.percentile(db, 90)),
        "clips": clips,
        "gaps": gaps,
        "prof": prof,
        "ticks10": int(np.sum(score > 10)),
        "ticks30": int(np.sum(score > 30)),
    }


def show(tag: str, a: dict) -> None:
    print(f"{tag}: {a['sec']:.1f}s  峰值 {a['peak']:.1f} dBFS  "
          f"削顶 {a['clips']} 点")
    print(f"   电平 p10 {a['p10']:6.1f} / p50 {a['p50']:6.1f} / p90 {a['p90']:6.1f} dB")
    print(f"   人声比底噪 {a['p90'] - a['p10']:5.1f} dB")
    print(f"   断音 {len(a['gaps'])} 处" +
          ("" if not a["gaps"] else
           "  " + ", ".join(f"{t:.2f}s×{d * 1000:.0f}ms" for t, d in a["gaps"][:6])))
    print(f"   尖峰 >10 {a['ticks10']} / >30 {a['ticks30']}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    p_on, p_off = sys.argv[1], sys.argv[2]
    on, off = analyse(p_on), analyse(p_off)
    print(f"A(开) = {Path(p_on).name}")
    print(f"B(关) = {Path(p_off).name}\n")
    show("AI 开", on)
    print()
    show("AI 关", off)
    print("\n" + "=" * 58)
    print(f"  底噪(关 − 开):{off['p10'] - on['p10']:+.1f} dB   ← 正数=AI 压下去的量")
    print(f"  人声(开 − 关):{on['p90'] - off['p90']:+.1f} dB   ← 负数=AI 削了人声")
    print(f"  人声比底噪提升:{(on['p90'] - on['p10']) - (off['p90'] - off['p10']):+.1f} dB")
    print("\n  频段(相对 1–3k,单位 dB):")
    print(f"    {'频段':>12} {'开':>8} {'关':>8} {'开-关':>8}")
    for k in on["prof"]:
        print(f"    {k:>12} {on['prof'][k]:8.1f} {off['prof'][k]:8.1f} "
              f"{on['prof'][k] - off['prof'][k]:8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
