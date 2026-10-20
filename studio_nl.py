"""自然语言调参:把「人声太干,加一点混响」变成可勾选、可撤销的参数动作。

  * 大脑是 DeepSeek(`chat/completions`),key 存在 `~/.singmic/secrets.json`,
    **只写不读**;环境变量 `DEEPSEEK_API_KEY` 优先(临时换 key 不用改文件)。
  * 发出去的只有:**参数 + 你这句话**。不发音频、不发路径、不发文件名、不发工程名。
    轨道名默认也不发(换成「主唱 / 伴奏 / 轨道 3」)。
  * 模型返回的 id 一律不可信:只认当前 scope 里真实存在的 trackId/clipId;
    数值按 `FX_TYPES` 的 min/max 夹紧;未知 kind/类型直接丢弃并计数。
  * **服务端不改工程** —— 只回一份动作清单,前端勾选后才应用,一次 Ctrl+Z 全退。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import studio_fx

HOME_CFG = Path(os.environ.get("SINGMIC_HOME") or (Path.home() / ".singmic"))
KEY_PATH = HOME_CFG / "secrets.json"
DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_OPS = 20
TIMEOUT = 30.0

_LOCK = threading.Lock()

_EFFECTS = ["filter", "eq3", "compressor", "limiter", "distortion",
            "chorus", "flanger", "tremolo", "bitcrush"]


# ---------------------------------------------------------------------------
# key:只写不读
# ---------------------------------------------------------------------------
def _read_keys() -> dict:
    with _LOCK:
        try:
            d = json.loads(KEY_PATH.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def _mask(tok: str) -> str:
    if not tok:
        return ""
    return tok[:3] + "••••" + tok[-3:] if len(tok) > 8 else "••••"


def key_state() -> dict:
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    d = _read_keys()
    tok = env or str(d.get("apiKey") or "")
    return {"token": tok, "base": str(d.get("baseUrl") or DEFAULT_BASE),
            "model": str(d.get("model") or DEFAULT_MODEL),
            "source": "env" if env else ("file" if tok else "")}


def status() -> dict:
    s = key_state()
    return {"configured": bool(s["token"]), "model": s["model"],
            "masked": _mask(s["token"]), "source": s["source"],
            "baseUrl": s["base"]}


def set_key(api_key: str, model: str = "", base_url: str = "") -> dict:
    k = (api_key or "").strip()
    if len(k) < 12:
        raise ValueError("key 太短,像是没粘全")
    d = _read_keys()
    d["apiKey"] = k
    if model:
        d["model"] = model.strip()
    if base_url:
        b = base_url.strip().rstrip("/")
        if not re.match(r"^https?://", b, re.I):
            raise ValueError("地址要以 http:// 或 https:// 开头")
        d["baseUrl"] = b
    HOME_CFG.mkdir(parents=True, exist_ok=True)
    tmp = KEY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, KEY_PATH)
    try:
        KEY_PATH.chmod(0o600)
    except OSError:
        pass
    return status()


def clear_key() -> dict:
    d = _read_keys()
    d.pop("apiKey", None)
    HOME_CFG.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    return status()


# ---------------------------------------------------------------------------
# 上下文:只给参数,不给身份
# ---------------------------------------------------------------------------
def _role_of(tr: dict) -> str:
    k = str(tr.get("kind") or "")
    n = str(tr.get("name") or "").lower()
    if k == "voice" or any(w in n for w in ("人声", "唱", "vocal", "主唱", "和声")):
        return "vocal"
    return "music"


def _fx_view(tr: dict) -> dict:
    fx = tr.get("fx") or {}
    out = {}
    rv = fx.get("reverb")
    if isinstance(rv, dict) and (rv.get("mix") or rv.get("size")):
        out["reverb"] = {"mix": round(float(rv.get("mix") or 0), 3),
                         "size": round(float(rv.get("size") or 0), 2)}
    dl = fx.get("delay")
    if isinstance(dl, dict) and dl:
        out["delay"] = {k: dl.get(k) for k in ("time", "feedback", "mix")
                        if dl.get(k) is not None}
    chain = []
    for s in (fx.get("chain") or [])[:12]:
        if not isinstance(s, dict):
            continue
        chain.append({"type": s.get("type"), "params": s.get("params") or {}})
    if chain:
        out["chain"] = chain
    return out


def _params_table() -> list[dict]:
    """把 FX_TYPES 压成模型看得懂的参数表(范围/默认/枚举),不带中文名。"""
    out = []
    for t in _EFFECTS:
        spec = studio_fx.FX_TYPES.get(t) or {}
        ps = []
        for p in spec.get("params") or []:
            item = {"key": p["key"], "type": p.get("type", "float"),
                    "min": p.get("min"), "max": p.get("max"),
                    "default": p.get("default")}
            if p.get("type") == "enum":
                item["options"] = [o["value"] for o in p.get("options") or []]
            ps.append(item)
        out.append({"type": t, "params": ps})
    return out


def build_context(project: dict, scope: dict, send_names: bool = False) -> dict:
    """scope = {"type":"project"} / {"type":"track","trackId":…} /
       {"type":"clips","clipIds":[…]}。"""
    tracks = project.get("tracks") or []
    stype = str(scope.get("type") or "project")
    want_track = str(scope.get("trackId") or "") if stype == "track" else ""
    want_clips = {str(x) for x in (scope.get("clipIds") or [])} if stype == "clips" else set()
    clip_tracks = set()
    if stype == "clips":
        for tr in tracks:
            for c in tr.get("clips") or []:
                if str(c.get("id")) in want_clips:
                    clip_tracks.add(str(tr.get("id")))

    names = {}
    vocal_i = 0
    for i, tr in enumerate(tracks):
        if _role_of(tr) == "vocal":
            vocal_i += 1
            names[str(tr.get("id"))] = "主唱" if vocal_i == 1 else f"和声 {vocal_i - 1}"
        else:
            names[str(tr.get("id"))] = "伴奏" if i == 1 else f"轨道 {i + 1}"

    ctx_tracks, ctx_clips = [], []
    for tr in tracks:
        tid = str(tr.get("id"))
        if stype == "track" and tid != want_track:
            continue
        if stype == "clips" and tid not in clip_tracks:
            continue
        item = {
            "id": tid,
            "name": str(tr.get("name") or tid) if send_names else names[tid],
            "role": _role_of(tr),
            "gain": round(float(tr.get("gain", 1) or 0), 3),
            "pan": round(float(tr.get("pan", 0) or 0), 3),
            "mute": bool(tr.get("mute")),
            "fx": _fx_view(tr),
        }
        ctx_tracks.append(item)
        for c in (tr.get("clips") or []):
            cid = str(c.get("id"))
            if stype == "clips" and cid not in want_clips:
                continue
            ctx_clips.append({
                "id": cid, "trackId": tid,
                "start": round(float(c.get("start") or 0), 3),
                "length": round(float(c.get("length") or 0), 3),
                "semis": int(c.get("semis") or 0),
                "cents": int(c.get("cents") or 0),
                "gain": round(float(c.get("gain", 1) or 0), 3),
                "kind": str(c.get("kind") or "note"),
                "hasCurve": bool(c.get("pitchCurve")),
            })
    # 整曲时 clip 可能上千:只给前 240 条,并说明截断(不假装全给了)
    truncated = 0
    if len(ctx_clips) > 240:
        truncated = len(ctx_clips) - 240
        ctx_clips = ctx_clips[:240]
    return {
        "scope": {"type": stype, "trackId": want_track or None},
        "tracks": ctx_tracks,
        "clips": ctx_clips,
        "clipsTruncated": truncated,
        "panRange": [-1, 1], "gainRange": [0, 1.5],
        "effects": _params_table(),
    }


SYSTEM = """You are a mixing assistant for a browser DAW. The user describes what they hear in plain
Chinese. Translate it into a short list of parameter changes.

Reply with ONE JSON object, no markdown:
{"reply": "<one short Chinese sentence>", "ops": [ ... ]}

Allowed ops (use ONLY these):
1. {"kind":"gain","trackId":"...","to":0.8}          # track fader, 0..1.5
2. {"kind":"pan","trackId":"...","to":-0.2}          # -1 left .. 1 right
3. {"kind":"reverb","trackId":"...","to":{"mix":0.18,"size":1.6}}  # mix 0..1, size 0.2..4
4. {"kind":"chain","trackId":"...","slots":[{"type":"compressor","params":{"threshold":-20,"ratio":3}}]}
   # types: filter, eq3, compressor, limiter, distortion, chorus, flanger, tremolo, bitcrush
   # use the provided effects table for valid params/ranges
5. {"kind":"clipgain","clipId":"...","to":1.2}       # 0..2
6. {"kind":"pitch","clipId":"...","to":{"semis":1,"cents":0}}   # semis -24..24, cents -100..100

Rules:
- Use only ids present in the given tracks/clips. Never invent ids.
- At most 20 ops. Prefer few, strong, musically sensible moves.
- Explain each op with "why" (short Chinese, may reference the user's words).
- If the request is not about mix parameters, return {"reply":"...","ops":[]}.
- Never output file paths, file names, project names, or audio data."""


# ---------------------------------------------------------------------------
# 调模型
# ---------------------------------------------------------------------------
def _post(url: str, key: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": "SingMic/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ask(text: str, ctx: dict) -> tuple[dict | None, str, float]:
    st = key_state()
    if not st["token"]:
        return None, "没配置 DeepSeek key", 0.0
    user = ("用户这句话:" + text.strip()[:500] + "\n\n当前工程上下文(JSON):\n"
            + json.dumps(ctx, ensure_ascii=False, separators=(",", ":")))
    body = {
        "model": st["model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    url = st["base"].rstrip("/") + "/chat/completions"
    t0 = time.time()
    last = ""
    for attempt in range(2):
        try:
            d = _post(url, st["token"], body, TIMEOUT)
            content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return _parse(content), "", round(time.time() - t0, 2)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:                       # noqa: BLE001
                detail = ""
            last = f"DeepSeek 回了 {e.code} {detail}".strip()
            if e.code in (400, 401, 402, 403, 422):
                break
        except Exception as e:                      # noqa: BLE001
            last = str(e)[:180] or e.__class__.__name__
        if attempt == 0:
            time.sleep(0.6)
    return None, last or "调不动模型", round(time.time() - t0, 2)


def _parse(content: str) -> dict | None:
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(s[i:j + 1])
            return d if isinstance(d, dict) else None
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 校验:模型说什么不算,这里说算
# ---------------------------------------------------------------------------
def _num(v, lo, hi, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return max(lo, min(hi, f))


def validate(raw: dict, ctx: dict, project: dict) -> tuple[list[dict], int, list[str]]:
    tracks = {str(t.get("id")): t for t in project.get("tracks") or []}
    ctx_tids = {str(t.get("id")) for t in ctx.get("tracks") or []}
    ctx_cids = {str(c.get("id")) for c in ctx.get("clips") or []}
    notes: list[str] = []
    out: list[dict] = []
    dropped = 0
    ops = raw.get("ops") if isinstance(raw, dict) else None
    if not isinstance(ops, list):
        return [], 0, ["模型没给出动作列表"]
    if len(ops) > MAX_OPS:
        notes.append(f"模型给了 {len(ops)} 条,只取前 {MAX_OPS} 条")
        dropped += len(ops) - MAX_OPS
        ops = ops[:MAX_OPS]

    def norm_name(a):
        tid = str(a.get("trackId") or "")
        tr = tracks.get(tid)
        return (tr.get("name") if tr else "") or tid

    for a in ops:
        if not isinstance(a, dict):
            dropped += 1
            continue
        k = str(a.get("kind") or "")
        why = str(a.get("why") or "")[:120]
        if k == "gain":
            tid = str(a.get("trackId") or "")
            if tid not in ctx_tids or tid not in tracks:
                dropped += 1
                continue
            to = _num(a.get("to"), 0.0, 1.5)
            if to is None:
                dropped += 1
                continue
            out.append({"id": "", "kind": "gain", "category": "vol",
                        "trackId": tid, "trackName": norm_name(a),
                        "from": round(float(tracks[tid].get("gain", 1) or 0), 3),
                        "to": round(to, 3), "why": why or "按你的描述调音量"})
        elif k == "pan":
            tid = str(a.get("trackId") or "")
            if tid not in ctx_tids or tid not in tracks:
                dropped += 1
                continue
            to = _num(a.get("to"), -1.0, 1.0)
            if to is None:
                dropped += 1
                continue
            out.append({"id": "", "kind": "pan", "category": "vol",
                        "trackId": tid, "trackName": norm_name(a),
                        "from": round(float(tracks[tid].get("pan", 0) or 0), 3),
                        "to": round(to, 3), "why": why or "按你的描述调声像"})
        elif k == "reverb":
            tid = str(a.get("trackId") or "")
            if tid not in ctx_tids or tid not in tracks:
                dropped += 1
                continue
            to = a.get("to") if isinstance(a.get("to"), dict) else {}
            mix = _num(to.get("mix"), 0.0, 1.0)
            size = _num(to.get("size"), 0.2, 4.0)
            if mix is None and size is None:
                dropped += 1
                continue
            cur = (tracks[tid].get("fx") or {}).get("reverb") or {}
            out.append({"id": "", "kind": "reverb", "category": "fx",
                        "trackId": tid, "trackName": norm_name(a),
                        "from": round(float(cur.get("mix") or 0), 3),
                        "to": {"mix": round(mix if mix is not None else float(cur.get("mix") or 0), 3),
                               "size": round(size if size is not None else float(cur.get("size") or 1.6), 2)},
                        "why": why or "按你的描述调混响"})
        elif k == "chain":
            tid = str(a.get("trackId") or "")
            if tid not in ctx_tids or tid not in tracks:
                dropped += 1
                continue
            slots = []
            for i, s in enumerate(a.get("slots") or []):
                slot = studio_fx.sanitize_slot(s, i)
                if slot is None:
                    continue
                slot["auto"] = True
                slot["why"] = str((s or {}).get("why") or "")[:120]
                slots.append(slot)
            if not slots:
                dropped += 1
                continue
            out.append({"id": "", "kind": "chain", "category": "fx",
                        "trackId": tid, "trackName": norm_name(a),
                        "slots": slots,
                        "why": why or "按你的描述加效果"})
        elif k == "clipgain":
            cid = str(a.get("clipId") or "")
            if cid not in ctx_cids:
                dropped += 1
                continue
            to = _num(a.get("to"), 0.0, 2.0)
            if to is None:
                dropped += 1
                continue
            cur = _find_clip(project, cid)
            out.append({"id": "", "kind": "clipgain", "category": "vol",
                        "clipId": cid, "clipName": "",
                        "from": round(float((cur or {}).get("gain", 1) or 0), 3),
                        "to": round(to, 3), "why": why or "按你的描述调这一块"})
        elif k == "pitch":
            cid = str(a.get("clipId") or "")
            if cid not in ctx_cids:
                dropped += 1
                continue
            to = a.get("to") if isinstance(a.get("to"), dict) else {}
            semis = _num(to.get("semis", 0), -24, 24)
            cents = _num(to.get("cents", 0), -100, 100)
            if semis is None and cents is None:
                dropped += 1
                continue
            cur = _find_clip(project, cid) or {}
            out.append({"id": "", "kind": "pitch", "category": "tune",
                        "clipId": cid,
                        "from": {"semis": int(cur.get("semis") or 0),
                                 "cents": int(cur.get("cents") or 0)},
                        "to": {"semis": int(round(semis or 0)),
                               "cents": int(round(cents or 0))},
                        "why": why or "按你的描述改这一块的音高"})
        else:
            dropped += 1
    return out, dropped, notes


def _find_clip(project: dict, cid: str) -> dict | None:
    for tr in project.get("tracks") or []:
        for c in tr.get("clips") or []:
            if str(c.get("id")) == cid:
                return c
    return None


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------
def plan(text: str, project: dict, scope: dict, send_names: bool = False) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "说一句想怎么改,比如「人声太干,加一点混响」"}
    ctx = build_context(project, scope, send_names=send_names)
    raw, err, sec = _ask(text, ctx)
    if raw is None:
        return {"ok": False, "reason": err, "sec_cost": sec}
    actions, dropped, notes = validate(raw, ctx, project)
    reply = str(raw.get("reply") or "").strip()[:300]
    if not actions:
        return {"ok": False, "reply": reply, "reason": "没听出要改什么,换个说法",
                "notes": notes, "sec_cost": sec}
    return {"ok": True, "reply": reply, "actions": actions, "notes": notes,
            "dropped": dropped, "sec_cost": sec}
