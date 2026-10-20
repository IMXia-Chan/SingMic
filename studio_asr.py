"""语音识别增强:给音节块标上「这个字/这个词」。

切分本身**不依赖**识别 —— 识别只负责把文字贴到已经切好的块上。
模型没下载、下载失败、识别失败,都只降级(没有文字),不影响切块。

模型(sherpa-onnx,放 `%USERPROFILE%\\.singmic\\models\\asr\\`,dev/live 共用):
  * Paraformer zh small —— 中文逐字时间戳
  * Whisper tiny(多语) —— 语言自动识别 + 英文词级时间戳

★ 下载走 GitHub API 的 asset 接口(`Accept: application/octet-stream`),
  不走 `github.com/.../releases/download/...` —— 后者在这台机上时通时断,
  但 `api.github.com` 稳定,asset 会 302 到 release-assets.githubusercontent.com。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

ASR_SR = 16000
CHUNK_S = 20.0
CHUNK_OVERLAP_S = 0.4
MODEL_ROOT = (Path(os.environ.get("SINGMIC_MODEL_DIR")
                   or (Path.home() / ".singmic" / "models")) / "asr")

RELEASE_API = ("https://api.github.com/repos/k2-fsa/sherpa-onnx/"
               "releases/tags/asr-models")

MODELS = {
    "paraformer": {
        "name": "Paraformer 中文小模型",
        "archive": "sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2",
        "dir": "sherpa-onnx-paraformer-zh-small-2024-03-09",
        "asset_id": 155854908,
        "bytes": 77920048,
    },
    "whisper": {
        "name": "Whisper tiny(多语)",
        "archive": "sherpa-onnx-whisper-tiny.tar.bz2",
        "dir": "sherpa-onnx-whisper-tiny",
        "asset_id": 179373699,
        "bytes": 116204861,
    },
}

_DL_LOCK = threading.Lock()
_DL: dict[str, dict] = {}
_REC_LOCK = threading.Lock()
_REC: dict[str, object] = {}
_RUN_LOCK = threading.Lock()
_ASSET_LOCK = threading.Lock()
_ASSET_URL: dict[str, str] = {}


# ---------------------------------------------------------------------------
# 模型文件
# ---------------------------------------------------------------------------
def _find(d: Path, *patterns: str, prefer_int8: bool = True) -> Path | None:
    if not d.is_dir():
        return None
    hits = []
    for pat in patterns:
        hits += [p for p in d.glob(pat) if p.is_file()]
    if not hits:
        return None
    if prefer_int8:
        hits.sort(key=lambda p: (0 if ".int8." in p.name else 1, len(p.name)))
    return hits[0]


def model_files(which: str) -> dict | None:
    """返回这个模型跑起来需要的文件;缺一个就返回 None。"""
    m = MODELS.get(which)
    if not m:
        return None
    d = MODEL_ROOT / m["dir"]
    if which == "paraformer":
        model = _find(d, "model*.onnx")
        tokens = _find(d, "tokens.txt", prefer_int8=False)
        if not model or not tokens:
            return None
        return {"model": model, "tokens": tokens}
    if which == "whisper":
        enc = _find(d, "*encoder*.onnx")
        dec = _find(d, "*decoder*.onnx")
        tokens = _find(d, "*tokens*.txt", prefer_int8=False)
        if not enc or not dec or not tokens:
            return None
        return {"encoder": enc, "decoder": dec, "tokens": tokens}
    return None


def _dir_bytes(which: str) -> int:
    d = MODEL_ROOT / MODELS[which]["dir"]
    if not d.is_dir():
        return 0
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def status() -> dict:
    out = {"dir": str(MODEL_ROOT), "models": {}, "downloads": _dl_status()}
    for k, m in MODELS.items():
        files = model_files(k)
        out["models"][k] = {
            "name": m["name"],
            "ready": files is not None,
            "bytes": _dir_bytes(k),
            "expected": m["bytes"],
        }
    return out


def ready(which: str) -> bool:
    return model_files(which) is not None


def any_ready() -> bool:
    return ready("paraformer") or ready("whisper")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def _dl_status() -> dict:
    with _DL_LOCK:
        return {k: dict(v) for k, v in _DL.items()}


def _asset_url(which: str) -> str:
    """先用 release API 解析 asset id,失败就用写死的 id。"""
    m = MODELS[which]
    with _ASSET_LOCK:
        if which in _ASSET_URL:
            return _ASSET_URL[which]
    url = ("https://api.github.com/repos/k2-fsa/sherpa-onnx/"
           "releases/assets/%d" % m["asset_id"])
    try:
        req = urllib.request.Request(RELEASE_API, headers={
            "User-Agent": "SingMic/1.0",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        for a in data.get("assets") or []:
            if a.get("name") == m["archive"]:
                url = ("https://api.github.com/repos/k2-fsa/sherpa-onnx/"
                       "releases/assets/%d" % int(a["id"]))
                break
    except Exception:
        pass
    with _ASSET_LOCK:
        _ASSET_URL[which] = url
    return url


def _set_dl(which: str, **kw) -> None:
    with _DL_LOCK:
        st = _DL.setdefault(which, {})
        st.update(kw)
        st["t"] = time.time()


def start_download(which: str) -> dict:
    if which not in MODELS:
        return {"ok": False, "error": "没有这个模型"}
    with _DL_LOCK:
        cur = _DL.get(which) or {}
        if cur.get("state") in ("downloading", "extracting"):
            return {"ok": True, "state": cur.get("state")}
        _DL[which] = {"state": "downloading", "got": 0,
                      "total": MODELS[which]["bytes"], "error": ""}
    th = threading.Thread(target=_download_worker, args=(which,),
                          name="asr-dl-" + which, daemon=True)
    th.start()
    return {"ok": True, "state": "downloading"}


def _download_worker(which: str) -> None:
    m = MODELS[which]
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    part = MODEL_ROOT / (m["archive"] + ".part")
    stage = "download"
    try:
        url = _asset_url(which)
        # ★ 断点续传:上次超时留下的 .part 接着下,不然 78MB/116MB 一次超时就前功尽弃。
        #   GitHub 支持 Range;万一重定向后不认(回 200),只能从头写。
        last = None
        for attempt in range(1, 4):
            have = part.stat().st_size if part.exists() else 0
            if have >= m["bytes"]:
                break
            headers = {
                "User-Agent": "SingMic/1.0",
                "Accept": "application/octet-stream",
            }
            if have:
                headers["Range"] = "bytes=%d-" % have
            req = urllib.request.Request(url, headers=headers)
            got = have
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    resumed = have and getattr(r, "status", 200) == 206
                    if have and not resumed:
                        got = 0
                    _set_dl(which, state="downloading", got=got, total=m["bytes"],
                            attempt=attempt)
                    with open(part, "ab" if resumed else "wb") as f:
                        while True:
                            buf = r.read(1 << 20)
                            if not buf:
                                break
                            f.write(buf)
                            got += len(buf)
                            _set_dl(which, got=got)
            except Exception as e:              # noqa: BLE001
                last = e
                _set_dl(which, state="downloading", got=got, error=str(e)[:200])
                continue
            last = None
            if part.stat().st_size >= m["bytes"]:
                break
        size = part.stat().st_size if part.exists() else 0
        if size < m["bytes"]:
            raise RuntimeError("下载不完整:%d/%d 字节%s"
                               % (size, m["bytes"],
                                  ("(%s)" % last) if last else ""))
        stage = "extract"
        _set_dl(which, state="extracting", got=size)
        _extract(part, MODEL_ROOT)
        part.unlink(missing_ok=True)
        if model_files(which) is None:
            raise RuntimeError("解包后没找到模型文件")
        _set_dl(which, state="done", got=size, error="")
    except Exception as e:                      # noqa: BLE001
        _set_dl(which, state="error", error=str(e)[:300])
        # 下载阶段的半截文件留着,下次点下载能续;解包失败说明包坏了,删掉重下。
        if stage == "extract":
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


def _extract(archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    with tarfile.open(archive, "r:bz2") as tf:
        try:
            tf.extractall(dest, filter="data")   # Python 3.12+:防路径穿越
        except TypeError:                        # pragma: no cover
            for mem in tf.getmembers():
                p = (dest / mem.name).resolve()
                if p.is_relative_to(dest) and (mem.isfile() or mem.isdir()):
                    tf.extract(mem, dest)


def delete_model(which: str) -> dict:
    if which not in MODELS:
        return {"ok": False, "error": "没有这个模型"}
    d = MODEL_ROOT / MODELS[which]["dir"]
    try:
        if d.is_dir():
            shutil.rmtree(d)
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "error": str(e)}
    with _REC_LOCK:
        _REC.pop(which, None)
    with _DL_LOCK:
        _DL.pop(which, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
def _get_rec(which: str, lang: str = ""):
    key = which + "|" + (lang or "auto")
    with _REC_LOCK:
        if key in _REC:
            return _REC[key]
        import sherpa_onnx
        files = model_files(which)
        if files is None:
            raise RuntimeError("模型没下载")
        if which == "paraformer":
            rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(files["model"]), tokens=str(files["tokens"]),
                num_threads=2, sample_rate=ASR_SR, feature_dim=80,
                decoding_method="greedy_search")
        else:
            rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(files["encoder"]), decoder=str(files["decoder"]),
                tokens=str(files["tokens"]), language=lang or "",
                task="transcribe", num_threads=2,
                enable_token_timestamps=True)
        _REC[key] = rec
        return rec


def _resample(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == ASR_SR:
        return x.astype(np.float32, copy=False)
    from scipy.signal import resample_poly
    g = math.gcd(int(sr), ASR_SR)
    y = resample_poly(x.astype(np.float64), ASR_SR // g, sr // g)
    return y.astype(np.float32)


_TOKEN_OK = re.compile(r"[0-9A-Za-z㐀-鿿]")
# 摊 token 时间的算法版本;改了 _clean_tokens/_active_span 就加一,免得旧缓存赖着
_TOK_ALGO_V = 2


def _clean_tokens(tokens, timestamps, span: tuple[float, float] | None = None):
    """清理 token。返回 (tokens, 是否真的有逐 token 时间戳)。

    ★ 实测这个 Paraformer 导出**根本不返回 timestamps**(恒为空);这时不能把
      每个 token 都记成 0 秒 —— 那样全部字都会挤到同一个块上。改成在这一段里
      按字数均分时间,并标 `approx`,让上层知道「时间是估的」。
    """
    keep = []
    for i, t in enumerate(tokens or []):
        raw = str(t).replace("▁", " ")
        lead = raw.startswith(" ")
        txt = raw.strip()
        if not txt or txt.startswith("<") or txt.endswith(">"):
            continue
        if not _TOKEN_OK.search(txt):
            continue
        keep.append((i, txt, lead))
    has_ts = bool(timestamps) and len(timestamps) >= len(tokens or []) \
        and any(float(v) > 0 for v in timestamps)
    a, b = span if span else (0.0, 0.0)
    out = []
    n = len(keep)
    for j, (i, txt, lead) in enumerate(keep):
        if has_ts:
            t0 = float(timestamps[i])
            t1 = float(timestamps[i + 1]) if i + 1 < len(timestamps) else t0 + 0.12
        elif b > a and n:
            step = (b - a) / n
            t0, t1 = a + j * step, a + (j + 1) * step
        else:
            t0, t1 = 0.0, 0.05
        if t1 <= t0:
            t1 = t0 + 0.05
        out.append({"t0": round(t0, 3), "t1": round(t1, 3),
                    "text": txt, "lead": lead})
    return out, has_ts


def _active_span(wave: np.ndarray, sr: int = ASR_SR) -> tuple[float, float]:
    """这一段里**真有人声**的首尾(秒),用来摊 token 时间。

    ★ 模型不给时间戳时,只能把字按顺序摊进一段时长。若按整段摊,前后静音也会
      分走时间 —— 词会漂到静音块上、末尾的字挤不进去。所以先按能量裁掉两头。
    """
    n = len(wave)
    if n == 0:
        return (0.0, 0.0)
    peak = float(np.abs(wave).max())
    thr = max(0.005, peak * 0.03)
    step = max(1, int(0.02 * sr))
    first = last = -1
    for i in range(0, n, step):
        if float(np.abs(wave[i:i + step]).max()) >= thr:
            if first < 0:
                first = i
            last = i
    if first < 0:
        return (0.0, n / float(sr))
    return (first / float(sr), min(n, last + step) / float(sr))


def _decode_chunk(rec, wave: np.ndarray, offset: float,
                  span: tuple[float, float] | None = None
                  ) -> tuple[list[dict], str, bool]:
    stream = rec.create_stream()
    stream.accept_waveform(ASR_SR, wave)
    rec.decode_stream(stream)
    res = stream.result
    if span is None:
        span = (0.0, len(wave) / float(ASR_SR))
    toks, has_ts = _clean_tokens(getattr(res, "tokens", None),
                                 getattr(res, "timestamps", None), span)
    for t in toks:
        t["t0"] = round(t["t0"] + offset, 3)
        t["t1"] = round(t["t1"] + offset, 3)
    lang = str(getattr(res, "lang", "") or getattr(res, "language", "") or "")
    return toks, lang, (not has_ts)


def _dedupe(items: list[dict]) -> list[dict]:
    out, seen = [], set()
    for t in items:
        k = (t["text"], round(t["t0"], 1))
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _words(tokens: list[dict], lang: str) -> list[dict]:
    """把子词拼成词:中文一字一词;英文按空格/▁ 起新词。"""
    if lang.startswith("zh"):
        return [dict(t) for t in tokens]
    words, cur = [], None
    for t in tokens:
        txt = t["text"]
        starts = bool(t.get("lead"))
        if not txt:
            continue
        if starts and cur:
            words.append(cur)
            cur = None
        if cur is None:
            cur = {"t0": t["t0"], "t1": t["t1"], "text": txt}
        else:
            cur["text"] += txt
            cur["t1"] = t["t1"]
        if re.search(r"[.!?,;:。！？，；：]$", txt):
            words.append(cur)
            cur = None
    if cur:
        words.append(cur)
    return words


def _cache_path(p: Path) -> Path:
    return p.with_suffix(".asr.json")


def _model_ver() -> str:
    bits = []
    for w in ("whisper", "paraformer"):
        files = model_files(w)
        if files:
            bits.append(w + ":" + ",".join(str(x.stat().st_size) for x in files.values()))
    return "|".join(bits)


def recognize(path: Path, lang: str = "auto") -> dict:
    """识别一条音频。返回 `{ok, lang, text, tokens, words, model, reason?}`。"""
    path = Path(path)
    if not any_ready():
        return {"ok": False, "reason": "识别模型未下载", "tokens": [], "words": []}
    try:
        st = path.stat()
        # ★ 版本号进 key:改了摊时间/清理 token 的算法,旧缓存必须失效
        key = "%d:%d|%s|%s|t%d" % (st.st_size, int(st.st_mtime), lang,
                                   _model_ver(), _TOK_ALGO_V)
        cp = _cache_path(path)
        if cp.exists():
            try:
                old = json.loads(cp.read_text(encoding="utf-8"))
                if old.get("_key") == key and old.get("ok"):
                    old.pop("_key", None)
                    old["cached"] = True
                    return old
            except Exception:                   # noqa: BLE001
                pass

        x, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if x.ndim == 2:
            x = x[:, 0]
        audio = _resample(x, sr)
        n = len(audio)
        step = max(1, int((CHUNK_S - CHUNK_OVERLAP_S) * ASR_SR))
        chunk = int(CHUNK_S * ASR_SR)

        with _RUN_LOCK:
            # 明确说中文且 Paraformer 在 => 直接用它,省掉一遍 Whisper
            if lang == "zh" and ready("paraformer"):
                rec = _get_rec("paraformer", "")
                use_whisper = False
            elif ready("whisper"):
                rec = _get_rec("whisper", lang if lang in ("zh", "en") else "")
                use_whisper = True
            elif ready("paraformer"):
                rec = _get_rec("paraformer", "")
                use_whisper = False
            else:                               # pragma: no cover
                return {"ok": False, "reason": "识别模型未下载",
                        "tokens": [], "words": []}

            all_toks: list[dict] = []
            langs: list[str] = []
            approx = True
            for s in range(0, n, step):
                seg = audio[s:s + chunk]
                if len(seg) < int(0.15 * ASR_SR):
                    break
                tt, ldet, ap = _decode_chunk(rec, seg, s / ASR_SR,
                                             _active_span(seg))
                all_toks += tt
                approx = approx and ap
                if ldet:
                    langs.append(ldet)
                if s + chunk >= n:
                    break

            if use_whisper:
                detected = langs[0] if langs else (lang if lang in ("zh", "en") else "")
            else:
                detected = lang if lang in ("zh", "en") else "zh"
            toks = _dedupe(all_toks)

            # 中文优先用 Paraformer:同语言下它对中文更稳
            if use_whisper and detected.startswith("zh") and ready("paraformer"):
                rec_zh = _get_rec("paraformer", "")
                zh: list[dict] = []
                approx_zh = True
                for s in range(0, n, step):
                    seg = audio[s:s + chunk]
                    if len(seg) < int(0.15 * ASR_SR):
                        break
                    tt, _ld, ap = _decode_chunk(rec_zh, seg, s / ASR_SR,
                                                _active_span(seg))
                    zh += tt
                    approx_zh = approx_zh and ap
                    if s + chunk >= n:
                        break
                if zh:
                    toks = _dedupe(zh)
                    approx = approx_zh

        if detected.startswith("zh"):
            text = "".join(t["text"] for t in toks)
        else:
            text = " ".join(t["text"] for t in toks)
        words = _words(toks, detected or lang)
        out = {"ok": True, "lang": detected or lang, "text": text,
               "tokens": toks, "words": words,
               "approx": bool(approx),      # 时间戳是均分估的,不是模型给的
               "model": "paraformer+whisper" if ready("paraformer") and ready("whisper")
                         else ("paraformer" if ready("paraformer") else "whisper"),
               "cached": False}
        try:
            cp.write_text(json.dumps({**out, "_key": key}, ensure_ascii=False),
                          encoding="utf-8")
        except Exception:                       # noqa: BLE001
            pass
        return out
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "reason": str(e)[:300], "tokens": [], "words": []}


def annotate(blocks: list[dict], asr: dict) -> list[dict]:
    """把识别结果贴到块上;返回**新块**,不改缓存里的对象。

    ★ 中文且模型没给时间戳(实测 Paraformer 不给)时,按「字序 + 块时长」铺:
      模型只会吐一串字,没有每个字的时刻;硬按 0 秒对齐会让所有字挤在同一块。
      这种贴法是估算,块上标 `textApprox`。
    """
    items = asr.get("words") or asr.get("tokens") or []
    lang = str(asr.get("lang") or "")
    if items and asr.get("approx") and lang.startswith("zh"):
        nb = [b for b in blocks if b.get("kind") != "silence"]
        if nb:
            total = sum(max(1e-3, float(b["t1"]) - float(b["t0"])) for b in nb)
            n = len(items)
            # ★ 按块时长把字**连续**分给块:每个字只用一次、不跳过。
            #   识别出的字数常比块多(快唱时相邻音节会并成一块),均分取样会漏字。
            bounds, acc = [0], 0.0
            for b in nb:
                acc += max(1e-3, float(b["t1"]) - float(b["t0"]))
                bounds.append(int(round(acc / total * n)))
            out, k = [], 0
            for b in blocks:
                c = dict(b)
                if c.get("kind") != "silence":
                    lo, hi = bounds[k], bounds[k + 1]
                    k += 1
                    if hi > lo:
                        c["text"] = "".join(it["text"].strip()
                                            for it in items[lo:hi])
                        c["textApprox"] = True
                out.append(c)
            return out
    out = []
    for b in blocks:
        c = dict(b)
        if c.get("kind") == "silence" or not items:
            out.append(c)
            continue
        t0, t1 = float(c["t0"]), float(c["t1"])
        best, best_ov = None, 0.0
        for it in items:
            ov = min(t1, it["t1"]) - max(t0, it["t0"])
            if ov > best_ov:
                best, best_ov = it, ov
        dur = max(1e-6, t1 - t0)
        if best is not None and best_ov >= min(0.04, 0.2 * dur):
            c["text"] = best["text"].strip()
            c["wordT0"], c["wordT1"] = best["t0"], best["t1"]
        out.append(c)
    return out
