"""原唱知识库(本机):把每首歌的「音高地图」存下来,下次秒开、还能认歌。

为什么要有它
------------
一首 3 分钟的歌第一次分析要跑两遍 YIN(约 2~5 秒),同一首歌你会反复唱,
每一次都重算完全没必要 —— 分析结果只取决于**音频本身**。

所以这里按内容算一个 id(`sha1(采样)`),存一次:
    projects/refdb/index.json    元数据(名字、时长、调性、用过几次、唱过几次)
    projects/refdb/<id>.npz      每 10ms 的音高 + 每 50ms 的 12 维色度指纹
★ 只存**派生数据**,不复制音频;音频还在 projects/media/ 里。

「RAG」在这里的意思
------------------
把你这次唱的**音高轨迹**当查询,去库里找最像的那一首。
比的是「音高类序列」:同一个调、同一个旋律走向;换调也能认(12 个半音轮着试),
整体错位也能认(时间上滑一遍找最高相关)。

★ 它认的是「唱的是不是同一首歌」,不是「这是哪一首专业录音」。
★ 它不懂歌词、不懂音色;你唱得跟原唱差很多时它也会给出「最像的那首」并标明把握,
   把握不够就明说「库里没有像的」,不硬认。
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from pathlib import Path

import numpy as np

import studio_mix as sm

DB_DIR = sm.ROOT / "projects" / "refdb"
INDEX = DB_DIR / "index.json"
VERSION = 1
CHROMA_HOP = 0.05
MAX_ENTRIES = 400          # 再多就按「最久没用」收掉(只收 take,不碰原唱)

_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 色度:从音高轨迹直接数「现在唱的是哪个音级」
# --------------------------------------------------------------------------
def _buckets(times: np.ndarray, hop: float, n: int) -> np.ndarray:
    return np.clip((times / hop).astype(np.int64), 0, max(0, n - 1))


def chroma_from_cents(cents: np.ndarray, times: np.ndarray,
                      voiced: np.ndarray, hop: float = CHROMA_HOP) -> np.ndarray:
    n = int(math.ceil(float(times[-1]) / hop)) + 1 if len(times) else 0
    C = np.zeros((max(0, n), 12), dtype=np.float32)
    if n <= 0:
        return C
    ok = np.isfinite(cents) & voiced
    if not ok.any():
        return C
    idx = _buckets(times[ok], hop, n)
    cc = np.mod(cents[ok], 1200.0) / 100.0
    b = np.mod(np.round(cc).astype(np.int64), 12)
    np.add.at(C, (idx, b), 1.0)
    nrm = np.linalg.norm(C, axis=1, keepdims=True)
    return (C / np.maximum(nrm, 1e-6)).astype(np.float32)


def frame_cents(cents: np.ndarray, times: np.ndarray, voiced: np.ndarray,
                hop: float = CHROMA_HOP) -> np.ndarray:
    """每 50ms 一个桶,桶里取实测音高的中位数(不是取整到半音)。

    ★ 为什么要留连续值:认歌时如果只比「取整后的音级」,一个偏 55 音分的音
      会被算到隔壁半音上,本来 95% 一样的两个人会掉到 65%。比 cents 就不会。
    """
    n = int(math.ceil(float(times[-1]) / hop)) + 1 if len(times) else 0
    out = np.full(max(0, n), np.nan, dtype=np.float32)
    if n <= 0:
        return out
    ok = np.isfinite(cents) & voiced
    if not ok.any():
        return out
    idx = _buckets(times[ok], hop, n)
    s = np.zeros(n, dtype=np.float64)
    c = np.zeros(n, dtype=np.int64)
    np.add.at(s, idx, cents[ok].astype(np.float64))
    np.add.at(c, idx, 1)
    has = c > 0
    out[has] = (s[has] / c[has]).astype(np.float32)
    return out


# --------------------------------------------------------------------------
# 内容 id
# --------------------------------------------------------------------------
def pcm_id(path: Path) -> str:
    """采样级指纹:同样的声音换个文件名/换个目录,id 不变。"""
    x, sr = sm.load_mono(Path(path))
    h = hashlib.sha1()
    h.update(f"pcm-v1|{int(sr)}|{len(x)}".encode("utf-8"))
    h.update(np.asarray(x, dtype=np.float32).tobytes())
    return h.hexdigest()[:16]


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(sm.ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
def _read() -> dict:
    if not INDEX.exists():
        return {"version": VERSION, "entries": {}}
    try:
        d = json.loads(INDEX.read_text(encoding="utf-8"))
        if not isinstance(d.get("entries"), dict):
            raise ValueError("bad index")
        d.setdefault("version", VERSION)
        return d
    except Exception:                     # noqa: BLE001
        return {"version": VERSION, "entries": {}}


def _write(idx: dict) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_name(INDEX.name + ".part")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INDEX)


def _npz(eid: str) -> Path:
    return DB_DIR / f"{eid}.npz"


def _load(eid: str, meta: dict) -> dict | None:
    p = _npz(eid)
    if not p.exists():
        return None
    try:
        z = np.load(p)
        d = dict(meta)
        d["cents"] = z["cents"].astype(np.float64)
        d["times"] = z["times"].astype(np.float64)
        d["voiced"] = z["voiced"].astype(bool)
        d["chroma"] = z["chroma"].astype(np.float32)
        d["fcents"] = (z["fcents"].astype(np.float32) if "fcents" in z.files
                       else frame_cents(z["cents"].astype(np.float64),
                                        z["times"].astype(np.float64),
                                        z["voiced"].astype(bool)))
        d["hop_s"] = float(meta.get("hop_s") or 0.01)
        d["blocks"] = meta.get("blocks") or []
        d["cached"] = True
        d["refdb"] = {"id": eid, "name": meta.get("name"), "used": meta.get("used", 0),
                      "role": meta.get("role", "ref"), "dur": meta.get("dur")}
        return d
    except Exception:                     # noqa: BLE001
        return None


def _touch(eid: str, rel: str, st) -> None:
    with _LOCK:
        idx = _read()
        e = idx["entries"].get(eid)
        if not e:
            return
        e["path"] = rel
        e["size"] = int(st.st_size)
        e["mtime_ns"] = int(st.st_mtime_ns)
        e["used"] = int(e.get("used", 0)) + 1
        e["last"] = round(time.time(), 1)
        _write(idx)


# --------------------------------------------------------------------------
# 查询 / 写入
# --------------------------------------------------------------------------
def get(path) -> dict | None:
    """按「路径+大小+时间」或「采样指纹」找一条。命中就直接返回现成的轨迹。"""
    path = Path(path).resolve()
    if not path.exists():
        return None
    st = path.stat()
    rel = _rel(path)
    with _LOCK:
        idx = _read()
        for eid, e in idx["entries"].items():
            if (e.get("path") == rel and int(e.get("size", -1)) == st.st_size
                    and int(e.get("mtime_ns", -1)) == st.st_mtime_ns):
                d = _load(eid, e)
                if d is not None:
                    _touch(eid, rel, st)
                    d["hit"] = "path"
                    return d
    # 路径变了(改名/挪目录/重新导入)—— 用采样指纹再找一次
    try:
        h = pcm_id(path)
    except Exception:                     # noqa: BLE001
        return None
    with _LOCK:
        idx = _read()
        e = idx["entries"].get(h)
        if not e:
            return None
        d = _load(h, e)
        if d is None:
            return None
        _touch(h, rel, st)
        d["hit"] = "content"
        return d


def put(path, info: dict, role: str = "ref", name: str | None = None) -> dict:
    """把一次分析结果存进库。同一个音频重复存只会更新元数据。"""
    path = Path(path).resolve()
    st = path.stat()
    cents = np.asarray(info["cents"], dtype=np.float64)
    times = np.asarray(info["times"], dtype=np.float64)
    voiced = np.asarray(info["voiced"], dtype=bool)
    if not len(times):
        raise ValueError("空音频")
    eid = pcm_id(path)
    chroma = chroma_from_cents(cents, times, voiced)
    fcents = frame_cents(cents, times, voiced)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _npz(eid).with_name(_npz(eid).name + ".part.npz")
    np.savez_compressed(tmp, cents=cents.astype(np.float32),
                        times=times.astype(np.float32),
                        voiced=voiced, chroma=chroma, fcents=fcents)
    tmp.replace(_npz(eid))

    now = round(time.time(), 1)
    with _LOCK:
        idx = _read()
        old = idx["entries"].get(eid) or {}
        meta = {
            "id": eid,
            "name": name or old.get("name") or path.stem,
            "path": _rel(path),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
            "dur": round(float(info.get("dur") or (times[-1] if len(times) else 0)), 2),
            "sr": int(info.get("sr") or sm.SR),
            "hop_s": round(float(info.get("hop_s") or 0.01), 4),
            "voiced_s": info.get("voiced_s"),
            "key": info.get("key"),
            "mode": info.get("mode"),
            "blocks": (info.get("blocks") or [])[:600],
            "role": role or old.get("role") or "ref",
            "created": old.get("created") or now,
            "used": int(old.get("used", 0)) + 1,
            "last": now,
            "takes": old.get("takes") or [],
        }
        idx["entries"][eid] = meta
        _prune(idx)
        _write(idx)
    return {"id": eid, "name": meta["name"], "used": meta["used"], "role": meta["role"]}


def _prune(idx: dict) -> None:
    ents = idx["entries"]
    if len(ents) <= MAX_ENTRIES:
        return
    # 只收「你自己唱的」,原唱一条都不删
    takes = [(e.get("last", 0), k) for k, e in ents.items() if e.get("role") == "take"]
    takes.sort()
    for _, k in takes[: max(0, len(ents) - MAX_ENTRIES)]:
        idx["entries"].pop(k, None)
        try:
            _npz(k).unlink()
        except OSError:
            pass


def record_take(ref_id: str, stats: dict) -> None:
    """记一笔「这次唱得怎么样」,下次可以告诉他有没有进步。"""
    if not ref_id:
        return
    with _LOCK:
        idx = _read()
        e = idx["entries"].get(str(ref_id))
        if not e:
            return
        takes = list(e.get("takes") or [])
        takes.append({"t": round(time.time(), 1),
                      "medianCents": stats.get("medianCents"),
                      "coverage": stats.get("coverage"),
                      "frames": stats.get("frames")})
        e["takes"] = takes[-40:]
        _write(idx)


def entries(role: str | None = None, alive: bool = True) -> list[dict]:
    """给界面列的清单。alive=False 时不查文件在不在(快)。"""
    with _LOCK:
        idx = _read()
        out = []
        for k, e in idx["entries"].items():
            if role and e.get("role") != role:
                continue
            d = {kk: e.get(kk) for kk in
                 ("id", "name", "path", "dur", "voiced_s", "key", "mode",
                  "role", "created", "used", "last", "takes")}
            d["takes"] = len(e.get("takes") or [])
            d["lastMedianCents"] = ((e.get("takes") or [{}])[-1] or {}).get("medianCents")
            if alive:
                p = (sm.ROOT / e["path"]).resolve()
                d["exists"] = p.exists()
            out.append(d)
    out.sort(key=lambda d: -(d.get("last") or 0))
    return out


def delete(eid: str) -> bool:
    eid = str(eid)
    with _LOCK:
        idx = _read()
        if eid not in idx["entries"]:
            return False
        idx["entries"].pop(eid, None)
        _write(idx)
    try:
        _npz(eid).unlink()
    except OSError:
        pass
    return True


# --------------------------------------------------------------------------
# 认歌:拿一段音高轨迹去找最像的那首
# --------------------------------------------------------------------------
def _corr_surface(take: np.ndarray, ref: np.ndarray, hop: float) -> tuple[np.ndarray, np.ndarray]:
    """返回 (每个 lag 的相关值, lag 对应的秒)。12 个半音轮着试,取最大。"""
    nT, nR = len(take), len(ref)
    L = nT + nR - 1
    nfft = 1 << int(math.ceil(math.log2(max(2, L))))
    A = [np.fft.rfft(take[:, d].astype(np.float64), nfft) for d in range(12)]
    B = [np.fft.rfft(ref[::-1, d].astype(np.float64), nfft) for d in range(12)]
    best = np.full(L, -1e30, dtype=np.float64)
    for rot in range(12):
        r = np.zeros(L, dtype=np.float64)
        for d in range(12):
            r += np.fft.irfft(A[(d - rot) % 12] * B[d], nfft)[:L]
        np.maximum(best, r, out=best)
    lags = np.arange(L) - (nR - 1)
    return best, lags * hop


def _agreement(qf: np.ndarray, rf: np.ndarray, lag: int,
               tol: float = 120.0) -> tuple[float, int]:
    """在给定 lag 上,两句唱的音高是不是同一个(允许换调、允许 tol 音分内的差)。

    ★ 比的是**连续音高(音分)**,不是取整后的音级 ——
      偏 55 音分的音如果按音级算会掉到隔壁,这里不会。
    """
    nT, nR = len(qf), len(rf)
    t0 = max(0, lag)
    t1 = min(nT, nR + lag)
    if t1 <= t0:
        return 0.0, 0
    a = qf[t0:t1]
    b = rf[t0 - lag:t1 - lag]
    both = np.isfinite(a) & np.isfinite(b)
    n = int(both.sum())
    if n == 0:
        return 0.0, 0
    a = a[both].astype(np.float64)
    b = b[both].astype(np.float64)
    best = 0.0
    for rot in range(12):
        d = np.mod(a - b - rot * 100.0 + 600.0, 1200.0) - 600.0
        hit = float(np.mean(np.abs(d) <= tol))
        if hit > best:
            best = hit
    return best, n


def identify(cents: np.ndarray, times: np.ndarray, voiced: np.ndarray,
             top: int = 3, min_agree: float = 0.72) -> list[dict]:
    """用你这次的音高轨迹去库里认歌。

    返回按分数降序的候选;`okAll=True` 才表示「够像」。不够像就全部 False。
    ★ 只在 role="ref"(原唱)里找 —— 拿自己的录音互相配对没有意义。
    ★ 判据是**对上了多少个音级**(换调已吸收),不是相关系数:
      同一首歌翻唱,通常 75%~95%;两首不同的歌,一般 40%~65%。
    """
    q = chroma_from_cents(cents, times, voiced)
    qf = frame_cents(cents, times, voiced)
    if len(q) < 8:
        return []
    with _LOCK:
        idx = _read()
        cand = [(k, e) for k, e in idx["entries"].items() if e.get("role", "ref") == "ref"]
    out = []
    for eid, e in cand:
        try:
            z = np.load(_npz(eid))
            R = z["chroma"].astype(np.float32)
            rf = (z["fcents"].astype(np.float32) if "fcents" in z.files
                  else frame_cents(z["cents"].astype(np.float64),
                                   z["times"].astype(np.float64),
                                   z["voiced"].astype(bool)))
        except Exception:                 # noqa: BLE001
            continue
        if len(R) < 8:
            continue
        surf, secs = _corr_surface(q, R, CHROMA_HOP)
        nT, nR = len(q), len(R)
        lags = np.rint(secs / CHROMA_HOP).astype(np.int64)
        overlap = np.minimum(nT, nR - lags)
        overlap = np.where(lags >= 0, overlap, np.minimum(nT + lags, nR))
        ok = overlap >= max(8, int(0.22 * min(nT, nR)))
        if not ok.any():
            continue
        s = np.where(ok, surf, -1e30)
        i = int(np.argmax(s))
        lag = int(lags[i])
        peak = float(surf[i])
        ov = int(overlap[i])
        if peak <= 0:
            continue
        agree, n_agree = _agreement(qf, rf, lag)
        base = float(np.median(np.abs(surf[ok])))
        conf = peak / base if base > 1e-9 else 0.0
        # 分数 = 对上比例 × 证据量(帧数的平方根)—— 既看像不像,也看对上了多久
        score = agree * math.sqrt(max(1, n_agree))
        out.append({
            "id": eid, "name": e.get("name"), "dur": e.get("dur"),
            "key": e.get("key"), "mode": e.get("mode"),
            "voiced_s": e.get("voiced_s"), "takes": len(e.get("takes") or []),
            "offset": round(float(secs[i]), 3), "conf": round(conf, 2),
            "agree": round(agree, 3), "agreeSec": round(n_agree * CHROMA_HOP, 1),
            "score": round(score, 2), "overlapSec": round(ov * CHROMA_HOP, 1),
            "ok": bool(agree >= min_agree and n_agree * CHROMA_HOP >= 3.0),
            "okAll": bool(agree >= min_agree and n_agree * CHROMA_HOP >= 3.0),
        })
    out.sort(key=lambda d: -d["score"])
    return out[:top]
