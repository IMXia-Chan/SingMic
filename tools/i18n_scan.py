# -*- coding: utf-8 -*-
"""i18n 覆盖率量具：把界面里出现的中文原文，跟各语言字典的键做差集。

用法：
    python tools/i18n_scan.py            # 打印缺译清单（默认以 zh-Hans 为基准）
    python tools/i18n_scan.py --json out.json
    python tools/i18n_scan.py --lang ja  # 只看某个语言缺哪些

只做静态扫描（源码 + HTML），不启动浏览器：注释里的中文不算，用户的运行时
数据（录音名、工程名）也不会漏进来。扫的是「可能显示出来」的字符串。

★ 判定规则：一段中文里只要还有汉字，就当作需要翻译；纯技术词
  （BPM、dB、wav）不重复上报。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# 这几份不属于 Studio 界面（老录音页 / 手机页 / 桌面指针皮肤）；
# presets.js 的长句是**发给平台的原话**，只收名字，不收 brief。
SKIP_FILES = {"index.html", "phone.html", "pointer.js"}
NAME_ONLY_FILES = {"presets.js"}

CJK = re.compile(r"[一-鿿]")
# 只含这些字符的片段算「非文案」：标点、数字、空白
NOISE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)

WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return WS.sub(" ", s).strip()


def has_cjk(s: str) -> bool:
    return bool(CJK.search(s))


# --------------------------------------------------------------------------
# JS：跳过注释与正则字面量，取出普通字符串与模板字符串里的**字面段**
# --------------------------------------------------------------------------
ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def js_unescape(s: str) -> str:
    r"""把源码里的字面量还原成**运行时字符串**：`\\` -> `\`、`\n` -> 换行、`\uXXXX` -> 字符。

    ★ 这一步不能省：路径里的双反斜杠在源码里是两个字符，运行时是一个；
      不还原就会把每一条带路径的键都判成「缺译」。
    """
    if "\\" not in s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in ESC:
            out.append(ESC[nxt])
            i += 2
        elif nxt == "u" and i + 6 <= n:
            try:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
            except ValueError:
                out.append(nxt)
                i += 2
        elif nxt == "x" and i + 4 <= n:
            try:
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
            except ValueError:
                out.append(nxt)
                i += 2
        else:
            out.append(nxt)          # \" \' \\ \/ 之类
            i += 2
    return "".join(out)


def js_literals(src: str):
    """依次产出字符串字面量里的原始文本（模板字符串已按 ${} 切开）。"""
    i, n = 0, len(src)
    last = ""            # 上一个有意义的字符，用来判断 / 是不是正则开头
    prev_tok = ""        # 上一个 token 的粗略类型
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n:
            nxt = src[i + 1]
            if nxt == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j + 1
                continue
            if nxt == "*":
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            # 正则字面量：只有上一个字符是「值的开头」时才算
            if last == "" or last in "(,=:[!&|?{};+-*%~^<>":
                i = _skip_regex(src, i)
                last = "x"
                prev_tok = "re"
                continue
        if c in "'\"":
            j = i + 1
            buf = []
            while j < n:
                ch = src[j]
                if ch == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if ch == c:
                    break
                if ch == "\n":     # 未闭合，别吞掉整份文件
                    break
                buf.append(ch)
                j += 1
            yield ("str", js_unescape("".join(buf)))
            i = j + 1
            last = c
            prev_tok = "str"
            continue
        if c == "`":
            j = i + 1
            buf = []
            while j < n:
                ch = src[j]
                if ch == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if ch == "`":
                    break
                if ch == "$" and j + 1 < n and src[j + 1] == "{":
                    yield ("tpl", js_unescape("".join(buf)))
                    buf = []
                    j = _skip_braces(src, j + 2)
                    continue
                buf.append(ch)
                j += 1
            yield ("tpl", js_unescape("".join(buf)))
            i = j + 1
            last = c
            prev_tok = "tpl"
            continue
        if not c.isspace():
            last = c
        i += 1


def _skip_regex(src: str, i: int) -> int:
    j = i + 1
    in_class = False
    while j < len(src):
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            j += 1
            while j < len(src) and src[j].isalpha():
                j += 1
            return j
        elif ch == "\n":
            return j
        j += 1
    return j


def _skip_braces(src: str, i: int) -> int:
    """从 ${ 后面开始，跳到配对的 } 之后（里面可能还有字符串）。"""
    depth = 1
    n = len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


TAG = re.compile(r"<[^>]*>")
ATTR_VAL = re.compile(r"""\b(?:title|placeholder|aria-label|alt|value)\s*=\s*"([^"]*)\"""")


def html_strings(src: str):
    src = re.sub(r"<!--.*?-->", " ", src, flags=re.S)
    for m in ATTR_VAL.finditer(src):
        yield m.group(1)
    for m in re.finditer(r">([^<>]+)<", src):
        yield m.group(1)


def strip_tags(s: str) -> str:
    return TAG.sub(" ", s)


def collect() -> dict[str, set[str]]:
    """返回 {归一化中文: {出现在哪些文件}}"""
    found: dict[str, set[str]] = {}

    def add(raw: str, where: str):
        if not raw or not has_cjk(raw):
            return
        # 带标签的整段不是文案（界面上永远看不到标签），只取去掉标签后的文字
        frags = [strip_tags(raw)] if "<" in raw else [raw]
        for frag in frags:
            s = norm(frag)
            if not s or not has_cjk(s) or NOISE.match(s):
                continue
            # 一段里既有代码又有文案时（`const a = '人声'`），只留中文那一截
            if len(s) > 120 or re.search(r"[;{}]\s*$", s):
                for part in re.split(r"[;{}]|=>|\bfunction\b", s):
                    p = norm(part)
                    if p and has_cjk(p) and not NOISE.match(p) and len(p) <= 120:
                        found.setdefault(p, set()).add(where)
                continue
            found.setdefault(s, set()).add(where)

    for p in sorted(WEB.rglob("*.js")):
        name = p.name
        if "i18n" in p.parts or name in SKIP_FILES:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if name in NAME_ONLY_FILES:
            # 这类文件里的长句是**发给平台的原话**，翻了会改变行为；只收界面上的名字
            for m in re.finditer(r"\b(?:name|label)\s*:\s*'([^']*)'", txt):
                add(m.group(1), name)
            for m in re.finditer(r"\[\s*'[^']*'\s*,\s*'([^']*)'\s*\]", txt):
                add(m.group(1), name)
            continue
        for kind, s in js_literals(txt):
            if kind == "tpl":
                # 模板里可能夹着整段 HTML，按标签/换行切成小片
                for piece in re.split(r"<[^>]*>|\n", strip_tags_keep(s)):
                    add(piece, name)
            else:
                add(s, name)

    for p in sorted(WEB.rglob("*.html")):
        if p.name in SKIP_FILES:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for s in html_strings(txt):
            add(s, p.name)

    return found


def strip_tags_keep(s: str) -> str:
    """把标签换成换行，保留文字，便于按行切。"""
    return TAG.sub("\n", s)


def load_dict(lang: str) -> set[str]:
    p = WEB / "studio" / "i18n" / f"{lang}.js"
    if not p.exists():
        return set()
    txt = p.read_text(encoding="utf-8")
    body = txt[txt.find("{"):]
    keys = set()
    for kind, s in js_literals(body):
        if kind == "tpl":
            continue
        keys.add(s)
    return keys


PAT_PH = re.compile(r"\{(\d)\}")


def pattern_regexes(keys) -> list[re.Pattern]:
    """把带 `{0}` 占位符的字典键编译成正则，用来判断「这句带变量的文案是否已覆盖」。

    ★ 量具必须认得占位符，否则每一条 `缓存 1757.9MB`、`已保存 xxx（留空 = 不改）`
      都会被报成缺译 —— 实际上它们只是命中了模板键。
    """
    out = []
    for k in keys:
        if not PAT_PH.search(k):
            continue
        esc = re.escape(k)
        out.append(re.compile("^" + re.sub(r"\\\{(\d)\\\}", r"(.+?)", esc) + "$"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en")
    ap.add_argument("--json", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", action="store_true", help="打印全部缺失条目")
    a = ap.parse_args()

    found = collect()
    # 字典的键是我们手写的，同样做一次归一化对得上
    keys = {norm(k) for k in load_dict(a.lang)}
    missing = sorted(k for k in found if k not in keys)

    print(f"扫描到中文片段 {len(found)} 条；{a.lang} 字典 {len(keys)} 条；缺 {len(missing)} 条")
    by_file: dict[str, int] = {}
    for k in missing:
        for f in found[k]:
            by_file[f] = by_file.get(f, 0) + 1
    for f, c in sorted(by_file.items(), key=lambda kv: -kv[1]):
        print(f"  {f:20s} {c}")

    if a.json:
        Path(a.json).write_text(
            json.dumps({"lang": a.lang, "missing": missing}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"已写 {a.json}")

    if a.show:
        for k in (missing[: a.limit] if a.limit else missing):
            print(k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
