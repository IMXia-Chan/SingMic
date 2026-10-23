# -*- coding: utf-8 -*-
"""SingMic 桌面壳 —— 双击 exe 弹出的那个窗口。

为什么要有这一层:现在整套东西是「Python 服务 + 浏览器打开 127.0.0.1」。
装壳之后由本进程负责:
  1. 抢一个空闲端口(不再写死 8765,撞了就起不来)
  2. 在本进程的线程里把服务拉起来(不开控制台、不留黑窗)
  3. 拿 WebView2 开一个真窗口,里面装的还是那套 studio 页面
  4. 窗口关掉 = 整个进程退出(服务和音频线程一起走,不留僵尸)

★ 单实例:命名互斥量。双击第二次会弹一句话然后退出 —— 两个实例写同一个工程会打架。
★ 数据目录:打包后的 exe 把 `SINGMIC_DATA` 设到 `%LOCALAPPDATA%\\SingMic\\data`
  (安装目录在 Program Files 下只读,写不进去);开发时直接跑 desktop.py 不设,
  仍然用程序目录。目录本身由 studio_mix 算,这一层只负责把环境变量交出去。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import queue
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ★ `-I`(隔离模式)不会把脚本目录塞进 sys.path —— server.py 自己干过一次,
#   这里也得自己干,否则一跑就是「No module named 'server'」。
BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import edition as _edition                                          # noqa: E402

# ★ 产品只有一个「声麦 Studio Pro」。数据目录、互斥量、标题**不随功能开关变化** ——
#   edition.py 只管接口放不放行,不该在盘上另起一套目录。
APP_NAME = "SingMic"
WIN_TITLE = _edition.label()       # 商店预留名;exe 名仍是 SingMic.exe,不动
MUTEX = "SingMic.Studio.Desktop.v1"


def _boot_log_path() -> Path:
    root = os.environ.get("SINGMIC_DATA")
    if root:
        return Path(root) / "boot.log"
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / APP_NAME / "boot.log"


_T_START = time.time()
# ★ 服务端(boot.log 的另一半)要和我用同一个起算点,不然两边的 +ms 对不上
os.environ.setdefault("SINGMIC_T0", str(_T_START))


def _blog(msg: str) -> None:
    """启动过程写一行日志。

    为什么要有:打包版是没有控制台的,启动卡在第二步时用户只看到一条不动的进度条,
    连句错误都没有 —— 上一版就是在「启动本地服务」那里空转了 60 秒而没法查。
    日志落在数据目录的 boot.log(每次启动覆盖),出问题先看它。
    ★ 每行带「距进程启动多少毫秒」—— 只到秒的话,快慢全看不出来。
    """
    try:
        p = _boot_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        ms = int((time.time() - _T_START) * 1000)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} +{ms:6d}ms {msg}\n")
    except Exception:                         # noqa: BLE001
        pass                                  # 日志写不出来也不能挡启动


_TRACE_SAMPLES: dict = {}


def _trace_sample_start():
    """启动阶段「这段时间到底在跑哪一行」的采样器(只在 TRACE 下开)。

    冻结包里 `import fastapi` 要 5 秒,同样的代码在普通 Python 里只要 0.4 秒 ——
    光看模块粒度只能知道「是 fastapi」,再往下就得知道**哪一行**在耗时间。
    每 20ms 看一次启动线程的栈顶,最后按「停留时长」汇总。
    """
    import sys
    ident = threading.get_ident()
    stop = threading.Event()

    def _run() -> None:
        while not stop.is_set():
            fr = sys._current_frames().get(ident)
            if fr is None:
                return
            try:
                code = fr.f_code
                key = f"{os.path.basename(code.co_filename)}:{fr.f_lineno} {code.co_name}"
            except Exception:                 # noqa: BLE001
                key = "?"
            _TRACE_SAMPLES[key] = _TRACE_SAMPLES.get(key, 0) + 1
            stop.wait(0.02)

    t = threading.Thread(target=_run, daemon=True, name="trace-sampler")
    t.start()

    def _stop() -> None:
        stop.set()
        top = sorted(_TRACE_SAMPLES.items(), key=lambda kv: -kv[1])[:25]
        total = sum(_TRACE_SAMPLES.values()) or 1
        for k, n in top:
            _blog(f"trace 热点 {n * 20:6d}ms {n * 100 // total:3d}%  {k}")

    return t, _stop


def _free_port() -> int:
    """让系统指派一个空闲端口。★ 别自己猜 8765,猜完那一瞬间可能被别人抢走。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _single_instance() -> bool:
    """抢命名互斥量。已经有活着的实例就返回 False(手放开,让那个继续用)。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, True, MUTEX)
        if not h:
            return True                       # 拿不到互斥量也不能因此开不起来
        if k32.GetLastError() == 183:         # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:                         # noqa: BLE001
        return True


def _say(text: str, title: str = WIN_TITLE) -> None:
    # 量具/命令行里跑的时候不许弹模态框(弹了就卡在等人点确定)
    if os.environ.get("SINGMIC_NO_DIALOG"):
        print(text, file=sys.stderr)
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:                         # noqa: BLE001
        print(text, file=sys.stderr)


WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _webview2_version() -> str:
    """系统里有没有 WebView2 运行时。

    ★ 商店版装到别人的 Windows 10 上时，没有这个运行时只会弹一个白窗口，
      用户看到的现象是「程序坏了」。宁可先查清楚、给下载地址，也不要白屏。
      检查顺序：HKLM(机器级安装) → HKCU(用户级安装) → 磁盘上的 Edge WebView 目录。
      返回 "" 表示没有，返回 "unknown" 表示查不了（非 Windows）—— 查不了就不拦。
    """
    try:
        import winreg
    except Exception:                         # noqa: BLE001
        return "unknown"
    keys = ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"))
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and str(v) != "0.0.0.0":
                    return str(v)
        except OSError:
            continue
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        p = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        try:
            if p.is_dir() and any(p.iterdir()):
                return "present"
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------- 启动自检
# ★ 为什么要这一层：引擎导入 3 秒上下（慢机器更久），uvicorn 起服务再等一两秒。
#   以前这段时间「什么都没发生」—— 用户以为没点开，再点一次，被单实例互斥量挡住，
#   表现出来的就是「双击没反应」。
# ★ 自检只判断「能不能跑」，不做修复；失败就把原话显示出来，不吞、不假装成功。

SPLASH_STEPS = [
    "检查程序文件",
    "准备数据目录",
    "加载音频引擎",
    "启动本地服务",
    "检查音频设备",
]

# ★ 这个页面是**内联**的，不走 HTTP —— 服务还没起来，没有东西可以给它发。
#   进度用 evaluate_js 推进来，所以不需要任何网络请求。
# ★ 样式走「开机动画」:图标悬在中间，下面一条会流动的光带;
#   没有百分比、没有步骤清单。真实进度只体现成光带左侧已点亮的那一段。
# ★ 图标是 packaging/icon.svg 里那只蝰蛇(只取 glyph 组,不要圆角底)。
#   改了 icon.svg 要在这里同步一份 —— 启动页必须内联,加载不了外部文件。
SPLASH_HTML = """<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8">
<style>
  html,body{height:100%;margin:0}
  body{background:radial-gradient(120% 90% at 50% 0%,#12395f 0%,#0a1b2e 58%,#071322 100%);
       color:#e8f0fa;font:14px/1.6 "Microsoft YaHei UI","Segoe UI",sans-serif;
       display:flex;align-items:center;justify-content:center;
       -webkit-user-select:none;user-select:none;overflow:hidden}
  /* ★ 自上而下:蛇 → 名字 → 进度条 → 条下的一行状态字。
     蛇周围不放任何东西;进度条是「开机动画」那种:底下一道光一直在跑,
     左边按真实进度慢慢点亮。 */
  .wrap{text-align:center}
  .mark{width:236px;height:236px;display:block;margin:0 auto;
        filter:drop-shadow(0 14px 34px rgba(24,110,210,.42));
        animation:float 4.2s ease-in-out infinite}
  @keyframes float{0%,100%{transform:translateY(-6px)}50%{transform:translateY(6px)}}
  .title{margin-top:26px;font-size:23px;font-weight:600;letter-spacing:.14em;color:#dceafb}
  .bar{position:relative;height:3px;width:220px;margin:30px auto 0;border-radius:2px;
       background:rgba(120,160,210,.16);overflow:hidden}
  .bar .fill{position:absolute;left:0;top:0;height:100%;width:0;border-radius:2px;
             background:linear-gradient(90deg,#1a6fd4,#5fc8ff);
             box-shadow:0 0 10px rgba(95,200,255,.5);
             transition:width .5s cubic-bezier(.4,0,.2,1)}
  /* 一直跑的那道光:与真实进度无关,开机动画的观感就在它身上 */
  .bar .shine{position:absolute;top:0;left:0;height:100%;width:36%;
              background:linear-gradient(90deg,rgba(170,225,255,0),rgba(170,225,255,.85),rgba(170,225,255,0));
              animation:sweep 1.5s cubic-bezier(.45,.05,.55,.95) infinite}
  @keyframes sweep{0%{transform:translateX(-100%)}100%{transform:translateX(370%)}}
  .sub{margin-top:14px;min-height:18px;font-size:12.5px;color:#7d94af;letter-spacing:.04em}
  .err{display:none;margin:26px auto 0;padding:12px 14px;border-radius:9px;text-align:left;
       background:rgba(255,93,93,.08);border:1px solid rgba(255,93,93,.32);
       color:#ffb3b3;font-size:12px;white-space:pre-wrap;word-break:break-word;
       max-width:560px;max-height:40vh;overflow:auto}
  .err.on{display:block}
  button{display:none;margin-top:18px;padding:7px 22px;border-radius:7px;cursor:pointer;
         border:1px solid #33506e;background:#16283f;color:#c8d8ea;font:inherit;font-size:12.5px}
  button.on{display:inline-block}
</style></head>
<body>
<div class="wrap">
  <svg class="mark" viewBox="8 6 48 52" aria-hidden="true">
    <defs>
      <linearGradient id="sk" x1="22" y1="10" x2="46" y2="52" gradientUnits="userSpaceOnUse">
        <stop stop-color="#c2f0ff"/><stop offset=".38" stop-color="#5cbdf8"/><stop offset="1" stop-color="#1a58a8"/>
      </linearGradient>
      <linearGradient id="bd" x1="26" y1="55" x2="46" y2="24" gradientUnits="userSpaceOnUse">
        <stop stop-color="#1a58a8"/><stop offset=".5" stop-color="#4fb2f6"/><stop offset="1" stop-color="#9fe2ff"/>
      </linearGradient>
    </defs>
    <path d="M32 54c-9.6 0-16.4-6-16.4-13.6C15.6 33 21.4 28 29 28"
          stroke="url(#bd)" stroke-width="6.8" stroke-linecap="round" fill="none"/>
    <path d="M32 54c9.6 0 16.4-6 16.4-13.6 0-5.6-3.6-9.8-9.4-11.6"
          stroke="url(#bd)" stroke-width="6.8" stroke-linecap="round" fill="none"/>
    <path d="M36.6 14.2c2.8 1 4.2 3.4 3.4 6.2-.8 2.8-3.2 4.4-6.4 5.4-3.6-2.4-5.4-4.4-5-7.4.4-2.8 2.8-4.8 8-4.2z"
          fill="url(#sk)"/>
    <ellipse cx="32.4" cy="19" rx="1.2" ry="3.4" fill="#071626"/>
  </svg>
  <div class="title">%TITLE%</div>
  <div class="bar"><i class="fill" id="fill"></i><i class="shine"></i></div>
  <div class="sub" id="sub">正在启动</div>
  <div class="err" id="err"></div>
  <button id="quit" onclick="pywebview.api.quit()">退出</button>
</div>
<script>
const STEPS = __STEPS__;
const fill = document.getElementById('fill'), sub = document.getElementById('sub');
let target = 0, creep = null;

function paint() { fill.style.width = target.toFixed(2) + '%'; }
function say(t) { if (sub.textContent !== t) sub.textContent = t; }

window.SC = {
  step(i, p, state, note) {
    clearInterval(creep); creep = null;
    target = p; paint();
    if (note) say(note);
    else if (state === 'run') say(STEPS[i] + '…');
    else if (state === 'ok' && i < STEPS.length - 1) say('正在启动');
    /* 长步骤拿不到真实进度:按时间慢慢爬,爬到本段的 92% 就停 */
    if (state === 'run') {
      const ceil = ((i + 0.92) / STEPS.length) * 100;
      creep = setInterval(() => {
        if (target >= ceil) return;
        target = Math.min(ceil, target + 0.3); paint();
      }, 200);
    }
  },
  fail(msg) {
    clearInterval(creep);
    const e = document.getElementById('err');
    e.textContent = msg; e.className = 'err on';
    document.getElementById('quit').className = 'on';
    document.querySelector('.bar').style.display = 'none';
    say('启动未完成');
  }
};
paint();
</script>
</body></html>"""


class _BootApi:
    """给自检页用的 JS 接口。失败时窗口要留着让人看清原因，所以必须有个「退出」。"""

    def __init__(self) -> None:
        # ★ 名字必须以 _ 开头:pywebview 生成 JS 桥时会用 get_functions() 把 js_api
        #   上「名字不带下划线」的属性**递归**走一遍(webview/util.py:190-206)。
        #   以前这里叫 self.win,于是它从 Window 一路爬进 WinForms 控件树 ——
        #   boot_stacks.log 里是几百层 get_functions,CPU 打满一个核,GIL 被占住,
        #   启动线程的 import 全被饿着,表现就是「有时 5 秒、有时 40 秒还卡着」。
        self._win = None

    def quit(self) -> None:
        try:
            self._win.destroy()
        except Exception:                     # noqa: BLE001
            os._exit(0)

    def toggle_fs(self) -> None:
        """F11:全屏开关。全屏后标题栏和关闭按钮都没了,不给人留一条退路不行。"""
        try:
            self._win.toggle_fullscreen()
        except Exception:                     # noqa: BLE001
            pass

    def ui_ready(self, ms: int, state: str = "") -> bool:
        """主界面自己报一声：模块脚本跑完了，此时页面已开多久。

        ★ 用来把「启动慢」拆成两半：Python 那边到 load_url 要多久，
          渲染进程把页面跑起来还要多久。浏览器里没有这个接口，静默跳过。
        """
        _blog(f"page ready ({state}), 页面内计时 {ms}ms")
        return True


def _work_area() -> tuple[int, int]:
    """屏幕可用区(扣掉任务栏)。窗口默认开成工作区的 9 成,别顶到屏幕边上。"""
    try:
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        if r.right - r.left > 600 and r.bottom - r.top > 400:
            return r.right - r.left, r.bottom - r.top
    except Exception:                         # noqa: BLE001
        pass
    return 1600, 1000


def main() -> int:
    # 冻结后 VST 宿主子进程就是「同一个 exe 带这个开关」—— 必须比单实例判断更早
    if "--vst-worker" in sys.argv:
        import studio_vst_worker
        return studio_vst_worker.main()

    if not _single_instance():
        _say(f"{WIN_TITLE} 已经在运行了。\n\n(同一个工程被两个窗口同时改会打架,所以只开一个。)")
        return 0

    # ★ 先确认界面能显示，再起服务和窗口。缺运行时时白屏是最难排查的一种「版本问题」。
    if _webview2_version() == "":
        _say("缺少 Microsoft Edge WebView2 运行时，界面无法显示。\n\n"
             f"请安装后重新打开{WIN_TITLE}：\n" + WEBVIEW2_URL +
             "\n\nMicrosoft Edge WebView2 Runtime is required to display the interface.")
        return 1

    port = _free_port()
    # ★ 必须在 import server 之前设:server.py 是**导入时**就把 PORT 定下来的
    os.environ["SINGMIC_PORT"] = str(port)
    os.environ.setdefault("SINGMIC_NO_ADB", "1")     # 壳里不折腾 USB 隧道

    # ★ 数据目录:安装版装在 Program Files,那里**不可写**。打包装好的 exe 必须把
    #   录音/工程/导出/缓存放到 %LOCALAPPDATA%\SingMic\data;开发时跑 desktop.py
    #   (没 frozen)仍用程序目录,跟以前一模一样。
    if getattr(sys, "frozen", False) and not os.environ.get("SINGMIC_DATA"):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        data = Path(local) / APP_NAME / "data"
        try:
            data.mkdir(parents=True, exist_ok=True)
            os.environ["SINGMIC_DATA"] = str(data)
        except OSError:
            pass                                       # 建不出来就退回程序目录,至少能跑

    # ★ 清空上一轮日志。必须放在 `SINGMIC_DATA` 定下来**之后** ——
    #   它决定 boot.log 落在哪:装好的 exe 用的是数据目录,开发时是程序目录。
    #   上一版这段在前面,打包版每次都清了个没在用的文件,数据目录里越积越长,
    #   排查时得先分辨哪几行是这一次的。
    try:
        _lp = _boot_log_path()
        _lp.parent.mkdir(parents=True, exist_ok=True)
        _lp.write_text("", encoding="utf-8")
    except Exception:                         # noqa: BLE001
        pass

    # ★ 排查用:开了 TRACE 就每 10 秒把每个线程的 Python 栈落一份(boot_stacks.log)。
    #   放在这里而不是模块顶部 —— 上面那段跑完,`SINGMIC_DATA` 才是最终值。
    #   卡住时 CPU 是 0,日志只会说「在 import」,只有栈能指出到底停在哪一行。
    if os.environ.get("SINGMIC_BOOT_TRACE"):
        try:
            import faulthandler
            _stk = open(_boot_log_path().with_name("boot_stacks.log"),
                        "a", buffering=1, encoding="utf-8")
            _stk.write(f"\n===== {time.strftime('%H:%M:%S')} 启动 =====\n")
            faulthandler.enable(file=_stk, all_threads=True)
            faulthandler.dump_traceback_later(10, repeat=True, file=_stk)
        except Exception:                     # noqa: BLE001
            pass

    try:
        import webview
    except Exception as e:                    # noqa: BLE001
        _say(f"启动失败：{e}")
        return 1

    # ★ WebView2 的缓存目录必须是我们自己的、固定的一个。
    #   用默认值会跟别的 WebView2 应用抢同一个目录,报 0x800700AA(资源在使用中),
    #   表现为「窗口根本不出来」而且不告诉你为什么。
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    wv_data = Path(local) / APP_NAME / "WebView2"
    try:
        wv_data.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    ww, wh = _work_area()

    # ★ 窗口先出来，再做重活。create_window 本身很快，重的都在 boot() 里。
    # ★ 启动页就是**全屏**的:与主界面同一个窗口,从一开始就铺满整块屏幕,
    #   中间不再「小窗 → 拉大 → 最大化 → 全屏」这一连串跳变。
    #   frameless 是兜底:万一这台机器上 fullscreen 没生效,至少还是个无边界的整屏窗口。
    api = _BootApi()
    win = webview.create_window(
        WIN_TITLE,
        html=(SPLASH_HTML
              .replace("__STEPS__", json.dumps(SPLASH_STEPS, ensure_ascii=False))
              .replace("%TITLE%", WIN_TITLE)),
        js_api=api,
        width=ww, height=wh,
        fullscreen=True,
        frameless=True,
        easy_drag=False,                      # 无边框时别把整块界面拖走
        background_color="#071322",
        text_select=False,
    )
    api._win = win

    # ★ 启动自检的进度只作用于界面。evaluate_js 会**等** WebView2 渲染进程应答,
    #   引擎冷启动那几秒里它会一直阻塞 —— 上一版就是这样:每推一次进度卡 20 秒,
    #   真正的活儿（导入引擎）反而排在这 20 秒后面。推界面丢到一个线程里做,
    #   启动流程自己一步不停。
    # ★ 队列只留最新一帧。上一版是「来一条排一条」:启动那几十秒里渲染进程一次应答要好几秒,
    #   于是积压了几十条已经过期的进度更新;页面切到主界面之后它们还在一条条执行,
    #   把「切过去」硬生生拖后几十秒。进度条只需要看到当前值,不需要补播历史。
    js_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    # ★ 开始往主界面切的那一刻把闸门关上:evaluate_js 是**同步**打在 WebView2 的 UI 线程上的,
    #   页面正在导航时来一发,会把导航一起挡住 —— 实测「资源全下完了,load 却晚二十多秒」。
    js_stop = threading.Event()

    def _push_js(code: str) -> None:
        if js_stop.is_set():
            return
        try:
            js_q.put_nowait(code)
            return
        except queue.Full:
            pass
        try:
            js_q.get_nowait()                 # 顶掉那条过期的
        except queue.Empty:
            pass
        try:
            js_q.put_nowait(code)
        except queue.Full:
            pass

    def _js_worker() -> None:
        while True:
            code = js_q.get()
            if js_stop.is_set():              # 已经在切页面了,这条作废
                continue
            try:
                win.evaluate_js(code)
            except Exception:                 # noqa: BLE001
                pass                          # 窗口没了就别再吵

    threading.Thread(target=_js_worker, name="splash-js", daemon=True).start()

    srv_error: list[BaseException] = []

    def step(i: int, frac: float, state: str = "run", note: str = "") -> None:
        n = len(SPLASH_STEPS)
        pct = int((i + max(0.0, min(1.0, frac))) / n * 100)
        js = "SC.step(%d,%d,%s,%s)" % (
            i, pct, json.dumps(state), json.dumps(note, ensure_ascii=False))
        _blog(f"step {i} {SPLASH_STEPS[i]} {state} {pct}% {note}".strip())
        _push_js(js)                          # 只管排队,不等渲染进程

    def _boot() -> str:
        # ---- 0 程序文件 -------------------------------------------------
        step(0, 0.2)
        # ★ 路径以 server.py 实际读的为准:界面首页在 web/studio.html,
        #   不是 web/studio/studio.html —— 写错就是每次启动都误报「文件不完整」。
        # ★ 打包后 server.py 已经编进 exe,_internal 里没有这个文件;只在源码运行时查它。
        need = ["web/studio.html", "web/studio/ui.js", "web/studio/i18n.js"]
        if not getattr(sys, "frozen", False):
            need.append("server.py")
        miss = [p for p in need if not (Path(BASE) / p).exists()]
        if miss:
            raise RuntimeError("程序文件不完整，缺少：\n" + "\n".join(miss)
                               + f"\n\n请重新安装{WIN_TITLE}。")
        step(0, 1.0, "ok")

        # ---- 1 数据目录 -------------------------------------------------
        step(1, 0.25)
        root = Path(os.environ.get("SINGMIC_DATA") or BASE)
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            raise RuntimeError(f"数据目录不可写：{root}\n{e}\n\n"
                               "请检查磁盘空间或文件夹权限。") from e
        try:
            free_gb = shutil.disk_usage(root).free / (1024 ** 3)
            note = f"剩余 {free_gb:.0f} GB"
        except OSError:
            note = ""
        step(1, 1.0, "ok", note)

        # ---- 2 音频引擎（最慢的一步）------------------------------------
        step(2, 0.05)
        _stop_sample = None
        if os.environ.get("SINGMIC_BOOT_TRACE"):
            try:
                _stop_sample = _trace_sample_start()[1]
            except Exception:                 # noqa: BLE001
                _stop_sample = None
        _blog("import uvicorn ...")
        import uvicorn
        # ★ 排查用:SINGMIC_BOOT_TRACE=1 时逐个模块记耗时,定位「导入引擎」为什么慢。
        #   必须在 import server **之前**跑 —— 之后所有模块都已缓存,量到的全是 0。
        if os.environ.get("SINGMIC_BOOT_TRACE"):
            # ★ 别把 scipy 列进来:它本该在后台热(见 _warm_optional),列进来这一发就是 27 秒,
            #   量到的会是一个「我们刻意不走的路径」。
            for _m in ("numpy", "soundfile", "sounddevice", "fastapi",
                       "tuning", "audio_engine", "studio_fx", "studio_vst", "studio_mix",
                       "studio_asr", "studio_syllable", "studio_api"):
                _blog(f"trace import {_m} ...")
                try:
                    __import__(_m)
                except Exception as e:        # noqa: BLE001
                    _blog(f"trace import {_m} failed: {e!r}")
                _blog(f"trace import {_m} done")
        _blog("import server ...")
        from server import app, HOST
        _blog(f"imported, HOST={HOST} port={port}")
        step(2, 1.0, "ok")

        # ---- 3 本地服务 -------------------------------------------------
        step(3, 0.05)

        def _run() -> None:
            _blog("server thread start")
            try:
                uvicorn.run(app, host=HOST, port=port, log_level="warning")
                _blog("server thread returned")
            except BaseException as e:        # noqa: BLE001
                _blog(f"server thread error: {e!r}")
                srv_error.append(e)

        threading.Thread(target=_run, name="singmic-server", daemon=True).start()

        url = f"http://{HOST}:{port}"
        t0 = time.time()
        ready = False
        last_err = ""
        while time.time() - t0 < 60:
            try:
                with urllib.request.urlopen(url + "/api/studio/about", timeout=1) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception as e:            # noqa: BLE001
                # 只在错误内容变化时记一行,别把日志刷成一片
                if repr(e) != last_err:
                    last_err = repr(e)
                    _blog(f"poll {url} -> {last_err}")
            step(3, min(0.92, 0.05 + (time.time() - t0) / 25.0))
            time.sleep(0.15)
        if not ready:
            why = f"\n\n{srv_error[0]}" if srv_error else ""
            raise RuntimeError("本地服务在 60 秒内没有应答。" + why)
        step(3, 1.0, "ok")

        # ---- 4 音频设备（缺麦克风不拦，只提示）--------------------------
        # ★ 这一步不再碰 PortAudio:这台机器的虚拟声卡会让设备枚举卡 0.9～20 秒
        #   (实测最慢一次 20.7 秒),而它原本只是给启动条写一行字 —— 界面被一行字
        #   拖住不值得。设备枚举挪到界面出来以后在后台做(见 _warm_optional)。
        step(4, 1.0, "ok")
        if _stop_sample:
            try:
                _stop_sample()
            except Exception:                 # noqa: BLE001
                pass
        return url

    def boot() -> None:
        _blog(f"boot start frozen={getattr(sys, 'frozen', False)} BASE={BASE} data={os.environ.get('SINGMIC_DATA')}")
        try:
            url = _boot()
        except Exception as e:                # noqa: BLE001
            import traceback
            _blog("BOOT FAILED\n" + traceback.format_exc())
            _push_js("SC.fail(%s)" % json.dumps(str(e), ensure_ascii=False))
            return
        # ★ 窗口从 create_window 那一刻就是全屏的,主界面沿用它,不再「先小后放大」。
        #   F11 是全屏的唯一退路(全屏后没有标题栏和关闭按钮)。
        def _on_loaded(*_a) -> None:
            try:
                if "/studio" not in (win.get_current_url() or ""):
                    return
            except Exception:                 # noqa: BLE001
                return
            _blog("studio loaded")
            try:
                win.evaluate_js(
                    "if(!window.__smFs){window.__smFs=1;"
                    "document.addEventListener('keydown',function(e){"
                    "if(e.key==='F11'){e.preventDefault();pywebview.api.toggle_fs();}});}")
            except Exception:                 # noqa: BLE001
                pass
            _warm_optional()

        # ★ 两件「不急但要花几秒」的事,都放到界面出来之后在后台做:
        #   ① PortAudio 设备枚举:0.9～20 秒,状态只决定一行提示,不值得挡界面;
        #   ② scipy:冻结包里第一次导入实测 26.9 秒,只有切分/导出才用到。
        #   ★ 不再等 pywebview 的 loaded 事件 —— 那个事件实测会晚到二十多秒、偶尔干脆不来
        #   (页面自己在 5 秒时就已经能用了),所以 load_url 之后立刻起线程。
        _warm_started = []

        def _warm_optional() -> None:
            if _warm_started:
                return
            _warm_started.append(1)

            def _run() -> None:
                t0 = time.time()
                try:
                    import sounddevice as sd
                    devs = sd.query_devices()
                    ins = sum(1 for d in devs if d.get("max_input_channels", 0) > 0)
                    outs = sum(1 for d in devs if d.get("max_output_channels", 0) > 0)
                    _blog(f"warm audio ok in {time.time() - t0:.1f}s, {ins} 个输入 / {outs} 个输出")
                except Exception as e:         # noqa: BLE001
                    _blog(f"warm audio failed: {e!r}")
                time.sleep(3.0)               # 先让首屏画完、别跟它抢
                t0 = time.time()
                try:
                    import scipy.signal        # noqa: F401
                    _blog(f"warm scipy ok in {time.time() - t0:.1f}s")
                except Exception as e:         # noqa: BLE001
                    _blog(f"warm scipy failed: {e!r}")

            threading.Thread(target=_run, name="warm-optional", daemon=True).start()

        try:
            win.events.loaded += _on_loaded
        except Exception:                     # noqa: BLE001
            pass

        js_stop.set()                         # ★ 切页面之前先闭嘴:见 js_stop 上面那段
        _blog("splash js stopped")
        try:
            win.load_url(url + "/studio")
            _blog("load_url /studio called")
        except Exception as e:                # noqa: BLE001
            js_stop.clear()
            _push_js("SC.fail(%s)" % json.dumps(f"界面加载失败：{e}", ensure_ascii=False))
        else:
            _warm_optional()

    # 窗口多开一条路:关窗口这件事 WebView2 会自己回收,但音频线程是 daemon,
    # 所以 start() 返回之后直接硬退,别让解释器在退出阶段卡住。
    try:
        webview.start(boot, gui="edgechromium", private_mode=False,
                      storage_path=str(wv_data))
    finally:
        del win
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
