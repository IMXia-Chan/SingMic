"""用一条真人录音量 DeepFilterNet3 —— 合成唱终究不是人嗓子。

用法:
    PYTHONIOENCODING=utf-8 python tools/eval_real_voice.py <录音文件>

录音只用来量,不写回、不改动。48k 以外的会先线性重采样到 48k(模型只吃 48k)。

量三样:
    [1] 整条录音过 DFN 掉多少(人声电平几乎不该动)
    [2] 最响的 5 秒 + 室内噪声配到 10dB 信噪比:人声掉多少、停顿里噪声剩多少
    [3] 那 5 秒的逐 0.25 秒包络 —— 看它是「整段慢慢掉」还是「一开始就掐死」
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "_ai_try"))
sys.path.insert(0, str(HERE))
os.environ["DEEPFILTER_STREAM_MODEL_DIR"] = str(ROOT / "_ai_try" / "model")

from deepfilter_stream.model import DeepFilterModel          # noqa: E402
from probe_dfnet import bed_noise, run_stream, rms, db, SR   # noqa: E402


def load_mono_48k(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32")
    if x.ndim == 2:
        x = x.mean(axis=1)
    x = np.asarray(x, np.float32)
    if sr != SR and len(x):
        n = int(round(len(x) * SR / sr))
        t = np.arange(n, dtype=np.float64) * (sr / SR)
        x = np.interp(t, np.arange(len(x)), x).astype(np.float32)
    return x


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    x = load_mono_48k(Path(sys.argv[1]))
    if len(x) < SR:
        print("录音太短(<1 秒)")
        return 2
    print(f"录音 {len(x)/SR:.1f}s(48k 单声道)")

    model = DeepFilterModel()
    lims = [None, 20.0, 12.0, 6.0]

    print("\n[1] 整条录音过 DFN 掉多少(负得越多 = 削得越狠)")
    for l in lims:
        w = run_stream(model, x, l)
        print(f"  {'无上限' if l is None else f'{l:g}dB':>8s}: {db(rms(w)) - db(rms(x)):+6.2f} dB")

    # 最响的 5 秒当「唱的那段」
    win = 5 * SR
    if len(x) > win:
        e = np.array([rms(x[i:i + win]) for i in range(0, len(x) - win + 1, SR // 2)])
        a = int(np.argmax(e)) * (SR // 2)
        v = x[a:a + win]
        print(f"\n最响的 5 秒在 {a/SR:.2f}s")
    else:
        v = x
        print("\n录音不足 5 秒,用整条")

    v = v.copy()
    v *= 0.08 / (rms(v) + 1e-12)                     # 正常麦电平
    nz = bed_noise(len(v))
    nz *= rms(v) / (rms(nz) * 10 ** (10.0 / 20))     # 10dB 信噪比
    mix = v + nz

    print("\n[2] 人声 + 室内噪声(10dB 信噪比)")
    fl = 960
    e = np.array([rms(v[i:i + fl]) for i in range(0, len(v) - fl + 1, fl)])
    act = e > 0.2 * e.max()
    for l in lims:
        vw = run_stream(model, v, l)
        mw = run_stream(model, mix, l)
        keep = np.array([db(rms(mw[i * fl:(i + 1) * fl])) - db(rms(vw[i * fl:(i + 1) * fl]))
                         for i, s in enumerate(act) if s])
        rest = np.array([db(rms(mw[i * fl:(i + 1) * fl])) for i, s in enumerate(act) if not s])
        dry = np.array([db(rms(mix[i * fl:(i + 1) * fl])) for i, s in enumerate(act) if not s])
        print(f"     {'无上限' if l is None else f'{l:g}dB':>8s}: "
              f"人中位 {np.median(keep):+6.2f} dB   停顿残噪比干声低 {np.median(dry)-np.median(rest):5.1f} dB")

    print("\n[3] 那 5 秒逐 0.25 秒(干 vs 湿)")
    blk = int(0.25 * SR)
    for l in (None, 12.0):
        w = run_stream(model, v, l)
        line = [f"{db(rms(v[i:i+blk])) - db(rms(w[i:i+blk])):+5.1f}"
                for i in range(0, len(v) - blk + 1, blk)]
        print(f"  {'无上限' if l is None else f'{l:g}dB'}: " + " ".join(line))

    return 0


if __name__ == "__main__":
    sys.exit(main())
