# -*- coding: utf-8 -*-
"""把 SingMic 打成能双击的 exe(onedir,不是单文件)。

为什么 onedir 不是 onefile:
  · onefile 每次启动都要把几百 MB 解到 temp,冷启动十几秒,做 DAW 不能这样;
  · onedir 的目录结构跟源码一致,`web/`、`recordings/` 这些相对路径不用改。

★ 故意排除的:torch / demucs / transformers / librosa / matplotlib。
  它们要么是「云端/本机补音」用的(那是**另一个** Python 环境),要么引擎里根本没用。
  不排除的话包会从 300MB 涨到 2GB+。
★ VST 宿主:pedalboard 必须留(studio_vst_worker 用),冻结后靠 `--vst-worker` 自调用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "SingMic"

HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "webview.platforms.edgechromium", "webview.platforms.winforms",
    "clr_loader", "pythonnet",
]

COLLECT = ["sherpa_onnx", "soundfile", "sounddevice", "pedalboard"]

EXCLUDE = [
    "torch", "torchaudio", "torchvision", "demucs", "transformers",
    "librosa", "matplotlib", "IPython", "pytest", "numba", "llvmlite",
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "notebook",
]


def main() -> int:
    if shutil.which("pyinstaller") is None:
        print("找不到 pyinstaller,先 pip install pyinstaller", file=sys.stderr)
        return 1
    args = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "desktop.py"),
        "--name", NAME,
        "--onedir",
        "--noconfirm", "--clean",
        "--noconsole",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyi"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
        # ★ 不传 --icon 的话 exe 顶着 PyInstaller 的默认图标,资源管理器里就是个陌生程序
        "--icon", str(ROOT / "packaging" / "app.ico"),
    ]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    for c in COLLECT:
        args += ["--collect-all", c]
    for e in EXCLUDE:
        args += ["--exclude-module", e]
    print(" ".join(args), flush=True)
    return subprocess.call(args, cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
