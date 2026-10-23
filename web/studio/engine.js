// SingMic Studio —— 播放引擎。只碰 Web Audio,不碰 DOM。
//
// 核心约定(照计划第三节):
//   ★ 移动/复制/裁剪 clip **不渲染** —— 同一份 AudioBuffer 换 start/offset 再排一次。
//   ★ 播放头 = anchor + (ctx.currentTime - anchorCtx),**不累加**。
//   ★ 300ms lookahead + 40ms tick;循环用「周期」排,不是到点重来。

export const LOOK = 0.3;
export const LOOK_HIDDEN = 1.0;
const TICK_MS = 40;

export class Engine {
  constructor() {
    this.ctx = null;
    this.master = null;
    this.playing = false;
    this.project = null;
    this.metro = false;
    this.masterGain = 1;

    this.bufs = new Map();       // sourceId -> AudioBuffer
    this.pending = new Map();    // sourceId -> Promise
    this.bad = new Set();        // sourceId -> 解不开
    this.rendered = new Map();   // renderKey -> AudioBuffer
    this.renderedPending = new Map();

    this.trackNodes = new Map();
    this.meters = new Map();     // 轨 id -> AnalyserNode(电平表旁观)
    this.masterAnalyser = null;
    this._scope = new Float32Array(512);
    this.live = [];              // 已排的 source,stop 时要全掐掉
    this.timer = 0;

    this.onstate = null;
    this.stats = { decodes: 0, renders: 0, scheduled: 0, pendingClips: 0 };

    // 插入效果链(AudioWorklet)。★ 加载失败就明确旁通,绝不静默。
    this.fxReady = false;
    this.fxError = '';
    this.fxInit = null;

    // ★ 冻结轨:trackId -> {sig, buffer, gainAtBake}
    //   有 buffer 的轨改播这条 WAV(烘焙里已含 轨道增益 → 内置链 → VST),
    //   并旁路 worklet 链(否则内置链会跑两遍)。
    this.frozen = new Map();
    this._aud = null;            // 短段试听的循环源
  }

  frozenOf(id) {
    const f = this.frozen.get(id);
    return (f && f.buffer) ? f : null;
  }

  // 冻结 WAV 里已经乘过 gainAtBake;推子要能继续动 ⇒ 用比例补偿
  _gainFor(tr) {
    const f = this.frozenOf(tr.id);
    const g = tr.gain ?? 1;
    if (!f || !(f.gainAtBake > 0)) return g;
    return g / f.gainAtBake;
  }

  setFrozen(trackId, info) {
    this.frozen.set(trackId, info);
    const n0 = this.trackFx && this.trackFx.get(trackId);
    if (n0 && n0.insert) n0.insert.port.postMessage({ type: 'chain', chain: [] });
    if (!this.playing || !this.project) return;
    const tr = this.project.tracks.find(t => t.id === trackId);
    const node = this.trackNodes.get(trackId);
    if (!tr || !node) return;
    // 掐掉这一轨已经排上的旧音频(可能已经排到 300ms 之后)
    this._stopTrackSources(trackId);
    if (!info || !info.buffer) return;
    const now = this.songNow();
    const songTo = Math.max(now, this.schedSong ?? now);
    const dur = Math.min(info.buffer.duration - now, songTo - now);
    if (dur > 0.002) {
      this._emit(info.buffer, node, { gain: 1, fadeIn: 0, fadeOut: 0 },
        this.ctx.currentTime + 0.02, now, dur, false, false, trackId);
    }
    this.applyMix();
  }

  clearFrozen(trackId) {
    this.frozen.delete(trackId);
    if (!this.playing || !this.project) return;
    // 取消冻结 = 回到「按 clip 现场播」;从当前位置重新起一次最干净
    this._stopTrackSources(trackId);
    const at = this.songNow();
    this.play(this.project, at);
  }

  _stopTrackSources(trackId) {
    for (let i = this.live.length - 1; i >= 0; i--) {
      const s = this.live[i];
      if (s._tr !== trackId) continue;
      this.live.splice(i, 1);
      try { s.stop(); } catch (e) {}
      try { s.disconnect(); } catch (e) {}
    }
  }

  // 加载效果链模块。★ 可重复调用;失败会记下原因,下次 play 再试。
  initFx() {
    if (this.fxReady) return Promise.resolve(true);
    if (this.fxInit) return this.fxInit;
    const ctx = this.ensure();
    if (!ctx.audioWorklet || typeof AudioWorkletNode === 'undefined') {
      this.fxError = '这台浏览器没有 AudioWorklet';
      return Promise.resolve(false);
    }
    this.fxInit = ctx.audioWorklet.addModule('/static/studio/fxworklet.js')
      .then(() => {
        this.fxReady = true;
        this.fxError = '';
        // 模块就绪前收下的链,现在补发
        for (const [id, n] of this.trackFx || new Map()) {
          if (n.chainWanted && (n.chainWanted.length || n.insert)) {
            this.setTrackChain({ id, fx: { chain: n.chainWanted } });
          }
        }
        return true;
      })
      .catch((e) => {
        this.fxReady = false;
        this.fxError = String((e && e.message) || e || '加载失败');
        this.fxInit = null;          // 允许下次重试
        return false;
      });
    return this.fxInit;
  }

  // 轨道上第一次真的有插件时才建 worklet;之后即使清空也保留节点走直通
  // (★ 播放中重建节点会把正在响的源全掐断)
  _ensureInsert(n) {
    if (!this.fxReady || !this.ctx || !n) return null;
    if (n.insert) return n.insert;
    let node;
    try {
      node = new AudioWorkletNode(this.ctx, 'singmic-insert', {
        numberOfInputs: 1, numberOfOutputs: 1,
        outputChannelCount: [1], channelCount: 1,
        channelCountMode: 'explicit', channelInterpretation: 'speakers',
      });
    } catch (e) {
      this.fxError = String((e && e.message) || e);
      this.fxReady = false;
      return null;
    }
    try { n.g.disconnect(); } catch (e) {}
    n.g.connect(node);
    node.connect(n.pre);
    n.insert = node;
    return node;
  }

  // 把一条轨的插入链下发给 worklet(不需要重建节点)
  setTrackChain(tr) {
    const n = this.trackFx && this.trackFx.get(tr.id);
    if (!n) return false;
    const real = Array.isArray(tr.fx && tr.fx.chain) ? tr.fx.chain : [];
    n.chainWanted = real;
    if (!this.fxReady) return false;
    // ★ 冻结轨的烘焙里已经含内置链 —— 再跑一遍就是两遍
    if (this.frozenOf(tr.id)) {
      if (n.insert) n.insert.port.postMessage({ type: 'chain', chain: [] });
      return true;
    }
    const chain = real;
    if (!chain.length && !n.insert) return true;   // 从没加过插件 = 零开销
    const node = this._ensureInsert(n);
    if (!node) return false;
    node.port.postMessage({ type: 'chain', chain });
    return true;
  }

  get fxStats() {
    const chains = {};
    for (const [id, n] of this.trackFx || new Map()) {
      if (n.insert) chains[id] = (n.chainWanted || []).length;
    }
    return { ready: !!this.fxReady, error: this.fxError,
             inserts: Object.keys(chains).length, chains };
  }

  ensure() {
    if (this.ctx) return this.ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    // ★ 请求 48k,和导出一致(冻结/试听都按这个采样率渲染)。
    //   浏览器不接受这个采样率时退回默认值,UI 会显示「播放 X Hz / 导出 48k,有细微差」。
    try {
      this.ctx = new AC({ latencyHint: 'interactive', sampleRate: 48000 });
    } catch (e) {
      this.ctx = new AC({ latencyHint: 'interactive' });
    }
    this.master = this.ctx.createGain();
    this.master.gain.value = 1;
    // ★ 电平表挂在总线上,只是个旁观者(analyser 直通)
    this.masterAnalyser = this.ctx.createAnalyser();
    this.masterAnalyser.fftSize = 512;
    this.master.connect(this.masterAnalyser);
    this.masterAnalyser.connect(this.ctx.destination);
    this._scope = new Float32Array(this.masterAnalyser.fftSize);
    return this.ctx;
  }

  // 一条轨现在的峰值电平(0..1);没接线就返回 0
  _peak(an) {
    if (!an) return 0;
    if (!this._scope || this._scope.length !== an.fftSize) {
      this._scope = new Float32Array(an.fftSize);
    }
    an.getFloatTimeDomainData(this._scope);
    let m = 0;
    for (let i = 0; i < this._scope.length; i++) {
      const v = Math.abs(this._scope[i]);
      if (v > m) m = v;
    }
    return m;
  }

  levels() {
    const out = {};
    for (const [id, an] of this.meters) out[id] = this._peak(an);
    return { tracks: out, master: this._peak(this.masterAnalyser) };
  }

  async resume() {
    const ctx = this.ensure();
    if (ctx.state !== 'running') await ctx.resume();
    return ctx;
  }

  // ---- 解码 -------------------------------------------------------------
  sourceUrl(src) {
    const p = String(src?.path || '');
    if (p.startsWith('recordings/')) return '/recordings/' + encodeURIComponent(p.slice(11));
    if (p.startsWith('projects/media/')) return '/api/studio/media/' + encodeURIComponent(p.slice(15));
    return '';
  }

  async buffer(sourceId, url) {
    if (this.bufs.has(sourceId)) return this.bufs.get(sourceId);
    if (this.pending.has(sourceId)) return this.pending.get(sourceId);
    if (!url) throw new Error('没有音频地址');
    const p = (async () => {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`音频取不到 ${r.status}`);
      const ab = await r.arrayBuffer();
      const b = await this.ensure().decodeAudioData(ab);
      this.bufs.set(sourceId, b);
      this.pending.delete(sourceId);
      this.stats.decodes++;
      return b;
    })();
    this.pending.set(sourceId, p);
    try {
      return await p;
    } catch (e) {
      this.pending.delete(sourceId);
      this.bad.add(sourceId);
      throw e;
    }
  }

  // 渲染过的片段(已经变过调),按 key 合并
  async renderedBuffer(key, url) {
    if (this.rendered.has(key)) return this.rendered.get(key);
    if (this.renderedPending.has(key)) return this.renderedPending.get(key);
    const p = (async () => {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`片段取不到 ${r.status}`);
      const b = await this.ensure().decodeAudioData(await r.arrayBuffer());
      this.rendered.set(key, b);
      this.renderedPending.delete(key);
      return b;
    })();
    this.renderedPending.set(key, p);
    try { return await p; } catch (e) { this.renderedPending.delete(key); throw e; }
  }

  // ---- 混音路由 ---------------------------------------------------------
  // ★ FX 的生成必须和导出端(studio_mix._make_ir)用**同一串随机数**:
  //   LCG 种子 12345/13344,同一个衰减包络,同样按**能量**归一。两边频谱才一致。
  //   ★ 别改成按峰值归一:0.6 秒噪声 IR 的总能量有几千,湿声会比干声大 30dB。
  makeIR(sec, decay) {
    const sr = this.ctx.sampleRate;
    const n = Math.max(1, Math.round(sec * sr));
    const buf = this.ctx.createBuffer(2, n, sr);
    const k = Math.max(1, Math.round(decay * sr));
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      let s = (12345 + ch * 999) | 0;
      let en = 0;
      for (let i = 0; i < n; i++) {
        s = (Math.imul(s, 1103515245) + 12345) | 0;
        const u = ((s >>> 0) / 4294967296) * 2 - 1;
        const v = u * Math.exp(-i / k);
        d[i] = v;
        en += v * v;
      }
      const g = en > 0 ? 1 / Math.sqrt(en) : 0;
      for (let i = 0; i < n; i++) d[i] *= g;
    }
    return buf;
  }

  _buildTracks() {
    for (const n of this.trackNodes.values()) { try { n.gain.disconnect(); } catch (e) {} }
    this.trackNodes.clear();
    this.trackPans = this.trackPans || new Map();
    this.trackPans.clear();
    this.trackFx = new Map();
    this.meters.clear();
    const anySolo = this.project.tracks.some(t => t.solo);
    for (const tr of this.project.tracks) {
      const g = this.ctx.createGain();
      const an = this.ctx.createAnalyser();
      an.fftSize = 512;
      const pan = this.ctx.createStereoPanner ? this.ctx.createStereoPanner() : null;
      const audible = !tr.mute && (!anySolo || tr.solo);
      g.gain.value = audible ? this._gainFor(tr) : 0;

      // 信号:g(轨道增益) → [insert worklet] → pre → [干 / 混响 / 延迟] → pan → 电平表 → 总线
      // ★ pre 是插入链的「出口」;旧混响/延迟都从 pre 取,所以链严格排在它们前面。
      //   numpy 侧(studio_mix)是同一个顺序。
      const pre = this.ctx.createGain();
      g.connect(pre);
      const dry = this.ctx.createGain();
      pre.connect(dry);
      const out = pan || an;          // FX 混完的汇合点(每轨自己的)

      if (pan) {
        pan.pan.value = Math.max(-1, Math.min(1, tr.pan || 0));
        dry.connect(pan); pan.connect(an); an.connect(this.master);
      } else {
        dry.connect(an); an.connect(this.master);
      }
      this.trackNodes.set(tr.id, g);
      this.meters.set(tr.id, an);
      if (pan) this.trackPans.set(tr.id, pan);
      const n = { g, pre, dry, out, insert: null, chainWanted: [],
                  rv: null, dl: null, rvKey: '', dlKey: '' };
      this.trackFx.set(tr.id, n);
      // 先建插入链(如果有),再设混响/延迟 —— 后者的支路要从 pre 取
      this.setTrackChain(tr);
      this.setTrackFx(tr);
    }
  }

  // 混响 / 延迟:节点按需创建,参数**边播边改**(不重建轨道,不会把正在响的源掐掉)
  setTrackFx(tr) {
    const n = this.trackFx?.get(tr.id);
    if (!n) return;
    const ctx = this.ctx, t = ctx.currentTime;
    const fx = tr.fx || {};
    const rv = fx.reverb || {};
    const dl = fx.delay || {};
    const rvMix = Math.max(0, Math.min(1, rv.mix || 0));
    const dlMix = Math.max(0, Math.min(1, dl.mix || 0));
    n.dry.gain.setTargetAtTime(Math.max(0, 1 - rvMix - dlMix), t, 0.02);

    if (rvMix > 0.001) {
      const size = Math.max(0.2, Math.min(4, rv.size || 1.6));
      if (!n.rv) {
        const conv = ctx.createConvolver();
        conv.normalize = false;
        const wet = ctx.createGain();
        wet.gain.value = 0;
        n.pre.connect(conv); conv.connect(wet); wet.connect(n.out);
        n.rv = { conv, wet };
      }
      const key = size.toFixed(2);
      if (n.rvKey !== key) {
        n.rv.conv.buffer = this.makeIR(size, Math.max(0.1, Math.min(3, size * 0.45)));
        n.rvKey = key;
      }
      n.rv.wet.gain.setTargetAtTime(rvMix, t, 0.02);
    } else if (n.rv) {
      n.rv.wet.gain.setTargetAtTime(0, t, 0.02);
    }

    if (dlMix > 0.001) {
      if (!n.dl) {
        const dly = ctx.createDelay(2.0);
        const fb = ctx.createGain();
        const wet = ctx.createGain();
        wet.gain.value = 0;
        n.pre.connect(dly); dly.connect(fb); fb.connect(dly);
        dly.connect(wet); wet.connect(n.out);
        n.dl = { dly, fb, wet };
      }
      n.dl.dly.delayTime.setTargetAtTime(
        Math.max(0.01, Math.min(2, dl.time || 0.25)), t, 0.05);
      n.dl.fb.gain.setTargetAtTime(Math.max(0, Math.min(0.92, dl.fb || 0)), t, 0.05);
      n.dl.wet.gain.setTargetAtTime(dlMix, t, 0.02);
    } else if (n.dl) {
      n.dl.wet.gain.setTargetAtTime(0, t, 0.02);
    }
  }

  applyMix() {
    if (!this.playing || !this.project) return;
    const anySolo = this.project.tracks.some(t => t.solo);
    for (const tr of this.project.tracks) {
      const n = this.trackNodes.get(tr.id);
      if (!n) continue;
      const audible = !tr.mute && (!anySolo || tr.solo);
      const v = audible ? this._gainFor(tr) : 0;
      n.gain.setTargetAtTime(v, this.ctx.currentTime, 0.01);
      const p = this.trackPans?.get(tr.id);
      if (p) p.pan.setTargetAtTime(Math.max(-1, Math.min(1, tr.pan || 0)),
        this.ctx.currentTime, 0.01);
    }
  }

  setMaster(v) { if (this.master) this.master.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02); }

  // 解码出来的 PCM 一共占多少字节(给内存提示用)
  memBytes() {
    let b = 0;
    for (const buf of this.bufs.values()) b += buf.length * buf.numberOfChannels * 4;
    for (const buf of this.rendered.values()) b += buf.length * buf.numberOfChannels * 4;
    return b;
  }

  // ---- 播放 -------------------------------------------------------------
  play(project, fromSec) {
    const ctx = this.ensure();
    if (ctx.state !== 'running') ctx.resume();
    // 效果链模块:第一次 play 就地加载;没就绪时链旁通(导出不受影响)
    this.initFx();
    this.project = project;
    this.stopSources();

    const ui = project.ui || {};
    const lp = ui.loop;
    this.looping = !!(lp && Array.isArray(lp) && lp[1] > lp[0] + 0.01);
    this.loopA = this.looping ? Math.max(0, lp[0]) : 0;
    this.loopB = this.looping ? lp[1] : 0;

    let s = Number(fromSec);
    if (!Number.isFinite(s) || s < 0) s = 0;
    if (this.looping && (s < this.loopA || s >= this.loopB)) s = this.loopA;

    this._buildTracks();

    this.anchorSong = s;
    this.anchorCtx = ctx.currentTime + 0.08;
    this.schedSong = s;
    this.schedWhen = this.anchorCtx;
    this.cycleEndWhen = this.looping ? this.anchorCtx + (this.loopB - s) : Infinity;

    this.playing = true;
    this._tick();
    clearInterval(this.timer);
    this.timer = setInterval(() => this._tick(), TICK_MS);
    this.onstate?.(true);
    document.addEventListener('visibilitychange', this._vis = () => this._tick());
  }

  _tick() {
    if (!this.playing) return;
    const ctx = this.ctx;
    const horizon = ctx.currentTime + (document.hidden ? LOOK_HIDDEN : LOOK);
    let guard = 0;
    while (this.schedWhen < horizon && guard++ < 64) {
      const end = Math.min(horizon, this.cycleEndWhen);
      if (end > this.schedWhen) {
        const dur = end - this.schedWhen;
        this._schedule(this.schedSong, this.schedSong + dur, this.schedWhen);
        this.schedSong += dur;
        this.schedWhen = end;
      }
      if (this.schedWhen >= this.cycleEndWhen - 1e-6) {
        if (!this.looping) break;
        this.schedSong = this.loopA;
        this.cycleEndWhen = this.schedWhen + (this.loopB - this.loopA);
      }
    }
    // 非循环:到工程尾巴就自己停
    if (!this.looping && this.project) {
      const end = projectEnd(this.project);
      if (end > 0 && this.songNow() > end + 0.6) this.pause();
    }
  }

  _schedule(songFrom, songTo, whenFrom) {
    const p = this.project;
    if (!p || songTo <= songFrom) return;
    if (this.metro) this._metro(songFrom, songTo, whenFrom);

    const tracks = p.tracks || [];
    const anySolo = tracks.some(t => t.solo);
    for (const tr of tracks) {
      const node = this.trackNodes.get(tr.id);
      if (!node || tr.mute || (anySolo && !tr.solo)) continue;
      // ★ 冻结轨:整轨一条 WAV,按工程时间轴当成一个无缝长的 clip
      const fz = this.frozenOf(tr.id);
      if (fz) {
        const en = fz.buffer.duration;
        if (en > songFrom && songTo > 0) {
          const a = Math.max(0, songFrom), b = Math.min(en, songTo);
          const dur = b - a;
          if (dur > 0.0005) {
            this._emit(fz.buffer, node, { gain: 1, fadeIn: 0, fadeOut: 0 },
              whenFrom + (a - songFrom), a, dur,
              a <= 0.001, b >= en - 0.002, tr.id);
          }
        }
        continue;                 // 不再排原始 clips / 音符
      }
      for (const c of tr.clips || []) {
        const st = +c.start || 0;
        const len = Math.max(0, +c.length || 0);
        const en = st + len;
        if (en <= songFrom || st >= songTo) continue;

        const a = Math.max(st, songFrom), b = Math.min(en, songTo);
        const when = whenFrom + (a - songFrom);
        const dur = b - a;
        if (dur <= 0.0005) continue;

        const buf = this._bufferFor(c);
        if (!buf) continue;              // 还没解码/没渲染 —— 这一轮跳过,绝不拿错音频冒充
        const off = c.renderKey ? (a - st) : (+c.srcIn || 0) + (a - st);
        this._emit(buf, node, c, when, off, dur, a === st, b === en, tr.id);
      }
      // 采样轨:卷帘上画的音符
      for (const nt of tr.notes || []) {
        const st = +nt.start || 0;
        const len = Math.max(0.01, +nt.length || 0);
        const en = st + len;
        if (en <= songFrom || st >= songTo) continue;
        const a = Math.max(st, songFrom), b = Math.min(en, songTo);
        const dur = b - a;
        if (dur <= 0.0005) continue;
        const buf = this._noteBuffer(tr, nt);
        if (!buf) continue;
        this._emitNote(buf, node, tr, nt, whenFrom + (a - songFrom), a - st, dur, tr.id);
      }
    }
  }

  // 采样轨/鼓轨的一个音符:按 (midi - baseMidi) 找池里那一条
  _poolEntry(tr, nt) {
    const smp = tr.sample;
    if (!smp || !smp.pool) return null;
    const semi = Math.round((+nt.midi || 60) - (+smp.baseMidi || 60));
    const lo = smp.poolLo ?? -7, hi = smp.poolHi ?? 7;
    if (semi < lo || semi > hi) return null;
    return smp.pool.find(x => x.semis === semi) || null;
  }

  _noteBuffer(tr, nt) {
    const e = this._poolEntry(tr, nt);
    if (!e) return null;
    const b = this.rendered.get(e.key);
    if (!b) this.renderedBuffer(e.key, e.url).catch(() => {});
    return b || null;
  }

  _emitNote(buf, dest, tr, nt, when, off, dur, trId) {
    const ctx = this.ctx;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const vel = Math.max(0.01, Math.min(1.2, nt.vel ?? 0.9));
    const atk = Math.max(0.001, tr.attack ?? 0.006);
    const rel = Math.max(0.005, Math.min(tr.release ?? 0.06, dur));
    const g = ctx.createGain();
    g.gain.setValueAtTime(off > 0 ? vel : 0.0001, when);
    if (off <= 0.001) g.gain.linearRampToValueAtTime(vel, when + atk);
    else g.gain.setValueAtTime(vel, when);
    g.gain.setValueAtTime(vel, when + Math.max(0, dur - rel));
    g.gain.linearRampToValueAtTime(0.0001, when + dur);
    src.connect(g); g.connect(dest);
    try { src.start(when, Math.max(0, off), dur); } catch (e) { return; }
    src._tr = trId || tr.id;
    this.stats.scheduled++;
    this.live.push(src);
    src.onended = () => {
      const i = this.live.indexOf(src);
      if (i >= 0) this.live.splice(i, 1);
      try { src.disconnect(); g.disconnect(); } catch (e) {}
    };
  }

  _bufferFor(c) {
    if (c.renderKey) {
      const b = this.rendered.get(c.renderKey);
      if (!b && c.renderUrl) {
        this.renderedBuffer(c.renderKey, c.renderUrl).catch(() => {});
      }
      return b || null;
    }
    const src = this.project.sources?.[c.sourceId];
    if (!src) return null;
    const b = this.bufs.get(c.sourceId);
    if (!b && !this.pending.has(c.sourceId) && !this.bad.has(c.sourceId)) {
      this.buffer(c.sourceId, this.sourceUrl(src)).catch(() => {});
    }
    return b || null;
  }

  _emit(buf, dest, clip, when, off, dur, atClipStart, atClipEnd, trId) {
    const ctx = this.ctx;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const cg = clip.gain ?? 1;
    const fi = Math.max(0, Math.min(clip.fadeIn || 0, dur / 2));
    const fo = Math.max(0, Math.min(clip.fadeOut || 0, dur / 2));

    if (fi > 0 || fo > 0 || cg !== 1) {
      const g = ctx.createGain();
      g.gain.setValueAtTime(atClipStart && fi > 0 ? 0 : cg, when);
      if (atClipStart && fi > 0) g.gain.linearRampToValueAtTime(cg, when + fi);
      if (atClipEnd && fo > 0) {
        g.gain.setValueAtTime(cg, when + Math.max(0, dur - fo));
        g.gain.linearRampToValueAtTime(0, when + dur);
      }
      src.connect(g); g.connect(dest);
    } else {
      src.connect(dest);
    }

    try { src.start(when, Math.max(0, off), dur); } catch (e) { return; }
    src._tr = trId || null;
    this.stats.scheduled++;
    this.live.push(src);
    src.onended = () => {
      const i = this.live.indexOf(src);
      if (i >= 0) this.live.splice(i, 1);
      try { src.disconnect(); } catch (e) {}
    };
  }

  _metro(songFrom, songTo, whenFrom) {
    const bpm = Math.max(20, +this.project.bpm || 120);
    const sig = this.project.timeSig || [4, 4];
    const beat = 60 / bpm, bar = beat * (sig[0] || 4);
    let i = Math.ceil(songFrom / beat - 1e-9);
    for (; i * beat < songTo; i++) {
      const t = i * beat;
      if (t < songFrom) continue;
      const accent = Math.abs((t / bar) - Math.round(t / bar)) < 1e-6;
      const when = whenFrom + (t - songFrom);
      const o = this.ctx.createOscillator(), g = this.ctx.createGain();
      o.frequency.value = accent ? 1600 : 1000;
      g.gain.setValueAtTime(0.0001, when);
      g.gain.exponentialRampToValueAtTime(accent ? 0.28 : 0.14, when + 0.002);
      g.gain.exponentialRampToValueAtTime(0.0001, when + 0.045);
      o.connect(g); g.connect(this.master);
      o.start(when); o.stop(when + 0.06);
    }
  }

  stopSources() {
    for (const s of this.live) { try { s.stop(); } catch (e) {} try { s.disconnect(); } catch (e) {} }
    this.live.length = 0;
  }

  pause() {
    if (!this.playing) return;
    this._pauseAt = this.songNow();
    this.playing = false;
    clearInterval(this.timer);
    this.timer = 0;
    this.stopSources();
    document.removeEventListener('visibilitychange', this._vis);
    this.onstate?.(false);
  }

  // 用户按停止
  stop() { this.pause(); this._pauseAt = 0; }

  songNow() {
    if (!this.playing || !this.ctx) return this._pauseAt || 0;
    const c = this.ctx.currentTime;
    if (c < this.anchorCtx) return this.anchorSong;
    if (!this.looping) return this.anchorSong + (c - this.anchorCtx);
    const len = this.loopB - this.loopA;
    const raw = (this.anchorSong - this.loopA) + (c - this.anchorCtx);
    return this.loopA + ((raw % len) + len) % len;
  }

  // 把某个 clip 单独试听(检查器用)
  async previewClip(clip) {
    await this.resume();
    const buf = this._bufferFor(clip);
    if (!buf) return false;
    const g = this.ctx.createGain(); g.connect(this.master);
    const s = this.ctx.createBufferSource(); s.buffer = buf;
    s.connect(g);
    const off = clip.renderKey ? 0 : (+clip.srcIn || 0);
    s.start(0, off, Math.max(0.02, +clip.length || 0));
    this.live.push(s);
    s.onended = () => { try { s.disconnect(); g.disconnect(); } catch (e) {} };
    return true;
  }

  // 卷帘上点一下的即听
  async previewNote(tr, note) {
    await this.resume();
    let buf = this._noteBuffer(tr, note);
    // ★ 第一次点某个音时样本还没解码 —— 以前直接返回 false,表现为「第一下没声,第二下才有」。
    //   这里等它下来再发声,鼓机上一个格子一下试听才不会漏。
    if (!buf) {
      const e = this._poolEntry(tr, note);
      if (!e) return false;
      try { buf = await this.renderedBuffer(e.key, e.url); }
      catch (err) { return false; }
    }
    const g = this.ctx.createGain();
    g.gain.value = Math.max(0.01, Math.min(1.2, note.vel ?? 0.9));
    g.connect(this.master);
    const s = this.ctx.createBufferSource();
    s.buffer = buf; s.connect(g);
    s.start(0, 0, Math.min(buf.duration, Math.max(0.05, note.length)));
    s.onended = () => { try { s.disconnect(); g.disconnect(); } catch (e) {} };
    return true;
  }

  // ---- 短段试听(VST)----------------------------------------------------
  // 渲染回来的是 POST 的字节,不是固定 URL ⇒ 解码成 AudioBuffer 再循环播。
  async audition(source, loop = true) {
    await this.resume();
    let ab;
    if (source instanceof Blob || (source && typeof source.arrayBuffer === 'function')) {
      ab = await source.arrayBuffer();
    } else {
      const r = await fetch(source, { cache: 'no-store' });
      if (!r.ok) {
        let msg = `试听失败 ${r.status}`;
        try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      ab = await r.arrayBuffer();
    }
    const buf = await this.ctx.decodeAudioData(ab);
    this.stopAudition();
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.loop = !!loop;
    const g = this.ctx.createGain();
    g.gain.value = 1;
    src.connect(g); g.connect(this.master);
    src.start();
    this._aud = { src, g };
    return buf.duration;
  }

  stopAudition() {
    const a = this._aud;
    this._aud = null;
    if (!a) return;
    try { a.src.stop(); } catch (e) {}
    try { a.src.disconnect(); a.g.disconnect(); } catch (e) {}
  }

  get auditioning() { return !!this._aud; }

  dropRendered(key) { this.rendered.delete(key); }
}

export function projectEnd(p) {
  let e = 0;
  for (const t of p.tracks || []) {
    for (const c of t.clips || []) e = Math.max(e, (+c.start || 0) + (+c.length || 0));
    for (const n of t.notes || []) e = Math.max(e, (+n.start || 0) + (+n.length || 0));
  }
  return e;
}
