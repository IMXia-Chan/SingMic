"""自查:在**别的线程**里开耳返设备。

★ 这条专门盯一个坑:PortAudio 的 WASAPI 后端靠 COM,而 COM 是 per-thread 的。
  主线程在 import sounddevice 时已经初始化过,所以「在 REPL 里手开一次」永远好使;
  server.py 把开设备扔进 asyncio 的线程池,那个线程没有 COM ——
  stream.start() 就抛 PaErrorCode -9999 / WdmSyncIoctl,一个 COM 字都不提。
  不在这条测试里复现一次,下次谁把这个修复删了都没人知道。

用法(要真的占用一下输出设备,不用手机):
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe tools\\selftest_output.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_engine import Engine, list_output_devices     # noqa: E402

checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok))
    print(f"  {'✓' if ok else '✗'} {name}{('  ' + detail) if detail else ''}", flush=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

    devs = list_output_devices()
    check("能列出输出设备", len(devs) > 0, f"{len(devs)} 台")
    check("WASAPI 层没有重复设备",
          len({n.lower() for _, n, _ in devs}) == len(devs))
    if not devs:
        return 1

    eng = Engine()
    err: list[BaseException] = []

    def worker() -> None:
        # ★ 这就是 server.py 里 run_in_executor 的那个线程 —— 没有 COM
        for _ in range(2):
            try:
                t = time.time()
                name = eng.open_output(None)
                print(f"    子线程开流:{name}  {time.time() - t:.3f}s", flush=True)
            except BaseException as e:                   # noqa: BLE001
                err.append(e)
            time.sleep(0.2)

    th = threading.Thread(target=worker, name="like-executor")
    th.start()
    th.join(timeout=30)
    check("子线程里开耳返设备(连开两次)", not err,
          "" if not err else f"{type(err[0]).__name__}: {err[0]}")

    # 主线程也不能因此坏掉
    try:
        eng.open_output(None)
        check("主线程再开一次", True)
    except Exception as e:                               # noqa: BLE001
        check("主线程再开一次", False, f"{e}")
    finally:
        eng.close_output()

    # 打不开的时候 self.stream 不许留半死的流(界面会以为设备好好的)
    check("关掉之后 stream 是空的", eng.stream is None)

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过", flush=True)
    if bad:
        print("★ 没过的:" + "、".join(bad), flush=True)
        return 1
    print("输出设备自查:全部通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
