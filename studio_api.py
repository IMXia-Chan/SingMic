"""Studio 的 HTTP 层。

★ 里面的重活(切片段、混音、导出)一律 `run_in_executor`,而且是**独立的** 2 线程池 ——
不能和 `/api/blocks` 抢默认池,否则切块的时候整页的播放/保存都会跟着卡。
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

import studio_asr
import studio_auto
import studio_external
import studio_fx
import studio_mix as sm
import studio_nl
import studio_ref
import studio_vst
import studio_voice
import tuning

ROOT = sm.ROOT
REC = sm.REC
MEDIA = sm.MEDIA
CACHE = sm.CACHE
EXPORTS = sm.EXPORTS
PROJECTS = sm.PROJECTS
PREVIEW = PROJECTS / "preview"

AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg"}
UPLOAD_MAX = 300 * 1024 * 1024
PROJECT_MAX = 4 * 1024 * 1024

_NAME_RE = re.compile(r"^[\w一-鿿 .\-]{1,64}$")
_CACHE_RE = re.compile(r"^(clip|track)_[0-9a-f]{20}\.wav$")

router = APIRouter(prefix="/api/studio", tags=["studio"])
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio")
_inflight: dict[str, asyncio.Future] = {}


# ---------------------------------------------------------------------------
# 效果器参数表 / 导出能力
# ---------------------------------------------------------------------------
@router.get("/fx_types")
async def get_fx_types() -> dict:
    """★ 前端的插件面板由这张表驱动 —— 参数范围/默认值只在这里写一份。"""
    return {"types": studio_fx.FX_TYPES, "max_chain": studio_fx.MAX_CHAIN}


@router.get("/export_capabilities")
async def get_export_capabilities() -> dict:
    ext = studio_external.get_renderer()
    return {"builtin": True, "stems": True, "vst": True,
            "external": bool(ext is not None and ext.available())}


# ---------------------------------------------------------------------------
# 语音识别模型(切分的「文字标注」增强)—— 没有模型也能切,只是没有字
# ---------------------------------------------------------------------------
@router.get("/asr/status")
async def asr_status() -> dict:
    return studio_asr.status()


@router.post("/asr/download")
async def asr_download(req: Request) -> dict:
    body = await req.json()
    which = str(body.get("which") or "all")
    todo = ["whisper", "paraformer"] if which == "all" else [which]
    if any(w not in studio_asr.MODELS for w in todo):
        raise HTTPException(404, "没有这个模型")
    for w in todo:
        studio_asr.start_download(w)
    return {"ok": True, "downloads": studio_asr.status()["downloads"]}


@router.delete("/asr/{which}")
async def asr_delete(which: str) -> dict:
    r = studio_asr.delete_model(which)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "没有这个模型")
    return r


# ---------------------------------------------------------------------------
# AI 补音(延长/缩短):本地为底,云端模型可用时精修
# ★ token 只写不读;只把选中那一段发到你自己配置的服务器。
# ---------------------------------------------------------------------------
@router.get("/voice/status")
async def voice_status(force: int = 0) -> dict:
    loop = asyncio.get_running_loop()
    if force:
        return await loop.run_in_executor(_pool, lambda: _voice_status_fresh())
    return await loop.run_in_executor(_pool, studio_voice.status)


def _voice_status_fresh() -> dict:
    studio_voice._HEALTH["t"] = 0.0
    return studio_voice.status()


@router.post("/voice/config")
async def voice_config(req: Request) -> dict:
    body = await req.json()
    try:
        studio_voice.save_config(body.get("baseUrl"), body.get("token"),
                                 body.get("enabled"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, studio_voice.status)


@router.post("/voice/config/clear")
async def voice_config_clear() -> dict:
    studio_voice.clear_token()
    return studio_voice.cfg_view()


@router.post("/voice/extend")
async def voice_extend(req: Request) -> dict:
    body = await req.json()
    rel = str(body.get("source") or "")
    p = safe_source(rel)
    try:
        a = float(body.get("srcIn") or 0.0)
        b = float(body.get("srcOut") or 0.0)
        target = float(body.get("targetSec") or 0.0)
    except (TypeError, ValueError):
        raise HTTPException(400, "时间参数不是数字")
    if target <= 0:
        raise HTTPException(400, "目标时长要大于 0")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(
        _pool, studio_voice.extend, p, rel, a, b, target, MEDIA,
        bool(body.get("local")))
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "补音失败")
    return r


# ---------------------------------------------------------------------------
# 自然语言调参(DeepSeek):只回动作清单,不改工程;key 只写不读
# ---------------------------------------------------------------------------
@router.get("/nl/status")
async def nl_status() -> dict:
    return studio_nl.status()


@router.post("/nl/key")
async def nl_key(req: Request) -> dict:
    body = await req.json()
    try:
        return studio_nl.set_key(str(body.get("apiKey") or ""),
                                 str(body.get("model") or ""),
                                 str(body.get("baseUrl") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nl/key/clear")
async def nl_key_clear() -> dict:
    return studio_nl.clear_key()


@router.post("/nl_plan")
async def nl_plan(req: Request) -> dict:
    body = await req.json()
    proj = _resolve_project(body)
    scope = body.get("scope")
    if not isinstance(scope, dict) or scope.get("type") not in ("project", "track", "clips"):
        scope = {"type": "project"}
    text = str(body.get("text") or "")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(
        _pool, functools.partial(studio_nl.plan, text, proj, scope,
                                 bool(body.get("sendNames"))))
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "没听出要改什么")
    return r


# ---------------------------------------------------------------------------
# VST3(离线)—— 扫描 / 看参数 / 冻结 / 短段试听
# ★ 真实加载只发生在独立子进程里,这里只做 HTTP 与路径校验。
# ---------------------------------------------------------------------------
def _resolve_project(b: dict) -> dict:
    proj = b.get("project")
    if isinstance(proj, str):
        p = safe_project(proj)
        if not p.exists():
            raise HTTPException(404, "没有这个工程")
        proj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(proj, dict) or not isinstance(proj.get("tracks"), list):
        raise HTTPException(400, "工程结构不对")
    return proj


def _track_of(proj: dict, tid) -> dict:
    for tr in proj.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    raise HTTPException(404, "没有这条轨")


def _out_sr(b: dict) -> int:
    """冻结/试听按**浏览器实际采样率**渲染(导出仍固定 48k)。"""
    try:
        sr = int(b.get("sr") or sm.SR)
    except (TypeError, ValueError):
        raise HTTPException(400, "sr 只收整数")
    if not 8000 <= sr <= 192000:
        raise HTTPException(400, "sr 只收 8000~192000")
    return sr


def _mix_with_vst(project, normalize, start, end, loudness):
    if studio_vst.project_has_vst(project):
        with studio_vst.VstHost() as host:
            return sm.mix_project(project, sm.SR, normalize, start, end,
                                  loudness, vst=host)
    return sm.mix_project(project, sm.SR, normalize, start, end, loudness)


def _stems_with_vst(project, stage, start, end):
    if stage == "post" and studio_vst.project_has_vst(project):
        with studio_vst.VstHost() as host:
            return sm.render_stems(project, sm.SR, stage, start, end, vst=host)
    return sm.render_stems(project, sm.SR, stage, start, end)


@router.get("/vst/plugins")
async def vst_plugins() -> dict:
    """扫允许的插件目录。★ 扫盘可能慢(Program Files),走独立线程池。"""
    loop = asyncio.get_running_loop()
    plugins, probe = await asyncio.gather(
        loop.run_in_executor(_pool, studio_vst.scan_plugins),
        loop.run_in_executor(_pool, studio_vst.probe),
    )
    dirs = [{"path": str(r), "exists": r.is_dir()}
            for r in studio_vst.plugin_roots()]
    return {"dirs": dirs, "main": str(studio_vst.ensure_vst_dir()),
            "max": studio_vst.MAX_PER_TRACK, "plugins": plugins,
            "probe": probe}


@router.post("/vst/inspect")
async def vst_inspect(request: Request) -> dict:
    b = await request.json()
    path = str(b.get("path") or "")
    loop = asyncio.get_running_loop()

    def job():
        with studio_vst.VstHost() as host:
            return host.inspect(path)

    try:
        return await loop.run_in_executor(_pool, job)
    except (studio_vst.VstError, studio_vst.VstUnavailable) as e:
        raise HTTPException(400, str(e))


@router.post("/vst/freeze")
async def vst_freeze(request: Request) -> dict:
    """把这一轨(增益 → 内置链 → VST)烘焙成一条 WAV,给正常播放用。"""
    b = await request.json()
    proj = _resolve_project(b)
    tr = _track_of(proj, b.get("trackId"))
    fx = tr.get("fx") or {}
    if not (isinstance(fx, dict) and fx.get("vst")):
        raise HTTPException(400, "这一轨没有 VST 插件")
    sr = _out_sr(b)
    sig = studio_vst.track_sig(proj, tr, sr)
    dst = CACHE / f"track_{sig}.wav"
    cached = dst.is_file()
    nondet = None
    t0 = time.perf_counter()
    if not cached:
        loop = asyncio.get_running_loop()

        def job():
            with studio_vst.VstHost() as host:
                y = sm.render_track_post(proj, tr, sr, 0.0, None, vst=host)
                # ★ 确定性探针:第一次冻结**连跑两遍**。
                #   有的插件内部随机 / LFO free-run,两次结果就不同 ——
                #   那就老实告诉用户「冻结只是预览」,不装作每次都一样。
                #   命中缓存时不再跑(缓存里已经是同一次结果)。
                nd = None
                try:
                    y2 = sm.render_track_post(proj, tr, sr, 0.0, None, vst=host)
                    nd = studio_vst.nondet_of(y, y2)
                except Exception:           # noqa: BLE001
                    nd = True
                # ★ 空轨不写文件:0 采样的 WAV 浏览器 decodeAudioData 会直接报
                #   「Unable to decode audio data」,看着像插件坏了。
                if len(y):
                    sm.write_wav_atomic(dst, y, sr, "FLOAT")
                return int(len(y)), nd

        try:
            _, nondet = await loop.run_in_executor(_pool, job)
        except (studio_vst.VstError, studio_vst.VstUnavailable) as e:
            raise HTTPException(400, str(e))
    try:
        info = sf.info(str(dst))
        sec = round(float(info.frames) / float(info.samplerate), 3)
    except Exception:                       # noqa: BLE001
        sec = 0.0
    if sec <= 0:
        # 空轨(没有任何 clip):无需冻结,也不该留一个 0 字节 WAV 在缓存里
        try:
            if dst.is_file():
                dst.unlink()
        except OSError:
            pass
        return {"sig": sig, "url": None, "empty": True,
                "cached": False, "sr": sr, "sec": 0.0, "nondet": False,
                "gain": float(tr.get("gain", 1) or 0),
                "sec_cost": round(time.perf_counter() - t0, 2)}
    return {"sig": sig, "url": f"/api/studio/cache/{dst.name}",
            "cached": cached, "sr": sr, "sec": sec, "nondet": nondet,
            "gain": float(tr.get("gain", 1) or 0),
            "sec_cost": round(time.perf_counter() - t0, 2)}


@router.post("/vst/audition")
async def vst_audition(request: Request):
    """短段试听:只算 t0..t1,边拖参数边刷新(约 150ms 一次)。"""
    b = await request.json()
    proj = _resolve_project(b)
    tr = _track_of(proj, b.get("trackId"))
    t0 = max(0.0, float(b.get("t0") or 0))
    t1 = max(t0 + 0.05, float(b.get("t1") or (t0 + 4.0)))
    end_all = max(t0 + 0.05, sm.project_end(proj))
    t1 = min(t1, end_all)
    solo = bool(b.get("solo", True))
    bypass = bool(b.get("bypass", False))
    sr = _out_sr(b)
    loop = asyncio.get_running_loop()

    def job():
        p = proj
        if bypass:
            p = json.loads(json.dumps(proj))
            for t in p.get("tracks") or []:
                fx = t.get("fx")
                if isinstance(fx, dict):
                    fx.pop("vst", None)
        if solo:
            tr2 = _track_of(p, tr.get("id"))
            with studio_vst.VstHost() as host:
                y = sm.render_track_post(p, tr2, sr, t0, t1, vst=host)
            return y
        with studio_vst.VstHost() as host:
            y, _ = sm.mix_project(p, sr, False, t0, t1, None, vst=host)
        return y

    t_start = time.perf_counter()
    try:
        y = await loop.run_in_executor(_pool, job)
    except (studio_vst.VstError, studio_vst.VstUnavailable) as e:
        raise HTTPException(400, str(e))
    buf = io.BytesIO()
    sf.write(buf, np.asarray(y, dtype=np.float32), sr,
             format="WAV", subtype="PCM_24")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"Cache-Control": "no-store",
                             "X-Render-Ms": str(int((time.perf_counter() - t_start) * 1000))})


# ---------------------------------------------------------------------------
# 路径安全 —— ★ 不能复用 server._safe(),它只认 recordings/*.wav
# ---------------------------------------------------------------------------
def _inside(p: Path, roots: tuple[Path, ...]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            if rp.is_relative_to(r.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def safe_project(name: str) -> Path:
    if not _NAME_RE.match(name or "") or name.startswith("."):
        raise HTTPException(400, "工程名不合规")
    sm.ensure_dirs()
    p = PROJECTS / f"{name}.json"
    if not _inside(p, (PROJECTS,)):
        raise HTTPException(400, "工程名不合规")
    return p


def safe_source(rel: str) -> Path:
    if not rel or "\x00" in rel:
        raise HTTPException(400, "没有这个文件")
    p = (ROOT / rel).resolve()
    if not _inside(p, (REC, MEDIA)):
        raise HTTPException(404, "没有这个文件")
    if p.suffix.lower() not in AUDIO_EXT or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    return p


def safe_cache(name: str) -> Path:
    if not _CACHE_RE.match(name or ""):
        raise HTTPException(404, "没有这个文件")
    return CACHE / name


def safe_media(name: str) -> Path:
    p = (MEDIA / name).resolve()
    if not _inside(p, (MEDIA,)) or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    return p


def _rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def _probe(p: Path) -> dict:
    try:
        i = sf.info(str(p))
        return {"exists": True, "sr": int(i.samplerate), "ch": int(i.channels),
                "dur": round(float(i.duration), 3)}
    except Exception:                      # noqa: BLE001
        return {"exists": False, "sr": 0, "ch": 0, "dur": 0.0}


# ---------------------------------------------------------------------------
# 工程 CRUD
# ---------------------------------------------------------------------------
@router.get("/projects")
async def list_projects() -> dict:
    sm.ensure_dirs()
    out = []
    for p in sorted(PROJECTS.glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            continue
        out.append({
            "name": j.get("name") or p.stem,
            "title": j.get("title") or "",
            "rev": int(j.get("rev") or 0),
            "bpm": j.get("bpm") or 120,
            "lengthSec": j.get("lengthSec") or 0,
            "tracks": len(j.get("tracks") or []),
            "modified": int(p.stat().st_mtime),
        })
    out.sort(key=lambda d: -d["modified"])
    return {"projects": out}


@router.get("/projects/{name}")
async def get_project(name: str) -> dict:
    p = safe_project(name)
    if not p.exists():
        raise HTTPException(404, "没有这个工程")
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                 # noqa: BLE001
        raise HTTPException(500, f"工程文件坏了:{e}")
    srcs = {}
    for sid, s in (j.get("sources") or {}).items():
        try:
            path = safe_source(s.get("path", ""))
            srcs[sid] = {**_probe(path), "path": s.get("path"), "name": s.get("name", "")}
        except HTTPException:
            srcs[sid] = {"exists": False, "sr": 0, "ch": 0, "dur": 0.0,
                         "path": s.get("path"), "name": s.get("name", "")}
    return {"project": j, "sources": srcs}


@router.put("/projects/{name}")
async def put_project(name: str, request: Request) -> dict:
    p = safe_project(name)
    raw = await request.body()
    if len(raw) > PROJECT_MAX:
        raise HTTPException(413, "工程太大")
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "不是合法的 JSON")
    if not isinstance(j, dict) or not isinstance(j.get("tracks"), list):
        raise HTTPException(400, "工程结构不对")

    old_rev = 0
    if p.exists():
        try:
            old_rev = int(json.loads(p.read_text(encoding="utf-8")).get("rev") or 0)
        except Exception:                  # noqa: BLE001
            old_rev = 0
    got = j.get("rev")
    if got is not None and int(got) != old_rev:
        raise HTTPException(409, f"别处已经改过了(rev {got} ≠ {old_rev})")

    j["rev"] = old_rev + 1
    j["name"] = name
    j.setdefault("version", 1)

    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
    if p.exists():
        try:
            p.replace(p.with_name(p.name + ".bak"))
        except OSError:
            pass
    tmp.replace(p)
    return {"ok": True, "rev": j["rev"], "modified": int(p.stat().st_mtime)}


@router.delete("/projects/{name}")
async def delete_project(name: str) -> dict:
    """删掉一个工程。

    ★ **只删工程 JSON**(连同它的 .bak/.part),`projects/media/` 里的音频一个都不动 ——
      媒体库是多个工程共享的,删了会把别的工程一起弄哑。
      没被引用的渲染缓存由「缓存」按钮那边清。
    ★ 当前打开的工程被删掉时,前端必须换一个/新建,否则自动保存会把文件写回来。
    """
    p = safe_project(name)
    if not p.exists():
        raise HTTPException(404, "没有这个工程")
    try:
        p.unlink()
    except OSError as e:
        raise HTTPException(500, f"删不掉:{e}")
    for extra in (p.with_name(p.name + ".bak"), p.with_name(p.name + ".part")):
        try:
            if extra.exists():
                extra.unlink()
        except OSError:
            pass
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# 上传 / 取音频
# ---------------------------------------------------------------------------
@router.post("/upload")
async def upload(request: Request, name: str = "") -> dict:
    """★ 故意不用 multipart —— 那样要装 python-multipart。

    浏览器直接把文件字节扔过来,名字走查询参数。
    """
    sm.ensure_dirs()
    base = Path(name or "").name
    ext = Path(base).suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(415, "只收 wav / mp3 / flac / ogg(其余格式导出时会失败)")
    if not base or "\x00" in base:
        raise HTTPException(400, "文件名不对")

    tmp = MEDIA / f"upload_{int(time.time() * 1000)}{ext}.part"
    total = 0
    sha = hashlib.sha1()
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > UPLOAD_MAX:
                    raise HTTPException(413, "文件太大(上限 300MB)")
                f.write(chunk)
                sha.update(chunk)
        try:
            # ★ 传文件对象,不传路径 —— 别让 libsndfile 去猜 `.part` 后缀
            with open(tmp, "rb") as fh:
                info = sf.info(fh)
        except Exception as e:             # noqa: BLE001
            raise HTTPException(415, f"这个文件读不了:{e}")
        # 内容寻址:同一份文件传两次只留一份
        dst = MEDIA / f"{sha.hexdigest()[:8]}_{base}"
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return {"source": {
        "path": _rel(dst), "name": base,
        "sr": int(info.samplerate), "ch": int(info.channels),
        "dur": round(float(info.duration), 3), "size": dst.stat().st_size,
    }}


@router.get("/media_list")
async def list_media() -> dict:
    """已导入的媒体(不含 upload_*.part)。"""
    sm.ensure_dirs()
    out = []
    for p in sorted(MEDIA.iterdir(), key=lambda q: -q.stat().st_mtime):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        # ★ 下划线开头 = 自测/冒烟用的临时音频(和 recordings 的约定一致),
        #   不列给用户看。它们还能被工程直接引用(路径访问不受影响),
        #   只是不该在「媒体」里冒充成他导入的素材。
        if p.name.startswith(("_", ".")):
            continue
        info = _probe(p)
        out.append({"name": p.name, "path": _rel(p), "size": p.stat().st_size, **info})
    return {"files": out}


@router.post("/media_rename")
async def media_rename(request: Request) -> dict:
    """给导入的媒体文件改名,并把所有工程里的引用一起改掉。

    ★ 只改 `projects/media/`(recordings/ 里是录音,被多处按文件名索引,不动)。
    ★ 盘上的文件改了、工程 JSON 也一起改 —— 否则页面自动保存会把旧路径写回去。
    ★ sourceId 的 key 不动:clip 靠它引用,不动就不会串。
    """
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    old_in = str(body.get("old") or "")
    new_in = str(body.get("new") or "")

    # 老名字:接受文件名或 projects/media/xxx 这种相对路径
    if not old_in or "\x00" in old_in:
        raise HTTPException(400, "老文件名是空的")
    oldp = safe_media(Path(old_in.replace("\\", "/")).name)

    # 新名字:只取文件名,带目录的一律拒绝
    if "/" in new_in or "\\" in new_in or "\x00" in new_in:
        raise HTTPException(400, "名字里不能带路径")
    newname = new_in.strip()
    if not newname:
        raise HTTPException(400, "新名字是空的")
    if newname.startswith(("_", ".")):
        raise HTTPException(400, "名字不能以下划线或点开头")
    if len(newname) > 120:
        raise HTTPException(400, "名字太长")
    if Path(newname).suffix.lower() not in AUDIO_EXT:
        raise HTTPException(415, "扩展名必须是 wav / mp3 / flac / ogg")
    if any(c in newname for c in '<>:"|?*'):
        raise HTTPException(400, "名字里有 Windows 不允许的字符")

    dst = (MEDIA / newname)
    oldname = oldp.name
    same = os.path.normcase(str(oldp.resolve())) == os.path.normcase(str(dst.resolve()))
    if same and newname == oldname:
        return {"ok": True, "name": newname, "rel": _rel(oldp), "updated": []}
    if dst.exists() and not same:
        raise HTTPException(409, "已经有同名文件")

    # 现在盘上所有引用这个文件的工程(先把原文读出来,失败要还原)
    old_rel = _rel(oldp)
    touched: list[Path] = []
    originals: dict[Path, str] = {}
    changed: dict[Path, dict] = {}
    for pj in sorted(PROJECTS.glob("*.json")):
        try:
            text = pj.read_text(encoding="utf-8")
            j = json.loads(text)
        except Exception:                  # noqa: BLE001
            continue
        hit = False
        for s in (j.get("sources") or {}).values():
            sp = str((s or {}).get("path") or "")
            if not sp:
                continue
            try:
                if (ROOT / sp).resolve() == oldp.resolve():
                    s["path"] = _rel(dst)
                    s["name"] = newname
                    hit = True
            except (OSError, ValueError):
                continue
        if hit:
            originals[pj] = text
            changed[pj] = j          # ★ 写的是这份改过的,不是重新读一遍原文
            touched.append(pj)

    # 改盘上的文件。只改大小写时 Windows 会当成同一个路径,必须走临时名。
    renamed = False
    try:
        if same:
            tmp = oldp.with_name(oldp.name + ".rename_tmp")
            oldp.rename(tmp)
            tmp.rename(dst)
        else:
            oldp.rename(dst)
        renamed = True

        for pj in touched:
            tmp = pj.with_name(pj.name + ".part")
            tmp.write_text(json.dumps(changed[pj], ensure_ascii=False, indent=1),
                           encoding="utf-8")
            try:
                pj.replace(pj.with_name(pj.name + ".bak"))
            except OSError:
                pass
            tmp.replace(pj)
    except Exception as e:                 # noqa: BLE001
        # 文件改了、工程没写完 ⇒ 全部退回去,不留半截状态
        for pj, text in originals.items():
            try:
                pj.write_text(text, encoding="utf-8")
            except OSError:
                pass
        if renamed:
            try:
                dst.rename(oldp)
            except OSError:
                pass
        raise HTTPException(500, f"改名失败,已退回:{e}")

    return {"ok": True, "name": newname, "rel": _rel(dst), "old": old_rel,
            "updated": [p.stem for p in touched]}


def _auto_work(proj: dict, scopes, tune: bool) -> dict:
    """测量 + 出方案。★ 只读,一个字节都不写盘。"""
    if studio_vst.project_has_vst(proj):
        try:
            with studio_vst.VstHost() as host:
                return studio_auto.auto_mix(proj, scopes, tune, host)
        except studio_vst.VstError as e:
            # 插件宿主起不来也照样给其余方案(音量/内置链/修音)
            only = [s for s in (scopes if isinstance(scopes, list)
                                else ["vol", "fx", "tune"]) if s != "vst"]
            d = studio_auto.auto_mix(proj, only, tune, None)
            d["notes"].insert(0, f"VST 宿主起不来,插件没动:{e}")
            return d
    return studio_auto.auto_mix(proj, scopes, tune, None)


@router.post("/auto_mix")
async def auto_mix(request: Request) -> dict:
    """一键调音:**只出方案**,不写工程。前端勾选后自己应用 + 一次撤销。

    body: {project | project名, scopes?: "all"|["vol","fx","vst","tune"], tune?: bool}
    """
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    proj = _resolve_project(body)
    scopes = body.get("scopes") or "all"
    if isinstance(scopes, str):
        scopes = "all"
    elif isinstance(scopes, list):
        scopes = [str(s) for s in scopes]
    else:
        scopes = "all"
    tune = bool(body.get("tune", True))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _pool, functools.partial(_auto_work, proj, scopes, tune))


@router.post("/reference/analyze")
async def reference_analyze(request: Request) -> dict:
    """按原唱矫正:**只出方案**,不写工程。

    body: {project | project名, refTrackId, takeTrackId,
           offset?: "auto"|秒, strength?: 0.9, mode?: "intonation"|"match"}
    """
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    proj = _resolve_project(body)
    ref_id = body.get("refTrackId")
    take_id = body.get("takeTrackId")
    if ref_id in (None, "") or take_id in (None, ""):
        raise HTTPException(400, "要先选「原唱轨」和「我的轨」")
    off = body.get("offset", "auto")
    if not isinstance(off, (int, float)):
        off = "auto"
    try:
        strength = float(body.get("strength", 0.9))
    except (TypeError, ValueError):
        strength = 0.9
    strength = max(0.1, min(1.0, strength))
    mode = str(body.get("mode") or "intonation")
    if mode not in ("intonation", "match"):
        mode = "intonation"
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _pool, functools.partial(
                studio_ref.analyze, proj, ref_id, take_id,
                offset=off, strength=strength, mode=mode))
    except studio_ref.RefError as e:
        raise HTTPException(400, str(e))


@router.post("/reference/separate")
async def reference_separate(request: Request) -> dict:
    """用 Demucs 把一条带伴奏的音频分成「人声 / 伴奏」。CPU,慢;结果按源缓存。

    body: {source: "projects/media/xxx.wav"}  → {sources:[{path,name,...}, ...]}
    """
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    src = safe_source(str(body.get("source") or ""))
    ok, why = studio_ref.demucs_available()
    if not ok:
        raise HTTPException(400, f"{studio_ref.MSG_DEMUCS}({why})")

    # ★ 目标名由「源的路径+大小+纳秒 mtime」决定 ⇒ 同一份源再点一次直接命中,
    #   不重跑 Demucs。名字照样是 `<8位哈希>_<原名>_人声.wav`,和导入的文件同一套。
    ident = hashlib.sha1(
        f"{src}|{src.stat().st_size}:{src.stat().st_mtime_ns}".encode("utf-8")).hexdigest()[:8]
    stem = Path(src.name).stem
    dsts = {"vocals": MEDIA / f"{ident}_{stem}_人声.wav",
            "instrumental": MEDIA / f"{ident}_{stem}_伴奏.wav"}
    if all(p.exists() for p in dsts.values()):
        outs = [{"path": _rel(dsts[k]), "name": dsts[k].name,
                 "kind": "voice" if k == "vocals" else "audio", **_probe(dsts[k])}
                for k in ("vocals", "instrumental")]
        return {"ok": True, "sources": outs, "cached": True, "sec_cost": 0}

    out_dir = PROJECTS / "tmp" / f"demucs_{ident}"
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(
            _pool, functools.partial(studio_ref.separate_vocals, src, out_dir))
    except studio_ref.RefError as e:
        raise HTTPException(400, str(e))
    try:
        for key in ("vocals", "instrumental"):
            p = Path(r[key])
            tmp = dsts[key].with_name(dsts[key].name + ".part")
            tmp.write_bytes(p.read_bytes())
            tmp.replace(dsts[key])
    except OSError as e:
        raise HTTPException(500, f"读不出 Demucs 的输出:{e}")
    outs = []
    for tag, key in (("人声", "vocals"), ("伴奏", "instrumental")):
        outs.append({"path": _rel(dsts[key]), "name": dsts[key].name,
                     "kind": "voice" if key == "vocals" else "audio",
                     **_probe(dsts[key])})
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)
    return {"ok": True, "sources": outs, "cached": False,
            "sec_cost": r.get("sec_cost", 0)}


@router.get("/reference/library")
async def reference_library(role: str = "ref") -> dict:
    """歌库里有哪些歌。★ 只列派生数据(音高地图),音频还在 media 里。"""
    import studio_refdb as rdb
    es = rdb.entries(role=(role if role in ("ref", "take") else None))
    total = sum(1 for _ in es)
    return {"ok": True, "entries": es, "count": total,
            "dir": "projects/refdb",
            "note": ("每首歌只存「音高地图」和色度指纹(约 100~200KB),不复制音频。"
                     "删掉这里的记录不影响你的音频文件。")}


@router.delete("/reference/library/{eid}")
async def reference_library_delete(eid: str) -> dict:
    import studio_refdb as rdb
    if not re.fullmatch(r"[0-9a-f]{8,32}", eid or ""):
        raise HTTPException(400, "id 不对")
    ok = rdb.delete(eid)
    if not ok:
        raise HTTPException(404, "歌库里没有这条")
    return {"ok": True, "id": eid}


def _identify_work(proj: dict, take_id) -> dict:
    import studio_refdb as rdb
    take_tr = studio_ref._find_track(proj, take_id)          # noqa: SLF001
    clips = studio_ref._usable_clips(proj, take_tr)          # noqa: SLF001
    if not clips:
        raise studio_ref.RefError(f"「{take_tr.get('name') or take_id}」上没有能读的音频")
    infos = {}
    for c in clips:
        got = studio_ref._clip_audio(proj, c)                # noqa: SLF001
        if not got:
            continue
        path, src = got
        k = str(path)
        if k in infos:
            continue
        infos[k] = studio_ref.analyse_source(path, role="take", name=src.get("name"))
    # ★ 一条轨通常只有一个源;有多个源时按时间拼会拼错,只用第一个并说明
    if not infos:
        raise studio_ref.RefError("读不出这条轨的音频")
    first = next(iter(infos.values()))
    cand = rdb.identify(first["cents"], first["times"], first["voiced"])
    return {"ok": True, "candidates": cand,
            "best": next((c for c in cand if c.get("okAll")), None),
            "take": {"trackId": take_tr.get("id"), "name": take_tr.get("name")}}


@router.post("/reference/identify")
async def reference_identify(request: Request) -> dict:
    """拿你这条轨去歌库里认歌:返回最像的几首 + 把握。

    ★ 认的是「唱的是不是同一首歌」(音高类序列 + 时间滑动),不是音色/是不是原版录音。
    """
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    proj = _resolve_project(body)
    take_id = body.get("takeTrackId")
    if take_id in (None, ""):
        raise HTTPException(400, "要选「我的轨」")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _pool, functools.partial(_identify_work, proj, take_id))
    except studio_ref.RefError as e:
        raise HTTPException(400, str(e))


@router.get("/media/{name}")
async def get_media(name: str) -> FileResponse:
    p = safe_media(name)
    mt = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
          ".ogg": "audio/ogg"}.get(p.suffix.lower(), "application/octet-stream")
    # ★ 不带 filename= —— 否则浏览器当附件下载,<audio>/decodeAudioData 都不好用
    return FileResponse(str(p), media_type=mt,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.delete("/media/{name}")
async def delete_media(name: str) -> dict:
    """真删 `projects/media/` 里的一个文件。

    ★ 只删盘上的音频;工程 JSON 不动 —— 用到它的工程会变「缺源」并标红,
      所以界面在删之前先问 `/usage` 把工程名报给用户。
    ★ 派生缓存(clip_*.wav)是内容寻址的,由缓存的清理口回收。
    """
    p = safe_media(name)
    try:
        p.unlink()
    except OSError as e:
        raise HTTPException(500, f"删不掉:{e}")
    return {"ok": True, "name": name}


@router.post("/usage")
async def media_usage(request: Request) -> dict:
    """这个录音/媒体文件被哪些工程引用着(删之前先说清楚)。"""
    try:
        body = await request.json()
    except Exception:                      # noqa: BLE001
        raise HTTPException(400, "要发 JSON")
    rel = str(body.get("path") or "")
    if not rel or "\x00" in rel:
        raise HTTPException(400, "路径不对")
    p = (ROOT / rel.replace("\\", "/")).resolve()
    if not _inside(p, (REC, MEDIA)):
        raise HTTPException(400, "路径不对")
    used: list[dict] = []
    for pj in sorted(PROJECTS.glob("*.json")):
        try:
            j = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            continue
        for s in (j.get("sources") or {}).values():
            sp = str((s or {}).get("path") or "")
            if not sp:
                continue
            try:
                if (ROOT / sp).resolve() == p:
                    used.append({"file": pj.stem, "title": j.get("title") or pj.stem})
                    break
            except (OSError, ValueError):
                continue
    return {"projects": used}


_EXPORT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}\.(wav|mp3|zip)$")


@router.get("/exports/{name}")
async def get_export(name: str) -> FileResponse:
    if not _EXPORT_RE.match(name or ""):
        raise HTTPException(404, "没有这个文件")
    p = (EXPORTS / name).resolve()
    if not _inside(p, (EXPORTS,)) or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    mt = {".mp3": "audio/mpeg", ".zip": "application/zip"}.get(
        p.suffix.lower(), "audio/wav")
    # ★ 导出文件是拿来下载的,这里**可以**带 filename=
    return FileResponse(str(p), media_type=mt, filename=p.name)


@router.get("/preview/{name}")
async def get_preview(name: str) -> FileResponse:
    """「渲染试听整曲」的结果。★ 不带 filename= —— 要能在 <audio> 里直接播。"""
    if not _EXPORT_RE.match(name or ""):
        raise HTTPException(404, "没有这个文件")
    p = (PREVIEW / name).resolve()
    if not _inside(p, (PREVIEW,)) or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    mt = {".mp3": "audio/mpeg"}.get(p.suffix.lower(), "audio/wav")
    return FileResponse(str(p), media_type=mt,
                        headers={"Cache-Control": "no-store"})


def _write_mp3(x: np.ndarray, sr: int, dst: Path, kbps: int = 320) -> None:
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(int(kbps))
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(int(x.shape[1]))
    enc.set_quality(2)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    data = enc.encode(pcm.tobytes()) + enc.flush()
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dst)


_STEMS_README = """SingMic Studio —— 分轨导出(stems)

这个包里每一轨一条单声道 24-bit WAV,采样率与导出主混音相同(默认 48k)。

stage = pre   链路在「插入效果链之前」:已经过轨道增益、片段淡入淡出,
              不含声像(pan),也不含旧的混响 / 延迟支路。
              ⇒ 把各轨按声像相加,就是「关掉插件链和混响/延迟」的主混音。
stage = post  已经过插入效果链(插件),同样含轨道增益,不含声像与旧混响/延迟。

用法:直接拖进 FL Studio / Reaper / 任何 VST 宿主,按 project.json 里的
轨道名、声像、增益重建即可。project.json 是导出时那一刻的完整工程快照。
manifest.json 记录每轨的样本数、起始时间与缺失的源文件。

★ 轨道的声像与旧的 reverb/delay 不在 stems 里 —— 它们属于混音台,
  请在宿主里按 project.json 重建。本包不带任何用户录音以外的隐私信息。
"""


def _stem_file_name(name: str, idx: int) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name or "")).strip("_")[:40]
    return f"{idx:02d}_{s or 'track'}.wav"


def _write_stems_zip(dst: Path, stems: list[dict], project: dict, sr: int,
                     stage: str, start: float, end: float,
                     missing: list[str]) -> dict:
    """把分轨 + 工程快照 + 清单打成一个 zip(.part 写完再 rename)。"""
    tracks = []
    tmp = dst.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, st in enumerate(stems, 1):
            fn = _stem_file_name(st.get("name"), i)
            buf = io.BytesIO()
            sf.write(buf, np.asarray(st["audio"], dtype=np.float32), sr,
                     format="WAV", subtype="PCM_24")
            zf.writestr(fn, buf.getvalue())
            tracks.append({"file": fn, "id": st.get("id"), "name": st.get("name"),
                           "samples": int(len(st["audio"])), "sr": sr,
                           "channels": 1})
        manifest = {"version": 1, "kind": "stems", "stage": stage,
                    "sr": sr, "channels": 1, "subtype": "PCM_24",
                    "start": round(float(start), 6),
                    "end": round(float(end), 6),
                    "tracks": tracks, "missing": missing}
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("project.json",
                    json.dumps(project, ensure_ascii=False, indent=2))
        zf.writestr("README.txt", _STEMS_README)
    tmp.replace(dst)
    return {"tracks": len(tracks), "missing": missing}


@router.post("/export")
async def export_project(request: Request) -> dict:
    """整曲导出。★ 走服务端 numpy,不走 OfflineAudioContext ——
    播放和导出必须是同一批缓存片段 + 同一套包络公式。"""
    b = await request.json()
    proj = b.get("project")
    if isinstance(proj, str):
        p = safe_project(proj)
        if not p.exists():
            raise HTTPException(404, "没有这个工程")
        proj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(proj, dict) or not isinstance(proj.get("tracks"), list):
        raise HTTPException(400, "工程结构不对")
    fmt = str(b.get("format") or "wav").lower()
    # stems: false | true | "pre" | "post" —— 分轨导出时格式固定是 zip
    raw_stems = b.get("stems", False)
    if isinstance(raw_stems, str):
        s_low = raw_stems.strip().lower()
        want_stems = s_low in ("pre", "post", "true", "1", "on")
        stage = "post" if s_low == "post" else "pre"
    else:
        want_stems = raw_stems is True
        stage = "pre"
    if not want_stems and fmt not in ("wav", "mp3"):
        raise HTTPException(415, "只导 wav / mp3")
    pipeline = str(b.get("pipeline") or "builtin").lower()
    if pipeline not in ("builtin", "external"):
        raise HTTPException(400, "pipeline 只认 builtin / external")
    renderer = None
    if pipeline == "external":
        if want_stems:
            raise HTTPException(400, "外部渲染器模式下不再打包分轨(先导一次 stems 再用宿主)")
        renderer = studio_external.get_renderer()
        if renderer is None or not renderer.available():
            raise HTTPException(400, "还没接外部渲染器 —— 现在只有内置效果器")
    normalize = bool(b.get("normalize", False))
    # loudness:null / 0 = 关;否则按 LUFS 归一 + 5ms 前瞻限幅(给 -14 / -9 两档)
    loud = b.get("loudness", None)
    loudness = None
    if loud not in (None, False, 0, "", "off"):
        try:
            loudness = float(loud)
        except (TypeError, ValueError):
            raise HTTPException(400, "loudness 必须是个数(-14 / -9)或 null")
        if not -40.0 <= loudness <= -5.0:
            raise HTTPException(400, "loudness 只收 -40 ~ -5 LUFS")
    rng = b.get("range") or None
    start = float(rng[0]) if isinstance(rng, list) and len(rng) > 0 else 0.0
    end = float(rng[1]) if isinstance(rng, list) and len(rng) > 1 else None

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    stem_name = re.sub(r"[^A-Za-z0-9_\-]", "", str(proj.get("name") or "studio"))[:40] or "studio"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # ---- 分轨:每轨一条单声道 wav,打 zip 给以后的 VST / 别的 DAW ----
    if want_stems:
        out = EXPORTS / f"{stem_name}_{stamp}_stems.zip"
        try:
            stems, missing = await loop.run_in_executor(
                _pool, functools.partial(_stems_with_vst, proj, stage, start, end))
            real_end = float(end) if end is not None else sm.project_end(proj)
            pack = await loop.run_in_executor(
                _pool, functools.partial(_write_stems_zip, out, stems, proj,
                                         sm.SR, stage, start, real_end, missing))
        except (studio_vst.VstError, studio_vst.VstUnavailable) as e:
            raise HTTPException(400, f"VST 失败:{e}")
        except Exception as e:             # noqa: BLE001
            raise HTTPException(500, f"分轨导出失败:{e}")
        return {"url": f"/api/studio/exports/{out.name}", "file": out.name,
                "format": "zip", "size": out.stat().st_size,
                "stems": stage, "tracks": pack["tracks"],
                "missing": pack["missing"], "sec": 0.0,
                "sec_cost": round(time.perf_counter() - t0, 2)}

    # ---- 外部渲染器(现在必然没有,走到这儿说明以后接上了)----
    if renderer is not None:
        stems, missing = await loop.run_in_executor(
            _pool, functools.partial(sm.render_stems, proj, sm.SR, "pre",
                                     start, end))
        pairs = [(st["name"], st["audio"]) for st in stems]
        try:
            audio = await loop.run_in_executor(
                _pool, functools.partial(renderer.render, pairs, proj, sm.SR))
        except Exception as e:             # noqa: BLE001
            raise HTTPException(500, f"外部渲染失败:{e}")
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 2 or audio.shape[1] != 2:
            raise HTTPException(500, "外部渲染器必须返回 (n, 2) 立体声")
        rep = {"sec": round(len(audio) / sm.SR, 3),
               "peak": round(float(np.max(np.abs(audio))) if len(audio) else 0.0, 4),
               "missing": missing, "pipeline": "external"}
    else:
        try:
            audio, rep = await loop.run_in_executor(
                _pool, functools.partial(_mix_with_vst, proj, normalize,
                                         start, end, loudness))
        except (studio_vst.VstError, studio_vst.VstUnavailable) as e:
            raise HTTPException(400, f"VST 失败:{e}")

    # preview=True:输出到 projects/preview/,给「渲染试听整曲」用(只留最近 20 个)
    want_preview = bool(b.get("preview", False))
    out_dir = PREVIEW if want_preview else EXPORTS
    if want_preview:
        PREVIEW.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem_name}_{stamp}.{fmt}"
    try:
        if fmt == "wav":
            await loop.run_in_executor(
                _pool, sm.write_wav_atomic, out, audio, sm.SR, "PCM_24")
        else:
            await loop.run_in_executor(
                _pool, _write_mp3, audio, sm.SR, out, 320)
    except Exception as e:                 # noqa: BLE001
        raise HTTPException(500, f"导出失败:{e}")
    if want_preview:
        url = f"/api/studio/preview/{out.name}"
        try:
            old = sorted(PREVIEW.glob("*.*"),
                         key=lambda q: q.stat().st_mtime, reverse=True)
            for q in old[20:]:
                q.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        url = f"/api/studio/exports/{out.name}"
    rep.update({"url": url, "file": out.name,
                "format": fmt, "size": out.stat().st_size,
                "preview": want_preview,
                "sec_cost": round(time.perf_counter() - t0, 2)})
    return rep


def _referenced_keys() -> set[str]:
    """所有工程里还被引用的缓存 key。

    ★ 片段 key(clip.renderKey)+ 采样池每一条(sample.pool[].key)。
      键里带 size:mtime,源文件一变 key 就变 —— 旧文件不会自动消失,所以要有清理。
    """
    keys: set[str] = set()
    for p in PROJECTS.glob("*.json"):
        try:
            proj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            continue
        for tr in proj.get("tracks") or []:
            for c in tr.get("clips") or []:
                if c.get("renderKey"):
                    keys.add(str(c["renderKey"]))
            smp = tr.get("sample") or {}
            for e in smp.get("pool") or []:
                if isinstance(e, dict) and e.get("key"):
                    keys.add(str(e["key"]))
    return keys


@router.get("/cache_info")
async def cache_info() -> dict:
    files = list(CACHE.glob("clip_*.wav"))
    track_files = list(CACHE.glob("track_*.wav"))
    total = sum(f.stat().st_size for f in files + track_files)
    used = _referenced_keys()
    have = {f.name[5:-4] for f in files}
    used_tracks = _referenced_track_sigs()
    have_tracks = {f.name[6:-4] for f in track_files}
    return {"files": len(files) + len(track_files),
            "track_files": len(track_files),
            "bytes": total,
            "used": len(used & have) + len(used_tracks & have_tracks),
            "orphan": len(have - used) + len(have_tracks - used_tracks),
            "mb": round(total / 1048576.0, 1)}


@router.post("/cache_clean")
async def cache_clean(request: Request) -> dict:
    """只删**没有任何工程引用**的缓存。删了不会丢东西 —— 下次用到会重新算。"""
    b = {}
    try:
        b = await request.json()
    except Exception:                       # noqa: BLE001
        pass
    include_used = bool(b.get("all"))       # all=true 连还在用的也删(慎用)
    used = _referenced_keys()
    used_sigs = _referenced_track_sigs()
    n = freed = 0
    targets = [(f, f.name[5:-4], used) for f in CACHE.glob("clip_*.wav")]
    targets += [(f, f.name[6:-4], used_sigs) for f in CACHE.glob("track_*.wav")]
    for f, key, refs in targets:
        if not include_used and key in refs:
            continue
        try:
            freed += f.stat().st_size
            f.unlink()
            n += 1
        except OSError:
            pass
    return {"removed": n, "freed_mb": round(freed / 1048576.0, 1)}


# ★ 冻结是按**浏览器实际采样率**渲染的,签名里带 sr。
#   只按 sm.SR 算,浏览器是 44.1k 时它那条 track_*.wav 就成了「孤儿」被删掉。
#   这里把所有常见上下文采样率都算一遍(只是 sha1,开销可以忽略)。
TRACK_SIG_RATES = (48000, 44100, 32000, 22050, 88200, 96000, 176400, 192000)


def _referenced_track_sigs() -> set[str]:
    """工程里「现在这一轨」应有的冻结签名。对不上的 track_*.wav 就是孤儿。"""
    sigs: set[str] = set()
    for p in PROJECTS.glob("*.json"):
        try:
            proj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            continue
        for tr in proj.get("tracks") or []:
            fx = tr.get("fx")
            if not (isinstance(fx, dict) and fx.get("vst")):
                continue
            for sr in TRACK_SIG_RATES:
                try:
                    sigs.add(studio_vst.track_sig(proj, tr, sr))
                except Exception:          # noqa: BLE001
                    continue
    return sigs


@router.get("/cache/{name}")
async def get_cache(name: str) -> FileResponse:
    p = safe_cache(name)
    if not p.exists():
        raise HTTPException(404, "没有这个缓存")
    return FileResponse(str(p), media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------------------
# 片段渲染(变调 / 变速)
# ---------------------------------------------------------------------------
async def _render_to_cache(source: str, src_in: float, src_out: float,
                           semis: float, cents: float,
                           out_sec: float | None) -> dict:
    path = safe_source(source)
    size, mtime = sm.file_identity(path)
    x, sr = sm.load_mono(path)

    src0 = int(round(float(src_in) * sr))
    src1 = int(round(float(src_out) * sr))
    if src1 <= src0:
        raise HTTPException(400, "片段长度不对")
    out_n = int(round(float(out_sec) * sr)) if out_sec else (src1 - src0)
    out_n = max(1, out_n)
    total_cents = float(semis or 0) * 100.0 + float(cents or 0)

    key = sm.clip_key(_rel(path), size, mtime, src0, src1, total_cents, out_n)
    dst = CACHE / f"clip_{key}.wav"
    if dst.exists():
        return {"key": key, "url": f"/api/studio/cache/clip_{key}.wav",
                "sec": out_n / sr, "sec_cost": 0.0, "cached": True}

    # 同一个 key 并发进来只算一次
    fut = _inflight.get(key)
    if fut is not None:
        await fut
        return {"key": key, "url": f"/api/studio/cache/clip_{key}.wav",
                "sec": out_n / sr, "sec_cost": 0.0, "cached": True}

    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    t0 = time.perf_counter()
    try:
        y = await asyncio.get_running_loop().run_in_executor(
            _pool, sm.render_piece, x, sr, src0, src1, total_cents, out_n)
        await asyncio.get_running_loop().run_in_executor(
            _pool, sm.write_wav_atomic, dst, y, sr)
    except Exception as e:                 # noqa: BLE001
        _inflight.pop(key, None)
        fut.set_exception(e)
        raise HTTPException(500, f"渲染失败:{e}")
    _inflight.pop(key, None)
    fut.set_result(True)
    return {"key": key, "url": f"/api/studio/cache/clip_{key}.wav",
            "sec": len(y) / sr, "sec_cost": round(time.perf_counter() - t0, 3),
            "cached": False}


@router.post("/render_clip")
async def render_clip(request: Request) -> dict:
    b = await request.json()
    return await _render_to_cache(
        b.get("source", ""), b.get("srcIn", 0), b.get("srcOut", 0),
        b.get("semis", 0), b.get("cents", 0), b.get("outSec"))


@router.post("/render_curve")
async def render_curve(request: Request) -> dict:
    """块内音高曲线。curve = [[t 相对 srcIn, 音分], …]。

    ★ key 里带曲线指纹 —— 改一点曲线就换一份缓存,不改就命中旧的。
    """
    b = await request.json()
    path = safe_source(b.get("source", ""))
    curve = b.get("curve") or []
    if not isinstance(curve, list) or len(curve) < 1 or len(curve) > 4000:
        raise HTTPException(400, "曲线格式不对")
    try:
        curve = [(float(t), float(c)) for t, c in curve]
    except Exception:                       # noqa: BLE001
        raise HTTPException(400, "曲线格式不对")
    sig = sm.curve_sig(curve)
    if not sig:
        raise HTTPException(400, "空曲线")

    size, mtime = sm.file_identity(path)
    x, sr = sm.load_mono(path)
    src0 = int(round(float(b.get("srcIn", 0)) * sr))
    src1 = int(round(float(b.get("srcOut", 0)) * sr))
    if src1 <= src0:
        raise HTTPException(400, "片段长度不对")
    out_sec = b.get("outSec")
    out_n = max(1, int(round(float(out_sec) * sr)) if out_sec else (src1 - src0))

    key = sm.clip_key(_rel(path), size, mtime, src0, src1, 0.0, out_n, sig)
    dst = CACHE / f"clip_{key}.wav"
    if dst.exists():
        return {"key": key, "sig": sig, "url": f"/api/studio/cache/clip_{key}.wav",
                "sec": out_n / sr, "sec_cost": 0.0, "cached": True}

    fut = _inflight.get(key)
    if fut is not None:
        await fut
        return {"key": key, "sig": sig, "url": f"/api/studio/cache/clip_{key}.wav",
                "sec": out_n / sr, "sec_cost": 0.0, "cached": True}

    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    t0 = time.perf_counter()
    try:
        y = await asyncio.get_running_loop().run_in_executor(
            _pool, sm.render_curve_piece, x, sr, src0, src1, curve, out_n)
        await asyncio.get_running_loop().run_in_executor(
            _pool, sm.write_wav_atomic, dst, y, sr)
    except Exception as e:                 # noqa: BLE001
        _inflight.pop(key, None)
        fut.set_exception(e)
        raise HTTPException(500, f"曲线渲染失败:{e}")
    _inflight.pop(key, None)
    fut.set_result(True)
    return {"key": key, "sig": sig, "url": f"/api/studio/cache/clip_{key}.wav",
            "sec": len(y) / sr, "sec_cost": round(time.perf_counter() - t0, 3),
            "cached": False}


@router.post("/bpm")
async def detect_bpm(request: Request) -> dict:
    """自动测速。**只填 BPM 框**,不擅自挪已有音频(改网格是用户按下才生效)。"""
    b = await request.json()
    path = safe_source(b.get("path", ""))
    t0 = time.perf_counter()
    res = await asyncio.get_running_loop().run_in_executor(
        _pool, sm.estimate_bpm, path)
    res["sec_cost"] = round(time.perf_counter() - t0, 2)
    return res


@router.post("/check_pitch")
async def check_pitch(request: Request) -> dict:
    """跑调检测。★ 只标红点、给建议;**改不改是用户点**。"""
    b = await request.json()
    path = safe_source(b.get("source", ""))
    t0 = time.perf_counter()
    res = await asyncio.get_running_loop().run_in_executor(_pool, sm.check_pitch, path)
    res["sec_cost"] = round(time.perf_counter() - t0, 2)
    return res


@router.post("/chords")
async def chords(request: Request) -> dict:
    """和弦检测。★ 只用来「建议」和声 —— 人声鼓点混在一起时它会错,别当乐理真理。"""
    b = await request.json()
    path = safe_source(b.get("source", ""))
    t0 = time.perf_counter()
    res = await asyncio.get_running_loop().run_in_executor(_pool, sm.detect_chords, path)
    res["sec_cost"] = round(time.perf_counter() - t0, 2)
    return res


@router.post("/pool")
async def pool(request: Request) -> dict:
    """采样音色池:一次 YIN,再逐半音 PSOLA。阶段 3 用。"""
    b = await request.json()
    path = safe_source(b.get("source", ""))
    src_in, src_out = float(b.get("srcIn", 0)), float(b.get("srcOut", 0))
    lo, hi = int(b.get("lo", -7)), int(b.get("hi", 7))
    lo, hi = max(-24, min(0, lo)), min(24, max(0, hi))
    x, sr = sm.load_mono(path)
    src0, src1 = int(round(src_in * sr)), int(round(src_out * sr))
    if src1 <= src0:
        raise HTTPException(400, "片段长度不对")
    size, mtime = sm.file_identity(path)

    def _work():
        piece = x[src0:src1]
        f0 = tuning_f0(piece, sr)
        out = []
        for s in range(lo, hi + 1):
            cents = s * 100.0
            key = sm.clip_key(_rel(path), size, mtime, src0, src1, cents, src1 - src0)
            dst = CACHE / f"clip_{key}.wav"
            if not dst.exists():
                sm.write_wav_atomic(dst, sm.render_piece(
                    x, sr, src0, src1, cents, src1 - src0, f0_hint=f0), sr)
            out.append({"semis": s, "key": key,
                        "url": f"/api/studio/cache/clip_{key}.wav"})
        return {"f0": f0, "entries": out}

    t0 = time.perf_counter()
    res = await asyncio.get_running_loop().run_in_executor(_pool, _work)
    base_midi = 69 + 12 * np.log2(max(res["f0"], 20.0) / 440.0)
    res.update({"baseMidi": float(base_midi),
                "note": midi_name(base_midi),
                "sec_cost": round(time.perf_counter() - t0, 3)})
    return res


def tuning_f0(piece: np.ndarray, sr: int) -> float:
    f0 = tuning.yin(np.asarray(piece, dtype=np.float32), sr)[0]
    v = f0[np.isfinite(f0) & (f0 > 40) & (f0 < 2000)]
    if len(v) == 0:
        return 160.0
    return float(np.median(v))


NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_name(m: float) -> str:
    n = int(round(m))
    return f"{NAMES[n % 12]}{n // 12 - 1}"
