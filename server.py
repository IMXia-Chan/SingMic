"""SingMic —— 手机当麦克风,电脑当调音台。

手机浏览器打开 `/phone`(USB 连着、跑过 adb reverse 就行),电脑浏览器打开 `/`。
声音链路:

    手机麦克风 -> USB -> 电脑 -> 混响/伴奏 -> 耳机(耳返) + WAV 录音

跑法:
    D:\\AI\\python.exe D:\\singmic\\server.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import sounddevice as sd
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
MUSIC = ROOT / "music"
REC = ROOT / "recordings"
CONFIG = ROOT / "config.toml"

sys.path.insert(0, str(ROOT))
from audio_engine import (                             # noqa: E402
    Engine, list_loopback_devices, list_output_devices, refresh_portaudio)
import tuning                                          # noqa: E402
import studio_api                                      # noqa: E402
import studio_asr                                      # noqa: E402
import studio_syllable                                 # noqa: E402


def _reopen_output(name: str | None) -> str:
    """★ 重开耳返流的**唯一入口**:先关、让 PortAudio 重新枚举、再开。

    PortAudio 的设备表是初始化那一刻冻住的 —— 不刷新的话,它会把声音送去
    一台**已经拔掉的耳机**,而且从头到尾不报一个错。详见
    `audio_engine.live_output_names`。
    """
    engine.close_output()
    refresh_portaudio()
    return engine.open_output(name)

HOST = "127.0.0.1"      # ★ 只绑本机。手机走 USB 隧道(adb reverse)进来,不暴露到局域网。
PORT = int(os.environ.get("SINGMIC_PORT", "8765"))

# ★★ adb reverse 是**按这一次 USB 连接**算的:拔一下手机、或 adb server 重启一次,
#   映射就没了,手机页面就变成「连不上电脑那头的服务」—— 而电脑这边看着一切正常。
#   所以每 10 秒自检一次,没了就自己接回来(找不到 adb 就当没这回事)。
_ADB = os.environ.get("ADB") or str(
    Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools"
    / ("adb.exe" if os.name == "nt" else "adb"))


def _reverse_ok() -> bool:
    try:
        r = subprocess.run([_ADB, "reverse", "--list"], capture_output=True,
                           text=True, timeout=5)
        return f"tcp:{PORT}" in (r.stdout or "")
    except Exception:                          # noqa: BLE001
        return True                            # 没有 adb / 走别的路 —— 别管


def _add_reverse() -> str:
    try:
        r = subprocess.run([_ADB, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip() or (r.stderr or "").strip()
    except Exception as e:                     # noqa: BLE001
        return f"{type(e).__name__}: {e}"

AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aiff", ".aif"}


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


cfg = load_config()
engine = Engine()
ui_clients: set[WebSocket] = set()
mic_clients: set[WebSocket] = set()


def apply_config() -> list[str]:
    """把 config.toml 里的东西塞进引擎。返回人话版的启动说明。"""
    notes: list[str] = []
    a = cfg.get("audio", {})
    v = cfg.get("voice", {})
    b = cfg.get("backing", {})

    engine.reverb_mix = float(v.get("reverb", 0.0))
    engine.backing_level = float(b.get("level", 0.7))
    engine.backing_loop = bool(b.get("loop", False))
    engine.set_params(
        gain=float(v.get("gain", 1.0)),
        denoise=float(v.get("denoise", 0.0)),
        gate=bool(v.get("gate", True)),
        gate_db=float(v.get("gate_db", -55.0)),
        hp_hz=float(v.get("hp_hz", 80.0)),
        eq_low=float(v.get("eq_low", 0.0)),
        eq_mid=float(v.get("eq_mid", 0.0)),
        eq_high=float(v.get("eq_high", 0.0)),
        deess=float(v.get("deess", 0.0)),
        comp=bool(v.get("comp", False)),
        comp_ratio=float(v.get("comp_ratio", 3.0)),
        comp_threshold=float(v.get("comp_threshold", -22.0)),
        makeup=float(v.get("makeup", 0.0)),
    )

    # ★ AI 降噪也要跟着 config 走。以前它只存在于引擎内存里,
    #   重启一次就悄悄变回「关」—— 而界面上勾还在(状态从引擎读,会显示 false,
    #   而人未必会去核对),等于白开。
    if bool(v.get("ai", False)):
        why = engine.set_ai(True)
        notes.append("AI 降噪:开" + (f"({why})" if why else ""))

    want = a.get("output") or None
    try:
        name = engine.open_output(want)
        notes.append(f"耳返输出:{name}")
    except Exception as e:
        notes.append(f"★ 输出设备没打开:{e}(网页上可以手动选)")

    src = b.get("source") or ""
    if src:
        try:
            got = engine.set_loop(src)
            notes.append(f"伴奏来源:电脑声音 · {got}")
        except Exception as e:
            notes.append(f"★ 伴奏来源没打开:{e}")

    fname = b.get("file") or ""
    if fname:
        p = MUSIC / fname
        if p.exists():
            try:
                engine.load_backing(p)
                notes.append(f"伴奏:{p.name}")
            except Exception as e:
                notes.append(f"★ 伴奏读不了:{p.name} / {e}")
        else:
            notes.append(f"★ 找不到伴奏文件:{p}")
    return notes


app = FastAPI(title="singmic", docs_url=None, redoc_url=None)
app.include_router(studio_api.router)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
# ★ 本地工具的页面一律不缓存:浏览器每次都回服务器问一句,没改就 304,几乎不花钱。
#   不加这句的后果是实打实的——pointer.js / ui.js 改完,他那边页面还是旧的,
#   于是"改了没反应"。这个坑踩过不止一次,别再摘掉。
NC = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers=NC)


@app.get("/phone")
async def phone() -> FileResponse:
    return FileResponse(WEB / "phone.html", headers=NC)


@app.get("/studio")
async def studio() -> FileResponse:
    """二级页面:FL 那种多轨工作室。"""
    return FileResponse(WEB / "studio.html", headers=NC)


@app.get("/api/devices")
async def api_devices() -> JSONResponse:
    return JSONResponse({
        "outputs": [{"id": i, "name": n, "rate": r} for i, n, r in list_output_devices()],
        "default": sd.default.device[1],
        # ★ 这些是「能被环回抓到」的设备名 —— 和上面那套名字不一定一样,
        #   网页上要分开列。
        "loopbacks": list_loopback_devices(),
        "loop_now": engine.loop.device,
    })


@app.get("/api/backing")
async def api_backing() -> JSONResponse:
    MUSIC.mkdir(parents=True, exist_ok=True)
    files = sorted(p.name for p in MUSIC.iterdir()
                   if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    return JSONResponse({"files": files, "current": engine.backing_name})


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(engine.status())


# ---------------------------------------------------------------------------
# 保存调音参数
# ---------------------------------------------------------------------------
# ★ 为什么必须有这个按钮:网页滑杆只发给正在跑的引擎,**不发到 config.toml**。
#   重启一次参数就回到配置文件里的旧值,看起来像「网页上的设置丢了」——
#   这个坑 2026-10-19 已经栽过一次。调完能按一下就存住。
_VOICE_KEYS = (
    "gain", "denoise", "gate", "gate_db", "hp_hz",
    "eq_low", "eq_mid", "eq_high", "deess",
    "comp", "comp_ratio", "comp_threshold", "makeup",
)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        # 别写出 0.35000000000000003 这种东西
        return f"{round(float(v), 4):g}"
    if isinstance(v, str):
        # ★ 设备名里有空格、括号、中文 —— 必须带引号,否则写回去的 toml 读不出来
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(v)


def _rewrite_section(section: str, keys: tuple[str, ...], values: dict) -> None:
    """只改指定 section 里那几行,注释和别的段一个字都不动。"""
    text = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    lines = text.splitlines(keepends=True)

    head = f"[{section}]"
    start = end = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() == head:
            start = i + 1
            break
    if start is None:
        lines.append("\n" + head + "\n" if text and not text.endswith("\n") else head + "\n")
        start = len(lines)
    for i in range(start, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break
    if end is None:
        end = len(lines)

    for k in keys:
        if k not in values:
            continue
        new = f"{k} = {_toml_value(values[k])}\n"
        hit = None
        for i in range(start, end):
            body = lines[i].lstrip()
            if body.startswith(k) and body[len(k):].lstrip().startswith("="):
                hit = i
                break
        if hit is None:
            lines.insert(end, new)
            end += 1
        else:
            lines[hit] = new
    CONFIG.write_text("".join(lines), encoding="utf-8")


def _save_voice_section(values: dict) -> None:
    _rewrite_section("voice", _VOICE_KEYS + ("reverb", "ai"), values)


@app.post("/api/save_preset")
async def api_save_preset() -> JSONResponse:
    try:
        values = {k: engine.params[k] for k in _VOICE_KEYS}
        values["reverb"] = engine.reverb_mix
        values["ai"] = bool(engine.ai is not None)
        _save_voice_section(values)
        # ★ 耳返设备也要存 —— 不存的话重启后回退到「系统默认」,
        #   而系统默认往往就是机箱扬声器(会啸叫)。以前就是这么丢的。
        _rewrite_section("audio", ("output",),
                         {"output": engine.device_name or ""})
        return JSONResponse({"ok": True, "saved": len(values) + 1,
                             "path": str(CONFIG)})
    except Exception as e:                      # noqa: BLE001
        raise HTTPException(500, f"存不进 config.toml:{e}")


# ---------------------------------------------------------------------------
# 录音导出
# ---------------------------------------------------------------------------
def _safe(name: str) -> Path:
    """★ 只认 recordings/ 里的文件名 —— 传 `../../../windows/system32` 过来一律当没这回事。"""
    p = (REC / Path(name).name).resolve()
    if p.parent != REC.resolve() or p.suffix.lower() != ".wav":
        raise HTTPException(404, "没有这个文件")
    return p


_AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg"}


def _safe_src(name: str = "", path: str = "") -> Path:
    """切块认两种来源:`recordings/` 里的文件名,或 `projects/media/` 里的相对路径。

    ★ 以前只认 recordings/ —— 导入的原唱/别人录的干声都在「媒体」里,
      永远切不出音块,整页音高视图里只能看到一条灰带。其余路径一律当没这回事。
    """
    rec = REC.resolve()
    med = (ROOT / "projects" / "media").resolve()
    if path:
        p = (ROOT / Path(path)).resolve()
        if not (p.is_relative_to(rec) or p.is_relative_to(med)):
            raise HTTPException(404, "没有这个文件")
    else:
        p = (rec / Path(name).name).resolve()
        if p.parent != rec:
            raise HTTPException(404, "没有这个文件")
    if p.suffix.lower() not in _AUDIO_EXT or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    return p


@app.get("/api/recordings")
async def api_recordings() -> JSONResponse:
    REC.mkdir(parents=True, exist_ok=True)
    items = []
    # ★ 下划线开头的是编辑器生成的预览,不是录音 —— 别挤进列表
    real = [q for q in REC.glob("*.wav") if not q.name.startswith("_")]
    for p in sorted(real, key=lambda q: q.stat().st_mtime, reverse=True)[:20]:
        try:
            info = sf.info(str(p))
            sec = info.frames / max(1, info.samplerate)
        except Exception:                       # noqa: BLE001
            sec = 0.0
        items.append({
            "name": p.name,
            "size": p.stat().st_size,
            "sec": round(sec, 1),
            "mp3": p.with_suffix(".mp3").exists(),
        })
    return JSONResponse({"files": items, "dir": str(REC)})


@app.delete("/api/recordings")
async def api_delete_all_recordings() -> JSONResponse:
    """★ 全删。删的是 recordings/ 里的 wav 和同名 mp3,别的地方一个字节都不碰。"""
    killed = 0
    failed: list[str] = []
    for p in list(REC.glob("*.wav")) + list(REC.glob("*.mp3")):
        try:
            p.unlink()
            killed += 1
        except Exception:                       # noqa: BLE001
            failed.append(p.name)               # 正在录的那个会删不掉,如实说
    return JSONResponse({"ok": not failed, "deleted": killed, "failed": failed})


@app.delete("/api/recordings/{name}")
async def api_delete_recording(name: str) -> JSONResponse:
    p = _safe(name)
    killed: list[str] = []
    for q in (p, p.with_suffix(".mp3")):
        if q.exists():
            try:
                q.unlink()
                killed.append(q.name)
            except Exception as e:              # noqa: BLE001
                raise HTTPException(409, f"删不掉 {q.name}:{e}") from e
    if not killed:
        raise HTTPException(404, "没有这个文件")
    return JSONResponse({"ok": True, "deleted": killed})


@app.get("/api/open_recordings")
async def api_open_recordings() -> JSONResponse:
    """★ 只在**这台电脑上**开资源管理器 —— 它是本机服务,不给任何人远程用。"""
    REC.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(REC))                  # noqa: S606
    except Exception as e:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True})


def _to_mp3(src: Path, dst: Path, kbps: int) -> None:
    """32bit float WAV → MP3。★ 手机、K歌 App、微信都能认。"""
    import lameenc
    import numpy as np

    x, sr = sf.read(str(src), dtype="float32", always_2d=True)
    enc = lameenc.Encoder()
    enc.set_bit_rate(int(kbps))
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(int(x.shape[1]))
    enc.set_quality(2)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    data = enc.encode(pcm.tobytes()) + enc.flush()
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dst)                            # ★ 写一半被打断不留半截 mp3


@app.get("/recordings/{name}")
async def get_recording(name: str) -> FileResponse:
    p = _safe(name)
    if not p.exists():
        raise HTTPException(404, "没有这个文件")
    return FileResponse(str(p), filename=p.name, media_type="audio/wav")


@app.get("/api/export_mp3/{name}")
async def export_mp3(name: str, kbps: int = 320) -> FileResponse:
    p = _safe(name)
    if not p.exists():
        raise HTTPException(404, "没有这个文件")
    dst = p.with_suffix(".mp3")
    if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
        try:
            await asyncio.get_running_loop().run_in_executor(None, _to_mp3, p, dst, kbps)
        except Exception as e:                  # noqa: BLE001
            raise HTTPException(500, f"转 MP3 失败:{e}") from e
    return FileResponse(str(dst), filename=dst.name, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# 音符块编辑器
# ---------------------------------------------------------------------------
_BLOCKS_MEM: dict[str, dict] = {}


def _blocks_json_path(p: Path) -> Path:
    return p.with_suffix(".blocks.json")


def _syl_json_path(p: Path) -> Path:
    """音节切分单独缓存,不覆盖旧的 `<stem>.blocks.json`。"""
    return p.with_suffix(".syl.json")


def _edits_json_path(p: Path) -> Path:
    return p.with_suffix(".edits.json")


def _file_key(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _blocks_build(p: Path, mode: str = "note") -> dict:
    """切块(7 分钟录音约 10 秒)。★ 只能在线程池里调,别堵事件循环。

    mode="note"      —— 旧行为:按音高跳变切(逐音)。
    mode="syllable"  —— 按音节核切(中文大致一字一块,英文一词多块)。
    两种切法各自缓存、互不覆盖。
    """
    mode = "syllable" if mode == "syllable" else "note"
    key = _file_key(p)
    mk = str(p) + "|" + mode
    hit = _BLOCKS_MEM.get(mk)
    if hit and hit.get("_key") == key:
        hit["cached"] = True
        return hit

    cp = _syl_json_path(p) if mode == "syllable" else _blocks_json_path(p)
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if data.get("_key") == key:
                data["cached"] = True
                _BLOCKS_MEM[mk] = data
                return data
        except Exception:                       # noqa: BLE001
            pass                                # 坏了就重算,不报错

    x, sr = sf.read(str(p), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x[:, 0]
    data = (studio_syllable.segment_syllables(x, sr) if mode == "syllable"
            else tuning.segment(x, sr))
    data["_key"] = key
    data["cached"] = False
    try:
        cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:                           # noqa: BLE001
        pass                                    # 缓存写不进去不影响功能
    _BLOCKS_MEM[mk] = data
    return data


def _blocks_for(p: Path, mode: str, lang: str, asr: bool) -> dict:
    """切块 + (可选)识别标注。识别失败只降级,不抛。"""
    data = _blocks_build(p, mode)
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    out["seg"] = mode
    if mode == "syllable" and asr:
        a = studio_asr.recognize(p, lang)
        if a.get("ok"):
            out["blocks"] = studio_asr.annotate(data["blocks"], a)
            out["asr"] = {"ok": True, "lang": a.get("lang", ""),
                          "text": a.get("text", ""), "model": a.get("model", ""),
                          "approx": bool(a.get("approx")),
                          "cached": bool(a.get("cached"))}
        else:
            # ★ 识别不可用不影响切分 —— 页面照样有块,只是没有文字
            out["asr"] = {"ok": False, "reason": a.get("reason", "识别不可用")}
    return out


def _edits_load(p: Path) -> dict:
    ep = _edits_json_path(p)
    if not ep.exists():
        return {}
    try:
        return json.loads(ep.read_text(encoding="utf-8")).get("blocks", {})
    except Exception:                           # noqa: BLE001
        return {}


def _render_job(p: Path, edits: dict, remix: bool, window, out: Path) -> dict:
    """渲染(整条约 2–6 秒;局部窗口快得多)。★ 线程池专用。"""
    x, sr = sf.read(str(p), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x[:, 0]
    blocks = _blocks_build(p)["blocks"]
    y = tuning.render_blocks(x, sr, blocks, edits)

    mix = None
    if remix:
        mp = p.with_name(p.name[:-len("_voice.wav")] + "_mix.wav") \
            if p.name.endswith("_voice.wav") else None
        if mp is not None and mp.exists():
            m, sr2 = sf.read(str(mp), dtype="float32", always_2d=True)
            if sr2 == sr:
                mix = tuning.rebuild_mix(m, x, y)

    ia, ib = 0, len(y)
    if window:
        ia = max(0, int(float(window[0]) * sr))
        ib = min(len(y), int(float(window[1]) * sr))
        y = y[ia:ib]
        if mix is not None:
            mix = mix[ia:ib]

    data = mix if mix is not None else y
    tmp = out.with_name(out.name + ".part")
    sf.write(str(tmp), data, sr, subtype="FLOAT", format="WAV")
    tmp.replace(out)

    # 预览文件会攒 —— 只留最近 40 个
    try:
        olds = sorted(REC.glob("_preview_*.wav"), key=lambda q: q.stat().st_mtime,
                      reverse=True)[40:]
        for q in olds:
            q.unlink(missing_ok=True)
    except Exception:                           # noqa: BLE001
        pass

    return {"sec": round(len(y) / sr, 3),
            "peak": round(float(abs(data).max()) if len(data) else 0.0, 3),
            "remixed": mix is not None}


@app.post("/api/blocks")
async def api_blocks(req: Request) -> JSONResponse:
    body = await req.json()
    p = _safe_src(str(body.get("name", "")), str(body.get("path", "")))
    # ★ 旧客户端不带 mode ⇒ 仍走「按音」,旧工程/旧脚本零影响
    mode = "syllable" if str(body.get("mode") or "") == "syllable" else "note"
    lang = str(body.get("lang") or "auto")
    if lang not in ("auto", "zh", "en"):
        lang = "auto"
    use_asr = bool(body.get("asr", False)) and mode == "syllable"
    t0 = time.perf_counter()
    try:
        data = await asyncio.get_running_loop().run_in_executor(
            None, _blocks_for, p, mode, lang, use_asr)
    except Exception as e:                      # noqa: BLE001
        raise HTTPException(500, f"切块失败:{e}") from e
    data["edits"] = _edits_load(p)
    data["sec_cost"] = round(time.perf_counter() - t0, 2)
    return JSONResponse(data)


@app.post("/api/render")
async def api_render(req: Request) -> JSONResponse:
    body = await req.json()
    p = _safe(str(body.get("name", "")))
    if not p.exists():
        raise HTTPException(404, "没有这个文件")

    edits = body.get("edits") or {}
    remix = bool(body.get("remix", True))
    window = body.get("window")
    if body.get("save"):
        try:
            _edits_json_path(p).write_text(
                json.dumps({"blocks": edits}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as e:                  # noqa: BLE001
            raise HTTPException(500, f"存编辑失败:{e}") from e

    tag = hashlib.md5(
        json.dumps([edits, remix, window], sort_keys=True, ensure_ascii=False)
        .encode("utf-8")).hexdigest()[:10]
    out = REC / f"_preview_{p.stem}_{tag}.wav"

    t0 = time.perf_counter()
    try:
        info = await asyncio.get_running_loop().run_in_executor(
            None, _render_job, p, edits, remix, window, out)
    except Exception as e:                      # noqa: BLE001
        raise HTTPException(500, f"渲染失败:{e}") from e

    info["url"] = f"/api/preview/{out.name}?v={int(time.time() * 1000)}"
    info["sec_cost"] = round(time.perf_counter() - t0, 2)
    return JSONResponse(info)


@app.get("/api/preview/{name}")
async def api_preview(name: str) -> FileResponse:
    """★ 不给 filename= —— 带了就会被浏览器当附件下载,<audio> 放不了。"""
    p = _safe(name)
    if not p.exists():
        raise HTTPException(404, "没有这个文件")
    return FileResponse(str(p), media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# 手机 -> 电脑(二进制 PCM)
# ---------------------------------------------------------------------------
@app.websocket("/ws/mic")
async def ws_mic(ws: WebSocket) -> None:
    await ws.accept()
    mic_clients.add(ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                engine.push_pcm16(data)
                continue
            text = msg.get("text")
            if text:
                try:
                    j = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if j.get("type") == "hello":
                    engine.set_phone_rate(int(j.get("rate") or 48000),
                                          str(j.get("fmt") or "i16"))
                    # ★ 手机自报的采集参数。只存着给界面看,不改任何行为。
                    cap = j.get("cap")
                    engine.cap_info = cap if isinstance(cap, dict) else {}
    except WebSocketDisconnect:
        pass
    finally:
        mic_clients.discard(ws)
        # ★ 手机断了就把缓冲清掉:留着的是几百毫秒前的旧声音,
        #   再连上会先播一段「过去」,而且欠载计数也没意义了。
        if not mic_clients:
            engine.voice.reset()
            # ★ 清完缓冲就是「重新攒」的开始 —— 接下来几秒的欠载是我们自己造的,
            #   不能让它把自适应水位永久抬上去(见 Engine.note_stall)。
            engine.note_stall()


# ---------------------------------------------------------------------------
# 电脑网页 <- 状态 / 控制
# ---------------------------------------------------------------------------
@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket) -> None:
    await ws.accept()
    ui_clients.add(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_ui(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        ui_clients.discard(ws)


async def handle_ui(ws: WebSocket, msg: dict) -> None:
    kind = msg.get("type")

    if kind == "set":
        if "reverb" in msg:
            engine.reverb_mix = max(0.0, min(1.0, float(msg["reverb"])))
        if "backing" in msg:
            engine.backing_level = max(0.0, min(1.5, float(msg["backing"])))
        if "monitor" in msg:
            engine.monitor_level = max(0.0, min(1.5, float(msg["monitor"])))
        # ★ 耳返绕开 AI:只改「耳朵听到的那一路」,录音文件一点不受影响。
        if "monitor_pre_ai" in msg:
            engine.monitor_pre_ai = bool(msg["monitor_pre_ai"])
        # ★ 低延迟:抖动缓冲封顶 30ms,代价是手机卡一下会「咔」一声。
        if "low_latency" in msg:
            engine.set_low_latency(bool(msg["low_latency"]))
        # ★ 量具:把抖动缓冲水位钉死在某毫秒,用来验证「N ms 到底撑不撑得住」。
        if "buffer_ms" in msg:
            engine.set_buffer_ms(float(msg["buffer_ms"]))
        # ★ 耳返那条路的降噪量。裸信号进耳返 = 一片嗡嗡,必须给它一层降噪。
        if "mon_denoise" in msg:
            engine.set_mon_denoise(float(msg["mon_denoise"]))
        # 转发给干声链(未知键由 set_params 自己忽略)
        engine.set_params(**{k: msg[k] for k in (
            "gain", "denoise", "gate", "gate_db", "hp_hz",
            "eq_low", "eq_mid", "eq_high",
            "deess", "comp", "comp_ratio", "comp_threshold", "makeup",
            "bypass") if k in msg})
        # ★ AI 降噪要加载模型(约 1 秒)。放在线程池里 —— 卡在事件循环里
        #   会把状态推送和录音都停住,听起来像后台死了。
        if "ai" in msg:
            want = bool(msg["ai"])
            if want != (engine.ai is not None):
                err = await asyncio.get_running_loop().run_in_executor(
                    None, engine.set_ai, want)
                if err:
                    await ws.send_text(json.dumps(
                        {"type": "notice", "text": "★ " + err}, ensure_ascii=False))
        return

    if kind == "record_start":
        try:
            mix_p, voice_p = engine.start_recording(REC)
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"开始录音:{Path(mix_p).name}"},
                ensure_ascii=False))
        except Exception as e:
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"★ 录音起不来:{e}"}, ensure_ascii=False))
        return

    if kind == "record_stop":
        engine.stop_recording()
        paths = engine.status()["paths"]
        await ws.send_text(json.dumps(
            {"type": "notice", "text": "录音已保存:" + " / ".join(Path(p).name for p in paths)},
            ensure_ascii=False))
        return

    if kind == "output":
        name = msg.get("device") or ""
        try:
            # ★ 开设备可能要几百毫秒,别卡住整个事件循环
            await asyncio.get_running_loop().run_in_executor(None, _reopen_output, name)
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"输出设备:{engine.device_name}"},
                ensure_ascii=False))
        except Exception as e:
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"★ 打不开设备:{e}"}, ensure_ascii=False))
        return

    if kind == "loop":
        name = msg.get("device") or ""
        try:
            # 开设备要几十~几百毫秒,别卡住事件循环
            got = await asyncio.get_running_loop().run_in_executor(
                None, engine.set_loop, name)
            text = f"伴奏来源:电脑声音 · {got}" if got else "伴奏来源:关掉了"
            await ws.send_text(json.dumps({"type": "notice", "text": text},
                                          ensure_ascii=False))
        except Exception as e:
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"★ {e}"}, ensure_ascii=False))
        return

    if kind == "backing":
        name = msg.get("file") or ""
        if not name:
            engine.backing = None
            engine.backing_raw = None
            engine.backing_name = ""
            return
        p = MUSIC / name
        try:
            engine.load_backing(p)
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"伴奏:{p.name}"}, ensure_ascii=False))
        except Exception as e:
            await ws.send_text(json.dumps(
                {"type": "notice", "text": f"★ 伴奏读不了:{e}"}, ensure_ascii=False))
        return


async def broadcast(msg: dict) -> None:
    text = json.dumps(msg, ensure_ascii=False)
    for ws in list(ui_clients):
        try:
            await ws.send_text(text)
        except Exception:
            ui_clients.discard(ws)


async def device_watch() -> None:
    """★ 每 2 秒看一圈设备。

    用户会断蓝牙、换有线耳机 —— 设备没了不能就当没事发生(那就是「突然没声音了」)。
    设备一没:自动切到系统当前默认输出,并告诉网页。
    设备列表一变(插上/拔掉):让网页重新拉一遍,不用手动刷新。
    """
    last_names: tuple[str, ...] | None = None
    next_try = 0.0
    last_frames = -1
    stalled = 0
    last_adb = 0.0
    while True:
        await asyncio.sleep(2.0)
        try:
            # USB 隧道自检(10 秒一次):拔插手机后 adb reverse 会丢,
            # 表现就是手机页「连不上电脑那头的服务」,电脑这边却一切正常。
            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_adb >= 10.0:
                last_adb = loop_now
                if not await asyncio.get_running_loop().run_in_executor(
                        None, _reverse_ok):
                    got = await asyncio.get_running_loop().run_in_executor(
                        None, _add_reverse)
                    print(f"[USB 隧道] 掉了,已重接:{got}", flush=True)

            names = tuple(n for _, n, _ in list_output_devices())
            if last_names is not None and names != last_names:
                await broadcast({"type": "reload"})
                if engine.loop.device and engine.loop.device not in names:
                    # 伴奏来源那台设备也没了
                    engine.loop.stop()
                    await broadcast({"type": "notice",
                                     "text": "伴奏来源的设备断开了,已停掉伴奏"})
            last_names = names

            if engine.stream is not None and not engine.device_alive():
                if asyncio.get_running_loop().time() < next_try:
                    continue
                old = engine.device_name
                try:
                    name = await asyncio.get_running_loop().run_in_executor(
                        None, _reopen_output, None)
                    stalled = 0
                    last_frames = engine._out_frames
                    await broadcast({"type": "notice",
                                     "text": f"耳返设备掉了({old}),已自动切到:{name}"})
                except Exception as e:
                    next_try = asyncio.get_running_loop().time() + 5.0
                    await broadcast({"type": "notice",
                                     "text": f"★ 耳返设备掉了,新的也打不开:{e}"})
                continue

            # ★★ 还有一种「没有声音」是设备**名字还在、流却停了**:
            #   蓝牙耳机闲置睡下去之后 WASAPI 就不再回调,但设备还在列表里,
            #   device_alive() 看不出来 —— 表现就是「界面一切正常,耳机里死寂」。
            #   判据只有一条:回调里的帧数不再往前走。连 6 秒不动就重开。
            if engine.stream is not None:
                now = asyncio.get_running_loop().time()
                if engine._out_frames == last_frames:
                    stalled += 1
                else:
                    stalled = 0
                last_frames = engine._out_frames
                if stalled >= 3 and now >= next_try:
                    want = engine.device_name
                    try:
                        got = await asyncio.get_running_loop().run_in_executor(
                            None, _reopen_output, want)
                        stalled = 0
                        last_frames = engine._out_frames
                        await broadcast({"type": "notice",
                                         "text": f"声音流停了(设备还在),已重新打开:{got}"})
                    except Exception as e:
                        next_try = now + 5.0
                        await broadcast({"type": "notice",
                                         "text": f"★ 声音流停了,重开失败:{e}"})
        except Exception as e:                     # noqa: BLE001
            print(f"[设备监视] ★ {type(e).__name__}: {e}", flush=True)


async def status_loop() -> None:
    """每秒 10 次把电平推给网页。★ 没有网页连着就不算。"""
    while True:
        try:
            if ui_clients:
                payload = json.dumps({"type": "status", **engine.status()},
                                     ensure_ascii=False)
                for ws in list(ui_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        ui_clients.discard(ws)
        except Exception as e:
            print(f"[状态推送] ★ {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(0.1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    MUSIC.mkdir(parents=True, exist_ok=True)
    REC.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(status_loop())
    watch = asyncio.create_task(device_watch())
    try:
        yield
    finally:
        for t in (task, watch):
            t.cancel()
        engine.close_output()


# ★ lifespan 写在路由后面(读起来顺),但 app 是前面建的 —— 这里补挂上去。
app.router.lifespan_context = lifespan


# 静态文件垫底。★ 同样不缓存(见上面 NC 那段注释):ui.js / pointer.js / css 改完,
#   他刷新就能看到,不需要每次都手动带 ?v=。
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _NoCacheStatic(directory=str(WEB)), name="static")


notes = apply_config()


def main() -> int:
    print("SingMic", flush=True)
    print(f"  电脑端:http://{HOST}:{PORT}/", flush=True)
    print(f"  手机端:http://{HOST}:{PORT}/phone  (需要先 adb reverse tcp:{PORT} tcp:{PORT})",
          flush=True)
    for n in notes:
        print("  " + n, flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
