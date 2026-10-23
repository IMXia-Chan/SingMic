# -*- coding: utf-8 -*-
"""用生成好的鼓组渲染一段 phonk 试听 —— 只为"先听见",不参与正式导出。

它和 Studio 走的是同一套素材(kits/<id>/*.wav),所以这里听着对,
装进 Studio 就也是那个声音。

用法:
  python tools/kit_demo.py                      # → Desktop/phonk试听.wav
  python tools/kit_demo.py --bpm 150 --out x.wav
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

KIT = Path(__file__).resolve().parent.parent / "kits" / "phonk"
SR = 48000

# 两小节。每小节 16 格(十六分音符)。
KICK = [0, 6, 8, 14] + [16, 22, 24, 30]
SNARE = [4, 12] + [20, 28]
HAT8 = [i for i in range(0, 32, 2)]
HAT_ROLL = [30, 31]                      # 小节末的十六分滚奏
# 牛铃旋律:(格, midi)。A 小调,phonk 最爱的那几个音
COWBELL = [(0, 69), (3, 69), (6, 72), (8, 69), (11, 67), (14, 69),
           (16, 69), (19, 69), (22, 72), (24, 76), (27, 74), (30, 69)]
# 808 低音:(格, midi, 长度格)
SUB = [(0, 33, 6), (8, 33, 6), (16, 33, 6), (24, 29, 6)]


def load(name: str) -> np.ndarray:
    y, sr = sf.read(KIT / name, dtype="float32", always_2d=False)
    assert sr == SR, f"{name} 采样率是 {sr},应该是 {SR}"
    return np.ascontiguousarray(y)


def place(buf: np.ndarray, y: np.ndarray, at: int, gain: float = 1.0) -> None:
    if at >= len(buf):
        return
    n = min(len(y), len(buf) - at)
    buf[at:at + n] += y[:n] * gain


def shift(y: np.ndarray, semis: float) -> np.ndarray:
    """按半音变调。升降一个八度以内用有理数重采样就够。"""
    if abs(semis) < 1e-6:
        return y
    # 12 个半音的比值里,常用的几个凑成小整数,避免长数组
    tbl = {0: (1, 1), 2: (9, 8), 3: (6, 5), 4: (5, 4), 5: (4, 3), 7: (3, 2),
           9: (8, 5), 10: (5, 3), 11: (16, 9), 12: (2, 1), 14: (16, 6),
           -2: (8, 9), -3: (5, 6), -4: (4, 5), -5: (3, 4), -7: (2, 3),
           -9: (5, 8), -10: (3, 5), -11: (9, 16), -12: (1, 2), -14: (6, 16)}
    s = int(round(semis))
    up, dn = tbl.get(s, (int(round(2 ** (s / 12) * 64)), 64))
    return resample_poly(y, up, dn).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=145.0)
    ap.add_argument("--bars", type=int, default=4)
    ap.add_argument("--drums-only", action="store_true", help="不放牛铃和 808,只听鼓")
    ap.add_argument("--clean", dest="dirty", action="store_false", help="不做总线削波/低通")
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "phonk试听.wav"))
    a = ap.parse_args()

    step = 60.0 / a.bpm / 4.0               # 一格 = 十六分音符
    n_step = int(round(step * SR))
    total = int(n_step * 16 * a.bars) + int(2 * SR)
    buf = np.zeros(total, dtype=np.float32)

    kit = {s["file"]: s for s in json.loads((KIT / "kit.json").read_text(encoding="utf-8"))["slots"]}
    kick, snare, clap = load("kick.wav"), load("snare.wav"), load("clap.wav")
    hatc, hato = load("hatc.wav"), load("hato.wav")
    crash, cbow = load("crash.wav"), load("cowbell.wav")

    def at(g: int) -> int:
        return g * n_step

    for bar in range(a.bars):
        b = bar * 32
        for g in KICK:
            place(buf, kick, at(g + b), 0.58)
        for g in SNARE:
            place(buf, snare, at(g + b), 0.78)
        for g in SNARE:
            place(buf, clap, at(g + b), 0.60)
        for g in HAT8:
            place(buf, hatc, at(g + b), 0.58 if g % 4 else 0.75)
        for g in HAT_ROLL:
            place(buf, hatc, at(g + b), 0.50)
        if not a.drums_only:
            for g, m in COWBELL:
                place(buf, shift(cbow, m - 56), at(g + b), 0.85)
            for g, m, ln in SUB:
                seg = shift(load("808.wav"), m - 33)
                seg = seg[: int(ln * n_step)]
                # 808 的收尾:最后 30 ms 淡出,不然切开会"啪"
                k = min(len(seg), int(0.03 * SR))
                if k:
                    seg = seg.copy()
                    seg[-k:] *= np.linspace(1, 0, k)
                place(buf, seg, at(g + b), 0.48)
        place(buf, crash, at(b), 0.30)

    # 侧链:kick 一响,其余的让路(phonk 的"抽吸"就是它)
    duck = np.ones(total, dtype=np.float32)
    for bar in range(a.bars):
        for g in KICK:
            i = at(g + bar * 32)
            k = min(total - i, int(0.18 * SR))
            if k > 0:
                duck[i:i + k] *= np.linspace(1.0, 0.35, k) ** 0.5
    buf *= duck

    # ---- 总线处理:phonk 的"脏"全在这一段
    # ① 软削波(把瞬态压平,听起来更响更脏)② 低通到 11 kHz(磁带味)
    buf = np.tanh(buf * (1.6 if a.dirty else 1.0))
    if a.dirty:
        from scipy.signal import butter, sosfilt
        buf = sosfilt(butter(2, 11000 / (SR / 2), "lowpass", output="sos"), buf)

    m = float(np.max(np.abs(buf))) or 1.0
    buf = (buf / m * 0.89).astype(np.float32)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, buf, SR, subtype="PCM_16")
    print(f"{a.bpm:g} BPM / {a.bars} 小节 / {len(buf)/SR:.2f}s → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
