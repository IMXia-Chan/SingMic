"""端到端:VST 标签(真 Edge 无头 + CDP)。

验的是计划里「第 2 步」那几条:
  扫描 → 加插件 → 冻结出 sig → 正常播放走冻结 WAV(旧 clips 不再排)
  → 短段试听湿/干不同、改参数后声音跟着变
  → 改参数 → 黄标(stale)→ 400ms 防抖后 sig 变化并自动换 buffer
  → 渲染试听整曲 → 错路径红标 → 删插件 + 撤销 → 刷新后还在

跑法(服务在 8766 时):
    SMOKE_PORT=8766 SMOKE_DBG=9226 PYTHONIOENCODING=utf-8 \
      D:\\AI\\python.exe D:\\singmic-open\\tools\\smoke_vst.py

★ 需要真插件(Airwindows)。没有插件/没有 pedalboard 时打印 SKIP,不算失败。
"""
from __future__ import annotations

import asyncio
import io
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_studio import CDP, EDGE, DBG, BASE, MEDIA, check, js_errors, wait_for  # noqa: E402


def http_json(path: str, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:  # noqa: F821
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def make_media() -> str:
    MEDIA.mkdir(parents=True, exist_ok=True)
    sr = 48000
    t = np.arange(int(sr * 6.0)) / sr
    a = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    p = MEDIA / "_vst_smoke.wav"
    sf.write(str(p), a, sr, subtype="PCM_16")
    return "projects/media/_vst_smoke.wav"


AUD_JS = r"""
window.__vstAud = async function (bypass) {
  const S = STUDIO.S;
  const tr = S.proj.tracks.find(t => t.id === S.selTrack) || S.proj.tracks[0];
  const r = await fetch('/api/studio/vst/audition', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project: S.proj, trackId: tr.id, t0: 0, t1: 4, solo: true,
      bypass: !!bypass, sr: STUDIO.engine.ctx?.sampleRate || 48000,
    }),
  });
  if (!r.ok) return { status: r.status, err: (await r.text()).slice(0, 300) };
  const ab = await r.arrayBuffer();
  const buf = await STUDIO.engine.ctx.decodeAudioData(ab.slice(0));
  const d = buf.getChannelData(0);
  let ss = 0, pk = 0, zc = 0;
  for (let i = 0; i < d.length; i++) {
    const v = d[i];
    ss += v * v;
    if (Math.abs(v) > pk) pk = Math.abs(v);
    if (i && ((v >= 0) !== (d[i - 1] >= 0))) zc++;
  }
  return { status: 200, dur: buf.duration, rms: Math.sqrt(ss / d.length),
           peak: pk, zc };
};
true
"""


async def main() -> int:
    media = make_media()

    # ---- 1. HTTP:宿主 + 扫描 ----
    st, pj = http_json("/api/studio/vst/plugins")
    probe = pj.get("probe") or {}
    check("GET /vst/plugins 200", st == 200, f"HTTP {st}")
    check("宿主可用(pedalboard 起得来)", probe.get("ok") is True, str(probe))
    plugins = pj.get("plugins") or []
    good = [p for p in plugins if p.get("ok") and p.get("path")]
    check("扫到至少一个可用插件", bool(good),
          f"{[p.get('name') for p in plugins]}")
    if not good:
        print("\n没有可用插件 —— SKIP 余下用例(装一个 .vst3 再跑)")
        return 0
    plug = good[0]

    st, ins = http_json("/api/studio/vst/inspect", "POST",
                        json.dumps({"path": plug["path"]}).encode())
    check("inspect 200 且有参数表",
          st == 200 and isinstance(ins.get("params"), list) and len(ins["params"]) > 0,
          f"HTTP {st} params={len(ins.get('params') or [])}")
    nump = [p for p in (ins.get("params") or [])
            if p.get("type") == "number" and isinstance(p.get("min"), (int, float))
            and isinstance(p.get("max"), (int, float)) and p["max"] > p["min"]]
    if not nump:
        print("\n这个插件没有带范围的数值参数 —— SKIP 参数相关用例")
        nump = []
    param = nump[0] if nump else None

    st, _ = http_json("/api/studio/vst/inspect", "POST",
                      json.dumps({"path": r"C:\Windows\notepad.exe"}).encode())
    check("越界插件路径被拒(400)", st == 400, f"HTTP {st}")

    # ★ 加载 ≠ 能出声。有的插件声明「2 进 2 出」(Dragonfly 四个混响),
    #   inspect 过得好好的,一 process 就 ValueError。这里每个插件真跑一段,
    #   并要求时长对得上 —— 只 inspect 不渲染会漏掉整整一类插件。
    for pl in good:
        proj = {
            "version": 1, "name": "vst_probe", "bpm": 120, "timeSig": [4, 4],
            "lengthSec": 8,
            "sources": {"src_probe": {"kind": "voice", "path": media,
                                      "sr": 48000, "ch": 1, "dur": 6.0}},
            "tracks": [{"id": "tr_probe", "name": "探针", "kind": "voice",
                        "gain": 1, "pan": 0,
                        "clips": [{"id": "cl_probe", "sourceId": "src_probe",
                                   "start": 0.0, "srcIn": 0.0, "srcOut": 6.0,
                                   "length": 6.0, "semis": 0, "cents": 0,
                                   "kind": "sound"}],
                        "fx": {"vst": [{"id": "vst_probe", "name": pl["name"],
                                        "bundle": pl["bundle"],
                                        "path": pl["path"],
                                        "bypass": False, "params": {}}]}}],
        }
        req = urllib.request.Request(
            BASE + "/api/studio/vst/audition",
            data=json.dumps({"project": proj, "trackId": "tr_probe",
                             "t0": 0, "t1": 4, "solo": True,
                             "bypass": False, "sr": 48000}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            y, _ = sf.read(io.BytesIO(raw), dtype="float32")
            ok = abs(len(y) / 48000 - 4.0) < 0.02 and np.all(np.isfinite(y))
            detail = f"{len(y) / 48000:.2f}s peak={np.max(np.abs(y)):.3f}"
        except urllib.error.HTTPError as e:   # noqa: F821
            ok = False
            detail = f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:160]}"
        check(f"插件真的能渲染:{pl['name']}", ok, detail)

    # ★ 空轨(一个 clip 都没有)冻结:绝不能产出 0 采样的 WAV ——
    #   浏览器 decodeAudioData 会报「Unable to decode audio data」,
    #   用户看到的是「重算失败」,像是插件坏了。
    empty_proj = {
        "version": 1, "name": "vst_empty", "bpm": 120, "timeSig": [4, 4],
        "lengthSec": 8, "sources": {},
        "tracks": [{"id": "tr_empty", "name": "空轨", "kind": "voice",
                    "gain": 1, "pan": 0, "clips": [],
                    "fx": {"vst": [{"id": "vst_e", "name": plug["name"],
                                    "bundle": plug["bundle"], "path": plug["path"],
                                    "bypass": False, "params": {}}]}}],
    }
    st, fr = http_json("/api/studio/vst/freeze", "POST",
                       json.dumps({"project": empty_proj,
                                   "trackId": "tr_empty", "sr": 48000}).encode())
    check("空轨冻结:明确回「无需冻结」,不给 0 采样 WAV",
          st == 200 and fr.get("empty") is True and not fr.get("url"),
          f"HTTP {st} {json.dumps(fr, ensure_ascii=False)[:160]}")

    # ---- 2. 浏览器 ----
    tmp = Path(tempfile.mkdtemp(prefix="st-vst-"))
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
        f"--user-data-dir={tmp}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio", "--window-size=1500,1000",
        "--autoplay-policy=no-user-gesture-required",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        version = None
        for _ in range(60):
            try:
                version = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{DBG}/json/version", timeout=1).read())
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.3)
        if not version:
            print("Edge 没起来")
            return 2

        req = urllib.request.Request(
            f"http://127.0.0.1:{DBG}/json/new?about:blank", method="PUT")
        tgt = json.loads(urllib.request.urlopen(req, timeout=5).read())
        async with websockets.connect(tgt["webSocketDebuggerUrl"],
                                      max_size=2 ** 25) as ws:
            c = CDP(ws)
            await c.call("Runtime.enable")
            await c.call("Log.enable")
            await c.call("Page.enable")
            await c.call("Page.navigate", {"url": BASE + "/studio"})

            got = await wait_for(
                c, "typeof STUDIO !== 'undefined' && STUDIO.ready === true",
                lambda v: v is True, 45)
            check("页面打开,STUDIO 就位", bool(got))
            if not got:
                raise RuntimeError("页面没起来:" + " | ".join(js_errors(c)[:3]))

            await c.ev("STUDIO.newProject()", await_promise=True)
            await c.ev(
                "(()=>{const S=STUDIO.S;"
                f"STUDIO.addClip({{kind:'media',name:'_vst_smoke.wav',"
                f"path:{json.dumps(media)},dur:6}},0,null);"
                "S.proj.lengthSec=6;S.proj.ui.loop=[0,4];"
                "return S.proj.tracks.length})()")
            await c.ev("STUDIO.save()", await_promise=True)
            proj_name = await c.ev("STUDIO.S.file")
            check("建工程 + 一条 6 秒的轨", isinstance(proj_name, str), str(proj_name))

            # VST 标签真的存在,点得开
            tab = await c.ev(
                "(()=>{const b=document.querySelector('.tabs button[data-tab=\"vst\"]');"
                "if(!b) return null;b.click();"
                "return {hidden:document.getElementById('panelVst').hidden}})()")
            check("VST 标签存在且能打开",
                  isinstance(tab, dict) and tab.get("hidden") is False, str(tab))

            await c.ev("STUDIO.vstScan()", await_promise=True, timeout=120)
            listed = await c.ev(
                "(()=>({list:(STUDIO.vstPlugins()||[]).map(p=>p.name),"
                " probe:STUDIO.vstProbe()}))()")
            check("页面里扫描到插件",
                  isinstance(listed, dict) and len(listed.get("list") or []) > 0,
                  str((listed or {}).get("list")))
            check("页面上宿主探针 ok",
                  isinstance(listed, dict)
                  and (listed.get("probe") or {}).get("ok") is True,
                  str((listed or {}).get("probe")))

            # 加插件
            added = await c.ev(
                f"(()=>{{const ok=STUDIO.vstAdd({json.dumps(plug['path'])});"
                "const tr=STUDIO.fxTrack();"
                "return {ok, slots:STUDIO.vstSlots().length, freeze:!!(tr.fx&&tr.fx.freeze),"
                " id:STUDIO.vstSlots()[0]?.id, tid:tr.id}})()")
            check("加到轨上,冻结默认开",
                  isinstance(added, dict) and added.get("ok") is True
                  and added.get("slots") == 1 and added.get("freeze") is True,
                  str(added))
            slot_id = (added or {}).get("id")
            track_id = (added or {}).get("tid")

            # ★ 滑杆得说人话:显示自然值(1.30 s,不是 0–1)、参数名/提示是中文、
            #   缓冲/采样率这种技术参数收在默认不展开的折叠区里。
            #   拿 Dragonfly 验 —— 它的 decay_s 有明确单位,Airwindows 全是 0–1。
            # ★ 要 Hall:它的 decay_s 默认 1.3 s,能证明显示的是自然值
            #   (EarlyReflections 只有百分比和米,证明力弱一些)
            dfly = (next((p for p in good if "Hall" in (p.get("name") or "")), None)
                    or next((p for p in good if "Dragonfly" in (p.get("name") or "")), None))
            if dfly:
                dtr = await c.ev(
                    "(()=>{const tr=STUDIO.addTrack('参数面板');"
                    f"STUDIO.vstAdd({json.dumps(dfly['path'])},tr);"
                    "return {tid:tr.id}})()")
                await wait_for(
                    c, "document.querySelectorAll('#vstParams .fxparam').length",
                    lambda v: isinstance(v, int) and v > 0, 90)
                pg = await c.ev(
                    "(()=>{const b=document.getElementById('vstParams');"
                    "const rows=[...b.querySelectorAll('.fxparam')];"
                    "const det=b.querySelector('details.vsttech');"
                    "return {n:rows.length,"
                    " labs:rows.map(r=>{const l=r.querySelector('label');"
                    "return l?l.textContent:'';}),"
                    " outs:rows.map(r=>{const o=r.querySelector('output');"
                    "return o?o.textContent:'';}),"
                    " hints:[...b.querySelectorAll('.phint')].map(x=>x.textContent),"
                    " det:!!det, open:det?det.open:null,"
                    " techN:det?det.querySelectorAll('.fxparam').length:0};})()")

                def _cjk(s):
                    return any("一" <= ch <= "鿿" for ch in str(s))

                labs = (pg or {}).get("labs") or []
                outs = (pg or {}).get("outs") or []
                hints = (pg or {}).get("hints") or []
                check("VST 滑杆显示自然值(衰减 1.30 s,不是 0–1)",
                      isinstance(pg, dict) and "1.30 s" in outs,
                      f"outs={outs[:6]}")
                check("VST 参数名与提示都是中文",
                      any(_cjk(x) for x in labs) and any(_cjk(x) for x in hints),
                      f"labs={labs[:4]} · hints={hints[:2]}")
                check("技术参数收在默认折叠的 <details> 里",
                      isinstance(pg, dict) and pg.get("det") is True
                      and pg.get("open") is False and (pg.get("techN") or 0) > 0,
                      f"det={(pg or {}).get('det')} open={(pg or {}).get('open')}"
                      f" n={(pg or {}).get('techN')}")
                # 清理:删掉临时轨上的插件,「当前轨」换回主轨(后面全靠它)
                await c.ev(
                    "(()=>{const tr=STUDIO.S.proj.tracks.find(t=>t.id==="
                    f"{json.dumps(dtr['tid'])});"
                    "STUDIO.S.selTrack=tr.id;"
                    "const id=(tr.fx&&tr.fx.vst&&tr.fx.vst[0])?tr.fx.vst[0].id:null;"
                    "if(id)STUDIO.vstRemove(id);"
                    f"STUDIO.S.selTrack={json.dumps(track_id)};"
                    "return true})()")

            # 冻结(显式等它算完)
            fz = await c.ev(
                f"STUDIO.vstFreezeNow({json.dumps(track_id)})",
                await_promise=True, timeout=180)
            check("冻结拿到 sig",
                  isinstance(fz, dict) and fz.get("state") == "ok"
                  and len(str(fz.get("sig") or "")) == 20
                  and float(fz.get("sec") or 0) > 0.5,
                  str(fz))
            old_sig = (fz or {}).get("sig")
            # ★ Airwindows Consolidated 自带抖动噪声,两次渲染就不同 ⇒ 应为 true。
            #   这一条既验「探针真的会响」,也记录这台机上的事实。
            check("冻结响应带 nondet 判定(此插件实测两次不同)",
                  isinstance((fz or {}).get("nondet"), bool),
                  f"nondet={(fz or {}).get('nondet')}")

            eng = await c.ev(
                "(()=>{const fz=STUDIO.engine.frozen.get(STUDIO.fxTrack().id);"
                "return {ids:STUDIO.engineFrozen(),"
                " dur:fz&&fz.buffer?fz.buffer.duration:0,"
                " gain:fz?fz.gainAtBake:null}})()")
            check("浏览器拿到了冻结 buffer(6 秒)",
                  isinstance(eng, dict) and track_id in (eng.get("ids") or [])
                  and abs(float(eng.get("dur") or 0) - 6.0) < 0.05, str(eng))

            # 正常播放:这一轨排的是冻结 WAV,不是原来那些 clip
            await c.ev("STUDIO.seekTo(0);STUDIO.play()")
            await asyncio.sleep(1.0)
            live = await c.ev(
                "(()=>{const e=STUDIO.engine;const tid=STUDIO.fxTrack().id;"
                "const fz=e.frozen.get(tid);"
                "const srcs=e.live.filter(s=>s._tr===tid);"
                "return {playing:e.playing, n:srcs.length,"
                " frozen:srcs.length>0&&srcs.every(s=>s.buffer===fz.buffer),"
                " t:e.songNow()}})()")
            check("播放中该轨走冻结 WAV(旧 clips 不再排)",
                  isinstance(live, dict) and live.get("playing") is True
                  and live.get("n", 0) > 0 and live.get("frozen") is True,
                  str(live))
            await c.ev("STUDIO.engine.pause();true")

            # 短段试听:湿 ≠ 干
            await c.ev(AUD_JS)
            wet = await c.ev("window.__vstAud(false)", await_promise=True, timeout=120)
            dry = await c.ev("window.__vstAud(true)", await_promise=True, timeout=120)
            check("试听返回 4 秒 WAV",
                  isinstance(wet, dict) and wet.get("status") == 200
                  and abs(float(wet.get("dur") or 0) - 4.0) < 0.02
                  and isinstance(dry, dict) and dry.get("status") == 200,
                  str({k: wet.get(k) for k in ("status", "dur", "rms")} if isinstance(wet, dict) else wet))
            check("干声 = 输入(220Hz 0.25 → RMS≈0.177)",
                  isinstance(dry, dict) and abs(float(dry.get("rms") or 0) - 0.1768) < 0.01,
                  f"dry rms={dry.get('rms') if isinstance(dry, dict) else dry}")
            wet_rms = float(wet.get("rms") or 0) if isinstance(wet, dict) else 0.0
            dry_rms = float(dry.get("rms") or 0) if isinstance(dry, dict) else 0.0
            check("湿声与干声真的不同(>1%)",
                  abs(wet_rms - dry_rms) / max(1e-9, dry_rms) > 0.01,
                  f"wet {wet_rms:.4f} vs dry {dry_rms:.4f}")

            # 「试听这一段」按钮:真的循环播
            await c.ev("STUDIO.vstAudToggle()", await_promise=True, timeout=120)
            aud = await c.ev("STUDIO.vstAudState()")
            lat = await c.ev("document.getElementById('vstLat').textContent")
            check("试听在循环播放,并显示往返耗时",
                  isinstance(aud, dict) and aud.get("active") is True
                  and aud.get("auditioning") is True
                  and isinstance(lat, str) and "往返" in lat, f"{aud} · {lat}")
            await c.ev("STUDIO.vstStopAudition();true")

            # 改参数:立刻黄标 → 防抖后 sig 变化
            if param:
                before = wet_rms
                stt = await c.ev(
                    f"(()=>{{STUDIO.vstSetParam({json.dumps(slot_id)},"
                    f"{json.dumps(param['key'])},{json.dumps(param['max'])});"
                    "return {state:STUDIO.vstFreezeState(),"
                    " cls:document.getElementById('vstFreezeState').className}})()")
                check("改参数立刻变「未同步」黄标",
                      isinstance(stt, dict)
                      and (stt.get("state") or {}).get("state") == "stale"
                      and "stale" in str(stt.get("cls")), str(stt))
                newfz = await wait_for(
                    c,
                    "(()=>{const s=STUDIO.vstFreezeState();return s&&s.state==='ok'"
                    f"&&s.sig!=={json.dumps(old_sig)}?s:null}})()",
                    lambda v: isinstance(v, dict), 180)
                check("400ms 防抖后自动重算,sig 已变",
                      isinstance(newfz, dict)
                      and len(str(newfz.get("sig"))) == 20, str(newfz))
                wet2 = await c.ev("window.__vstAud(false)", await_promise=True, timeout=120)
                r2 = float(wet2.get("rms") or 0) if isinstance(wet2, dict) else 0.0
                check("参数变了,试听波形跟着变",
                      isinstance(wet2, dict) and wet2.get("status") == 200
                      and abs(r2 - before) > max(1e-4, before * 0.005),
                      f"{before:.4f} → {r2:.4f}")
            else:
                newfz = fz

            # 整曲「渲染试听」:走导出路径
            await c.ev("STUDIO.vstPreviewAll()", await_promise=True, timeout=300)
            prev = await c.ev(
                "(()=>{const a=document.getElementById('vstPrev');"
                "return {src:a.src, st:document.getElementById('vstPrevState').textContent}})()")
            purl = (prev or {}).get("src") or ""
            okprev = False
            if purl:
                with urllib.request.urlopen(purl, timeout=60) as r:
                    raw = r.read()
                okprev = r.status == 200 and len(raw) > 10000 and raw[:4] == b"RIFF"
            check("渲染试听整曲:拿到非空 WAV,状态写明完成",
                  okprev and "完成" in str((prev or {}).get("st")),
                  f"{purl} · {(prev or {}).get('st')}")

            # 错路径:红标,不是静默干声
            bad = await c.ev(
                "(()=>{const tr=STUDIO.addTrack('坏插件');"
                "const ok=STUDIO.vstAdd('C:\\\\Windows\\\\notepad.exe',tr);"
                "return {ok,tid:tr.id,id:STUDIO.vstSlots()[0]?.id}})()")
            check("坏路径也加得上(错误留到冻结时暴露)",
                  isinstance(bad, dict) and bad.get("ok") is True, str(bad))
            bstate = await wait_for(
                c,
                "(()=>{const s=STUDIO.vstFreezeState();return s&&s.state==='error'?s:null})()",
                lambda v: isinstance(v, dict), 60)
            btext = await c.ev(
                "(()=>{const e=document.getElementById('vstFreezeState');"
                "return {cls:e.className, txt:e.textContent}})()")
            check("冻结失败 → 红标写明原因",
                  isinstance(bstate, dict)
                  and "error" in str((btext or {}).get("cls"))
                  and "重算失败" in str((btext or {}).get("txt")),
                  str(btext))

            # ★ 回归:面板必须跟着「当前轨」走,不能挂着上一条轨的槽位
            #   (以前只有点底部标签才重画;在播放列表里换轨后点 × 会删错轨,
            #    看起来就是「删了没反应,刷新才对」)
            tid_bad = (bad or {}).get("tid")
            sw = await c.ev(
                "(()=>{const bad=" + json.dumps(tid_bad) + ";"
                "const other=STUDIO.S.proj.tracks.find(t=>t.id!==bad);"
                "STUDIO.S.selTrack=other.id;"
                "const h1=document.getElementById('vstTrackName').textContent;"
                "STUDIO.S.selTrack=bad;"
                "const h2=document.getElementById('vstTrackName').textContent;"
                "return {want1:other.name,want2:"
                "(STUDIO.S.proj.tracks.find(t=>t.id===bad)||{}).name,"
                "h1,h2}})()")
            check("切换当前轨,面板立刻换成那条轨",
                  isinstance(sw, dict)
                  and sw.get("h1") == sw.get("want1")
                  and sw.get("h2") == sw.get("want2"), str(sw))

            # 删掉 + 撤销
            bad_id = (bad or {}).get("id")
            rm = await c.ev(
                f"(()=>{{const n0=STUDIO.S.drawStaticN||0;"
                f"STUDIO.vstRemove({json.dumps(bad_id)});"
                "return {slots:STUDIO.vstSlots().length,"
                " frozen:STUDIO.engineFrozen(), n0, n1:STUDIO.S.drawStaticN||0}})()")
            check("删插件后槽位空、冻结取消",
                  isinstance(rm, dict) and rm.get("slots") == 0
                  and (bad or {}).get("tid") not in (rm.get("frozen") or []), str(rm))
            # ★ 轨头那个绿/黄/红的「VST」小标画在 canvas 上:删除后必须重画,
            #   否则下面板空着、画布上的标还在 —— 就是「刷新才消失」
            check("删插件后画布上的 VST 标立刻消失(不用刷新)",
                  isinstance(rm, dict) and rm.get("n1", 0) > rm.get("n0", 0), str(rm))
            await c.ev("STUDIO.undo();true")
            await asyncio.sleep(0.3)
            und = await c.ev("STUDIO.vstSlots().length")
            check("撤销把插件还回来", und == 1, f"slots={und}")
            # ★ 撤销会换掉整个工程对象;面板必须当场重画,不许等刷新
            rows_und = await c.ev(
                "document.querySelectorAll('#vstSlots .fxslot').length")
            check("撤销后面板立刻显示插件(不用刷新)",
                  rows_und == 1, f"rows={rows_und}")

            # 刷新:插件与冻结状态都在
            await c.ev("STUDIO.save()", await_promise=True)
            await c.call("Page.reload")
            got2 = await wait_for(
                c, "typeof STUDIO !== 'undefined' && STUDIO.ready === true",
                lambda v: v is True, 60)
            check("刷新后页面就位", bool(got2))
            # 刷新后载入的是最近那个工程(坏插件那一条也在里面)
            await wait_for(
                c, "STUDIO.S.proj && STUDIO.S.proj.tracks.length >= 2",
                lambda v: v is True, 60)
            kept = await c.ev(
                "(()=>{const S=STUDIO.S;"
                "const tr=S.proj.tracks.find(t=>(t.fx&&t.fx.vst||[]).length"
                "&&!(t.fx.vst[0].path||'').includes('notepad'));"
                "if(!tr) return {file:S.file, tracks:S.proj.tracks.length};"
                "S.selTrack=tr.id;STUDIO.vstOpen(tr.id);"
                "return {file:S.file, tracks:S.proj.tracks.length, tid:tr.id,"
                " vst:(tr.fx.vst||[]).length, freeze:!!tr.fx.freeze}})()")
            check("刷新后 VST 槽位与冻结开关都在",
                  isinstance(kept, dict) and kept.get("vst") == 1
                  and kept.get("freeze") is True, str(kept))
            rebaked = await wait_for(
                c,
                "(()=>{const s=STUDIO.vstFreezeState();"
                "return s&&s.state==='ok'?s:null})()",
                lambda v: isinstance(v, dict), 180)
            check("刷新后自动重算冻结",
                  isinstance(rebaked, dict) and len(str(rebaked.get("sig") or "")) == 20,
                  str(rebaked))

            # 坏插件那一路的 400 是**故意**造的(浏览器会记网络日志),不是异常
            errs = [e for e in js_errors(c)
                    if not ("400" in e
                            and ("/vst/inspect" in e or "/vst/freeze" in e))]
            check("全程无 JS 异常", not errs, " | ".join(errs[:3]))
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        # ★ 每次跑都留一个几百 MB 的 Edge 配置目录 ⇒ 跑完就删,别攒成 1.6G
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in __import__("smoke_studio").RESULTS if not r[1]]
    print(f"\n{len(__import__('smoke_studio').RESULTS) - len(bad)}/"
          f"{len(__import__('smoke_studio').RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(asyncio.run(main()))
