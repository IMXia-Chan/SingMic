"""按「音节核」切块 —— 一个元音核 = 一个音节,中文大致一字一块,英文一词多音节就多块。

为什么不按音高切:一个字/一个音节里常带滑音、颤音,按音高跳变切会把一个字切成好几截;
反过来,两个不同的字如果唱同一个音高,按音高又切不开。

做法(纯 numpy + scipy,不碰盘):
  1. 复用 `tuning.yin()` 的 f0 / 非周期度 / 电平(10ms 一帧)。
  2. 另算 200–3000 Hz 频带能量当「响度」,80ms 平滑压掉颤音;浊音再加一点权重。
  3. 只在有声段里找响度峰(最小间隔 120ms、最小突出度 4dB),一个峰 = 一个音节核。
  4. 两核之间取响度谷底,再向最近的谱通量峰(辅音起音)靠拢。
  5. 静音段单独成块,保证整条时间轴平铺、一个采样都不漏。

★ 块只描述「时间 + 音高」,不生成新音频;变调/变速仍走 tuning 里的 PSOLA。
"""
from __future__ import annotations

import numpy as np


def _find_peaks(a, **kw):
    """scipy 按需导入。

    ★ 冻结成 exe 之后 scipy 是启动最贵的一大块,而切块是用户点了「按字/音节」才走的路,
      开机不该为它付钱 —— 这里只留一个壳,第一次真要用再导入。
    """
    from scipy.signal import find_peaks
    return find_peaks(a, **kw)

import tuning

HOP = tuning.HOP
WIN = tuning.WIN
MIN_SYL_S = 0.06          # 短于这个的块并进邻居
MIN_NUCLEUS_S = 0.12      # 两个音节核至少隔这么远
PROMINENCE_DB = 4.0       # 颤音/气声起伏压不住就不算音节核
SMOOTH_S = 0.08
BAND_LO = 200.0
BAND_HI = 3000.0
BAND_WIN = 1024           # 21ms:够短,字与字之间的短缝不会被长窗糊平
GAP_CLOSE_S = 0.10        # 字内的短暂闭气(塞音)别把音节劈开


def _frame_center(i: int, sr: int) -> float:
    return (i * HOP + WIN / 2.0) / sr


def _runs(mask: np.ndarray) -> list[list[int]]:
    """布尔序列 -> [[start, end), ...]。"""
    if not len(mask):
        return []
    d = np.diff(np.r_[False, mask, False].astype(np.int8))
    idx = np.flatnonzero(d)
    return [[int(a), int(b)] for a, b in zip(idx[::2], idx[1::2])]


def _band_energy_and_flux(x: np.ndarray, sr: int, nf: int):
    """200–3000 Hz 频带能量(dB)与归一化谱通量,帧中心与 `tuning.yin()` 对齐。

    ★ 窗长用 1024 而不是 2048:2048(42.7ms)会把 40ms 的字间短缝糊平,
      整条录音的「静音底」看起来和唱的地方差不多;1024 的短缝能掉到 -120dB。
      为了让帧中心仍落在 `i*HOP + WIN/2`,起点要右移 `(WIN-BAND_WIN)//2`。
    """
    off = (WIN - BAND_WIN) // 2
    xx = x[off:] if off > 0 else x
    frames = np.lib.stride_tricks.sliding_window_view(xx, BAND_WIN)[::HOP]
    n = min(nf, len(frames))
    frames = frames[:n]
    freqs = np.fft.rfftfreq(BAND_WIN, 1.0 / sr)
    sel = (freqs >= BAND_LO) & (freqs <= BAND_HI)
    win = np.hanning(BAND_WIN)
    energy = np.full(n, -120.0)
    flux = np.zeros(n)
    prev = None
    CH = 512
    for s in range(0, n, CH):
        F = np.asarray(frames[s:s + CH], dtype=np.float64) * win
        M = np.abs(np.fft.rfft(F, axis=1))[:, sel]
        energy[s:s + len(M)] = 10.0 * np.log10((M * M).sum(axis=1) + 1e-12)
        if prev is not None:
            flux[s] = float(np.maximum(M[0] - prev, 0.0).sum()
                            / max(1e-9, float(M[0].sum())))
        if len(M) > 1:
            d = np.maximum(M[1:] - M[:-1], 0.0)
            flux[s + 1:s + len(M)] = d.sum(axis=1) / np.maximum(1e-9, M[1:].sum(axis=1))
        prev = M[-1]
    return energy, flux


def _smooth(a: np.ndarray, frames: int) -> np.ndarray:
    frames = max(1, int(frames))
    if frames <= 1 or len(a) < 3:
        return a
    k = np.ones(frames) / frames
    return np.convolve(a, k, mode="same")


def segment_syllables(x, sr: int, hop: int = HOP, win: int = WIN):
    """把整条人声按音节切成平铺的块。

    返回结构与 `tuning.segment()` 兼容,块上多两个字段:
      `f0lo/f0hi` —— 块内浊音 f0 的 10/90 分位(Hz,音节带滑音时用得上)
      `syl`       —— 非静音块的序号(从 1 开始)
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    dur = n / sr
    if n < 512:
        return _single(x, sr, dur)

    f0, aper, level = tuning.yin(x, sr, hop, win)
    nf = len(f0)
    if nf <= 1:
        return _single(x, sr, dur)

    band_db, flux = _band_energy_and_flux(x, sr, nf)
    m = min(len(band_db), nf)
    f0, aper, level = f0[:m], aper[:m], level[:m]
    band_db, flux = band_db[:m], flux[:m]
    nf = m

    # 浊音权重:气声/清辅音的「响度」不该跟元音一样高
    voice_w = np.clip((0.45 - aper) / 0.45, 0.0, 1.0)
    voice_w[~np.isfinite(f0)] = 0.0
    son = _smooth(band_db, SMOOTH_S * sr / hop) + 12.0 * _smooth(voice_w, 0.03 * sr / hop)

    # ★ 用频带能量定「有声」,不用 level:level 是 2048 窗的窄带能量,
    #   连唱 6 个 180ms 的字(缝 40ms)时它几乎是一条直线,p15+6 会把整条都判成静音。
    bfloor = float(np.percentile(band_db, 10))
    bhigh = float(np.percentile(band_db, 95))
    dyn = max(0.0, bhigh - bfloor)
    thr = bfloor + min(10.0, max(3.0, 0.35 * dyn))
    active = band_db > thr
    voiced = active & np.isfinite(f0) & (aper < 0.32)

    # 字内短暂闭气/爆破:把 <100ms 的静音缝补上,别把一个字劈成两半
    gap = max(1, int(round(GAP_CLOSE_S * sr / hop)))
    runs = _runs(active)
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    runs = merged

    min_frames = max(2, int(round(MIN_SYL_S * sr / hop)))
    min_dist = max(1, int(round(MIN_NUCLEUS_S * sr / hop)))

    segs: list[list[int]] = []          # [start, end, is_active]
    prev_end = 0
    for a, b in runs:
        if a > prev_end:
            segs.append([prev_end, a, 0])
        segs.append([a, b, 1])
        prev_end = b
    if prev_end < nf:
        segs.append([prev_end, nf, 0])

    # 每个有声段里找音节核,再按谷底 + 谱通量切
    pieces: list[list[int]] = []        # [start, end, is_active]
    for a, b, is_act in segs:
        if not is_act:
            pieces.append([a, b, 0])
            continue
        sub = son[a:b].copy()
        ok = voiced[a:b]
        if not ok.any():
            pieces.append([a, b, 1])
            continue
        sub[~ok] = -np.inf
        peaks, _ = _find_peaks(sub, distance=min_dist, prominence=PROMINENCE_DB)
        peaks = peaks + a
        if not len(peaks):
            # 有声但找不到峰(很短的轻声字):整段当一个音节核
            vv = np.flatnonzero(ok)
            peaks = np.array([a + int(vv[np.argmax(sub[vv])])])

        bounds = [a]
        for p, q in zip(peaks[:-1], peaks[1:]):
            if q - p <= 1:
                continue
            seg = son[p + 1:q]
            v = p + 1 + int(np.argmin(seg))
            # 谷底附近找谱通量最大的那一帧 = 辅音起音
            lo = max(p + 1, v - 4)
            hi = min(q, v + 5)
            if hi > lo:
                fb = lo + int(np.argmax(flux[lo:hi]))
                fmed = float(np.median(flux[a:b]))
                if flux[fb] > fmed + 1e-6:
                    v = fb
            if v > bounds[-1]:
                bounds.append(v)
        bounds.append(b)
        for i in range(len(bounds) - 1):
            if bounds[i + 1] > bounds[i]:
                pieces.append([bounds[i], bounds[i + 1], 1])

    # 太短的块并进邻居
    merged2: list[list[int]] = []
    for p in pieces:
        if merged2 and p[1] - p[0] < min_frames:
            merged2[-1][1] = p[1]
        elif merged2 and merged2[-1][1] - merged2[-1][0] < min_frames:
            p[0] = merged2[-1][0]
            merged2[-1] = p
        else:
            merged2.append(p)
    pieces = merged2

    # 帧区间 -> 时间块 + 分类
    half = WIN / 2.0
    blocks = []
    syl = 0
    notes = []
    for a, b, is_act in pieces:
        t0 = 0.0 if a == 0 else (a * hop + half) / sr
        t1 = dur if b >= nf else (b * hop + half) / sr
        if t1 <= t0:
            continue
        kind = "silence"
        blk = {"t0": t0, "t1": t1, "kind": kind}
        if is_act:
            v = voiced[a:b]
            fs = f0[a:b]
            fs = fs[np.isfinite(fs) & (fs > 0)]
            vfrac = float(v.mean()) if len(v) else 0.0
            if vfrac >= 0.35 and len(fs) >= 2:
                med = float(np.median(fs))
                if 60.0 <= med <= 1000.0:
                    blk["kind"] = "note"
                    blk["f0"] = round(med, 1)
                    blk["note"] = tuning.note_name(med)
                    # ★ YIN 会偶发倍频/半频错判(220Hz 的「啊」偶尔出 1185Hz);
                    #   音域只统计中位数上下一个八度以内的帧,不然 f0hi 会被噪声顶到 1200。
                    near = fs[(fs > med * 0.5) & (fs < med * 2.0)]
                    if not len(near):
                        near = fs
                    if len(near) >= 5:
                        lo, hi = np.percentile(near, [10, 90])
                    else:
                        lo, hi = float(near.min()), float(near.max())
                    blk["f0lo"] = round(float(lo), 1)
                    blk["f0hi"] = round(float(hi), 1)
                    notes.append((med, t1 - t0))
            if blk["kind"] != "note":
                blk["kind"] = "sound"
        if blk["kind"] != "silence":
            syl += 1
            blk["syl"] = syl
        blocks.append(blk)

    if not blocks:
        return _single(x, sr, dur)

    # 相邻同类(静音/气声)并回去
    merged3 = []
    for b in blocks:
        if (merged3 and b["kind"] == merged3[-1]["kind"]
                and b["kind"] in ("silence", "sound")):
            merged3[-1]["t1"] = b["t1"]
        else:
            merged3.append(b)
    blocks = merged3

    voiced_s = sum(b["t1"] - b["t0"] for b in blocks if b["kind"] == "note")
    key, mode, conf = tuning.detect_key(notes)

    for idx, b in enumerate(blocks):
        b["id"] = idx
        b["t0"] = round(float(b["t0"]), 4)
        b["t1"] = round(float(b["t1"]), 4)
    return {"sr": sr, "dur": round(dur, 4), "key": key, "mode": mode,
            "conf": round(conf, 3), "voiced_s": round(voiced_s, 2),
            "blocks": blocks}


def _single(x: np.ndarray, sr: int, dur: float) -> dict:
    """太短/没帧可算:整条一块。"""
    rms = float(np.sqrt(np.mean(np.square(x))) if len(x) else 0.0)
    kind = "note" if rms > 1e-4 else "silence"
    blk = {"id": 0, "t0": 0.0, "t1": round(dur, 4), "kind": kind}
    if kind != "silence":
        blk["syl"] = 1
    return {"sr": sr, "dur": round(dur, 4), "key": "C", "mode": "major",
            "conf": 0.0, "voiced_s": round(dur if kind == "note" else 0.0, 2),
            "blocks": [blk]}
