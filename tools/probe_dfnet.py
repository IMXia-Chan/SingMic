"""拿「真语音」和「像唱歌的合成声」去量 DeepFilterNet3 —— 上一版用纯谐波当人声是错的。

上一版的教训(写死在这里,免得再犯):
    合成的纯谐波(只有 f0 和泛音、没有共振峰、没有字与字之间的过渡)
    被 DFN3 当成噪声整个吃掉:-48 dB。那个数**不能**说明它吃不吃唱的。

这一版用三样东西:
    [A] Windows 自带的真人声(SAPI 合成,有共振峰和音节过渡)
    [B] 共振峰合成的「唱」:声门脉冲 + 元音共振峰 + 换音高 + 颤音 + 气声
    [C] 上面两样各掺一份室内噪声(底噪 + 键盘 + 旁人说话)

每个都跑三档「最多能压多少」(atten_lim_db):
    None = 不设上限(模型想压多少压多少)
    20 / 12 / 6 dB = 最多就压这么多 —— 用来找「既不把人声压没、又能压住键盘」的点

判据:(写在这里,不事后改)
    ★ 人声自己掉超过 3 dB ⇒ 这一档不能用(耳朵已经能听出「人没了」)
    ★ 噪声只掉不到 6 dB ⇒ 这一档没意义
    ★ 延时用互相关量,不信文档

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\probe_dfnet.py

★ 真人声那两段是 Windows 的 SAPI 现合成的,第一次跑会自动生成到
  `%TEMP%\\singmic_probe\\`(中文 Huihui / 英文 Zira)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import lfilter, resample_poly

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "_ai_try"))
os.environ["DEEPFILTER_STREAM_MODEL_DIR"] = str(ROOT / "_ai_try" / "model")

from deepfilter_stream.model import DeepFilterModel   # noqa: E402

SR = 48000
PROBE = Path(os.environ["TEMP"]) / "singmic_probe"


def ensure_speech() -> None:
    """真人声样本不在就现做一份(Windows SAPI;中文 Huihui / 英文 Zira)。"""
    PROBE.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("speech_zh.wav", "Microsoft Huihui Desktop",
         "今天下午我们在房间里唱歌,外面下着小雨。我把窗帘拉开,看见楼下的树在风里慢慢摇。"
         "我觉得这首歌的高音有点难,但是我还是想把它唱完,因为我很喜欢这首歌。"),
        ("speech_en.wav", "Microsoft Zira Desktop",
         "The quick brown fox jumps over the lazy dog, and then it runs away "
         "into the quiet forest where nobody can hear it sing."),
    ]
    for name, voice, text in jobs:
        p = PROBE / name
        if p.exists() and p.stat().st_size > 1000:
            continue
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SelectVoice('{voice}'); "
              f"$s.SetOutputToWaveFile('{p}'); "
              f"$s.Speak('{text}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print(f"  现做了一份真人声:{p.name}")


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def db(v):
    return 20.0 * np.log10(max(float(v), 1e-12))


def read_wav(p: Path) -> tuple[np.ndarray, int]:
    w = wave.open(str(p), "rb")
    sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
    raw = w.readframes(w.getnframes())
    w.close()
    if sw == 2:
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
    else:
        raise SystemExit(f"不认识的位宽 {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), sr


def to48(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return x
    from fractions import Fraction
    r = Fraction(SR, sr)
    return resample_poly(x, r.numerator, r.denominator).astype(np.float32)


def run_stream(model, x, atten_lim_db=None, frame=512):
    """按 512 帧推,前面补一帧、后面补一帧,输出与输入等长(补的先导帧抵掉算法延时)。"""
    st = model.new_stream(atten_lim_db=atten_lim_db)
    pad = np.zeros(frame, dtype=np.float32)
    out = [pad]
    xx = np.concatenate([x.astype(np.float32), np.zeros(frame, np.float32)])
    for i in range(0, len(xx) - frame + 1, frame):
        out.append(np.asarray(st.process_frame(xx[i:i + frame]), np.float32))
    y = np.concatenate(out)
    return y[: len(x)]


def measure_delay_of(model, x, atten_lim_db=None):
    """互相关量「补的先导帧之外」还剩多少延时。"""
    y = run_stream(model, x, atten_lim_db)
    n = min(len(x), len(y)) - 4800
    a = x[4800:4800 + n]
    b = y[4800:4800 + n]
    best, lag = -1e9, 0
    for d in range(-2048, 2049, 8):
        if d >= 0:
            u, v = a[: n - d], b[d:]
        else:
            u, v = a[-d:], b[: n + d]
        c = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-20))
        if c > best:
            best, lag = c, d
    return lag, best


def formant_sing(n, f0_notes, vowel_seq, breath=0.08, rs=None):
    """像唱歌的合成声:声门脉冲串 + 元音共振峰 + 换音 + 颤音 + 一点气声。

    ★ 这不是真唱 —— 它**有共振峰和换音**,这两样正是上一版纯谐波缺的。
    """
    rs = rs or np.random.RandomState(7)
    t = np.arange(n) / SR
    # 音高轨迹:每个音持续 ~0.6s,音符之间滑 60ms
    seg = 0.6
    idx = np.minimum((t / seg).astype(int), len(f0_notes) - 1)
    f0 = np.array(f0_notes, np.float64)[idx]
    nxt = np.array(f0_notes, np.float64)[np.minimum(idx + 1, len(f0_notes) - 1)]
    frac = (t % seg) / seg
    glide = np.clip((frac - 1.0 + 0.1) / 0.1, 0, 1)
    f0 = f0 * (1 - glide) + nxt * glide
    f0 *= 1.0 + 0.012 * np.sin(2 * np.pi * 5.2 * t)      # 颤音
    # 声门脉冲:每个周期一个衰减脉冲
    phase = np.cumsum(f0) / SR
    src = np.diff(np.floor(phase), prepend=0.0)
    src = lfilter([1.0], [1.0, -0.97], src)               # 轻微低通,像声带
    src = src + breath * rs.randn(n)                      # 气声
    # 元音:三对共振峰,按时间平滑切换
    V = {"a": [(800, 80), (1200, 90), (2500, 120)],
         "i": [(300, 60), (2200, 100), (3000, 120)],
         "u": [(350, 60), (800, 80), (2300, 120)]}
    y = np.zeros(n, np.float32)
    seq = [V[k] for k in vowel_seq]
    pos = np.linspace(0, len(seq) - 1, n)
    for j in range(3):
        fc = np.array([v[j][0] for v in seq], np.float64)
        bw = np.array([v[j][1] for v in seq], np.float64)
        F = np.interp(pos, np.arange(len(seq)), fc)
        B = np.interp(pos, np.arange(len(seq)), bw)
        # 慢变的共振峰:分段常值 + 用 lfilter 没法做,这里用慢速重采样近似
        step = 512
        out = np.zeros(n, np.float32)
        zi = None
        for i in range(0, n - step + 1, step):
            f, b = F[i], B[i]
            r = np.exp(-np.pi * b / SR)
            th = 2 * np.pi * f / SR
            a = [1.0, -2 * r * np.cos(th), r * r]
            if zi is None:
                zi = np.zeros(2)
            out[i:i + step], zi = lfilter([1 - r], a, src[i:i + step], zi=zi)
        y += out * (1.0 / (j + 1))
    y *= 0.2 / (np.abs(y).max() + 1e-9)
    return y.astype(np.float32)


def bed_noise(n, rs=None):
    """室内底噪 + 键盘 + 旁人说话。"""
    rs = rs or np.random.RandomState(11)
    t = np.arange(n) / SR
    hiss = 0.004 * rs.randn(n)
    hum = 0.002 * np.sin(2 * np.pi * 50 * t) + 0.001 * np.sin(2 * np.pi * 100 * t)
    keys = np.zeros(n, np.float32)
    i = int(0.05 * SR)
    while i < n:
        L = int(0.010 * SR)
        tt = np.arange(L) / SR
        burst = 0.18 * np.exp(-tt / 0.002) * rs.randn(L)
        keys[i:i + L] += burst[: max(0, min(L, n - i))]
        i += int(0.16 * SR * (0.6 + 0.8 * rs.rand()))
    bab = 0.035 * rs.randn(n)
    env = (0.5 + 0.5 * np.sin(2 * np.pi * 3.7 * t + 0.3)) ** 2
    bab *= env
    return (hiss + hum + keys + bab).astype(np.float32)


def main() -> int:
    ensure_speech()
    sp_zh, sr1 = read_wav(PROBE / "speech_zh.wav")
    sp_en, sr2 = read_wav(PROBE / "speech_en.wav")
    sp_zh = to48(sp_zh, sr1)
    sp_en = to48(sp_en, sr2)
    print(f"真语音(中文 SAPI) {len(sp_zh)/SR:.1f}s / (英文) {len(sp_en)/SR:.1f}s")

    n = min(len(sp_zh), int(7.5 * SR))
    sing = formant_sing(int(7.5 * SR), [196, 220, 262, 330, 294, 262, 196, 165],
                        ["a", "a", "i", "u", "a", "u", "i", "a"])
    print(f"合成「唱」 {len(sing)/SR:.1f}s(共振峰 + 换音 + 颤音)")

    print("\n加载模型…")
    model = DeepFilterModel()

    print("\n[A] 真实延时(互相关,不信文档)")
    print("    ★ 量到的 d 已经含了「补进去的先导帧」;减去它才是模型自身。")
    for lim in (None, 12.0):
        d, c = measure_delay_of(model, sp_zh[: SR * 3], lim)
        print(f"  {'无上限' if lim is None else f'{lim:g}dB':>6s}: "
              f"模型自身 {d - 512} 样本 = {(d - 512) / SR * 1000:.2f} ms;"
              f"按 512 块对齐后共 {d} 样本 = {d / SR * 1000:.2f} ms(相关 {c:.3f})")

    cases = {
        "中文语音": sp_zh[:n],
        "英文语音": sp_en[: min(len(sp_en), n)],
        "合成「唱」": sing,
    }
    lims = [None, 20.0, 12.0, 6.0]

    print("\n[B] 只有人声:自己掉多少(>3dB 就不能用)")
    print(f"  {'信号':12s} " + "".join(f"{'无上限' if l is None else f'{l:g}dB':>10s}" for l in lims))
    for name, x in cases.items():
        row = []
        for lim in lims:
            y = run_stream(model, x, lim)
            row.append(db(rms(x)) - db(rms(y)))
        print(f"  {name:12s} " + "".join(f"{v:10.2f}" for v in row))

    print("\n[C] 人声 + 室内噪声(信噪比 10dB,人声按正常麦电平归一)")
    print("    ★ 电平很重要:人声太轻时(信噪比 0dB)模型会连人带噪声一起压 ——")
    print("      那是量具的错,不是模型的本事。这里按 0.08 RMS 归一,再按 10dB SNR 配噪声。")
    print("    (模型是非线性的,没法真分轨;「人还在」= 混音湿声和『人声单独湿声』在说话帧里差多少)")
    fl = 960
    for name, x in [("中文语音", sp_zh[:n]), ("合成「唱」", sing)]:
        x = x * (0.08 / (rms(x) + 1e-12))
        noise = bed_noise(len(x))
        nz = noise * (rms(x) / (rms(noise) * 10 ** (10.0 / 20)))
        mix = x + nz
        e = np.array([rms(x[i:i + fl]) for i in range(0, len(x) - fl + 1, fl)])
        speech = e > 0.2 * e.max()
        print(f"  -- {name}(说话帧占 {speech.mean()*100:.0f}%)--")
        for lim in lims:
            v_wet = run_stream(model, x, lim)
            m_wet = run_stream(model, mix, lim)
            keep = np.array([db(rms(m_wet[i * fl:(i + 1) * fl])) - db(rms(v_wet[i * fl:(i + 1) * fl]))
                             for i, s in enumerate(speech) if s])
            rest = np.array([db(rms(m_wet[i * fl:(i + 1) * fl]))
                             for i, s in enumerate(speech) if not s])
            dry_rest = np.array([db(rms(mix[i * fl:(i + 1) * fl]))
                                 for i, s in enumerate(speech) if not s])
            print(f"     {'无上限' if lim is None else f'{lim:g}dB':>8s}: "
                  f"人中位 {np.median(keep):+5.2f} dB   "
                  f"停顿里残噪比干声低 {np.median(dry_rest) - np.median(rest):5.1f} dB")

    print("\n[D] 最坏情况:一个长音(唱到一半拖住),模型会不会把它当噪声掐掉")
    long_tone = formant_sing(int(4 * SR), [262] * 8, ["a"] * 8, breath=0.05)
    for lim in lims:
        y = run_stream(model, long_tone, lim)
        a, b = SR, int(3.5 * SR)
        print(f"  长音 {'无上限' if lim is None else f'{lim:g}dB':>8s}: 降 {db(rms(long_tone[a:b])) - db(rms(y[a:b])):6.2f} dB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
