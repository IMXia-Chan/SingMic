"""音符块编辑器:测音高(YIN)/ 切块 / PSOLA 变调变速 / 渲染 / 重做成品。

纯 numpy,**不碰盘、不读配置、不 import 本项目其它模块** —— 好让它能被单独测。

   * yin(x, sr)          -> (f0, aper, level_db)
   * segment(x, sr)      -> {"sr","dur","key","mode","voiced_s","blocks":[...]}
   * psola(x, sr, alpha, beta)
   * render_blocks(voice, sr, blocks, edits)
   * rebuild_mix(mix, voice_old, voice_new)

★ 两种块:
    note    —— 有声、音高可测。能改音高(PSOLA)、能改长度(PSOLA 变速)。
    sound   —— 气声 / 辅音 / 起音。只能改长度(PSOLA 变速),不能改音高。
    silence —— 静音。只能被拖长时吃掉,自己没内容。
★ 三种块平铺整条时间轴,一个采样都不漏。
"""
from __future__ import annotations

import numpy as np

HOP = 480              # 10ms @ 48k
WIN = 2048             # 42.7ms
MIN_BLOCK_S = 0.06     # 短于这个的块并进邻居
MIN_NEIGHBOR_S = 0.02  # 拖长时,紧邻的下一块至少留这么长

_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_MINOR = (0, 2, 3, 5, 7, 8, 10)


# --------------------------------------------------------------------------
# 音名
# --------------------------------------------------------------------------
def hz_to_midi(f: float) -> float:
    return 69.0 + 12.0 * np.log2(max(float(f), 1e-9) / 440.0)


def note_name(f: float) -> str:
    if not np.isfinite(f) or f <= 0:
        return ""
    m = int(round(hz_to_midi(f)))
    return _NAMES[m % 12] + str(m // 12 - 1)


# --------------------------------------------------------------------------
# YIN
# --------------------------------------------------------------------------
def yin(x, sr, hop: int = HOP, win: int = WIN,
        fmin: float = 65.0, fmax: float = 1200.0, thr: float = 0.15):
    """逐帧 YIN。返回 (f0, aperiodicity, level_db),帧 i 覆盖 [i*hop, i*hop+win)。

    ★ 差分函数  d(tau) = Σ(x[j]-x[j+tau])²  用能量+自相关算:
        d = e0 + et - 2*ac,   e0 = cs[N-tau],  et = cs[N]-cs[tau]
      少减一项能量会让音高**整差一个八度**(这个坑踩过)。
    ★ numpy 的 irfft 自带 1/n,**不要再除 nfft** —— 再除一次 CMND 全变成 ~1.0。
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n < 512:
        return (np.full(1, np.nan), np.ones(1), np.full(1, -120.0))

    win = int(min(win, n))
    win = 1 << int(np.floor(np.log2(win)))
    win = max(win, 512)
    tmax = int(sr / fmin) + 1
    tmax = min(tmax, win - 2)
    # ★ fmax 必须真的把搜索范围截住,不然 2.3kHz 的气声也会被当成「音」
    tmin = max(1, int(sr / fmax))
    nfft = 1 << int(np.ceil(np.log2(win + tmax)))
    taus = np.arange(1, tmax + 1, dtype=np.float64)

    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    nf = len(frames)
    f0 = np.full(nf, np.nan)
    aper = np.ones(nf)
    level = np.full(nf, -120.0)

    CH = 1024
    for s in range(0, nf, CH):
        F = np.asarray(frames[s:s + CH], dtype=np.float64)
        e = np.einsum("ij,ij->i", F, F)
        level[s:s + len(F)] = 10.0 * np.log10(e / win + 1e-24)

        X = np.fft.rfft(F, nfft, axis=1)
        AC = np.fft.irfft(X * np.conj(X), nfft, axis=1)[:, :tmax + 1]

        CS = np.concatenate([np.zeros((len(F), 1)), np.cumsum(F * F, axis=1)], axis=1)
        e0 = CS[:, win - taus.astype(np.int64)]
        et = CS[:, [win]] - CS[:, taus.astype(np.int64)]
        D = np.maximum(e0 + et - 2.0 * AC[:, 1:], 0.0)

        cums = np.cumsum(D, axis=1)
        DN = D * taus / np.maximum(cums, 1e-12)
        if tmin > 1:
            DN[:, :tmin - 1] = 9.0          # 低于 fmax 对应的周期,不参与搜索

        # 第一个「低于阈值的局部极小」
        lm = (DN[:, 1:-1] <= DN[:, :-2]) & (DN[:, 1:-1] <= DN[:, 2:])
        cand = lm & (DN[:, 1:-1] < thr)
        has = cand.any(axis=1)
        first = np.argmax(cand, axis=1) + 1          # DN 里的下标 (= tau)

        argmin = np.argmin(DN, axis=1)
        gmin = DN[np.arange(len(DN)), argmin]
        sel = np.where(has, first, np.where(gmin < 0.60, argmin, -1))

        rows = np.arange(len(F))
        good = sel >= 1
        if not good.any():
            continue
        r = rows[good]
        t = sel[good]
        t = np.clip(t, 1, len(taus) - 2)
        a, b, c = DN[r, t - 1], DN[r, t], DN[r, t + 1]
        den = a - 2.0 * b + c
        shift = np.where(np.abs(den) > 1e-12, 0.5 * (a - c) / np.where(den == 0, 1, den), 0.0)
        shift = np.clip(shift, -1.0, 1.0)
        # ★ t 是 DN 的下标,真正的 tau 还要 +1 —— 差这一位 220Hz 会测成 221Hz
        f0[s + r] = sr / (t + 1 + shift)
        aper[s + r] = b

    return f0, aper, level


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------
def segment(x, sr, hop: int = HOP, win: int = WIN,
            fmin: float = 65.0, fmax: float = 1200.0,
            voiced_aper: float = 0.32, min_block: float = MIN_BLOCK_S):
    """把整条人声切成平铺的块。"""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    dur = n / sr
    f0, aper, level = yin(x, sr, hop, win, fmin, fmax)
    nf = len(f0)

    if nf == 0:
        return {"sr": sr, "dur": dur, "key": "C", "mode": "major", "voiced_s": 0.0,
                "blocks": [{"id": 0, "t0": 0.0, "t1": round(dur, 4), "kind": "silence"}]}

    floor = float(np.percentile(level, 15))
    voiced = np.isfinite(f0) & (aper < voiced_aper) & (level > floor + 8.0)

    # f0 五帧中值 —— 真录音里 66/139/283 Hz 会来回跳(半频/倍频错误),
    # 3 帧压不住,5 帧(50ms)刚好,又不会抹掉真的换音
    f0m = f0.copy()
    for i in np.flatnonzero(np.isfinite(f0)):
        w = f0[max(0, i - 2):i + 3]
        w = w[np.isfinite(w)]
        if len(w):
            f0m[i] = float(np.median(w))

    # 0 静音 / 1 音 / 2 气声辅音
    lab = np.zeros(nf, dtype=np.int8)
    lab[(~voiced) & (level > floor + 6.0)] = 2
    lab[voiced] = 1

    runs = []
    i = 0
    while i < nf:
        j = i + 1
        while j < nf and lab[j] == lab[i]:
            j += 1
        runs.append([i, j, int(lab[i])])
        i = j

    # ★ 短于 70ms 的 run,若左右同类就并进去 —— 不然同一个音会被切成
    #   note / sound / note 一截一截,网页上看着像碎渣
    min_run = max(2, int(round(0.08 * sr / hop)))
    r = 1
    while r < len(runs) - 1:
        a, b, k = runs[r]
        if (b - a) < min_run and runs[r - 1][2] == runs[r + 1][2]:
            runs[r - 1][1] = runs[r + 1][1]
            del runs[r:r + 2]
            r = max(1, r - 1)
        else:
            r += 1

    # 音块里音高跳超过 90 音分 ⇒ 断开。
    # ★ 但要「确认」:接下来 3 帧也得一起偏,单帧的倍频错误不算换音
    pieces = []
    for a, b, k in runs:
        if k != 1:
            pieces.append([a, b, k])
            continue
        start = a
        hist = []
        for i in range(a, b):
            if not np.isfinite(f0m[i]):
                continue
            ci = 1200.0 * np.log2(f0m[i] / 440.0)
            if hist:
                ref = float(np.median(hist[-15:]))
                if abs(ci - ref) > 90.0:
                    nxt = f0m[i:i + 3]
                    nxt = nxt[np.isfinite(nxt)]
                    if len(nxt) >= 2:
                        nc = 1200.0 * np.log2(nxt / 440.0)
                        if abs(float(np.median(nc)) - ref) > 90.0:
                            pieces.append([start, i, 1])
                            start = i
                            hist = []
            hist.append(ci)
        pieces.append([start, b, 1])

    # 帧区间 -> 时间块(以帧中心为界)
    half = win / 2.0
    blocks = []
    for a, b, k in pieces:
        blocks.append({"t0": (a * hop + half) / sr,
                       "t1": (b * hop + half) / sr,
                       "kind": ("silence", "note", "sound")[k]})
    blocks[0]["t0"] = 0.0
    blocks[-1]["t1"] = dur
    for i in range(len(blocks) - 1):
        blocks[i]["t1"] = blocks[i + 1]["t0"]

    # 太短的并进邻居
    merged = []
    for b in blocks:
        if merged and (b["t1"] - b["t0"]) < min_block:
            merged[-1]["t1"] = b["t1"]
        elif merged and (merged[-1]["t1"] - merged[-1]["t0"]) < min_block:
            b["t0"] = merged[-1]["t0"]
            merged[-1] = b
        else:
            merged.append(dict(b))
    blocks = merged

    # 每块的音高;顺便把「不像音」的块降级成 sound
    notes = []
    for blk in blocks:
        if blk["kind"] != "note":
            continue
        i0 = int(max(0, (blk["t0"] * sr - half) / hop))
        i1 = int(min(nf, (blk["t1"] * sr - half) / hop + 1))
        w = f0m[i0:i1]
        w = w[np.isfinite(w) & (w > 0)]
        if len(w) == 0:
            blk["kind"] = "sound"
            continue
        f = float(np.median(w))
        spread = 0.0
        if len(w) >= 4:
            lo, hi = np.percentile(w, [25, 75])
            spread = 1200.0 * np.log2(hi / max(lo, 1e-9))
        # 1kHz 以上 / 块内音高散得太开 —— 是气声或噪声,不是能改的音
        if f > 1000.0 or spread > 250.0:
            blk["kind"] = "sound"
            continue
        blk["f0"] = round(f, 1)
        blk["note"] = note_name(f)
        notes.append((f, blk["t1"] - blk["t0"]))

    # 降级后可能出现相邻的 sound / silence,并回去
    merged2 = []
    for b in blocks:
        if merged2 and b["kind"] == merged2[-1]["kind"] and b["kind"] != "note":
            merged2[-1]["t1"] = b["t1"]
        else:
            merged2.append(dict(b))
    blocks = merged2

    voiced_s = sum(blk["t1"] - blk["t0"] for blk in blocks if blk["kind"] == "note")
    key, mode, conf = detect_key(notes)

    for idx, blk in enumerate(blocks):
        blk["id"] = idx
        blk["t0"] = round(blk["t0"], 4)
        blk["t1"] = round(blk["t1"], 4)

    return {"sr": sr, "dur": round(dur, 4), "key": key, "mode": mode,
            "conf": round(conf, 3), "voiced_s": round(voiced_s, 2),
            "blocks": blocks}


# Krumhansl-Kessler 调性轮廓(公开发表的心理声学研究,不是谁的源代码)
_KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                      2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                      2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _corr(a, b) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-12 else 0.0


def detect_key(notes):
    """notes = [(f0, 时长)] -> (主音名, 大小调, 把握 0..1)。

    ★ 只用「音级在不在音阶里」打分不够:A-C#-E 同时落在 A 大调、C# 小调、F# 小调里,
      会并列、把握永远是 0。改用 KK 轮廓做相关,再给首尾音落在主音上加一点分。
    """
    if not notes:
        return "C", "major", 0.0

    hist = np.zeros(12)
    for f, d in notes:
        pc = int(round(hz_to_midi(f))) % 12
        hist[pc] += max(float(d), 0.01)
    if hist.sum() <= 0:
        return "C", "major", 0.0

    start_pc = int(round(hz_to_midi(notes[0][0]))) % 12
    end_pc = int(round(hz_to_midi(notes[-1][0]))) % 12

    scored = []
    for root in range(12):
        for mode, prof in (("major", _KK_MAJOR), ("minor", _KK_MINOR)):
            sc = _corr(hist, np.roll(prof, root))
            # 调性通常落在首尾 —— 只给一点点分,不压过轮廓本身
            if start_pc == root:
                sc += 0.06
            if end_pc == root:
                sc += 0.06
            scored.append((sc, root, mode))
    scored.sort(reverse=True)
    best, second = scored[0], scored[1]
    conf = max(0.0, min(1.0, (best[0] - second[0]) * 3.0))
    return _NAMES[best[1]], best[2], conf


# --------------------------------------------------------------------------
# PSOLA
# --------------------------------------------------------------------------
def _psola_core(x, sr, alpha: float, f0, hop: int,
                fallback_f0: float, beta_at):
    """PSOLA 的共用内核。beta_at(u) 里 u 是**输入**采样位置,返回这一点的频率比。

    ★ psola / psola_curve 都走这里 —— 两条路必须逐点同一套颗粒逻辑,
      不然「常数曲线」和「直接变调」会给出两份不一样的声音。
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    n_out = max(1, int(round(n * alpha)))
    if n < 64:
        return np.asarray(x, dtype=np.float32)

    if f0 is None:
        f0 = yin(x, sr, hop)[0]  # 只为拿 f0
    f0 = np.asarray(f0, dtype=np.float64)
    idx = np.arange(len(f0), dtype=np.float64) * hop
    valid = np.isfinite(f0) & (f0 > 40.0) & (f0 < 2000.0)
    if valid.sum() >= 2:
        f0s = np.interp(np.arange(n, dtype=np.float64), idx[valid], f0[valid])
    elif valid.sum() == 1:
        f0s = np.full(n, float(f0[valid][0]))
    else:
        f0s = np.full(n, fallback_f0)
    f0s = np.clip(f0s, 50.0, 1200.0)

    # 输入基音标记
    marks = []
    c = 0.0
    while c < n:
        # ★ round() 会把 n-0.4 顶成 n —— 最后一个标记越界,f0s[marks] 会 IndexError
        marks.append(min(int(round(c)), n - 1))
        T = sr / f0s[min(int(c), n - 1)]
        c += max(T, 2.0)
    marks = np.asarray(marks, dtype=np.int64)
    Tm = np.maximum((sr / f0s[marks]).astype(np.int64), 2)

    pad = 2 * int(Tm.max()) + 4
    y = np.zeros(n_out + pad * 2, dtype=np.float64)
    wsum = np.zeros_like(y)
    hann_cache: dict[int, np.ndarray] = {}

    p = 0.0
    while p < n_out:
        u = p / alpha
        k = int(np.searchsorted(marks, u))
        if k <= 0:
            k = 0
        elif k >= len(marks):
            k = len(marks) - 1
        elif abs(marks[k] - u) > abs(marks[k - 1] - u):
            k -= 1
        m = marks[k]
        T = int(Tm[k])
        a = max(m - T, 0)
        b = min(m + T, n)
        grain = x[a:b]
        if len(grain) < 4:
            break
        w = hann_cache.get(len(grain))
        if w is None:
            w = np.hanning(len(grain))
            hann_cache[len(grain)] = w

        pos = int(round(p)) - (m - a) + pad
        lo = max(pos, 0)
        hi = min(pos + len(grain), len(y))
        if hi > lo:
            y[lo:hi] += grain[lo - pos:hi - pos] * w[lo - pos:hi - pos]
            wsum[lo:hi] += w[lo - pos:hi - pos]

        T_out = sr / max(float(beta_at(u)) * f0s[min(int(u), n - 1)], 20.0)
        p += max(T_out, 2.0)

    y = y / np.maximum(wsum, 0.08)
    return np.asarray(y[pad:pad + n_out], dtype=np.float32)


def psola(x, sr, alpha: float = 1.0, beta: float = 1.0,
          f0=None, hop: int = HOP, fallback_f0: float = 160.0):
    """时域 PSOLA:alpha = 输出/输入时长,beta = 频率比(2^(半音/12))。

    每个颗粒都是原波形的一段 —— **共振峰天然不变**,拖几个半音也不会变卡通。
    输出长度 = round(len(x) * alpha)。
    """
    b = float(beta)
    return _psola_core(x, sr, alpha, f0, hop, fallback_f0, lambda u: b)


def psola_curve(x, sr, alpha: float = 1.0, curve=None,
                f0=None, hop: int = HOP, fallback_f0: float = 160.0):
    """按**逐点音分曲线**搬音高。curve = [(t 秒, cents), …],t 相对 x[0]。

    ★ 边界:±50 音分内自然;±200 还不会崩,但颗粒感很明显。
    用来改滑音、压一点颤音;拿它硬造颤音会抖 —— 颤音本来就该保留原样。
    """
    if not curve:
        return psola(x, sr, alpha, 1.0, f0, hop, fallback_f0)
    pts = sorted((float(t), float(c)) for t, c in curve)
    ts = np.asarray([p[0] for p in pts], dtype=np.float64)
    cs = np.asarray([p[1] for p in pts], dtype=np.float64)
    if len(ts) < 2 or ts[-1] <= ts[0]:
        ts = np.array([0.0, 1e9])
        cs = np.array([cs[0], cs[0]])
    else:
        if ts[0] > 0:
            ts = np.concatenate(([0.0], ts))
            cs = np.concatenate(([cs[0]], cs))
        if ts[-1] < 1e9:
            ts = np.concatenate((ts, [1e9]))
            cs = np.concatenate((cs, [cs[-1]]))
    # ★ 曲线只夹在 ±200 音分:再大就是另一个音了,不是「修」
    cs = np.clip(cs, -200.0, 200.0)

    def beta_at(u):
        return 2.0 ** (float(np.interp(u / sr, ts, cs)) / 1200.0)

    return _psola_core(x, sr, alpha, f0, hop, fallback_f0, beta_at)


# --------------------------------------------------------------------------
# 渲染:把编辑应用到整条干声
# --------------------------------------------------------------------------
def _fade(x, sr, s: float = 0.005):
    k = int(min(len(x) // 2, s * sr))
    if k <= 1:
        return x
    w = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
    x = x.copy()
    x[:k] *= w
    x[-k:] *= w[::-1]
    return x


def render_blocks(voice, sr, blocks, edits=None):
    """按块渲染。**没编辑过的块逐点原样拷** —— 一个比特都不变。"""
    voice = np.asarray(voice, dtype=np.float32)
    n = len(voice)
    y = np.zeros(n, dtype=np.float32)
    edits = edits or {}
    min_len = int(MIN_NEIGHBOR_S * sr)
    N = len(blocks)
    cursor = 0

    for i, b in enumerate(blocks):
        bid = b.get("id", i)
        e = edits.get(str(bid)) or edits.get(bid) or {}
        src0 = int(round(b["t0"] * sr))
        src1 = int(round(b["t1"] * sr))
        src0 = min(max(src0, 0), n)
        src1 = min(max(src1, src0), n)
        hard_end = int(round(blocks[i + 1]["t1"] * sr)) if i + 1 < N else n

        deleted = bool(e.get("deleted"))
        if deleted:
            out_len = max(0, src1 - cursor)
        elif e.get("len"):
            want = int(round(float(e["len"]) * sr))
            if i + 1 < N:
                room = hard_end - cursor - min_len
            else:
                room = n - cursor
            out_len = max(min_len, min(want, max(min_len, room)))
        else:
            out_len = src1 - cursor
        out_len = max(0, min(out_len, n - cursor))

        if not deleted and out_len > 0:
            piece = voice[src0:src1]
            semis = float(e.get("semis", 0) or 0) + float(e.get("cents", 0) or 0) / 100.0
            beta = 2.0 ** (semis / 12.0) if b["kind"] == "note" else 1.0
            alpha = out_len / max(1, len(piece))
            changed = abs(alpha - 1.0) > 1e-3 or abs(beta - 1.0) > 1e-4

            if not changed:
                # ★ 别「顺手」把 silence 写成零:原文件那一段是极轻的房间底噪,
                #   写零会让整条文件每个采样都变,听感上也多一道突兀的断层
                row = piece.copy()
            elif b["kind"] == "silence":
                row = np.zeros(out_len, dtype=np.float32)
            else:
                row = psola(piece, sr, alpha=alpha, beta=beta, f0=None)
                row = _fade(row, sr)

            m = min(len(row), n - cursor)
            if m > 0:
                y[cursor:cursor + m] = row[:m]
        cursor += out_len

    # PSOLA 的相位叠加会顶出 1.0 一点点;没编辑的块本来就 <=1,夹一下不影响逐点相同
    return np.clip(y, -1.0, 1.0)


def rebuild_mix(mix, voice_old, voice_new):
    """mix_new = voice_new + (mix_old − voice_old),逐点对齐。

    ★ mix = voice + 伴奏 是逐点成立的(实测残差全 0),所以残差法能精确还原伴奏,
      两条轨天然对齐,不用重新对拍。
    """
    m = np.asarray(mix, dtype=np.float64)
    vo = np.asarray(voice_old, dtype=np.float64)
    vn = np.asarray(voice_new, dtype=np.float64)
    n = len(m)
    if len(vo) < n:
        vo = np.pad(vo, (0, n - len(vo)))
    if len(vn) < n:
        vn = np.pad(vn, (0, n - len(vn)))
    vo, vn = vo[:n], vn[:n]
    if m.ndim == 2:
        base = m - np.stack([vo, vo], axis=1)
        out = base + np.stack([vn, vn], axis=1)
    else:
        out = m - vo + vn
    return np.asarray(np.clip(out, -1.0, 1.0), dtype=np.float32)
