# -*- coding: utf-8 -*-
"""按 packaging/store/listing.md 的六项，在**专用演示数据**上拍商店截图。

为什么要单独一个脚本而不是手动截屏:
  · 商店截图会公开，里面**不能**出现本机用户名、真实录音文件名、真实工程名、任何 key；
    手动截图很难保证，所以这里用临时数据根 + 临时配置根，跑完整个删掉。
  · 演示音频是程序合成的（正弦基频 + 元音包络 + 颤音），不含任何真人声音。

用法:
    python tools/shots_store.py
输出:
    packaging\\store\\screenshots\\*.png （1920×1080）

★ 端口与调试端口在这里写死为 8771 / 9331，避免和冒烟(8766/92xx)、正式(8765)撞。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "store" / "screenshots"
PORT = int(os.environ.get("SHOT_PORT", "8771"))
DBG = int(os.environ.get("SHOT_DBG", "9331"))
BASE = f"http://127.0.0.1:{PORT}"
EDGE = next((p for p in (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
) if Path(p).exists()), "msedge.exe")

SR = 48000
DUR = 9.0


# ---------------------------------------------------------------- 演示音频
def _syllables(n: int) -> list[tuple[float, float, float]]:
    """返回 n 个 (起始秒, 时长, 基频Hz)。旋律自己定，听感像哼唱即可。"""
    notes = [392.0, 440.0, 493.9, 523.3, 493.9, 440.0, 392.0, 349.2, 392.0, 440.0, 392.0, 329.6]
    out, t = [], 0.35
    for i in range(n):
        d = 0.46 if i % 4 != 3 else 0.62
        out.append((t, d, notes[i % len(notes)]))
        t += d + 0.08
    return out


def _vowel(f0: float, dur: float, detune: float = 1.0, vib: float = 0.02) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = f0 * detune * (1.0 + vib * np.sin(2 * np.pi * 5.2 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    # 前三个共振峰近似元音 a
    sig = sum(a * np.sin(k * ph) for k, a in ((1, 1.0), (2, 0.42), (3, 0.22), (4, 0.10)))
    env = np.minimum(1.0, np.minimum(t / 0.04, (dur - t) / 0.06))
    env = np.clip(env, 0, 1)
    return (0.22 * sig * env / 1.74).astype(np.float32)


def make_demo(media: Path) -> dict[str, Path]:
    media.mkdir(parents=True, exist_ok=True)
    n = int(SR * DUR)
    syl = _syllables(12)

    lead = np.zeros(n, np.float32)
    ref = np.zeros(n, np.float32)
    for (t0, d, f0) in syl:
        i = int(t0 * SR)
        seg = _vowel(f0, d)
        lead[i:i + len(seg)] += seg
        # 原唱参照:旋律相同、整体略高、颤音更大（跟原唱时会显示为第二条线）
        seg2 = _vowel(f0, d, detune=1.055, vib=0.035)
        ref[i:i + len(seg2)] += seg2

    # 伴奏:三个和弦铺底 + 每拍一个低频点
    back = np.zeros(n, np.float32)
    tt = np.arange(n) / SR
    for k, chord in enumerate(((261.6, 329.6, 392.0), (220.0, 261.6, 329.6),
                               (174.6, 220.0, 261.6))):
        a, b = k * 3.0, min(DUR, (k + 1) * 3.0)
        m = (tt >= a) & (tt < b)
        for f in chord:
            back[m] += 0.055 * np.sin(2 * np.pi * f * tt[m])
    for beat in np.arange(0.0, DUR, 0.5):
        i = int(beat * SR)
        ln = int(0.12 * SR)
        k = np.arange(ln) / SR
        back[i:i + ln] += (0.16 * np.sin(2 * np.pi * 60 * k) *
                           np.exp(-k * 26)).astype(np.float32)

    paths = {}
    for name, data in (("人声.wav", lead), ("原唱.wav", ref), ("伴奏.wav", back)):
        p = media / name
        sf.write(str(p), data, SR, subtype="PCM_16")
        paths[name] = p
    return paths


# ---------------------------------------------------------------- CDP
class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

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
            d = res["exceptionDetails"]
            raise RuntimeError(d.get("text", "") + " " +
                               str(d.get("exception", {}).get("description", "")))
        return res.get("result", {}).get("value")


async def wait_for(c: CDP, expr: str, cond, timeout: float = 60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = await c.ev(expr)
        except Exception:                      # noqa: BLE001
            v = None
        if cond(v):
            return v
        await asyncio.sleep(0.25)
    return None


async def shot(c: CDP, name: str) -> Path:
    await asyncio.sleep(0.7)                   # 等一次重绘
    r = await c.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    data = base64.b64decode(r["result"]["data"])
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_bytes(data)
    print(f"  · {p.name}  {len(data) // 1024} KB", flush=True)
    return p


# ---------------------------------------------------------------- 主流程
async def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="shots-"))
    data_root = tmp / "data"
    home_root = tmp / "home"
    media = data_root / "projects" / "media"
    (data_root / "projects").mkdir(parents=True, exist_ok=True)
    home_root.mkdir(parents=True, exist_ok=True)
    paths = make_demo(media)

    env = dict(os.environ,
               SINGMIC_PORT=str(PORT),
               SINGMIC_DATA=str(data_root),
               SINGMIC_HOME=str(home_root),
               SINGMIC_NO_ADB="1")
    srv = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "server.py"],
                           cwd=str(ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ud = tmp / "edge"
    edge = None
    try:
        for _ in range(120):
            try:
                if urllib.request.urlopen(BASE + "/studio", timeout=1).status == 200:
                    break
            except Exception:                  # noqa: BLE001
                await asyncio.sleep(0.5)
        else:
            print("服务器没起来")
            return 2

        edge = subprocess.Popen([
            EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
            f"--user-data-dir={ud}", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--mute-audio", "--force-device-scale-factor=1",
            "--window-size=1920,1080", "--hide-scrollbars",
            "--autoplay-policy=no-user-gesture-required", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        tgt = None
        for _ in range(80):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{DBG}/json/new?about:blank",
                                             method="PUT")
                tgt = json.loads(urllib.request.urlopen(req, timeout=2).read())
                break
            except Exception:                  # noqa: BLE001
                await asyncio.sleep(0.4)
        if not tgt:
            print("Edge 没起来")
            return 2

        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=2 ** 26) as ws:
            c = CDP(ws)
            await c.call("Runtime.enable")
            await c.call("Page.enable")
            await c.call("Emulation.setDeviceMetricsOverride",
                         {"width": 1920, "height": 1080, "deviceScaleFactor": 1,
                          "mobile": False})
            await c.call("Page.navigate", {"url": BASE + "/studio"})
            if not await wait_for(c, "typeof STUDIO !== 'undefined' && STUDIO.ready === true",
                                  lambda v: v is True, 60):
                print("页面没起来")
                return 3

            await c.ev("STUDIO.newProject()", await_promise=True)
            await asyncio.sleep(0.5)

            # ★ 路径必须是**相对数据根**的（`projects/media/x.wav`）：
            #   波形解码靠 engine.sourceUrl() 把相对路径翻成 `/api/studio/media/…`，
            #   传绝对路径它就返回空串，clip 永远没有波形。
            paths = {k: f"projects/media/{k}" for k in paths}
            lead, ref, back = paths["人声.wav"], paths["原唱.wav"], paths["伴奏.wav"]
            # ★ addClip 每次都会新开一条轨，新轨落在 tracks 末尾 —— 加完立刻给它改名，
            #   不能等事后按 clip 名猜（clip 上不一定带 name）。
            info = await c.ev(
                "(()=>{const S=STUDIO.S;const add=(nm,path,tn)=>{"
                f"STUDIO.addClip({{kind:'media',name:nm,path:path,dur:{DUR}}},0,null);"
                "const t=S.proj.tracks[S.proj.tracks.length-1];if(t)t.name=tn;};"
                # ★ 直接嵌 json.dumps 的结果（它本身就是合法的 JS 字符串字面量，含双引号）。
                #   别再套 !r —— 那会把引号写进值里，路径变成 "\"projects/media/x.wav\""，
                #   sourceUrl() 认不出来，波形永远是空的。
                f"add('人声.wav',{json.dumps(lead)},'主唱(源)');"
                f"add('伴奏.wav',{json.dumps(back)},'伴奏');"
                f"add('原唱.wav',{json.dumps(ref)},'原唱参照');"
                "S.proj.tracks=S.proj.tracks.filter(t=>(t.clips||[]).length);"
                "return {tracks:S.proj.tracks.map(t=>({id:t.id,name:t.name,n:t.clips.length}))}})()")
            print("  建好演示工程:", info, flush=True)

            # 切成按字/音节的块，再落成一条「块轨」（界面上点「整页编辑」走的就是这条）
            vocal_rel = "projects/media/人声.wav"
            # ★ 关掉识别：合成音调送进 ASR 只会得到一串幻觉文字（实测 12 块全是 "Music"）。
            await c.ev(f"STUDIO.loadBlocks({json.dumps(vocal_rel)}, {{asr:false}})",
                       await_promise=True)
            nb = await c.ev("(()=>{const b=(STUDIO.S.blocks&&STUDIO.S.blocks.blocks)||[];"
                            "return b.filter(x=>x.kind!=='silence').length})()")
            print(f"  切出 {nb} 块", flush=True)
            binfo = await c.ev(
                "(()=>{const S=STUDIO.S;"
                f"const tr=STUDIO.blocksToTrack({json.dumps(vocal_rel)});"
                "if(!tr)return null;"
                "const plain=S.proj.tracks.find(t=>t!==tr&&t.name==='主唱(源)');"
                "if(plain)S.proj.tracks=S.proj.tracks.filter(t=>t!==plain);"
                "tr.name='主唱';"
                "const b=S.proj.tracks.find(t=>t.name==='伴奏'),"
                "      r=S.proj.tracks.find(t=>t.name==='原唱参照');"
                "S.proj.tracks=[tr,b,r].filter(Boolean);"
                "S.selTrack=tr.id;S.sel.clear();"
                "if(tr.clips[0])S.sel.add(tr.clips[0].id);"
                "return {id:tr.id,n:tr.clips.length,"
                "tracks:S.proj.tracks.map(t=>({id:t.id,name:t.name,n:t.clips.length}))}})()")
            print("  块轨:", binfo, flush=True)
            tid0 = (binfo or {}).get("id") or info["tracks"][0]["id"]
            refid = ""
            for t in (binfo or {}).get("tracks", info["tracks"]):
                if t.get("name") == "原唱参照":
                    refid = t["id"]

            # 主唱轨挂一个压缩器 + 均衡，效果页才有参数可看
            await c.ev("(()=>{const S=STUDIO.S;const tr=S.proj.tracks[0];"
                       "S.selTrack=tr.id;STUDIO.openFx(tr.id,null);"
                       "STUDIO.fxAdd('compressor');STUDIO.fxAdd('eq3');"
                       "STUDIO.openFx(tr.id,STUDIO.fxChain()[0].id);})()")
            await asyncio.sleep(0.4)
            await c.ev("document.querySelector('.tabs button[data-tab=\"roll\"]').click()")

            # ★ 存盘再读回来：`loadProject` 会给**每一条**源排波形解码（建工程时只算用到的），
            #   顺带把检查器/混音台刷成最终状态。
            fname = await c.ev("STUDIO.S.file")
            await c.ev("STUDIO.save()", await_promise=True)
            await c.ev(f"STUDIO.load({json.dumps(fname)})", await_promise=True)
            await asyncio.sleep(0.8)

            # 波形是异步解码算的，等算完再拍，否则 clip 是一块纯色
            npeaks = await wait_for(
                c, "STUDIO.S.peaks ? STUDIO.S.peaks.size : 0",
                lambda v: isinstance(v, int) and v >= 3, 25)
            print(f"  波形峰值就绪: {npeaks}", flush=True)

            # 钢琴卷帘面板要有东西可看：从主唱第 5 块建一条真·采样轨，
            # 再摆一串音符（走的是界面上「建采样乐器」同一条路径，不是画上去的）。
            await c.ev(
                "(()=>{const S=STUDIO.S;const tr=S.proj.tracks[0];"
                "const c=tr.clips[Math.min(4,tr.clips.length-1)];"
                "S.selTrack=tr.id;S.sel.clear();S.sel.add(c.id);})()")
            await c.ev("STUDIO.createSamplerFromClip()", await_promise=True, timeout=180)
            smp = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>t.kind==='sampler');"
                "if(!tr)return null;"
                "const m=Math.round((tr.sample&&tr.sample.baseMidi)||60);"
                "const fig=[[0,0],[1,4],[2,7],[3,4],[4,0],[5,2],[6,5],[7,2]];"
                "tr.notes=fig.map((f,i)=>({id:'nt_demo'+i,start:0.35+i*0.52,"
                "length:0.42,midi:m+f[1],vel:0.9}));"
                "S.rollTrack=tr.id;STUDIO.rollScrollTo(m);STUDIO.drawRoll();"
                "return {name:tr.name,n:tr.notes.length,base:m}})()")
            print("  采样轨:", smp, flush=True)

            # 1 主界面：在画布上真点一下第一个块（选中态、检查器都跟着对）
            await c.ev("STUDIO.bigClose&&STUDIO.bigClose()")
            # 横向放大到「整条 9 秒正好铺满时间轴」：默认缩放只占半屏，右边一大片空；
            #   再大就会把伴奏/原唱的尾巴切在视口外。
            await c.ev("(()=>{const S=STUDIO.S;S.view.t0=0;S.view.y=0;"
                       "S.view.pps=144;STUDIO.drawStatic();STUDIO.drawRoll();})()")
            await asyncio.sleep(0.4)
            geo = await c.ev(
                "(()=>{const S=STUDIO.S;const tr=S.proj.tracks[0];"
                "const c=tr.clips[Math.min(3,tr.clips.length-1)];"
                "const r=document.getElementById('plStatic').getBoundingClientRect();"
                "return {x:r.left+168+((c.start+c.length/2)-S.view.t0)*S.view.pps,"
                "        y:r.top+30-S.view.y+46}})()")
            for t, btns in (("mousePressed", 1), ("mouseReleased", 0)):
                await c.call("Input.dispatchMouseEvent",
                             {"type": t, "x": geo["x"], "y": geo["y"],
                              "button": "left", "buttons": btns, "clickCount": 1})
            await asyncio.sleep(0.6)
            await shot(c, "01_主界面.png")

            # 2 整页音高视图 + 原唱参照条
            await c.ev(f"STUDIO.bigOpen({json.dumps(tid0)})", await_promise=True)
            await asyncio.sleep(0.6)
            await c.ev("STUDIO.bigViewSet('pitch')")
            await c.ev("STUDIO.bigRefOpen(true)")
            await asyncio.sleep(0.4)
            await c.ev(f"STUDIO.bigRefPick({json.dumps(refid)})")
            # 真跑一次「跟原唱」分析：出来的是原唱目标线，界面上才看得出这是干什么的
            try:
                await c.ev("STUDIO.bigRefAnalyze()", await_promise=True, timeout=180)
                print("  跟原唱:", await c.ev("STUDIO.bigRefState()"), flush=True)
            except Exception as e:                 # noqa: BLE001
                print("  跟原唱分析失败（照拍）:", e, flush=True)
            await asyncio.sleep(0.5)
            await shot(c, "02_音高曲线.png")
            await c.ev("STUDIO.bigClose()")
            await asyncio.sleep(0.4)

            # 3 效果器页：★ 必须**带槽位 id** 打开（openFx(id,null) 会把选中清掉，
            #   右侧就只剩「选一个插件看参数」一句空话）。
            await c.ev("(()=>{const S=STUDIO.S;const tr=S.proj.tracks[0];"
                       "S.selTrack=tr.id;const ch=STUDIO.fxChain();"
                       "STUDIO.openFx(tr.id,ch.length?ch[0].id:null);})()")
            await asyncio.sleep(0.6)
            await shot(c, "03_效果器链.png")

            # 4 混音台：把底部面板抬高一点，推子与 M/S 才不会被窗口底边切掉
            await c.ev("document.querySelector('.tabs button[data-tab=\"mix\"]').click()")
            await c.ev("document.getElementById('bottom').style.flexBasis='430px'")
            await asyncio.sleep(0.6)
            await shot(c, "04_混音台.png")
            await c.ev("document.getElementById('bottom').style.flexBasis=''")

            # 5 AI 伴奏面板：空表单拍出来像没用过，填一份**自拟歌词**的示例
            #   （示例是原创的四句，不引用任何已发表歌词；API Key 留空，不造假凭据）。
            await c.ev("document.querySelector('.tabs button[data-tab=\"roll\"]').click()")
            await c.ev("document.getElementById('genPanel').hidden=false")
            await c.ev(
                "(()=>{const g=(i)=>document.getElementById(i);"
                "const v=g('genVocal');v.checked=true;"
                "v.dispatchEvent(new Event('change',{bubbles:true}));"
                "const pr=g('genPrompt');"
                "pr.value='流行抒情，72 bpm，钢琴与弦乐铺底，主歌贴脸、副歌加一层和声，4/4 直拍';"
                "pr.dispatchEvent(new Event('input',{bubbles:true}));"
                "const ly=g('genLyrics');"
                "ly.value='[Verse]\\n夜色落在窗台\\n风把旧信翻开\\n"
                "[Chorus]\\n我还在等一个答案\\n等到灯火都变淡';"
                "ly.dispatchEvent(new Event('input',{bubbles:true}));})()")
            await asyncio.sleep(0.6)
            await shot(c, "05_AI伴奏.png")
            await c.ev("document.getElementById('genPanel').hidden=true")

            # 6 设置 → 界面语言
            await c.ev("document.getElementById('settings').hidden=false")
            await asyncio.sleep(0.5)
            await c.ev("(()=>{const e=document.querySelector('#setNav [data-sec=\\'prefs\\']');"
                       "if(e)e.click();})()")
            await asyncio.sleep(0.8)
            await shot(c, "06_设置语言.png")
            await c.ev("document.getElementById('settings').hidden=true")

            print(f"完成，共 {len(list(OUT.glob('*.png')))} 张 → {OUT}")
            return 0
    finally:
        if edge is not None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(edge.pid)],
                           capture_output=True)
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                            f"Where-Object {{ $_.CommandLine -match 'remote-debugging-port={DBG}' }} | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                            "-ErrorAction SilentlyContinue }"], capture_output=True)
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:                      # noqa: BLE001
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
