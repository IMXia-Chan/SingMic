"""端到端:真 Edge(无头)+ CDP,把 Studio 页面跑一遍。

阶段 0/1 的验收:
  页面 200 无报错 → STUDIO 就位 → 建工程 → 加两条 clip → 真点播放(时钟在走)
  → 移动/复制 **渲染数不变** → 改音高 **只发 1 次** → 刷新还原。

跑法(先确保服务在 8765):
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\smoke_studio.py
"""
from __future__ import annotations

import asyncio
import base64
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
# ★ 用别的端口跑(例如 SingMic 正在 8765 上跑着)时:
#   SMOKE_PORT=8766 SMOKE_DBG=9225 python tools/smoke_studio.py
BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8765")
DBG = int(os.environ.get("SMOKE_DBG", "9224"))
# ★ 开跑之前就记下偏好文件在不在 —— 浏览器那一段会自己往盘上写,
#   等测到那一步再判断就永远是「本来就有」,测完也就删不掉了。
PREFS_FILE = (Path(os.environ.get("SINGMIC_HOME") or (Path.home() / ".singmic")) / "prefs.json")
PREFS_HAD = PREFS_FILE.exists()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # 量曲线要借 tuning.yin
MEDIA = ROOT / "projects" / "media"
PROJ = ROOT / "projects"

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
            d = res["exceptionDetails"]
            raise RuntimeError(d.get("text", "") + " " + str(d.get("exception", {}).get("description", "")))
        return res.get("result", {}).get("value")


async def wait_for(c: CDP, expr: str, cond, timeout: float = 60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = await c.ev(expr)
        except Exception:                       # noqa: BLE001
            v = None
        if cond(v):
            return v
        await asyncio.sleep(0.25)
    return None


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


def sm_end(proj: dict) -> float:
    e = 0.0
    for t in proj.get("tracks") or []:
        for c in t.get("clips") or []:
            e = max(e, float(c.get("start", 0)) + float(c.get("length", 0)))
        for n in t.get("notes") or []:
            e = max(e, float(n.get("start", 0)) + float(n.get("length", 0)))
    return e


def http(path: str, method: str = "GET", data: bytes | None = None, ctype: str = ""):
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:         # noqa: F821
        return e.code, e.read()


def http_hdr(path: str, hdrs: dict) -> int:
    """带自定义请求头发一个 GET,只回状态码。用来验 Host / Origin 护栏。"""
    req = urllib.request.Request(BASE + path, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:         # noqa: F821
        return e.code


def ws_status(origin: str, host: str = "") -> int:
    """裸 socket 发一次 WebSocket 握手,回 HTTP 状态码(101 = 接了)。

    ★ 不用 websockets 库:它握手失败直接抛异常,拿不到状态码;
      服务端 `close()` 在 `accept()` 之前发的是 **HTTP 403**,这正是要验的东西。
    """
    hostport = (host or BASE.split("//", 1)[1])
    key = base64.b64encode(b"0123456789abcdef").decode()
    req = (f"GET /ws/ui HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", int(hostport.rsplit(":", 1)[1])), timeout=10) as s:
        s.sendall(req.encode())
        first = s.recv(64).decode("latin-1")
    try:
        return int(first.split(" ")[1])
    except (IndexError, ValueError):
        return 0


def make_media() -> tuple[Path, Path, Path, Path]:
    MEDIA.mkdir(parents=True, exist_ok=True)
    sr = 48000
    t = np.arange(int(sr * 3.0)) / sr
    # 220Hz + 轻微包络,够解码、够画波形
    a = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 - np.exp(-t * 6))).astype(np.float32)
    pa = MEDIA / "_smoke_a.wav"
    sf.write(str(pa), a, sr, subtype="PCM_16")
    b = (0.25 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    pb = MEDIA / "_smoke_b.wav"
    sf.write(str(pb), b, sr, subtype="PCM_16")
    # 一句话:4 个准的 + 1 个偏高 40 音分 + 1 个偏高 150 音分
    def _vowel(f0, sec, cents=0.0):
        f = f0 * 2 ** (cents / 1200.0)
        n = int(sr * sec)
        tt = np.arange(n) / sr
        ph = 2 * np.pi * f * tt
        y = sum((1 / k) * np.sin(k * ph) for k in range(1, 9))
        env = np.minimum(1, tt / 0.03) * np.minimum(1, (sec - tt) / 0.05)
        return (0.22 * y * np.clip(env, 0, 1)).astype(np.float32)
    parts = [np.zeros(int(sr * 0.15), dtype=np.float32)]
    for f0, c in ((261.63, 0), (293.66, 0), (329.63, 0),
                  (261.63, 0), (329.63, 40), (293.66, 150)):
        parts.append(_vowel(f0, 0.45, c))
        parts.append(np.zeros(int(sr * 0.12), dtype=np.float32))
    pc = MEDIA / "_pitchtest.wav"
    sf.write(str(pc), np.concatenate(parts), sr, subtype="PCM_16")

    # C–G–Am–F,给和弦检测用
    def _chord(notes, sec):
        n = int(sr * sec)
        tt = np.arange(n) / sr
        y = np.zeros(n)
        for m in notes:
            f = 440.0 * 2 ** ((m - 69) / 12)
            for k in (1, 2, 3):
                y += (0.12 / k) * np.sin(2 * np.pi * f * k * tt + k)
        env = np.minimum(1, tt / 0.05) * np.minimum(1, (sec - tt) / 0.08)
        return (y * np.clip(env, 0, 1)).astype(np.float32)
    pd = MEDIA / "_chords.wav"
    sf.write(str(pd), np.concatenate([
        _chord([60, 64, 67], 1.6), _chord([55, 59, 62], 1.6),
        _chord([57, 60, 64], 1.6), _chord([53, 57, 60], 1.6)]), sr, subtype="PCM_16")
    return pa, pb, pc, pd


def make_ref_media() -> tuple[Path, Path, Path]:
    """造「原唱」和「我唱的」:同一条旋律,「我唱的」升 3 个半音、开头空 0.8 秒、
    每个音各自偏 ±50 音分。

    ★ 为什么要「各自偏」而不是整体偏:整体偏会被当成换调吸收掉(那正是设计),
      只有**逐音**的偏差才会出「修音」动作。
    ★ 为什么要升 3 个半音:认歌必须扛得住换调 —— 这是 12 个半音轮流试的那条路。
    """
    sr = 48000
    midi = [60, 62, 64, 65, 67, 65, 64, 62, 60, 62, 64, 60]

    def synth(notes, dur):
        n = int(dur * sr)
        y = np.zeros(n, dtype=np.float64)
        for t0, t1, m in notes:
            a, b = int(t0 * sr), min(n, int(t1 * sr))
            if b <= a:
                continue
            seg = np.arange(b - a) / sr
            f0 = 440.0 * 2 ** ((m - 69) / 12.0)
            vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.2 * seg)
            ph = 2 * np.pi * f0 * np.cumsum(vib) / sr
            v = sum((1.0 / k) * np.sin(k * ph) for k in range(1, 6))
            env = np.clip(np.minimum(seg / 0.03, (t1 - t0 - seg) / 0.04), 0, 1)
            y[a:b] += 0.3 * v * env
        return y.astype(np.float32)

    dur = 0.55 * len(midi) + 0.5
    notes = [(i * 0.55, i * 0.55 + 0.48, m) for i, m in enumerate(midi)]
    pr = MEDIA / "_rs_ref.wav"
    sf.write(str(pr), synth(notes, dur), sr, subtype="PCM_16")

    err = [50, -50, 45, -45, 50, -50, 45, -45, 50, -50, 45, -45]
    take = synth([(t0, t1, m + 3 + err[i] / 100.0)
                  for i, (t0, t1, m) in enumerate(notes)], dur)
    pt = MEDIA / "_rs_take.wav"
    sf.write(str(pt), np.concatenate([np.zeros(int(0.8 * sr), np.float32), take]),
             sr, subtype="PCM_16")

    # ★ 另一条**真的不同**的旋律:不能是上面那条的移调,否则「换调也能认」
    #   会把它正确认成同一首,这个用例就白测了。
    other = synth([(i * 0.55, i * 0.55 + 0.48, m) for i, m in
                   enumerate([72, 71, 72, 74, 72, 69, 67, 69, 72, 74, 76, 72])], dur)
    po = MEDIA / "_rs_other.wav"
    sf.write(str(po), other, sr, subtype="PCM_16")
    return pr, pt, po


def make_misc_media() -> tuple[Path, Path, Path]:
    """媒体改名 / 片段音量对账用的两条音频。

    ★ 改名那条**不能**以下划线开头:`/media_list` 会把 `_` 开头的临时文件藏起来,
      而「列表里显示新名字」正是要验的东西。
    """
    sr = 48000
    t = np.arange(int(sr * 2.0)) / sr
    y = (0.30 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    pr = MEDIA / "rnm_test.wav"
    sf.write(str(pr), y, sr, subtype="PCM_16")
    pd = MEDIA / "rnm_dup.wav"
    sf.write(str(pd), y, sr, subtype="PCM_16")
    pc = MEDIA / "_cgain.wav"
    sf.write(str(pc), y, sr, subtype="PCM_16")
    return pr, pd, pc


async def main() -> int:
    pa, pb, pc, pd = make_media()
    rs_ref, rs_take, rs_other = make_ref_media()
    rnm_a, rnm_dup, cgain = make_misc_media()
    a_path = "projects/media/_smoke_a.wav"
    b_path = "projects/media/_smoke_b.wav"

    # ---- 本机服务护栏:Host / Origin ----
    # ★ 验的是「他自己浏览器里的别的网页打不进本机端口」,不是局域网 —— 那是绑地址管的。
    #   ① DNS rebinding 时浏览器发的 Host 还是 evil.com,所以 Host 白名单能挡。
    #   ② 跨站直接 fetch 本机端口:响应读不到(CORS 没开),但 text/plain 的 POST
    #      不预检、副作用照样发生,所以带 Origin 的请求必须校验。
    check("护栏:正常请求(无 Origin)照常 200",
          http_hdr("/api/studio/ui_ping", {}) == 200)
    check("护栏:Host 不是本机 → 403(挡 DNS rebinding)",
          http_hdr("/api/studio/ui_ping", {"Host": "evil.example:8766"}) == 403)
    check("护栏:外站 Origin → 403(挡跨站简单请求)",
          http_hdr("/api/studio/ui_ping", {"Origin": "http://evil.example"}) == 403)
    check("护栏:Origin: null → 403",
          http_hdr("/api/studio/ui_ping", {"Origin": "null"}) == 403)
    check("护栏:本机 Origin 放行",
          http_hdr("/api/studio/ui_ping", {"Origin": BASE}) == 200)
    check("护栏:WebSocket 外站 Origin → 403",
          ws_status("http://evil.example") == 403)
    check("护栏:WebSocket 本机 Origin → 101",
          ws_status(BASE) == 101)

    # ---- 纯 HTTP 的工程 CRUD ----
    # ★ 上一轮要是被中途打断,`_smoke_proj` 会留在盘上、rev 已经不是 0;
    #   不先删掉,第一条 PUT 就撞 409,看着像「保存坏了」。
    http("/api/studio/projects/_smoke_proj", "DELETE")
    body = {
        "version": 1, "rev": 0, "title": "冒烟", "bpm": 128,
        "timeSig": [4, 4], "lengthSec": 30, "sources": {},
        "tracks": [{"id": "tr_x", "name": "主唱", "kind": "voice", "gain": 0.8,
                    "pan": -0.2, "mute": False, "solo": False,
                    "clips": [{"id": "cl_x", "sourceId": "s", "start": 1.25,
                               "srcIn": 0.5, "srcOut": 1.5, "length": 1.0}]}],
        "ui": {"snap": "1/16", "loop": [0, 8], "zoom": 64},
    }
    st, raw = http("/api/studio/projects/_smoke_proj", "PUT",
                   json.dumps(body).encode(), "application/json")
    check("工程 PUT 成功", st == 200, f"HTTP {st}")
    st, raw = http("/api/studio/projects/_smoke_proj")
    got = json.loads(raw)["project"] if st == 200 else {}
    same = (got.get("title") == "冒烟" and got.get("bpm") == 128
            and got.get("tracks", [{}])[0].get("gain") == 0.8
            and got.get("tracks", [{}])[0].get("pan") == -0.2
            and got.get("tracks", [{}])[0].get("clips", [{}])[0].get("start") == 1.25
            and got.get("ui", {}).get("loop") == [0, 8])
    check("PUT → GET 逐字段一致", same, f"{got.get('title')} {got.get('bpm')} {got.get('ui')}")
    st, _ = http("/api/studio/projects/_smoke_proj", "PUT",
                 json.dumps(body).encode(), "application/json")
    check("rev 冲突返回 409", st == 409, f"HTTP {st}")
    st, _ = http("/api/studio/projects/..%2Fevil")
    check("路径穿越被拒", st in (400, 404), f"HTTP {st}")
    st, _ = http("/api/studio/projects/.hidden", "PUT", b"{}", "application/json")
    check("非法工程名被拒", st == 400, f"HTTP {st}")
    st, _ = http("/api/studio/media/nope.exe")
    check("非音频扩展名被拒", st == 404, f"HTTP {st}")
    st, raw = http("/api/studio/upload?name=x.exe", "POST", b"MZ", "application/octet-stream")
    check("上传拒绝 .exe", st == 415, f"HTTP {st}")

    # ---- 删工程:只删工程 JSON(媒体库一个字节都不动) ----
    http("/api/studio/projects/_del_probe", "DELETE")     # 上一次失败留下的,先清掉
    st, _ = http("/api/studio/projects/_del_probe", "PUT",
                 json.dumps(dict(body, title="删我")).encode(), "application/json")
    check("删工程:先建一个探针工程", st == 200, f"HTTP {st}")
    st, _ = http("/api/studio/projects/_del_probe")
    check("删工程:建好了能读", st == 200, f"HTTP {st}")
    st, _ = http("/api/studio/projects/_del_probe", "DELETE")
    check("删工程:DELETE 返回 200", st == 200, f"HTTP {st}")
    st, _ = http("/api/studio/projects/_del_probe")
    check("删工程:删完再读是 404", st == 404, f"HTTP {st}")
    st, raw = http("/api/studio/projects")
    names = [p.get("name") for p in (json.loads(raw).get("projects") or [])]
    check("删工程:列表里也没有了", "_del_probe" not in names, str(names[:5]))
    st, _ = http("/api/studio/projects/_del_probe", "DELETE")
    check("删工程:删不存在的 → 404", st == 404, f"HTTP {st}")
    st, _ = http("/api/studio/projects/.hidden", "DELETE")
    check("删工程:非法名字 → 400", st == 400, f"HTTP {st}")

    # ---- 删录音 / 媒体:先查「谁在用」,再真删 ----
    probe_media = MEDIA / "_del_probe.wav"
    MEDIA.mkdir(parents=True, exist_ok=True)
    sf.write(str(probe_media), np.zeros(4800, dtype=np.float32), 48000)
    probe_rec = ROOT / "recordings" / "_del_probe_voice.wav"
    probe_rec.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(probe_rec), np.zeros(4800, dtype=np.float32), 48000)
    use_body = {
        "version": 1, "rev": 0, "title": "用着它", "bpm": 120,
        "timeSig": [4, 4], "lengthSec": 10,
        "sources": {
            "s_m": {"kind": "audio", "path": "projects/media/_del_probe.wav", "dur": 0.1},
            "s_r": {"kind": "voice", "path": "recordings/_del_probe_voice.wav", "dur": 0.1},
        },
        "tracks": [], "ui": {},
    }
    http("/api/studio/projects/_usage_probe", "DELETE")
    st, _ = http("/api/studio/projects/_usage_probe", "PUT",
                 json.dumps(use_body).encode(), "application/json")
    check("删文件:先建一个引用它的工程", st == 200, f"HTTP {st}")
    for rel, what in (("projects/media/_del_probe.wav", "媒体"),
                      ("recordings/_del_probe_voice.wav", "录音")):
        st, raw = http("/api/studio/usage", "POST",
                       json.dumps({"path": rel}).encode(), "application/json")
        uj = json.loads(raw) if st == 200 else {}
        hit = [p.get("file") for p in (uj.get("projects") or [])]
        check(f"删文件:查得出哪个工程在用({what})",
              st == 200 and "_usage_probe" in hit, str(uj)[:160])
    st, _ = http("/api/studio/usage", "POST",
                 json.dumps({"path": "config.toml"}).encode(), "application/json")
    check("删文件:usage 拒绝媒体库之外的路径", st == 400, f"HTTP {st}")
    st, _ = http("/api/studio/media/_del_probe.wav", "DELETE")
    check("删媒体:DELETE 返回 200", st == 200, f"HTTP {st}")
    check("删媒体:盘上真没了", not probe_media.exists())
    st, _ = http("/api/studio/media/_del_probe.wav", "DELETE")
    check("删媒体:再删一次 → 404", st == 404, f"HTTP {st}")
    st, _ = http("/api/recordings/_del_probe_voice.wav", "DELETE")
    check("删录音:DELETE 返回 200", st == 200, f"HTTP {st}")
    check("删录音:盘上真没了", not probe_rec.exists())
    http("/api/studio/projects/_usage_probe", "DELETE")

    tmp = Path(tempfile.mkdtemp(prefix="st-smoke-"))
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
        f"--user-data-dir={tmp}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio", "--window-size=1500,1000",
        "--autoplay-policy=no-user-gesture-required",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = False
    created_proj = ""
    try:
        version = None
        for _ in range(60):
            try:
                version = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{DBG}/json/version", timeout=1).read())
                break
            except Exception:               # noqa: BLE001
                await asyncio.sleep(0.3)
        if not version:
            print("Edge 没起来")
            return 2

        req = urllib.request.Request(f"http://127.0.0.1:{DBG}/json/new?about:blank", method="PUT")
        tgt = json.loads(urllib.request.urlopen(req, timeout=5).read())
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=2 ** 25) as ws:
            c = CDP(ws)
            await c.call("Runtime.enable")
            await c.call("Log.enable")
            await c.call("Page.enable")
            await c.call("Page.navigate", {"url": BASE + "/studio"})

            got = await wait_for(
                c, "typeof STUDIO !== 'undefined' && STUDIO.ready === true",
                lambda v: v is True, 45)
            check("/studio 打开,STUDIO 就位", bool(got))
            if not got:
                raise RuntimeError("页面没起来:" + " | ".join(js_errors(c)[:3]))

            # 新建一个干净工程
            await c.ev("STUDIO.newProject()", await_promise=True)
            await asyncio.sleep(0.4)
            nm = await c.ev("STUDIO.S.file")
            created_proj = nm if isinstance(nm, str) else ""
            check("新建工程并落盘", isinstance(nm, str) and nm.startswith("proj_"), str(nm))

            # 加两条 clip(走的就是拖放那条代码路径)
            added = await c.ev(
                "(()=>{const S=STUDIO.S;"
                f"STUDIO.addClip({{kind:'media',name:'_smoke_a.wav',path:{json.dumps(a_path)},dur:3}},0,null);"
                f"STUDIO.addClip({{kind:'rec',name:'_smoke_b.wav',path:{json.dumps(b_path)},dur:3,"
                "srcIn:0.5,srcOut:2.0},4,null);"
                "return {tracks:S.proj.tracks.length, clips:S.proj.tracks.reduce((n,t)=>n+t.clips.length,0)}})()")
            check("拖入两条 clip(不同轨)", added and added.get("clips") == 2, str(added))

            # 真点播放按钮 —— 不是调函数,是派发鼠标事件
            box = await c.ev(
                "(()=>{const r=document.getElementById('btnPlay').getBoundingClientRect();"
                "return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
            for t in ("mousePressed", "mouseReleased"):
                await c.call("Input.dispatchMouseEvent",
                             {"type": t, "x": box["x"], "y": box["y"],
                              "button": "left", "buttons": 1 if t == "mousePressed" else 0,
                              "clickCount": 1})
            await asyncio.sleep(0.9)
            clock = await c.ev(
                "(()=>({state:STUDIO.engine.ctx.state, playing:STUDIO.engine.playing,"
                " t:STUDIO.engine.songNow()}))()")
            t1 = clock.get("t", 0) if isinstance(clock, dict) else 0
            await asyncio.sleep(0.8)
            t2 = await c.ev("STUDIO.engine.songNow()")
            check("播放时钟真的在走",
                  isinstance(clock, dict) and clock.get("state") == "running"
                  and clock.get("playing") and t2 - t1 > 0.4,
                  f"{clock} → {t2:.2f}")

            # 移动:零渲染
            r0 = await c.ev("STUDIO_STATS.renders")
            # 直接走界面同一条路:改 start 再重绘(拖动逻辑写的是同一个字段)
            await c.ev("(()=>{const S=STUDIO.S;S.proj.tracks.flatMap(t=>t.clips)[0].start=2.0;"
                       "return true})()")
            await c.ev("STUDIO.save()", await_promise=True)
            r1 = await c.ev("STUDIO_STATS.renders")
            check("移动剪辑不触发渲染", r0 == 0 and r1 == 0, f"renders {r0}→{r1}")

            # ←→ 微调(对齐人声和伴奏用):一格 / Shift 10ms / Alt 1ms,都不触发渲染
            nd = await c.ev(
                "(()=>{const S=STUDIO.S;const c=S.proj.tracks.flatMap(t=>t.clips)[0];"
                "S.sel.clear();S.sel.add(c.id);"
                "const r0=STUDIO_STATS.renders;"
                "const fire=(o)=>{window.dispatchEvent(new KeyboardEvent('keydown',"
                "Object.assign({key:'ArrowRight',bubbles:true},o)));};"
                "let s=c.start;fire({});const d1=c.start-s;"
                "s=c.start;fire({shiftKey:true});const d2=c.start-s;"
                "s=c.start;fire({altKey:true});const d3=c.start-s;"
                "return {d1,d2,d3,r0,r1:STUDIO_STATS.renders}})()")
            check("←→ 微调选中块:一格 / 10ms / 1ms,零渲染",
                  isinstance(nd, dict) and abs(nd.get("d1", 0) - 0.125) < 1e-6
                  and abs(nd.get("d2", 0) - 0.010) < 1e-6
                  and abs(nd.get("d3", 0) - 0.001) < 1e-6
                  and nd.get("r0") == nd.get("r1"), str(nd))

            # 变调:只发 1 次
            await c.ev(
                "(()=>{const S=STUDIO.S;const c=S.proj.tracks.flatMap(t=>t.clips)[1];"
                "S.sel.clear();S.sel.add(c.id);c.semis=2;c.needsRender=true;"
                "return true})()")
            await c.ev("STUDIO.renderPending()", await_promise=True, timeout=120)
            r2 = await c.ev("STUDIO_STATS.renders")
            key = await c.ev("(()=>{const c=STUDIO.S.proj.tracks.flatMap(t=>t.clips)[1];"
                             "return {k:c.renderKey, url:c.renderUrl, need:c.needsRender}})()")
            check("变调 +2 → 恰好 1 次渲染", r2 == 1, f"renders {r1}→{r2}")
            # ★ key 是裸的 20 位 hex;`clip_` 前缀只加在文件名/URL 上
            check("拿回缓存 key 与 URL",
                  isinstance(key, dict) and len(str(key.get("k") or "")) == 20
                  and "/api/studio/cache/clip_" in str(key.get("url")), str(key))
            if key and key.get("url"):
                st, raw = http(key["url"])
                check("缓存 wav 可取且有内容", st == 200 and len(raw) > 1000,
                      f"HTTP {st} {len(raw) if raw else 0} 字节")

            # 复制:仍然不渲染
            await c.ev("STUDIO.duplicateSelected()")
            r3 = await c.ev("STUDIO_STATS.renders")
            check("复制剪辑不触发渲染", r3 == r2, f"renders {r2}→{r3}")

            # ---- 阶段 3:采样池 + 钢琴卷帘 ----
            await c.ev(
                "(()=>{const S=STUDIO.S;const c=S.proj.tracks.flatMap(t=>t.clips)[0];"
                "S.sel.clear();S.sel.add(c.id);return true})()")
            smp = await c.ev("STUDIO.createSamplerFromClip().then(()=>{"
                             "const tr=STUDIO.rollTrack();"
                             "return {name:tr&&tr.name, pool:tr&&tr.sample.pool.length,"
                             " base:tr&&tr.sample.baseMidi, notes:tr&&tr.notes.length,"
                             " lo:tr&&tr.sample.poolLo, hi:tr&&tr.sample.poolHi}})",
                             await_promise=True, timeout=120)
            check("从块建采样池(±7 = 15 条)",
                  isinstance(smp, dict) and smp.get("pool") == 15
                  and smp.get("lo") == -7 and smp.get("hi") == 7, str(smp))

            # 池里每条都能取到,而且 C4 那条的主频对得上
            if smp:
                base = smp.get("base") or 57.0
                semis = int(round(60 - base))
                url = await c.ev(
                    "(()=>{const tr=STUDIO.rollTrack();"
                    f"const e=tr.sample.pool.find(x=>x.semis==={semis});return e&&e.url}})()")
                check("池条目有 URL", isinstance(url, str) and "/cache/clip_" in url, str(url))
                if url:
                    st, wav = http(url)
                    check("池条目 wav 可取", st == 200 and len(wav) > 1000, f"{len(wav)} 字节")
                    if st == 200 and len(wav) > 1000:
                        import io
                        y, sr2 = sf.read(io.BytesIO(wav), dtype="float32")
                        if getattr(y, "ndim", 1) == 2:
                            y = y.mean(axis=1)
                        seg = y[len(y) // 4: len(y) // 4 + 8192]
                        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
                        f_peak = np.fft.rfftfreq(len(seg), 1 / sr2)[int(np.argmax(spec))]
                        want = 440.0 * 2 ** ((60 - 69) / 12)   # C4 = 261.63
                        cents = 1200 * np.log2(max(f_peak, 1) / want)
                        check("C4 采样音高误差 ≤20 音分", abs(cents) <= 20,
                              f"{f_peak:.1f}Hz vs {want:.1f}Hz ({cents:+.1f}¢)")

            # 在卷帘上真的画一个音符(派发真指针事件)
            geo = await c.ev(
                "(()=>{STUDIO.rollScrollTo(60);"
                "const r=document.getElementById('rollCanvas').getBoundingClientRect();"
                "const S=STUDIO.S;const y=r.top+22+(96-60)*12-S.rollY;"
                "const x=r.left+44+(0-S.view.t0)*S.view.pps+8;"
                "return {x,y,before:STUDIO.rollTrack().notes.length}})()")
            for t, btns in (("mousePressed", 1), ("mouseReleased", 0)):
                await c.call("Input.dispatchMouseEvent",
                             {"type": t, "x": geo["x"], "y": geo["y"],
                              "button": "left", "buttons": btns, "clickCount": 1})
            await asyncio.sleep(0.3)
            after_notes = await c.ev("STUDIO.rollTrack().notes.length")
            check("卷帘上画得出音符", after_notes == geo["before"] + 1,
                  f"{geo['before']} → {after_notes}")

            # ---- 阶段 4:撤销重做 / 自动保存 / 独奏 / 对拍 / 复制粘贴 / 右键菜单 ----
            n0 = await c.ev("STUDIO.S.proj.tracks.reduce((n,t)=>n+t.clips.length,0)")
            await c.ev("(()=>{const S=STUDIO.S;const c=S.proj.tracks.flatMap(t=>t.clips)[0];"
                       "S.sel.clear();S.sel.add(c.id);STUDIO.deleteSelected();return true})()")
            n1 = await c.ev("STUDIO.S.proj.tracks.reduce((n,t)=>n+t.clips.length,0)")
            await c.ev("STUDIO.undo()")
            n2 = await c.ev("STUDIO.S.proj.tracks.reduce((n,t)=>n+t.clips.length,0)")
            await c.ev("STUDIO.redo()")
            n3 = await c.ev("STUDIO.S.proj.tracks.reduce((n,t)=>n+t.clips.length,0)")
            await c.ev("STUDIO.undo()")
            check("删除/撤销/重做 全可逆", n1 == n0 - 1 and n2 == n0 and n3 == n0 - 1,
                  f"{n0}→{n1}→{n2}→{n3}")

            # 改 BPM ⇒ 800ms 防抖自动落盘
            await c.ev("(()=>{const el=document.getElementById('inBpm');"
                       "el.value=111;el.dispatchEvent(new Event('change'));return true})()")
            await asyncio.sleep(1.3)
            st, raw = http("/api/studio/projects/" + created_proj)
            saved_bpm = json.loads(raw)["project"].get("bpm") if st == 200 else None
            check("改动后 1 秒内自动落盘", saved_bpm == 111, f"盘上 bpm={saved_bpm}")

            # 独奏:别的轨在混音图上真的被关掉
            solo = await c.ev(
                "(()=>{const S=STUDIO.S,e=STUDIO.engine;"
                "S.proj.tracks.forEach(t=>{t.solo=false;t.mute=false;});"
                "S.proj.tracks[0].solo=true;e.play(S.proj,0);"
                "const ids=S.proj.tracks.map(t=>t.id);"
                "const g=ids.map(i=>{const n=e.trackNodes.get(i);return n?+n.gain.value.toFixed(3):null});"
                "e.pause();S.proj.tracks[0].solo=false;"
                "return {ids,g,target:S.proj.tracks[0].gain}})()")
            gs = solo.get("g") if isinstance(solo, dict) else None
            check("独奏时其他轨增益归零",
                  isinstance(gs, list) and len(gs) > 1 and gs[0] == 1.0
                  and all(x == 0 for x in gs[1:]), str(solo))

            # 对拍:只挪起点,零渲染,落点误差 <1ms
            qz = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.filter(t=>t.kind==='audio').flatMap(t=>t.clips)[0];"
                "c.start=1.37;S.sel.clear();S.sel.add(c.id);"
                "const r0=STUDIO_STATS.renders;"
                "const n=STUDIO.quantizeSelected(1);"
                "const g=(60/S.proj.bpm)/4;"     # 1/16
                "const err=Math.abs(c.start-Math.round(c.start/g)*g);"
                "return {n,start:c.start,g,err,r0,r1:STUDIO_STATS.renders}})()")
            check("对拍到 1/16 网格、误差 <1ms、零渲染",
                  isinstance(qz, dict) and qz.get("n") == 1 and qz.get("err", 1) < 0.001
                  and qz.get("r0") == qz.get("r1"),
                  str(qz))

            # Ctrl+C / Ctrl+V:粘在播放头,渲染数不变
            cp = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.flatMap(t=>t.clips)[0];"
                "S.sel.clear();S.sel.add(c.id);"
                "const n=STUDIO.copySelected();S.playAt=20;"
                "const r0=STUDIO_STATS.renders;const m=STUDIO.pasteClips();"
                "const cs=S.proj.tracks.flatMap(t=>t.clips).filter(x=>x.start>=19);"
                "return {n,m,starts:cs.map(x=>+x.start.toFixed(3)),r0,r1:STUDIO_STATS.renders}})()")
            check("复制粘贴落在播放头且零渲染",
                  isinstance(cp, dict) and cp.get("m") == 1
                  and abs((cp.get("starts") or [0])[0] - 20) < 0.002
                  and cp.get("r0") == cp.get("r1"), str(cp))

            # S 切分:两半长度相加等于原长,普通块零渲染
            sp = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.filter(t=>t.kind==='audio').flatMap(t=>t.clips)"
                ".find(x=>!x.renderKey&&x.length>0.5);"
                "const L=c.length,r0=STUDIO_STATS.renders;"
                "S.playAt=c.start+L/2;S.sel.clear();S.sel.add(c.id);"
                "const n=STUDIO.splitSelected();"
                "const ids=[...S.sel];"
                "const cs=ids.map(i=>S.proj.tracks.flatMap(t=>t.clips).find(x=>x.id===i)).filter(Boolean);"
                "return {n,L,sum:cs.reduce((a,x)=>a+x.length,0),"
                "starts:cs.map(x=>+x.start.toFixed(3)),r0,r1:STUDIO_STATS.renders}})()")
            check("在播放头切分,两半长度守恒且零渲染",
                  isinstance(sp, dict) and sp.get("n") == 1
                  and abs(sp.get("sum", 0) - sp.get("L", 1)) < 1e-4
                  and sp.get("r0") == sp.get("r1"), str(sp))

            # ★ 接缝:真接缝处淡变必须清零
            #   以前整块深拷贝 ⇒ 左半留 20ms 淡出、右半留 8ms 淡入,接缝凭空塌 28ms。
            #   这一条是「切成一块一块能不能听」的底线,不是修饰。
            sm = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const cs=S.proj.tracks.flatMap(t=>t.clips)"
                ".filter(x=>S.sel.has(x.id)).sort((a,b)=>a.start-b.start);"
                "if(cs.length!==2)return {bad:'选中的不是两块:'+cs.length};"
                "return {out:+cs[0].fadeOut,inn:+cs[1].fadeIn}})()")
            check("切分:接缝两边淡变都是 0(不再塌出 28ms 的坑)",
                  isinstance(sm, dict) and sm.get("out") == 0 and sm.get("inn") == 0,
                  str(sm))

            # 反过来:拿掉一半,剩下那块失去接缝伙伴 ⇒ 头/尾淡变要长回 2.5ms
            sd = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const cs=S.proj.tracks.flatMap(t=>t.clips)"
                ".filter(x=>S.sel.has(x.id)).sort((a,b)=>a.start-b.start);"
                "S.sel.clear();S.sel.add(cs[0].id);STUDIO.deleteSelected();"
                "const rest=S.proj.tracks.flatMap(t=>t.clips).find(x=>x.id===cs[1].id);"
                "const inn=rest?+rest.fadeIn:null;STUDIO.undo();"
                "return {n:rest?1:0,inn}})()")
            check("删掉接缝的一半:另一半淡变长回 2.5ms(硬边不留)",
                  isinstance(sd, dict) and sd.get("n") == 1
                  and abs((sd.get("inn") or 0) - 0.0025) < 1e-9, str(sd))

            # ---- 标记(手动打点):加 / 吸附 / 对齐 / 撤销 / 存盘 ----
            mk = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const before=STUDIO.markers().length;"
                "const n0=S.drawStaticN||0;"
                "S.playAt=7.0;"
                "window.dispatchEvent(new KeyboardEvent('keydown',{key:'m',bubbles:true}));"
                "const ms=STUDIO.markers();"
                "return {before,after:ms.length,t:ms.length?ms[ms.length-1].t:null,"
                "n0,n1:S.drawStaticN||0}})()")
            check("按 M 在播放头加标记,画布重画",
                  isinstance(mk, dict) and mk.get("after") == mk.get("before", 0) + 1
                  and mk.get("n0", 1) < mk.get("n1", 0), str(mk))

            snm = await c.ev(
                "(()=>{const m=STUDIO.markers()[0];"
                "return {want:m.t,got:STUDIO.snapT(m.t+0.002)}})()")
            check("靠近标记时吸附到标记上",
                  isinstance(snm, dict)
                  and abs(snm.get("got", 0) - snm.get("want", 1)) < 1e-6, str(snm))

            al = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.filter(t=>t.kind==='audio')"
                ".flatMap(t=>t.clips)[0];"
                "c.start=4;const L=c.length;"
                "const m=STUDIO.markerAdd(10);"
                "S.sel.clear();S.sel.add(c.id);"
                "const r0=STUDIO_STATS.renders;"
                "const n=STUDIO.markerAlign(m.id,5.5);"
                "return {start:c.start,L,len:c.length,n,r0,r1:STUDIO_STATS.renders}})()")
            check("对齐到标记:块里播放头那一处落到标记上,长度与渲染数不变",
                  isinstance(al, dict) and abs(al.get("start", 0) - 8.5) < 0.02
                  and abs(al.get("L", 0) - al.get("len", 1)) < 1e-9
                  and al.get("r0") == al.get("r1"), str(al))

            neg_ = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.filter(t=>t.kind==='audio')"
                ".flatMap(t=>t.clips)[0];"
                "c.start=0.2;S.sel.clear();S.sel.add(c.id);"
                "const m=STUDIO.markerAdd(0);"
                "const n=STUDIO.markerAlign(m.id,0.3);"
                "return {start:c.start,n}})()")
            check("对齐不会把块推到 0 左边",
                  isinstance(neg_, dict) and neg_.get("start", -1) >= -1e-9, str(neg_))

            und = await c.ev(
                "(()=>{STUDIO.undo();"
                "const c=STUDIO.S.proj.tracks.filter(t=>t.kind==='audio')"
                ".flatMap(t=>t.clips)[0];"
                "return {start:c.start}})()")
            check("撤销把这步对齐退回去",
                  isinstance(und, dict) and abs(und.get("start", 0) - 0.2) < 1e-6, str(und))

            foc = await c.ev(
                "(()=>{const n0=STUDIO.markers().length;"
                "const i=document.getElementById('projName');i.focus();"
                "i.dispatchEvent(new KeyboardEvent('keydown',{key:'m',bubbles:true}));"
                "const n1=STUDIO.markers().length;i.blur();return {n0,n1}})()")
            check("输入框里按 M 不会加标记",
                  isinstance(foc, dict) and foc.get("n0") == foc.get("n1"), str(foc))

            await c.ev("STUDIO.save()", await_promise=True)
            per = await c.ev(
                "fetch('/api/studio/projects/'+encodeURIComponent(STUDIO.S.file))"
                ".then(r=>r.json()).then(j=>{const ms=j.project.ui.markers||[];"
                "return {n:ms.length,names:ms.map(m=>m.name)}})", await_promise=True)
            check("标记存进工程(刷新后还在)",
                  isinstance(per, dict) and per.get("n") == 3, str(per))

            # 右键菜单真的弹出来
            rcm = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const a=document.getElementById('playArea').getBoundingClientRect();"
                "const idx=S.proj.tracks.findIndex(t=>t.clips.some(x=>!x.renderKey));"
                "const c=S.proj.tracks[idx].clips.find(x=>!x.renderKey);"
                "const x=a.left+168+(c.start+0.05-S.view.t0)*S.view.pps;"
                "const y=a.top+30+idx*92+46;"
                "document.getElementById('playArea').dispatchEvent(new MouseEvent('contextmenu',"
                "{clientX:x,clientY:y,bubbles:true,cancelable:true}));"
                "const m=document.getElementById('ctx');"
                "const r={on:m.classList.contains('on'),n:m.querySelectorAll('.ci').length,"
                "labels:[...m.querySelectorAll('.ci span:first-child')].map(s=>s.textContent)};"
                "m.classList.remove('on');return r})()")
            check("右键弹出菜单(含复制/切分/对拍)",
                  isinstance(rcm, dict) and rcm.get("on") and rcm.get("n", 0) >= 8
                  and any("切分" in s for s in rcm.get("labels") or []), str(rcm))
            check("右键菜单里有「拆成乐器轨」四轨/六轨两个口",
                  isinstance(rcm, dict)
                  and any("拆成乐器轨（四轨" in s for s in rcm.get("labels") or [])
                  and any("拆成乐器轨（六轨" in s for s in rcm.get("labels") or []),
                  str(rcm.get("labels")) if isinstance(rcm, dict) else "")

            # 混音台:每条轨一条推子 + 总推子,电平表在跑
            mix = await c.ev(
                "(()=>{STUDIO.renderMixer();"
                "const strips=document.querySelectorAll('#mixStrips .strip');"
                "const master=document.querySelector('#mixStrips .strip.master');"
                "const lv=STUDIO.engine.levels();"
                "return {strips:strips.length,tracks:STUDIO.S.proj.tracks.length,"
                "master:!!master,meter:document.querySelectorAll('#mixStrips canvas.meter').length,"
                "lvl:typeof lv.master}})()")
            check("混音台有每轨推子 + 总推子 + 电平表",
                  isinstance(mix, dict) and mix.get("strips") == mix.get("tracks") + 1
                  and mix.get("master") and mix.get("meter") == mix.get("tracks") + 1,
                  str(mix))

            # ---- AI 辅助:跑调检测 → 红点 → 一键只修超标的 ----
            setup = await c.ev(
                "fetch('/api/studio/check_pitch',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({source:'projects/media/_pitchtest.wav'})})"
                ".then(r=>r.json()).then(r=>{"
                "const S=STUDIO.S;const hit=r.blocks.filter(b=>b.status!=='ok');"
                "let k=0;const ids=[];"
                "for(const b of hit){"
                "const c=STUDIO.addClip({kind:'block',name:'_pitchtest.wav',"
                "path:'projects/media/_pitchtest.wav',dur:2.9,"
                "blockId:b.blockId,srcIn:b.t0,srcOut:b.t1},40+k*2,null);"
                "ids.push(c.id);k++;}"
                "S.sel.clear();for(const i of ids)S.sel.add(i);"
                "return {hit:hit.length,types:hit.map(b=>b.status),ids}})",
                await_promise=True, timeout=180)
            check("把该修/不该修的块摆进来", isinstance(setup, dict) and setup.get("hit") == 2,
                  str(setup))
            pchk = await c.ev(
                "STUDIO.checkPitch().then(r=>({root:r&&r.root,mode:r&&r.mode,"
                "red:r&&r.red,yellow:r&&r.yellow,flags:STUDIO.S.pitchFlags.size,"
                "msg:document.getElementById('pitchMsg').textContent}))",
                await_promise=True, timeout=180)
            check("跑调检测:红 1 黄 1,并标到两块上",
                  isinstance(pchk, dict) and pchk.get("red") == 1
                  and pchk.get("yellow") == 1 and pchk.get("flags") == 2,
                  str(pchk))
            fix = await c.ev(
                "STUDIO.applyPitchFix().then(n=>{"
                "const S=STUDIO.S;"
                "const c=S.proj.tracks.flatMap(t=>t.clips).find(x=>x.start===40);"
                "return {n,semis:c.semis,cents:c.cents,key:!!c.renderKey,"
                "need:c.needsRender,renders:STUDIO_STATS.renders}})",
                await_promise=True, timeout=180)
            shift = (fix.get("semis", 0) * 100 + fix.get("cents", 0)) if isinstance(fix, dict) else 0
            check("一键只修超标的:1 块、≈-40 音分、只渲染 1 次",
                  isinstance(fix, dict) and fix.get("n") == 1
                  and abs(shift + 40) <= 15 and fix.get("key") and not fix.get("need"),
                  str(fix))

            # ---- 阶段 6:和声 / 加厚 / 和弦 ----
            har = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.flatMap(t=>t.clips).find(x=>x.start===40);"
                "S.sel.clear();S.sel.add(c.id);"
                "return STUDIO.harmonySelected('third').then(n=>{"
                "const tr=S.proj.tracks.find(t=>t.name==='和声 三度');"
                "if(!tr)return {n};"
                "return {n,gain:tr.gain,pan:tr.pan,"
                "src:{start:c.start,semis:c.semis,note:c.note||''},"
                "h:tr.clips.map(x=>({start:x.start,semis:x.semis,note:x.note||'',key:!!x.renderKey}))}"
                "})})()", await_promise=True, timeout=180)
            ok6 = isinstance(har, dict) and har.get("n") == 1 and har.get("h")
            if ok6:
                h = har["h"][0]
                shift = (h.get("semis") or 0) - (har["src"].get("semis") or 0)
                ok6 = (abs(h["start"] - 40.015) < 1e-6
                       and abs(har["pan"] + 0.35) < 1e-6 and abs(har["gain"] - 0.65) < 1e-6
                       and h.get("key"))
                check("三度和声:新轨 pan -0.35 / 增益 0.65 / 延 15ms",
                      ok6, str({k: har.get(k) for k in ("n", "gain", "pan")}) + f" start={h.get('start')}")
                check("三度落在调内(+3 或 +4 半音)", shift in (3, 4), f"{shift:+d} 半音 ({h.get('note')})")
                # 结果音级必须还在 C 大调里
                PC = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
                      "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}
                nn = str(h.get("note") or "")
                import re as _re
                mm = _re.match(r"([A-G]#?)(-?\d+)", nn)
                okpc = False
                pcx = None
                if mm:                       # note 已经是搬完之后的音名
                    pcx = PC[mm.group(1)]
                    okpc = pcx in (0, 2, 4, 5, 7, 9, 11)
                check("和声音级在 C 大调内", okpc,
                      f"{nn or '(块里没 note)'} {shift:+d} → pc {'?' if pcx is None else pcx}")

            dbl = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.flatMap(t=>t.clips).find(x=>x.start===40.015);"
                "S.sel.clear();S.sel.add(c.id);"
                "const r0=STUDIO_STATS.renders;"
                "return STUDIO.harmonySelected('double').then(n=>{"
                "const A=S.proj.tracks.find(t=>t.name==='加厚 L');"
                "const B=S.proj.tracks.find(t=>t.name==='加厚 R');"
                "return {n,r0,r1:STUDIO_STATS.renders,"
                "A:A&&{g:A.gain,p:A.pan,s:A.clips.map(c=>c.start)},"
                "B:B&&{g:B.gain,p:B.pan,s:B.clips.map(c=>c.start)}}})})()",
                await_promise=True, timeout=180)
            d = dbl if isinstance(dbl, dict) else {}
            okd = (d.get("n") == 2 and d.get("A") and d.get("B")
                   and abs(d["A"]["p"] + 0.5) < 1e-6 and abs(d["B"]["p"] - 0.5) < 1e-6
                   and abs(d["A"]["g"] - 0.5) < 1e-6
                   and abs(d["A"]["s"][0] - 40.027) < 1e-6
                   and abs(d["B"]["s"][0] - 40.036) < 1e-6)
            check("加厚双轨:pan ±0.5 / 增益 0.5 / 延 12ms + 21ms", okd, str(d)[:200])
            check("加厚是同音,一次渲染都不发", d.get("r1") == d.get("r0"),
                  f"renders {d.get('r0')} → {d.get('r1')}")

            chd = await c.ev(
                "(()=>{const S=STUDIO.S;"
                f"const c=STUDIO.addClip({{kind:'media',name:'_chords.wav',path:{json.dumps('projects/media/_chords.wav')},dur:6.4}},60,null);"
                "S.sel.clear();S.sel.add(c.id);"
                "return STUDIO.detectChords().then(r=>({n:r?r.length:0,"
                "names:r?r.map(x=>x.name):[],band:!!(S.chords&&S.chords.length)}))})()",
                await_promise=True, timeout=180)
            names = (chd or {}).get("names") or []
            hitc = sum(1 for i, w in enumerate(("C", "G", "Am", "F"))
                       if i < len(names) and names[i] == w)
            check("C–G–Am–F 在页面里测出 ≥3/4 且画上和弦带",
                  hitc >= 3 and (chd or {}).get("band"), str(names))

            # ---- 阶段 7:块内音高曲线 ----
            cur = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const c=S.proj.tracks.flatMap(t=>t.clips).find(x=>!x.pitchCurve&&"
                "S.proj.sources[x.sourceId]&&"
                "String(S.proj.sources[x.sourceId].path).indexOf('_smoke_a.wav')>=0);"
                "if(!c)return {err:'没找到 _smoke_a 的块'};"
                "c.semis=0;c.cents=0;"
                "c.pitchCurve=[[0,50],[+c.length.toFixed(3),50]];"
                "const r0=STUDIO_STATS.renders;"
                "return STUDIO.renderCurveClip(c).then(r=>({key:r&&r.key,url:r&&r.url,"
                "cached:r&&r.cached,renderSec:r&&r.sec,"
                "drenders:STUDIO_STATS.renders-r0,id:c.id,len:c.length}))})()",
                await_promise=True, timeout=180)
            check("画 +50 音分曲线 → 出缓存 key,恰好渲染 1 次",
                  isinstance(cur, dict) and cur.get("key") and cur.get("drenders") == 1,
                  str({k: cur.get(k) for k in ("key", "cached", "drenders", "len")}) if isinstance(cur, dict) else str(cur))
            if isinstance(cur, dict) and cur.get("url"):
                st, wav = http(cur["url"])
                if st == 200 and wav[:4] == b"RIFF":
                    import io as _io
                    import tuning as _tn
                    yy, srr = sf.read(_io.BytesIO(wav), always_2d=False)
                    mid = yy[int(0.2 * srr):int(0.8 * srr)]
                    f0v, _ap, _lv = _tn.yin(mid, srr)
                    fmeas = float(np.nanmedian(f0v))
                    want = 220.0 * 2 ** (50 / 1200)
                    ec = 1200 * np.log2(fmeas / want)
                    check("曲线渲染实测 +50±5 音分", abs(ec) <= 5,
                          f"{fmeas:.2f}Hz vs {want:.2f}Hz ({ec:+.1f}¢)")
                else:
                    check("曲线缓存 wav 下得下来", False, f"HTTP {st}")
                cur2 = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const c=S.proj.tracks.flatMap(t=>t.clips).find(x=>x.pitchCurve&&x.pitchCurve.length);"
                    "return STUDIO.renderCurveClip(c).then(r=>({cached:r&&r.cached,key:r&&r.key}))})()",
                    await_promise=True, timeout=180)
                check("曲线没改 → 第二次命中缓存", (cur2 or {}).get("cached") is True, str(cur2))

            # ---- 阶段 5:整曲导出 ----
            await c.ev("STUDIO.save()", await_promise=True)
            exp = await c.ev("STUDIO.exportProject('wav')", await_promise=True, timeout=240)
            check("导出 WAV 有结果", isinstance(exp, dict) and "/exports/" in str(exp.get("url")),
                  str({k: exp.get(k) for k in ("file", "sec", "peak_db", "sec_cost")}) if isinstance(exp, dict) else str(exp))
            if isinstance(exp, dict) and exp.get("url"):
                st, wav = http(exp["url"])
                ok_wav = st == 200 and wav[:4] == b"RIFF" and len(wav) > 48000
                check("导出的 WAV 下得下来且是 RIFF", ok_wav, f"HTTP {st} {len(wav)} 字节")
                if ok_wav:
                    import io
                    y, sr_e = sf.read(io.BytesIO(wav), always_2d=True)
                    proj_on_disk = json.loads(
                        http("/api/studio/projects/" + created_proj)[1])["project"]
                    want_sec = sm_end(proj_on_disk)
                    check("导出时长与工程一致(±10ms)",
                          abs(len(y) / sr_e - want_sec) < 0.010,
                          f"{len(y)/sr_e:.3f}s vs 工程 {want_sec:.3f}s")
                    check("导出是立体声 48k", sr_e == 48000 and y.shape[1] == 2,
                          f"{sr_e}Hz × {y.shape[1]}")
                # 清理这次导出的文件
                fn = str(exp.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)

            # ---- 阶段 5b:发行版导出(规格 / 检查 / 标签)----
            await c.ev("STUDIO.openExportPanel()", await_promise=True)
            dflt = await c.ev(
                "(()=>({hid:document.getElementById('expPanel').hidden,"
                "fmt:document.getElementById('expFmt').value,"
                "sr:document.getElementById('expSr').value,"
                "bits:document.getElementById('expBits').value,"
                "loud:document.getElementById('expLoud').value,"
                "title:document.getElementById('expTitle').value}))()")
            check("导出面板打得开,默认 wav/48k/24bit/-14",
                  isinstance(dflt, dict) and dflt.get("hid") is False
                  and dflt.get("fmt") == "wav" and dflt.get("sr") == "48000"
                  and dflt.get("bits") == "24" and dflt.get("loud") == "-14",
                  str(dflt))
            check("歌名默认带出工程名", bool((dflt or {}).get("title")), str(dflt))

            # 传一张封面(jpg),像发 demo 那样带上
            cover_bytes = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                           + b"\xff\xdb\x00\x43\x00" + b"\x08" * 64 + b"\xff\xd9")
            cst, craw = http("/api/studio/cover?name=smoke_cover.jpg", "POST",
                             cover_bytes, "application/octet-stream")
            cover_path = ""
            if cst == 200:
                try:
                    cover_path = json.loads(craw.decode("utf-8")).get("path", "")
                except Exception:              # noqa: BLE001
                    cover_path = ""
            check("封面传得上(存进媒体目录)", bool(cover_path), f"HTTP {cst} {cover_path}")

            # ★ 顺序要紧:先把封面写进 meta、把面板打开(打开时会按工程重置一遍控件),
            #   再改控件 —— 反过来会被 loadExpPanel 盖掉。
            js_set = ("(async()=>{"
                      + (f"STUDIO.S.proj.meta=Object.assign({{}},STUDIO.S.proj.meta,"
                         f"{{cover:{json.dumps(cover_path)}}});" if cover_path else "")
                      + "await STUDIO.openExportPanel();"
                      "document.getElementById('expSr').value='44100';"
                      "document.getElementById('expBits').value='16';"
                      "document.getElementById('expTitle').value='冒烟测试';"
                      "document.getElementById('expArtist').value='Selftest';"
                      "const l=document.getElementById('expLoud');l.value='-14';"
                      "l.dispatchEvent(new Event('change'));"
                      "return STUDIO.exportOptions();})()")
            opts = await c.ev(js_set, await_promise=True)
            check("面板的值真的变成导出选项(44.1k / 16bit / -14)",
                  isinstance(opts, dict) and opts.get("sr") == 44100
                  and opts.get("bits") == 16 and opts.get("loudness") == -14,
                  str(opts))
            check("封面进了导出选项", bool((opts or {}).get("cover")),
                  str((opts or {}).get("cover")))

            chk = await c.ev("STUDIO.exportCheck()", await_promise=True, timeout=240)
            vmap = {v["key"]: v for v in (chk or {}).get("verdicts", [])}
            rep = (chk or {}).get("report") or {}
            check("发行检查量得出来(44.1k 渲染)",
                  isinstance(chk, dict) and rep.get("sr") == 44100, str(rep)[:160])
            check("发行检查:真峰 ≤ -1.0 dBTP",
                  vmap.get("true_peak", {}).get("ok") is True, str(vmap.get("true_peak")))
            check("发行检查:响度落在 -14 ±1",
                  vmap.get("lufs", {}).get("ok") is True, str(vmap.get("lufs")))

            # 导一版 44.1k / 16bit wav:规格与字节都要对
            r16 = await c.ev("STUDIO.exportProject(null,{})", await_promise=True,
                             timeout=240)
            if isinstance(r16, dict) and r16.get("url"):
                st, wav = http(r16["url"])
                if st == 200:
                    import io as _io
                    y16, sr16 = sf.read(_io.BytesIO(wav), always_2d=True)
                    info16 = sf.info(_io.BytesIO(wav))
                    check("wav 按 44.1k / PCM_16 写出来",
                          sr16 == 44100 and info16.subtype == "PCM_16",
                          f"{sr16}Hz {info16.subtype}")
                    check("导出回执带发行规格",
                          r16.get("sr") == 44100 and r16.get("bits") == 16,
                          str({k: r16.get(k) for k in ("sr", "bits", "tagged")}))
                    check("导出回执带发行检查(真峰)",
                          bool((r16.get("check") or {}).get("report")),
                          str((r16.get("check") or {}).get("report"))[:120])
                fn = str(r16.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)

            # 换 mp3:标签要真写进文件(ID3v2.3 + UTF-16 中文)
            await c.ev("(()=>{const e=document.getElementById('expFmt');"
                       "e.value='mp3';e.dispatchEvent(new Event('change'));"
                       "return e.value})()")
            check("换 mp3 后位深框灰掉",
                  await c.ev("document.getElementById('expBits').disabled") is True)
            rmp3 = await c.ev("STUDIO.exportProject(null,{})", await_promise=True,
                              timeout=240)
            if isinstance(rmp3, dict) and rmp3.get("url"):
                st, blob = http(rmp3["url"])
                check("mp3 文件头是 ID3v2.3",
                      st == 200 and blob[:6] == b"ID3\x03\x00\x00",
                      f"HTTP {st} {blob[:10].hex()}")
                check("mp3 里读得到中文标题",
                      b"\xff\xfe" + "冒烟测试".encode("utf-16-le") in blob)
                check("mp3 里带着封面(APIC)",
                      b"APIC" in blob and b"image/jpeg" in blob)
                fn = str(rmp3.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)
            # 交付给唱片公司 = 母带前混音:不压响度、峰留 -6、标签默认不写
            lab = await c.ev(
                "(()=>{const s=document.getElementById('expTarget');"
                "s.value='label';s.dispatchEvent(new Event('change'));"
                "const o=STUDIO.exportOptions();"
                "return {o:o,loud:document.getElementById('expLoud').value,"
                "embed:document.getElementById('expEmbed').checked,"
                "bits:document.getElementById('expBits').value,"
                "note:document.getElementById('expTargetNote').textContent};})()")
            check("选「交给唱片公司」→ 响度关、余量 -6、标签默认不写",
                  isinstance(lab, dict) and lab.get("loud") == "off"
                  and (lab.get("o") or {}).get("peak") == -6
                  and lab.get("embed") is False
                  and (lab.get("o") or {}).get("loudness") is None,
                  str(lab))
            chk2 = await c.ev("STUDIO.exportCheck()", await_promise=True, timeout=240)
            rep2 = (chk2 or {}).get("report") or {}
            vk2 = {v["key"] for v in (chk2 or {}).get("verdicts", [])}
            # ★ 量真峰:余量是给后级的,采样峰比它低零点几 dB 是正常的
            check("母带前混音:真峰落在 -6 ±0.2 dBTP",
                  abs(float(rep2.get("true_peak_db", 99)) + 6.0) <= 0.2,
                  str(rep2.get("true_peak_db")))
            check("母带前混音不判响度(没有 lufs 这一项)", "lufs" not in vk2,
                  str(sorted(vk2)))
            await c.ev("(()=>{const s=document.getElementById('expTarget');"
                       "s.value='self';s.dispatchEvent(new Event('change'));"
                       "return 1})()")
            check("选回「自己发行」→ 响度回到 -14、标签重新勾上",
                  (await c.ev("STUDIO.exportOptions()")).get("loudness") == -14
                  and await c.ev("document.getElementById('expEmbed').checked") is True)
            await c.ev("document.getElementById('expPanel').hidden=true")

            # ---- 阶段 8:每轨混响/延迟 + 母带 ----
            fxr = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "tr.fx={reverb:{mix:0.35,size:1.2},delay:{time:0.3,fb:0.25,mix:0.15}};"
                "STUDIO.engine.setTrackFx(tr);STUDIO.renderMixer();"
                "const n=STUDIO.engine.trackFx.get(tr.id)||{};"
                "return {rv:!!n.rv,dl:!!n.dl,"
                "ir:n.rv?n.rv.conv.buffer.duration:0};})()")
            check("混响/延迟节点按需建出来(IR 长 1.2s)",
                  (fxr or {}).get("rv") and (fxr or {}).get("dl")
                  and abs(float((fxr or {}).get("ir") or 0) - 1.2) < 0.02, str(fxr))
            await c.ev("STUDIO.save()", await_promise=True)

            async def export_loud(label: str) -> dict | None:
                r = await c.ev("STUDIO.exportProject('wav')", await_promise=True, timeout=240)
                if not isinstance(r, dict) or not r.get("url"):
                    check(label, False, str(r)[:160])
                    return None
                st, wav = http(r["url"])
                fn = str(r.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)
                if st != 200:
                    check(label, False, f"HTTP {st}")
                    return None
                import io
                import studio_mix as smx
                y, sr_e = sf.read(io.BytesIO(wav), always_2d=True)
                r["_lufs"] = smx.integrated_lufs(y, sr_e)
                r["_peak"] = float(np.max(np.abs(y)))
                r["_y"] = y[: sr_e * 10]
                return r

            e14 = await export_loud("默认 −14 LUFS 导出")
            if e14:
                check("导出响度 −14 ±1 LUFS", abs(e14["_lufs"] + 14.0) <= 1.0,
                      f"{e14['_lufs']:.2f} LUFS · 峰值 {e14['_peak']:.3f}")
                check("限幅后峰值 ≤ 1 且不削平", e14["_peak"] <= 1.0
                      and e14["_peak"] < 10 ** (-0.25 / 20),
                      f"{20*np.log10(max(e14['_peak'],1e-9)):+.2f} dBFS")

            await c.ev("document.getElementById('selLoud').value='-9'")
            e9 = await export_loud("−9 LUFS 导出")
            if e9:
                check("切到 −9 LUFS 后实测 −9 ±1", abs(e9["_lufs"] + 9.0) <= 1.0,
                      f"{e9['_lufs']:.2f} LUFS")

            await c.ev("document.getElementById('selLoud').value='off'")
            eoff = await export_loud("关母带导出")
            if eoff:
                check("关掉母带 → 不走 LUFS 归一",
                      not eoff.get("loudness"), str(eoff.get("loudness")))
            await c.ev("document.getElementById('selLoud').value='-14'")

            # 混响真的进了导出:同一工程关 FX 再导一次,尾巴能量必须差出很多
            fxo = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "S._fxSave=tr.fx;delete tr.fx;"
                "return STUDIO.save().then(()=>true,()=>false);})()",
                await_promise=True)
            eo = None
            if fxo:
                eo = await export_loud("关 FX 导出")
            restored = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "if(!S._fxSave)return false;"
                "tr.fx=S._fxSave;delete S._fxSave;"
                "return STUDIO.save().then(()=>true,()=>false);})()",
                await_promise=True)
            check("FX 参数能存能还原", restored is True and fxo is True, str(restored))
            if e14 and eo:
                # 两边都归一化了,比响度分不出来 —— 比起波形差异
                a, b = e14["_y"], eo["_y"]
                m = min(len(a), len(b))
                d = np.sqrt(np.mean((a[:m] - b[:m]) ** 2))
                s = np.sqrt(np.mean(b[:m] ** 2)) or 1e-9
                check("开 FX 和关 FX 的导出声确实不同", d / s > 0.02,
                      f"差异 {d/s*100:.1f}% RMS")

            # 刷新后 FX 参数还在
            await c.ev("STUDIO.save()", await_promise=True)

            # ---- 阶段 10:内置插件链(FL 式插入效果)----
            fready = await wait_for(
                c, "STUDIO.fxStats ? STUDIO.fxStats() : null",
                lambda v: bool(v and v.get("ready")), 45)
            check("效果器 worklet 加载成功", bool(fready),
                  str((fready or {}).get("error") if isinstance(fready, dict) else fready))

            fadd = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "S.selTrack=tr.id;STUDIO.openFx(tr.id,null);"
                "const ok=STUDIO.fxAdd('compressor');"
                "const ch=STUDIO.fxChain();"
                "return {ok:ok,len:ch.length,id:ch[0]&&ch[0].id,type:ch[0]&&ch[0].type,"
                "track:tr.id};})()")
            check("加一个压缩插件:链长 1",
                  bool(fadd) and fadd.get("ok") and fadd.get("len") == 1
                  and fadd.get("type") == "compressor", str(fadd))

            await asyncio.sleep(0.25)
            fist = await c.ev("STUDIO.fxStats()")
            check("worklet 插入节点已建立",
                  bool(fist) and fist.get("inserts", 0) >= 1
                  and fist.get("chains", {}).get((fadd or {}).get("track"), 0) >= 1,
                  str(fist))

            fops = await c.ev(
                "(()=>{const S=STUDIO.S;const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "const out={};"
                "STUDIO.fxAdd('filter');out.afterAdd=STUDIO.fxChain().length;"
                "const fid=STUDIO.fxChain()[1].id;STUDIO.openFx(tr.id,fid);"
                "const sl=document.querySelector('#fxParams input[type=range]');"
                "if(sl){sl.value='700';sl.dispatchEvent(new Event('input',{bubbles:true}));}"
                "out.cutoff=STUDIO.fxChain()[1].params.cutoff;"
                "const s1=document.querySelectorAll('#fxSlots .fxslot');"
                "s1[1].querySelector('button[data-a=\"on\"]').click();"
                "out.bypass=STUDIO.fxChain()[1].enabled===false;"
                "STUDIO.renderFxPanel();"
                "const s2=document.querySelectorAll('#fxSlots .fxslot');"
                "s2[1].querySelector('button[data-a=\"up\"]').click();"
                "out.order=STUDIO.fxChain().map(s=>s.type).join(',');"
                "const s3=document.querySelectorAll('#fxSlots .fxslot');"
                "s3[0].querySelector('button[data-a=\"del\"]').click();"
                "out.afterDel=STUDIO.fxChain().length;"
                "STUDIO.undo();out.afterUndo=STUDIO.fxChain().length;"
                "out.slots=STUDIO.fxSlots();"
                "return out;})()")
            fops = fops or {}
            check("加滤波 → 滑杆改参数生效",
                  fops.get("afterAdd") == 2 and isinstance(fops.get("cutoff"), (int, float))
                  and abs(float(fops.get("cutoff", 1000)) - 1000) > 10, str(fops))
            check("旁通按钮生效", fops.get("bypass") is True, str(fops))
            check("上移真的换了处理顺序", fops.get("order") == "filter,compressor", str(fops))
            check("删除后链短一条", fops.get("afterDel") == 1, str(fops))
            check("撤销把删掉的插件还原", fops.get("afterUndo") == 2, str(fops))
            check("效果器面板画出槽位", (fops.get("slots") or 0) == 2,
                  f"slots={fops.get('slots')}")

            # 加一条空轨 → 改名 → 撤销(和双击轨名走的是同一个字段)
            trk = await c.ev(
                "(()=>{const S=STUDIO.S;const n0=S.proj.tracks.length;"
                "STUDIO.addTrack();const n1=S.proj.tracks.length;"
                "S.proj.tracks[n1-1].name='吉他';STUDIO.renderMixer();STUDIO.drawStatic();"
                "const got=S.proj.tracks[n1-1].name;"
                "STUDIO.undo();return {n0,n1,got,n2:S.proj.tracks.length}})()")
            check("加一条空轨 + 改名 + 撤销",
                  isinstance(trk, dict) and trk.get("n1") == (trk.get("n0") or 0) + 1
                  and trk.get("got") == "吉他" and trk.get("n2") == trk.get("n0"), str(trk))

            # 播放穿过插件链:有电平、不报错
            # ★ 前面切过块、挪过位置,固定时间点不一定踩在音频上 —— 从最早那个块起,
            #   往后挪几个位置各试一次;电平表要到「真的有声」(>1e-3),不是 >0 就算过
            flv = None
            for off in (0.3, 0.6, 1.0, 1.4):
                await c.ev(f"(()=>{{const S=STUDIO.S;"
                           f"const st=S.proj.tracks.flatMap(t=>(t.clips||[]).map(c=>c.start));"
                           f"STUDIO.seekTo((st.length?Math.min(...st):0)+{off});"
                           f"if(!STUDIO.engine.playing)STUDIO.play();"
                           f"if(!STUDIO.engine.playing)STUDIO.play();}})()")
                await asyncio.sleep(0.6)
                flv = await c.ev("STUDIO.engine.levels()")
                if flv and float(flv.get("master") or 0) > 1e-3:
                    break
            await c.ev("STUDIO.engine.pause()")
            check("带插件链播放有电平",
                  bool(flv) and float((flv or {}).get("master") or 0) > 1e-3, str(flv))

            # 带链导出
            fexp = await c.ev("STUDIO.exportProject('wav')", await_promise=True, timeout=240)
            check("带插件链导出 WAV 成功",
                  isinstance(fexp, dict) and "/exports/" in str(fexp.get("url")),
                  str({k: (fexp or {}).get(k) for k in ("file", "sec", "sec_cost")}))
            if isinstance(fexp, dict) and fexp.get("url"):
                st, wav = http(fexp["url"])
                check("带链导出的文件是 RIFF 且非空",
                      st == 200 and wav[:4] == b"RIFF" and len(wav) > 48000,
                      f"HTTP {st} {len(wav)} 字节")
                fn = str(fexp.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)

            # 分轨 ZIP
            stz = await c.ev("STUDIO.exportStems('pre')", await_promise=True, timeout=240)
            check("分轨导出返回 zip",
                  isinstance(stz, dict) and stz.get("format") == "zip"
                  and str(stz.get("file", "")).endswith(".zip"),
                  str({k: (stz or {}).get(k) for k in ("file", "tracks", "stems")}))
            if isinstance(stz, dict) and stz.get("url"):
                st, rawz = http(stz["url"])
                import io as _io
                import zipfile as _zip
                names, wavs = [], []
                if st == 200 and rawz[:2] == b"PK":
                    with _zip.ZipFile(_io.BytesIO(rawz)) as zf:
                        names = zf.namelist()
                        wavs = [n for n in names if n.lower().endswith(".wav")]
                        head = zf.read(wavs[0])[:4] if wavs else b""
                else:
                    head = b""
                check("分轨 zip 能解出 wav + 清单",
                      head == b"RIFF" and "manifest.json" in names
                      and "project.json" in names and "README.txt" in names,
                      f"HTTP {st} · {names[:6]}")
                check("分轨条数 = 报告条数", len(wavs) == int(stz.get("tracks") or -1),
                      f"{len(wavs)} vs {stz.get('tracks')}")
                fn = str(stz.get("file") or "")
                if fn:
                    (ROOT / "projects" / "exports" / fn).unlink(missing_ok=True)

            # 浏览器 OfflineAudioContext(同一个 worklet)+ Python numpy:逐点必须一致
            par = await c.ev(
                "(async()=>{const ctx=new OfflineAudioContext(1,4800,48000);"
                "await ctx.audioWorklet.addModule('/static/studio/fxworklet.js');"
                "const n=new AudioWorkletNode(ctx,'singmic-insert',{numberOfInputs:1,"
                "numberOfOutputs:1,outputChannelCount:[1],channelCount:1,"
                "channelCountMode:'explicit'});"
                "n.port.postMessage({type:'chain',chain:["
                "{id:'a',type:'filter',params:{mode:'lowpass',cutoff:1000,q:0.707}},"
                "{id:'b',type:'compressor',params:{threshold:-18,ratio:3,attack:12,"
                "release:140,knee:6,makeup:0}},"
                "{id:'c',type:'bitcrush',params:{bits:6,crushHz:6000,mix:1}}]});"
                "await new Promise(r=>setTimeout(r,60));"
                "const buf=ctx.createBuffer(1,4800,48000);const d=buf.getChannelData(0);"
                "let s=7;for(let i=0;i<4800;i++){s=(Math.imul(s,1664525)+1013904223)|0;"
                "d[i]=((s>>>0)/4294967296*2-1)*0.3;}"
                "const src=ctx.createBufferSource();src.buffer=buf;"
                "src.connect(n);n.connect(ctx.destination);src.start();"
                "const out=await ctx.startRendering();const y=out.getChannelData(0);"
                "let rms=0;for(const v of y)rms+=v*v;"
                "return {samples:Array.from(y.slice(0,64)),tail:Array.from(y.slice(-64)),"
                "rms:Math.sqrt(rms/y.length)};})()",
                await_promise=True, timeout=90)
            if isinstance(par, dict) and par.get("samples"):
                import studio_fx as sfx
                _s = 7
                pin = np.empty(4800, dtype=np.float32)
                for _i in range(4800):
                    _s = (_s * 1664525 + 1013904223) & 0xFFFFFFFF
                    pin[_i] = np.float32(((_s / 4294967296.0) * 2 - 1) * 0.3)
                _py = sfx.apply_chain(pin, 48000, [
                    {"id": "a", "type": "filter",
                     "params": {"mode": "lowpass", "cutoff": 1000, "q": 0.707}},
                    {"id": "b", "type": "compressor",
                     "params": {"threshold": -18, "ratio": 3, "attack": 12,
                                "release": 140, "knee": 6, "makeup": 0}},
                    {"id": "c", "type": "bitcrush",
                     "params": {"bits": 6, "crushHz": 6000, "mix": 1}},
                ]).astype(np.float64)
                _e0 = float(np.max(np.abs(_py[:64] - np.array(par["samples"]))))
                _et = float(np.max(np.abs(_py[-64:] - np.array(par["tail"]))))
                _er = abs(float(np.sqrt(np.mean(_py ** 2))) - float(par.get("rms") or 0))
                check("浏览器 worklet ↔ numpy 逐点一致(<1e-4)",
                      max(_e0, _et) < 1e-4 and _er < 1e-4,
                      f"head {_e0:.1e} · tail {_et:.1e} · rms {_er:.1e}")
            else:
                check("浏览器 worklet ↔ numpy 逐点一致(<1e-4)", False, str(par)[:160])

            # 截图:断言证明不了「画得对不对」
            await c.ev("STUDIO.seekTo(1.0); STUDIO.engine.pause();")
            await asyncio.sleep(0.4)
            await c.ev("document.querySelector('.tabs button[data-tab=\"mix\"]').click()")
            await asyncio.sleep(0.35)
            shot = await c.call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("result", {}).get("data")
            if data:
                (ROOT / "_shot_studio_mix.png").write_bytes(base64.b64decode(data))
            await c.ev("document.querySelector('.tabs button[data-tab=\"roll\"]').click()")
            await asyncio.sleep(0.2)
            shot = await c.call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("result", {}).get("data")
            if data:
                (ROOT / "_shot_studio.png").write_bytes(base64.b64decode(data))

            # 刷新还原
            before = await c.ev(
                "(()=>{const S=STUDIO.S;return {bpm:S.proj.bpm,"
                "clips:S.proj.tracks.reduce((n,t)=>n+t.clips.length,0),"
                "file:S.file, semis:(S.proj.tracks.flatMap(t=>t.clips).find(x=>x.renderKey)||{}).semis,"
                "gain:S.proj.tracks[0].gain, loop:S.proj.ui.loop,"
                "fx:(S.proj.tracks.find(t=>t.fx)||{}).fx}})()")
            await c.ev("STUDIO.save()", await_promise=True)
            await c.call("Page.reload")
            await wait_for(c, "typeof STUDIO !== 'undefined' && STUDIO.ready === true",
                           lambda v: v is True, 45)
            await asyncio.sleep(0.6)
            after = await c.ev(
                "(()=>{const S=STUDIO.S;return {bpm:S.proj.bpm,"
                "clips:S.proj.tracks.reduce((n,t)=>n+t.clips.length,0),"
                "file:S.file, semis:(S.proj.tracks.flatMap(t=>t.clips).find(x=>x.renderKey)||{}).semis,"
                "gain:S.proj.tracks[0].gain, loop:S.proj.ui.loop,"
                "fx:(S.proj.tracks.find(t=>t.fx)||{}).fx}})()")
            check("刷新后工程逐字段还原", before == after, f"{before} vs {after}")

            # ---- 歌库 + 跟原唱(本机知识库:认歌 / 第二次秒开 / 应用可撤销) ----
            # ★ 歌库按内容指纹存,同一段音频跑第二遍就会命中上一遍的记录,
            #   「第一次听就存下」必然失败。跑之前先清掉本次测试用的几条(只删 _rs_ 开头的)。
            try:
                _lib = json.loads(urllib.request.urlopen(
                    BASE + "/api/studio/reference/library", timeout=10).read())
                for _e in (_lib.get("entries") or []):
                    if str(_e.get("name") or "").startswith("_rs_"):
                        _rq = urllib.request.Request(
                            BASE + "/api/studio/reference/library/" + _e["id"],
                            method="DELETE")
                        urllib.request.urlopen(_rq, timeout=10).read()
            except Exception:      # noqa: BLE001  清不掉就让断言如实失败,不掩盖
                pass

            rs = await c.ev(
                "(()=>{const A=(n,d)=>STUDIO.addClip({kind:'media',name:n,"
                "path:'projects/media/'+n,dur:d},0,null);"
                "A('_rs_ref.wav',7.1);A('_rs_take.wav',7.9);A('_rs_other.wav',7.1);"
                "const S=STUDIO.S;"
                "const out=S.proj.tracks.map(t=>{const c=t.clips[0];"
                "const s=c&&S.proj.sources[c.sourceId];"
                "return {id:t.id,name:s&&s.name,n:t.clips.length};})"
                ".filter(o=>o.name&&o.name.indexOf('_rs_')===0);"
                "const t=out.find(o=>o.name==='_rs_take.wav');"
                "if(t){const tr=S.proj.tracks.find(x=>x.id===t.id);"
                "tr.clips[0].start=2.0;STUDIO.drawStatic();}"
                "return out;})()")
            if not (isinstance(rs, list) and len(rs) == 3
                    and all(o.get("n") == 1 for o in rs)):
                check("歌库:三条测试轨都建起来了", False, str(rs)[:200])
                ri = ti = oi = None
            else:
                byn = {o["name"]: o for o in rs}
                ri, ti, oi = byn["_rs_ref.wav"]["id"], \
                    byn["_rs_take.wav"]["id"], byn["_rs_other.wav"]["id"]
                check("歌库:三条测试轨都建起来了", True, ", ".join(byn))

            if ri:
                # ★ 先开面板:下拉的选项是 refOpen 里才填的 —— 反过来设 .value 会落空
                opened = await c.ev(
                    "(()=>{STUDIO.refOpen();"
                    "return {open:!document.getElementById('refPanel').hidden,"
                    "a:document.getElementById('refRef').value,"
                    "b:document.getElementById('refTake').value};})()")
                check("跟原唱:面板打得开", bool((opened or {}).get("open")), str(opened))
                check("跟原唱:首次打开时两条下拉不是同一条轨",
                      (opened or {}).get("a") != (opened or {}).get("b"),
                      str(opened))
                pick = await c.ev(f"STUDIO.refTrackPick({json.dumps(ri)},{json.dumps(ti)})")
                check("跟原唱:选好「原唱轨 / 我的轨」", pick == [ri, ti], str(pick))

                t0 = time.time()
                plan1 = await c.ev("STUDIO.refAnalyze()",
                                   await_promise=True, timeout=180)
                dt1 = time.time() - t0
                acts1 = (plan1 or {}).get("actions") or []
                db1 = (plan1 or {}).get("refdb") or {}
                check("跟原唱:分析出方案", bool(plan1 and plan1.get("ok")) and len(acts1) > 0,
                      f"{len(acts1)} 项 · 对上 {float((plan1 or {}).get('coverage') or 0)*100:.0f}%"
                      f" · {dt1:.2f}s")
                check("歌库:第一次听就存下", db1.get("hit") == "new"
                      and db1.get("name") == "_rs_ref.wav", str(db1))
                check("歌库:提示「第一次听到」", any("第一次听到这首歌" in n
                                                   for n in (plan1 or {}).get("notes") or []),
                      " | ".join((plan1 or {}).get("notes") or [])[:160])
                check("修音动作是逐点曲线(每音各自偏)",
                      any(a.get("kind") == "pitch" for a in acts1),
                      str([a.get("kind") for a in acts1]))

                idr = await c.ev("STUDIO.refIdentify()",
                                 await_promise=True, timeout=180)
                best = (idr or {}).get("best")
                check("认歌:换调 + 错位 0.8 秒还认得出来",
                      bool(best) and best.get("name") == "_rs_ref.wav"
                      and float(best.get("agree") or 0) >= 0.72, str(best)[:200])

                await c.ev(f"STUDIO.refTrackPick({json.dumps(ri)},{json.dumps(oi)})")
                idr2 = await c.ev("STUDIO.refIdentify()",
                                  await_promise=True, timeout=180)
                best2 = (idr2 or {}).get("best")
                check("认歌:另一首歌不会被认成这首",
                      not best2 or best2.get("name") != "_rs_ref.wav",
                      str(best2)[:200])
                await c.ev(f"STUDIO.refTrackPick({json.dumps(ri)},{json.dumps(ti)})")

                t0 = time.time()
                plan2 = await c.ev("STUDIO.refAnalyze()",
                                   await_promise=True, timeout=180)
                dt2 = time.time() - t0
                db2 = (plan2 or {}).get("refdb") or {}
                check("歌库:第二次分析读存下的音高地图",
                      db2.get("hit") in ("path", "content") and db2.get("name") == "_rs_ref.wav",
                      f"hit={db2.get('hit')} · {dt1:.2f}s → {dt2:.2f}s")
                check("歌库:提示「这首歌认识」",
                      any("这首歌认识" in n for n in (plan2 or {}).get("notes") or []),
                      " | ".join((plan2 or {}).get("notes") or [])[:160])

                snap_before = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"const t=S.proj.tracks.find(x=>x.id==={json.dumps(ti)});"
                    "return t.clips.map(c=>({id:c.id,start:c.start,semis:c.semis||0,"
                    "cents:c.cents||0,curve:(c.pitchCurve||[]).length}));})()")
                done = await c.ev("STUDIO.refApply()",
                                  await_promise=True, timeout=240)
                snap_after = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"const t=S.proj.tracks.find(x=>x.id==={json.dumps(ti)});"
                    "return t.clips.map(c=>({id:c.id,start:c.start,semis:c.semis||0,"
                    "cents:c.cents||0,curve:(c.pitchCurve||[]).length,"
                    "key:!!c.renderKey}));})()")
                a0 = (snap_after or [{}])[0] if snap_after else {}
                b0 = (snap_before or [{}])[0] if snap_before else {}
                touched = (a0.get("curve", 0) > 0 or a0.get("semis", 0) != b0.get("semis", 0)
                           or a0.get("cents", 0) != b0.get("cents", 0)
                           or a0.get("start") != b0.get("start"))
                check("跟原唱:应用真的改了「我的轨」", int(done or 0) > 0 and touched,
                      f"应用 {done} 项 · {b0} → {a0}")
                check("跟原唱:改完的音重新渲染了", bool(a0.get("key")), str(a0))

                back = await c.ev(
                    "(()=>{STUDIO.undo();const S=STUDIO.S;"
                    f"const t=S.proj.tracks.find(x=>x.id==={json.dumps(ti)});"
                    "return t.clips.map(c=>({id:c.id,start:c.start,semis:c.semis||0,"
                    "cents:c.cents||0,curve:(c.pitchCurve||[]).length}));})()")
                check("跟原唱:一次 Ctrl+Z 全部还原", back == snap_before,
                      f"{snap_before} vs {back}")

                lib = await c.ev("STUDIO.refLibrary()", await_promise=True)
                names = [e.get("name") for e in ((lib or {}).get("entries") or [])]
                check("歌库:列表里有这首歌", "_rs_ref.wav" in names,
                      f"{lib.get('count') if isinstance(lib, dict) else '?'} 首")

            # ---- 一键调音:测量 → 面板 → 应用 → 一次 Ctrl+Z 全还原 ----
            am = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const A=(n,d)=>{STUDIO.addClip({kind:'media',name:n,"
                "path:'projects/media/'+n,dur:d},0,null);"
                "return S.proj.tracks.find(x=>x.id===S.selTrack);};"
                "const tv=A('_rs_take.wav',7.9), tb=A('_rs_ref.wav',7.1);"
                "tv.name='主唱';tv.kind='voice';tv.gain=0.4;"
                "tb.name='伴奏';tb.kind='audio';"
                "window.__amAll=S.proj.tracks;"        # 只测这两条,别把整台工程都量一遍
                "S.proj.tracks=[tv,tb];"
                "return {v:tv.id,b:tb.id};})()")
            if not (isinstance(am, dict) and am.get("v") and am.get("b")):
                check("一键调音:测试轨建起来了", False, str(am))
            else:
                vId, bId = am["v"], am["b"]
                try:
                    plan = await c.ev("STUDIO.autoMix()",
                                      await_promise=True, timeout=300)
                finally:
                    restored = await c.ev(
                        "(()=>{const S=STUDIO.S;S.proj.tracks=window.__amAll;"
                        "delete window.__amAll;STUDIO.drawStatic();"
                        "return S.proj.tracks.length;})()")
                pan_open = await c.ev(
                    "!document.getElementById('autoPanel').hidden")
                acts = (plan or {}).get("actions") or []
                gains = [a for a in acts if a.get("kind") == "gain"
                         and a.get("trackId") == vId]
                check("一键调音:面板打开、给出方案",
                      bool(pan_open) and len(acts) > 0,
                      f"{len(acts)} 项 · 恢复 {restored} 条轨")
                check("一键调音:人声被往上提(音量那一项)",
                      bool(gains) and float(gains[0]["to"]) > float(gains[0]["from"]),
                      str(gains[:1])[:200])

                snap_am = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"const a=S.proj.tracks.find(t=>t.id==={json.dumps(vId)});"
                    f"const b=S.proj.tracks.find(t=>t.id==={json.dumps(bId)});"
                    "return {ga:a.gain,gb:b.gain,fa:JSON.stringify(a.fx||null),"
                    "fb:JSON.stringify(b.fx||null)};})()")
                done_am = await c.ev("STUDIO.autoApply()")
                after_am = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"const a=S.proj.tracks.find(t=>t.id==={json.dumps(vId)});"
                    f"const b=S.proj.tracks.find(t=>t.id==={json.dumps(bId)});"
                    "const auto=a.fx&&(a.fx.chain||[]).filter(s=>s&&s.auto).length||0;"
                    "return {ga:a.gain,gb:b.gain,fa:JSON.stringify(a.fx||null),"
                    "fb:JSON.stringify(b.fx||null),auto:auto,"
                    "panel:document.getElementById('autoPanel').hidden};})()")
                check("一键调音:应用改了推子(档位/增益真的变了)",
                      int(done_am or 0) > 0
                      and (after_am or {}).get("ga") != (snap_am or {}).get("ga"),
                      f"应用 {done_am} 项 · 人声 "
                      f"{(snap_am or {}).get('ga')} → {(after_am or {}).get('ga')}")
                check("一键调音:面板应用完自己关掉",
                      (after_am or {}).get("panel") is True, str((after_am or {}).get("panel")))
                back_am = await c.ev(
                    "(()=>{STUDIO.undo();const S=STUDIO.S;"
                    f"const a=S.proj.tracks.find(t=>t.id==={json.dumps(vId)});"
                    f"const b=S.proj.tracks.find(t=>t.id==={json.dumps(bId)});"
                    "return {ga:a.gain,gb:b.gain,fa:JSON.stringify(a.fx||null),"
                    "fb:JSON.stringify(b.fx||null)};})()")
                check("一键调音:一次 Ctrl+Z 增益/效果链全部还原",
                      back_am == snap_am, f"{snap_am} vs {back_am}")

            # ---- 媒体改名:盘上改 + 工程引用跟着改(源的 sourceId 不动) ----
            rn = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "S.proj.sources['src_rnm']={kind:'audio',name:'rnm_test.wav',"
                "path:'projects/media/rnm_test.wav',dur:2.0};"
                "return S.proj.sources['src_rnm'].path;})()")
            check("媒体改名:先有一个指向它的源", rn == "projects/media/rnm_test.wav", str(rn))
            # 名字里带路径 / 扩展名不对 / 下划线开头 / 重名 —— 全都得拒
            def _post_rename(old, new):
                return http("/api/studio/media_rename", "POST",
                            json.dumps({"old": old, "new": new}).encode(),
                            "application/json")[0]
            check("媒体改名:名字里带路径 → 400",
                  _post_rename("rnm_test.wav", "a/b.wav") == 400)
            check("媒体改名:扩展名不是音频 → 415",
                  _post_rename("rnm_test.wav", "x.txt") == 415)
            check("媒体改名:下划线开头 → 400",
                  _post_rename("rnm_test.wav", "_x.wav") == 400)
            check("媒体改名:目标已存在 → 409",
                  _post_rename("rnm_test.wav", "rnm_dup.wav") == 409)

            r1 = await c.ev("STUDIO.mediaRename('projects/media/rnm_test.wav',"
                            "'rnm_test2.wav')",
                            await_promise=True, timeout=60)
            chk = await c.ev(
                "(()=>{const S=STUDIO.S;const s=S.proj.sources['src_rnm'];"
                "return {path:s&&s.path,name:s&&s.name,sid:!!S.proj.sources['src_rnm'],"
                "media:(S.media||[]).map(f=>f.name)};})()")
            check("媒体改名:工程里的引用跟着改、sourceId 不变",
                  (chk or {}).get("path") == "projects/media/rnm_test2.wav"
                  and (chk or {}).get("name") == "rnm_test2.wav"
                  and (chk or {}).get("sid") is True, str(chk))
            names_l = (chk or {}).get("media") or []
            check("媒体改名:媒体列表显示新名字",
                  "rnm_test2.wav" in names_l and "rnm_test.wav" not in names_l,
                  f"{names_l[:4]}")
            st, wav2 = http("/api/studio/media/rnm_test2.wav")
            check("媒体改名:改完的文件还能取(能播)",
                  st == 200 and wav2[:4] == b"RIFF", f"HTTP {st} {len(wav2)} 字节")
            r2 = await c.ev("STUDIO.mediaRename('projects/media/rnm_test2.wav',"
                            "'rnm_test.wav')",
                            await_promise=True, timeout=60)
            chk2 = await c.ev("(()=>{const S=STUDIO.S;"
                              "const s=S.proj.sources['src_rnm'];"
                              "return {path:s&&s.path,media:(S.media||[]).map(f=>f.name)};})()")
            check("媒体改名:能再改回原名",
                  (chk2 or {}).get("path") == "projects/media/rnm_test.wav"
                  and "rnm_test.wav" in ((chk2 or {}).get("media") or []),
                  str(chk2)[:200])

            # ---- 片段音量:角标文本 + 撤销 + 多选一起改 ----
            cg = await c.ev(
                "(()=>{const S=STUDIO.S;"
                # 挑一条**有两块**的轨:后面多选一起改要用它
                "const tr=S.proj.tracks.filter(t=>(t.clips||[]).length>=2)"
                ".sort((a,b)=>b.clips.length-a.clips.length)[0];"
                "if(!tr)return null;"
                "const it=tr.clips.find(c=>Math.abs((c.gain??1)-1)<0.005&&c.length>0.5)"
                "||tr.clips[0];"
                "const id=it.id;"
                "const r0=STUDIO_STATS.renders;"
                "const before=STUDIO.clipGainText(id);"
                "const v=STUDIO.clipGain(id,0.5);"
                "const after=STUDIO.clipGainText(id);"
                "const r1=STUDIO_STATS.renders;"
                "return {tid:tr.id,n:tr.clips.length,cid:id,before:before,"
                "after:after,v:v,drend:r1-r0};})()")
            if not isinstance(cg, dict):
                check("片段音量:找到一条能改的块", False, str(cg))
            else:
                check("片段音量:角标从 100% 变成 −6.0dB",
                      cg.get("before") == "100%" and cg.get("after") == "-6.0dB"
                      and abs(float(cg.get("v") or 0) - 0.5) < 1e-6,
                      str(cg))
                check("片段音量:只改音量,一次渲染都不发",
                      cg.get("drend") == 0, f"渲染 +{cg.get('drend')}")
                bt = await c.ev(f"(()=>{{STUDIO.undo();"
                                f"return STUDIO.clipGainText({json.dumps(cg['cid'])});}})()")
                check("片段音量:撤销一次回到 100%", bt == "100%", str(bt))

                # 多选:检查器里改增益必须**两块都变**(以前只改第一块)
                mul = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(cg['tid'])});"
                    "const ids=tr.clips.slice(0,2).map(c=>c.id);"
                    "if(ids.length<2)return {err:'不足两块'};"
                    "S.sel=new Set(ids);STUDIO.renderInspector();"
                    "const g=document.getElementById('iGain');"
                    "g.value='0.5';"
                    "g.dispatchEvent(new Event('input',{bubbles:true}));"
                    "g.dispatchEvent(new Event('change',{bubbles:true}));"
                    "const gains=tr.clips.slice(0,2).map(c=>c.gain);"
                    "const title=document.querySelector('#inspector h3, #iTitle')?.textContent||'';"
                    "return {ids:ids,gains:gains,title:title};})()")
                gs = (mul or {}).get("gains") or []
                check("片段音量:多选时两块一起改",
                      len(gs) == 2 and all(abs(float(g) - 0.5) < 1e-6 for g in gs),
                      str(mul))
                bm = await c.ev("(()=>{STUDIO.undo();const S=STUDIO.S;"
                                f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(cg['tid'])});"
                                "return tr.clips.slice(0,2).map(c=>({g:c.gain}));})()")
                check("片段音量:多选改完撤销一次全部回 100%",
                      all(abs(float(x.get("g", 1)) - 1.0) < 1e-6 for x in (bm or [])),
                      str(bm))

            # ---- clip.gain 真的进了 numpy:导出后两段 RMS 比 = 0.5 ----
            cgproj = {
                "version": 1, "rev": 0, "bpm": 120, "timeSig": [4, 4],
                "lengthSec": 2.0,
                "sources": {"s1": {"path": "projects/media/_cgain.wav", "dur": 2.0}},
                "tracks": [{"id": "t1", "name": "音量对账", "kind": "audio",
                            "gain": 1.0, "pan": 0.0, "clips": [
                                {"id": "c1", "sourceId": "s1", "start": 0.0,
                                 "srcIn": 0.0, "srcOut": 1.0, "length": 1.0,
                                 "gain": 1.0, "fadeIn": 0.0, "fadeOut": 0.0,
                                 "semis": 0, "cents": 0, "kind": "sound"},
                                {"id": "c2", "sourceId": "s1", "start": 1.0,
                                 "srcIn": 1.0, "srcOut": 2.0, "length": 1.0,
                                 "gain": 0.5, "fadeIn": 0.0, "fadeOut": 0.0,
                                 "semis": 0, "cents": 0, "kind": "sound"}]}],
            }
            st, raw = http("/api/studio/export", "POST", json.dumps(
                {"project": cgproj, "format": "wav", "normalize": False,
                 "loudness": None}).encode(), "application/json")
            eg = json.loads(raw) if st == 200 else {}
            if st == 200 and eg.get("url"):
                st2, wv = http(eg["url"])
                if st2 == 200 and wv[:4] == b"RIFF":
                    import io as _io2
                    ye, sre = sf.read(_io2.BytesIO(wv), always_2d=True)
                    r_hi = float(np.sqrt(np.mean(ye[int(0.15 * sre):int(0.85 * sre)] ** 2)))
                    r_lo = float(np.sqrt(np.mean(ye[int(1.15 * sre):int(1.85 * sre)] ** 2)))
                    ratio = r_lo / max(r_hi, 1e-12)
                    check("片段音量:导出后 0.5 增益那段的 RMS = 另一半 ×0.5",
                          abs(ratio - 0.5) <= 0.01, f"{ratio:.4f}")
                else:
                    check("片段音量:对账导出能下载", False, f"HTTP {st2}")
                (ROOT / "projects" / "exports" / str(eg.get("file") or "_")).unlink(
                    missing_ok=True)
            else:
                check("片段音量:对账导出成功", False, f"HTTP {st} {raw[:120]}")

            # ---- 整页块编辑:一轨的块铺成一整条,拖/删/切都直接改主时间轴 ----
            big = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "let tr=S.proj.tracks.find(t=>(t.clips||[]).length>=2);"
                "if(!tr){"
                "STUDIO.addClip({kind:'media',name:'_smoke_a.wav',"
                "path:'projects/media/_smoke_a.wav',dur:3,srcIn:0,srcOut:1},0,null);"
                "STUDIO.addClip({kind:'media',name:'_smoke_a.wav',"
                "path:'projects/media/_smoke_a.wav',dur:3,srcIn:1,srcOut:2},2,null);"
                "tr=S.proj.tracks.find(t=>(t.clips||[]).length>=2);}"
                "const ok=STUDIO.bigOpen(tr.id);"
                "const st=STUDIO.bigState();"
                "return {ok:ok,open:st.open,tid:tr.id,n:st.clips.length,"
                "visible:!document.getElementById('bigEdit').hidden,"
                "canvas:!!document.getElementById('bigCanvas')};})()")
            check("块编辑:整页打得开、显示这一轨的块",
                  isinstance(big, dict) and big.get("open") and big.get("visible")
                  and big.get("canvas") and (big.get("n") or 0) >= 2, str(big))

            # 真的按住拖(派发指针事件)—— 只改 start,一次渲染都不发
            geo = await c.ev(
                "(()=>{const st=STUDIO.bigState();"
                "const c=[...st.clips].sort((a,b)=>a.start-b.start)[0];"
                "const cv=document.getElementById('bigCanvas');"
                "const r=cv.getBoundingClientRect();"
                "const xa=r.left+st.t0x+(c.start-st.t0)*st.pps;"
                "const xb=r.left+st.t0x+(c.start+c.length-st.t0)*st.pps;"
                "return {x:(xa+xb)/2,y:r.top+st.laneY+st.laneH/2,"
                "id:c.id,start:c.start,pps:st.pps};})()")
            r0 = await c.ev("STUDIO_STATS.renders")
            dx = 60
            await c.call("Input.dispatchMouseEvent",
                         {"type": "mousePressed", "x": geo["x"], "y": geo["y"],
                          "button": "left", "buttons": 1, "clickCount": 1})
            for k in (1, 2, 3):
                await c.call("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": geo["x"] + dx * k / 3,
                              "y": geo["y"], "button": "left", "buttons": 1})
            await c.call("Input.dispatchMouseEvent",
                         {"type": "mouseReleased", "x": geo["x"] + dx, "y": geo["y"],
                          "button": "left", "buttons": 0, "clickCount": 1})
            await asyncio.sleep(0.25)
            dragged = await c.ev(
                "(()=>{const st=STUDIO.bigState();"
                f"const c=st.clips.find(x=>x.id==={json.dumps(geo['id'])});"
                "return {start:c&&c.start,len:c&&c.length,renders:STUDIO_STATS.renders};})()")
            want = geo["start"] + dx / geo["pps"]
            check("块编辑:拖得动,而且一次渲染都不发",
                  dragged and abs(float(dragged.get("start", -9)) - want) < 0.08
                  and dragged.get("renders") == r0,
                  f"{geo['start']:.3f} → {(dragged or {}).get('start')} "
                  f"(想 {want:.3f}) · 渲染 {r0}→{(dragged or {}).get('renders')}")

            # 删一块 = 原地留空(后面的块不许跟着往前挤)
            gap = await c.ev(
                "(()=>{const st=STUDIO.bigState();"
                "const cs=[...st.clips].sort((a,b)=>a.start-b.start);"
                "if(cs.length<2)return {err:'不足两块'};"
                "const victim=cs[0],keep=cs[1],n0=cs.length,keepStart=keep.start;"
                "STUDIO.bigSelect([victim.id]);STUDIO.deleteSelected();"
                "const st2=STUDIO.bigState();"
                "const k=st2.clips.find(c=>c.id===keep.id);"
                "return {n0:n0,n1:st2.clips.length,keepStart:keepStart,keepNow:k&&k.start};})()")
            check("块编辑:删一块 = 原地留空,后面的块不跟着挪",
                  isinstance(gap, dict) and gap.get("n1") == (gap.get("n0") or 0) - 1
                  and abs(float(gap.get("keepNow", 0)) - float(gap.get("keepStart", -1))) < 1e-6,
                  str(gap))
            und = await c.ev("(()=>{STUDIO.undo();return STUDIO.bigState().clips.length;})()")
            check("块编辑:撤销一次把删掉的块拿回来",
                  und == (gap or {}).get("n0"), f"{und} vs {(gap or {}).get('n0')}")

            # 在块中间切一刀:两块严丝合缝
            sp = await c.ev(
                "(()=>{const S=STUDIO.S;const st=STUDIO.bigState();"
                "const c=[...st.clips].sort((a,b)=>a.start-b.start)[0];"
                "if(!c)return {err:'没有块'};"
                "const n0=st.clips.length;"
                "S.playAt=c.start+c.length/2;"
                "STUDIO.bigSelect([c.id]);"
                "const n=STUDIO.splitSelected();"
                "const st2=STUDIO.bigState();"
                "const cs=[...st2.clips].sort((a,b)=>a.start-b.start);"
                "const half=c.start+c.length/2;"
                "const left=cs.find(x=>Math.abs(x.start-c.start)<1e-6);"
                "const right=cs.find(x=>Math.abs(x.start-half)<1e-6);"
                "const seam=left&&right?Math.abs(left.start+left.length-right.start):null;"
                "return {n:n,n0:n0,n1:st2.clips.length,seam:seam};})()")
            check("块编辑:在块中间切一刀 = 两块严丝合缝",
                  (sp or {}).get("n") == 1 and (sp or {}).get("n1") == (sp or {}).get("n0", 0) + 1
                  and (sp or {}).get("seam") is not None and (sp or {}).get("seam") < 1e-6,
                  str(sp))
            await c.ev("STUDIO.undo()")
            closed = await c.ev(
                "(()=>{STUDIO.bigClose();"
                "return {open:STUDIO.bigState().open,"
                "hidden:document.getElementById('bigEdit').hidden};})()")
            check("块编辑:Escape/× 关得掉这一页",
                  (closed or {}).get("open") is False and (closed or {}).get("hidden") is True,
                  str(closed))

            # 点左栏一条录音 = 切块 + 铺成一条轨 + 进整页(他那句「一整个页面」)
            # ★ 纯正弦切不出块(YIN 判不出有声)—— 要带泛音,像唱出来的
            rec_probe = ROOT / "recordings" / "smoke_blocks_voice.wav"
            rec_probe.parent.mkdir(parents=True, exist_ok=True)
            _sr = 48000

            def _blk_vowel(f0, sec):
                n = int(_sr * sec)
                tt = np.arange(n) / _sr
                ph = 2 * np.pi * f0 * tt
                y = sum((1 / k) * np.sin(k * ph) for k in range(1, 9))
                env = np.minimum(1, tt / 0.03) * np.minimum(1, (sec - tt) / 0.05)
                return (0.22 * y * np.clip(env, 0, 1)).astype(np.float32)

            _parts = [np.zeros(int(_sr * 0.15), dtype=np.float32)]
            for _f in (261.63, 293.66, 329.63, 261.63, 329.63, 293.66):
                _parts.append(_blk_vowel(_f, 0.45))
                _parts.append(np.zeros(int(_sr * 0.12), dtype=np.float32))
            sf.write(str(rec_probe), np.concatenate(_parts), _sr, subtype="PCM_16")
            await c.ev("document.getElementById('btnRecRefresh').click(), true")
            await asyncio.sleep(0.5)
            listed = await c.ev("STUDIO.S.recs.map(f=>f.name)")
            check("块编辑:测试录音出现在左栏",
                  "smoke_blocks_voice.wav" in (listed or []), str(listed)[:180])
            item = await c.ev(
                "(()=>{const box=document.getElementById('recList');"
                "const it=[...box.querySelectorAll('.it')]"
                ".find(x=>x.dataset.name==='smoke_blocks_voice.wav');"
                "if(!it)return null;const r=it.getBoundingClientRect();"
                "return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
            if item:
                for t_, btns in (("mousePressed", 1), ("mouseReleased", 0)):
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": t_, "x": item["x"], "y": item["y"],
                                  "button": "left", "buttons": btns, "clickCount": 1})
                await wait_for(c, "STUDIO.bigState().open", lambda v: v is True, 45)
                clicked = await c.ev(
                    "(()=>{const st=STUDIO.bigState();const S=STUDIO.S;"
                    "const tr=S.proj.tracks.find(t=>t.id===st.trackId);"
                    "const cs=[...(st.clips||[])].sort((a,b)=>a.start-b.start);"
                    "return {open:st.open,n:cs.length,bigFor:tr&&tr.bigFor,"
                    "first:cs[0]&&cs[0].start};})()")
                # ★ bigFor 现在带切法后缀(`路径|syllable`):换切法不能复用旧轨
                check("块编辑:点一条录音就进整页,块铺成一条轨",
                      (clicked or {}).get("open") is True
                      and (clicked or {}).get("n", 0) >= 1
                      and str((clicked or {}).get("bigFor") or "").split("|")[0]
                      == "recordings/smoke_blocks_voice.wav",
                      str(clicked))

                # ================= 逐音精修(Melodyne 式)+ 原唱条 =================
                async def mdrag(x0, y0, x1, y1, steps=3, modifiers=0):
                    """真的按住鼠标拖:pointerdown → 几次 pointermove → pointerup。"""
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": "mousePressed", "x": x0, "y": y0,
                                  "button": "left", "buttons": 1,
                                  "clickCount": 1, "modifiers": modifiers})
                    for k in range(1, steps + 1):
                        await c.call("Input.dispatchMouseEvent",
                                     {"type": "mouseMoved",
                                      "x": x0 + (x1 - x0) * k / steps,
                                      "y": y0 + (y1 - y0) * k / steps,
                                      "button": "left", "buttons": 1,
                                      "modifiers": modifiers})
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": "mouseReleased", "x": x1, "y": y1,
                                  "button": "left", "buttons": 0,
                                  "clickCount": 1, "modifiers": modifiers})

                def pstate(cid, extra=""):
                    return (f"(()=>{{const c=STUDIO.bigPitchInfo().find(x=>x.id==={json.dumps(cid)});"
                            "return c?{semis:c.semis,cents:c.cents,start:c.start,"
                            "length:c.length,sound:c.soundingMidi,need:c.needsRender,"
                            "key:!!c.renderKey,curve:c.curve,editable:c.editable,"
                            "renders:STUDIO_STATS.renders"
                            + extra + "}:null;})()")

                # ---- 1) 切到音高视图:钢琴条 + 每个音自己的 f0 ----
                pv = await c.ev(
                    "(()=>{STUDIO.bigViewSet('pitch');"
                    "const st=STUDIO.bigState();const info=STUDIO.bigPitchInfo();"
                    "const notes=info.filter(x=>x.editable);"
                    "const snd=info.filter(x=>!x.editable);"
                    "return {view:st.view,t0x:st.t0x,geo:st.pitch,"
                    "notes:notes.map(x=>({id:x.id,f0:x.f0,base:x.baseMidi,"
                    "sound:x.soundingMidi})),"
                    "nsnd:snd.length,edit:snd.map(x=>x.editable)};})()")
                check("音高视图:方块切换 + 56px 钢琴条",
                      (pv or {}).get("view") == "pitch" and (pv or {}).get("t0x") == 56
                      and bool((pv or {}).get("geo"))
                      and len((pv or {}).get("notes") or []) >= 3,
                      str({k: (pv or {}).get(k) for k in ("view", "t0x")})[:160])
                pnotes = (pv or {}).get("notes") or []
                check("音高视图:每个音都带着自己的 f0(不是瞎猜的 C4)",
                      bool(pnotes) and all(float(n.get("f0") or 0) > 0 for n in pnotes),
                      str([(n.get("f0"), round(float(n.get("base") or 0), 1))
                           for n in pnotes])[:200])
                if (pv or {}).get("nsnd"):
                    check("音高视图:气声块标着不可拖",
                          all(v is False for v in pv["edit"]), str(pv["edit"]))

                # ---- 2) 真鼠标竖向拖 2 个半音:吸附到半音 + 松手只渲染一次 ----
                first = await c.ev(
                    "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                    "const c=info[0];if(!c)return null;"
                    "const p=STUDIO.bigPitchPoint(c.id);"
                    "STUDIO.bigSelect([c.id]);"
                    "return {id:c.id,x:p.x,y:p.y,semi:p.semiPx,start:c.start,"
                    "sound:c.soundingMidi,semis:c.semis,cents:c.cents};})()")
                check("音高视图:拿得到音符块的屏幕坐标",
                      bool(first and float(first.get("semi") or 0) > 4), str(first)[:160])
                if first:
                    r0 = await c.ev("STUDIO_STATS.renders")
                    dy = 2.0 * float(first["semi"])
                    await mdrag(first["x"], first["y"],
                                first["x"], first["y"] - dy)
                    got = await wait_for(c, pstate(first["id"]),
                                         lambda v: v and v.get("key") and not v.get("need"), 45)
                    r1 = (got or {}).get("renders")
                    check("音高视图:竖向拖 2 个半音 = semis+2、起点不动、只渲染 1 次",
                          bool(got) and got.get("semis") == 2
                          and abs(float(got.get("start") or -9)
                                  - float(first["start"])) < 1e-6
                          and abs(float(got.get("sound") or 0)
                                  - (float(first["sound"] or 0) + 2)) < 0.01
                          and r1 == (r0 or 0) + 1,
                          f"{(got or {}).get('semis')} 半音 · "
                          f"{(first.get('sound') or 0):.2f}→{(got or {}).get('sound')} · "
                          f"渲染 {r0}→{r1}")
                    await c.ev("STUDIO.undo()")
                    und = await c.ev(pstate(first["id"]))
                    check("音高视图:撤销一次回到原音高",
                          bool(und) and (und.get("semis") or 0) == 0
                          and abs(float(und.get("cents") or 0)) < 0.01,
                          str({k: (und or {}).get(k) for k in ("semis", "cents")}))

                # ---- 3) Alt 拖 = 按音分(不吸附半音) ----
                alt = await c.ev(
                    "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                    "const c=info[1]||info[0];if(!c)return null;"
                    "const p=STUDIO.bigPitchPoint(c.id);"
                    "STUDIO.bigSelect([c.id]);"
                    "return {id:c.id,x:p.x,y:p.y,semi:p.semiPx,semis:c.semis};})()")
                if alt:
                    await mdrag(alt["x"], alt["y"], alt["x"],
                                alt["y"] - float(alt["semi"]) * 0.5, modifiers=1)
                    ac = await wait_for(c, pstate(alt["id"]),
                                        lambda v: v and v.get("key") and not v.get("need"), 45)
                    check("音高视图:按住 Alt 拖半个半音 = 50 音分(不吸附)",
                          bool(ac) and (ac.get("semis") or 0) == 0
                          and abs(float(ac.get("cents") or 0) - 50) <= 2,
                          str({k: (ac or {}).get(k) for k in ("semis", "cents")}))
                    await c.ev("STUDIO.undo()")

                # ---- 4) 多选:拖第一个,两个一起走同样的 delta ----
                multi = await c.ev(
                    "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                    "if(info.length<2)return null;const a=info[0],b=info[1];"
                    "STUDIO.bigSelect([a.id,b.id]);"
                    "const p=STUDIO.bigPitchPoint(a.id);"
                    "return {ids:[a.id,b.id],x:p.x,y:p.y,semi:p.semiPx,"
                    "before:[a.semis,b.semis]};})()")
                if multi:
                    await mdrag(multi["x"], multi["y"], multi["x"],
                                multi["y"] - float(multi["semi"]))
                    both = await wait_for(
                        c,
                        "(()=>{const info=STUDIO.bigPitchInfo();"
                        f"const a=info.find(x=>x.id==={json.dumps(multi['ids'][0])});"
                        f"const b=info.find(x=>x.id==={json.dumps(multi['ids'][1])});"
                        "if(!a||!b)return null;"
                        "return {a:a.semis,b:b.semis,keys:[!!a.renderKey,!!b.renderKey],"
                        "need:[a.needsRender,b.needsRender]};})()",
                        lambda v: v and all(v.get("keys") or []) and not any(v.get("need") or []),
                        60)
                    check("音高视图:多选时拖一个 +1 半音,两块一起 +1",
                          bool(both) and both.get("a") == 1 and both.get("b") == 1,
                          str(both))
                    await c.ev("STUDIO.undo()")

                # ---- 5) 横向拖 = 只挪时间,音高一点不动、零渲染 ----
                await c.ev("(()=>{STUDIO.S.proj.ui.snap='off';"
                           "document.getElementById('selSnap').value='off';"
                           "return true;})()")
                mv = await c.ev(
                    "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                    "const c=info[0];if(!c)return null;const p=STUDIO.bigPitchPoint(c.id);"
                    "STUDIO.bigSelect([c.id]);"
                    "return {id:c.id,x:p.x,y:p.y,start:c.start,semis:c.semis,"
                    "cents:c.cents,pps:STUDIO.bigState().pps};})()")
                if mv:
                    r0 = await c.ev("STUDIO_STATS.renders")
                    await mdrag(mv["x"], mv["y"], mv["x"] + 60, mv["y"])
                    await asyncio.sleep(0.2)
                    mg = await c.ev(pstate(mv["id"]))
                    want = float(mv["start"]) + 60.0 / float(mv["pps"])
                    check("音高视图:横向拖 60px = 只改 start,音高不动、零渲染",
                          bool(mg) and abs(float(mg.get("start") or -9) - want) < 0.08
                          and (mg.get("semis") or 0) == 0
                          and abs(float(mg.get("cents") or 0)) < 0.01
                          and mg.get("renders") == r0,
                          f"{(mv.get('start') or 0):.3f}→{(mg or {}).get('start')} "
                          f"(想 {want:.3f}) · 渲染 {r0}→{(mg or {}).get('renders')}")
                    await c.ev("STUDIO.undo()")
                await c.ev("(()=>{STUDIO.S.proj.ui.snap='1/16';"
                           "document.getElementById('selSnap').value='1/16';"
                           "return true;})()")

                # ---- 6) 笔:在选中的音里画曲线 ----
                pen = await c.ev(
                    "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                    "const c=info[0];if(!c)return null;"
                    "STUDIO.bigSelect([c.id]);STUDIO.bigPenSet(true);"
                    "const p=STUDIO.bigPitchPoint(c.id);"
                    "return {id:c.id,x:p.x,y:p.y,semi:p.semiPx,"
                    "span:c.length};})()")
                if pen:
                    px, py = pen["x"], pen["y"]
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": "mousePressed", "x": px - 30, "y": py,
                                  "button": "left", "buttons": 1, "clickCount": 1})
                    for dx, dy2 in ((-12, -6), (8, 8), (24, -4)):
                        await c.call("Input.dispatchMouseEvent",
                                     {"type": "mouseMoved", "x": px + dx,
                                      "y": py + dy2, "button": "left", "buttons": 1})
                    await c.call("Input.dispatchMouseEvent",
                                 {"type": "mouseReleased", "x": px + 24, "y": py - 4,
                                  "button": "left", "buttons": 0, "clickCount": 1})
                    pcv = await wait_for(
                        c, pstate(pen["id"]),
                        lambda v: v and v.get("key")
                        and isinstance(v.get("curve"), list) and len(v["curve"]) > 1, 60)
                    cur = (pcv or {}).get("curve") or []
                    span = float(pen["span"])
                    check("音高视图:笔画出一道曲线(首尾点齐、常数清零、渲染成功)",
                          bool(cur) and abs(float(cur[0][0])) < 1e-6
                          and abs(float(cur[-1][0]) - span) < 0.03
                          and (pcv.get("semis") or 0) == 0
                          and abs(float(pcv.get("cents") or 0)) < 0.01
                          and bool(pcv.get("key")),
                          f"{len(cur)} 点 · t {cur[0][0] if cur else '?'}…"
                          f"{cur[-1][0] if cur else '?'} / span {span:.3f}")
                    await c.ev("STUDIO.bigPenSet(false)")

                    # 拉平 = 曲线全 0(不是删曲线)
                    nflat = await c.ev("STUDIO.bigFlat()", await_promise=True,
                                       timeout=90)
                    pf = await c.ev(pstate(pen["id"]))
                    fcur = (pf or {}).get("curve") or []
                    check("音高视图:拉平 = 曲线每一点都归 0",
                          nflat == 1 and bool(fcur)
                          and all(abs(float(v[1])) < 0.01 for v in fcur)
                          and bool(pf.get("key")),
                          f"{len(fcur)} 点 · max {max((abs(float(v[1])) for v in fcur),
                                                     default=-1):.2f}")

                    # 带曲线切分:两半都从 0 起、各自渲染
                    a0 = float(pen["span"])
                    win = await c.ev(
                        "(()=>{const info=STUDIO.bigPitchInfo().filter(x=>x.editable);"
                        "const c=info[0];if(!c)return null;"
                        "const A=c.start,B=c.start+c.length;"
                        "STUDIO.S.playAt=A+c.length/2;STUDIO.bigSelect([c.id]);"
                        "const n=STUDIO.splitSelected();"
                        f"window.__w=[A-1e-6,B+1e-6];"
                        "return {n:n,A:A,B:B,mid:A+c.length/2};})()")
                    halves = await wait_for(
                        c,
                        "(()=>{const st=STUDIO.bigState();const info=STUDIO.bigPitchInfo();"
                        "const A=window.__w[0],B=window.__w[1];"
                        "return st.clips.filter(x=>x.start>=A&&x.start+x.length<=B)"
                        ".map(x=>{const p=info.find(y=>y.id===x.id)||{};"
                        "return {id:x.id,start:x.start,length:x.length,srcIn:x.srcIn,"
                        "srcOut:x.srcOut,curve:p.curve,key:!!p.renderKey,"
                        "need:p.needsRender};});})()",
                        lambda v: v and len(v) == 2
                        and all(h.get("curve") and h.get("key") for h in v), 60)
                    hh = halves or []
                    ok_split = (bool(win and win.get("n") == 1) and len(hh) == 2
                                and abs(float(hh[0]["start"]) - float(win["A"])) < 1e-6
                                and all(abs(float(h["srcOut"]) - float(h["srcIn"])
                                            - float(h["length"])) < 1e-6 for h in hh))
                    if ok_split:
                        for h in hh:
                            hc = h.get("curve") or []
                            ok_split = ok_split and bool(hc) \
                                and abs(float(hc[0][0])) < 1e-6 \
                                and abs(float(hc[-1][0]) - float(h["length"])) < 0.03
                    check("音高视图:带曲线切一刀 = 两半曲线都重标时间、各自渲染",
                          ok_split, str(hh)[:320])
                    await c.ev("STUDIO.undo()")
                    await asyncio.sleep(0.2)
                else:
                    check("音高视图:笔测试的块找得到", False, str(pen))

                # ---- 7) 带曲线的裁剪:右裁、左裁各一次 ----
                await c.ev("(()=>{STUDIO.S.proj.ui.snap='off';"
                           "document.getElementById('selSnap').value='off';"
                           "return true;})()")
                fid = first["id"] if first else ""
                tr_r = await c.ev(
                    "(()=>{const st=STUDIO.bigState();"
                    f"const c=st.clips.find(x=>x.id==={json.dumps(fid)});"
                    "if(!c)return null;const info=STUDIO.bigPitchInfo()"
                    ".find(y=>y.id===c.id);"
                    "const p=STUDIO.bigPitchPoint(c.id);STUDIO.bigSelect([c.id]);"
                    "return {id:c.id,cx:p.x,cy:p.y,w:c.length*st.pps,start:c.start,"
                    "len:c.length,srcIn:c.srcIn,srcOut:c.srcOut,curve:info.curve};})()")
                if tr_r:
                    r0 = await c.ev("STUDIO_STATS.renders")
                    xr = float(tr_r["cx"]) + float(tr_r["w"]) / 2
                    await mdrag(xr - 2, tr_r["cy"], xr - 32, tr_r["cy"])
                    tg = await wait_for(c, pstate(tr_r["id"]),
                                        lambda v: v and v.get("key") and not v.get("need"), 60)
                    tc = (tg or {}).get("curve") or []
                    check("音高视图:右边裁短,曲线跟着裁、时间从 0 起",
                          bool(tg)
                          and abs(float(tg.get("start") or 0)
                                  - float(tr_r["start"])) < 1e-6
                          and float(tg.get("length") or 9) < float(tr_r["len"]) - 0.03
                          and (tg.get("renders") or 0) > (r0 or 0)
                          and bool(tc) and abs(float(tc[0][0])) < 1e-6
                          and abs(float(tc[-1][0]) - float(tg["length"])) < 0.03,
                          f"len {tr_r['len']:.3f}→{(tg or {}).get('length')} · "
                          f"{len(tc)} 点 · 渲染 {r0}→{(tg or {}).get('renders')}")
                    # 左裁
                    geo2 = await c.ev(
                        "(()=>{const st=STUDIO.bigState();"
                        f"const c=st.clips.find(x=>x.id==={json.dumps(fid)});"
                        "const p=STUDIO.bigPitchPoint(c.id);STUDIO.bigSelect([c.id]);"
                        "return {id:c.id,cx:p.x,cy:p.y,w:c.length*st.pps,start:c.start,"
                        "len:c.length,srcIn:c.srcIn};})()")
                    r1 = await c.ev("STUDIO_STATS.renders")
                    xl = float(geo2["cx"]) - float(geo2["w"]) / 2
                    await mdrag(xl + 2, geo2["cy"], xl + 22, geo2["cy"])
                    tg2 = await wait_for(c, pstate(geo2["id"]),
                                         lambda v: v and v.get("key") and not v.get("need"), 60)
                    tc2 = (tg2 or {}).get("curve") or []
                    check("音高视图:左边裁短,起点/源入一起走,曲线仍从 0 起",
                          bool(tg2)
                          and float(tg2.get("start") or 0) > float(geo2["start"]) + 0.02
                          and (tg2.get("renders") or 0) > (r1 or 0)
                          and bool(tc2) and abs(float(tc2[0][0])) < 1e-6
                          and abs(float(tc2[-1][0]) - float(tg2["length"])) < 0.03,
                          f"start {geo2['start']:.3f}→{(tg2 or {}).get('start')} · "
                          f"{len(tc2)} 点")

                    # ---- 8) 检查器改「源入」⇒ 曲线平移、缓存清掉 ----
                    await c.ev(
                        "(()=>{const st=STUDIO.bigState();"
                        f"const c=st.clips.find(x=>x.id==={json.dumps(fid)});"
                        "const p=STUDIO.bigPitchPoint(c.id);"
                        "window.__pt={x:p.x,y:p.y};STUDIO.bigSelect([c.id]);"
                        "return true;})()")
                    pt = await c.ev("window.__pt")
                    for _t, _b in (("mousePressed", 1), ("mouseReleased", 0)):
                        await c.call("Input.dispatchMouseEvent",
                                     {"type": _t, "x": pt["x"], "y": pt["y"],
                                      "button": "left", "buttons": _b,
                                      "clickCount": 1})
                    await asyncio.sleep(0.2)
                    # 检查器里的曲线画布:以前 172×72 又没标注,他只能猜
                    ui_cv = await c.ev(
                        "(()=>{const cv=document.getElementById('iCurve');"
                        "const h=document.querySelector('.ivhint');"
                        "if(!cv)return {err:'没有曲线画布'};"
                        "const r=cv.getBoundingClientRect();"
                        "return {w:Math.round(r.width),h:Math.round(r.height),"
                        "hint:h?h.textContent.trim().slice(0,10):''};})()")
                    check("检查器:曲线画布够大、旁边写着它是干嘛的",
                          isinstance(ui_cv, dict) and ui_cv.get("w", 0) >= 190
                          and ui_cv.get("h", 0) >= 110 and bool(ui_cv.get("hint")),
                          str(ui_cv))
                    di = await c.ev(
                        "(()=>{const st=STUDIO.bigState();"
                        f"const c=st.clips.find(x=>x.id==={json.dumps(fid)});"
                        "const el=document.getElementById('iIn');"
                        "if(!el)return {err:'检查器没出来'};"
                        "const before=c.srcIn;el.value=(before+0.05).toFixed(3);"
                        "el.dispatchEvent(new Event('change'));"
                        "return {id:c.id,before:before,set:+el.value};})()")
                    if isinstance(di, dict) and di.get("id"):
                        key0 = await wait_for(c, pstate(di["id"]),
                                              lambda v: v and v.get("key")
                                              and not v.get("need"), 60)
                        st_now = await c.ev(
                            "(()=>{const st=STUDIO.bigState();"
                            f"const c=st.clips.find(x=>x.id==={json.dumps(di['id'])});"
                            "return {srcIn:c&&c.srcIn,length:c&&c.length};})()")
                        ci = (key0 or {}).get("curve") or []
                        check("检查器:改「源入」⇒ 曲线平移重标、缓存失效后重渲染",
                              bool(st_now)
                              and abs(float(st_now.get("srcIn") or 0)
                                      - float(di["set"])) < 1e-3
                              and bool(ci) and abs(float(ci[0][0])) < 1e-6
                              and bool((key0 or {}).get("key")),
                              f"srcIn {di.get('before')}→{(st_now or {}).get('srcIn')} · "
                              f"{len(ci)} 点")
                    else:
                        check("检查器:改「源入」⇒ 曲线平移重标、缓存失效后重渲染",
                              False, str(di))

                    # 清空曲线 = 回到原始音高
                    nclr = await c.ev("STUDIO.bigCurveClear()")
                    pcl = await c.ev(pstate(fid))
                    check("音高视图:清空曲线 = 没有曲线、缓存也清掉",
                          nclr == 1 and bool(pcl)
                          and not pcl.get("curve") and not pcl.get("key")
                          and (pcl.get("semis") or 0) == 0,
                          f"清 {nclr} 个 · key={bool((pcl or {}).get('key'))}")
                await c.ev("(()=>{STUDIO.S.proj.ui.snap='1/16';"
                           "document.getElementById('selSnap').value='1/16';"
                           "return true;})()")

                # ================= 原唱条:只画目标线,只修选中的音 =================
                rsetup = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const A=(n,d)=>STUDIO.addClip({kind:'media',name:n,"
                    "path:'projects/media/'+n,dur:d},0,null);"
                    "A('_rs_ref.wav',7.1);A('_rs_take.wav',7.9);"
                    "const byName=n=>{for(const t of S.proj.tracks){"
                    "const c=(t.clips||[])[0];const s=c&&S.proj.sources[c.sourceId];"
                    "if(s&&s.name===n)return t;}return null;};"
                    "const rtr=byName('_rs_ref.wav'),ttr=byName('_rs_take.wav');"
                    "if(!rtr||!ttr)return {err:'没建起来'};"
                    "const rc=rtr.clips[0],tc=ttr.clips[0];"
                    "rc.start=0;rc.length=7.1;rc.srcIn=0;rc.srcOut=7.1;"
                    "tc.start=2.0;tc.length=4.0;tc.srcIn=0;tc.srcOut=4.0;tc.id='tk_a';"
                    "tc.kind='note';tc.f0=261.63;"      # ★ 媒体倒进来默认是 sound,这里当「唱的」用
                    "const b=JSON.parse(JSON.stringify(tc));"
                    "b.id='tk_b';b.start=6.0;b.length=3.9;b.srcIn=4.0;b.srcOut=7.9;"
                    "ttr.clips=[tc,b];"
                    "STUDIO.bigOpen(ttr.id);STUDIO.bigViewSet('pitch');"
                    "STUDIO.bigRefOpen(true);STUDIO.bigRefPick(rtr.id);"
                    "return {ref:rtr.id,take:ttr.id,clips:ttr.clips.map(c=>c.id),"
                    "view:STUDIO.bigState().view,bar:!document.getElementById('bigRefBar').hidden};})()")
                ref_tid = (rsetup or {}).get("ref")
                take_tid = (rsetup or {}).get("take")
                check("原唱条:能展开、能选原唱轨(不进弹窗)",
                      bool(ref_tid and take_tid) and rsetup.get("view") == "pitch"
                      and rsetup.get("bar") is True, str(rsetup)[:200])
                if ref_tid and take_tid:
                    plan = await c.ev("STUDIO.bigRefAnalyze()", await_promise=True,
                                      timeout=240)
                    stt = await c.ev("STUDIO.bigRefState()")
                    check("原唱条:分析出方案,状态里写明覆盖度",
                          bool(stt and stt.get("hasPlan"))
                          and (stt.get("pitchActions") or 0) >= 1
                          and float(stt.get("coverage") or 0) > 0.3
                          and "对上" in str(stt.get("status") or ""),
                          str(stt)[:220])
                    tgt = await c.ev("STUDIO.bigTargetMidi('tk_a')")
                    check("原唱条:画的是「我的原始音高 + 修正量」的目标线",
                          isinstance(tgt, (int, float)), str(tgt))

                    ap = await c.ev(
                        "(()=>{const S=STUDIO.S;"
                        f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(take_tid)});"
                        "const a=tr.clips.find(c=>c.id==='tk_a');"
                        "const b=tr.clips.find(c=>c.id==='tk_b');"
                        "const snapB=JSON.stringify({curve:b.pitchCurve||null,"
                        "semis:b.semis||0,cents:b.cents||0,start:b.start,"
                        "length:b.length,key:b.renderKey||null});"
                        "const r0=STUDIO_STATS.renders;"
                        "STUDIO.bigSelect(['tk_a']);"
                        "return STUDIO.bigRefApplySel().then(n=>({n:n,"
                        "renders:STUDIO_STATS.renders-r0,"
                        "a:{curve:(a.pitchCurve||[]).length,semis:a.semis||0,"
                        "cents:a.cents||0,key:!!a.renderKey},"
                        "b:JSON.stringify({curve:b.pitchCurve||null,"
                        "semis:b.semis||0,cents:b.cents||0,start:b.start,"
                        "length:b.length,key:b.renderKey||null}),"
                        "snapB:snapB,status:STUDIO.bigRefState().status}));})()",
                        await_promise=True, timeout=240)
                    apd = ap or {}
                    check("原唱条:只修选中的那个音,别的音一个字段都没动",
                          int(apd.get("n") or 0) >= 1
                          and (apd.get("a") or {}).get("key") is True
                          and ((apd.get("a") or {}).get("curve", 0) > 0
                               or (apd.get("a") or {}).get("semis", 0) != 0)
                          and apd.get("b") == apd.get("snapB")
                          and int(apd.get("renders") or 0) >= 1,
                          f"修 {apd.get('n')} 个 · a={apd.get('a')} · "
                          f"b没动={apd.get('b') == apd.get('snapB')}")

                    back = await c.ev(
                        "(()=>{STUDIO.undo();const S=STUDIO.S;"
                        f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(take_tid)});"
                        "const a=tr.clips.find(c=>c.id==='tk_a');"
                        "return {curve:(a.pitchCurve||[]).length,"
                        "semis:a.semis||0,cents:a.cents||0};})()")
                    check("原唱条:一次 Ctrl+Z 全退",
                          bool(back) and not back.get("curve")
                          and (back.get("semis") or 0) == 0
                          and abs(float(back.get("cents") or 0)) < 0.01, str(back))

                    stale = await c.ev(
                        "(()=>{const S=STUDIO.S;"
                        f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(take_tid)});"
                        "const b=tr.clips.find(c=>c.id==='tk_b');b.start=6.6;"
                        "const mk=()=>JSON.stringify({curve:b.pitchCurve||null,"
                        "semis:b.semis||0,cents:b.cents||0,start:b.start,"
                        "length:b.length});"
                        "const before=mk();STUDIO.bigSelect(['tk_b']);"
                        "return STUDIO.bigRefApplySel().then(n=>({n:n,before:before,"
                        "after:mk(),status:STUDIO.bigRefState().status}));})()",
                        await_promise=True, timeout=120)
                    check("原唱条:挪过位置的音会被拒,一个字段都不改",
                          int((stale or {}).get("n") or 0) == 0
                          and (stale or {}).get("before") == (stale or {}).get("after")
                          and "挪过" in str((stale or {}).get("status") or ""),
                          str(stale)[:220])

                    show = await c.ev(
                        "(()=>{const S=STUDIO.S;"
                        f"const tr=S.proj.tracks.find(t=>t.id==={json.dumps(take_tid)});"
                        "const mk=()=>JSON.stringify(tr.clips.map(c=>({id:c.id,"
                        "start:c.start,length:c.length,semis:c.semis||0,"
                        "cents:c.cents||0,curve:c.pitchCurve||null,key:c.renderKey||null})));"
                        "const snap=mk();const off=STUDIO.bigRefShowSet(false);"
                        "const s1=STUDIO.bigRefState();const same=snap===mk();"
                        "STUDIO.bigRefShowSet(true);"
                        "return {off:off,show:s1.show,same:same,"
                        "on:STUDIO.bigRefState().show};})()")
                    check("原唱条:「显示原唱线」开关只影响画面,不动数据",
                          show.get("off") is False and show.get("show") is False
                          and show.get("same") is True and show.get("on") is True,
                          str(show))

                    await c.ev(
                        "(()=>{const S=STUDIO.S;STUDIO.bigClose();"
                        "S.proj.tracks=S.proj.tracks.filter(t=>"
                        f"t.id!=={json.dumps(ref_tid)}&&t.id!=={json.dumps(take_tid)});"
                        "STUDIO.drawStatic();return true;})()")

                # 关页 + 清掉这条测试轨

            # ---- 媒体切块(以前只有「录音」能切)+ 导入原唱入口 ----
            mb = await c.ev(
                "STUDIO.loadBlocks('projects/media/_pitchtest.wav').then(()=>{"
                "const bs=(STUDIO.S.blocks&&STUDIO.S.blocks.blocks)||[];"
                "return {n:bs.filter(b=>b.kind==='note').length,why:STUDIO.blocksWhy()};})",
                await_promise=True, timeout=90)
            check("媒体路径也能切出音块(导入的干声/别人的录音)",
                  isinstance(mb, dict) and mb.get("n", 0) >= 3, str(mb))

            badp = await c.ev(
                "fetch('/api/blocks',{method:'POST',headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({path:'projects/media/../../server.py'})}).then(r=>r.status)",
                await_promise=True, timeout=20)
            check("切块只认录音/媒体目录,越界路径 404", badp == 404, f"HTTP {badp}")

            # ---- 切法:按字/音节 vs 按音(旧行为) ----
            sy = await c.ev(
                "STUDIO.loadBlocks('projects/media/_pitchtest.wav',{mode:'syllable'}).then(()=>{"
                "const S=STUDIO.S,bs=(S.blocks&&S.blocks.blocks)||[];"
                "const nb=bs.filter(b=>b.kind==='note');"
                "return {seg:S.blocksMode,n:nb.length,"
                "syl:nb.length>0&&nb.every(b=>Number(b.syl)>=1)};})",
                await_promise=True, timeout=90)
            check("切法=按字/音节:seg=syllable、每个音块带 syl 序号",
                  isinstance(sy, dict) and sy.get("seg") == "syllable"
                  and sy.get("n", 0) >= 1 and sy.get("syl") is True, str(sy))

            an = await c.ev(
                "STUDIO.loadBlocks('projects/media/_pitchtest.wav',{mode:'note'}).then(()=>{"
                "const S=STUDIO.S,bs=(S.blocks&&S.blocks.blocks)||[];"
                "const nb=bs.filter(b=>b.kind==='note');"
                "return {seg:S.blocksMode,n:nb.length,"
                "syl:nb.some(b=>'syl' in b)};})",
                await_promise=True, timeout=90)
            check("切法=按音:seg=note、块上没有 syl(旧行为)",
                  isinstance(an, dict) and an.get("seg") == "note"
                  and an.get("n", 0) >= 1 and an.get("syl") is False, str(an))

            legacy = await c.ev(
                "fetch('/api/blocks',{method:'POST',headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({path:'projects/media/_pitchtest.wav'})})"
                ".then(r=>r.json()).then(j=>({seg:j.seg,"
                "syl:(j.blocks||[]).some(b=>'syl' in b)}))",
                await_promise=True, timeout=60)
            check("旧客户端(不带 mode)仍是按音切",
                  (legacy or {}).get("seg") == "note"
                  and (legacy or {}).get("syl") is False, str(legacy))

            await c.ev(
                "(()=>{const el=document.getElementById('blockMode');"
                "el.value='syllable';el.dispatchEvent(new Event('change'));return true;})()")
            await wait_for(c, "STUDIO.S.blocks&&STUDIO.S.blocks.seg",
                           lambda v: v == "syllable", timeout=60)
            check("切法下拉切换后会重新切块(seg 跟着变)",
                  (await c.ev("STUDIO.S.blocks&&STUDIO.S.blocks.seg")) == "syllable")

            # ---- 我的声音 / 补音:面板 → 生成 → 应用 → 一次撤销 ----
            vs = await c.ev(
                "STUDIO.voiceOpen().then(()=>{"
                "const box=document.getElementById('voiceState');"
                "return {open:!document.getElementById('voicePanel').hidden,"
                "text:((box&&box.textContent)||'').replace(/\\s+/g,' ').trim()};})",
                await_promise=True, timeout=30)
            check("我的声音面板:能打开、显示云端/本地状态",
                  isinstance(vs, dict) and vs.get("open") is True
                  and "本地补音" in vs.get("text", ""), str(vs)[:200])

            ex = await c.ev(
                "(()=>{const c=STUDIO.addClip({kind:'media',name:'_pitchtest.wav',"
                "path:'projects/media/_pitchtest.wav',dur:3.57},0,null);"
                "c.kind='note';c.srcIn=0.20;c.srcOut=1.20;c.length=1.0;"
                "const S=STUDIO.S;S.sel.clear();S.sel.add(c.id);STUDIO.extendOpen();"
                "return {tr:S.proj.tracks.find(t=>t.id===S.selTrack).id,"
                "clip:c.id,target:document.getElementById('extTarget').value,"
                "open:!document.getElementById('extPanel').hidden};})()")
            check("补音弹窗:选中音块能打开、默认目标 2×",
                  isinstance(ex, dict) and ex.get("open") is True
                  and abs(float(ex.get("target") or 0) - 2.0) <= 0.01, str(ex))

            if isinstance(ex, dict) and ex.get("clip"):
                await c.ev("STUDIO.extendGo()", await_promise=True, timeout=180)
                est = await c.ev("STUDIO.extendState()")
                res = (est or {}).get("result") or {}
                # ★ 引擎不能写死成 local:RVC 服务(本机 8000 或远端)只要活着,走的就是 cloud。
                #   先问 status 通不通,再拿这个当期望值 —— 否则「服务开没开」决定这条测试红不红。
                try:
                    _st, _b = http("/api/studio/voice/status")
                    want = "cloud" if (json.loads(_b).get("cloud") or {}).get("reachable") else "local"
                except Exception:
                    want = "local"
                check("补音生成:%s 引擎、时长=目标" % ("云端" if want == "cloud" else "本地"),
                      bool(res.get("ok")) and res.get("engine") == want
                      and abs(float(res.get("sec") or 0) - 2.0) < 0.05,
                      str(res)[:220])
                gname = str(res.get("path") or "").split("/")[-1]
                gst, _ = http("/api/studio/media/" + urllib.parse.quote(gname)) \
                    if gname else (0, b"")
                check("生成的补音文件能通过媒体接口取到",
                      gst == 200, f"HTTP {gst} {gname}")

                before_ex = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const it=S.proj.tracks.flatMap(t=>t.clips||[])"
                    f".find(c=>String(c.id)==={json.dumps(ex['clip'])});"
                    "return {srcId:it.sourceId,len:it.length,srcOut:it.srcOut,"
                    "blockId:it.blockId||null,curve:!!it.pitchCurve};})()")
                await c.ev("STUDIO.extendApply()")
                after_ex = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const it=S.proj.tracks.flatMap(t=>t.clips||[])"
                    f".find(c=>String(c.id)==={json.dumps(ex['clip'])});"
                    "return {srcId:it.sourceId,len:it.length,srcOut:it.srcOut,"
                    "blockId:it.blockId||null,curve:!!it.pitchCurve};})()")
                check("应用补音:换 source、长度=目标、清掉 blockId/曲线",
                      (after_ex or {}).get("srcId") != (before_ex or {}).get("srcId")
                      and abs(float((after_ex or {}).get("len") or 0) - 2.0) < 0.05
                      and (after_ex or {}).get("blockId") is None
                      and (after_ex or {}).get("curve") is False,
                      f"{before_ex} → {after_ex}")
                await c.ev("STUDIO.undo()")
                back_ex = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const it=S.proj.tracks.flatMap(t=>t.clips||[])"
                    f".find(c=>String(c.id)==={json.dumps(ex['clip'])});"
                    "return {srcId:it.sourceId,len:it.length,srcOut:it.srcOut,"
                    "blockId:it.blockId||null,curve:!!it.pitchCurve};})()")
                check("补音应用后一次 Ctrl+Z 全还原", back_ex == before_ex,
                      f"{before_ex} vs {back_ex}")

                # 收工:删测试轨和生成的音频
                await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"S.proj.tracks=S.proj.tracks.filter(t=>t.id!=={json.dumps(ex['tr'])});"
                    "S.sel.clear();STUDIO.drawStatic();return true;})()")
                if gname:
                    http("/api/studio/media/" + urllib.parse.quote(gname), "DELETE")

            # ---- 调音助手:拦截 /nl_plan,面板 → 勾选 → 应用 → 撤销 ----
            await c.ev(r"""
            (()=>{
              const orig = window.fetch;
              window.__nlOrig = orig;
              window.fetch = function(url, opt){
                const u = String(url || '');
                if (u.indexOf('/api/studio/nl/status') >= 0) {
                  return Promise.resolve(new Response(JSON.stringify(
                    {configured:true, model:'deepseek-chat',
                     masked:'sk-****test', source:'file'}),
                    {status:200, headers:{'Content-Type':'application/json'}}));
                }
                if (u.indexOf('/api/studio/nl_plan') >= 0) {
                  const S = STUDIO.S;
                  const tr = S.proj.tracks.find(t=>t.id===S.selTrack) || S.proj.tracks[0];
                  window.__nlBody = JSON.parse(opt.body);
                  const a = {id:'', kind:'gain', category:'vol', trackId:tr.id,
                             trackName:tr.name, from:tr.gain,
                             to:Math.min(1.5, Number(tr.gain)+0.2), why:'人声小一点'};
                  return Promise.resolve(new Response(JSON.stringify(
                    {ok:true, reply:'把人声提一点', actions:[a],
                     notes:[], dropped:0, sec_cost:0.1}),
                    {status:200, headers:{'Content-Type':'application/json'}}));
                }
                return orig.apply(this, arguments);
              };
              return true;
            })()
            """)
            try:
                await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "if(!S.proj.tracks.some(t=>t.id===S.selTrack))"
                    "S.selTrack=(S.proj.tracks[0]||{}).id;return true;})()")
                nlo = await c.ev(
                    "STUDIO.nlOpen().then(()=>({"
                    "open:!document.getElementById('nlPanel').hidden,"
                    "keyRowHidden:document.getElementById('nlKeyRow').hidden,"
                    "state:document.getElementById('nlState').textContent}))",
                    await_promise=True, timeout=30)
                check("调音助手:面板能打开、已配置 key 时不显示填 key 行",
                      isinstance(nlo, dict) and nlo.get("open") is True
                      and nlo.get("keyRowHidden") is True, str(nlo)[:200])

                await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "document.getElementById('nlScope').value='track';"
                    "document.getElementById('nlText').value='人声小一点';"
                    "return true;})()")
                await c.ev("STUDIO.nlAsk()", await_promise=True, timeout=60)
                plan_nl = await c.ev("STUDIO.nlPlan()")
                rows = await c.ev(
                    "document.querySelectorAll('#nlBody input[type=checkbox]').length")
                body_nl = await c.ev("window.__nlBody")
                check("调音助手:理解后给出可勾选的动作",
                      isinstance(plan_nl, dict) and len(plan_nl.get("actions") or []) == 1
                      and rows == 1, f"{plan_nl} rows={rows}")
                check("调音助手:请求里 sendNames 默认关、只发这句话和参数",
                      (body_nl or {}).get("sendNames") is False
                      and (body_nl or {}).get("text") == "人声小一点",
                      str(body_nl)[:160])

                g0 = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const tr=S.proj.tracks.find(t=>t.id===S.selTrack);"
                    "return tr.gain;})()")
                done_nl = await c.ev("STUDIO.nlApply()")
                g1 = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const tr=S.proj.tracks.find(t=>t.id===S.selTrack);"
                    "return tr.gain;})()")
                closed = await c.ev("document.getElementById('nlPanel').hidden")
                check("调音助手:应用后推子真的变了、面板关闭",
                      done_nl == 1 and abs(float(g1) - float(g0)) > 1e-6 and closed is True,
                      f"{g0} → {g1} done={done_nl} closed={closed}")
                await c.ev("STUDIO.undo()")
                g2 = await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    "const tr=S.proj.tracks.find(t=>t.id===S.selTrack);"
                    "return tr.gain;})()")
                check("调音助手:一次 Ctrl+Z 全还原",
                      abs(float(g2) - float(g0)) < 1e-9, f"{g0} vs {g2}")
            finally:
                await c.ev(
                    "(()=>{if(window.__nlOrig){window.fetch=window.__nlOrig;"
                    "delete window.__nlOrig;}delete window.__nlBody;return true;})()")

            why = await c.ev(
                "(()=>{const S=STUDIO.S,b0=S.blocks,n0=S.blocksName;"
                "S.blocks={blocks:[{kind:'silence'}]};S.blocksName='recordings/x_voice.wav';"
                "const w=STUDIO.blocksWhy();S.blocks=b0;S.blocksName=n0;return w;})()")
            check("切不出块时会说清楚为什么(不再静默不开页面)",
                  isinstance(why, str) and "精确零" in why, str(why))

            imp = await c.ev(r"""
            (async()=>{
              const sr=8000,n=sr;
              const ab=new ArrayBuffer(44+n*2), dv=new DataView(ab);
              const wr=(o,s)=>{for(let i=0;i<s.length;i++)dv.setUint8(o+i,s.charCodeAt(i));};
              wr(0,'RIFF');dv.setUint32(4,36+n*2,true);wr(8,'WAVEfmt ');
              dv.setUint32(16,16,true);dv.setUint16(20,1,true);dv.setUint16(22,1,true);
              dv.setUint32(24,sr,true);dv.setUint32(28,sr*2,true);dv.setUint16(32,2,true);
              dv.setUint16(34,16,true);wr(36,'data');dv.setUint32(40,n*2,true);
              for(let i=0;i<n;i++){dv.setInt16(44+i*2,
                Math.round(Math.sin(2*Math.PI*220*i/sr)*8000),true);}
              const f=new File([ab],'_ref_import_test.wav',{type:'audio/wav'});
              const tr=await STUDIO.refImportFile(f);
              if(!tr) return {err:'没建轨'};
              return {id:tr.id,name:tr.name,clips:(tr.clips||[]).length,
                      sel:document.getElementById('refRef').value};
            })()
            """, await_promise=True, timeout=90)
            check("跟原唱:能直接「导入原唱」→ 建轨并自动选成原唱轨",
                  isinstance(imp, dict) and imp.get("clips") == 1
                  and imp.get("sel") == imp.get("id"), str(imp))

            # 收工:把测试轨和上传的文件删掉
            if isinstance(imp, dict) and imp.get("id"):
                await c.ev(
                    "(()=>{const S=STUDIO.S;"
                    f"S.proj.tracks=S.proj.tracks.filter(t=>t.id!=={json.dumps(imp['id'])});"
                    "STUDIO.drawStatic();return true;})()")
                mnames = await c.ev(
                    "fetch('/api/studio/media_list').then(r=>r.json()).then(j=>"
                    "(j.files||[]).filter(f=>f.name.includes('_ref_import_test'))"
                    ".map(f=>f.name))", await_promise=True, timeout=20)
                for nm in mnames or []:
                    http("/api/studio/media/" + nm, "DELETE")

            # 删录音(界面路径):确认后真删、左栏跟着少一条
            # ★ 2026-10-22 起确认框是页面内的 .uiwrap（不再走原生 confirm）：
            #   这里用一个 25ms 的「自动按确定」小泵，弹出来就点。
            del_rec = await c.ev(
                "(()=>{const t=setInterval(()=>{const b=document.querySelector('.uiwrap .uik');"
                "if(b){clearInterval(t);b.click();}},25);"
                "return STUDIO.deleteRecording('smoke_blocks_voice.wav')"
                ".then(ok=>{clearInterval(t);"
                "return {ok:ok,still:STUDIO.S.recs.some(f=>f.name==='smoke_blocks_voice.wav')};})"
                ".catch(e=>{clearInterval(t);return {err:String(e)};});})()",
                await_promise=True, timeout=60)
            check("删录音(界面):确认后从盘上删掉、左栏也没了",
                  (del_rec or {}).get("ok") is True
                  and (del_rec or {}).get("still") is False, str(del_rec))

            # 删媒体(界面路径):同样真删、列表刷新
            ui_media = MEDIA / "_del_ui.wav"
            sf.write(str(ui_media), np.zeros(4800, dtype=np.float32), 48000)
            del_med = await c.ev(
                "(()=>{const t=setInterval(()=>{const b=document.querySelector('.uiwrap .uik');"
                "if(b){clearInterval(t);b.click();}},25);"
                "return STUDIO.deleteMedia({name:'_del_ui.wav',"
                "path:'projects/media/_del_ui.wav',dur:0.1})"
                ".then(ok=>{clearInterval(t);"
                "return {ok:ok,still:(STUDIO.S.media||[]).some(f=>f.name==='_del_ui.wav')};})"
                ".catch(e=>{clearInterval(t);return {err:String(e)};});})()",
                await_promise=True, timeout=60)
            check("删媒体(界面):确认后从盘上删掉、列表也没了",
                  (del_med or {}).get("ok") is True
                  and (del_med or {}).get("still") is False, str(del_med))

            # 删工程(界面路径):删的正好是当前打开的,自动保存不许写回来
            del_proj = await c.ev(
                "(()=>{return STUDIO.newProject().then(()=>{"
                "const f=STUDIO.S.file;"
                "return STUDIO.deleteProject(f).then(ok=>({ok:ok,file:f,now:STUDIO.S.file}));});})()",
                await_promise=True, timeout=60)
            await asyncio.sleep(1.4)            # 超过 800ms 的自动保存防抖
            dpid = (del_proj or {}).get("file") or "nope"
            st, _ = http("/api/studio/projects/" + urllib.parse.quote(dpid))
            check("删工程(界面):正在打开的删掉后,自动保存不会写回来",
                  (del_proj or {}).get("ok") is True and st == 404,
                  f"{del_proj} → HTTP {st}")
            check("删工程(界面):删完自动换到另一个工程",
                  isinstance(del_proj, dict) and del_proj.get("now")
                  and del_proj.get("now") != dpid, str(del_proj))
            check("删工程:按钮和函数都在",
                  bool(await c.ev(
                      "!!document.getElementById('btnDelProj')"
                      " && typeof STUDIO.deleteProject==='function'")))

            # ---- 阶段 9:性能 ----
            perf = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.clips||[]).length);"
                "const keep=tr.clips, c0=keep[0];"
                "tr.clips=Array.from({length:300},(_,i)=>({...c0,id:'perf_'+i,"
                "start:i*0.15,length:0.14,renderKey:null,renderUrl:null,pitchCurve:null}));"
                "STUDIO.drawStatic();"
                "const ts=[];for(let k=0;k<8;k++){const t0=performance.now();"
                "STUDIO.drawStatic();ts.push(performance.now()-t0);}"
                "ts.sort((a,b)=>a-b);const med=ts[4];"
                "tr.clips=keep;STUDIO.drawStatic();"
                "return {med:med,max:ts[ts.length-1]};})()")
            check("300 个块重绘(中位数)≥30fps", (perf or {}).get("med", 999) < 33,
                  f"中位 {float((perf or {}).get('med', -1)):.1f}ms · "
                  f"最慢 {float((perf or {}).get('max', -1)):.1f}ms")

            dec = await c.ev(
                "fetch('/api/recordings').then(r=>r.json()).then(j=>{"
                "const f=(j.files||[]).slice().sort((a,b)=>b.sec-a.sec)[0];"
                "if(!f)return {none:true};"
                "const t0=performance.now();"
                "return fetch('/recordings/'+encodeURIComponent(f.name))"
                ".then(r=>r.arrayBuffer()).then(ab=>{const t1=performance.now();"
                "const nb=ab.byteLength;"          # ★ decodeAudioData 会把 ArrayBuffer 摘掉(byteLength 变 0)
                "return STUDIO.engine.ensure().decodeAudioData(ab).then(buf=>({"
                "fetch_ms:t1-t0,decode_ms:performance.now()-t1,"
                "sec:buf.duration,sr:buf.sampleRate,ch:buf.numberOfChannels,"
                "mb:nb/1048576}));});})",
                await_promise=True, timeout=180)
            if isinstance(dec, dict) and dec.get("none"):
                # 这台机器上一条录音都没有(空装/已清空的公开版)—— 跳过,不算失败
                print("  - 最长录音解码 <3 秒(没有录音,跳过)", flush=True)
            elif isinstance(dec, dict):
                check("最长录音解码 <3 秒", dec.get("decode_ms", 1e9) < 3000,
                      f"{dec.get('sec', 0)/60:.1f} 分钟 · 取 {dec.get('mb', 0):.0f}MB "
                      f"{dec.get('fetch_ms', 0):.0f}ms · 解码 {dec.get('decode_ms', 0):.0f}ms")
            else:
                check("最长录音解码 <3 秒", False, "拿不到录音列表")

            # ---- 拆轨按钮:四轨 / 六轨各带各的档位(不真跑 demucs,只拦请求) ----
            gch = await c.ev(
                "(async()=>{const orig=window.fetch;const seen=[];"
                "window.fetch=async(url,opts)=>{const u=String(url);let body=null;"
                "try{body=opts&&opts.body?JSON.parse(opts.body):null;}catch(e){}"
                "seen.push({u,body});"
                "const data=u.includes('/gen/fetch')"
                "?{source:{path:'projects/media/x.wav',name:'x.wav',sec:3}}"
                ":(u.includes('/reference/stems')?{ok:true,sources:[]}:{});"
                "return {ok:true,headers:{get:()=>'application/json'},"
                "json:async()=>data,text:async()=>''};};"
                "try{document.getElementById('genPanel').hidden=false;"
                "STUDIO.genChoices('t_probe',[{path:'projects/media/x.wav',sec:3}]);"
                "const b4=document.querySelector('#genBody [data-split]');"
                "const b6=document.querySelector('#genBody [data-split6]');"
                "b6.click();await new Promise(r=>setTimeout(r,80));"
                "const s6=(seen.find(x=>x.u.includes('/reference/stems'))||{}).body||{};"
                "seen.length=0;b4.click();await new Promise(r=>setTimeout(r,80));"
                "const s4=(seen.find(x=>x.u.includes('/reference/stems'))||{}).body||{};"
                "return {has4:!!b4,has6:!!b6,m6:s6.model,m4:s4.model,"
                "src:s6.source};}finally{window.fetch=orig;"
                "document.getElementById('genPanel').hidden=true;}})()",
                await_promise=True, timeout=60)
            check("拆轨:四轨/六轨两个按钮都在,各自把档位发给后端",
                  isinstance(gch, dict) and gch.get("has4") and gch.get("has6")
                  and gch.get("m6") == "6" and gch.get("m4") == "4"
                  and gch.get("src") == "projects/media/x.wav", str(gch))

            # ---- 生成面板:大窗 + 双栏 + 字数 + 歌曲结构 ----
            # 他原话「那个框是真的小」⇒ 这里真量窗口尺寸和居中,不看样式表写没写
            gnew = await c.ev(
                "(async()=>{"
                "const p=document.getElementById('genPanel');p.hidden=false;"
                "await new Promise(r=>setTimeout(r,60));"
                "const r=p.getBoundingClientRect();"
                "const prov=document.getElementById('genProvider');"
                "prov.value='bailian';prov.dispatchEvent(new Event('change'));"
                "await new Promise(r=>setTimeout(r,30));"
                "const models=[...document.getElementById('genModel').options].map(o=>o.value);"
                "const vocal=document.getElementById('genVocal');vocal.checked=true;"
                "vocal.dispatchEvent(new Event('change'));"
                "const pr=document.getElementById('genPrompt');"
                "const lyr=document.getElementById('genLyrics');"
                "pr.value='R&B, 82 bpm, Rhodes';pr.dispatchEvent(new Event('input'));"
                "lyr.value='夜';lyr.dispatchEvent(new Event('input'));"
                "const pc=document.getElementById('genPromptCount').textContent;"
                "const lc=document.getElementById('genLyricsCount').textContent;"
                "const hint1=document.getElementById('genLyricsHint').textContent;"
                "lyr.value='';lyr.dispatchEvent(new Event('input'));"
                "const form=document.getElementById('genForm');form.value='pop';"
                "form.dispatchEvent(new Event('change'));"
                "const chips=document.querySelectorAll('#genFormChips [data-sec]').length;"
                "const len=document.getElementById('genFormLen').textContent;"
                "document.getElementById('genFormApply').click();"
                "const txt=lyr.value;"
                "p.hidden=true;"
                "return {w:Math.round(r.width),h:Math.round(r.height),"
                "cx:Math.round(r.left+r.width/2),vw:innerWidth,models,pc,lc,hint1,"
                "chips,len,txt:txt.slice(0,60)};})()",
                await_promise=True, timeout=40)
            gn = gnew if isinstance(gnew, dict) else {}
            check("生成面板够大且居中(≥1000×600,横向居中)",
                  gn.get("w", 0) >= 1000 and gn.get("h", 0) >= 600
                  and abs(gn.get("cx", 0) - gn.get("vw", 0) / 2) <= 2,
                  f"{gn.get('w')}×{gn.get('h')} · 中心 {gn.get('cx')} / 视口 {gn.get('vw')}")
            check("换平台会重铺模型清单(百炼只给 fun-music)",
                  gn.get("models") == ["auto", "fun-music-v1"], str(gn.get("models")))
            check("字数统计按平台给上限(百炼歌词 350 / 风格 1000)",
                  gn.get("pc") == "19 / 1000" and gn.get("lc") == "1 / 350",
                  f"风格 {gn.get('pc')} · 歌词 {gn.get('lc')}")
            check("歌词太短时明确说还差几个字",
                  "还差" in str(gn.get("hint1")), str(gn.get("hint1"))[:60])
            check("歌曲结构:模板出段落芯片 + 预计时长,套用后写进歌词",
                  gn.get("chips") == 9 and "[Intro]" in str(gn.get("txt"))
                  and "[Chorus]" in str(gn.get("txt")) and "小节" in str(gn.get("len")),
                  f"{gn.get('chips')} 段 · {gn.get('len')} · {str(gn.get('txt'))[:30]}")

            # ---- 结构模板要够多、要有地图;模型清单按「平台 × 人声/伴奏」筛 ----
            gext = await c.ev(
                "(async()=>{"
                "const p=document.getElementById('genPanel');p.hidden=false;"
                "const fs=document.getElementById('genForm');"
                "const nForms=fs.options.length;"
                "const prov=document.getElementById('genProvider');"
                "const vocal=document.getElementById('genVocal');"
                "prov.value='mureka';prov.dispatchEvent(new Event('change'));"
                "vocal.checked=true;vocal.dispatchEvent(new Event('change'));"
                "const songM=[...document.getElementById('genModel').options].map(o=>o.value);"
                "vocal.checked=false;vocal.dispatchEvent(new Event('change'));"
                "const instM=[...document.getElementById('genModel').options].map(o=>o.value);"
                "fs.value='edm';fs.dispatchEvent(new Event('change'));"
                "const drop=[...document.querySelectorAll('#genFormChips [data-sec]')]"
                ".some(b=>b.dataset.sec==='Drop');"
                "const maps=document.querySelectorAll('#genFormMap .gseg').length;"
                "const mapH=document.getElementById('genFormMap').getBoundingClientRect().height;"
                "fs.value='custom';fs.dispatchEvent(new Event('change'));"
                "const ci=document.getElementById('genFormCustom');"
                "ci.value='Intro 4, Verse 1 8, Chorus 16';ci.dispatchEvent(new Event('input'));"
                "const cchips=document.querySelectorAll('#genFormChips [data-sec]').length;"
                "const clen=document.getElementById('genFormLen').textContent;"
                "p.hidden=true;"
                "return {nForms,songM,instM,drop,maps,mapH,cchips,clen};})()",
                await_promise=True, timeout=40)
            ge = gext if isinstance(gext, dict) else {}
            check("结构模板够多(≥12 个,含电子/摇滚/古风/配乐)",
                  ge.get("nForms", 0) >= 12, str(ge.get("nForms")))
            check("EDM 模板带 Drop,歌曲地图按段数出块",
                  ge.get("drop") is True and ge.get("maps") == 7
                  and ge.get("mapH", 0) >= 20,
                  f"Drop={ge.get('drop')} · {ge.get('maps')} 块 · 高 {ge.get('mapH')}")
            check("自定义结构能写小节数(Intro 4 + Verse 8 + Chorus 16)",
                  ge.get("cchips") == 3 and "28 小节" in str(ge.get("clen")),
                  f"{ge.get('cchips')} 段 · {ge.get('clen')}")
            check("模型清单按平台×人声筛:mureka 带人声有 mureka-9,纯伴奏换成 7.5",
                  "mureka-9" in (ge.get("songM") or [])
                  and "mureka-9" not in (ge.get("instM") or [])
                  and "mureka-7.5" in (ge.get("instM") or []),
                  f"带人声 {ge.get('songM')} · 纯伴奏 {ge.get('instM')}")

            # ---- 参数化段落(照 MuseAI 大师写歌):前奏秒 / 主歌段 / 副歌遍 / 间奏 / 尾奏 ----
            gpar = await c.ev(
                "(async()=>{"
                "const p=document.getElementById('genPanel');p.hidden=false;"
                "const pr=document.getElementById('genPrompt');"
                "pr.value='R&B, 82 bpm, Rhodes';pr.dispatchEvent(new Event('input'));"
                "const fs=document.getElementById('genForm');"
                "fs.value='custom';fs.dispatchEvent(new Event('change'));"
                "const ci=document.getElementById('genFormCustom');ci.value='';"
                "ci.dispatchEvent(new Event('input'));"
                "const params=document.getElementById('genFormParams');"
                "const vis=!params.hidden;"
                "const setP=(id,v)=>{const el=document.getElementById(id);"
                "el.value=v;el.dispatchEvent(new Event('input'));};"
                "setP('gpIntro',10);setP('gpVerse',2);setP('gpChorus',3);setP('gpOutro',6);"
                "const gi=document.getElementById('gpInter');gi.checked=true;"
                "gi.dispatchEvent(new Event('change'));"
                "const secs=[...document.querySelectorAll('#genFormChips [data-sec]')]"
                ".map(b=>b.dataset.sec);"
                "const len=document.getElementById('genFormLen').textContent;"
                "const mapN=document.querySelectorAll('#genFormMap .gseg').length;"
                "setP('gpVerse',1);"
                "const secs2=document.querySelectorAll('#genFormChips [data-sec]').length;"
                "setP('gpIntro',99);"
                "const lenClamp=document.getElementById('genFormLen').textContent;"
                "fs.value='pop';fs.dispatchEvent(new Event('change'));"
                "const hid=params.hidden;"
                "p.hidden=true;"
                "return {vis,secs,len,mapN,secs2,lenClamp,hid};})()",
                await_promise=True, timeout=40)
            gp = gpar if isinstance(gpar, dict) else {}
            _secs = [str(x) for x in (gp.get("secs") or [])]
            check("自定义结构有参数化输入(前奏秒/主歌段/副歌遍/间奏/尾奏)",
                  gp.get("vis") is True and gp.get("hid") is True,
                  f"custom 显示={gp.get('vis')} · 切回模板后隐藏={gp.get('hid')}")
            check("按参数搭段落:2 主歌 3 副歌 + 间奏插在末遍副歌前",
                  len(_secs) == 8 and _secs[:2] == ["Intro", "Verse 1"]
                  and _secs[-3:] == ["Interlude", "Chorus", "Outro"]
                  and _secs.count("Chorus") == 3
                  and "49 小节" in str(gp.get("len"))
                  and gp.get("mapN") == 8,
                  f"{_secs} · {gp.get('len')}")
            check("改主歌段数会即时重算(2 段→1 段少一段主歌)",
                  gp.get("secs2") == 7, str(gp.get("secs2")))
            check("参数超界被夹紧(前奏 99 秒→30 秒,不是 99 秒)",
                  "48 小节" in str(gp.get("lenClamp")), str(gp.get("lenClamp")))

            # ---- 组合模板 / 分类标签 / 段落内小标签(耳语·独白·电话音…) ----
            gtag = await c.ev(
                "(async()=>{"
                "const p=document.getElementById('genPanel');p.hidden=false;"
                "const pump=setInterval(()=>{const b=document.querySelector('.uiwrap .uik');"
                "if(b)b.click();},25);"
                "const ta=document.getElementById('genPrompt');"
                "const lyr=document.getElementById('genLyrics');"
                "const vocal=document.getElementById('genVocal');"
                "vocal.checked=true;vocal.dispatchEvent(new Event('change'));"
                "ta.value='';ta.dispatchEvent(new Event('input'));"
                "const pre=document.querySelectorAll('#genPresets [data-preset]');"
                "const nPre=pre.length;"
                "pre[0].click();"
                "await new Promise(r=>setTimeout(r,160));"
                "const brief=ta.value;"
                "const briefLen=brief.length;"
                "ta.value='trap';ta.dispatchEvent(new Event('input'));"
                "const mood=document.querySelector('#genMoods .gchip');mood.click();"
                "const afterTag=ta.value;"
                "mood.click();"
                "const afterDup=ta.value;"
                "const instr=document.querySelector('#genInstrs .gchip');instr.click();"
                "const afterInstr=ta.value;"
                "lyr.value='';lyr.dispatchEvent(new Event('input'));"
                "const fx=[...document.querySelectorAll('#genFxChips [data-sec]')]"
                ".map(b=>b.dataset.sec);"
                "document.querySelector('#genFxChips [data-sec=\"Whisper\"]').click();"
                "const lyrFx=lyr.value;"
                "const fs=document.getElementById('genForm');"
                "const nForms=fs.options.length;"
                "fs.value='rapfull';fs.dispatchEvent(new Event('change'));"
                "const rapSecs=[...document.querySelectorAll('#genFormChips [data-sec]')]"
                ".map(b=>b.dataset.sec);"
                "fs.value='story';fs.dispatchEvent(new Event('change'));"
                "const storySecs=[...document.querySelectorAll('#genFormChips [data-sec]')]"
                ".map(b=>b.dataset.sec);"
                "const storyLbl=document.querySelector('#genFormChips [data-sec=\"Spoken\"]')"
                "?document.querySelector('#genFormChips [data-sec=\"Spoken\"]').textContent:'';"
                "clearInterval(pump);"
                "p.hidden=true;"
                "return {nPre,briefLen,bpm:/\\d+\\s?bpm/i.test(brief),afterTag,afterDup,"
                "afterInstr,fx,lyrFx,nForms,rapSecs,storySecs,storyLbl};})()",
                await_promise=True, timeout=40)
            gt = gtag if isinstance(gtag, dict) else {}
            check("组合模板:一整套风格说明一键填满(≥6 条,含 BPM/乐器/人声处理)",
                  gt.get("nPre", 0) >= 6 and gt.get("briefLen", 0) >= 60
                  and gt.get("bpm") is True,
                  f"{gt.get('nPre')} 条 · {gt.get('briefLen')} 字 · 带 BPM={gt.get('bpm')}")
            check("单项标签接在风格说明后面,重复点不会出现两遍",
                  gt.get("afterTag") == "trap, 温暖、治愈"
                  and gt.get("afterDup") == "trap, 温暖、治愈",
                  f"{gt.get('afterTag')!r} → {gt.get('afterDup')!r}")
            check("不同分类(流派/情绪/乐器/人声/节奏)都能续着接",
                  gt.get("afterInstr") == "trap, 温暖、治愈, Rhodes 电钢",
                  repr(gt.get("afterInstr")))
            check("段落内小标签够全(说唱/独奏/耳语/独白/机器人/电话音/磁带底噪/人群)",
                  {"Rap", "Solo", "Whisper", "Spoken", "Robot Voice", "Telephone",
                   "Tape Hiss", "Crowd"} <= set(gt.get("fx") or []),
                  str(gt.get("fx")))
            check("点「耳语」把 [Whisper] 插进歌词光标处",
                  "[Whisper]" in str(gt.get("lyrFx")), repr(gt.get("lyrFx"))[:40])
            check("结构模板新增说唱多段/叙事/耳语/现场/器乐独奏",
                  gt.get("nForms", 0) >= 18
                  and "Rap" in (gt.get("rapSecs") or [])
                  and "Spoken" in (gt.get("storySecs") or [])
                  and "独白" in str(gt.get("storyLbl")),
                  f"{gt.get('nForms')} 个 · rapfull={gt.get('rapSecs')} · "
                  f"story 独白={gt.get('storyLbl')}")

            # ---- 组合模板库:65 条 + 分类筛选 + 搜索(只读,不试听、不花钱) ----
            glib = await c.ev(
                "(async()=>{const p=document.getElementById('genPanel');p.hidden=false;"
                "document.getElementById('genLibOpen').click();"
                "await new Promise(r=>setTimeout(r,250));"
                "const rows=()=>document.querySelectorAll('#libList .librow').length;"
                "const all=rows();"
                "const cats=document.querySelectorAll('#libCats .libcat').length;"
                "const cnt=document.getElementById('libCount').textContent;"
                "const s=document.getElementById('libSearch');"
                "s.value='古风';s.dispatchEvent(new Event('input'));"
                "const f1=rows();"
                "s.value='zzzznomatchx';s.dispatchEvent(new Event('input'));"
                "const f2=rows();"
                "s.value='';s.dispatchEvent(new Event('input'));"
                "document.querySelector('#libCats [data-cat=\\'rap\\']').click();"
                "const rap=rows();"
                "document.querySelector('#libCats [data-cat=\\'all\\']').click();"
                "document.getElementById('libClose').click();"
                "p.hidden=true;"
                "return {all,cats,cnt,f1,f2,rap,back:rows()};})()",
                await_promise=True, timeout=40)
            gl = glib if isinstance(glib, dict) else {}
            check("组合模板库:一共 65 条,12 个分类 + 「全部」",
                  gl.get("all") == 65 and gl.get("cats") == 13
                  and "65" in str(gl.get("cnt")),
                  f"{gl.get('all')} 条 · {gl.get('cats')} 个分类 · 计数 {gl.get('cnt')!r}")
            check("模板库搜索真的在筛(古风 < 全量,乱码 → 0 条)",
                  0 < gl.get("f1", 0) < 65 and gl.get("f2") == 0
                  and gl.get("back") == 65,
                  f"古风 {gl.get('f1')} · 乱码 {gl.get('f2')} · 清空回来 {gl.get('back')}")
            check("模板库按分类筛:说唱 6 条",
                  gl.get("rap") == 6, f"{gl.get('rap')} 条")

            # ---- 新手引导:四步走完 / 上一步 / 关掉后记住 ----
            gob = await c.ev(
                "(async()=>{STUDIO.guide(0);await new Promise(r=>setTimeout(r,80));"
                "const s0=STUDIO.guideState();"
                "document.getElementById('obNext').click();"
                "await new Promise(r=>setTimeout(r,40));"
                "const s1=STUDIO.guideState();"
                "document.getElementById('obBack').click();"
                "await new Promise(r=>setTimeout(r,40));"
                "const s2=STUDIO.guideState();"
                "for(let i=0;i<4;i++){document.getElementById('obNext').click();"
                "await new Promise(r=>setTimeout(r,40));}"
                "const s3=STUDIO.guideState();"
                "return {s0,s1,s2,s3,flag:localStorage.getItem('singmic.onboard.v1')};})()",
                await_promise=True, timeout=30)
            go = gob if isinstance(gob, dict) else {}
            s0, s1, s2, s3 = (go.get("s0") or {}), (go.get("s1") or {}), \
                             (go.get("s2") or {}), (go.get("s3") or {})
            check("新手引导:四步,每步都有标题和要点",
                  s0.get("open") is True and s0.get("total") == 4
                  and s0.get("points", 0) >= 3 and bool(s0.get("title")),
                  f"第 {s0.get('step')} 步 · {s0.get('points')} 条 · {s0.get('title')!r}")
            check("新手引导:下一步 / 上一步都对",
                  s1.get("step") == 1 and s1.get("title") != s0.get("title")
                  and s2.get("step") == 0 and s2.get("title") == s0.get("title"),
                  f"1:{s1.get('step')} · back:{s2.get('step')}")
            check("新手引导:走完自动关,并记住「下次不再自动显示」",
                  s3.get("open") is False and go.get("flag") == "1",
                  f"open={s3.get('open')} · flag={go.get('flag')!r}")

            # ---- 设置:八个分页 + 偏好真的写进本机 + 关于页的版号/协议 ----
            gset = await c.ev(
                "(async()=>{document.getElementById('btnSettings').click();"
                "await new Promise(r=>setTimeout(r,250));"
                "const nav=[...document.querySelectorAll('#setNav .setitem')]"
                ".map(b=>b.dataset.sec);"
                "const body=document.getElementById('setBody');"
                "const heads={};"
                "for(const b of document.querySelectorAll('#setNav .setitem')){"
                "b.click();await new Promise(r=>setTimeout(r,180));"
                "heads[b.dataset.sec]=body.querySelector('h3')?.textContent||'';}"
                "document.querySelector('#setNav [data-sec=\\'prefs\\']').click();"
                "await new Promise(r=>setTimeout(r,120));"
                "const st=document.getElementById('prefStage');"
                "st.value='note';"
                "st.dispatchEvent(new Event('change',{bubbles:true}));"
                "const mode1=document.getElementById('blockMode').value;"
                "const prefs1=JSON.parse(localStorage.getItem('singmic.prefs.v1')||'{}');"
                "st.value='syllable';"
                "st.dispatchEvent(new Event('change',{bubbles:true}));"
                "document.querySelector('#setNav [data-sec=\\'data\\']').click();"
                "for(let i=0;i<100;i++){await new Promise(r=>setTimeout(r,200));"
                "if(!/读取中/.test(body.innerText))break;}"
                "const dataTxt=body.innerText;"
                "document.querySelector('#setNav [data-sec=\\'about\\']').click();"
                "await new Promise(r=>setTimeout(r,500));"
                "const aboutTxt=body.innerText;"
                "document.getElementById('setClose').click();"
                "return {nav,heads,mode1,prefs1,dataTxt,aboutTxt,"
                "closed:document.getElementById('settings').hidden};})()",
                await_promise=True, timeout=40)
            gs = gset if isinstance(gset, dict) else {}
            hs = gs.get("heads") or {}
            want_heads = {"prefs": "偏好设置", "data": "数据与缓存", "manual": "使用手册",
                          "faq": "常见问题", "keys": "快捷键速查",
                          "contact": "联系与反馈", "log": "更新日志", "about": ""}
            # ★ 关于页的标题是**版本名**(社区版/Pro 各一个名字),不写死;
            #   只要求它是产品名,别的一个字都不许变。
            ok_heads = all(
                ("声麦 Studio Pro" in str(hs.get("about"))) if k == "about"
                else hs.get(k) == v
                for k, v in want_heads.items())
            check("设置:八个分页都在,每页都有内容",
                  gs.get("nav") == list(want_heads.keys()) and ok_heads,
                  f"{gs.get('nav')} · {hs}")
            check("偏好:改「默认切法」立刻套到左栏,并写进本机 localStorage",
                  gs.get("mode1") == "note" and (gs.get("prefs1") or {}).get("stage") == "note",
                  f"blockMode={gs.get('mode1')} · prefs={gs.get('prefs1')}")
            check("数据与缓存页:缓存量和存放路径都写出来",
                  "缓存" in str(gs.get("dataTxt")) and "recordings" in str(gs.get("dataTxt"))
                  and "exports" in str(gs.get("dataTxt")),
                  str(gs.get("dataTxt"))[:60].replace(chr(10), " "))
            check("关于页:版号 / GPL-3.0 / 开发者 / 第三方组件",
                  "1.0.0" in str(gs.get("aboutTxt"))
                  and "GPL-3.0" in str(gs.get("aboutTxt"))
                  and "IMXia-Chan" in str(gs.get("aboutTxt"))
                  and "DeepFilterNet3" in str(gs.get("aboutTxt")),
                  str(gs.get("aboutTxt"))[:60].replace(chr(10), " "))
            check("设置:点 × 关掉", gs.get("closed") is True)

            # ---- 偏好存到本机文件(不在浏览器里):换壳 / 换浏览器不丢 ----
            def _prefs(method="GET", body=None):
                st, raw = http("/api/studio/prefs", method,
                               None if body is None else json.dumps(body).encode(),
                               "application/json" if body is not None else "")
                try:
                    return st, json.loads(raw)
                except Exception:                     # noqa: BLE001
                    return st, {}
            _st0, _p0 = _prefs()
            _before = _p0.get("prefs") or {}
            # ★ 量具不许留副作用:这文件是「这台电脑的真设置」,测试前没有就测完删掉,
            #   否则跑一次冒烟就把默认值焊死进配置文件,压过用户自己在浏览器里选的。
            _pf = Path(_p0.get("path") or "")
            _had = PREFS_HAD
            _prefs("POST", {"prefs": {"confirmCost": True}})
            _st1, _p1 = _prefs()
            # 非法值:切法只认 syllable/note,额外的键一律不落盘
            _prefs("POST", {"prefs": {"stage": "bogus", "evil": 1}})
            _st2, _p2 = _prefs()
            _prefs("POST", {"prefs": {"confirmCost": _before.get("confirmCost", False)}})
            check("偏好存进 ~/.singmic/prefs.json:合并写,非法值被丢",
                  _st1 == 200
                  and (_p1.get("prefs") or {}).get("confirmCost") is True
                  and (_p2.get("prefs") or {}).get("stage") == _before.get("stage")
                  and "evil" not in (_p2.get("prefs") or {}),
                  f"{_p1.get('prefs')} · 非法后={_p2.get('prefs')} · {_p1.get('path')}")
            if not _had and str(_pf):
                try:
                    _pf.unlink(missing_ok=True)   # 原来没有就还回去,别留测试造的文件
                except OSError:
                    pass

            # ---- 界面语言:设置里能换,换了真的换字,切回来原样还原 ----
            # ★ 这条同时验证三件事:字典文件真的能加载、DOM 改写生效、还原不残留译文。
            gl = await c.ev(
                "(async()=>{"
                "const m=await import('/static/studio/i18n.js');"
                "const read=()=>(document.getElementById('btnRecRefresh')||{}).textContent||'';"
                "const zh0=read();"
                "await m.setLang('en');"
                "await new Promise(r=>setTimeout(r,120));"
                "const en=read();"
                # ★ 手册里那条「手机录音」是**唯一靠整段 HTML 查表**的文案(键里带 <b><code>,
                #   漏一个空格就永远翻不出来,界面上还看不出错)。这里直接查它的键。
                "const k1=\"<b>手机录音</b>：用手机自带的录音功能或任意录音 App 录好后，"
                "把文件传到电脑，从「媒体 → 导入」加入工程；桌面版只在电脑本机提供服务，手机不能直连。\";"
                "const phoneEn=m.t(k1);"
                "await m.setLang('ja');"
                "await new Promise(r=>setTimeout(r,120));"
                "const ja=read();"
                "await m.setLang('zh-Hans');"
                "await new Promise(r=>setTimeout(r,120));"
                "return {zh0,en,ja,back:read(),phoneEn,langs:m.LANGS.map(l=>l.id),"
                "htmlLang:document.documentElement.lang};})()",
                await_promise=True, timeout=30)
            gl = gl if isinstance(gl, dict) else {}
            check("界面语言:五种可选,切英文/日文真的换字,切回来还原",
                  gl.get("langs") == ["zh-Hans", "zh-Hant", "en", "ja", "ko"]
                  and gl.get("zh0") == "刷新"
                  and gl.get("en") not in ("", "刷新")
                  and gl.get("ja") not in ("", "刷新", gl.get("en"))
                  and gl.get("back") == "刷新",
                  str(gl))
            check("界面语言:手册里那条「手机录音」整段 HTML 能查到英文译文",
                  str(gl.get("phoneEn", "")).startswith("<b>Phone recordings</b>"),
                  str(gl.get("phoneEn", ""))[:60])
            # 设置页里那个下拉框:跟随系统 + 五种语言 = 6 项
            glp = await c.ev(
                "(async()=>{"
                "const s=document.getElementById('prefLang');"
                "if(!s){document.getElementById('btnSettings').click();"
                "await new Promise(r=>setTimeout(r,250));"
                "document.querySelector('#setNav [data-sec=\\'prefs\\']').click();"
                "await new Promise(r=>setTimeout(r,150));}"
                "const el=document.getElementById('prefLang');"
                "const out={n:el?el.options.length:0,"
                "vals:el?[...el.options].map(o=>o.value):[],"
                "sel:el?el.value:null};"
                "document.getElementById('setClose')?.click();"
                "return out;})()",
                await_promise=True, timeout=20)
            glp = glp if isinstance(glp, dict) else {}
            check("设置里有「界面语言」下拉:跟随系统 + 五种语言",
                  glp.get("n") == 6
                  and glp.get("vals") == ["", "zh-Hans", "zh-Hant", "en", "ja", "ko"],
                  str(glp))

            # ---- 工程文件带身份:桌面版 / 别的工具靠这两个字段认版本 ----
            _stp, _rawp = http("/api/studio/projects/_smoke_proj")
            _jp = (json.loads(_rawp).get("project") or {}) if _stp == 200 else {}
            check("工程文件带身份:format + version(装壳之后靠它认版本)",
                  _jp.get("format") == "singmic-project" and int(_jp.get("version") or 0) >= 1,
                  f"format={_jp.get('format')} · version={_jp.get('version')}")

            # ---- 点面板外面就收(他要的:不用非得找那个 ×)+ 面板带了科技外壳 ----
            fsx = await c.ev(
                "(async()=>{"
                "const sc=()=>document.getElementById('fxScrim');"
                "const pan=document.getElementById('settings');"
                "const ids=['autoPanel','refPanel','voicePanel','extPanel','nlPanel',"
                "  'genPanel','genLib','drumPanel','expPanel'];"
                "document.getElementById('btnSettings').click();"
                "await new Promise(r=>setTimeout(r,420));"
                "const cv=pan.querySelector('.fxflow');"   # 粒子层在**面板里**,不在 scrim
                "const a={open:!pan.hidden, scrim:!sc().hidden, z:sc().style.zIndex,"
                "  fx:pan.classList.contains('fxpanel'), scan:!!pan.querySelector('.fxscan'),"
                "  corner:!!pan.querySelector('.fxcorner'), canvas:!!cv, inPanel:!!cv&&cv.parentElement===pan,"
                "  glass:(getComputedStyle(pan).backdropFilter||'').indexOf('blur')>=0,"
                "  bg:getComputedStyle(pan).backgroundColor};"
                # 粒子到底画没画:数 canvas 上的非零 alpha 像素,别只看元素在不在
                "a.pixel=(()=>{try{const c=cv.getContext('2d');"
                "  const d=c.getImageData(0,0,cv.width,cv.height).data;let n=0;"
                "  for(let i=3;i<d.length;i+=4)if(d[i])n++;return n;}catch(e){return -1;}})();"
                "document.querySelector('#setNav [data-sec=\\'faq\\']').click();"
                "await new Promise(r=>setTimeout(r,220));"
                "a.inside=!pan.hidden;"                      # 点面板里面不许误关
                "sc().click();"
                "await new Promise(r=>setTimeout(r,420));"
                "a.closed=pan.hidden; a.scrimGone=sc().hidden;"
                # 别的面板还开着 ⇒ scrim 本来就该留着(它服务的是「还有面板开着」)
                "a.others=ids.filter(i=>!document.getElementById(i).hidden);"
                "return a;})()", await_promise=True)
            fsk = fsx if isinstance(fsx, dict) else {}
            # 前面几步可能留着别的面板开着,这时 scrim 留着是**对的**;
            # 一个都不剩时它才必须收起来。
            _scrim_ok = (fsk.get("scrimGone") is False) if fsk.get("others") \
                else (fsk.get("scrimGone") is True)
            check("点面板外面就收:scrim 出现、点它就关、点面板里不关",
                  fsk.get("open") is True and fsk.get("scrim") is True
                  and fsk.get("inside") is True and fsk.get("closed") is True
                  and _scrim_ok,
                  f"{fsk}")
            check("面板外壳:科技边框 + 扫描带 + 四角括号挂上了",
                  fsk.get("fx") is True and fsk.get("scan") is True
                  and fsk.get("corner") is True,
                  f"fx={fsk.get('fx')} · scan={fsk.get('scan')} · z={fsk.get('z')}")
            # 玻璃:必须是真 backdrop-filter(不是画的假磨砂),底色必须半透明
            _bg = str(fsk.get("bg") or "")
            _alpha = 1.0
            try:
                _alpha = float(_bg.rsplit(",", 1)[1].rstrip(")").strip()) if "," in _bg else 1.0
            except (ValueError, IndexError):
                pass
            # ★ alpha 必须落在 (0, 0.9):0 说明根本没设底色(量具会假通过)
            check("磨砂玻璃:面板真 blur 了背景 + 底色半透明",
                  fsk.get("glass") is True and 0.05 < _alpha < 0.9,
                  f"blur={fsk.get('glass')} · bg={_bg}")
            check("面板里的粒子流:canvas 是面板的孩子且真的画上了",
                  fsk.get("canvas") is True and fsk.get("inPanel") is True
                  and isinstance(fsk.get("pixel"), int) and fsk.get("pixel", 0) > 200,
                  f"canvas={fsk.get('canvas')} · 在面板里={fsk.get('inPanel')} · 亮像素={fsk.get('pixel')}")

            # ---- 生成:面板选的平台必须原样发给后端(以前只认「最后存过 key 的平台」) ----
            gpost = await c.ev(
                "(async()=>{const orig=window.fetch,oc=window.confirm;const seen=[];"
                "const pump=setInterval(()=>{const b=document.querySelector('.uiwrap .uik');"
                "if(b)b.click();},25);"
                "window.fetch=async(url,opts)=>{const u=String(url);let body=null;"
                "try{body=opts&&opts.body?JSON.parse(opts.body):null;}catch(e){}"
                "seen.push({u,body});"
                "const data=u.includes('/gen/create')?{taskId:'t_probe',provider:'bailian'}:{};"
                "return {ok:true,headers:{get:()=>'application/json'},"
                "json:async()=>data,text:async()=>''};};"
                "try{"
                "const p=document.getElementById('genPanel');p.hidden=false;"
                "const prov=document.getElementById('genProvider');prov.value='bailian';"
                "prov.dispatchEvent(new Event('change'));"
                "document.getElementById('genVocal').checked=true;"
                "document.getElementById('genVocal').dispatchEvent(new Event('change'));"
                "const lyr=document.getElementById('genLyrics');"
                "lyr.value='夜色落在窗台上,我还是没有睡。';lyr.dispatchEvent(new Event('input'));"
                "document.getElementById('genGo').click();"
                "await new Promise(r=>setTimeout(r,220));"
                "clearInterval(pump);"
                "const body=(seen.find(x=>x.u.includes('/gen/create'))||{}).body||{};"
                "const busy=document.getElementById('genGo').disabled;"
                "document.getElementById('genClose').click();"
                "return {provider:body.provider,instrumental:body.instrumental,"
                "lyrics:!!body.lyrics,promptLen:(body.prompt||'').length,busy:busy};"
                "}finally{window.fetch=orig;clearInterval(pump);"
                "document.getElementById('genPanel').hidden=true;}})()",
                await_promise=True, timeout=40)
            gp = gpost if isinstance(gpost, dict) else {}
            check("生成:平台/人声/歌词按面板发出去,生成中按钮被按住",
                  gp.get("provider") == "bailian" and gp.get("instrumental") is False
                  and gp.get("lyrics") and gp.get("promptLen", 0) > 0 and gp.get("busy"),
                  str(gp))

            cache = await c.ev("STUDIO.cacheInfo()", await_promise=True)
            check("缓存占用报得出来", isinstance(cache, dict) and "mb" in cache, str(cache))

            # ★ 越界路径那条 404 是**故意**造的(浏览器会记网络日志),不是异常
            errs = [e for e in js_errors(c)
                    if not ("404" in e and "/api/blocks" in e)]
            check("页面没有 JS 报错", not errs, " | ".join(errs[:3]))
            ok = all(o for _, o, _ in RESULTS)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:                   # noqa: BLE001
            proc.kill()
        # ★ Edge 会派生出**脱离父进程**的子进程,只 terminate 父进程会留下一整窝
        #   (实测跑七轮积了 40 个,持续吃 CPU)。先按进程树杀,再按本轮调试端口收尾。
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        except Exception:                   # noqa: BLE001
            pass
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -match 'remote-debugging-port={DBG}' }} | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                 "-ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=25)
        except Exception:                   # noqa: BLE001
            pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        # 歌库是这个测试自己往里塞的 —— 收工时删掉,别让「歌库」变成垃圾场
        try:
            st, raw = http("/api/studio/reference/library?role=all")
            if st == 200:
                for e in json.loads(raw).get("entries") or []:
                    if str(e.get("name") or "").startswith("_rs_"):
                        http("/api/studio/reference/library/" + e["id"], "DELETE")
        except Exception:                   # noqa: BLE001
            pass
        for p in (PROJ / "_smoke_proj.json", PROJ / "_smoke_proj.json.bak",
                  pa, pb, pc, rs_ref, rs_take, rs_other,
                  rnm_a, rnm_dup, cgain,
                  MEDIA / "rnm_test2.wav",
                  PROJ / f"{created_proj}.json" if created_proj else PROJ / "_none_.json",
                  PROJ / f"{created_proj}.json.bak" if created_proj else PROJ / "_none_.bak"):
            p.unlink(missing_ok=True)

    n_ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {n_ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
