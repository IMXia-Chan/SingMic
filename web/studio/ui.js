// SingMic Studio —— 界面、状态、画布、拖拽。引擎在 engine.js。
//
// ★ 铁律：一个 clip 只要有 srcIn/srcOut/start，就能靠**同一份 AudioBuffer** 摆到任何地方。
//   移动、复制、裁剪右端点 → 一次渲染请求都不发。只有真改音高/真拉伸才发。

import { Engine, projectEnd } from './engine.js';
import { PRESETS, PRESET_CATS } from './presets.js';
import { t, setLang, langId, initLang, detectLang, patchNativeDialogs, LANGS } from './i18n.js';

const $ = (id) => document.getElementById(id);
// ★ 启动计时：把「页面自己走到了哪一步」报给桌面壳（浏览器里没有 api，静默跳过）
const SM_PING = (tag) => { try {
  fetch('/api/studio/ui_ping?tag=' + encodeURIComponent(tag)
        + '&t=' + Math.round(performance.now())).catch(() => {});
} catch (e) {} };
SM_PING('ui.js 开始执行');
const engine = new Engine();

const HEAD_W = 168;
const RULER_H = 30;
const TRACK_H = 92;
const CLIP_PAD = 3;
const EDGE = 6;

const COLOR = {
  voice: '#2f6f9f', audio: '#8a5a2b', sampler: '#6b4f9e', harmony: '#9e4f6b',
  drum: '#4f7a5c',
};

// ★ 当前轨（S.selTrack）一变，VST / 效果器面板必须立刻跟着换轨。
//   以前只有「点底部标签」时才重画：先点了别的轨，面板还挂着上一条轨的槽位，
//   在上头点 × 删的是另一条轨（或者看着像「删了没反应」），得刷新才正常。
let _selTrackId = null;
function renderTrackScopedPanels() {
  if (!S.proj) return;                 // 工程还没加载，面板无从画起
  const fx = $('panelFx'), vst = $('panelVst');
  if (fx && !fx.hidden) renderFxPanel();
  if (vst && !vst.hidden) renderVstPanel();
}

const S = {
  file: '',
  proj: null,
  rev: 0,
  dirty: false,
  conflict: false,
  sel: new Set(),
  noteSel: new Set(),
  rollTrack: null,
  rollY: 0,
  selTrack: null,
  view: { t0: 0, pps: 64, y: 0 },
  peaks: new Map(),
  recs: [],
  media: [],
  blocks: null,
  blocksName: '',
  blocksMode: 'syllable',  // 当前块用的是哪种切法
  asrReady: false,
  asrStatus: null,
  drag: null,
  undo: [],
  redo: [],
  saveTimer: 0,
  playAt: 0,
  renderTok: 0,
  pitchFlags: new Map(),   // clipId -> 检测出来的块（★ 运行时，不写进工程）
  pitchInfo: null,
  pitchInfoPath: '',
  chords: null,            // 和弦带（★ 运行时，不写进工程）
  chordsPath: '',
};

// 把 selTrack 换成访问器：任何一处赋值都会把 VST/效果器面板刷新成新轨。
_selTrackId = S.selTrack;
Object.defineProperty(S, 'selTrack', {
  configurable: true,
  enumerable: true,
  get: () => _selTrackId,
  set: (v) => {
    if (v === _selTrackId) return;
    _selTrackId = v;
    renderTrackScopedPanels();
  },
});

// ---------------------------------------------------------------- 偏好（设置里改，存本机）
/* ★ 这些是「这台电脑一直记住」的选择，不进工程文件 —— 换台电脑就该是默认值。
   读不到 / 写不进去（隐私模式）就当默认值用，不许因此让界面起不来。
   ★ 存两份：localStorage 是**同步**的缓存（开局立刻有值，界面不会先闪一下默认值）,
     `~/.singmic/prefs.json`（走 /api/studio/prefs）才是持久那一份。
     为什么要多这一份：以后装壳成桌面 App、或者同时开两个浏览器，localStorage 是各算各的；
     文件只有一个，谁打开都是同一套设置。 */
const PREFS_KEY = 'singmic.prefs.v1';
const PREFS_DEF = {
  stage: 'syllable',     // 点一条新录音时的默认切法
  confirmCost: false,    // 每次试听 / 生成都再确认一次费用
  autosave: true,        // 改动后自动保存
  guide: true,           // 首次打开自动弹新手引导
  lang: '',              // 界面语言；空 = 跟随系统
};
const PREFS = (() => {
  let got = {};
  try { got = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch { got = {}; }
  return { ...PREFS_DEF, ...got };
})();
function prefsSave() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(PREFS)); } catch { /* 记不住算了 */ }
  // 文件那份失败了不许弹错、不许挡人用 —— 它只是「换壳也能记住」的备份，不是主路
  api('/prefs', { method: 'POST', json: { prefs: PREFS } }).catch(() => {});
}
/* 把一个偏好对象套到界面上。改完设置页要重画，所以抽出来给两边共用。 */
function prefsApply(p) {
  Object.assign(PREFS, p);
  if ($('blockMode')) $('blockMode').value = PREFS.stage;
  S.blocksMode = PREFS.stage;
  $('prefStage') && ($('prefStage').value = PREFS.stage);
  $('prefAutosave') && ($('prefAutosave').checked = !!PREFS.autosave);
  $('prefConfirmCost') && ($('prefConfirmCost').checked = !!PREFS.confirmCost);
  $('prefGuide') && ($('prefGuide').checked = !!PREFS.guide);
  $('prefLang') && ($('prefLang').value = PREFS.lang || '');
  if (p && 'lang' in p) {
    // 语言是异步加载字典的，不阻塞开局；失败就留在中文，不弹错
    (PREFS.lang ? setLang(PREFS.lang) : initLang(null)).catch(() => {});
  }
}
/* 开局问一次文件里那份。文件比 localStorage 新/全，以它为准（浏览器只是缓存）。 */
async function prefsLoad() {
  try {
    const r = await api('/prefs');
    const got = (r && r.prefs) || {};
    if (Object.keys(got).length) { prefsApply(got); prefsSave(); }
  } catch { /* 服务端不可达就继续用缓存那份 */ }
}
function prefsSet(k, v) {
  PREFS[k] = v;
  prefsSave();
  if (k === 'stage') {                 // 默认切法：下拉框和内部状态一起改，别只顾一头
    if ($('blockMode')) $('blockMode').value = v;
    S.blocksMode = v;                  // 只改「下一次用哪个」，已经切好的块不重切
  }
}
// 开局先把缓存那份套上，再异步问文件 —— 两步都不许抛
prefsApply({});
$('blockMode') && ($('blockMode').value = PREFS.stage);
S.blocksMode = PREFS.stage;
// 界面语言：显式选过就照选的，没选过跟随系统（zh-Hant / en / ja / ko，其余简体）
patchNativeDialogs();
initLang(PREFS.lang || null).catch(() => {});

// ---------------------------------------------------------------- 工具
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmt = (n, k = 2) => Number(n).toFixed(k);

function hash8(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(16).padStart(8, '0');
}
function uid(p) {
  return p + '_' + Date.now().toString(36).slice(-5) + Math.random().toString(36).slice(2, 6);
}
function niceName(n) {
  return String(n || '').replace(/_voice\.wav$/, ' 人声').replace(/_mix\.wav$/, ' 成品')
    .replace(/\.(wav|mp3|flac|ogg)$/i, '').replace(/^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_?/, '') || n;
}
function shortName(n) {
  const s = niceName(n);
  return s.length > 22 ? s.slice(0, 21) + '…' : s;
}
// 媒体文件在盘上是 `<sha1前8位>_<原名>`；给人看的只有原名
function mediaDisplayName(n) {
  return String(n || '').replace(/^[0-9a-f]{8}_/i, '');
}

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers = { 'Content-Type': 'application/json', ...(o.headers || {}) };
    delete o.json;
  }
  const r = await fetch('/api/studio' + path, o);
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = (body && body.detail) || body || `HTTP ${r.status}`;
    const e = new Error(msg);
    e.status = r.status;
    throw e;
  }
  return body;
}

// ---------------------------------------------------------------- 工程
/* ★ 工程文件是对外契约：以后桌面版 / 手机版 / 别人写的工具都要读它。
   `format` 认身份，`version` 认格式版本 —— 只加字段不动语义就不用升版；
   删字段、改字段含义必须 version+1，并在读的时候写迁移。 */
function blankProject(title) {
  return {
    format: 'singmic-project', version: 1, rev: 0, name: '', title: title || t('未命名'),
    bpm: 120, timeSig: [4, 4], lengthSec: 60,
    sources: {},
    // 默认轨名是产品文案（会一直显示在调音台上），跟界面语言走；建完之后就是数据，改语言不再动它
    tracks: [
      { id: uid('tr'), name: t('主唱'), kind: 'voice', color: COLOR.voice, gain: 1, pan: 0, mute: false, solo: false, clips: [] },
      { id: uid('tr'), name: t('伴奏'), kind: 'audio', color: COLOR.audio, gain: 1, pan: 0, mute: false, solo: false, clips: [] },
    ],
    ui: { snap: '1/16', loop: [0, 0], zoom: 64 },
  };
}

function ensureSource(path, name, dur, kind) {
  const sid = 'src_' + hash8(path);
  const srcs = S.proj.sources;
  if (srcs[sid]) { if (dur) srcs[sid].dur = Math.max(srcs[sid].dur || 0, dur); return sid; }
  srcs[sid] = {
    kind: kind || (path.includes('/media/') ? 'audio' : 'voice'),
    path, name: name || path.split('/').pop(), sr: 48000, ch: 1,
    dur: dur || 0, size: 0, mtime: 0,
  };
  return sid;
}

function newTrack(kind, name) {
  const tr = {
    id: uid('tr'), name: name || (kind === 'voice' ? '人声' : kind === 'audio' ? '音频' : '采样'),
    kind, color: COLOR[kind] || COLOR.audio, gain: 1, pan: 0, mute: false, solo: false, clips: [],
  };
  S.proj.tracks.push(tr);
  return tr;
}

// 手动加一条空轨（和声/吉他/鼓……随便加，名字在混音台上双击改）
function addTrack(name) {
  if (!S.proj) return null;
  takeSnap();
  let n = S.proj.tracks.length + 1;
  const used = new Set(S.proj.tracks.map(t => t.name));
  while (used.has(name || `轨道 ${n}`)) n++;
  const tr = newTrack('audio', name || `轨道 ${n}`);
  S.selTrack = tr.id;
  scrollTrackIntoView(tr);      // 新轨在下面 ⇒ 自动滚出来，不然「加了但看不见」
  afterStructure();
  return tr;
}

// 双击混音台上的轨名就地改名
function startRenameTrack(tr, el) {
  if (!tr || !el || el.querySelector('input')) return;
  const old = tr.name || '';
  const inp = document.createElement('input');
  inp.className = 'nmEdit';
  inp.value = old;
  inp.spellcheck = false;
  el.textContent = '';
  el.appendChild(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    const v = String(inp.value || '').trim().slice(0, 32);
    if (ok && v && v !== old) { takeSnap(); tr.name = v; markDirty(); }
    afterStructure();
  };
  inp.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  };
  inp.onblur = () => finish(true);
  inp.onclick = (e) => e.stopPropagation();
}

async function newProject() {
  bigClose();                            // 换工程 = 整页块编辑必须关掉（它指着旧工程的轨）
  // 默认工程名是产品文案（会在列表里一直显示），跟着界面语言走
  const stamp = new Date().toLocaleString(
    langId() === 'zh-Hans' || langId() === 'zh-Hant' ? 'zh-CN' : langId(),
    { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  const title = `${t('工程')} ${stamp}`;
  const file = 'proj_' + Date.now().toString(36);
  S.proj = blankProject(title);
  S.file = file;
  S.rev = 0;
  resetFrozenState();
  S.sel.clear();
  S.undo.length = 0; S.redo.length = 0;
  CLIPB = null;                       // 剪贴板不跨工程
  S.pitchFlags.clear(); S.pitchInfo = null;
  S.chords = null; S.chordsPath = '';
  $('pitchMsg').hidden = true;
  history.replaceState(null, '', '?p=' + encodeURIComponent(file));
  $('projName').value = title;
  syncTransport();
  drawStatic(); renderInspector(); renderMixer(); renderVstPanel();
  await saveNow(true);
}

async function loadProject(file) {
  bigClose();
  setSaveState('读取中…');
  const r = await api('/projects/' + encodeURIComponent(file));
  S.proj = r.project;
  S.file = file;
  S.rev = r.project.rev || 0;
  S.sourcesInfo = r.sources || {};
  S.proj.sources = S.proj.sources || {};
  S.proj.tracks = (S.proj.tracks || []).map(t => ({
    gain: 1, pan: 0, mute: false, solo: false, clips: [], ...t,
    clips: (t.clips || []).map(c => ({ gain: 1, semis: 0, cents: 0, ...c })),
  }));
  S.proj.ui = S.proj.ui || {};
  S.view.pps = S.proj.ui.zoom || 64;
  resetFrozenState();
  S.sel.clear();
  S.undo.length = 0; S.redo.length = 0;
  CLIPB = null;
  S.pitchFlags.clear(); S.pitchInfo = null;
  S.chords = null; S.chordsPath = '';
  $('pitchMsg').hidden = true;
  S.dirty = false; S.conflict = false;
  history.replaceState(null, '', '?p=' + encodeURIComponent(file));
  $('projName').value = S.proj.title || S.proj.name || file;
  syncTransport();
  drawStatic(); drawOverlay(); renderInspector(); renderMixer(); renderVstPanel();
  const smp = S.proj.tracks.find(t => t.kind === 'sampler');
  if (smp) { S.rollTrack = smp.id; rollScrollTo(smp.sample?.baseMidi ?? 60); }
  setSaveState('已保存');
  for (const sid of Object.keys(S.proj.sources)) ensurePeaks(sid);
  for (const tr of S.proj.tracks) for (const c of tr.clips) if (c.renderKey) ensureRenderedPeaks(c);
  // 打开工程就把开了冻结的 VST 轨算回来（命中缓存时几乎不花时间）
  for (const tr of S.proj.tracks) if (vstEnabled(tr)) scheduleFreeze(tr.id, 0);
}

function setSaveState(t, cls) {
  const el = $('saveState');
  el.textContent = t;
  el.className = cls || '';
}

function markDirty() {
  S.dirty = true;
  clearTimeout(S.saveTimer);
  if (S.conflict) { setSaveState('⚠ 已在别处修改', 'dirty'); return; }
  // 关掉自动保存：只把状态写出来，什么时候存由他自己按 Ctrl+S
  if (!PREFS.autosave) { setSaveState('未保存 · Ctrl+S', 'dirty'); return; }
  setSaveState('未保存…', 'dirty');
  S.saveTimer = setTimeout(() => saveNow().catch(() => {}), 800);
}

async function saveNow(force) {
  if (!S.proj || !S.file) return;
  if (S.conflict && !force) { setSaveState('⚠ 已在别处修改', 'dirty'); return; }
  clearTimeout(S.saveTimer);
  S.proj.ui = S.proj.ui || {};
  S.proj.ui.zoom = S.view.pps;
  S.proj.title = $('projName').value || S.proj.title;
  const body = JSON.parse(JSON.stringify(S.proj));
  if (!force) body.rev = S.rev;
  else delete body.rev;
  try {
    const r = await api('/projects/' + encodeURIComponent(S.file), { method: 'PUT', json: body });
    S.rev = r.rev;
    S.proj.rev = r.rev;
    S.dirty = false; S.conflict = false;
    setSaveState('已保存');
  } catch (e) {
    if (e.status === 409) { S.conflict = true; setSaveState('⚠ 已在别处修改', 'dirty'); }
    else { setSaveState('保存失败', 'dirty'); console.error(e); }
    throw e;
  }
}

// ---------------------------------------------------------------- 撤销
function takeSnap() {
  if (!S.proj) return;
  S.undo.push(JSON.stringify(S.proj));
  if (S.undo.length > 80) S.undo.shift();
  S.redo.length = 0;
}
function undo() {
  if (!S.undo.length) return;
  S.redo.push(JSON.stringify(S.proj));
  S.proj = JSON.parse(S.undo.pop());
  S.sel.clear();
  afterStructure();
}
function redo() {
  if (!S.redo.length) return;
  S.undo.push(JSON.stringify(S.proj));
  S.proj = JSON.parse(S.redo.pop());
  S.sel.clear();
  afterStructure();
}
function afterStructure() {
  markDirty();
  drawStatic(); drawOverlay(); renderInspector(); renderMixer(); renderFxPanel();
  // ★ 撤销/重做会把 S.proj 整个换成一个新对象 —— VST 面板如果还留着上一次
  //   渲染出来的行，那些按钮闭包指向的是**旧工程里的轨道**，点 × 等于删在空气上。
  //   每次结构变化都按现场重画，面板和工程永远同源。
  renderVstPanel();
  engine.project = S.proj;
  engine.applyMix();
  vstTouchedAll();
  checkMemory();
  // ★ 整页块编辑开着的时候，主时间轴的结构变化（拖/删/切/撤销）必须同时反映到那一页
  if (BIG.open) drawBig();
}

// ★ 解码出来的 PCM 是 float32 × 声道数；一小时的 48k 立体声就 1.3GB。
//   工程大了要提醒用户，别等浏览器崩了才发现。
let memWarned = 0;
function checkMemory() {
  if (!engine.memBytes) return;
  const b = engine.memBytes();
  const mb = b / 1048576;
  if (mb > 700 && Date.now() - memWarned > 120000) {
    memWarned = Date.now();
    toast(`⚠ 已解码音频约 ${Math.round(mb)}MB。工程较大时，` +
      '关闭页面后重新打开可释放内存（工程不会丢失）。', 12000);
  }
}

// ---------------------------------------------------------------- 几何
function barSec() {
  const [n] = S.proj.timeSig || [4, 4];
  return (60 / S.proj.bpm) * n;
}
function beatSec() { return 60 / S.proj.bpm; }
function gridSec() {
  const snap = S.proj.ui?.snap || '1/16';
  if (snap === 'off') return 0;
  if (snap === 'bar') return barSec();
  if (snap === 'beat') return beatSec();
  if (snap === '1/8') return beatSec() / 2;
  return beatSec() / 4;
}
// ---------------------------------------------------------------- 标记
// 手动落的时间点（标「开唱」「伴奏进」），存在工程的 ui.markers 里 ——
// 跟撤销、保存、刷新一起走。标记是**歌里的第几秒**，不绑在任何块上。
function markers() {
  if (!S.proj) return [];
  if (!S.proj.ui) S.proj.ui = {};
  if (!Array.isArray(S.proj.ui.markers)) S.proj.ui.markers = [];
  return S.proj.ui.markers;
}
// 标尺上按水平距离命中（5px）
function markerHit(x, y) {
  if (y >= RULER_H || x < HEAD_W) return null;
  let best = null, bd = 5.5;
  for (const m of markers()) {
    const d = Math.abs(xOf(m.t) - x);
    if (d <= bd) { bd = d; best = m; }
  }
  return best;
}
function markerAdd(t, name) {
  if (!S.proj) return null;
  takeSnap();
  const list = markers();
  const m = {
    id: uid('mk'),
    t: Math.max(0, snapT(t ?? S.playAt)),
    name: name || ('M' + (list.length + 1)),
  };
  list.push(m);
  markDirty(); drawStatic();
  return m;
}
function markerDelete(id) {
  const list = markers();
  const i = list.findIndex(m => m.id === id);
  if (i < 0) return;
  takeSnap();
  list.splice(i, 1);
  markDirty(); drawStatic();
}

function snapT(t, skipId) {
  // ★ 标记优先：6 像素以内直接落在标记上 —— 他要的「打点对齐」靠的就是这个
  const tol = 6 / S.view.pps;
  for (const m of markers()) {
    if (m.id !== skipId && Math.abs(t - m.t) <= tol) return m.t;
  }
  const g = gridSec();
  return g > 0 ? Math.round(t / g) * g : t;
}
function xOf(t) { return HEAD_W + (t - S.view.t0) * S.view.pps; }
function tOf(x) { return S.view.t0 + (x - HEAD_W) / S.view.pps; }

function trackRows() {
  const rows = [];
  let y = RULER_H - S.view.y;
  for (const tr of S.proj?.tracks || []) {
    rows.push({ tr, y, h: TRACK_H });
    y += TRACK_H;
  }
  return rows;
}
function rowAt(y) {
  for (const r of trackRows()) if (y >= r.y && y < r.y + r.h) return r;
  return null;
}

// 轨道总高超出可视区时，最多能往下滚多少像素
function maxScrollY() {
  return Math.max(0, (S.proj?.tracks.length || 0) * TRACK_H - (H - RULER_H));
}

// 让某条轨道出现在可视范围里（加轨 / 拖出新轨之后自动滚过去）
function scrollTrackIntoView(tr) {
  if (!S.proj || !tr) return;
  const i = S.proj.tracks.indexOf(tr);
  if (i < 0) return;
  const y = RULER_H - S.view.y + i * TRACK_H;   // 这一轨现在在屏幕上的 y
  if (y < RULER_H) S.view.y = Math.max(0, S.view.y - (RULER_H - y));
  else if (y + TRACK_H > H) S.view.y = clamp(S.view.y + (y + TRACK_H - H), 0, maxScrollY());
}
function clipAt(x, y) {
  const r = rowAt(y);
  if (!r || x < HEAD_W) return null;
  const t = tOf(x);
  for (const c of r.tr.clips) {
    if (t >= c.start && t <= c.start + c.length) return { tr: r.tr, clip: c, row: r };
  }
  return null;
}
// ---------------------------------------------------------------- 片段音量
// ★ 切分出来的每一段本来就是独立 clip,`gain` 一直存在；
//   这里只是把它**画出来、能直接拖**，不用去检查器里找。
function clipGainText(c) {
  const g = Number(c.gain ?? 1);
  if (Math.abs(g - 1) < 0.005) return '100%';
  const db = 20 * Math.log10(Math.max(g, 0.0001));
  return (db > 0 ? '+' : '') + db.toFixed(1) + 'dB';
}
function clipGainRect(row, c) {
  const y0 = row.y + CLIP_PAD, h = row.h - CLIP_PAD * 2;
  const xa = Math.max(HEAD_W, xOf(c.start));
  const xb = Math.min(W, xOf(c.start + c.length));
  const gw = 40;
  // 太窄就不画 —— 不挡波形，也不和左右裁剪抓手抢
  if (xb - xa < gw + 34) return null;
  return { x: xb - gw - 16, y: y0 + h - 15, w: gw, h: 12,
           text: clipGainText(c) };
}
function clipGainHit(x, y) {
  const row = rowAt(y);
  if (!row) return null;
  const t = tOf(x);
  for (const c of row.tr.clips) {
    if (t < c.start || t > c.start + c.length) continue;
    const r = clipGainRect(row, c);
    if (!r) continue;
    if (x >= r.x - 2 && x <= r.x + r.w + 2
        && y >= r.y - 2 && y <= r.y + r.h + 2) {
      return { tr: row.tr, clip: c, row, rect: r };
    }
  }
  return null;
}

// 对一组块设同一个音量（%）。`pct` 是 0–200 的百分数。
function setClipGainPct(items, pct) {
  const v = clamp(Number(pct) / 100, 0, 2);
  if (!Number.isFinite(v)) return null;
  takeSnap();
  for (const { clip } of items) clip.gain = v;
  afterStructure();
  setSaveState(`片段音量 ${clipGainText({ gain: v })}`);
  return v;
}

// 点角标 / 右键「音量…」：输入 0–200 的百分数
async function askClipGain() {
  const sels = selectedClips();
  if (!sels.length) return;
  const cur = Math.round(Number(sels[0].clip.gain ?? 1) * 100);
  const s = await uiPrompt('片段音量（0–200%）', String(cur), { title: '片段音量' });
  if (s === null) return;
  const pct = Number(String(s).replace(/[%％\s]/g, ''));
  if (!Number.isFinite(pct)) { setSaveState('音量要填 0–200 的数字'); return; }
  setClipGainPct(sels, pct);
}

function selectedClips() {
  const out = [];
  for (const tr of S.proj?.tracks || []) for (const c of tr.clips) if (S.sel.has(c.id)) out.push({ tr, clip: c });
  return out;
}
function clipById(id) {
  for (const tr of S.proj?.tracks || []) for (const c of tr.clips) if (c.id === id) return { tr, clip: c };
  return null;
}

// ---------------------------------------------------------------- 画布
const area = $('playArea');
const cvS = $('plStatic');
const cvO = $('plOverlay');
const gS = cvS.getContext('2d');
const gO = cvO.getContext('2d');
let W = 800, H = 500, DPR = 1;

function resize() {
  const r = area.getBoundingClientRect();
  W = Math.max(100, r.width); H = Math.max(100, r.height);
  DPR = Math.min(2, window.devicePixelRatio || 1);
  for (const c of [cvS, cvO]) {
    c.width = Math.round(W * DPR);
    c.height = Math.round(H * DPR);
  }
  drawStatic(); drawOverlay();
  drawRoll();
}

function roundRect(g, x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function drawStatic() {
  if (!S.proj) return;
  S.drawStaticN = (S.drawStaticN || 0) + 1;   // 冒烟测试用来确认画布真的重画过
  const g = gS;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.clearRect(0, 0, W, H);
  g.fillStyle = '#0d1017';
  g.fillRect(0, 0, W, H);

  const bar = barSec(), beat = beatSec();
  const tEnd = tOf(W);

  const rows = trackRows();

  // 1) 轨道底色
  for (let i = 0; i < rows.length; i++) {
    const { tr, y, h } = rows[i];
    if (y > H || y + h < RULER_H) continue;
    g.fillStyle = i % 2 ? '#10141c' : '#0e1219';
    g.fillRect(HEAD_W, y, W - HEAD_W, h);
    if (S.selTrack === tr.id) {
      g.fillStyle = 'rgba(63,182,255,.045)';
      g.fillRect(HEAD_W, y, W - HEAD_W, h);
    }
  }

  // 2) 循环区
  const lp = S.proj.ui?.loop;
  if (lp && lp[1] > lp[0]) {
    const a = xOf(lp[0]), b = xOf(lp[1]);
    g.fillStyle = 'rgba(63,182,255,.07)';
    g.fillRect(Math.max(HEAD_W, a), RULER_H, Math.max(0, b - Math.max(HEAD_W, a)), H);
  }

  // 3) 网格（在 clip 下面 —— 不然线压在波形上）
  const startBar = Math.max(0, Math.floor(S.view.t0 / bar));
  const endBar = Math.min(startBar + 4000, Math.ceil(tEnd / bar));
  const [nn] = S.proj.timeSig || [4, 4];
  for (let b = startBar; b <= endBar; b++) {
    const x = xOf(b * bar);
    if (x >= HEAD_W) {
      g.strokeStyle = '#1e2636';
      g.beginPath(); g.moveTo(x + .5, RULER_H); g.lineTo(x + .5, H); g.stroke();
    }
    for (let k = 1; k < nn; k++) {
      const bx = xOf(b * bar + k * beat);
      if (bx < HEAD_W) continue;
      g.strokeStyle = '#151b27';
      g.beginPath(); g.moveTo(bx + .5, RULER_H); g.lineTo(bx + .5, H); g.stroke();
    }
  }

  // 4) 轨道头 + clip
  for (const { tr, y, h } of rows) {
    if (y > H || y + h < RULER_H) continue;
    const selectedTrack = S.selTrack === tr.id;
    g.fillStyle = tr.mute ? '#14181f' : '#151a24';
    g.fillRect(0, y, HEAD_W, h);
    g.fillStyle = tr.color || COLOR.voice;
    g.fillRect(0, y, 4, h);
    g.strokeStyle = '#1b2130';
    g.beginPath(); g.moveTo(0, y + h - .5); g.lineTo(W, y + h - .5); g.stroke();

    g.fillStyle = selectedTrack ? '#e8f4ff' : '#b9c3d3';
    g.font = '600 12px "Segoe UI","Microsoft YaHei",sans-serif';
    g.fillText(tr.name || t('轨'), 12, y + 20);
    g.fillStyle = '#5b657a';
    g.font = '10px Consolas,monospace';
    // 画布上的字走不了 DOM 翻译，必须自己过 t()
    g.fillText(`${t('{0} 块').replace('{0}', tr.clips.length)}${tr.solo ? ' · SOLO' : ''}${tr.mute ? ' · ' + t('静音') : ''}`, 12, y + 36);

    // VST 标：绿=冻结已同步 / 黄=旧快照或正在算 / 红=失败 / 灰=没冻结
    const vs = vstStateOf(tr);
    if (vs) {
      const col = vs.state === 'ok' ? '#3ddc97' : vs.state === 'error' ? '#ff5a76'
        : vs.state === 'off' ? '#6d7a8f' : '#ffc857';
      g.fillStyle = col;
      g.beginPath();
      g.arc(HEAD_W - 22, y + 16, 4, 0, Math.PI * 2);
      g.fill();
      g.font = '600 10px Consolas,monospace';
      g.fillText('VST', HEAD_W - 46, y + 19);
    }

    // M / S
    const bw = 24, bh = 17;
    const sx = HEAD_W - 30, mx = HEAD_W - 58, by = y + h - 24;
    drawHeadBtn(g, mx, by, bw, bh, 'M', tr.mute, '#ff5a76');
    drawHeadBtn(g, sx, by, bw, bh, 'S', tr.solo, '#ffc857');
    // 增益
    g.fillStyle = '#6d7a8f';
    g.font = '10px Consolas,monospace';
    g.fillText(`${Math.round((tr.gain ?? 1) * 100)}%`, 12, y + h - 12);

    // clip
    for (const c of tr.clips) {
      const x0 = xOf(c.start), x1 = xOf(c.start + c.length);
      if (x1 < HEAD_W || x0 > W) continue;
      drawClip(g, tr, c, x0, Math.max(HEAD_W, x0), Math.min(W, x1), y, h);
    }
    // 采样轨的音符：也是「块」，只是小一点
    if (tr.notes?.length) {
      const lo = tr.sample?.poolLo ?? -7, hi = tr.sample?.poolHi ?? 7;
      const base = tr.sample?.baseMidi ?? 60;
      for (const nt of tr.notes) {
        const x0 = xOf(nt.start), x1 = xOf(nt.start + nt.length);
        if (x1 < HEAD_W || x0 > W) continue;
        const semi = Math.round(nt.midi - base);
        const out = semi < lo || semi > hi;
        const yy = y + h * 0.5 - (nt.midi - base) * 1.6 - 5;
        roundRect(g, Math.max(HEAD_W, x0), clamp(yy, y + 2, y + h - 12),
          Math.max(2, x1 - Math.max(HEAD_W, x0)), 10, 2);
        g.fillStyle = out ? '#57302f' : (tr.color || COLOR.sampler);
        g.fill();
        g.strokeStyle = 'rgba(0,0,0,.4)';
        g.stroke();
      }
    }
  }

  // 标尺
  g.fillStyle = '#11151d';
  g.fillRect(0, 0, W, RULER_H);
  g.strokeStyle = '#242a38';
  g.beginPath(); g.moveTo(0, RULER_H - .5); g.lineTo(W, RULER_H - .5); g.stroke();
  g.fillStyle = '#0d1017';
  g.fillRect(0, 0, HEAD_W, RULER_H);
  g.fillStyle = '#59647a';
  g.font = '10px Consolas,monospace';
  const step = bar * S.view.pps > 56 ? 1 : Math.ceil(56 / (bar * S.view.pps));
  const firstLabel = Math.floor(S.view.t0 / bar / step) * step;
  for (let b = Math.max(0, firstLabel); b * bar <= tEnd; b += step) {
    const x = xOf(b * bar);
    if (x < HEAD_W - 2) continue;
    g.strokeStyle = '#2b3345';
    g.beginPath(); g.moveTo(x + .5, 16); g.lineTo(x + .5, RULER_H); g.stroke();
    g.fillStyle = '#7f8ba1';
    g.fillText(String(b + 1), x + 3, 13);
  }
  // 秒刻度
  g.fillStyle = '#3d4658';
  for (let s = Math.ceil(S.view.t0); s * S.view.pps < W; s += 1) {
    const x = xOf(s);
    if (x < HEAD_W) continue;
    g.fillRect(x, RULER_H - 5, 1, 5);
  }

  // 标记（手动打点）：标尺上一面小旗 + 轨道区一条虚线。
  // ★ 颜色避开播放头（橙）和循环区（蓝），压在最上面 —— 波形再密也看得见。
  const mks = markers();
  if (mks.length) {
    g.save();
    g.font = '9px Consolas,monospace';
    for (const m of mks) {
      const x = xOf(m.t);
      if (x < HEAD_W - 1 || x > W) continue;
      g.strokeStyle = 'rgba(79,209,197,.5)';
      g.lineWidth = 1;
      g.setLineDash([3, 4]);
      g.beginPath(); g.moveTo(x + .5, RULER_H); g.lineTo(x + .5, H); g.stroke();
      g.setLineDash([]);
      // 小旗（只占标尺下半截，不压小节号）
      g.fillStyle = '#4fd1c5';
      g.beginPath();
      g.moveTo(x, RULER_H - 2);
      g.lineTo(x, RULER_H - 14);
      g.lineTo(x + 7, RULER_H - 8);
      g.closePath();
      g.fill();
      // 名字：先铺一块底，免得和秒刻度叠在一起看不清
      const label = String(m.name || '');
      if (label) {
        const w = g.measureText(label).width + 4;
        g.fillStyle = '#0b0f15';
        g.fillRect(x + 3, RULER_H - 12, w, 10);
        g.fillStyle = '#7ee6da';
        g.fillText(label, x + 5, RULER_H - 4);
      }
    }
    g.restore();
  }

  // 和弦带：画在标尺下半部 —— ★ 只作建议，不是乐理真理
  if (S.chords && S.chords.length) {
    for (const c of S.chords) {
      const x0 = Math.max(HEAD_W, xOf(c.t0)), x1 = Math.min(W, xOf(c.t1));
      if (x1 <= x0) continue;
      g.fillStyle = c.quality === 'min' ? 'rgba(120,92,200,.26)' : 'rgba(64,150,118,.26)';
      g.fillRect(x0, 17, x1 - x0, RULER_H - 17);
      g.strokeStyle = 'rgba(180,210,255,.10)';
      g.strokeRect(x0 + .5, 17.5, x1 - x0 - 1, RULER_H - 18);
      if (x1 - x0 > 16) {
        g.fillStyle = '#cfe8dd';
        g.font = '600 9.5px Consolas,monospace';
        g.fillText(c.name, x0 + 3, RULER_H - 4);
      }
    }
  }

  // 轨道滚动条（只在轨道超出可视高度时出现）
  const contentH = S.proj.tracks.length * TRACK_H;
  const viewH = H - RULER_H;
  if (contentH > viewH + 1) {
    const bx = W - 7, by = RULER_H + 2, bh = viewH - 4;
    g.fillStyle = 'rgba(255,255,255,.045)';
    g.fillRect(bx, by, 5, bh);
    const thumbH = Math.max(26, bh * viewH / contentH);
    const maxY = contentH - viewH;
    const ty = by + (bh - thumbH) * (maxY > 0 ? clamp(S.view.y, 0, maxY) / maxY : 0);
    g.fillStyle = 'rgba(120,158,200,.55)';
    g.fillRect(bx, ty, 5, thumbH);
  }

  // 左下角信息
  g.fillStyle = '#3f4757';
  g.font = '10px Consolas,monospace';
  g.fillText(`${S.proj.bpm} BPM · ${S.proj.timeSig.join('/')} · ${S.view.pps | 0}px/s`, 8, H - 6);
}

function drawHeadBtn(g, x, y, w, h, label, on, color) {
  roundRect(g, x, y, w, h, 3);
  g.fillStyle = on ? color : '#1b2130';
  g.fill();
  g.strokeStyle = on ? color : '#2b3345';
  g.stroke();
  g.fillStyle = on ? '#101318' : '#7b8598';
  g.font = '700 10px "Segoe UI",sans-serif';
  g.textAlign = 'center';
  g.fillText(label, x + w / 2, y + h / 2 + 3.5);
  g.textAlign = 'left';
}

function drawClip(g, tr, c, xFull, xa, xb, rowY, rowH) {
  const y = rowY + CLIP_PAD, h = rowH - CLIP_PAD * 2;
  const w = xb - xa;
  if (w < 1) return;
  const sel = S.sel.has(c.id);
  const missing = !S.proj.sources?.[c.sourceId];
  const col = missing ? '#5a2b2b' : (tr.color || COLOR.voice);

  g.save();
  roundRect(g, xa, y, w, h, 4);
  g.clip();

  const grad = g.createLinearGradient(0, y, 0, y + h);
  grad.addColorStop(0, shade(col, 1.22));
  grad.addColorStop(.16, shade(col, .95));
  grad.addColorStop(1, shade(col, .62));
  g.fillStyle = grad;
  g.fillRect(xa, y, w, h);

  // 波形（渲染过的块画的是渲染后那条）
  const pk = S.peaks.get(c.renderKey ? 'rk:' + c.renderKey : c.sourceId);
  if (pk && !missing) {
    g.globalAlpha = .55;
    g.strokeStyle = '#e9f6ff';
    g.lineWidth = 1;
    g.beginPath();
    const mid = y + h * .56, amp = h * .34;
    const base = c.renderKey ? 0 : (c.srcIn || 0);
    for (let px = Math.floor(xa); px < xa + w; px++) {
      const tt = base + (px - xOf(c.start)) / S.view.pps;
      const i = Math.floor(tt * pk.perSec);
      if (i < 0 || i >= pk.min.length) continue;
      const v0 = pk.min[i], v1 = pk.max[i];
      g.moveTo(px + .5, mid - v1 * amp);
      g.lineTo(px + .5, mid - v0 * amp);
    }
    g.stroke();
    g.globalAlpha = 1;
  }

  // 音高曲线（有才画）
  if (Array.isArray(c.pitchCurve) && c.pitchCurve.length > 1) {
    g.strokeStyle = 'rgba(255,196,110,.8)';
    g.lineWidth = 1.2;
    g.beginPath();
    c.pitchCurve.forEach(([t, cc], i) => {
      const xx = xOf(c.start + Math.min(t, c.length));
      const yy = y + h * .52 - clamp(cc / 200, -1, 1) * h * .3;
      if (i) g.lineTo(xx, yy); else g.moveTo(xx, yy);
    });
    g.stroke();
  }

  // 标题
  g.fillStyle = 'rgba(4,10,18,.55)';
  g.fillRect(xa, y, w, 15);
  g.fillStyle = '#eaf4ff';
  g.font = '600 10.5px "Segoe UI","Microsoft YaHei",sans-serif';
  // ★ 块轨上十几个块是**同一个源切出来的**，都写源文件名（「人声」「人声」…）等于没写。
  //   这种块自己带着音名 / 气声标记，标题就写它；普通片段仍写源文件名。
  const label = (tr.bigFor && (c.note || c.kind === 'sound'))
    ? (c.kind === 'sound' ? t('气声') : String(c.note))
    : shortName(S.proj.sources?.[c.sourceId]?.name || '?');
  g.fillText(label, xa + 5, y + 11);

  // 角标
  if (c.semis) {
    const s = (c.semis > 0 ? '+' : '') + c.semis;
    g.fillStyle = '#0b1a26';
    g.font = '700 10px Consolas,monospace';
    const tw = g.measureText(s).width + 7;
    roundRect(g, xa + w - tw - 4, y + 2, tw, 12, 3);
    g.fillStyle = '#7fe0ff'; g.fill();
    g.fillStyle = '#04121c';
    g.fillText(s, xa + w - tw, y + 11.5);
  }
  if (c.needsRender) {
    g.fillStyle = 'rgba(255,200,87,.16)';
    g.fillRect(xa, y, w, h);
    g.fillStyle = '#ffc857';
    g.font = '10px "Microsoft YaHei",sans-serif';
    g.fillText(t('渲染中…'), xa + 5, y + h - 6);
  }
  if (c.deleted) {
    g.fillStyle = 'rgba(0,0,0,.55)';
    g.fillRect(xa, y, w, h);
  }
  g.restore();

  // 边框
  roundRect(g, xa + .5, y + .5, w - 1, h - 1, 4);
  g.strokeStyle = sel ? '#7fe0ff' : 'rgba(0,0,0,.5)';
  g.lineWidth = sel ? 2 : 1;
  g.stroke();
  g.lineWidth = 1;

  // ★ 片段音量角标：右下角，让切出来的每一段都看得见自己的音量。
  //   位置和命中判定共用 clipGainRect()，两边永远对得上。
  {
    const gr = clipGainRect({ y: rowY, h: rowH }, c);
    if (gr) {
      const gv = Number(c.gain ?? 1);
      const changed = Math.abs(gv - 1) >= 0.005;
      g.font = '700 9px Consolas,monospace';
      const tw = Math.min(gr.w, g.measureText(gr.text).width + 8);
      const bx = gr.x + gr.w - tw;
      roundRect(g, bx, gr.y, tw, gr.h, 3);
      g.fillStyle = changed ? 'rgba(255,200,87,.92)' : 'rgba(4,10,18,.5)';
      g.fill();
      g.fillStyle = changed ? '#241a00' : '#b8cee0';
      g.textAlign = 'center';
      g.fillText(gr.text, bx + tw / 2, gr.y + gr.h - 3);
      g.textAlign = 'left';
    }
  }

  // 跑调标记：红 = 该修；黄 = 偏太多，不硬拽。⭐ 已经动过音高的块不再标
  const pf = S.pitchFlags.get(c.id);
  if (pf && (pf.status === 'red' || pf.status === 'yellow')) {
    const dot = pf.status === 'red' ? '#ff4d6d' : '#ffc857';
    g.fillStyle = dot;
    g.beginPath();
    g.arc(xa + w - 8, y + h - 8, 3.5, 0, Math.PI * 2);
    g.fill();
    if (pf.status === 'red') {
      g.strokeStyle = dot;
      g.lineWidth = 2;
      roundRect(g, xa + 1, y + 1, w - 2, h - 2, 4);
      g.stroke();
      g.lineWidth = 1;
    }
  }

  // 选中时的左右抓手
  if (sel) {
    g.fillStyle = '#7fe0ff';
    g.fillRect(xa + 1, y + h / 2 - 8, 2, 16);
    g.fillRect(xa + w - 3, y + h / 2 - 8, 2, 16);
  }
}

function shade(hex, k) {
  const n = parseInt(hex.slice(1), 16);
  const r = clamp(Math.round(((n >> 16) & 255) * k), 0, 255);
  const gg = clamp(Math.round(((n >> 8) & 255) * k), 0, 255);
  const b = clamp(Math.round((n & 255) * k), 0, 255);
  return `rgb(${r},${gg},${b})`;
}

// ---- 覆盖层（播放头 / 拖拽幽灵 / 框选） ----
const ov = { dragGhost: null, marquee: null };

function drawOverlay() {
  const g = gO;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.clearRect(0, 0, W, H);
  if (!S.proj) return;

  // 播放头
  const t = S.playAt;
  const x = xOf(t);
  if (x >= HEAD_W - 1 && x <= W) {
    g.fillStyle = '#ff8a3d';
    g.fillRect(x, 0, 1, H);
    g.beginPath();
    g.moveTo(x - 5, 0); g.lineTo(x + 5, 0); g.lineTo(x, 8);
    g.closePath(); g.fill();
  }

  if (ov.marquee) {
    const { x0, y0, x1, y1 } = ov.marquee;
    g.fillStyle = 'rgba(63,182,255,.12)';
    g.fillRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
    g.strokeStyle = '#3fb6ff';
    g.strokeRect(Math.min(x0, x1) + .5, Math.min(y0, y1) + .5, Math.abs(x1 - x0), Math.abs(y1 - y0));
  }

  if (ov.dragGhost) {
    const { xa, xb, y, h } = ov.dragGhost;
    g.fillStyle = 'rgba(127,224,255,.18)';
    g.fillRect(xa, y, xb - xa, h);
    g.strokeStyle = '#7fe0ff';
    g.strokeRect(xa + .5, y + .5, xb - xa, h);
  }
}

// ---- 播放头动画 ----
let raf = 0;
function tickFrame() {
  // ★ 这里以前是 `raf = 0; return;` —— 一旦启动那一刻工程还是空的（页面刚打开、工程还在读）,
  //   逐帧循环就此断掉，而且没有人再拉它：画布冻在最后一帧、时钟不动、控制台一个错都不报，
  //   只有点一下播放按钮才会重新起来。现在改成工程没到位时用 200ms 的空转等着，
  //   工程一落地下一跳就恢复正常逐帧绘制。别改回直接 return。
  if (!S.proj) { raf = 0; setTimeout(startFrames, 200); return; }
  if (engine.playing) {
    S.playAt = engine.songNow();
    const x = xOf(S.playAt);
    if (x > W - 40 || x < HEAD_W) followPlayhead();
  }
  drawOverlay();
  updateClock();
  if (BIG.open) drawBig();
  if (!$('panelRoll').hidden) drawRoll();
  raf = requestAnimationFrame(tickFrame);
}
function startFrames() { if (!raf) raf = requestAnimationFrame(tickFrame); }
function followPlayhead() {
  const span = (W - HEAD_W) / S.view.pps;
  S.view.t0 = Math.max(0, S.playAt - span * 0.1);
  drawStatic(); renderInspector();
}

function updateClock() {
  if (!S.proj) return;
  const bar = barSec(), beat = beatSec();
  const t = Math.max(0, S.playAt);
  const b = Math.floor(t / bar);
  const rem = t - b * bar;
  const bt = Math.floor(rem / beat);
  const tick = Math.floor((rem - bt * beat) / beat * 96);
  $('clock').textContent =
    `${String(b + 1).padStart(3, '0')}:${String(bt + 1).padStart(2, '0')}:${String(tick).padStart(3, '0')}`;
}

// ---------------------------------------------------------------- 波形峰值
// 缓存键：普通块 = sourceId;**渲染过的块 = "rk:<renderKey>"**（它和源不是一条波形）
async function ensurePeaksAny(key, loader) {
  if (S.peaks.has(key)) return;
  S._peakBusy = S._peakBusy || new Set();
  if (S._peakBusy.has(key)) return;
  S._peakBusy.add(key);
  try {
    const buf = await loader();
    const perSec = 400;
    const n = Math.max(1, Math.ceil(buf.duration * perSec));
    const mn = new Float32Array(n), mx = new Float32Array(n);
    const ch = buf.getChannelData(0);
    const per = ch.length / n;
    for (let i = 0; i < n; i++) {
      let lo = 0, hi = 0;
      const a = Math.floor(i * per), b = Math.min(ch.length, Math.floor((i + 1) * per));
      for (let j = a; j < b; j += 3) {
        const v = ch[j];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      mn[i] = lo; mx[i] = hi;
    }
    S.peaks.set(key, { perSec, min: mn, max: mx });
    drawStatic();
  } catch (e) {
    console.warn('peaks failed', key, e);
  } finally {
    S._peakBusy.delete(key);
  }
}

function ensurePeaks(sid) {
  const src = S.proj?.sources?.[sid];
  if (!src) return;
  return ensurePeaksAny(sid, () => engine.buffer(sid, engine.sourceUrl(src)));
}

function ensureRenderedPeaks(c) {
  if (!c.renderKey || !c.renderUrl) return;
  return ensurePeaksAny('rk:' + c.renderKey,
    () => engine.renderedBuffer(c.renderKey, c.renderUrl));
}

// ---------------------------------------------------------------- 轨道 / clip 操作
function addClip(payload, t, row) {
  takeSnap();
  let tr = row?.tr;
  const kind = payload.kind === 'media' ? 'audio'
    : (/_mix\.wav$/i.test(payload.name || '') ? 'audio' : 'voice');
  if (!tr) {
    tr = newTrack(kind);
    S.selTrack = tr.id;
    scrollTrackIntoView(tr);
  }
  const path = payload.path;
  const sid = ensureSource(path, payload.name, payload.dur, kind);
  let srcIn = payload.srcIn ?? 0;
  let srcOut = payload.srcOut ?? payload.dur ?? 0;
  if (!srcOut || srcOut <= srcIn) srcOut = srcIn + 1;
  const clip = {
    id: uid('cl'), sourceId: sid, start: Math.max(0, t), srcIn, srcOut,
    length: srcOut - srcIn, semis: 0, cents: 0, gain: 1,
    fadeIn: 0.008, fadeOut: 0.02, kind: kind === 'audio' ? 'sound' : 'note',
  };
  if (payload.blockId !== undefined) clip.blockId = payload.blockId;
  if (payload.note) clip.note = payload.note;
  if (payload.f0 > 0) clip.f0 = payload.f0;
  tr.clips.push(clip);
  tr.clips.sort((a, b) => a.start - b.start);
  S.sel.clear(); S.sel.add(clip.id);
  afterStructure();
  ensurePeaks(sid);
  // 拖进来一段伴奏 ⇒ 顺手测一次速（只填框，不自动改网格）
  if (payload.kind === 'media') setTimeout(() => detectBpm(path, true), 60);
  return clip;
}

// 把选中的块整体前后挪 dt 秒（只改 start，一次渲染都不发）
function nudgeSelected(dt) {
  const sel = selectedClips();
  if (!sel.length || !dt) return false;
  takeSnap();
  for (const { clip } of sel) clip.start = Math.max(0, +(clip.start + dt).toFixed(6));
  for (const tr of S.proj.tracks) tr.clips.sort((a, b) => a.start - b.start);
  afterStructure();
  const ms = Math.round(dt * 1000 * 100) / 100;
  $('hint').textContent = `${t('选中 {0} 块').replace('{0}', sel.length)} · ${t('挪')} ${ms > 0 ? '+' : ''}${ms} ms`
    + ` · 起点 ${sel[0].clip.start.toFixed(3)}s`;
  return true;
}

function deleteSelected() {
  const sel = selectedClips();
  const rt = rollTrack();
  const notes = rt ? (rt.notes || []).filter(n => S.noteSel.has(n.id)) : [];
  if (!sel.length && !notes.length) return;
  takeSnap();
  const touched = new Set();
  for (const { tr, clip } of sel) {
    const i = tr.clips.indexOf(clip);
    if (i >= 0) tr.clips.splice(i, 1);
    touched.add(tr);
  }
  if (notes.length) {
    rt.notes = rt.notes.filter(n => !S.noteSel.has(n.id));
    S.noteSel.clear();
  }
  S.sel.clear();
  // ★ 删掉一块，旁边那块原先的「真接缝」可能就变成硬边了 ⇒ 重新判一次
  for (const tr of touched) seamFades(tr);
  afterStructure();
}

function duplicateSelected() {
  const sel = selectedClips();
  if (!sel.length) return;
  takeSnap();
  const news = [];
  const touched = new Set();
  for (const { tr, clip } of sel) {
    const n = JSON.parse(JSON.stringify(clip));
    n.id = uid('cl');
    n.start = clip.start + Math.max(0.05, gridSec() || 0.25);
    tr.clips.push(n);
    news.push(n.id);
    touched.add(tr);
  }
  tr_sort();
  // ★ 复制出来的那一份多半是硬边（头尾要不要淡变，由接缝关系决定）
  for (const tr of touched) seamFades(tr);
  S.sel = new Set(news);
  afterStructure();
}
function tr_sort() {
  for (const tr of S.proj.tracks) tr.clips.sort((a, b) => a.start - b.start);
}

// ---------------------------------------------------------------- 剪贴板
// ★ 剪贴板里存的是**块的定义**（起点/源入源出/音高），不是音频。
//   粘贴 = 同一份 AudioBuffer 再排一次 ⇒ 一次渲染都不发。
let CLIPB = null;

function clipPayload(clip) {
  const n = JSON.parse(JSON.stringify(clip));
  delete n._tok;
  delete n.needsRender;
  return n;
}

function copySelected() {
  const sel = selectedClips();
  if (!sel.length) return 0;
  CLIPB = {
    file: S.file,
    t0: Math.min(...sel.map(o => o.clip.start)),
    items: sel.map(({ tr, clip }) => ({ trackId: tr.id, kind: tr.kind,
      name: tr.name, clip: clipPayload(clip) })),
  };
  setSaveState(t('复制了 {0} 块').replace('{0}', sel.length));
  setTimeout(() => setSaveState(S.dirty ? '未保存…' : '已保存'), 1200);
  return sel.length;
}

function cutSelected() {
  if (!copySelected()) return;
  deleteSelected();
}

function pasteClips() {
  if (!CLIPB) return 0;
  if (CLIPB.file !== S.file) { CLIPB = null; setSaveState('剪贴板内容来自其他工程'); return 0; }
  takeSnap();
  // 粘在播放头；播放头在 0 就贴着原位置往右一格
  const base = S.playAt > 0.001 ? S.playAt : CLIPB.t0 + (gridSec() || 0.25);
  const news = [];
  const touched = new Set();
  for (const it of CLIPB.items) {
    let tr = S.proj.tracks.find(t => t.id === it.trackId);
    if (!tr) {
      tr = newTrack(it.kind, it.name);
      S.selTrack = tr.id;
    }
    const c = JSON.parse(JSON.stringify(it.clip));
    c.id = uid('cl');
    c.start = Math.max(0, base + (c.start - CLIPB.t0));
    if (c.sourceId && !S.proj.sources[c.sourceId]) continue;   // 源没了就别粘
    tr.clips.push(c);
    news.push(c.id);
    touched.add(tr);
    ensurePeaks(c.sourceId);
    if (c.renderKey) ensureRenderedPeaks(c);
  }
  tr_sort();
  for (const tr of touched) seamFades(tr);
  S.sel = new Set(news);
  afterStructure();
  return news.length;
}

// 把「播放头此刻所指的那一处内容」平移到某个标记上。
// ★ 只改 start，长度与 srcIn/srcOut 一个都不动 ⇒ 一次渲染都不发。
// ★ 选中的块全部一起平移（人声 + 和声不会散架）。
function alignSelectedToMarker(markerId) {
  const m = markers().find(x => x.id === markerId);
  if (!m) return 0;
  const sels = selectedClips();
  if (!sels.length) return 0;
  let d = m.t - S.playAt;
  for (const { clip } of sels) d = Math.max(d, -clip.start);   // 不许推到 0 左边
  if (Math.abs(d) < 1e-9) return sels.length;
  takeSnap();
  for (const { clip } of sels) clip.start = Math.max(0, clip.start + d);
  seamFadesAll(new Set(sels.map(x => x.tr)));   // 整体平移也会改变接缝关系
  afterStructure();
  setSaveState(`对到「${m.name}」：挪了 ${d >= 0 ? '+' : ''}${d.toFixed(3)} 秒`);
  return sels.length;
}

// ---------------------------------------------------------------- 接缝（防咔哒）
// ★ 这是「切成一块一块」能不能听的根本，不是可有可无的修饰。
//
// 两个块挨在一起只有两种情况：
//   ① **真接缝**：同一次切分出来的两半 —— 时间上首尾相接，**源波形也接得上**。
//      那里本来就是连续的一串样本，任何淡变都是多余的凹坑。
//      以前是整块深拷贝 ⇒ 左半在切点留着 20ms 淡出、右半留着 8ms 淡入，
//      接缝凭空塌下去 28ms —— 这就是「抽一下」的来源，不是咔哒。
//   ② **断开的边**：挪过、裁过、粘贴出来的块 —— 两边是**不同的波形**,
//      落差就是一声「咔」，必须留 2~3ms 淡变把台阶磨圆。
//
// ★ 一个例外：变过调 / 画过曲线的块是**重新渲染**出来的，
//   两次渲染的相位对不上 —— 哪怕源上连续，也仍然要留淡变。
//
// ★ 只**抬高**他自己设的淡变；只有真接缝才会清零（那里为零才是对的）。
const SEAM_MIN = 0.0025;   // 2.5ms：听不出是个凹坑，足够压住咔哒
const SEAM_EQ = 1e-4;      // 判定「接得上」的容差（秒）

function seamFades(tr) {
  const clips = tr?.clips || [];
  let fixed = 0;
  for (const c of clips) {
    let headSeam = false, tailSeam = false;
    for (const o of clips) {
      if (o === c || o.sourceId !== c.sourceId) continue;
      if (Math.abs((o.start + o.length) - c.start) < SEAM_EQ
          && Math.abs(o.srcOut - c.srcIn) < SEAM_EQ) headSeam = true;
      if (Math.abs((c.start + c.length) - o.start) < SEAM_EQ
          && Math.abs(c.srcOut - o.srcIn) < SEAM_EQ) tailSeam = true;
    }
    const rendered = (typeof clipHasPitch === 'function') && clipHasPitch(c);
    const wantIn = (headSeam && !rendered) ? 0
      : Math.max(Number(c.fadeIn) || 0, SEAM_MIN);
    const wantOut = (tailSeam && !rendered) ? 0
      : Math.max(Number(c.fadeOut) || 0, SEAM_MIN);
    if (c.fadeIn !== wantIn || c.fadeOut !== wantOut) {
      c.fadeIn = wantIn; c.fadeOut = wantOut; fixed++;
    }
  }
  return fixed;
}
function seamFadesAll(tracks) {
  let n = 0;
  for (const tr of (tracks || S.proj?.tracks || [])) n += seamFades(tr);
  return n;
}

// ---------------------------------------------------------------- 切分 / 对拍
function splitSelected() {
  const sels = selectedClips();
  const t = S.playAt;
  if (!sels.length || !(t > 0.001)) return 0;
  takeSnap();
  let n = 0;
  const keep = [];
  const touched = new Set();
  for (const { tr, clip } of sels) {
    const a = clip.start, b = clip.start + clip.length;
    if (t <= a + 0.03 || t >= b - 0.03) { keep.push(clip.id); continue; }
    const cut = t - a;
    // 源里切在哪：拉伸过的块要按比例换算
    const ratio = clip.length > 1e-6 ? (clip.srcOut - clip.srcIn) / clip.length : 1;
    const srcCut = clip.srcIn + cut * ratio;
    const oldSrcIn = clip.srcIn, oldSrcOut = clip.srcOut;
    const oldCurve = curveSnapshot(clip);
    const right = JSON.parse(JSON.stringify(clip));
    right.id = uid('cl');
    right.start = t;
    right.length = clip.length - cut;
    right.srcIn = srcCut;
    clip.length = cut;
    clip.srcOut = srcCut;
    // ★ 曲线按**源时间**切开：左段从 0 起、末点落在切点；右段重标到自己的 srcIn
    if (oldCurve) {
      const leftSpan = srcCut - oldSrcIn;
      const rightSpan = oldSrcOut - srcCut;
      const lc = curveRange(oldCurve, 0, leftSpan);
      const rc = curveRange(oldCurve, leftSpan, leftSpan + rightSpan);
      if (lc) clip.pitchCurve = lc; else delete clip.pitchCurve;
      if (rc) right.pitchCurve = rc; else delete right.pitchCurve;
      clip.semis = 0; clip.cents = 0;
      right.semis = 0; right.cents = 0;
    }
    // ★ 变过调的块，两半都得重新渲染（旧 key 是整段的）；没变调的块一次都不发
    if (clip.renderKey || right.renderKey || oldCurve) {
      clip.renderKey = null; clip.renderUrl = null;
      right.renderKey = null; right.renderUrl = null;
    }
    clip.needsRender = clipHasPitch(clip);
    right.needsRender = clipHasPitch(right);
    tr.clips.push(right);
    keep.push(clip.id, right.id);
    touched.add(tr);
    n++;
  }
  tr_sort();
  S.sel = new Set(keep);
  // ★ 两半在源上是同一串样本 ⇒ 接缝处把淡变清零，不然会塌出一个凹坑
  for (const tr of touched) seamFades(tr);
  afterStructure();
  if (n) renderPending();
  return n;
}

// 对拍量化：★ 只挪 start，长度一个采样都不动 ⇒ 零渲染。
// 强度 <1 只挪一部分；人声轨默认提前 20ms（贴着拍子听起来才对）
function quantizeSelected(strength = 1) {
  const g = gridSec();
  if (!g) return 0;
  const sels = selectedClips();
  if (!sels.length) return 0;
  takeSnap();
  let n = 0;
  for (const { tr, clip } of sels) {
    const lead = tr.kind === 'voice' ? 0.020 : 0;
    const target = Math.max(0, Math.round(clip.start / g) * g - lead);
    const s = Math.max(0, clip.start + (target - clip.start) * strength);
    if (Math.abs(s - clip.start) > 1e-4) { clip.start = s; n++; }
  }
  tr_sort();
  seamFadesAll(new Set(sels.map(s => s.tr)));   // 量化=挪边 ⇒ 接缝要重判
  afterStructure();
  return n;
}

// ---------------------------------------------------------------- 指针交互
let ptr = null;
let gainWheelSnap = 0;   // 角标滚轮：连续滚动只留一次撤销快照

area.addEventListener('pointerdown', (e) => {
  if (!S.proj) return;
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  area.setPointerCapture(e.pointerId);
  ptr = { x0: x, y0: y, x, y, shift: e.shiftKey, alt: e.altKey, mode: '' };

  if (y < RULER_H) {
    // ★ 标记优先于「点标尺挪播放头」—— 不然永远拖不动它
    const mk = markerHit(x, y);
    if (mk) {
      takeSnap();
      ptr.mode = 'marker';
      ptr.marker = mk;
      return;
    }
    if (e.shiftKey) { ptr.mode = 'loop'; ptr.loopA = snapT(tOf(x)); }
    else { ptr.mode = 'seek'; seekTo(snapT(Math.max(0, tOf(x)))); }
    drawStatic(); return;
  }
  ptr.mode = 'idle';
  if (x < HEAD_W) {
    const row = rowAt(y);
    if (!row) return;
    S.selTrack = row.tr.id;
    if (row.tr.kind === 'sampler' || row.tr.kind === 'drum') {
      if (S.rollTrack !== row.tr.id) { S.rollTrack = row.tr.id; rollScrollTo(row.tr.sample?.baseMidi ?? 60); }
      drawRoll();
    }
    const bx = x;
    if (bx >= HEAD_W - 58 && bx <= HEAD_W - 34 && y >= row.y + row.h - 24 && y <= row.y + row.h - 7) {
      takeSnap(); row.tr.mute = !row.tr.mute; afterStructure(); return;
    }
    if (bx >= HEAD_W - 30 && bx <= HEAD_W - 6 && y >= row.y + row.h - 24 && y <= row.y + row.h - 7) {
      takeSnap(); row.tr.solo = !row.tr.solo; afterStructure(); return;
    }
    S.sel.clear();
    drawStatic(); renderInspector(); return;
  }

  // ★ 片段音量角标优先：命中它就先管音量，不进块的移动/裁剪
  const gh = clipGainHit(x, y);
  if (gh) {
    S.selTrack = gh.tr.id;
    if (e.shiftKey) S.sel.add(gh.clip.id);
    else if (!S.sel.has(gh.clip.id)) { S.sel.clear(); S.sel.add(gh.clip.id); }
    ptr.mode = 'clipgain';
    ptr.snapTaken = false;
    // 一次拖全部选中块（和检查器一致）；只选了一块时就是这一块
    ptr.gainClips = selectedClips().map(({ clip }) => ({
      clip, gain: Number(clip.gain ?? 1),
    }));
    drawStatic(); renderInspector();
    return;
  }

  const hit = clipAt(x, y);
  if (hit) {
    S.selTrack = hit.tr.id;
    if (!e.shiftKey && !S.sel.has(hit.clip.id)) { S.sel.clear(); S.sel.add(hit.clip.id); }
    else if (e.shiftKey) {
      if (S.sel.has(hit.clip.id)) S.sel.delete(hit.clip.id); else S.sel.add(hit.clip.id);
    }
    const x0 = xOf(hit.clip.start), x1 = xOf(hit.clip.start + hit.clip.length);
    let mode = 'move';
    if (x - x0 < EDGE) mode = 'trimL';
    else if (x1 - x < EDGE) mode = 'trimR';
    if (e.altKey && mode === 'move') mode = 'stretchR';

    const orig = selectedClips().map(({ tr, clip }) => ({
      tr, clip, start: clip.start, srcIn: clip.srcIn, srcOut: clip.srcOut,
      length: clip.length, renderKey: clip.renderKey || null,
      curve: curveSnapshot(clip),
    }));
    // 把被点中的那个放在第一个
    const idx = orig.findIndex(o => o.clip === hit.clip);
    if (idx > 0) orig.unshift(orig.splice(idx, 1)[0]);
    ptr.mode = mode;
    ptr.orig = orig;
    ptr.rowH = hit.row.h;
    ptr.copied = e.ctrlKey || e.metaKey;
    if (ptr.copied) {
      // Ctrl+拖 = 复制一份再拖
      takeSnap();
      const made = [];
      for (const o of orig) {
        const n = JSON.parse(JSON.stringify(o.clip));
        n.id = uid('cl');
        o.tr.clips.push(n);
        made.push(n);
      }
      S.sel = new Set(made.map(c => c.id));
      ptr.orig = selectedClips().map(({ tr, clip }) => ({
        tr, clip, start: clip.start, srcIn: clip.srcIn, srcOut: clip.srcOut,
        length: clip.length, renderKey: clip.renderKey || null,
      }));
    } else {
      takeSnap();
    }
    drawStatic(); renderInspector();
    return;
  }

  // 空白：框选
  S.sel.clear();
  ptr.mode = 'marquee';
  ov.marquee = { x0: x, y0: y, x1: x, y1: y };
  drawStatic(); drawOverlay(); renderInspector();
});

area.addEventListener('pointermove', (e) => {
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  if (!ptr) { area.style.cursor = cursorFor(x, y); return; }
  ptr.x = x; ptr.y = y;

  if (ptr.mode === 'marker') {
    // ★ 排除自己：拖动中的标记不能被自己吸住（否则永远停在原地）
    ptr.marker.t = Math.max(0, snapT(tOf(x), ptr.marker.id));
    drawStatic();
    return;
  }
  if (ptr.mode === 'seek') { seekTo(snapT(Math.max(0, tOf(x)))); return; }
  if (ptr.mode === 'loop') {
    const t = snapT(Math.max(0, tOf(x)));
    S.proj.ui.loop = [Math.min(ptr.loopA, t), Math.max(ptr.loopA, t)];
    $('chkLoop').checked = S.proj.ui.loop[1] - S.proj.ui.loop[0] > .01;
    drawStatic(); return;
  }
  if (ptr.mode === 'marquee') {
    ov.marquee = { x0: ptr.x0, y0: ptr.y0, x1: x, y1: y };
    drawOverlay();
    return;
  }
  if (ptr.mode === 'clipgain') {
    // 上拖变大：1px = 1%，按住 Shift 变成 0.2%
    const step = e.shiftKey ? 0.002 : 0.01;
    const dg = (ptr.y0 - y) * step;
    if (Math.abs(dg) > 1e-6) {
      if (!ptr.snapTaken) { takeSnap(); ptr.snapTaken = true; }
      for (const it of ptr.gainClips) {
        it.clip.gain = clamp(Math.round((it.gain + dg) * 100) / 100, 0, 2);
      }
      markDirty();
      drawStatic(); renderInspector();
    }
    return;
  }
  if (ptr.mode === 'idle') return;

  const d = (x - ptr.x0) / S.view.pps;
  const o0 = ptr.orig?.[0];
  if (!o0) return;

  if (ptr.mode === 'move') {
    const want = Math.max(0, snapT(o0.start + d));
    const dd = want - o0.start;
    const row = rowAt(y);
    for (const o of ptr.orig) {
      o.clip.start = Math.max(0, o.start + dd);
      if (row && row.tr.kind === o.tr.kind && o === o0) {
        const i = o.tr.clips.indexOf(o.clip);
        if (i >= 0) o.tr.clips.splice(i, 1);
        row.tr.clips.push(o.clip);
        o.tr = row.tr;
        S.selTrack = row.tr.id;
      }
    }
    tr_sort();
    ov.dragGhost = null;
    drawStatic(); renderInspector();
  } else if (ptr.mode === 'trimL') {
    const want = snapT(o0.start + d);
    const dStart = clamp(want - o0.start, -o0.srcIn, o0.length - 0.03);
    for (const o of ptr.orig) {
      o.clip.start = o.start + dStart;
      o.clip.srcIn = Math.max(0, o.srcIn + dStart);
      o.clip.length = Math.max(0.03, o.length - dStart);
      o.clip.srcOut = o.clip.srcIn + o.clip.length;
      // ★ 曲线跟着裁：新 srcIn 相对旧的是 dStart，取 [dStart, dStart+新长] 这一段
      if (o.curve) {
        const r = curveRange(o.curve, dStart, dStart + o.clip.length);
        if (r) o.clip.pitchCurve = r; else delete o.clip.pitchCurve;
        o.clip.semis = 0; o.clip.cents = 0;
      }
      if (o.curve || o.renderKey) clipInvalidatePitchRender(o.clip);
    }
    drawStatic(); renderInspector();
  } else if (ptr.mode === 'trimR') {
    const want = snapT(o0.start + o0.length + d);
    const newLen = Math.max(0.03, want - o0.start);
    for (const o of ptr.orig) {
      o.clip.length = newLen;
      o.clip.srcOut = o.clip.srcIn + newLen;
      if (o.curve) {
        const r = curveRange(o.curve, 0, newLen);
        if (r) o.clip.pitchCurve = r; else delete o.clip.pitchCurve;
        o.clip.semis = 0; o.clip.cents = 0;
      }
      if (o.curve || o.renderKey) clipInvalidatePitchRender(o.clip);
    }
    drawStatic(); renderInspector();
  } else if (ptr.mode === 'stretchR') {
    const want = Math.max(0.05, snapT(o0.start + o0.length + d));
    const newLen = Math.max(0.05, want - o0.start);
    for (const o of ptr.orig) {
      o.clip.length = newLen;
      o.clip.needsRender = true;
    }
    drawStatic(); renderInspector();
  }
});

area.addEventListener('pointerup', (e) => {
  if (!ptr) return;
  const mode = ptr.mode;
  if (mode === 'marquee' && ov.marquee) {
    const { x0, y0, x1, y1 } = ov.marquee;
    const ta = tOf(Math.min(x0, x1)), tb = tOf(Math.max(x0, x1));
    const ya = Math.min(y0, y1), yb = Math.max(y0, y1);
    for (const row of trackRows()) {
      if (row.y + row.h < ya || row.y > yb) continue;
      for (const c of row.tr.clips) {
        if (c.start + c.length >= ta && c.start <= tb) S.sel.add(c.id);
      }
    }
    ov.marquee = null;
  }
  if (mode === 'marker') markDirty();
  if (mode === 'clipgain') {
    if (ptr.snapTaken) {
      afterStructure();          // 冻结签名含 clip.gain ⇒ 顺带重算
      const v = ptr.gainClips[0]?.clip.gain ?? 1;
      setSaveState(`片段音量 ${clipGainText({ gain: v })}`);
    } else {
      // 只是点了一下（没拖动）= 直接输入数字
      const moved = Math.abs(ptr.x - ptr.x0) + Math.abs(ptr.y - ptr.y0);
      if (moved < 3) askClipGain();
    }
  }
  if (['move', 'trimL', 'trimR', 'stretchR'].includes(mode)) {
    tr_sort();
    // ★ 拖完/裁完，接缝关系全变了：原来连续的可能被拉开（要淡变）,
    //   原来断开的可能又对上了（可以清零）。每次收手都重判一遍，别让它留硬边。
    seamFadesAll(new Set(ptr.orig.map(o => o.tr)));
    afterStructure();
    // ★ 裁剪会改源范围 ⇒ 带曲线/音高的块要重渲染（纯移动仍然零渲染）
    if (['trimL', 'trimR', 'stretchR'].includes(mode)) renderPending();
  }
  ov.dragGhost = null;
  ptr = null;
  drawStatic(); drawOverlay(); renderInspector();
});

// 双击播放列表左侧的轨名 → 就地改名（不用去混音台）
area.addEventListener('dblclick', (e) => {
  if (!S.proj) return;
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  // ★ 双击音量角标 = 恢复 100%
  const gh = y >= RULER_H ? clipGainHit(x, y) : null;
  if (gh) {
    takeSnap();
    for (const { clip } of (S.sel.has(gh.clip.id) ? selectedClips() : [{ clip: gh.clip }])) {
      clip.gain = 1;
    }
    afterStructure();
    setSaveState('片段音量已恢复 100%');
    return;
  }
  if (x >= HEAD_W || y < RULER_H) return;
  const row = rowAt(y);
  if (!row) return;
  if (y > row.y + 34) return;            // 下半截是 M/S/增益，别误触
  openTrackNameEditor(row.tr, r.left + 10, r.top + row.y + 8);
});

function openTrackNameEditor(tr, pageX, pageY) {
  document.querySelectorAll('.trackNameEdit').forEach(n => n.remove());
  const inp = document.createElement('input');
  inp.className = 'trackNameEdit';
  inp.value = tr.name || '';
  inp.spellcheck = false;
  inp.style.left = Math.round(pageX) + 'px';
  inp.style.top = Math.round(pageY) + 'px';
  document.body.appendChild(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    const v = String(inp.value || '').trim().slice(0, 32);
    inp.remove();
    if (ok && v && v !== tr.name) {
      takeSnap(); tr.name = v; markDirty(); drawStatic(); renderMixer(); renderFxPanel();
    } else {
      drawStatic();
    }
  };
  inp.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  };
  inp.onblur = () => finish(true);
}

function cursorFor(x, y) {
  if (y < RULER_H) return markerHit(x, y) ? 'ew-resize' : 'col-resize';
  if (x < HEAD_W) return 'default';
  if (clipGainHit(x, y)) return 'ns-resize';
  const hit = clipAt(x, y);
  if (!hit) return 'default';
  const x0 = xOf(hit.clip.start), x1 = xOf(hit.clip.start + hit.clip.length);
  if (x - x0 < EDGE || x1 - x < EDGE) return 'ew-resize';
  return 'grab';
}

function seekTo(t) {
  S.playAt = Math.max(0, t);
  if (engine.playing) engine.play(S.proj, S.playAt);
  drawOverlay(); updateClock();
  ensureVisible(S.playAt);
}
function ensureVisible(t) {
  const span = (W - HEAD_W) / S.view.pps;
  if (t < S.view.t0 || t > S.view.t0 + span * .92) {
    S.view.t0 = Math.max(0, t - span * .3);
    drawStatic();
  }
}

// 滚轮
area.addEventListener('wheel', (e) => {
  if (!S.proj) return;
  e.preventDefault();
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left;
  const wy = e.clientY - r.top;
  // ★ 角标上的滚轮 = 调音量；别让 Ctrl/Shift 的缩放/横滚抢走
  const gh = wy >= RULER_H ? clipGainHit(x, wy) : null;
  if (gh) {
    const step = e.shiftKey ? 0.01 : 0.05;
    // 连续滚只取一次撤销快照（700ms 以内算同一次）
    const now = Date.now();
    if (now - gainWheelSnap > 700) { takeSnap(); gainWheelSnap = now; }
    const targets = S.sel.has(gh.clip.id) ? selectedClips() : [{ clip: gh.clip }];
    for (const { clip } of targets) {
      const v = Number(clip.gain ?? 1) - Math.sign(e.deltaY) * step;
      clip.gain = clamp(Math.round(v * 100) / 100, 0, 2);
    }
    markDirty();
    drawStatic(); renderInspector();
    return;
  }
  if (e.ctrlKey || e.metaKey) {
    const anchor = tOf(x);
    const k = Math.pow(1.0016, -e.deltaY);
    S.view.pps = clamp(S.view.pps * k, 6, 900);
    S.view.t0 = Math.max(0, anchor - (x - HEAD_W) / S.view.pps);
  } else if (e.shiftKey) {
    // Shift+滚轮 = 横向走时间轴
    S.view.t0 = Math.max(0, S.view.t0 + (e.deltaY + e.deltaX) / S.view.pps * 1.2);
  } else {
    // ★ 普通滚轮 = 上下滚轨道。以前这里是横向，结果轨一多新轨就滚不出来。
    //   触控板横向滑（deltaX 明显大于 deltaY）仍然走时间轴。
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) * 1.5) {
      S.view.t0 = Math.max(0, S.view.t0 + e.deltaX / S.view.pps * 1.2);
    } else {
      S.view.y = clamp(S.view.y + e.deltaY, 0, maxScrollY());
    }
  }
  drawStatic(); drawOverlay(); renderInspector();
}, { passive: false });

// ---------------------------------------------------------------- 拖放
function dtPayload(e) {
  for (const t of ['text/x-singmic', 'text/plain']) {
    const v = e.dataTransfer.getData(t);
    if (!v) continue;
    try { return JSON.parse(v); } catch (err) { /* ignore */ }
  }
  return null;
}
area.addEventListener('dragover', (e) => {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  if (!S.proj) return;
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const t = Math.max(0, snapT(tOf(x)));
  const row = rowAt(y);
  if (row) {
    const h = row.h - CLIP_PAD * 2;
    ov.dragGhost = { xa: Math.max(HEAD_W, xOf(t)), xb: xOf(t) + 60, y: row.y + CLIP_PAD, h };
  } else ov.dragGhost = null;
  drawOverlay();
});
area.addEventListener('dragleave', () => { ov.dragGhost = null; drawOverlay(); });
area.addEventListener('drop', (e) => {
  e.preventDefault();
  ov.dragGhost = null;
  const p = dtPayload(e);
  if (!p || !S.proj) return;
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const t = Math.max(0, snapT(tOf(x)));
  const row = rowAt(y);
  addClip(p, t, row);
  drawOverlay();
});

// ---------------------------------------------------------------- 浏览器
function makeItem(text, sub, payload, cls) {
  const d = document.createElement('div');
  d.className = 'it ' + (cls || '');
  d.draggable = true;
  d.innerHTML = `<span class="dot"></span><span>${text}</span><span class="k">${sub || ''}</span>`;
  d.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/x-singmic', JSON.stringify(payload));
    e.dataTransfer.effectAllowed = 'copy';
  });
  return d;
}

async function loadRecordings() {
  try {
    const r = await fetch('/api/recordings').then(x => x.json());
    S.recs = r.files || [];
  } catch (e) { S.recs = []; }
  const box = $('recList');
  box.textContent = '';
  $('recCount').textContent = S.recs.length ? `(${S.recs.length})` : '';
  if (!S.recs.length) {
    box.innerHTML = '<div class="empty">还没有录音</div>';
    return;
  }
  for (const f of S.recs) {
    const isMix = f.name.includes('_mix');
    const it = makeItem(
      shortName(f.name),
      `${Math.round(f.sec)}s`,
      { kind: 'rec', name: f.name, path: 'recordings/' + f.name, dur: f.sec },
      isMix ? 'src-mix' : 'src-voice'
    );
    it.title = f.name;
    it.dataset.name = f.name;
    it.addEventListener('click', async () => {
      box.querySelectorAll('.it').forEach(x => x.classList.remove('on'));
      it.classList.add('on');
      S.pick = { kind: 'rec', path: 'recordings/' + f.name, name: f.name, dur: f.sec };
      // ★ 他要的「一整页」：点一条录音 = 把它的块铺成一条轨，直接进整页编辑。
      //   不改块的切法，只是把那些块从「一堆小芯片」摆成一条连着的时间线。
      const rel = 'recordings/' + f.name;
      await loadBlocks(rel);
      const tr = blocksToTrack(rel);
      if (tr) bigOpen(tr.id);
      else setSaveState(blocksWhy());
    });
    it.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      showCtx(e.clientX, e.clientY, [
        { label: '整页编辑…', act: async () => {
          const rel = 'recordings/' + f.name;
          await loadBlocks(rel);
          const tr = blocksToTrack(rel);
          if (tr) bigOpen(tr.id);
          else setSaveState(blocksWhy());
        } },
        { sep: true },
        { label: '删除录音…', act: () => {
          deleteRecording(f.name).catch(err => uiAlert('删除失败：' + err.message));
        } },
      ]);
    });
    box.appendChild(it);
  }
}

async function loadMedia() {
  try {
    const r = await api('/media_list');
    S.media = r.files || [];
  } catch (e) { S.media = []; }
  const box = $('mediaList');
  box.textContent = '';
  if (!S.media.length) {
    box.innerHTML = '<div class="empty">点「导入」加伴奏</div>';
    return;
  }
  for (const f of S.media) {
    const shown = mediaDisplayName(f.name);
    const it = makeItem(shortName(shown), `${Math.round(f.dur)}s`,
      { kind: 'media', name: f.name, path: f.path, dur: f.dur }, 'src-mix');
    it.title = `${shown}\n（右键改名）`;
    it.addEventListener('click', () => {
      box.querySelectorAll('.it').forEach(x => x.classList.remove('on'));
      it.classList.add('on');
      S.pick = { kind: 'media', path: f.path, name: f.name, dur: f.dur };
    });
    it.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      showCtx(e.clientX, e.clientY, [
        // ★ 导入的音频也能切成一个个音块（以前只有左栏「录音」里能切）
        { label: '正在切分为音块（整页编辑）…', act: async () => {
            const rel = String(f.path).replace(/\\/g, '/');
            await loadBlocks(rel);
            const tr = blocksToTrack(rel);
            if (tr) bigOpen(tr.id);
            else setSaveState(blocksWhy());
        } },
        { sep: true },
        { label: '重命名…', act: async () => {
          const v = await uiPrompt('新的文件名（扩展名可以省）', shown, { title: '重命名' });
          if (v == null) return;
          const want = String(v).trim();
          if (!want) return;
          // 用户可能只输入了「伴奏」，扩展名沿用原来的
          const ext = (f.name.match(/\.[^.]+$/) || [''])[0];
          const full = /\.[a-z0-9]{2,5}$/i.test(want) ? want : want + ext;
          mediaRename(f.path, full).catch(err => setSaveState('改名失败：' + err.message));
        } },
        { sep: true },
        { label: '删除…', act: () => {
          deleteMedia(f).catch(err => uiAlert('删除失败：' + err.message));
        } },
      ]);
    });
    box.appendChild(it);
  }
}

// 媒体改名：盘上改名 + 当前页面里的工程引用一起换
async function mediaRename(oldPath, newName) {
  const r = await api('/media_rename', { method: 'POST', json: { old: oldPath, new: newName } });
  const oldRel = r.old || String(oldPath).replace(/\\/g, '/');
  for (const s of Object.values(S.proj?.sources || {})) {
    if (String(s.path || '').replace(/\\/g, '/') === oldRel) {
      s.path = r.rel;
      s.name = r.name;
    }
  }
  if (S.pick && String(S.pick.path || '').replace(/\\/g, '/') === oldRel) {
    S.pick.path = r.rel;
    S.pick.name = r.name;
  }
  markDirty();          // 服务端已写盘，但页面上的工程也得跟着落一次
  await loadMedia();
  if (S.proj) drawStatic();
  setSaveState(`已改名为「${mediaDisplayName(r.name)}」`);
  return r;
}

async function loadBlocks(rel, opts = {}) {
  // ★ 切块现在认两种来源：recordings/ 的录音，和 projects/media/ 导入的音频。
  // ★ 切法有两种：按字/音节（默认）和按音（旧的逐音切法）；识别只负责贴文字。
  const sec = $('secBlocks'), box = $('blockList');
  const mode = opts.mode || $('blockMode')?.value || 'syllable';
  const wantAsr = !!(opts.asr != null
    ? opts.asr
    : ($('blockAsr')?.checked && S.asrReady));
  S.blocksMode = mode;
  box.textContent = '';
  sec.hidden = false;
  $('blockInfo').textContent = mode === 'syllable' ? '按字/音节分析中…' : '按音分析中…';
  try {
    const r = await fetch('/api/blocks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: rel, mode, asr: wantAsr, lang: 'auto' }),
    }).then(x => x.json());
    if (r.detail) throw new Error(r.detail);
    S.blocks = r;
    S.blocksName = rel;
    S.blocksMode = r.seg || mode;
    const notes = (r.blocks || []).filter(b => b.kind !== 'silence');
    const asrTxt = r.asr
      ? (r.asr.ok ? ` · 识别 ${r.asr.lang || ''}`.trim() : ' · 无文字')
      : '';
    $('blockInfo').textContent = `${t('{0} 块').replace('{0}', notes.length)} · ${r.key || ''}${r.mode === 'minor' ? 'm' : ''}${asrTxt}`;
    $('blockInfo').title = (r.asr && !r.asr.ok)
      ? `识别不可用：${r.asr.reason || ''}（切分不受影响）`
      : (r.asr?.approx
        ? '该识别模型不提供逐字时间戳；文字按音节顺序估算对齐，仅供参考'
        : '');
    for (const b of notes) {
      const dur = b.t1 - b.t0;
      const label = b.text
        ? (b.textApprox ? '~' + b.text : b.text)
        : (b.kind === 'note' ? `${b.note || '?'}` : '气声');
      const it = makeItem(
        label,
        fmt(dur, 2) + 's',
        {
          kind: 'block', name: rel.split('/').pop(), path: rel, dur: r.dur,
          blockId: b.id, srcIn: b.t0, srcOut: b.t1, note: b.note,
          f0: (b.f0 || 0) > 0 ? b.f0 : 0,
          text: b.text || '', syl: b.syl || 0,
          f0lo: b.f0lo || 0, f0hi: b.f0hi || 0,
        }, 'src-voice'
      );
      it.title = `${b.text ? b.text + ' · ' : ''}${b.kind} ${b.t0.toFixed(3)}–${b.t1.toFixed(3)}`;
      box.appendChild(it);
    }
    if (!notes.length) box.innerHTML = '<div class="empty">这条里没有唱的内容</div>';
  } catch (e) {
    S.blocks = null;
    S.blocksName = '';
    $('blockInfo').textContent = '';
    $('blockInfo').title = '';
    box.innerHTML = `<div class="empty">切不开：${e.message}</div>`;
  }
}

// 识别模型状态：没下载就把「识别」置灰，并露出「下载识别模型」
async function loadAsrStatus() {
  let st = null;
  try { st = await api('/asr/status'); } catch (e) { /* 老后端没有就算了 */ }
  S.asrStatus = st;
  const models = st?.models || {};
  const names = Object.entries(models).filter(([, m]) => m.ready).map(([, m]) => m.name);
  S.asrReady = names.length > 0;
  const cb = $('blockAsr'), wrap = $('blockAsrWrap'), dl = $('btnAsrDownload');
  if (!cb || !wrap || !dl) return;
  cb.disabled = !S.asrReady;
  wrap.classList.toggle('off', !S.asrReady);
  dl.hidden = S.asrReady;
  if (S.asrReady) {
    wrap.title = '识别模型已就绪：' + names.join('、') + '（只标文字，不改切法）';
  } else {
    cb.checked = false;
    wrap.title = '识别模型未下载 — 请点击右侧「下载识别模型」';
  }
}

async function downloadAsrModels() {
  const btn = $('btnAsrDownload');
  if (!btn || btn.disabled) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '下载中…';
  try {
    await api('/asr/download', { method: 'POST', json: { which: 'all' } });
    for (let i = 0; i < 900; i++) {
      await new Promise(r => setTimeout(r, 2000));
      let st = null;
      try { st = await api('/asr/status'); } catch (e) { continue; }
      S.asrStatus = st;
      const ds = st?.downloads || {};
      const active = Object.values(ds).filter(d =>
        d.state === 'downloading' || d.state === 'extracting');
      if (active.length) {
        const d = active[0];
        const pct = d.total ? Math.floor(100 * (d.got || 0) / d.total) : 0;
        btn.textContent = d.state === 'extracting' ? '解包…' : `下载 ${pct}%`;
        continue;
      }
      const errs = Object.entries(ds).filter(([, d]) => d.state === 'error');
      if (errs.length) throw new Error(errs.map(([, d]) => d.error).join('; '));
      break;
    }
    await loadAsrStatus();
    if (S.asrReady) {
      $('blockAsr').checked = true;
      toast('识别模型已就绪', 2200);
      if (S.blocksName) await loadBlocks(S.blocksName, { asr: true });
    }
  } catch (e) {
    toast('下载失败：' + e.message, 4500);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    await loadAsrStatus();
  }
}

$('blockMode')?.addEventListener('change', async (e) => {
  S.blocksMode = e.target.value;
  if (S.blocksName) await loadBlocks(S.blocksName, { mode: S.blocksMode });
});
$('blockAsr')?.addEventListener('change', async () => {
  if (S.blocksName) await loadBlocks(S.blocksName);
});
$('btnAsrDownload')?.addEventListener('click', downloadAsrModels);

// ---------------------------------------------------------------- 我的声音 / 补音
// ★ 本地补音永远可用；云端只是「更像你」。连不上就退回本地，不让界面卡住。
let VOICE = null;          // 最近一次 /voice/status
let EXT = null;            // 当前补音上下文 {clip, tr, srcDur, result}

function voiceStateHtml(st) {
  const c = st?.cloud || {}, cfg = st?.config || {};
  const rows = [];
  const dot = (ok) => ok ? '● 已连接' : '○ 未连接';
  rows.push(`<div class="mrow"><span class="mname">云端</span><span class="mtag ${c.reachable ? 'vocal' : ''}">${
    cfg.configured ? dot(c.reachable) : '未配置'}</span><span>${c.detail || ''}</span></div>`);
  if (c.reachable) {
    rows.push(`<div class="mrow"><span class="mname">模型</span><span class="mtag">${c.modelVersion || '—'}</span><span>${c.engine || ''} · ${c.device || ''}</span></div>`);
    rows.push(`<div class="mrow"><span class="mname">训练素材</span><span class="mtag">${c.materialsSec ? Math.round(c.materialsSec / 60) + ' 分钟' : '—'}</span><span>${c.trainedAt || ''}</span></div>`);
  }
  rows.push(`<div class="mrow"><span class="mname">本地补音</span><span class="mtag">可用</span><span>颗粒拼接 · 0.3×–8×</span></div>`);
  return rows.join('');
}

function renderVoicePanel() {
  const st = VOICE;
  if (!st) return;
  const cfg = st.config || {};
  const url = $('voiceUrl'), tok = $('voiceToken'), en = $('voiceEnabled');
  if (url && document.activeElement !== url) url.value = cfg.baseUrl || '';
  if (tok && document.activeElement !== tok) tok.placeholder = cfg.hasToken ? '已保存 ' + cfg.tokenMasked + '（留空 = 不改）' : '还没填';
  if (en) en.checked = cfg.enabled !== false;
  const box = $('voiceState');
  if (box) box.innerHTML = voiceStateHtml(st);
  const body = $('voiceBody');
  if (!body) return;
  if (!cfg.configured) {
    body.innerHTML = `<div class="agroup">接入步骤</div>
      <div class="inote" style="padding:4px 0;line-height:1.7">
      1. 按仓库中 <b>cloud/README.md</b> 在您自有的云 GPU 上完成训练并启动服务<br>
      2. 把服务地址（http://IP:8000）和 token 填在上面 → 保存<br>
      3. 点击「测试连接」，显示「已连接」即完成配置<br>
      ★ 未接入云端时补音功能照常可用，仅音色相似度较低。</div>`;
  } else {
    body.innerHTML = `<div class="agroup">说明</div>
      <div class="inote" style="padding:4px 0;line-height:1.7">
      补音在「检查器 → 补音…」或「块编辑 → 补音…」。<br>
      云端短时不可用（关机/断网）时自动用本地结果，不会失败。</div>`;
  }
}

async function loadVoiceStatus(force = false) {
  try {
    VOICE = await api('/voice/status' + (force ? '?force=1' : ''));
  } catch (e) {
    VOICE = { config: {}, cloud: { detail: e.message }, local: {} };
  }
  renderVoicePanel();
  return VOICE;
}

async function openVoicePanel() {
  $('voicePanel').hidden = false;
  await loadVoiceStatus(false);
}

async function saveVoiceConfig() {
  const btn = $('voiceSave');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '保存中…';
  try {
    VOICE = await api('/voice/config', {
      method: 'POST',
      json: {
        baseUrl: $('voiceUrl').value.trim(),
        token: $('voiceToken').value.trim(),
        enabled: $('voiceEnabled').checked,
      },
    });
    $('voiceToken').value = '';
    renderVoicePanel();
    toast('云端设置已保存', 2200);
  } catch (e) {
    toast('保存失败：' + e.message, 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function clearVoiceToken() {
  try {
    await api('/voice/config/clear', { method: 'POST', json: {} });
    await loadVoiceStatus(true);
    toast('token 已清除', 2200);
  } catch (e) {
    toast('清除失败：' + e.message, 4000);
  }
}

function fmtSec(s) {
  const v = Number(s) || 0;
  return v < 10 ? v.toFixed(2) + 's' : v.toFixed(1) + 's';
}

function updateExtRatio() {
  if (!EXT) return;
  const t = Math.max(0.05, Number($('extTarget').value) || 0);
  $('extRatio').textContent = (t / Math.max(0.01, EXT.srcDur)).toFixed(2) + '×';
}

function openExtendDialog() {
  const sel = selectedClips();
  if (!sel.length) { toast('请先选择一个块', 2200); return; }
  const { tr, clip } = sel[0];
  const src = S.proj.sources?.[clip.sourceId];
  if (!src) { toast('该块没有来源音频', 2600); return; }
  if (clip.kind !== 'note') { toast('仅「音」块支持补音；气声块不支持', 3000); return; }
  const srcDur = Math.max(0.02, (clip.srcOut ?? 0) - (clip.srcIn ?? 0));
  EXT = { tr, clip, srcDur, result: null };
  $('extPanel').hidden = false;
  $('extHead').textContent = `${tr.name || '轨道'} · ${clip.note || ''} ${shortName(src.name || '')}`;
  $('extSrcLen').textContent = fmtSec(srcDur);
  $('extTarget').value = Math.min(30, srcDur * 2).toFixed(2);
  $('extTarget').max = Math.min(30, srcDur * 8).toFixed(2);
  updateExtRatio();
  $('extApply').disabled = true;
  $('extGo').disabled = false;
  $('extBody').innerHTML = `<div class="inote" style="padding:6px 0;line-height:1.7">
    源片段 ${fmtSec(srcDur)} → 目标 ${fmtSec(srcDur * 2)}（默认 2×）。<br>
    最多可缩短至 30%；倍数极大时会循环最稳定的一段，可能产生颗粒感。</div>`;
  $('extNotes').textContent = '仅发送选中的片段；0.3×–8× 范围内最稳定，极端倍数会产生颗粒感。';
}

function closeExtendDialog() {
  $('extPanel').hidden = true;
  const a = $('extPanel').querySelector('audio');
  if (a) a.pause();
  EXT = null;
}

async function doExtend() {
  if (!EXT) return;
  const { clip, srcDur } = EXT;
  const src = S.proj.sources?.[clip.sourceId];
  const target = Math.min(30, Math.max(0.05, Number($('extTarget').value) || 0));
  const btn = $('extGo');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '生成中…';
  $('extBody').innerHTML = '<div class="inote" style="padding:6px 0">正在生成…' +
    (target / srcDur > 2 ? '（超过 2× 时处理较慢）' : '') + '</div>';
  try {
    const r = await api('/voice/extend', {
      method: 'POST',
      json: {
        source: src.path, srcIn: clip.srcIn, srcOut: clip.srcOut,
        targetSec: target, local: $('extLocal').checked,
      },
    });
    EXT.result = r;
    const name = r.path.split('/').pop();
    const eng = r.engine === 'cloud'
      ? `云端模型 ${r.modelVersion || ''}` : '本地颗粒拼接';
    $('extBody').innerHTML = `<div class="agroup">生成结果</div>
      <div class="mrow" style="display:flex;gap:10px;flex-wrap:wrap;padding:3px 0;font-size:11px;color:#aab8cc">
        <span class="mname">${eng}</span>
        <span class="mtag">${fmtSec(r.sec)}</span>
        ${r.cached ? '<span class="mtag">缓存命中</span>' : ''}
      </div>
      ${r.note ? `<div class="inote" style="padding:2px 0">${r.note}</div>` : ''}
      <audio controls style="width:100%;height:30px;margin-top:6px"
        src="/api/studio/media/${encodeURIComponent(name)}"></audio>`;
    $('extNotes').textContent = '请先试听，确认后再「应用」；应用后可按 Ctrl+Z 撤销。';
    $('extApply').disabled = false;
  } catch (e) {
    $('extBody').innerHTML = `<div class="inote" style="padding:6px 0;color:#ff9b9b">${e.message}</div>`;
    $('extNotes').textContent = '生成失败 —— 可勾选「只用本地」后重试。';
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function applyExtension() {
  if (!EXT?.result) return;
  const { clip } = EXT;
  const r = EXT.result;
  const sid = ensureSource(r.path, r.path.split('/').pop(), r.sec, 'voice');
  takeSnap();
  clip.sourceId = sid;
  clip.srcIn = 0;
  clip.srcOut = r.sec;
  clip.length = r.sec;
  delete clip.blockId;              // 源换了，不再对应原来的块
  delete clip.pitchCurve;
  clip.renderKey = null;
  clip.renderUrl = null;
  clip.needsRender = false;
  ensurePeaks(sid);
  markDirty();
  afterStructure();
  renderInspector();
  closeExtendDialog();
  toast(r.engine === 'cloud' ? '已应用云端补音' : '已应用本地补音', 2600);
}

$('btnVoice')?.addEventListener('click', openVoicePanel);
$('voiceClose')?.addEventListener('click', () => { $('voicePanel').hidden = true; });
$('voiceSave')?.addEventListener('click', saveVoiceConfig);
$('voiceClear')?.addEventListener('click', clearVoiceToken);
$('voiceTest')?.addEventListener('click', async () => {
  toast('正在连接云端…', 1500);
  await loadVoiceStatus(true);
  const c = VOICE?.cloud || {};
  toast(c.reachable ? `已连接 · ${c.modelVersion || '模型版本未知'}` :
    `连不上：${c.detail || '检查地址/token'}`, 4000);
});
$('bigExtend')?.addEventListener('click', openExtendDialog);
$('extClose')?.addEventListener('click', closeExtendDialog);
$('extCancel')?.addEventListener('click', closeExtendDialog);
$('extGo')?.addEventListener('click', doExtend);
$('extApply')?.addEventListener('click', applyExtension);
$('extTarget')?.addEventListener('input', updateExtRatio);
for (const b of document.querySelectorAll('.extq')) {
  b.addEventListener('click', () => {
    if (!EXT) return;
    $('extTarget').value = Math.min(30, EXT.srcDur * Number(b.dataset.x)).toFixed(2);
    updateExtRatio();
  });
}

// ---------------------------------------------------------------- 调音助手（DeepSeek）
// ★ 只发参数和他那句话；AI 的返回要过服务端白名单，这里再勾一次才落到工程上。
let NL_PLAN = null;

function nlScopeNow() {
  const v = $('nlScope').value;
  if (v === 'clips') {
    const ids = selectedClips().map(x => x.clip.id);
    return ids.length ? { type: 'clips', clipIds: ids } : null;
  }
  if (v === 'track') {
    const tid = S.selTrack || S.proj?.tracks?.[0]?.id;
    return tid ? { type: 'track', trackId: tid } : null;
  }
  return { type: 'project' };
}

function nlActionTitle(a) {
  if (a.kind === 'clipgain') {
    const p = v => Math.round(Number(v) * 100) + '%';
    return `这一块音量 ${p(a.from)} → ${p(a.to)}`;
  }
  if (a.kind === 'pan') {
    const p = v => {
      const n = Number(v) || 0;
      if (Math.abs(n) < 0.02) return '居中';
      return (n < 0 ? '左 ' : '右 ') + Math.round(Math.abs(n) * 100) + '%';
    };
    return `声像 ${p(a.from)} → ${p(a.to)}`;
  }
  return autoActionTitle(a);
}

function nlActionDetail(a) {
  if (a.kind === 'reverb') {
    return `干湿 ${Math.round((a.from || 0) * 100)}% → ${Math.round(a.to.mix * 100)}%` +
      ` · 空间 ${a.to.size}`;
  }
  if (a.kind === 'chain') {
    return (a.slots || []).map(s => {
      const n = t((typeof FX_TYPES !== 'undefined' && FX_TYPES[s.type] || {}).name || s.type);
      return n + (s.why ? `(${s.why})` : '');
    }).join(' · ');
  }
  if (a.kind === 'pitch') {
    const f = a.from || {}, t = a.to || {};
    return `${f.semis || 0} 半音 ${f.cents || 0} 音分 → ${t.semis || 0} 半音 ${t.cents || 0} 音分`;
  }
  return '';
}

async function nlLoadStatus() {
  let st = null;
  try { st = await api('/nl/status'); } catch (e) { st = null; }
  const row = $('nlKeyRow');
  if (row) row.hidden = !!st?.configured;
  const box = $('nlState');
  if (!box) return;
  box.innerHTML = st?.configured
    ? `<div class="mrow"><span class="mname">大脑</span><span class="mtag vocal">${escHtml(st.model || 'DeepSeek')}</span>
       <span>key ${escHtml(st.masked || '')}${st.source === 'env' ? ' · 来自环境变量' : ' · 保存于本机'}</span></div>`
    : `<div class="mrow"><span class="mname">大脑</span><span class="mtag">未配置</span>
       <span>需配置 DeepSeek key 后方可使用；未配置不影响其他功能</span></div>`;
}

async function openNlPanel() {
  if (!S.proj) { toast('请先打开一个工程', 2200); return; }
  $('nlPanel').hidden = false;
  const n = selectedClips().length;
  $('nlScope').value = n ? 'clips' : (S.selTrack ? 'track' : 'project');
  await nlLoadStatus();
}

async function nlSaveKey() {
  const k = ($('nlKey')?.value || '').trim();
  if (!k) { toast('请先填写 API Key', 2000); return; }
  try {
    await api('/nl/key', { method: 'POST', json: { apiKey: k } });
    $('nlKey').value = '';
    await nlLoadStatus();
    toast('API Key 已保存（仅存储于本机服务端）', 3000);
  } catch (e) {
    toast('保存失败：' + e.message, 4000);
  }
}

function nlRenderPlan(r) {
  NL_PLAN = r;
  const acts = r.actions || [];
  const html = [`<div class="agroup">AI:${escHtml(r.reply || '')}</div>`];
  acts.forEach((a, i) => {
    const detail = nlActionDetail(a);
    html.push(`<label class="arow"><input type="checkbox" data-i="${i}" checked>
      <span><b>${escHtml(nlActionTitle(a))}</b>
      ${detail ? `<br><i class="inote">${escHtml(detail)}</i>` : ''}
      ${a.why ? `<br><i class="inote">${escHtml(a.why)}</i>` : ''}</span></label>`);
  });
  if (r.dropped) {
    html.push(`<div class="inote" style="padding:4px 0">有 ${r.dropped} 条没过校验，已经丢掉</div>`);
  }
  $('nlBody').innerHTML = html.join('');
  $('nlApply').disabled = !acts.length;
  const extra = (r.notes || []).join(' · ');
  $('nlNotes').textContent = extra
    ? extra + ' · 本次仅发送参数与本条描述'
    : `本次仅发送参数与本条指令 · ${r.sec_cost != null ? r.sec_cost + 's' : ''}`;
}

async function nlGo() {
  if (!S.proj) return;
  const text = ($('nlText')?.value || '').trim();
  if (!text) { toast('请输入调整需求，例如「人声太干」', 2400); return; }
  const scope = nlScopeNow();
  if (!scope) { toast('当前范围内没有可调整的对象，请更换范围', 2600); return; }
  const btn = $('nlGo');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '想…';
  $('nlBody').innerHTML = '<div class="inote" style="padding:6px 0">正在理解…</div>';
  try {
    const r = await api('/nl_plan', {
      method: 'POST',
      json: { project: S.proj, text, scope, sendNames: $('nlSendNames').checked },
    });
    nlRenderPlan(r);
  } catch (e) {
    $('nlBody').innerHTML = `<div class="inote" style="padding:6px 0;color:#ff9b9b">${escHtml(e.message)}</div>`;
    $('nlApply').disabled = true;
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function nlApplyNow() {
  if (!NL_PLAN || !S.proj) return 0;
  const picks = [];
  document.querySelectorAll('#nlBody input[type=checkbox]').forEach(cb => {
    if (cb.checked) picks.push(NL_PLAN.actions[+cb.dataset.i]);
  });
  if (!picks.length) { toast('未选择任何项目', 2000); return 0; }
  const { done } = applyActions(picks);
  if (!done) { toast('当前方案已失效，请重新解析', 3000); return 0; }
  $('nlPanel').hidden = true;
  toast(`调音助手：应用了 ${done} 项 · Ctrl+Z 全部撤销`, 3600);
  return done;
}

$('btnNL')?.addEventListener('click', openNlPanel);
$('nlClose')?.addEventListener('click', () => { $('nlPanel').hidden = true; });
$('nlCancel')?.addEventListener('click', () => { $('nlPanel').hidden = true; });
$('nlKeySave')?.addEventListener('click', nlSaveKey);
$('nlGo')?.addEventListener('click', nlGo);
$('nlApply')?.addEventListener('click', nlApplyNow);
$('nlText')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); nlGo(); }
});

// ---------------------------------------------------------------- AI 伴奏（Mureka）
// ★ 只发文字（风格描述 + 歌词）。**不发录音、不发路径、不发文件名。**
//   平台那边有「参考音频」和「音色克隆」两个功能，本项目里**没有开关** ——
//   那两个功能的意思就是"把声音传上去"。
// ★ 一次生成扣一次钱，所以点「生成」前必须有一次明说的确认。
let genTask = null;
let genTimer = 0;
let genStatus = null;          // 最近一次 /gen/status，用来判断选中的平台有没有 key
let genT0 = 0;                 // 这次生成开始的时刻（算「已用时」）
let genTick = 0;               // 秒表 interval

const GEN_KEY_HINT = {
  mureka: 'Mureka 的 key(platform.mureka.cn → API Keys)',
  bailian: '百炼的 key（阿里云百炼控制台 → API-KEY,sk- 开头）',
  tokenhub: '腾讯云的 API Key(TokenHub 控制台 → API Key)',
};

// 「还没填 key」时给这个平台的操作路径。写成一张表，免得再拼 if。
const GEN_NO_KEY = {
  mureka: 'Mureka:platform.mureka.cn 建 API Key；歌词生曲约 ¥0.33/首，提示词生曲约 ¥2/首。',
  bailian: '百炼：在控制台创建 API Key，并在「模型广场」申请开通 fun-music；按秒计费，无需单次充值 200 元。',
  tokenhub: '腾讯云 TokenHub：控制台建 API Key，再去「在线推理 → 语音模型」给 '
    + 'mureka-music-v9 开「后付费」；按首计费。',
};

// 点「生成」前那句确认里的计费说明
// 费用只写最短的那一句，细节在「说明」页里
const GEN_BILL = {
  mureka: '按首 ¥0.33 起',
  bailian: '¥0.002/秒 · 三分钟约 ¥0.4',
  tokenhub: '按首 ¥0.33 · 带 5 秒水印',
};

/* 每个平台的真实上限。★这几个数不是拍的，是 studio_gen.py 里写死的截断/校验值：
   百炼带人声 5–350 字、纯伴奏 prompt 2000；腾讯云歌词 5000、风格词 1024;
   Mureka 风格词 1024、歌词 5000。界面上统一把**风格说明**卡在 1000 字，
   比三方里最小的那个还低，填到 1000 一定不会被悄悄截掉。 */
const GEN_LIMITS = {
  mureka:   { style: 1000, lyric: 5000, lyrMin: 0, gender: false },
  bailian:  { style: 1000, lyric: 350,  lyrMin: 5, gender: true  },
  tokenhub: { style: 1000, lyric: 5000, lyrMin: 2, gender: true  },
};

/* 模型清单：**按「平台 × 有没有人声」两层筛**。
   ★ 不是一张混合清单 —— 型号和端点是绑死的： Mureka 的 mureka-9 / o2 只能走
     「歌词生曲」，纯伴奏那条端点不认（找它们会回 400）；纯音乐要用 BGM 型号。
   标「未实测」的型号来自平台文档，我们这边还没有真打过 —— 摆在最后，
   选错了平台会回 400（不扣钱），不会悄悄出一首别的歌。 */
const GEN_MODELS = {
  mureka: {
    song: [['auto', '自动（平台默认）'], ['mureka-9', 'mureka-9 · 旗舰'],
           ['mureka-o2', 'mureka-o2 · 推理'], ['mureka-7.6', 'mureka-7.6 · 便宜'],
           ['mureka-8', 'mureka-8 · 未实测']],
    inst: [['auto', '自动（平台默认）'], ['mureka-7.6', 'mureka-7.6'],
           ['mureka-7.5', 'mureka-7.5 · BGM 专用（未实测）'],
           ['mureka-8', 'mureka-8 · 未实测']],
  },
  bailian: {
    song: [['auto', '自动（fun-music）'], ['fun-music-v1', 'fun-music-v1']],
    inst: [['auto', '自动（fun-music）'], ['fun-music-v1', 'fun-music-v1']],
  },
  tokenhub: {
    song: [['auto', '自动（mureka-music-v9）'], ['mureka-music-v9', 'mureka-music-v9']],
    inst: [],
  },
};

// ★ 型号 / 平台的能力说明**不放在面板上**了 —— 全在「说明」页里（studio.html #genHelp）,
//   面板只留控件和当场要动手的提示。改说明去改那页，别在这儿再写一份。

/* 歌曲结构模板：段落标签 → 小节数。小节数是给「预计时长」用的，
   也是这套结构的常见编配（流行 56 小节≈2:20 @120bpm）。 */
const GEN_FORMS = {
  pop: [['Intro', 4], ['Verse 1', 8], ['Pre-Chorus', 4], ['Chorus', 8],
        ['Verse 2', 8], ['Chorus', 8], ['Bridge', 4], ['Chorus', 8], ['Outro', 4]],
  ballad: [['Intro', 4], ['Verse 1', 8], ['Chorus', 8], ['Verse 2', 8],
           ['Chorus', 8], ['Bridge', 8], ['Chorus', 8], ['Outro', 4]],
  rnb: [['Intro', 4], ['Verse 1', 8], ['Hook', 8], ['Verse 2', 8],
        ['Hook', 8], ['Bridge', 4], ['Hook', 8], ['Outro', 4]],
  rap: [['Intro', 4], ['Verse 1', 16], ['Hook', 8], ['Verse 2', 16],
        ['Hook', 8], ['Outro', 4]],
  // 说唱加强版：两段 Rap 单独标出来，平台会把这两段唱成纯说
  rapfull: [['Intro', 4], ['Verse 1', 8], ['Rap', 16], ['Hook', 8],
            ['Verse 2', 8], ['Rap', 16], ['Hook', 8], ['Outro', 4]],
  // 叙事：开头先独白，把故事交代清楚再唱歌
  story: [['Intro', 8], ['Spoken', 4], ['Verse 1', 8], ['Chorus', 8],
          ['Verse 2', 8], ['Chorus', 8], ['Bridge', 4], ['Chorus', 8], ['Outro', 8]],
  // 耳语 / 气声：主歌压在耳边唱，副歌才放开
  whisper: [['Intro', 4], ['Whisper', 8], ['Verse 1', 8], ['Chorus', 8],
            ['Whisper', 4], ['Verse 2', 8], ['Chorus', 8], ['Outro', 4]],
  // 现场：两头是人群，中间才是歌
  live: [['Crowd', 4], ['Intro', 4], ['Verse 1', 8], ['Chorus', 8],
         ['Verse 2', 8], ['Chorus', 8], ['Crowd', 4], ['Outro', 4]],
  // 器乐：没有词，主题—独奏—主题
  solo: [['Intro', 8], ['Theme A', 16], ['Solo', 16], ['Interlude', 8],
         ['Solo', 16], ['Theme A', 16], ['Outro', 8]],
  // 电子：段落是「铺 → 起 → Drop」，主歌只是过门
  edm: [['Intro', 8], ['Build-up', 8], ['Drop', 16], ['Verse', 8],
        ['Build-up', 4], ['Drop', 16], ['Outro', 8]],
  rock: [['Intro', 4], ['Verse 1', 8], ['Pre-Chorus', 4], ['Chorus', 8],
         ['Verse 2', 8], ['Chorus', 8], ['Solo', 8], ['Chorus', 8], ['Outro', 4]],
  folk: [['Intro', 4], ['Verse 1', 8], ['Chorus', 8], ['Verse 2', 8],
         ['Chorus', 8], ['Bridge', 4], ['Chorus', 8], ['Outro', 4]],
  citypop: [['Intro', 4], ['Verse 1', 8], ['Pre-Chorus', 4], ['Chorus', 8],
            ['Verse 2', 8], ['Chorus', 8], ['Bridge', 8], ['Chorus', 8], ['Outro', 4]],
  guofeng: [['Intro', 8], ['Verse 1', 8], ['Chorus', 8], ['Interlude', 4],
            ['Verse 2', 8], ['Chorus', 8], ['Bridge', 4], ['Chorus', 8], ['Outro', 8]],
  lofi: [['Intro', 4], ['Verse 1', 8], ['Hook', 8], ['Verse 2', 8],
         ['Hook', 8], ['Outro', 4]],
  // 影视/配乐：没有人声，段落是「主题 → 铺 → 高潮」
  cinematic: [['Intro', 8], ['Theme A', 16], ['Build-up', 8], ['Climax', 16],
              ['Theme B', 8], ['Climax', 16], ['Outro', 8]],
  short: [['Intro', 4], ['Verse 1', 8], ['Chorus', 8], ['Outro', 4]],
};

// 段落标签的中文名字（标签本身必须是英文，平台认的是 [Verse] 这种）
const GEN_SEC_CN = {
  Intro: '前奏', Verse: '主歌', 'Pre-Chorus': '预副歌', 'Pre Chorus': '预副歌',
  Chorus: '副歌', 'Post-Chorus': '后副歌', Hook: '记忆点', Refrain: '叠句',
  Bridge: '桥段', Outro: '尾声', Ending: '收尾', Tag: '尾句',
  Instrumental: '间奏', Interlude: '间奏', Break: '断拍', Solo: '独奏',
  'Build-up': '推上去', 'Build Up': '推上去', Drop: 'Drop', Climax: '高潮',
  Rap: '说唱段', Spoken: '独白', 'Ad-lib': '即兴', Theme: '主题',
  'Theme A': '主题 A', 'Theme B': '主题 B',
  Verse2: '主歌 2', Chorus2: '副歌 2',
  // 一小段的「怎么唱 / 怎么处理」：平台认这些方括号标签，写进歌词里就生效
  Whisper: '耳语', 'Robot Voice': '机器人声', Telephone: '电话音',
  'Tape Hiss': '磁带底噪', Crowd: '人群声', Choir: '合唱',
};

/* 歌曲地图上每种段落一个色块 —— 看结构就知道哪段是副歌，不用读字 */
const GEN_SEC_COLOR = {
  Intro: '#20262f', Outro: '#20262f', Ending: '#20262f', Tag: '#20262f',
  Verse: '#26303f', Chorus: '#2f4a63', 'Post-Chorus': '#2f4a63',
  'Pre-Chorus': '#274058', Hook: '#3d2f4a', Refrain: '#3d2f4a',
  Bridge: '#4a3a22', Break: '#2a2a2a', Solo: '#4a3a22',
  'Build-up': '#2e4438', Drop: '#5a2f3a', Climax: '#5a2f3a',
  Instrumental: '#222b36', Interlude: '#222b36', Rap: '#3b3350',
  Spoken: '#2a2a2a', 'Ad-lib': '#2a2a2a', Theme: '#33405a',
  'Theme A': '#33405a', 'Theme B': '#33405a',
  Whisper: '#2c3b48', 'Robot Voice': '#3a3348', Telephone: '#43342a',
  'Tape Hiss': '#2a2a2a', Crowd: '#3a2f2f', Choir: '#33405a',
};

function genSecColor(name) {
  const m = /^(.+?)\s+\d+$/.exec(name);
  return GEN_SEC_COLOR[m ? m[1] : name] || GEN_SEC_COLOR[name] || '#2a3342';
}

function genSecLabel(name) {
  const m = /^(.+?)\s+(\d+)$/.exec(name);
  const base = m ? m[1] : name;
  const cn = GEN_SEC_CN[base];
  return cn ? (cn + (m ? ' ' + m[2] : '')) : name;
}

/** 状态行：一句话 + （忙的时候）转圈和已用时。
 *  ★ 忙的标记只挂一次：轮询每 3 秒会重画这一行，不能每次都把秒表归零。 */
function genSay(html, busy) {
  const b = $('genState');
  if (!b) return;
  b.innerHTML = `<div class="gstate${busy ? ' busy' : ''}">`
    + (busy ? '<span class="gspin"></span>' : '')
    + `<span class="gmsg">${html}</span>`
    + (busy ? '<span id="genElapsed" class="gprog">0:00</span>' : '')
    + '</div>';
  if (busy) genClock(false); else genClockStop();
}

/* 已用时。★不画百分比进度条 —— 平台不给进度，画出来的都是假的。 */
function genClock(reset) {
  if (reset || !genT0) genT0 = Date.now();
  const upd = () => {
    const s = Math.max(0, Math.floor((Date.now() - genT0) / 1000));
    const e = $('genElapsed');
    if (e) e.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  };
  upd();
  if (!genTick) genTick = setInterval(upd, 1000);
}

function genClockStop() {
  if (genTick) { clearInterval(genTick); genTick = 0; }
  genT0 = 0;
}

/** 生成中：把「生成」和「试跑」都按住，免得连点扣两次钱。 */
function genSetBusy(on) {
  const go = $('genGo');
  if (go) { go.disabled = !!on; go.textContent = on ? '生成中…' : '生成'; }
  if ($('genTest')) $('genTest').disabled = !!on;
  if ($('genFormApply')) $('genFormApply').disabled = !!on;
}

function genCounts() {
  const p = $('genProvider')?.value || 'mureka';
  const lim = GEN_LIMITS[p] || GEN_LIMITS.mureka;
  const mark = (id, n, max) => {
    const el = $(id);
    if (!el) return;
    el.textContent = `${n} / ${max}`;
    el.classList.toggle('over', n > max);
  };
  const sp = ($('genPrompt')?.value || '').length;
  const ly = ($('genLyrics')?.value || '').length;
  mark('genPromptCount', sp, lim.style);
  mark('genLyricsCount', ly, lim.lyric);
  const vocal = !!$('genVocal')?.checked;
  const hint = $('genLyricsHint');
  if (hint) {
    if (!vocal) {
      hint.className = 'genhint';
      hint.innerHTML = '不勾选「带 AI 人声」时输出纯伴奏，歌词不会被发送。';
    } else if (ly < lim.lyrMin) {
      hint.className = 'genhint warn';
      hint.innerHTML = `还差 ${lim.lyrMin - ly} 字 —— ${p === 'bailian' ? '百炼' : '这个平台'}的歌词至少 ${lim.lyrMin} 字。`;
    } else if (ly > lim.lyric) {
      hint.className = 'genhint over';
      hint.innerHTML = `超了 ${ly - lim.lyric} 字${p === 'bailian' ? '，百炼只收 350 字，先删短或者分两段生成' : ''}。`;
    } else {
      hint.className = 'genhint';
      hint.innerHTML = `还能写 ${lim.lyric - ly} 字 · 段落标签（如 <code>[Verse]</code>）同样计入字数。`;
    }
  }
}

function genCost() {
  const el = $('genCost');
  if (!el) return;
  const p = $('genProvider')?.value || 'mureka';
  /* 价格文案是变量拼出来的，整句进不了字典 —— 两截分别 t() */
  el.innerHTML = escHtml(t('预计费用：')) + escHtml(t(GEN_BILL[p] || '按首扣费（约 ¥0.3–¥2）'));
}

/** 换平台 / 换人声勾选 / 改字数，都走这里 —— 一处同步所有联动。 */
function genSyncForm() {
  const p = $('genProvider')?.value || 'mureka';
  const lim = GEN_LIMITS[p] || GEN_LIMITS.mureka;
  // 模型下拉按「平台 × 人声/伴奏」重铺；保住已经选的那个，保不住就回「自动」
  const sel = $('genModel');
  const inst = !($('genVocal')?.checked);
  if (sel) {
    const want = sel.value;
    const byMode = (GEN_MODELS[p] || GEN_MODELS.mureka);
    // ★ 这一路根本没有型号（腾讯云的纯伴奏）时也要有东西可选 ——
    //   下拉空着会看起来像坏了，而且没法从「自动」切回去
    const list = (byMode[inst ? 'inst' : 'song'] || []).length
      ? byMode[inst ? 'inst' : 'song']
      : (byMode.song || [['auto', '自动']]);
    sel.innerHTML = list
      .map(([v, t]) => `<option value="${escHtml(v)}">${escHtml(t)}</option>`).join('');
    sel.value = [...sel.options].some(o => o.value === want) ? want : 'auto';
  }
  // ★ 面板上只留「当场要你动手」的话；每个型号干啥活收在「说明」页里
  if ($('genModelHint')) {
    const noInst = inst && !(((GEN_MODELS[p] || {}).inst) || []).length;
    $('genModelHint').innerHTML = noInst
      ? '<b>该平台不支持纯伴奏</b> —— 需勾选「带 AI 人声」才能生成。'
      : '';
    $('genModelHint').hidden = !noInst;
  }
  // key 行：这个平台已有 key 就收起来，不占地方
  const has = !!(genStatus?.hasKey || {})[p];
  if ($('genKeyRow')) $('genKeyRow').hidden = has;
  if ($('genKey')) $('genKey').placeholder = GEN_KEY_HINT[p] || 'API key';
  // 平台能力那句话已经挪进「说明」页；这里只在缺 key 的时候提醒一句
  if ($('genProvHint')) {
    $('genProvHint').hidden = has;
    $('genProvHint').innerHTML = has
      ? ''
      : `<b>还没有 key:</b>${GEN_NO_KEY[p] || '去控制台建一个，填在上面那一栏'}`
        + ' · 也可先执行「试跑（免费）」';
  }
  // 状态行跟着**当前选的平台**走。以前它报的是「配置里存的那个平台」,
  // 下拉换成百炼、上面还写着腾讯云已配置 —— 会让人以为 key 填错地方了。
  if (genStatus && !genTimer && !genT0) {
    const nm = ({ mureka: 'Mureka', bailian: '阿里云百炼', tokenhub: '腾讯云 TokenHub' })[p] || p;
    const same = genStatus.provider === p;
    // 整句由变量拼出来，字典键只能覆盖整句 —— 所以这里必须逐段过 t()
    genSay(has
      ? `<span class="inote">${escHtml(t('{0} 已配置好 key').replace('{0}', nm))}`
        + (same && genStatus.masked ? ' · ' + escHtml(genStatus.masked) : '')
        + (same && genStatus.source === 'env'
          ? ' · ' + escHtml(t('来自环境变量')) : ' · ' + escHtml(t('存在本机'))) + '</span>'
      : `<span class="inote">${escHtml(t('{0} 尚未配置 API Key；也可先执行「试跑（免费）」。').replace('{0}', nm))}</span>`);
  }
  // 男女声只有「带人声 + 百炼/腾讯云」才起作用：其余情况置灰，别让人以为选了就有用
  if ($('genGender')) {
    $('genGender').disabled = !(($('genVocal')?.checked) && lim.gender);
    $('genGender').title = lim.gender
      ? '百炼 fun-music 与腾讯云 mureka-music-v9 均支持选择男女声（仅在勾选「带 AI 人声」时生效）'
      : 'Mureka 这一路不收「男女声」参数（它从歌词和风格里判断）';
  }
  genCounts();
  genCost();
}

// 老名字留着：还有别处按它调用
function genSyncProviderRow() { genSyncForm(); }

/* ---------------------------------------------------------------- 歌曲结构
 * 选模板 → 一串段落标签 → 「套用到歌词」把骨架写进去。
 * ★ 标签用小节数只算「预计时长」，不会发给平台（平台只收歌词原文）。
 */
/* 秒 → 小节（4/4 拍：一小节 4 拍，一拍 60/BPM 秒）。MuseAI 的大师模式是按秒配的，
   我们这里跟着它走，再按当前 BPM 折算成小节 —— 换 BPM 结构不会跟着缩水。 */
function genBarsOfSec(sec) {
  const bpm = genBpm();
  return Math.max(1, Math.round((+sec || 0) * bpm / 240));
}

/* 参数化结构：前奏 / 主歌 n 段 / 副歌 m 遍 / 间奏 / 尾奏。
   ★ 主歌和副歌**交替**排（主歌1 副歌 主歌2 副歌…），不够的副歌接在最后 ——
     这是流行歌的标准排法，不是随便堆。 */
function genParamSections() {
  const clamp = (v, lo, hi, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d;
  };
  const intro = clamp($('gpIntro')?.value, 0, 30, 8);
  const nv = clamp($('gpVerse')?.value, 1, 3, 2);
  const nc = clamp($('gpChorus')?.value, 1, 4, 2);
  const outro = clamp($('gpOutro')?.value, 0, 30, 8);
  const inter = !!$('gpInter')?.checked;
  const out = [];
  if (intro > 0) out.push(['Intro', genBarsOfSec(intro)]);
  const mid = [];
  for (let i = 1; i <= nv; i++) {
    mid.push([`Verse ${i}`, 8], ['Chorus', 8]);
  }
  for (let k = nv; k < nc; k++) mid.push(['Chorus', 8]);
  // 间奏插在最后一个副歌前面（「间奏 → 最后一遍副歌」是流行歌的标准排法）,
  // 只有一个副歌时退化成插在副歌后面
  if (inter) {
    const ci = mid.map(s => s[0]).lastIndexOf('Chorus');
    if (ci > 0) mid.splice(ci, 0, ['Interlude', 4]);
    else mid.push(['Interlude', 4]);
  }
  out.push(...mid);
  if (outro > 0) out.push(['Outro', genBarsOfSec(outro)]);
  return out;
}

function genFormData() {
  const name = $('genForm')?.value || 'pop';
  if (name !== 'custom') return GEN_FORMS[name] || GEN_FORMS.pop;
  const raw = ($('genFormCustom')?.value || '').trim();
  // 文字框留空 → 按上面那几个参数搭；写了字 → 以文字为准
  if (!raw) return genParamSections();
  return raw.split(/[,，、]/).map(s => s.trim().replace(/^\[|\]$/g, '').trim())
    .filter(Boolean).map(s => {
      const m = /^(.+?)\s+(\d+)$/.exec(s);
      if (m) return [m[1], Math.min(64, Math.max(1, +m[2]))];
      return [s, /^(Intro|Outro|前奏|尾声)$/i.test(s) ? 4 : 8];
    });
}

/** 从风格说明里找 BPM；找不到按 120 算。只用来估时长，不影响生成。 */
function genBpm() {
  const t = ($('genPrompt')?.value || '');
  const m = /(\d{2,3})\s*(?:bpm|BPM|Bpm|拍)/.exec(t);
  const b = m ? +m[1] : 0;
  return (b >= 40 && b <= 240) ? b : 120;
}

function genRenderForm() {
  const box = $('genFormChips');
  const secs = genFormData();
  /* ★ 段落名和小节数都是变量，进不了字典（键是中文原文）——在渲染时用 t() 拼 */
  const barsText = (n) => t('{0} 小节').replace('{0}', String(n));
  /* 段落名 = 「主歌」+ 序号：只翻词本身，序号原样保留（各语序不同也没关系） */
  const secName = (n) => genSecLabel(String(n))
    .replace(/^(.*?)(\s*\d+)?$/, (s, w, num) => t(w) + (num || ''));
  if (box) {
    box.innerHTML = secs.length
      ? secs.map(([n, bars]) =>
          `<button class="gchip" data-sec="${escHtml(n)}"`
          + ` title="${escHtml(t('点击将 [{0}] 插入歌词光标处').replace('{0}', n))}">`
          + `${escHtml(secName(n))}<i>${escHtml(barsText(bars))}</i></button>`).join('')
      : `<span class="genhint">${escHtml(t('自定义：先填上面的参数；要更细就在文字框里写段落名。'))}</span>`;
  }
  const bars = secs.reduce((a, [, b]) => a + b, 0);
  const bpm = genBpm();
  const sec = bars > 0 ? Math.round(bars * 4 * 60 / bpm) : 0;
  const len = $('genFormLen');
  if (len) {
    len.textContent = sec
      ? t('共 {0} 小节 · 约 {1} @ {2} BPM')
        .replace('{0}', String(bars))
        .replace('{1}', `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`)
        .replace('{2}', String(bpm))
      : '';
  }
  // 歌曲地图：每个段落一块，宽度按小节数分 —— 一眼看出副歌在哪、整首多长
  const map = $('genFormMap');
  if (map) {
    map.innerHTML = secs.map(([n, b]) => {
      const cn = genSecLabel(n);
      return `<span class="gseg" style="flex:${b} 1 0;background:${genSecColor(n)}"`
        + ` title="${escHtml(secName(n))} · ${escHtml(barsText(b))}">`
        + (b >= 6 ? `<em>${escHtml(secName(n))}</em>` : '') + '</span>';
    }).join('');
    map.hidden = !secs.length;
  }
}

/** 套用到歌词：写骨架并留空行。已有歌词要先确认 —— 那是他自己打的字。 */
async function genApplyForm() {
  const secs = genFormData();
  if (!secs.length) { toast('请先选择一个结构模板', 2400); return; }
  const ta = $('genLyrics');
  if (!ta) return;
  const body = secs.map(([n]) => `[${n}]\n`).join('\n');
  if (ta.value.trim() && !(await uiConfirm('应用结构将覆盖当前歌词，是否继续？', { danger: true }))) return;
  ta.value = body;
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  genCounts();
  toast('结构已写入歌词，请在每段标签下填写内容', 3200);
}

/** 点段落芯片：把 [Verse 1] 插到光标处（不覆盖已有文字）。 */
function genInsertSection(n) {
  const ta = $('genLyrics');
  if (!ta) return;
  if (ta.disabled) { toast('请先勾选「带 AI 人声」', 2400); return; }
  const tag = `[${n}]`;
  const pos = ta.selectionStart ?? ta.value.length;
  const s = ta.value;
  const sep = (!s ? '' : (s.slice(0, pos).endsWith('\n\n') ? '' : (s.slice(0, pos).endsWith('\n') ? '\n' : '\n\n')));
  ta.value = s.slice(0, pos) + sep + tag + '\n' + s.slice(pos);
  const at = pos + sep.length + tag.length + 1;
  ta.focus();
  ta.setSelectionRange(at, at);
  genCounts();
}

async function genLoadStatus() {
  let st = null;
  try { st = await api('/gen/status'); } catch (e) { st = null; }
  genStatus = st;
  if (!st) { genSay('<span class="inote">读不到状态：服务没起来？</span>'); return; }
  if (st.provider && $('genProvider')) $('genProvider').value = st.provider;
  genSyncForm();
  if (st.configured) {
    const line = t('已配置 · {0} · key {1}')
      .replace('{0}', st.providerName || st.provider).replace('{1}', st.masked || '');
    genSay(`<span class="inote">${escHtml(line)}`
      + (st.source === 'env' ? ' · ' + escHtml(t('来自环境变量')) : ' · ' + escHtml(t('存在本机')))
      + '</span>');
  } else {
    genSay(`<span class="inote">${escHtml(t('尚未填写 API Key。可先执行「试跑（免费）」了解界面用法。'))}</span>`);
  }
}

async function openGenPanel() {
  $('genPanel').hidden = false;
  genRenderForm();
  await genLoadStatus();
}

async function genSaveKey() {
  const k = ($('genKey')?.value || '').trim();
  if (!k) { toast('请先填写 API Key', 2000); return; }
  try {
    await api('/gen/config', { method: 'POST', json: {
      token: k, provider: $('genProvider')?.value || 'mureka' } });
    $('genKey').value = '';
    await genLoadStatus();
    toast('API Key 已保存（仅存储于本机服务端）', 3000);
  } catch (e) { toast('保存失败：' + e.message, 4000); }
}

async function genClearKey() {
  if (!(await uiConfirm('清掉本机保存的 key？清完要重新填一次。', { danger: true }))) return;
  try { await api('/gen/config/clear', { method: 'POST', json: {} }); await genLoadStatus(); }
  catch (e) { toast('清除失败：' + e.message, 3000); }
}

function genStop() {
  // 只停轮询，**不清 genTask** —— 生成成功后还要拿任务号去下载
  // （以前这里清成 null，试跑出来的候选虽然能点，但调试口读不到任务号）
  if (genTimer) { clearInterval(genTimer); genTimer = 0; }
  genClockStop();          // 秒表一起停，下次生成从 0:00 开始
}

function genRenderChoices(taskId, choices) {
  const box = $('genBody');
  if (!box) return;
  if (!choices.length) { box.innerHTML = '<div class="inote">没有可用的结果</div>'; return; }
  box.innerHTML = choices.map((c, i) => {
    // 本地试跑给的是 path；真平台给的是临时外链（腾讯云 12 小时后失效）,
    // 两种都能直接试听 —— 外链还没下载时就用外链播。
    const url = c.path
      ? ('/api/studio/media/' + encodeURIComponent(c.path.split(/[\\/]/).pop()))
      : (c.url || '');
    const sec = c.duration ? `${(+c.duration).toFixed(1)}s` : '';
    const mark = c.watermarked ? ' · <span class="inote">尾部 5 秒平台水印</span>' : '';
    return `<div class="mrow" data-i="${i}">
      <span class="mname">候选 ${i + 1}${sec ? ' · ' + sec : ''}${mark}</span>
      ${url ? `<audio controls preload="none" src="${escHtml(url)}" style="height:28px;flex:1;min-width:220px"></audio>` : '<span class="grow"></span>'}
      <button class="mini" data-place="${i}">放到轨道</button>
      <button class="mini" data-duo="${i}" title="拆分为「人声」与「伴奏」两条轨：伴奏可用于自行演唱，人声可设为目标参照">人声/伴奏</button>
      <button class="mini" data-split="${i}" title="用 Demucs 拆成 鼓 / 贝斯 / 其他，每条单独一轨好调（CPU 上要几分钟）">拆成四轨</button>
      <button class="mini" data-split6="${i}" title="拆得更细：鼓 / 贝斯 / 其他 / 吉他 / 钢琴。★ 只有编曲里真有吉他和钢琴才拆得出来，电钢琴/合成器常被分到「其他」；比四轨慢一些">拆成六轨</button>
    </div>`;
  }).join('');
  box.querySelectorAll('[data-place]').forEach(btn => {
    btn.addEventListener('click', () => genPlace(taskId, +btn.dataset.place));
  });
  box.querySelectorAll('[data-duo]').forEach(btn => {
    btn.addEventListener('click', () => genPlace(taskId, +btn.dataset.duo, 'duo'));
  });
  box.querySelectorAll('[data-split]').forEach(btn => {
    btn.addEventListener('click', () => genPlace(taskId, +btn.dataset.split, '4'));
  });
  box.querySelectorAll('[data-split6]').forEach(btn => {
    btn.addEventListener('click', () => genPlace(taskId, +btn.dataset.split6, '6'));
  });
}

async function genStart() {
  const mock = false;
  const pid = $('genProvider')?.value || 'mureka';
  const lim = GEN_LIMITS[pid] || GEN_LIMITS.mureka;
  const prompt = ($('genPrompt')?.value || '').trim();
  const vocal = !!$('genVocal')?.checked;
  const lyrics = ($('genLyrics')?.value || '').trim();
  // 腾讯云实测**只能**歌词生成歌曲（纯伴奏那一路在网关上不存在），提前拦住，
  // 别让他等一次请求换回一个 400。
  if (pid === 'tokenhub' && !vocal) {
    toast('腾讯云仅提供「带 AI 人声的整首歌」，不支持纯伴奏。'
      + '要纯伴奏切百炼/Mureka；或者勾上「带 AI 人声」写歌词，生成后用「人声/伴奏」拆出伴奏。', 7000);
    return;
  }
  // 百炼带人声那一路**只认歌词**，风格描述递过去也会被忽略；
  // 腾讯云那一路风格词是可选的。两家这时都不强制写风格描述。
  const lyricOnly = vocal && ['bailian', 'tokenhub'].includes(pid);
  if (!prompt && !lyricOnly) { toast('请先填写风格说明', 2400); return; }
  // 字数在**本地**先拦：发出去被平台截断，他花的钱买到的就不是自己写的那份
  if (prompt.length > lim.style) {
    toast(`风格说明上限 ${lim.style} 字（当前 ${prompt.length} 字），请先精简`, 4200);
    return;
  }
  if (vocal && !lyrics) { toast('勾选「带 AI 人声」后必须填写歌词', 2600); return; }
  if (vocal && lyrics.length < lim.lyrMin) {
    toast(`这个平台的歌词至少 ${lim.lyrMin} 字（现在 ${lyrics.length} 字）`, 3600);
    return;
  }
  if (vocal && lyrics.length > lim.lyric) {
    toast(`这个平台的歌词最多 ${lim.lyric} 字（现在 ${lyrics.length} 字），先删短或者分两段生成`, 4600);
    return;
  }
  genStop();
  genTask = null;
  $('genBody').innerHTML = '';
  genSetBusy(true);
  genSay('<span class="inote">正在提交…</span>', true);
  try {
    const r = await api('/gen/create', { method: 'POST', json: {
      provider: pid, prompt, lyrics, instrumental: !vocal,
      model: $('genModel')?.value || 'auto',
      gender: $('genGender')?.value || 'female', mock } });
    genTask = r.taskId;
    genSay('<span class="inote">已提交，等待平台处理…（通常 1–3 分钟，请勿关闭页面）</span>', true);
    genTimer = setInterval(() => {
      // 把 taskId 挂到这次查询上，回来时才知道点「放到轨道」用哪个 id
      api('/gen/task/' + encodeURIComponent(genTask))
        .then(x => { x.__task = genTask; return x; })
        .then(onGenResult)
        .catch(e => genSay(`<span class="inote">查询失败：${escHtml(e.message)}（还在重试）</span>`, true));
    }, 3000);
  } catch (e) {
    genSetBusy(false);
    genSay(`<span class="inote">没提交成功：${escHtml(e.message)}</span>`);
    toast('提交失败：' + e.message, 5000);
  }
}

function onGenResult(r) {
  const s = r?.status || '';
  if (s === 'succeeded') {
    const tid = r.__task || genTask;
    genStop();
    genSetBusy(false);
    genSay('<span class="inote">生成好了 —— 先试听，满意就「放到轨道」或拆轨。</span>');
    genRenderChoices(tid, r.choices || []);
    return;
  }
  if (s === 'failed' || s === 'timeouted' || s === 'cancelled') {
    genStop();
    genSetBusy(false);
    genSay(`<span class="inote">没生成成功：${escHtml(r.reason || s)}</span>`);
    return;
  }
  genSay(`<span class="inote">生成中…(${escHtml(r.rawStatus || s || 'running')}) 通常 1–3 分钟</span>`, true);
}

async function genMock() {
  genStop();
  genTask = null;
  $('genBody').innerHTML = '';
  genSetBusy(true);
  genSay('<span class="inote">本地渲染中…</span>', true);
  try {
    // 试跑也走「一整首带人声」：鼓是本地的，人声是系统语音念的歌词。
    // 这样没填 key 也能把「拆人声/伴奏 → 跟原唱」整条路真跑一遍。
    const r = await api('/gen/create', { method: 'POST', json: {
      prompt: ($('genPrompt')?.value || '本地试跑').trim(),
      lyrics: ($('genLyrics')?.value || '').trim(),
      instrumental: false, mock: true } });
    genTask = r.taskId;
    genSay('<span class="inote">本地渲染中（不联网、不产生费用；人声由系统语音合成，仅用于流程验证）…</span>', true);
    let n = 0;
    genTimer = setInterval(async () => {
      n++;
      if (n > 40) { genStop(); genSetBusy(false); genSay('<span class="inote">试跑超时</span>'); return; }
      try {
        const x = await api('/gen/task/' + encodeURIComponent(r.taskId));
        x.__task = r.taskId;
        onGenResult(x);
      } catch (e) { genStop(); genSetBusy(false); genSay(`<span class="inote">${escHtml(e.message)}</span>`); }
    }, 1500);
  } catch (e) { toast('试跑失败：' + e.message, 4000); }
}

const STEM_NAME = { drums: '鼓', bass: '贝斯', other: '其他', vocals: '人声',
                    guitar: '吉他', piano: '钢琴' };

async function genPlace(taskId, index, split) {
  const box = $('genBody');
  const lock = on => box?.querySelectorAll('button').forEach(b => { b.disabled = !!on; });
  try {
    if (split) { lock(true); genSay('<span class="inote">正在下载…</span>'); }
    const r = await api('/gen/fetch', { method: 'POST', json: { taskId, index } });
    const src = r?.source;
    if (!src?.path) throw new Error('没拿到文件');

    if (!split) {
      const clip = addClip({ kind: 'media', path: src.path, name: src.name, dur: src.sec || 0 }, 0);
      $('genPanel').hidden = true;
      toast('已添加到轨道，可在该轨道上录音', 3600);
      return clip;
    }

    // ---- 人声 / 伴奏：两轨。伴奏留着你自己唱，人声当原唱参照 ----
    if (split === 'duo') {
      genSay('<span class="inote">正在分离人声与伴奏…CPU 处理需要一定时间，请勿关闭此面板</span>');
      const s = await api('/reference/separate', { method: 'POST', json: { source: src.path } });
      const arr = s?.sources || [];
      const voice = arr.find(x => x.kind === 'voice');
      const inst = arr.find(x => x.kind !== 'voice');
      if (!inst) throw new Error('没拆出伴奏');
      const ci = addClip({ kind: 'media', path: inst.path, name: inst.name, dur: inst.dur || src.sec || 0 }, 0);
      const ti = S.proj.tracks.find(t => t.clips.includes(ci));
      if (ti) ti.name = '伴奏';
      if (voice?.path) {
        const cv = addClip({ kind: 'media', path: voice.path, name: voice.name, dur: voice.dur || src.sec || 0 }, 0);
        const tv = S.proj.tracks.find(t => t.clips.includes(cv));
        if (tv) { tv.name = 'AI 人声（可当原唱）'; tv.gain = 0.9; }
      }
      $('genPanel').hidden = true;
      toast(voice?.path
        ? '拆分完成 · 伴奏轨可用于自行演唱；调整音高时点击「🎤 跟原唱」，原唱轨选择 AI 人声轨'
        : '拆好了 · 只有伴奏（这一首本来就没有人声）', 6000);
      return [ci];
    }

    const six = split === '6';
    genSay(`<span class="inote">正在拆${six ? ' 6 ' : ''}轨（${six ? '鼓 / 贝斯 / 其他 / 吉他 / 钢琴 / 人声' : '鼓 / 贝斯 / 其他'}）… CPU 上要几分钟，别关这个面板</span>`);
    const s = await api('/reference/stems', { method: 'POST', json: { source: src.path, model: six ? '6' : '4' } });
    const all = s?.sources || [];
    // 纯器乐的伴奏里「人声」那轨其实是残渣；拆不出来的乐器那几轨也是。
    // 低于 -55 dB 的不摆上来，免得轨道列表里多一条空轨
    const keep = all.filter(x => (x.db ?? -120) > -55);
    const skip = all.filter(x => !keep.includes(x));
    if (!keep.length) throw new Error('分离结果为空，该片段可能过短');
    for (const x of keep) {
      const clip = addClip({ kind: 'media', path: x.path, name: x.name, dur: x.dur || src.sec || 0 }, 0);
      const tr = S.proj.tracks.find(t => t.clips.includes(clip));
      if (tr) tr.name = STEM_NAME[x.stem] || tr.name;
    }
    $('genPanel').hidden = true;
    const skipped = skip.length ? ` · 跳过 ${skip.map(x => STEM_NAME[x.stem] || x.stem).join('/')}（空）` : '';
    toast(`拆成 ${keep.length} 轨 · 每条都能单独加 EQ / 压缩 / 混响${skipped}`, 5200);
    return keep;
  } catch (e) {
    lock(false);
    toast((split ? '拆轨失败：' : '放不进轨道：') + e.message, 6000);
  }
}

// 选中一块 → 把它所在的**整个音频文件**拆成乐器轨。
// ★ 拆的是源文件，不是这一块：Demucs 要整首上下文才分得准；所以拆出来的轨
//   按「这块在源文件里的起点」摆回去，和原来的块对齐。块被裁过/挪过也照样对。
async function stemsFromClip(model) {
  const sel = selectedClips();
  if (sel.length !== 1) { toast('每次仅可拆分一个块', 2400); return; }
  const { clip } = sel[0];
  const src = S.proj?.sources?.[clip.sourceId];
  if (!src?.path) { toast('该块没有音频文件，无法拆分', 2600); return; }
  const six = model === '6';
  setSaveState(`正在拆${six ? ' 6 ' : ' 4 '}轨…CPU 上要几分钟，别关这个页面`);
  try {
    const r = await api('/reference/stems', {
      method: 'POST', json: { source: src.path, model: six ? '6' : '4' } });
    const all = r?.sources || [];
    // 拆不出来的乐器那几轨是残渣，低于 -55 dB 不摆上来
    const keep = all.filter(x => (x.db ?? -120) > -55);
    const skip = all.filter(x => !keep.includes(x));
    if (!keep.length) throw new Error('分离结果为空，该片段可能过短');
    const origin = Math.max(0, (Number(clip.start) || 0) - (Number(clip.srcIn) || 0));
    for (const x of keep) {
      const c = addClip({ kind: 'media', path: x.path, name: x.name, dur: x.dur || src.dur || 0 }, origin);
      const tr = S.proj.tracks.find(t => t.clips.includes(c));
      if (tr) tr.name = '拆分 · ' + (STEM_NAME[x.stem] || x.stem);
    }
    const skipped = skip.length ? ` · 跳过 ${skip.map(x => STEM_NAME[x.stem] || x.stem).join('/')}（空）` : '';
    setSaveState(`拆成 ${keep.length} 轨 · 每条能单独调音量/EQ/压缩${skipped}`);
  } catch (e) {
    setSaveState('拆轨失败：' + e.message);
    toast('拆轨失败：' + e.message, 6000);
  }
}

$('btnGen')?.addEventListener('click', openGenPanel);
$('genClose')?.addEventListener('click', () => {
  genStop(); genSetBusy(false); $('genPanel').hidden = true;
});
$('genKeySave')?.addEventListener('click', genSaveKey);
$('genProvider')?.addEventListener('change', genSyncForm);
// 换型号不再在面板上写一段介绍 —— 各型号干啥活都在「说明」页的「模型怎么选」里
$('genModel')?.addEventListener('change', genCost);
$('genKeyClear')?.addEventListener('click', genClearKey);
$('genTest')?.addEventListener('click', genMock);
$('genGo')?.addEventListener('click', async () => {
  const pid = $('genProvider')?.value || 'mureka';
  const vocal = !!$('genVocal')?.checked;
  const bill = GEN_BILL[pid] || '按首扣费（约 ¥0.3–¥2）';
  const nm = ({ mureka: 'Mureka', bailian: '阿里云百炼', tokenhub: '腾讯云 TokenHub' })[pid] || pid;
  const msg = `平台：${nm}\n`
    + (vocal ? '内容：一首**带 AI 人声**的歌\n' : '内容：一条纯伴奏（没有人声）\n')
    + `计费：${bill}\n\n该操作将实际调用平台接口并产生费用，确认生成？`;
  if (!(await uiConfirm(msg, { title: '确认生成', okText: '生成', danger: true }))) return;
  genStart();
});
/* ------------------------------------------------------------ 组合模板库
 * 65 条模板按分类列出来，每条能**试听 30 秒**。
 * ★ 试听是**真的让平台按这条模板生成一段**（平台不接受指定时长，只能整首生成）,
 *   我们只放前 30 秒；同一条生成过一次就缓存在服务端，再听一秒出声、不再花钱。
 * ★ 所以第一次点要问一遍「这次开始不再逐个问」，之后同一次会话里不再打断。
 */
const LIB = { cat: 'all', q: '', asked: false, playing: '', task: '', timer: 0 };
const libAudio = new Audio();
const libCache = new Map();                 // id -> {key, sec}
const LIB_CLIP = 30;                        // 只听前 30 秒

function libCatLabel(id) {
  const c = PRESET_CATS.find(x => x[0] === id);
  return c ? c[1] : id;
}

function libFiltered() {
  const q = LIB.q.trim().toLowerCase();
  return PRESETS.filter((p) => {
    if (LIB.cat !== 'all' && p.cat !== LIB.cat) return false;
    if (!q) return true;
    return (p.name + ' ' + p.brief + ' ' + p.bpm + ' ' + libCatLabel(p.cat))
      .toLowerCase().includes(q);
  });
}

function libBtnText(p) {
  if (LIB.playing === p.id) {
    const t = Math.min(LIB_CLIP, Math.floor(libAudio.currentTime || 0));
    return `■ ${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
  }
  if (LIB.task === p.id) return '生成中…';
  return libCache.has(p.id) ? '▶ 再听' : '▶ 试听';
}

function libRender() {
  const cats = $('libCats');
  const list = $('libList');
  if (!cats || !list) return;
  const cnt = (id) => (id === 'all' ? PRESETS.length
    : PRESETS.filter(p => p.cat === id).length);
  cats.innerHTML = PRESET_CATS.filter(c => c[0] === 'all' || cnt(c[0]) > 0)
    .map(([id, nm]) => `<button class="libcat${LIB.cat === id ? ' on' : ''}" `
      + `data-cat="${id}">${escHtml(nm)}<i>${cnt(id)}</i></button>`).join('');

  const items = libFiltered();
  if ($('libCount')) $('libCount').textContent = `${items.length} 条`;
  if (!items.length) {
    list.innerHTML = '<div class="libempty">没有匹配的模板 —— 换个词试试（流派、BPM、情绪都算）</div>';
    return;
  }
  list.innerHTML = items.map((p) => {
    const c = libCache.get(p.id);
    return `<div class="librow${LIB.playing === p.id ? ' on' : ''}" data-id="${p.id}">`
      + '<div class="libmeta">'
      + `<div class="libname">${escHtml(p.name)}`
      + `<i class="libtag bpm">${p.bpm} BPM</i>`
      + `<i class="libtag">${escHtml(libCatLabel(p.cat))}</i></div>`
      + `<div class="libb">${escHtml(p.brief)}</div>`
      + `<div class="libbar"${LIB.playing === p.id ? '' : ' hidden'}><i style="width:0"></i></div>`
      + (c ? `<div class="libb" style="color:#4f7a5c">已缓存 · 平台给了 `
        + `${Math.floor((c.sec || 0) / 60)}:${String(Math.round((c.sec || 0) % 60)).padStart(2, '0')}`
        + ` · 再次播放不产生费用</div>` : '')
      + '</div>'
      + '<div class="libact">'
      + `<button class="libbtn${LIB.playing === p.id ? ' playing' : ''}" `
      + `data-act="aud">${libBtnText(p)}</button>`
      + '<button class="libbtn use" data-act="use">用这个</button>'
      + '</div></div>';
  }).join('');
}

function libTouchRow(id) {
  const row = document.querySelector(`.librow[data-id="${id}"]`);
  const p = PRESETS.find(x => x.id === id);
  if (!row || !p) return;
  const b = row.querySelector('[data-act="aud"]');
  if (b) {
    b.textContent = libBtnText(p);
    b.classList.toggle('playing', LIB.playing === id);
  }
  row.classList.toggle('on', LIB.playing === id);
  const bar = row.querySelector('.libbar');
  if (bar) bar.hidden = LIB.playing !== id;
}

function libProgress() {
  if (!LIB.playing) return;
  const row = document.querySelector(`.librow[data-id="${LIB.playing}"]`);
  const bar = row?.querySelector('.libbar i');
  if (bar) bar.style.width = `${Math.min(100, (libAudio.currentTime || 0) / LIB_CLIP * 100)}%`;
  const p = PRESETS.find(x => x.id === LIB.playing);
  const b = row?.querySelector('[data-act="aud"]');
  if (p && b) b.textContent = libBtnText(p);
  if ((libAudio.currentTime || 0) >= LIB_CLIP) {
    libAudio.pause();
    libAudio.currentTime = 0;
    libStop();
  }
}

function libStop() {
  const id = LIB.playing;
  LIB.playing = '';
  libAudio.pause();
  if (id) libTouchRow(id);
}

libAudio.addEventListener('timeupdate', libProgress);
libAudio.addEventListener('ended', libStop);

/** 轮询试听任务，直到出结果（或失败）。平台这一路快的十几秒、慢的几十秒。 */
async function libPoll(tid, id, t0) {
  for (;;) {
    await new Promise(r => setTimeout(r, 2500));
    const r = await api(`/gen/task/${tid}`);
    const sec = Math.round((Date.now() - t0) / 1000);
    const row = document.querySelector(`.librow[data-id="${id}"]`);
    const b = row?.querySelector('[data-act="aud"]');
    if (b && LIB.task === id) {
      b.textContent = `生成中 ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
    }
    if (r?.status === 'succeeded') return r;
    if (r?.status && r.status !== 'running') {
      throw new Error(r.reason || r.status);
    }
    if (sec > 360) throw new Error('已等待 6 分钟仍未完成 —— 请关闭后稍后重试');
  }
}

async function libAudition(p, free) {
  if (LIB.playing === p.id) { libStop(); return; }
  if (LIB.task) { toast('已有生成任务进行中，请稍候', 2400); return; }
  if (LIB.playing) libStop();

  // 默认每次会话只问一次；设置里把「每次都要确认」打开就逐条问
  if (!free && (!LIB.asked || PREFS.confirmCost)) {
    const ok = await uiConfirm(
      `「${p.name}」试听将由平台按该模板实际生成一次。\n\n`
      + '· 平台不支持指定时长，只能整首生成，按整首计费（约 ¥0.4）\n'
      + '· 仅播放前 30 秒\n'
      + '· 同一模板试听一次后缓存在本机，再次播放不产生费用\n\n'
      + (PREFS.confirmCost ? '继续？' : '本次会话将不再逐条询问，是否继续？'), { title: '试听会产生费用' });
    if (!ok) return;
    LIB.asked = true;
  }

  LIB.task = p.id;
  libTouchRow(p.id);
  const t0 = Date.now();
  try {
    const st = await api('/gen/preview', {
      method: 'POST',
      json: { prompt: p.brief, provider: free ? 'mock' : 'bailian' },
    });
    if ($('libHint')) {
      $('libHint').textContent = st.cached
        ? '该条已在缓存中 —— 可直接播放，不产生费用。'
        : '正在按该模板生成（平台不支持指定时长，仅播放前 30 秒）…';
    }
    if (!st.cached) await libPoll(st.taskId, p.id, t0);
    const f = await api('/gen/preview/fetch', { method: 'POST', json: { taskId: st.taskId } });
    libCache.set(p.id, { key: f.key, sec: f.sec || 0, free: !!free });
    LIB.task = '';
    libRender();
    LIB.playing = p.id;
    libAudio.src = `/api/studio/gen/preview/audio/${f.key}`;
    libAudio.currentTime = 0;
    libTouchRow(p.id);
    try { await libAudio.play(); } catch (e) { toast(`播不出来：${e.message}`, 3200); }
    if ($('libHint')) {
      const sec = Math.round(f.sec || 0);
      $('libHint').textContent = free
        ? '这是本地鼓组试跑，只有节奏和速度 —— 不代表这条模板的音色。'
        : `平台给了 ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')},`
          + '按该时长计费；仅播放前 30 秒。再次点击该条不产生费用。';
    }
  } catch (e) {
    LIB.task = '';
    libTouchRow(p.id);
    toast(String(e.message || e).slice(0, 120), 4200);
  }
  libTouchRow(p.id);
}

async function libUse(p) {
  const ta = $('genPrompt');
  if (!ta) return;
  if (ta.value.trim() && ta.value.trim() !== p.brief
      && !(await uiConfirm('将替换当前风格说明，是否继续？', { danger: true }))) return;
  ta.value = p.brief;
  genCounts(); genRenderForm();
  libClose();
  ta.focus();
  toast(`已填入「${p.name}」· 可再改 BPM / 乐器`, 2600);
}

function libOpen() {
  const el = $('genLib');
  if (!el) return;
  el.hidden = false;
  libRender();
  $('libSearch')?.focus();
}

function libClose() {
  if ($('genLib')) $('genLib').hidden = true;
  libStop();
}

$('genLibOpen')?.addEventListener('click', libOpen);
$('libClose')?.addEventListener('click', libClose);
// 说明页：面板上所有「旁边的解释」都收在这里
$('genHelpBtn')?.addEventListener('click', () => { if ($('genHelp')) $('genHelp').hidden = false; });
$('genHelpClose')?.addEventListener('click', () => { if ($('genHelp')) $('genHelp').hidden = true; });
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('genLib') && !$('genLib').hidden) { libClose(); return; }
  if ($('genHelp') && !$('genHelp').hidden) $('genHelp').hidden = true;
});
$('libSearch')?.addEventListener('input', (e) => {
  LIB.q = e.target.value || '';
  libRender();
});
$('libCats')?.addEventListener('click', (e) => {
  const b = e.target.closest('.libcat');
  if (!b) return;
  LIB.cat = b.dataset.cat || 'all';
  libRender();
});
$('libList')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]');
  const row = e.target.closest('.librow');
  if (!b || !row) return;
  const p = PRESETS.find(x => x.id === row.dataset.id);
  if (!p) return;
  if (b.dataset.act === 'use') libUse(p);
  else libAudition(p, false);
});
$('libTest')?.addEventListener('click', () => {
  const items = libFiltered();
  const p = items[0] || PRESETS[0];
  libAudition(p, true);
});

/* ---------------------------------------------------------------------------
   首次引导：第一次打开自动走一遍，之后右上「?」随时调出来
   ★ 一页只讲一件事，不放整段说明书 —— 要查细节是「说明」页的活。
   --------------------------------------------------------------------------- */
const OB_KEY = 'singmic.onboard.v1';
/* ★ 这个参数必须在模块加载时就抓住：载入工程时 replaceState 会把地址改写成
   `?p=<工程>`,`onboard` 到那会儿已经没了 —— 等 obMaybe 再读永远读不到。 */
const OB_FORCE = new URLSearchParams(location.search).get('onboard');
const OB_STEPS = [
  {
    k: '开场', t: '录音、修音、编曲与混音，一处完成',
    lead: '录音 → 修音 → 编曲 → 导出，四项操作均在此页完成。建议先阅读以下四步；之后可随时通过右上角「设置」重新查看。',
    pts: [
      ['素材', '把手机或录音笔录好的文件导进来，或者用现成的 wav / mp3。'],
      ['修音', '载入后自动切成块，拖块改音高；「跟原唱」把跑调的地方收回去。'],
      ['伴奏', '自己弹、用鼓机，或者让 AI 按现成模板生成一条。'],
    ],
  },
  {
    k: '素材', t: '先把声音拿进来',
    lead: '左侧「录音」为程序目录中已有的录音文件，「媒体」为导入的音频，两者均可直接拖入轨道。',
    pts: [
      ['手机录', '手机录好后把文件传到电脑，从「媒体 → 导入」加入工程。'],
      ['导入', '通过「媒体 → 导入」选择文件；若仅有他人作品，可用「跟原唱」导入主唱作为参照。'],
      ['对准', '拖入轨道后按空格键试听；「对拍」将选中的块对齐到网格。'],
    ],
    acts: [['import', '导入一条音频']],
  },
  {
    k: '修音', t: '切块、改音高、收跑调',
    lead: '在左栏选择录音后自动切块（默认按字 / 音节）；「整页编辑」可展开完整片段进行编辑。',
    pts: [
      ['改音高', '上下拖动块即可升降调；整页视图支持沿音高线逐点绘制。'],
      ['跟原唱', '顶部「🎤 跟原唱」选一条参考人声，算出该移多少，点确认才真改。'],
      ['一键调音', '「✨ 一键调音」测量响度、峰值与音高偏差，输出可勾选的调整方案，不会自动修改工程。'],
      ['安全', '以上处理均在本机完成，不联网、不上传。'],
    ],
  },
  {
    k: '伴奏', t: '配伴奏，然后导出',
    lead: '即使没有伴奏也可开始：模板库提供 65 条预设配置，试听仅播放前 30 秒（平台按整首计费）；如需零成本验证，可使用「试跑」。',
    pts: [
      ['AI 伴奏', '顶部「🎵 AI 伴奏」→「组合模板库」选择模板 → 生成 → 自动拆分为「人声 / 伴奏」两条轨道。'],
      ['试跑', '全程在本机执行：鼓组与系统语音朗读歌词，以零费用完成完整流程，确认效果后再使用付费生成。'],
      ['导出', '「导出」生成整首混音；「分轨」导出 ZIP，供其他软件进行后期处理。'],
    ],
    acts: [['gen', '打开 AI 伴奏']],
  },
];
let obIdx = 0;

function obFlagGet() {
  try { return localStorage.getItem(OB_KEY) === '1'; } catch { return true; }
}
function obFlagSet(on) {
  try {
    if (on) localStorage.setItem(OB_KEY, '1');
    else localStorage.removeItem(OB_KEY);
  } catch { /* 隐私模式：记不住就算了，别挡住引导本身 */ }
}

function obRender() {
  const st = OB_STEPS[obIdx];
  const last = obIdx === OB_STEPS.length - 1;
  $('obKicker').textContent = st.k;
  $('obNum').textContent = String(obIdx + 1).padStart(2, '0');
  $('obTotal').textContent = `共 ${OB_STEPS.length} 步`;
  $('obStep').textContent = `第 ${obIdx + 1} 步 / 共 ${OB_STEPS.length} 步`;
  $('obTitle').textContent = st.t;
  $('obLead').textContent = st.lead;
  $('obPoints').innerHTML = st.pts.map(([k, v]) =>
    `<li><b>${escHtml(k)}</b><span>${escHtml(v)}</span></li>`).join('');
  $('obActs').innerHTML = (st.acts || []).map(([act, label]) =>
    `<button class="obact" data-ob="${act}">${escHtml(label)}</button>`).join('');
  $('obDots').innerHTML = OB_STEPS.map((_, i) =>
    `<i class="${i === obIdx ? 'on' : ''}"></i>`).join('');
  $('obBack').disabled = obIdx === 0;
  $('obNext').textContent = last ? '开始用' : '下一步';
}

function obShow(i = 0) {
  const el = $('onboard');
  if (!el) return;
  obIdx = Math.max(0, Math.min(OB_STEPS.length - 1, i));
  obRender();
  el.hidden = false;
  $('obAgain').checked = true;
  $('obNext').focus();
}

function obClose() {
  if ($('onboard')) $('onboard').hidden = true;
  obFlagSet(!!$('obAgain')?.checked);
}

function obGo(d) {
  const n = obIdx + d;
  if (n < 0) return;
  if (n >= OB_STEPS.length) { obClose(); return; }
  obIdx = n;
  obRender();
}

/** 第一次打开才自动出现：已经有素材的人不算「第一次」，不拿引导盖住他正在用的东西 */
function obMaybe() {
  const q = OB_FORCE;
  if (q === '0') return;
  if (q === '1') { obShow(0); return; }
  if (!PREFS.guide) return;          // 设置里关掉了「启动时自动弹」
  if (obFlagGet()) return;
  const hasClips = (S.proj?.tracks || []).some(t => (t.clips || []).length);
  const hasSound = (S.recs || []).length || (S.media || []).length || hasClips;
  if (hasSound) { obFlagSet(true); return; }
  obShow(0);
}

$('btnGuide')?.addEventListener('click', () => obShow(0));
$('genGuide2')?.addEventListener('click', () => {
  if ($('genHelp')) $('genHelp').hidden = true;
  obShow(0);
});
$('obNext')?.addEventListener('click', () => obGo(1));
$('obBack')?.addEventListener('click', () => obGo(-1));
$('obSkip')?.addEventListener('click', obClose);
$('obActs')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-ob]');
  if (!b) return;
  const act = b.dataset.ob;
  obClose();
  if (act === 'import') $('fileImport')?.click();
  else if (act === 'gen') openGenPanel();
});
document.addEventListener('keydown', (e) => {
  if (!$('onboard') || $('onboard').hidden) return;
  if (e.key === 'Escape') { obClose(); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { obGo(1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { obGo(-1); e.preventDefault(); }
});

/* ---------------------------------------------------------------------------
   设置：帮助与反馈 + 关于
   ★ 手册 / FAQ / 快捷键 / 日志这些长文字只放在这儿一份；面板上、按钮旁边
     都不再重复解释（他要的是「点进一个界面看说明」）。
   --------------------------------------------------------------------------- */
const SET = { sec: 'prefs', about: null, aboutErr: false, cache: null };
let SET_SEQ = 0;

function setPrefsHtml() {
  const on = (b) => (b ? ' checked' : '');
  return `
<h3>偏好设置</h3>
<p>以下设置仅作用于本机，不随工程文件保存；在其他设备上使用默认值。</p>
<div class="setrows">
  <div class="setrow"><b>界面语言</b><span>
    <select id="prefLang" class="setinp">
      <option value=""${PREFS.lang ? '' : ' selected'}>跟随系统</option>${LANGS.map((l) =>
        `<option value="${l.id}"${PREFS.lang === l.id ? ' selected' : ''}>${l.label}</option>`).join('')}
    </select>
    <em class="setnote">切换后界面文字立即更新；工程内容、录音名称与用户输入的文字不会被翻译。</em></span></div>
  <div class="setrow"><b>默认切法</b><span>
    <select id="prefStage" class="setinp">
      <option value="syllable"${PREFS.stage === 'syllable' ? ' selected' : ''}>按字 / 音节（推荐）</option>
      <option value="note"${PREFS.stage === 'note' ? ' selected' : ''}>按音（逐音）</option>
    </select>
    <em class="setnote">新建录音所使用的默认切分方式；也可在左栏下拉框中按次修改。</em></span></div>

  <div class="setrow"><b>自动保存</b><span>
    <label class="setchk"><input id="prefAutosave" type="checkbox"${on(PREFS.autosave)}>
      修改后 0.8 秒自动保存</label>
    <em class="setnote">关闭后仅在 <code>Ctrl+S</code> 或切换工程时写入；右上角将持续提示「未保存」。</em></span></div>

  <div class="setrow"><b>费用确认</b><span>
    <label class="setchk"><input id="prefConfirmCost" type="checkbox"${on(PREFS.confirmCost)}>
      每次试听 / 生成前均再次确认</label>
    <em class="setnote">默认每次会话仅询问一次。如需对每笔费用单独确认，请勾选此项。</em></span></div>

  <div class="setrow"><b>新手引导</b><span>
    <label class="setchk"><input id="prefGuide" type="checkbox"${on(PREFS.guide)}>
      首次启动时自动显示引导</label>
    <em class="setnote">关闭后仍可随时从本页底部「再看一遍新手引导」重新打开。</em></span></div>
</div>
<p class="settip">引导中的四个步骤（素材 → 修音 → 伴奏 → 导出）可在此重新查看：
  <button id="setGuide" class="setmini">再看一遍新手引导</button></p>
`;
}

async function setDataWait() {
  const [a, c] = await Promise.all([
    setAbout(),
    api('/cache_info').catch(() => null),
  ]);
  SET.cache = c;
  const p = a.paths || {};
  const cRow = c
    ? `<div class="setrow"><b>渲染缓存</b><span>${c.files} 个文件 · ${c.mb} MB
        <em class="setnote">正在使用 ${c.used} 个，未被引用 ${c.orphan} 个。</em></span></div>`
    : '<div class="setrow"><b>渲染缓存</b><span class="muted">读不到</span></div>';
  return `
<h3>数据与缓存</h3>
<div class="setrows">${cRow}
  <div class="setrow"><b>录音</b><span><code>${escHtml(p.recordings || '…')}</code></span></div>
  <div class="setrow"><b>工程</b><span><code>${escHtml(p.projects || '…')}</code></span></div>
  <div class="setrow"><b>导出</b><span><code>${escHtml(p.exports || '…')}</code></span></div>
  <div class="setrow"><b>程序</b><span><code>${escHtml(p.root || '…')}</code></span></div>
</div>
<p>缓存是切块、变调、效果渲染出来的中间音频 —— 删了不会丢工程，下次用到会重新算。</p>
<p><button id="setCacheClean" class="setmini">清理未被引用的缓存</button></p>
<p class="settip">录音、工程、成品都只存在这台电脑上，整个软件没有「上传我的作品」这个动作。</p>
`;
}

const SET_MANUAL = `
<h3>使用手册</h3>
<p>从拿到一条声音到导出成品，一共四步。每步都能单独用，不必全走。</p>

<h4>一、声音从哪来</h4>
<ul>
  <li><b>手机录音</b>：用手机自带的录音功能或任意录音 App 录好后，把文件传到电脑，从「媒体 → 导入」加入工程；桌面版只在电脑本机提供服务，手机不能直连。</li>
  <li><b>导入文件</b>：左栏「媒体 → 导入」选 wav / mp3 / flac / ogg，再拖到轨道上。</li>
  <li><b>外部歌曲作为参照</b>：顶部「🎤 跟原唱 → 导入原唱」，仅作参照，不会修改录音。</li>
</ul>

<h4>二、修音</h4>
<ul>
  <li>在左栏选择<b>录音</b>后将自动切块（默认按字 / 音节）；「整页编辑」可铺满整页，便于精细编辑。</li>
  <li><b>拖块上下</b>是升降调；整页的音高视图里能对着音高线逐点画（曲线最多 ±200 音分）。</li>
  <li>「<b>🎤 跟原唱</b>」先分析后给出方案，确认后才修改；无法对齐时会如实提示，不做强制修改。</li>
  <li>「<b>✨ 一键调音</b>」测量响度、峰值、频段与音高偏差，列出可勾选的调整方案，不会自动修改工程。</li>
  <li>改错了就 <code>Ctrl+Z</code>；一次操作 = 一步撤销。</li>
</ul>

<h4>三、伴奏</h4>
<ul>
  <li><b>自己的伴奏</b>：直接导入拖到一条轨上，按 BPM 对整齐。</li>
  <li><b>AI 伴奏</b>：顶部「🎵 AI 伴奏」→ 填平台 key → 点「组合模板库」挑一条
      → 生成 → 拆分为「人声 / 伴奏」两轨，伴奏轨可用于自行演唱。</li>
  <li>模板库每条均可<b>试听前 30 秒</b>。平台不支持指定时长，只能整首生成，因此<b>按整首计费</b>；
      同一模板试听一次后缓存在本机，再次播放不产生费用。</li>
  <li>零费用验证：<b>「试跑（免费）」</b>使用本机鼓组与系统语音合成一首歌曲，
      轨道拆分功能照常可用，可在不产生费用的情况下完成完整流程。</li>
  <li><b>鼓机</b>：顶部「🥁 鼓机」新建一条鼓轨，在下方的网格卷帘中逐格编辑。</li>
</ul>

<h4>四、效果与导出</h4>
<ul>
  <li>混音台每条轨均可挂载<b>插入效果链</b>（EQ / 压缩 / 混响等），参数修改后即时生效。</li>
  <li><b>VST3 插件</b>无法实时运行，需先「冻结」该轨；未安装 pedalboard 时本页会说明缺失的依赖。</li>
  <li>「导出」输出整首混音（按住 <code>Alt</code> 为 MP3 320k），「分轨」输出 ZIP 供其他软件做后期；
      文件位于 <code>projects/exports/</code>。</li>
  <li>母带响度在顶部选择（-14 / -9 LUFS / 关闭）。</li>
</ul>

<h4>五、上传范围</h4>
<ul>
  <li><b>录音不上传</b>：切块、修音、混音、导出均在本机完成。</li>
  <li><b>AI 生成</b>仅发送风格说明与歌词，不发送录音、文件名或工程名。</li>
  <li><b>音色补音</b>仅发送选中的片段（≤15 秒）至您自行配置的服务器。</li>
  <li>平台 API Key 仅保存在本机，界面中仅显示掩码。</li>
</ul>

<h4>六、已知限制</h4>
<ul>
  <li>含伴奏的整曲切块精度较低（伴奏会被识别为气声）；需精修时请使用清唱，或先执行人声分离。</li>
  <li>音高检测未使用神经网络，清唱效果最佳；曲线精度为每 20 ms 一个采样点。</li>
  <li>音高偏移超过 12 半音或拉伸超过 1.5 倍时会出现明显颗粒感（PSOLA 的固有特性）。</li>
  <li>和弦检测在人声或鼓点存在时可能出错，结果仅作和声参考。</li>
</ul>

<p class="settip">首次使用建议先阅读引导：<button id="setGuide" class="setmini">再看一遍新手引导</button></p>
`;

const SET_FAQ = `
<h3>常见问题</h3>
<details class="faq"><summary>手机录的音怎样导入电脑？</summary>
  <div>桌面版只在本机地址上运行服务，手机不能直接连接。请先在手机上录音，再通过数据线、网盘或聊天工具把文件传到电脑，然后从<b>「媒体 → 导入」</b>加入工程；支持 wav / mp3 / flac / ogg / m4a。</div></details>

<details class="faq"><summary>AI 试听为何不能只生成 30 秒？</summary>
  <div>平台不提供「时长」参数，时长由提示词与歌词决定。当前实现为生成整首、仅播放前 30 秒，
  因此<b>按整首计费</b>。同一模板试听一次后缓存在本机，再次播放不产生费用。</div></details>

<details class="faq"><summary>「试听」是否产生费用？</summary>
  <div>产生费用，每次为一条整首（百炼约 ¥0.4 / 三分钟；Mureka 与腾讯云 ¥0.33/首）。
  首次试听会弹出确认，同一会话内不再逐条询问。<b>「试跑（免费）」完全在本机运行，不联网、不产生费用。</b></div></details>

<details class="faq"><summary>录音是否会被上传？</summary>
  <div>不会。切块、修音、混音、导出的全部计算均在本机完成。
  AI 生成仅发送<b>风格说明与歌词</b>；音色补音仅发送<b>选中的片段</b>至您自行配置的服务器。</div></details>

<details class="faq"><summary>切出的块过碎 / 与文字不对齐</summary>
  <div>通常是因为使用了含伴奏的录音：伴奏会被人声检测识别为气声，导致边界不清晰。请使用清唱，
  或先执行一次人声分离。此外「切法」可在<b>按字 / 音节</b>与<b>按音</b>之间切换，
  后者在长音较多时更稳定。</div></details>

<details class="faq"><summary>「跟原唱」提示无法对齐 / 未给出方案</summary>
  <div>两条声音的节拍或速度不同，自动对齐找不到共同点。请先将两条轨按 BPM 大致对齐，再执行分析；
  无法对齐时界面会明确提示，不会在未经确认的情况下修改音高。</div></details>

<details class="faq"><summary>VST 插件无声音 / 持续加载</summary>
  <div>VST3 无法实时运行，需先「冻结」该轨再试听。冻结在后台独立进程中离线渲染；
  未安装 <code>pedalboard</code> 或插件扫描失败时，本页会说明缺失项。</div></details>

<details class="faq"><summary>导出文件位于何处？</summary>
  <div><code>projects/exports/</code>。分轨是一个 ZIP，里面每条轨一个 wav；导出面板里也有直达链接。</div></details>
`;

const SET_KEYS = `
<h3>快捷键速查</h3>
<p>光标停在输入框 / 下拉框里时，除了 <code>Esc</code>，快捷键都不生效。</p>
<div class="setkeys">
  <div class="setkey"><kbd><i>空格</i></kbd><span>播放 / 暂停</span></div>
  <div class="setkey"><kbd><i>Delete</i><i>Backspace</i></kbd><span>删除选中的块（原位留空）</span></div>
  <div class="setkey"><kbd><i>Ctrl+Z</i><i>Ctrl+Shift+Z</i></kbd><span>撤销 / 重做（也认 <i>Ctrl+Y</i>）</span></div>
  <div class="setkey"><kbd><i>Ctrl+S</i></kbd><span>立即保存</span></div>
  <div class="setkey"><kbd><i>Ctrl+C</i><i>X</i><i>V</i></kbd><span>复制 / 剪切 / 粘贴到播放头</span></div>
  <div class="setkey"><kbd><i>Ctrl+D</i></kbd><span>原地复制一份</span></div>
  <div class="setkey"><kbd><i>Ctrl+A</i></kbd><span>全选所有块</span></div>
  <div class="setkey"><kbd><i>S</i></kbd><span>在播放头切分选中的块</span></div>
  <div class="setkey"><kbd><i>M</i></kbd><span>在播放头加一个标记</span></div>
  <div class="setkey"><kbd><i>←</i><i>→</i></kbd><span>有选中块：微调位置（默认网格 / <i>Shift</i> 10ms / <i>Alt</i> 1ms）；没选中：挪播放头</span></div>
  <div class="setkey"><kbd><i>Home</i><i>End</i></kbd><span>回开头 / 跳到结尾</span></div>
  <div class="setkey"><kbd><i>Esc</i></kbd><span>取消选择；整页编辑开着时先关整页编辑</span></div>
  <div class="setkey"><kbd><i>滚轮</i></kbd><span>上下滚动轨道</span></div>
  <div class="setkey"><kbd><i>Shift</i> + <i>滚轮</i></kbd><span>横向滚动时间轴</span></div>
  <div class="setkey"><kbd><i>Ctrl</i> + <i>滚轮</i></kbd><span>以鼠标位置为锚点缩放</span></div>
  <div class="setkey"><kbd>音高视图 上下拖</kbd><span>改音高（吸附到半音）</span></div>
  <div class="setkey"><kbd>音高视图 <i>Alt</i> + 上下拖</kbd><span>按音分微调（0.1¢）</span></div>
  <div class="setkey"><kbd>导出时按住 <i>Alt</i></kbd><span>混音导 MP3 320k / 分轨用插件链之后</span></div>
</div>
`;

const SET_LOG = `
<h3>更新日志</h3>
<h4>v1.0.0 · 2026-10-22</h4>
<ul>
  <li>新增<b>新手引导</b>：第一次打开自动走四步，之后在「设置 → 使用手册」随时重看。</li>
  <li>新增<b>设置中心</b>：使用手册 / 常见问题 / 快捷键 / 联系 / 更新日志 / 关于。</li>
  <li>AI 生成面板：所有旁注收进单独一页「说明」，控件旁边不再堆解释。</li>
</ul>
<h4>v0.9 · 2026-10-21</h4>
<ul>
  <li><b>组合模板库 65 条</b>(12 个分类)，可搜索，可试听前 30 秒，试听结果缓存在本机。</li>
  <li><b>参数化歌曲结构</b>：前奏秒数 / 主歌段数 / 副歌遍数 / 间奏 / 尾奏，改一个数就重算小节。</li>
  <li>歌词段落标签扩充：说唱 / 故事 / 耳语 / 现场 / 独奏；人声特效与节奏标签分组。</li>
</ul>
<h4>v0.8 · 2026-10-20</h4>
<ul>
  <li><b>按字 / 音节切块</b>，可选语音识别给块标上字词。</li>
  <li><b>个人音色补音</b>：把一段音延长 / 缩短，本机 GPU 推理，失败自动退回本地补音。</li>
  <li><b>自然语言调音</b>：以自然语言描述听感，由 DeepSeek 生成方案，确认勾选后方可应用。</li>
</ul>
<h4>更早</h4>
<ul>
  <li>鼓机、VST3 离线渲染与冻结、分轨导出、LUFS 母带、每轨效果链、和弦检测、跟原唱矫正。</li>
</ul>
`;

function setAboutHtml(a) {
  const rows = [
    ['开源协议', a.license
      ? `<a href="${escHtml(a.licenseUrl || '#')}" target="_blank" rel="noopener">${escHtml(a.license)}</a>`
      : '…'],
    ['开发者', escHtml(a.developer || '…')],
    ['版权', `© ${escHtml(a.copyright || '…')} ${escHtml(a.developer || '')}`],
    /* ★ 运行环境只显示操作系统 —— Python 版本和端口号是实现细节,不该出现在产品里 */
    ['运行环境', escHtml(a.platform || '…')],
    ['源码', a.contactGithub
      ? `<a href="${escHtml(a.contactGithub)}" target="_blank" rel="noopener">${escHtml(a.contactGithub)}</a>`
      : '…'],
  ];
  const tp = (a.thirdParty || []).map(t =>
    `<li><b>${escHtml(t.name)}</b> —— ${escHtml(t.use)};
      <a href="${escHtml(t.url)}" target="_blank" rel="noopener">${escHtml(t.license)}</a></li>`).join('');
  return `
<h3>${escHtml(a.editionName || '声麦 Studio Pro')} <span class="muted" style="font-size:12px;font-weight:400">SingMic</span></h3>
<div class="setabout-ver"><b>${escHtml(a.version || '…')}</b>
  <span>${a.versionDate ? t('发布于') + ' ' + escHtml(a.versionDate) : ''}</span></div>
<div class="setrows">${rows.map(([k, v]) =>
    `<div class="setrow"><b>${k}</b><span>${v}</span></div>`).join('')}</div>
<h4>第三方组件</h4>
<ul>${tp || '<li>…</li>'}</ul>
<h4>隐私</h4>
<p>录音、工程、导出的成品都只存在这台电脑上。云端仅在主动执行「生成 / 试听 / 补音」时参与，
发送内容见「使用手册 · 什么会上传」。</p>
<p class="settip">SingMic 以 ${escHtml(a.license || 'GPL-3.0')} 授权：自己用、改、再传都行；
改过的版本再发布时也要按同一协议开源。</p>
`;
}

function setContactHtml(a) {
  const mail = a.contactEmail
    ? `<a href="mailto:${escHtml(a.contactEmail)}">${escHtml(a.contactEmail)}</a>`
    : '<span class="muted">未公开（可在 Microsoft Store 应用页面留言）</span>';
  return `
<h3>联系与反馈</h3>
<div class="setrows">
  <div class="setrow"><b>邮箱</b><span>${mail}</span></div>
  <div class="setrow"><b>GitHub</b><span>${a.contactGithub
    ? `<a href="${escHtml(a.contactGithub)}" target="_blank" rel="noopener">${escHtml(a.contactGithub)}</a>`
    : '…'}</span></div>
  <div class="setrow"><b>版本</b><span>${escHtml(a.version || '…')}${a.versionDate ? ' · ' + escHtml(a.versionDate) : ''}</span></div>
</div>
<p class="settip">提交问题时请附上以下信息，可显著减少沟通成本：<b>版本号、操作系统、复现步骤、控制台报错</b>。
  版本号可在「关于声麦 Studio Pro」页面查看。</p>
<p>功能建议、听感问题、界面说明不清等，均可作为反馈提交。</p>
`;
}

async function setAbout() {
  if (SET.about || SET.aboutErr) return SET.about || {};
  try { SET.about = await api('/about'); }
  catch { SET.aboutErr = true; SET.about = {}; }
  return SET.about;
}

// 这三页的标题是固定的，不必等后端 —— 先挂标题再填内容
const SET_HEAD = { data: '数据与缓存', about: '关于', contact: '联系与反馈' };

function setShow(sec) {
  SET.sec = sec || 'prefs';
  const my = ++SET_SEQ;
  for (const b of document.querySelectorAll('#setNav .setitem')) {
    b.classList.toggle('on', b.dataset.sec === SET.sec);
  }
  const body = $('setBody');
  if (!body) return;
  if (SET.sec === 'prefs') body.innerHTML = setPrefsHtml();
  else if (SET.sec === 'manual') body.innerHTML = SET_MANUAL;
  else if (SET.sec === 'faq') body.innerHTML = SET_FAQ;
  else if (SET.sec === 'keys') body.innerHTML = SET_KEYS;
  else if (SET.sec === 'log') body.innerHTML = SET_LOG;
  else {
    // 联系 / 关于 / 数据：等后端把版号、路径拿回来。★ 期间切了页就别盖回去
    // ★ 标题要**先**出来再等数：缓存那页要数两千多个文件，以前是一整页空白一秒多
    body.innerHTML = `<h3>${SET_HEAD[SET.sec] || '读取中'}</h3>`
      + '<p class="setnote">读取中…</p>';
    const which = SET.sec;
    const job = which === 'data' ? setDataWait()
      : setAbout().then((a) => (which === 'about' ? setAboutHtml(a) : setContactHtml(a)));
    job.then((html) => { if (my === SET_SEQ) body.innerHTML = html; })
       .catch(() => { if (my === SET_SEQ) body.innerHTML = '<p>读取失败，稍后重试</p>'; });
  }
  body.scrollTop = 0;
}

function setOpen(sec) {
  const el = $('settings');
  if (!el) return;
  el.hidden = false;
  setShow(sec || SET.sec);
}

function setClose() {
  if ($('settings')) $('settings').hidden = true;
}

$('btnSettings')?.addEventListener('click', () => setOpen());
$('setClose')?.addEventListener('click', setClose);
$('setNav')?.addEventListener('click', (e) => {
  const b = e.target.closest('.setitem');
  if (b) setShow(b.dataset.sec);
});
$('setBody')?.addEventListener('click', async (e) => {
  if (e.target.closest('#setGuide')) { setClose(); obShow(0); return; }
  if (e.target.closest('#setCacheClean')) {
    const c = SET.cache;
    if (!c) { toast('正在读取缓存信息，请稍候', 1800); return; }
    if (!c.orphan) { toast(`缓存 ${c.mb}MB，都是还在用的，没得清`); return; }
    if (!(await uiConfirm(`清理 ${c.orphan} 个未被引用的片段缓存？\n`
      + '仅删除未被工程引用的片段；再次使用时将重新生成，不会丢失工程数据。'))) return;
    try {
      const r = await api('/cache_clean', { method: 'POST', json: {} });
      toast(`清掉 ${r.removed} 个 · 腾出 ${r.freed_mb}MB`);
      engine.rendered && engine.rendered.clear && engine.rendered.clear();
      cacheInfo();
      setShow('data');                    // 重画这一页，数字跟着变
    } catch (err) { toast('清理失败：' + err.message, 8000); }
  }
});
$('setBody')?.addEventListener('change', (e) => {
  const t = e.target;
  if (!t || !t.id) return;
  if (t.id === 'prefStage') prefsSet('stage', t.value);
  else if (t.id === 'prefAutosave') prefsSet('autosave', !!t.checked);
  else if (t.id === 'prefConfirmCost') prefsSet('confirmCost', !!t.checked);
  else if (t.id === 'prefGuide') prefsSet('guide', !!t.checked);
  else if (t.id === 'prefLang') {
    prefsSet('lang', t.value);
    // 选「跟随系统」时不写入具体语言，下次换机器仍然跟着那台机器的系统走
    (t.value ? setLang(t.value) : setLang(detectLang())).catch(() => {});
  }
});
document.addEventListener('keydown', (e) => {
  if (!$('settings') || $('settings').hidden) return;
  if (e.key === 'Escape') { setClose(); e.preventDefault(); }
});

// ------------------------------------------------------------ 面板外壳：点外面就收 + 科技边框
/* ★ 他原话：「点个控件，得点控件的叉掉才能退，有点麻烦」—— 所以点面板外面任意地方都算关掉。
   做法：给这些面板配一层全屏 scrim(z 比面板小 1),scrim 被点一下就**替用户点那个 ×**。
   为什么不直接 `.hidden = true`：每个面板收的时候要干的活儿不一样（AI 伴奏要 genStop()、
   块编辑要落笔…)，点 × 走的是它自己那条路，不会漏掉清理。
   ★ 用 `click` 不用 `pointerdown`:pointerdown 就把 scrim 藏了，松手那一下的 click
     会穿到下面的时间轴上，「关面板」顺手点到别的东西。 */
const FX_PANELS = [
  ['autoPanel', 'autoClose'], ['refPanel', 'refClose'], ['voicePanel', 'voiceClose'],
  ['extPanel', 'extClose'], ['nlPanel', 'nlClose'], ['genPanel', 'genClose'],
  ['genLib', 'libClose'], ['drumPanel', 'drumClose'], ['expPanel', 'expClose'],
  ['settings', 'setClose'],
];
let fxScrim = null;

/* ---------------------------------------------------------- 面板里的粒子流
   ★ 他 2026-10-22 明确：「粒子流在控件板**里面**，不是全部页面」。
     所以粒子层是每块面板自己的一层 canvas，贴在**面板背景之上、内容之下**:
     面板是 fixed + z-index ⇒ 自成层叠上下文，孩子里的 z-index:-1 正好落在
     「面板底色之上、正文之下」—— 字照样看得清，粒子在字后面流。
   ★ 一块面板一层，各流各的；只有**看得见**的那几块才跑 rAF，全关掉就停。 */
const FXFLOW = new Map();          // 面板元素 -> {cv, ctx, ps, w, h, dpr}
let fxFlowRaf = 0;

function fxFlowInit(el) {
  let ent = FXFLOW.get(el);
  if (ent) return ent;
  const cv = document.createElement('canvas');
  cv.className = 'fxflow';
  el.insertBefore(cv, el.firstChild);
  ent = { cv, ctx: cv.getContext('2d'), ps: [], w: 0, h: 0, dpr: 0 };
  FXFLOW.set(el, ent);
  return ent;
}

function fxFlowFit(ent, el) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = el.clientWidth, h = el.clientHeight;
  if (!w || !h) return false;
  if (w === ent.w && h === ent.h && dpr === ent.dpr) return true;
  const ox = ent.w ? w / ent.w : 1, oy = ent.h ? h / ent.h : 1;
  ent.w = w; ent.h = h; ent.dpr = dpr;
  ent.cv.width = Math.round(w * dpr);
  ent.cv.height = Math.round(h * dpr);
  ent.cv.style.width = w + 'px';
  ent.cv.style.height = h + 'px';
  ent.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  for (const p of ent.ps) { p.x *= ox; p.y *= oy; }   // 改尺寸时按比例搬家，不挤成一堆
  return true;
}

function fxFlowSeed(ent) {
  if (ent.ps.length) return;
  // ★ 他的原话：「极其微弱，不然喧宾夺主」—— 密度、亮度、粗细全都压到最低档，
  //   只在空白处看得见一点流向，绝不去抢字的注意力。
  const n = Math.round(Math.min(120, Math.max(30, ent.w * ent.h / 16000)));
  for (let i = 0; i < n; i++) ent.ps.push({
    x: Math.random() * ent.w, y: Math.random() * ent.h,
    r: Math.random() * .7 + .4,
    vx: Math.random() * .26 + .05,                    // 整体往右慢慢流
    vy: (Math.random() - .5) * .10,
    ph: Math.random() * 6.283,
    a: Math.random() * .13 + .09,
    h: Math.random() < .72 ? 196 : 262,               // 冰蓝为主，少量紫
  });
}

function fxFlowTick(t) {
  let any = false;
  for (const [el, ent] of FXFLOW) {
    if (el.hidden || !fxFlowFit(ent, el)) continue;
    any = true;
    fxFlowSeed(ent);
    const { ctx, w, h } = ent;
    /* 短拖尾：不清屏，而是每帧擦掉一点 —— 点就成了小光迹，像水在流 */
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillStyle = 'rgba(0,0,0,.22)';
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'source-over';   // 不做加法发光，叠起来才不会抢眼
    for (const p of ent.ps) {
      p.x += p.vx * 16.7; p.y += (p.vy + Math.sin(t / 1500 + p.ph) * .14) * 16.7;
      if (p.x > w + 10) { p.x = -10; p.y = Math.random() * h; }
      if (p.y < -10) p.y = h + 10;
      else if (p.y > h + 10) p.y = -10;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.2832);
      ctx.fillStyle = `hsla(${p.h},100%,80%,${p.a})`;
      ctx.shadowBlur = 4; ctx.shadowColor = `hsla(${p.h},100%,70%,.35)`;
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }
  fxFlowRaf = any ? requestAnimationFrame(fxFlowTick) : 0;
}

function fxFlowSync(on) {
  if (!on || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    if (fxFlowRaf) { cancelAnimationFrame(fxFlowRaf); fxFlowRaf = 0; }
    for (const ent of FXFLOW.values()) ent.ctx.clearRect(0, 0, ent.w, ent.h);
    return;
  }
  if (!fxFlowRaf) fxFlowRaf = requestAnimationFrame(fxFlowTick);
}

function fxLive() {
  return FX_PANELS
    .map(([id, btn]) => {
      const el = $(id);
      if (!el || el.hidden) return null;
      return { id, btn, z: parseInt(getComputedStyle(el).zIndex, 10) || 60 };
    })
    .filter(Boolean)
    .sort((a, b) => a.z - b.z);        // 大的在后面 = 盖在上面的那个
}

function fxSync() {
  if (!fxScrim) return;
  const vis = fxLive();
  if (!vis.length) { fxScrim.hidden = true; fxFlowSync(false); return; }
  fxScrim.style.zIndex = String(Math.max(1, vis[vis.length - 1].z - 1));
  fxScrim.hidden = false;
  fxFlowSync(true);
}

function fxTopClose() {
  const vis = fxLive();
  if (!vis.length) return;
  const b = $(vis[vis.length - 1].btn);
  if (b) b.click();
  else fxSync();
}

(() => {
  fxScrim = document.createElement('div');
  fxScrim.id = 'fxScrim';
  fxScrim.hidden = true;
  document.body.appendChild(fxScrim);
  fxScrim.addEventListener('click', (e) => { e.stopPropagation(); fxTopClose(); });
  const obs = new MutationObserver(fxSync);
  for (const [id] of FX_PANELS) {
    const el = $(id);
    if (!el) continue;
    el.classList.add('fxpanel');
    fxFlowInit(el);                              // 面板里的粒子流（背景之上、内容之下）
    const scan = document.createElement('i');    // 那条从上往下扫的光带
    scan.className = 'fxscan';
    el.appendChild(scan);
    const corner = document.createElement('i');  // 四角 L 括号
    corner.className = 'fxcorner';
    el.appendChild(corner);
    obs.observe(el, { attributes: true, attributeFilter: ['hidden'] });
  }
  // 引导卡：点背景 = 跳过（和「跳过引导」同一条路）；点卡片本体不算
  $('onboard')?.addEventListener('click', (e) => { if (e.target === $('onboard')) obClose(); });
  // AI 伴奏里的「说明」浮层：点它自己的背景关掉，不连累外面的面板
  $('genHelp')?.addEventListener('click', (e) => {
    if (e.target === $('genHelp')) $('genHelpClose')?.click();
  });
})();

/** 面板上的「常用」模板：同样来自 PRESETS(quick:1)，不在这儿重复写一遍 */
function genQuickPresets() {
  const box = $('genPresets');
  if (!box) return;
  box.innerHTML = PRESETS.filter(p => p.quick).map((p) =>
    `<button class="gchip preset" data-preset="${p.id}"
      title="${escHtml(p.brief.slice(0, 90))}">${escHtml(p.name)}</button>`).join('');
}
genQuickPresets();

// 风格说明 / 歌词：每敲一个字都更新字数，风格里的 bpm 还会带着「预计时长」走
$('genPrompt')?.addEventListener('input', () => { genCounts(); genRenderForm(); });
$('genLyrics')?.addEventListener('input', genCounts);
$('genGender')?.addEventListener('change', genCost);
// 组合模板：**整套替换**（它本来就是一条完整的风格说明，接在后面会自相矛盾）。
// 已经有内容时先问一句 —— 那是他自己打的字，不能一声不响盖掉。
$('genPresets')?.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-preset]');
  const ta = $('genPrompt');
  if (!b || !ta) return;
  const p = PRESETS.find(x => x.id === b.dataset.preset);
  if (!p) return;
  if (ta.value.trim() && ta.value.trim() !== p.brief
      && !(await uiConfirm('组合模板将替换当前风格说明，是否继续？', { danger: true }))) return;
  ta.value = p.brief;
  ta.focus();
  genCounts(); genRenderForm();
  toast('组合模板已填入，可继续调整 BPM、乐器与情绪', 2600);
});

// 单项标签（流派 / 情绪 / 乐器 / 人声特效 / 节奏）:**接在后面**，空框就直接填
['genStyles', 'genMoods', 'genInstrs', 'genVocals', 'genGrooves'].forEach((id) => {
  $(id)?.addEventListener('click', (e) => {
    const b = e.target.closest('.gchip');
    const ta = $('genPrompt');
    if (!b || !ta) return;
    const s = b.dataset.style || '';
    const cur = ta.value.trim();
    // 已经写过的词不重复接（点两下不该出现两个「温暖」）
    if (s && cur.includes(s)) return;
    ta.value = cur ? (cur.replace(/[,，\s]+$/, '') + ', ' + s) : s;
    ta.focus();
    genCounts(); genRenderForm();
  });
});
// 结构：换模板 / 改自定义 / 点「套用到歌词」/ 点段落芯片
$('genForm')?.addEventListener('change', () => {
  const custom = $('genForm')?.value === 'custom';
  if ($('genFormParams')) $('genFormParams').hidden = !custom;
  if ($('genFormCustom')) $('genFormCustom').hidden = !custom;
  genRenderForm();
});
$('genFormCustom')?.addEventListener('input', genRenderForm);
// 参数化结构：改哪个数字结构就当场重排
['gpIntro', 'gpVerse', 'gpChorus', 'gpOutro'].forEach((id) => {
  $(id)?.addEventListener('input', genRenderForm);
});
$('gpInter')?.addEventListener('change', genRenderForm);
$('genFormApply')?.addEventListener('click', genApplyForm);
['genFormChips', 'genFxChips'].forEach((id) => {
  $(id)?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-sec]');
    if (b) genInsertSection(b.dataset.sec);
  });
});
$('genLyricsClear')?.addEventListener('click', async () => {
  const ta = $('genLyrics');
  if (!ta || !ta.value) return;
  if (!(await uiConfirm('清空歌词？', { danger: true, okText: '清空' }))) return;
  ta.value = ''; genCounts(); ta.focus();
});
$('genVocal')?.addEventListener('change', (e) => {
  const lyr = $('genLyrics');
  if (lyr) {
    lyr.disabled = !e.target.checked;
    lyr.placeholder = e.target.checked
      ? t('每行一句；点击上方的结构芯片可插入 [Verse 1] 等段落标签')
      : t('仅在勾选「带 AI 人声」时需要');
  }
  genSyncForm();
});

// ---------------------------------------------------------------- 鼓机
// ★ 鼓轨和采样轨是**同一个数据结构**：一个 midi 号对应一个 wav。
//   鼓组样本由服务端按内容哈希写进 cache，浏览器播它、导出也读它。
async function addDrumTrack(kit = 'phonk') {
  try {
    const r = await api(`/kit/${kit}/pool`);
    if (!r?.pool?.length) throw new Error(t('这个鼓组是空的'));
    takeSnap();
    const tr = newTrack('drum', `${t('鼓')} · ${(r.name || kit).toUpperCase()}`);
    tr.kit = r.kit || kit;
    tr.sample = { baseMidi: r.baseMidi, poolLo: r.poolLo, poolHi: r.poolHi, pool: r.pool };
    tr.attack = r.attack ?? 0.002;
    tr.release = r.release ?? 0.05;
    tr.notes = [];
    S.rollTrack = tr.id;
    S.selTrack = tr.id;
    afterStructure();
    document.querySelector('.tabs button[data-tab="roll"]')?.click();
    rollScrollTo(Math.round(r.baseMidi) + 6);
    setSaveState(t('鼓组 {0} · 在卷帘里点格子放鼓，左边一行一件').replace('{0}', r.name));
    $('drumPanel').hidden = true;
    toast(t('鼓组「{0}」已就位 · 在卷帘里点格子放鼓').replace('{0}', r.name), 4200);
    return tr;
  } catch (e) { toast('加鼓轨失败：' + e.message, 5000); }
}

function drumSay(html) {
  const el = $('drumState');
  if (el) el.innerHTML = html;
}

async function drumLoadList() {
  const box = $('drumList');
  if (!box) return;
  box.innerHTML = `<div class="inote">${escHtml(t('读鼓组…'))}</div>`;
  try {
    const r = await api('/kit/list');
    const kits = r?.kits || [];
    box.innerHTML = kits.length ? kits.map(k => `<div class="mrow">
        <span class="mname">${escHtml(k.name)}</span>
        <span class="inote">${escHtml(t('{0} 件').replace('{0}', k.count))}</span>
        <span class="grow"></span>
        <button class="mini" data-kit="${escHtml(k.id)}">加一条轨</button>
      </div>`).join('')
      : '<div class="inote">还没有鼓组 —— 底下填个文件夹路径导入一个</div>';
    box.querySelectorAll('[data-kit]').forEach(b => {
      b.addEventListener('click', () => addDrumTrack(b.dataset.kit));
    });
  } catch (e) {
    box.innerHTML = `<div class="inote">${escHtml(t('读不到鼓组：{0}').replace('{0}', e.message))}</div>`;
  }
}

function openDrumPanel() {
  const p = $('drumPanel');
  if (!p) return;
  p.hidden = false;
  drumLoadList();
}

$('btnDrum')?.addEventListener('click', openDrumPanel);
$('drumClose')?.addEventListener('click', () => { $('drumPanel').hidden = true; });
$('drumImport')?.addEventListener('click', async () => {
  const dir = ($('drumDir')?.value || '').trim();
  if (!dir) { drumSay('<span class="inote">先填一个文件夹路径</span>'); return; }
  drumSay('<span class="inote">导入中…</span>');
  try {
    const r = await api('/kit/import', { method: 'POST', json: {
      dir, name: ($('drumName')?.value || '').trim(),
      title: ($('drumName')?.value || '').trim() } });
    const miss = (r?.skipped || []).length ? ` · 没认出来：${r.skipped.join('、')}` : '';
    drumSay(`<span class="inote">导入 ${r.count} 件${miss}</span>`);
    await drumLoadList();
  } catch (e) {
    drumSay(`<span class="inote">导入失败：${escHtml(e.message)}</span>`);
  }
});

// 上传
$('btnImport').addEventListener('click', () => $('fileImport').click());
$('fileImport').addEventListener('change', async (e) => {
  const f = e.target.files?.[0];
  e.target.value = '';
  if (!f) return;
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  if (!['wav', 'mp3', 'flac', 'ogg'].includes(ext)) {
    uiAlert('只收 wav / mp3 / flac / ogg（m4a 导不出来，不收）');
    return;
  }
  const old = $('btnImport').textContent;
  $('btnImport').textContent = '传…';
  try {
    const r = await fetch('/api/studio/upload?name=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f,
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    await loadMedia();
    setSaveState('导入好了');
    setTimeout(() => setSaveState(S.dirty ? '未保存…' : '已保存'), 1600);
  } catch (err) {
    uiAlert('导入失败：' + err.message);
  } finally {
    $('btnImport').textContent = old;
  }
});

// ---------------------------------------------------------------- 块内音高曲线
// ★ 曲线存 [[t 相对 srcIn, 音分], …]；渲染 key 里带曲线指纹，改一点就换一份。
const CURVE_RANGE = 120;            // 显示 ±120 音分
const CVW = 200, CVH = 122;         // 检查器宽 232px:200 + 左右各 10 + 边框

function curvePts(clip) {
  if (Array.isArray(clip.pitchCurve) && clip.pitchCurve.length) return clip.pitchCurve;
  const base = (clip.semis || 0) * 100 + (clip.cents || 0);
  return base ? [[0, base], [Math.max(0.01, clip.length), base]] : [];
}

// ---------------------------------------------------------------- 音高 / 曲线 helper
// ★ 一个 clip = 一个音。`f0` 是切块时测到的基频（Hz）；旧工程没有就按 note、
//   再没有按 C4 画 —— 只是显示，不改数据。
// ★ 曲线是**修正量**(±200 音分硬顶，后端同样夹)；非空时 semis/cents 必须是 0,
//   因为渲染端有曲线就完全不看常数。所有写曲线的操作都顺手把常数清零。
const PITCH_CAP = 200;
const PIANO_W = 56;                  // 整页音高视图左侧钢琴条宽度
const SOUND_BAND_H = 26;             // 底部给气声留的一条

function hzMidi(hz) { return 69 + 12 * Math.log2(hz / 440); }

function clipHasPitch(clip) {
  if (!clip) return false;
  if (Array.isArray(clip.pitchCurve) && clip.pitchCurve.length) return true;
  return !!(clip.semis || clip.cents);
}

// 这个音**本来**唱在哪个音高（MIDI，含小数）
function clipBaseMidi(clip) {
  const f = Number(clip?.f0);
  if (f > 0 && isFinite(f)) return hzMidi(f);
  const path = String(S.proj?.sources?.[clip?.sourceId]?.path || '').replace(/\\/g, '/');
  const m = noteMidiOf(clip, path);
  return (m != null && isFinite(m)) ? m : 60;
}

// 曲线在 t（相对 srcIn 的源秒）处的修正值：线性插值，两端按端点外推
function curveValueAt(pts, t) {
  if (!Array.isArray(pts) || !pts.length) return 0;
  const n = pts.length;
  if (t <= pts[0][0]) return +pts[0][1] || 0;
  if (t >= pts[n - 1][0]) return +pts[n - 1][1] || 0;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (pts[m][0] <= t) lo = m; else hi = m;
  }
  const t0 = +pts[lo][0], c0 = +pts[lo][1];
  const t1 = +pts[hi][0], c1 = +pts[hi][1];
  const k = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
  return c0 + (c1 - c0) * k;
}

// 当前整体偏移（音分）：有曲线取中位数，没有就是常数
function pitchOffsetOf(clip) {
  if (Array.isArray(clip?.pitchCurve) && clip.pitchCurve.length) {
    const a = clip.pitchCurve.map(p => +p[1] || 0).sort((x, y) => x - y);
    return a[a.length >> 1];
  }
  return (clip?.semis || 0) * 100 + (clip?.cents || 0);
}

// 偏移动态拆成「半音 + 音分」：输入框显示用
function pitchSplit(clip) {
  const t = pitchOffsetOf(clip);
  const s = Math.trunc(t / 100);
  return { semis: s, cents: +(t - s * 100).toFixed(1) };
}

// 现在实际听起来在哪（MIDI）
function clipSoundingMidi(clip) {
  return clipBaseMidi(clip) + pitchOffsetOf(clip) / 100;
}

function curveSnapshot(clip) {
  return Array.isArray(clip?.pitchCurve) && clip.pitchCurve.length
    ? clip.pitchCurve.map(p => [+p[0], +p[1]]) : null;
}

function curveShift(pts, d) {
  if (!Array.isArray(pts) || !pts.length) return [];
  if (!d) return pts.map(p => [+p[0], +p[1]]);
  return pts.map(([t, c]) => [+t, clamp(+c + d, -PITCH_CAP, PITCH_CAP)]);
}

// 取曲线 [a, b]（源秒，相对旧 srcIn）这一段，重标到从 0 起 —— 切分 / 裁剪 / 改 srcIn 用
function curveRange(pts, a, b) {
  if (!Array.isArray(pts) || !pts.length) return null;
  const lo = Math.min(a, b), hi = Math.max(a, b);
  if (!(hi - lo > 1e-4)) return null;
  const out = [[0, +curveValueAt(pts, lo).toFixed(1)]];
  for (const [t, c] of pts) {
    if (+t > lo + 1e-4 && +t < hi - 1e-4) out.push([+(+t - lo).toFixed(3), +c]);
  }
  out.push([+(hi - lo).toFixed(3), +curveValueAt(pts, hi).toFixed(1)]);
  return out;
}

// 等距抽稀到 ≤maxN 点（首尾一定保留；后端最多收 4000 点）
function simplifyCurve(pts, maxN = 800) {
  const n = Array.isArray(pts) ? pts.length : 0;
  if (n <= maxN) return (pts || []).slice();
  const out = [pts[0]];
  const step = (n - 1) / (maxN - 1);
  for (let i = 1; i < maxN - 1; i++) out.push(pts[Math.round(i * step)]);
  out.push(pts[n - 1]);
  return out;
}

// 清掉这一块的渲染缓存；只有真的带着音高才标「待渲染」
function clipInvalidatePitchRender(clip) {
  clip.renderKey = null;
  clip.renderUrl = null;
  clip.needsRender = clipHasPitch(clip);
  return clip.needsRender;
}

// 把「整体偏移」写成绝对值。有曲线时 = 平移曲线（不然常数和曲线打架，渲染只认曲线）
function setPitchOffset(clip, totalCents) {
  if (Array.isArray(clip.pitchCurve) && clip.pitchCurve.length) {
    const d = totalCents - pitchOffsetOf(clip);
    clip.pitchCurve = curveShift(clip.pitchCurve, d);
    clip.semis = 0; clip.cents = 0;
  } else {
    const t = clamp(totalCents, -2400, 2400);
    const s = Math.trunc(t / 100);
    clip.semis = s;
    clip.cents = +(t - s * 100).toFixed(1);
  }
  clipInvalidatePitchRender(clip);
  clip.needsRender = true;
  return clip;
}

// 常数折成一条平曲线（笔模式第一次落笔 / 拉平用）
function ensureCurveForPen(clip) {
  if (Array.isArray(clip.pitchCurve) && clip.pitchCurve.length) return clip.pitchCurve;
  const span = Math.max(0.01, (clip.srcOut - clip.srcIn) || clip.length || 0.01);
  const base = (clip.semis || 0) * 100 + (clip.cents || 0);
  clip.pitchCurve = [[0, base], [+span.toFixed(3), base]];
  clip.semis = 0; clip.cents = 0;
  return clip.pitchCurve;
}

function setupCurveEditor(cv, clip) {
  if (!cv) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  cv.width = CVW * dpr; cv.height = CVH * dpr;
  cv.style.width = CVW + 'px'; cv.style.height = CVH + 'px';
  const g = cv.getContext('2d');
  const span = Math.max(0.01, clip.length);
  const X = (t) => (t / span) * CVW;
  const Y = (c) => CVH / 2 - (c / CURVE_RANGE) * (CVH / 2 - 3);
  const T = (x) => clamp(x / CVW, 0, 1) * span;
  const C = (y) => clamp((CVH / 2 - y) / (CVH / 2 - 3) * CURVE_RANGE, -200, 200);

  function draw() {
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, CVW, CVH);
    g.fillStyle = '#0a0d13'; g.fillRect(0, 0, CVW, CVH);
    for (const c of [-100, -50, 0, 50, 100]) {
      g.strokeStyle = c === 0 ? '#2e3a4f' : '#161d29';
      g.beginPath(); g.moveTo(0, Y(c) + .5); g.lineTo(CVW, Y(c) + .5); g.stroke();
    }
    // ★ 以前这块一个字都没有，他只能猜：左边标「改多少音分」，底边标「块里第几秒」
    g.font = '9.5px Consolas,"Microsoft YaHei",sans-serif';
    g.textBaseline = 'middle';
    for (const [c, s] of [[CURVE_RANGE, '+120'], [-CURVE_RANGE, '-120']]) {
      g.fillStyle = '#3f4757';
      g.fillText(s, 4, c > 0 ? Y(c) + 8 : Y(c) - 8);
    }
    g.textBaseline = 'alphabetic';
    g.fillText('0s', 4, CVH - 4);
    g.textAlign = 'right';
    g.fillText(span.toFixed(span >= 10 ? 1 : 2) + 's', CVW - 4, CVH - 4);
    g.textAlign = 'left';
    const pts = curvePts(clip);
    if (!pts.length) {
      g.fillStyle = '#4a5468';
      g.font = '10px "Segoe UI","Microsoft YaHei",sans-serif';
      g.fillText(t('这块要改多少音分'), 6, CVH / 2 - 16);
      g.fillText(t('按住拖：左右 = 时间，上下 = 音分'), 6, CVH / 2 + 2);
      return;
    }
    g.fillStyle = '#5d708c';
    g.textAlign = 'right'; g.textBaseline = 'middle';
    g.fillText(t('0 = 原音高'), CVW - 4, Y(0) - 8);
    g.textAlign = 'left'; g.textBaseline = 'alphabetic';
    g.strokeStyle = '#ffb35c'; g.lineWidth = 1.6;
    g.beginPath();
    pts.forEach(([t, c], i) => { const x = X(t), y = Y(c); if (i) g.lineTo(x, y); else g.moveTo(x, y); });
    g.stroke();
    g.fillStyle = '#ffd6a3';
    for (const [t, c] of pts) g.fillRect(X(t) - 1.2, Y(c) - 1.2, 2.4, 2.4);
  }

  let drawing = false;
  function put(ev) {
    const r = cv.getBoundingClientRect();
    const x = clamp(ev.clientX - r.left, 0, CVW);
    const y = clamp(ev.clientY - r.top, 0, CVH);
    const t = +T(x).toFixed(3), c = +C(y).toFixed(1);
    if (!Array.isArray(clip.pitchCurve)) clip.pitchCurve = [];
    const pts = clip.pitchCurve;
    const i = pts.findIndex(p => Math.abs(p[0] - t) < 0.015);
    if (i >= 0) pts[i] = [t, c]; else pts.push([t, c]);
    pts.sort((a, b) => a[0] - b[0]);
    draw();
  }

  cv.onpointerdown = (e) => {
    e.preventDefault();
    try { cv.setPointerCapture(e.pointerId); } catch (_) {}
    // ★ 已经调过半音/音分的块：先把那个常数折进曲线，不然两个偏移会打架
    if (!Array.isArray(clip.pitchCurve) || !clip.pitchCurve.length) {
      const base = (clip.semis || 0) * 100 + (clip.cents || 0);
      clip.pitchCurve = base ? [[0, base], [+span.toFixed(3), base]] : [];
      clip.semis = 0; clip.cents = 0;
      takeSnap();
    }
    drawing = true;
    put(e);
  };
  cv.onpointermove = (e) => { if (drawing) put(e); };
  cv.onpointerup = (e) => {
    if (!drawing) return;
    drawing = false;
    try { cv.releasePointerCapture(e.pointerId); } catch (_) {}
    const pts = clip.pitchCurve || [];
    pts.sort((a, b) => a[0] - b[0]);
    if (pts.length && pts[pts.length - 1][0] < span - 1e-6) {
      pts.push([+span.toFixed(3), pts[pts.length - 1][1]]);   // 末端拉平到块尾
    }
    const nEl = $('iCurveN');
    if (nEl) nEl.textContent = pts.length + ' 点';
    markDirty();
    renderCurveClip(clip).catch(e => setSaveState('曲线渲染失败：' + e.message));
  };
  draw();
}

async function renderCurveClip(clip) {
  const src = S.proj.sources?.[clip.sourceId];
  if (!src) return null;
  const tok = ++S.renderTok;
  clip._tok = tok;
  const costEl = $('iCost');
  if (costEl) costEl.textContent = '按曲线渲染…';
  const r = await api('/render_curve', {
    method: 'POST',
    json: {
      source: src.path, srcIn: clip.srcIn, srcOut: clip.srcOut,
      outSec: clip.length, curve: clip.pitchCurve || [],
    },
  });
  if (clip._tok !== tok || !clipById(clip.id)) return null;
  clip.renderKey = r.key; clip.renderUrl = r.url;
  clip.needsRender = false;
  engine.stats.renders++;
  ensureRenderedPeaks(clip);
  if (costEl) costEl.textContent = `曲线渲染 ${r.sec_cost}s${r.cached ? '（缓存）' : ''}`;
  afterStructure();
  if (engine.playing) engine.play(S.proj, engine.songNow());
  return r;
}

// ---------------------------------------------------------------- 检查器
function renderInspector() {
  const body = $('inspBody');
  const sel = selectedClips();
  if (!sel.length) {
    body.className = 'inote';
    body.innerHTML = S.selTrack
      ? '选中了轨道。点一个块看详情。'
      : '选中一个块 · 拖到右边摆位置';
    return;
  }
  body.className = '';
  const { tr, clip } = sel[0];
  const src = S.proj.sources?.[clip.sourceId];
  const many = sel.length > 1;
  // ★ 有曲线时，这两个框显示的是「整体偏移」（曲线中位数），改它 = 整条曲线平移
  const poff = pitchSplit(clip);
  body.innerHTML = `
    <div class="irow"><label>轨道</label><span>${tr.name}</span></div>
    <div class="irow"><label>来源</label><span title="${src?.name || ''}">${shortName(src?.name || '缺失')}</span></div>
    <div class="irow"><label>类型</label><span class="itag">${clip.kind || 'note'}</span>${many ? `<span class="itag warn">${t('共 {0} 块').replace('{0}', sel.length)}</span>` : ''}</div>
    <div class="irow"><label>起点</label><input id="iStart" type="number" step="0.001" value="${fmt(clip.start, 3)}"><span class="v">s</span></div>
    <div class="irow"><label>长度</label><input id="iLen" type="number" step="0.001" value="${fmt(clip.length, 3)}"><span class="v">s</span></div>
    <div class="irow"><label>源入</label><input id="iIn" type="number" step="0.001" value="${fmt(clip.srcIn, 3)}"><span class="v">s</span></div>
    <div class="irow"><label>音高</label><input id="iSemis" type="number" step="1" min="-24" max="24" value="${poff.semis}"><span class="v">半音</span></div>
    <div class="irow"><label>音分</label><input id="iCents" type="number" step="5" min="-100" max="100" value="${poff.cents}"><span class="v">¢</span></div>
    <div class="irow"><label>${t('增益')}${many ? ` ${t('({0} 块)').replace('{0}', sel.length)}` : ''}</label><input id="iGain" type="range" min="0" max="2" step="0.01" value="${clip.gain ?? 1}"><span class="v" id="iGainV">${Math.round((clip.gain ?? 1) * 100)}%</span></div>
    ${many ? '<div class="inote" style="margin:-4px 0 6px">增益将同时应用于所有选中的块；其余字段仅修改第一个块。</div>' : ''}
    <div class="irow"><label>曲线</label><span class="v" id="iCurveN">${clip.pitchCurve?.length ? clip.pitchCurve.length + ' 点' : '无'}</span>
      <button id="iCurveClr" class="mini" title="清空曲线，回到原始音高">清空</button></div>
    <div class="ivhint">本块音高调整：横轴为块内时间（秒），纵轴为音分（向上为升高，0 为不改变）。
      按住拖动即可绘制曲线，松开后自动重新渲染。</div>
    <canvas id="iCurve"></canvas>
    <div class="ibtns">
      <button id="iPlay">试听</button>
      <button id="iExtend"${clip.kind === 'note' ? '' : ' disabled title="仅「音」块支持补音"'}>补音…</button>
      <button id="iDup">复制</button>
      <button id="iSamp">建采样乐器</button>
      <button id="iReset">恢复原样</button>
      <button id="iDel">删除</button>
    </div>
    <div class="inote" id="iCost"></div>
  `;
  const commit = (fn) => { takeSnap(); fn(); afterStructure(); };
  $('iStart').onchange = (e) => commit(() => {
    const v = Math.max(0, +e.target.value || 0);
    clip.start = v;
  });
  $('iLen').onchange = (e) => commit(() => {
    const v = Math.max(0.03, +e.target.value || 0);
    clip.length = v;
    if (clip.renderKey) { clip.renderKey = null; }
    clip.needsRender = true;
    renderPending();
  });
  $('iIn').onchange = (e) => {
    const v = Math.max(0, +e.target.value || 0);
    const oldCurve = curveSnapshot(clip);
    const oldOut = clip.srcOut;
    commit(() => {
      const d = v - clip.srcIn;
      clip.srcIn = v;
      // ★ 曲线是相对 srcIn 的：起点一动，整条曲线跟着反方向平移
      if (oldCurve) {
        const span = Math.max(0.01, oldOut - v);
        const r = curveRange(oldCurve, d, d + span);
        if (r) clip.pitchCurve = r; else delete clip.pitchCurve;
        clip.semis = 0; clip.cents = 0;
      }
      clipInvalidatePitchRender(clip);
    });
    renderPending();
  };
  $('iSemis').onchange = (e) => {
    const s = clamp(Math.round(+e.target.value || 0), -24, 24);
    const cur = pitchSplit(clip);
    commit(() => { setPitchOffset(clip, s * 100 + cur.cents); });
    renderPending();
  };
  $('iCents').onchange = (e) => {
    const c = clamp(+e.target.value || 0, -100, 100);
    const cur = pitchSplit(clip);
    commit(() => { setPitchOffset(clip, cur.semis * 100 + c); });
    renderPending();
  };
  if (!many) {
    setupCurveEditor($('iCurve'), clip);
    $('iCurveClr').onclick = () => {
      takeSnap();
      delete clip.pitchCurve;
      clip.renderKey = null; clip.renderUrl = null; clip.needsRender = false;
      afterStructure(); renderInspector();
    };
  }
  // ★ 增益是多选时唯一「一起改」的字段：一次拖动只留一次撤销快照
  let gainSnap = false;
  $('iGain').oninput = (e) => {
    const v = clamp(+e.target.value, 0, 2);
    if (!gainSnap) { takeSnap(); gainSnap = true; }
    const items = many ? sel : [{ tr, clip }];
    for (const it of items) it.clip.gain = v;
    $('iGainV').textContent = Math.round(v * 100) + '%';
    markDirty(); drawStatic();
  };
  $('iGain').onchange = () => {
    gainSnap = false;
    engine.project = S.proj;
    afterStructure();
  };
  $('iPlay').onclick = async () => {
    await engine.resume();
    if (!(await engine.previewClip(clip))) {
      $('iCost').textContent = '音频尚未解码完成，请稍后重试';
      setTimeout(() => { $('iCost').textContent = ''; }, 1500);
    }
  };
  $('iExtend').onclick = () => openExtendDialog();
  $('iDup').onclick = () => duplicateSelected();
  $('iSamp').onclick = () => createSamplerFromClip();
  $('iReset').onclick = () => commit(() => {
    clip.semis = 0; clip.cents = 0;
    delete clip.pitchCurve;                    // ★ 恢复原样 = 连曲线一起删
    clip.renderKey = null; clip.renderUrl = null; clip.needsRender = false;
    clip.srcOut = clip.srcIn + (clip.srcOut - clip.srcIn);
    clip.length = clip.srcOut - clip.srcIn;
  });
  $('iDel').onclick = () => deleteSelected();
}

// 把 needsRender 的块真的送去渲染。
// items 不给 = 选中的块；给了 = 只渲染这几块（整页拖完音高用）。
async function renderPending(items) {
  const todos = (items || selectedClips()).filter(({ clip }) => clip.needsRender);
  if (!todos.length) return;
  for (const { clip } of todos) {
    const src = S.proj.sources?.[clip.sourceId];
    if (!src) continue;
    // ★ 带曲线的块走 render_curve —— 切分/裁剪/撤销后不用到处复制分支
    if (Array.isArray(clip.pitchCurve) && clip.pitchCurve.length) {
      try { await renderCurveClip(clip); }
      catch (e) {
        const costEl = $('iCost');
        if (costEl) costEl.textContent = '渲染失败：' + e.message;
        clip.needsRender = false;
      }
      continue;
    }
    const tok = ++S.renderTok;
    clip._tok = tok;
    const costEl = $('iCost');
    if (costEl) costEl.textContent = '渲染中…';
    try {
      const r = await api('/render_clip', {
        method: 'POST',
        json: {
          source: src.path, srcIn: clip.srcIn, srcOut: clip.srcOut,
          semis: clip.semis || 0, cents: clip.cents || 0, outSec: clip.length,
        },
      });
      // ★ 回来时如果这个块又变了，丢弃 —— 绝不拿旧音冒充
      if (clip._tok !== tok || !clipById(clip.id)) continue;
      clip.renderKey = r.key;
      clip.renderUrl = r.url;
      clip.needsRender = false;
      engine.stats.renders++;
      ensureRenderedPeaks(clip);
      if (costEl) costEl.textContent = `渲染 ${r.sec_cost}s${r.cached ? '（缓存）' : ''}`;
    } catch (e) {
      if (costEl) costEl.textContent = '渲染失败：' + e.message;
      clip.needsRender = false;
    }
  }
  afterStructure();
  if (engine.playing) engine.play(S.proj, engine.songNow());
}

// ---------------------------------------------------------------- 混音台
// ★ 推子/声像/电平表。电平表只是**看**，不参与混音 —— analyser 串在轨道的
//   gain → pan 之后、master 之前，直通不染色。
let meterRAF = 0;
const meterSmooth = new Map();   // 节点 -> 已经衰减过的读数

const panText = (p) => Math.abs(p) < 0.02 ? 'C'
  : (p < 0 ? 'L' + Math.round(-p * 100) : 'R' + Math.round(p * 100));

function renderMixer() {
  if (!S.proj) return;
  const box = $('mixStrips');
  box.textContent = '';
  for (const tr of S.proj.tracks) {
    const d = document.createElement('div');
    d.className = 'strip';
    d.innerHTML = `
      <div class="nm" title="${tr.name}">${tr.name}</div>
      <div class="fbody">
        <input class="fader" type="range" min="0" max="1.5" step="0.01" value="${tr.gain ?? 1}">
        <canvas class="meter"></canvas>
      </div>
      <input class="pan" type="range" min="-1" max="1" step="0.02" value="${tr.pan || 0}">
      <div class="nm sub"><span class="gv">${Math.round((tr.gain ?? 1) * 100)}%</span> <span class="pv">${panText(tr.pan || 0)}</span></div>
      <div class="fxs">
        <label title="混响（湿声比例）">混<input class="rv" type="range" min="0" max="0.8" step="0.01" value="${(tr.fx && tr.fx.reverb && tr.fx.reverb.mix) || 0}"></label>
        <label title="延迟（湿声比例）">延<input class="dl" type="range" min="0" max="0.8" step="0.01" value="${(tr.fx && tr.fx.delay && tr.fx.delay.mix) || 0}"></label>
      </div>
      <div class="fxchips"></div>
      <div class="ms">
        <button class="m ${tr.mute ? 'on' : ''}">M</button>
        <button class="s solo ${tr.solo ? 'on' : ''}">S</button>
      </div>`;
    // 双击轨名改名（Enter 确认 / Esc 取消）
    const nmEl = d.querySelector('.nm');
    if (nmEl) nmEl.ondblclick = () => startRenameTrack(tr, nmEl);

    // 插件芯片（FL Mixer 那种）：点一下 = 选中这轨、切到效果器页
    const chips = d.querySelector('.fxchips');
    for (const slot of (tr.fx?.chain || [])) {
      const ft = fxType(slot.type);
      if (!ft) continue;
      const c = document.createElement('div');
      c.className = 'chip' + (slot.enabled === false ? ' off' : '');
      c.title = t(ft.name) + (slot.enabled === false ? t('（已旁通）') : '');
      c.innerHTML = `<span class="dot" style="background:${ft.color}"></span><span>${t(ft.name)}</span>`;
      c.onclick = () => openFx(tr.id, slot.id);
      chips.appendChild(c);
    }
    // VST 徽标：有插件才显示；黄/红 = 冻结过期/失败
    const vs = vstStateOf(tr);
    if (vs) {
      const c = document.createElement('div');
      c.className = 'chip vst' + (vs.cls ? ' ' + vs.cls : '');
      c.title = `${vstSlotsOf(tr).length} 个 VST · ${vs.label}`;
      const n = vstSlotsOf(tr).length;
      c.innerHTML = `<span class="dot" style="background:${vs.state === 'ok' ? '#3ddc97'
        : vs.state === 'error' ? '#ff5a76' : vs.state === 'off' ? '#4d5668' : '#ffc857'}"></span>`
        + `<span>VST ${n}</span>`;
      c.onclick = () => openVst(tr.id);
      chips.appendChild(c);
    }
    const plus = document.createElement('div');
    plus.className = 'chip plus';
    plus.textContent = '＋ 插件';
    plus.onclick = () => openFx(tr.id, null);
    chips.appendChild(plus);
    d.querySelector('.fader').addEventListener('input', (e) => {
      tr.gain = +e.target.value;
      d.querySelector('.gv').textContent = Math.round(tr.gain * 100) + '%';
      engine.applyMix();
      markDirty();
      vstTouched(tr);            // 冻结里乘过旧增益 ⇒ 要重算
    });
    d.querySelector('.pan').addEventListener('input', (e) => {
      tr.pan = +e.target.value;
      d.querySelector('.pv').textContent = panText(tr.pan);
      engine.applyMix();
      markDirty();
    });
    const setFx = (key, v) => {
      tr.fx = tr.fx || {};
      tr.fx[key] = tr.fx[key] || {};
      tr.fx[key].mix = v;
      if (key === 'reverb' && !(tr.fx.reverb.size > 0)) tr.fx.reverb.size = 1.6;
      if (key === 'delay') {
        if (!(tr.fx.delay.time > 0)) tr.fx.delay.time = 0.25;
        if (!(tr.fx.delay.fb > 0)) tr.fx.delay.fb = 0.3;
      }
      engine.setTrackFx(tr);
      markDirty();
    };
    d.querySelector('.rv').addEventListener('input', (e) => setFx('reverb', +e.target.value));
    d.querySelector('.dl').addEventListener('input', (e) => setFx('delay', +e.target.value));
    d.querySelector('.m').onclick = () => {
      takeSnap(); tr.mute = !tr.mute; afterStructure();
    };
    d.querySelector('.s').onclick = () => {
      takeSnap(); tr.solo = !tr.solo; afterStructure();
    };
    d.querySelector('.meter').dataset.track = tr.id;
    box.appendChild(d);
  }
  // 总推子（不存工程 —— 它是「这台机器的音量」，不是作品的一部分）
  const m = document.createElement('div');
  m.className = 'strip master';
  m.innerHTML = `
    <div class="nm">总输出</div>
    <div class="fbody">
      <input class="fader" type="range" min="0" max="1.2" step="0.01" value="${engine.masterGain ?? 1}">
      <canvas class="meter"></canvas>
    </div>
    <div class="nm sub"><span class="gv">${Math.round((engine.masterGain ?? 1) * 100)}%</span></div>`;
  m.querySelector('.fader').addEventListener('input', (e) => {
    engine.masterGain = +e.target.value;
    engine.setMaster(engine.masterGain);
    m.querySelector('.gv').textContent = Math.round(engine.masterGain * 100) + '%';
  });
  m.querySelector('.meter').dataset.track = '@master';
  box.appendChild(m);
  startMeters();
}

function startMeters() {
  if (meterRAF) return;
  const box = $('mixStrips');
  const tick = () => {
    meterRAF = requestAnimationFrame(tick);
    if (!S.proj || $('panelMix').hidden) return;
    const lv = engine.levels();
    for (const cv of box.querySelectorAll('canvas.meter')) {
      const id = cv.dataset.track;
      const raw = id === '@master' ? lv.master : (lv.tracks[id] || 0);
      const prev = meterSmooth.get(id) || 0;
      // 快升慢降 —— 和真推子表的读法一致
      const v = raw > prev ? raw : prev * 0.86;
      meterSmooth.set(id, v);
      drawMeter(cv, v);
    }
  };
  meterRAF = requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- 效果器（插件链）
// ★ 参数表只从服务端 `/api/studio/fx_types` 拿，前端不另抄一份 —— 否则默认值/范围迟早漂移。
let FX_TYPES = {};
let FX_MAX = 24;
let fxSel = null;

function fxType(t) { return FX_TYPES[t] || null; }

async function loadFxTypes() {
  try {
    const r = await api('/fx_types');
    FX_TYPES = r.types || {};
    FX_MAX = r.max_chain || 24;
  } catch (e) {
    console.error('效果器参数表加载失败', e);
  }
  fillFxAddSel();
}

function fillFxAddSel() {
  const sel = $('fxAddSel');
  if (!sel) return;
  sel.textContent = '';
  const groups = new Map();
  for (const [k, ft] of Object.entries(FX_TYPES)) {
    const g = ft.group || '其它';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push([k, ft]);
  }
  // 效果器名和分组名来自服务端（studio_fx.py），是数据不是文案 —— 显示时必须过 t()
  for (const [g, items] of groups) {
    const og = document.createElement('optgroup');
    og.label = t(g);
    for (const [k, ft] of items) {
      const o = document.createElement('option');
      o.value = k; o.textContent = t(ft.name);
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
}

function fxTrack() {
  if (!S.proj) return null;
  return S.proj.tracks.find(t => t.id === S.selTrack) || S.proj.tracks[0] || null;
}

// ★ 只读。重绘混音台时会遍历**所有**轨，这里一旦顺手建字段，
//   每条轨都会长出 fx:{chain:[]} —— 「这条轨有没有插件」就再也分不出来，
//   空链还会被自动保存写回工程文件。
function fxChainOf(tr) {
  return (tr && tr.fx && Array.isArray(tr.fx.chain)) ? tr.fx.chain : [];
}

// 真正要改链时才走这个，允许建字段
function fxChainEdit(tr) {
  if (!tr) return null;
  tr.fx = tr.fx || {};
  if (!Array.isArray(tr.fx.chain)) tr.fx.chain = [];
  return tr.fx.chain;
}

function pushChain(tr) {
  engine.setTrackChain(tr);
  engine.project = S.proj;
  vstTouched(tr);
}

function openFx(trackId, slotId) {
  if (trackId) S.selTrack = trackId;
  fxSel = slotId || null;
  const b = document.querySelector('.tabs button[data-tab="fx"]');
  if (b) b.click();
  renderFxPanel();
}

// log / lin 滑杆映射（只影响手感，不影响传给后端的值）
function fxToPos(p, v) {
  const lo = p.min, hi = p.max;
  const c = Math.max(lo, Math.min(hi, Number(v)));
  if (p.curve === 'log' && lo > 0) return 1000 * Math.log(c / lo) / Math.log(hi / lo);
  return hi > lo ? 1000 * (c - lo) / (hi - lo) : 0;
}
function fxFromPos(p, pos) {
  const lo = p.min, hi = p.max;
  let v = (p.curve === 'log' && lo > 0)
    ? lo * Math.pow(hi / lo, pos / 1000)
    : lo + (hi - lo) * pos / 1000;
  const step = p.step || 0.01;
  v = Math.round(v / step) * step;
  return Math.max(lo, Math.min(hi, v));
}
function fxFmt(p, v) {
  const step = p.step || 0.01;
  const dec = step >= 1 ? 0 : (step >= 0.1 ? 1 : 2);
  let s = Number(v).toFixed(dec);
  if (p.unit === 'Hz' && Number(v) >= 1000) {
    s = (Number(v) / 1000).toFixed(Number(v) >= 10000 ? 1 : 2) + 'k';
  }
  return s + (p.unit ? ' ' + p.unit : '');
}

function renderFxPanel() {
  if (!$('fxSlots')) return;
  const tr = fxTrack();
  $('fxTrackName').textContent = tr ? tr.name : '—';

  // 浏览器不支持 / 加载失败 ⇒ 明确说明，不静默
  const w = $('fxWarn');
  const hasChain = (S.proj?.tracks || []).some(t => (t.fx?.chain || []).length);
  if (w) {
    if (!engine.fxReady && engine.fxError) {
      w.hidden = false;
      w.textContent = `⚠ 播放时插件链是旁通的（导出仍然生效）:${engine.fxError}`;
    } else if (hasChain && !engine.fxReady) {
      w.hidden = false;
      w.textContent = '效果器模块还在加载…';
    } else {
      w.hidden = true;
    }
  }

  const box = $('fxSlots');
  box.textContent = '';
  const chain = fxChainOf(tr);
  if (!chain.length) {
    const e = document.createElement('div');
    e.className = 'fxslot-empty';
    e.textContent = tr ? '这一轨还没有插件。' : '先建一条轨。';
    box.appendChild(e);
  }
  chain.forEach((slot, i) => {
    const ft = fxType(slot.type);
    const d = document.createElement('div');
    d.className = 'fxslot' + (slot.id === fxSel ? ' sel' : '')
      + (slot.enabled === false ? ' bypass' : '');
    d.innerHTML = `<span class="idx">${i + 1}</span>
      <span class="dot" style="background:${ft ? ft.color : '#4d5668'}"></span>
      <span class="nm">${ft ? escHtml(t(ft.name)) : t('未知插件（已跳过）')}</span>
      <button data-a="on" title="旁通">${slot.enabled === false ? '○' : '●'}</button>
      <button data-a="up" title="上移">↑</button>
      <button data-a="down" title="下移">↓</button>
      <button data-a="del" title="删除">×</button>`;
    d.onclick = (e) => {
      if (e.target.closest('button')) return;
      fxSel = slot.id;
      renderFxPanel();
    };
    d.querySelectorAll('button').forEach(b => {
      b.onclick = (e) => {
        e.stopPropagation();
        const a = b.dataset.a;
        if (a === 'on') {
          takeSnap();
          slot.enabled = slot.enabled === false;   // 缺省视为开
        } else if (a === 'up' && i > 0) {
          takeSnap();
          chain.splice(i - 1, 0, chain.splice(i, 1)[0]);
        } else if (a === 'down' && i < chain.length - 1) {
          takeSnap();
          chain.splice(i + 1, 0, chain.splice(i, 1)[0]);
        } else if (a === 'del') {
          takeSnap();
          chain.splice(i, 1);
          if (fxSel === slot.id) fxSel = null;
        } else return;
        pushChain(tr);
        markDirty();
        renderFxPanel(); renderMixer();
      };
    });
    box.appendChild(d);
  });
  renderFxParams(tr);
}

function renderFxParams(tr) {
  const box = $('fxParams');
  if (!box) return;
  box.textContent = '';
  const chain = fxChainOf(tr);
  const slot = chain.find(s => s.id === fxSel);
  if (!slot) {
    const d = document.createElement('div');
    d.className = 'inote';
    d.textContent = chain.length ? '点一个插件看参数' : '这一轨还没有插件 —— 左边选一个加进来';
    box.appendChild(d);
    return;
  }
  const ft = fxType(slot.type);
  if (!ft) {
    const d = document.createElement('div');
    d.className = 'inote';
    d.textContent = '这个插件这台机器不认识（已跳过，数据原样保留）';
    box.appendChild(d);
    return;
  }
  slot.params = slot.params || {};
  const h = document.createElement('h4');
  h.textContent = t(ft.name);
  box.appendChild(h);
  const sub = document.createElement('div');
  sub.className = 'sub';
  // 参数表（名字/分组/标签）都来自服务端，是数据 —— 显示时逐段过 t()
  sub.textContent = t('第 {0} 个 · {1}')
    .replace('{0}', String(chain.indexOf(slot) + 1)).replace('{1}', t(ft.group))
    + (slot.enabled === false ? ' · ' + t('已旁通') : '');
  box.appendChild(sub);

  for (const p of ft.params) {
    const row = document.createElement('div');
    row.className = 'fxparam';
    const lab = document.createElement('label');
    lab.textContent = t(p.label);
    row.appendChild(lab);
    const cur = (slot.params[p.key] !== undefined) ? slot.params[p.key] : p.default;

    if (p.type === 'enum') {
      const sel = document.createElement('select');
      for (const o of (p.options || [])) {
        const op = document.createElement('option');
        op.value = o.value; op.textContent = t(o.label);
        sel.appendChild(op);
      }
      sel.value = String(cur);
      const out = document.createElement('output');
      out.textContent = '';
      sel.onchange = () => {
        takeSnap();
        slot.params[p.key] = sel.value;
        pushChain(tr); markDirty(); renderFxPanel();
      };
      row.appendChild(sel);
      row.appendChild(out);
    } else {
      const sl = document.createElement('input');
      sl.type = 'range';
      sl.min = 0; sl.max = 1000; sl.step = 1;
      sl.value = String(fxToPos(p, cur));
      const out = document.createElement('output');
      out.textContent = fxFmt(p, cur);
      let snapped = false;
      sl.addEventListener('pointerdown', () => { snapped = false; });
      sl.addEventListener('input', () => {
        if (!snapped) { takeSnap(); snapped = true; }   // ★ 改之前留档
        const v = fxFromPos(p, +sl.value);
        slot.params[p.key] = v;
        out.textContent = fxFmt(p, v);
        pushChain(tr);
        markDirty();
      });
      sl.addEventListener('change', () => { snapped = false; });
      row.appendChild(sl);
      row.appendChild(out);
    }
    box.appendChild(row);
  }
}

function addFx(type, trIn) {
  const tr = trIn || fxTrack();
  if (!tr) return false;
  const ft = fxType(type);
  if (!ft) return false;
  const chain = fxChainEdit(tr);
  if (!chain) return false;
  if (chain.length >= FX_MAX) {
    $('hint').textContent = t('每条轨最多 {0} 个插件').replace('{0}', String(FX_MAX));
    return false;
  }
  takeSnap();
  const params = {};
  for (const p of ft.params) params[p.key] = p.default;
  const slot = { id: uid('fx'), type, enabled: true, params };
  chain.push(slot);
  fxSel = slot.id;
  S.selTrack = tr.id;
  pushChain(tr);
  markDirty();
  renderFxPanel(); renderMixer();
  return true;
}

function drawMeter(cv, v) {
  const h = cv.clientHeight || 0, w = cv.clientWidth || 6;
  if (!h) return;
  if (cv.height !== h) cv.height = h;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, w, h);
  g.fillStyle = '#0a0d13';
  g.fillRect(0, 0, w, h);
  const db = 20 * Math.log10(Math.max(v, 1e-5));
  const frac = clamp((db + 54) / 54, 0, 1);
  const bh = Math.round(frac * (h - 2));
  const grd = g.createLinearGradient(0, h, 0, 0);
  grd.addColorStop(0, '#2fbf71');
  grd.addColorStop(0.72, '#d8d84a');
  grd.addColorStop(1, '#ff4d4d');
  g.fillStyle = grd;
  g.fillRect(1, h - 1 - bh, Math.max(2, w - 2), bh);
}

// ---------------------------------------------------------------- 运输栏
function syncTransport() {
  if (!S.proj) return;
  $('inBpm').value = S.proj.bpm;
  $('selSig').value = (S.proj.timeSig || [4, 4]).join('/') === '3/4' ? '3/4' : '4/4';
  $('selSnap').value = S.proj.ui?.snap || '1/16';
  const lp = S.proj.ui?.loop;
  $('chkLoop').checked = !!(lp && lp[1] > lp[0] + .01);
  $('chkMetro').checked = !!engine.metro;
}

$('btnPlay').onclick = () => togglePlay();
$('btnStop').onclick = () => {
  engine.stop();
  S.playAt = 0;
  $('btnPlay').textContent = '▶';
  $('btnPlay').classList.remove('on');
  drawOverlay(); updateClock();
};
function togglePlay() {
  if (!S.proj) return;
  if (engine.playing) {
    engine.pause();
    $('btnPlay').textContent = '▶';
    $('btnPlay').classList.remove('on');
  } else {
    engine.metro = $('chkMetro').checked;
    S.playAt = Math.max(0, S.playAt);
    engine.play(S.proj, S.playAt);
    $('btnPlay').textContent = '⏸';
    $('btnPlay').classList.add('on');
    startFrames();
  }
}
engine.onstate = (on) => {
  if (!on) {
    $('btnPlay').textContent = '▶';
    $('btnPlay').classList.remove('on');
    S.playAt = engine.songNow();
  }
};

$('inBpm').onchange = (e) => {
  const v = clamp(+e.target.value || 120, 40, 240);
  e.target.value = v;
  takeSnap();
  S.proj.bpm = v;
  markDirty(); drawStatic(); drawOverlay();
};
$('selSig').onchange = (e) => {
  takeSnap();
  S.proj.timeSig = e.target.value === '3/4' ? [3, 4] : [4, 4];
  markDirty(); drawStatic();
};
$('selSnap').onchange = (e) => {
  S.proj.ui.snap = e.target.value;
  markDirty();
};
$('chkLoop').onchange = (e) => {
  S.proj.ui = S.proj.ui || {};
  if (e.target.checked) {
    if (!(S.proj.ui.loop?.[1] > S.proj.ui.loop?.[0])) {
      const bar = barSec();
      S.proj.ui.loop = [Math.floor(S.playAt / bar) * bar, Math.floor(S.playAt / bar) * bar + bar * 4];
    }
  } else S.proj.ui.loop = [0, 0];
  markDirty(); drawStatic();
};
$('chkMetro').onchange = (e) => {
  engine.metro = e.target.checked;
  if (engine.playing) engine.play(S.proj, engine.songNow());
};
$('btnZoomIn').onclick = () => { S.view.pps = clamp(S.view.pps * 1.35, 6, 900); drawStatic(); markDirty(); };
$('btnZoomOut').onclick = () => { S.view.pps = clamp(S.view.pps / 1.35, 6, 900); drawStatic(); markDirty(); };
// ---------------------------------------------------------------- 导出
// ★ WAV 24bit 是默认；按住 Alt 点 = MP3 320k(K 歌发微信那种)
function toast(html, ms = 14000) {
  let el = $('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  // ★ 右下角这条提示原来只能等它自己消失(最长十几秒),内容一长就挡住工作区。
  //   加一个 × ,并且 ms<=0 表示「不自动关」——那种要人读的提示就交给用户自己关。
  el.innerHTML = `<div class="tx">${html}</div>` +
    '<button class="tx-x" type="button" title="关闭" aria-label="关闭">×</button>';
  el.querySelector('.tx-x').onclick = () => {
    clearTimeout(toast._t);
    el.classList.remove('on');
  };
  el.classList.add('on');
  clearTimeout(toast._t);
  if (ms > 0) toast._t = setTimeout(() => el.classList.remove('on'), ms);
}

let exporting = false;
let expLoaded = false;        // 面板开过一次 = 里面的值说了算（包括「清空」）
let expCover = '';            // 封面在媒体目录里的相对路径

// ---- 发行版的导出选项：格式 / 采样率 / 位深 / 响度 / 标签 / 封面 ----
function expTags() {
  const meta = (S.proj && S.proj.meta) || {};
  const t = {};
  if (!expLoaded) {
    const old = meta.tags && typeof meta.tags === 'object' ? meta.tags : {};
    for (const k of ['title', 'artist', 'album', 'year', 'genre', 'isrc']) {
      if (old[k]) t[k] = String(old[k]);
    }
    return t;
  }
  const ids = { title: 'expTitle', artist: 'expArtist', album: 'expAlbum',
    year: 'expYear', genre: 'expGenre', isrc: 'expIsrc' };
  for (const [k, id] of Object.entries(ids)) {
    const el = $(id);
    const v = el ? el.value.trim() : '';
    if (v) t[k] = v;
  }
  return t;
}

// ★ 两种交付的规格不一样，所以是「选一个，别的字段跟着变」，不是让你自己拼：
//   自己发行 = 成品（压响度、卡真峰、带标签）；交给唱片公司 = 母带前混音（留余量、不写标签）。
const EXP_PRESETS = {
  self: { fmt: 'wav', sr: '48000', bits: '24', loud: '-14', embed: true, peak: null },
  label: { fmt: 'wav', sr: '48000', bits: '24', loud: 'off', embed: false, peak: -6 },
};
const EXP_NOTE = {
  self: '成品：-14 LUFS · 真峰 −1.0 dBTP · 标签与封面已写入文件，可直接上传网易云、QQ 音乐人、抖音与 Spotify',
  label: '母带前混音：<b>不压响度</b>，峰留 −6 dBFS 给母带师；标签默认不写（他们按后台填）',
};

function expTarget() {
  return $('expTarget') && $('expTarget').value === 'label' ? 'label' : 'self';
}

function applyExpPreset(name) {
  const p = EXP_PRESETS[name] || EXP_PRESETS.self;
  if ($('expFmt')) $('expFmt').value = p.fmt;
  if ($('expSr')) $('expSr').value = p.sr;
  if ($('expBits')) $('expBits').value = p.bits;
  if ($('expLoud')) $('expLoud').value = p.loud;
  if ($('selLoud')) $('selLoud').value = p.loud;
  if ($('expEmbed')) $('expEmbed').checked = p.embed;
  if ($('expAlsoMp3') && name === 'label') $('expAlsoMp3').checked = false;
  syncExpFmt();
  if ($('expTargetNote')) $('expTargetNote').innerHTML = EXP_NOTE[name] || '';
}

function exportOptions() {
  const g = (id, dflt) => ($(id) ? $(id).value : dflt);
  // ★ 响度只有一个开关：顶栏那个 `selLoud`。面板里的下拉只是它的另一张脸，
  //   两边互相同步 —— 否则「面板开过之后顶栏就不管用了」，这种暗规则最难查。
  const loud = $('selLoud') ? $('selLoud').value : '-14';
  const tgt = expLoaded ? expTarget() : 'self';
  const embed = $('expEmbed') ? $('expEmbed').checked : true;
  return {
    format: g('expFmt', 'wav') || 'wav',
    target: tgt,
    sr: Number(g('expSr', '48000')) || 48000,
    bits: Number(g('expBits', '24')) || 24,
    loudness: loud === 'off' ? null : Number(loud),
    peak: tgt === 'label' ? EXP_PRESETS.label.peak : null,
    embed,
    tags: embed ? expTags() : {},
    cover: embed ? (expLoaded ? (expCover || null)
      : ((S.proj && S.proj.meta && S.proj.meta.cover) || null)) : null,
  };
}

function rememberExport(opts) {
  if (!S.proj) return;
  S.proj.ui = S.proj.ui || {};
  S.proj.meta = S.proj.meta || {};
  S.proj.ui.export = {
    format: opts.format, sr: opts.sr, bits: opts.bits,
    loudness: opts.loudness == null ? 'off' : String(opts.loudness),
    target: opts.target || 'self', embed: opts.embed !== false,
    tags: opts.tags, cover: opts.cover || '',
  };
  // 标签也落进 meta：下次开工程还在
  S.proj.meta.tags = opts.tags;
  if (opts.cover) S.proj.meta.cover = opts.cover;
  else delete S.proj.meta.cover;
}

function loadExpPanel() {
  const prev = (S.proj && S.proj.ui && S.proj.ui.export) || {};
  const meta = (S.proj && S.proj.meta) || {};
  const tags = { ...(meta.tags || {}), ...(prev.tags || {}) };
  if ($('expFmt')) $('expFmt').value = prev.format === 'mp3' ? 'mp3' : 'wav';
  if ($('expSr')) $('expSr').value = String(prev.sr || 48000);
  if ($('expBits')) $('expBits').value = String(prev.bits || 24);
  if ($('expTarget')) $('expTarget').value = prev.target === 'label' ? 'label' : 'self';
  // 上次是交给唱片公司、且存的是「不归一」，就得把顶栏那个也拉回 off —— 两个是同一个开关
  if (prev.target === 'label' && prev.loudness && prev.loudness !== '-14') {
    if ($('selLoud')) $('selLoud').value = prev.loudness;
  }
  if ($('expLoud')) {
    $('expLoud').value = $('selLoud') ? $('selLoud').value : '-14';
  }
  if ($('expEmbed')) $('expEmbed').checked = prev.embed !== false;
  if ($('expTargetNote')) $('expTargetNote').innerHTML = EXP_NOTE[expTarget()];
  $('expTitle').value = tags.title || (S.proj && (S.proj.title || S.proj.name)) || '';
  $('expArtist').value = tags.artist || '';
  $('expAlbum').value = tags.album || '';
  $('expYear').value = tags.year || '';
  $('expGenre').value = tags.genre || '';
  $('expIsrc').value = tags.isrc || '';
  expCover = prev.cover || meta.cover || '';
  renderExpCover();
  $('expBody').innerHTML = '';
  $('expState').innerHTML = '';
  expLoaded = true;
  syncExpFmt();
}

function syncExpFmt() {
  // 位深只对 wav 有意义；mp3 那边灰掉，别让人以为能选
  const wav = !$('expFmt') || $('expFmt').value === 'wav';
  if ($('expBits')) $('expBits').disabled = !wav;
  if ($('expSr')) $('expSr').disabled = false;
}

function renderExpCover() {
  const el = $('expCoverState');
  if (!el) return;
  const base = expCover.split('/').pop();
  el.innerHTML = expCover
    ? `封面 <b>${base}</b> · ` +
      `<a href="/api/studio/media/${encodeURIComponent(base)}" target="_blank">看</a>`
    : '没选封面';
}

async function openExportPanel() {
  if (!S.proj) return;
  $('expPanel').hidden = false;
  loadExpPanel();
}

function expSetBusy(on, text) {
  const b = $('expGo');
  if (!b) return;
  b.disabled = on;
  b.textContent = on ? (text || '导出…') : '导出';
  const c = $('expCheck');
  if (c) c.disabled = on;
}

function renderExpCheck(r) {
  const rep = r.report || r.check && r.check.report || {};
  const vs = r.verdicts || (r.check && r.check.verdicts) || [];
  const bad = vs.filter((v) => !v.ok).length;
  $('expState').innerHTML =
    `<div class="mrow"><span class="mname">成品</span><span>${rep.sec}s · ` +
    `${rep.channels} 声道 · ${rep.sr} Hz</span></div>` +
    `<div class="mrow"><span class="mname">响度</span><span>${rep.lufs} LUFS · ` +
    `采样峰 ${rep.sample_peak_db} dBFS · <b>真峰 ${rep.true_peak_db} dBTP</b></span></div>` +
    (r.peak_db != null
      ? `<div class="mrow"><span class="mname">交付</span><span>母带前混音 · ` +
        `峰压到 ${Number(r.peak_db).toFixed(1)} dBFS（留余量给母带师）</span></div>` : '') +
    `<div class="mrow"><span class="mname">结论</span>` +
    `<span class="${bad ? '' : 'mtag'}">${bad ? `★ ${bad} 项不过` : '全部通过'}</span></div>`;
  const rows = vs.map((v) => `<div class="arow" style="cursor:default">
      <b style="color:${v.ok ? '#6fd08c' : '#ff7b7b'}">${v.ok ? '✓' : '✗'}</b>
      <span class="atitle">${v.label}</span>
      <span>${v.value}</span>
      <span class="awhy">要 ${v.want}</span></div>`).join('');
  if (!rows) {
    $('expBody').innerHTML = `<div class="agroup">还没检查</div>
      <div class="inote">点「检查」先量一遍（渲染 + 测量，不写文件）。</div>`;
    return;
  }
  $('expBody').innerHTML = `<div class="agroup">发行检查 · 和导出走同一条混音路</div>`
    + rows
    + `<div class="awhy" style="padding:6px 0">渲染耗时 ${r.sec_cost ?? '—'}s</div>`;
}

async function exportCheck() {
  if (!S.proj || exporting) return null;
  const o = exportOptions();
  const b = $('expCheck');
  if (b) { b.disabled = true; b.textContent = '检查中…'; }
  $('expState').innerHTML = `<div class="mrow"><span class="mname">检查</span>
    <span>正在走导出那条路渲染整曲…</span></div>`;
  try {
    const r = await api('/release_check', {
      method: 'POST',
      json: { project: S.proj, loudness: o.loudness, sr: o.sr,
        format: o.format, peak_db: o.peak },
    });
    renderExpCheck(r);
    return r;
  } catch (e) {
    $('expState').innerHTML = `<div class="mrow"><span class="mname">检查</span>
      <span>失败：${e.message}</span></div>`;
    return null;
  } finally {
    if (b) { b.disabled = false; b.textContent = '检查'; }
  }
}

async function exportProject(fmt, opts) {
  if (!S.proj || exporting) return null;
  exporting = true;
  const btn = $('btnExport');
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '导出…';
  setSaveState('导出中…');
  const isStems = fmt === 'stems';
  const stage = (opts && opts.stage) === 'post' ? 'post' : 'pre';
  try {
    const ex = exportOptions();
    const format = fmt || ex.format || 'wav';
    // ★ 先记进工程再存盘：标签/封面要跟着工程走，下次开还在
    if (!isStems) rememberExport(ex);
    await saveNow().catch(() => {});
    S.proj.ui = S.proj.ui || {};
    const r = await api('/export', {
      method: 'POST',
      json: isStems
        ? { project: S.proj, format: 'zip', stems: stage }
        : {
          project: S.proj, format, normalize: true,
          loudness: opts && opts.loudness !== undefined ? opts.loudness : ex.loudness,
          peak_db: ex.peak,
          sr: ex.sr, bits: ex.bits, tags: ex.tags, cover: ex.cover,
        },
    });
    if (isStems) {
      setSaveState(`分轨 ${r.tracks} 条`);
      toast(`分轨导出好了 · ${r.tracks} 轨（${stage === 'post' ? '插件链之后' : '插件链之前'}）` +
        `${r.missing && r.missing.length ? `<br>⚠ ${r.missing.length} 个源没找到` : ''}` +
        `<br><a href="${r.url}" download>下载 ${r.file}</a> ` +
        `<span class="dim">${(r.size / 1048576).toFixed(1)}MB · ${r.sec_cost}s</span>`);
      return r;
    }
    const ldb = r.loudness ? ` · ${r.loudness.lufs_after} LUFS` : '';
    const rep = (r.check && r.check.report) || null;
    const vs = (r.check && r.check.verdicts) || [];
    const bad = vs.filter((v) => !v.ok);
    const spec = `${r.sr || ''}Hz${r.bits ? ` · ${r.bits}bit` : ''}`;
    const rel = rep
      ? `<br>真峰 ${rep.true_peak_db} dBTP · ${rep.lufs} LUFS` +
        (bad.length ? `<br>★ ${bad.length} 项不过：${bad.map((v) => v.label).join('、')}`
          : ' · 发行检查全过')
      : '';
    setSaveState(`导出 ${r.sec}s`);
    toast(`导出好了 · ${r.sec}s · ${spec}${ldb}${r.normalized ? ' · 已归一' : ''}` +
      `${r.tagged ? ' · 带标签' : ''}${rel}` +
      `${r.missing && r.missing.length ? `<br>⚠ ${r.missing.length} 个块没混进去` : ''}` +
      `<br><a href="${r.url}" download>下载 ${r.file}</a> ` +
      `<span class="dim">${(r.size / 1048576).toFixed(1)}MB · ${r.sec_cost}s</span>`);
    if ($('expPanel') && !$('expPanel').hidden && r.check) renderExpCheck(r);
    return r;
  } catch (e) {
    setSaveState('导出失败');
    toast('导出失败：' + e.message, 8000);
    return null;
  } finally {
    exporting = false;
    btn.disabled = false; btn.textContent = old;
  }
}
$('btnExport').onclick = () => openExportPanel();
$('btnExport').title = '导出发行版：格式 / 采样率 / 位深 / 标签 / 封面，先检查再导';
$('expClose')?.addEventListener('click', () => { $('expPanel').hidden = true; });
$('expCancel')?.addEventListener('click', () => { $('expPanel').hidden = true; });
$('expFmt')?.addEventListener('change', syncExpFmt);
$('expLoud')?.addEventListener('change', () => {
  if ($('selLoud')) $('selLoud').value = $('expLoud').value;
});
$('selLoud')?.addEventListener('change', () => {
  if ($('expLoud')) $('expLoud').value = $('selLoud').value;
});
$('expCheck')?.addEventListener('click', () => exportCheck());
$('expTarget')?.addEventListener('change', () => applyExpPreset(expTarget()));
$('expGo')?.addEventListener('click', async () => {
  expSetBusy(true, '导出…');
  const alsoMp3 = $('expAlsoMp3') && $('expAlsoMp3').checked
    && exportOptions().format === 'wav';
  const r = await exportProject(null, {});
  let m = null;
  if (r && alsoMp3) m = await exportProject('mp3', {});
  expSetBusy(false);
  if (r && m) {
    toast(`两版都导好了<br><a href="${r.url}" download>下载 ${r.file}</a> ` +
      `<span class="dim">${(r.size / 1048576).toFixed(1)}MB</span><br>` +
      `<a href="${m.url}" download>下载 ${m.file}</a> ` +
      `<span class="dim">${(m.size / 1048576).toFixed(1)}MB</span>`, 20000);
  }
  if (r && !r.missing) $('expPanel').hidden = true;
});
$('expCoverBtn')?.addEventListener('click', () => $('expCoverFile').click());
$('expCoverFile')?.addEventListener('change', async (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const st = $('expCoverState');
  st.textContent = '上传中…';
  try {
    const r = await fetch('/api/studio/cover?name=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f,
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    expCover = j.path;
    rememberExport(exportOptions());     // 封面立刻记进工程
    markDirty();
    renderExpCover();
  } catch (err) {
    st.textContent = '封面没传上：' + err.message;
  }
});
$('btnStems').onclick = (e) => exportProject('stems', { stage: e.altKey ? 'post' : 'pre' });
$('saveState').onclick = async () => {
  if (S.conflict) {
    if (await uiConfirm('该工程已在其他位置被修改，是否覆盖？', { danger: true, okText: '覆盖' })) saveNow(true);
  } else saveNow().catch(() => {});
};
// 渲染缓存：显示占用，顺手清掉没有工程引用的片段
async function cacheInfo() {
  try {
    const r = await api('/cache_info');
    $('btnCache').textContent = `缓存 ${r.mb}MB`;
    $('btnCache').title = `缓存 ${r.files} 个文件 / ${r.mb}MB · 还在用 ${r.used} 个 · ` +
      `未被引用 ${r.orphan} 个 · 点击可清理未被引用的文件`;
    return r;
  } catch (e) {
    return null;
  }
}
$('btnCache').onclick = async () => {
  const r = await cacheInfo();
  if (!r) return;
  if (!r.orphan) { toast(`缓存 ${r.mb}MB，都是还在用的，没得清`); return; }
  if (!(await uiConfirm(`清掉 ${r.orphan} 个没被引用的片段缓存？\n` +
               '仅删除未被工程引用的片段；再次使用时将重新生成，不会丢失工程数据。'))) return;
  try {
    const c = await api('/cache_clean', { method: 'POST', json: {} });
    toast(`清掉 ${c.removed} 个 · 腾出 ${c.freed_mb}MB`);
    engine.rendered && engine.rendered.clear && engine.rendered.clear();
    cacheInfo();
  } catch (e) { toast('清理失败：' + e.message, 8000); }
};
cacheInfo();

$('btnRecRefresh').onclick = () => {
  loadRecordings().then(() => toast(`录音列表已刷新（${S.recs.length} 条）`, 1800));
};

$('btnAddTrack').onclick = () => {
  const tr = addTrack();
  if (tr) toast(`加了「${tr.name}」，双击混音台上的名字可以改`, 2600);
};

$('btnNew').onclick = async () => {
  if (S.dirty && !(await uiConfirm('当前工程没保存完，继续新建？', { danger: true, okText: '继续' }))) return;
  newProject()
    .then(() => loadProjectList())
    .catch(e => uiAlert('新建失败：' + e.message));
};

// ---- 工程列表 ----
async function loadProjectList() {
  const sel = $('projList');
  let list = [];
  try { list = (await api('/projects')).projects || []; } catch (e) { list = []; }
  sel.textContent = '';
  if (!list.length) {
    const o = document.createElement('option');
    o.textContent = '还没有工程'; o.value = '';
    sel.appendChild(o);
    return;
  }
  for (const p of list) {
    const o = document.createElement('option');
    o.value = p.name;
    o.textContent = (p.title || p.name);
    sel.appendChild(o);
  }
  if (S.file) sel.value = S.file;
}
$('projList').onchange = async (e) => {
  const v = e.target.value;
  if (!v || v === S.file) return;
  if (S.dirty && !(await uiConfirm('当前工程还没存完，切走？', { danger: true, okText: '切走' }))) {
    e.target.value = S.file; return;
  }
  loadProject(v)
    .then(() => loadProjectList())
    .catch(err => { uiAlert('打开失败：' + err.message); e.target.value = S.file; });
};

// 删工程：只删工程文件（媒体库是共享的，一个音频都不动）。
// ★ 删的正好是当前打开的：必须先掐掉自动保存的计时器 ——
//   否则 800ms 后它会把刚删掉的工程原样写回来，看起来像「删不掉」。
async function deleteProject(name) {
  const file = name || S.file;
  if (!file) throw new Error('没有工程');
  await api('/projects/' + encodeURIComponent(file), { method: 'DELETE' });
  if (file === S.file) {
    clearTimeout(S.saveTimer); S.saveTimer = 0; S.dirty = false;
    const r = await api('/projects');
    const rest = (r.projects || []).filter(p => p.name !== file);
    if (rest.length) await loadProject(rest[0].name);
    else await newProject();          // 一个都不剩：开一个新的空工程
  }
  await loadProjectList();
  setSaveState(`已删除工程「${file}」`);
  return true;
}

$('btnDelProj').onclick = async () => {
  if (!S.file) { setSaveState('先打开一个工程'); return; }
  const shown = $('projName').value || S.file;
  if (!(await uiConfirm(`删掉工程「${shown}」?\n\n只删工程文件 —— 导入的音频还留在媒体库，别的工程不受影响。\n删了就找不回来了。`,
      { title: '删除工程', danger: true, okText: '删除' }))) return;
  deleteProject(S.file).catch(e => uiAlert('删除失败：' + e.message));
};

$('btnQuant').onclick = () => {
  const n = quantizeSelected(1);
  if (!n) { setSaveState('先选块'); setTimeout(() => setSaveState('已保存'), 1200); }
  else setSaveState(t('对拍了 {0} 块').replace('{0}', n) + ' · ' + t('0 渲染'));
};

// ---------------------------------------------------------------- 右键菜单
const ctxEl = document.createElement('div');
ctxEl.id = 'ctx';
document.body.appendChild(ctxEl);

function hideCtx() { ctxEl.classList.remove('on'); }
window.addEventListener('pointerdown', (e) => {
  if (!ctxEl.contains(e.target)) hideCtx();
}, true);
window.addEventListener('blur', hideCtx);

function showCtx(x, y, items) {
  ctxEl.textContent = '';
  for (const it of items) {
    if (it.sep) {
      const s = document.createElement('div');
      s.className = 'sp';
      ctxEl.appendChild(s);
      continue;
    }
    const d = document.createElement('div');
    d.className = 'ci' + (it.dis ? ' dis' : '');
    const t = document.createElement('span');
    t.textContent = it.label;
    d.appendChild(t);
    if (it.key) {
      const k = document.createElement('span');
      k.className = 'k'; k.textContent = it.key;
      d.appendChild(k);
    }
    if (!it.dis) d.onclick = () => { hideCtx(); it.act(); };
    ctxEl.appendChild(d);
  }
  ctxEl.classList.add('on');
  const r = ctxEl.getBoundingClientRect();
  ctxEl.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 6)) + 'px';
  ctxEl.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 6)) + 'px';
}

function resetClip(clip) {
  clip.semis = 0; clip.cents = 0;
  delete clip.pitchCurve;
  clip.length = clip.srcOut - clip.srcIn;
  clip.renderKey = null; clip.renderUrl = null; clip.needsRender = false;
}

area.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  if (!S.proj) return;
  const r = area.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;

  // 标尺：标记的加/改/挪/删，以及「在这里切分」
  if (y < RULER_H) {
    const mk = markerHit(x, y);
    if (mk) {
      showCtx(e.clientX, e.clientY, [
        { label: '重命名…', act: async () => {
          const v = await uiPrompt('标记名', mk.name, { title: '重命名标记' });
          if (v == null) return;
          takeSnap(); mk.name = String(v).trim().slice(0, 16) || mk.name;
          markDirty(); drawStatic();
        } },
        { label: '把播放头放到这里', act: () => seekTo(mk.t) },
        { label: '在这里切分选中的块', act: () => {
          const old = S.playAt;
          S.playAt = mk.t;
          const n = splitSelected();
          S.playAt = old;
          drawOverlay(); drawStatic();
          if (!n) setSaveState('没切到：选中的块不包含这个位置');
        } },
        { sep: true },
        { label: '删除标记', act: () => markerDelete(mk.id) },
      ]);
      return;
    }
    showCtx(e.clientX, e.clientY, [
      { label: '在这里加标记', key: 'M', act: () => {
        const m = markerAdd(snapT(Math.max(0, tOf(x))));
        if (m) setSaveState(`加了标记「${m.name}」`);
      } },
      { label: '把播放头放到这里', act: () => seekTo(snapT(Math.max(0, tOf(x)))) },
    ]);
    return;
  }

  // 表头：轨道的静音/独奏/重命名都在这一层
  if (x < HEAD_W) {
    const row = rowAt(y);
    if (!row) return;
    S.selTrack = row.tr.id;
    showCtx(e.clientX, e.clientY, [
      { label: row.tr.mute ? '取消静音' : '静音', act: () => { takeSnap(); row.tr.mute = !row.tr.mute; afterStructure(); } },
      { label: row.tr.solo ? '取消独奏' : '独奏', act: () => { takeSnap(); row.tr.solo = !row.tr.solo; afterStructure(); } },
      { sep: true },
      { label: '改名…', act: async () => {
        const v = await uiPrompt('轨道名', row.tr.name, { title: '重命名轨道' });
        if (v) { takeSnap(); row.tr.name = v.slice(0, 24); afterStructure(); }
      } },
      { label: '删除这条轨', act: async () => {
        if (!(await uiConfirm(`删掉「${row.tr.name}」和上面所有块？`, { danger: true, okText: '删除' }))) return;
        takeSnap();
        const i = S.proj.tracks.indexOf(row.tr);
        if (i >= 0) S.proj.tracks.splice(i, 1);
        S.sel.clear(); S.selTrack = null;
        afterStructure();
      } },
    ]);
    return;
  }

  const hit = clipAt(x, y);
  if (hit) {
    if (!S.sel.has(hit.clip.id)) {
      S.sel.clear(); S.sel.add(hit.clip.id);
      S.selTrack = hit.tr.id;
      drawStatic(); renderInspector();
    }
    const one = selectedClips().length === 1 ? hit.clip : null;
    showCtx(e.clientX, e.clientY, [
      { label: '试听这块', act: () => engine.previewClip(hit.clip) },
      { sep: true },
      { label: '复制', key: 'Ctrl+C', act: copySelected },
      { label: '剪切', key: 'Ctrl+X', act: cutSelected },
      { label: '粘贴到播放头', key: 'Ctrl+V', dis: !CLIPB, act: pasteClips },
      { label: '复制一份', key: 'Ctrl+D', act: duplicateSelected },
      { sep: true },
      { label: '在播放头切分', key: 'S', act: splitSelected },
      { label: '对拍到网格', act: () => { const n = quantizeSelected(1); if (n) setSaveState(t('对拍了 {0} 块').replace('{0}', n)); } },
      { sep: true },
      { label: `片段音量…（现在 ${Math.round(Number(hit.clip.gain ?? 1) * 100)}%）`, act: askClipGain },
      { label: '片段音量恢复 100%', act: () => setClipGainPct(selectedClips(), 100) },
      // ★ 「匹配」的操作口：让播放头这一处（比如开唱第一个字）落在某个标记上
      ...(markers().length ? [
        { sep: true },
        ...markers().map(m => ({
          label: `对齐到标记：${m.name}`,
          act: () => { if (!alignSelectedToMarker(m.id)) setSaveState('没有选中块'); },
        })),
      ] : []),
      { label: '建采样乐器', dis: !one || one.kind === 'sound', act: createSamplerFromClip },
      { sep: true },
      // ★ 拆乐器：每件乐器一条轨，才能一样一样调。拆的是**整个源文件**,
      //   不是这一块（整首的上下文才分得准）。
      { label: '拆成乐器轨（四轨：鼓/贝斯/其他/人声）', dis: !one, act: () => stemsFromClip('4') },
      { label: '拆成乐器轨（六轨：多吉他/钢琴）', dis: !one, act: () => stemsFromClip('6') },
      { sep: true },
      { label: '和声 +三度（调内）', act: () => harmonySelected('third').catch(e => setSaveState('和声失败：' + e.message)) },
      { label: '和声 +四度', act: () => harmonySelected('fourth').catch(e => setSaveState('和声失败：' + e.message)) },
      { label: '和声 +五度', act: () => harmonySelected('fifth').catch(e => setSaveState('和声失败：' + e.message)) },
      { label: '和声 +八度', act: () => harmonySelected('octave').catch(e => setSaveState('和声失败：' + e.message)) },
      { label: '加厚（双轨 L/R）', act: () => harmonySelected('double').catch(e => setSaveState('加厚失败：' + e.message)) },
      { sep: true },
      { label: '恢复原样', act: () => {
        takeSnap();
        for (const { clip } of selectedClips()) resetClip(clip);
        afterStructure();
      } },
      { label: '删除', key: 'Del', act: deleteSelected },
    ]);
    return;
  }

  // 空白处
  S.sel.clear(); drawStatic(); renderInspector();
  showCtx(e.clientX, e.clientY, [
    { label: '粘贴到播放头', key: 'Ctrl+V', dis: !CLIPB, act: pasteClips },
    { label: '全选', key: 'Ctrl+A', act: () => {
      S.sel.clear();
      for (const tr of S.proj.tracks) for (const c of tr.clips) S.sel.add(c.id);
      drawStatic(); renderInspector();
    } },
    { label: '在播放头循环这一小节', act: () => {
      const bar = barSec();
      const b0 = Math.floor(S.playAt / bar) * bar;
      S.proj.ui.loop = [b0, b0 + bar];
      $('chkLoop').checked = true;
      markDirty(); drawStatic();
    } },
  ]);
});

// Tap tempo
let taps = [];
$('btnTap').onclick = () => {
  const now = performance.now();
  taps = taps.filter(t => now - t < 2500);
  taps.push(now);
  if (taps.length >= 2) {
    const d = [];
    for (let i = 1; i < taps.length; i++) d.push(taps[i] - taps[i - 1]);
    d.sort((a, b) => a - b);
    const med = d[Math.floor(d.length / 2)];
    const bpm = clamp(60000 / med, 40, 240);
    $('inBpm').value = bpm.toFixed(1);
    takeSnap();
    S.proj.bpm = +bpm.toFixed(1);
    markDirty(); drawStatic();
  }
};
// ---- 自动测速（结果只填框，不擅自改网格） ----
function pickPath() {
  for (const { clip } of selectedClips()) {
    const s = S.proj.sources?.[clip.sourceId];
    if (s?.path) return s.path;
  }
  if (S.pick?.path) return S.pick.path;
  const tr = S.proj.tracks.find(t => t.kind === 'audio');
  const c = tr?.clips?.[0];
  if (c) return S.proj.sources?.[c.sourceId]?.path || '';
  return '';
}

async function detectBpm(path, quiet) {
  if (!path) { if (!quiet) setSaveState('先选一条录音/伴奏'); return; }
  const btn = $('btnBpm');
  const old = btn.textContent;
  btn.textContent = '测…'; btn.disabled = true;
  try {
    const r = await api('/bpm', { method: 'POST', json: { path } });
    if (r.err) throw new Error(r.err);
    $('inBpm').value = r.bpm;
    S.proj.ui = S.proj.ui || {};
    S.proj.ui.bpmHint = { bpm: r.bpm, offset: r.offset, conf: r.conf };
    markDirty();
    setSaveState(`${r.bpm} BPM · 把握 ${(r.conf || 0).toFixed(1)} · 回车生效`);
  } catch (e) {
    if (!quiet) setSaveState('测速失败：' + e.message);
    console.warn('bpm', e);
  } finally {
    btn.textContent = old; btn.disabled = false;
  }
}
$('btnBpm').onclick = () => detectBpm(pickPath(), false);

// ---- 跑调检测（★ 只标；改不改由用户点一下） ----
async function checkPitch() {
  const path = pickPath();
  if (!path) { setSaveState('先选一条录音'); return null; }
  const btn = $('btnPitch');
  const old = btn.textContent;
  btn.textContent = '查…'; btn.disabled = true;
  setSaveState('查跑调…');
  try {
    const r = await api('/check_pitch', { method: 'POST', json: { source: path } });
    S.pitchInfo = r;
    S.pitchInfoPath = path;
    markPitchFlags(path);
    setSaveState(`${r.root}${r.mode === 'minor' ? 'm' : ''} · 红 ${r.red} 黄 ${r.yellow}`);
    return r;
  } catch (e) {
    setSaveState('查跑调失败：' + e.message);
    return null;
  } finally {
    btn.textContent = old; btn.disabled = false;
  }
}

function markPitchFlags(path) {
  const r = S.pitchInfo;
  if (!r) return;
  S.pitchFlags.clear();
  let red = 0, yellow = 0;
  for (const tr of S.proj.tracks) {
    for (const c of tr.clips) {
      const src = S.proj.sources?.[c.sourceId];
      if (!src || src.path !== path) continue;
      if (c.kind === 'sound') continue;
      let b = null;
      if (c.blockId !== undefined) {
        b = r.blocks.find(x => x.blockId === c.blockId) || null;
      }
      if (!b) {
        // 没 blockId（整条拖进来的）⇒ 取重叠最多的那一块
        let best = 0;
        for (const x of r.blocks) {
          const ov = Math.min(x.t1, c.srcOut) - Math.max(x.t0, c.srcIn);
          if (ov > best) { best = ov; b = x; }
        }
        if (best < 0.05) b = null;
      }
      if (!b) continue;
      S.pitchFlags.set(c.id, b);
      if (b.status === 'red') red++;
      else if (b.status === 'yellow') yellow++;
    }
  }
  const el = $('pitchMsg');
  if (!r.blocks.length) { el.hidden = true; }
  else {
    el.hidden = false;
    el.className = 'pmsg ' + (red ? 'bad' : 'ok');
    el.textContent = red
      ? `音准偏差 ${red} 处 · 点击此处仅修正这些位置`
      : (yellow ? `无需要修正的位置（偏差过大 ${yellow} 处，不做强制校正）`
        : `整体音准准确 · ${r.root}${r.mode === 'minor' ? 'm' : ''}`);
  }
  drawStatic();
}

async function applyPitchFix() {
  const todos = [];
  for (const tr of S.proj.tracks) {
    for (const c of tr.clips) {
      const b = S.pitchFlags.get(c.id);
      if (!b || b.status !== 'red' || !b.suggest) continue;
      if (c.semis || c.cents) continue;      // ★ 已经动过的不重复叠加
      c.semis = b.suggest.semis;
      c.cents = b.suggest.cents;
      c.needsRender = true;
      todos.push(c);
    }
  }
  if (!todos.length) return 0;
  takeSnap();
  S.sel = new Set(todos.map(c => c.id));
  afterStructure();
  setSaveState(`修 ${todos.length} 处…`);
  await renderPending();
  for (const c of todos) S.pitchFlags.delete(c.id);
  $('pitchMsg').textContent = `修了 ${todos.length} 处 · 颤音原样保留`;
  $('pitchMsg').className = 'pmsg ok';
  drawStatic();
  return todos.length;
}

$('btnPitch').onclick = () => checkPitch();
$('pitchMsg').onclick = () => {
  if (S.pitchInfo && S.pitchInfo.red) applyPitchFix().catch(e => setSaveState('修失败：' + e.message));
};

// ---- 和弦检测（★ 只画一条「建议」带，不改任何块） ----
async function detectChords() {
  const path = pickPath();
  if (!path) { setSaveState('先选一条伴奏/录音'); return null; }
  const btn = $('btnChords');
  const old = btn.textContent;
  btn.textContent = '查…'; btn.disabled = true;
  setSaveState('测和弦…');
  try {
    const r = await api('/chords', { method: 'POST', json: { source: path } });
    S.chords = r.chords || [];
    S.chordsPath = path;
    drawStatic();
    const names = [...new Set(S.chords.map(c => c.name))].slice(0, 8).join(' ');
    setSaveState(S.chords.length ? `和弦 ${S.chords.length} 段 · ${names}` : '没测出和弦');
    return S.chords;
  } catch (e) {
    setSaveState('测和弦失败：' + e.message);
    return null;
  } finally {
    btn.textContent = old; btn.disabled = false;
  }
}
$('btnChords').onclick = () => detectChords();

// ---- 和声 / 加厚（★ 全是「同一份 AudioBuffer 换个音高再排一次」） ----
const NOTE_PC = { C: 0, 'C#': 1, D: 2, 'D#': 3, E: 4, F: 5, 'F#': 6, G: 7, 'G#': 8, A: 9, 'A#': 10, B: 11 };

// 这个块对应检测结果里的哪一块（★ 块没有 note 字段时靠它兜底）
function blockForClip(clip, path) {
  const f = S.pitchFlags.get(clip.id);
  if (f) return f;
  const r = S.pitchInfo;
  if (!r || !r.blocks) return null;
  if (path && S.pitchInfoPath && S.pitchInfoPath !== path) return null;
  if (clip.blockId !== undefined) {
    const m = r.blocks.find(x => x.blockId === clip.blockId);
    if (m) return m;
  }
  let best = 0, out = null;
  for (const x of r.blocks) {
    const ov = Math.min(x.t1, clip.srcOut) - Math.max(x.t0, clip.srcIn);
    if (ov > best) { best = ov; out = x; }
  }
  return best >= 0.05 ? out : null;
}

function midiName(m) {
  const names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  return names[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1);
}

function noteMidiOf(clip, path) {
  if (clip.note) {
    const m = /^([A-G]#?)(-?\d+)$/.exec(String(clip.note));
    if (m) return 12 * (+m[2] + 1) + NOTE_PC[m[1]];
  }
  const b = blockForClip(clip, path);
  if (b && b.f0 > 0) return 69 + 12 * Math.log2(b.f0 / 440);
  return null;
}

// 调内的七个音级（拿跑调检测判出的调；没有就按 C 大调）
function keyPcs() {
  const names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  let root = 0, minor = false;
  const r = S.pitchInfo;
  if (r && r.root) {
    const i = names.indexOf(r.root);
    if (i >= 0) root = i;
    minor = r.mode === 'minor';
  }
  const steps = minor ? [0, 2, 3, 5, 7, 8, 10] : [0, 2, 4, 5, 7, 9, 11];
  return steps.map(x => (x + root) % 12).sort((a, b) => a - b);
}

// 在音阶里往上走 steps 级（C 大调里 E 的「三度」是 G = +3，不是死板的 +4）
function diatonicShift(midi, steps, pcs) {
  const arr = pcs;
  const pc = ((Math.round(midi) % 12) + 12) % 12;
  let deg = arr.indexOf(pc);
  if (deg < 0) {                       // 不在调内 ⇒ 先贴到最近的音级
    let bd = 99;
    arr.forEach((p, i) => {
      const d = Math.min(Math.abs(p - pc), 12 - Math.abs(p - pc));
      if (d < bd) { bd = d; deg = i; }
    });
  }
  const d = deg + steps;
  return arr[d % 7] - arr[deg] + 12 * Math.floor(d / 7);
}

const HARM = {
  third: { steps: 2, fb: 4, name: '和声 三度' },
  fourth: { steps: 3, fb: 5, name: '和声 四度' },
  fifth: { steps: 4, fb: 7, name: '和声 五度' },
  octave: { steps: 7, fb: 12, name: '和声 八度' },
};

async function harmonySelected(kind) {
  const sel = selectedClips().filter(({ clip }) => clip.kind !== 'sound');
  if (!sel.length) { setSaveState('先选人声块再加和声'); return 0; }
  const path = S.proj.sources?.[sel[0].clip.sourceId]?.path || '';
  // ★ 不知道调就先判调（只读分析，一个块都不改）
  if (kind !== 'double' && (!S.pitchInfo || (path && S.pitchInfoPath !== path))) {
    await checkPitch();
  }
  const pcs = keyPcs();
  const plans = kind === 'double'
    ? [{ name: '加厚 L', pan: -0.5, delay: 0.012, gain: 0.5 },
       { name: '加厚 R', pan: 0.5, delay: 0.021, gain: 0.5 }]
    : [{ name: HARM[kind].name, pan: -0.35, delay: 0.015, gain: 0.65 }];

  takeSnap();
  const made = [];
  for (const p of plans) {
    const tr = newTrack('harmony', p.name);
    tr.pan = p.pan; tr.gain = p.gain;
    for (const { clip } of sel) {
      if (!S.proj.sources?.[clip.sourceId]) continue;
      let shift = 0, noteOut = clip.note || '';
      if (kind !== 'double') {
        const midi = noteMidiOf(clip, path);
        if (midi == null) shift = HARM[kind].fb;
        else {
          shift = diatonicShift(midi, HARM[kind].steps, pcs);
          noteOut = midiName(Math.round(midi) + shift);
        }
      }
      const n = clipPayload(clip);
      if (noteOut) n.note = noteOut;
      n.id = uid('cl');
      n.start = Math.max(0, clip.start + p.delay);
      n.semis = (clip.semis || 0) + shift;
      n.cents = clip.cents || 0;
      n.needsRender = !!clip.needsRender || shift !== 0;
      if (shift !== 0) { n.renderKey = null; n.renderUrl = null; }
      tr.clips.push(n);
      made.push(n);
    }
  }
  if (!made.length) { S.undo.pop(); return 0; }
  tr_sort();
  S.sel = new Set(made.map(c => c.id));
  S.selTrack = S.proj.tracks[S.proj.tracks.length - 1]?.id;
  afterStructure();
  setSaveState(`${plans.map(p => p.name).join(' + ')} · ${t('{0} 块').replace('{0}', made.length)} · ${t('正在渲染…')}`);
  await renderPending();
  return made.length;
}

// 改名
$('projName').onchange = () => {
  S.proj.title = $('projName').value;
  markDirty();
};

// ---------------------------------------------------------------- 键盘
window.addEventListener('keydown', (e) => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (['input', 'select', 'textarea'].includes(tag)) return;
  const mod = e.ctrlKey || e.metaKey;
  // 整页块编辑开着：Escape 先关这一页（不顺手清掉主时间轴的选中）
  if (BIG.open && e.key === 'Escape') { e.preventDefault(); bigClose(); return; }
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); return; }
  if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelected(); return; }
  if (mod && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
    return;
  }
  if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
  if (mod && e.key.toLowerCase() === 's') {
    e.preventDefault(); saveNow(true).catch(() => {}); return;
  }
  if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelected(); return; }
  if (mod && e.key.toLowerCase() === 'c') { e.preventDefault(); copySelected(); return; }
  if (mod && e.key.toLowerCase() === 'x') { e.preventDefault(); cutSelected(); return; }
  if (mod && e.key.toLowerCase() === 'v') { e.preventDefault(); pasteClips(); return; }
  if (!mod && e.key.toLowerCase() === 's') { e.preventDefault(); splitSelected(); return; }
  if (!mod && e.key.toLowerCase() === 'm') {
    e.preventDefault();
    const m = markerAdd(S.playAt);
    if (m) setSaveState(`加了标记「${m.name}」`);
    return;
  }
  if (mod && e.key.toLowerCase() === 'a') {
    e.preventDefault();
    S.sel.clear();
    for (const tr of S.proj.tracks) for (const c of tr.clips) S.sel.add(c.id);
    drawStatic(); renderInspector();
    return;
  }
  if (e.key === 'Escape') { S.sel.clear(); drawStatic(); renderInspector(); return; }
  if (e.key === 'Home') { e.preventDefault(); seekTo(0); return; }
  if (e.key === 'End') { e.preventDefault(); seekTo(projectEnd(S.proj)); return; }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault();
    const dir = e.key === 'ArrowRight' ? 1 : -1;
    // 有选中的块 ⇒ 左右微调位置（对齐用）；没有选中才挪播放头
    if (selectedClips().length) {
      const g = gridSec();
      const step = e.altKey ? 0.001 : (e.shiftKey ? 0.010 : (g > 0 ? g : 0.010));
      nudgeSelected(dir * step);
      return;
    }
    const g = gridSec() || 0.1;
    seekTo(Math.max(0, S.playAt + dir * g));
  }
});

// ---------------------------------------------------------------- 钢琴卷帘
const RK = { keyW: 44, rulerH: 22, rowH: 12, hi: 96 };
let rollDrag = null;

function rollTrack() {
  // 鼓轨和采样轨共用同一个卷帘：鼓组也是「一个 midi 一行对应一个样本」，结构完全一样
  const pick = k => S.proj?.tracks.find(t => t.kind === k && t.id === S.rollTrack);
  return pick('sampler') || pick('drum')
    || S.proj?.tracks.find(t => t.kind === 'sampler')
    || S.proj?.tracks.find(t => t.kind === 'drum') || null;
}
function rollOfY(y) { return RK.hi - Math.floor((y - RK.rulerH + S.rollY) / RK.rowH); }
function yOfRoll(m) { return RK.rulerH + (RK.hi - m) * RK.rowH - S.rollY; }
function rxOf(t) { return RK.keyW + (t - S.view.t0) * S.view.pps; }
function rtOf(x) { return S.view.t0 + (x - RK.keyW) / S.view.pps; }

function drawRoll() {
  const c = $('rollCanvas');
  if (!c || !S.proj) return;
  const box = c.parentElement.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const W2 = Math.max(60, Math.floor(box.width)), H2 = Math.max(60, Math.floor(box.height));
  if (c.width !== W2 * dpr || c.height !== H2 * dpr) {
    c.width = W2 * dpr; c.height = H2 * dpr;
  }
  const g = c.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W2, H2);
  g.fillStyle = '#0b0e14'; g.fillRect(0, 0, W2, H2);

  const tr = rollTrack();
  const beat = beatSec(), bar = barSec();

  // 音高行（鼓轨：一行一件鼓，左边写中文名）
  const drum = tr?.kind === 'drum';
  const kitRows = new Map();
  if (drum) for (const s of tr?.sample?.pool || []) {
    kitRows.set((tr.sample.baseMidi || 36) + (+s.semis || 0), s.label || '');
  }
  const m0 = Math.max(0, rollOfY(H2)), m1 = Math.min(127, rollOfY(RK.rulerH));
  for (let m = m0; m <= m1; m++) {
    const y = yOfRoll(m);
    const inKit = kitRows.has(m);
    const black = [1, 3, 6, 8, 10].includes(m % 12);
    g.fillStyle = drum ? (inKit ? '#131922' : '#0a0d12') : (black ? '#0d1118' : '#11151d');
    g.fillRect(RK.keyW, y, W2 - RK.keyW, RK.rowH);
    g.strokeStyle = '#161c27';
    g.beginPath(); g.moveTo(RK.keyW, y + RK.rowH - .5); g.lineTo(W2, y + RK.rowH - .5); g.stroke();
    // 键盘
    g.fillStyle = drum ? (inKit ? '#2a3342' : '#12161d') : (black ? '#1a2029' : '#d7dce5');
    g.fillRect(0, y, RK.keyW - 4, RK.rowH - 1);
    if (drum) {
      if (inKit) {
        g.fillStyle = '#d7dce5';
        g.font = '10px "Microsoft YaHei",sans-serif';
        g.fillText(kitRows.get(m).slice(0, 3), 3, y + RK.rowH - 2);
      }
    } else if (m % 12 === 0) {
      g.fillStyle = '#4a5468';
      g.font = '9px Consolas,monospace';
      g.fillText('C' + (m / 12 - 1), 3, y + RK.rowH - 3);
    }
  }

  // 网格
  const s0 = Math.floor(S.view.t0 / bar) - 1, s1 = Math.ceil(rtOf(W2) / bar) + 1;
  for (let b = Math.max(0, s0); b <= s1; b++) {
    const x = rxOf(b * bar);
    if (x >= RK.keyW && x < W2) {
      g.strokeStyle = '#1e2636';
      g.beginPath(); g.moveTo(x + .5, RK.rulerH); g.lineTo(x + .5, H2); g.stroke();
    }
    for (let k = 1; k < (S.proj.timeSig || [4, 4])[0]; k++) {
      const bx = rxOf(b * bar + k * beat);
      if (bx < RK.keyW || bx >= W2) continue;
      g.strokeStyle = '#151b27';
      g.beginPath(); g.moveTo(bx + .5, RK.rulerH); g.lineTo(bx + .5, H2); g.stroke();
    }
  }

  // 标尺
  g.fillStyle = '#11151d'; g.fillRect(0, 0, W2, RK.rulerH);
  g.strokeStyle = '#242a38';
  g.beginPath(); g.moveTo(0, RK.rulerH - .5); g.lineTo(W2, RK.rulerH - .5); g.stroke();
  g.fillStyle = '#7f8ba1'; g.font = '10px Consolas,monospace';
  for (let b = Math.max(0, s0); b <= s1; b++) {
    const x = rxOf(b * bar);
    if (x < RK.keyW || x > W2) continue;
    g.fillText(String(b + 1), x + 3, 13);
  }

  // 音符
  const smp = tr?.sample;
  if (tr) {
    const lo = smp?.poolLo ?? -7, hi = smp?.poolHi ?? 7;
    for (const nt of tr.notes || []) {
      const x0 = rxOf(nt.start), x1 = rxOf(nt.start + nt.length);
      if (x1 < RK.keyW || x0 > W2) continue;
      const y = yOfRoll(nt.midi);
      const semi = Math.round(nt.midi - (smp?.baseMidi ?? 60));
      const out = semi < lo || semi > hi;
      const sel = S.noteSel.has(nt.id);
      g.fillStyle = out ? '#4a2b2b' : (sel ? '#7fe0ff' : '#3f8fc4');
      roundRect(g, Math.max(RK.keyW, x0), y + 1, Math.max(3, x1 - Math.max(RK.keyW, x0)), RK.rowH - 2, 3);
      g.fill();
      g.strokeStyle = sel ? '#e9f8ff' : 'rgba(0,0,0,.45)';
      g.stroke();
      if (x1 - x0 > 26) {
        g.fillStyle = out ? '#c99' : '#04121c';
        g.font = '9px Consolas,monospace';
        g.fillText(String(nt.midi), Math.max(RK.keyW, x0) + 3, y + RK.rowH - 3);
      }
    }
  }

  // 播放头
  const px = rxOf(S.playAt);
  if (px >= RK.keyW && px <= W2) {
    g.fillStyle = '#ff8a3d';
    g.fillRect(px, 0, 1, H2);
  }

  const hint = $('panelRoll').querySelector('.pnote');
  if (hint) hint.hidden = !!tr;
  /* 轨道名是用户数据不能进字典键，只翻后面那截固定提示 */
  $('hint').textContent = tr
    ? `${tr.name} · ${t('点空白画音符 · 拖右边缘改长短 · 右键删')} · ${smp?.note || ''}`
    : '';
}

function rollScrollTo(midi) {
  const box = $('rollCanvas').parentElement.getBoundingClientRect();
  const target = (RK.hi - midi) * RK.rowH - (box.height * 0.55);
  S.rollY = clamp(target, 0, Math.max(0, (RK.hi - 24) * RK.rowH - box.height + RK.rulerH));
  drawRoll();
}

$('rollCanvas').addEventListener('pointerdown', async (e) => {
  const tr = rollTrack();
  if (!tr) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  if (y < RK.rulerH) {
    seekTo(snapT(Math.max(0, rtOf(x))));
    return;
  }
  if (x < RK.keyW) {
    const m = clamp(rollOfY(y), 0, 127);
    await engine.resume();
    engine.previewNote(tr, { midi: m, length: 0.4, vel: 0.9 });
    return;
  }
  e.currentTarget.setPointerCapture(e.pointerId);

  // 命中已有音符？
  let hit = null;
  for (const nt of tr.notes) {
    if (nt.midi !== rollOfY(y)) continue;
    const x0 = rxOf(nt.start), x1 = rxOf(nt.start + nt.length);
    if (x >= x0 - 1 && x <= x1 + 1) { hit = nt; break; }
  }
  if (hit) {
    S.noteSel = new Set([hit.id]);
    const x1 = rxOf(hit.start + hit.length);
    rollDrag = {
      tr, note: hit, mode: x1 - x < 5 ? 'resize' : 'move',
      x0: x, y0: y, t0: hit.start, m0: hit.midi, l0: hit.length,
    };
    engine.previewNote(tr, hit);
  } else {
    // 鼓轨：只有鼓组里有的那一行才算数，点到空白行不给加（加了也发不出声）
    const mNow = clamp(rollOfY(y), 0, 127);
    if (tr.kind === 'drum') {
      const base = tr.sample?.baseMidi ?? 36;
      const inKit = (tr.sample?.pool || []).some(s => base + (+s.semis || 0) === mNow);
      if (!inKit) { setSaveState('这一行没有鼓，点左边写着鼓名的那几行'); return; }
    }
    takeSnap();
    const t = Math.max(0, snapT(rtOf(x)));
    const g0 = gridSec() || beatSec() / 4;
    const nt = { id: uid('nt'), start: t, length: g0, midi: mNow, vel: 0.9 };
    tr.notes.push(nt);
    S.noteSel = new Set([nt.id]);
    rollDrag = { tr, note: nt, mode: 'resize', x0: x, y0: y, t0: t, m0: nt.midi, l0: nt.length };
    engine.previewNote(tr, nt);
  }
  drawRoll(); markDirty();
});

$('rollCanvas').addEventListener('pointermove', (e) => {
  if (!rollDrag) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const d = (x - rollDrag.x0) / S.view.pps;
  const nt = rollDrag.note;
  if (rollDrag.mode === 'move') {
    nt.start = Math.max(0, snapT(rollDrag.t0 + d));
    const m = clamp(rollDrag.m0 - Math.round((y - rollDrag.y0) / RK.rowH), 0, 127);
    if (m !== nt.midi) { nt.midi = m; engine.previewNote(rollDrag.tr, nt); }
  } else {
    const end = snapT(rollDrag.t0 + rollDrag.l0 + d);
    nt.length = Math.max(gridSec() || 0.05, end - nt.start);
  }
  drawRoll();
});

$('rollCanvas').addEventListener('pointerup', () => {
  if (!rollDrag) return;
  rollDrag = null;
  markDirty();
  if (engine.playing) engine.play(S.proj, engine.songNow());
});

$('rollCanvas').addEventListener('contextmenu', (e) => {
  e.preventDefault();
  const tr = rollTrack();
  if (!tr) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const m = rollOfY(y);
  const i = tr.notes.findIndex(nt => nt.midi === m
    && x >= rxOf(nt.start) - 1 && x <= rxOf(nt.start + nt.length) + 1);
  if (i >= 0) {
    takeSnap();
    tr.notes.splice(i, 1);
    afterStructure();
    drawRoll();
  }
});

$('rollCanvas').addEventListener('wheel', (e) => {
  if (!rollTrack()) return;
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const anchor = rtOf(x);
    S.view.pps = clamp(S.view.pps * Math.pow(1.0016, -e.deltaY), 6, 900);
    S.view.t0 = Math.max(0, anchor - (x - RK.keyW) / S.view.pps);
    drawStatic();
  } else {
    S.rollY = clamp(S.rollY + e.deltaY, 0, (RK.hi - 24) * RK.rowH);
  }
  drawRoll();
}, { passive: false });

// 从选中的块建一条采样轨
async function createSamplerFromClip() {
  const sel = selectedClips()[0];
  if (!sel) return;
  const { clip } = sel;
  const src = S.proj.sources?.[clip.sourceId];
  if (!src) { uiAlert('这个块的源文件找不到了'); return; }
  const box = $('iCost');
  if (box) box.textContent = '建采样池…';
  try {
    const r = await api('/pool', {
      method: 'POST',
      json: { source: src.path, srcIn: clip.srcIn, srcOut: clip.srcOut, lo: -7, hi: 7 },
    });
    takeSnap();
    const tr = newTrack('sampler', t('采样') + ' ' + (clip.note || niceName(src.name)).slice(0, 10));
    tr.sample = {
      sourceId: clip.sourceId, srcIn: clip.srcIn, srcOut: clip.srcOut,
      f0: r.f0, baseMidi: r.baseMidi, note: r.note,
      poolLo: -7, poolHi: 7, pool: r.entries,
    };
    tr.attack = 0.006; tr.release = 0.06; tr.notes = [];
    S.rollTrack = tr.id;
    // 默认 C4 附近画一个，提示这是干什么的
    const m = Math.round(r.baseMidi);
    tr.notes.push({ id: uid('nt'), start: snapT(S.playAt), length: Math.max(0.25, beatSec() / 2), midi: m, vel: 0.9 });
    afterStructure();
    document.querySelector('.tabs button[data-tab="roll"]').click();
    rollScrollTo(m);
    setSaveState(`采样池 ${r.entries.length} 条 · ${r.note}`);
  } catch (e) {
    if (box) box.textContent = '建采样失败：' + e.message;
  }
}

// ---------------------------------------------------------------- VST3（离线）
// ★ 浏览器加载不了 VST3。插件在 Python 子进程里离线算：
//   ① 冻结：把「轨道增益 → 内置链 → VST」烘焙成一条 WAV，正常播放就播它；
//   ② 短段试听：只算 4 秒，边拉参数边刷新（150ms 节流）。
// ★ 插件名和参数名是插件自己报的英文。这里只做「显示层」翻译：
//   存进工程里的仍然是英文 key / 原始插件名 —— 换台机器或插件升级都不会因为翻译对不上。
const VST_CN_NAME = {
  'Airwindows Consolidated': 'Airwindows 合集',
  'DragonflyEarlyReflections': 'Dragonfly 早期反射',
  'DragonflyHallReverb': 'Dragonfly 大厅混响',
  'DragonflyRoomReverb': 'Dragonfly 房间混响',
  'DragonflyPlateReverb': 'Dragonfly 板式混响',
  'TDR Nova': 'TDR Nova 动态均衡',
  'TDR Kotelnikov': 'TDR Kotelnikov 压缩',
  'TDR Molotok': 'TDR Molotok 染色压缩',
  'TDR VOS SlickEQ': 'TDR SlickEQ 均衡',
};
function vstCnName(name) {
  return VST_CN_NAME[name] || String(name || 'VST').replace(/([a-z])([A-Z])/g, '$1 $2');
}

// 每个插件「是什么、怎么用」。没收录的插件不硬编，照样能用，只是没有说明。
const VST_GUIDE = {
  'TDR Nova': {
    kind: '动态均衡器（EQ）',
    what: '按频段单独调整音量，并可设置阈值，仅在对应频段过响时自动压缩。人声处理中最常用的工具。',
    tips: [
      '低切：选中 HP，频率 80–100Hz —— 去掉轰隆声，人声立刻干净。',
      '提亮：第 1–2 段选 Bell,2–4kHz 提 +1~+3dB。',
      '齿音：6–8kHz 那一段，把「动态（Dyn）」打开，阈值 -30 左右，只压刺耳的部分。',
    ],
  },
  'TDR Kotelnikov': {
    kind: '压缩器（母带级）',
    what: '按比例压缩超过阈值的峰值，使整体更饱满、更响且不产生削顶。适用于人声轨或总线。',
    tips: [
      '默认阈值 0dB 几乎不动作 —— 想听出效果，先把「阈值（Threshold）」拉到 -20 ~ -30。',
      '比率（Ratio）2–4 最自然；越高压得越狠。',
      '压缩造成的电平下降可用「补偿增益（Makeup）」补回，否则整体音量会偏低。',
    ],
  },
  'TDR Molotok': {
    kind: '压缩器（染色重）',
    what: '相比 Kotelnikov 音色更明显、压缩感更强，适用于鼓、贝斯，也可增强人声力度。',
    tips: [
      '默认阈值不低，想明显听到效果先把「阈值（Thresh.）」拉到 -25 ~ -35。',
      '「模式（Mode）」可在压缩、限制等行为间切换，建议先使用默认值。',
      '压缩造成的电平下降可用「补偿增益（Makeup）」补回；「干湿比（DryMix）」可实现并联压缩。',
    ],
  },
  'TDR VOS SlickEQ': {
    kind: '均衡器（EQ，带染色）',
    what: '三段 EQ，分低/中/高，带几种「调音台风格」的音色。比 Nova 简单，适合先快速把声音调顺。',
    tips: [
      '低段（Low）处理 100Hz 附近的厚薄，中段（Mid）管人声的「鼻子」，高段（High）管亮度和空气感。',
      '建议先小幅调整 ±2 dB，无明显差异后再增大；EQ 单次调整幅度过大将导致音色失真。',
      '「输出增益（Output）」用于补回整体电平，便于与干声进行 A/B 对比。',
    ],
  },
  'DragonflyHallReverb': {
    kind: '大厅混响',
    what: '大空间、长尾音，接近音乐厅效果。适合为副歌增加空间感。',
    tips: [
      '先调「干声电平 / 晚期混响电平（Dry / Late）」——干声 1.0、混响 0.25 起步。',
      '「衰减（Decay）」2–3 秒即可；过长会导致人声浑浊。',
      '「预延迟（Predelay）」20–40 ms 可使人声更靠前。',
    ],
  },
  'DragonflyRoomReverb': {
    kind: '房间混响',
    what: '小空间、短衰减，接近普通房间。可减轻干声的贴耳感，但不会明显变湿。',
    tips: [
      '「空间大小（Size）」3–8 米最接近真实房间。',
      '干声 1.0、早期/晚期各 0.2–0.3 起步。',
      '四个混响中染色最少，适合作为整首歌的默认选择。',
    ],
  },
  'DragonflyPlateReverb': {
    kind: '板式混响',
    what: '经典录音棚板式混响，明亮、密集、尾音平滑。流行人声最常用的混响类型，比大厅混响更易听清。',
    tips: [
      '「衰减（Decay）」1.5–2.5 秒。',
      '将「高频衰减（Dampen）」调低（例如 6–8 kHz）可柔化音色，减少刺耳感。',
      '干声 1.0、湿声 0.2–0.35 起步。',
    ],
  },
  'DragonflyEarlyReflections': {
    kind: '早期反射',
    what: '仅添加声音到达墙面后的早期反射，不延长尾音。可增加人声的空间感，同时保持清晰度。',
    tips: [
      '与其他混响择一使用即可，叠加容易导致音色浑浊。',
      '干声 1.0、湿声 0.15–0.3 起步。',
      '想「有房间但不湿」的时候用它。',
    ],
  },
  'Airwindows Consolidated': {
    kind: '效果合集',
    what: '单个插件集成了多种 Airwindows 效果，通过「算法（Replace）」切换。适合为声音添加染色，不属于精确处理工具。',
    tips: [
      '先选算法（Replace），再调亮度/失谐/宏大这三个「口味」旋钮。',
      '干湿比（Dry/Wet）先放在 0.5 左右试。',
      '每次渲染可能存在细微差异 —— 界面会标注，导出以最终结果为准。',
    ],
  },
};
function vstGuideOf(name) {
  if (VST_GUIDE[name]) return VST_GUIDE[name];
  const compact = String(name || '').replace(/\s+/g, '');
  for (const k of Object.keys(VST_GUIDE)) {
    if (k.replace(/\s+/g, '') === compact) return VST_GUIDE[k];
  }
  return null;
}

// 参数名翻译：先查整表，再按 band_N_/hp_/lp_/wide_band_ 前缀拼，最后按单词替换。
const VST_PARAM_CN = {
  threshold_db: '阈值', threshold: '阈值', thresh_db: '阈值', knee: '拐点',
  ratio: '压缩比', attack_ms: '启动', release_peak_ms: '峰值释放',
  release_rms_ms: 'RMS 释放', release_ms: '释放',
  makeup_db: '补偿增益', out_gain_db: '输出增益',
  dry_mix_db: '干声混合', dry_mix: '干湿比', dry_wet: '干湿比',
  dry_mix_mode: '干声模式', dry_level: '干声电平', wet_level: '湿声电平',
  early_level: '早期反射电平', late_level: '晚期混响电平',
  early_send: '早期反射发送', input_level: '输入电平', output_level: '输出电平',
  mono_behaviour: '单声道处理', buffer_size_frames: '缓冲大小',
  sample_rate_frames: '采样率', size_m: '空间大小', width: '立体声宽度',
  predelay_ms: '预延迟', diffuse: '扩散度', decay_s: '衰减时间',
  modulation: '调制', spin_hz: '旋转速度', wander_ms: '漂移', wander: '漂移',
  low_cut_hz: '低切', high_cut_hz: '高切',
  low_cross_hz: '低频分频点', high_cross_hz: '高频分频点',
  low_mult_x: '低频倍数', high_mult_x: '高频倍数',
  dampen_hz: '高频衰减', early_damp_hz: '早期反射衰减',
  late_damp_hz: '晚期混响衰减', low_boost: '低频提升',
  boost_freq_hz: '提升频率', algorithm: '算法', replace: '算法',
  brightness: '亮度', detune: '失谐', bigness: '宏大',
  peak_crest_db: '峰值因数', soft_knee_db: '软拐点',
  sc_hp_freq_hz: '侧链高通频率', sc_hp_slope: '侧链高通斜率',
  sc_stereo_diff: '侧链立体声差', delta: '只听差异',
  bypass: '旁通', mixed_bypass: '旁通',
  quality: '音质档', mode: '模式', program: '预设',
  gr_disp_scale: '压缩表刻度', gr_disp_mode: '压缩表模式',
  channels: '声道', solo: '独听', analyzer_mode: '频谱显示',
  display_eq_range: '频谱显示范围', display_fft_floor: '频谱底噪',
  sidechain_mode: '侧链模式', analyzer_speed: '频谱刷新速度',
  master_bypass: '总旁通', dynamic_delta: '动态差异',
  output_gain_db: '输出增益', eq_auto_gain: 'EQ 自动增益',
  sc_filter_freq_hz: '侧链滤波频率', sc_filter_on: '侧链滤波开关',
  drymix: '干湿比', output_db: '输出电平', stereo_mode: '立体声模式',
  meter_range: '电平表范围', meter_mode: '电平表模式',
  meter_source: '电平表来源', meter_speed: '电平表速度',
  // band / hp / lp / wide_band 的后缀
  selected: '选中', active: '启用', gain_db: '增益', q: 'Q 值',
  frequency_hz: '频率', type: '类型', dyn: '动态',
  split: '分割',
};
const VST_PARAM_WORDS = [
  ['Release Peak', '峰值释放'], ['Release RMS', 'RMS 释放'],
  ['Early Level', '早期反射电平'], ['Late Level', '晚期混响电平'],
  ['Early Send', '早期反射发送'], ['Dry/Wet', '干湿比'],
  ['Dry Level', '干声电平'], ['Wet Level', '湿声电平'],
  ['Input Level', '输入电平'], ['Output Level', '输出电平'],
  ['Buffer Size', '缓冲大小'], ['Sample Rate', '采样率'],
  ['Mono Behaviour', '单声道处理'], ['Soft Knee', '软拐点'],
  ['Peak Crest', '峰值因数'], ['Out Gain', '输出增益'],
  ['Dry Mix', '干声混合'], ['Predelay', '预延迟'],
  ['Low Cut', '低切'], ['High Cut', '高切'],
  ['Low Cross', '低频分频点'], ['High Cross', '高频分频点'],
  ['Low Mult', '低频倍数'], ['High Mult', '高频倍数'],
  ['Early Damp', '早期反射衰减'], ['Late Damp', '晚期混响衰减'],
  ['Boost Freq', '提升频率'], ['Low Boost', '低频提升'],
  ['Threshold', '阈值'], ['Frequency', '频率'], ['Release', '释放'],
  ['Attack', '启动'], ['Makeup', '补偿增益'], ['Decay', '衰减'],
  ['Diffuse', '扩散度'], ['Modulation', '调制'], ['Algorithm', '算法'],
  ['Program', '预设'], ['Quality', '音质档'], ['Bypass', '旁通'],
  ['Width', '宽度'], ['Size', '空间大小'], ['Spin', '旋转速度'],
  ['Wander', '漂移'], ['Dampen', '高频衰减'], ['Gain', '增益'],
  ['Band', '频段'], ['Selected', '选中'], ['Active', '启用'],
  ['Mode', '模式'], ['Ratio', '压缩比'], ['Split', '分割'],
  ['Delta', '差异'], ['Dry', '干声'], ['Level', '电平'],
];
function vstParamCn(name, p) {
  const key = String(p.key || '');
  if (VST_PARAM_CN[key]) return VST_PARAM_CN[key];
  const m = key.match(/^band_(\d+)_(.+)$/);
  if (m) return `第${m[1]}段·${VST_PARAM_CN[m[2]] || m[2]}`;
  const pre = key.match(/^(hp|lp|wide_band)_(.+)$/);
  if (pre) {
    const tag = pre[1] === 'hp' ? '高通' : (pre[1] === 'lp' ? '低通' : '全频段');
    return `${tag}·${VST_PARAM_CN[pre[2]] || pre[2]}`;
  }
  // ★ 没收录的插件：按单词表翻译 label，能翻多少翻多少，剩下的保持英文。
  let s = String(p.label || key);
  for (const [en, cn] of VST_PARAM_WORDS) {
    s = s.replace(new RegExp('\\b' + en.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi'), cn);
  }
  return s;
}

// 参数「这一根滑杆是干嘛的」。★ 插件只报英文 key，这里用人话解释；
// 没收录的按词根兜底，翻不出来就不写（不硬编）。
const VST_PARAM_HELP = {
  decay_s: '混响尾音长度。人声建议 1.5–2.5 秒，过长会浑浊。',
  predelay_ms: '干声之后延迟多久出现混响。20–40 ms 可使人声更靠前、更清晰。',
  dry_level: '原声保留比例。人声一般 85–90，过低会显得过分湿润。',
  late_level: '混响尾音音量。建议从 15–25 起，过高会掩盖字音。',
  early_level: '首次反射量。10–20 即可，过高会导致浑浊。',
  size_m: '空间大小（米）。越大越像大厅，越小越像房间。',
  width: '立体声宽度。100% 最自然，过宽会使人声发散。',
  threshold_db: '超过这个电平才开始压。数字越小压得越多；人声从 −20 起试。',
  ratio: '压缩比。2–4:1 最自然；4:1 表示超出阈值的部分压成四分之一。',
  attack_ms: '启动时间。越短越能保留瞬态；人声 10–20 ms 较为自然。',
  release_ms: '释放时间。过短会产生呼吸感；人声建议 100–200 ms。',
  release_peak_ms: '峰值释放。处理尖峰后多快松手；80–150ms 起步。',
  release_rms_ms: '整体释放。控制持续响度的回落；约 200 ms 更平滑。',
  makeup_db: '补偿增益。用于补回压缩造成的整体电平下降。',
  dry_wet: '干湿比。0 是原声，100 是全效果；压缩器一般 100。',
  dry_mix_db: '干声混合量。0dB 表示原声全保留。',
};

const VST_HELP_RULES = [
  [/threshold|thresh/, '超过这个电平才开始压；数字越小压得越多。'],
  [/ratio/, '压缩比；4:1 = 超出阈值的部分压成四分之一。'],
  [/attack/, '启动时间；越短越能保留瞬态，人声 10–20 ms。'],
  [/release/, '释放时间；过短会产生呼吸感，人声 100–200 ms。'],
  [/makeup/, '补偿增益；压缩后变小了用它补回来。'],
  [/knee/, '拐点；越大进入压缩越柔和。'],
  [/dry.*wet|wet_level|dry_level|dry_mix|^mix$/, '干湿比；0 是原声，越大效果越多。'],
  [/decay/, '衰减时间；混响尾巴有多长。'],
  [/predelay/, '预延迟；干声之后延迟多久出现混响，20–40 ms 可使人声更清晰。'],
  [/size/, '空间大小；越大越像大厅。'],
  [/width/, '立体声宽度；100% 左右最自然。'],
  [/frequency|freq/, '这一段作用的频率。'],
  [/gain_db|out_gain|input_level|output_level/, '增益；正数变大，负数变小（dB）。'],
  [/(^|_)q$/, 'Q 值；越大越窄，越小越宽。'],
  [/type/, '这一段的方式：搁架 / 峰值 / 高通 / 低通。'],
  [/dyn/, '动态；打开后只在超过阈值时压这一段。'],
  [/hp_|highpass|low_cut/, '高通（低切）；切掉更低的轰隆声。'],
  [/lp_|lowpass|high_cut/, '低通（高切）；切掉更高的嘶声。'],
  [/brightness/, '亮度；越大越亮、越靠前。'],
  [/detune/, '失谐；少量增加可提升宽度与厚度。'],
  [/bigness/, '宏大；加空间感。'],
];
function vstParamHelp(p) {
  const key = String(p.key || '').toLowerCase();
  if (VST_PARAM_HELP[key]) return VST_PARAM_HELP[key];
  for (const [re, txt] of VST_HELP_RULES) if (re.test(key)) return txt;
  return '';
}

// ★ 缓冲 / 采样率 / 电平表 / 频谱显示这些不是「调音」用的，折叠起来不挡路。
function vstParamTech(p) {
  const k = String(p.key || '').toLowerCase();
  if (/^(buffer_size|sample_rate|meter_|analyzer_|display_|gr_disp|channels$|bypass$|master_bypass$|solo$)/.test(k)) return true;
  return ['buffer_size_frames', 'sample_rate_frames', 'channels', 'bypass',
    'master_bypass', 'solo'].includes(k);
}

// 数值显示：加上单位；bool/string 不走这里
function vstUnitOf(p) {
  if (p.units) return String(p.units);
  const k = String(p.key || '').toLowerCase();
  if (/_hz$|frequency_hz$|freq_hz$/.test(k)) return 'Hz';
  if (/_db$|_gain$/.test(k)) return 'dB';
  if (/_ms$/.test(k)) return 'ms';
  if (/_s$/.test(k)) return 's';
  if (/_m$/.test(k)) return 'm';
  if (/_pct$|_percent$/.test(k)) return '%';
  if (/_x$|_mult$/.test(k)) return '×';
  return '';
}
function vstFmt2(p, v) {
  const step = Number.isFinite(p.step) && p.step > 0 ? p.step : 0.01;
  const dec = step >= 1 ? 0 : (step >= 0.1 ? 1 : (step >= 0.01 ? 2 : 3));
  const u = vstUnitOf(p);
  return Number(v).toFixed(dec) + (u ? ' ' + u : '');
}
function vstRangeText(p) {
  const parts = [];
  if (Number.isFinite(p.min) && Number.isFinite(p.max)) {
    parts.push(`范围 ${vstFmt2(p, p.min)} – ${vstFmt2(p, p.max)}`);
  }
  if (p.default !== undefined && p.default !== null && p.type === 'number') {
    parts.push(`默认 ${vstFmt2(p, p.default)}`);
  }
  return parts.join(' · ');
}

let VST_LIST = [];
let VST_DIRS = [];
let VST_MAIN = '';
let VST_MAX = 8;
let VST_PROBE = null;              // {ok, error, pedalboard}
let vstSel = null;
const vstParams = new Map();       // path -> {params:[]} | {error}
const vstInspecting = new Set();
const vstFrozen = new Map();       // trackId -> {sig,state,error,sec,cost,cached,gen,buffer,gainAtBake}
const vstTimers = new Map();
const vstAud = { active: false, bypass: false, timer: 0, last: 0, gen: 0 };

function vstSlotsOf(tr) {
  return (tr && tr.fx && Array.isArray(tr.fx.vst)) ? tr.fx.vst : [];
}
function vstSlotsEdit(tr) {
  if (!tr) return null;
  tr.fx = tr.fx || {};
  if (!Array.isArray(tr.fx.vst)) tr.fx.vst = [];
  return tr.fx.vst;
}
function vstEnabled(tr) {
  return !!(tr && tr.fx && tr.fx.freeze && vstSlotsOf(tr).length);
}

// 黄 = 旧快照/正在算；红 = 失败；绿 = 已同步
function vstStateOf(tr) {
  const n = vstSlotsOf(tr).length;
  if (!n) return null;
  if (!tr.fx.freeze) return { state: 'off', cls: '', label: 'VST 未冻结' };
  const st = vstFrozen.get(tr.id);
  if (!st) return { state: 'stale', cls: 'stale', label: '待重算' };
  if (st.state === 'error') return { state: 'error', cls: 'error', label: '重算失败' };
  if (st.state === 'ok') return { state: 'ok', cls: '', label: '已同步' };
  return { state: 'stale', cls: 'stale', label: '正在重算…' };
}

function hint(msg) { const el = $('hint'); if (el) el.textContent = msg; }

let vstScanned = false;
async function ensureVstLoaded() {
  if (vstScanned) return;
  vstScanned = true;
  await scanVst();
}

async function scanVst() {
  try {
    const r = await api('/vst/plugins');
    VST_LIST = r.plugins || [];
    VST_DIRS = r.dirs || [];
    VST_MAIN = r.main || '';
    VST_MAX = r.max || 8;
    VST_PROBE = r.probe || null;
  } catch (e) {
    VST_LIST = [];
    VST_PROBE = { ok: false, error: e.message };
  }
  fillVstAddSel();
  renderVstPanel();
}

function fillVstAddSel() {
  const sel = $('vstAddSel');
  if (!sel) return;
  sel.textContent = '';
  if (!VST_LIST.length) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = '（没扫到插件）';
    sel.appendChild(o);
    return;
  }
  for (const p of VST_LIST) {
    const o = document.createElement('option');
    o.value = p.path || p.bundle || '';
    o.textContent = vstCnName(p.name) + (p.ok ? '' : '（不可用）');
    o.title = p.name;
    sel.appendChild(o);
  }
}

function vstDirsHtml() {
  const dirs = VST_DIRS.filter(d => d.exists).map(d => d.path);
  if (!dirs.length && VST_MAIN) dirs.push(VST_MAIN);
  const sr = engine.ctx?.sampleRate || 0;
  const srNote = (sr && sr !== 48000)
    ? `<div class="vstnote"><b>⚠ 播放 ${sr} Hz / 导出 48k</b> —— 冻结按播放采样率算，和导出会有细微差。</div>`
    : '';
  // ★ 一行 + 省略号：面板默认很矮，多行会把下面的插件槽挤出去
  return `<div class="vstdirline" title="${escHtml(t('插件文件夹：'))}\n${escHtml(dirs.join('\n'))}">`
    + `<b>${escHtml(t('插件文件夹：'))}</b>${dirs.map(d => `<span>${escHtml(d)}</span>`).join('<i>·</i>')}</div>`
    + srNote;
}

function openVst(trackId) {
  if (trackId) S.selTrack = trackId;
  const b = document.querySelector('.tabs button[data-tab="vst"]');
  if (b) b.click();
  ensureVstLoaded();
  renderVstPanel();
}

function renderVstPanel() {
  if (!$('vstSlots')) return;
  const tr = fxTrack();
  $('vstTrackName').textContent = tr ? tr.name : '—';

  const w = $('vstWarn');
  if (w) {
    if (VST_PROBE && VST_PROBE.ok === false) {
      w.hidden = false;
      w.textContent = '⚠ VST 宿主不可用（多半是没装 pedalboard）:' + VST_PROBE.error;
    } else {
      w.hidden = true;
    }
  }
  $('vstDirs').innerHTML = vstDirsHtml();

  const box = $('vstSlots');
  box.textContent = '';
  const slots = vstSlotsOf(tr);
  if (!slots.length) {
    const e = document.createElement('div');
    e.className = 'fxslot-empty';
    e.textContent = tr ? '这一轨还没有 VST。' : '先建一条轨。';
    box.appendChild(e);
  }
  slots.forEach((slot, i) => {
    const d = document.createElement('div');
    d.className = 'fxslot' + (slot.id === vstSel ? ' sel' : '')
      + (slot.bypass ? ' bypass' : '');
    d.title = slots.length > 1 ? '点击后在右侧调整该插件参数' : '';
    d.innerHTML = `<span class="idx">${i + 1}</span>
      <span class="dot" style="background:#6ea8fe"></span>
      <span class="nm" title="${(slot.name || 'VST').replace(/"/g, '&quot;')}">${vstCnName(slot.name)}</span>
      <button data-a="on" title="旁通">${slot.bypass ? '○' : '●'}</button>
      <button data-a="up" title="上移">↑</button>
      <button data-a="down" title="下移">↓</button>
      <button data-a="del" title="删除">×</button>`;
    d.onclick = (e) => {
      if (e.target.closest('button')) return;
      vstSel = slot.id;
      ensureVstInspect(slot);
      renderVstPanel();
    };
    d.querySelectorAll('button').forEach(b => {
      b.onclick = (e) => {
        e.stopPropagation();
        const a = b.dataset.a;
        if (a === 'on') {
          takeSnap();
          slot.bypass = !slot.bypass;
        } else if (a === 'up' && i > 0) {
          takeSnap();
          slots.splice(i - 1, 0, slots.splice(i, 1)[0]);
        } else if (a === 'down' && i < slots.length - 1) {
          takeSnap();
          slots.splice(i + 1, 0, slots.splice(i, 1)[0]);
        } else if (a === 'del') {
          takeSnap();
          slots.splice(i, 1);
          if (vstSel === slot.id) vstSel = null;
          if (!slots.length) {
            tr.fx.freeze = false;
            clearTimeout(vstTimers.get(tr.id));
            vstTimers.delete(tr.id);
            vstFrozen.delete(tr.id);
            // 播放中取消冻结会重新调度这一轨；它出问题也不许挡住下面的重画
            try { engine.clearFrozen(tr.id); } catch (err) { console.warn(err); }
          }
        } else return;
        markDirty();
        // ★ 重画放 finally：上面任何一步抛错，面板也必须立刻反映「已经删了」,
        //   绝不留下「得刷新页面才看得到删除」的假象。
        try {
          vstTouched(tr);
        } finally {
          // ★ drawStatic 不能漏：轨头那个绿/黄/红的「VST」小标是画在 canvas 上的，
          //   只重画下面板的话，插件的槽位没了、画布上的标还在，得刷新才消失。
          renderVstPanel(); renderMixer(); drawStatic();
        }
      };
    });
    box.appendChild(d);
  });
  renderVstParams(tr);
  updateVstState(false);
}

function ensureVstInspect(slot) {
  if (!slot) return;
  const key = slot.path || slot.bundle || '';
  if (!key || vstParams.has(key) || vstInspecting.has(key)) return;
  vstInspecting.add(key);
  api('/vst/inspect', { method: 'POST', json: { path: key } })
    .then((r) => vstParams.set(key, r))
    .catch((e) => vstParams.set(key, { error: e.message, params: [] }))
    .finally(() => {
      vstInspecting.delete(key);
      if ($('panelVst') && !$('panelVst').hidden) renderVstParams(fxTrack());
    });
}

function vstParamRange(p) {
  let lo = Number.isFinite(p.min) ? p.min : 0;
  let hi = Number.isFinite(p.max) ? p.max : 1;
  if (!(hi > lo)) hi = lo + 1;
  return [lo, hi];
}
function vstFmt(p, v) {
  const step = Number.isFinite(p.step) && p.step > 0 ? p.step : 0.01;
  const dec = step >= 1 ? 0 : (step >= 0.1 ? 1 : (step >= 0.01 ? 2 : 3));
  return Number(v).toFixed(dec) + (p.units ? ' ' + p.units : '');
}

function renderVstParams(tr) {
  const box = $('vstParams');
  if (!box) return;
  box.textContent = '';
  const slots = vstSlotsOf(tr);
  const slot = slots.find(s => s.id === vstSel);
  if (!slot) {
    const d = document.createElement('div');
    d.className = 'inote';
    d.textContent = slots.length ? '点一个插件看参数' : '这一轨还没有 VST —— 左边选一个加进来';
    box.appendChild(d);
    return;
  }
  const key = slot.path || slot.bundle || '';
  const data = vstParams.get(key);
  const guide = vstGuideOf(slot.name);
  const h = document.createElement('h4');
  h.textContent = vstCnName(slot.name);
  h.title = slot.name || 'VST';
  box.appendChild(h);
  const sub = document.createElement('div');
  sub.className = 'sub';
  // ★ 一轨挂多个 VST 时，参数面板一次只显示「选中的那个」——
  //   把「第几个 / 共几个」和「怎么切」写出来，否则会以为只能调一个。
  const idx = slots.indexOf(slot) + 1;
  sub.textContent = (slots.length > 1
    ? `第 ${idx} / ${slots.length} 个 · 点左边的插件切换`
    : `第 ${idx} 个`) + ' · 离线渲染'
    + (slot.bypass ? ' · 已旁通' : '');
  box.appendChild(sub);

  // ★ 插件自己只会报英文参数名，这里把「这是什么、怎么调」用人话写出来
  if (guide) {
    const g = document.createElement('div');
    g.className = 'vstguide';
    // ★ 面板只有一百多像素高：说明默认只露一行，「怎么调」收在 <details> 里，
    //   否则它会把下面真正要动的参数挤出可视区。
    g.innerHTML = `<div><span class="kind">${guide.kind}</span> · ${guide.what}</div>`
      + (guide.tips && guide.tips.length
        ? `<details><summary>怎么调（${guide.tips.length} 条）</summary>`
          + `<ul>${guide.tips.map(t => `<li>${t}</li>`).join('')}</ul></details>`
        : '');
    box.appendChild(g);
  }

  if (!data) {
    ensureVstInspect(slot);
    const d = document.createElement('div');
    d.className = 'inote';
    d.textContent = '正在读取参数…';
    box.appendChild(d);
    return;
  }
  if (data.error) {
    const d = document.createElement('div');
    d.className = 'inote';
    d.style.color = '#ff8fa3';
    d.textContent = '这个插件读不出来：' + data.error;
    box.appendChild(d);
    return;
  }
  const params = data.params || [];
  if (!params.length) {
    const d = document.createElement('div');
    d.className = 'inote';
    d.textContent = '这个插件没有可调参数。';
    box.appendChild(d);
    return;
  }
  slot.params = slot.params || {};
  const setParam = (key, v) => {
    slot.params[key] = v;
    markDirty(); vstTouched(tr); vstAudKick();
  };
  const makeRow = (p, tech) => {
    const row = document.createElement('div');
    row.className = 'fxparam' + (tech ? ' tech' : '');
    const lab = document.createElement('label');
    lab.textContent = vstParamCn(slot.name, p);
    // 中文名后面把英文原名与完整范围留在提示里 —— 想上网查的时候还找得到
    lab.title = `${p.label || p.key} (${p.key})`
      + (vstRangeText(p) ? ` · ${vstRangeText(p)}` : '');
    row.appendChild(lab);
    const cur = (slot.params[p.key] !== undefined) ? slot.params[p.key] : p.default;

    if (p.type === 'bool') {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = (cur === true || cur === 1 || cur === 'True' || cur === 'true');
      cb.addEventListener('change', () => {
        takeSnap();
        setParam(p.key, !!cb.checked);
        renderVstParams(tr);
      });
      row.appendChild(cb);
      const out = document.createElement('output');
      out.textContent = cb.checked ? '开' : '关';
      row.appendChild(out);
    } else if (p.type === 'string' && Array.isArray(p.options) && p.options.length > 1) {
      const sel = document.createElement('select');
      const vals = p.options.map(String);
      const cv = String(cur ?? '');
      if (!vals.includes(cv)) vals.unshift(cv);
      for (const o of vals) {
        const op = document.createElement('option');
        op.value = o; op.textContent = o;
        sel.appendChild(op);
      }
      sel.value = cv;
      sel.addEventListener('change', () => {
        takeSnap();
        setParam(p.key, sel.value);
      });
      row.appendChild(sel);
      row.appendChild(document.createElement('output'));
    } else if (p.type === 'string' || p.type === 'other') {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = String(cur ?? '');
      inp.addEventListener('change', () => {
        takeSnap();
        setParam(p.key, inp.value);
      });
      row.appendChild(inp);
      row.appendChild(document.createElement('output'));
    } else if (Number.isFinite(p.min) && Number.isFinite(p.max)) {
      const [lo, hi] = vstParamRange(p);
      const sl = document.createElement('input');
      sl.type = 'range';
      sl.min = 0; sl.max = 1000; sl.step = 1;
      const toPos = (v) => Math.round(1000 * (Math.max(lo, Math.min(hi, Number(v))) - lo) / (hi - lo));
      const fromPos = (pos) => {
        let v = lo + (hi - lo) * pos / 1000;
        const step = Number.isFinite(p.step) && p.step > 0 ? p.step : 0;
        if (step) v = Math.round(v / step) * step;
        return Math.max(lo, Math.min(hi, v));
      };
      sl.value = String(toPos(cur));
      const out = document.createElement('output');
      out.textContent = vstFmt2(p, cur);
      let snapped = false;
      sl.addEventListener('pointerdown', () => { snapped = false; });
      sl.addEventListener('input', () => {
        if (!snapped) { takeSnap(); snapped = true; }   // 一次拖动只留一档
        const v = fromPos(+sl.value);
        setParam(p.key, v);
        out.textContent = vstFmt2(p, v);
      });
      sl.addEventListener('change', () => { snapped = false; vstAudKick(); });
      row.appendChild(sl);
      row.appendChild(out);
    } else {
      // 插件没报范围（Airwindows 的 input/output level 就是这样）
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = '0.01';
      inp.value = String(Number.isFinite(Number(cur)) ? Number(cur) : 0);
      inp.addEventListener('change', () => {
        takeSnap();
        setParam(p.key, Number(inp.value) || 0);
      });
      row.appendChild(inp);
      row.appendChild(document.createElement('output'));
    }

    const hint = [vstParamHelp(p), vstRangeText(p)].filter(Boolean).join(' · ');
    if (hint && !tech) {
      const d = document.createElement('div');
      d.className = 'phint';
      d.textContent = hint;
      row.appendChild(d);
    }
    return row;
  };

  // ★ 常用的放上面；缓冲/采样率/电平表/频谱这些收进折叠区，不挡路。
  for (const p of params.filter(x => !vstParamTech(x))) box.appendChild(makeRow(p, false));
  const techParams = params.filter(vstParamTech);
  if (techParams.length) {
    const det = document.createElement('details');
    det.className = 'vsttech';
    const sum = document.createElement('summary');
    sum.textContent = `技术参数（通常无需调整 · ${techParams.length} 项）`;
    det.appendChild(sum);
    for (const p of techParams) det.appendChild(makeRow(p, true));
    box.appendChild(det);
  }
  const acts = document.createElement('div');
  acts.className = 'vstparam-actions';
  const rst = document.createElement('button');
  rst.textContent = '恢复默认';
  rst.onclick = () => {
    takeSnap();
    slot.params = {};
    markDirty(); vstTouched(tr); vstAudKick(); renderVstParams(tr);
  };
  acts.appendChild(rst);
  box.appendChild(acts);
}

function addVst(path, trIn) {
  const tr = trIn || fxTrack();
  if (!tr) return false;
  const p = VST_LIST.find(x => (x.path || x.bundle) === path) || (path ? { name: 'VST', path, bundle: '' } : null);
  const slots = vstSlotsEdit(tr);
  if (!p) return false;
  if (slots.length >= VST_MAX) { hint(`每轨最多 ${VST_MAX} 个 VST`); return false; }
  takeSnap();
  const slot = {
    id: uid('vst'), name: p.name || 'VST',
    bundle: p.bundle || '', path: p.path || path,
    bypass: false, params: {},
  };
  slots.push(slot);
  tr.fx.freeze = true;               // ★ 默认开冻结 —— 否则正常播放里听不到它
  vstSel = slot.id;
  S.selTrack = tr.id;
  ensureVstInspect(slot);
  markDirty();
  vstTouched(tr);
  renderVstPanel(); renderMixer(); drawStatic();
  return true;
}

function scheduleFreeze(trackId, delay = 400) {
  clearTimeout(vstTimers.get(trackId));
  vstTimers.set(trackId, setTimeout(() => {
    vstTimers.delete(trackId);
    doFreeze(trackId).catch(() => {});
  }, Math.max(0, delay)));
}

async function doFreeze(trackId) {
  const tr = S.proj && S.proj.tracks.find(t => t.id === trackId);
  if (!tr || !vstEnabled(tr)) return;
  let st = vstFrozen.get(trackId);
  if (!st) { st = { sig: '', state: 'working', gen: 0 }; vstFrozen.set(trackId, st); }
  const my = ++st.gen;
  st.state = 'working';
  updateVstState(false);
  try {
    const r = await api('/vst/freeze', {
      method: 'POST',
      json: {
        project: S.proj, trackId,
        sr: engine.ctx?.sampleRate || 48000,
      },
    });
    if (my !== st.gen) return;                    // 有更新的请求了
    // ★ 这一轨没有任何音频 ⇒ 服务端不产出文件。0 采样的 WAV 会让
    //   decodeAudioData 报「Unable to decode audio data」，看起来像插件坏了。
    if (!r.url || !(r.sec > 0)) {
      engine.clearFrozen(trackId);
      if (st.sig && st.sig !== r.sig) engine.dropRendered(`freeze:${trackId}:${st.sig}`);
      st.sig = r.sig; st.buffer = null; st.empty = true;
      st.state = 'ok'; st.error = ''; st.sec = 0; st.cost = r.sec_cost; st.cached = false;
      updateVstState(true);
      return;
    }
    st.empty = false;
    if (!st.buffer || st.sig !== r.sig) {
      const key = `freeze:${trackId}:${r.sig}`;
      const buf = await engine.renderedBuffer(key, r.url);
      if (my !== st.gen) return;
      engine.setFrozen(trackId, { sig: r.sig, buffer: buf, gainAtBake: r.gain });
      // 旧 buffer 从解码缓存里丢掉（不然一条轨改几次就攒一堆）
      if (st.sig) engine.dropRendered(`freeze:${trackId}:${st.sig}`);
      st.buffer = buf;
    }
    st.sig = r.sig; st.state = 'ok'; st.error = '';
    st.sec = r.sec; st.cost = r.sec_cost; st.cached = r.cached;
    // ★ nondet 只在真跑的时候有值；命中缓存时保留上一次的结论
    if (r.nondet !== null && r.nondet !== undefined) st.nondet = !!r.nondet;
  } catch (e) {
    if (my !== st.gen) return;
    st.state = 'error'; st.error = e.message;
  }
  updateVstState(true);
}

// 这一轨有任何会改变声音的改动 ⇒ 标黄 + 400ms 防抖重算
function vstTouched(tr) {
  if (!tr || !vstEnabled(tr)) return;
  const st = vstFrozen.get(tr.id);
  if (st && st.state === 'ok') st.state = 'stale';
  scheduleFreeze(tr.id);
  updateVstState(false);
}
function vstTouchedAll() {
  for (const tr of (S.proj?.tracks || [])) vstTouched(tr);
}

function updateVstState(full = true) {
  const tr = fxTrack();
  const el = $('vstFreezeState');
  if (el) {
    if (!tr || !vstSlotsOf(tr).length) { el.textContent = ''; el.className = 'vststate'; }
    else if (!tr.fx.freeze) {
      el.textContent = '冻结关着 —— 不冻结时正常播放里没有 VST';
      el.className = 'vststate stale';
    } else {
      const f = vstFrozen.get(tr.id);
      if (!f) { el.textContent = '未同步（正在算…）'; el.className = 'vststate stale'; }
      else if (f.state === 'error') {
        el.textContent = '重算失败：' + f.error;
        el.className = 'vststate error';
      } else if (f.state === 'ok') {
        el.textContent = f.empty
          ? '当前轨道没有音频，无需冻结'
          : `已同步 · ${f.sec}s`
            + (f.cached ? ' · 命中缓存' : (f.cost ? ` · 重算 ${f.cost}s` : ''))
            + (f.nondet ? ' · ⚠ 该插件每次渲染可能略有差异，冻结仅用于预览' : '');
        el.className = 'vststate ok';
      } else {
        el.textContent = '效果已改，正在重算…';
        el.className = 'vststate stale';
      }
    }
  }
  const fb = $('vstFreeze');
  if (fb) fb.checked = !!(tr && tr.fx && tr.fx.freeze);
  if (full) { renderMixer(); drawStatic(); }
}

function resetFrozenState() {
  for (const t of vstTimers.values()) clearTimeout(t);
  vstTimers.clear();
  vstFrozen.clear();
  if (engine.frozen) engine.frozen.clear();
  engine.stopAudition();
  vstAud.active = false; vstAud.gen++;
  vstAud.bypass = false;
  vstSel = null;
}

// ---- 短段试听 ---------------------------------------------------------
function vstRange() {
  const lp = S.proj?.ui?.loop;
  if (Array.isArray(lp) && lp[1] > lp[0] + 0.2) {
    return [Math.max(0, lp[0]), Math.min(lp[1], Math.max(0, lp[0]) + 8)];
  }
  const t0 = Math.max(0, S.playAt || 0);
  return [t0, t0 + 4];
}

async function vstAudDo() {
  const tr = fxTrack();
  if (!tr || !vstSlotsOf(tr).length) return;
  const [t0, t1] = vstRange();
  const my = ++vstAud.gen;
  const t = performance.now();
  const lat = $('vstLat');
  if (lat) lat.textContent = '在算…';
  try {
    const r = await fetch('/api/studio/vst/audition', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project: S.proj, trackId: tr.id, t0, t1, solo: true,
        bypass: !!vstAud.bypass,
        sr: engine.ctx?.sampleRate || 48000,
      }),
    });
    if (!r.ok) {
      let msg = `试听失败 ${r.status}`;
      try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    if (my !== vstAud.gen) return;                // 已经有更新的请求了
    const sec = await engine.audition(blob, true);
    if (my !== vstAud.gen) return;
    if (lat) {
      lat.textContent = `往返 ${Math.round(performance.now() - t)}ms`
        + ` · ${sec.toFixed(2)}s 循环` + (vstAud.bypass ? ' · 干声' : '');
    }
  } catch (e) {
    if (my !== vstAud.gen) return;
    vstAud.active = false;
    if (lat) lat.textContent = '';
    syncAudBtn();
    toast('试听失败：' + e.message, 8000);
  }
}

function vstAudKick() {
  if (!vstAud.active) return;
  const now = performance.now();
  const dt = now - vstAud.last;
  if (dt >= 150) { vstAud.last = now; vstAudDo(); return; }
  if (!vstAud.timer) {
    vstAud.timer = setTimeout(() => {
      vstAud.timer = 0;
      vstAud.last = performance.now();
      vstAudDo();
    }, Math.max(20, 150 - dt));
  }
}

function syncAudBtn() {
  const b = $('vstAudBtn');
  if (b) {
    b.classList.toggle('on', vstAud.active);
    b.textContent = vstAud.active ? '■ 停止试听' : '▶ 试听这一段（循环）';
  }
  const d = $('vstDryBtn');
  if (d) d.textContent = vstAud.bypass ? '听湿声 A/B' : '听干声 A/B';
}

async function vstAudToggle() {
  const tr = fxTrack();
  if (!tr || !vstSlotsOf(tr).length) { hint('先加一个 VST'); return; }
  if (vstAud.active) {
    vstAud.active = false;
    vstAud.gen++;
    engine.stopAudition();
    syncAudBtn();
    return;
  }
  if (engine.playing) {
    engine.pause();
    $('btnPlay').textContent = '▶';
    $('btnPlay').classList.remove('on');
  }
  vstAud.active = true;
  vstAud.bypass = false;
  syncAudBtn();
  await vstAudDo();
  syncAudBtn();
}

async function vstDryToggle() {
  if (!vstAud.active) { await vstAudToggle(); }
  vstAud.bypass = !vstAud.bypass;
  syncAudBtn();
  await vstAudDo();
  syncAudBtn();
}

// ---- 整曲「渲染试听」：走的就是导出那条路径 -----------------------------
let vstPreviewing = false;
async function vstPreviewAll() {
  if (!S.proj || vstPreviewing) return;
  vstPreviewing = true;
  const btn = $('vstPrevBtn');
  const el = $('vstPrev');
  const st = $('vstPrevState');
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '渲染中…';
  if (st) st.textContent = '正在通过导出流程渲染整曲…';
  try {
    if (engine.playing) {
      engine.pause();
      $('btnPlay').textContent = '▶';
      $('btnPlay').classList.remove('on');
    }
    vstAud.active = false; engine.stopAudition(); syncAudBtn();
    const lv = $('selLoud') ? $('selLoud').value : '-14';
    const r = await api('/export', {
      method: 'POST',
      json: {
        project: S.proj, format: 'wav', normalize: true,
        loudness: lv === 'off' ? null : Number(lv), preview: true,
      },
    });
    el.src = r.url + '?t=' + Date.now();
    el.hidden = false;
    el.play().catch(() => {});
    if (st) st.textContent = `完成 · ${r.sec}s · ${(r.size / 1048576).toFixed(1)}MB · ${r.sec_cost}s`;
  } catch (e) {
    if (st) st.textContent = '渲染失败：' + e.message;
    toast('渲染试听失败：' + e.message, 8000);
  } finally {
    vstPreviewing = false;
    btn.disabled = false; btn.textContent = old;
  }
}

// ---- 事件 --------------------------------------------------------------
if ($('vstScanBtn')) $('vstScanBtn').onclick = () => scanVst();
if ($('vstAddBtn')) $('vstAddBtn').onclick = () => {
  const p = $('vstAddSel').value;
  if (!p) { hint('未检测到插件，请将插件放入插件目录后点击「刷新」'); return; }
  if (!addVst(p)) hint('加不上：先选一条轨');
};
if ($('vstFreeze')) $('vstFreeze').onchange = () => {
  const tr = fxTrack();
  if (!tr) return;
  takeSnap();
  tr.fx = tr.fx || {};
  tr.fx.freeze = $('vstFreeze').checked;
  if (tr.fx.freeze) {
    vstFrozen.delete(tr.id);
    scheduleFreeze(tr.id, 0);
  } else {
    clearTimeout(vstTimers.get(tr.id));
    vstTimers.delete(tr.id);
    vstFrozen.delete(tr.id);
    engine.clearFrozen(tr.id);
  }
  markDirty();
  updateVstState(true);
};
if ($('vstAudBtn')) $('vstAudBtn').onclick = () => vstAudToggle();
if ($('vstDryBtn')) $('vstDryBtn').onclick = () => vstDryToggle();
if ($('vstPrevBtn')) $('vstPrevBtn').onclick = () => vstPreviewAll();

// 底部 tab
document.querySelectorAll('.tabs button[data-tab]').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.tabs button[data-tab]').forEach(x => x.classList.toggle('on', x === b));
    $('panelRoll').hidden = b.dataset.tab !== 'roll';
    $('panelMix').hidden = b.dataset.tab !== 'mix';
    $('panelFx').hidden = b.dataset.tab !== 'fx';
    $('panelVst').hidden = b.dataset.tab !== 'vst';
    if (b.dataset.tab === 'mix') renderMixer();
    if (b.dataset.tab === 'fx') renderFxPanel();
    if (b.dataset.tab === 'vst') { ensureVstLoaded(); renderVstPanel(); }
  };
});

$('fxAddBtn').onclick = () => {
  const t = $('fxAddSel').value;
  if (!addFx(t)) $('hint').textContent = '加不上：先选一条轨';
};

// ---------------------------------------------------------------- 启动
// ---------------------------------------------------------------- 一键调音
// ★ 面板只显示方案；应用时才改工程，而且**只取一次撤销快照**。
let AUTO_PLAN = null;

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function autoCatName(c) {
  return ({ vol: '音量', fx: '效果', vst: 'VST', tune: '修音' })[c] || c;
}

function autoActionTitle(a) {
  if (a.kind === 'gain') {
    const p = v => Math.round(Number(v) * 100) + '%';
    return `音量 ${p(a.from)} → ${p(a.to)}`;
  }
  if (a.kind === 'chain') {
    const names = (a.slots || []).map(s => t((FX_TYPES[s.type] || {}).name || s.type));
    return `加 ${names.length} 个自动效果：${names.join(' · ')}`;
  }
  if (a.kind === 'reverb') {
    return `混响 ${Math.round((a.from || 0) * 100)}% → ${Math.round(a.to.mix * 100)}%`;
  }
  if (a.kind === 'vst') {
    const ks = Object.keys(a.params || {});
    return `${a.plugin}:${ks.join('、')}`;
  }
  if (a.kind === 'pitch') {
    const f = a.from || {}, t = a.to || {};
    return `修音 ${f.semis || 0}半音${f.cents || 0}音分 → ${t.semis}半音${t.cents}音分`;
  }
  return a.kind;
}

// VST 参数那一行更长一点：把 from → to 也写出来
function autoActionDetail(a) {
  if (a.kind !== 'vst') return '';
  return Object.entries(a.params || {}).map(([k, v]) =>
    `${k} ${v.from == null ? '默认' : v.from} → ${v.to}`).join(' · ');
}

function renderAutoPanel() {
  const r = AUTO_PLAN;
  if (!r) return;
  const ms = r.measures || [];
  $('autoMeasure').innerHTML = ms.length ? ms.map(m => `
    <div class="mrow">
      <span class="mname">${escHtml(m.name)}</span>
      <span class="mtag ${m.role === 'vocal' ? 'vocal' : ''}">${m.role === 'vocal' ? '人声' : '伴奏'}</span>
      <span>${fmt(m.lufs, 1)} LUFS</span>
      <span>峰值 ${fmt(m.peak_db, 1)} dB</span>
      <span>动态 ${fmt(m.crest_db, 1)} dB</span>
    </div>`).join('') : '<div class="inote">没有可测的轨</div>';

  const acts = r.actions || [];
  if (!acts.length) {
    $('autoBody').innerHTML = '<div class="inote">没找到需要改的地方（或者工程里没有声音）。</div>';
  } else {
    // 按轨分组，让人能对上
    const groups = new Map();
    acts.forEach((a, i) => {
      const key = a.trackId || '其它';
      if (!groups.has(key)) groups.set(key, { name: a.trackName || '其它', items: [] });
      groups.get(key).items.push({ a, i });
    });
    let html = '';
    for (const g of groups.values()) {
      html += `<div class="agroup">${escHtml(g.name)}</div>`;
      for (const { a, i } of g.items) {
        const det = autoActionDetail(a);
        html += `
          <label class="arow">
            <input type="checkbox" data-i="${i}" checked>
            <div>
              <div class="atitle"><span class="abadge">${autoCatName(a.category)}</span>${escHtml(autoActionTitle(a))}</div>
              ${det ? `<div class="awhy">${escHtml(det)}</div>` : ''}
              <div class="awhy">${escHtml(a.why || '')}</div>
            </div>
          </label>`;
      }
    }
    $('autoBody').innerHTML = html;
  }
  $('autoNotes').innerHTML = [
    ...(r.notes || []).map(n => `<div class="n">· ${escHtml(n)}</div>`),
    ...(r.limits || []).map(n => `<div>· ${escHtml(n)}</div>`),
  ].join('');
  autoApplyLabel();
}

function autoApplyLabel() {
  const n = document.querySelectorAll('#autoBody input[type=checkbox]:checked').length;
  const btn = $('autoApply');
  btn.textContent = n ? `应用 ${n} 项` : '应用';
  btn.disabled = !AUTO_PLAN || !n;
}

function autoSelect(mode) {
  if (!AUTO_PLAN) return 0;
  let n = 0;
  document.querySelectorAll('#autoBody input[type=checkbox]').forEach(cb => {
    const a = AUTO_PLAN.actions[+cb.dataset.i];
    cb.checked = mode === 'all' || (a && a.category === mode);
    if (cb.checked) n++;
  });
  autoApplyLabel();
  return n;
}

function autoClose() {
  $('autoPanel').hidden = true;
  AUTO_PLAN = null;
}

async function autoMix() {
  if (!S.proj) return null;
  $('autoPanel').hidden = false;
  $('autoMeasure').innerHTML = '';
  $('autoNotes').innerHTML = '';
  $('autoBody').innerHTML = '<div class="inote">正在测量（每轨渲染一次）…大型工程可能需要数秒</div>';
  $('autoApply').disabled = true;
  $('btnAuto').disabled = true;
  try {
    const r = await api('/auto_mix', {
      method: 'POST', json: { project: S.proj, scopes: 'all', tune: true },
    });
    AUTO_PLAN = r;
    renderAutoPanel();
    return r;
  } catch (e) {
    AUTO_PLAN = null;
    $('autoBody').innerHTML = `<div class="inote">测不了：${escHtml(e.message)}</div>`;
    $('autoApply').disabled = true;
    return null;
  } finally {
    $('btnAuto').disabled = false;
  }
}

// ★ 「一键调音」和「💬 调音助手」共用这一份应用逻辑：
//   收到的是同一种动作契约，只取一次撤销快照 ⇒ 一次 Ctrl+Z 全退。
function applyActions(picks) {
  if (!S.proj || !Array.isArray(picks) || !picks.length) return { done: 0, pitchIds: [] };
  takeSnap();
  let done = 0;
  const pitchIds = [];
  for (const a of picks) {
    const tr = (S.proj.tracks || []).find(t => String(t.id) === String(a.trackId));
    if ((a.kind === 'clipgain' || a.kind === 'pitch') && !tr) {
      const c = clipById(a.clipId)?.clip;   // 已有的 helper 返回 {tr, clip}
      if (!c) continue;
      if (a.kind === 'clipgain') {
        c.gain = clamp(Number(a.to), 0, 2);
      } else {
        c.semis = clamp(Math.round(Number(a.to?.semis) || 0), -24, 24);
        c.cents = clamp(Math.round(Number(a.to?.cents) || 0), -100, 100);
        c.renderKey = null;
        c.needsRender = true;
        pitchIds.push(c.id);
      }
      done++;
      continue;
    }
    if (!tr) continue;
    if (a.kind === 'gain') {
      tr.gain = clamp(Number(a.to), 0, 1.5);
      done++;
    } else if (a.kind === 'pan') {
      tr.pan = clamp(Number(a.to), -1, 1);
      done++;
    } else if (a.kind === 'chain') {
      tr.fx = tr.fx || {};
      // 只替换自动槽，他手动加的插件一个都不动
      const kept = (tr.fx.chain || []).filter(s => !(s && s.auto));
      tr.fx.chain = [...(a.slots || []).map(s => JSON.parse(JSON.stringify(s))), ...kept];
      done++;
    } else if (a.kind === 'reverb') {
      tr.fx = tr.fx || {};
      tr.fx.reverb = { ...(tr.fx.reverb || {}), mix: a.to.mix, size: a.to.size };
      done++;
    } else if (a.kind === 'vst') {
      const slots = (tr.fx && tr.fx.vst) || [];
      let s = slots.find(x => String(x.id) === String(a.slotId));
      if (!s) s = slots.find(x => String(x.name) === String(a.plugin));
      if (!s) continue;
      s.params = s.params || {};
      for (const [k, v] of Object.entries(a.params || {})) s.params[k] = v.to;
      done++;
    } else if (a.kind === 'pitch') {
      const c = (tr.clips || []).find(x => String(x.id) === String(a.clipId));
      if (!c) continue;
      c.semis = a.to.semis;
      c.cents = a.to.cents;
      c.renderKey = null;
      c.needsRender = true;
      pitchIds.push(c.id);
      done++;
    } else if (a.kind === 'clipgain') {
      const c = (tr.clips || []).find(x => String(x.id) === String(a.clipId));
      if (!c) continue;
      c.gain = clamp(Number(a.to), 0, 2);
      done++;
    }
  }
  if (!done) return { done: 0, pitchIds };

  afterStructure();
  if (pitchIds.length) {
    // renderPending() 只看选中的块：临时选中这些，发出去，再把选择还回去
    const keep = S.sel;
    S.sel = new Set(pitchIds);
    renderPending();
    S.sel = keep;
  }
  return { done, pitchIds };
}

function autoApply() {
  if (!AUTO_PLAN || !S.proj) return 0;
  const picks = [];
  document.querySelectorAll('#autoBody input[type=checkbox]').forEach(cb => {
    if (cb.checked) picks.push(AUTO_PLAN.actions[+cb.dataset.i]);
  });
  if (!picks.length) { setSaveState('一项都没勾'); return 0; }

  const { done } = applyActions(picks);
  if (!done) { setSaveState('当前方案已失效，请重新执行一键调音'); return 0; }

  autoClose();
  setSaveState(`一键调音：应用了 ${done} 项 · Ctrl+Z 全部撤销`);
  return done;
}

$('btnAuto').onclick = () => autoMix();
$('autoClose').onclick = autoClose;
$('autoCancel').onclick = autoClose;
$('autoApply').onclick = autoApply;
$('autoSelAll').onclick = () => autoSelect('all');
$('autoSelVol').onclick = () => autoSelect('vol');
$('autoSelFx').onclick = () => autoSelect('fx');
$('autoSelVst').onclick = () => autoSelect('vst');
$('autoSelTune').onclick = () => autoSelect('tune');
document.getElementById('autoBody').addEventListener('change', (e) => {
  if (e.target && e.target.type === 'checkbox') autoApplyLabel();
});

// ---------------------------------------------------------------- 跟原唱矫正
// ★ 面板只出方案；应用时才改工程，而且**只取一次撤销快照**。
//   歌库（studio_refdb）在服务端：同一首歌第二次分析直接读上次存下的音高地图。
let REF_PLAN = null;
let REF_LIB = null;

function refTracks() { return (S.proj && S.proj.tracks) || []; }
function refTrackName(t) { return t.name || ('轨 ' + String(t.id)); }

function refFillSelects() {
  const list = refTracks();
  const html = list.map(t =>
    `<option value="${escHtml(t.id)}">${escHtml(refTrackName(t))}</option>`).join('');
  const a = $('refRef').value, b = $('refTake').value;
  const b_ok = list.some(t => String(t.id) === b);
  $('refRef').innerHTML = html;
  $('refTake').innerHTML = html;
  if (list.some(t => String(t.id) === a)) $('refRef').value = a;
  if (b_ok) $('refTake').value = b;
  if (!list.some(t => String(t.id) === $('refTake').value)) {
    const other = list.find(t => String(t.id) !== $('refRef').value);
    if (other) $('refTake').value = String(other.id);
  }
  if (!list.some(t => String(t.id) === $('refRef').value) && list[0]) {
    $('refRef').value = String(list[0].id);
  }
  // ★ 第一次打开时两个下拉都默认停在第一条轨 —— 一点「分析」就是「不能是同一条」。
  //   只在用户**没手选过**「我的轨」(b 无效)时替他换一条；他自己选的组合不擅自改。
  if (!b_ok && $('refRef').value === $('refTake').value) {
    const other = list.find(t => String(t.id) !== $('refRef').value);
    if (other) $('refTake').value = String(other.id);
  }
}

function refRenderLib() {
  const el = $('refLib');
  if (!REF_LIB) { el.innerHTML = `<span class="inote">${t('歌库读不出来')}</span>`; return; }
  const es = REF_LIB.entries || [];
  const first = es[0];
  /* ★ 动态部分用 t() 现拼：整段带数字/歌名的句子进不了字典（键是中文原文） */
  el.innerHTML = '<span class="inote">' + (first
    ? t('歌库 {0} 首 · 最近：').replace('{0}', String(REF_LIB.count || es.length)) + escHtml(first.name || '')
    : t('歌库 · 还没存过，分析一首就自动收进来')) + '</span>';
}

async function refLibRefresh() {
  try { REF_LIB = await api('/reference/library'); } catch (e) { REF_LIB = null; }
  refRenderLib();
}

function refOpen() {
  if (!S.proj) return false;
  $('refPanel').hidden = false;
  $('autoPanel').hidden = true;
  refFillSelects();
  refRenderLib();
  refLibRefresh();
  if (!REF_PLAN) {
    $('refMeasure').innerHTML =
      '<div class="inote">选好「原唱轨」和「我的轨」，点「分析」;' +
      '无法确定原唱轨时，点击「认歌」由曲库自动识别。</div>';
    $('refBody').innerHTML = '';
    $('refNotes').innerHTML = '';
    refDrawGraph();
  }
  return true;
}

function refClose() {
  $('refPanel').hidden = true;
  REF_PLAN = null;
  refDrawGraph();
}

// ---- 导入原唱：选文件 → 传进媒体库 → 建一条轨 → 直接选成「原唱轨」 ----
// ★ 以前面板里只有下拉框：原唱必须先自己「媒体→导入→拖进轨道」才会出现，
//   他问「我怎么上传原唱」就是卡在这一步。现在两个地方都有「导入原唱…」。
async function refImportFile(f) {
  if (!f || !S.proj) return null;
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  if (!['wav', 'mp3', 'flac', 'ogg'].includes(ext)) {
    uiAlert('只收 wav / mp3 / flac / ogg');
    return null;
  }
  const btn = $('refImport'), big = $('bigRefImport');
  const o1 = btn ? btn.textContent : '', o2 = big ? big.textContent : '';
  if (btn) { btn.textContent = '导入中…'; btn.disabled = true; }
  if (big) { big.textContent = '导入中…'; big.disabled = true; }
  try {
    const r = await fetch('/api/studio/upload?name=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f,
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    const src = j.source;
    await loadMedia();
    takeSnap();
    const tr = newTrack('voice', `原唱 · ${niceName(src.name)}`.slice(0, 24));
    const sid = ensureSource(src.path, src.name, src.dur, 'voice');
    tr.clips.push({
      id: uid('cl'), sourceId: sid, start: 0, srcIn: 0, srcOut: src.dur,
      length: src.dur, semis: 0, cents: 0, gain: 1,
      fadeIn: 0.008, fadeOut: 0.02, kind: 'sound',
    });
    S.selTrack = tr.id;
    afterStructure();
    ensurePeaks(sid);
    if (typeof refFillSelects === 'function') {
      refFillSelects();
      $('refRef').value = String(tr.id);
      const other = refTracks().find(
        t => String(t.id) !== String(tr.id) && (t.clips || []).length);
      if (other) $('refTake').value = String(other.id);
    }
    if (BIG.refBarOpen) {
      bigRefFillTracks();
      if ($('bigRefTrack')) $('bigRefTrack').value = String(tr.id);
    }
    setSaveState(`原唱「${src.name}」已导入 · 点「分析」开始对照`);
    return tr;
  } catch (e) {
    uiAlert('导入原唱失败：' + e.message);
    return null;
  } finally {
    if (btn) { btn.textContent = o1; btn.disabled = false; }
    if (big) { big.textContent = o2; big.disabled = false; }
  }
}

(function wireRefImport() {
  const btn = $('refImport'), big = $('bigRefImport'), inp = $('refFile');
  if (!inp) return;
  const pick = () => inp.click();
  if (btn) btn.onclick = pick;
  if (big) big.onclick = pick;
  inp.addEventListener('change', async (e) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    await refImportFile(f);
  });
})();

// ---- 认歌：拿「我的轨」去歌库里找最像的那首 ----
async function refIdentify() {
  if (!S.proj) return null;
  const takeId = $('refTake').value;
  if (!takeId) return null;
  $('refId').disabled = true;
  $('refBody').innerHTML = '<div class="inote">正在识别歌曲…（需先测量当前轨道的音高）</div>';
  try {
    const r = await api('/reference/identify', {
      method: 'POST', json: { project: S.proj, takeTrackId: takeId },
    });
    const cand = r.candidates || [];
    if (!cand.length) {
      $('refMeasure').innerHTML =
        '<div class="inrow">歌库中没有原唱 —— 请先选择一条原唱轨并点击「分析」，系统将学习该歌曲。</div>';
      return r;
    }
    const rows = cand.map((c, i) => `
      <div class="mrow">
        <span class="mname">${i === 0 && c.okAll ? '最像' : '候选'}</span>
        <span class="mtag ${c.okAll ? 'vocal' : ''}">${escHtml(c.name || '')}</span>
        <span>对上 ${Math.round((c.agree || 0) * 100)}%</span>
        <span>共 ${fmt(c.agreeSec, 1)} 秒</span>
        <span>错位 ${fmt((c.offset || 0) * 1000, 0)} ms</span>
        <span>${c.okAll ? '像是同一首' : '不太像'}</span>
      </div>`).join('');
    $('refMeasure').innerHTML = rows +
      `<div class="mrow"><span class="inote">${
        r.best ? '已按它选好原唱轨（若这首歌在工程里）' : '未找到足够相似的歌曲 —— 可能为新歌'}</span></div>`;

    // 认出来了就把原唱轨选到对应那一条（按 source 路径对上）
    if (r.best) {
      const hit = (REF_LIB?.entries || []).find(e => e.id === r.best.id);
      if (hit) {
        const tr = refTracks().find(t => (t.clips || []).some(c => {
          const src = S.proj.sources?.[c.sourceId];
          return src && String(src.path).replace(/\\/g, '/') === String(hit.path);
        }));
        if (tr && String(tr.id) !== String(takeId)) $('refRef').value = String(tr.id);
      }
    }
    $('refBody').innerHTML = '';
    return r;
  } catch (e) {
    $('refMeasure').innerHTML = `<div class="inrow">认不了：${escHtml(e.message)}</div>`;
    return null;
  } finally {
    $('refId').disabled = false;
  }
}

async function refAnalyze() {
  if (!S.proj) return null;
  const refId = $('refRef').value, takeId = $('refTake').value;
  if (!refId || !takeId) {
    $('refBody').innerHTML = '<div class="inote">先选两条轨</div>';
    return null;
  }
  if (String(refId) === String(takeId)) {
    $('refBody').innerHTML = '<div class="inote">「原唱轨」与「我的轨」不能为同一轨道</div>';
    return null;
  }
  REF_PLAN = null;
  $('refGo').disabled = true; $('refId').disabled = true;
  $('refMeasure').innerHTML = '';
  $('refNotes').innerHTML = '';
  $('refBody').innerHTML = '<div class="inote">正在逐帧测音高并对轴…' +
    '首次分析一首 3 分钟的歌曲约需 2～5 秒，同一歌曲后续分析即时完成</div>';
  refDrawGraph();
  try {
    const r = await api('/reference/analyze', {
      method: 'POST',
      json: {
        project: S.proj, refTrackId: refId, takeTrackId: takeId,
        offset: 'auto', strength: Number($('refStrength').value),
        mode: $('refMode').value,
      },
    });
    REF_PLAN = r;
    refRenderPanel();
    refLibRefresh();
    return r;
  } catch (e) {
    REF_PLAN = null;
    $('refBody').innerHTML = `<div class="inote">分析不了：${escHtml(e.message)}</div>`;
    return null;
  } finally {
    $('refGo').disabled = false; $('refId').disabled = false;
  }
}

function refActionTitle(a) {
  if (a.kind === 'shift') {
    const ms = Number(a.deltaSec) * 1000;
    return `整体${ms >= 0 ? '后' : '前'}移 ${Math.abs(ms).toFixed(0)} ms`;
  }
  if (a.mode === 'curve') {
    const n = (a.to?.curve || []).length;
    return `逐点修音曲线（${n} 个点，最多 ±200 音分）`;
  }
  const total = Number(a.to?.semis || 0) * 100 + Number(a.to?.cents || 0);
  return `整段平移 ${total >= 0 ? '+' : ''}${total.toFixed(0)} 音分`;
}

function refRenderPanel() {
  const r = REF_PLAN;
  if (!r) return;
  const rows = [];
  rows.push(`<div class="mrow">
      <span class="mname">原唱</span>
      <span class="mtag vocal">${escHtml(r.ref?.name || '')}</span>
      <span>${t('{0} 块').replace('{0}', r.ref?.clips || 0)}</span>
      <span>${r.ref?.key ? escHtml(r.ref.key) : '调性没测出'}</span>
    </div>`);
  rows.push(`<div class="mrow">
      <span class="mname">我的</span>
      <span class="mtag">${escHtml(r.take?.name || '')}</span>
      <span>${t('{0} 块').replace('{0}', r.take?.clips || 0)} · ${t('{0} 秒').replace('{0}', fmt(r.takeSec, 1))}</span>
    </div>`);
  rows.push(`<div class="mrow">
      <span class="mname">对上</span>
      <span>${Math.round((r.coverage || 0) * 100)}% 的时间</span>
      <span>共 ${fmt(r.matchedSec, 1)} 秒</span>
      <span>${r.offsetAuto
        ? `错位 ${fmt(r.offset * 1000, 0)} ms（把握 ${fmt(r.offsetConf, 2)}）`
        : '错位由用户指定'}</span>
    </div>`);
  if (r.refdb) {
    rows.push(`<div class="mrow">
        <span class="mname">歌库</span>
        <span class="mtag">${escHtml(r.refdb.name || '')}</span>
        <span>${r.refdb.hit === 'new' ? '第一次听，已存下' : '上次存过'}</span>
        <span>已使用 ${r.refdb.used || 0} 次 · 已录入 ${r.refdb.takes || 0} 次</span>
      </div>`);
  }
  $('refMeasure').innerHTML = rows.join('');

  const acts = r.actions || [];
  if (!acts.length) {
    $('refBody').innerHTML = '<div class="inote">没有要改的地方（对轴不可信的原因见下）。</div>';
  } else {
    $('refBody').innerHTML = acts.map((a, i) => {
      const st = a.stats || {};
      const det = a.kind === 'pitch'
        ? `对上了 ${Math.round((st.coverage || 0) * 100)}% · ` +
          `中位差 ${st.medianCents == null ? '—' : st.medianCents} 音分 · ` +
          `最大 ${st.peakCents == null ? '—' : st.peakCents} 音分`
        : '';
      return `
        <label class="arow">
          <input type="checkbox" data-i="${i}" checked>
          <div>
            <div class="atitle"><span class="abadge">${
              a.kind === 'shift' ? '对轴' : '音高'}</span>${escHtml(refActionTitle(a))}</div>
            ${det ? `<div class="awhy">${escHtml(det)}</div>` : ''}
            <div class="awhy">${escHtml(a.why || '')}</div>
          </div>
        </label>`;
    }).join('');
  }

  $('refNotes').innerHTML = [
    ...(r.notes || []).map(n => `<div class="n">· ${escHtml(n)}</div>`),
    ...(r.limits || []).map(n => `<div>· ${escHtml(n)}</div>`),
  ].join('');
  refDrawGraph();
  refApplyLabel();
}

function refApplyLabel() {
  const n = document.querySelectorAll('#refBody input[type=checkbox]:checked').length;
  const btn = $('refApply');
  btn.textContent = n ? `应用 ${n} 项` : '应用';
  btn.disabled = !REF_PLAN || !n;
}

function refSelect(mode) {
  if (!REF_PLAN) return 0;
  let n = 0;
  document.querySelectorAll('#refBody input[type=checkbox]').forEach(cb => {
    cb.checked = mode === 'all';
    if (cb.checked) n++;
  });
  refApplyLabel();
  return n;
}

// ---- 对照曲线：橙色 = 你，青色 = 原唱 ----
function refDrawGraph() {
  const cv = $('refGraph');
  if (!cv) return;
  const w = cv.clientWidth || 520;
  const h = 110;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(w * dpr));
  cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const pv = REF_PLAN && REF_PLAN.preview;
  if (!pv || !pv.t || !pv.t.length) {
    g.fillStyle = '#5b687b';
    g.font = '11px sans-serif';
    g.fillText(t('分析完成后将显示两条线：橙色为当前录音，青色为原唱'), 10, h / 2);
    return;
  }
  const t0 = pv.t[0], t1 = pv.t[pv.t.length - 1];
  const vals = pv.his.concat(pv.ref).filter(v => isFinite(v));
  if (!vals.length) return;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max(60, (hi - lo) * 0.08);
  lo -= pad; hi += pad;
  const padL = 8, padR = 8, padT = 14, padB = 12;
  const X = t => padL + (t - t0) / Math.max(1e-6, t1 - t0) * (w - padL - padR);
  const Y = c => padT + (hi - c) / Math.max(1e-6, hi - lo) * (h - padT - padB);

  // 每 100 音分一条横线（半音）
  const step = (hi - lo) > 1400 ? 1200 : 100;
  g.strokeStyle = '#1a2130';
  g.lineWidth = 1;
  g.fillStyle = '#46536a';
  g.font = '9px sans-serif';
  for (let c = Math.ceil(lo / step) * step; c <= hi; c += step) {
    const y = Y(c);
    g.beginPath(); g.moveTo(padL, y); g.lineTo(w - padR, y); g.stroke();
  }

  const draw = (arr, color) => {
    g.strokeStyle = color; g.lineWidth = 1.4; g.beginPath();
    let pen = false;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (!isFinite(v)) { pen = false; continue; }
      const x = X(pv.t[i]), y = Y(v);
      if (!pen) { g.moveTo(x, y); pen = true; } else g.lineTo(x, y);
    }
    g.stroke();
  };
  draw(pv.ref, '#4fd1c5');
  draw(pv.his, '#ffa94d');

  g.font = '10px sans-serif';
  g.fillStyle = '#ffa94d'; g.fillText(t('我的'), 10, 11);
  g.fillStyle = '#4fd1c5'; g.fillText(t('原唱'), 40, 11);
}

async function refApply() {
  if (!REF_PLAN || !S.proj) return 0;
  const picks = [];
  document.querySelectorAll('#refBody input[type=checkbox]').forEach(cb => {
    if (cb.checked) picks.push(REF_PLAN.actions[+cb.dataset.i]);
  });
  if (!picks.length) { setSaveState('一项都没勾'); return 0; }

  takeSnap();                        // ★ 只取一次 ⇒ 一次 Ctrl+Z 全退
  let done = 0;
  const curveIds = [], constIds = [];
  for (const a of picks) {
    const tr = (S.proj.tracks || []).find(t => String(t.id) === String(a.trackId));
    if (!tr) continue;
    if (a.kind === 'shift') {
      const clips = tr.clips || [];
      if (!clips.length) continue;
      const minStart = Math.min(...clips.map(c => Number(c.start) || 0));
      let d = Number(a.deltaSec) || 0;
      if (minStart + d < 0) d = -minStart;         // 不推到 0 左边
      for (const c of clips) c.start = Math.max(0, (Number(c.start) || 0) + d);
      done++;
    } else if (a.kind === 'pitch') {
      const c = (tr.clips || []).find(x => String(x.id) === String(a.clipId));
      if (!c) continue;
      if (a.mode === 'curve' && Array.isArray(a.to?.curve) && a.to.curve.length) {
        c.pitchCurve = a.to.curve.map(p => [Number(p[0]), Number(p[1])]);
        c.semis = 0; c.cents = 0;
        curveIds.push(c.id);
      } else {
        c.semis = Number(a.to?.semis) || 0;
        c.cents = Number(a.to?.cents) || 0;
        delete c.pitchCurve;
        constIds.push(c.id);
      }
      c.renderKey = null;
      c.renderUrl = null;
      c.needsRender = true;
      done++;
    }
  }
  if (!done) {
    setSaveState('当前方案已失效，请重新执行分析');
    return 0;
  }
  afterStructure();
  setSaveState(`跟原唱：应用了 ${done} 项 · Ctrl+Z 全部撤销`);

  // 曲线走 render_curve，常数走 render_clip
  for (const id of curveIds) {
    const got = clipById(id);
    if (got) { try { await renderCurveClip(got.clip); } catch (e) { /* 已标 needsRender */ } }
  }
  if (constIds.length) {
    const keep = S.sel;
    S.sel = new Set(constIds);
    try { await renderPending(); } finally { S.sel = keep; }
  }
  refClose();
  return done;
}

$('btnRef').onclick = () => refOpen();
$('refClose').onclick = refClose;
$('refCancel').onclick = refClose;
$('refGo').onclick = refAnalyze;
$('refId').onclick = refIdentify;
$('refApply').onclick = refApply;
$('refSelAll').onclick = () => refSelect('all');
$('refSelNone').onclick = () => refSelect('none');
$('refStrength').addEventListener('input', () => {
  $('refStrengthV').textContent = Math.round(Number($('refStrength').value) * 100) + '%';
});
document.getElementById('refBody').addEventListener('change', (e) => {
  if (e.target && e.target.type === 'checkbox') refApplyLabel();
});

// ---------------------------------------------------------------- 整页块编辑
// ★ 他原话：「我要切的块不是这样的切法，是一整个页面，所有块连在一起，
//   然后可以拖拉移动删等等这些块，而不是单独的一个个块」
//   ⇒ 这一页把**一条轨的 clips** 铺成一整条时间线（不再是左栏那堆小芯片）。
//   拖 = 改 start（不渲染）· 拖两边 = 改长度 · 双击 = 切开 · Delete = 删、原地留空。
//   ★ 没有第二份数据：改的就是工程里那一轨，关掉这一页改动已经在主时间轴上。
const BIG = {
  open: false, trackId: null,
  t0: 0, pps: 120,
  drag: null,
  cv: null, g: null, w: 800, h: 400, dpr: 1,
  t0x: 12, ruler: 30, laneY: 46, laneH: 140,
  // ★ 逐音精修：块 / 音高 两个视图。音高视图里上下拖 = 改音高。
  view: 'blocks', pen: false, frozen: null, noDbl: 0,
  refPlan: null, refTrackId: '', refBarOpen: false, refShow: true, refBusy: false,
};

const bigCv = $('bigCanvas');
const bigG = bigCv ? bigCv.getContext('2d') : null;
BIG.cv = bigCv;
BIG.g = bigG;

function bigTrack() {
  if (!S.proj || !BIG.trackId) return null;
  return (S.proj.tracks || []).find(t => t.id === BIG.trackId) || null;
}
function bigX(t) { return BIG.t0x + (t - BIG.t0) * BIG.pps; }
function bigT(x) { return BIG.t0 + (x - BIG.t0x) / BIG.pps; }
function trSortBig() {
  const tr = bigTrack();
  if (tr) tr.clips.sort((a, b) => a.start - b.start);
}
function fmtBigT(t, step) {
  if (step < 1) return t.toFixed(2);
  const m = Math.floor(t / 60), s = Math.round(t - m * 60);
  return m ? `${m}:${String(s).padStart(2, '0')}` : `${s}s`;
}

function fillBigTracks() {
  const sel = $('bigTrack');
  if (!sel || !S.proj) return;
  sel.textContent = '';
  for (const t of S.proj.tracks) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${t.name || t.kind} · ${(t.clips || []).length} 块`;
    sel.appendChild(o);
  }
  if (BIG.trackId) sel.value = BIG.trackId;
}

function bigResize() {
  if (!bigCv) return;
  const r = bigCv.getBoundingClientRect();
  BIG.w = Math.max(200, r.width);
  BIG.h = Math.max(140, r.height);
  BIG.dpr = Math.min(2, window.devicePixelRatio || 1);
  bigCv.width = Math.round(BIG.w * BIG.dpr);
  bigCv.height = Math.round(BIG.h * BIG.dpr);
  BIG.laneY = BIG.ruler + 16;
  BIG.laneH = clamp(BIG.h - BIG.laneY - 40, 90, 240);
  drawBig();
}

function bigOpen(trackId) {
  if (!S.proj) return false;
  let tr = trackId ? S.proj.tracks.find(t => t.id === trackId) : null;
  if (!tr) {
    tr = S.proj.tracks.find(t => t.id === S.selTrack && (t.clips || []).length)
      || S.proj.tracks.find(t => (t.clips || []).length)
      || S.proj.tracks[0];
  }
  if (!tr) { toast('当前工程没有轨道'); return false; }
  BIG.open = true;
  BIG.trackId = tr.id;
  BIG.drag = null;
  BIG.pen = false;
  BIG.frozen = null;
  BIG.refPlan = null;          // ★ 换轨/重开 = 旧的原唱方案作废
  BIG.refBarOpen = false;
  BIG.refShow = true;
  BIG.noDbl = 0;
  S.selTrack = tr.id;
  S.sel.clear();
  $('bigEdit').hidden = false;
  if ($('bigRefBar')) $('bigRefBar').hidden = true;
  fillBigTracks();
  bigResize();
  bigViewSet('blocks');
  bigFit();
  renderInspector();
  return true;
}

function bigClose() {
  if (!BIG.open && $('bigEdit').hidden) return;
  BIG.open = false;
  BIG.drag = null;
  BIG.trackId = null;
  BIG.pen = false;
  BIG.frozen = null;
  BIG.refPlan = null;          // ★ 关页清掉目标线，别串到下一次
  BIG.refBarOpen = false;
  if ($('bigRefBar')) $('bigRefBar').hidden = true;
  $('bigEdit').hidden = true;
  drawStatic(); drawOverlay();
}

// 整页的吸附：标记优先 + 网格。★ 用 BIG.pps（主视图那个缩放在整页里是错的）
function bigSnapT(t) {
  const tol = 6 / BIG.pps;
  for (const m of markers()) if (Math.abs(t - m.t) <= tol) return m.t;
  const g = gridSec();
  return g > 0 ? Math.round(t / g) * g : t;
}

function bigFit() {
  const tr = bigTrack();
  if (!tr) return;
  const cs = tr.clips || [];
  if (!cs.length) { BIG.t0 = 0; BIG.pps = 120; drawBig(); return; }
  const a = Math.min(...cs.map(c => c.start));
  const b = Math.max(...cs.map(c => c.start + c.length));
  BIG.pps = clamp((BIG.w - BIG.t0x - 20) / Math.max(0.5, b - a), 4, 1200);
  BIG.t0 = Math.max(0, a - 10 / BIG.pps);
  drawBig();
}

// ---- 块 / 音高 视图切换
function bigViewSet(v) {
  const view = v === 'pitch' ? 'pitch' : 'blocks';
  BIG.view = view;
  BIG.t0x = view === 'pitch' ? PIANO_W : 12;
  BIG.pen = false;
  BIG.frozen = null;
  for (const [id, on] of [['bigViewBlocks', view === 'blocks'], ['bigViewPitch', view === 'pitch']]) {
    const el = $(id); if (el) el.classList.toggle('on', on);
  }
  for (const id of ['bigPen', 'bigFlat', 'bigClrCurve', 'bigRefBtn']) {
    const el = $(id); if (el) el.hidden = view !== 'pitch';
  }
  const hint = $('bigHint');
  if (hint) {
    hint.textContent = view === 'pitch'
      ? '上下拖 = 改音高（半音吸附 / Alt 按音分） · 左右拖 = 移位置 · 拖两边 = 改长度 · 双击 = 切开 · Delete = 删'
      : '拖 = 移动 · 拖两边 = 改长度 · 双击 = 切开 · Delete = 删（原地留空） · Ctrl+Z 撤销';
  }
  syncBigPen();
  if (BIG.open) bigFit();
  return view;
}

function syncBigPen() {
  const b = $('bigPen');
  if (b) {
    b.classList.toggle('on', BIG.pen);
    b.textContent = BIG.pen ? '改曲线 ●' : '改曲线';
  }
  if (BIG.cv && !BIG.drag) BIG.cv.style.cursor = BIG.pen ? 'crosshair' : '';
}

function bigPenSet(on) {
  BIG.pen = !!on;
  syncBigPen();
  if (BIG.open) drawBig();
  return BIG.pen;
}

function bigClipAt(x, y) {
  const tr = bigTrack();
  if (!tr) return null;
  if (y < BIG.laneY - 6 || y > BIG.laneY + BIG.laneH + 6) return null;
  const cs = [...tr.clips].sort((a, b) => a.start - b.start);
  for (let i = cs.length - 1; i >= 0; i--) {
    const c = cs[i];
    const a = bigX(c.start), b = bigX(c.start + c.length);
    if (x >= a - 2 && x <= b + 2) return c;
  }
  return null;
}

// ---- 音高视图的几何
// 可见音符（含曲线/原唱目标）的 2%~98% 分位决定音域，至少 12 个半音。
// ★ 拖动期间用 BIG.frozen：不然指针底下的音会跳。
function bigPitchRange() {
  if (BIG.frozen) return BIG.frozen;
  const tr = bigTrack();
  const vals = [];
  const tA = BIG.t0 - 0.5, tB = BIG.t0 + Math.max(1, BIG.w - BIG.t0x) / BIG.pps + 0.5;
  for (const c of tr?.clips || []) {
    if (c.kind === 'sound') continue;
    if (c.start + c.length < tA || c.start > tB) continue;
    const base = clipBaseMidi(c);
    vals.push(base + pitchOffsetOf(c) / 100);
    if (Array.isArray(c.pitchCurve)) {
      for (const p of c.pitchCurve) vals.push(base + (+p[1] || 0) / 100);
    }
    // 原唱目标线也在画面里 ⇒ 一起参与取景，不然目标跑到画面外
    const a = BIG.refShow ? bigRefActionFor(c.id) : null;
    if (a) {
      if (a.mode === 'curve' && Array.isArray(a.to?.curve)) {
        for (const p of a.to.curve) vals.push(base + (+p[1] || 0) / 100);
      } else {
        vals.push(base + ((Number(a.to?.semis) || 0) * 100 + (Number(a.to?.cents) || 0)) / 100);
      }
    }
  }
  const s = vals.filter(v => isFinite(v)).sort((a, b) => a - b);
  let lo, hi;
  if (!s.length) { lo = 55; hi = 67; }        // 空轨：G3~G4
  else {
    const q = (p) => s[clamp(Math.round((s.length - 1) * p), 0, s.length - 1)];
    lo = Math.floor(q(0.02)) - 2;             // ★ 去掉倍频离群点
    hi = Math.ceil(q(0.98)) + 2;
  }
  if (hi - lo < 12) { const mid = (hi + lo) / 2; lo = Math.floor(mid) - 6; hi = lo + 12; }
  return { lo, hi };
}

function bigPitchGeo() {
  const r = bigPitchRange();
  const y0 = BIG.ruler + 12;
  const y1 = Math.max(y0 + 40, BIG.h - SOUND_BAND_H - 16);
  const h = y1 - y0;
  const span = Math.max(1, r.hi - r.lo);
  const semiPx = h / span;
  return {
    lo: r.lo, hi: r.hi, y0, y1, h, semiPx,
    Y: (m) => y0 + (r.hi - m) * semiPx,
    M: (y) => r.hi - (y - y0) / semiPx,
  };
}

function pitchEdgeOf(x, a, b) {
  if (x - a <= 7 && a > BIG.t0x) return 'L';
  if (b - x <= 7) return 'R';
  return null;
}

// 先判气声带，再判音符块（竖向容差 0.65 个半音）
function bigPitchHit(x, y) {
  const tr = bigTrack();
  if (!tr) return null;
  const geo = bigPitchGeo();
  const cs = [...tr.clips].sort((a, b) => a.start - b.start);
  if (y >= BIG.h - SOUND_BAND_H - 10) {
    for (let i = cs.length - 1; i >= 0; i--) {
      const c = cs[i];
      if (c.kind !== 'sound') continue;
      const a = bigX(c.start), b = bigX(c.start + c.length);
      if (x >= a - 2 && x <= b + 2) return { clip: c, part: 'sound', edge: pitchEdgeOf(x, a, b) };
    }
  }
  for (let i = cs.length - 1; i >= 0; i--) {
    const c = cs[i];
    if (c.kind === 'sound') continue;
    const a = bigX(c.start), b = bigX(c.start + c.length);
    if (x < a - 2 || x > b + 2) continue;
    const yc = geo.Y(clipSoundingMidi(c));
    if (Math.abs(y - yc) <= Math.max(6, geo.semiPx * 0.65)) {
      return { clip: c, part: 'note', edge: pitchEdgeOf(x, a, b) };
    }
  }
  return null;
}

function bigCursor(x, y) {
  if (y < BIG.ruler) return 'text';
  if (BIG.view === 'pitch') {
    if (BIG.pen) return 'crosshair';
    const hit = bigPitchHit(x, y);
    if (!hit) return 'default';
    if (hit.edge) return 'ew-resize';
    return hit.part === 'sound' ? 'grab' : 'ns-resize';
  }
  const hit = bigClipAt(x, y);
  if (!hit) return 'default';
  const a = bigX(hit.start), b = bigX(hit.start + hit.length);
  if (x - a <= 8 || b - x <= 8) return 'ew-resize';
  return 'grab';
}

// ---- 原唱方案（整页这条，和弹窗里的 REF_PLAN 是两份）
function bigRefActionFor(clipId) {
  const acts = BIG.refPlan?.actions || [];
  return acts.find(a => a.kind === 'pitch' && String(a.clipId) === String(clipId)) || null;
}

// 目标音高 = 你的原始音高 + 这个音的修正量（不是原唱的绝对音高）
function bigTargetMidi(clip, a, tt, base) {
  const span = Math.max(0.01, clip.srcOut - clip.srcIn);
  const ratio = span / Math.max(1e-6, clip.length);
  const srcT = clamp((tt - clip.start) * ratio, 0, span);
  if (a.mode === 'curve' && Array.isArray(a.to?.curve) && a.to.curve.length) {
    return base + curveValueAt(a.to.curve, srcT) / 100;
  }
  return base + ((Number(a.to?.semis) || 0) * 100 + (Number(a.to?.cents) || 0)) / 100;
}

// ---------------------------------------------------------------- 整页：画
function bigDrawRuler(g, W, H) {
  g.fillStyle = '#0e131c';
  g.fillRect(0, 0, W, BIG.ruler);
  let step = 1;
  for (const s of [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]) {
    if (s * BIG.pps >= 46) { step = s; break; }
  }
  const tA = BIG.t0, tB = BIG.t0 + (W - BIG.t0x) / BIG.pps;
  g.font = '10px Consolas,monospace';
  for (let i = Math.ceil(tA / step); i * step <= tB; i++) {
    const t = i * step, x = bigX(t);
    g.strokeStyle = '#1a2331';
    g.beginPath(); g.moveTo(x + .5, 0); g.lineTo(x + .5, H); g.stroke();
    g.fillStyle = '#7f8fa3';
    g.fillText(fmtBigT(t, step), x + 3, 13);
  }
  g.strokeStyle = '#243043';
  g.beginPath(); g.moveTo(0, BIG.ruler + .5); g.lineTo(W, BIG.ruler + .5); g.stroke();
}

function bigDrawMarks(g, W, H) {
  const x0 = BIG.t0x;
  for (const m of markers()) {
    const x = bigX(m.t);
    if (x < x0 || x > W) continue;
    g.strokeStyle = 'rgba(79,209,197,.5)';
    g.setLineDash([4, 4]);
    g.beginPath(); g.moveTo(x + .5, BIG.ruler); g.lineTo(x + .5, H); g.stroke();
    g.setLineDash([]);
    g.fillStyle = '#4fd1c5';
    g.font = '10px "Microsoft YaHei",sans-serif';
    g.fillText(m.name, x + 3, BIG.ruler + 11);
  }
  const px = bigX(S.playAt);
  if (px >= x0 - 1 && px <= W) {
    g.fillStyle = '#ff8a3d';
    g.fillRect(px, BIG.ruler, 1.5, H - BIG.ruler);
    g.beginPath();
    g.moveTo(px - 5, BIG.ruler); g.lineTo(px + 6, BIG.ruler);
    g.lineTo(px + .5, BIG.ruler + 8); g.closePath(); g.fill();
  }
}

function bigDrawInfo(tr) {
  const cs = tr?.clips || [];
  const end = cs.length ? Math.max(...cs.map(c => c.start + c.length)) : 0;
  let info = `${cs.length} 块 · 到 ${fmt(end, 2)}s`;
  if (BIG.view === 'pitch') {
    const notes = cs.filter(c => c.kind !== 'sound').length;
    info = `${notes} 个音 + ${cs.length - notes} 段气声 · 到 ${fmt(end, 2)}s`;
  }
  if ($('bigInfo').textContent !== info) $('bigInfo').textContent = info;
}

function bigDrawBlocksBody(g, tr, W, H) {
  const x0 = BIG.t0x;
  const laneY = BIG.laneY, laneH = BIG.laneH;
  const cs = [...tr.clips].sort((a, b) => a.start - b.start);

  // 轨底 + 空隙（删块 = 原地留空，这里看得见）
  g.fillStyle = '#0e1219';
  g.fillRect(x0, laneY, W - x0, laneH);
  let prev = 0;
  for (const c of cs) {
    if (c.start > prev + 1e-4) {
      const ga = Math.max(x0, bigX(prev)), gb = Math.min(W, bigX(c.start));
      if (gb - ga > .5) {
        g.fillStyle = 'rgba(255,255,255,.02)';
        g.fillRect(ga, laneY, gb - ga, laneH);
        if (gb - ga > 34) {
          g.fillStyle = '#4a5566';
          g.font = '11px "Microsoft YaHei",sans-serif';
          g.fillText(t('空'), ga + 6, laneY + laneH / 2 + 4);
        }
      }
    }
    prev = Math.max(prev, c.start + c.length);
  }

  // 块
  const col = tr.color || COLOR.voice;
  const srcName = (sid) => shortName(S.proj.sources?.[sid]?.name || tr.name || '?');
  for (const c of cs) {
    const xa = bigX(c.start), xb = bigX(c.start + c.length);
    if (xb < x0 - 2 || xa > W + 2) continue;
    const ax = Math.max(x0 - 1, xa), bx = Math.min(W + 1, xb);
    if (bx - ax < .5) continue;
    const sel = S.sel.has(c.id);
    const y = laneY + 8, h = laneH - 16;

    g.save();
    roundRect(g, ax, y, bx - ax, h, 5);
    g.clip();
    const grad = g.createLinearGradient(0, y, 0, y + h);
    grad.addColorStop(0, shade(col, 1.22));
    grad.addColorStop(.2, shade(col, .95));
    grad.addColorStop(1, shade(col, .6));
    g.fillStyle = grad;
    g.fillRect(ax, y, bx - ax, h);

    // 标题条：音名 + 源
    g.fillStyle = 'rgba(4,10,18,.55)';
    g.fillRect(ax, y, bx - ax, 17);
    g.fillStyle = '#eaf4ff';
    g.font = '600 11px "Segoe UI","Microsoft YaHei",sans-serif';
    const label = (c.note ? c.note + ' · ' : (c.kind === 'sound' ? t('气声') + ' · ' : '')) + srcName(c.sourceId);
    g.fillText(label, ax + 6, y + 12);

    // 长度 + 位置
    if (bx - ax > 52) {
      g.fillStyle = 'rgba(234,244,255,.75)';
      g.font = '10px Consolas,monospace';
      g.fillText(fmt(c.length, 2) + 's', ax + 6, y + h - 7);
    }
    const gv = Number(c.gain ?? 1);
    if (Math.abs(gv - 1) >= 0.005 && bx - ax > 76) {
      g.fillStyle = '#ffd479';
      g.textAlign = 'right';
      g.fillText(clipGainText(c), bx - 6, y + h - 7);
      g.textAlign = 'left';
    }
    if (c.needsRender) {
      g.fillStyle = 'rgba(255,200,87,.16)';
      g.fillRect(ax, y, bx - ax, h);
      g.fillStyle = '#ffc857';
      g.font = '10px "Microsoft YaHei",sans-serif';
      g.fillText(t('渲染中…'), ax + 6, y + h - 24);
    }
    g.restore();

    roundRect(g, ax + .5, y + .5, Math.max(1, bx - ax - 1), h - 1, 5);
    g.strokeStyle = sel ? '#7fe0ff' : 'rgba(0,0,0,.5)';
    g.lineWidth = sel ? 2 : 1;
    g.stroke();
    g.lineWidth = 1;

    if (sel) {
      g.fillStyle = '#7fe0ff';
      g.fillRect(ax + 1, y + h / 2 - 9, 3, 18);
      g.fillRect(bx - 4, y + h / 2 - 9, 3, 18);
    }
  }
}

// 音高视图：钢琴条 + 音块（按曲线起伏） + 原始音高虚线 + 原唱目标线 + 气声带
function bigDrawPitchBody(g, tr, W, H) {
  const x0 = BIG.t0x;
  const geo = bigPitchGeo();
  const cs = [...tr.clips].sort((a, b) => a.start - b.start);

  // 半音横线（整行背景）
  for (let m = geo.lo; m <= geo.hi; m++) {
    const y = Math.round(geo.Y(m)) + .5;
    const oct = (((m % 12) + 12) % 12) === 0;
    g.strokeStyle = oct ? '#1c2838' : '#121a26';
    g.beginPath(); g.moveTo(x0, y); g.lineTo(W, y); g.stroke();
  }

  // 钢琴条：白键铺满，黑键盖在左边
  g.fillStyle = '#d9e2ee';
  for (let m = geo.lo; m <= geo.hi; m++) {
    const pc = ((m % 12) + 12) % 12;
    if ([1, 3, 6, 8, 10].includes(pc)) continue;
    const yA = geo.Y(m + 0.5), yB = geo.Y(m - 0.5);
    g.fillRect(0, yA, PIANO_W, Math.max(1, yB - yA));
  }
  g.fillStyle = '#141b26';
  for (let m = geo.lo; m <= geo.hi; m++) {
    const pc = ((m % 12) + 12) % 12;
    if (![1, 3, 6, 8, 10].includes(pc)) continue;
    const yA = geo.Y(m + 0.5), yB = geo.Y(m - 0.5);
    g.fillRect(0, yA, PIANO_W * 0.62, Math.max(1, yB - yA));
  }
  g.font = '9px Consolas,monospace';
  g.fillStyle = '#26364c';
  for (let m = geo.lo; m <= geo.hi; m++) {
    if (((m % 12) + 12) % 12 !== 0) continue;
    g.fillText(midiName(m), 2, geo.Y(m) + 3);
  }
  g.strokeStyle = '#243043';
  g.beginPath(); g.moveTo(PIANO_W - .5, BIG.ruler); g.lineTo(PIANO_W - .5, H); g.stroke();

  // 气声带
  const sy = H - SOUND_BAND_H - 6;
  g.fillStyle = 'rgba(255,255,255,.03)';
  g.fillRect(x0, sy, W - x0, SOUND_BAND_H);
  g.fillStyle = '#4a5566';
  g.font = '10px "Microsoft YaHei",sans-serif';
  g.fillText(t('气声'), 4, sy + SOUND_BAND_H / 2 + 3);

  const halfPx = Math.max(4, geo.semiPx * 0.34);
  for (const c of cs) {
    const xa = bigX(c.start), xb = bigX(c.start + c.length);
    if (xb < x0 - 2 || xa > W + 2) continue;
    const ax = Math.max(x0, xa), bx = Math.min(W, xb);
    if (bx - ax < .5) continue;
    const sel = S.sel.has(c.id);

    if (c.kind === 'sound') {
      const hb = SOUND_BAND_H - 8;
      g.fillStyle = sel ? 'rgba(127,224,255,.22)' : 'rgba(140,152,170,.16)';
      roundRect(g, ax, sy + 4, Math.max(2, bx - ax), hb, 4); g.fill();
      roundRect(g, ax + .5, sy + 4.5, Math.max(1, bx - ax - 1), hb - 1, 4);
      g.strokeStyle = sel ? '#7fe0ff' : 'rgba(140,152,170,.5)';
      g.stroke();
      if (bx - ax > 40) {
        g.fillStyle = '#8c98aa';
        g.font = '10px "Microsoft YaHei",sans-serif';
        g.fillText(t('气声'), ax + 5, sy + hb - 5);
      }
      continue;
    }

    const base = clipBaseMidi(c);
    const span = Math.max(0.01, c.srcOut - c.srcIn);
    const ratio = span / Math.max(1e-6, c.length);
    const curve = Array.isArray(c.pitchCurve) && c.pitchCurve.length ? c.pitchCurve : null;
    const midiAt = (tt) => base + (curve
      ? curveValueAt(curve, clamp((tt - c.start) * ratio, 0, span))
      : pitchOffsetOf(c)) / 100;

    // 音块：上下沿跟着曲线走，一眼能看出这个音怎么弯
    const pts = [];
    for (let x = ax; x <= bx + 3; x += 3) {
      const xx = Math.min(x, bx);
      pts.push([xx, geo.Y(midiAt(bigT(xx)))]);
    }
    if (pts.length < 2) continue;
    g.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const [x, y] = pts[i];
      if (i) g.lineTo(x, y - halfPx); else g.moveTo(x, y - halfPx);
    }
    for (let i = pts.length - 1; i >= 0; i--) g.lineTo(pts[i][0], pts[i][1] + halfPx);
    g.closePath();
    g.fillStyle = sel ? 'rgba(127,224,255,.42)' : 'rgba(255,179,92,.34)';
    g.fill();
    g.strokeStyle = sel ? '#7fe0ff' : 'rgba(255,179,92,.85)';
    g.lineWidth = sel ? 1.6 : 1;
    g.stroke();
    g.lineWidth = 1;

    if (sel) {
      g.fillStyle = '#7fe0ff';
      g.fillRect(ax, geo.Y(clipSoundingMidi(c)) - 9, 3, 18);
      g.fillRect(bx - 3, geo.Y(clipSoundingMidi(c)) - 9, 3, 18);
    }

    // 原始音高（橙色虚线）
    const yr = geo.Y(base);
    g.strokeStyle = 'rgba(255,169,77,.45)';
    g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(ax, yr); g.lineTo(bx, yr); g.stroke();
    g.setLineDash([]);

    // 原唱目标线（青色实线）= 你现在的音高 + 这份方案要改的量
    const a = BIG.refShow ? bigRefActionFor(c.id) : null;
    if (a) {
      g.strokeStyle = '#4fd1c5';
      g.lineWidth = 1.6;
      g.beginPath();
      let pen = false;
      for (let x = ax; x <= bx + 3; x += 3) {
        const xx = Math.min(x, bx);
        const y = geo.Y(bigTargetMidi(c, a, bigT(xx), base));
        if (!pen) { g.moveTo(xx, y); pen = true; } else g.lineTo(xx, y);
      }
      g.stroke();
      g.lineWidth = 1;
    }

    // 标签：音名 + 偏移
    if (bx - ax > 34) {
      const off = pitchOffsetOf(c);
      const txt = midiName(Math.round(clipSoundingMidi(c)))
        + (Math.abs(off) >= 0.5 ? ` ${off > 0 ? '+' : ''}${Math.round(off)}¢` : '');
      g.fillStyle = sel ? '#d8f7ff' : '#ffd6a3';
      g.font = '600 10px "Segoe UI","Microsoft YaHei",sans-serif';
      g.fillText(txt, ax + 4, geo.Y(clipSoundingMidi(c)) - halfPx - 4);
    }
  }
}

function drawBig() {
  if (!BIG.open) return;
  const tr = bigTrack();
  if (!tr) { bigClose(); return; }
  const g = bigG, W = BIG.w, H = BIG.h;
  if (!g) return;
  g.setTransform(BIG.dpr, 0, 0, BIG.dpr, 0, 0);
  g.fillStyle = '#0a0d13';
  g.fillRect(0, 0, W, H);

  bigDrawRuler(g, W, H);
  if (BIG.view === 'pitch') bigDrawPitchBody(g, tr, W, H);
  else bigDrawBlocksBody(g, tr, W, H);
  bigDrawMarks(g, W, H);
  bigDrawInfo(tr);
}

// 把一条录音切出来的块**铺成一条轨**（一头一尾排开），然后进整页编辑。
// 已经有这条录音对应的「块轨」就复用，不会每点一下多一条。
// ★ rel = `recordings/xxx.wav` 或 `projects/media/xxx.wav`，两种来源同一个切块口。
function blocksToTrack(rel) {
  if (!S.proj || !rel) return null;
  const r = S.blocksName === rel ? S.blocks : null;
  if (!r || !(r.blocks || []).length) return null;
  // 全静音（没唱）⇒ 不建空轨，也别打开一个空页面
  if (!r.blocks.some(b => b.kind !== 'silence')) return null;
  const base = rel.split('/').pop();
  const mode = S.blocksMode || 'syllable';
  const bkey = rel + '|' + mode;   // ★ 换切法不能复用旧轨，否则会把「按音」的块当「按字」
  let tr = S.proj.tracks.find(t => t.bigFor === bkey);
  if (!tr && mode === 'note') tr = S.proj.tracks.find(t => t.bigFor === rel);
  if (tr) { tr.bigFor = bkey; return tr; }
  const isMix = /_mix\.(wav|mp3|flac|ogg)$/i.test(base);
  takeSnap();
  tr = newTrack(isMix ? 'audio' : 'voice', `块 · ${niceName(base)}`.slice(0, 24));
  tr.bigFor = bkey;
  const sid = ensureSource(rel, base, r.dur, isMix ? 'audio' : 'voice');
  for (const b of r.blocks) {
    if (b.kind === 'silence') continue;
    const len = Math.max(0.03, b.t1 - b.t0);
    const c = {
      id: uid('cl'), sourceId: sid, blockId: b.id,
      start: b.t0, srcIn: b.t0, srcOut: b.t0 + len, length: len,
      semis: 0, cents: 0, gain: 1, fadeIn: 0.008, fadeOut: 0.02,
      kind: b.kind === 'note' ? 'note' : 'sound',
    };
    if (b.note) c.note = b.note;
    if ((b.f0 || 0) > 0) c.f0 = b.f0;   // ★ 逐音精修要知道这个音本来唱在哪
    if (b.syl) c.syl = b.syl;
    if (b.text) c.text = b.text;
    if ((b.f0lo || 0) > 0) c.f0lo = b.f0lo;
    if ((b.f0hi || 0) > 0) c.f0hi = b.f0hi;
    tr.clips.push(c);
  }
  tr.clips.sort((a, b) => a.start - b.start);
  S.selTrack = tr.id;
  afterStructure();
  ensurePeaks(sid);
  return tr;
}

// ★ 切不出块时说清楚为什么 —— 以前是静默不开页面，他只会以为「功能没做」。
function blocksWhy() {
  const bs = (S.blocks && S.blocks.blocks) || [];
  const notes = bs.filter(b => b.kind !== 'silence');
  const rel = S.blocksName || '';
  if (!bs.length) return '切不出音块：这条音频里没找到有声的部分';
  if (!notes.length) {
    return /_voice\./i.test(rel)
      ? '该录音人声通道为「精确零」（静音）：这条文件里没有人声，录音时麦克风可能没有录入声音'
      : '该片段没有演唱内容（整条静音）';
  }
  return '切不出音块';
}

// ---------------------------------------------------------------- 删除录音 / 媒体
// ★ 他拍板：「都真删（先弹确认）」。删之前先把「哪些工程正在用它」说清楚。
async function usageOf(rel) {
  try {
    return await api('/usage', { method: 'POST', json: { path: rel } });
  } catch (e) {
    return { projects: [] };
  }
}

function usageLine(rel, u) {
  const used = (u.projects || []).map(p => p.title || p.file);
  const curTitle = S.proj?.title || S.file;
  const inCur = Object.values(S.proj?.sources || {}).some(
    s => String(s.path || '').replace(/\\/g, '/') === rel);
  if (inCur && !used.includes(curTitle)) used.push(curTitle + '（还没保存）');
  if (!used.length) return '';
  return `\n\n★ 这些工程正在用它：${used.join('、')}\n`
    + '删除后这些工程中的当前轨道将变红并静音 —— 工程文件不会被删除，但音频无法恢复。';
}

async function deleteRecording(name) {
  const rel = 'recordings/' + name;
  const u = await usageOf(rel);
  if (!(await uiConfirm(`删掉录音「${niceName(name)}」?\n\n文件会从盘上真删，删了就找不回来。`
    + usageLine(rel, u), { title: '删除录音', danger: true, okText: '删除' }))) return false;
  const r = await fetch('/api/recordings/' + encodeURIComponent(name), { method: 'DELETE' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
  if (S.pick && S.pick.path === rel) {
    S.pick = null; S.blocks = null; S.blocksName = '';
    $('secBlocks').hidden = true;
  }
  await loadRecordings();
  setSaveState(`已删除录音「${niceName(name)}」`);
  return true;
}

async function deleteMedia(f) {
  const rel = String(f.path || '').replace(/\\/g, '/');
  const u = await usageOf(rel);
  if (!(await uiConfirm(`删掉媒体「${mediaDisplayName(f.name)}」?\n\n文件会从盘上真删，删了就找不回来。`
    + usageLine(rel, u), { title: '删除文件', danger: true, okText: '删除' }))) return false;
  await api('/media/' + encodeURIComponent(f.name), { method: 'DELETE' });
  if (S.pick && String(S.pick.path || '').replace(/\\/g, '/') === rel) S.pick = null;
  await loadMedia();
  setSaveState(`已删除媒体「${mediaDisplayName(f.name)}」`);
  return true;
}

// ---- 整页块编辑：交互
if (bigCv) {
  const bigPos = (e) => {
    const r = bigCv.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  // 拖动开始时的快照：时间、源范围、曲线、常数。之后每次移动都从它重算，不累加。
  function bigOrig(tr) {
    return tr.clips.filter(c => S.sel.has(c.id)).map(c => ({
      clip: c, start: c.start, srcIn: c.srcIn, srcOut: c.srcOut,
      length: c.length, renderKey: c.renderKey || null,
      curve: curveSnapshot(c), semis: c.semis || 0, cents: c.cents || 0,
      pitch: pitchOffsetOf(c),
    }));
  }

  // 笔：在选中的音里某一点写修正量（源时间，±200 音分）,20ms 内合并成一点
  function bigPenPut(x, y) {
    const clip = BIG.drag?.clip;
    if (!clip) return;
    const geo = bigPitchGeo();
    const span = Math.max(0.01, clip.srcOut - clip.srcIn);
    const ratio = span / Math.max(1e-6, clip.length);
    const tt = clamp(bigT(x), clip.start, clip.start + clip.length);
    const t = +((tt - clip.start) * ratio).toFixed(3);
    const c = +clamp((geo.M(y) - clipBaseMidi(clip)) * 100, -PITCH_CAP, PITCH_CAP).toFixed(1);
    if (!Array.isArray(clip.pitchCurve)) clip.pitchCurve = [];
    const pts = clip.pitchCurve;
    const i = pts.findIndex(p => Math.abs(+p[0] - t) < 0.02);
    if (i >= 0) pts[i] = [t, c]; else pts.push([t, c]);
    pts.sort((a, b) => +a[0] - +b[0]);
    clip.semis = 0; clip.cents = 0;
    clipInvalidatePitchRender(clip);
    clip.needsRender = true;
    markDirty();
  }

  function bigPenEnd(d) {
    const clip = d.clip;
    if (!clip || !clipById(clip.id)) return;
    const span = Math.max(0.01, clip.srcOut - clip.srcIn);
    let pts = Array.isArray(clip.pitchCurve) ? clip.pitchCurve.map(p => [+p[0], +p[1]]) : [];
    if (!pts.length) pts = [[0, 0], [+span.toFixed(3), 0]];
    else {
      if (pts[0][0] > 1e-4) pts.unshift([0, pts[0][1]]); else pts[0][0] = 0;
      const last = +span.toFixed(3);
      const tail = pts[pts.length - 1];
      if (tail[0] < span - 1e-4) pts.push([last, tail[1]]); else tail[0] = last;
    }
    clip.pitchCurve = simplifyCurve(pts, 800);
    clip.semis = 0; clip.cents = 0;
    clip.renderKey = null; clip.renderUrl = null; clip.needsRender = true;
    BIG.noDbl = performance.now() + 320;      // ★ 笔一松手别触发双击切分
    afterStructure();
    drawBig();
    renderCurveClip(clip).catch(e => setSaveState('曲线渲染失败：' + e.message));
  }

  function bigPitchDown(e, x, y) {
    const tr = bigTrack();
    const geo = bigPitchGeo();
    const hit = bigPitchHit(x, y);
    if (BIG.pen) {
      if (!hit || hit.part !== 'note') {
        setSaveState('仅可在音符块上绘制（气声块不调整音高）');
        return;
      }
      S.selTrack = tr.id;
      if (!S.sel.has(hit.clip.id)) { S.sel.clear(); S.sel.add(hit.clip.id); }
      takeSnap();
      ensureCurveForPen(hit.clip);
      BIG.frozen = { lo: geo.lo, hi: geo.hi };
      BIG.drag = { mode: 'pen', clip: hit.clip, x0: x, y0: y, geo };
      bigPenPut(x, y);
      drawBig(); renderInspector();
      return;
    }
    if (!hit) {
      S.sel.clear(); renderInspector(); drawBig();
      return;
    }
    S.selTrack = tr.id;
    if (e.shiftKey) {
      if (S.sel.has(hit.clip.id)) S.sel.delete(hit.clip.id); else S.sel.add(hit.clip.id);
    } else if (!S.sel.has(hit.clip.id)) {
      S.sel.clear(); S.sel.add(hit.clip.id);
    }
    const orig = bigOrig(tr);
    const i = orig.findIndex(o => o.clip === hit.clip);
    if (i > 0) orig.unshift(orig.splice(i, 1)[0]);
    takeSnap();                             // 一次手势只取一次快照
    BIG.drag = {
      mode: 'pending', x0: x, y0: y, orig, geo,
      edge: hit.edge, sound: hit.part === 'sound',
    };
    renderInspector(); drawBig();
  }

  bigCv.addEventListener('pointerdown', (e) => {
    if (!BIG.open || !bigTrack()) return;
    const { x, y } = bigPos(e);
    bigCv.setPointerCapture?.(e.pointerId);
    if (y < BIG.ruler) {
      BIG.drag = { mode: 'seek' };
      seekTo(Math.max(0, bigT(x)));
      drawBig();
      return;
    }
    if (BIG.view === 'pitch') { bigPitchDown(e, x, y); return; }

    const hit = bigClipAt(x, y);
    if (!hit) {
      S.sel.clear(); renderInspector(); drawBig();
      return;
    }
    S.selTrack = bigTrack().id;
    if (e.shiftKey) {
      if (S.sel.has(hit.id)) S.sel.delete(hit.id); else S.sel.add(hit.id);
    } else if (!S.sel.has(hit.id)) {
      S.sel.clear(); S.sel.add(hit.id);
    }
    const xa = bigX(hit.start), xb = bigX(hit.start + hit.length);
    let mode = 'move';
    if (x - xa <= 8 && xa > BIG.t0x) mode = 'trimL';
    else if (xb - x <= 8) mode = 'trimR';
    const orig = bigOrig(bigTrack());
    const i = orig.findIndex(o => o.clip === hit);
    if (i > 0) orig.unshift(orig.splice(i, 1)[0]);
    takeSnap();                       // 一次拖动只取一次快照
    BIG.drag = { mode, x0: x, y0: y, orig };
    renderInspector();
    drawBig();
  });

  bigCv.addEventListener('pointermove', (e) => {
    if (!BIG.open) return;
    const { x, y } = bigPos(e);
    const d = BIG.drag;
    if (!d) { bigCv.style.cursor = bigCursor(x, y); return; }
    if (d.mode === 'seek') {
      bigCv.style.cursor = 'text';
      seekTo(Math.max(0, bigT(x)));
      drawBig();
      return;
    }

    // ★ 按下后先分不清他要上下还是左右：超过 4px 再按主轴定
    if (d.mode === 'pending') {
      const dx = x - d.x0, dy = y - d.y0;
      if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
      if (!d.sound && Math.abs(dy) > Math.abs(dx)) {
        d.mode = 'pitch';
        BIG.frozen = { lo: d.geo.lo, hi: d.geo.hi };   // ★ 拖动中冻结音域
        bigCv.style.cursor = 'ns-resize';
      } else {
        d.mode = d.edge === 'L' ? 'trimL' : (d.edge === 'R' ? 'trimR' : 'move');
        bigCv.style.cursor = d.mode === 'move' ? 'grabbing' : 'ew-resize';
      }
    }

    // 竖向拖 = 改音高。★ 从手势起点快照重算（不累加）；多选一起走同样 delta
    if (d.mode === 'pitch') {
      const geo = d.geo;
      const o0 = d.orig[0];
      if (!o0) return;
      const rawCents = (d.y0 - y) / Math.max(1e-6, geo.semiPx) * 100;
      let delta;
      if (e.altKey) {
        delta = Math.round(rawCents * 10) / 10;            // Alt = 按音分
      } else {
        // 默认吸附到半音 —— 对的是**实际音高**，不是位移量
        const base0 = clipBaseMidi(o0.clip);
        const target = Math.round(base0 + (o0.pitch + rawCents) / 100);
        delta = (target - base0) * 100 - o0.pitch;
      }
      d.delta = delta;
      for (const o of d.orig) {
        if (o.curve) {
          // 有曲线 ⇒ 整条平移（保留颤音形状），夹 ±200
          o.clip.pitchCurve = curveShift(o.curve, delta);
          o.clip.semis = 0; o.clip.cents = 0;
        } else {
          const t = clamp(o.pitch + delta, -2400, 2400);
          const s = Math.trunc(t / 100);
          o.clip.semis = s;
          o.clip.cents = +(t - s * 100).toFixed(1);
        }
        clipInvalidatePitchRender(o.clip);
      }
      markDirty(); drawBig(); renderInspector();
      return;
    }

    if (d.mode === 'pen') {
      bigPenPut(x, y);
      drawBig();
      return;
    }

    const o0 = d.orig?.[0];
    if (!o0) return;
    const dt = (x - d.x0) / BIG.pps;
    if (d.mode === 'move') {
      const want = Math.max(0, bigSnapT(o0.start + dt));
      const dd = want - o0.start;
      for (const o of d.orig) o.clip.start = Math.max(0, o.start + dd);
      trSortBig();
    } else if (d.mode === 'trimR') {
      const want = bigSnapT(o0.start + o0.length + dt);
      const len = Math.max(0.03, want - o0.start);
      for (const o of d.orig) {
        o.clip.length = len;
        o.clip.srcOut = o.clip.srcIn + len;
        if (o.curve) {
          const r = curveRange(o.curve, 0, len);
          if (r) o.clip.pitchCurve = r; else delete o.clip.pitchCurve;
          o.clip.semis = 0; o.clip.cents = 0;
        }
        if (o.curve || o.renderKey) clipInvalidatePitchRender(o.clip);
      }
    } else if (d.mode === 'trimL') {
      const want = bigSnapT(o0.start + dt);
      const ds = clamp(want - o0.start, -o0.srcIn, o0.length - 0.03);
      for (const o of d.orig) {
        o.clip.start = Math.max(0, o.start + ds);
        o.clip.srcIn = Math.max(0, o.srcIn + ds);
        o.clip.length = Math.max(0.03, o.length - ds);
        o.clip.srcOut = o.clip.srcIn + o.clip.length;
        if (o.curve) {
          const r = curveRange(o.curve, ds, ds + o.clip.length);
          if (r) o.clip.pitchCurve = r; else delete o.clip.pitchCurve;
          o.clip.semis = 0; o.clip.cents = 0;
        }
        if (o.curve || o.renderKey) clipInvalidatePitchRender(o.clip);
      }
    }
    markDirty();
    drawBig();
    renderInspector();
  });

  const bigEndDrag = () => {
    const d = BIG.drag;
    BIG.drag = null;
    BIG.frozen = null;
    syncBigPen();
    if (!d || d.mode === 'seek' || d.mode === 'pending') return;
    if (d.mode === 'pen') { bigPenEnd(d); return; }
    trSortBig();
    afterStructure();
    // ★ 只在真的动过音高/源范围时渲染；纯移动仍然零渲染（铁律不变）
    if (['trimL', 'trimR', 'stretchR', 'pitch'].includes(d.mode)) renderPending(d.orig);
    drawBig();
  };
  bigCv.addEventListener('pointerup', bigEndDrag);
  bigCv.addEventListener('pointercancel', bigEndDrag);

  bigCv.addEventListener('dblclick', (e) => {
    if (!BIG.open) return;
    if (BIG.pen) return;
    if (BIG.noDbl && performance.now() < BIG.noDbl) return;
    const { x, y } = bigPos(e);
    if (y < BIG.ruler) return;
    let hit = null;
    if (BIG.view === 'pitch') hit = bigPitchHit(x, y);
    else { const c = bigClipAt(x, y); hit = c ? { clip: c } : null; }
    if (!hit) return;
    const clip = hit.clip;
    const t = bigT(x);
    if (t <= clip.start + 0.03 || t >= clip.start + clip.length - 0.03) return;
    S.sel.clear(); S.sel.add(clip.id);
    S.playAt = t;
    const n = splitSelected();
    if (n) setSaveState('切开一块 · Ctrl+Z 撤销');
    drawBig();
  });

  bigCv.addEventListener('wheel', (e) => {
    if (!BIG.open) return;
    e.preventDefault();
    const { x } = bigPos(e);
    if (e.ctrlKey || e.metaKey) {
      const t = bigT(x);
      BIG.pps = clamp(BIG.pps * (e.deltaY < 0 ? 1.2 : 1 / 1.2), 4, 1600);
      BIG.t0 = Math.max(0, t - (x - BIG.t0x) / BIG.pps);
    } else {
      const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      BIG.t0 = Math.max(0, BIG.t0 + d / BIG.pps);
    }
    drawBig();
  }, { passive: false });

  new ResizeObserver(() => { if (BIG.open) bigResize(); }).observe(bigCv);
}

// ---- 拉平 / 清曲线
async function bigFlatSel() {
  const picks = selectedClips().map(x => x.clip).filter(c => c.kind !== 'sound');
  if (!picks.length) { setSaveState('先在音高视图里选几个音'); return 0; }
  takeSnap();
  for (const c of picks) {
    ensureCurveForPen(c);                       // 常数先折成曲线，拉平才是真拉平
    c.pitchCurve = c.pitchCurve.map(([t]) => [+t, 0]);
    c.semis = 0; c.cents = 0;
    c.renderKey = null; c.renderUrl = null; c.needsRender = true;
  }
  afterStructure();
  for (const c of picks) {
    try { await renderCurveClip(c); } catch (_) { /* 已标 needsRender，下次再试 */ }
  }
  setSaveState(`拉平了 ${picks.length} 个音 · Ctrl+Z 撤销`);
  drawBig();
  return picks.length;
}

function bigClearCurveSel() {
  const picks = selectedClips().map(x => x.clip);
  if (!picks.length) { setSaveState('先在音高视图里选几个音'); return 0; }
  takeSnap();
  let n = 0;
  for (const c of picks) {
    if (!clipHasPitch(c)) continue;
    delete c.pitchCurve;
    c.semis = 0; c.cents = 0;
    c.renderKey = null; c.renderUrl = null; c.needsRender = false;
    n++;
  }
  afterStructure(); renderInspector(); drawBig();
  setSaveState(n ? `清空了 ${n} 个音的曲线` : '选中的音本来就没改过');
  return n;
}

// ---- 原唱条（和弹窗里的 REF_PLAN 是两份；这里只改选中的音）
function bigRefState(msg, cls) {
  const el = $('bigRefState');
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'inote' + (cls ? ' ' + cls : '');
}

function bigRefFillTracks() {
  const sel = $('bigRefTrack');
  if (!sel || !S.proj) return;
  sel.textContent = '';
  for (const t of S.proj.tracks) {
    if (String(t.id) === String(BIG.trackId)) continue;
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${t.name || t.kind} · ${(t.clips || []).length} 块`;
    sel.appendChild(o);
  }
  const opts = [...sel.options];
  let pick = opts.some(o => o.value === String(BIG.refTrackId)) ? String(BIG.refTrackId) : '';
  if (!pick) {
    const other = S.proj.tracks.find(t => String(t.id) !== String(BIG.trackId)
        && t.kind === 'voice' && (t.clips || []).length)
      || S.proj.tracks.find(t => String(t.id) !== String(BIG.trackId) && (t.clips || []).length);
    if (other) pick = String(other.id);
  }
  if (pick) sel.value = pick;
  BIG.refTrackId = sel.value || '';
}

function bigRefBarSet(on) {
  BIG.refBarOpen = !!on;
  const bar = $('bigRefBar');
  if (bar) bar.hidden = !BIG.refBarOpen;
  const b = $('bigRefBtn');
  if (b) b.classList.toggle('on', BIG.refBarOpen);
  if (BIG.refBarOpen) {
    bigRefFillTracks();
    if (!BIG.refPlan) bigRefState('选好原唱轨点「分析」· 只改选中的音');
  }
  if (BIG.open) bigResize();          // 栏子占地方，画布高度变了
}

async function bigRefAnalyze() {
  if (!S.proj || !BIG.trackId) return null;
  const refId = BIG.refTrackId || $('bigRefTrack')?.value || '';
  if (!refId) { bigRefState('先选一条原唱轨', 'warn'); return null; }
  if (String(refId) === String(BIG.trackId)) {
    bigRefState('原唱轨与当前轨道不能为同一轨道', 'warn'); return null;
  }
  BIG.refBusy = true;
  BIG.refPlan = null;
  bigRefState('正在逐帧测音高并对轴…');
  try {
    const r = await api('/reference/analyze', {
      method: 'POST',
      json: {
        project: S.proj, refTrackId: refId, takeTrackId: BIG.trackId,
        offset: 'auto', strength: Number($('bigRefStrength')?.value || 0.9),
        mode: $('bigRefMode')?.value || 'intonation',
      },
    });
    BIG.refPlan = r;
    const acts = (r.actions || []).filter(a => a.kind === 'pitch');
    const cov = Math.round((r.coverage || 0) * 100);
    bigRefState(`${acts.length} 个音可修 · 对上 ${cov}%`
      + (cov < 40 ? ' · 无法对齐时不要强制修正' : ''), cov < 40 ? 'warn' : 'ok');
    drawBig();
    return r;
  } catch (e) {
    BIG.refPlan = null;
    bigRefState('分析不了：' + e.message, 'warn');
    drawBig();
    return null;
  } finally {
    BIG.refBusy = false;
  }
}

async function bigRefApplySel() {
  const tr = bigTrack();
  if (!tr) return 0;
  if (!BIG.refPlan) { bigRefState('先点「分析」', 'warn'); return 0; }
  const ids = [...S.sel];
  if (!ids.length) { bigRefState('先在音高视图里选几个音', 'warn'); return 0; }

  const picks = [];
  let stale = 0, missing = 0;
  for (const id of ids) {
    const c = (tr.clips || []).find(x => x.id === id);
    if (!c) continue;
    if (c.kind === 'sound') { missing++; continue; }
    const a = bigRefActionFor(id);
    if (!a) { missing++; continue; }
    // ★ 分析之后挪过/改过长度的音不再硬套
    if (Math.abs(Number(a.start) - c.start) > 0.02
      || Math.abs(Number(a.length) - c.length) > 0.02) { stale++; continue; }
    picks.push({ c, a });
  }
  if (!picks.length) {
    if (stale) bigRefState(`${stale} 个音挪过位置 · 重新点一次「分析」`, 'warn');
    else bigRefState(missing ? `选中的音里没有对得上的（${missing} 个）` : '没有可应用的', 'warn');
    return 0;
  }

  takeSnap();                       // ★ 只取一次 ⇒ 一次 Ctrl+Z 全退
  const curveIds = [], constIds = [];
  for (const { c, a } of picks) {
    if (a.mode === 'curve' && Array.isArray(a.to?.curve) && a.to.curve.length) {
      c.pitchCurve = a.to.curve.map(p => [+p[0], clamp(+p[1], -PITCH_CAP, PITCH_CAP)]);
      c.semis = 0; c.cents = 0;
      curveIds.push(c.id);
    } else {
      c.semis = Number(a.to?.semis) || 0;
      c.cents = Number(a.to?.cents) || 0;
      delete c.pitchCurve;
      constIds.push(c.id);
    }
    c.renderKey = null; c.renderUrl = null; c.needsRender = true;
  }
  afterStructure();
  const bits = [`修了 ${picks.length} 个`];
  if (missing) bits.push(`${missing} 个没对上`);
  if (stale) bits.push(`${stale} 个挪过`);
  bigRefState(bits.join(' · ') + ' · Ctrl+Z 撤销', stale || missing ? 'warn' : 'ok');
  drawBig();

  for (const id of curveIds) {
    const got = clipById(id);
    if (got) { try { await renderCurveClip(got.clip); } catch (_) {} }
  }
  if (constIds.length) {
    const keep = S.sel;
    S.sel = new Set(constIds);
    try { await renderPending(); } finally { S.sel = keep; }
  }
  drawBig();
  return picks.length;
}

$('bigViewBlocks').onclick = () => bigViewSet('blocks');
$('bigViewPitch').onclick = () => bigViewSet('pitch');
$('bigPen').onclick = () => bigPenSet(!BIG.pen);
$('bigFlat').onclick = () => { bigFlatSel(); };
$('bigClrCurve').onclick = () => { bigClearCurveSel(); };
$('bigRefBtn').onclick = () => bigRefBarSet(!BIG.refBarOpen);
$('bigRefGo').onclick = () => bigRefAnalyze();
$('bigRefApply').onclick = () => bigRefApplySel();
$('bigRefTrack').onchange = () => {
  BIG.refTrackId = $('bigRefTrack').value;
  BIG.refPlan = null;
  bigRefState('换了原唱轨 · 重新分析', 'warn');
  drawBig();
};
$('bigRefShow').onchange = () => {
  BIG.refShow = $('bigRefShow').checked;
  drawBig();
};
$('bigRefStrength').addEventListener('input', () => {
  $('bigRefStrengthV').textContent = Math.round(Number($('bigRefStrength').value) * 100) + '%';
  // ★ 强度变了旧方案就作废，免得线还在画上一版的
  BIG.refPlan = null;
  bigRefState('强度改过 · 重新分析', 'warn');
  drawBig();
});
$('bigRefMode').onchange = () => {
  BIG.refPlan = null;
  bigRefState('模式改过 · 重新分析', 'warn');
  drawBig();
};

$('bigClose').onclick = () => bigClose();
$('bigPlay').onclick = () => { if (!engine.playing) seekTo(S.playAt); togglePlay(); };
$('bigFit').onclick = () => bigFit();
$('bigZoomIn').onclick = () => { BIG.pps = clamp(BIG.pps * 1.35, 4, 1600); drawBig(); };
$('bigZoomOut').onclick = () => { BIG.pps = clamp(BIG.pps / 1.35, 4, 1600); drawBig(); };
$('bigTrack').onchange = () => {
  BIG.trackId = $('bigTrack').value;
  BIG.refPlan = null;               // 换了我的轨 ⇒ 旧方案作废
  BIG.refTrackId = '';
  S.selTrack = BIG.trackId;
  S.sel.clear();
  if (BIG.refBarOpen) bigRefFillTracks();
  renderInspector();
  bigFit();
};
$('btnBig').onclick = () => {
  if (!bigOpen()) setSaveState('先选中一条有块的轨');
};
$('btnBlocksAll').onclick = async () => {
  const rel = S.blocksName
    || (S.pick?.path ? String(S.pick.path).replace(/\\/g, '/') : '');
  if (!rel) { setSaveState('先在左栏点一条录音或媒体'); return; }
  if (S.blocksName !== rel || !S.blocks) await loadBlocks(rel);
  const tr = blocksToTrack(rel);
  if (tr) bigOpen(tr.id);
  else setSaveState(blocksWhy());
};

// ---------------------------------------------------------------- 版本(社区版 / Pro)
// 社区版把 Pro 功能**灰掉而不是藏起来**:按钮还在,右上角一个 PRO 角标,
// 功能开关。2026-10-23 起只发一个全功能版本,这段现在不会灰任何按钮 ——
// 留着是因为服务端的 _pro 守卫还在,以后若要再分版,前端这边不用重写。
// ★ 前端只管显示;真正的门在服务端(studio_api.py 的 _pro 守卫),绕不过去。
const EDITION = { pro: true, name: '声麦 Studio Pro', features: null };

const PRO_BTN = {
  btnAuto: ['auto', '一键调音'],
  btnRef: ['ref', '跟原唱'],
  btnVoice: ['voiceCloud', '个人音色补音'],
  btnNL: ['nl', '调音助手'],
  btnGen: ['gen', 'AI 伴奏'],
  btnStems: ['stems', '分轨导出'],
};

function applyEdition(a) {
  EDITION.pro = a.edition === 'pro';
  EDITION.name = a.editionName || '声麦 Studio Pro';
  document.title = EDITION.name;
  EDITION.features = a.features || null;
  const on = (k) => (EDITION.features && k in EDITION.features)
    ? !!EDITION.features[k] : EDITION.pro;
  for (const [id, [key, label]] of Object.entries(PRO_BTN)) {
    const b = $(id);
    if (!b || on(key)) continue;
    b.classList.add('pro-lock');
    b.dataset.proLock = label;
    b.setAttribute('aria-disabled', 'true');
    b.title = `「${label}」是 Pro 版功能`;
  }
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest && ev.target.closest('.pro-lock');
  if (!b) return;
  ev.preventDefault();
  ev.stopPropagation();
  toast(`「${b.dataset.proLock}」是 Pro 版功能,当前为社区版。`, 4500);
}, true);

async function boot() {
  resize();
  new ResizeObserver(() => resize()).observe(area);
  startFrames();
  engine.ensure();

  // 偏好先问文件那份（比 localStorage 全），再往下走 —— 引导要不要弹就看它
  await prefsLoad();
  await loadRecordings();
  await loadMedia();
  await loadAsrStatus();
  // 版本先问清楚:Pro 按钮要在这之前就灰掉,免得开了面板才被 403 拦
  try { applyEdition(await api('/about')); } catch { /* 问不到就按 Pro 处理 */ }

  const q = new URLSearchParams(location.search).get('p');
  try {
    if (q) {
      await loadProject(q);
    } else {
      const r = await api('/projects');
      if (r.projects?.length) await loadProject(r.projects[0].name);
      else await newProject();
    }
  } catch (e) {
    console.error('载入工程失败', e);
    await newProject();
  }
  drawStatic(); drawOverlay(); renderMixer(); renderInspector(); drawRoll();
  loadProjectList().catch(() => {});
  await loadFxTypes();
  engine.initFx().then(() => renderFxPanel());
  renderFxPanel();

  window.STUDIO = {
    S, engine, ready: true,
    save: saveNow, load: loadProject, newProject, deleteProject,
    deleteRecording, deleteMedia,
    // 整页块编辑 —— 冒烟测试走这几个口
    bigOpen, bigClose, bigFit,
    bigState: () => ({
      open: BIG.open, trackId: BIG.trackId, pps: BIG.pps, t0: BIG.t0,
      t0x: BIG.t0x, ruler: BIG.ruler, laneY: BIG.laneY, laneH: BIG.laneH,
      w: BIG.w, h: BIG.h,
      view: BIG.view, pen: BIG.pen, frozen: BIG.frozen ? { ...BIG.frozen } : null,
      pitch: BIG.view === 'pitch' ? bigPitchGeo() : null,
      clips: (bigTrack()?.clips || []).map(c => ({
        id: c.id, start: c.start, length: c.length,
        srcIn: c.srcIn, srcOut: c.srcOut,
      })),
    }),
    bigSelect: (ids) => {
      S.sel.clear();
      for (const id of (ids || [])) S.sel.add(id);
      if (BIG.open) drawBig();
      return S.sel.size;
    },
    // 逐音精修 / 原唱条
    bigViewSet, bigPenSet,
    bigPitchInfo: () => (bigTrack()?.clips || []).map(c => ({
      id: c.id, start: c.start, length: c.length, kind: c.kind || 'note',
      f0: Number(c.f0) > 0 ? Number(c.f0) : null,
      baseMidi: c.kind === 'sound' ? null : clipBaseMidi(c),
      soundingMidi: c.kind === 'sound' ? null : clipSoundingMidi(c),
      semis: c.semis || 0, cents: c.cents || 0, offset: pitchOffsetOf(c),
      curve: curveSnapshot(c), hasPitch: clipHasPitch(c),
      needsRender: !!c.needsRender, renderKey: c.renderKey || null,
      editable: c.kind !== 'sound',
    })),
    // 这个音在整页画布上的中心点（测试用真鼠标去拖它）
    bigPitchPoint: (clipId) => {
      const c = (bigTrack()?.clips || []).find(x => String(x.id) === String(clipId));
      if (!c || BIG.view !== 'pitch' || !bigCv) return null;
      const r = bigCv.getBoundingClientRect();
      const geo = bigPitchGeo();
      return {
        x: r.left + bigX(c.start + c.length / 2),
        y: r.top + geo.Y(clipSoundingMidi(c)),
        left: r.left, top: r.top, semiPx: geo.semiPx,
      };
    },
    bigFlat: () => bigFlatSel(),
    bigCurveClear: () => bigClearCurveSel(),
    bigRefOpen: (on) => { bigRefBarSet(on); return BIG.refBarOpen; },
    bigRefPick: (refId) => {
      if ($('bigRefTrack')) $('bigRefTrack').value = String(refId);
      BIG.refTrackId = String(refId);
      BIG.refPlan = null;
      return BIG.refTrackId;
    },
    bigRefAnalyze: () => bigRefAnalyze(),
    bigRefApplySel: () => bigRefApplySel(),
    bigRefShowSet: (on) => {
      BIG.refShow = !!on;
      if ($('bigRefShow')) $('bigRefShow').checked = BIG.refShow;
      drawBig();
      return BIG.refShow;
    },
    bigRefState: () => {
      const p = BIG.refPlan;
      return {
        hasPlan: !!p,
        actions: (p?.actions || []).length,
        pitchActions: (p?.actions || []).filter(a => a.kind === 'pitch').length,
        coverage: p?.coverage ?? null,
        status: $('bigRefState')?.textContent || '',
        show: BIG.refShow,
      };
    },
    bigTargetMidi: (clipId) => {
      const c = (bigTrack()?.clips || []).find(x => String(x.id) === String(clipId));
      const a = c ? bigRefActionFor(c.id) : null;
      if (!c || !a) return null;
      return bigTargetMidi(c, a, c.start, clipBaseMidi(c));
    },
    blocksToTrack,
    // 切块 / 导入原唱 —— 冒烟测试走这两个口
    loadBlocks, blocksWhy, loadAsrStatus, refImportFile,
    addClip, seekTo, play: () => { seekTo(S.playAt); togglePlay(); },
    addTrack, renameTrack: startRenameTrack, nudgeSelected,
    renderPending, deleteSelected, duplicateSelected, undo, redo,
    createSamplerFromClip, rollTrack, drawRoll, rollScrollTo,
    copySelected, cutSelected, pasteClips, splitSelected, quantizeSelected,
    // 标记（手动打点）—— 冒烟测试走这几个口
    markers: () => markers().map(m => ({ id: m.id, t: m.t, name: m.name })),
    markerAdd: (t) => markerAdd(t), markerDelete,
    markerRename: (id, name) => {
      const m = markers().find(x => x.id === id);
      if (!m) return false;
      takeSnap(); m.name = String(name || '').slice(0, 16) || m.name;
      markDirty(); drawStatic(); return true;
    },
    markerAlign: (markerId, atT) => {
      if (typeof atT === 'number') S.playAt = atT;
      return alignSelectedToMarker(markerId);
    },
    snapT: (t, skipId) => snapT(t, skipId),
    renderMixer, resetClip, checkPitch, applyPitchFix, exportProject,
    openExportPanel, exportCheck, exportOptions,
    exportStems: (stage) => exportProject('stems', { stage: stage || 'pre' }),
    detectChords, harmonySelected, diatonicShift, keyPcs, noteMidiOf,
    renderCurveClip, setupCurveEditor, curvePts,
    drawStatic, checkMemory, cacheInfo, renderInspector,
    stats: engine.stats,
    // 效果器（冒烟测试也走这几个口）
    fxTypes: FX_TYPES, fxAdd: addFx, openFx, renderFxPanel,
    fxTrack, fxChain: () => fxChainOf(fxTrack()),
    fxSlots: () => ($('fxSlots') ? $('fxSlots').querySelectorAll('.fxslot').length : -1),
    fxRemove: (id) => {
      const tr = fxTrack(); if (!tr) return false;
      const ch = fxChainEdit(tr);
      if (!ch) return false;
      const i = ch.findIndex(s => s.id === id);
      if (i < 0) return false;
      takeSnap(); ch.splice(i, 1); pushChain(tr); markDirty();
      renderFxPanel(); renderMixer();
      return true;
    },
    fxSetParam: (id, key, val) => {
      const tr = fxTrack(); if (!tr) return false;
      const s = fxChainOf(tr).find(x => x.id === id);
      if (!s) return false;
      s.params = s.params || {}; s.params[key] = val;
      pushChain(tr); markDirty(); renderFxPanel();
      return true;
    },
    fxStats: () => engine.fxStats,
    // VST（冒烟测试也走这几个口）
    vstScan: scanVst,
    vstPlugins: () => VST_LIST,
    vstProbe: () => VST_PROBE,
    vstAdd: addVst,
    vstOpen: openVst,
    vstSlots: () => vstSlotsOf(fxTrack()),
    vstSetParam: (id, key, val) => {
      const tr = fxTrack(); if (!tr) return false;
      const s = vstSlotsOf(tr).find(x => x.id === id);
      if (!s) return false;
      s.params = s.params || {}; s.params[key] = val;
      markDirty(); vstTouched(tr); renderVstPanel();
      return true;
    },
    vstRemove: (id) => {
      const tr = fxTrack(); if (!tr) return false;
      const ch = vstSlotsEdit(tr);
      const i = ch.findIndex(s => s.id === id);
      if (i < 0) return false;
      takeSnap(); ch.splice(i, 1);
      if (!ch.length) {
        tr.fx.freeze = false;
        vstFrozen.delete(tr.id);
        engine.clearFrozen(tr.id);
      }
      markDirty(); vstTouched(tr); renderVstPanel(); renderMixer(); drawStatic();
      return true;
    },
    vstFreezeOn: (on) => {
      const tr = fxTrack();
      if (!tr) return false;
      const cb = $('vstFreeze');
      if (cb) { cb.checked = !!on; cb.onchange(); }
      return !!(tr.fx && tr.fx.freeze);
    },
    vstFreezeState: () => {
      const tr = fxTrack();
      const st = tr ? vstFrozen.get(tr.id) : null;
      return st ? { state: st.state, sig: st.sig, error: st.error, sec: st.sec } : null;
    },
    vstFreezeNow: async (trackId) => {
      const id = trackId || fxTrack()?.id;
      if (!id) return null;
      // ★ 先掐掉防抖里的那一次，否则两代同时跑，先返回的是「被顶掉」那代
      clearTimeout(vstTimers.get(id));
      vstTimers.delete(id);
      await doFreeze(id);
      // 万一还是被更新的一代接过去了，等它落定再返回
      for (let i = 0; i < 600; i++) {
        const st = vstFrozen.get(id);
        if (!st || st.state !== 'working') return st;
        await new Promise(r => setTimeout(r, 100));
      }
      return vstFrozen.get(id);
    },
    vstAudition: (bypass) => { vstAud.bypass = !!bypass; return vstAudDo(); },
    vstAudToggle,
    vstAudState: () => ({ active: vstAud.active, bypass: vstAud.bypass,
                          gen: vstAud.gen, auditioning: engine.auditioning }),
    vstStopAudition: () => { vstAud.active = false; vstAud.gen++; engine.stopAudition(); syncAudBtn(); },
    vstPreviewAll,
    engineFrozen: () => Array.from(engine.frozen.keys()),
    // 片段音量（冒烟测试不模拟鼠标拖拽）
    clipGain: (clipId, v) => {
      const it = clipById(clipId);
      if (!it) return null;
      takeSnap();
      it.clip.gain = clamp(Number(v), 0, 2);
      markDirty(); drawStatic(); renderInspector();
      return it.clip.gain;
    },
    clipGainText: (clipId) => {
      const it = clipById(clipId);
      return it ? clipGainText(it.clip) : null;
    },
    mediaRename,
    mediaList: () => (S.media || []).map(f => ({ name: f.name, path: f.path })),
    // 一键调音（冒烟测试走这几个口）
    autoMix,
    autoApply,
    autoPlan: () => AUTO_PLAN,
    autoSelect,
    // 我的声音 / 补音 / 调音助手（冒烟测试走这几个口）
    voiceOpen: openVoicePanel,
    voiceState: () => VOICE,
    extendOpen: openExtendDialog,
    extendGo: doExtend,
    extendApply: applyExtension,
    extendState: () => (EXT ? {
      clipId: EXT.clip && EXT.clip.id, srcDur: EXT.srcDur,
      result: EXT.result, panel: !document.getElementById('extPanel').hidden,
    } : null),
    nlOpen: openNlPanel,
    nlAsk: nlGo,
    nlApply: nlApplyNow,
    nlPlan: () => NL_PLAN,
    // AI 伴奏（冒烟测试走这几个口：试跑不花钱，所以可以整条走一遍）
    genOpen: openGenPanel,
    genMock,
    genPlace,
    genChoices: genRenderChoices,      // 冒烟：只看按钮，不真去拆
    genState: () => ({
      panel: !document.getElementById('genPanel').hidden,
      task: genTask,
      state: (document.getElementById('genState') || {}).textContent || '',
      choices: document.querySelectorAll('#genBody [data-place]').length,
    }),
    // 鼓机
    drumAdd: addDrumTrack,
    drumState: () => {
      const tr = S.proj?.tracks.find(t => t.kind === 'drum');
      if (!tr) return null;
      return { id: tr.id, name: tr.name, kit: tr.kit, base: tr.sample?.baseMidi,
               slots: (tr.sample?.pool || []).length, notes: (tr.notes || []).length };
    },
    drumPreview: (midi) => {
      const tr = S.proj?.tracks.find(t => t.kind === 'drum');
      if (!tr) return false;
      return engine.previewNote(tr, { midi, length: 0.3, vel: 0.9 });
    },
    // 跟原唱矫正 + 歌库（冒烟测试走这几个口）
    refOpen,
    refAnalyze: () => refAnalyze(),
    refApply,
    refPlan: () => REF_PLAN,
    refIdentify: () => refIdentify(),
    refSelect,
    refLibrary: () => REF_LIB,
    refTrackPick: (refId, takeId) => {
      $('refRef').value = String(refId);
      $('refTake').value = String(takeId);
      return [$('refRef').value, $('refTake').value];
    },
    // 新手引导（冒烟测试走这两个口）
    guide: obShow,
    guideClose: obClose,
    guideState: () => ({
      open: !document.getElementById('onboard').hidden,
      step: obIdx, total: OB_STEPS.length,
      title: document.getElementById('obTitle').textContent,
      points: document.querySelectorAll('#obPoints li').length,
      acts: document.querySelectorAll('#obActs [data-ob]').length,
      seen: obFlagGet(),
    }),
    get project() { return S.proj; },
  };
  window.STUDIO_STATS = engine.stats;
  SM_PING('boot 跑完');
  // 首次引导：等首屏画完再弹，别跟加载抢那一两百毫秒
  setTimeout(obMaybe, 350);
}
boot().catch(e => {
  console.error(e);
  setSaveState('启动失败', 'dirty');
});

/* ---------------------------------------------------------------- 应用内弹窗
   ★ 他的要求(2026-10-22):这是桌面应用,不是浏览器页面 ——
     原生 alert / confirm / prompt 会贴在窗口顶部,还会把「127.0.0.1:端口」写进框里。
     一律换成页面正中的对话框:与「退出应用」那张同一套深色卡片,不显示任何网址。
   ★ 全部是 Promise:
       await uiConfirm('删掉…')   → true / false
       await uiPrompt('名字', 旧值) → 字符串 / null(取消)
       await uiAlert('失败了…')     → 无返回值
     ★ 不要在任何地方再直接写原生 confirm —— 它同步返回,语义和这里对不上。
   ★ 回车 = 确定,Esc / 点遮罩 = 取消(和退出框一致)。 */
const MODALS = [];

function uiAsk(o = {}) {
  const title = o.title || '';
  const text = o.message == null ? '' : String(o.message);
  const okText = o.okText || '确定';
  const cancelText = o.cancelText === null ? null : (o.cancelText || '取消');
  const danger = !!o.danger;

  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'uiwrap';
    wrap.innerHTML =
      '<div class="uicard" role="dialog" aria-modal="true">'
      + (title ? '<div class="uit"></div>' : '')
      + '<div class="uim"></div>'
      + (o.input == null ? '' : '<input class="uiinput" type="text" spellcheck="false">')
      + '<div class="uib">'
      + (cancelText === null ? '' : '<button class="uic" type="button"></button>')
      + '<button class="uik' + (danger ? ' danger' : '') + '" type="button"></button>'
      + '</div></div>';
    if (title) wrap.querySelector('.uit').textContent = title;
    wrap.querySelector('.uim').textContent = text;      // ★ 用 textContent:文案里带文件名也不能当 HTML
    const inp = wrap.querySelector('.uiinput');
    if (inp) {
      inp.value = o.input;
      inp.placeholder = o.placeholder || '';
    }
    const bOk = wrap.querySelector('.uik');
    const bNo = wrap.querySelector('.uic');
    bOk.textContent = okText;
    if (bNo) bNo.textContent = cancelText;

    let done = false;
    const close = (ok) => {
      if (done) return;
      done = true;
      const i = MODALS.indexOf(entry);
      if (i >= 0) MODALS.splice(i, 1);
      wrap.remove();
      resolve(ok ? (inp ? inp.value : true) : (inp ? null : false));
    };
    const entry = { el: wrap, cancel: () => close(false), ok: () => close(true) };
    MODALS.push(entry);

    bOk.addEventListener('click', () => close(true));
    if (bNo) bNo.addEventListener('click', () => close(false));
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(false); });
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); close(true); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
    });

    document.body.appendChild(wrap);
    // 危险操作用焦点落在「取消」上,回车不会顺手把东西删了
    requestAnimationFrame(() => {
      if (danger && bNo) bNo.focus();
      else if (inp) { inp.focus(); inp.select(); }
      else bOk.focus();
    });
  });
}

const uiConfirm = (message, o = {}) => uiAsk({ message, ...o }).then(v => v === true);
const uiAlert = (message, o = {}) => uiAsk({ title: o.title || '提示', message, cancelText: null,
  okText: o.okText || '知道了', ...o }).then(() => undefined);
const uiPrompt = (message, value, o = {}) => uiAsk({ message, input: value == null ? '' : String(value),
  ...o }).then(v => (v == null ? null : v));

/* ---------------------------------------------------------------- ESC：逐级返回 / 退出
   ★ 他的要求(2026-10-22):左上角一颗 ESC,按一下退回上一级;多级就一级一级退,
     退到底弹「是否退出应用」。
   ★ 「一级」= 盖在时间轴上的任意一层:各类面板、整页块编辑、新手引导、帮助页、
     退出确认框它自己。收哪一层不靠调用顺序猜 —— 按 z-index 挑最上面那层。
   ★ 关面板走它自己的 × (dispatch click),不直接 .hidden:
     AI 伴奏收起来要 genStop()、块编辑要落笔,自己那条路才不漏清理。 */
const ESC_LAYERS = [
  ['autoPanel', 'autoClose'], ['refPanel', 'refClose'], ['voicePanel', 'voiceClose'],
  ['extPanel', 'extClose'], ['nlPanel', 'nlClose'], ['genPanel', 'genClose'],
  ['genLib', 'libClose'], ['drumPanel', 'drumClose'], ['expPanel', 'expClose'],
  ['settings', 'setClose'], ['genHelp', 'genHelpClose'],
  ['bigEdit', 'bigClose'], ['onboard', null], ['quitDlg', 'quitCancel'],
];

function escTopLayer() {
  const list = [];
  for (const [id, closeId] of ESC_LAYERS) {
    const el = $(id);
    if (!el || el.hidden) continue;
    list.push({ el, closeId, z: Number(getComputedStyle(el).zIndex) || 0 });
  }
  list.sort((a, b) => a.z - b.z);
  return list.length ? list[list.length - 1] : null;
}

/* 退出确认框:只建一次,之后开关就行 */
let QUIT = null;
function quitAsk() {
  if (!QUIT) {
    const wrap = document.createElement('div');
    wrap.id = 'quitDlg';
    wrap.className = 'quitwrap';
    wrap.hidden = true;
    wrap.innerHTML =
      '<div class="quitcard" role="dialog" aria-modal="true" aria-labelledby="quitTitle">'
      + '<div class="quitt" id="quitTitle">退出声麦 Studio Pro？</div>'
      + '<p class="quitp">工程会自动保存，下次打开可以接着做。</p>'
      + '<div class="quitb">'
      + '<button id="quitCancel" type="button">取消</button>'
      + '<button id="quitOk" type="button" class="danger">退出应用</button>'
      + '</div></div>';
    document.body.appendChild(wrap);
    wrap.querySelector('#quitCancel').addEventListener('click', () => { wrap.hidden = true; });
    wrap.querySelector('#quitOk').addEventListener('click', () => {
      // 桌面壳里由 Python 关窗口;浏览器里退回 window.close()
      const api = window.pywebview && window.pywebview.api;
      if (api && api.quit) { api.quit(); return; }
      window.close();
      setTimeout(() => toast('当前环境不允许直接退出，请关闭窗口。', 6000), 400);
    });
    wrap.addEventListener('click', (e) => { if (e.target === wrap) wrap.hidden = true; });
    QUIT = wrap;
  }
  QUIT.hidden = false;
  QUIT.querySelector('#quitOk').focus();
}

function escBack() {
  // 应用内弹窗盖在最上面:一次只关它,底下的面板不动
  if (MODALS.length) { MODALS[MODALS.length - 1].cancel(); return; }
  const top = escTopLayer();
  if (!top) { quitAsk(); return; }
  if (top.el.id === 'quitDlg') { top.el.hidden = true; return; }
  if (top.el.id === 'onboard') { obClose(); return; }
  const btn = top.closeId ? $(top.closeId) : null;
  if (btn) { btn.click(); return; }
  top.el.hidden = true;
}

$('btnEsc')?.addEventListener('click', escBack);
/* 键盘 Esc 与左上角那颗键走同一条路。
   ★ 必须在**捕获阶段**处理:面板、块编辑、设置各自都挂过 Escape,不抢先就会被连着
     触发两次 —— 实测「设置 + 块编辑」同时开着时,一下就把两层都关了。
   ★ 关掉一层之后 stopPropagation,让下面那些老处理器这一次不重复动作;
     顶层什么都关不掉时放行,由原来的处理器清空选中,再弹退出确认。
   ★ 输入框里的 Esc 属于输入框自己(放弃改名),这里一律不接。 */
window.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || e.defaultPrevented) return;
  // 应用内弹窗优先:它自己的 keydown 也会处理,close() 是幂等的,不会关两次
  if (MODALS.length) {
    e.preventDefault();
    e.stopPropagation();
    MODALS[MODALS.length - 1].cancel();
    return;
  }
  const el = e.target;
  const tag = (el && el.tagName || '').toLowerCase();
  if (['input', 'select', 'textarea'].includes(tag) || (el && el.isContentEditable)) return;
  if (!escTopLayer()) {                       // 顶层:放行给原来的「Esc 清空选中」,再问退出
    quitAsk();
    return;
  }
  e.preventDefault();
  e.stopPropagation();
  escBack();
}, true);
