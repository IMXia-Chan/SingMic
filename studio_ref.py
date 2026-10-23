"""按原唱矫正:拿**原唱人声**当参照,把你的音高往它上面收。

★ 这里的「AI」和 `studio_auto.py` 一样,是本机 numpy 测量 + 规则,不是神经网络。
  不上传、不联网;原唱和你的人声都只在这台机器上算。
★ 它做的事只有一件:**每一刻,原唱唱多高,你就往那个高度收**。
  颤音不动(曲线是平滑过的),换气/辅音不动,时间轴不动(除非你勾「整体前移」)。

用法:
    analyze(project, ref_track_id, take_track_id, ...) -> dict   # 只出方案,不写工程

术语:
    offset = 你的时间 − 原唱的时间。offset = +0.30 表示**你比原唱晚 0.3 秒**。
    intonation 模式(默认):只修「相对音准」,你整体换了个调唱也照样修;
    match 模式:连整体音高一起对到原唱(要求同一个调/同一个八度)。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

import studio_mix as sm
import studio_refdb as rdb
import tuning

SR = sm.SR
GRID = 0.02                 # 矫正曲线的网格(秒)
VOICED_APER = 0.32
CURVE_MAX_CENTS = 200.0     # ★ psola_curve 的硬上限就是 ±200 音分
MIN_CORRECT_CENTS = 2.0     # 小于这个听不出来,不写
MATCH_MAX_RAW = 350.0       # 偏差超过这个,八成不是「跑调」而是换了音/测错,不硬拽

LIMITS = [
    "原唱和你的人声都只在**这台机器**上算;不上传、不联网。",
    "它只改音高,不改时间、不改音色、不改颤音。",
    "「AI」= 本机 YIN 测音高 + 一道带上下限的规则,不是神经网络。",
    "歌库只存每首歌的「音高地图」(约 100~200KB),不复制音频;同一首歌第二次分析秒开。",
    "应用后按一次 Ctrl+Z 可以全部撤销。",
]


class RefError(Exception):
    """给用户看的错(接口直接回 400)。"""


MSG_DEMUCS = ("没装 demucs。装法:`python -m pip install demucs`"
              "(会拉 PyTorch,约 1~2GB;CPU 上一首 3 分钟的歌大概 5~20 分钟)")


# --------------------------------------------------------------------------
# 分析缓存:每条源只测一次
# --------------------------------------------------------------------------
def _cache_key(path: Path) -> str:
    st = path.stat()
    # ★ 用纳秒 mtime:同一秒内覆盖同名同大小的文件(合成测试/重新导入)不会命中旧缓存
    raw = f"{path}|{st.st_size}:{st.st_mtime_ns}|ref-v1|hop{tuning.HOP}/{tuning.WIN}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def analyse_source(path: Path, role: str = "take", name: str | None = None) -> dict:
    """整条源的音高轨迹 + 音符块。

    ★ 三级缓存,先快后慢:
      1. `projects/refdb/`(按**内容**认歌,改名也一样命中) —— 秒开
      2. `projects/cache/ref_<key>.npz`(按路径+大小+时间) —— 秒开
      3. 真的跑 YIN(两遍)—— 3 分钟的歌大约 2~5 秒
    """
    path = Path(path).resolve()
    if not path.exists():
        raise RefError(f"找不到音频文件:{path.name}")

    hit = None
    try:
        hit = rdb.get(path)
    except Exception:                     # noqa: BLE001
        hit = None
    if hit is not None:
        return hit

    key = _cache_key(path)
    npz = sm.CACHE / f"ref_{key}.npz"
    js = sm.CACHE / f"ref_{key}.json"
    if npz.exists() and js.exists():
        try:
            z = np.load(npz)
            d = json.loads(js.read_text(encoding="utf-8"))
            d["cents"] = z["cents"].astype(np.float64)
            d["times"] = z["times"].astype(np.float64)
            d["voiced"] = z["voiced"].astype(bool)
            d["cached"] = True
            _learn(path, d, role, name)
            return d
        except Exception:                   # noqa: BLE001
            pass

    x, sr = sm.load_mono(path)
    f0, aper, level = tuning.yin(x, sr)
    hop_s = tuning.HOP / float(sr)
    times = (np.arange(len(f0), dtype=np.float64) * tuning.HOP
             + tuning.WIN / 2.0) / float(sr)
    times = np.clip(times, 0.0, len(x) / float(sr))

    floor = float(np.percentile(level, 15)) if len(level) else -120.0
    voiced = np.isfinite(f0) & (aper < VOICED_APER) & (level > floor + 8.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        cents = 1200.0 * np.log2(np.where(f0 > 0, f0, np.nan) / 440.0)
    cents = np.where(voiced, cents, np.nan)

    seg = tuning.segment(x, sr)
    blocks = [{"t0": round(float(b["t0"]), 4), "t1": round(float(b["t1"]), 4),
               "kind": b.get("kind"), "f0": b.get("f0"), "note": b.get("note")}
              for b in (seg.get("blocks") or [])]

    d = {"path": str(path), "sr": int(sr), "dur": len(x) / float(sr),
         "hop": tuning.HOP, "win": tuning.WIN, "hop_s": hop_s,
         "key": seg.get("key"), "mode": seg.get("mode"),
         "voiced_s": seg.get("voiced_s"), "blocks": blocks, "cached": False}
    try:
        sm.CACHE.mkdir(parents=True, exist_ok=True)
        tmp = npz.with_name(npz.name + ".part.npz")
        np.savez_compressed(tmp, cents=cents.astype(np.float32),
                            times=times.astype(np.float32),
                            voiced=voiced)
        tmp.replace(npz)
        jtmp = js.with_name(js.name + ".part")
        jtmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        jtmp.replace(js)
    except OSError:
        pass
    d["cents"] = cents
    d["times"] = times
    d["voiced"] = voiced
    _learn(path, d, role, name)
    return d


def _learn(path: Path, d: dict, role: str, name: str | None) -> None:
    """把这次分析存进知识库。★ 存不下来不该让整次分析失败。"""
    try:
        r = rdb.put(path, d, role=role, name=name or path.stem)
        d["refdb"] = r
    except Exception:                     # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 工程里的轨 / 片段
# --------------------------------------------------------------------------
def _find_track(project: dict, tid) -> dict:
    for tr in project.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    raise RefError(f"工程里没有这条轨:{tid}")


def _clip_audio(project: dict, clip: dict) -> tuple[Path, dict] | None:
    sid = clip.get("sourceId")
    src = (project.get("sources") or {}).get(sid)
    if not src or not src.get("path"):
        return None
    path = (sm.ROOT / src["path"]).resolve()
    try:
        path.relative_to(sm.ROOT)
    except ValueError:
        return None
    if not path.exists():
        return None
    return path, src


def _usable_clips(project: dict, tr: dict) -> list[dict]:
    out = []
    for c in tr.get("clips") or []:
        if float(c.get("length") or 0) <= 0:
            continue
        if _clip_audio(project, c) is None:
            continue
        out.append(c)
    out.sort(key=lambda c: float(c.get("start") or 0))
    return out


def _src_to_time(clip: dict, ts: float) -> float:
    """源内时间 → 时间轴时间(考虑伸缩)。"""
    start = float(clip.get("start") or 0)
    length = max(1e-9, float(clip.get("length") or 0))
    si = float(clip.get("srcIn") or 0)
    so = float(clip.get("srcOut") or 0)
    d = max(1e-9, so - si)
    return start + (ts - si) * length / d


def _time_to_src(clip: dict, t: float) -> float:
    start = float(clip.get("start") or 0)
    length = max(1e-9, float(clip.get("length") or 0))
    si = float(clip.get("srcIn") or 0)
    so = float(clip.get("srcOut") or 0)
    return si + (t - start) * (so - si) / length


def _sample(cents: np.ndarray, times: np.ndarray, t: np.ndarray) -> np.ndarray:
    """在 times 上最近邻取 cents;越界/无声 → NaN。"""
    if len(times) == 0:
        return np.full(np.shape(t), np.nan)
    idx = np.searchsorted(times, t)
    idx = np.clip(idx, 0, len(times) - 1)
    lo = np.clip(idx - 1, 0, len(times) - 1)
    pick = np.where(np.abs(times[lo] - t) <= np.abs(times[idx] - t), lo, idx)
    return cents[pick]


# --------------------------------------------------------------------------
# 对轴:用「唱的是哪个音」的 12 维色度做互相关
# --------------------------------------------------------------------------
def _timeline_chroma(project: dict, tr: dict, infos: dict, hop: float = 0.05) -> np.ndarray:
    clips = _usable_clips(project, tr)
    if not clips:
        return np.zeros((0, 12), dtype=np.float32)
    end = max(float(c.get("start") or 0) + float(c.get("length") or 0) for c in clips)
    n = max(1, int(math.ceil(end / hop)) + 1)
    C = np.zeros((n, 12), dtype=np.float32)
    for c in clips:
        got = _clip_audio(project, c)
        if not got:
            continue
        path, _ = got
        info = infos.get(str(path))
        if info is None:
            try:
                info = analyse_source(path)
            except RefError:
                continue
            infos[str(path)] = info
        start = float(c.get("start") or 0)
        length = float(c.get("length") or 0)
        k0 = max(0, int(math.floor(start / hop)))
        k1 = min(n, int(math.ceil((start + length) / hop)))
        if k1 <= k0:
            continue
        tt = np.arange(k0, k1) * hop
        ss = _time_to_src(c, tt)
        cc = _sample(info["cents"], info["times"], ss)
        ok = np.isfinite(cc)
        if not ok.any():
            continue
        pc = np.mod(cc[ok], 1200.0) / 100.0
        b = np.mod(np.round(pc).astype(np.int64), 12)
        rows = np.arange(k0, k1)[ok]
        np.add.at(C, (rows, b), 1.0)
    # 行归一化,免得响的一段把整条相关压死
    nrm = np.linalg.norm(C, axis=1, keepdims=True)
    return (C / np.maximum(nrm, 1e-6)).astype(np.float32)


def estimate_offset(take_chroma: np.ndarray, ref_chroma: np.ndarray,
                    hop: float = 0.05, max_sec: float = 8.0) -> tuple[float, float]:
    """返回 (offset 秒, 把握)。offset = 你的时间 − 原唱的时间。"""
    nT, nR = len(take_chroma), len(ref_chroma)
    if nT < 8 or nR < 8:
        return 0.0, 0.0
    L = nT + nR - 1
    nfft = 1 << int(math.ceil(math.log2(max(2, L))))
    # ★ 12 个半音一起试:他整首换了调唱也照样对得上。
    #   旋转只是把色度那 12 个格子转一下,频谱可以复用。
    A = [np.fft.rfft(take_chroma[:, d].astype(np.float64), nfft) for d in range(12)]
    B = [np.fft.rfft(ref_chroma[::-1, d].astype(np.float64), nfft) for d in range(12)]

    lags = np.arange(L) - (nR - 1)              # 单位:帧
    secs = lags * hop
    keep = np.abs(secs) <= max_sec
    if not keep.any():
        return 0.0, 0.0
    sec = secs[keep]
    # ★ 轻微偏好小偏移:一段人声通常就摆在原唱附近;
    #   结构相似的段落会把远处也顶出峰来,没有这个先验会挑错。
    w = np.exp(-np.abs(sec) / 6.0)

    best = None
    for rot in range(12):                        # ★ 12 个半音都试,换调也照样对得上
        r = np.zeros(L, dtype=np.float64)
        for d in range(12):
            r += np.fft.irfft(A[(d - rot) % 12] * B[d], nfft)[:L]
        s = r[keep] * w
        i = int(np.argmax(s))
        if best is None or s[i] > best[0]:
            best = (float(s[i]), i, s)
    peak, i, s = best
    med = float(np.median(np.abs(s)))
    conf = peak / med if med > 1e-12 and peak > 0 else 0.0
    # 抛物线插值,取亚帧精度
    off = float(sec[i])
    if 0 < i < len(s) - 1:
        a, b, c = s[i - 1], s[i], s[i + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-12:
            off += hop * 0.5 * (a - c) / den
    return off, conf


# --------------------------------------------------------------------------
# 曲线工具
# --------------------------------------------------------------------------
def _fill_nan(a: np.ndarray) -> np.ndarray:
    """线性填 NaN;全 NaN 返回全 0 并标记。"""
    a = np.asarray(a, dtype=np.float64).copy()
    ok = np.isfinite(a)
    if not ok.any():
        return np.zeros_like(a)
    if ok.all():
        return a
    good = np.flatnonzero(ok)
    a[~ok] = np.interp(np.flatnonzero(~ok), good, a[ok])
    return a


def _med(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(a) < k:
        return a
    h = k // 2
    p = np.pad(a, (h, h), mode="edge")
    v = np.lib.stride_tricks.sliding_window_view(p, k)
    return np.median(v, axis=1)


def _box(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(a) < k:
        return a
    ker = np.ones(k, dtype=np.float64) / k
    return np.convolve(a, ker, mode="same")


def _rdp(pts: list[list[float]], tol: float) -> list[list[float]]:
    """按**竖直误差**简化折线(音分差才是听得出/听不出的东西)。"""
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x0, y0 = pts[i]
        x1, y1 = pts[j]
        dx = x1 - x0
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            x, y = pts[k]
            yp = y0 if dx <= 1e-12 else y0 + (y1 - y0) * (x - x0) / dx
            e = abs(y - yp)
            if e > best:
                best, bi = e, k
        if best > tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [pts[k] for k in range(n) if keep[k]]


def _to_curve(tt: np.ndarray, cc: np.ndarray, max_points: int = 1500) -> list:
    pts = [[round(float(t), 3), round(float(c), 1)] for t, c in zip(tt, cc)]
    for tol in (2.0, 4.0, 8.0, 12.0):
        out = _rdp(pts, tol)
        if len(out) <= max_points:
            return out
    step = int(math.ceil(len(pts) / max_points))
    return pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])


def _split_cents(total: float) -> tuple[int, float]:
    semis = int(round(total / 100.0))
    cents = total - semis * 100.0
    return max(-24, min(24, semis)), round(max(-100.0, min(100.0, cents)), 1)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def analyze(project: dict, ref_track_id, take_track_id,
            offset="auto", strength: float = 0.9, mode: str = "intonation",
            max_cents: float = CURVE_MAX_CENTS) -> dict:
    t_start = time.time()
    if str(ref_track_id) == str(take_track_id):
        raise RefError("原唱轨和你自己的轨不能是同一条")
    ref_tr = _find_track(project, ref_track_id)
    take_tr = _find_track(project, take_track_id)
    ref_clips = _usable_clips(project, ref_tr)
    take_clips = _usable_clips(project, take_tr)
    if not ref_clips:
        raise RefError(f"「{ref_tr.get('name') or ref_track_id}」上没有能读的音频")
    if not take_clips:
        raise RefError(f"「{take_tr.get('name') or take_track_id}」上没有能读的音频")

    infos: dict[str, dict] = {}
    notes: list[str] = []
    ref_paths = set()
    for c in ref_clips:
        got = _clip_audio(project, c)
        if got:
            ref_paths.add(str(got[0]))
    for c in ref_clips + take_clips:
        got = _clip_audio(project, c)
        if not got:
            continue
        path, src = got
        k = str(path)
        if k in infos:
            continue
        try:
            infos[k] = analyse_source(
                path, role=("ref" if k in ref_paths else "take"),
                name=src.get("name"))
        except RefError as e:
            notes.append(str(e))

    # ---- 对轴 ----
    auto_off = True
    if isinstance(offset, (int, float)):
        off = float(offset)
        conf = 9.99
        auto_off = False
    else:
        T = _timeline_chroma(project, take_tr, infos)
        R = _timeline_chroma(project, ref_tr, infos)
        off, conf = estimate_offset(T, R)
        if conf < 1.35 or abs(off) < 0.06:
            if abs(off) >= 0.06 and conf < 1.35:
                notes.append(f"对轴把握不足(把握 {conf:.2f}),先按你现在摆的位置算;"
                             f"如果整体听着错位,把两条轨手动对一下再来")
            off = 0.0
        else:
            notes.append(f"检测到你的轨比原唱晚 {off*1000:+.0f} ms(把握 {conf:.2f})")

    # ---- 逐片段算矫正 ----
    actions: list[dict] = []
    preview_t: list[float] = []
    preview_h: list[float] = []
    preview_r: list[float] = []

    def collect(off_: float):
        raw_all: list[float] = []
        per_clip: list[dict] = []
        matched = total = 0
        for c in take_clips:
            got = _clip_audio(project, c)
            if not got:
                continue
            path, _ = got
            info = infos.get(str(path))
            if not info:
                continue
            start = float(c.get("start") or 0)
            length = float(c.get("length") or 0)
            n = max(2, int(math.ceil(length / GRID)) + 1)
            tt = start + np.arange(n) * GRID
            tt = np.minimum(tt, start + length)

            his = _sample(info["cents"], info["times"], _time_to_src(c, tt))
            his_ok = np.isfinite(his)

            ref_c = np.full(n, np.nan)
            for rc in ref_clips:
                rstart = float(rc.get("start") or 0)
                rend = rstart + float(rc.get("length") or 0)
                m = (tt - off_ >= rstart) & (tt - off_ <= rend)
                if not m.any():
                    continue
                rgot = _clip_audio(project, rc)
                if not rgot:
                    continue
                rinfo = infos.get(str(rgot[0]))
                if not rinfo:
                    continue
                ss = _time_to_src(rc, tt[m] - off_)
                ref_c[m] = _sample(rinfo["cents"], rinfo["times"], ss)

            both = his_ok & np.isfinite(ref_c)
            total += n
            matched += int(both.sum())
            if both.sum() < max(8, int(0.08 * n)):
                per_clip.append({"clip": c, "n": n, "corr": None, "cover": 0.0,
                                 "his": his, "ref": ref_c, "tt": tt, "info": info})
                continue

            # 先各自去掉颤音(50ms 中值),再相减 —— 颤音必须原样留下
            his_f = _fill_nan(his)
            ref_f = _fill_nan(ref_c)
            raw = _med(ref_f, 3) - _med(his_f, 3)
            raw = _med(raw, 5)
            raw_all.extend(raw[both].tolist())
            per_clip.append({"clip": c, "n": n, "corr": raw, "cover": float(both.mean()),
                             "his": his, "ref": ref_c, "tt": tt, "info": info})
        return raw_all, per_clip, matched, total

    raw_all, per_clip, matched_frames, total_frames = collect(off)
    if not raw_all:
        raise RefError("两轨在时间上几乎没有重叠的「唱」;把它们摆到同一段,或者先对一下轴")

    raw_arr = np.asarray(raw_all, dtype=np.float64)

    # ★ 对轴体检:对完之后大部分帧还差着一大截,说明「同一时刻」根本对不上
    #   (换了段落 / 拉伸比例不同 / 原唱轨放错了)。这时逐点修只会制造怪音。
    #   自动对轴错了的话,退回「按你摆的位置」再算一次,取更像的那个。
    def _mismatch(a):
        return float(np.mean(np.abs(a - np.median(a)) > 150.0))

    if auto_off and off != 0.0 and _mismatch(raw_arr) > 0.45:
        raw0, per0, m0, t0 = collect(0.0)
        if raw0:
            a0 = np.asarray(raw0, dtype=np.float64)
            if _mismatch(a0) < _mismatch(raw_arr) - 0.05:
                notes.append("自动对轴的结果看起来不对,已退回「按你现在摆的位置」来算")
                off, conf = 0.0, 0.0
                raw_all, per_clip, matched_frames, total_frames = raw0, per0, m0, t0
                raw_arr = a0
    global_shift = float(np.median(raw_arr))
    octave = 0
    if 1100.0 <= abs(global_shift) <= 1300.0:
        octave = int(round(global_shift / 1200.0)) * 1200
        global_shift -= octave
        notes.append(f"原唱和你差一个八度({octave/1200:+.0f} 个),已按八度归一,"
                     f"不会把你硬拽上去")

    mismatch = _mismatch(raw_arr)
    trust = mismatch <= 0.45
    if not trust:
        notes.append(f"⚠ 有 {mismatch*100:.0f}% 的时间和原唱对不上 —— 对轴不准,"
                     f"所以**一条修改都没给**。先把两条轨摆到同一段、确认没有拉伸,再分析")
        notes.append("这一步宁可不动,也不能拿错位的原唱去改你的音高")

    base = 0.0
    lift = 0.0
    if mode == "intonation" and abs(global_shift) > 250.0:
        base = global_shift
        # ★ global_shift 是「要往哪搬」:正 = 你要升高 ⇒ 说明你比原唱**低**
        notes.append(f"你整体比原唱{'低' if global_shift > 0 else '高'} "
                     f"{abs(global_shift)/100:.1f} 个半音 —— 按「只修音准」处理:"
                     f"保持你的调,只修跑掉的地方。想直接对到原唱,把模式切到「完全跟原唱」")
    elif mode == "match" and abs(global_shift) > 190.0:
        # ★ 逐点曲线的硬上限是 ±200 音分;更大的整体差必须先当常数搬过去
        lift = round(global_shift)
        notes.append(f"按「完全跟原唱」处理:整体{'上' if lift > 0 else '下'}移 "
                     f"{abs(lift)/100:.2f} 个半音(超过逐点曲线的 ±200 上限,"
                     f"这一轨只能整体搬过去 + 逐点修剩下的)")
    elif mode == "match" and abs(global_shift) > 5.0:
        notes.append(f"按「完全跟原唱」处理:整体{'上' if global_shift > 0 else '下'}移 "
                     f"{abs(global_shift):.0f} 音分")

    for item in per_clip:
        if not trust:
            break
        c = item["clip"]
        raw = item["corr"]
        if raw is None:
            continue
        both = np.isfinite(raw)
        corr = raw - base - lift
        # 明显不是「跑调」的帧:不硬拽,标出来,用邻域补
        bad = np.abs(corr) > MATCH_MAX_RAW
        if bad.any():
            corr = corr.copy()
            corr[bad] = np.nan
        corr = _fill_nan(corr)
        corr = np.clip(corr * float(strength), -max_cents, max_cents)
        corr = _box(_med(corr, 3), 3)

        tt = item["tt"]
        his = item["his"]
        audible = np.isfinite(his)
        peak = float(np.max(np.abs(corr)))
        med = float(np.median(corr[audible])) if audible.any() else 0.0
        spread = float(np.percentile(corr[audible], 90) - np.percentile(corr[audible], 10)) \
            if audible.sum() > 4 else 0.0

        # 预览(给界面画两条线),降采样到 ~100ms
        step = max(1, int(0.1 / GRID))
        for k in range(0, len(tt), step):
            if not np.isfinite(his[k]):
                continue
            preview_t.append(round(float(tt[k]), 2))
            preview_h.append(round(float(his[k]), 1))
            # raw = 原唱 − 你,所以 原唱 = 你 + raw
            preview_r.append(round(float(his[k] + raw[k]), 1))

        # ★ 逐点修正很小、但整体要搬一个掉(「完全跟原唱」模式)时也得出一条动作
        if not audible.any() or (peak < MIN_CORRECT_CENTS and abs(lift) < MIN_CORRECT_CENTS):
            continue

        old_semis = float(c.get("semis") or 0)
        old_cents = float(c.get("cents") or 0)
        old_total = old_semis * 100.0 + old_cents
        # 曲线/常数都是**绝对**值(相对源),所以重跑结果稳定,不会越修越高
        a = {
            "id": f"r{len(actions)+1}", "kind": "pitch", "category": "tune",
            "trackId": take_tr.get("id"), "trackName": take_tr.get("name"),
            "clipId": c.get("id"), "clipName": c.get("name") or "",
            "start": round(float(c.get("start") or 0), 2),
            "length": round(float(c.get("length") or 0), 2),
            "from": {"semis": int(round(old_semis)), "cents": round(old_cents, 1)},
            "stats": {"frames": int(audible.sum()), "coverage": round(item["cover"], 2),
                      "medianCents": round(med, 0), "peakCents": round(peak, 0),
                      "spreadCents": round(spread, 0)},
        }
        if spread < 30.0 or abs(lift) > 0:
            semis, cents = _split_cents(med + lift)
            a["mode"] = "constant"
            a["to"] = {"semis": semis, "cents": cents}
            if abs(lift) > 0:
                a["why"] = (f"按「完全跟原唱」整体搬 {lift/100:+.2f} 个半音"
                            f"(这一轨差得太多,逐点曲线搬不动),剩下的细节按 "
                            f"{int(strength*100)}% 收回来")
            else:
                a["why"] = (f"这一段原唱平均比你{'高' if med + lift > 0 else '低'} "
                            f"{abs(med + lift):.0f} 音分;"
                            f"按 {int(strength*100)}% 收回来(整段平移,颤音原样保留)")
        else:
            # ★ 曲线的时间是**源内**的(t 相对 srcIn),不是时间轴的 ——
            #   片段被拉伸过时两者不一样,搞错会让曲线整段错位。
            src_t = _time_to_src(c, tt) - float(c.get("srcIn") or 0)
            curve = _to_curve(src_t, corr)
            a["mode"] = "curve"
            a["to"] = {"semis": 0, "cents": 0, "curve": curve}
            a["why"] = (f"这一段里有 {int(item['cover']*100)}% 的时间能对上原唱;"
                        f"按 20ms 一条曲线逐点收 {int(strength*100)}%,"
                        f"每个音各自落到原唱的音高上(最多 ±{int(max_cents)} 音分),"
                        f"颤音不动")
        actions.append(a)

    # 整体偏移也是一条动作(他自己勾)
    if trust and auto_off and abs(off) >= 0.06 and conf >= 1.35:
        actions.insert(0, {
            "id": "r0", "kind": "shift", "category": "tune",
            "trackId": take_tr.get("id"), "trackName": take_tr.get("name"),
            "deltaSec": round(-off, 3), "conf": round(conf, 2),
            "why": (f"你的轨比原唱{'晚' if off > 0 else '早'} {abs(off)*1000:.0f} ms"
                    f"(把握 {conf:.2f})。勾上 = 把你这一轨整体"
                    f"{'前' if off > 0 else '后'}移 {abs(off)*1000:.0f} ms(块只挪位置,不渲染)"),
        })

    # 预览只留最多 2000 点
    if len(preview_t) > 2000:
        step = int(math.ceil(len(preview_t) / 2000))
        preview_t, preview_h, preview_r = preview_t[::step], preview_h[::step], preview_r[::step]

    if not trust:
        # 对轴不可信时,连「检测到你晚了 X ms」也收回 —— 那个数同样不可信
        notes = [n for n in notes if not n.startswith("检测到你的轨比原唱")]
    elif not actions:
        notes.append("几乎没有需要修的地方 —— 你已经唱得很贴原唱了")

    # ---- 知识库:这首歌认识吗、唱过几次、这次比上次好吗 ----
    ref_meta = None
    for k in ref_paths:
        inf = infos.get(k) or {}
        if inf.get("refdb"):
            r0 = dict(inf["refdb"])
            r0["used"] = int(inf.get("used", r0.get("used", 0)) or 0)
            hist = inf.get("takes") or []
            r0["takes"] = len(hist)
            r0["lastMedianCents"] = (hist[-1] or {}).get("medianCents") if hist else None
            r0["hit"] = inf.get("hit") or "new"
            ref_meta = r0
            break
    if ref_meta:
        if ref_meta["hit"] in ("path", "content"):
            notes.append(f"这首歌认识(第 {ref_meta['used']} 次):音高地图直接读上次存的,"
                         f"没有重算。")
        else:
            notes.append("第一次听到这首歌 —— 已经把它存进歌库,下次同一首秒开。")

    take_med = None
    take_cov = None
    for k, inf in infos.items():
        if k in ref_paths:
            continue
        e = inf.get("refdb")
        if e:
            meds = [a.get("stats", {}).get("medianCents") for a in actions
                    if a.get("stats") and a["stats"].get("medianCents") is not None]
            take_med = round(float(np.median(meds)), 1) if meds else 0.0
            take_cov = round(matched_frames / max(1, total_frames), 3)
            break
    if ref_meta and take_med is not None:
        prev = ref_meta.get("lastMedianCents")
        if prev is not None:
            d_ = abs(take_med) - abs(float(prev))
            notes.append(f"这首歌你唱过 {ref_meta['takes']} 次;上次整体偏 {abs(float(prev)):.0f} 音分,"
                         f"这次 {abs(take_med):.0f} 音分"
                         + (f"(进步 {abs(d_):.0f})" if d_ < -2 else
                            (f"(比上次多偏 {abs(d_):.0f})" if d_ > 2 else "(基本一样)")))
        rdb.record_take(ref_meta["id"], {"medianCents": take_med,
                                         "coverage": take_cov,
                                         "frames": int(matched_frames)})

    dur = float(sum(float(c.get("length") or 0) for c in take_clips))
    res = {
        "ok": True,
        "offset": round(float(off), 3),
        "offsetConf": round(float(conf), 2),
        "offsetAuto": auto_off,
        "globalShiftCents": round(global_shift, 1),
        "octaveCents": octave,
        "baseCents": round(base, 1),
        "strength": strength,
        "mode": mode,
        "ref": {"trackId": ref_tr.get("id"), "name": ref_tr.get("name"),
                "key": _common_key(_keys_of(infos, project, ref_clips)),
                "clips": len(ref_clips)},
        "take": {"trackId": take_tr.get("id"), "name": take_tr.get("name"),
                 "key": _common_key(_keys_of(infos, project, take_clips)),
                 "clips": len(take_clips)},
        "coverage": round(matched_frames / max(1, total_frames), 3),
        "matchedSec": round(matched_frames * GRID, 1),
        "takeSec": round(dur, 1),
        "preview": {"t": preview_t, "his": preview_h, "ref": preview_r},
        "refdb": ref_meta,
        "actions": actions,
        "notes": notes,
        "limits": LIMITS,
        "sec_cost": round(time.time() - t_start, 2),
    }
    return res


def _keys_of(infos: dict, project: dict, clips: list[dict]) -> list[dict]:
    out = []
    for c in clips:
        got = _clip_audio(project, c)
        if not got:
            continue
        info = infos.get(str(got[0]))
        if info:
            out.append(info)
    return out


def _common_key(infos: list[dict]) -> dict:
    if not infos:
        return {}
    votes: dict[tuple, float] = {}
    for i in infos:
        k = (i.get("key") or "C", i.get("mode") or "major")
        votes[k] = votes.get(k, 0.0) + float(i.get("voiced_s") or 1.0)
    k = max(votes, key=votes.get)
    return {"root": k[0], "mode": k[1]}


# --------------------------------------------------------------------------
# 人声分离(Demucs)——可选;没装就说清楚,不假装能做
# --------------------------------------------------------------------------
def demucs_available() -> tuple[bool, str]:
    try:
        import importlib.util
        if importlib.util.find_spec("demucs") is None:
            return False, "没装 demucs"
        return True, ""
    except Exception as e:                   # noqa: BLE001
        return False, f"检查 demucs 失败:{e}"


def separate_vocals(path: Path, out_dir: Path, timeout: int = 1800) -> dict:
    """跑 Demucs 分出 vocals / no_vocals。CPU,慢;结果按源文件缓存。"""
    ok, why = demucs_available()
    if not ok:
        raise RefError(f"{MSG_DEMUCS}({why})")
    path = Path(path).resolve()
    if not path.exists():
        raise RefError(f"找不到音频文件:{path.name}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    v = out_dir / "htdemucs" / stem / "vocals.wav"
    nv = out_dir / "htdemucs" / stem / "no_vocals.wav"
    if v.exists() and nv.exists():
        return {"vocals": str(v), "instrumental": str(nv), "cached": True}
    t0 = time.time()
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals",
           "-n", "htdemucs", "-o", str(out_dir), str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RefError(f"Demucs 超时({timeout}s)。CPU 上长歌会很慢,先切一小段再试")
    if p.returncode != 0:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
        raise RefError("Demucs 失败:" + (" / ".join(tail) or f"退出码 {p.returncode}"))
    if not (v.exists() and nv.exists()):
        raise RefError("Demucs 跑完了但没找到输出文件")
    return {"vocals": str(v), "instrumental": str(nv), "cached": False,
            "sec_cost": round(time.time() - t0, 1)}


STEMS4 = ("drums", "bass", "other", "vocals")
STEMS6 = ("drums", "bass", "other", "vocals", "guitar", "piano")

# 档位 -> (demucs 模型名, 轨名列表)。★ 模型名必须和 `-n` 一致,缓存目录靠它认。
STEM_MODELS = {
    "4": ("htdemucs", STEMS4),
    "6": ("htdemucs_6s", STEMS6),
}


def separate_stems(path: Path, out_dir: Path, timeout: int = 3600,
                   model: str = "4") -> dict:
    """多轨分离:四轨(鼓/贝斯/其他/人声)或六轨(多出吉他/钢琴)。

    ★ 和 separate_vocals 的区别只在命令行:不加 `--two-stems`,Demucs 就按
      完整的多轨模型跑一遍。CPU 上比两轨**贵不了多少**
      (同一张网络,多出来的只是最后几次波形相加),但拿到的东西完全不同——
      「自己再调」得有独立的一轨鼓、一轨贝斯才调得动。

    ★ 六轨就是 Demucs 的**上限**,不是「想拆多细都行」:
      吉他/钢琴只在它们真的占主导时才分得出来,电钢琴、合成器、弦乐通常落在
      「其他」里;再往下拆「底鼓/军鼓/镲片」做不到(数学上就分不开)。
    """
    ok, why = demucs_available()
    if not ok:
        raise RefError(f"{MSG_DEMUCS}({why})")
    key = "6" if str(model) == "6" else "4"
    net, names = STEM_MODELS[key]
    path = Path(path).resolve()
    if not path.exists():
        raise RefError(f"找不到音频文件:{path.name}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / net / path.stem
    outs = {k: base / f"{k}.wav" for k in names}
    if all(p.exists() for p in outs.values()):
        return {**{k: str(v) for k, v in outs.items()}, "cached": True, "model": key}

    t0 = time.time()
    # ★ 六轨模型的 yaml 要现拉,走的是 HuggingFace;国内直连会卡在重试里出不来。
    #   带上镜像(模型已在本地缓存时这一步会被跳过,不影响四轨)。
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    cmd = [sys.executable, "-m", "demucs", "-n", net,
           "-o", str(out_dir), str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RefError(f"Demucs 超时({timeout}s)。这个长度在 CPU 上太久,先剪短一点")
    if p.returncode != 0:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
        raise RefError("Demucs 失败:" + (" / ".join(tail) or f"退出码 {p.returncode}"))
    missing = [k for k, v in outs.items() if not v.exists()]
    if missing:
        raise RefError("Demucs 跑完了但这些轨没出来:" + "、".join(missing))
    return {**{k: str(v) for k, v in outs.items()}, "cached": False,
            "sec_cost": round(time.time() - t0, 1), "model": key}
