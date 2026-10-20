"""声音处理链 —— 给唱歌用的。

    x -> 高通 -> 噪声门 -> 频谱降噪 -> 三段 EQ -> 压缩 -> (混响在外面) -> 限制器

★★ 这一整个文件都跑在**音频实时线程**里。所以:
   - 不许 print、不许 open()、不许碰文件;
   - 不许每个回调新建大数组(除了固定大小的临时数组);
   - 所有「设计滤波器」的活都在 __init__ 里一次做完。

★ 为什么用 FFT 做 EQ 和降噪,而不是一串 biquad:
  这台机器的 Python 是 3.14,scipy 还没装上;纯 Python 逐样本递归滤波
  (biquad)在 48kHz 下会把实时线程吃掉一大半 CPU。
  短时傅里叶 + 重叠相加是等效的(零相位),而且 numpy 的 FFT 是 C 写的,快得多。
  重建是精确的:窗函数和归一化在下面逐点算过,不是拍脑袋的 1.5。
"""
from __future__ import annotations

import numpy as np


class SpectralEQ:
    """短时傅里叶 + 重叠相加。高通 / 降噪 / 三段 EQ 全在这一层做。

    帧长 = 2 × block,跳跃 = block(50% 重叠),Hann 窗。
    """

    # ---- 降噪常数(自测直接读这几个数,别在代码里再埋一份) ----
    DD_ALPHA = 0.98          # 判决引导先验 SNR 的递归系数(0.98 ≈ 100ms 记忆)
    MS_SUBWIN_S = 0.10       # 最小统计的子窗长
    MS_WINDOWS = 8           # 保留几个子窗(观察窗 = 0.8 秒)
    # ★ 子窗最小值天然比噪声均值低 2.66dB(白噪实测,逐 bin 1σ 只有 0.72dB ——
    #   所以它是个「统计常数」,不是拍出来的):10^(2.66/10) = 1.85。
    #   量法见 tools/calib_denoise.py。
    MS_BIAS = 1.85

    def __init__(self, sr: int, block: int) -> None:
        self.sr = sr
        self.block = block
        self.n = 2 * block
        self.win = np.hanning(self.n).astype(np.float32)
        self.freqs = np.fft.rfftfreq(self.n, 1.0 / sr).astype(np.float32)
        nb = len(self.freqs)

        # ★ 逐点归一化:不是所有窗函数的 COLA 常数都是漂亮的 1.5,
        #   这里直接把「两块叠加后的窗平方和」算出来,按点除 —— 精确到浮点。
        self.norm = (self.win[:block] ** 2 + self.win[block:] ** 2).astype(np.float32)
        self.norm = np.maximum(self.norm, 1e-6)

        self.prev_in = np.zeros(block, dtype=np.float32)
        self.tail = np.zeros(block, dtype=np.float32)

        # ---- 降噪状态 ----
        self.noise = None                       # 噪声功率估计
        self.ps = None                          # 平滑后的功率谱
        self.sub_min = None                     # 当前子窗内的最小值
        self.ring = None                        # 各子窗最小值(最小统计的环)
        self.ring_pos = 0
        self.ring_n = 0
        self.ms_blocks = max(1, int(round(self.MS_SUBWIN_S * sr / max(1, block))))
        self.ms_left = self.ms_blocks
        self.dd_state = np.zeros(nb, dtype=np.float32)   # DD 递归:G_prev² · γ_prev
        self.g_applied = np.ones(nb, dtype=np.float32)   # 真正乘上去的增益(带时间平滑)
        self._gwork = np.ones(nb, dtype=np.float32)      # 临时数组,不在实时线程里新分配

        # 参数(默认都是「不处理」)
        self.hp_hz = 80.0
        self.eq_low_db = 0.0
        self.eq_mid_db = 0.0
        self.eq_high_db = 0.0
        self.denoise = 0.0
        self._dirty = True
        self._mag = np.ones(len(self.freqs), dtype=np.float32)

        # ---- 齿音抑制(de-esser)------------------------------------------
        # ★ 为什么必须有它:专业人声链一定会补 3~8kHz(清晰度/临场感),
        #   而 s / sh / 齿音正好也在这一段 —— 补高频和刺耳是同一件事的两面。
        #   专业做法不是「不补」,是「补完再把那一瞬压回去」。
        # ★ 拓扑照开源共识(Calf Deesser / oxideav-audio-filter / deesser crate):
        #   高频段检测 + 向下压缩,增益只作用在高频段,中低频一个采样都不碰。
        # ★ 判据用「高频占全频的比例」,不是绝对电平:
        #   绝对电平会被增益、离麦远近、这句唱得响不响改掉;而齿音的本质
        #   是「高频相对太突出」,这个比例对音量不敏感。
        self.deess = 0.0             # 0..1 强度旋钮(网页那个滑杆)
        self.deess_hz = 5500.0       # 受压段起点
        self.deess_rel_db = -20.0    # 高频比全频低这么多以上 → 不算齿音,不压
        self.deess_max_db = 9.0      # 上限。再多就是「大舌头」
        self.deess_ratio = 3.0
        self.deess_gain_db = 0.0     # 平滑后的当前衰减(负数)
        # 软过渡:硬切在 5.5kHz 会「咔」一声,用一个 1kHz 的坡道淡化边界
        self._deess_w = np.clip((self.freqs - self.deess_hz) / 1000.0,
                                0.0, 1.0).astype(np.float32)
        self._deess_mask = self._deess_w > 0.0
        # ★ 预分配,实时线程里不许每帧 new 一个大数组
        self._deess_bins = np.ones(len(self.freqs), dtype=np.float32)

    # ------------------------------------------------------------ 设计
    def _design(self) -> None:
        f = self.freqs
        mag = np.ones_like(f, dtype=np.float32)

        # 二阶高通:80Hz 以下迅速滚掉(隆隆声、桌面震动、喷麦)
        if self.hp_hz > 20:
            r = (f / self.hp_hz) ** 4
            mag *= r / (1.0 + r)

        # 低架:250Hz 以下,温和地增/减
        if abs(self.eq_low_db) > 0.01:
            mag *= 10 ** (self.eq_low_db * (1.0 / (1.0 + (f / 250.0) ** 2)) / 20.0)

        # 中频峰:1.5kHz,给声音「贴脸」的那一段
        if abs(self.eq_mid_db) > 0.01:
            octv = np.log2(np.maximum(f, 1.0) / 1500.0)
            shape = np.exp(-(octv / 0.9) ** 2)
            mag *= 10 ** (self.eq_mid_db * shape / 20.0)

        # 高架:4kHz 以上,气声和齿音都在这儿
        if abs(self.eq_high_db) > 0.01:
            mag *= 10 ** (self.eq_high_db * (1.0 - 1.0 / (1.0 + (f / 4000.0) ** 2)) / 20.0)

        self._mag = mag.astype(np.float32)
        self._dirty = False

    def reset(self) -> None:
        self.prev_in[:] = 0
        self.tail[:] = 0
        self.noise = None
        self.ps = None
        self.sub_min = None
        self.ring = None
        self.ring_pos = 0
        self.ring_n = 0
        self.ms_left = self.ms_blocks
        self.dd_state[:] = 0.0
        self.g_applied[:] = 1.0

    # ------------------------------------------------------------ 降噪
    def _track_noise(self, p: np.ndarray) -> None:
        """最小统计法跟踪噪声功率。

        ★ 为什么是「子窗最小值」而不是「快降慢升」:
          快降慢升的跟踪器永远停在噪声的**低分位**(它升得慢),偏多少取决于
          噪声的起伏,不是一个常数 —— 以前乘的 2.5 是拿白噪凑出来的,
          碰上真实房间噪声(空调、机箱、人声渗进来)就不对了。
          最小值统计量的偏差是**纯统计**的:跟噪声是什么无关,只跟子窗数、
          自由度有关,所以可以用一个测出来的常数补平。
        ★ 平滑功率谱再取最小值:直接拿单帧周期图取最小,会被某一帧的
          随机低谷骗到(白噪单帧功率能掉到均值的 1/6),估出来的噪声偏低。
        """
        # ★ 谐波保护(2026-10-18 用真唱的长音量出来的):
        #   拖住不换的长音在频谱上是「稳定 + 峰高又少」,最小值统计分不出
        #   它和噪声 —— 0.8 秒后噪声底爬到长音上,长音整段被压 36dB。
        #   判据:峰值/均值很高,而且比当前噪声底响得多 ⇒ 这一帧不喂噪声估计。
        #   (人声的元音也会命中的,这是想要的:噪声底只该在停顿/清辅音里更新。)
        tonal = self._is_tonal(self.ps if self.ps is not None else p)

        if self.ps is None:
            self.ps = p.copy()
            self.sub_min = p.copy()
            self.ring = np.full((self.MS_WINDOWS, len(p)), np.inf, dtype=np.float32)
            self.ring_pos = 0
            self.ring_n = 0
            self.ms_left = self.ms_blocks
        else:
            # 65ms 的平滑(0.85/0.15 @10ms 帧),既要抹掉单帧起伏,又不能把
            # 突然变响的人声拖得太久
            self.ps *= 0.85
            self.ps += 0.15 * p
            # ★ 逐帧照样取最小:长音的 ps 比噪声底高,min 天然取不到它;
            #   取到只可能是「真的更低了」—— 那本来就该更新。真正把长音
            #   灌进 ring 的是下面「换子窗时拿 ps 当起点」那一步。
            np.minimum(self.sub_min, self.ps, out=self.sub_min)

        self.ms_left -= 1
        if self.ms_left <= 0:
            # 一个子窗结束:记下它的最小值,换一个空的重新开始
            self.ms_left = self.ms_blocks
            self.ring[self.ring_pos] = self.sub_min
            self.ring_pos = (self.ring_pos + 1) % self.MS_WINDOWS
            self.ring_n = min(self.MS_WINDOWS, self.ring_n + 1)
            # ★ 谐波帧里「换新子窗」不能拿当前 ps 当起点 —— 那等于把长音
            #   直接写进 ring。用上一次的噪声估计顶上,等停了再恢复。
            if tonal and self.noise is not None:
                self.sub_min = self.noise.copy()
            else:
                self.sub_min = self.ps.copy()
            rows = self.ring if self.ring_n == self.MS_WINDOWS else self.ring[:self.ring_n]
            self.noise = (rows.min(axis=0) * np.float32(self.MS_BIAS)).astype(np.float32)

    def _is_tonal(self, ps: np.ndarray) -> bool:
        """这一帧像不像「稳定的谐波」(长音/元音),而不是噪声。"""
        if self.noise is None:
            return False
        m = float(ps.mean())
        if m <= 1e-12:
            return False
        return (float(ps.max()) > 8.0 * m) and (m > 4.0 * float(self.noise.mean()))

    def _denoise(self, p: np.ndarray) -> np.ndarray:
        """算这一帧要乘的幅度增益(和 bins 等长)。

        ★ 用的是「判决引导(Ephraim–Malah)先验 SNR + Wiener 增益」,
          不是老的「1 - 过减系数 × 噪声/信号」:
          - 老的式子逐 bin 独立算,噪声里那些刚好低的 bin 会一帧开一帧关 ——
            **那就是「水声」**;而且它没有记忆,人声一进来也得等一帧才开。
          - DD 用上一帧的增益做先验,弱音不会瞬间掉下去,强音一进来立刻打开,
            同样抑制量下残留的波动小得多(这就是它在增强里当了几十年标配的原因)。
        """
        self._track_noise(p)
        if self.noise is None:
            # 前 100ms 还没攒够一个子窗,先原样放过去
            self.g_applied[:] = 1.0
            return self.g_applied

        s = float(self.denoise)
        # 过减:强噪声下 DD 会偏「保守」,把噪声先放大一点点再算 SNR,
        # 强度越大放得越多 —— 这就是旋钮拉满时「压得更死」的那部分
        over = 1.0 + 1.4 * s
        gamma = self.ps / (self.noise * np.float32(over) + np.float32(1e-20))

        # DD:ξ = α·G²γ(上一帧) + (1-α)·max(γ-1, 0)
        xi = np.float32(self.DD_ALPHA) * self.dd_state
        xi += np.float32(1.0 - self.DD_ALPHA) * np.maximum(gamma - 1.0, 0.0)
        g = self._gwork
        np.divide(xi, 1.0 + xi, out=g)

        # ★ 频率方向 3 点平滑:邻 bin 一起进出,单 bin 的孤立翻转就没了
        #   (这就是「水声」的另一个来源 —— 一个 bin 在 0 和 1 之间来回跳)
        g[1:-1] = (0.25 * g[:-2] + 0.5 * g[1:-1] + 0.25 * g[2:]).astype(np.float32)

        # 强度旋钮:同样是那个 SNR,强度越大压得越深(指数 > 1 就是在 dB 上加倍)
        np.power(g, np.float32(1.0 + 1.5 * s), out=g)
        # 地板:再深也留一点,全零会让底噪变成「呼吸」一样的抽气声
        floor = np.float32(10.0 ** (-(6.0 + 30.0 * s * s) / 20.0))
        np.maximum(g, floor, out=g)
        # ★ 起得快、落得慢:人声的起音被慢吞吞地打开会听到「扑」的一声
        rise = g > self.g_applied
        ga = self.g_applied
        ga[rise] = 0.60 * g[rise] + 0.40 * ga[rise]
        ga[~rise] = 0.30 * g[~rise] + 0.70 * ga[~rise]
        # 给下一帧的 DD 用:ξ 的先验 = 这一帧真正用掉的增益² × 这一帧的 SNR
        self.dd_state = (ga * ga * gamma).astype(np.float32)
        return ga

    # ------------------------------------------------------------ 处理
    def process(self, x: np.ndarray) -> np.ndarray:
        if self._dirty:
            self._design()

        n = self.block
        frame = np.concatenate((self.prev_in, x)) * self.win
        self.prev_in = x.copy()

        X = np.fft.rfft(frame).astype(np.complex64)
        mag = np.abs(X)

        # ---- 频谱降噪 ----
        if self.denoise > 0.0001:
            X *= self._denoise(mag * mag)
        else:
            # ★ 关掉旋钮要把所有状态清干净,不然重新打开时第一次会用旧的世界
            self.g_applied[:] = 1.0
            self.dd_state[:] = 0.0
            self.noise = None
            self.ps = None
            self.sub_min = None
            self.ring = None
            self.ring_pos = 0
            self.ring_n = 0
            self.ms_left = self.ms_blocks

        # ---- 高通 + EQ ----
        if self._mag is not None:
            X *= self._mag

        # ---- 齿音抑制 ----
        # ★ 放在 EQ **之后**:量到的是「已经补过 3~8kHz 的声音」。
        #   放在前面等于拿没补过的信号判断齿音 —— 补上去的那几 dB 从来没被看见,
        #   滑杆推得越狠,齿音漏得越多,而且完全不报错。
        if self.deess > 0.0001:
            m = np.abs(X)
            m *= m
            e_hi = float(m[self._deess_mask].mean()) + 1e-20
            e_all = float(m.mean()) + 1e-20
            rel_db = 10.0 * np.log10(e_hi / e_all)
            over = rel_db - self.deess_rel_db
            if over > 0.0:
                target_db = -over * (1.0 - 1.0 / self.deess_ratio) * self.deess
                target_db = max(target_db, -self.deess_max_db * self.deess)
            else:
                target_db = 0.0
            # 起得快(齿音是一瞬间)、落得慢(松太快会「噗」)
            coef = 0.55 if target_db < self.deess_gain_db else 0.20
            self.deess_gain_db += (target_db - self.deess_gain_db) * coef
            g = np.float32(10.0 ** (self.deess_gain_db / 20.0))
            b = self._deess_bins
            b[:] = 1.0
            # 只改高频那几个 bin,坡道处按权重渐变
            np.add(b, self._deess_w * (g - np.float32(1.0)), out=b,
                   where=self._deess_mask)
            X *= b
        else:
            self.deess_gain_db = 0.0

        y = np.fft.irfft(X, n=self.n).astype(np.float32) * self.win
        # 重叠相加:这一帧的前半 + 上一帧留下的后半
        head = y[:n] + self.tail
        self.tail = y[n:]
        return (head / self.norm).astype(np.float32)


class Gate:
    """噪声门 —— 不唱的时候把麦克风关掉,免得耳机里全是底噪。

    ★ 用「保持 + 释放」,不是硬切:硬切会在一句话的尾巴上砍出咔哒声。
    """

    def __init__(self, sr: int, block: int) -> None:
        self.block_ms = 1000.0 * block / sr
        self.threshold_db = -55.0
        self.enabled = True
        self.gain = 1.0
        self.hold_blocks = 0
        self.hold_max = int(120 / self.block_ms)      # 120ms 保持

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return x
        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        db = 20.0 * np.log10(rms)
        if db > self.threshold_db:
            self.hold_blocks = self.hold_max
            target = 1.0
        else:
            if self.hold_blocks > 0:
                self.hold_blocks -= 1
                target = 1.0
            else:
                target = 0.0
        # 开得快(5ms)、关得慢(180ms)
        coef = 0.55 if target > self.gain else 0.035
        self.gain += (target - self.gain) * coef
        return x * float(self.gain)


class Compressor:
    """压缩器 —— 唱得响的时候往回收一点,让整首歌不会忽大忽小。

    ★ 块级(10ms)增益,不是逐样本包络。对这个用途足够,而且绝不会过冲。
    """

    def __init__(self, sr: int, block: int) -> None:
        bms = 1000.0 * block / sr
        self.attack = 1.0 - np.exp(-bms / 8.0)       # 8ms
        self.release = 1.0 - np.exp(-bms / 180.0)    # 180ms
        self.threshold_db = -22.0
        self.ratio = 3.0
        self.makeup_db = 0.0
        self.enabled = False
        self.gain_db = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return x
        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        level_db = 20.0 * np.log10(rms)
        over = max(0.0, level_db - self.threshold_db)
        target_db = -over * (1.0 - 1.0 / max(1.0, self.ratio)) + self.makeup_db
        coef = self.attack if target_db < self.gain_db else self.release
        self.gain_db += (target_db - self.gain_db) * coef
        g = float(10.0 ** (self.gain_db / 20.0))
        return x * g


class Limiter:
    """最后一道:超过 ±1 直接削平。宁可扁一点,不许爆。"""

    def process(self, x: np.ndarray) -> np.ndarray:
        return np.clip(x, -1.0, 1.0)


class VoiceChain:
    """一条完整的干声处理链。混响在引擎里做(它需要分块卷积)。"""

    def __init__(self, sr: int, block: int) -> None:
        self.sr = sr
        self.block = block
        self.eq = SpectralEQ(sr, block)
        self.gate = Gate(sr, block)
        self.comp = Compressor(sr, block)
        self.limiter = Limiter()
        self.input_gain = 1.0

        # 对外参数(网页改这些)
        self.denoise = 0.0
        self.hp_hz = 80.0               # ★ 高通拐点。以前它被偷偷绑在噪声门上
                                        #   (开=80、关=40),关掉门声音会突然变厚 ——
                                        #   这种隐藏耦合没人查得到,现在独立成一个数。
        self.gate_enabled = True
        self.gate_db = -55.0
        self.eq_low_db = 0.0
        self.eq_mid_db = 0.0
        self.eq_high_db = 0.0
        self.deess = 0.0
        self.comp_enabled = False
        self.comp_ratio = 3.0
        self.comp_threshold_db = -22.0
        self.comp_makeup_db = 0.0

    def set(
        self,
        gain: float | None = None,
        denoise: float | None = None,
        hp_hz: float | None = None,
        gate: bool | None = None,
        gate_db: float | None = None,
        eq_low: float | None = None,
        eq_mid: float | None = None,
        eq_high: float | None = None,
        deess: float | None = None,
        comp: bool | None = None,
        comp_ratio: float | None = None,
        comp_threshold: float | None = None,
        makeup: float | None = None,
    ) -> None:
        if gain is not None:
            self.input_gain = float(np.clip(gain, 0.0, 8.0))
        if denoise is not None:
            self.denoise = float(np.clip(denoise, 0.0, 1.0))
            self.eq.denoise = self.denoise
        if gate is not None:
            self.gate_enabled = bool(gate)
            self.gate.enabled = self.gate_enabled
        if gate_db is not None:
            self.gate_db = float(gate_db)
            self.gate.threshold_db = self.gate_db
        if eq_low is not None:
            self.eq_low_db = float(np.clip(eq_low, -15, 15))
        if eq_mid is not None:
            self.eq_mid_db = float(np.clip(eq_mid, -15, 15))
        if eq_high is not None:
            self.eq_high_db = float(np.clip(eq_high, -15, 15))
        if deess is not None:
            self.deess = float(np.clip(deess, 0.0, 1.0))
            self.eq.deess = self.deess
        if comp is not None:
            self.comp_enabled = bool(comp)
            self.comp.enabled = self.comp_enabled
        if comp_ratio is not None:
            self.comp_ratio = float(np.clip(comp_ratio, 1.0, 10.0))
            self.comp.ratio = self.comp_ratio
        if comp_threshold is not None:
            self.comp_threshold_db = float(comp_threshold)
            self.comp.threshold_db = self.comp_threshold_db
        if makeup is not None:
            self.comp_makeup_db = float(np.clip(makeup, 0.0, 18.0))
            self.comp.makeup_db = self.comp_makeup_db
        if hp_hz is not None:
            self.hp_hz = float(np.clip(hp_hz, 20.0, 300.0))
        # 把这些丢给频谱层(hp 也算:改了它也要重新设计幅度)
        dirty = (self.eq.hp_hz != self.hp_hz
                 or self.eq.eq_low_db != self.eq_low_db
                 or self.eq.eq_mid_db != self.eq_mid_db
                 or self.eq.eq_high_db != self.eq_high_db)
        self.eq.hp_hz = self.hp_hz
        self.eq.eq_low_db = self.eq_low_db
        self.eq.eq_mid_db = self.eq_mid_db
        self.eq.eq_high_db = self.eq_high_db
        if dirty:
            self.eq._dirty = True

    def process(self, x: np.ndarray) -> np.ndarray:
        x = x * self.input_gain
        x = self.gate.process(x)
        # ★ 压缩挪到 EQ **之前**(2026-10-19,照专业人声链的共识改的):
        #   标准顺序是 减法 EQ → 压缩 → de-esser → 加法 EQ。
        #   我们只做一次 FFT,四件事在频谱层一次算完,所以能落到代码里的顺序
        #   就是「压缩 → [高通 + 三段 EQ + de-esser]」。
        #   这一条最要紧:压缩在响的地方把齿音一起抬起来,de-esser 必须
        #   看得见被抬过的那一版;以前 de-esser 根本没有,这个问题看不出来。
        x = self.comp.process(x)
        x = self.eq.process(x)
        # ★ 最后一道削顶。之前这个 Limiter 类写着却没人调用 —— 增益拉满时
        #   录音里会出现超过 0dBFS 的采样,回放时才是爆音。宁可在源头削平。
        return self.limiter.process(x)

    def reset(self) -> None:
        self.eq.reset()
        self.gate.gain = 1.0
        self.comp.gain_db = 0.0
