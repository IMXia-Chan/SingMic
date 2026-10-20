"""实时音频引擎 —— 手机麦克风进来,混响/伴奏出去,同时录下来。

★★ 一条铁律:音频回调里**不许等、不许分配大块内存、不许打印**。
   回调是实时线程,它一卡,耳机里就是一声爆音。所有慢活(读文件、写盘、
   算混响的脉冲响应)都在别的线程里做完,回调只做「取一块、算一下、塞出去」。

数据流:

    手机 --WS(int16)--> 抖动缓冲 --+--> 混响 --> 混音 --> 输出设备(耳返)
                                  |              |
                                  |              +--> 录音线程(mix.wav)
                                  +--> 录音线程(voice.wav, 干声)

★ 抖动缓冲是必须的:手机发过来的包不可能每 20ms 准点到。
  直接拿来就用的话,晚到一包 = 一个爆音。
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

try:                                  # ★ 装不上也不能让整个程序起不来
    import soundcard as sc             # 抓「电脑正在放的声音」靠它(WASAPI 环回)
    _HAVE_SC = True
except Exception:                      # noqa: BLE001
    sc = None                          # type: ignore[assignment]
    _HAVE_SC = False

from dsp import Gate, VoiceChain

SR = 48000          # 内部一律 48k
BLOCK = 480         # 默认块长(10ms),真正开流时按 BLOCK_MS 算

# ★★ 处理块长 **10ms**(默认)。
#   2026-10-18 试过无条件改成 5ms,**蓝牙上直接爆**:蓝牙走 WASAPI 共享,
#   设备周期就是 10ms,回调改成 5ms 喂一次,喂不上就是一个个静音洞 ——
#   实测听到的就是「断断续续、咔咔响」。
#   ⇒ 所以块长**跟着设备走**,见 `pick_block_ms()`。蓝牙拿 10ms,有线拿 5ms。
BLOCK_MS = 10.0
BLOCK_MS_FAST = 5.0  # 设备周期撑得住时用这个
# ★ 判据是**设备自己报的最小输出延迟**(WASAPI 的 default_low_output_latency)。
#   实测:有线耳机 3.0ms、板载声卡 3.0ms、虚拟环回 3.0ms;
#   蓝牙 A2DP 那档报的是几十毫秒。≤ 6ms 才敢给 5ms 块。
FAST_PERIOD_MS = 6.0
# ★ 抖动缓冲用**毫秒**定上下限,不再用「几块」。块长一改,「3 块」就从 30ms
#   变成 15ms —— 那种跟着块长跑的常数,改块长的时候最容易悄悄改坏。
BUF_LO_MS = 15.0     # 下限(实际还会跟 max(2 块) 取大 —— 缓冲装不下两块必断)
BUF_HI_MS = 80.0     # 普通模式上限
BUF_HI_LL_MS = 25.0  # 低延迟模式上限(实测包间隔 p95 8.2ms / max 18.9ms)


def _pct(vals, q: float) -> float:
    """分位数(毫秒)。空集合回 0 —— 别让状态接口因为一个空 deque 炸掉。"""
    if not vals:
        return 0.0
    a = sorted(vals)
    i = min(len(a) - 1, max(0, int(round(q * (len(a) - 1)))))
    return round(float(a[i]), 1)


def pick_block_ms(info: dict) -> float:
    """★★ **永远是 10ms。5ms 这条路 2026-10-18 试过,又栽了一次。**

    当时以为「设备报 3ms 周期就一定喂得上」—— 拿有线耳机实测:
    裸流确实能跑到 **201 回调/秒**,于是放心改成 5ms 块。真跑起来:
    `out_sps` 在 31k~50k 之间抖、`overruns` 一路涨到 96、
    抖动缓冲冲到 **480ms 上限**。

    原因不是设备喂不上,是**我们自己的处理链吃不下**:
    块长减半 = 每秒回调翻倍 = 谱减的 FFT 和混响的分块卷积全部翻倍,
    实时线程做不完,输出就变慢,手机那边还在按原速灌 → 一路堆到顶。

    ⇒ **块长不是独立的旋钮,它绑定整条链的算力预算。想再小,先得把
      混响/谱减挪出实时线程。在那之前这一条别动。**
    """
    return BLOCK_MS


def _open_out(dev, rate: int, block: int, callback, extra):
    """开一个输出流并起它。回 (stream|None, latency_秒, 错误串)。

    ★ 失败时自己收尸:半开的流不关掉,下一次开同一个设备会报
      「设备被占用」—— 而报错里不会提是我们自己没关。
    """
    s = None
    try:
        kw = dict(device=dev, samplerate=rate, channels=2, dtype="float32",
                  blocksize=block, latency="low", callback=callback)
        if extra is not None:
            kw["extra_settings"] = extra
        s = sd.OutputStream(**kw)
        s.start()
        return s, float(s.latency), ""
    except Exception as e:                 # noqa: BLE001
        if s is not None:
            try:
                s.close()
            except Exception:              # noqa: BLE001
                pass
        return None, 0.0, str(e)[:200]


def _ai_latency_ms() -> float:
    """AI 那一级自己说的延时(实测的,不是封装文档上的)。取不到就报 0。"""
    try:
        import ai_denoise
        return ai_denoise.latency_ms()
    except Exception:                      # noqa: BLE001
        return 0.0

# ---- 漂移校正的三个可调量(tools/selftest_drift.py 会扫它们) ----
FILL_DEAD = 0.004   # 死区:水位离目标不到 4ms 就完全不修
FILL_KP = 0.50      # 比例:水位偏多少,当场修多少
FILL_KI = 0.0015    # 积分:极慢,专门吃掉比例项天生留下的稳态偏移
DRIFT_MAX = 0.015   # 修正量硬上限 ±1.5%(实测两块晶振差约 0.8~1%)


# ---------------------------------------------------------------------------
# 分块卷积(混响) —— 纯 numpy,不依赖 scipy
# ---------------------------------------------------------------------------
class PartitionedConvolver:
    """均匀分块卷积。

    ★ 为什么要分块:混响的脉冲响应有好几千个采样点,直接 np.convolve 是
      O(块长 × IR 长),每个回调要算几千万次,实时线程直接嗝屁。
      分块 + FFT 之后每个回调只做几十次复数乘法,轻松跑住。
    """

    def __init__(self, ir: np.ndarray, block: int = BLOCK) -> None:
        self.block = block
        ir = np.asarray(ir, dtype=np.float32)
        n_parts = max(1, (len(ir) + block - 1) // block)
        padded = np.zeros(n_parts * block, dtype=np.float32)
        padded[:len(ir)] = ir
        self.n_parts = n_parts
        self.fft_n = 2 * block
        # 每个分区预先算好频域,回调里不再做 FFT
        parts = padded.reshape(n_parts, block)
        self.H = np.fft.rfft(parts, n=self.fft_n, axis=1).astype(np.complex64)
        self.hist = np.zeros((n_parts, self.fft_n // 2 + 1), dtype=np.complex64)
        self.prev = np.zeros(block, dtype=np.float32)
        self.pos = 0

    def reset(self) -> None:
        self.hist[:] = 0
        self.prev[:] = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        b = self.block
        frame = np.concatenate((self.prev, x))
        self.prev = x
        X = np.fft.rfft(frame, n=self.fft_n).astype(np.complex64)

        self.hist[self.pos] = X
        # 分区 p 用的输入是「p 块之前」的那一块
        idx = (self.pos - np.arange(self.n_parts)) % self.n_parts
        Y = np.sum(self.H * self.hist[idx], axis=0)
        self.pos = (self.pos + 1) % self.n_parts

        y = np.fft.irfft(Y, n=self.fft_n)[-b:]
        return y.astype(np.float32)


def make_reverb_ir(seconds: float = 0.55, decay: float = 3.2,
                   sr: int = SR, seed: int = 7) -> np.ndarray:
    """造一条「房间」的脉冲响应。

    ★ 这是自己造的,不是录音采样 —— 所以它没有任何版权问题,也足够像房间。
      前 8ms 保留一点直声,后面是越来越稀的尾巴。
    """
    rs = np.random.RandomState(seed)          # ★ 固定种子:每次启动的混响一模一样
    n = int(seconds * sr)
    t = np.arange(n, dtype=np.float32) / sr
    env = np.exp(-decay * t)
    noise = rs.randn(n).astype(np.float32)
    # 一点高频衰减:过一道一阶低通,听起来不像白噪声尾巴
    noise = np.concatenate(([noise[0]], noise[1:] * 0.6 + noise[:-1] * 0.4))
    ir = noise * env
    ir[: int(0.004 * sr)] *= 0.3              # 直声留一点,不要糊
    ir /= max(1e-6, np.sqrt(np.sum(ir ** 2)))
    return ir


# ---------------------------------------------------------------------------
# 抖动缓冲
# ---------------------------------------------------------------------------
class VoiceBuffer:
    """手机进来的声音放这儿,输出回调从这儿取。

    ★ 目标水位 = 4 块(40ms)。低于它就补静音,高于上限就丢最老的 ——
      (选 40 不是 30:模拟实测 30ms 时「网络抖动 60ms」那档会欠载 45 次,
       40ms 只丢 4 次 —— 多 10ms 延迟换掉一整片爆音,值。)
      宁可偶尔断一下,也不许让延迟越积越大(latency 会一路涨到几秒)。
    """

    def __init__(self, target: int = 4 * BLOCK, limit: int = 48 * BLOCK,
                 channels: int = 1) -> None:
        self.lock = threading.Lock()
        self.channels = channels
        self.buf = np.zeros((0, channels) if channels > 1 else 0, dtype=np.float32)
        self.target = target
        self.limit = limit
        self.primed = False
        self.underruns = 0
        self.overruns = 0
        self.last_rx = 0.0

    def push(self, x: np.ndarray) -> None:
        with self.lock:
            self.buf = np.concatenate((self.buf, x)) if len(self.buf) else x.copy()
            if len(self.buf) > self.limit:
                dropped = len(self.buf) - self.limit
                self.buf = self.buf[dropped:]
                self.overruns += 1
            if not self.primed and len(self.buf) >= self.target:
                self.primed = True
            self.last_rx = time.time()

    def pull(self, n: int) -> np.ndarray:
        with self.lock:
            if not self.primed or len(self.buf) < n:
                # ★ 只有「手机刚刚还在发」才算欠载。手机压根没连的时候
                #   每个空块都记一次,几秒钟就攒出两万多,把真正的欠载淹了。
                if self.primed and (time.time() - self.last_rx) < 0.5:
                    self.underruns += 1
                return np.zeros((n, self.channels) if self.channels > 1 else n,
                                dtype=np.float32)
            out = self.buf[:n].copy()
            self.buf = self.buf[n:]
            return out

    def reset(self) -> None:
        with self.lock:
            self.buf = np.zeros((0, self.channels) if self.channels > 1 else 0,
                                dtype=np.float32)
            self.primed = False
            self.underruns = 0
            self.overruns = 0

    def fill_ratio(self) -> float:
        with self.lock:
            return len(self.buf) / max(1, self.limit)

    def fill_samples(self) -> int:
        with self.lock:
            return len(self.buf)


class Resampler:
    """重采样 —— 既换采样率,也吸收两台设备的时钟差。

    ★★ 为什么非有它不可(2026-10-18 真机实测):
       手机说 48k,电脑也说 48k,但两块晶振不一样,实测差约 0.8%。
       没有校正的话,抖动缓冲会一路涨到上限(480ms),耳返延迟越来越大,
       然后每隔一会儿丢一大块 —— 听起来就是「越来越拖,然后咔一下」。
       ★ 正确做法不是「攒满了丢一块」,而是**微调重采样比例**,让它悄悄多消耗 0.8%,
         缓冲自然稳在目标水位,一个采样点都不用丢。

    d > 0 = 多消耗(缓冲偏高时用);d < 0 = 少消耗(缓冲见底时用)。
    """

    def __init__(self, src_rate: int, dst_rate: int = SR) -> None:
        self.src = src_rate
        self.dst = dst_rate
        self.ratio = src_rate / dst_rate
        self.d = 0.0
        self.pos = 0.0          # 读指针(浮点)在 data 里的位置
        self.history = np.zeros(0, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        """★ 状态必须这样维护,不然会静默地把声音复制一遍:
        每次调用后,只把「最后一个整数位置之后」的那一小截留作历史,
        读指针的小数部分存在 pos 里。之前那版用 int(负数) 截断,
        历史变成了整块 960 个样本 —— 每来一块就把上一块又吐一遍,
        输出的样本数是输入的两倍,抖动缓冲几秒钟就顶满。
        """
        step = self.ratio * (1.0 + self.d)
        data = np.concatenate((self.history, x)) if len(self.history) else x
        pos = self.pos

        n_out = 0
        if len(data) - pos >= 1.0:
            n_out = int(np.floor((len(data) - pos - 1.0) / step)) + 1
        if n_out <= 0:
            # 攒着,等下一块再说
            self.history = data
            self.pos = pos
            return np.zeros(0, dtype=np.float32)

        idx = pos + np.arange(n_out) * step
        out = np.interp(idx, np.arange(len(data)), data).astype(np.float32)

        end = pos + n_out * step          # 下一块要从这儿接着读
        keep = int(np.floor(end))
        if keep < len(data):
            self.history = data[keep:]
            self.pos = end - keep
        else:
            self.history = np.zeros(0, dtype=np.float32)
            self.pos = end - len(data)
        return out


class DriftController:
    """盯着抖动缓冲的水位,吐出应该用多大的重采样修正。

    ★ 手机那路和「环回伴奏」那路各用一个 —— 两条晶振差是各自独立的。
    """

    def __init__(self, sr: int, target: int) -> None:
        self.sr = sr
        self.target = target
        self.fill_avg = float(target)
        self.integ = 0.0
        self.d = 0.0

    def reset(self, target: int | None = None) -> None:
        if target is not None:
            self.target = target
        self.fill_avg = float(self.target)
        self.integ = 0.0
        self.d = 0.0

    def update(self, fill: int) -> float:
        # ★ 必须用「慢速平均 + 死区」,不能拿瞬时水位去调。
        #   WebSocket 本来就是一阵一阵到的,瞬时水位能差几十毫秒;
        #   拿它做校正会让修正量在 ±1.2% 之间来回撞 —— 那就是**颤音**
        #   (1% 的频率摆动 ≈ 17 音分,唱的时候听得出来)。
        # ★★ 但只有比例项不行:比例控制必须「留一个误差」才有输出。
        #   实测手机快 1% 时缓冲就稳在了 87ms —— 白多 47ms 延迟。
        #   所以再加一个极慢的积分项,把那个稳态偏移吃掉。
        self.fill_avg = 0.98 * self.fill_avg + 0.02 * fill
        err = (self.fill_avg - self.target) / float(self.sr)
        if err > FILL_DEAD:
            eff = err - FILL_DEAD
        elif err < -FILL_DEAD:
            eff = err + FILL_DEAD
        else:
            eff = 0.0                       # 死区里两个项都不动
        prop = max(-DRIFT_MAX, min(DRIFT_MAX, eff * FILL_KP))
        self.integ = max(-DRIFT_MAX, min(DRIFT_MAX, self.integ + eff * FILL_KI))
        self.d = max(-1.5 * DRIFT_MAX, min(1.5 * DRIFT_MAX, prop + self.integ))
        return self.d


# ---------------------------------------------------------------------------
# 电脑正在放的声音 → 伴奏(WASAPI 环回)
# ---------------------------------------------------------------------------
class LoopSource:
    """把「电脑上正在放的音乐」抓回来,当伴奏混进耳返和录音。

    ★★ 铁律:**环回来源绝不能是耳返设备本身。**
      环回抓的是「这台设备最终放出去的声音」,里面已经含了我们自己的耳返。
      同一台设备既当耳返又当来源 = 麦克风 → 耳返 → 又被抓回来 → 再放大 → 啸叫。
      所以打开时先比对设备名,一样就直接拒绝。

    ★ 抓到的声音**不做任何加工**(不降噪、不过 EQ、不加混响)——
      设计上就定死:伴奏不需要加工,只要一个音量。
    """

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        # ★ 伴奏**保持立体声**:音乐播放器放的是立体声,压成单声道再复制到两边,
        #   声场就没了 —— 要的是「伴奏不加工」,压成单声道就是加工。
        self.buf = VoiceBuffer(channels=2)
        self.resamplers: list[Resampler] = []
        self.dc: DriftController | None = None
        self.rate = SR
        self.device = ""
        self.error = ""
        self.rms = 0.0
        self.running = False

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, device_name: str, out_rate: int) -> str:
        self.stop()
        if not _HAVE_SC:
            raise RuntimeError("没装 soundcard 库,抓不了电脑声音(pip install soundcard)")
        self.rate = SR
        # 左右各一个重采样器(历史各自独立),修正量 d 一起设
        self.resamplers = [Resampler(self.rate, out_rate), Resampler(self.rate, out_rate)]
        self.buf = VoiceBuffer(channels=2)     # target 按 48k 的 4 块算
        self.dc = DriftController(self.rate, self.buf.target)
        self.stop_flag.clear()
        self.error = ""
        self.device = device_name
        self.thread = threading.Thread(target=self._run, name="loopback", daemon=True)
        self.thread.start()
        return device_name

    def stop(self) -> None:
        self.stop_flag.set()
        t = self.thread
        self.thread = None
        if t is not None:
            t.join(timeout=1.0)
        self.buf.reset()
        self.rms = 0.0
        self.device = ""

    def _run(self) -> None:
        # ★★ 必须在**这个线程里**初始化 COM,而且设备也要在这里打开。
        #   在别的线程里 get_microphone、在这里 record —— 实测直接
        #   `RuntimeError: Error 0x800401f0`(CO_E_NOTINITIALIZED),一句话都不提 COM。
        hr = 1
        try:
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_MULTITHREADED
        except Exception:                                      # noqa: BLE001
            pass
        try:
            mic = sc.get_microphone(self.device, include_loopback=True)
            self.running = True
            with mic.recorder(samplerate=self.rate, channels=2,
                              blocksize=BLOCK) as rec:
                while not self.stop_flag.is_set():
                    data = rec.record(BLOCK)
                    if data is None or len(data) == 0:
                        continue
                    st = data.astype(np.float32)
                    if st.ndim == 1:
                        st = np.stack([st, st], axis=1)
                    self.rms = float(np.sqrt(np.mean(st * st)) + 1e-12)
                    if self.resamplers and self.dc is not None:
                        with self.buf.lock:
                            fill = len(self.buf.buf)
                        d = self.dc.update(fill)
                        chans = []
                        for ch, rs in enumerate(self.resamplers):
                            rs.d = d
                            chans.append(rs.process(st[:, ch]))
                        if chans and len(chans[0]):
                            n = min(len(c) for c in chans)
                            st = np.stack([c[:n] for c in chans], axis=1)
                        else:
                            st = None
                    if st is not None and len(st):
                        self.buf.push(st)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False
            if hr in (0, 1):          # S_OK / S_FALSE = 这一次确实是我们初始化的
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def pull(self, n: int) -> np.ndarray:
        # ★ 伴奏缺口就补静音 —— 绝不在这里等(这是实时线程)
        return self.buf.pull(n)


# ---------------------------------------------------------------------------
# 录音
# ---------------------------------------------------------------------------
class Recorder:
    """写盘在单独的线程 —— 回调里写文件 = 必定爆音。"""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.paths: list[str] = []
        self.written = 0
        # ★ 开头那截「还没收到手机数据」的真空要不要写:不写。
        #   实测点录音时手机偶尔还没开始送音,开头会多出 1.5~5.2 秒的**精确零**
        #   (不是安静,是缓冲区空着)。人声和混音一起按同一位移裁,不许各裁各的。
        self._started = False

    def start(self, out_dir: Path, sr: int = SR) -> tuple[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        mix_p = out_dir / f"{stamp}_mix.wav"
        voice_p = out_dir / f"{stamp}_voice.wav"
        self.sr = sr
        self.paths = [str(mix_p), str(voice_p)]
        self.written = 0
        self._started = False
        self.stop_flag.clear()
        self.q = queue.Queue(maxsize=200)
        self.thread = threading.Thread(
            target=self._run, args=(mix_p, voice_p, sr), name="recorder", daemon=True)
        self.thread.start()
        return self.paths[0], self.paths[1]

    def _run(self, mix_p: Path, voice_p: Path, sr: int) -> None:
        # ★ 32bit float WAV:整条链本来就是 float32,落盘不再回炉。
        #   float32 的尾巴是硬件底噪,不是量化噪声 —— 这就是「无损输出」。
        with sf.SoundFile(str(mix_p), "w", samplerate=sr, channels=2,
                          subtype="FLOAT") as fm, \
             sf.SoundFile(str(voice_p), "w", samplerate=sr, channels=1,
                          subtype="FLOAT") as fv:
            while True:
                try:
                    item = self.q.get(timeout=0.2)
                except queue.Empty:
                    if self.stop_flag.is_set():
                        break
                    continue
                if item is None:
                    break
                self._write(fm, fv, *item)
            # 把队列里剩下的写完再收工
            while True:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                self._write(fm, fv, *item)

    def _write(self, fm, fv, voice: np.ndarray, mix: np.ndarray) -> None:
        if not self._started:
            # ★ 精确零 = 缓冲区空(没收到手机数据),不是安静。
            #   真实的安静房间里也有底噪,永远不会恰好等于 0.0。
            # ★★ 但「只录电脑放的声音(不接手机)」时 voice 永远是精确零 ——
            #   以前只认 voice,那种录法每个块都被当成真空丢掉,录出来是 0 帧的空文件。
            #   所以还要看 mix(它含伴奏):两块都全零才算真空。
            nz = np.flatnonzero(voice != 0)
            if nz.size == 0:
                nz = np.flatnonzero(np.any(mix != 0, axis=-1))
                if nz.size == 0:
                    return                  # 整块都是真空,这一块先不写
            k = int(nz[0])
            voice = voice[k:]
            mix = mix[k:]
            self._started = True
        # ★ mix 本身已经是立体声(人声复制到两边 + 伴奏原样)
        fm.write(mix if mix.ndim == 2 else np.stack([mix, mix], axis=1))
        fv.write(voice)
        self.written += len(mix)

    def push(self, voice: np.ndarray, mix: np.ndarray) -> None:
        try:
            self.q.put_nowait((voice, mix))
        except queue.Full:
            # ★ 写盘跟不上就丢,绝不让回调等 —— 宁可录音缺一小段,不要让耳返爆音
            pass

    def stop(self) -> None:
        self.stop_flag.set()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def _com_init() -> None:
    """★ 在**当前线程**初始化 COM —— PortAudio 的 WASAPI 后端离不开它。

    COM 是 per-thread 的:主线程在 `import sounddevice` 那一步(Pa_Initialize)
    就已经初始化过了,所以**在主线程里开流一切正常**;
    换到别的线程(比如 asyncio 的 `run_in_executor` 线程池)去开,
    `stream.start()` 会抛 `PaErrorCode -9999` + `WdmSyncIoctl ...
    GLE = 0x00000492`,**一个字都不提 COM**,看着像蓝牙/设备坏了。

    ★ 只初始化、**不在结束时 CoUninitialize**:开出来的公寓要活到这条流被关掉
      为止,而线程池的线程是复用的;提前拆掉 COM 会把还在用的流一起带走。
      每个线程多留一个公寓,进程退出时一起消失,不是问题。
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_MULTITHREADED
    except Exception:                                   # noqa: BLE001
        pass


def _wasapi_index() -> int | None:
    for i, a in enumerate(sd.query_hostapis()):
        if "WASAPI" in str(a.get("name", "")):
            return i
    return None


def _is_junk_device(name: str) -> bool:
    """Windows 自带的假设备/汇总设备,选了一点用没有。"""
    n = name.strip()
    return (not n or n.endswith("()") or "@System32" in n
            or "声音映射器" in n or "Sound Mapper" in n
            or n.startswith("主声音驱动") or n.startswith("Primary Sound"))


def list_output_devices() -> list[tuple[int, str, int]]:
    """★ 只列 **WASAPI** 这一层、并且按名字去重。

    以前把 MME / DirectSound / WASAPI / WDM-KS 四层全列出来,
    同一副耳机出现四次(名字还不一样),选都没法选。
    WASAPI 延迟最低、名字最干净,就只留它。
    """
    api = _wasapi_index()
    out: list[tuple[int, str, int]] = []
    seen: set[str] = set()
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] <= 0:
            continue
        if api is not None and d["hostapi"] != api:
            continue
        name = str(d["name"]).strip()
        if _is_junk_device(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append((i, name, int(d["default_samplerate"])))
    return out


def default_output_index() -> int | None:
    """系统当前默认输出(WASAPI 那一层)。没有就退到第一个能用的。"""
    devs = list_output_devices()
    if not devs:
        return None
    api = _wasapi_index()
    if api is not None:
        try:
            d = sd.query_hostapis(api).get("default_output_device", -1)
            if any(i == d for i, _, _ in devs):
                return int(d)
        except Exception:              # noqa: BLE001
            pass
    return devs[0][0]


def list_loopback_devices() -> list[str]:
    """能抓「它正在放的声音」的设备(扬声器/耳机/VB-Cable)。

    ★ 用 soundcard 那边的设备名 —— 它和 PortAudio 的名字不保证一模一样,
      所以环回来源必须按这个名字去开,不能拿耳返那套名字混着用。
    """
    if not _HAVE_SC:
        return []
    try:
        return [s.name for s in sc.all_speakers()]
    except Exception:                  # noqa: BLE001
        return []


def live_output_names() -> list[str]:
    """★★ 用 COM 实时枚举扬声器 —— **不要用 PortAudio 那份快照**。

    2026-10-18 实测到的真事:PortAudio 的设备表是 `Pa_Initialize` 那一刻
    **冻住的**,之后插拔耳机它一概不知道。

        服务进程(23:17 起)→ 列表里还写着「耳机 (某蓝牙耳机)」
        新建一个进程(23:2x) → 那副耳机**根本不存在**,已经换成有线耳机了

    于是 `device_alive()` 高兴地说「在」,`out_sps` 却一直是 0 —— 流早就跟着
    那台已经消失的设备一起死了。表现就是:**换上有线耳机,一点耳返都没有,
    而界面上一切正常**。这一条害人之处就在于它不报错。
    """
    if _HAVE_SC:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)   # ★ COM 按线程算
            return [str(s.name).strip() for s in sc.all_speakers()]
        except Exception:              # noqa: BLE001
            pass
    return [n for _, n, _ in list_output_devices()]


def refresh_portaudio() -> None:
    """让 PortAudio **重新枚举**设备。

    ★ 它会把 PortAudio 名下所有流关掉 —— 所以只能在「反正要重开」的时候调,
      调用顺序必须是 `close_output()` → 这个 → `open_output()`。
    """
    try:
        sd._terminate()                # noqa: SLF001
    except Exception:                  # noqa: BLE001
        pass
    try:
        sd._initialize()               # noqa: SLF001
    except Exception:                  # noqa: BLE001
        pass


def find_device(name: str | None, fallback: int | None = None) -> int | None:
    """按名字模糊找设备。★ 找不到就返回 None,不静默挑一个错的。

    ★ 只在 list_output_devices() 那一份(WASAPI+去重)里找 ——
      不然「耳机 (某蓝牙耳机)」会命中 MME 那台,延迟白高一大截。
    """
    devs = list_output_devices()
    if not name:
        return fallback if fallback is not None else default_output_index()
    key = name.strip().lower()
    for i, n, _ in devs:                       # 精确优先
        if n.lower() == key:
            return i
    for i, n, _ in devs:
        if key in n.lower():
            return i
    return None


class Engine:
    def __init__(self, sr: int = SR, block: int | None = None) -> None:
        self.sr = sr
        # ★ 块长只有一处真相:BLOCK_MS。以前默认值是模块级 BLOCK=480,
        #   改块长要改两处,漏一处就成了「开流 5ms、处理 10ms」。
        self.block = int(block) if block else max(64, int(round(BLOCK_MS / 1000.0 * sr)))
        self.block_ms_set = BLOCK_MS     # 开流时按设备重算(见 pick_block_ms)
        block = self.block               # 下面几处还按块长建对象

        self.voice = VoiceBuffer(target=int(round(0.020 * sr)))
        self.resampler: Resampler | None = None
        self.resampler_rate = sr
        self.phone_fmt = "i16"             # 手机发过来的样本格式(hello 里会改)
        # ★ 手机页面实际拿到的采集参数(getSettings)。浏览器可以「你要求关掉
        #   处理、它照样开着」,所以必须让它自己报上来,不是我们以为。
        self.cap_info: dict = {}
        self.dc = DriftController(sr, self.voice.target)
        self.loop = LoopSource()           # 电脑正在放的声音(伴奏)
        self._rx_bytes = 0
        self._rx_bps = 0.0
        self._rx_t0 = time.time()
        self._out_frames = 0
        self._out_sps = 0.0
        self._stat_t = time.time()
        self._stat_f = 0

        self.recorder = Recorder()
        self.recording = False

        # 可调参数(网页那边改)
        self.reverb_mix = 0.0
        self.backing_level = 0.7
        self.monitor_level = 1.0

        self.params: dict = {
            "gain": 1.0, "denoise": 0.0, "gate": True, "gate_db": -55.0,
            # ★ 高通拐点从写死的 80Hz 变成可调(2026-10-19):
            #   专业人声链在 80~120Hz 之间按嗓音挑,不是一律 80。
            "hp_hz": 80.0,
            "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0,
            "deess": 0.0,
            "comp": False, "comp_ratio": 3.0, "comp_threshold": -22.0, "makeup": 0.0,
            # ★ 直通对照:跳过整条链(只保留输入增益),用来 A/B 听「处理前/后」。
            #   调音时听不出差别就说明这个滑杆在空转。
            "bypass": False,
        }
        self.chain_bypass = False
        self.chain = VoiceChain(sr, block)
        self.chain.set(**{k: v for k, v in self.params.items() if k != "bypass"})
        self.reverb = PartitionedConvolver(make_reverb_ir(sr=sr), block=block)

        # 伴奏(文件)
        self.backing: np.ndarray | None = None      # (n, 2) float32
        self.backing_raw: np.ndarray | None = None  # 原采样率的那份
        self.backing_raw_rate = SR
        self.backing_pos = 0
        self.backing_loop = False
        self.backing_name = ""

        self.stream: sd.OutputStream | None = None
        self.device: int | None = None
        self.device_name = ""

        # 抖动缓冲水位自适应(见 _tune_buffer)
        self._tune_und = 0
        self._tune_t = time.time()
        self._tune_last_bad = time.time()
        self._tune_hold = 0          # 「撑不住的水位」记忆(见 _tune_buffer)
        self._tune_hold_t = 0.0
        self._tune_up_t = 0.0        # 上一次往上加的时刻(加块至少隔 2 秒)
        self._open_t = 0.0           # 当前这条流是什么时候开的(开流后 5 秒不判欠载)
        # ★ 最近欠载的时刻。自适应靠它分辨「反复欠载」和「偶发一次离群」。
        self._und_times: deque[float] = deque(maxlen=16)

        # 表头(给网页画电平)
        self.lock = threading.Lock()
        self.rms = 0.0
        self.peak = 0.0
        self.out_latency_ms = 0.0
        # ★ 输出流实际开成什么样:"exclusive" / "shared"。蓝牙一般开不出独占,
        #   那不是失败,是这个方案在这台设备上拿不到 —— 界面要说实话。
        self.out_mode = "none"
        self.excl_err = ""

        # ★ 包到达的抖动:手机到底是不是「一阵一阵」送过来的。
        #   以前只能看抖动缓冲涨没涨,那是果;这里量的是因。
        self._arr_last = 0.0
        self._arr_gaps: deque[float] = deque(maxlen=512)

        # AI 降噪(DeepFilterNet3)。★ 默认关;打开了才加载,包不在就只报错、不崩。
        self.ai = None
        self.ai_error = ""
        # ★ 伴奏对齐:AI 那一级让**人声**晚了 60ms,伴奏要跟着晚同样多,
        #   不然录出来的 mix 里人声永远拖在伴奏后面。存的是整块(10ms)的队列。
        self.back_q: deque[np.ndarray] = deque()

        # ★ 耳返旁路:耳返走 AI **之前**的信号,省掉模型的 60ms。
        #   录音那一路照旧吃 AI —— 迟到的代价只让人听见,不让文件承担。
        self.monitor_pre_ai = False
        # ★ 耳返那条路用**自己的一整条轻链**:高通 + 我们的谱减 + 门。
        #   只过门是不够的 —— 实测室内底噪 −40dBFS,裸信号进耳返就是一片「嗡嗡嗡」。
        #   这套谱减是零延迟的(块内 overlap-add),不像 AI 要等 60ms。
        self.mon_chain = VoiceChain(sr, block)
        self.mon_denoise = 0.65
        self.mon_chain.set(denoise=self.mon_denoise, hp_hz=80.0)
        self.mon_gate = self.mon_chain.gate   # 兼容:外部只读它
        # ★ 低延迟模式:抖动缓冲水位上限从 8 块(80ms)锁到 3 块(30ms)。
        self.low_latency = False

    # ------------------------------------------------------------ 输出
    def open_output(self, device_name: str | None = None) -> str:
        _com_init()                        # ★ 谁调用就在谁的线程里初始化 COM
        self.close_output()
        # ★ 不给名字 = 用系统当前的默认输出(连着蓝牙就是用蓝牙,换成有线就跟着换)
        dev = find_device(device_name)
        if dev is None:
            raise RuntimeError(f"找不到输出设备:{device_name}")
        info = sd.query_devices(dev)
        # ★ 设备不支持 48k 就退到它的默认采样率 —— 然后整条链都跟着它走。
        #   半路转换只做一层,不然两处重采样会互相打架。
        rate = SR
        try:
            sd.check_output_settings(device=dev, samplerate=rate,
                                     channels=2, dtype="float32")
        except Exception:
            rate = int(info["default_samplerate"])

        self.sr = rate
        # ★ 块长按设备挑:有线 5ms、蓝牙 10ms。见 pick_block_ms()。
        self.block_ms_set = pick_block_ms(info)
        self.block = max(64, int(round(self.block_ms_set / 1000.0 * rate)))
        self.back_q.clear()                # 块长可能变了,队列里旧的块不能留
        # ★★ 换流就把抖动缓冲**倒空**。不倒的话,上一任流留下的旧账
        #   (比如「只进不出」攒下的 480ms)会原封不动带进新流 ——
        #   新流一开就还是那个 480ms 的延迟,而且靠 2.25% 的漂移校正
        #   要二十几秒才排得出去,中间一直在丢音。重启之后看着像没修好。
        self.voice.reset()
        # ★ 换了设备就换采样率。AI 那一级只吃 48k:不是 48k 就把它摘掉,
        #   并且把原因留在状态里 —— 不许静默失效。
        if self.ai is not None and rate != 48000:
            self.ai = None
            self.ai_error = f"AI 降噪需要 48000Hz,当前设备是 {rate}Hz,已自动关闭"
        self.reverb = PartitionedConvolver(make_reverb_ir(sr=rate), block=self.block)
        # ★ 换了采样率,整条处理链都要按新参数重建 —— 参数本身原样带过去
        self.chain = VoiceChain(rate, self.block)
        # ★ bypass 是引擎级开关,链子不认识它 —— 这里漏过滤过一次,
        #   表现是「换了输出采样率就整个耳返打不开」,只有启动日志里那一行提了。
        self.chain.set(**{k: v for k, v in self.params.items() if k != "bypass"})
        # 耳返那条轻链也跟着重建(块长/采样率都变了),门参数照抄主链
        self.mon_chain = VoiceChain(rate, self.block)
        self.mon_chain.set(denoise=self.mon_denoise, hp_hz=80.0)
        self.mon_chain.gate.enabled = self.chain.gate_enabled
        self.mon_chain.gate.threshold_db = self.chain.gate_db
        self.mon_gate = self.mon_chain.gate
        # 手机那边可能还是 48k,重建重采样器(★ 两边同采样率时也要留着,
        # 它现在兼任时钟漂移校正,不能因为「率一样」就丢掉)
        self.resampler = Resampler(self.resampler_rate, rate)
        # ★ 换个设备就从「20ms」重新起算,由 _tune_buffer 慢慢往下探/往上加。
        #   以前是按块算的(3 块),块长一改这个数就跟着漂 —— 现在直接写毫秒。
        self.voice.target = int(round(0.020 * rate))
        self._tune_und = self.voice.underruns
        self._tune_t = time.time()
        self._tune_last_bad = time.time()
        # ★ 换了设备就把「撑不住的水位」忘了:蓝牙撑不住的,有线可能撑得住。
        self._tune_hold = 0
        self._tune_hold_t = 0.0
        self._tune_up_t = 0.0
        self._open_t = time.time()   # ★ 开流后的预热期,见 _tune_buffer 开头
        self._und_times.clear()      # 上一任流的欠载记录不能带进新流
        self.dc = DriftController(rate, self.voice.target)
        # ★ 环回来源要是正好是新的耳返设备 → 立刻停掉,不然一定啸叫
        if self.loop.active and self.loop.device and info["name"]:
            if self.loop.device.strip().lower() == info["name"].strip().lower():
                self.loop.stop()
        # 伴奏是按旧的 sr 载入的,重载一次
        if self.backing_raw is not None:
            self._rebuild_backing()

        # ★ 先开、再起、**成功了才挂到 self.stream 上**。
        #   起不来就当场关掉并抛出去 —— 半死的流挂上去会变成
        #   「界面说设备好好的,耳机里一点声音都没有」,一查一个不吱声。
        #
        # ★★ 只用**共享模式**。独占这条路上栽过一次,别再来(2026-10-18):
        #   实测有线 Realtek 独占 8.0ms / 共享 22.0ms —— 确实快 14ms。
        #   但**在蓝牙上再开一个独占流,会把已经跑着的共享流弄死**:
        #     独占尝试前 36 回调/秒 → 尝试后 **0 回调/秒**,
        #     而设备还在、device_alive() 说一切正常 ⇒「只进不出」,
        #     抖动缓冲顶到 480ms、overruns 疯涨,听起来就是断断续续咔咔响。
        #   而且独占会把设备锁住,别的音乐软件就放不出声了。
        #   ⇒ 那 14ms 不要了。**谁也别再往这儿加第二次开流。**
        self.out_mode = "shared"
        self.excl_err = ""
        stream, _lat, err = _open_out(dev, rate, self.block, self._callback, None)
        if stream is None:
            raise RuntimeError(f"开不了输出设备:{err}")
        self.stream = stream
        self.device = dev
        self.device_name = info["name"]
        # ★ PortAudio 自己估的输出缓冲(含驱动那层)。这是延迟账里唯一
        #   以前只能靠猜的一项 —— 现在直接问它要。
        try:
            self.out_latency_ms = float(stream.latency) * 1000.0
        except Exception:                  # noqa: BLE001
            self.out_latency_ms = 0.0
        return info["name"]

    def _tune_buffer(self) -> None:
        """★ 抖动缓冲的水位**自己找**:先低,一有欠载就往上加,稳 20 秒再往下试。

        ★ 为什么不能一上来就压到最低:不同手机 / USB / 系统负载下的抖动差很多,
          写死一个数,要么延迟白高(常见的抱怨就是这个),要么每隔几秒咔一下。
        ★ 规则只有两条,而且都很慢:缓冲一变,漂移校正就得跟着重新收敛,
          一次跳两块会听出「拉了一下」。
        """
        now = time.time()
        # ★★ 刚开流的前 5 秒**不判欠载**。
        #   开流那一阵手机在重连、抖动缓冲从 0 攒起、重采样器在收敛 ——
        #   这几秒里的欠载全是我们自己造的,但自适应会当成「链路撑不住」,
        #   一路把水位从 20ms 顶到 30、40、70ms,而且 5 分钟内不肯降回来。
        #   表现就是:**什么都没变,重启一次延迟就高一截。**
        if now - self._open_t < 20.0:
            self._tune_und = self.voice.underruns
            self._tune_last_bad = now
            self._tune_t = now
            return
        sr = self.sr or SR
        # ★ 下限还要**装得下两块**:少于两块,只要一包晚到就是一个静音洞。
        lo = max(int(round(BUF_LO_MS / 1000.0 * sr)), 2 * self.block)
        # ★ 低延迟模式:封顶 25ms,不许它自己爬到 60ms。
        hi = int(round((BUF_HI_LL_MS if self.low_latency else BUF_HI_MS) / 1000.0 * sr))
        hi = max(hi, lo + self.block)
        if self.voice.underruns < self._tune_und:
            # ★ 计数变小 = 手机重连时 voice.reset() 把它清零了,不是「没有欠载」。
            #   不认出来就会把「清零」当成「又出了一次事」,每重连一次白涨 10ms。
            self._tune_und = self.voice.underruns
            self._tune_t = now           # 刚重连,重新起算「稳了多久」,别马上往下探
        elif self.voice.underruns > self._tune_und:
            # 出事了。★ 但**至少隔 2 秒才加一块**:一次系统卡顿能连着欠载几次,
            #   不设间隔就会一口气涨到 80ms —— 然后又得等好几分钟才降回来。
            delta = self.voice.underruns - self._tune_und
            self._tune_und = self.voice.underruns
            self._tune_last_bad = now
            for _ in range(min(delta, 16)):
                self._und_times.append(now)
            # ★★ **一次离群不算数**。手机 JS 线程偶尔卡一下就是 60~70ms 的空洞
            #   (实测 arr_max 到过 67.9ms),那种洞靠 20ms 水位挡不住,靠 40ms 也
            #   照样挡不住 —— 为它把水位永久抬高,就是「延迟白高,咔照旧」。
            #   ⇒ 只有 10 秒内**反复**欠载,才认定是水位真的不够。
            recent = sum(1 for t in self._und_times if now - t <= 15.0)
            if recent >= 5 and now - self._tune_up_t >= 2.0:
                self._tune_up_t = now
                self._tune_t = now
                # ★ 记住「这个水位它撑不住」:降到这儿就会再咔一下,所以
                #   5 分钟内不许再探到这个高度以下(不然就是每 20 秒咔一声)。
                self._tune_hold = self.voice.target
                self._tune_hold_t = now
                self.voice.target = min(hi, self.voice.target + self.block)
                self._und_times.clear()
        elif now - self._tune_last_bad > 20.0 and now - self._tune_t > 20.0:
            # 20 秒一个欠载都没有 → 往下探一块(探到亏了下一轮会自己加回来)
            nxt = max(lo, self.voice.target - self.block)
            # ★ 但「刚刚撑不住的那个水位」5 分钟内不许再碰
            allowed = (nxt > self._tune_hold) or (now - self._tune_hold_t > 300.0)
            if self.voice.target > lo and allowed:
                self.voice.target = nxt
                self._tune_t = now
        if self.dc is not None:
            self.dc.target = self.voice.target

    def device_alive(self) -> bool:
        """当前耳返设备还在不在(蓝牙断了/耳机拔了 → False)。

        ★★ 必须查 **实时** 的那一份(`live_output_names`)。查 PortAudio 快照的话,
          拔掉的耳机在那里永远「还在」—— 见 `live_output_names` 的注释。
        """
        if self.stream is None:
            return False
        me = (self.device_name or "").strip().lower()
        if not me:
            return False
        return any(n.strip().lower() == me for n in live_output_names())

    def close_output(self) -> None:
        _com_init()                        # stop/close 也要同一套 COM
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # ------------------------------------------------------------ 回调
    def _callback(self, outdata, frames, time_info, status) -> None:
        # ★ 这一整个函数都在实时线程里。不要 print,不要 open(),不要等锁太久。
        n = frames
        self._out_frames += n          # ★ 只加一个整数,别在实时线程里取时间
        # 取手机来的声音(不足就补静音 —— 宁可安静一瞬,也不要等)
        raw = self.voice.pull(n)
        # ★ 耳返旁路要用「AI 之前」的这份。别指望 raw 还会是原样 ——
        #   下一行就把 raw 换成了模型的输出。
        raw_pre_ai = raw

        # AI 降噪(可选):放在所有 EQ/压缩之前 —— 模型要的是「原始一点的麦声」。
        ai = self.ai
        if ai is not None:
            try:
                raw = ai.process(raw)
            except Exception as e:              # noqa: BLE001
                # ★ 实时线程里出事只许「摘掉这一级」,不许把异常抛出去 ——
                #   抛出去整个输出流就断了,听起来是「耳返突然没声」。
                self.ai = None
                self.ai_error = f"AI 降噪出错,已自动关闭:{e}"

        # 整条干声链:增益 -> 噪声门 -> 降噪 + 高通 + EQ -> 压缩
        # ★ 直通对照只保留输入增益 —— 不保留的话 A/B 时音量差一大截,
        #   耳朵会先被响度骗走,听不出音色。
        if self.chain_bypass:
            voice = raw * float(self.params.get("gain", 1.0))
        else:
            voice = self.chain.process(raw)

        # 混响(干湿比)
        wet = self.reverb.process(voice) if self.reverb_mix > 0.0001 else None
        v = voice if wet is None else (voice * (1.0 - 0.35 * self.reverb_mix)
                                       + wet * (0.9 * self.reverb_mix))

        # 伴奏:文件伴奏 + 电脑正在放的声音(环回)。两个加起来,
        # 都**不经过调音链** —— 伴奏不需要加工,只要一个音量。
        # ★ 人声是单声道,伴奏是立体声:人声复制到左右,伴奏原样保留两边,
        #   声场才不会被压扁。
        voice2 = np.repeat(v[:, None], 2, axis=1)
        back = self.loop.pull(n)                       # 立体声,原样
        if self.backing is not None:
            back = back + self._next_backing(n)[:, None]

        # ★ AI 开着的时候人声晚了 60ms,伴奏得跟着晚同样多 —— 否则录出来的
        #   mix 里人声永远拖在伴奏后面。整块地挪,不做插值。
        # ★ 只挪一次。耳返旁路要的是「没挪过」的那份,所以保留 back。
        back_ai = self._delay_backing(back) if ai is not None else back

        # ★ 限幅放在最后一道:超过 ±1 直接削平。
        #   再花哨的软限幅也不如「别把一整段录音毁在三个采样点上」。
        # ★ 伴奏音量只压伴奏,别把人声一起乘进去。
        # ★★ 耳返音量**只许作用于耳返**,不许乘进录音 —— 2026-10-19 修的。
        #   以前 monitor_level 一起乘进了 mix,后果是:把耳返拉小,录出来的
        #   mix.wav 跟着一起变小声;拉到 0 就是一条静音文件。而电平表读的是
        #   out_mix,于是一起「死」—— 表现就是「关掉耳返就录不了、电平也不跳」,
        #   看着像录音依赖耳返,其实只是音量被乘错了地方。
        mix = np.clip(voice2 + back_ai * float(self.backing_level), -1.0, 1.0)

        # ★ 耳返旁路:让演唱者听到的是「此刻的自己」,不是 60ms 前的自己。
        #   这一路只用噪声门(逐块、零延迟),不加任何看未来的处理;
        #   录音那一路(mix)照旧吃 AI —— 文件里的音质一分不让。
        out_mix = np.clip(mix * float(self.monitor_level), -1.0, 1.0)
        if self.monitor_pre_ai and ai is not None:
            mon = self.mon_chain.process(raw_pre_ai)
            mon2 = np.repeat(mon[:, None], 2, axis=1)
            out_mix = np.clip((mon2 + back * float(self.backing_level))
                              * float(self.monitor_level), -1.0, 1.0)

        outdata[:] = out_mix

        # ★ 电平表量的是「麦克风进来多少」(录音那一路),**不是耳返音量** ——
        #   耳返拉到 0 的时候电平表也得跳,不然看着像没在录。
        rms = float(np.sqrt(np.mean(voice2 * voice2)) + 1e-12)
        pk = float(np.max(np.abs(voice2)))
        self.rms = rms
        self.peak = pk
        if self.recording:
            self.recorder.push(voice, mix)

    def _delay_backing(self, back: np.ndarray) -> np.ndarray:
        """把伴奏推迟 AI 那一级的延时(整块挪,不插值)。

        ★ 实时线程:这里只允许 deque 的 append/popleft,不许分配大块、不许等。
        ★ AI 的延时是 6 块(60ms);不是整块就退回 0 —— 宁可不对齐,
          也不许吐出一块长度不对的东西把整条混音搞崩。
        """
        n = len(back)
        ai = self.ai
        d = int(getattr(ai, "delay", 0)) if ai is not None else 0
        k = d // n if (n and d >= n and d % n == 0) else 0
        q = self.back_q
        if k == 0:
            if q:
                q.clear()
            return back
        if len(q) < k:
            # 刚打开:先垫几块静音 —— 人声那边也是同样长短的起步空白
            q.append(back)
            return np.zeros_like(back)
        while len(q) > k:
            q.popleft()
        q.append(back)
        return q.popleft()

    def _next_backing(self, n: int) -> np.ndarray:
        if self.backing is None:
            return np.zeros(n, dtype=np.float32)
        out = np.zeros(n, dtype=np.float32)
        pos = 0
        while pos < n:
            take = min(n - pos, len(self.backing) - self.backing_pos)
            if take <= 0:
                if self.backing_loop:
                    self.backing_pos = 0
                    continue
                break
            out[pos:pos + take] = self.backing[self.backing_pos:self.backing_pos + take, 0]
            self.backing_pos += take
            pos += take
        return out

    # ------------------------------------------------------------ 环回伴奏
    def set_loop(self, source: str) -> str:
        """把「电脑正在放的声音」接成伴奏。传空字符串 = 关掉。

        ★ 环回来源和耳返设备是同一台 → 直接拒绝(抓回来的会含我们自己的耳返,
          麦克风 → 耳返 → 再被抓回来,几秒内就是啸叫)。
        """
        name = (source or "").strip()
        if not name:
            self.loop.stop()
            return ""
        out_name = (self.device_name or "").strip().lower()
        if out_name and name.strip().lower() == out_name:
            raise RuntimeError(
                f"不能抓耳返设备自己的声音({name}):会形成回授啸叫。"
                "请挑另一台设备(比如 Realtek 扬声器 / CABLE Input),"
                "或者把耳返换到别的设备。")
        return self.loop.start(name, self.sr)

    def set_params(self, **kw) -> None:
        """网页改参数都走这里。★ 未知键直接忽略,不许因为一个拼错的键整个崩掉。"""
        for k, v in kw.items():
            if k in self.params:
                self.params[k] = v
        # ★ bypass 不是链子里的参数(链子只认识自己的那套),单独拿在手里。
        was_bypass = self.chain_bypass
        self.chain_bypass = bool(self.params.get("bypass", False))
        if self.chain_bypass != was_bypass:
            # 切回来时链子里还留着上一次的尾巴/降噪状态,不清会「噗」一下。
            self.chain.reset()
        chain_params = {k: v for k, v in self.params.items() if k != "bypass"}
        self.chain.set(**chain_params)
        # 耳返那条轻链的门跟着主链走 —— 两个门用同一个阈值,否则会一个开一个关
        self.mon_chain.gate.enabled = self.chain.gate_enabled
        self.mon_chain.gate.threshold_db = self.chain.gate_db

    def set_mon_denoise(self, v: float) -> None:
        """耳返那条路的降噪量(0~1)。只影响耳朵听到的,不影响录音。"""
        self.mon_denoise = float(max(0.0, min(1.0, v)))
        self.mon_chain.set(denoise=self.mon_denoise)

    def set_low_latency(self, on: bool) -> None:
        """低延迟模式:抖动缓冲水位封顶 25ms。

        ★ 现在它自己会爬到 60ms(手机那头 JS 线程一卡就欠载一次)。
          封顶以后手机偶尔卡一下会「咔」一声 —— 一个 10ms 的静音洞,
          不是爆音。这是一个明说的取舍,不是 bug。
        """
        self.low_latency = bool(on)
        if self.low_latency:
            cap = int(round(BUF_HI_LL_MS / 1000.0 * (self.sr or SR)))
            self.voice.target = min(self.voice.target, cap)

    def set_buffer_ms(self, ms: float) -> int:
        """手动把抖动缓冲水位钉在 N 毫秒,并把欠载计数清零重新起算。

        ★ 这是**量具**,不是调音旋钮:自适应那套会自动把水位往上爬,爬上去之后
          5 分钟内不肯再降(`_tune_hold`)。想验证「30ms 到底撑不撑得住」,
          只能把 hold 一起清掉,否则测的一直是它记住的那个旧水位。
        """
        sr = self.sr or SR
        lo = max(int(round(BUF_LO_MS / 1000.0 * sr)), 2 * self.block)
        hi = int(round(BUF_HI_MS / 1000.0 * sr))
        self.voice.target = max(lo, min(hi, int(round(ms / 1000.0 * sr))))
        self.voice.underruns = 0
        self._tune_und = 0
        self._tune_hold = 0
        self._tune_hold_t = 0.0
        self._tune_t = time.time()
        self._tune_last_bad = 0.0
        if self.dc is not None:
            self.dc.target = self.voice.target
        return self.voice.target

    def note_stall(self) -> None:
        """重活(加载模型、手机重连)之后调一次:把这一段欠载当没发生。

        ★ 加载 AI 模型要 1 秒,事件循环被饿住 → 一串欠载 → 自适应逻辑
          以为「手机送不过来」,把水位永久抬高 10~30ms,**而且 5 分钟内不肯再降**。
          那几次欠载是我们自己堵出来的,不是链路的问题,不能算在链路账上。
        """
        self._tune_und = self.voice.underruns
        self._tune_t = time.time()
        # ★★ 还要**重启预热期**。手机一重连,抖动缓冲就被 reset 成空的,
        #   要攒回 20ms 水位得好几秒,这中间必然欠载好几次 —— 那全是我们自己造的。
        #   不认这一段,自适应就把「手机刚连上」误判成「链路撑不住」,白抬 20ms。
        self._open_t = time.time()
        self._und_times.clear()

    def set_ai(self, on: bool) -> str:
        """开/关 AI 降噪。返回空串 = 成功,否则是给人看的原因。

        ★ 加载要 1 秒左右 —— 绝不能在音频回调里做,所以由调用方(服务端线程)
          在外面调;这里只负责建好之后换上去。
        """
        on = bool(on)
        if not on:
            self.ai = None
            self.ai_error = ""
            self.back_q.clear()                 # 伴奏不再推迟
            return ""
        if self.ai is not None:
            return ""
        import ai_denoise                       # 包不在这里才炸,主路不受影响
        if self.sr != ai_denoise.NATIVE_SR:
            self.ai_error = f"AI 降噪需要 48000Hz,当前是 {self.sr}Hz"
            return self.ai_error
        try:
            self.ai = ai_denoise.AIDenoiser(self.sr)
            self.ai_error = ""
            self.back_q.clear()                 # 从零开始垫,别拿旧块对齐
            self.note_stall()                   # 加载那一秒的欠载不算链路的账
            return ""
        except Exception as e:                  # noqa: BLE001
            self.ai = None
            self.ai_error = f"AI 降噪起不来:{e}"
            return self.ai_error

    # ------------------------------------------------------------ 手机输入
    def set_phone_rate(self, rate: int, fmt: str = "i16") -> None:
        """手机每次连上来都会喊一次 hello —— 那就当一次全新的连接处理。

        fmt="f32" = 手机直接发 float32(无损);"i16" = 旧页面发的 int16。
        """
        rate = int(rate or self.sr)
        self.phone_fmt = "f32" if str(fmt).lower() in ("f32", "float32") else "i16"
        self.voice.reset()
        self.chain.reset()
        self.reverb.reset()
        # ★ 就算两边都是 48k 也要有重采样器 —— 它现在兼任时钟漂移校正。
        self.resampler_rate = rate
        self.resampler = Resampler(rate, self.sr)
        self.dc.reset()
        # ★ 重连这一下必然会欠几次(页面切后台/重连的空档),那不算链路撑不住。
        self.note_stall()

    def push_pcm16(self, data: bytes) -> None:
        # ★ 记账:每秒到底收到多少字节。48000Hz 单声道 int16 = 96000 字节/秒。
        #   这个数一出来,「手机发快了」还是「电脑收慢了」当场分得清。
        now_rx = time.time()
        # ★ 记下相邻两包的间隔(毫秒)。手机会把几包攒一起发,间隔就会一会儿 2ms
        #   一会儿 40ms —— 这正是抖动缓冲被迫爬高的原因,得先看得见它。
        if self._arr_last > 0.0:
            self._arr_gaps.append((now_rx - self._arr_last) * 1000.0)
        self._arr_last = now_rx
        self._rx_bytes += len(data)
        if now_rx - self._rx_t0 >= 1.0:
            self._rx_bps = self._rx_bytes / (now_rx - self._rx_t0)
            self._rx_bytes = 0
            self._rx_t0 = now_rx
        if self.phone_fmt == "f32":
            x = np.frombuffer(data, dtype="<f4").astype(np.float32)
        else:
            x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if self.resampler is None:
            self.resampler = Resampler(self.sr, self.sr)
            self.dc.reset()
        # 时钟漂移校正(细节在 DriftController 里)
        v = self.voice
        with v.lock:
            fill = len(v.buf)
        self.resampler.d = self.dc.update(fill)

        x = self.resampler.process(x)
        if len(x):
            was = self.voice.primed
            self.voice.push(x)
            # ★ 刚开流 / 刚重开流之后,缓冲是从**空**开始攒的,头几次欠载
            #   是必然的、也是我们自己造的。不把基线归零的话,自适应会把这些
            #   当成「这条链路撑不住」,白白把水位涨 20ms 并锁 5 分钟。
            if not was and self.voice.primed:
                self.note_stall()

    @property
    def drift(self) -> float:
        return float(self.dc.d)

    # ------------------------------------------------------------ 伴奏
    def load_backing(self, path: Path) -> str:
        # ★ 原始数据留一份:换输出设备(采样率变了)时要重新换算,
        #   不能拿已经换算过的再换算一次 —— 越换越糊。
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        self.backing_raw = data
        self.backing_raw_rate = rate
        self._rebuild_backing()
        self.backing_name = path.name
        return path.name

    def _rebuild_backing(self) -> None:
        data = self.backing_raw
        if data is None:
            return
        if self.backing_raw_rate != self.sr:
            data = self._resample_array(data, self.backing_raw_rate)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        self.backing = data
        self.backing_pos = 0

    def _resample_array(self, data: np.ndarray, src_rate: int) -> np.ndarray:
        """整段重采样(伴奏、离线用)。线性插值,足够。"""
        n_out = int(len(data) * self.sr / src_rate)
        idx = np.linspace(0, len(data) - 1, n_out)
        out = np.empty((n_out, data.shape[1]), dtype=np.float32)
        for ch in range(data.shape[1]):
            out[:, ch] = np.interp(idx, np.arange(len(data)), data[:, ch])
        return out

    # ------------------------------------------------------------ 录音
    def start_recording(self, out_dir: Path) -> tuple[str, str]:
        paths = self.recorder.start(out_dir, self.sr)
        self.recording = True
        return paths

    def stop_recording(self) -> None:
        self.recording = False
        self.recorder.stop()

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        now = time.time()
        # ★ 水位自适应挂在这里:网页每 250ms 拉一次状态,这个频率足够;
        #   而且它绝不能挂在实时回调里(那里不能取时间、不能等锁)。
        self._tune_buffer()
        # ★ 输出端实际每秒跑多少采样。标称 48000 不等于真的 48000 ——
        #   蓝牙设备跑不动时回调会变慢,声音整体被拉长(音调变低),
        #   抖动缓冲也会一路涨。这个数能当场分辨「手机发快了」还是「电脑收慢了」。
        dt = now - self._stat_t
        if dt > 0.001:
            self._out_sps = (self._out_frames - self._stat_f) / dt
            self._stat_t = now
            self._stat_f = self._out_frames
        return {
            "phone": (now - self.voice.last_rx) < 0.8,
            "recording": self.recording,
            "device": self.device_name,
            "backing": self.backing_name,
            "loop_on": self.loop.active,
            "loop_device": self.loop.device,
            "loop_rms": round(float(self.loop.rms), 5),
            "loop_error": self.loop.error,
            "rms": round(float(self.rms), 5),
            "peak": round(float(self.peak), 5),
            "underruns": self.voice.underruns,
            "overruns": self.voice.overruns,
            "buffer": round(self.voice.fill_ratio(), 3),
            "buffer_ms": round(self.voice.fill_samples() / max(1, self.sr) * 1000, 1),
            "target_ms": round(self.voice.target / max(1, self.sr) * 1000, 1),
            "drift": round(self.drift, 5),
            "phone_rate": self.resampler_rate,
            "phone_fmt": self.phone_fmt,
            "cap": dict(self.cap_info),
            # ★ 断连时必须显示 0。以前这个数只在收到包时更新,断了以后
            #   永远停在最后一个值上 —— 界面看着「还在传 191KB/s」,
            #   实际一个字节都没有。2026-10-19 就是这么被它骗过一次。
            "rx_bps": round(float(self._rx_bps) if (now - self.voice.last_rx) < 0.8 else 0.0, 0),
            "out_sps": round(float(self._out_sps), 0),
            "recorded_s": round(self.recorder.written / self.sr, 1),
            "paths": self.recorder.paths,
            "params": dict(self.params),
            "ai": self.ai is not None,
            "ai_error": self.ai_error,
            "ai_latency_ms": (0.0 if self.ai is None else _ai_latency_ms()),
            "reverb_mix": round(float(self.reverb_mix), 3),
            "backing_level": round(float(self.backing_level), 3),
            "monitor_level": round(float(self.monitor_level), 3),
            "monitor_pre_ai": bool(self.monitor_pre_ai),
            "low_latency": bool(self.low_latency),
            "mon_denoise": round(float(self.mon_denoise), 2),
            "out_latency_ms": round(float(self.out_latency_ms), 1),
            "out_mode": self.out_mode,
            "excl_err": self.excl_err,
            "block_ms": round(1000.0 * self.block / (self.sr or SR), 1),
            "arr_p50_ms": _pct(self._arr_gaps, 0.50),
            "arr_p95_ms": _pct(self._arr_gaps, 0.95),
            "arr_max_ms": round(max(self._arr_gaps), 1) if self._arr_gaps else 0.0,
            "sr": self.sr,
        }
