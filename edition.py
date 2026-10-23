# -*- coding: utf-8 -*-
"""功能开关。**2026-10-23 起只发一个全功能版本,不再分社区版 / Pro。**

为什么还留着这一层:后端接口守卫(`studio_api._pro`)和前端灰按钮都挂在这里,
以后若要再分版,只改这个文件,业务代码一行不动。

读法(按优先级):
  1. 环境变量 `SINGMIC_EDITION` —— 只在开发时用来模拟受限状态;
  2. 打包版 exe 同目录的 `edition.txt` —— make_msix.py 铺目录时写进去的;
  3. 都没有 ⇒ **全功能**(PRO)。发布版一律全开。

★ 产品名只有一个:「声麦 Studio Pro」。`label()` 不随开关变化 —— 商店里就一个产品,
  把开关临时拨到 community 也不该换个名字。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

COMMUNITY = "community"
PRO = "pro"

PRODUCT_NAME = "声麦 Studio Pro"

# 曾经 Pro 独有的功能,前端拿它决定灰不灰按钮。现在发布版全开,保留给以后分版用。
PRO_FEATURES = {
    "voiceCloud": "个人音色补音",
    "gen": "AI 伴奏",
    "nl": "调音助手",
    "ref": "跟原唱",
    "auto": "一键调音",
    "stems": "分轨导出",
}

EDITION_FILE = "edition.txt"


def _from_file() -> str:
    if not getattr(sys, "frozen", False):
        return ""
    try:
        p = Path(sys.executable).resolve().parent / EDITION_FILE
        if p.is_file():
            return p.read_text(encoding="utf-8").strip().lower()
    except OSError:
        pass
    return ""


def current() -> str:
    for raw in (os.environ.get("SINGMIC_EDITION") or "", _from_file()):
        v = raw.strip().lower()
        if v in (COMMUNITY, PRO):
            return v
    return PRO


def is_pro() -> bool:
    return current() == PRO


def label() -> str:
    return PRODUCT_NAME


def features() -> dict:
    """前端要的开关表:全功能版全开;只有显式拨到 community 才全关。"""
    pro = is_pro()
    return {k: pro for k in PRO_FEATURES}


def description() -> str:
    return "录音、修音、编曲、混音与导出,全部在本机完成的一体化音频工作站。"
