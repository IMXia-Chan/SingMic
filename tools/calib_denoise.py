"""量降噪的两件事:噪声估计偏了多少、旋钮各档到底压多少 dB。

★ 这个脚本存在的唯一理由:MS_BIAS 这个常数不能拍脑袋。
  它是**统计**偏差(最小值天然比均值低),跟噪声是白噪、空调声还是人声无关,
  所以可以一次量准、写死、以后再验。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\calib_denoise.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dsp import VoiceChain          # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR, BLOCK = 48000, 480


def rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def db(v: float) -> float:
    return 20.0 * np.log10(max(float(v), 1e-12))


def run(chain: VoiceChain, x: np.ndarray) -> np.ndarray:
    data = np.concatenate((x, np.zeros(BLOCK * 8, np.float32)))
    out = [chain.process(data[i:i + BLOCK]) for i in range(0, len(data) - BLOCK + 1, BLOCK)]
    y = np.concatenate(out)
    return np.roll(y, -BLOCK)[:len(x)]


def chain(denoise: float) -> VoiceChain:
    c = VoiceChain(SR, BLOCK)
    c.set(gain=1.0, denoise=denoise, gate=False, hp_hz=20.0,
          eq_low=0.0, eq_mid=0.0, eq_high=0.0, comp=False, makeup=0.0)
    return c


def main() -> None:
    rs = np.random.RandomState(7)

    # ---- 1. 噪声估计偏差:喂纯白噪,看 chain.eq.noise 和真实功率谱差多少 ----
    white = (rs.randn(SR * 4) * 0.01).astype(np.float32)
    c = chain(0.7)
    run(c, white)
    est = c.eq.noise
    # 真实值:同样窗长/重叠下,白噪功率谱的期望(用同一套 STFT 量,别用解析式)
    win = c.eq.win
    p = []
    prev = np.zeros(BLOCK, np.float32)
    for i in range(0, len(white) - BLOCK, BLOCK):
        x = white[i:i + BLOCK]
        X = np.fft.rfft(np.concatenate((prev, x)) * win)
        prev = x
        p.append(np.abs(X) ** 2)
    truth = np.mean(np.stack(p[len(p) // 2:]), axis=0)       # 后半段(跟踪器已稳)
    bias_db = 10.0 * np.log10(np.mean(truth) / np.mean(est))
    print(f"噪声估计比真实低 {bias_db:+.2f} dB   ⇒ MS_BIAS 应设为 {10**(bias_db/10):.2f}")
    print(f"  (逐 bin 1σ 波动 {np.std(10*np.log10(est/truth)):.2f} dB)")

    # ---- 2. 各档压多少:纯噪声段 vs 人声段(440Hz 音,近似「人在唱」) ----
    print("\n 旋钮   底噪降    人声(响)  人声(中)  人声(轻)")
    noise = (rs.randn(SR * 3, ) * 0.01).astype(np.float32)
    t = np.arange(SR) / SR

    def tone_at(a):
        return (a * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    # 先 2 秒纯噪声让估计器把底噪学明白,再叠人声
    sig = np.concatenate((noise, tone_at(0.3) + noise[:SR]))
    dry = run(chain(0.0), sig)

    sigs = {a: np.concatenate((noise, tone_at(a) + noise[:SR])) for a in (0.3, 0.03, 0.005)}
    drys = {a: run(chain(0.0), s) for a, s in sigs.items()}
    for s in (0.3, 0.5, 0.7, 0.85, 1.0):
        wets = {a: run(chain(s), g) for a, g in sigs.items()}
        nb = db(rms(drys[0.3][SR:2 * SR])) - db(rms(wets[0.3][SR:2 * SR]))
        a, b = int(3.2 * SR), int(3.9 * SR)
        ks = [db(rms(wets[v][a:b])) - db(rms(drys[v][a:b])) for v in (0.3, 0.03, 0.005)]
        print(f"  {s:.2f}   {nb:6.1f} dB  {ks[0]:+7.2f}  {ks[1]:+7.2f}  {ks[2]:+7.2f}")

    # ---- 3. 「水声」探针:纯噪声下,输出短时电平的抖动 ----
    print("\n 水声(纯噪声时 20ms 电平的 1σ,越小越像噪声、越大越像抽气)")
    for s in (0.0, 0.5, 0.7, 1.0):
        y = run(chain(s), noise)
        seg = y[SR:]
        n = 960
        r = np.array([rms(seg[i:i + n]) for i in range(0, len(seg) - n, n)])
        rel = 20 * np.log10(r / r.mean() + 1e-12)
        print(f"  旋钮 {s:.1f}: {np.std(rel):5.2f} dB")


if __name__ == "__main__":
    main()
