"""量具:开一个**共享**输出流,再去试一次独占(故意让它失败),
看第一个流会不会被弄死。

★ 症状:耳返突然没声、缓冲一路顶到 480ms、overruns 猛涨,而
  device_alive() 说设备好好的 —— 「只进不出」。
"""
import ctypes
import sys
import time

import numpy as np
import sounddevice as sd

ctypes.windll.ole32.CoInitializeEx(None, 2)


class Counter:
    def __init__(self):
        self.n = 0
        self.err = None

    def __call__(self, outdata, frames, t, status):
        self.n += 1
        outdata.fill(0.0)


def run(dev, label, block=480, rate=48000):
    c = Counter()
    s = sd.OutputStream(device=dev, samplerate=rate, channels=2,
                        dtype="float32", blocksize=block, latency="low",
                        callback=c)
    s.start()
    t0 = time.time()
    time.sleep(3.0)
    before = c.n / (time.time() - t0)
    print(f"[{label}] 独占尝试前: {before:6.1f} 回调/秒,stream.active={s.active}")

    # 跟 engine 里一模一样的动作:再开一个独占流
    try:
        e = sd.WasapiSettings(exclusive=True)
        s2 = sd.OutputStream(device=dev, samplerate=rate, channels=2,
                             dtype="float32", blocksize=block, latency="low",
                             callback=None, extra_settings=e)
        s2.start()
        s2.stop(); s2.close()
        print(f"[{label}] 独占**开成功了**(跟真机不一样,注意)")
    except Exception as ex:
        print(f"[{label}] 独占开不出来: {type(ex).__name__}: {str(ex)[:90]}")

    t1 = time.time()
    n0 = c.n
    time.sleep(4.0)
    after = (c.n - n0) / (time.time() - t1)
    print(f"[{label}] 独占尝试后: {after:6.1f} 回调/秒,stream.active={s.active}")
    try:
        s.stop(); s.close()
    except Exception:
        pass
    return before, after


def main():
    devs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if "wasapi" in sd.query_hostapis(d["hostapi"])["name"].lower()
            and d["max_output_channels"] > 0]
    for i, n in devs:
        print(f"  #{i} {n}")
    for i, n in devs:
        if "耳机" in n or "headphone" in n.lower() or "bluetooth" in n.lower():
            print(f"\n=== #{i} {n} ===")
            b, a = run(i, "蓝牙")
            print(f"→ {'★ 是它:独占尝试把共享流弄死了' if a < b * 0.2 else '共享流没事'}")


if __name__ == "__main__":
    sys.exit(main())
