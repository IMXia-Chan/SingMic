"""单独跑一遍人声分离(素材带伴奏时用)。输出 `<out>/<原名>_vocals.wav`。

用法:
    python separate_vocals.py --in ~/raw --out ~/vocals
    python separate_vocals.py --in a.mp3 --out ~/vocals

依赖:`demucs`(pip install demucs)。没装就直接报错,不悄悄跳过 ——
跳过会让带伴奏的原曲混进训练集,模型会学到别人的音色。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def separate(src: Path, out_dir: Path) -> Path:
    exe = shutil.which("demucs")
    if exe is None:
        raise SystemExit("没装 demucs:pip install demucs")
    tmp = out_dir / "_demucs"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "--two-stems=vocals", "-n", "htdemucs", "-o", str(tmp), str(src)]
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    cand = tmp / "htdemucs" / src.stem / "vocals.wav"
    if not cand.is_file():
        raise SystemExit("demucs 跑完了但没找到 %s" % cand)
    dst = out_dir / (src.stem + "_vocals.wav")
    shutil.copy2(cand, dst)
    shutil.rmtree(tmp, ignore_errors=True)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    dst.mkdir(parents=True, exist_ok=True)
    files = [src] if src.is_file() else sorted(
        p for p in src.rglob("*") if p.suffix.lower() in EXTS)
    if not files:
        print("没有音频文件", file=sys.stderr)
        return 2
    for p in files:
        try:
            out = separate(p, dst)
            print("  →", out, flush=True)
        except subprocess.CalledProcessError as e:
            print("  失败:", p.name, e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
