# -*- coding: utf-8 -*-
"""sounddevice 的延迟代理。

为什么要有：`import sounddevice` 在模块末尾就会调 `Pa_Initialize()`，把整台机器的
音频设备枚举一遍（sounddevice.py 第 2973 行）。这台机器上装着虚拟声卡（酷狗、
VB-Cable 之类），实测这一步 0.9 秒到 20 秒不等 —— 而它正好落在启动最前面，
界面就被一条不动的进度条拖在那儿。

所以启动路径上不再 import sounddevice，改用本代理：第一次真正用到音频接口时才导入。
模块级写法 `import lazy_sd as sd` 与 `import sounddevice as sd` 等价，
`sd.query_devices()` / `sd.default` / `sd._terminate()` 这些用法一行都不用改。

★ 打包时 sounddevice 仍要在 COLLECT 里（见 tools/build_app.py）——
  这里走的是 importlib 动态导入，PyInstaller 静态分析看不到。
"""
import importlib

_real = None


def _mod():
    global _real
    if _real is None:
        _real = importlib.import_module("sounddevice")
    return _real


def __getattr__(name):
    return getattr(_mod(), name)


def __dir__():
    return dir(_mod())
