"""外部渲染器的接口 —— **以后接 VST 宿主的那道缝**。

这一轮**不加载 VST、不执行任何外部命令**。这里只把接口先定死,
免得以后接宿主时把导出流程推翻重来。

接法(以后):
  1. 写一个类实现 `ExternalRenderer`;
  2. `render()` 收到的是**分轨**(每轨单声道 float32,已含轨道增益、不含声像),
     返回立体声 `(n, 2)` float32;
  3. 宿主建议跑在**独立进程**里。`pedalboard` / `dawdreamer` 都是 **GPL-3.0**,
     按独立可选依赖接入,**不要并进主代码树**(本项目同为 GPL-3.0,协议上相容,
     但独立进程能让「没装宿主」和「宿主崩了」都不影响主程序);
  4. 把 `get_renderer()` 改成返回你的实现即可,`studio_api.export_project` 不用动。
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class ExternalRenderer(Protocol):
    """外部 VST/DAW 渲染器的最小契约。"""

    def available(self) -> bool:
        ...

    def render(self, stems: list[tuple[str, np.ndarray]], project: dict,
               sr: int) -> np.ndarray:
        """返回 (样本数, 2) 的 float32 立体声。"""
        ...


def get_renderer() -> ExternalRenderer | None:
    """还没有接宿主。以后在这里返回实现。"""
    return None
