"""量「每 10ms 块接缝处的跳变」到底是信号本来就有的,还是我们处理链做出来的。

★ 判据只有一条:喂一段**连续**(相位跨块不断)的正弦 / 噪声进去,
  块接缝处的跳变必须和块内一样大。接缝更大 = 链子自己造的咔。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\probe_seams.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dsp import SpectralEQ, VoiceChain   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR, BLOCK = 48000, 480
SECONDS = 10
N = SR * SECONDS


def signal_sine() -> np.ndarray:
    """1kHz + 220Hz,连续相位。"""
    t = np.arange(N, dtype=np.float64) / SR
    return (0.15 * np.sin(2 * np.pi * 1000 * t)
            + 0.05 * np.sin(2 * np.pi * 220 * t + 0.7)).astype(np.float32)


def signal_noise(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (0.05 * rng.standard_normal(N)).astype(np.float32)


def run_chain(make, x: np.ndarray) -> np.ndarray:
    obj = make()
    out = np.empty_like(x)
    for i in range(0, len(x) - BLOCK + 1, BLOCK):
        out[i:i + BLOCK] = obj.process(x[i:i + BLOCK])
    return out


def measure(y: np.ndarray) -> tuple[float, float, float, float]:
    """返回 (接缝中位跳变, 块内中位跳变, 比值, 接缝跳变/局部RMS 最大)。"""
    d = np.abs(np.diff(y.astype(np.float64)))
    idx = np.arange(len(d))
    is_edge = (idx % BLOCK) == (BLOCK - 1)       # 第 block-1 个差 = 块末尾到下一块开头
    edge = d[is_edge]
    inner = d[~is_edge]
    e_med = float(np.median(edge))
    i_med = float(np.median(inner))
    ratio = e_med / max(i_med, 1e-20)
    # 局部 RMS(接缝前后各 5ms)
    win = BLOCK
    loc = np.empty(len(edge), dtype=np.float64)
    for k, j in enumerate(idx[is_edge]):
        a, b = max(0, j - win), min(len(y), j + win)
        loc[k] = np.sqrt(np.mean(y[a:b].astype(np.float64) ** 2)) + 1e-20
    rel = edge.astype(np.float64) / loc
    return e_med, i_med, ratio, float(rel.max())


def main() -> int:
    for name, gen in (("正弦", signal_sine), ("白噪", signal_noise)):
        x = gen()
        print(f"\n=== {name} ===", flush=True)

        # 0) 原样(参照:信号本身)
        m = measure(x)
        print(f"  直通            edge/inner = {m[2]:.2f}   最大相对跳变 {m[3]:.2f}")

        # 1) 纯重叠相加:所有处理关掉、高通也关掉
        def ola_only():
            e = SpectralEQ(SR, BLOCK)
            e.hp_hz = 0.0
            e.eq_low_db = e.eq_mid_db = e.eq_high_db = 0.0
            e.denoise = 0.0
            e.deess = 0.0
            return e
        m = measure(run_chain(ola_only, x))
        print(f"  OLA(什么都不做)  edge/inner = {m[2]:.2f}   最大相对跳变 {m[3]:.2f}")

        # 2) 只开高通 80Hz
        def hp_only():
            e = SpectralEQ(SR, BLOCK)
            e.hp_hz = 80.0
            e.eq_low_db = e.eq_mid_db = e.eq_high_db = 0.0
            return e
        m = measure(run_chain(hp_only, x))
        print(f"  只开高通 80Hz    edge/inner = {m[2]:.2f}   最大相对跳变 {m[3]:.2f}")

        # 3) 整条链,按 config.toml 那套
        def full():
            vc = VoiceChain(SR, BLOCK)
            vc.input_gain = 1.5
            vc.denoise = 0.0
            vc.gate_enabled = False
            vc.eq_low_db = -1.5
            vc.eq_mid_db = 3.0
            vc.eq_high_db = 4.0
            vc.deess = 0.35
            vc.comp.enabled = False
            return vc
        m = measure(run_chain(full, x))
        print(f"  整条链(config)   edge/inner = {m[2]:.2f}   最大相对跳变 {m[3]:.2f}")

    # ---- 真实录音(可选:命令行传一条录音路径过来) ----
    import soundfile as sf
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if p is not None and p.exists():
        y, sr = sf.read(str(p), dtype="float32")
        if y.ndim > 1:
            y = y[:, 0]
        if sr == SR:
            m = measure(y)
            print(f"\n=== 真实录音 ===\n  edge/inner = {m[2]:.2f}   最大相对跳变 {m[3]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
