"""DeepFilterNet3 流式降噪 —— 可选的一级,接在干声链前面。

为什么是它(用时长为 45 秒的真人录音量过,见 tools/eval_real_voice.py):
    · 说话/唱的时候基本不动(0~2 dB),拖住 5 秒的长音也只掉 0.2~1.6 dB;
    · 停顿里房间噪声压 33~47 dB;
    · ★ 我们自己写的那套谱减,同样条件下把长音压掉 36 dB —— 它才是「降噪很垃圾」的根。
    · 代价:模型自身延时 1536 点(32.0ms,实测),加块对齐约 52ms。

★ 这个模块**必须能被安全地关掉**:包不在 / 模型不在了 / 采样率不是 48k,
  都只是「这一级不可用」,主路照常出声。绝不许因此整个服务起不来。

★ 只在 48k 下开:模型是 48k 的,44.1k 要先重采样,多一层损耗,不如不开。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PKG = HERE / "_ai_try"
MODEL = PKG / "model"
NATIVE_SR = 48000

# 实测:模型自身延时 1536 点 = 32.0ms(互相关量的,别信封装的 latency_ms)。
# 再留余量当 FIFO,否则 480/512 的节奏会让它隔一阵子吐不出整块。
# ★ 2026-10-18 扫过一遍(整段 45 秒真录音):960 刚好零欠载、但最低水位只剩
#   480(= 一块),一点余量都没有;864 会欠载 846 次。取 1344 —— 最低 864,
#   正好是 6 块,而且总延时 2880 = 6×480,伴奏对齐可以按整块推迟,不用插值。
MODEL_DELAY = 1536
FIFO_PREFILL = 1344
# ★ 总延时 = 模型自己的 1536 + 我们自己预灌的 1344 = 2880 点 = 60.0ms。
#   (别把 MODEL_DELAY 再加进 pending 里 —— 2026-10-18 量出 84ms,就是重复算了。)
TOTAL_DELAY = MODEL_DELAY + FIFO_PREFILL


def available() -> bool:
    return (PKG / "deepfilter_stream").is_dir() and (MODEL / "denoiser_model.onnx").is_file()


def latency_ms() -> float:
    return round(TOTAL_DELAY * 1000.0 / NATIVE_SR, 1)


class AIDenoiser:
    """一整条流:模型会话 + 每路流的状态 + 固定长度的进出。

    只许在**一个线程**里调 process();引擎里就是音频回调那一个线程。
    """

    def __init__(self, sr: int = NATIVE_SR, threads: int = 1) -> None:
        if sr != NATIVE_SR:
            raise ValueError(f"AI 降噪只支持 48000Hz,当前 {sr}Hz")
        if not available():
            raise RuntimeError(f"找不到 DeepFilterNet 包/模型:{PKG}")
        if str(PKG) not in sys.path:
            sys.path.insert(0, str(PKG))
        os.environ.setdefault("DEEPFILTER_STREAM_MODEL_DIR", str(MODEL))

        from deepfilter_stream.model import DeepFilterModel
        self.model = DeepFilterModel(intra_op_num_threads=threads)
        self.stream = self.model.new_stream()
        self.frame = int(self.model.frame_size)
        # ★ FIFO 先灌余量:模型吐 512 的整块,我们要 480 的整块,
        #   不预灌就会隔一阵子「不够 480」→ 补零 → 听感是每隔几秒漏一下。
        # ★ 只灌 FIFO_PREFILL,不要再加 MODEL_DELAY —— 模型输出**本来就晚了 1536**,
        #   再垫一遍就是白多 32ms(实测 84ms,不是 52ms)。
        self.pending = np.zeros(FIFO_PREFILL, dtype=np.float32)
        self.delay = TOTAL_DELAY

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        out = self.stream.process(np.ascontiguousarray(x, dtype=np.float32), NATIVE_SR)
        if out is not None and len(out):
            self.pending = np.concatenate((self.pending, np.asarray(out, dtype=np.float32)))
        if len(self.pending) >= n:
            y = self.pending[:n]
            self.pending = self.pending[n:]
        else:
            # 理论上预灌 960 之后不会走到这里;真走到了就补零,不许改变长度。
            y = np.zeros(n, dtype=np.float32)
            y[: len(self.pending)] = self.pending
            self.pending = self.pending[:0]
        return y

    def reset(self) -> None:
        self.stream = self.model.new_stream()
        self.pending = np.zeros(FIFO_PREFILL, dtype=np.float32)
