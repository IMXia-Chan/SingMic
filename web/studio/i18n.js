/* ===========================================================================
 * 界面多语言（i18n）
 *
 * 设计取舍：
 *   1) **字典以中文原文为键**。这样不用给一千多条文案起英文 key，调用点一处不改，
 *      漏译的地方自然回落到中文，不会出现空白或 undefined。
 *   2) 渲染出来的界面按**文本节点精确匹配**翻译，动态面板重新渲染后由
 *      MutationObserver 再走一遍；带变量的句子（`已保存 ${n} 处`）不会命中，
 *      仍然显示中文 —— 宁可漏，不可把句子拼坏。
 *   3) 切回简体中文时用记住的原文还原，不做反向查表（反向会撞同义句）。
 *
 * ★ 不要翻译的东西：录音名、工程名、用户输入、<code>/<kbd> 里的技术词。
 * ========================================================================= */

export const LANGS = [
  { id: 'zh-Hans', label: '简体中文' },
  { id: 'zh-Hant', label: '繁體中文' },
  { id: 'en', label: 'English' },
  { id: 'ja', label: '日本語' },
  { id: 'ko', label: '한국어' },
];

const DEFAULT = 'zh-Hans';
const LS_KEY = 'singmic.lang';

let CUR = DEFAULT;
const DICTS = Object.create(null);      // id -> {中文: 译文}
const PATTERNS = Object.create(null);   // id -> [[正则, 译文模板]]
let loading = null;

/* 记住被我们改过的节点原文，切回中文时照原样写回去 */
const ORIG_TEXT = new Map();            // TextNode -> 原文
const ORIG_ATTR = new Map();            // Element -> Map(attr -> 原文)
const ORIG_HTML = new Map();            // Element -> 原 innerHTML

/* 一整段里有 <b>/<code> 夹着的（`关闭后仅在 <code>Ctrl+S</code> 或切换工程时写入`），
   按文本节点拆开翻会把语序弄乱（日韩英语序都跟中文不同），只能整段当 HTML 翻。
   ★ 只认纯排版标签：带 id / 按钮的段落整段换掉会打断已绑定的监听。 */
const SAFE_HTML = new Set(['B', 'I', 'EM', 'STRONG', 'CODE', 'KBD', 'SPAN', 'BR',
  'SMALL', 'SUB', 'SUP', 'MARK', 'U']);
const INTERACTIVE = 'button,input,select,textarea,a[href],canvas,svg,audio,video,[id],[onclick]';

function safeHtmlEl(el) {
  if (!el.children.length) return false;
  for (const c of el.querySelectorAll('*')) {
    if (!SAFE_HTML.has(c.tagName)) return false;
    if (c.matches(INTERACTIVE)) return false;
    if (ATTRS.some((a) => c.hasAttribute(a))) return false;
  }
  return true;
}

const TAGSEQ = /<\/?([a-zA-Z][\w-]*)/g;

function tagSeq(s) {
  const out = [];
  TAGSEQ.lastIndex = 0;
  let m;
  while ((m = TAGSEQ.exec(s))) out.push(m[1].toLowerCase());
  return out.join(',');
}

function trHtml(el) {
  const cur = el.innerHTML;
  let orig = ORIG_HTML.get(el);
  if (orig === undefined) { orig = cur; ORIG_HTML.set(el, orig); }
  if (CUR === DEFAULT) {
    if (cur !== orig) el.innerHTML = orig;
    return true;
  }
  if (cur !== orig) return true;          // 已经是译文（或别处改过），别再套一层
  // 字典键是收词时压成单行、去掉首尾空白的，这里也压一遍才对得上
  const key = orig.replace(/\s+/g, ' ').trim();
  const got = t(key);
  if (got === key) return false;
  // ★ 标签序列必须原样保留：译错一个闭合，整块版面就散了 —— 对不上就退回中文
  if (tagSeq(got) !== tagSeq(key)) return false;
  el.innerHTML = got;
  return true;
}

const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'KBD', 'TEXTAREA', 'CANVAS']);

export function langId() { return CUR; }

export function langLabel(id = CUR) {
  const l = LANGS.find((x) => x.id === id);
  return l ? l.label : id;
}

/* 系统语言猜一个：认得 zh-Hant / zh-Hans、en、ja、ko，其余一律简体 */
export function detectLang() {
  const list = (navigator.languages && navigator.languages.length
    ? navigator.languages : [navigator.language || '']);
  for (const raw of list) {
    const s = String(raw || '').toLowerCase();
    if (!s) continue;
    if (s.startsWith('zh')) {
      return /hant|tw|hk|mo/.test(s) ? 'zh-Hant' : 'zh-Hans';
    }
    if (s.startsWith('ja')) return 'ja';
    if (s.startsWith('ko')) return 'ko';
    if (s.startsWith('en')) return 'en';
  }
  return DEFAULT;
}

export function savedLang() {
  try {
    const v = localStorage.getItem(LS_KEY);
    if (v && LANGS.some((l) => l.id === v)) return v;
  } catch { /* 读不到就用系统语言 */ }
  return null;
}

/* 带变量的句子（`缓存 {0}MB`）：字典键里用 {0} {1} 占位，
   加载时编译成「字面量 + (.*?)」的正则，渲染出来就能对上。 */
function compilePatterns(dict) {
  const out = [];
  for (const [k, v] of Object.entries(dict)) {
    if (!k.includes('{0}') && !/\{\d\}/.test(k)) continue;
    const esc = k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp('^' + esc.replace(/\\\{(\d)\\\}/g, '(.+?)') + '$');
    out.push([re, String(v)]);
  }
  return out;
}

async function ensureDict(id) {
  if (id === DEFAULT || DICTS[id]) return;
  loading = import(`./i18n/${id}.js`).then((m) => {
    DICTS[id] = m.default || {};
    PATTERNS[id] = compilePatterns(DICTS[id]);
  }).catch(() => { DICTS[id] = {}; PATTERNS[id] = []; });
  await loading;
}

/* 直接翻译一个字符串（toast、confirm、canvas 标签这类不走 DOM 的地方用） */
export function t(zh) {
  if (CUR === DEFAULT || typeof zh !== 'string') return zh;
  const d = DICTS[CUR];
  if (d && d[zh]) return d[zh];
  const pats = PATTERNS[CUR];
  if (pats) {
    for (const [re, tpl] of pats) {
      const m = re.exec(zh);
      if (!m) continue;
      return tpl.replace(/\{(\d)\}/g, (s, i) => (m[+i + 1] != null ? m[+i + 1] : s));
    }
  }
  return zh;
}

/* 文本节点里的换行/缩进只在源码里存在：查表前先压成单空格，
   否则跨行写的一段话永远对不上字典键。 */
function trText(s) {
  if (!s) return s;
  const core = s.trim();
  if (!core) return s;
  const flat = core.replace(/\s+/g, ' ');
  const got = t(flat);
  if (got === flat) return s;
  return s.replace(core, got);
}

function skipEl(el) {
  for (let n = el; n; n = n.parentElement) {
    if (SKIP_TAGS.has(n.tagName) || n.hasAttribute('data-i18n-skip')) return true;
  }
  return false;
}

function trAttrs(el) {
  for (const a of ATTRS) {
    if (!el.hasAttribute(a)) continue;
    let m = ORIG_ATTR.get(el);
    if (!m) { m = new Map(); ORIG_ATTR.set(el, m); }
    const orig = m.has(a) ? m.get(a) : el.getAttribute(a);
    if (!m.has(a)) m.set(a, orig);
    const want = CUR === DEFAULT ? orig : trText(orig);
    if (el.getAttribute(a) !== want) el.setAttribute(a, want);
  }
}

function walk(root) {
  if (!root) return;
  if (root.nodeType === Node.TEXT_NODE) {
    const el = root.parentElement;
    if (!el || skipEl(el)) return;
    const orig = ORIG_TEXT.has(root) ? ORIG_TEXT.get(root) : root.data;
    if (!ORIG_TEXT.has(root)) ORIG_TEXT.set(root, orig);
    const want = CUR === DEFAULT ? orig : trText(orig);
    if (root.data !== want) root.data = want;
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  if (skipEl(root)) {
    /* ★ 被跳过的元素（TEXTAREA 等）里面是用户输入，不能动；但挂在它身上的
       placeholder / title 是界面文案 —— 只翻属性，不进正文。 */
    if (SKIP_TAGS.has(root.tagName)) trAttrs(root);
    return;
  }
  trAttrs(root);
  /* 混合内容整段翻：成功了就不再往下走（子节点已被换掉） */
  if (safeHtmlEl(root) && trHtml(root)) return;
  for (let n = root.firstChild; n; n = n.nextSibling) walk(n);
}

/* ★ 切回中文时也要走一遍：walk() 在 DEFAULT 下会把记住的原文写回去。
   这里不能因为「当前是中文」就早退，否则界面上会留着一半译文。 */
export function applyDom(root = document.documentElement) {
  if (!root) return;
  walk(root);
}

/* 面板是 innerHTML 重画的：重画后自动再翻一遍 */
let pending = 0;
function schedule() {
  if (CUR === DEFAULT || pending) return;
  pending = requestAnimationFrame(() => {
    pending = 0;
    try { applyDom(document.documentElement); } catch { /* 结构在半途被替换，下一帧再说 */ }
  });
}
function observe() {
  const mo = new MutationObserver(() => schedule());
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  return mo;
}

let observer = null;

export async function setLang(id, opts = {}) {
  const next = LANGS.some((l) => l.id === id) ? id : DEFAULT;
  CUR = next;
  try { localStorage.setItem(LS_KEY, next); } catch { /* 记不住也能用 */ }
  document.documentElement.lang = next;
  await ensureDict(next);
  applyDom(document.documentElement);
  if (!observer && next !== DEFAULT) observer = observe();
  if (opts.silent !== true) {
    window.dispatchEvent(new CustomEvent('singmic:lang', { detail: next }));
  }
  return next;
}

/* 启动：优先级 显式指定 > 上次选的 > 系统语言 */
export async function initLang(prefer) {
  const id = (prefer && LANGS.some((l) => l.id === prefer) && prefer)
    || savedLang() || detectLang();
  return setLang(id);
}

/* 原生弹窗也翻（`confirm()` 的文案是我们自己写的） */
export function patchNativeDialogs() {
  if (patchNativeDialogs.done) return;
  patchNativeDialogs.done = true;
  const c = window.confirm;
  const a = window.alert;
  window.confirm = (m) => c.call(window, t(m));
  window.alert = (m) => a.call(window, t(m));
}
