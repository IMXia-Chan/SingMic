# -*- coding: utf-8 -*-
"""i18n 覆盖率的**渲染面**量具：真的把每个面板打开，收集屏幕上出现过的中文。

为什么要第二个量具：
  静态扫描（i18n_scan.py）会把 `已提交…${x}…` 这类模板切成碎片，报一堆假缺失；
  真正显示出来的是一整句。这里用无头 Edge 打开每个面板，读**渲染后的**文本节点
  与 title/placeholder/aria-label，得到的就是「他在界面上真能看到的中文」。

用法：
    python tools/i18n_dom_scan.py --port 8766 --lang en
    python tools/i18n_dom_scan.py --port 8766 --lang en --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CJK = re.compile(r"[一-鿿]")

# 每一步：名字 -> 驱动界面的 JS（同步，执行完等一下再收字）
STEPS: list[tuple[str, str]] = [
    ("顶栏", ""),
    ("左侧栏", "document.getElementById('btnRecRefresh')"),
    ("设置-偏好", "document.getElementById('btnSettings').click()"),
    ("设置-数据", "document.querySelector('#setNav [data-sec=data]').click()"),
    ("设置-帮助", "document.querySelector('#setNav [data-sec=help]')?.click()"),
    ("设置-手册", "document.querySelector('#setNav [data-sec=manual]').click()"),
    ("设置-FAQ", "document.querySelector('#setNav [data-sec=faq]').click()"),
    ("设置-快捷键", "document.querySelector('#setNav [data-sec=keys]').click()"),
    ("设置-邮箱", "document.querySelector('#setNav [data-sec=contact]').click()"),
    ("设置-日志", "document.querySelector('#setNav [data-sec=log]').click()"),
    ("设置-关于", "document.querySelector('#setNav [data-sec=about]').click()"),
    ("一键调音", "document.getElementById('btnSettings').click(); "
                 "document.getElementById('btnAuto').click()"),
    ("跟原唱", "document.getElementById('btnRef').click()"),
    ("我的声音", "document.getElementById('btnVoice').click()"),
    ("调音助手", "document.getElementById('btnNL').click()"),
    ("AI 伴奏", "document.getElementById('btnGen').click()"),
    ("模板库", "document.getElementById('btnGenLib')?.click()"),
    ("鼓机", "document.getElementById('btnDrum').click()"),
    ("导出", "document.getElementById('btnExport').click()"),
    ("混音台", "document.querySelector('[data-tab=mix]')?.click()"),
    ("效果", "document.querySelector('[data-tab=fx]')?.click()"),
    ("插件", "document.querySelector('[data-tab=vst]')?.click()"),
]

HARVEST = r"""(() => {
  const out = new Set();      // 单独成句的文本
  const html = new Set();     // 带 <b>/<code> 夹着的整段（按 innerHTML 当键）
  const CJKRE = new RegExp('[一-鿿]');
  const add = (s, dst) => {
    if (typeof s === 'string' && CJKRE.test(s)) dst.add(s.replace(/\s+/g, ' ').trim());
  };
  const ok = (el) => {
    try {
      if (!el || !el.getClientRects || !el.getClientRects().length) {
        // ★ 下拉选项在收起状态没有自己的矩形，但它是看得见的 —— 以前这里把它整类漏掉了，
        //   「跟随系统 / 按字音节（推荐）」这种只出现在 option 里的文案从没被量到过。
        if (el && el.tagName === 'OPTION') {
          const s = el.closest('select');
          if (!s || !s.getClientRects().length) return false;
          const st = getComputedStyle(s);
          return st.visibility !== 'hidden' && st.display !== 'none';
        }
        return false;
      }
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none') return false;
      if (/^(SCRIPT|STYLE|TEXTAREA)$/.test(el.tagName)) return false;
      if (el.closest('[data-i18n-skip]')) return false;
      return true;
    } catch (e) { return false; }
  };
  // 与 i18n.js 里的 safeHtmlEl 保持一致：只有纯排版标签的段落才整段翻
  const SAFE = new Set(['B','I','EM','STRONG','CODE','KBD','SPAN','BR','SMALL','SUB','SUP','MARK','U']);
  const INTER = 'button,input,select,textarea,a[href],canvas,svg,audio,video,[id],[onclick]';
  const ATTRS = ['title','placeholder','aria-label','alt'];
  const safeHtmlEl = (el) => {
    if (!el.children || !el.children.length) return false;
    for (const c of el.querySelectorAll('*')) {
      if (!SAFE.has(c.tagName)) return false;
      if (c.matches(INTER)) return false;
      if (ATTRS.some((a) => c.hasAttribute(a))) return false;
    }
    return true;
  };
  const covered = new Set();   // 已经被整段 HTML 覆盖的祖先
  for (const el of document.body.querySelectorAll('*')) {
    if (!ok(el) || !safeHtmlEl(el)) continue;
    if (!CJKRE.test(el.textContent || '')) continue;
    html.add(el.innerHTML.replace(/\s+/g, ' ').trim());   // ★ 与 i18n.js 的 trHtml 一致
    covered.add(el);
  }
  const inCovered = (el) => {
    for (let p = el; p; p = p.parentElement) if (covered.has(p)) return true;
    return false;
  };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const el = n.parentElement;
    if (ok(el) && !inCovered(el)) add(n.nodeValue, out);
  }
  for (const el of document.querySelectorAll('[title],[placeholder],[aria-label],[alt]')) {
    if (!ok(el)) continue;
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v) add(v, out);
    }
  }
  return { text: [...out], html: [...html] };
})()"""


async def scan(port: int, dbg: int, lang: str = "") -> dict[str, list[str]]:
    prof = tempfile.mkdtemp(prefix="i18nscan-")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={dbg}",
         f"--user-data-dir={prof}", "--no-first-run", "--mute-audio",
         "--window-size=1600,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version", timeout=1).read()
                break
            except Exception:
                await asyncio.sleep(0.3)
        req = urllib.request.Request(f"http://127.0.0.1:{dbg}/json/new?about:blank", method="PUT")
        tgt = json.loads(urllib.request.urlopen(req, timeout=5).read())
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=2 ** 26) as ws:
            cid = [0]

            async def call(method, params=None):
                cid[0] += 1
                await ws.send(json.dumps({"id": cid[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if msg.get("id") == cid[0]:
                        return msg

            async def ev(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("result", {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
            # ★ 必须**真的把界面切到目标语言**再收字。之前这里没切，页面仍是中文，
            #   量出来的「缺 0 条」其实是「字典里有这些键」，不是「界面上真的翻了」——
            #   量具自己撒谎。语言选择存 localStorage，先种进去再重载即可。
            if lang:
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/studio"})
                await asyncio.sleep(2)
                await ev(f"localStorage.setItem('singmic.lang', {json.dumps(lang)})")
            await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/studio"})
            await asyncio.sleep(5)
            if lang:
                got_lang = await ev("document.documentElement.lang")
                if got_lang != lang:
                    print(f"  ! 界面语言是 {got_lang}，不是 {lang} —— 量出来的数不作数",
                          file=sys.stderr)

            got: dict[str, dict[str, list[str]]] = {}
            for name, js in STEPS:
                if js:
                    try:
                        await ev(f"(()=>{{{js}}})()")
                    except Exception as e:      # 某个面板打不开就跳过，不影响别的
                        print(f"  ! {name}: {e}", file=sys.stderr)
                await asyncio.sleep(1.2)
                try:
                    got[name] = await ev(HARVEST) or {"text": [], "html": []}
                except Exception:
                    got[name] = {"text": [], "html": []}
            return got
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def load_keys(lang: str) -> tuple[set[str], list]:
    """返回 (已覆盖的字符串, 模板正则)。

    ★ 只看「键」不够：日语/韩语译文本身也是汉字（CJK 区间），会被当成没翻。
      所以把字典的**值**也收进来 —— 屏幕上出现的就是某个值，说明已经翻过了。
      模板（带 {0}）两边都编译：键用来认「还没翻的中文原文」，值用来认「已经翻出来的句子」。
    """
    p = Path(__file__).resolve().parent.parent / "web" / "studio" / "i18n" / f"{lang}.js"
    if not p.exists():
        return set(), []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from i18n_scan import js_literals, norm, pattern_regexes
    txt = p.read_text(encoding="utf-8")
    keys: set[str] = set()
    vals: set[str] = set()
    for i, (kind, s) in enumerate(js_literals(txt[txt.find("{"):])):
        if kind != "str":
            continue
        # 键与值交替出现：偶数位是键，奇数位是值（同一个对象字面量里成对）
        (keys if i % 2 == 0 else vals).add(norm(s))
    allv = keys | vals
    return allv, pattern_regexes(allv)


# 用户数据不是文案：录音名、工程名、路径、缓存体积
USERDATA = re.compile(
    r"\.(wav|mp3|flac|ogg|zip|json|mid|mp4)(?![a-z])|^[A-Za-z]:|^\\"
    r"|^(demo|gen_mock|ext_|_smoke|sample|rec|take)", re.I)
SIZES = re.compile(r"^\d+(?:\.\d+)?\s*(?:MB|GB|KB|个文件|个)$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--dbg", type=int, default=9261)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--json", default="")
    ap.add_argument("--cross", action="store_true",
                    help="再跑一遍中文原版，只报「和目标语言下都一模一样」的字符串 —— 这是唯一不靠字典自证的判据")
    a = ap.parse_args()

    if a.cross:
        base = asyncio.run(scan(a.port, a.dbg, "zh-Hans"))
        got = asyncio.run(scan(a.port, a.dbg + 1, a.lang))
        for name, items in got.items():
            b = base.get(name) or {}
            for kind in ("text", "html"):
                keep = set(b.get(kind) or [])
                items[kind] = [x for x in items.get(kind, []) if x in keep]
    else:
        got = asyncio.run(scan(a.port, a.dbg, a.lang))
    keys, pats = load_keys(a.lang)

    def covered(x: str) -> bool:
        return x in keys or any(r.match(x) for r in pats)

    seen: dict[str, str] = {}
    seen_html: dict[str, str] = {}
    total = 0
    for name, items in got.items():
        for kind, dst in (("text", seen), ("html", seen_html)):
            for x in items.get(kind, []):
                total += 1
                if x in dst:
                    continue
                # 交叉模式下「和中文本一模一样」本身就是判据，不再拿字典自证
                if not a.cross and covered(x):
                    continue
                if USERDATA.search(x) or SIZES.match(x):
                    continue
                if name == "AI 伴奏" and "bpm" in x:   # 风格描述原样发给平台，故意不翻
                    continue
                dst[x] = name
    tag = "（交叉模式：与中文原版逐条比对）" if a.cross else ""
    print(f"渲染面共出现 {total} 条（含重复/用户数据）；{a.lang} 缺 "
          f"文本 {len(seen)} 条 / 整段 {len(seen_html)} 条{tag}")
    by: dict[str, list[str]] = {}
    for k, name in seen.items():
        by.setdefault(name, []).append(k)
    for name, lst in by.items():
        print(f"\n=== {name} ({len(lst)})")
        for x in sorted(lst):
            print(f"  {x[:110]}")
    if seen_html:
        print(f"\n=== 整段（innerHTML，按段翻）{len(seen_html)}")
        for x in sorted(seen_html):
            print(f"  {x[:130]}")
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"text": sorted(seen), "html": sorted(seen_html)}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\n已写 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
