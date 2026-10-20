"""SingMic Studio —— 每轨插入效果链(内置插件)。

★ 这个文件有两份实现之一:另一份是 `web/studio/fxworklet.js`。
  两边的算法、默认值、参数范围**必须逐项一致**,否则「听到的 = 导出的」这条就破了。
  参数表只在这里写一份,前端通过 `/api/studio/fx_types` 拿,不许再抄。

★ 所有效果器都是**单声道、无前瞻**;参数在导出时是**稳态值**,
  浏览器侧为了防拉链会做 20ms 平滑,静止后两边一致。

★ 这里只用 numpy/scipy,不 import FastAPI,方便单测直接调。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

MAX_CHAIN = 24

# ---------------------------------------------------------------------------
# 参数表(唯一真相)
# ---------------------------------------------------------------------------
# type: "float" | "enum"
# curve: "lin" | "log"(只影响前端滑杆手感,不影响算法)
FX_TYPES: dict[str, dict[str, Any]] = {
    "filter": {
        "name": "滤波", "group": "滤波", "color": "#3f7fbf",
        "params": [
            {"key": "mode", "label": "类型", "type": "enum", "default": "lowpass",
             "options": [{"value": "lowpass", "label": "低通"},
                         {"value": "highpass", "label": "高通"},
                         {"value": "bandpass", "label": "带通"}]},
            {"key": "cutoff", "label": "截止", "type": "float", "min": 20, "max": 20000,
             "default": 1000, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "q", "label": "谐振", "type": "float", "min": 0.1, "max": 12,
             "default": 0.707, "step": 0.01, "unit": "Q", "curve": "log"},
        ],
    },
    "eq3": {
        "name": "3 段 EQ", "group": "滤波", "color": "#3f7fbf",
        "params": [
            {"key": "lowFreq", "label": "低架频率", "type": "float", "min": 40, "max": 800,
             "default": 120, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "lowGain", "label": "低架", "type": "float", "min": -18, "max": 18,
             "default": 0, "step": 0.1, "unit": "dB"},
            {"key": "midFreq", "label": "中频", "type": "float", "min": 200, "max": 8000,
             "default": 1000, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "midQ", "label": "中频 Q", "type": "float", "min": 0.3, "max": 8,
             "default": 1, "step": 0.01, "unit": "Q", "curve": "log"},
            {"key": "midGain", "label": "中频增益", "type": "float", "min": -18, "max": 18,
             "default": 0, "step": 0.1, "unit": "dB"},
            {"key": "highFreq", "label": "高架频率", "type": "float", "min": 2000, "max": 16000,
             "default": 6000, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "highGain", "label": "高架", "type": "float", "min": -18, "max": 18,
             "default": 0, "step": 0.1, "unit": "dB"},
        ],
    },
    "compressor": {
        "name": "压缩", "group": "动态", "color": "#bf8a3f",
        "params": [
            {"key": "threshold", "label": "阈值", "type": "float", "min": -60, "max": 0,
             "default": -18, "step": 0.5, "unit": "dB"},
            {"key": "ratio", "label": "比率", "type": "float", "min": 1, "max": 20,
             "default": 3, "step": 0.1, "unit": ":1", "curve": "log"},
            {"key": "attack", "label": "启动", "type": "float", "min": 0.5, "max": 100,
             "default": 12, "step": 0.5, "unit": "ms", "curve": "log"},
            {"key": "release", "label": "释放", "type": "float", "min": 10, "max": 1000,
             "default": 140, "step": 1, "unit": "ms", "curve": "log"},
            {"key": "knee", "label": "软拐点", "type": "float", "min": 0, "max": 24,
             "default": 6, "step": 0.5, "unit": "dB"},
            {"key": "makeup", "label": "补偿增益", "type": "float", "min": 0, "max": 24,
             "default": 0, "step": 0.1, "unit": "dB"},
        ],
    },
    "limiter": {
        "name": "限幅", "group": "动态", "color": "#bf8a3f",
        "params": [
            {"key": "ceiling", "label": "天花板", "type": "float", "min": -12, "max": 0,
             "default": -1, "step": 0.1, "unit": "dB"},
            {"key": "release", "label": "释放", "type": "float", "min": 5, "max": 500,
             "default": 50, "step": 1, "unit": "ms", "curve": "log"},
        ],
    },
    "distortion": {
        "name": "失真", "group": "失真", "color": "#bf3f5f",
        "params": [
            {"key": "drive", "label": "驱动", "type": "float", "min": 1, "max": 30,
             "default": 4, "step": 0.1, "unit": "×", "curve": "log"},
            {"key": "tone", "label": "音色", "type": "float", "min": 200, "max": 18000,
             "default": 8000, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "mix", "label": "干湿", "type": "float", "min": 0, "max": 1,
             "default": 1, "step": 0.01, "unit": ""},
        ],
    },
    "chorus": {
        "name": "合唱", "group": "调制", "color": "#7f3fbf",
        "params": [
            {"key": "rate", "label": "速率", "type": "float", "min": 0.05, "max": 8,
             "default": 0.5, "step": 0.01, "unit": "Hz", "curve": "log"},
            {"key": "depth", "label": "深度", "type": "float", "min": 0, "max": 15,
             "default": 6, "step": 0.1, "unit": "ms"},
            {"key": "base", "label": "基础延迟", "type": "float", "min": 5, "max": 30,
             "default": 12, "step": 0.1, "unit": "ms"},
            {"key": "mix", "label": "干湿", "type": "float", "min": 0, "max": 1,
             "default": 0.5, "step": 0.01, "unit": ""},
        ],
    },
    "flanger": {
        "name": "镶边", "group": "调制", "color": "#7f3fbf",
        "params": [
            {"key": "rate", "label": "速率", "type": "float", "min": 0.05, "max": 8,
             "default": 0.3, "step": 0.01, "unit": "Hz", "curve": "log"},
            {"key": "depth", "label": "深度", "type": "float", "min": 0.1, "max": 8,
             "default": 2, "step": 0.1, "unit": "ms"},
            {"key": "base", "label": "基础延迟", "type": "float", "min": 0.5, "max": 5,
             "default": 1.5, "step": 0.1, "unit": "ms"},
            {"key": "feedback", "label": "反馈", "type": "float", "min": 0, "max": 0.9,
             "default": 0.5, "step": 0.01, "unit": ""},
            {"key": "mix", "label": "干湿", "type": "float", "min": 0, "max": 1,
             "default": 0.5, "step": 0.01, "unit": ""},
        ],
    },
    "tremolo": {
        "name": "颤音", "group": "调制", "color": "#7f3fbf",
        "params": [
            {"key": "rate", "label": "速率", "type": "float", "min": 0.1, "max": 12,
             "default": 4, "step": 0.01, "unit": "Hz", "curve": "log"},
            {"key": "depth", "label": "深度", "type": "float", "min": 0, "max": 1,
             "default": 0.6, "step": 0.01, "unit": ""},
        ],
    },
    "bitcrush": {
        "name": "比特压缩", "group": "破坏", "color": "#5f5f5f",
        "params": [
            {"key": "bits", "label": "位深", "type": "float", "min": 4, "max": 16,
             "default": 8, "step": 1, "unit": "bit"},
            {"key": "crushHz", "label": "采样率", "type": "float", "min": 500, "max": 24000,
             "default": 6000, "step": 1, "unit": "Hz", "curve": "log"},
            {"key": "mix", "label": "干湿", "type": "float", "min": 0, "max": 1,
             "default": 1, "step": 0.01, "unit": ""},
        ],
    },
}


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
def default_params(type_name: str) -> dict[str, Any]:
    spec = FX_TYPES.get(type_name) or {}
    return {p["key"]: p.get("default") for p in spec.get("params", [])}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def sanitize_slot(slot: Any, index: int = 0) -> dict[str, Any] | None:
    """把一个槽位收拾成可执行的样子;未知类型返回 None。"""
    if not isinstance(slot, dict):
        return None
    t = str(slot.get("type") or "")
    spec = FX_TYPES.get(t)
    if not spec:
        return None
    raw = slot.get("params") if isinstance(slot.get("params"), dict) else {}
    params: dict[str, Any] = {}
    for p in spec["params"]:
        k = p["key"]
        v = raw.get(k, p.get("default"))
        if p["type"] == "enum":
            ok = {o["value"] for o in p.get("options", [])}
            params[k] = v if v in ok else p.get("default")
        else:
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = float(p.get("default") or 0)
            if not math.isfinite(f):
                f = float(p.get("default") or 0)
            lo = float(p.get("min", f))
            hi = float(p.get("max", f))
            params[k] = _clamp(f, lo, hi)
    sid = slot.get("id")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        sid = f"fx_auto_{index}"
    return {"id": sid, "type": t, "enabled": bool(slot.get("enabled", True)),
            "params": params}


def sanitize_chain(chain: Any) -> list[dict[str, Any]]:
    """★ 未知类型**跳过**(不删用户的原文 —— 那是前端的事),链长封顶,id 去重。"""
    if not isinstance(chain, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(chain):
        slot = sanitize_slot(raw, i)
        if slot is None:
            continue
        base = slot["id"]
        sid = base
        n = 1
        while sid in seen:
            sid = f"{base}_{n}"
            n += 1
        slot["id"] = sid
        seen.add(sid)
        out.append(slot)
        if len(out) >= MAX_CHAIN:
            break
    return out


# ---------------------------------------------------------------------------
# 基础 DSP(与 fxworklet.js 同一套公式)
# ---------------------------------------------------------------------------
def _rbj(kind: str, sr: int, freq: float, q: float, gain_db: float = 0.0):
    """RBJ Audio EQ Cookbook 系数,返回已按 a0 归一的 (b, a)。"""
    freq = _clamp(float(freq), 10.0, sr * 0.49)
    w0 = 2.0 * math.pi * freq / sr
    cw, sw = math.cos(w0), math.sin(w0)
    if kind in ("lowpass", "highpass"):
        q = max(0.05, min(20.0, float(q)))
        alpha = sw / (2.0 * q)
        if kind == "lowpass":
            b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        else:
            b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind == "bandpass":
        q = max(0.05, min(20.0, float(q)))
        alpha = sw / (2.0 * q)
        b0, b1, b2 = alpha, 0.0, -alpha
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind == "peaking":
        q = max(0.05, min(20.0, float(q)))
        A = 10 ** (float(gain_db) / 40.0)
        alpha = sw / (2.0 * q)
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif kind == "lowshelf":
        A = 10 ** (float(gain_db) / 40.0)
        alpha = sw / 2 * math.sqrt(2.0)      # S = 1
        sa = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + sa)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - sa)
        a0 = (A + 1) + (A - 1) * cw + sa
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - sa
    elif kind == "highshelf":
        A = 10 ** (float(gain_db) / 40.0)
        alpha = sw / 2 * math.sqrt(2.0)
        sa = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) + (A - 1) * cw + sa)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - sa)
        a0 = (A + 1) - (A - 1) * cw + sa
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - sa
    else:
        raise ValueError(kind)
    return (np.array([b0, b1, b2], dtype=np.float64) / a0,
            np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64))


def _lfilter(b, a, x: np.ndarray, zi: np.ndarray | None = None) -> np.ndarray:
    from scipy.signal import lfilter
    if zi is None:
        return lfilter(b, a, x)
    y, _ = lfilter(b, a, x, zi=zi)
    return y


def _env_pair(absx: np.ndarray, sr: int, attack_ms: float, release_ms: float):
    """双一阶包络:快的管启动、慢的管释放,取两者最大值。

    ★ 这样两边都能纯向量化(浏览器那边就是两条一阶递归),不用滑窗最大值。
    """
    aa = math.exp(-1.0 / max(1e-4, float(attack_ms) * 0.001 * sr))
    ar = math.exp(-1.0 / max(1e-4, float(release_ms) * 0.001 * sr))
    fast = _lfilter([1 - aa], [1, -aa], absx)
    slow = _lfilter([1 - ar], [1, -ar], absx)
    return np.maximum(fast, slow)


def _mod_delay(x: np.ndarray, sr: int, rate: float, base_ms: float,
               depth_ms: float, phase0: float = 0.0) -> np.ndarray:
    """调制延迟 + 线性插值;延迟 = base + depth·(0.5+0.5·sin)。"""
    n = np.arange(len(x), dtype=np.float64)
    d = (base_ms + depth_ms * (0.5 + 0.5 * np.sin(
        2 * math.pi * float(rate) * n / sr + phase0))) * sr / 1000.0
    maxd = int(math.ceil((base_ms + depth_ms) * sr / 1000.0)) + 2
    buf = np.concatenate([np.zeros(maxd, dtype=np.float64), x])
    r = n + maxd - d
    i0 = np.floor(r).astype(np.int64)
    frac = r - i0
    return buf[i0] * (1 - frac) + buf[i0 + 1] * frac


def _comb_feedback(y: np.ndarray, d: int, fb: float) -> np.ndarray:
    """固定延迟 d 的反馈:out[n] = y[n] + fb·out[n-d]。按 d 分块递推。"""
    d = max(1, int(d))
    n = len(y)
    if n == 0:
        return y
    m = (n + d - 1) // d
    pad = np.zeros(m * d, dtype=np.float64)
    pad[:n] = y
    B = pad.reshape(m, d)
    Y = np.empty_like(B)
    Y[0] = B[0]
    for k in range(1, m):
        Y[k] = B[k] + fb * Y[k - 1]
    return Y.reshape(-1)[:n]


# ---------------------------------------------------------------------------
# 各个效果器
# ---------------------------------------------------------------------------
def fx_filter(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    b, a = _rbj(str(p["mode"]), sr, float(p["cutoff"]), float(p["q"]))
    return _lfilter(b, a, x)


def fx_eq3(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    y = x
    for kind, f, g, q in (
        ("lowshelf", p["lowFreq"], p["lowGain"], 0.707),
        ("peaking", p["midFreq"], p["midGain"], p["midQ"]),
        ("highshelf", p["highFreq"], p["highGain"], 0.707),
    ):
        if abs(float(g)) < 1e-9:
            continue
        b, a = _rbj(kind, sr, float(f), float(q), float(g))
        y = _lfilter(b, a, y)
    return y


def fx_distortion(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    drive = max(1.0, float(p["drive"]))
    mix = _clamp(float(p["mix"]), 0.0, 1.0)
    th = math.tanh(drive)
    shaped = np.tanh(x * drive) / (th if abs(th) > 1e-9 else 1.0)
    tone = _clamp(float(p["tone"]), 20.0, sr * 0.49)
    a = 1.0 - math.exp(-2.0 * math.pi * tone / sr)
    shaped = _lfilter([a], [1, -(1 - a)], shaped)
    return x * (1 - mix) + shaped * mix


def fx_compressor(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    thr = float(p["threshold"])
    ratio = max(1.0, float(p["ratio"]))
    knee = max(0.0, float(p["knee"]))
    makeup = float(p["makeup"])
    # ★ 整流后的 |x| 经一阶平滑后收敛到**均值**(正弦 2/π),不是峰值。
    #   乘 π/2 把它校准成峰值 —— 否则「阈值 −18dB」对正弦要多 4dB 才动,
    #   和 FL / 常见压缩器的读数对不上。JS 侧同一行,两边必须一起改。
    env = _env_pair(np.abs(x) * (math.pi / 2.0), sr, p["attack"], p["release"])
    level = 20.0 * np.log10(env + 1e-9)
    over = level - thr
    slope = 1.0 - 1.0 / ratio
    if knee <= 0.01:
        gr = np.maximum(0.0, over) * slope
    else:
        half = knee / 2.0
        gr = np.where(
            over <= -half, 0.0,
            np.where(over >= half, over * slope,
                     slope * (over + half) ** 2 / (2.0 * knee)))
    return x * (10 ** ((makeup - gr) / 20.0))


def fx_limiter(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    ceiling = 10 ** (float(p["ceiling"]) / 20.0)
    # ★ 同压缩:π/2 把均值检波校准成峰值,否则单频峰值到 ceiling 也不压
    env = _env_pair(np.abs(x) * (math.pi / 2.0), sr, 1.0, p["release"])
    target = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
    rel = math.exp(-1.0 / max(1e-4, float(p["release"]) * 0.001 * sr))
    smooth = _lfilter([1 - rel], [1, -rel], target, zi=np.array([rel]))
    # ★ 攻击瞬间生效(取较小者),释放才平滑 —— 保证天花板不被冲过去
    return x * np.minimum(target, smooth)


def fx_chorus(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    wet = _mod_delay(x, sr, p["rate"], p["base"], p["depth"])
    mix = _clamp(float(p["mix"]), 0.0, 1.0)
    return x * (1 - mix) + wet * mix


def fx_flanger(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    wet = _mod_delay(x, sr, p["rate"], p["base"], p["depth"])
    fb = _clamp(float(p["feedback"]), 0.0, 0.9)
    if fb > 0.001:
        d = max(1, int(round(float(p["base"]) * sr / 1000.0)))
        wet = _comb_feedback(wet, d, fb)
    mix = _clamp(float(p["mix"]), 0.0, 1.0)
    return x * (1 - mix) + wet * mix


def fx_tremolo(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    n = np.arange(len(x), dtype=np.float64)
    mod = 0.5 + 0.5 * np.sin(2 * math.pi * float(p["rate"]) * n / sr)
    return x * (1.0 - _clamp(float(p["depth"]), 0.0, 1.0) * mod)


def fx_bitcrush(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    bits = int(round(_clamp(float(p["bits"]), 4, 16)))
    hold = max(1, int(round(sr / max(1.0, float(p["crushHz"])))))
    idx = (np.arange(len(x), dtype=np.int64) // hold) * hold
    held = x[idx]
    steps = float(2 ** (bits - 1))
    q = np.floor(held * steps + 0.5) / steps      # ★ 和 JS Math.round 一样:半值向上
    mix = _clamp(float(p["mix"]), 0.0, 1.0)
    return x * (1 - mix) + q * mix


_DISPATCH = {
    "filter": fx_filter,
    "eq3": fx_eq3,
    "compressor": fx_compressor,
    "limiter": fx_limiter,
    "distortion": fx_distortion,
    "chorus": fx_chorus,
    "flanger": fx_flanger,
    "tremolo": fx_tremolo,
    "bitcrush": fx_bitcrush,
}


def apply_chain(x: np.ndarray, sr: int, chain: Any) -> np.ndarray:
    """按顺序过整条链。链为空/全旁通时**原样返回** —— 旧工程逐点不变。"""
    slots = sanitize_chain(chain)
    if not slots:
        return x
    y = np.asarray(x, dtype=np.float64)
    for slot in slots:
        if not slot.get("enabled", True):
            continue
        fn = _DISPATCH.get(slot["type"])
        if fn is None:
            continue
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            y = fn(y, sr, slot["params"])
        y = np.nan_to_num(y, nan=0.0, posinf=4.0, neginf=-4.0)
    return y.astype(np.float32)
