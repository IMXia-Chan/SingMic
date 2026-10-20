"""端到端:真 Edge(无头)+ CDP,把网页上的块编辑器真拖一遍。

验的是**整条路**:页面加载 → 自动切块 → 在画布上真的 mousedown/move/up →
音高改了 → 后台渲染 → <audio> 拿到 audio/wav → Delete 留空。

跑法(先确保服务在 8765):
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\smoke_editor.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://127.0.0.1:8765"
DBG = 9223
ROOT = Path(__file__).resolve().parent.parent
REC = ROOT / "recordings"
SMOKE = "_smoke_voice.wav"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.events: list[dict] = []

    async def call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if msg.get("id") == mid:
                return msg
            self.events.append(msg)

    async def ev(self, expr: str, await_promise: bool = False, timeout: float = 60.0):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True,
                             "awaitPromise": await_promise}, timeout)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"].get("text", "JS error"))
        return res.get("result", {}).get("value")


async def wait_for(c: CDP, expr: str, cond, timeout: float = 90.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = await c.ev(expr)
        except Exception:                       # noqa: BLE001
            v = None
        if cond(v):
            return v
        await asyncio.sleep(0.4)
    return None


def abs_url(u: str) -> str:
    """页面里拿到的可能是相对路径,也可能是带端口的绝对地址 —— 统一成能 fetch 的。"""
    if u.startswith("/"):
        return BASE + u
    if "8765" in u:
        return BASE + u.split("8765", 1)[1]
    return u


def js_errors(c: CDP) -> list[str]:
    out = []
    for e in c.events:
        m = e.get("method")
        if m == "Runtime.exceptionThrown":
            d = e["params"]["exceptionDetails"]
            out.append(d.get("text", "") + " " + str(d.get("exception", {}).get("description", "")))
        elif m == "Log.entryAdded":
            en = e["params"]["entry"]
            if en.get("level") == "error":
                out.append(en.get("text", "") + " <" + str(en.get("url", "")) + ">")
    return out


async def main() -> int:
    # ---- 准备一条被测录音(不动正式录音) ----
    # 用法: python tools/smoke_editor.py [录音路径]
    #   不给路径就用 recordings/ 里最新的 *_voice.wav。
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        cand = sorted(REC.glob("*_voice.wav"), key=lambda p: p.stat().st_mtime)
        if not cand:
            print("recordings/ 里没有 *_voice.wav;先录一条,或把路径作为参数传进来")
            return 2
        src = cand[-1]
    dst = REC / SMOKE
    smix = REC / "_smoke_mix.wav"
    if not src.exists():
        print("找不到测试用录音", src)
        return 2
    shutil.copyfile(src, dst)
    m = src.with_name(src.name.replace("_voice.wav", "_mix.wav"))
    if m.exists():
        shutil.copyfile(m, smix)

    tmp = Path(tempfile.mkdtemp(prefix="ed-smoke-"))
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
        f"--user-data-dir={tmp}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio", "--window-size=1400,1200",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = False
    try:
        version = None
        for _ in range(60):
            try:
                version = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{DBG}/json/version", timeout=1).read())
                break
            except Exception:                   # noqa: BLE001
                await asyncio.sleep(0.3)
        if not version:
            print("Edge 没起来")
            return 2

        req = urllib.request.Request(f"http://127.0.0.1:{DBG}/json/new?about:blank", method="PUT")
        tgt = json.loads(urllib.request.urlopen(req, timeout=5).read())
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=2 ** 24) as ws:
            c = CDP(ws)
            await c.call("Runtime.enable")
            await c.call("Log.enable")
            await c.call("Page.enable")
            await c.call("Page.navigate", {"url": BASE + "/"})

            got = await wait_for(c, "typeof ED !== 'undefined' && document.readyState === 'complete'",
                                 lambda v: v is True)
            check("页面加载,编辑器脚本就位", bool(got))
            if not got:
                raise RuntimeError("页面没加载起来")

            # 自动打开最新一条(可能是 7 分钟那条),等它切完
            n = await wait_for(c, "ED.blocks.length", lambda v: isinstance(v, int) and v > 0, 120)
            check("自动切块出块表", bool(n), f"{n} 块")

            # 换成副本,别碰正式录音
            await c.ev(f"edOpen({json.dumps(SMOKE)})", await_promise=True)
            nm = await wait_for(c, "ED.name", lambda v: v == SMOKE, 120)
            check("切到测试录音", nm == SMOKE, str(nm))

            note = await c.ev(
                "(()=>{const b=ED.blocks.find(x=>x.kind==='note');"
                "return b?{id:b.id,t0:b.t0,t1:b.t1,f0:b.f0}:null})()")
            check("块表里有可编辑的音", note is not None, str(note))

            if note:
                # 关掉音阶吸附,拖 +2 半音,看是不是真的写进了 edits
                await c.ev("document.getElementById('edScale').checked=false;"
                           "const b=ED.blocks.find(x=>x.id===" + str(note["id"]) + ");"
                           "ED.view.t0=Math.max(0,b.t0-0.5); ED.view.pps=200; edDraw();"
                           "document.getElementById('edCard').scrollIntoView({block:'center'}); true")
                await asyncio.sleep(0.3)
                # 留一张截图给人看 —— 断言证明不了「画得对不对」
                shot = await c.call("Page.captureScreenshot", {"format": "png"})
                data = shot.get("result", {}).get("data")
                if data:
                    import base64
                    (ROOT / "_shot_editor.png").write_bytes(base64.b64decode(data))
                geom = await c.ev(
                    "(()=>{const b=ED.blocks.find(x=>x.id==" + str(note["id"]) + ");"
                    "const r=edCanvas.getBoundingClientRect();"
                    "return {x:r.left+(b.t0+b.t1)/2*0+0, cx:r.left+edX((b.t0+b.t1)/2),"
                    " y:r.top+edY(bMidi(b)), row:edRow, h:r.height}})()")
                # 用画布坐标(不是页面坐标)算拖拽点
                cv = await c.ev("(()=>{const r=edCanvas.getBoundingClientRect();"
                                "return {left:r.left,top:r.top}})()")
                x = cv["left"] + (await c.ev(
                    "(()=>{const b=ED.blocks.find(x=>x.id==" + str(note["id"]) + ");"
                    "return edX((b.t0+b.t1)/2)})()"))
                y = cv["top"] + (await c.ev(
                    "(()=>{const b=ED.blocks.find(x=>x.id==" + str(note["id"]) + ");"
                    "return edY(bMidi(b))})()"))

                async def mouse(t, yy, buttons=1):
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": t, "x": x, "y": yy, "button": "left",
                                  "buttons": buttons, "clickCount": 1})

                await mouse("mousePressed", y)
                await mouse("mouseMoved", y - geom["row"] * 2)
                await mouse("mouseReleased", y - geom["row"] * 2, buttons=0)
                await asyncio.sleep(1.5)

                semis = await c.ev("(ED.edits[" + str(note["id"]) + "]||{}).semis")
                check("画布上真拖 +2 半音 → 写进编辑", semis == 2, f"semis={semis}")

                audio = await wait_for(c, "document.getElementById('edAudio').src",
                                       lambda v: isinstance(v, str) and "/api/preview/" in v, 30)
                check("后台渲染出预览 URL", bool(audio), str(audio)[-60:])

                # ★ 挂上 URL ≠ 在放。真的走时钟才算数。
                play = await wait_for(
                    c, "(()=>{const a=document.getElementById('edAudio');"
                       "return {t:a.currentTime, paused:a.paused,"
                       " err:a.error?a.error.code:0}})()",
                    lambda v: isinstance(v, dict) and v.get("t", 0) > 0.3, 20)
                check("松手后自动播放了", bool(play),
                      str(play))

                if audio:
                    with urllib.request.urlopen(abs_url(audio), timeout=20) as r:
                        ctype = r.headers.get("content-type", "")
                        body = r.read()
                    check("<audio> 拿到 audio/wav(不是附件)",
                          ctype.startswith("audio/wav") and len(body) > 1000,
                          f"{ctype} {len(body)} 字节")

                # Delete → 留空
                await c.ev("ED.sel=" + str(note["id"]) + "; true")
                await c.call("Input.dispatchKeyEvent",
                             {"type": "keyDown", "key": "Delete", "code": "Delete",
                              "windowsVirtualKeyCode": 46})
                await c.call("Input.dispatchKeyEvent",
                             {"type": "keyUp", "key": "Delete", "code": "Delete",
                              "windowsVirtualKeyCode": 46})
                await asyncio.sleep(1.2)
                dele = await c.ev("(ED.edits[" + str(note["id"]) + "]||{}).deleted")
                check("Delete 留空", dele is True, f"deleted={dele}")

                # 编辑存档:服务端读回来还在
                saved = await c.ev(
                    "fetch('/api/blocks',{method:'POST',headers:{'Content-Type':'application/json'},"
                    "body:JSON.stringify({name:" + json.dumps(SMOKE) + "})})"
                    ".then(r=>r.json()).then(j=>j.edits)", await_promise=True)
                check("编辑已存档到 <录音>.edits.json",
                      isinstance(saved, dict) and str(note["id"]) in saved, str(saved)[:80])

                # Ctrl+Z 撤销 —— 应该把刚才那条删除退回去
                await c.call("Input.dispatchKeyEvent",
                             {"type": "keyDown", "key": "z", "code": "KeyZ",
                              "windowsVirtualKeyCode": 90, "modifiers": 2})
                await c.call("Input.dispatchKeyEvent",
                             {"type": "keyUp", "key": "z", "code": "KeyZ",
                              "windowsVirtualKeyCode": 90, "modifiers": 2})
                await asyncio.sleep(1.0)
                after = await c.ev("ED.edits[" + str(note["id"]) + "]")
                check("Ctrl+Z 撤销回去", not (after or {}).get("deleted"), str(after))

                # 「整条试听」:全片渲染 + 下载链接
                await c.ev("document.getElementById('edPlayAll').click(); true")
                full = await wait_for(c, "ED.lastFull",
                                      lambda v: isinstance(v, str) and "/api/preview/" in v, 60)
                dl = await c.ev("(()=>{const d=document.getElementById('edDl');"
                                "return {hidden:d.hidden, name:d.getAttribute('download')}})()")
                check("整条试听出全片预览 + 下载链接",
                      bool(full) and isinstance(dl, dict) and not dl.get("hidden")
                      and str(dl.get("name", "")).endswith("_edited.wav"),
                      f"{str(full)[-40:]} {dl}")
                if full:
                    win = await c.ev("ED.dur")
                    with urllib.request.urlopen(abs_url(full), timeout=60) as r:
                        fb = r.read()
                    # ★ 别拿「字节数 ÷ 4 ÷ 48000」算时长 —— 带伴奏的预览是双声道,会正好翻倍。
                    import struct
                    ch = struct.unpack("<H", fb[22:24])[0]
                    sr_h = struct.unpack("<I", fb[24:28])[0]
                    bits = struct.unpack("<H", fb[34:36])[0]
                    secs = (len(fb) - 44) / (ch * bits // 8) / sr_h
                    check("全片预览长度与录音一致",
                          abs(secs - float(win or 0)) < 0.5,
                          f"{secs:.2f}s / 原 {win}s · {ch} 声道 {sr_h}Hz {bits}bit")

            errs = js_errors(c)
            check("页面没有 JS 报错", not errs, " | ".join(errs[:3]))
            ok = all(o for _, o, _ in RESULTS)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:                       # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        for q in (dst, smix, REC / "_smoke_voice.blocks.json",
                  REC / "_smoke_voice.edits.json", *REC.glob("_preview__smoke_voice_*.wav")):
            q.unlink(missing_ok=True)

    n_ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {n_ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
