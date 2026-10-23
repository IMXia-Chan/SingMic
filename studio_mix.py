"""Studio 的纯 DSP 层 —— **不 import FastAPI**,可以单独测、单独跑。

这里只管三件事:
  1. 一个片段该怎么变调/变速(`render_piece`),以及它的缓存 key(`clip_key`);
  2. 整曲怎么混(`mix_project`,阶段 5 才接上);
  3. 目录与原子落盘(`write_wav_atomic`)。

★ 原录音只读。所有产物进 `projects/`,一个字节都不回写 `recordings/`。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

import studio_fx
import studio_vst
import tuning

# 程序目录：web/、tools/、kits 模板这些**只读资源**在这里；打包后就是 _internal。
APP = Path(__file__).resolve().parent
# 界面上「程序」那一行要显示**安装目录**（exe 所在），不是内部的 _internal
APP_HOME = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP

# 数据根：录音、工程、导出、渲染缓存、鼓包。
# ★ 工程和媒体里存的都是**相对数据根**的路径，所以下面 ROOT 必须指数据根，
#   不能再指程序目录 —— 这两个概念从这一行开始分开。
# ★ 没设 SINGMIC_DATA 时 DATA == APP，开发与便携版的行为跟以前一模一样；
#   安装版由 desktop.py 设到 %LOCALAPPDATA%\SingMic\data（Program Files 只读，写不进去）。
_env_data = (os.environ.get("SINGMIC_DATA") or "").strip()
DATA = Path(_env_data).expanduser() if _env_data else APP

ROOT = DATA                      # 兼容旧代码：相对路径的解析基准
REC = DATA / "recordings"
PROJECTS = DATA / "projects"
MEDIA = PROJECTS / "media"
CACHE = PROJECTS / "cache"
EXPORTS = PROJECTS / "exports"

SR = 48000
DSP_VERSION = "psola-v1"

# ★ 只有**真改过**的片段才加淡入淡出;没改的逐点原样,一个采样都不许动。
FADE_S = 0.005


def ensure_dirs() -> None:
    for p in (PROJECTS, MEDIA, CACHE, EXPORTS):
        p.mkdir(parents=True, exist_ok=True)


def file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return int(st.st_size), int(st.st_mtime)


def clip_key(source_rel: str, size: int, mtime: int,
             src0: int, src1: int, cents: float, out_n: int,
             extra: str = "") -> str:
    """内容寻址的缓存 key。

    ★ 里面**不许出现 block id** —— 它会随重新切块漂移,旧缓存就悄悄串到别的音上。
    ★ `extra` 只有「音高曲线」会用(曲线指纹);空串时 key 与以前逐字节一致,
      所以旧缓存全部照常命中。
    """
    raw = (f"{source_rel}|{size}:{mtime}|{int(src0)}|{int(src1)}|"
           f"{int(round(cents))}|{int(out_n)}|{DSP_VERSION}")
    if extra:
        raw += f"|{extra}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def curve_sig(curve) -> str:
    """曲线指纹。只看曲线本身(时间 2 位小数、音分 1 位),和剪辑位置无关。"""
    if not curve:
        return ""
    try:
        pts = sorted((round(float(t), 2), round(float(c), 1)) for t, c in curve)
    except Exception:                       # noqa: BLE001
        return ""
    if not pts:
        return ""
    return "cv" + hashlib.sha1(json.dumps(pts).encode("utf-8")).hexdigest()[:10]


def load_mono(path: Path) -> tuple[np.ndarray, int]:
    x, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x.mean(axis=1).astype(np.float32)
    return x, int(sr)


def load_stereo(path: Path) -> tuple[np.ndarray, int]:
    """读成 (n, 2)。单声道文件复制到左右 —— 和浏览器把单声道源接进立体声总线一致。

    ★ 以前整条混音路都是 `load_mono`:导出的歌被并成双单声道(L == R)。
      本来有立体声宽度的歌,过一遍调音台就窄了,两边电平还各低 3 dB。
    """
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    elif x.shape[1] > 2:
        x = x[:, :2]
    return np.ascontiguousarray(x, dtype=np.float32), int(sr)


def _fade(x: np.ndarray, sr: int, sec: float = FADE_S) -> np.ndarray:
    n = len(x)
    k = min(int(sec * sr), n // 2)
    if k > 0:
        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
        x = x.copy()
        x[:k] *= ramp
        x[-k:] *= ramp[::-1]
    return x


def render_piece(x: np.ndarray, sr: int, src0: int, src1: int,
                 cents: float = 0.0, out_n: int | None = None,
                 f0_hint: float | None = None) -> np.ndarray:
    """把 x[src0:src1] 变调 `cents` 音分、拉伸到 out_n 个采样。

    ★ 没变调也没变长时**逐点原样返回**(调用方据此保证恒等)。
    """
    src0 = max(0, int(src0))
    src1 = min(len(x), int(src1))
    if src1 <= src0:
        return np.zeros(0, dtype=np.float32)
    piece = np.asarray(x[src0:src1], dtype=np.float64)
    n = len(piece)
    want = int(out_n) if out_n else n

    if abs(cents) < 0.5 and want == n:
        return piece.astype(np.float32)

    alpha = want / n
    beta = 2.0 ** (float(cents) / 1200.0)
    f0 = None
    if f0_hint and f0_hint > 0:
        f0 = np.full(max(1, int(math.ceil(n / tuning.HOP)) + 2), float(f0_hint))
    y = tuning.psola(piece, sr, alpha=alpha, beta=beta, f0=f0)
    y = np.asarray(y, dtype=np.float32)
    if len(y) != want:
        if len(y) > want:
            y = y[:want]
        else:
            y = np.pad(y, (0, want - len(y)))
    y = _fade(y, sr)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def render_curve_piece(x: np.ndarray, sr: int, src0: int, src1: int,
                       curve, out_n: int | None = None) -> np.ndarray:
    """按**块内音高曲线**渲染一段。curve = [(t 秒, cents), …],t 相对 srcIn。"""
    src0 = max(0, int(src0))
    src1 = min(len(x), int(src1))
    if src1 <= src0:
        return np.zeros(0, dtype=np.float32)
    piece = np.asarray(x[src0:src1], dtype=np.float64)
    n = len(piece)
    want = int(out_n) if out_n else n
    if not curve and want == n:
        return piece.astype(np.float32)
    y = tuning.psola_curve(piece, sr, alpha=want / n, curve=curve)
    y = np.asarray(y, dtype=np.float32)
    if len(y) != want:
        if len(y) > want:
            y = y[:want]
        else:
            y = np.pad(y, (0, want - len(y)))
    y = _fade(y, sr)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def write_wav_atomic(path: Path, data: np.ndarray, sr: int = SR,
                     subtype: str = "FLOAT") -> None:
    """先写 .part 再改名 —— 半截文件不许留在盘上。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    # ★ 必须显式给 format —— soundfile 认不出 `.wav.part` 的后缀
    sf.write(str(tmp), data, sr, subtype=subtype, format="WAV")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 自动测速(DSP,不是神经网络)
# ---------------------------------------------------------------------------
def _onset_env(x: np.ndarray, sr: int, n: int = 1024, hop: int = 256) -> np.ndarray:
    """谱通量(正半波差分)当起音包络。分块算 —— 7 分钟整段做 STFT 会吃掉几百 MB。"""
    win = np.hanning(n).astype(np.float32)
    frames = 1 + (len(x) - n) // hop
    if frames < 8:
        return np.zeros(0, dtype=np.float64)
    env = np.zeros(frames, dtype=np.float64)
    prev = None
    chunk = 1024
    for s in range(0, frames, chunk):
        e = min(frames, s + chunk)
        idx = np.arange(n)[None, :] + hop * np.arange(s, e)[:, None]
        S = np.abs(np.fft.rfft(x[idx] * win, axis=1))
        if prev is not None:
            env[s - 1] = float(np.maximum(0.0, S[0] - prev).sum())
        if e - s > 1:
            d = np.maximum(0.0, np.diff(S, axis=0)).sum(axis=1)
            env[s:s + len(d)] = d
        prev = S[-1]
    return env


def _autocorr(e: np.ndarray) -> np.ndarray:
    n = 1 << int(2 * len(e) - 1).bit_length()
    F = np.fft.rfft(e, n)
    acf = np.fft.irfft(F * np.conj(F), n)[:len(e)]
    return acf / (acf[0] + 1e-12)


def estimate_bpm(path: Path, max_sec: float = 120.0) -> dict:
    """从一段音频里测速。

    ★ 只取前 120 秒 —— 取样再长也不会更准,只会更慢。
    ★ 候选按「梳状」打分(第 1/2/3/4 拍都算),避免 60/120/240 这种倍频误判。
    """
    x, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if len(x) > sr * max_sec:
        x = x[: int(sr * max_sec)]
    if sr != 22050 and len(x) > 1:
        from scipy.signal import resample_poly
        g = math.gcd(int(sr), 22050)
        x = resample_poly(x, 22050 // g, int(sr) // g).astype(np.float32)
        sr = 22050
    if len(x) < sr * 2:
        return {"bpm": 0.0, "candidates": [], "offset": 0.0, "conf": 0.0,
                "err": "太短"}

    n, hop = 1024, 256
    env = _onset_env(x, sr, n, hop)
    if len(env) < 16:
        return {"bpm": 0.0, "candidates": [], "offset": 0.0, "conf": 0.0,
                "err": "太短"}
    fps = sr / hop
    k = max(3, int(fps * 0.5))
    ma = np.convolve(env, np.ones(k) / k, mode="same")
    e = np.maximum(0.0, env - ma)
    e = (e - e.mean()) / (e.std() + 1e-9)

    acf = _autocorr(e)
    scored: list[tuple[float, float]] = []
    for bpm in np.arange(60.0, 180.01, 0.25):
        p = 60.0 * fps / bpm
        if p < 2 or p * 4 >= len(acf):
            continue
        s = 0.0
        for mult, w in ((1, 1.0), (2, 0.6), (3, 0.35), (4, 0.25)):
            i = int(round(p * mult))
            if 0 < i < len(acf):
                s += w * float(acf[i])
        # ★ 倍频先验:没有这一项,纯鼓点会被判成一半速度(120 → 60)。
        #   不奖励 p/2 —— 那会反过来专挑慢的那一半。
        prior = math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.6) ** 2)
        scored.append((s * prior, float(bpm)))
    if not scored:
        return {"bpm": 0.0, "candidates": [], "offset": 0.0, "conf": 0.0,
                "err": "测不出来"}
    scored.sort(reverse=True)
    top = scored[0]
    cands = [top]
    for s, b in scored[1:]:
        if all(abs(b - c[1]) / c[1] > 0.05 for c in cands):
            cands.append((s, b))
        if len(cands) >= 3:
            break
    second = next((s for s, b in cands[1:] if abs(b - top[1]) / top[1] > 0.08), 0.0)
    conf = float(top[0] / second) if second > 1e-6 else 10.0

    # 相位 + 小数级速度精修。
    # ★ 必须**按小数位置插值**取拍点:128 BPM 的 p=46.87,直接 round 成 47,
    #   半分钟就漂出去几十毫秒,相位怎么对都对不上。
    xs = np.arange(len(e))

    def comb(bpm: float) -> tuple[float, float]:
        pp = 60.0 * fps / bpm
        best = (-1e18, 0.0)
        for phi in np.arange(0.0, max(1.0, pp), 0.5):
            pos = np.arange(phi, len(e) - 1, pp)
            if len(pos) < 3:
                continue
            v = float(np.interp(pos, xs, e).mean())   # ★ 用均值,不然拍子越多分越高
            if v > best[0]:
                best = (v, phi)
        return best

    bpm_best, phi_best, v_best = top[1], 0.0, -1e18
    lo = max(60.0, top[1] - 1.6)
    hi = min(180.0, top[1] + 1.6)
    for bpm2 in np.arange(lo, hi + 1e-9, 0.05):
        v, phi = comb(float(bpm2))
        if v > v_best:
            v_best, bpm_best, phi_best = v, float(bpm2), phi

    # ★ 帧 -> 秒:phi 是**帧**号,要乘 hop 才是采样数。
    offset = ((phi_best * hop + n / 2.0) / sr) % (60.0 / bpm_best)

    return {
        "bpm": round(float(bpm_best), 2),
        "candidates": [{"bpm": round(float(bpm_best), 2), "score": round(top[0], 3)}]
        + [{"bpm": round(b, 2), "score": round(s, 3)} for s, b in cands[1:]],
        "offset": round(float(offset), 3),
        "conf": round(float(min(conf, 10.0)), 2),
        "sec": round(len(x) / sr, 2),
    }


# ---------------------------------------------------------------------------
# 跑调检测(只标,不替人改)
# ---------------------------------------------------------------------------
_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_MINOR = (0, 2, 3, 5, 7, 8, 10)
_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def nearest_scale_midi(midi: float, pcs) -> int:
    m = int(round(midi))
    for d in range(0, 13):
        for cand in (m - d, m + d):
            if cand % 12 in pcs:
                return cand
    return m


# ---------------------------------------------------------------------------
# 和弦检测(色度 + 模板 + Viterbi 平滑)
# ---------------------------------------------------------------------------
_CHORDS = ((0, 4, 7, "maj"), (0, 3, 7, "min"))


def detect_chords(path: Path, max_sec: float = 180.0,
                  n: int = 4096, hop: int = 1024) -> dict:
    """★ 诚实版:鼓和人声都在的时候它会错。只拿来建议和声,不当乐理真理。"""
    x, sr = load_mono(path)
    if len(x) > sr * max_sec:
        x = x[: int(sr * max_sec)]
    if sr != 22050:
        x = _resample(x, sr, 22050)
        sr = 22050
    if len(x) < sr:
        return {"chords": [], "sec": round(len(x) / sr, 2), "err": "太短"}

    win = np.hanning(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    valid = (freqs > 55) & (freqs < 2000)
    midi = 69 + 12 * np.log2(freqs[valid] / 440)
    pcs = np.round(midi).astype(int) % 12
    frames = 1 + (len(x) - n) // hop
    chroma = np.zeros((frames, 12), dtype=np.float32)
    chunk = 512
    for s in range(0, frames, chunk):
        e = min(frames, s + chunk)
        idx = np.arange(n)[None, :] + hop * np.arange(s, e)[:, None]
        S = np.abs(np.fft.rfft(x[idx] * win, axis=1))[:, valid]
        for p in range(12):
            m = pcs == p
            if m.any():
                chroma[s:e, p] = S[:, m].sum(axis=1)
    chroma /= (chroma.sum(axis=1, keepdims=True) + 1e-9)

    # 24 个三和弦模板
    T = []
    for root in range(12):
        for iv, q in ((_CHORDS[0], "maj"), (_CHORDS[1], "min")):
            v = np.zeros(12, dtype=np.float32)
            for k in iv[:3]:
                v[(root + k) % 12] = 1.0
            T.append(v / np.linalg.norm(v))
    T = np.stack(T)
    sim = chroma @ T.T                       # frames × 24

    # Viterbi:换和弦要付一点代价,否则每两帧就跳一次
    pen = 0.06
    F, K = sim.shape
    dp = np.empty((F, K), dtype=np.float32)
    bp = np.zeros((F, K), dtype=np.int16)
    dp[0] = sim[0]
    for i in range(1, F):
        best = dp[i - 1].copy()
        arg = np.arange(K, dtype=np.int16)
        for k in range(K):
            alt = dp[i - 1] - pen
            j = int(np.argmax(alt))
            if alt[j] > best[k]:
                best[k] = alt[j]
                arg[k] = j
        dp[i] = sim[i] + best
        bp[i] = arg
    path_idx = np.zeros(F, dtype=np.int16)
    path_idx[-1] = int(np.argmax(dp[-1]))
    for i in range(F - 1, 0, -1):
        path_idx[i - 1] = bp[i, path_idx[i]]

    # 合并连续段
    out = []
    t_hop = hop / sr
    i = 0
    while i < F:
        j = i
        while j + 1 < F and path_idx[j + 1] == path_idx[i]:
            j += 1
        k = int(path_idx[i])
        root = k // 2
        q = _CHORDS[k % 2][3]
        seg = sim[i:j + 1, k]
        out.append({"t0": round(i * t_hop, 3), "t1": round((j + 1) * t_hop, 3),
                    "root": _NOTE_NAMES[root], "quality": q,
                    "name": _NOTE_NAMES[root] + ("m" if q == "min" else ""),
                    "conf": round(float(seg.mean()), 3)})
        i = j + 1
    # 太短的段并进前一段
    merged = []
    for c in out:
        if merged and c["t1"] - c["t0"] < 0.25:
            merged[-1]["t1"] = c["t1"]
            continue
        if merged and merged[-1]["name"] == c["name"]:
            merged[-1]["t1"] = c["t1"]
            continue
        merged.append(c)
    return {"chords": merged, "sec": round(len(x) / sr, 2)}


def _resample(y: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or len(y) == 0:
        return y.astype(np.float32, copy=False)
    from scipy.signal import resample_poly
    g = math.gcd(int(sr_in), int(sr_out))
    return resample_poly(y, int(sr_out) // g, int(sr_in) // g).astype(np.float32)


def _apply_env(y: np.ndarray, sr: int, gain: float,
               fade_in: float, fade_out: float) -> np.ndarray:
    """与浏览器调度器同一套包络:两端线性。(n,) 和 (n,2) 都吃。"""
    y = np.asarray(y, dtype=np.float32) * float(gain)
    n = len(y)
    k_in = min(int(round(max(0.0, fade_in) * sr)), n)
    k_out = min(int(round(max(0.0, fade_out) * sr)), max(0, n - k_in))
    ramp_in = np.linspace(0.0, 1.0, k_in, dtype=np.float32)
    ramp_out = np.linspace(1.0, 0.0, k_out, dtype=np.float32)
    if y.ndim == 2:                      # 立体声:同一条斜线作用在每一列
        ramp_in = ramp_in[:, None]
        ramp_out = ramp_out[:, None]
    if k_in > 0:
        y[:k_in] *= ramp_in
    if k_out > 0:
        y[-k_out:] *= ramp_out
    return y


def _raw(path: Path, memo: dict) -> tuple[np.ndarray, int]:
    k = "raw:" + str(path)
    if k not in memo:
        memo[k] = load_mono(path)
    return memo[k]


def _load_src(path: Path, sr_out: int, memo: dict) -> np.ndarray:
    """源文件 → (n, 2) / 48k。★ 缓存 key 带 `st:` 前缀,别和单声道那份撞。"""
    k = "st:" + str(path)
    if k in memo:
        return memo[k]
    y, sr = load_stereo(path)
    y = _resample(y, sr, sr_out)
    memo[k] = y
    return y


def _src_channels(path: Path, memo: dict) -> int:
    """源文件原本有几条声道 —— 决定这一轨用哪种声像律。"""
    k = "ch:" + str(path)
    if k not in memo:
        try:
            memo[k] = int(sf.info(str(path)).channels)
        except Exception:                 # noqa: BLE001
            memo[k] = 1
    return memo[k]


def _clip_piece(project: dict, clip: dict, sr_out: int, memo: dict,
                log: list) -> np.ndarray | None:
    """一个 clip 该发出来的波形 (n, 2)(已经变调/变速过)。

    ★ 变过调的块仍是**单声道**渲染(PSOLA 是单声道算法),再复制到左右两边。
      这是明说的限制:变调会把立体声宽度收成居中,不变调的块宽度原样保留。
    """
    src = (project.get("sources") or {}).get(clip.get("sourceId"))
    semis = float(clip.get("semis") or 0)
    cents = float(clip.get("cents") or 0)
    length = max(0.0, float(clip.get("length") or 0))
    out_n = int(round(length * sr_out))
    if out_n <= 0:
        return None
    curve = clip.get("pitchCurve") or []
    csig = curve_sig(curve) if curve else ""
    need = (abs(semis) >= 1e-9 or abs(cents) >= 1e-9 or bool(csig))
    if need:
        # ★ 与 /render_clip 完全同一个 key —— 导出用的就是刚放出来听的那一份。
        #   ★ 源采样率是多少就按多少算(44.1k 的源在 48k 工程里也能命中同一份缓存)。
        try:
            path = (ROOT / src["path"]).resolve()
            size, mtime = file_identity(path)
            x, sr = _raw(path, memo)
            src0 = int(round(float(clip.get("srcIn") or 0) * sr))
            src1 = int(round(float(clip.get("srcOut") or 0) * sr))
            out_n_src = int(round(length * sr))
            # ★ 有曲线时半音/音分已经折进曲线(UI 会清零),key 里也必须用 0 ——
            #   要和 /render_curve 算出来的是同一个 key,否则导出会自己另算一份。
            key = clip_key(str(src["path"]), size, mtime, src0, src1,
                           0.0 if csig else (semis * 100.0 + cents), out_n_src, csig)
            f = CACHE / f"clip_{key}.wav"
            if not f.exists():
                y = (render_curve_piece(x, sr, src0, src1, curve, out_n_src) if csig
                     else render_piece(x, sr, src0, src1, semis * 100.0 + cents, out_n_src))
                write_wav_atomic(f, y, sr)
            y, sr_file = load_mono(f)
            if sr_file != sr_out:
                y = _resample(y, sr_file, sr_out)
            if len(y) != out_n:
                y = y[:out_n] if len(y) > out_n else np.pad(y, (0, out_n - len(y)))
            return np.repeat(y.astype(np.float32, copy=False)[:, None], 2, axis=1)
        except Exception as e:            # noqa: BLE001
            log.append(f"{clip.get('id')}: {e}")
            return None
    if not src:
        log.append(f"{clip.get('id')}: 源没了")
        return None
    try:
        x = _load_src((ROOT / src["path"]).resolve(), sr_out, memo)
    except Exception as e:                # noqa: BLE001
        log.append(f"{clip.get('id')}: {e}")
        return None
    a = int(round(float(clip.get("srcIn") or 0) * sr_out))
    b = int(round(float(clip.get("srcOut") or 0) * sr_out))
    y = x[max(0, a):min(len(x), max(a, b))]
    if len(y) < out_n:
        y = np.pad(y, ((0, out_n - len(y)), (0, 0)))
    elif len(y) > out_n:
        y = y[:out_n]
    return y


def _note_piece(tr: dict, nt: dict, sr_out: int, memo: dict) -> np.ndarray | None:
    smp = tr.get("sample") or {}
    pool = smp.get("pool") or []
    semi = int(round(float(nt.get("midi") or 60) - float(smp.get("baseMidi") or 60)))
    if semi < int(smp.get("poolLo", -7)) or semi > int(smp.get("poolHi", 7)):
        return None
    e = next((x for x in pool if int(x.get("semis", 0)) == semi), None)
    if not e:
        return None
    f = CACHE / f"clip_{e['key']}.wav"
    if not f.exists():
        return None
    base = load_mono(f)[0]
    dur = max(0.01, float(nt.get("length") or 0))
    n = int(round(dur * sr_out))
    y = base[:n] if len(base) >= n else np.pad(base, (0, n - len(base)))
    vel = max(0.01, min(1.2, float(nt.get("vel", 0.9))))
    atk = max(0.001, float(tr.get("attack", 0.006)))
    rel = max(0.005, min(float(tr.get("release", 0.06)), dur))
    env = np.full(n, vel, dtype=np.float32)
    ki = min(n, int(round(atk * sr_out)))
    if ki > 0:
        env[:ki] = np.linspace(0.0, vel, ki, dtype=np.float32)
    ko = min(n, int(round(rel * sr_out)))
    if ko > 0:
        env[n - ko:] = np.linspace(vel, 0.0, ko, dtype=np.float32)
    # 采样器的音色池是单声道 ⇒ 左右各放一份
    return np.repeat((y.astype(np.float32) * env)[:, None], 2, axis=1)


# ---------------------------------------------------------------------------
# 每轨 FX(和 engine.js 的节点图一一对应)
# ---------------------------------------------------------------------------
def _make_ir(sr: int, sec: float, decay: float) -> np.ndarray:
    """★ 必须和 engine.makeIR **同一串随机数**:同样的 LCG、同样的包络、同样按能量归一。

    ★ 按**能量**归一(∑v² = 1),不是按峰值 —— 按峰值归一时 0.6 秒的噪声冲激响应
      总能量有几千,湿声会比干声大 30dB,一开混响整条就爆。
    """
    n = max(1, int(round(sec * sr)))
    k = max(1, int(round(decay * sr)))
    ir = np.zeros((2, n), dtype=np.float32)
    for ch in range(2):
        s = (12345 + ch * 999) & 0xFFFFFFFF
        v = np.empty(n, dtype=np.float64)
        for i in range(n):
            s = (s * 1103515245 + 12345) & 0xFFFFFFFF
            v[i] = (s / 4294967296.0) * 2.0 - 1.0
        v *= np.exp(-np.arange(n, dtype=np.float64) / k)
        e = float(np.sqrt(np.sum(v * v)))
        ir[ch] = (v / e) if e > 0 else 0.0
    return ir


def _delay_wet(x: np.ndarray, sr: int, t: float, fb: float, mix: float) -> np.ndarray:
    """DelayNode + 反馈:干路之外算出来的那一路。按 d 分块递推,不写 Python 大循环。"""
    d = max(1, int(round(max(0.01, min(2.0, t)) * sr)))
    fb = max(0.0, min(0.92, float(fb)))
    n = len(x)
    if n == 0:
        return np.zeros_like(x)
    m = (n + d - 1) // d
    pad = np.zeros(m * d, dtype=np.float64)
    pad[:n] = x
    B = pad.reshape(m, d)
    # Web Audio 的 DelayNode:输入 = x + fb·(上一段输出),输出 = 输入延后 d。
    # ⇒ Y[k] = B[k-1] + fb·Y[k-1],第 0 段还没东西可延
    Y = np.zeros_like(B)
    for k in range(1, m):
        Y[k] = B[k - 1] + fb * Y[k - 1]
    return (Y.reshape(-1)[:n] * mix).astype(np.float32)


def _per_ch(fn, x: np.ndarray, *a, **kw) -> np.ndarray:
    """把只认单声道的处理(插入链 / VST / 混响延迟)逐声道跑一遍。

    ★ 压缩、限幅这类**非线性**处理按声道各算各的 —— 左右两边的增益会略有差别,
      这正是大多数 DAW 不勾「立体声联动」时的行为。要联动得另写,别在这儿糊。
    """
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        return fn(x, *a, **kw)
    cols = [np.asarray(fn(x[:, c], *a, **kw), dtype=np.float32) for c in range(x.shape[1])]
    return np.stack(cols, axis=1)


def _apply_fx(x: np.ndarray, sr: int, fx: dict) -> np.ndarray:
    """干*(1-rv-dl) + 混响*rv + 延迟*dl。参数与 engine.js 完全一致。"""
    from scipy.signal import fftconvolve
    rv = fx.get("reverb") or {}
    dl = fx.get("delay") or {}
    rv_mix = max(0.0, min(1.0, float(rv.get("mix") or 0)))
    dl_mix = max(0.0, min(1.0, float(dl.get("mix") or 0)))
    if rv_mix <= 0.001 and dl_mix <= 0.001:
        return x
    dry = max(0.0, 1.0 - rv_mix - dl_mix)
    y = x.astype(np.float64) * dry
    if rv_mix > 0.001:
        size = max(0.2, min(4.0, float(rv.get("size") or 1.6)))
        ir = _make_ir(sr, size, max(0.1, min(3.0, size * 0.45)))
        # 单声道源:直接拿单声道 IR 卷积(两条声道 IR 一样,没必要分两次)
        w = fftconvolve(x.astype(np.float64), ir.mean(axis=0))[: len(x)]
        y += w * rv_mix
    if dl_mix > 0.001:
        y += _delay_wet(x.astype(np.float64), sr, dl.get("time") or 0.25,
                        dl.get("fb") or 0.0, dl_mix)
    return y.astype(np.float32)


# ---------------------------------------------------------------------------
# 母带:LUFS(BS.1770 简化)+ 5ms 前瞻软限幅
# ---------------------------------------------------------------------------
def k_weight(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """BS.1770 的两级 K 加权(48k 系数;本工程导出固定 48k)。"""
    from scipy.signal import lfilter
    if sr != 48000:
        # 非 48k 时用同样的 48k 系数会偏一点点 —— 明说,不装作没差
        pass
    b1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    a1 = [1.0, -1.69065929318241, 0.73248077421585]
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, -1.99004745483398, 0.99007225036621]
    z = lfilter(b1, a1, y, axis=0)
    return lfilter(b2, a2, z, axis=0)


def integrated_lufs(y: np.ndarray, sr: int = SR) -> float:
    """积分响度:400ms 块 / 75% 重叠 / -70 绝对门 / -10 相对门。"""
    if y.ndim == 1:
        y = y[:, None]
    k = k_weight(np.asarray(y, dtype=np.float64), sr)
    block = max(1, int(round(0.4 * sr)))
    step = max(1, int(round(0.1 * sr)))
    if len(k) < block:
        return -70.0
    idx = range(0, len(k) - block + 1, step)
    ms = np.array([float(np.mean(k[i:i + block] ** 2, axis=0).sum()) for i in idx])
    if not len(ms):
        return -70.0
    l = -0.691 + 10.0 * np.log10(ms + 1e-12)
    l = l[l > -70.0]
    if not len(l):
        return -70.0
    rel = -0.691 + 10.0 * np.log10(np.mean(10 ** ((l + 0.691) / 10.0))) - 10.0
    l2 = l[l > rel]
    if not len(l2):
        l2 = l
    return float(-0.691 + 10.0 * np.log10(np.mean(10 ** ((l2 + 0.691) / 10.0))))


def limit_peaks(y: np.ndarray, sr: int = SR, ceiling_db: float = -0.3,
                lookahead: float = 0.005) -> np.ndarray:
    """5ms 前瞻软限幅:先把该压的地方提前压下去,再平滑;★ 不硬削平波形。"""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    th = 10 ** (ceiling_db / 20.0)
    look = max(1, int(round(lookahead * sr)))
    a = np.abs(np.asarray(y, dtype=np.float64))
    peak = maximum_filter1d(a.max(axis=1), size=2 * look + 1, mode="nearest")
    gt = np.minimum(1.0, th / np.maximum(peak, 1e-9))
    g = np.minimum(1.0, uniform_filter1d(gt, size=look + 1, mode="nearest"))
    out = y * g[:, None]
    p = float(np.max(np.abs(out))) if out.size else 0.0
    if p > th:                      # 最后一道保险(通常不会触发)
        out = out * (th / p)
    return out.astype(np.float32)


def master_to(y: np.ndarray, sr: int = SR, target: float = -14.0,
              ceiling_db: float = -1.0) -> tuple[np.ndarray, dict]:
    """母带:按 LUFS 定响度,再按**真峰**限幅。

    ★ 以前天花板是 -0.3 dBFS 的**采样峰** —— 流媒体转 AAC/Opus 会重建波形,
      采样之间的峰能再冒出来零点几 dB,到平台上就削顶。发行线是 -1 dBTP。
    """
    from studio_release import limit_true_peak, true_peak_db

    before = integrated_lufs(y, sr)
    gain = 10 ** ((float(target) - before) / 20.0)
    src = np.asarray(y, dtype=np.float64)
    y2 = limit_true_peak(src * gain, sr, ceiling_db)
    # ★ 限幅会吃掉一点响度(峰值材料吃得多)。再补一次增益重限一遍,
    #   让成品落在目标上;补两次还到不了就照实报,不硬凑。
    after = integrated_lufs(y2, sr)
    for _ in range(2):
        miss = float(target) - after
        if abs(miss) <= 0.3:
            break
        gain *= 10 ** (miss / 20.0)
        y2 = limit_true_peak(src * gain, sr, ceiling_db)
        after = integrated_lufs(y2, sr)
    return y2, {"lufs_before": round(before, 2),
                "gain_db": round(20.0 * math.log10(max(gain, 1e-9)), 2),
                "lufs_after": round(after, 2),
                "true_peak_db": round(float(true_peak_db(y2, sr)), 2),
                "ceiling_db": round(float(ceiling_db), 2)}


def _apply_vst(vst, tbuf: np.ndarray, sr: int, fx, label: str) -> np.ndarray:
    """轨道增益 + 内置链之后、旧混响/延迟之前,过这一轨的 VST 链。

    ★ 有 VST 却没给宿主 ⇒ 抛错。绝不静默当成「没有插件」继续混 ——
      那样导出的文件听起来正常,其实是错的。
    """
    if not isinstance(fx, dict):
        return tbuf
    slots = fx.get("vst")
    if not isinstance(slots, list) or not slots:
        return tbuf
    if vst is None:
        raise studio_vst.VstUnavailable(
            f"轨道「{label}」有 VST 插件,但这次渲染没有启动 VST 宿主")
    return vst.render(tbuf, sr, slots, label)


def _check_vst(vst, fx, label: str) -> None:
    """空轨也要查一遍。

    ★ 这一轨一个 clip 都没有时,`_apply_vst` 根本不会被调用;插件路径坏了
      就悄悄「成功」出一条静音 —— 正是「报错停下」要拦的那种。
      这里只做**能离线判定**的检查(宿主在不在、路径还存不存在),
      插件自身加载失败仍会在真正处理音频时暴露。
    """
    if not isinstance(fx, dict):
        return
    slots = fx.get("vst")
    if not isinstance(slots, list) or not slots:
        return
    if vst is None:
        raise studio_vst.VstUnavailable(
            f"轨道「{label}」有 VST 插件,但这次渲染没有启动 VST 宿主")
    studio_vst.sanitize_slots(slots)


def mix_project(project: dict, sr_out: int = SR, normalize: bool = False,
                start: float = 0.0, end: float | None = None,
                loudness: float | None = None, vst=None,
                ceiling: float = -1.0,
                normalize_db: float | None = None) -> tuple[np.ndarray, dict]:
    """把工程混成一条立体声。

    ★ 用的是**和播放同一批缓存片段、同一套包络公式**,所以导出的和听到的是同一个东西。
    """
    ensure_dirs()
    if end is None:
        end = project_end(project)
    start = max(0.0, float(start))
    end = max(start, float(end))
    n = int(round((end - start) * sr_out))
    out = np.zeros((n, 2), dtype=np.float32)
    if n <= 0:
        # ★ 空工程也要查插件:路径坏了不能因为「没音频」就静默成功
        _solo = any(t.get("solo") for t in (project.get("tracks") or []))
        for _tr in project.get("tracks") or []:
            if _tr.get("mute") or (_solo and not _tr.get("solo")):
                continue
            _check_vst(vst, _tr.get("fx"),
                       str(_tr.get("name") or _tr.get("id") or "轨道"))
        return out, {"sec": 0.0, "peak": 0.0, "clips": 0, "missing": []}

    tracks = project.get("tracks") or []
    any_solo = any(t.get("solo") for t in tracks)
    memo: dict = {}
    missing: list[str] = []
    placed = 0

    for tr in tracks:
        if tr.get("mute") or (any_solo and not tr.get("solo")):
            continue
        g = float(tr.get("gain", 1) or 0)
        pan = max(-1.0, min(1.0, float(tr.get("pan", 0) or 0)))

        # ★ 先攒进本轨的临时缓冲,等整轨 FX 走完再乘轨道增益、再分左右 ——
        #   这个顺序和 engine.js 的 g → FX → pan 逐节点对应。
        tbuf = None
        n_here = 0
        # 这一轨是不是「真立体声」—— 决定后面用平衡律还是等功率声像律。
        # ★ 浏览器用的是 StereoPannerNode:StereoPannerNode 对立体声输入和单声道输入
        #   用的是**两套不同的公式**。导出必须照抄,否则「听到的 ≠ 导出的」。
        stereo_track = False

        for c in tr.get("clips") or []:
            y = _clip_piece(project, c, sr_out, memo, missing)
            if y is None:
                continue
            src = (project.get("sources") or {}).get(c.get("sourceId")) or {}
            if src.get("path") and _src_channels((ROOT / src["path"]).resolve(), memo) > 1:
                stereo_track = True
            y = _apply_env(y, sr_out, float(c.get("gain", 1) or 0),
                           float(c.get("fadeIn", 0) or 0),
                           float(c.get("fadeOut", 0) or 0))
            i0 = int(round((float(c.get("start") or 0) - start) * sr_out))
            a = max(0, i0)
            b = min(n, i0 + len(y))
            if b <= a:
                continue
            if tbuf is None:
                tbuf = np.zeros((n, 2), dtype=np.float32)
            tbuf[a:b] += y[a - i0: b - i0]
            n_here += 1

        for nt in tr.get("notes") or []:
            y = _note_piece(tr, nt, sr_out, memo)
            if y is None:
                continue
            i0 = int(round((float(nt.get("start") or 0) - start) * sr_out))
            a = max(0, i0)
            b = min(n, i0 + len(y))
            if b <= a:
                continue
            if tbuf is None:
                tbuf = np.zeros((n, 2), dtype=np.float32)
            tbuf[a:b] += y[a - i0: b - i0]
            n_here += 1

        if tbuf is None:
            # ★ 空轨不处理音频,但插件坏了也必须报错(见 _check_vst)
            _check_vst(vst, tr.get("fx"), str(tr.get("name") or tr.get("id") or "轨道"))
            continue
        # ★ 顺序必须和 engine.js 的节点图一模一样:
        #   轨道增益 → 插入链(worklet) → 旧混响/延迟 → 声像。
        #   压缩/失真/限幅是**非线性**的,增益放前放后出来的不是一个东西。
        tbuf *= g
        fx = tr.get("fx") or {}
        if isinstance(fx, dict) and fx.get("chain"):
            tbuf = _per_ch(studio_fx.apply_chain, tbuf, sr_out, fx["chain"])
        _label = str(tr.get("name") or tr.get("id") or "轨道")
        tbuf = _per_ch(lambda col: _apply_vst(vst, col, sr_out, fx, _label), tbuf)
        if fx:
            tbuf = _per_ch(_apply_fx, tbuf, sr_out, fx)

        # ---- 声像:照抄 Web Audio 的 StereoPannerNode(engine.js 用的就是它)----
        if stereo_track:
            # 立体声输入:居中 = 原样;推到一边 = 把另一边并过来、把本边压掉
            x = pan + 1.0 if pan <= 0 else pan
            cth, sth = math.cos(x * math.pi / 2.0), math.sin(x * math.pi / 2.0)
            L, R = tbuf[:, 0], tbuf[:, 1]
            if pan <= 0:
                out[:, 0] += L + R * cth
                out[:, 1] += R * sth
            else:
                out[:, 0] += L * cth
                out[:, 1] += R + L * sth
        else:
            # 单声道输入:等功率(居中时两边各 0.707,总功率不变)
            th = (pan + 1.0) * math.pi / 4.0
            out[:, 0] += tbuf[:, 0] * math.cos(th)
            out[:, 1] += tbuf[:, 1] * math.sin(th)
        placed += n_here

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    normalized = False
    loud_rep = None
    if normalize and peak > 1e-9:
        from studio_release import true_peak_db as _tpd
        # ★ 按「采样峰和真峰里更紧的那个」缩 —— 只卡采样峰会留下 dBTP 超标。
        tgt_db = float(ceiling)                  # 发行线(-1 dBTP;mp3 更紧)
        exact = False
        if normalize_db is not None and not loudness:
            # 交出去的「母带前混音」:不压响度,峰**对齐**到指定余量(比如 -6 dBFS)
            tgt_db = float(normalize_db)
            exact = True                         # ★ 余量是双向的:小了也要抬上来
        target = 10 ** (tgt_db / 20.0)
        cur = max(peak, 10 ** (_tpd(out, sr_out) / 20.0))
        if exact or cur > target:
            out *= target / max(cur, 1e-12)
            normalized = True
    if loudness:
        # ★ 母带在归一化之后:先定响度,再用前瞻限幅兜峰值
        out, loud_rep = master_to(out, sr_out, float(loudness), float(ceiling))
        normalized = True
    np.clip(out, -1.0, 1.0, out=out)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    rep = {"sec": round(n / sr_out, 3),
           "peak": round(peak, 5),
           "peak_db": round(20 * math.log10(max(peak, 1e-9)), 2),
           "normalized": normalized, "clips": placed,
           "missing": missing[:8]}
    if loud_rep:
        rep["loudness"] = loud_rep
    return out, rep


def render_stems(project: dict, sr_out: int = SR, stage: str = "pre",
                 start: float = 0.0, end: float | None = None, vst=None
                 ) -> tuple[list[dict], list[str]]:
    """分轨导出:每轨一条波形 (n, 2)(给 VST 宿主 / 别的 DAW 用)。

    ★ `stage="pre"`  = 插入链之前(含轨道增益、淡入淡出,**不含**声像与旧混响/延迟)
      `stage="post"` = 插入链之后
    ★ 处理顺序、包络公式、采样对齐和 `mix_project` 完全同一套 —— 所以
      「pre 的各轨按增益/声像相加」必须等于「关掉链和 FX 的主混音」,这是可对账的。
    ★ 立体声源的宽度原样保留;单声道轨左右两份相同。
    """
    ensure_dirs()
    stage = "post" if str(stage).lower() == "post" else "pre"
    if end is None:
        end = project_end(project)
    start = max(0.0, float(start))
    end = max(start, float(end))
    n = int(round((end - start) * sr_out))
    out: list[dict] = []
    if n <= 0:
        return out, []

    tracks = project.get("tracks") or []
    any_solo = any(t.get("solo") for t in tracks)
    memo: dict = {}
    missing: list[str] = []

    for idx, tr in enumerate(tracks):
        if tr.get("mute") or (any_solo and not tr.get("solo")):
            continue
        g = float(tr.get("gain", 1) or 0)
        tbuf = np.zeros((n, 2), dtype=np.float32)
        has = False
        for c in tr.get("clips") or []:
            y = _clip_piece(project, c, sr_out, memo, missing)
            if y is None:
                continue
            y = _apply_env(y, sr_out, float(c.get("gain", 1) or 0),
                           float(c.get("fadeIn", 0) or 0),
                           float(c.get("fadeOut", 0) or 0))
            i0 = int(round((float(c.get("start") or 0) - start) * sr_out))
            a = max(0, i0)
            b = min(n, i0 + len(y))
            if b <= a:
                continue
            tbuf[a:b] += y[a - i0: b - i0]
            has = True
        for nt in tr.get("notes") or []:
            y = _note_piece(tr, nt, sr_out, memo)
            if y is None:
                continue
            i0 = int(round((float(nt.get("start") or 0) - start) * sr_out))
            a = max(0, i0)
            b = min(n, i0 + len(y))
            if b <= a:
                continue
            tbuf[a:b] += y[a - i0: b - i0]
            has = True
        if not has:
            continue
        tbuf *= g
        fx = tr.get("fx") or {}
        chain = fx.get("chain") if isinstance(fx, dict) else None
        if stage == "post" and chain:
            tbuf = _per_ch(studio_fx.apply_chain, tbuf, sr_out, chain)
        if stage == "post":
            _label = str(tr.get("name") or tr.get("id") or "轨道")
            tbuf = _per_ch(lambda col: _apply_vst(vst, col, sr_out, fx, _label), tbuf)
        tbuf = np.nan_to_num(tbuf, nan=0.0, posinf=4.0, neginf=-4.0)
        out.append({
            "id": str(tr.get("id") or f"tr_{idx + 1}"),
            "name": str(tr.get("name") or f"轨道 {idx + 1}"),
            "audio": tbuf.astype(np.float32),
        })
    return out, missing[:8]


def render_track_post(project: dict, track: dict, sr_out: int = SR,
                      start: float = 0.0, end: float | None = None,
                      vst=None) -> np.ndarray:
    """渲染**一条轨**的「增益 → 内置链 → VST」结果 (n, 2)。

    ★ 冻结轨用:忽略 mute/solo(它只是一条音频),不含声像,也不含旧混响/延迟
      —— 和 `mix_project` 里这一轨进入声像之前的那一步逐点一致。
    ★ 冻结的是立体声:源宽就宽,不会因为冻结被并窄。
    """
    ensure_dirs()
    if end is None:
        end = project_end(project)
    start = max(0.0, float(start))
    end = max(start, float(end))
    n = int(round((end - start) * sr_out))
    label = str(track.get("name") or track.get("id") or "轨道")
    if n <= 0:
        # ★ 空工程/空轨也要查插件(见 _check_vst)
        _check_vst(vst, track.get("fx"), label)
        return np.zeros((0, 2), dtype=np.float32)
    memo: dict = {}
    missing: list[str] = []
    tbuf = np.zeros((n, 2), dtype=np.float32)
    has = False
    for c in track.get("clips") or []:
        y = _clip_piece(project, c, sr_out, memo, missing)
        if y is None:
            continue
        y = _apply_env(y, sr_out, float(c.get("gain", 1) or 0),
                       float(c.get("fadeIn", 0) or 0),
                       float(c.get("fadeOut", 0) or 0))
        i0 = int(round((float(c.get("start") or 0) - start) * sr_out))
        a = max(0, i0)
        b = min(n, i0 + len(y))
        if b <= a:
            continue
        tbuf[a:b] += y[a - i0: b - i0]
        has = True
    for nt in track.get("notes") or []:
        y = _note_piece(track, nt, sr_out, memo)
        if y is None:
            continue
        i0 = int(round((float(nt.get("start") or 0) - start) * sr_out))
        a = max(0, i0)
        b = min(n, i0 + len(y))
        if b <= a:
            continue
        tbuf[a:b] += y[a - i0: b - i0]
        has = True
    if not has:
        # ★ 空轨不处理音频,但插件坏了也必须报错(见 _check_vst)
        _check_vst(vst, track.get("fx"),
                   str(track.get("name") or track.get("id") or "轨道"))
        return tbuf
    tbuf *= float(track.get("gain", 1) or 0)
    fx = track.get("fx") or {}
    if isinstance(fx, dict) and fx.get("chain"):
        tbuf = _per_ch(studio_fx.apply_chain, tbuf, sr_out, fx["chain"])
    tbuf = _per_ch(lambda col: _apply_vst(vst, col, sr_out, fx, label), tbuf)
    return np.nan_to_num(tbuf, nan=0.0, posinf=4.0, neginf=-4.0).astype(np.float32)


def check_pitch(path: Path, red_cents: float = 30.0, yellow_cents: float = 100.0) -> dict:
    """按块算「离调内最近的音差多少音分」。

    ★ 只**标**两档:红 = 该修(30~100 音分),黄 = 偏太多,可能根本是另一个音。
      黄的一律不给建议 —— 硬拽过去就是制造错音。
    ★ 建议的修法是**整块常数平移**(semis + cents),块内的颤音原样保留。
    """
    # ★ 阈值必须进 key:30 / 45 音分是两套标记结果,混用会读到旧结论
    key = hashlib.sha1(f"{path}|{path.stat().st_size}:{int(path.stat().st_mtime)}"
                       f"|r{red_cents:g}|y{yellow_cents:g}"
                       .encode("utf-8")).hexdigest()[:16]
    cache = CACHE / f"pitch_{key}.json"
    if cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            d["cached"] = True
            return d
        except Exception:                  # noqa: BLE001
            pass

    x, sr = load_mono(path)
    seg = tuning.segment(x, sr)
    blocks = seg.get("blocks") or []
    notes = [(b["f0"], b["t1"] - b["t0"]) for b in blocks
             if b.get("kind") == "note" and b.get("f0")]

    if notes:
        root_name, mode, conf = tuning.detect_key(notes)
        root = _NOTE_NAMES.index(root_name) if root_name in _NOTE_NAMES else 0
    else:
        root_name, mode, conf, root = "C", "major", 0.0, 0
    pcs = {(p + root) % 12 for p in (_MINOR if mode == "minor" else _MAJOR)}

    out = []
    red = yellow = 0
    for b in blocks:
        if b.get("kind") != "note" or not b.get("f0"):
            continue
        midi = 69.0 + 12.0 * math.log2(max(float(b["f0"]), 1e-3) / 440.0)
        m = nearest_scale_midi(midi, pcs)
        dev = (midi - m) * 100.0
        status = "ok"
        sug = None
        if abs(dev) > yellow_cents:
            status = "yellow"
            yellow += 1
        elif abs(dev) > red_cents:
            status = "red"
            red += 1
            shift = -dev
            semis = int(round(shift / 100.0))
            cents = shift - semis * 100.0
            sug = {"semis": max(-24, min(24, semis)),
                   "cents": round(max(-100.0, min(100.0, cents)), 1)}
        out.append({
            "blockId": b.get("id"), "t0": b.get("t0"), "t1": b.get("t1"),
            "f0": round(float(b["f0"]), 2), "note": b.get("note"),
            "target": f"{_NOTE_NAMES[m % 12]}{m // 12 - 1}",
            "cents": round(dev, 1), "status": status, "suggest": sug,
        })

    res = {"key": key, "root": root_name, "mode": mode, "conf": round(float(conf), 2),
           "red": red, "yellow": yellow, "blocks": out,
           "voiced_s": round(sum(n[1] for n in notes), 2), "cached": False}
    try:
        cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return res


def project_end(project: dict) -> float:
    """工程里最靠后的一个东西的结束时间(秒)。"""
    end = 0.0
    for tr in project.get("tracks") or []:
        for c in tr.get("clips") or []:
            end = max(end, float(c.get("start", 0)) + float(c.get("length", 0)))
        for nt in tr.get("notes") or []:
            end = max(end, float(nt.get("start", 0)) + float(nt.get("length", 0)))
    return end
