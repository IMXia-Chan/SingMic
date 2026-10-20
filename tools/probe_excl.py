"""量具:独占模式到底开不开得出来、开出来省多少毫秒。

★ 只量不改:开一个空流(回调直接填零),跑 1 秒,把 stream.latency 打出来。
★ 不抢服务:服务那边 device_name 是空的才跑得动。
"""
import sys

import numpy as np
import sounddevice as sd


def com_init() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception:                       # noqa: BLE001
        pass


def find_wasapi_out() -> list[tuple[int, str]]:
    out = []
    try:
        api = [i for i, h in enumerate(sd.query_hostapis())
               if "wasapi" in h["name"].lower()]
    except Exception:                       # noqa: BLE001
        return out
    if not api:
        return out
    hi = api[0]
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == hi and d["max_output_channels"] > 0:
            out.append((i, d["name"]))
    return out


def try_open(dev: int, rate: int, block: int, exclusive: bool):
    extra = sd.WasapiSettings(exclusive=True) if exclusive else None
    # ★ 不给回调 = 阻塞模式。这里只问「开不开得出来、报多少延迟」,
    #   流不填数据只会 underrun,不影响流自己的 latency 读数。
    kw = dict(device=dev, samplerate=rate, channels=2, dtype="float32",
              blocksize=block, latency="low")
    s = None
    try:
        s = sd.OutputStream(**kw, **({"extra_settings": extra} if extra else {}))
        s.start()
        lat = float(s.latency) * 1000.0
        s.stop()
        s.close()
        return True, lat, None
    except Exception as e:                  # noqa: BLE001
        if s is not None:
            try:
                s.close()
            except Exception:               # noqa: BLE001
                pass
        return False, 0.0, str(e)[:160]


def main() -> None:
    com_init()
    devs = find_wasapi_out()
    if not devs:
        print("没有 WASAPI 输出设备")
        return
    default = sd.default.device[1]
    print(f"默认输出 #{default}")
    for i, name in devs:
        mark = " ←默认" if i == default else ""
        print(f"  #{i} {name}{mark}")

    targets = [(i, n) for i, n in devs if i == default] or devs[:1]
    for dev, name in targets:
        print(f"\n=== #{dev} {name} ===")
        for block in (240, 480):
            for excl in (True, False):
                ok, lat, err = try_open(dev, 48000, block, excl)
                tag = "独占" if excl else "共享"
                ms = 1000.0 * block / 48000.0
                if ok:
                    print(f"  {tag} 块{ms:.1f}ms → 开成,输出延迟 {lat:.1f}ms")
                else:
                    print(f"  {tag} 块{ms:.1f}ms → 开不出来: {err}")


if __name__ == "__main__":
    out = np.zeros((240, 2), dtype=np.float32)
    sys.exit(main())
