# -*- coding: utf-8 -*-
"""发行级母带与交付检查:真峰限幅 / 抖动 / 发行规格判定 / ID3 标签。

为什么要有这个模块,一句话:**采样峰达标不等于发行达标**。

流媒体(Dolby/Spotify/网易云/QQ/抖音)在编码成 AAC/Opus 时会重建波形,
采样之间的峰(真峰 / dBTP)可以比采样峰高出零点几 dB。一个「采样峰 -0.3 dBFS」
的母带,过一遍 AAC 就成了削顶 —— 用户听到的是失真,而你在自己机器上量不出来。

所以:
  · `true_peak_db()`  4× 过采样后的峰,就是 dBTP
  · `limit_true_peak()`  按 dBTP 限,不是按采样峰限
  · `dither()`  32 bit float → 16 bit 时的 TPDF 抖动(24 bit 不需要)
  · `measure()` / `verdicts()`  导出前把发行方会卡的东西一次量完
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

import studio_mix as sm


def _resample_poly(a, factor: int, axis: int = 0):
    """scipy 按需导入。

    ★ 只在量真峰/限幅时才需要它。冻结成 exe 之后 scipy 这一包有 48MB、
      启动时把整个 scipy.signal 拉起来要多花十几秒 —— 用户打开软件不该为
      「导出时才用得到的功能」等这么久。"""
    from scipy.signal import resample_poly
    return resample_poly(a, factor, 1, axis=axis)

SR = sm.SR

# 发行方普遍写的两条线
MAX_TRUE_PEAK_DB = -1.0          # dBTP
LUFS_TOLERANCE = 1.0             # ±1 LU


def _as2d(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y)
    return y[:, None] if y.ndim == 1 else y


def true_peak_db(y: np.ndarray, sr: int = SR, factor: int = 4) -> float:
    """dBTP:4× 过采样后的绝对值峰。"""
    a = _as2d(np.asarray(y, dtype=np.float64))
    if not a.size:
        return -120.0
    up = _resample_poly(a, factor, 0)
    p = float(np.max(np.abs(up))) if up.size else 0.0
    return 20.0 * math.log10(max(p, 1e-12))


def _oversampled_peaks(y: np.ndarray, factor: int = 4) -> np.ndarray:
    """把 4× 域上的峰折回**基础采样**的网格:每个基础采样一个「这段里最大多少」。

    ★ 限幅要在基础采样率上加增益,所以需要一个与 y 等长的包络;
      直接在 4× 域上改波形再降采样,会引入降采样自身的过冲。
    """
    a = _as2d(np.asarray(y, dtype=np.float64))
    n = len(a)
    up = np.abs(_resample_poly(a, factor, 0))     # (≈n*factor, ch)
    # 声道先合并,再按 factor 个一组折回基础采样
    p4 = up.max(axis=1)
    nf = len(p4) // factor
    if nf < 1:
        return np.abs(a).max(axis=1)
    p = p4[:nf * factor].reshape(nf, factor).max(axis=1)
    # resample_poly 首尾各补了半节 FIR,长度不一定是 n —— 统一成 n
    if len(p) < n:
        p = np.pad(p, (0, n - len(p)), mode="edge")
    elif len(p) > n:
        p = p[:n]
    return p


def limit_true_peak(y: np.ndarray, sr: int = SR, ceiling_db: float = MAX_TRUE_PEAK_DB,
                    lookahead: float = 0.005, iters: int = 3) -> np.ndarray:
    """按**真峰**限幅。

    ★ 两步走:
      1) 在 4× 域上找峰 → 折回基础采样 → 前瞻(往前看 5ms)+ 平滑 → 乘增益。
         迭代几次,让平滑后的新形状也达标(平滑会漏一点点)。
      2) **静态兜底**:还剩零点几 dB 的差就直接整体缩一下。
         宁可响度差 0.3 dB,也不许交付的母带在 AAC 里削顶。
    """
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    th = 10.0 ** (ceiling_db / 20.0)
    out = _as2d(np.asarray(y, dtype=np.float64)).copy()
    if not out.size:
        return out.astype(np.float32)
    look = max(1, int(round(lookahead * sr)))
    for _ in range(max(1, int(iters))):
        peak = _oversampled_peaks(out)
        if float(peak.max()) <= th * 1.0005:
            break
        gt = np.minimum(1.0, th / np.maximum(peak, 1e-12))
        # 前瞻:先把该让的地方提前让下去(否则就是硬削)
        g = minimum_filter1d(gt, size=2 * look + 1, mode="nearest")
        g = uniform_filter1d(g, size=look + 1, mode="nearest")
        out *= np.minimum(g, 1.0)[:, None]
    tp = 10.0 ** (true_peak_db(out, sr) / 20.0)
    if tp > th:                       # 静态兜底
        out *= th / tp
    return out.astype(np.float32)


def dither(y: np.ndarray, bits: int = 16, seed: int = 20261021) -> np.ndarray:
    """量化前的 TPDF 抖动(±1 LSB)。

    ★ 24 bit 的底噪本来就在 -144 dBFS,不需要抖;16 bit 不抖会出「量化毛刺」,
      那种毛刺在安静段和混响尾巴上听得出来。
    ★ 固定种子:同样的工程导出两次,文件逐字节一样 —— 发行要可复现。
    """
    if int(bits) >= 24:
        return np.asarray(y, dtype=np.float32)
    lsb = 2.0 ** -(int(bits) - 1)
    rng = np.random.default_rng(int(seed))
    a = _as2d(np.asarray(y, dtype=np.float32))
    n = (rng.random(a.shape) - rng.random(a.shape)).astype(np.float32)
    return (a + n * np.float32(lsb)).astype(np.float32)


def _db(x: float) -> float:
    return 20.0 * math.log10(max(float(x), 1e-12))


def measure(y: np.ndarray, sr: int = SR) -> dict:
    """发行方会看的几个数,一次量完。"""
    a = _as2d(np.asarray(y, dtype=np.float32))
    if not a.size:
        return {"sec": 0.0, "channels": 0}
    mono = a.mean(axis=1) if a.shape[1] > 1 else a[:, 0]
    peak = float(np.max(np.abs(a)))
    tp = true_peak_db(a, sr)
    # 「削平」= 连着好几个采样都顶在同一个最大值上。
    # ★ 不能数「≥ 0.999 的采样个数」:满幅正弦的峰附近有好几个采样都很大,
    #   但它们数值各不相同 —— 那没削,只是响。
    clipped = 0
    for c in range(a.shape[1]):
        col = np.abs(a[:, c])
        if not col.size:
            continue
        hot = (np.abs(col - col.max()) < 1e-6).astype(np.int8)
        d = np.diff(np.concatenate(([0], hot, [0])))
        st = np.nonzero(d == 1)[0]
        en = np.nonzero(d == -1)[0]
        if len(st):
            clipped = max(clipped, int((en - st).max()))
    thr = 10 ** (-60 / 20.0)
    hot = np.nonzero(np.abs(mono) > thr)[0]
    head_s = float(hot[0]) / sr if len(hot) else float(len(mono)) / sr
    tail_s = float(len(mono) - hot[-1] - 1) / sr if len(hot) else float(len(mono)) / sr
    lr = None
    phase = None
    if a.shape[1] > 1 and len(a) > 1:
        l = a[:, 0].astype(np.float64)
        r = a[:, 1].astype(np.float64)
        if float(np.std(l)) > 1e-9 and float(np.std(r)) > 1e-9:
            lr = round(float(np.corrcoef(l, r)[0, 1]), 4)
            phase = bool(lr < -0.2)          # 反相:单声道兼容会掉一半
    return {
        "sec": round(len(a) / float(sr), 3),
        "sr": int(sr),
        "channels": int(a.shape[1]),
        "lufs": round(float(sm.integrated_lufs(a, sr)), 2),
        "sample_peak_db": round(_db(peak), 2),
        "true_peak_db": round(float(tp), 2),
        "clipped": clipped,
        "dc": round(float(np.mean(a)), 5),
        "head_silence": round(head_s, 3),
        "tail_silence": round(tail_s, 3),
        "lr_corr": lr,
        "out_of_phase": phase,
    }


def verdicts(rep: dict, target_lufs: float | None = None,
             max_tp: float = MAX_TRUE_PEAK_DB) -> list[dict]:
    """把测量结果翻成「过 / 不过」的清单。★ 只判可量的事,不评价好不好听。"""
    out: list[dict] = []

    def add(key: str, label: str, ok: bool, value: str, want: str) -> None:
        out.append({"key": key, "label": label, "ok": bool(ok),
                    "value": value, "want": want})

    tp = rep.get("true_peak_db")
    add("true_peak", "真峰(dBTP)", tp is not None and tp <= max_tp,
        f"{tp:+.2f} dBTP" if tp is not None else "—", f"≤ {max_tp:+.1f} dBTP")

    if target_lufs is not None:
        lu = rep.get("lufs")
        ok = lu is not None and abs(lu - target_lufs) <= LUFS_TOLERANCE
        add("lufs", "整体响度", ok,
            f"{lu:.2f} LUFS" if lu is not None else "—",
            f"{target_lufs:+.0f} ±{LUFS_TOLERANCE:.0f} LUFS")

    add("clipped", "削平的样本", int(rep.get("clipped") or 0) < 3,
        str(rep.get("clipped")), "连续贴顶 < 3 个采样")
    dc = abs(float(rep.get("dc") or 0.0))
    add("dc", "直流偏移", dc < 1e-3, f"{rep.get('dc')}", "|DC| < 0.001")
    add("head", "开头静音", float(rep.get("head_silence") or 0) <= 1.0,
        f"{rep.get('head_silence')}s", "≤ 1.0s")
    add("tail", "结尾静音", float(rep.get("tail_silence") or 0) <= 3.0,
        f"{rep.get('tail_silence')}s", "≤ 3.0s")
    if rep.get("lr_corr") is not None:
        add("phase", "左右相位", not rep.get("out_of_phase"),
            f"相关 {rep.get('lr_corr')}", "不反相(相关 > -0.2)")
    return out


# ---------------------------------------------------------------------------
# ID3 标签 —— ★ 不装 mutagen:只写需要的几个帧,几十行够了
# ---------------------------------------------------------------------------
def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _frame(fid: str, payload: bytes) -> bytes:
    return (fid.encode("ascii") + len(payload).to_bytes(4, "big") + b"\x00\x00"
            + payload)


def _text(fid: str, value: str) -> bytes:
    """ID3v2.3 的文本帧:编码字节 0x01 = UTF-16 带 BOM。

    ★ 用 2.3 而不是 2.4:2.3 + UTF-16 是中文标签兼容性最好的一种组合
      (老播放器、车机、Windows 资源管理器都吃)。
    """
    return _frame(fid, b"\x01" + b"\xff\xfe" + str(value).encode("utf-16-le"))


def add_wav_id3(path, tag: bytes) -> int:
    """把 ID3v2 标签塞进 wav 的 `id3 ` 块(RIFF 允许的私有块 id)。

    ★ 不能直接拼在文件尾:RIFF 的 size 字段得跟着改,而且有些解码器/发行后台
      只扫 `data` 之前的块。所以插在 `fmt ` 之后、`data` 之前。
    ★ 块大小写的是**标签本身的字节数**;标签长度是奇数时补一个 0 凑偶,
      但 size 不算那个补位 —— 这是 RIFF 的规矩,写错了 libsndfile 会读不出来。
    """
    if not tag:
        return 0
    p = Path(path)
    blob = p.read_bytes()
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE")
    pos, insert_at = 12, None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        csz = int.from_bytes(blob[pos + 4:pos + 8], "little")
        if cid == b"fmt " and insert_at is None:
            insert_at = pos + 8 + csz + (csz & 1)
        pos += 8 + csz + (csz & 1)
    if insert_at is None:
        insert_at = 12
    pad = b"\x00" if len(tag) & 1 else b""
    chunk = b"id3 " + len(tag).to_bytes(4, "little") + tag + pad
    out = blob[:insert_at] + chunk + blob[insert_at:]
    out = b"RIFF" + (len(out) - 8).to_bytes(4, "little") + out[8:]
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_bytes(out)
    tmp.replace(p)
    return len(tag)


def id3v23(tags: dict | None = None, cover: bytes | None = None,
           cover_mime: str = "image/jpeg") -> bytes:
    """拼一个 ID3v2.3 头。没有标签也没有封面就返回空 bytes。"""
    tags = {k: v for k, v in (tags or {}).items() if v not in (None, "")}
    if not tags and not cover:
        return b""
    body = b""
    for key, fid in (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"),
                     ("albumArtist", "TPE2"), ("year", "TDRC"),
                     ("track", "TRCK"), ("genre", "TCON"), ("isrc", "TSRC")):
        if key in tags:
            body += _text(fid, tags[key])
    if tags.get("comment"):
        body += _frame("COMM", b"\x01eng" + b"\xff\xfe"
                       + str(tags["comment"]).encode("utf-16-le"))
    if cover:
        mime = str(cover_mime or "image/jpeg").encode("ascii", "replace")
        body += _frame("APIC", b"\x00" + mime + b"\x00" + b"\x03" + b"\x00" + cover)
    return b"ID3\x03\x00\x00" + _syncsafe(len(body)) + body
