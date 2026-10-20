// 浏览器 worklet ↔ numpy 的逐点对照工具(自测调用)。
//
// 用法:node tools/fx_parity.js '<json>'
//   json = {"sr":48000,"n":8192,"seed":7,"chain":[...]}
// 输出:{ "samples": [...前 64 个], "rms": ..., "peak": ... }
//
// ★ 不启动浏览器:把 fxworklet.js 当普通脚本加载,给 `AudioWorkletProcessor`
//   / `registerProcessor` 两个壳,然后按 128 采样块喂同一段合成信号。

const path = require('path');
const { readFileSync } = require('fs');

const cfg = JSON.parse(process.argv[2] || '{}');
const sr = cfg.sr || 48000;
const n = Math.min(48000 * 10, cfg.n || 8192);

global.sampleRate = sr;
let PROC = null;
global.registerProcessor = (name, cls) => { PROC = cls; };
global.AudioWorkletProcessor = class {
  constructor() {
    const handlers = [];
    this.port = {
      postMessage() {},
      set onmessage(fn) { this._fn = fn; },
      get onmessage() { return this._fn; },
    };
  }
};

// 用 Function 包一层,避免 CommonJS 的模块级 const 冲突
const src = readFileSync(path.join(__dirname, '..', 'web', 'studio', 'fxworklet.js'), 'utf8');
new Function('sampleRate', 'AudioWorkletProcessor', 'registerProcessor', src)(
  sr, global.AudioWorkletProcessor, global.registerProcessor);

if (!PROC) {
  console.error('fxworklet.js 没有注册 processor');
  process.exit(2);
}

// 与 Python 侧同一串伪随机数(简单的 LCG,不依赖 Math.random)
let s = (cfg.seed || 7) | 0;
const inp = new Float32Array(n);
for (let i = 0; i < n; i++) {
  s = (Math.imul(s, 1664525) + 1013904223) | 0;
  inp[i] = ((s >>> 0) / 4294967296) * 2 - 1;
  inp[i] *= 0.3;
}

const p = new PROC();
p.setChain(cfg.chain || []);
const out = new Float32Array(n);
const BLK = 128;
for (let i = 0; i < n; i += BLK) {
  const m = Math.min(BLK, n - i);
  const ib = new Float32Array(m);
  ib.set(inp.subarray(i, i + m));
  const ob = new Float32Array(m);
  p.process([[ib]], [[ob]]);
  out.set(ob, i);
}

let sum = 0, pk = 0;
for (let i = 0; i < n; i++) {
  sum += out[i] * out[i];
  const a = Math.abs(out[i]);
  if (a > pk) pk = a;
}
console.log(JSON.stringify({
  samples: Array.from(out.slice(0, 64)),
  tail: Array.from(out.slice(n - 64)),
  rms: Math.sqrt(sum / n),
  peak: pk,
}));
