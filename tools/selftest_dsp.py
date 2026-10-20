"""调音链逐项验收 —— 每个旋钮都要能单独量出效果。

★ 为什么要有这个文件:
   界面上的滑块「动了」不等于「声音变了」。这里每一项都拿数值说话:
   EQ +6dB 就必须真的涨 ~6dB,噪声门关了就必须真的静下来,
   全关的时候必须**原样还回来**(重建误差 <-60dB)。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_dsp.py
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

SR = 48000
BLOCK = 480
RESULTS: list[tuple[str, bool, str]] = []


def log(m=""):
    print(str(m), flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    log(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""))


def run(chain: VoiceChain, x: np.ndarray) -> np.ndarray:
    """按 10ms 一块推完整条链。返回和 x 等长的输出(丢掉首块的延迟)。"""
    n = len(x)
    pad = np.zeros(BLOCK * 8, dtype=np.float32)
    data = np.concatenate((x, pad))
    out = []
    for i in range(0, len(data) - BLOCK + 1, BLOCK):
        out.append(chain.process(data[i:i + BLOCK]))
    y = np.concatenate(out)
    # ★ 频谱那层有一块(10ms)延迟。输出比输入晚,所以往左挪一格 ——
    #   符号搞反的话「重建误差」会量出 -1dB,看着像链子坏了,其实是量具坏了。
    y = np.roll(y, -BLOCK)
    y = y[:n]
    # ★ 头 40ms 是 STFT 从「静音」突然起播的振铃,不是链子算错。
    #   真实使用里手机第一块也是从静音开始的,一样会有这 10ms;
    #   量重建精度时必须把它排除,否则量的是启动瞬态。
    y[:4 * BLOCK] = 0
    return y


def db(v: float) -> float:
    return 20.0 * np.log10(max(float(v), 1e-12))


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def tone(freq, seconds, amp=0.3, sr=SR):
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def band_db(x, freq, sr=SR):
    """某个频率附近的能量(dB)。用整段 FFT,取 ±20Hz。"""
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    m = np.abs(f - freq) < 20
    return db(np.sqrt(np.mean(spec[m] ** 2)))


def fresh(**kw) -> VoiceChain:
    c = VoiceChain(SR, BLOCK)
    c.set(gain=1.0, denoise=0.0, gate=False, gate_db=-55.0,
          eq_low=0.0, eq_mid=0.0, eq_high=0.0,
          comp=False, comp_ratio=3.0, comp_threshold=-22.0, makeup=0.0)
    if kw:
        c.set(**kw)
    return c


# ---------------------------------------------------------------------------
log("\n[1] 全关时必须是「原样还回来」")
sig = tone(440, 1.0, 0.3)
# ★ 高通拐点也要挪开,否则量到的是「高通按设计减了 0.1%」,不是重建误差
y = run(fresh(hp_hz=20.0), sig)
s = 4 * BLOCK
err = rms(y[s:] - sig[s:])
check("重建误差 < -60dB", db(err) - db(rms(sig)) < -60,
      f"信噪 {-(db(err) - db(rms(sig))):.1f} dB")

# ---------------------------------------------------------------------------
log("\n[2] 高通:80Hz 以下要真的削掉(拐点默认 80Hz)")
S4 = 4 * BLOCK
lo = band_db(run(fresh(), tone(40, 1.0, 0.3))[S4:SR], 40)
mid = band_db(run(fresh(), tone(440, 1.0, 0.3))[S4:SR], 440)
check("40Hz 比 440Hz 低 15dB 以上", mid - lo > 15, f"差 {mid - lo:.1f} dB")
check("440Hz 本身几乎不动", abs(mid - band_db(tone(440, 1.0, 0.3)[S4:SR], 440)) < 1.0,
      f"{mid - band_db(tone(440, 1.0, 0.3)[S4:SR], 440):+.1f} dB")
g = band_db(run(fresh(hp_hz=200.0), tone(40, 1.0, 0.3))[S4:SR], 40) - lo
check("hp_hz 可调(调到 200Hz,40Hz 再降 >10dB)", g < -10, f"再降 {g:.1f} dB")

# ---------------------------------------------------------------------------
log("\n[3] 三段 EQ:标称多少,就得涨多少")
def eq_gain(freq, setting):
    dry = band_db(run(fresh(), tone(freq, 1.0, 0.2))[:SR], freq)
    wet = band_db(run(fresh(**setting), tone(freq, 1.0, 0.2))[:SR], freq)
    return wet - dry

g = eq_gain(80, {"eq_low": 6.0})
check("低音 +6dB @80Hz", 4.5 < g < 7.5, f"实测 {g:+.1f} dB")
g = eq_gain(1500, {"eq_mid": 6.0})
check("中音 +6dB @1.5kHz", 4.5 < g < 7.5, f"实测 {g:+.1f} dB")
g = eq_gain(10000, {"eq_high": 6.0})
check("高音 +6dB @10kHz", 4.5 < g < 7.5, f"实测 {g:+.1f} dB")
g = eq_gain(440, {"eq_low": 6.0})
check("低音不动中频(440Hz 变化 <1.5dB)", abs(g) < 1.5, f"实测 {g:+.1f} dB")

# ---------------------------------------------------------------------------
log("\n[4] 噪声门:说完话必须真的静下来")
c = fresh(gate=True, gate_db=-45.0)
speech = tone(440, 1.0, 0.3)
silence = np.zeros(int(1.5 * SR), dtype=np.float32)
yy = run(c, np.concatenate((speech, silence)))
tail = yy[int(1.8 * SR):int(2.4 * SR)]
check("静音段衰减 > 40dB", db(rms(tail)) < -60, f"尾音 {db(rms(tail)):.1f} dBFS")
check("说话段没被误关", db(rms(yy[int(0.4 * SR):int(0.9 * SR)])) > -20,
      f"{db(rms(yy[int(0.4*SR):int(0.9*SR)])):.1f} dBFS")

# ---------------------------------------------------------------------------
log("\n[5] 降噪:底噪要真的下去,人声要留住")
rs = np.random.RandomState(3)
noise = (rs.randn(2 * SR) * 0.01).astype(np.float32)      # 大约 -40dB
mixsig = np.concatenate((noise, tone(440, 1.0, 0.3) + noise[:SR]))
dry = run(fresh(), mixsig)
wet = run(fresh(denoise=0.7), mixsig)
# ★ 量「稳下来以后」的底噪:前 0.5s 是跟踪器在学底噪,拿它算成绩是自欺。
noise_before = db(rms(dry[int(1.0 * SR):int(1.9 * SR)]))
noise_after = db(rms(wet[int(1.5 * SR):int(1.9 * SR)]))
voice_keep = db(band_db(wet[int(2.2 * SR):int(2.9 * SR)], 440)) - \
             db(band_db(dry[int(2.2 * SR):int(2.9 * SR)], 440))
check("底噪降 > 6dB", noise_before - noise_after > 6,
      f"{noise_before:.1f} → {noise_after:.1f} dB")
check("人声损失 < 3dB", voice_keep > -3.0, f"{voice_keep:+.1f} dB")

# ★ 深度降噪(需求里点名要的):旋钮拉满时必须压得下去,而且别把人声一起削了
c1 = fresh(denoise=1.0)
dry1 = run(fresh(), mixsig)
wet1 = run(c1, mixsig)
deep = db(rms(dry1[int(1.0 * SR):int(1.9 * SR)])) - db(rms(wet1[int(1.5 * SR):int(1.9 * SR)]))
keep1 = db(band_db(wet1[int(2.2 * SR):int(2.9 * SR)], 440)) - \
        db(band_db(dry1[int(2.2 * SR):int(2.9 * SR)], 440))
check("深度降噪(100%)底噪降 > 25dB", deep > 25, f"降 {deep:.1f} dB")
check("深度降噪时人声损失 < 6dB", keep1 > -6.0, f"{keep1:+.1f} dB")

# ★ 噪声估计本身准不准 —— 这是整个降噪的地基。偏了的话旋钮只是个安慰剂
#   (历史上这里偏过 20.7dB,旋钮拉满也几乎不动,还没人发现)。
rs2 = np.random.RandomState(11)
c2 = fresh(denoise=0.7)
run(c2, (rs2.randn(SR * 4) * 0.01).astype(np.float32))
est = c2.eq.noise
# 用同一套 STFT 量真实功率谱,别拿解析式子比(窗和重叠本身就有增益)
win = c2.eq.win
pp, prev = [], np.zeros(BLOCK, np.float32)
wn = (rs2.randn(SR * 4) * 0.01).astype(np.float32)
for i in range(0, len(wn) - BLOCK, BLOCK):
    xx = wn[i:i + BLOCK]
    X = np.fft.rfft(np.concatenate((prev, xx)) * win)
    prev = xx
    pp.append(np.abs(X) ** 2)
truth = np.mean(np.stack(pp[len(pp) // 2:]), axis=0)
bias = 10.0 * np.log10(np.mean(truth) / np.mean(est))
check("噪声估计与真实功率差 < 1.5dB", abs(bias) < 1.5, f"{bias:+.2f} dB")

# ---------------------------------------------------------------------------
log("\n[6] 压缩:响的往回收,轻的别动")
def crest(ratio):
    # 前 0.5s 大声,后 0.5s 小声
    big = tone(440, 0.5, 0.6)
    small = tone(440, 0.5, 0.06)
    out = run(fresh(comp=True, comp_ratio=ratio, comp_threshold=-22.0, makeup=0.0),
              np.concatenate((big, small)))
    b = rms(out[int(0.25 * SR):int(0.45 * SR)])
    s = rms(out[int(0.75 * SR):int(0.95 * SR)])
    return db(b) - db(s)

c1, c10 = crest(1.0), crest(10.0)
check("压缩比 10:1 比 1:1 把落差压小", c10 < c1 - 3,
      f"1:1 落差 {c1:.1f}dB → 10:1 落差 {c10:.1f}dB")

# ---------------------------------------------------------------------------
log("\n[7] 输出不许爆")
loud = tone(440, 1.0, 4.0)          # 远超 0dBFS
yy = run(fresh(gain=4.0), loud)
check("4 倍增益后峰值仍在 ±1.2 内", float(np.max(np.abs(yy))) < 1.2,
      f"峰值 {float(np.max(np.abs(yy))):.3f}")

# ---------------------------------------------------------------------------
ok = sum(1 for _, o, _ in RESULTS if o)
log(f"\n{'='*52}")
log(f"  通过 {ok}/{len(RESULTS)}")
for name, o, detail in RESULTS:
    if not o:
        log(f"  ✗ {name} {detail}")
log(f"{'='*52}")
sys.exit(0 if ok == len(RESULTS) else 1)
