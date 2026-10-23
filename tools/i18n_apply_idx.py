# -*- coding: utf-8 -*-
"""按**序号**把译文贴回 `i18n_items_ship.json` 里的原文，再并进字典。

为什么不直接写「中文键: 译文」：键必须逐字一致，差一个全角括号、一个空格就静默失效。
序号不会抄错，键永远从原文里取。

用法：
    python tools/i18n_apply_idx.py --lang en --map C:/.../tr_en_map.json

map 结构：
    {"text": {"91": "…", "92": "…"}, "html": {"92": "…"}, "extra": {"中文原句": "…"}}
`extra` 用来放几处**在用 t() 现拼**的句子（它们不在 items 里）。
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
    ap.add_argument("--items", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()

    items = json.loads(Path(a.items).read_text(encoding="utf-8"))
    m = json.loads(Path(a.map).read_text(encoding="utf-8"))

    data: dict[str, str] = {}
    for kind in ("text", "html"):
        src = items[kind]
        for idx, val in (m.get(kind) or {}).items():
            i = int(idx)
            if not 0 <= i < len(src):
                print(f"✗ {kind}[{i}] 越界（共 {len(src)} 条）")
                return 2
            if not isinstance(val, str) or not val.strip():
                print(f"✗ {kind}[{i}] 译文为空")
                return 2
            data[src[i]] = val
    for k, v in (m.get("extra") or {}).items():
        if not isinstance(v, str) or not v.strip():
            print(f"✗ extra「{k[:30]}」译文为空")
            return 2
        data[k] = v
    if not data:
        print("map 是空的，没东西可并")
        return 2
    return merge(a.lang, data, a.overwrite)


if __name__ == "__main__":
    sys.exit(main())
