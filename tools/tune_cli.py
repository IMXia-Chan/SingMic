"""音符块编辑器的命令行版:不进网页也能切块 / 渲染 / 听。

用法:
    python tools/tune_cli.py blocks  recordings\\xxx_voice.wav
    python tools/tune_cli.py render  recordings\\xxx_voice.wav  edits.json
        [--mix recordings\\xxx_mix.wav] [--out out.wav] [--window 12.0,18.0]

edits.json 长这样(时间轴不联动,删了就留空):
    {"blocks": {"3": {"semis": 2},
                "4": {"len": 0.62},
                "5": {"deleted": true}}}
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tuning  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def write_wav(path: Path, data, sr: int) -> None:
    """先写 .part 再改名 —— 半截文件不许留在盘上。"""
    tmp = path.with_name(path.name + ".part")
    # ★ 必须显式给 format —— soundfile 认不出 .wav.part 的后缀
    sf.write(str(tmp), data, sr, subtype="FLOAT", format="WAV")
    tmp.replace(path)


def cmd_blocks(wav: Path) -> int:
    x, sr = sf.read(str(wav), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x[:, 0]
    t0 = time.perf_counter()
    res = tuning.segment(x, sr)
    dt = time.perf_counter() - t0

    print(f"{wav.name}  {res['dur']:.2f}s  {sr}Hz")
    print(f"调性 {res['key']} {res['mode']}  把握 {res.get('conf', 0):.2f}  "
          f"有声 {res['voiced_s']:.1f}s  块 {len(res['blocks'])}  耗时 {dt:.2f}s")
    print(f"\n{'id':>4} {'起':>9} {'止':>9} {'长':>6}  {'种类':<8} {'音高':>8}  音名")
    for b in res["blocks"]:
        print(f"{b['id']:>4} {b['t0']:9.3f} {b['t1']:9.3f} "
              f"{b['t1'] - b['t0']:6.3f}  {b['kind']:<8} "
              f"{b.get('f0', 0):8.1f}  {b.get('note', '')}")
    return 0


def cmd_render(wav: Path, edits_path: Path | None, mix_path: Path | None,
               out_path: Path | None, window) -> int:
    x, sr = sf.read(str(wav), dtype="float32", always_2d=False)
    if x.ndim == 2:
        x = x[:, 0]

    t0 = time.perf_counter()
    res = tuning.segment(x, sr)
    blocks = res["blocks"]

    edits = {}
    if edits_path is not None:
        edits = json.loads(edits_path.read_text(encoding="utf-8")).get("blocks", {})

    y = tuning.render_blocks(x, sr, blocks, edits)

    mix = None
    if mix_path is not None:
        m, sr2 = sf.read(str(mix_path), dtype="float32", always_2d=True)
        if sr2 != sr:
            print(f"!! 采样率不一致 {sr} vs {sr2}")
            return 2
        mix = tuning.rebuild_mix(m, x, y)

    if window:
        a, b = window
        ia, ib = int(a * sr), int(b * sr)
        y = y[ia:ib]
        if mix is not None:
            mix = mix[ia:ib]

    out_path = out_path or wav.with_name(wav.stem + "_edited.wav")
    write_wav(out_path, y, sr)
    print(f"写出 {out_path}  {len(y)/sr:.2f}s")

    if mix is not None:
        mp = out_path.with_name(out_path.stem.replace("_voice", "_mix") + ".wav") \
            if "_voice" in out_path.stem else out_path.with_name(out_path.stem + "_mix.wav")
        write_wav(mp, mix, sr)
        print(f"写出 {mp}  {len(mix)/sr:.2f}s")

    n_ed = sum(1 for v in edits.values() if v)
    print(f"编辑 {n_ed} 块 · 切块+渲染 {time.perf_counter()-t0:.2f}s")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("blocks", "render"):
        print(__doc__)
        return 2
    cmd = args[0]
    args = args[1:]
    if not args:
        print(__doc__)
        return 2
    wav = Path(args[0])
    args = args[1:]

    if cmd == "blocks":
        return cmd_blocks(wav)

    edits_path = Path(args[0]) if args and not args[0].startswith("--") else None
    if edits_path is not None:
        args = args[1:]

    mix_path, out_path, window = None, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mix" and i + 1 < len(args):
            mix_path = Path(args[i + 1]); i += 2
        elif a == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1]); i += 2
        elif a == "--window" and i + 1 < len(args):
            window = tuple(float(v) for v in args[i + 1].split(",")); i += 2
        else:
            print(f"不认识的参数 {a}")
            return 2
    return cmd_render(wav, edits_path, mix_path, out_path, window)


if __name__ == "__main__":
    sys.exit(main())
