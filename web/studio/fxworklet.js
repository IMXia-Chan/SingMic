// SingMic Studio —— 每轨插入效果链(AudioWorklet)。
//
// ★ 这个文件是 `studio_fx.py` 的镜像:同样的公式、同样的默认值、同样的边界。
//   改任何一处都必须同时改另一边,否则「听到的 = 导出的」就破了。
// ★ 单声道、无前瞻;参数每 128 采样块平滑一次(20ms),静止后与导出完全一致。
// ★ 效果器状态按 slot.id 保留 —— 拖滑杆不会清空延迟线 / 压缩包络。

const BLOCK = 128;
const SMOOTH_SEC = 0.02;
const TAU = Math.PI * 2;

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const finite = (v, d) => (Number.isFinite(v) ? v : d);

function smoothCoef() {
  return 1 - Math.exp(-BLOCK / (SMOOTH_SEC * sampleRate));
}

// ---------------------------------------------------------------------------
// 双二阶(RBJ),TDF-II;与 numpy 的 scipy.signal.lfilter 同一种状态形式
// ---------------------------------------------------------------------------
class Biquad {
  constructor() {
    this.b0 = 1; this.b1 = 0; this.b2 = 0;
    this.a1 = 0; this.a2 = 0;
    this.s1 = 0; this.s2 = 0;
  }

  set(kind, freq, q, gainDb) {
    freq = clamp(freq, 10, sampleRate * 0.49);
    const w0 = TAU * freq / sampleRate;
    const cw = Math.cos(w0), sw = Math.sin(w0);
    let b0, b1, b2, a0, a1, a2;
    if (kind === 'lowpass' || kind === 'highpass') {
      q = clamp(q, 0.05, 20);
      const alpha = sw / (2 * q);
      if (kind === 'lowpass') { b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2; }
      else { b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2; }
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha;
    } else if (kind === 'bandpass') {
      q = clamp(q, 0.05, 20);
      const alpha = sw / (2 * q);
      b0 = alpha; b1 = 0; b2 = -alpha;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha;
    } else if (kind === 'peaking') {
      q = clamp(q, 0.05, 20);
      const A = Math.pow(10, gainDb / 40);
      const alpha = sw / (2 * q);
      b0 = 1 + alpha * A; b1 = -2 * cw; b2 = 1 - alpha * A;
      a0 = 1 + alpha / A; a1 = -2 * cw; a2 = 1 - alpha / A;
    } else if (kind === 'lowshelf') {
      const A = Math.pow(10, gainDb / 40);
      const alpha = sw / 2 * Math.SQRT2;
      const sa = 2 * Math.sqrt(A) * alpha;
      b0 = A * ((A + 1) - (A - 1) * cw + sa);
      b1 = 2 * A * ((A - 1) - (A + 1) * cw);
      b2 = A * ((A + 1) - (A - 1) * cw - sa);
      a0 = (A + 1) + (A - 1) * cw + sa;
      a1 = -2 * ((A - 1) + (A + 1) * cw);
      a2 = (A + 1) + (A - 1) * cw - sa;
    } else { // highshelf
      const A = Math.pow(10, gainDb / 40);
      const alpha = sw / 2 * Math.SQRT2;
      const sa = 2 * Math.sqrt(A) * alpha;
      b0 = A * ((A + 1) + (A - 1) * cw + sa);
      b1 = -2 * A * ((A - 1) + (A + 1) * cw);
      b2 = A * ((A + 1) + (A - 1) * cw - sa);
      a0 = (A + 1) - (A - 1) * cw + sa;
      a1 = 2 * ((A - 1) - (A + 1) * cw);
      a2 = (A + 1) - (A - 1) * cw - sa;
    }
    this.b0 = b0 / a0; this.b1 = b1 / a0; this.b2 = b2 / a0;
    this.a1 = a1 / a0; this.a2 = a2 / a0;
  }

  process(x) {
    const y = this.b0 * x + this.s1;
    this.s1 = this.b1 * x - this.a1 * y + this.s2;
    this.s2 = this.b2 * x - this.a2 * y;
    return y;
  }
}

// ---------------------------------------------------------------------------
// 效果器基类:目标参数 -> 每块平滑一次
// ---------------------------------------------------------------------------
class Effect {
  constructor(id, type, params) {
    this.id = id;
    this.type = type;
    this.enabled = true;
    this.cur = {};
    this.target = {};
    this.setTarget(params, true);
  }

  setTarget(params, immediate) {
    params = params || {};
    for (const k of Object.keys(params)) {
      const v = params[k];
      if (typeof v === 'number' && Number.isFinite(v)) {
        this.target[k] = v;
        if (immediate || !(k in this.cur)) this.cur[k] = v;
      } else {
        this.target[k] = v;
        this.cur[k] = v;          // 枚举/布尔直接切换
      }
    }
  }

  beginBlock() {
    const a = smoothCoef();
    for (const k of Object.keys(this.target)) {
      const t = this.target[k];
      if (typeof t === 'number') this.cur[k] = this.cur[k] + (t - this.cur[k]) * a;
      else this.cur[k] = t;
    }
    this.update();
  }

  update() {}
  process(x) { return x; }
}

// ---------------------------------------------------------------------------
// 滤波
// ---------------------------------------------------------------------------
class FilterFx extends Effect {
  constructor(id, p) { super(id, 'filter', p); this.bq = new Biquad(); }
  update() {
    this.bq.set(this.cur.mode || 'lowpass', this.cur.cutoff || 1000, this.cur.q ?? 0.707, 0);
  }
  process(x) { return this.bq.process(x); }
}

class Eq3Fx extends Effect {
  constructor(id, p) {
    super(id, 'eq3', p);
    this.low = new Biquad(); this.mid = new Biquad(); this.high = new Biquad();
  }
  update() {
    this.low.set('lowshelf', this.cur.lowFreq || 120, 0.707, this.cur.lowGain || 0);
    this.mid.set('peaking', this.cur.midFreq || 1000, this.cur.midQ ?? 1, this.cur.midGain || 0);
    this.high.set('highshelf', this.cur.highFreq || 6000, 0.707, this.cur.highGain || 0);
  }
  process(x) {
    let y = x;
    if (Math.abs(this.cur.lowGain || 0) > 1e-9) y = this.low.process(y);
    if (Math.abs(this.cur.midGain || 0) > 1e-9) y = this.mid.process(y);
    if (Math.abs(this.cur.highGain || 0) > 1e-9) y = this.high.process(y);
    return y;
  }
}

// ---------------------------------------------------------------------------
// 失真
// ---------------------------------------------------------------------------
class DistortionFx extends Effect {
  constructor(id, p) { super(id, 'distortion', p); this.toneY = 0; }
  update() {
    this.drive = Math.max(1, this.cur.drive || 1);
    this.den = Math.tanh(this.drive);
    this.tone = clamp(this.cur.tone || 8000, 20, sampleRate * 0.49);
    this.a = 1 - Math.exp(-TAU * this.tone / sampleRate);
    this.mix = clamp(this.cur.mix ?? 1, 0, 1);
  }
  process(x) {
    const shaped = Math.tanh(x * this.drive) / (Math.abs(this.den) > 1e-9 ? this.den : 1);
    this.toneY += this.a * (shaped - this.toneY);
    return x * (1 - this.mix) + this.toneY * this.mix;
  }
}

// ---------------------------------------------------------------------------
// 压缩 / 限幅
// ---------------------------------------------------------------------------
class CompressorFx extends Effect {
  constructor(id, p) { super(id, 'compressor', p); this.fast = 0; this.slow = 0; }
  update() {
    this.thr = this.cur.threshold ?? -18;
    this.ratio = Math.max(1, this.cur.ratio ?? 3);
    this.knee = Math.max(0, this.cur.knee ?? 6);
    this.makeup = this.cur.makeup ?? 0;
    this.aa = Math.exp(-1 / Math.max(1e-4, (this.cur.attack ?? 12) * 0.001 * sampleRate));
    this.ar = Math.exp(-1 / Math.max(1e-4, (this.cur.release ?? 140) * 0.001 * sampleRate));
    this.slope = 1 - 1 / this.ratio;
    this.half = this.knee / 2;
  }
  process(x) {
    // ★ |x| 一阶平滑收敛到均值(正弦 2/π),乘 π/2 校准成峰值 —— 与 studio_fx.py 同一行
    const a = Math.abs(x) * (Math.PI / 2);
    this.fast = (1 - this.aa) * a + this.aa * this.fast;
    this.slow = (1 - this.ar) * a + this.ar * this.slow;
    const env = Math.max(this.fast, this.slow);
    const over = 20 * Math.log10(env + 1e-9) - this.thr;
    let gr;
    if (this.knee <= 0.01) gr = Math.max(0, over) * this.slope;
    else if (over <= -this.half) gr = 0;
    else if (over >= this.half) gr = over * this.slope;
    else gr = this.slope * (over + this.half) * (over + this.half) / (2 * this.knee);
    return x * Math.pow(10, (this.makeup - gr) / 20);
  }
}

class LimiterFx extends Effect {
  constructor(id, p) {
    super(id, 'limiter', p);
    this.fast = 0; this.slow = 0; this.gs = 1;
  }
  update() {
    this.ceiling = Math.pow(10, (this.cur.ceiling ?? -1) / 20);
    this.ar = Math.exp(-1 / Math.max(1e-4, (this.cur.release ?? 50) * 0.001 * sampleRate));
    this.aa = Math.exp(-1 / Math.max(1e-4, 0.001 * sampleRate)); // 固定 1ms 启动
    this.rel = this.ar;
  }
  process(x) {
    // ★ 同压缩:π/2 把均值检波校准成峰值
    const a = Math.abs(x) * (Math.PI / 2);
    this.fast = (1 - this.aa) * a + this.aa * this.fast;
    this.slow = (1 - this.ar) * a + this.ar * this.slow;
    const env = Math.max(this.fast, this.slow);
    const target = Math.min(1, this.ceiling / Math.max(env, 1e-9));
    this.gs = (1 - this.rel) * target + this.rel * this.gs;
    return x * Math.min(target, this.gs);
  }
}

// ---------------------------------------------------------------------------
// 合唱 / 镶边
// ---------------------------------------------------------------------------
class ChorusFx extends Effect {
  constructor(id, p, type = 'chorus') {
    super(id, type, p);
    this.phase = 0;
    this.mkBuf();
  }
  mkBuf() {
    const maxd = Math.ceil(((this.cur.base || 12) + (this.cur.depth || 6)) * sampleRate / 1000) + 4;
    if (!this.buf || this.buf.length < maxd) this.buf = new Float32Array(maxd);
    this.size = this.buf.length;
    this.write = 0;
  }
  update() {
    this.base = this.cur.base ?? 12;
    this.depth = Math.max(0, this.cur.depth ?? 6);
    this.rate = Math.max(0.001, this.cur.rate ?? 0.5);
    this.mix = clamp(this.cur.mix ?? 0.5, 0, 1);
    const need = Math.ceil((this.base + this.depth) * sampleRate / 1000) + 4;
    if (!this.buf || this.buf.length < need) this.mkBuf();
  }
  readMod(x) {
    const d = (this.base + this.depth * (0.5 + 0.5 * Math.sin(this.phase))) * sampleRate / 1000;
    let r = this.write - d;
    if (r < 0) r += this.size * Math.ceil(-r / this.size);
    const i0 = Math.floor(r) % this.size;
    const i1 = (i0 + 1) % this.size;
    const frac = r - Math.floor(r);
    const wet = this.buf[i0] * (1 - frac) + this.buf[i1] * frac;
    this.buf[this.write] = x;
    this.write = (this.write + 1) % this.size;
    this.phase += TAU * this.rate / sampleRate;
    if (this.phase > TAU) this.phase -= TAU;
    return wet;
  }
  process(x) {
    const wet = this.readMod(x);
    return x * (1 - this.mix) + wet * this.mix;
  }
}

class FlangerFx extends ChorusFx {
  constructor(id, p) {
    // ★ 第二个参数必须是 p、第三个才是 type —— 之前写成 (id,'flanger',p),
    //   结果 params 变成了字符串 'flanger',参数全丢、退化成默认值(音色直接错)。
    super(id, p, 'flanger');
    this.fbBuf = new Float32Array(4096);
    this.fbWrite = 0;
    this.dFix = 1;
  }
  update() {
    super.update();
    const d = Math.max(1, Math.round((this.base || 1.5) * sampleRate / 1000));
    if (!this.fbBuf || this.fbBuf.length < d + 2) this.fbBuf = new Float32Array(d + 2);
    this.dFix = d;
    this.fb = clamp(this.cur.feedback ?? 0.5, 0, 0.9);
  }
  process(x) {
    const wetMod = this.readMod(x);
    const size = this.fbBuf.length;
    let ri = this.fbWrite - this.dFix;
    while (ri < 0) ri += size;
    const fixed = this.fbBuf[ri % size];
    const wet = wetMod + this.fb * fixed;
    this.fbBuf[this.fbWrite] = wet;
    this.fbWrite = (this.fbWrite + 1) % size;
    return x * (1 - this.mix) + wet * this.mix;
  }
}

// ---------------------------------------------------------------------------
// 颤音 / 比特压缩
// ---------------------------------------------------------------------------
class TremoloFx extends Effect {
  constructor(id, p) { super(id, 'tremolo', p); this.phase = 0; }
  update() {
    this.rate = Math.max(0.001, this.cur.rate ?? 4);
    this.depth = clamp(this.cur.depth ?? 0.6, 0, 1);
  }
  process(x) {
    const g = 1 - this.depth * (0.5 + 0.5 * Math.sin(this.phase));
    this.phase += TAU * this.rate / sampleRate;
    if (this.phase > TAU) this.phase -= TAU;
    return x * g;
  }
}

class BitcrushFx extends Effect {
  constructor(id, p) {
    super(id, 'bitcrush', p);
    this.counter = 0; this.held = 0;
  }
  update() {
    this.bits = Math.round(clamp(this.cur.bits ?? 8, 4, 16));
    const hz = Math.max(1, this.cur.crushHz ?? 6000);
    this.hold = Math.max(1, Math.round(sampleRate / hz));
    this.steps = Math.pow(2, this.bits - 1);
    this.mix = clamp(this.cur.mix ?? 1, 0, 1);
    if (this.counter >= this.hold) this.counter = 0;
  }
  process(x) {
    if (this.counter === 0) {
      this.held = Math.round(x * this.steps) / this.steps;
    }
    this.counter = (this.counter + 1) % this.hold;
    return x * (1 - this.mix) + this.held * this.mix;
  }
}

const CTORS = {
  filter: FilterFx,
  eq3: Eq3Fx,
  distortion: DistortionFx,
  compressor: CompressorFx,
  limiter: LimiterFx,
  chorus: ChorusFx,
  flanger: FlangerFx,
  tremolo: TremoloFx,
  bitcrush: BitcrushFx,
};

function makeEffect(slot) {
  const C = CTORS[slot.type];
  if (!C) return null;
  const fx = new C(String(slot.id || Math.random().toString(36).slice(2)),
                   slot.params || {});
  fx.enabled = slot.enabled !== false;
  return fx;
}

// ---------------------------------------------------------------------------
// Processor
// ---------------------------------------------------------------------------
class InsertChain extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chain = [];
    this.port.onmessage = (e) => {
      const d = e.data || {};
      if (d.type === 'chain') this.setChain(Array.isArray(d.chain) ? d.chain : []);
      else if (d.type === 'reset') this.chain = [];
    };
  }

  setChain(list) {
    const old = new Map(this.chain.map((fx) => [fx.id, fx]));
    const next = [];
    for (const slot of list) {
      if (!slot || !slot.type) continue;
      const prev = old.get(slot.id);
      if (prev && prev.type === slot.type) {
        prev.enabled = slot.enabled !== false;
        prev.setTarget(slot.params || {});
        next.push(prev);
      } else {
        const fx = makeEffect(slot);
        if (fx) next.push(fx);
      }
      if (next.length >= 24) break;
    }
    this.chain = next;
  }

  process(inputs, outputs) {
    const out = outputs[0];
    if (!out || !out.length) return true;
    const ch0 = out[0];
    const n = ch0.length;
    const inp = (inputs[0] && inputs[0][0]) ? inputs[0][0] : null;
    if (!inp) {
      for (const ch of out) ch.fill(0);
      return true;
    }
    const chain = this.chain;
    if (!chain.length) {
      ch0.set(inp);
      for (let c = 1; c < out.length; c++) out[c].set(inp);
      return true;
    }
    for (const fx of chain) fx.beginBlock();
    for (let i = 0; i < n; i++) {
      let v = inp[i];
      for (let k = 0; k < chain.length; k++) {
        const fx = chain[k];
        if (fx.enabled) v = fx.process(v);
      }
      if (!Number.isFinite(v)) v = 0;
      if (v > 4) v = 4; else if (v < -4) v = -4;
      ch0[i] = v;
    }
    for (let c = 1; c < out.length; c++) out[c].set(ch0);
    return true;
  }
}

registerProcessor('singmic-insert', InsertChain);
