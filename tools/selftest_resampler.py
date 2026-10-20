"""重采样器验收 —— 这个类曾经把声音悄悄复制一倍,必须有测试盯着。

★ 事故记录(2026-10-18):
   旧实现每次调用后把「整块上一个输入」当历史留着,于是每来 960 个样本
   就吐回约 1920 个。抖动缓冲几秒内顶到上限,耳返延迟涨到 480ms。
   它不报错、不崩、声音听起来还「有声音」,所以只能靠数样本数抓。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_resampler.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_engine import Resampler     # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR = 48000
BLOCK = 960          # 手机实际发的大小(20ms)
RES = []


def check(name, ok, detail=""):
    RES.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def feed(rs: Resampler, src: np.ndarray) -> np.ndarray:
    out = []
    for i in range(0, len(src) - BLOCK + 1, BLOCK):
        out.append(rs.process(src[i:i + BLOCK]))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


print("\n[1] 同采样率、不校正 —— 必须一个样本不多一个不少")
src = (0.3 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR)).astype(np.float32)
rs = Resampler(SR, SR)
out = feed(rs, src)
check("样本数完全一致", len(out) == len(src), f"{len(out)} vs {len(src)}")
check("波形完全一致", np.max(np.abs(out - src)) < 1e-6,
      f"最大差 {float(np.max(np.abs(out - src))):.2e}")

print("\n[2] 时钟漂移校正 —— 多消耗 1.2%,样本数就必须少 1.2%")
rs = Resampler(SR, SR)
rs.d = 0.012
out = feed(rs, src)
want = len(src) / 1.012
check("输出样本数 ≈ 输入/1.012", abs(len(out) - want) < 3, f"{len(out)} vs 期望 {want:.0f}")
check("没有重复播放(不再翻倍)", len(out) < len(src) * 1.01, f"{len(out)}/{len(src)}")

print("\n[3] 44100 -> 48000")
rs = Resampler(44100, 48000)
src44 = (0.3 * np.sin(2 * np.pi * 440 * np.arange(44100) / 44100)).astype(np.float32)
out = feed(rs, src44)
# feed() 只喂整块(44100 不是 960 的整数倍),剩下的几个样本没进链,按实际喂进去的算
fed = (len(src44) // BLOCK) * BLOCK
want = fed * 48000 / 44100
check("输出样本数 ≈ 输入的 1.088 倍", abs(len(out) - want) < 5, f"{len(out)} vs 期望 {want:.0f}")

print("\n[4] 换率之后频率不能跑")
seg = out[4800:4800 + 16384]
spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
f = np.fft.rfftfreq(len(seg), 1 / 48000)
peak = float(f[int(np.argmax(spec))])
check("主频仍是 440Hz", abs(peak - 440) < 6, f"实测 {peak:.1f} Hz")

print("\n[5] 漂移校正不能把声音弄断(逐块边界跳变要小)")
rs = Resampler(SR, SR)
rs.d = 0.005
out = feed(rs, src)
# 正常 440Hz 正弦相邻样本最大差 ≈ 2π*440/48000*0.3 ≈ 0.017;
# 有接缝的话会出现远大于它的台阶。
jump = float(np.max(np.abs(np.diff(out[:SR - 2000]))))
check("相邻样本最大跳变 < 0.05", jump < 0.05, f"最大跳变 {jump:.4f}")

ok = sum(1 for _, o, _ in RES if o)
print(f"\n{'=' * 48}\n  通过 {ok}/{len(RES)}\n{'=' * 48}")
sys.exit(0 if ok == len(RES) else 1)
