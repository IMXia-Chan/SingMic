"""一键调音:本机测量 + 写死的规则,只出**方案**,不写工程。

★ 这里的「AI」不是神经网络:它是 numpy 测量(响度/峰值/频段/动态)
  加一套有上下限的规则。不上传任何音频,不联网。
★ 它只回答「可量的事」:响度平衡、峰值不削、低频不轰、明显的跑调。
  审美仍然是他的 —— 所有动作在界面上逐条列出,勾了才应用。
★ 所有改动都只是「建议值」:前端应用时**只取一次撤销快照**,Ctrl+Z 全退。

接口(给 `studio_api.py` 用):
    auto_mix(project, scopes="all", tune=True, vst_host=None) -> dict
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

import studio_fx
import studio_mix as sm

SR = sm.SR
BAND_EDGES = (20.0, 200.0, 500.0, 2000.0, 5000.0, 20000.0)
BAND_NAMES = ("20-200", "200-500", "500-2k", "2k-5k", "5k-20k")

LIMITS = [
    "「AI」= 本机测量 + 写死的规则,不是神经网络;不上传、不联网。",
    "它只保证响度平衡、峰值不削、低频不轰这类可量的事;审美仍然是你。",
    "应用后按一次 Ctrl+Z 可以全部撤销。",
]

# ★ 只改已经挂在轨上的 VST 槽,绝不自动加插件。
#   key 先按精确名找,找不到再按正则找;字符串型参数一律不碰(没有可靠的选项表)。
RECIPES = [
    {
        "match": ("kotelnikov",),
        "label": "人声压缩(温和)",
        "set": [
            ("threshold", -20), ("ratio", 2.5), ("attack", 15),
            ("release", 120), ("makeup", 1.5), ("dry_wet", 100),
        ],
    },
    {
        "match": ("dragonflyhall", "dragonflyhallreverb"),
        "label": "大厅混响",
        "set": [
            ("decay", 2.0), ("predelay", 25), ("size", 32),
            ("dry", 85), ("late", 15), ("width", 110),
        ],
    },
    {
        "match": ("dragonflyplate", "dragonflyplatereverb"),
        "label": "板式混响",
        "set": [
            ("decay", 1.8), ("predelay", 20), ("size", 24),
            ("dry", 85), ("late", 18), ("width", 110),
        ],
    },
    {
        "match": ("dragonflyroom", "dragonflyroomreverb"),
        "label": "房间混响",
        "set": [
            ("decay", 1.2), ("predelay", 12), ("size", 8),
            ("dry", 85), ("late", 15), ("width", 110),
        ],
    },
    {
        "match": ("slickeq",),
        "label": "轻微 EQ",
        "set": [
            (r"low.*gain", -1.5), (r"mid.*gain", 1.5), (r"high.*gain", 1.0),
        ],
    },
]

# 未收录插件的「词根兜底」:只在插件类型能认出来时才下手
GENERIC = {
    "compressor": (("comp", "compress", "limiter", "limiting"),
                   [("threshold", -20), ("ratio", 2.5), ("attack", 15),
                    ("release", 140)]),
    "reverb": (("reverb", "hall", "plate", "room", "混响"),
               [("decay", 1.8), ("predelay", 20), ("dry_wet", 85)]),
}


# ---------------------------------------------------------------------------
# 测量
# ---------------------------------------------------------------------------
def _db(x: float) -> float:
    return 20.0 * math.log10(max(float(x), 1e-9))


def _bands(y: np.ndarray) -> dict:
    """五段能量占比(不依赖 scipy:直接 rfft 分帧累加功率谱)。"""
    n = 4096
    x = np.asarray(y, dtype=np.float64).reshape(-1)
    if len(x) == 0:
        return {k: 0.0 for k in BAND_NAMES}
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    hop = n // 2
    win = np.hanning(n)
    frames = list(range(0, len(x) - n + 1, hop))
    # 长音频抽帧:最多算 2000 帧,结论足够稳,又不拖慢
    stride = max(1, len(frames) // 2000)
    acc = np.zeros(n // 2 + 1)
    cnt = 0
    for i in frames[::stride]:
        seg = x[i:i + n] * win
        acc += np.abs(np.fft.rfft(seg)) ** 2
        cnt += 1
    if not cnt:
        return {k: 0.0 for k in BAND_NAMES}
    acc /= cnt
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    out = {}
    for k, (a, b) in enumerate(zip(BAND_EDGES, BAND_EDGES[1:])):
        m = (freqs >= a) & (freqs < b)
        out[BAND_NAMES[k]] = float(acc[m].sum())
    total = sum(out.values()) or 1e-12
    return {k: round(v / total, 4) for k, v in out.items()}


def _centroid(y: np.ndarray) -> float:
    n = 4096
    x = np.asarray(y, dtype=np.float64).reshape(-1)
    if len(x) < n:
        return 0.0
    # 取中段一帧就够(只是一个「亮还是闷」的粗指标)
    i = max(0, len(x) // 2 - n // 2)
    mag = np.abs(np.fft.rfft(x[i:i + n] * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    s = mag.sum()
    return float((freqs * mag).sum() / s) if s > 1e-12 else 0.0


def _dyn_db(y: np.ndarray) -> float:
    """400ms 窗 RMS(dB)的标准差:越大越「一跳一跳」。"""
    w = int(round(0.4 * SR))
    x = np.asarray(y, dtype=np.float64).reshape(-1)
    if len(x) < w:
        return 0.0
    n = len(x) // w
    if n < 2:
        return 0.0
    r = x[:n * w].reshape(n, w)
    rms = np.sqrt(np.mean(r ** 2, axis=1))
    lv = 20.0 * np.log10(np.maximum(rms, 1e-6))
    lv = lv[lv > -60.0]
    return float(np.std(lv)) if len(lv) > 1 else 0.0


def _role(tr: dict) -> str:
    name = str(tr.get("name") or "")
    if tr.get("kind") == "voice":
        return "vocal"
    if any(k in name for k in ("人声", "主唱", "vocal", "voc", "lead", "唱")):
        return "vocal"
    return "backing"


def measure(project: dict) -> tuple[list[dict], dict, list[str]]:
    """每轨一条测量结果 + 伴奏合计响度 + 丢了哪些源。"""
    end = sm.project_end(project)
    if end <= 0:
        return [], {"lufs": -70.0}, []
    stems, missing = sm.render_stems(project, SR, "pre", 0.0, end)
    measures: list[dict] = []
    backing_audio = None
    for st in stems:
        y = np.asarray(st["audio"], dtype=np.float32)
        if not len(y):
            continue
        peak = float(np.max(np.abs(y))) if len(y) else 0.0
        rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2))) if len(y) else 0.0
        m = {
            "trackId": st["id"], "name": st["name"], "role": _role(
                _track_by_id(project, st["id"]) or {}),
            "lufs": round(float(sm.integrated_lufs(y, SR)), 2),
            "peak_db": round(_db(peak), 2),
            "rms_db": round(_db(rms), 2),
            "crest_db": round(_db(peak) - _db(rms), 2),
            "dyn_db": round(_dyn_db(y), 2),
            "centroid_hz": round(_centroid(y), 0),
            "bands": _bands(y),
        }
        measures.append(m)
        if m["role"] == "backing":
            backing_audio = y if backing_audio is None else _mix_sum(backing_audio, y)
    blufs = -70.0
    if backing_audio is not None and len(backing_audio):
        blufs = float(sm.integrated_lufs(backing_audio, SR))
    return measures, {"lufs": round(blufs, 2)}, missing


def _mix_sum(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = max(len(a), len(b))
    out = np.zeros(n, dtype=np.float32)
    out[:len(a)] += a
    out[:len(b)] += b
    return out


def _track_by_id(project: dict, tid: str) -> dict | None:
    for tr in project.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    return None


# ---------------------------------------------------------------------------
# 方案
# ---------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _fx_slot(kind: str, overrides: dict, why: str) -> dict:
    """按 FX_TYPES 的默认值建一个 auto 槽,再覆盖要改的那几项。"""
    t = studio_fx.FX_TYPES.get(kind) or {}
    params = {}
    for p in t.get("params") or []:
        params[p["key"]] = p.get("default")
    params.update(overrides)
    return {"id": f"auto_{kind}_{abs(hash((kind, tuple(sorted(overrides.items()))))) & 0xffff:04x}",
            "type": kind, "enabled": True, "auto": True,
            "params": params, "why": why}


def _gain_action(project: dict, tr: dict, m: dict, target: float,
                 why_ok: str, actions: list, notes: list) -> None:
    if m["lufs"] <= -55.0:
        return
    delta = _clamp(target - m["lufs"], -9.0, 9.0)
    capped = False
    if m["peak_db"] + delta > -1.0:
        # ★ 峰值保护:再高会削顶,只提到 -1 dB
        d2 = _clamp(-1.0 - m["peak_db"], -9.0, 9.0)
        if d2 < delta:
            delta, capped = d2, True
    if abs(delta) < 0.5:
        return
    g0 = float(tr.get("gain", 1) or 0)
    g1 = _clamp(g0 * (10 ** (delta / 20.0)), 0.0, 1.5)
    real = 20.0 * math.log10(max(g1, 1e-6) / max(g0, 1e-6)) if g0 > 0 else 0.0
    if abs(real) < 0.3:
        return
    why = why_ok.format(real=real)
    if abs(real - delta) > 0.2:
        why += "(推子最多 1.5×,已经到顶)"
    if capped:
        why += ",再高会削顶(峰值会超过 -1 dB)"
    actions.append({
        "id": "", "kind": "gain", "category": "vol",
        "trackId": str(tr.get("id")), "trackName": tr.get("name") or "轨道",
        "from": round(g0, 3), "to": round(g1, 3),
        "db": round(real, 1), "why": why,
    })


def _chain_actions(tr: dict, m: dict, role: str, actions: list) -> None:
    slots = []
    if role == "vocal":
        b = m["bands"]
        if b["20-200"] > 0.18:
            slots.append(_fx_slot("filter", {"mode": "highpass", "cutoff": 85, "q": 0.707},
                                  "低频占 {:.0%},高通 85Hz 去掉轰隆声".format(b["20-200"])))
        if m["crest_db"] > 14.0:
            th = _clamp(m["peak_db"] - 10.0, -28.0, -12.0)
            slots.append(_fx_slot("compressor", {
                "threshold": round(th, 1), "ratio": 3.0, "attack": 15.0,
                "release": 140.0, "knee": 6.0, "makeup": 2.0,
            }, "动态 {:.1f} dB,偏跳:3:1 轻压把字句捏稳".format(m["crest_db"])))
        if b["200-500"] > 0.35:
            slots.append(_fx_slot("eq3", {
                "midFreq": 350, "midGain": -2.0, "midQ": 1.0,
            }, "200–500Hz 占 {:.0%},这里最容易闷:中频减 2 dB".format(b["200-500"])))
        if b["2k-5k"] < 0.08:
            slots.append(_fx_slot("eq3", {
                "midFreq": 3200, "midGain": 1.5, "midQ": 1.0,
            }, "2–5kHz 只占 {:.0%},咬字会糊:中频提 1.5 dB".format(b["2k-5k"])))
    else:
        b = m["bands"]
        if m["crest_db"] > 16.0:
            slots.append(_fx_slot("compressor", {
                "threshold": -18.0, "ratio": 2.0, "attack": 20.0,
                "release": 200.0, "knee": 6.0, "makeup": 1.0,
            }, "动态 {:.1f} dB:2:1 轻压,让伴奏更贴".format(m["crest_db"])))
        if b["20-200"] > 0.35:
            slots.append(_fx_slot("eq3", {
                "lowFreq": 120, "lowGain": -1.5,
            }, "低频占 {:.0%}:低架减 1.5 dB".format(b["20-200"])))
    if slots:
        actions.append({
            "id": "", "kind": "chain", "category": "fx",
            "trackId": str(tr.get("id")), "trackName": tr.get("name") or "轨道",
            "slots": slots,
            "why": "按这一轨的测量值加 {} 个自动效果(重跑会替换旧的自动槽)".format(len(slots)),
        })


def _reverb_action(tr: dict, role: str, actions: list) -> None:
    if role != "vocal":
        return
    fx = tr.get("fx") or {}
    rv = fx.get("reverb") or {}
    if float(rv.get("mix", 0) or 0) >= 0.08:
        return
    # ★ 已经有 VST 混响就不再加旧混响,免得双混响糊成一团
    for s in fx.get("vst") or []:
        if any(k in str(s.get("name") or "").lower()
               for k in ("reverb", "hall", "plate", "room", "混响")):
            return
    actions.append({
        "id": "", "kind": "reverb", "category": "fx",
        "trackId": str(tr.get("id")), "trackName": tr.get("name") or "轨道",
        "from": round(float(rv.get("mix", 0) or 0), 3),
        "to": {"mix": 0.14, "size": 1.6},
        "why": "人声现在几乎是干的:加一点混响(14%、空间 1.6),不会糊",
    })


# --- VST ------------------------------------------------------------------
def _find_key(params: list[dict], want: str) -> str | None:
    keys = [str(p.get("key") or "") for p in params]
    if want in keys:
        return want
    wl = want.lower().replace("_", "")
    for k in keys:
        if k.lower().replace("_", "") == wl:
            return k
    return None


def _is_number(p: dict) -> bool:
    return p.get("type") == "number" and p.get("max") is not None \
        and p.get("min") is not None


def _vst_plan(host, tr: dict, role: str, notes: list, actions: list) -> None:
    fx = tr.get("fx") or {}
    for slot in fx.get("vst") or []:
        if slot.get("bypass"):
            continue
        name = str(slot.get("name") or "")
        low = name.lower()
        rec = None
        for r in RECIPES:
            if any(k in low for k in r["match"]):
                rec = r
                break
        rules: list[tuple[str, float]] = []
        label = ""
        if rec:
            rules = list(rec["set"])
            label = rec["label"]
        else:
            for kind, (words, rs) in GENERIC.items():
                if any(w in low for w in words):
                    rules = list(rs)
                    label = "保守设置"
                    break
        if not rules:
            continue
        try:
            info = host.inspect(slot.get("path") or "")
        except Exception as e:             # noqa: BLE001
            notes.append("插件「{}」读不出参数,跳过:{}".format(name, e))
            continue
        params = info.get("params") or []
        cur = dict(slot.get("params") or {})
        target = dict(cur)
        wants: dict[str, float] = {}
        for want, val in rules:
            key = _find_key(params, want) if not want.startswith("^") else None
            # 正则规则(SlickEQ 那类):在数值参数里按 key 找
            if key is None and any(ch in want for ch in ".*[]()|"):
                import re
                try:
                    rx = re.compile(want, re.I)
                except re.error:
                    continue
                for p in params:
                    if _is_number(p) and rx.search(str(p.get("key") or "")):
                        key = str(p["key"])
                        break
            if not key:
                continue
            p = next((x for x in params if str(x.get("key")) == key), None)
            if not p or not _is_number(p):
                continue
            lo, hi = float(p["min"]), float(p["max"])
            v = _clamp(float(val), lo, hi)
            wants[key] = v
            target[key] = v
        if not wants:
            continue

        # ★ 验货:真的设进去再读回来;插件不吃/自己夹住的值直接丢弃
        try:
            chk = host.inspect(slot.get("path") or "", params=wants)
        except Exception as e:             # noqa: BLE001
            notes.append("插件「{}」设参数失败,跳过:{}".format(name, e))
            continue
        applied = chk.get("applied") or {}
        good: dict[str, dict] = {}
        dropped = []
        for key, want in wants.items():
            got = applied.get(key)
            if isinstance(got, dict) and got.get("error"):
                dropped.append(key)
                continue
            try:
                g = float(got)
            except (TypeError, ValueError):
                dropped.append(key)
                continue
            if abs(g - want) > max(0.02, abs(want) * 0.02 + 1e-6):
                dropped.append(key)
                continue
            old = cur.get(key)
            try:
                oldf = float(old)
            except (TypeError, ValueError):
                oldf = None
            if oldf is not None and abs(oldf - g) < 1e-6:
                continue                    # 已经是这个值
            good[key] = {"from": old, "to": round(g, 3)}
        if dropped:
            notes.append("插件「{}」不吃这些值,已跳过:{}".format(
                name, "、".join(sorted(dropped))))
        if not good:
            continue
        actions.append({
            "id": "", "kind": "vst", "category": "vst",
            "trackId": str(tr.get("id")), "trackName": tr.get("name") or "轨道",
            "slotId": str(slot.get("id") or ""), "plugin": name,
            "params": good,
            "why": "{}:分轨测出需要 {} 的方向".format(label, "、".join(sorted(good))),
        })


# --- 修音 -----------------------------------------------------------------
def _pitch_actions(project: dict, tune: bool, actions: list, notes: list) -> None:
    if not tune:
        return
    by_src: dict[str, list[tuple[dict, dict]]] = {}
    for tr in project.get("tracks") or []:
        for c in tr.get("clips") or []:
            if c.get("deleted") or c.get("semis") or c.get("cents"):
                continue                    # ★ 已经动过音高的块不重复叠加
            if c.get("kind") == "audio":
                continue
            sid = c.get("sourceId")
            s = (project.get("sources") or {}).get(sid) or {}
            path = str(s.get("path") or "")
            if not path:
                continue
            by_src.setdefault(path, []).append((tr, c))

    for path, pairs in by_src.items():
        fp = Path(path)
        if not fp.is_absolute():
            fp = sm.ROOT / fp
        if not fp.is_file():
            notes.append("找不到源文件,修音跳过:{}".format(path))
            continue
        try:
            base = sm.check_pitch(fp, 30.0, 100.0)
            total = len(base.get("blocks") or [])
            ratio = (base.get("red") or 0) / max(1, total)
            # 红点太多 ⇒ 用 45 音分只修明显的,别把人味修掉
            cp = sm.check_pitch(fp, 45.0, 100.0) if ratio > 0.15 else base
        except Exception as e:             # noqa: BLE001
            notes.append("修音检测失败,跳过:{}".format(e))
            continue
        blocks = [b for b in (cp.get("blocks") or []) if b.get("status") == "red"]
        if not blocks:
            continue
        for tr, c in pairs:
            a = float(c.get("srcIn") or 0.0)
            b = float(c.get("srcOut") or (a + float(c.get("length") or 0)))
            hit = [x for x in blocks
                   if float(x.get("t1") or 0) > a and float(x.get("t0") or 0) < b]
            if not hit:
                continue
            wsum = sum(max(0.01, float(x["t1"]) - float(x["t0"])) for x in hit)
            dev = sum(float(x.get("cents") or 0) *
                      max(0.01, float(x["t1"]) - float(x["t0"])) for x in hit) / wsum
            shift = -dev * 0.9             # 强度 90%
            if abs(shift) < 3.0:
                continue
            semis = int(round(shift / 100.0))
            cents = shift - semis * 100.0
            semis = int(_clamp(semis, -24, 24))
            cents = round(_clamp(cents, -100.0, 100.0), 1)
            actions.append({
                "id": "", "kind": "pitch", "category": "tune",
                "trackId": str(tr.get("id")), "trackName": tr.get("name") or "轨道",
                "clipId": str(c.get("id") or ""),
                "from": {"semis": int(c.get("semis") or 0), "cents": round(float(c.get("cents") or 0), 1)},
                "to": {"semis": semis, "cents": cents},
                "why": "偏 {:+.0f} 音分,按 90% 收回来(整块平移,颤音保留)".format(dev),
                "flagged": len(hit),
                "threshold": 45 if (cp is not base) else 30,
            })


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------
def auto_mix(project: dict, scopes="all", tune: bool = True,
             vst_host=None) -> dict:
    t0 = time.time()
    if isinstance(scopes, str):
        wanted = {"vol", "fx", "vst", "tune"}
    else:
        wanted = {str(s) for s in (scopes or [])} or {"vol", "fx", "vst", "tune"}
    actions: list[dict] = []
    notes: list[str] = []

    measures, backing, missing = measure(project)
    if missing:
        notes.append("这些源文件找不到,没算进测量:{}".format("、".join(missing)))
    if not measures:
        return {"ok": True, "measures": [], "actions": [], "notes": ["工程里没有声音可测"],
                "limits": LIMITS, "sec_cost": round(time.time() - t0, 2)}

    tracks = {str(t.get("id")): t for t in project.get("tracks") or []}
    vocals = [m for m in measures if m["role"] == "vocal"]
    backs = [m for m in measures if m["role"] == "backing"]

    if "vol" in wanted:
        for m in vocals:
            tr = tracks.get(m["trackId"])
            if not tr:
                continue
            if backs:
                target = backing["lufs"] + 3.0
                why = ("人声 {:.1f} LUFS、伴奏合计 {:.1f} LUFS:"
                       "目标是人声比伴奏高 3 dB,{{real:+.1f}} dB").format(
                           m["lufs"], backing["lufs"])
            else:
                target = -18.0
                why = "只有人声:{:+.1f} dB 拉到 -18 LUFS".format(-18.0 - m["lufs"])
            _gain_action(project, tr, m, target, why, actions, notes)
        if len(backs) == 1:
            m = backs[0]
            tr = tracks.get(m["trackId"])
            if tr:
                _gain_action(project, tr, m, -20.0,
                             "单轨伴奏拉到 -20 LUFS({real:+.1f} dB)", actions, notes)
        elif len(backs) > 1:
            notes.append("有 {} 条伴奏轨:各自的内部平衡没动,只调人声".format(len(backs)))

    if "fx" in wanted:
        for m in measures:
            tr = tracks.get(m["trackId"])
            if not tr:
                continue
            _chain_actions(tr, m, m["role"], actions)
            _reverb_action(tr, m["role"], actions)

    if "vst" in wanted:
        if vst_host is None:
            have = sum(len((t.get("fx") or {}).get("vst") or [])
                       for t in project.get("tracks") or [])
            if have:
                notes.append("工程里有 {} 个 VST,但这次没起宿主,没动它们".format(have))
        else:
            for tr in project.get("tracks") or []:
                if (tr.get("fx") or {}).get("vst"):
                    _vst_plan(vst_host, tr, _role(tr), notes, actions)

    if "tune" in wanted:
        _pitch_actions(project, tune, actions, notes)

    for i, a in enumerate(actions):
        a["id"] = "a{}".format(i + 1)

    return {
        "ok": True,
        "measures": measures,
        "backing_lufs": backing["lufs"],
        "actions": actions,
        "notes": notes,
        "limits": LIMITS,
        "sec_cost": round(time.time() - t0, 2),
    }
