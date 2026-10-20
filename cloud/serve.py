"""补音推理服务 —— 跑在**你自己的**云服务器上,只认一个 Bearer token。

接口(与 Studio 的 `studio_voice.py` 对齐,不要随意改名):
  GET  /health   → {ok, model_version, materials_sec, trained_at, engine, device}
  POST /extend   → JSON {audio_b64, sample_rate, target_sec}
                   ← JSON {ok, wav_b64, model_version, sec, elapsed}
                   失败 ← {ok:false, reason} 或 HTTP 4xx/5xx

流程:收原始片段 → 等长拉伸到目标时长(保音高)→ RMVPE 取 f0 →
RVC 用你的音色模型重合成 → 裁/补到精确采样数 → 回 16bit wav。

★ 不落盘保存用户音频:临时文件用 tempfile,请求结束即删。
★ token 必须由环境变量 SINGMIC_TOKEN 给出;没设就拒绝启动。
★ 模型路径/索引/清单都用环境变量,见 README。
"""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

TOKEN = os.environ.get("SINGMIC_TOKEN", "").strip()
MODEL = Path(os.environ.get("SINGMIC_MODEL", "/opt/singmic/assets/model.pth"))
INDEX = Path(os.environ.get("SINGMIC_INDEX", "")) if os.environ.get("SINGMIC_INDEX") else None
MANIFEST = Path(os.environ.get("SINGMIC_MANIFEST", "/opt/singmic/assets/manifest.json"))
DEVICE = os.environ.get("SINGMIC_DEVICE", "cuda:0")
MAX_IN_S = 16.0
MAX_OUT_S = 30.0

if not TOKEN:
    raise SystemExit("SINGMIC_TOKEN 没设 —— 拒绝启动一个不设防的推理服务")

MANIFEST_DATA = {}
try:
    MANIFEST_DATA = json.loads(MANIFEST.read_text("utf-8"))
except (OSError, ValueError):
    MANIFEST_DATA = {}

app = FastAPI(title="SingMic voice serve")
_vc = None
_vc_err = ""
_vc_lock = threading.Lock()      # GPU 一次只跑一段,串行,别把显存打爆
_init_lock = threading.Lock()


def _auth(auth: str | None) -> None:
    got = (auth or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, TOKEN):
        raise HTTPException(401, "token 不对")


def _rvc_config():
    """RVC 新版(tag 2.3.x)的 Config 是个单例,而且 __init__ 里会跑 argparse。

    ★ uvicorn 的命令行参数会被 argparse 当成非法选项直接炸掉 —— 所以构造时把
      sys.argv 临时缩成只剩程序名,构造完再还回去。
    """
    import sys
    from configs.config import Config

    argv = sys.argv
    sys.argv = [argv[0] if argv else "serve"]
    try:
        return Config()
    finally:
        sys.argv = argv


def _get_vc():
    """懒加载 RVC;失败把原因留着,/extend 直接 503(Studio 会退回本地)。"""
    global _vc, _vc_err
    with _init_lock:
        if _vc is not None or _vc_err:
            return _vc
        try:
            if not MODEL.is_file():
                raise RuntimeError("找不到模型文件:%s" % MODEL)
            # RVC 用环境变量找权重目录;必须在 import/构造 Config 之前设好
            os.environ.setdefault("weight_root", str(MODEL.parent))
            os.environ.setdefault("rmvpe_root", "/opt/rvc/assets/rmvpe")
            os.environ.setdefault("index_root", str(MODEL.parent))
            os.environ.setdefault("outside_index_root", str(MODEL.parent))
            from infer.vc.modules import VC            # RVC 仓库里的类
            vc = VC(_rvc_config())
            # ★ get_vc 只认文件名(内部拼 weight_root);索引在 vc_single 里单独传
            vc.get_vc(MODEL.name)
            _vc = vc
            idx = str(INDEX) if INDEX and INDEX.is_file() else ""
            print("[voice] 模型已加载:", MODEL, "index:", idx or "(无)", flush=True)
        except Exception as e:                    # noqa: BLE001
            _vc_err = "%s: %s" % (e.__class__.__name__, e)
            print("[voice] 模型加载失败:", _vc_err, flush=True)
        return _vc


def _stretch(x: np.ndarray, sr: int, target_sec: float) -> np.ndarray:
    """保音高等长拉伸。优先 librosa;没有就退到 PSOLA(见 tuning.py 同一套思路)。"""
    n_t = int(round(target_sec * sr))
    if len(x) < 64:
        return np.zeros(max(n_t, 1), dtype=np.float32)
    rate = len(x) / max(n_t, 1)
    if abs(rate - 1.0) < 1e-3:
        return x.astype(np.float32)
    try:
        import librosa
        y = librosa.effects.time_stretch(x.astype(np.float32), rate=rate)
    except Exception:                             # noqa: BLE001
        # 没 librosa:线性插值兜底(音高会跟着变,但至少长度对;由 RVC 重新合成 f0 前不可用)
        idx = np.linspace(0, len(x) - 1, n_t)
        y = np.interp(idx, np.arange(len(x)), x)
    if len(y) < n_t:
        y = np.pad(y, (0, n_t - len(y)))
    return np.asarray(y[:n_t], dtype=np.float32)


def _fit(y: np.ndarray, n_t: int) -> np.ndarray:
    if len(y) < n_t:
        y = np.pad(y, (0, n_t - len(y)))
    y = np.asarray(y[:n_t], dtype=np.float32)
    k = min(len(y) // 2, int(0.005 * 48000))
    if k > 1:
        w = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
        y[:k] *= w
        y[-k:] *= w[::-1]
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0.999:
        y *= 0.98 / peak
    return y


def _convert(x: np.ndarray, sr: int, target_sec: float) -> tuple[np.ndarray, int]:
    """拉伸 → RVC 重合成。返回 (音频, 采样率)。"""
    vc = _get_vc()
    if vc is None:
        raise RuntimeError(_vc_err or "模型没加载")
    stretched = _stretch(x, sr, target_sec)
    with tempfile.TemporaryDirectory(prefix="singmic-") as td:
        src = Path(td) / "seg.wav"
        sf.write(str(src), np.clip(stretched, -1.0, 1.0), sr, format="WAV", subtype="PCM_16")
        idx = str(INDEX) if INDEX and INDEX.is_file() else ""
        with _vc_lock:
            # 新版签名:vc_single(sid, 输入wav, 变调, f0方法, 索引, 检索强度,
            #                   resample_sr, 音量混合, protect) —— 共 9 个位置参数
            _info, (out_sr, out) = vc.vc_single(
                0, str(src), 0, "rmvpe", idx, 0.75, 0, 0.25, 0.33)
        if out is None:
            raise RuntimeError("RVC 没合成出来:%s" % str(_info)[-300:])
    out = np.asarray(out, dtype=np.float32)
    if out.ndim > 1:
        out = out.mean(axis=1)
    return out, int(out_sr)


@app.get("/health")
def health():
    vc = _get_vc()
    return {
        "ok": vc is not None,
        "model_version": str(MANIFEST_DATA.get("version") or ""),
        "materials_sec": int(MANIFEST_DATA.get("materials_sec") or 0),
        "trained_at": str(MANIFEST_DATA.get("trained_at") or ""),
        "engine": "rvc-v2",
        "device": DEVICE,
        "reason": _vc_err,
    }


@app.post("/extend")
def extend(body: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    t0 = time.time()
    try:
        raw = base64.b64decode(body.get("audio_b64") or "", validate=True)
    except Exception:                             # noqa: BLE001
        raise HTTPException(400, "audio_b64 解不开")
    if not raw:
        raise HTTPException(400, "没有音频")
    try:
        x, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception:                             # noqa: BLE001
        raise HTTPException(400, "不是能读的 wav")
    if x.shape[1] > 1:
        x = x.mean(axis=1)
    else:
        x = x[:, 0]
    if len(x) / sr > MAX_IN_S:
        raise HTTPException(413, "片段超过 %.0f 秒" % MAX_IN_S)
    target = float(body.get("target_sec") or 0)
    if not (0.05 <= target <= MAX_OUT_S):
        raise HTTPException(400, "target_sec 超出 0.05–%.0f" % MAX_OUT_S)

    try:
        y, out_sr = _convert(x, sr, target)
    except Exception as e:                        # noqa: BLE001
        # ★ 失败就让 Studio 退回本地,不要拿「拉伸过的原声」冒充模型结果
        return JSONResponse(status_code=503, content={
            "ok": False, "reason": "%s: %s" % (e.__class__.__name__, str(e)[:160])})
    y = _fit(y, int(round(target * out_sr)))
    bio = io.BytesIO()
    sf.write(bio, np.clip(y, -1.0, 1.0), out_sr, format="WAV", subtype="PCM_16")
    return {
        "ok": True,
        "wav_b64": base64.b64encode(bio.getvalue()).decode("ascii"),
        "model_version": str(MANIFEST_DATA.get("version") or "unknown"),
        "sec": round(len(y) / out_sr, 3),
        "elapsed": round(time.time() - t0, 2),
    }
