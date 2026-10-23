# -*- coding: utf-8 -*-
"""把一条鼓轨变成「音符表」——这是把 AI 那个黑盒挖开一个洞的办法。

两件事分开做,别混在一起:

**一、什么时候有一下(检测)**
三个频段各自算谱通量(spectral flux),各自找峰,再合并:
低频 <150Hz(底鼓)、中频 150–4k(军鼓/牛铃)、高频 >6k(镲)。
必须分开,混成一条算的话低频能量压倒性大,镲永远检不出来。

**二、这一下是什么(分类)**
不靠「哪个频段最响」这种阈值——那套在第一版里把底鼓全判成军鼓,
修完又把三十多个镲漏成九个。改成**模板比对**:拿本机鼓组里每个样本
的前 120ms 算一条对数频谱,和这一下的对数频谱算余弦相似度,谁最像算谁。
鼓组本来就是最后要用来重放的那套,所以"像"就等于"对"。

同一下里可以同时出两个音(底鼓+镲是常事),所以模板过了线的都留下。

用法:
    python tools/drum_transcribe.py 鼓轨.wav --out 重放.wav [--kit kits/phonk]
    python tools/drum_transcribe.py 鼓轨.wav --json 音符.json

★ 只转鼓。贝斯/和弦要 f0 追踪,是另一件事,这里不假装能做。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SR = 44100
N_FFT = 2048
HOP = 220                 # 5ms 一帧
MIN_GAP = 0.045           # 两次击打至少隔 45ms
N_BINS = 32               # 模板用的对数频段数

# GM 打击乐音高号,和 kits/phonk/kit.json 对齐
KICK, SNARE, HATC, HATO, CRASH, COWBELL = 36, 38, 42, 46, 49, 56


# ---------------------------------------------------------------------------
# 基本量
# ---------------------------------------------------------------------------
def load_mono(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1), int(sr)


def stft_mag(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """短时傅里叶幅度谱 (帧 × 频点)。加汉宁窗,首尾补零。"""
    win = np.hanning(n_fft).astype(np.float32)
    n = max(1, 1 + (len(y) - n_fft) // hop)
    pad = np.pad(y, (n_fft // 2, n_fft // 2))
    frames = np.lib.stride_tricks.as_strided(
        pad, shape=(n, n_fft),
        strides=(pad.strides[0] * hop, pad.strides[0]), writeable=False)
    return np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32)


def band_flux(mag: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    """某频段的谱通量(只取正向变化)。"""
    f = np.fft.rfftfreq(mag.shape[1] * 2 - 1, 1 / sr)[: mag.shape[1]]
    sel = (f >= lo) & (f < hi)
    if not sel.any():
        return np.zeros(mag.shape[0], dtype=np.float32)
    m = mag[:, sel]
    d = np.diff(m, axis=0, prepend=m[:1])
    return np.maximum(d, 0).sum(axis=1)


def _nz(x: np.ndarray) -> np.ndarray:
    m = float(x.max())
    return x / m if m > 0 else x


def pick_peaks(env: np.ndarray, sr: int, hop: int, min_gap: float,
               k: float = 1.35, delta: float = 0.02) -> list[int]:
    """局部极大 + 自适应门限(局部中位数)+ 最小间距。返回帧号。"""
    if env.max() <= 0:
        return []
    gap = max(1, int(min_gap * sr / hop))
    w = max(3, int(0.25 * sr / hop)) | 1
    pad = np.pad(env, (w // 2, w // 2), mode="edge")
    med = np.array([np.median(pad[i:i + w]) for i in range(len(env))], dtype=np.float32)
    thr = med * k + delta * float(env.max())
    cand = np.where(env > thr)[0]
    order = cand[np.argsort(env[cand])[::-1]]
    taken: list[int] = []
    for i in order:
        if all(abs(int(i) - j) >= gap for j in taken):
            taken.append(int(i))
    return sorted(taken)


# ---------------------------------------------------------------------------
# 模板:对数频谱,余弦相似度
# ---------------------------------------------------------------------------
def log_spectrum(seg: np.ndarray, sr: int) -> np.ndarray:
    """一段音频 → N_BINS 条对数间隔频带的能量(dB),去均值、单位化。"""
    n = 1 << int(np.ceil(np.log2(max(256, len(seg)))))
    win = np.hanning(len(seg)).astype(np.float32)
    m = np.abs(np.fft.rfft(seg * win, n=n)).astype(np.float64)
    f = np.fft.rfftfreq(n, 1 / sr)
    edges = np.geomspace(40, min(16000, sr / 2 - 100), N_BINS + 1)
    v = np.zeros(N_BINS, dtype=np.float64)
    for b in range(N_BINS):
        sel = (f >= edges[b]) & (f < edges[b + 1])
        v[b] = m[sel].mean() if sel.any() else 0.0
    v = 20 * np.log10(np.maximum(v, 1e-7))
    v -= v.mean()
    nrm = float(np.linalg.norm(v))
    return v / nrm if nrm > 0 else v


def load_kit(kit_dir: Path) -> dict[int, np.ndarray]:
    meta = json.loads((kit_dir / "kit.json").read_text("utf-8"))
    out = {}
    for slot in meta.get("slots", []):
        p = kit_dir / slot["file"]
        if p.exists():
            y, sr = sf.read(str(p), dtype="float32", always_2d=True)
            out[int(slot["midi"])] = y.mean(axis=1)
    return out


def templates(kit: dict[int, np.ndarray], sr_out: int = SR, sec: float = 0.12) -> dict[int, np.ndarray]:
    """每个鼓样本的前 sec 秒 → 对数频谱模板。采样率不同的先重采样。"""
    tpl = {}
    for midi, y in kit.items():
        if y.size == 0:
            continue
        if abs(sr_out - SR) > 1:             # 鼓组是 48k,音频是 44.1k
            g = np.gcd(int(sr_out), int(SR))
            y = resample_poly(y, sr_out // g, SR // g).astype(np.float32)
        seg = y[: int(sec * sr_out)]
        if len(seg) < 64:
            continue
        tpl[midi] = log_spectrum(seg, sr_out)
    return tpl


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def transcribe(path: Path, kit_dir: Path | None = None,
               sec: float = 0.12, score_min: float = 0.55) -> list[dict]:
    y, sr = load_mono(path)
    mag = stft_mag(y, N_FFT, HOP)
    fl = _nz(band_flux(mag, sr, 0, 150))
    fm = _nz(band_flux(mag, sr, 150, 4000))
    fh = _nz(band_flux(mag, sr, 6000, sr / 2))
    env = fl + 0.8 * fm + fh

    idx = set(pick_peaks(env, sr, HOP, MIN_GAP))
    for band in (fl, fh):                    # 弱的那一路单独再找一遍,别被强的一路盖掉
        idx.update(pick_peaks(band, sr, HOP, MIN_GAP, k=1.2, delta=0.015))
    idx = sorted(idx)

    tpl = {}
    if kit_dir is not None:
        tpl = templates(load_kit(Path(kit_dir)), sr)
    notes: list[dict] = []

    for i in idx:
        t = i * HOP / sr
        # 这一下的频谱(取击打后 120ms)
        a = max(0, i * HOP - HOP)
        seg = y[a:a + int(sec * sr)]
        if len(seg) < 64:
            continue
        v = log_spectrum(seg, sr)

        if tpl:
            scored = sorted(((float(np.dot(v, tv)), m) for m, tv in tpl.items()), reverse=True)
            best = scored[0][0]
            # 同一下可以有好几个鼓(底鼓+镲),过线的都留
            hits = [(s, m) for s, m in scored if s >= max(score_min, best - 0.12)]
        else:
            # 没有鼓组时退回频段判断(只当调试用)
            hits = [(max(fl[i], 0.0), KICK)] if fl[i] >= max(fm[i], fh[i]) else (
                [(fh[i], HATC)] if fh[i] >= fm[i] else [(fm[i], SNARE)])

        for score, midi in hits:
            ln = 0.25 if midi in (HATO, CRASH) else 0.10
            vel = float(np.clip(0.45 + 0.55 * float(np.abs(seg).max()), 0.3, 1.0))
            notes.append({"start": round(t, 3), "length": ln, "midi": int(midi),
                          "vel": round(vel, 3), "score": round(score, 3)})
    notes.sort(key=lambda n: (n["start"], n["midi"]))
    return notes


def render(notes: list[dict], kit: dict[int, np.ndarray], sr: int = SR) -> np.ndarray:
    """用本机鼓组把音符表重放出来。"""
    if not notes:
        return np.zeros(sr, dtype=np.float32)
    end = max(n["start"] + n["length"] + 2.0 for n in notes)
    buf = np.zeros(int(end * sr) + sr, dtype=np.float32)
    for n in notes:
        s = kit.get(int(n["midi"]))
        if s is None:
            continue
        i = int(n["start"] * sr)
        if i >= len(buf):
            continue
        v = float(n.get("vel", 0.8)) ** 1.6      # 力度听感更接近平方
        seg = s[: len(buf) - i]
        buf[i:i + len(seg)] += seg * v
    m = float(np.max(np.abs(buf))) if buf.size else 0.0
    if m > 1.0:
        buf /= m
    return buf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--kit", default=str(Path(__file__).resolve().parent.parent / "kits" / "phonk"))
    ap.add_argument("--no-kit", action="store_true", help="不用模板,退回频段判断(调试用)")
    a = ap.parse_args()

    src = Path(a.audio)
    kit_dir = None if a.no_kit else Path(a.kit)
    notes = transcribe(src, kit_dir)

    counts: dict[int, int] = {}
    for n in notes:
        counts[n["midi"]] = counts.get(n["midi"], 0) + 1
    name = {36: "底鼓", 38: "军鼓", 42: "闭镲", 46: "开镲", 49: "长镲", 56: "牛铃"}
    print(f"{src.name}:{len(notes)} 个音 —— "
          + "、".join(f"{name.get(k, k)} {v}" for k, v in sorted(counts.items())))

    if a.json:
        Path(a.json).write_text(json.dumps(notes, ensure_ascii=False, indent=1), "utf-8")
        print("音符表 →", a.json)
    if a.out:
        kit = load_kit(Path(a.kit))
        if not kit:
            print("鼓组没找到,跳过重放")
            return 1
        y = render(notes, kit)
        sf.write(a.out, y, SR, subtype="PCM_16")
        print(f"重放 → {a.out}({len(y)/SR:.1f}s,用了 {len(kit)} 个鼓组槽位)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
