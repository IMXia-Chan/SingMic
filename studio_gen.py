# -*- coding: utf-8 -*-
"""AI 生成伴奏 —— 接外部音乐生成 API(目前是 Mureka)。

三条硬规矩,写死在代码里:

1. **只发文字。** 发出去的只有「风格描述」和「你写的歌词」。
   不发音频、不发路径、不发文件名、不发工程名。
   Mureka 有 `reference_id` / `vocal_id`(上传一段音频让它照着做 / 克隆音色)——
   这两个参数在本文件里**根本不出现**,不是"默认关",是没有这个开关。
   一旦哪天有人想加,先想想这个项目的立场。

2. **n 强制 1。** Mureka 的 `n` 默认值是 2,按首计费。
   不显式传 1,点一次生成就扣两首的钱。

3. **key 只写不读。** 存 `~/.singmic/music.json`,界面只回掩码。
   环境变量 `SINGMIC_MUSIC_KEY` 优先(临时换 key 不用改文件)。

生成是异步的:`create()` 拿任务号,`query()` 轮询到结束,`fetch()` 把音频落到
`projects/media/` 里,和手动导入的音频走同一条路。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import studio_mix as _sm                        # noqa: E402  (数据根只认这一处)

APP = Path(__file__).resolve().parent          # 程序目录(web/ tools/ 这类只读资源)
ROOT = _sm.DATA                                 # 媒体相对路径的解析基准
MEDIA = _sm.MEDIA
# 组合模板试听的缓存:同一段风格说明生成过一次就存这儿,再听不走平台、不花钱
PREVIEW_DIR = _sm.DATA / "cache" / "gen_preview"
HOME_CFG = Path(os.environ.get("SINGMIC_HOME") or (Path.home() / ".singmic"))
CFG_PATH = HOME_CFG / "music.json"

CREATE_TIMEOUT = 30.0
QUERY_TIMEOUT = 20.0
GET_TIMEOUT = 180.0

_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}          # task_id -> {provider, mode, at}

PROVIDERS = {
    "mureka": {
        "name": "Mureka",
        "base": "https://api.mureka.cn/v1",
        "song": "/song/generate",
        "instrumental": "/instrumental/generate",
        "query": "/song/query/{task}",
    },
    # 阿里云百炼。和 Mureka 不一样:它是**同步**接口,一次 POST 等两三分钟直接回
    # 音频地址(24 小时有效),没有任务号可轮询。为了不把请求线程占死,这里丢到
    # 后台线程跑,对前端仍然装成「提交→轮询」,界面一行不用改。
    # 计费按音频时长:¥0.002/秒 —— 3 分钟约 ¥0.36。
    "bailian": {
        "name": "阿里云百炼",
        "base": "https://dashscope.aliyuncs.com/api/v1",
        "music": "/services/audio/music/generation",
        "model": "fun-music-v1",
        "sync": True,
    },
    # 腾讯云 TokenHub 转卖的同一个模型(mureka-music-v9)。也是**同步**接口(实测 66 秒出歌)。
    # ★ n 默认是 2,按首扣费 —— 必须显式传 1,跟 Mureka 直连是同一个坑。
    # ★★ 实测(2026-10-21)只支持「歌词生成歌曲」;纯伴奏那一路在网关上不存在,
    #    见 _tokenhub_input 的注释。看单页就把「带 AI 人声」勾上。
    # 开通前提:控制台 → 在线推理 → 语音模型 → 对该模型开「后付费」。
    "tokenhub": {
        "name": "腾讯云 TokenHub",
        "base": "https://tokenhub.tencentmaas.com/v1",
        "music": "/wand/mureka-music/generation",
        "model": "mureka-music-v9",
        "sync": True,
    },
}

# 同步平台(百炼 / 腾讯云 TokenHub)一次生成要两三分钟,超时在这里单独放宽
SYNC_TIMEOUT = 600.0


class GenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# key:只写不读
# ---------------------------------------------------------------------------
def _cfg() -> dict:
    with _LOCK:
        try:
            d = json.loads(CFG_PATH.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def _mask(tok: str) -> str:
    if not tok:
        return ""
    return (tok[:3] + "••••" + tok[-3:]) if len(tok) > 8 else "••••"


def _tokens() -> dict[str, str]:
    """每个平台各存一把 key,换平台不用重填。老配置里的单个 token 认成 mureka。"""
    d = _cfg()
    tk = d.get("tokens")
    if isinstance(tk, dict):
        return {str(k): str(v) for k, v in tk.items() if v}
    old = str(d.get("token") or "")
    if not old:
        return {}
    return {str(d.get("provider") or "mureka"): old}


def key_state() -> dict:
    env = os.environ.get("SINGMIC_MUSIC_KEY", "").strip()
    d = _cfg()
    pid = str(d.get("provider") or "mureka")
    if pid not in PROVIDERS:
        pid = "mureka"
    tok = env or _tokens().get(pid, "")
    bases = d.get("bases") if isinstance(d.get("bases"), dict) else {}
    return {
        "configured": bool(tok),
        "provider": pid,
        "providerName": PROVIDERS[pid]["name"],
        "base": str(bases.get(pid) or d.get("base") or PROVIDERS[pid]["base"]),
        "masked": _mask(tok),
        "source": "env" if env else ("file" if tok else ""),
        "hasKey": {k: bool(v) for k, v in _tokens().items()},
    }


def set_config(token: str, provider: str = "mureka", base: str = "") -> dict:
    token = (token or "").strip()
    if not token:
        raise GenError("key 是空的")
    if provider not in PROVIDERS:
        raise GenError(f"不认识的平台:{provider}")
    HOME_CFG.mkdir(parents=True, exist_ok=True)
    d = _cfg()
    tokens = _tokens()
    tokens[provider] = token
    d["tokens"] = tokens
    d["provider"] = provider
    d.pop("token", None)
    base = (base or "").strip()
    if base:
        bases = d.get("bases") if isinstance(d.get("bases"), dict) else {}
        bases[provider] = base
        d["bases"] = bases
    tmp = CFG_PATH.with_suffix(".part")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(CFG_PATH)
    return key_state()


def clear_config(provider: str = "") -> dict:
    d = _cfg()
    tokens = _tokens()
    pid = provider or str(d.get("provider") or "mureka")
    tokens.pop(pid, None)
    d["tokens"] = tokens
    d.pop("token", None)
    try:
        CFG_PATH.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return key_state()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _token(pid: str | None = None) -> str:
    env = os.environ.get("SINGMIC_MUSIC_KEY", "").strip()
    if env:
        return env
    pid = pid or str(_cfg().get("provider") or "mureka")
    return _tokens().get(pid, "")


def _base(pid: str) -> str:
    d = _cfg()
    bases = d.get("bases") if isinstance(d.get("bases"), dict) else {}
    return str(bases.get(pid) or PROVIDERS[pid]["base"])


def _req(url: str, token: str, body: dict | None = None, timeout: float = CREATE_TIMEOUT) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        raise GenError(f"平台返回 {e.code}:{detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise GenError(f"连不上平台({e.reason})") from None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise GenError("平台回的不是 JSON") from None
    if not isinstance(obj, dict):
        raise GenError("平台回的格式不对")
    return obj


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def create(prompt: str, lyrics: str = "", instrumental: bool = True,
           model: str = "auto", mock: bool = False, gender: str = "female",
           provider: str = "") -> dict:
    """提交一次生成。返回任务号,之后用 query() 轮询。

    ★ `instrumental=True` 走纯器乐接口 —— 那是"伴奏",没有人声。
      他要在上面唱歌,带人声的歌对他没用。
    ★ `provider` 由面板显式传:以前只认配置里「最后保存过 key 的那个平台」,
      在下拉里换平台不生效,会悄悄按旧平台扣钱。现在按面板说的来,
      找不到这个平台的 key 就直说,不退回别的平台。
    """
    prompt = (prompt or "").strip()
    lyrics = (lyrics or "").strip()
    if not prompt and (instrumental or not lyrics):
        raise GenError("先写一句风格描述(比如:phonk, 145 bpm, dark, 808 cowbell)")

    if mock:
        tid = "mock_" + hashlib.sha1(f"{prompt}{time.time()}".encode()).hexdigest()[:12]
        _TASKS[tid] = {"provider": "mock", "mode": "instrumental" if instrumental else "song",
                       "at": time.time(), "prompt": prompt, "lyrics": lyrics}
        return {"taskId": tid, "provider": "mock", "mode": "instrumental" if instrumental else "song"}

    pid = str(provider or "").strip() or key_state()["provider"]
    if pid not in PROVIDERS:
        raise GenError(f"不认识的平台:{pid}")
    token = _token(pid)
    if not token:
        nm = PROVIDERS[pid]["name"]
        raise GenError(f"{nm} 还没填 API key(在上方「API key」那一栏填好后点保存)")
    spec = PROVIDERS[pid]

    if spec.get("sync"):
        return _create_sync(pid, token, prompt, lyrics, instrumental, model, gender)

    path = spec["instrumental"] if instrumental else spec["song"]
    body: dict = {"model": model, "n": 1}      # ★ n=1 写死,默认是 2,会扣两首的钱
    body["prompt"] = prompt[:1024]
    if not instrumental:
        if not lyrics:
            raise GenError("生成带人声的歌必须给歌词")
        body["lyrics"] = lyrics[:5000]

    obj = _req(_base(pid).rstrip("/") + path, token, body)
    tid = str(obj.get("id") or obj.get("task_id") or "")
    if not tid:
        raise GenError(f"平台没给任务号:{str(obj)[:200]}")
    _TASKS[tid] = {"provider": pid, "mode": "instrumental" if instrumental else "song",
                   "at": time.time(), "prompt": prompt}
    return {"taskId": tid, "provider": pid,
            "mode": "instrumental" if instrumental else "song"}


# ---------------------------------------------------------------------------
# 同步平台(百炼 fun-music / 腾讯云 TokenHub):装成"提交→轮询"给前端用
# ---------------------------------------------------------------------------
def _bailian_input(prompt: str, lyrics: str, instrumental: bool, gender: str) -> dict:
    """百炼的入参。★它同时收到 prompt 和 lyrics 时**只认 lyrics**,所以带人声那一路
    风格描述是不起作用的 —— 这不是我们这边能左右的,界面上要写清楚。"""
    if instrumental:
        if not prompt:
            raise GenError("纯伴奏要写一句风格描述")
        return {"prompt": prompt[:2000], "is_instrumental": True, "format": "mp3"}
    if len(lyrics) < 5:
        raise GenError("百炼的歌词至少要 5 个字")
    if len(lyrics) > 350:
        raise GenError(f"百炼的歌词上限 350 字(现在 {len(lyrics)} 字),"
                       "先删短一点,或者分成两段各生成一次再拼")
    return {"lyrics": lyrics, "gender": "male" if gender == "male" else "female",
            "format": "mp3"}


def _tokenhub_input(prompt: str, lyrics: str, instrumental: bool, gender: str) -> dict:
    """腾讯云 TokenHub(mureka-music-v9)的入参。

    ★ 2026-10-21 实测(真打过 5 次请求):**这个网关上只有「歌词生成歌曲」能用**。
      文档说的另外两种在网关上都不生效 ——
        `instrumental: true`   → 400 "The lyrics are empty."
        `is_instrumental: true`→ 同样 400
        只给 prompt / 再加 styles → 同样 400
      连文档「生成纯音乐」那节自己的请求示例(只有 model+prompt+n)都过不了。
      所以这里不做「纯伴奏」,直接拦下来并说清楚,别让他白花一次钱等一个 400。
    ★ n 默认 2 且**按数量计费**,这里一律写死 1。
    """
    if instrumental:
        raise GenError("腾讯云的 mureka-music-v9 实际只支持「歌词生成歌曲」:"
                       "不给歌词一律被网关拒掉(文档说能生成纯音乐,实测无效)。"
                       "要纯伴奏请切阿里云百炼或 Mureka;或者在这儿写歌词生成整首,"
                       "再用「人声/伴奏」拆出伴奏。")
    if len(lyrics) < 2:
        raise GenError("歌词太短了,至少写一句")
    body: dict = {"lyrics": lyrics[:5000], "n": 1}
    if prompt:
        # 歌词模式下 prompt 是**风格提示词**,上限 1024(不是 2000)
        body["prompt"] = prompt[:1024]
    if gender in ("male", "female"):
        body["gender"] = gender
    return body


def _sync_parse(pid: str, obj: dict) -> dict:
    """把两家同步接口的回包拆成 {url, duration, lyricsBack[, watermarked]}。"""
    marked = False
    if pid == "bailian":
        out = obj.get("output") or {}
        u = str((out.get("audio") or {}).get("url") or "")
        dur = (obj.get("usage") or {}).get("duration")
        back = (out.get("extra_info") or {}).get("lyrics") or ""
    else:                                        # tokenhub
        st = str(obj.get("status") or "").lower()
        if st and st != "succeeded":
            why = str(obj.get("failed_reason") or "").strip()
            if st in ("preparing", "queued", "running", "streaming", "reviewing"):
                # 官方文档说这是**同步**接口,一次请求直接回结果。真回了中间态,
                # 说明它偷偷变成异步了 —— 我们没有任务号可以轮询,只能明说。
                raise GenError(f"平台没直接返回结果(状态 {st})—— 这个接口本该是同步的,"
                               "重试一次;老这样就说明平台改了契约")
            raise GenError(f"平台没生成成功:{st}" + (f" —— {why}" if why else ""))
        ch = obj.get("choices") or []
        first = ch[0] if ch and isinstance(ch[0], dict) else {}
        u = str(first.get("url") or first.get("wav_url") or first.get("flac_url") or "")
        dur = first.get("duration")
        if isinstance(dur, (int, float)) and dur > 1000:   # 毫秒 → 秒
            dur = round(dur / 1000.0, 2)
        back = ""
        # 官方默认在尾部加 5 秒水印,且没有关闭参数 —— 界面上要照实说
        marked = bool(obj.get("watermarked"))
    if not u:
        raise GenError(f"平台说好了,但没给音频地址:{str(obj)[:200]}")
    if pid != "bailian":
        return {"url": u, "duration": dur, "lyricsBack": back, "watermarked": marked}
    return {"url": u, "duration": dur, "lyricsBack": back}


def _sync_worker(tid: str, pid: str, url: str, token: str, body: dict) -> None:
    """后台线程:同步等平台出歌(两三家都是同步接口),结果写回 _TASKS。"""
    t = _TASKS.get(tid)
    try:
        obj = _req(url, token, body, timeout=SYNC_TIMEOUT)
        if t is not None:
            t.update({"status": "done", **_sync_parse(pid, obj)})
    except GenError as e:
        if t is not None:
            t.update({"status": "failed", "reason": str(e)})
    except Exception as e:                       # noqa: BLE001
        if t is not None:
            t.update({"status": "failed", "reason": f"调用出错:{e}"})


def _create_sync(pid: str, token: str, prompt: str, lyrics: str,
                 instrumental: bool, model: str, gender: str) -> dict:
    if pid == "bailian":
        inp = _bailian_input(prompt, lyrics, instrumental, gender)
        mdl = PROVIDERS[pid]["model"]
        if str(model or "").startswith("fun-music"):
            mdl = str(model)
        body: dict = {"model": mdl, "input": inp}
        prefix = "bl_"
    else:
        inp = _tokenhub_input(prompt, lyrics, instrumental, gender)
        mdl = PROVIDERS[pid]["model"]
        if str(model or "").startswith("mureka-music"):
            mdl = str(model)
        body = {"model": mdl, **inp}
        prefix = "th_"
    mode = "instrumental" if instrumental else "song"
    tid = prefix + hashlib.sha1(f"{prompt}{lyrics}{time.time()}".encode()).hexdigest()[:12]
    _TASKS[tid] = {"provider": pid, "mode": mode, "at": time.time(),
                   "prompt": prompt, "status": "running"}
    url = _base(pid).rstrip("/") + PROVIDERS[pid]["music"]
    threading.Thread(target=_sync_worker, args=(tid, pid, url, token, body),
                     daemon=True).start()
    return {"taskId": tid, "provider": pid, "mode": mode}


MOCK_LYRIC = ("夜色落在窗台,我还是没有睡。"
              "你说的那句话,我到现在还记得。")

# 系统自带的语音合成念词。脚本本身只有 ASCII,中文走单独的 utf-8 文件读进来
# ——PowerShell 5.1 按 ANSI 读 .ps1,中文直接写进去会被啃坏。
_PS_TTS = r"""
param([string]$Out, [string]$TextFile)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1
if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }
$s.Rate = -2
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$s.SetOutputToWaveFile($Out)
$s.Speak($text)
$s.Dispose()
"""


def _tts_wav(text: str, out: Path) -> bool:
    """把词念成 wav。没有 PowerShell / 没有语音音色就返回 False,不抛。"""
    if os.name != "nt":
        return False
    tmpd = Path(tempfile.mkdtemp(prefix="singmic_tts_"))
    try:
        ps, txt = tmpd / "tts.ps1", tmpd / "lyric.txt"
        ps.write_text(_PS_TTS, encoding="utf-8")
        txt.write_text(text, encoding="utf-8")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps), "-Out", str(out), "-TextFile", str(txt)],
                           capture_output=True, timeout=180)
        return p.returncode == 0 and out.exists() and out.stat().st_size > 2000
    except Exception:
        return False
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _mock_beat() -> Path:
    """试跑的鼓:本地 drum kit 渲染,和 Studio 里的鼓机是同几份采样。"""
    MEDIA.mkdir(parents=True, exist_ok=True)
    dst = MEDIA / "gen_mock_beat.wav"
    if dst.exists():
        return dst
    tmp = Path(tempfile.gettempdir()) / "singmic_gen_mock_beat.wav"
    subprocess.run([sys.executable, str(APP / "tools" / "kit_demo.py"),
                    "--bpm", "120", "--bars", "8", "--out", str(tmp)],
                   check=True, capture_output=True, timeout=300)
    shutil.move(str(tmp), dst)
    return dst


def _mock_audio(mode: str = "instrumental", lyrics: str = "") -> Path:
    """试跑的音频。

    纯器乐 = 一段鼓;带人声 = 鼓 + 系统语音念的词。

    人声那层不是为了好听,是为了让「拆人声/伴奏」这条路**在没 key 时也能真跑一遍**:
    实测 demucs 会把这层当成 vocals(相关 0.94、伴奏残留 0.02),
    所以试跑拆出来的是真拆,不是摆样子。
    """
    beat = _mock_beat()
    if mode != "song":
        return beat
    text = (lyrics or "").strip() or MOCK_LYRIC
    tag = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    dst = MEDIA / f"gen_mock_song_{tag}.wav"
    if dst.exists():
        return dst

    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    tmp = Path(tempfile.mkdtemp(prefix="singmic_mock_"))
    try:
        voice = tmp / "voice.wav"
        if not _tts_wav(text, voice):
            return beat                      # 念不出来就退回纯器乐,不假装有人声
        d, sr = sf.read(str(beat), dtype="float32", always_2d=False)
        if getattr(d, "ndim", 1) > 1:
            d = d.mean(axis=1)
        v, vsr = sf.read(str(voice), dtype="float32", always_2d=False)
        if getattr(v, "ndim", 1) > 1:
            v = v.mean(axis=1)
        if vsr != sr:
            v = resample_poly(v, sr, vsr).astype("float32")
        if len(v) > len(d):
            v = v[: len(d)]
        else:
            reps = int(np.ceil(len(d) / max(1, len(v))))
            v = np.tile(v, reps)[: len(d)]
            k = min(2000, len(v) // 4)
            if k:
                v[:k] *= np.linspace(0, 1, k)
                v[-k:] *= np.linspace(1, 0, k)
        peak = float(np.max(np.abs(v))) or 1.0
        v = v / peak * 0.45
        mix = d * 0.7 + v
        peak = float(np.max(np.abs(mix))) or 1.0
        mix = (mix / peak * 0.89).astype("float32")
        sf.write(str(dst), mix, sr, subtype="PCM_16")
        return dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def preview_key(prompt: str, provider: str = "bailian", model: str = "auto") -> str:
    return hashlib.sha1(f"pv1|{provider}|{model}|{prompt}".encode("utf-8")).hexdigest()[:16]


def preview_start(prompt: str, provider: str = "bailian", model: str = "auto") -> dict:
    """组合模板的「试听」:同一个模板**只花一次钱**。

    ★ 平台不接受「给我 30 秒」,只能整首生成 —— 所以钱是按整首算的,
      我们只放前 30 秒,并在返回里把平台实际给了多长照实写出来。
    ★ 生成过的那条按 key 落盘,再点试听直接读本机文件,一秒出声、不再扣钱。
    ★ 只发风格描述(和面板上生成时发的是同一类东西),不发音频、不发路径。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise GenError("这条模板没有风格说明")
    if provider == "mock":                       # 本地鼓组试跑:不联网、不花钱
        key = preview_key(prompt, "mock", model)
        r = create(prompt, "", True, model, True)
        tid = str(r.get("taskId") or "")
        _TASKS.setdefault(tid, {}).update({"preview": True, "key": key})
        return {"taskId": tid, "provider": "mock", "cached": False, "key": key}
    key = preview_key(prompt, provider, model)
    dst = PREVIEW_DIR / f"{key}.mp3"
    for ext in (".mp3", ".wav"):                 # 本地试跑存的是 wav
        alt = PREVIEW_DIR / f"{key}{ext}"
        if alt.exists() and alt.stat().st_size > 4096:
            dst = alt
            break
    meta = PREVIEW_DIR / f"{key}.json"
    if dst.exists() and dst.stat().st_size > 4096:
        sec = 0.0
        try:
            sec = float(json.loads(meta.read_text("utf-8")).get("sec") or 0)
        except Exception:                        # noqa: BLE001
            sec = 0.0
        tid = "pv_" + key
        _TASKS[tid] = {"provider": provider, "mode": "instrumental", "at": time.time(),
                       "status": "done", "path": str(dst), "duration": sec,
                       "preview": True, "key": key}
        return {"taskId": tid, "provider": provider, "cached": True, "key": key}

    # 平台没有「时长」入参:这句只是**建议**,它给多长我们照实播/照实记账
    tag = f"{prompt}，30 秒短片段，只有一个循环"
    r = create(tag, "", True, model, False, "female", provider)
    tid = str(r.get("taskId") or "")
    _TASKS.setdefault(tid, {}).update({"preview": True, "key": key, "previewTag": tag})
    return {"taskId": tid, "provider": provider, "cached": False, "key": key}


def preview_fetch(task_id: str) -> dict:
    """试听生成完:把音频落到 cache/gen_preview/,返回本机路径 + 平台实际时长。"""
    t = _TASKS.get(task_id) or {}
    key = str(t.get("key") or "")
    if not key:
        raise GenError("这不是试听任务")
    res = query(task_id)
    if res.get("status") != "succeeded":
        raise GenError(res.get("reason") or "生成还没结束")
    ch = res.get("choices") or []
    if not ch:
        raise GenError("平台说完成了,但没给音频地址")
    item = ch[0]
    sec = item.get("duration") or 0
    dst = PREVIEW_DIR / f"{key}.mp3"
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    local = str(item.get("path") or "")
    if local and Path(local).exists():
        # 假任务(本地试跑)已经是 wav:原样收进缓存目录
        src = Path(local)
        dst = PREVIEW_DIR / f"{key}{src.suffix}"
        if not dst.exists():
            shutil.copyfile(src, dst)
        return {"key": key, "path": _rel(dst), "sec": sec, "cached": False}

    url = str(item.get("url") or "")
    if not url:
        raise GenError("平台没给音频地址")
    try:
        with urllib.request.urlopen(urllib.request.Request(url),
                                    timeout=GET_TIMEOUT) as r:
            data = r.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        raise GenError(f"下载失败:{e}") from None
    if len(data) < 4096:
        raise GenError("下载到的文件太小,可能不是音频")
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dst)
    try:
        (PREVIEW_DIR / f"{key}.json").write_text(
            json.dumps({"sec": sec}, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return {"key": key, "path": _rel(dst), "sec": sec, "cached": False}


def query(task_id: str) -> dict:
    """查一次任务。没结束就返回 running,结束了返回 choices。"""
    t = _TASKS.get(task_id) or {}
    pid = str(t.get("provider") or key_state()["provider"])

    if PROVIDERS.get(pid, {}).get("sync"):
        # 同步平台的任务只活在本进程里;服务重启后这个号就查不到了 ——
        # 不明说的话前端会一直转圈。
        if task_id not in _TASKS:
            return {"status": "failed",
                    "reason": "服务重启过,这个生成任务已经丢了(钱大概率已经花了),请重新提交"}
        st = str(t.get("status") or "running")
        if st == "failed":
            return {"status": "failed", "reason": str(t.get("reason") or "生成失败")}
        if st != "done":
            return {"status": "running", "rawStatus": "generating"}
        return {"status": "succeeded", "choices": [{
            "url": str(t.get("url") or ""),
            "path": str(t.get("path") or ""),
            "duration": t.get("duration"),
            "lyricsBack": str(t.get("lyricsBack") or ""),
            "watermarked": bool(t.get("watermarked")),
            "title": f"{PROVIDERS[pid]['name']} {PROVIDERS[pid]['model']}",
        }]}

    if pid == "mock":
        # 假装要三秒,好让前端的轮询逻辑也被真正走一遍
        if time.time() - float(t.get("at") or 0) < 3.0:
            return {"status": "running"}
        mode = str(t.get("mode") or "instrumental")
        p = _mock_audio(mode, str(t.get("lyrics") or ""))
        sec = 0.0
        try:
            import soundfile as sf
            sec = round(sf.info(str(p)).frames / float(sf.info(str(p)).samplerate), 2)
        except Exception:
            sec = 8.6
        has_vocal = mode == "song" and p.name.startswith("gen_mock_song")
        return {"status": "succeeded",
                "choices": [{"url": "", "path": str(p), "duration": sec,
                             "title": ("本地试跑(带人声,没花一分钱)" if has_vocal
                                       else "本地试跑(纯器乐,没花一分钱)")}]}

    token = _token(pid)
    if not token:
        raise GenError("还没填 API key")
    spec = PROVIDERS.get(pid, PROVIDERS["mureka"])
    url = _base(pid).rstrip("/") + spec["query"].format(task=task_id)
    obj = _req(url, token, timeout=QUERY_TIMEOUT)
    status = str(obj.get("status") or "")
    out = {"status": status, "rawStatus": status}
    if status in ("failed", "timeouted", "cancelled"):
        out["reason"] = str(obj.get("failed_reason") or status)
        return out
    if status != "succeeded":
        return out
    choices = []
    for c in (obj.get("choices") or []):
        if not isinstance(c, dict):
            continue
        u = str(c.get("url") or c.get("audio_url") or "")
        if not u:
            continue
        choices.append({"url": u, "id": str(c.get("id") or ""),
                        "duration": c.get("duration"),
                        "title": str(c.get("title") or "")})
    out["choices"] = choices
    if not choices:
        out["reason"] = "平台说完成了,但没给音频地址"
    return out


def fetch(task_id: str, index: int = 0) -> dict:
    """把选中的那一首下载到 projects/media/,返回可导入的 source。"""
    res = query(task_id)
    if res.get("status") != "succeeded":
        raise GenError(f"任务还没完成:{res.get('status')}")
    ch = res.get("choices") or []
    if not ch:
        raise GenError(res.get("reason") or "没有可下载的音频")
    if not 0 <= index < len(ch):
        raise GenError("选的那一首不存在")
    item = ch[index]

    # 假任务:文件已经在本机了
    if item.get("path"):
        p = Path(item["path"])
        return _source_of(p, item)

    url = item["url"]
    MEDIA.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=GET_TIMEOUT) as r:
            data = r.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        raise GenError(f"下载失败:{e}") from None
    if len(data) < 4096:
        raise GenError("下载到的文件太小,可能不是音频")
    sha = hashlib.sha1(data).hexdigest()[:8]
    name = f"ai_{sha}.mp3"
    dst = MEDIA / name
    if not dst.exists():
        tmp = dst.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dst)
    return _source_of(dst, item)


def _source_of(p: Path, item: dict) -> dict:
    return {
        "source": {
            "path": str(p.relative_to(ROOT)).replace("\\", "/"),
            "name": p.name,
            "sec": item.get("duration"),
            "ai": True,
            "title": item.get("title") or "",
        }
    }


def release(task_id: str) -> None:
    _TASKS.pop(task_id, None)
