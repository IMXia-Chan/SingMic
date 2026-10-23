# -*- coding: utf-8 -*-
"""把一批译文并进 `web/studio/i18n/<lang>.js`。

输入是 JSON：{"中文原文": "译文", ...}（键必须与字典键逐字一致）。
已经存在的键默认**不覆盖**（`--overwrite` 才覆盖），这样重复跑不会把人工校准过的
译文冲掉。

用法：
    python tools/i18n_merge.py --lang en --src new_en.json
    python tools/i18n_merge.py --lang en --src pat.json --overwrite
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DICT_DIR = ROOT / "web" / "studio" / "i18n"


def js_str(s: str) -> str:
    """JSON 的字符串转义与 JS 兼容；ensure_ascii=False 保留中文可读。"""
    return json.dumps(s, ensure_ascii=False)


def normkey(k: str) -> str:
    """字典键必须是「压成单空格 + 去首尾空白」的规范形式。

    ★ 运行时 trText / trHtml 查表前都压空白；键里带一个多余的首空格就永远对不上，
      而且不报错、只是静默显示中文 —— 这类不一致必须在写进字典前抹平。
    """
    return re.sub(r"\s+", " ", k).strip()


def read_keys(text: str) -> set[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from i18n_scan import js_literals
    keys = set()
    for kind, s in js_literals(text[text.find("{"):]):
        if kind == "str":
            keys.add(normkey(s))
    return keys


def normalize_file(lang: str) -> int:
    """把字典里所有键改写成规范形式；重复键只留最后一次出现的。"""
    p = DICT_DIR / f"{lang}.js"
    text = p.read_text(encoding="utf-8")
    body = text.rstrip()
    end = body.rfind("};")
    if end < 0:
        print("字典文件结构不认识（找不到收尾的 };）")
        return 2
    dec = json.JSONDecoder()
    out, at, changed, dup = [], {}, 0, 0
    for line in body[:end].splitlines(keepends=True):
        s = line.lstrip()
        if not s.startswith('"'):
            out.append(line)
            continue
        try:
            key, idx = dec.raw_decode(s)
        except ValueError:
            out.append(line)
            continue
        nk = normkey(key)
        if nk in at:
            out[at[nk]] = ""
            dup += 1
        at[nk] = len(out)
        if nk == key:
            out.append(line)
        else:
            indent = line[: len(line) - len(s)]
            out.append(f"{indent}{js_str(nk)}{s[idx:]}")
            changed += 1
    head = "".join(x for x in out if x != "").rstrip()
    if not head.endswith(","):
        head += ","
    p.write_text(head + "\n" + body[end:].lstrip() + "\n", encoding="utf-8")
    print(f"{lang}: 规范化键 {changed} 条，去掉重复键 {dup} 条 → {p.name}")
    return 0


def merge(lang: str, data: dict, overwrite: bool = False) -> int:
    p = DICT_DIR / f"{lang}.js"
    if not p.exists():
        print(f"没有这个语言文件：{p}")
        return 2
    text = p.read_text(encoding="utf-8")
    have = read_keys(text)

    add, repl, skip = [], {}, 0
    for k, v in data.items():
        k = normkey(k)
        if not isinstance(v, str):
            continue
        if k in have:
            if overwrite:
                repl[k] = v
            else:
                skip += 1
            continue
        add.append((k, v))

    body = text.rstrip()
    end = body.rfind("};")
    if end < 0:
        print("字典文件结构不认识（找不到收尾的 };）")
        return 2

    # 覆盖模式：在原文件里逐行核对键名，命中就整行换成新译文（避免同名键出现两次）
    src_lines = body[:end].splitlines(keepends=True)
    left = dict(repl)
    if left:
        dec = json.JSONDecoder()
        for i, line in enumerate(src_lines):
            s = line.lstrip()
            if not s.startswith('"'):
                continue
            try:
                key, _ = dec.raw_decode(s)
            except ValueError:
                continue
            if key in left:
                indent = line[: len(line) - len(s)]
                src_lines[i] = f"{indent}{js_str(key)}: {js_str(left.pop(key))},\n"

    head = "".join(src_lines).rstrip()
    if not head.endswith(","):
        head += ","
    lines = "".join(f"  {js_str(k)}: {js_str(v)},\n" for k, v in add)
    out = head + "\n" + lines + "};\n"

    p.write_text(out, encoding="utf-8")
    print(f"{lang}: 新增 {len(add)} 条，覆盖 {len(repl)} 条，跳过 {skip} 条 → {p.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True)
    ap.add_argument("--src", default="")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--normalize", action="store_true", help="只把键改写成规范形式")
    a = ap.parse_args()
    if a.normalize:
        return normalize_file(a.lang)
    if not a.src:
        ap.error("要么给 --src，要么给 --normalize")

    data = json.loads(Path(a.src).read_text(encoding="utf-8"))
    return merge(a.lang, data, a.overwrite)




if __name__ == "__main__":
    sys.exit(main())
