# -*- coding: utf-8 -*-
"""生成鼓组采样 —— **全部由合成器算出来,不下载任何现成音色**。

这样做的理由不是省事,是许可证:
  任何从网上下载的鼓采样,哪怕标着"免费",都可能禁止再分发,
  而这个项目是公开发布、要进 git 的。自己算出来的波形没有这个问题。

音色照的是 808 那一套做法(phonk / trap / 早期电音都在用):
  · 底鼓、军鼓、拍手 = 正弦 + 噪声 + 包络
  · 镲片、牛铃      = 6 个方波叠加再做高通(这是 808 真正的做法,
                      用白噪声做出来的镲是"撒沙",不是镲)

用法:
  python tools/make_kit.py                 # 写到 kits/phonk/
  python tools/make_kit.py --out D:/x/kit  # 换个地方

输出同时写一份 kit.json,前端和导出都读它。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

SR = 48000
RNG = np.random.default_rng(20261021)      # 固定种子:每次生成的文件字节一致


# ---------------------------------------------------------------- 小工具
def t_of(x: np.ndarray) -> np.ndarray:
    return np.arange(len(x), dtype=np.float64) / SR


def decay(n: int, tau: float) -> np.ndarray:
    """指数衰减包络。tau 是衰减到 1/e 的时间(秒)。"""
    return np.exp(-np.arange(n, dtype=np.float64) / (max(tau, 1e-4) * SR))


def hp(x: np.ndarray, f: float, order: int = 4) -> np.ndarray:
    return sosfilt(butter(order, f / (SR / 2), "highpass", output="sos"), x)


def bp(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, [lo / (SR / 2), min(hi, SR / 2 - 100) / (SR / 2)],
                          "bandpass", output="sos"), x)


def metal(n: int) -> np.ndarray:
    """808 的"金属"振荡器:6 个固定频率的方波叠加。

    ★ 这 6 个频率是 808 电路里的实际分频比,不是随便选的 ——
      换成别的数字,镲片就散了。"""
    t = np.arange(n, dtype=np.float64) / SR
    s = np.zeros(n)
    for f in (205.3, 304.4, 369.6, 522.7, 540.0, 800.0):
        s += np.sign(np.sin(2 * np.pi * f * t))
    return s / 6.0


def finish(y: np.ndarray, peak: float = 0.92, fade_out: float = 0.004) -> np.ndarray:
    """归一 + 尾部淡出 + 截掉尾巴静音。

    ★ 淡入只有 0.4 ms —— 打击乐的**起振就是它本身**,淡入长了会把瞬态削平
      (第一版用 4 ms,镲片的峰值被压到 0.25,听起来是"沙"不是"镲")。"""
    y = np.asarray(y, dtype=np.float64)
    ki = min(max(1, int(0.0004 * SR)), len(y) // 8) if len(y) > 8 else 0
    ko = min(max(1, int(fade_out * SR)), len(y) // 4) if len(y) > 8 else 0
    if ki > 0:
        y[:ki] *= np.linspace(0, 1, ki)
    if ko > 0:
        y[-ko:] *= np.linspace(1, 0, ko)
    # 掐掉末尾低于 -70 dB 的部分
    idx = np.where(np.abs(y) > 10 ** (-70 / 20))[0]
    if idx.size:
        y = y[: idx[-1] + 1]
    # ★ 归一放在最后:淡入会削掉起振那一下,先归一再淡入,峰值就对不上标称值了
    m = float(np.max(np.abs(y))) if y.size else 0.0
    if m > 0:
        y = y / m * peak
    return y.astype(np.float32)


# ---------------------------------------------------------------- 鼓件
def kick() -> np.ndarray:
    """808 底鼓:一个从 180 Hz 掉到 55 Hz 的正弦,带 2 ms 的点击声。"""
    n = int(0.42 * SR)
    t = t_of(np.zeros(n))
    f = 55.0 + 125.0 * np.exp(-t / 0.020)          # 音高包络
    ph = 2 * np.pi * np.cumsum(f) / SR
    # ★ 衰减从 0.13 收到 0.085:底鼓拖太久会和 808 低音糊成一团,
    #   phonk 的底鼓是"一下",不是"一坨"
    body = np.sin(ph) * decay(n, 0.085)
    click = RNG.normal(0, 1, n) * decay(n, 0.0012) * 0.55
    y = body + click
    # ★ 饱和给到 3.5:干净的牛铃声正弦放进 phonk 里是不合格的,
    #   要能听见"方"了的那一面
    return finish(np.tanh(y * 3.5) / np.tanh(3.5))


def snare() -> np.ndarray:
    """军鼓:185 Hz 的鼓皮音 + 高通噪声,噪声衰减比鼓皮慢。"""
    n = int(0.34 * SR)
    t = t_of(np.zeros(n))
    # 鼓皮音:185 Hz 的基调 + 一处 220 Hz 的"体",只有噪声的军鼓是空的
    tone = (np.sin(2 * np.pi * 185.0 * t) * 0.55
            + np.sin(2 * np.pi * 220.0 * t) * decay(n, 0.040) * 0.30)
    tone *= decay(n, 0.060)
    noise = hp(RNG.normal(0, 1, n), 1200) * decay(n, 0.100)
    y = tone + noise * 0.90
    return finish(np.tanh(y * 2.2) / np.tanh(2.2))


def clap() -> np.ndarray:
    """拍手:3 次相隔 10 ms 的噪声 + 一条长尾巴,再带通成"啪"。"""
    n = int(0.38 * SR)
    y = np.zeros(n)
    for i, d in enumerate((0.000, 0.010, 0.021, 0.032)):
        k = int(d * SR)
        seg = RNG.normal(0, 1, n - k) * decay(n - k, 0.009 if i < 3 else 0.085)
        y[k:] += seg * (1.0 if i < 3 else 0.8)
    return finish(bp(y, 900, 4200))


def hat(open_: bool) -> np.ndarray:
    """闭合/开放镲:808 的金属振荡器 + 高通。"""
    n = int((0.42 if open_ else 0.09) * SR)
    y = hp(metal(n), 6800) * decay(n, 0.24 if open_ else 0.022)
    # ★ 峰值给到 0.82:镲的能量几乎全在 7 kHz 以上,按"感官响度"归一的话
    #   它比底鼓低 16 dB,进混音等于没响。这里按峰值给足,音量交给力度格。
    return finish(y, peak=0.82)


def crash() -> np.ndarray:
    """长镲:金属振荡器 + 一点噪声,衰减一秒。"""
    n = int(1.60 * SR)
    y = hp(metal(n), 3800) * decay(n, 0.55)
    y += hp(RNG.normal(0, 1, n), 6200) * decay(n, 0.42) * 0.5
    return finish(y, peak=0.7)


def cowbell() -> np.ndarray:
    """808 牛铃 —— phonk 的主旋律就是它。

    ★ 两个方波 540 / 800 Hz,不是正弦。用正弦出来是"叮",不是牛铃。"""
    n = int(0.40 * SR)
    t = t_of(np.zeros(n))
    y = (np.sign(np.sin(2 * np.pi * 540.0 * t))
         + np.sign(np.sin(2 * np.pi * 800.0 * t))) * 0.5
    y = bp(y, 420, 3400) * decay(n, 0.115)
    return finish(y, peak=0.8)


def sub808() -> np.ndarray:
    """808 低音:41 Hz(E1)加一点二次谐波。放进采样轨能变调,但不会滑音 ——
    滑音要等合成器,这是后面的事。"""
    n = int(2.00 * SR)
    t = t_of(np.zeros(n))
    y = (np.sin(2 * np.pi * 41.20 * t) * 0.85
         + np.sin(2 * np.pi * 82.41 * t) * 0.15) * decay(n, 0.55)
    return finish(np.tanh(y * 1.4) / np.tanh(1.4))


# ---------------------------------------------------------------- 组装
# midi 用通用鼓组的编号(GM):以后换真采样、接 SoundFont 都不用改工程数据
KIT = [
    (36, "kick",   "底鼓", kick),
    (38, "snare",  "军鼓", snare),
    (39, "clap",   "拍手", clap),
    (42, "hatc",   "闭镲", lambda: hat(False)),
    (46, "hato",   "开镲", lambda: hat(True)),
    (49, "crash",  "长镲", crash),
    (56, "cowbell", "牛铃", cowbell),
]

# 不属于鼓组、但一起生成给采样轨用的
EXTRA = [("808", sub808, "808 低音")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "kits" / "phonk"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    slots = []
    for midi, key, label, fn in KIT:
        y = fn()
        sf.write(out / f"{key}.wav", y, SR, subtype="PCM_16")
        slots.append({"midi": midi, "file": f"{key}.wav", "label": label,
                      "sec": round(len(y) / SR, 4)})
        print(f"  {key:8s} {label:4s} midi={midi:3d}  {len(y)/SR:.3f}s  peak={np.max(np.abs(y)):.2f}")

    for key, fn, label in EXTRA:
        y = fn()
        sf.write(out / f"{key}.wav", y, SR, subtype="PCM_16")
        print(f"  {key:8s} {label:4s} (采样源)  {len(y)/SR:.3f}s")

    meta = {
        "id": "phonk",
        "name": "808 / Phonk",
        "note": "全部由 tools/make_kit.py 合成,无第三方采样,可自由分发",
        "sr": SR,
        "slots": slots,
    }
    (out / "kit.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写好 {len(slots)} 个鼓件 + {len(EXTRA)} 个采样源 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
