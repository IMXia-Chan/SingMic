# -*- coding: utf-8 -*-
"""把「按顺序排列的译文数组」贴回中文原文上，再并进字典。

为什么要按顺序：字典键是中文原文，逐条重打一遍很容易差一个空格、一个全角括号，
对不上就静默不生效。这里只写译文，键从 `i18n_items.json` 按位置取，逐字一致。

用法：
    python tools/i18n_apply.py --lang en --src tr_en.json \
        --items <临时目录>/i18n_items.json

输入 JSON：{"text": [...], "html": [...]}，两个数组的长度必须与 items 完全一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_merge import merge  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--items", required=True)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()

    items = json.loads(Path(a.items).read_text(encoding="utf-8"))
    tr = json.loads(Path(a.src).read_text(encoding="utf-8"))

    data: dict[str, str] = {}
    bad = 0
    for kind in ("text", "html"):
        src, dst = items[kind], tr.get(kind) or []
        if len(src) != len(dst):
            print(f"✗ {kind}: 原文 {len(src)} 条，译文 {len(dst)} 条 —— 数目对不上，先补齐")
            bad += 1
            continue
        for k, v in zip(src, dst):
            if not isinstance(v, str) or not v.strip():
                print(f"✗ 空译文：{k[:40]}")
                bad += 1
                continue
            data[k] = v
    if bad:
        return 2

    return merge(a.lang, data, a.overwrite)


if __name__ == "__main__":
    sys.exit(main())
