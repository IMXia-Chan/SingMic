"""把一堆录音整理成 RVC 能吃的训练集 —— 在云服务器上跑,也在本机可以跑。

做的事:
  1. 扫目录里的音频(wav/mp3/flac/ogg/m4a);
  2. 立体声文件且装了 demucs → 先分人声(带伴奏的整曲就是这样来的);
     单声道录音就当人声,分不出伴奏也照用,但会在报告里标出来;
  3. 转 48k 单声道、峰值归一到 −1 dBFS(只往下压,不把削顶的片段再顶上去);
  4. 简易 VAD:按 20ms 帧能量找有声段,切 3–12 秒,前后留 100ms;
  5. 质检:削顶比例 >0.5%、RMS < −45 dBFS、长度不足的片段丢掉并计数;
  6. 写 `dataset/<名>_<序号>.wav`(PCM16)与 `dataset/meta.json`。

★ 素材里**不要有**音乐/和声/多人说话;分离不干净会让模型学到别人的音色。
★ 2–10 分钟的干净人声就能训基础版;越多越稳。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
FRAME = 0.02
MIN_CLIP = 3.0
MAX_CLIP = 12.0
PAD = 0.10


def load_audio(p: Path) -> tuple[np.ndarray, int]:
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    return x, sr


def to_mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]


def resample(x: np.ndarray, sr: int, dst: int) -> np.ndarray:
    if sr == dst:
        return x.astype(np.float32)
    g = math.gcd(int(sr), int(dst))
    y = resample_poly(x, dst // g, sr // g)
    return np.asarray(y, dtype=np.float32)


def separate_vocals(p: Path, out_dir: Path) -> Path | None:
    """调 demucs 分出人声;没有 demucs 或失败返回 None(调用方记警告)。"""
    if shutil.which("demucs") is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [shutil.which("demucs"), "--two-stems=vocals", "-n", "htdemucs",
           "-o", str(out_dir), str(p)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    cand = out_dir / "htdemucs" / p.stem / "vocals.wav"
    return cand if cand.is_file() else None


def activity(x: np.ndarray, sr: int) -> list[tuple[int, int]]:
    """20ms 帧能量 → 有声区间(采样下标)。阈值 = 底噪 p10 + 12dB。"""
    n = int(FRAME * sr)
    m = len(x) // n
    if m < 2:
        return [(0, len(x))] if len(x) else []
    e = (x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12
    db = 10 * np.log10(e)
    floor = float(np.percentile(db, 10))
    high = float(np.percentile(db, 95))
    thr = floor + min(18.0, max(8.0, 0.45 * (high - floor)))
    on = db > thr
    runs, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i))
            s = None
    if s is not None:
        runs.append((s, m))
    runs = [(a, b) for a, b in runs if (b - a) * FRAME >= 0.15]
    merged = []
    for a, b in runs:                       # 字间短缝(<120ms)不切
        if merged and (a - merged[-1][1]) * FRAME < 0.12:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(int(a * n), min(len(x), int(b * n))) for a, b in merged]


def slice_runs(x: np.ndarray, sr: int, runs) -> list[np.ndarray]:
    out, cur, cur_start = [], [], None
    for a, b in runs:
        a2 = max(0, a - int(PAD * sr))
        b2 = min(len(x), b + int(PAD * sr))
        if cur and (b2 - cur_start) / sr > MAX_CLIP:
            out.append(x[cur_start:cur[-1]])
            cur, cur_start = [], None
        if not cur:
            cur_start = a2
        cur.append(b2)
        if (b2 - cur_start) / sr >= MIN_CLIP:
            out.append(x[cur_start:b2])
            cur, cur_start = [], None
    if cur and (cur[-1] - cur_start) / sr >= MIN_CLIP:
        out.append(x[cur_start:cur[-1]])
    return [s for s in out if len(s) / sr >= MIN_CLIP]


def normalize_peak(x: np.ndarray, dbfs: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak <= 1e-6:
        return x
    target = 10 ** (dbfs / 20.0)
    # 削顶的片段别再往上顶;只允许压下来
    return x * min(1.0, target / peak)


def main() -> int:
    ap = argparse.ArgumentParser(description="整理补音训练素材")
    ap.add_argument("--in", dest="src", required=True, help="原始录音目录")
    ap.add_argument("--out", dest="dst", required=True, help="输出目录(会清空)")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--demucs", choices=["auto", "on", "off"], default="auto",
                    help="auto=立体声文件才分人声")
    ap.add_argument("--no-clean", action="store_true", help="别清空输出目录(追加)")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_dir():
        print("没有这个目录:", src, file=sys.stderr)
        return 2
    if dst.exists() and not args.no_clean:
        for p in dst.glob("*.wav"):
            p.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    tmp = dst / "_sep"

    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in EXTS)
    report = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "dataset": str(dst), "files": [], "clips": 0, "seconds": 0.0,
              "skipped": []}
    idx = 0
    for p in files:
        item = {"file": p.name, "clips": 0, "seconds": 0.0, "note": ""}
        try:
            x2, sr = load_audio(p)
        except Exception as e:                # noqa: BLE001
            report["skipped"].append({"file": p.name, "why": "读不了: %s" % e})
            continue
        stereo = x2.shape[1] > 1
        sep = None
        if args.demucs == "on" or (args.demucs == "auto" and stereo):
            sep = separate_vocals(p, tmp)
            if sep is None:
                item["note"] = "demucs 不可用,按整条当人声"
                report["skipped"].append(
                    {"file": p.name, "why": "立体声但没分人声(demucs 缺失/失败)"})
        if sep is not None:
            x2, sr = load_audio(sep)
            item["note"] = "已分人声"
        x = resample(to_mono(x2), sr, args.sr)
        if len(x) / args.sr < MIN_CLIP:
            report["skipped"].append({"file": p.name, "why": "短于 3 秒"})
            continue
        runs = activity(x, args.sr)
        pieces = slice_runs(x, args.sr, runs)
        for piece in pieces:
            peak = float(np.max(np.abs(piece)))
            clip_ratio = float((np.abs(piece) > 0.995).mean())
            rms_db = 20 * math.log10(float(np.sqrt(np.mean(piece ** 2)) + 1e-12))
            if clip_ratio > 0.005 or rms_db < -45:
                report["skipped"].append(
                    {"file": p.name, "why": "质检不过(削顶 %.2f%% / %.1f dBFS)"
                     % (clip_ratio * 100, rms_db)})
                continue
            piece = normalize_peak(piece)
            idx += 1
            name = "%s_%03d.wav" % (p.stem, idx)
            sf.write(str(dst / name), piece, args.sr, subtype="PCM_16")
            item["clips"] += 1
            item["seconds"] += len(piece) / args.sr
        report["clips"] += item["clips"]
        report["seconds"] += item["seconds"]
        report["files"].append(item)
        print("%-40s %2d 段 / %6.1fs %s" % (p.name, item["clips"], item["seconds"],
                                           item["note"]), flush=True)

    shutil.rmtree(tmp, ignore_errors=True)
    report["total_sec"] = round(report["seconds"], 1)
    (dst / "meta.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   "utf-8")
    print("\n共 %d 段 / %.1f 秒 → %s" % (report["clips"], report["seconds"], dst))
    if report["seconds"] < 180:
        print("★ 不足 3 分钟,不建议训练;至少 8 分钟才像样。")
    elif report["seconds"] < 480:
        print("★ 只有 %.1f 分钟,只能训基础版;多录一些会更稳。" % (report["seconds"] / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
