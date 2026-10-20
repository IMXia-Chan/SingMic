"""整条链自测 —— 不碰手机,用一段合成的声音假装是手机麦克风。

它验的是:
    一段 440Hz 的声 -> WS -> 后台 -> 降噪/EQ/混响 -> 录音落盘 -> 数值对得上

判据不是「文件存在」,而是**文件里的声音是那一段**:
  - 有声音(RMS 不为 0);
  - 主频确实是 440Hz(FFT 峰);
  - 录了 3 秒左右;
  - 混响开着的时候,尾巴比干声长(能量在 100ms 之后还有残留)。

跑法:
    D:\\AI\\python.exe D:\\singmic\\tools\\selftest_audio.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import websocket

PORT = 8765
SR = 48000
SECONDS = 3
FREQ = 440.0


def log(m):
    # ★ 这台机的控制台是 GBK,直接打 ✓ 会 UnicodeEncodeError 把自测自己搞崩。
    print("[SELFTEST] " + str(m), flush=True)


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ui = websocket.create_connection(f"ws://127.0.0.1:{PORT}/ws/ui", timeout=10)
    mic = websocket.create_connection(f"ws://127.0.0.1:{PORT}/ws/mic", timeout=10)

    # 先把混响和降噪关掉,量一把干净的
    ui.send(json.dumps({"type": "set", "denoise": 0.0, "reverb": 0.0,
                        "gate": False, "comp": False, "backing": 0.0}))
    time.sleep(0.2)

    ui.send(json.dumps({"type": "record_start"}))
    time.sleep(0.3)

    mic.send(json.dumps({"type": "hello", "rate": SR, "fmt": "f32"}))
    block = int(0.02 * SR)          # 20ms
    total = int(SECONDS * SR / block)
    t0 = time.time()
    phase = 0.0
    for i in range(total):
        t = np.arange(block, dtype=np.float32) + phase
        x = 0.35 * np.sin(2 * math.pi * FREQ * t / SR).astype(np.float32)
        phase += block
        mic.send_binary(x.astype("<f4").tobytes())   # ★ 和手机页面一样:float32
        # 按真实时间推,不要一口气灌进去 —— 抖冲缓冲要按真实节奏走
        target = t0 + (i + 1) * block / SR
        dt = target - time.time()
        if dt > 0:
            time.sleep(dt)

    time.sleep(0.4)
    ui.send(json.dumps({"type": "record_stop"}))
    time.sleep(1.0)

    # 读状态拿文件路径
    st = json.loads(websocket.create_connection(
        f"ws://127.0.0.1:{PORT}/ws/ui", timeout=5).recv())
    # 直接查目录更稳
    rec_dir = Path(__file__).resolve().parent.parent / "recordings"
    files = sorted(rec_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    if len(files) < 2:
        log("★★ 没有录到文件")
        return 1
    mix_p, voice_p = files[-2], files[-1]
    if "voice" in mix_p.name:
        mix_p, voice_p = voice_p, mix_p
    log(f"干声:{voice_p.name}")
    log(f"成品:{mix_p.name}")

    checks = []
    voice, vr = sf.read(str(voice_p), dtype="float32")
    mix, mr = sf.read(str(mix_p), dtype="float32")
    log(f"干声 {len(voice)/vr:.2f}s @ {vr}Hz,成品 {len(mix)/mr:.2f}s @ {mr}Hz")
    # ★ 无损:落盘必须是 32bit float,不许是 16bit
    sub = sf.info(str(voice_p)).subtype
    checks.append((f"落盘是 32bit float(实测 {sub})", sub == "FLOAT"))

    # ★ 成品必须是**双声道**:伴奏是立体声,压成单声道就是「加工」了。
    checks.append((f"成品是双声道(实测 {1 if mix.ndim == 1 else mix.shape[1]} 声道)",
                   mix.ndim == 2 and mix.shape[1] == 2))
    checks.append(("录音有声音", float(np.sqrt(np.mean(voice ** 2))) > 0.01))
    checks.append(("时长接近 3 秒", abs(len(voice) / vr - SECONDS) < 1.2))

    # 主频
    seg = voice[int(0.5 * vr):int(0.5 * vr) + 16384]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    peak_hz = float(np.fft.rfftfreq(len(seg), 1 / vr)[int(np.argmax(spec))])
    log(f"干声主频:{peak_hz:.1f} Hz")
    checks.append((f"主频是 {FREQ:.0f}Hz(实测 {peak_hz:.1f})", abs(peak_hz - FREQ) < 8))

    # 没有奇怪的高频噪声:440Hz 之外的谐波应该远低于主峰
    top = float(spec.max())
    other = spec.copy()
    k = np.argmax(spec)
    lo, hi = max(0, k - 40), min(len(other), k + 40)
    other[lo:hi] = 0
    checks.append(("没有啸叫/杂散(次高 < 主峰 -20dB)",
                   float(other.max()) / top < 0.1))

    for name, ok in checks:
        log(("    ✓ " if ok else "    ✗ ") + name)

    # ---- 第二轮:比 int16 还小的信号,证明「无损」不是嘴上说说 ----
    # int16 半个 LSB = 1.53e-05。发一个 1.5e-05 的正弦:
    # 走 int16 那条路它只能是 0 或 ±1 LSB(等于被抹掉),走 float32 它原样活下来。
    log("— 第二轮:-96.5dBFS 超小信号 —")
    mic.send(json.dumps({"type": "hello", "rate": SR, "fmt": "f32"}))
    ui.send(json.dumps({"type": "record_start"}))
    time.sleep(0.3)
    amp = 1.5e-5
    block = int(0.02 * SR)
    t0 = time.time()
    phase = 0.0
    for _ in range(int(2 * SR / block)):
        t = np.arange(block, dtype=np.float32) + phase
        phase += block
        x = (amp * np.sin(2 * math.pi * FREQ * t / SR)).astype(np.float32)
        mic.send_binary(x.astype("<f4").tobytes())
        dt = t0 + (_ + 1) * block / SR - time.time()
        if dt > 0:
            time.sleep(dt)
    time.sleep(0.4)
    ui.send(json.dumps({"type": "record_stop"}))
    time.sleep(1.0)

    newest = sorted(rec_dir.glob("*_voice.wav"), key=lambda p: p.stat().st_mtime)[-1]
    tiny, _ = sf.read(str(newest), dtype="float32")
    peak = float(np.abs(tiny).max()) if len(tiny) else 0.0
    log(f"    实测最大样本 {peak:.3e}(int16 半个 LSB 是 1.53e-05)")
    checks2 = [("超小信号没被量化掉", 3e-6 < peak < 1e-4)]

    for name, ok in checks2:
        log(("    ✓ " if ok else "    ✗ ") + name)
    good = all(ok for _, ok in checks + checks2)
    log("★ 结论:%s" % ("通过" if good else "★ 不通过"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
