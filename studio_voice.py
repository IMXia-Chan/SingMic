"""AI 补音:把选中那一段「延长 / 缩短」,先用本地颗粒拼接,云端模型可用时再精修。

边界(必须守住):
  * 只发**选中的那一段**(≤15 秒)到**你自己配置的**云服务器;不发工程、不发歌、
    不发文件名、不发路径。云端收到的文件名固定是 `seg.wav`。
  * 云端连不上/超时/返回坏音频 → 自动用本地结果,并把原因带回去(fallback,不静默)。
  * 生成的文件落在 `projects/media/`,名字是 `ext_<hash>.wav`,key 里带源文件指纹
    与模型版本,换模型后不会命中旧文件。
    ★ 不放子目录:媒体接口的 `{name}` 路由不跨 `/`,放子目录浏览器取不到音频。

本地算法(不用神经网络):
  * 缩短:PSOLA 压缩(alpha = 目标/原长),夹在 0.3 以上。
  * 延长:从原段里挑最稳的一小段(f0 方差最小)当「循环体」,按基音周期对齐、
    40ms 等功率交叉淡化地接在后面;抖动比整段硬拉小得多。
  * 两端 5ms 淡入淡出,峰值超 0.999 就整体压到 0.98,长度精确到采样。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

import tuning

HOME_CFG = Path(os.environ.get("SINGMIC_HOME") or (Path.home() / ".singmic"))
CFG_PATH = HOME_CFG / "cloud.json"
GEN_PREFIX = "ext_"

MAX_SEG_S = 15.0          # 只发这么长;超了截断并在 reason 里说明
MAX_OUT_S = 30.0
MIN_RATIO = 0.3           # 本地缩短的下限
MAX_RATIO = 8.0
CONNECT_TIMEOUT = 2.0
TOTAL_TIMEOUT = 60.0
HEALTH_TTL = 30.0

_MODEL = "local-wsola-v1"
_CFG_LOCK = threading.Lock()
_HEALTH = {"t": 0.0, "data": None}


# ---------------------------------------------------------------------------
# 配置:token 只写不读
# ---------------------------------------------------------------------------
def _clean_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://[^\s/]+", url, re.I):
        raise ValueError("地址要以 http:// 或 https:// 开头")
    return url


def load_config() -> dict:
    with _CFG_LOCK:
        try:
            d = json.loads(CFG_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
    return {
        "baseUrl": _clean_base(str(d.get("baseUrl") or "")) if d.get("baseUrl") else "",
        "token": str(d.get("token") or ""),
        "enabled": bool(d.get("enabled", True)),
    }


def save_config(base_url=None, token=None, enabled=None) -> dict:
    with _CFG_LOCK:
        try:
            cur = json.loads(CFG_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            cur = {}
        if base_url is not None:
            cur["baseUrl"] = _clean_base(str(base_url))
        if token is not None:
            t = str(token).strip()
            if t:
                cur["token"] = t
        if enabled is not None:
            cur["enabled"] = bool(enabled)
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CFG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CFG_PATH)
    _HEALTH["t"] = 0.0
    return cfg_view()


def clear_token() -> dict:
    with _CFG_LOCK:
        try:
            cur = json.loads(CFG_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            cur = {}
        cur.pop("token", None)
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
    _HEALTH["t"] = 0.0
    return cfg_view()


def _mask(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 8:
        return "••••"
    return tok[:3] + "••••" + tok[-3:]


def cfg_view() -> dict:
    c = load_config()
    return {"baseUrl": c["baseUrl"], "tokenMasked": _mask(c["token"]),
            "hasToken": bool(c["token"]), "enabled": c["enabled"],
            "configured": bool(c["baseUrl"] and c["token"])}


# ---------------------------------------------------------------------------
# 云端:健康探测(带 30 秒缓存)
# ---------------------------------------------------------------------------
def probe_health(force: bool = False) -> dict:
    c = load_config()
    out = {"ok": False, "configured": bool(c["baseUrl"] and c["token"]),
           "reachable": False, "detail": "", "modelVersion": "", "materialsSec": 0,
           "trainedAt": "", "engine": "", "device": ""}
    if not out["configured"]:
        out["detail"] = "没填云端地址或 token"
        return out
    now = time.time()
    if not force and _HEALTH["data"] is not None and now - _HEALTH["t"] < HEALTH_TTL:
        return dict(_HEALTH["data"])
    req = urllib.request.Request(c["baseUrl"] + "/health", headers={
        "Authorization": "Bearer " + c["token"], "User-Agent": "SingMic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT + 2.0) as r:
            d = json.load(r)
        out.update({
            "ok": bool(d.get("ok", True)),
            "reachable": True,
            "modelVersion": str(d.get("model_version") or ""),
            "materialsSec": int(d.get("materials_sec") or 0),
            "trainedAt": str(d.get("trained_at") or ""),
            "engine": str(d.get("engine") or ""),
            "device": str(d.get("device") or ""),
        })
    except urllib.error.HTTPError as e:
        out["detail"] = "云端回了 %d" % e.code
    except Exception as e:                          # noqa: BLE001
        out["detail"] = _short(e)
    _HEALTH["t"], _HEALTH["data"] = now, dict(out)
    return out


def _short(e) -> str:
    s = str(e) or e.__class__.__name__
    return s[:160]


# ---------------------------------------------------------------------------
# 本地补音
# ---------------------------------------------------------------------------
def _fade(x: np.ndarray, sr: int, s: float = 0.005) -> np.ndarray:
    k = int(min(len(x) // 2, s * sr))
    if k <= 1:
        return x
    w = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
    # ★ (n,1) 的单声道立体声数组要补一维,否则 (k,1) *= (k,) 会广播成 (k,k)
    if x.ndim == 2:
        w = w[:, None]
    x = x.copy()
    x[:k] *= w
    x[-k:] *= w[::-1]
    return x


def _mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 2:
        return x.mean(axis=1)
    return x


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def _psola_multi(x: np.ndarray, sr: int, alpha: float) -> np.ndarray:
    """PSOLA 的立体声包装(逐声道,不共享偏移 —— 只用于兜底)。"""
    if x.ndim == 1:
        return tuning.psola(x, sr, alpha, 1.0)
    return np.stack([tuning.psola(x[:, c], sr, alpha, 1.0)
                     for c in range(x.shape[1])], axis=1)


def _wsola(x: np.ndarray, sr: int, n_target: int) -> np.ndarray:
    """WSOLA 时间伸缩:读指针以 n/n_target 的速度走完整段,颗粒按波形相似度对齐后叠加。

    ★ 以前是「拿最稳的一小段循环贴」。贴片间隔本身会变成一个几赫兹的节拍
      (3 秒素材循环体只有 0.18 秒 = 每秒贴 5~6 次),听感就是「得得得」;
      接缝相位对不上时还会周期性塌音量,就是「卡住」。
    ★ WSOLA 不重复固定的片,只把整段**放慢**,读指针全程只走一遍原音频,
      所以既没有节拍,也不会有二次裁切。立体声共用同一组偏移,声像不会晃。
    """
    x = np.asarray(x, dtype=np.float64)
    one_d = x.ndim == 1
    X = x[:, None] if one_d else x
    n = X.shape[0]
    L = int(round(0.040 * sr))
    L = max(512, L - (L & 1))
    Hs = L // 2
    max_start = n - L
    if max_start <= 0 or Hs <= 0:
        return _psola_multi(x, sr, n_target / max(1, n))

    alpha = n_target / float(n)
    Ha = Hs / alpha
    D = max(8, int(round(0.010 * sr)))          # 对齐搜索半径 ±10ms
    win = np.hanning(L + 1)[:-1]                 # 周期窗:50% 重叠和恒为 1
    mono = X.mean(axis=1)

    out = np.zeros((n_target + L, X.shape[1]))
    pos = 0.0
    prev = 0
    k = 0
    while True:
        base = min(max_start, int(round(pos)))
        if k == 0:
            chosen = base
        else:
            lo = max(0, base - D)
            hi = min(max_start, base + D)
            tgt_src = prev + Hs
            chosen = base
            if hi > lo and tgt_src + Hs <= n:
                tgt = mono[tgt_src:tgt_src + Hs]
                seg = mono[lo:hi + Hs]
                if len(tgt) == Hs and len(seg) >= Hs:
                    cc = np.correlate(seg, tgt, mode="valid")
                    chosen = lo + int(np.argmax(cc))
        o = k * Hs
        out[o:o + L] += X[chosen:chosen + L] * win[:, None]
        prev = chosen
        k += 1
        pos += Ha
        if o >= n_target:
            break
    y = out[:n_target]
    return y[:, 0] if one_d else y


def local_extend(x: np.ndarray, sr: int, target_n: int) -> tuple[np.ndarray, str]:
    """返回 (音频, 说明)。说明非空 = 做了什么妥协。"""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    note = ""
    if target_n == n:
        y = x.copy()
    elif target_n > n:
        y = _wsola(x, sr, target_n)
    else:
        alpha = max(MIN_RATIO, target_n / max(1, n))
        if alpha > 1.0:
            alpha = 1.0
        y = _psola_multi(x, sr, alpha)
        if alpha > target_n / max(1, n) + 1e-9:
            note = "本地最多缩到 %.0f%%,已按 %.0f%% 输出" % (
                MIN_RATIO * 100, alpha * 100)
    y = np.asarray(y, dtype=np.float64)
    if len(y) < target_n:
        y = np.pad(y, (0, target_n - len(y)))
    y = y[:target_n]
    y = _fade(y, sr)
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0.999:
        y *= 0.98 / peak
    return y.astype(np.float32), note


# ---------------------------------------------------------------------------
# 云端调用
# ---------------------------------------------------------------------------
def _wav_bytes(x: np.ndarray, sr: int) -> bytes:
    bio = io.BytesIO()
    sf.write(bio, np.clip(x, -1.0, 1.0), sr, format="WAV", subtype="PCM_16")
    return bio.getvalue()


def _cloud_extend(x: np.ndarray, sr: int, target_sec: float) -> dict:
    """POST 到 /extend。成功返回 {ok, audio, modelVersion};失败 {ok:False, reason}。"""
    c = load_config()
    if not (c["baseUrl"] and c["token"] and c["enabled"]):
        return {"ok": False, "reason": "云端未配置"}
    body = json.dumps({
        "audio_b64": base64.b64encode(_wav_bytes(x, sr)).decode("ascii"),
        "sample_rate": int(sr),
        "target_sec": round(float(target_sec), 3),
    }).encode("utf-8")
    last = ""
    for attempt in range(2):            # 最多 1 次重试
        req = urllib.request.Request(c["baseUrl"] + "/extend", data=body, method="POST",
                                     headers={
                                         "Authorization": "Bearer " + c["token"],
                                         "Content-Type": "application/json",
                                         "User-Agent": "SingMic/1.0",
                                     })
        try:
            with urllib.request.urlopen(req, timeout=TOTAL_TIMEOUT) as r:
                d = json.load(r)
            if not d.get("ok"):
                return {"ok": False, "reason": str(d.get("reason") or "云端没给结果")[:200]}
            raw = base64.b64decode(d.get("wav_b64") or "")
            if not raw:
                return {"ok": False, "reason": "云端返回空音频"}
            y, sr2 = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
            if not np.isfinite(y).all():
                return {"ok": False, "reason": "云端音频里有 NaN"}
            want = int(round(target_sec * sr2))
            if abs(len(y) - want) > max(0.02 * want, 0.05 * sr2):
                return {"ok": False,
                        "reason": "云端长度不对(%.2fs,要 %.2fs)" % (len(y) / sr2, target_sec)}
            return {"ok": True, "audio": y.T if y.shape[1] > 1 else y[:, 0],
                    "sr": int(sr2), "modelVersion": str(d.get("model_version") or "unknown")}
        except urllib.error.HTTPError as e:
            last = "云端回了 %d" % e.code
            if 400 <= e.code < 500:     # 4xx 重试也没用
                break
        except Exception as e:          # noqa: BLE001
            last = _short(e)
        if attempt == 0:
            time.sleep(0.5)
    return {"ok": False, "reason": last or "云端不可达"}


# ---------------------------------------------------------------------------
# 对外:补音
# ---------------------------------------------------------------------------
def _hash_key(rel: str, st: os.stat_result, a: int, b: int,
              target_n: int, engine: str, ver: str) -> str:
    s = "%s|%d|%d|%d|%d|%d|%s|%s" % (
        rel, st.st_size, int(st.st_mtime), a, b, target_n, engine, ver)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def extend(source: Path, rel: str, src_in: float, src_out: float, target_sec: float,
           media_dir: Path, force_local: bool = False) -> dict:
    """生成一段补音。source 已由调用方校验过在 recordings/ 或 projects/media 里。"""
    info = sf.info(str(source))
    sr = int(info.samplerate)
    dur = float(info.duration)
    a = int(round(max(0.0, min(src_in, dur)) * sr))
    b = int(round(max(0.0, min(src_out, dur)) * sr))
    cap = ""
    if b - a < int(0.05 * sr):
        return {"ok": False, "reason": "选中的这段太短(不到 50ms)"}
    if (b - a) / sr > MAX_SEG_S:
        b = a + int(MAX_SEG_S * sr)
        cap = "只取了前 %.0f 秒" % MAX_SEG_S
    target_sec = float(max(0.05, min(MAX_OUT_S, target_sec)))
    target_n = int(round(target_sec * sr))

    x, sr2 = sf.read(str(source), start=a, frames=b - a,
                     dtype="float32", always_2d=True)
    if sr2 != sr:
        sr = int(sr2)
    if x.shape[1] > 1:
        x = x[:, :2]
    else:
        x = x[:, 0]

    st = source.stat()
    media_dir.mkdir(parents=True, exist_ok=True)

    # ① 云端:先看**云端 key** 的缓存;再打云端。失败才走本地。
    #   ★ 缓存文件名必须按「实际是谁生成的」算 —— 云端失败时写下的本地结果
    #     绝不能落在云端 key 上,不然下次会被当成云端结果返回(说谎)。
    want_cloud = not force_local
    health = {"reachable": False, "modelVersion": ""}
    reason = cap
    if want_cloud:
        health = probe_health()
        want_cloud = bool(health.get("reachable"))
        if not want_cloud:
            reason = "云端不可用,已用本地补音" + (
                "(%s)" % health["detail"] if health.get("detail") else "")
    cloud_ver = health.get("modelVersion") or "unknown"
    if want_cloud:
        hit = _cache_hit(media_dir, rel, st, a, b, target_n, "cloud", cloud_ver)
        if hit:
            return _ok(hit, media_dir, target_sec, "cloud", cloud_ver, True, cap)
        r = _cloud_extend(x, sr, target_sec)
        if r.get("ok"):
            y = np.asarray(r["audio"], dtype=np.float32)
            if y.ndim == 1:
                y = y[:, None]
            if y.shape[0] != target_n:
                # 云端采样率可能与源不同,统一到目标的采样数
                idx = np.linspace(0, y.shape[0] - 1, target_n).astype(np.int64)
                y = y[idx]
            ver = r.get("modelVersion") or cloud_ver
            dst = _cache_path(media_dir, rel, st, a, b, target_n, "cloud", ver)
            _write_wav(dst, y, sr, target_n)
            return _ok(dst, media_dir, target_sec, "cloud", ver, False, cap)
        reason = "云端不可用,已用本地补音" + ("(%s)" % r.get("reason") if r.get("reason") else "")

    # ② 本地
    hit = _cache_hit(media_dir, rel, st, a, b, target_n, "local", _MODEL)
    if hit:
        return _ok(hit, media_dir, target_sec, "local", _MODEL, True, cap)
    y, note = local_extend(x, sr, target_n)
    dst = _cache_path(media_dir, rel, st, a, b, target_n, "local", _MODEL)
    _write_wav(dst, y, sr, target_n)
    out = _ok(dst, media_dir, target_sec, "local", _MODEL, False, cap)
    out["note"] = " · ".join([p for p in (cap, note, reason) if p])
    return out


def _cache_path(media_dir: Path, rel: str, st, a: int, b: int,
                target_n: int, engine: str, ver: str) -> Path:
    key = _hash_key(rel, st, a, b, target_n, engine, ver)
    return media_dir / ("%s%s.wav" % (GEN_PREFIX, key))


def _cache_hit(media_dir: Path, rel: str, st, a: int, b: int,
               target_n: int, engine: str, ver: str) -> Path | None:
    p = _cache_path(media_dir, rel, st, a, b, target_n, engine, ver)
    if not p.is_file():
        return None
    try:
        chk, _ = sf.read(str(p), dtype="float32")
        return p if abs(len(chk) - target_n) <= 2 else None
    except Exception:                            # noqa: BLE001
        return None


def _write_wav(dst: Path, y: np.ndarray, sr: int, target_n: int) -> None:
    y = np.asarray(y, dtype=np.float64)
    y = y[:target_n]
    if len(y) < target_n:
        y = np.pad(y, (0, target_n - len(y)))
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0.999:
        y = y * (0.98 / peak)
    tmp = dst.with_suffix(".wav.part")
    sf.write(str(tmp), np.clip(y, -1.0, 1.0), sr, format="WAV", subtype="PCM_24")
    os.replace(tmp, dst)


def _ok(dst: Path, media_dir: Path, target_sec: float, engine: str,
        ver: str, cached: bool, note: str) -> dict:
    return {"ok": True, "path": _rel_to_media(dst, media_dir),
            "sec": round(target_sec, 3), "engine": engine, "modelVersion": ver,
            "cached": cached, "note": note}


def _rel_to_media(p: Path, media_dir: Path) -> str:
    """换成前端能用的相对路径:`projects/media/ext_....wav`。"""
    try:
        rel = p.relative_to(media_dir).as_posix()
        return "%s/%s/%s" % (media_dir.parent.name, media_dir.name, rel)
    except ValueError:
        return p.name


def status() -> dict:
    c = cfg_view()
    h = probe_health() if c["configured"] else {
        "ok": False, "reachable": False, "detail": "没配置云端", "modelVersion": "",
        "materialsSec": 0, "trainedAt": "", "engine": "", "device": ""}
    return {
        "config": c,
        "cloud": {k: h.get(k) for k in ("ok", "reachable", "detail", "modelVersion",
                                        "materialsSec", "trainedAt", "engine", "device")},
        "local": {"ok": True, "engine": _MODEL,
                  "limits": {"minRatio": MIN_RATIO, "maxRatio": MAX_RATIO,
                             "maxSegSec": MAX_SEG_S, "maxOutSec": MAX_OUT_S}},
    }
