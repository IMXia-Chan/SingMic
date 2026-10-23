"""VST3 worker —— **只有这个文件 import pedalboard**。

父进程(`studio_vst.py`)通过 stdin/stdout 一行一个 JSON 跟它说话:
  {"op":"inspect","path":"...dll"}                         → 参数表
  {"op":"process","in":..,"out":..,"sr":48000,"slots":[...]} → 处理音频

★ 为什么单独一个进程:
  1. 插件崩溃/卡死杀不到 HTTP 服务 —— 这是主要理由;
  2. pedalboard 是 GPL-3.0,本项目同为 GPL-3.0,
     所以进程边界**不是**为了绕开许可,只是顺手把「插件依赖」和主程序隔开:
     不装 pedalboard,主程序照常跑。

★ 用 `-I` 启动(父进程负责),所以这里不要依赖当前目录里的任何模块。
"""
from __future__ import annotations

import json
import sys
import traceback

import numpy as np
import pedalboard as pb
import soundfile as sf

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

_PLUGINS: dict[str, object] = {}


def _get_plugin(path: str):
    p = _PLUGINS.get(path)
    if p is None:
        # ★ 必须给内部 DLL,不能给 .vst3 bundle 目录
        p = pb.load_plugin(path, initialization_timeout=20.0)
        _PLUGINS[path] = p
    return p


def _num(x):
    """只把真正能转成数字的东西转出来;`None` / 字符串一律回 None。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _json_val(x):
    """把 getattr 读回来的 numpy 标量变成 JSON 能写的东西。"""
    if isinstance(x, (bool, str)):
        return x
    if isinstance(x, (int, float, np.integer, np.floating)):
        f = float(x)
        return f if np.isfinite(f) else None
    return str(x)


def _param_view(v, plug=None, key=None):
    """★ pedalboard 0.9.26 的 `plugin.parameters` 给的是 AudioProcessorParameter
      对象(不是 float):取值范围在 `.range`/`.min_value`/`.max_value`,当前值在
      `.raw_value`(永远 0–1)。**给人看/写进工程的必须是自然值**:
      `getattr(plugin, key)` 读自然值,`setattr(plugin, key, 自然值)` 写回。

    ★ 这里有两个曾经把整套参数变成 0–1 数字框的坑:
      1. `.range` 的第三项(步长)经常是 None,`float(None)` 一抛,
         原来的 `except` 把 min/max 一起丢了 —— 现在逐项转,谁也不连坐;
      2. `default` 原来取 `raw_value`,永远是 0–1 —— 现在取当前自然值。"""
    name = str(getattr(v, "name", "") or "")
    units = getattr(v, "units", None)

    # 老版 pedalboard 直接给 float/str/bool
    if isinstance(v, (int, float, bool, str)) or not hasattr(v, "raw_value"):
        if isinstance(v, bool):
            return {"type": "bool", "default": bool(v), "min": None,
                    "max": None, "step": None, "label": name, "units": units}
        if isinstance(v, str):
            return {"type": "string", "default": v, "min": None,
                    "max": None, "step": None, "label": name, "units": units}
        n = _num(v)
        if n is not None:
            return {"type": "number", "default": n, "min": None,
                    "max": None, "step": None, "label": name, "units": units}
        return {"type": "other", "default": str(v), "min": None,
                "max": None, "step": None, "label": name, "units": units}

    raw = getattr(v, "raw_value", None)
    # ★ 自然值:优先问插件本体(`getattr(plugin, key)` 返回的 WeakTypeWrapper
    #   自带 `.type` / `.valid_values`,能直接转成 bool/str/float)。
    nat = None
    if plug is not None and key is not None:
        try:
            nat = getattr(plug, key)
        except Exception:                          # noqa: BLE001
            nat = None
    if nat is None:
        nat = raw

    pytype = getattr(nat, "type", None) or getattr(v, "type", None)
    valid = getattr(nat, "valid_values", None) or getattr(v, "valid_values", None)
    options = None
    if isinstance(valid, (list, tuple)) and 0 < len(valid) <= 64:
        try:
            options = [_json_val(x) for x in valid]
        except Exception:                          # noqa: BLE001
            options = None

    rng = getattr(v, "range", None)
    if not rng:
        try:
            rng = (getattr(v, "min_value", None),
                   getattr(v, "max_value", None),
                   getattr(v, "step_size", None))
        except Exception:                          # noqa: BLE001
            rng = ()
    try:
        lo = _num(rng[0]) if len(rng) > 0 else None
    except Exception:                              # noqa: BLE001
        lo = None
    try:
        hi = _num(rng[1]) if len(rng) > 1 else None
    except Exception:                              # noqa: BLE001
        hi = None
    try:
        step = _num(rng[2]) if len(rng) > 2 else None
    except Exception:                              # noqa: BLE001
        step = None
    if step is not None and step <= 0:
        step = None

    def _out(kind, default, **kw):
        d = {"type": kind, "default": default, "min": None, "max": None,
             "step": None, "label": name, "units": units,
             "raw": _json_val(raw)}
        if options:
            d["options"] = options
        d.update(kw)
        return d

    if pytype is bool or isinstance(nat, bool):
        try:
            return _out("bool", bool(nat))
        except Exception:                          # noqa: BLE001
            return _out("bool", False)
    if pytype is str or isinstance(nat, str):
        try:
            return _out("string", str(nat))
        except Exception:                          # noqa: BLE001
            return _out("string", str(raw))
    n = _num(nat)
    if n is not None:
        return _out("number", n, min=lo, max=hi, step=step)
    # 最后再试一次:WeakTypeWrapper 的 float 有时只在 pytype 已知时可用
    if pytype in (float, int):
        try:
            return _out("number", float(nat), min=lo, max=hi, step=step)
        except Exception:                          # noqa: BLE001
            pass
    return _out("other", str(nat))


def op_ping(_req: dict) -> dict:
    """★ 父进程靠这个判断「宿主到底能不能用」(pedalboard 装没装)。"""
    return {"ok": True, "pedalboard": getattr(pb, "__version__", "?")}


def op_inspect(req: dict) -> dict:
    import time
    t0 = time.time()
    plug = _get_plugin(str(req["path"]))
    # ★ 可选 params:先把自然值设进去,再把**回读值**带回去。
    #   自动配方靠这一步验货 —— 插件不吃/自行夹住的值会被当场发现。
    applied: dict = {}
    for k, v in (req.get("params") or {}).items():
        try:
            setattr(plug, str(k), v)
            applied[str(k)] = _json_val(getattr(plug, str(k)))
        except Exception as e:                     # noqa: BLE001
            applied[str(k)] = {"error": str(e)}
    params = []
    try:
        items = dict(plug.parameters).items()
    except Exception:                              # noqa: BLE001
        items = []
    for k, v in items:
        d = _param_view(v, plug, str(k))
        params.append({"key": str(k), **d})
    latency = None
    try:
        latency = float(plug.latency_seconds)
    except Exception:                              # noqa: BLE001
        pass
    return {"ok": True, "name": str(getattr(plug, "name", "") or ""),
            "params": params, "applied": applied, "latency": latency,
            "sec": round(time.time() - t0, 3)}


def _mono(y) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 2:
        # pedalboard 给的是 (声道, 采样)
        if y.shape[0] <= 2:
            y = y.mean(axis=0)
        elif y.shape[1] <= 2:
            y = y.mean(axis=1)
        else:
            y = y.reshape(-1)
    return y.reshape(-1)


def _run(plug, x: np.ndarray, sr: int):
    """★ 一律先喂立体声(同一条复制成 L/R),回来取平均合回单声道。

       为什么不是「先单声道、不行再转立体声」:
       - Dragonfly 四个混响声明 2 进 2 出,喂单声道会抛 ValueError,那种还接得住;
       - **TDR Molotok 喂单声道是直接段错误**,整个 worker 进程当场没了,
         Python 里连 except 都轮不到 —— 兜底代码根本没机会跑。
       反过来,纯单声道插件喂立体声只是抛一个可捕获的 ValueError,再退回单声道即可。
       实测这台机上 Airwindows / Nova / Kotelnikov / Molotok / Dragonfly 全吃立体声。"""
    stereo = np.stack([x, x])
    try:
        return plug.process(stereo, sr, reset=True)
    except ValueError as e:
        if "channel" not in str(e).lower():
            raise
        return plug.process(x, sr, reset=True)


def op_process(req: dict) -> dict:
    import time
    t0 = time.time()
    x, sr_in = sf.read(str(req["in"]), dtype="float32", always_2d=False)
    x = _mono(x)
    sr = int(req.get("sr") or sr_in or 48000)
    for slot in req.get("slots") or []:
        plug = _get_plugin(str(slot["path"]))
        for k, v in (slot.get("params") or {}).items():
            try:
                if k not in plug.parameters:
                    continue
                if isinstance(v, str):
                    setattr(plug, k, v)
                else:
                    setattr(plug, k, float(v))
            except Exception:                      # noqa: BLE001
                # 单个参数设不上不影响其它参数;真全错会在 process 时暴露
                continue
        y = _mono(_run(plug, x, sr))
        if len(y) < len(x):
            y = np.pad(y, (0, len(x) - len(y)))
        elif len(y) > len(x):
            y = y[:len(x)]
        x = np.nan_to_num(y, nan=0.0, posinf=4.0, neginf=-4.0).astype(np.float32)
    sf.write(str(req["out"]), x, sr, format="WAV", subtype="FLOAT")
    return {"ok": True, "sec": round(time.time() - t0, 3), "samples": int(len(x))}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "ping":
                resp = op_ping(req)
            elif op == "inspect":
                resp = op_inspect(req)
            elif op == "process":
                resp = op_process(req)
            elif op == "exit":
                break
            else:
                resp = {"ok": False, "error": f"未知操作:{op}"}
        except Exception as e:                     # noqa: BLE001
            resp = {"ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc()[-1500:]}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
