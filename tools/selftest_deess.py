"""齿音抑制(de-esser)的行为自测 —— 不用手机也不用声卡。

★ 它必须同时满足两件互相矛盾的事:
   ① 该压的时候压得下去(高频相对中频太突出 = 齿音)
   ② 不该压的时候一点都别碰(正常唱歌时高频本来就比中频低)
  只满足①的叫「压高频」,只满足②的叫「没装」。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_deess.py
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

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


SR, BLOCK = 48000, 480
T = np.arange(BLOCK, dtype=np.float32) / SR


def tone(f: float, amp: float = 0.3) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * f * T)).astype(np.float32)


def peak_db(x: np.ndarray, f0: float) -> float:
    """这一块里最靠近 f0 的那个 bin 的幅度(dB)。"""
    w = np.hanning(len(x))
    X = np.abs(np.fft.rfft(x * w))
    fr = np.fft.rfftfreq(len(x), 1.0 / SR)
    i = int(np.argmin(np.abs(fr - f0)))
    return 20 * np.log10(float(X[max(0, i - 1):i + 2].max()) + 1e-12)


def run(eq: SpectralEQ, x: np.ndarray, blocks: int = 30) -> np.ndarray:
    """按块喂进去,返回最后一块的输出(让平滑状态先跑稳)。"""
    y = None
    for _ in range(blocks):
        y = eq.process(x)
    return y


def main() -> int:
    # ---- 1) 齿音:高频和中频一样响 → 高频该被压,中频一个 dB 都不该动 ----
    x = tone(1000) + tone(6500)
    eq0 = SpectralEQ(SR, BLOCK); eq0.deess = 0.0
    eq1 = SpectralEQ(SR, BLOCK); eq1.deess = 1.0
    y0 = run(eq0, x)
    y1 = run(eq1, x)
    d_mid = peak_db(y1, 1000) - peak_db(y0, 1000)
    d_hi = peak_db(y1, 6500) - peak_db(y0, 6500)
    check("齿音时高频被压下去", d_hi < -4.0, f"6.5kHz {d_hi:+.1f} dB")
    check("中频一点都没碰", abs(d_mid) < 0.3, f"1kHz {d_mid:+.1f} dB")

    # ---- 2) 正常唱歌:只有中频,高频很弱 → 不许触发 ----
    x2 = tone(1000) + tone(6500, amp=0.003)     # 高频低 40dB
    eq0b = SpectralEQ(SR, BLOCK); eq0b.deess = 0.0
    eq1b = SpectralEQ(SR, BLOCK); eq1b.deess = 1.0
    y0b = run(eq0b, x2)
    y1b = run(eq1b, x2)
    d_mid2 = peak_db(y1b, 1000) - peak_db(y0b, 1000)
    d_hi2 = peak_db(y1b, 6500) - peak_db(y0b, 6500)
    check("正常唱歌时不触发(中频)", abs(d_mid2) < 0.3, f"1kHz {d_mid2:+.1f} dB")
    check("正常唱歌时不触发(高频)", abs(d_hi2) < 1.0, f"6.5kHz {d_hi2:+.1f} dB")

    # ---- 3) 纯低频:更不该触发 ----
    x3 = tone(120)
    eq0c = SpectralEQ(SR, BLOCK); eq0c.deess = 0.0
    eq1c = SpectralEQ(SR, BLOCK); eq1c.deess = 1.0
    d_low = peak_db(run(eq1c, x3), 120) - peak_db(run(eq0c, x3), 120)
    check("纯低频不受影响", abs(d_low) < 0.1, f"120Hz {d_low:+.1f} dB")

    # ---- 4) 强度旋钮是单调的:0 < 50% < 100% ----
    x4 = tone(1000) + tone(6500)
    prev = 0.0
    mono = True
    for strength in (0.25, 0.5, 1.0):
        e = SpectralEQ(SR, BLOCK); e.deess = strength
        d = peak_db(run(e, x4), 6500) - peak_db(run(SpectralEQ(SR, BLOCK), x4), 6500)
        if d > prev + 0.5:      # 更大的强度反而压得更少
            mono = False
        prev = d
    check("强度旋钮单调(越拉越狠)", mono, f"最终 {prev:+.1f} dB")

    # ---- 5) 上线封顶:再狠也不超过 9dB,不然听起来像大舌头 ----
    eq5 = SpectralEQ(SR, BLOCK); eq5.deess = 1.0
    x5 = tone(6500, 0.5) + tone(1000, 0.01)     # 几乎纯齿音
    d5 = peak_db(run(eq5, x5), 6500) - peak_db(run(SpectralEQ(SR, BLOCK), x5), 6500)
    check("上限不超过 9dB", d5 > -10.0, f"{d5:+.1f} dB")

    # ---- 6) 链序:压缩必须在 EQ 之前(2026-10-19 照专业共识改的) ----
    vc = VoiceChain(SR, BLOCK)
    order = []
    orig_eq, orig_comp = vc.eq.process, vc.comp.process
    vc.comp.process = lambda x: (order.append('comp'), orig_comp(x))[1]
    vc.eq.process = lambda x: (order.append('eq'), orig_eq(x))[1]
    vc.process(np.zeros(BLOCK, dtype=np.float32))
    check("链序 = 压缩 → EQ(de-esser 在 EQ 里)", order == ['comp', 'eq'], str(order))

    # ---- 7) 关掉时真的是直通(不做任何乘) ----
    e7 = SpectralEQ(SR, BLOCK); e7.deess = 0.0
    y7 = run(e7, x)
    y7b = run(SpectralEQ(SR, BLOCK), x)
    check("关掉时和默认完全一致", np.allclose(y7, y7b), "")

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
