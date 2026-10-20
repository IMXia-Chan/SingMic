/*
 * 全站科技感指针。
 *
 * 为什么不直接用 CSS 的 cursor:url():
 *   · Windows 上的 Edge/Chromium 对 SVG 当指针并不稳(值算对了、画面上还是系统箭头,不报错);
 *   · PNG 能用,但尺寸被系统卡着(32px 一放大就糊),而且**做不出真正的外发光**;
 *   · 更要命的是各处的 cursor:crosshair / grab / text 会把它盖掉,
 *     结果就是"只有按钮上是科技箭头,拖到别处又变回系统鼠标"。
 * 所以这里把系统指针整个藏起来,自己画一个跟着鼠标走。
 *
 * ★ 藏系统指针的样式是**由这段脚本注入**的:脚本没加载成功,`.np` 这个类就不会加上,
 *   页面自然退回 CSS 里那套 cursor:url(...) —— 不会出现"整页没有指针"。
 */
(() => {
  if (window.__neonPointer) return;
  window.__neonPointer = true;

  // 环形准星:圆环 + 中心点 + 四根刻度。★ 点击点在**圆的中心**,
  // 所以元素要往左上偏 (HOT, HOT) 才能让中心落在鼠标坐标上。
  const HOT = 14;

  const STYLE = `
  html.np, html.np *{ cursor:none !important; }
  #np{ position:fixed; left:0; top:0; width:28px; height:28px; pointer-events:none;
       z-index:2147483647; transform:translate3d(-99px,-99px,0); will-change:transform;
       filter:drop-shadow(0 0 2px rgba(150,248,255,.9))
              drop-shadow(0 0 7px rgba(53,212,255,.75))
              drop-shadow(0 0 15px rgba(53,212,255,.4));
       opacity:0; transition:opacity .12s linear; }
  #np.on{ opacity:1 }
  /* 按下时缩一点(以准星中心为原点),手感上有回应 */
  #np svg{ display:block; width:100%; height:100%; transform-origin:50% 50%;
           transition:transform .07s ease-out }
  #np.down svg{ transform:scale(.86) }
  `;

  const MARK = `<svg viewBox="-14 -14 28 28" xmlns="http://www.w3.org/2000/svg">
    <circle r="10.5" fill="none" stroke="#7ff0ff" stroke-width="1.1" opacity=".8"/>
    <circle r="1.9" fill="#eafcff"/>
    <path d="M0 -14 L0 -11 M0 11 L0 14 M-14 0 L-11 0 M11 0 L14 0"
          stroke="#7ff0ff" stroke-width="1.1" opacity=".9"/>
  </svg>`;

  function build() {
    const st = document.createElement('style');
    st.textContent = STYLE;
    document.head.appendChild(st);
    const el = document.createElement('div');
    el.id = 'np';
    el.innerHTML = MARK;
    document.body.appendChild(el);
    // ★ `html.np`(藏系统指针)不在这里加,而是等我们自己的指针真的亮起来再加 ——
    //   否则一旦 #np 因为任何原因没显示出来,整页就一个指针都不剩。
    return el;
  }

  let el = null;
  let seen = false;   // 鼠标在这个页面上出现过(用来决定"回到窗口时该不该点亮")
  let on = false;     // 现在是不是亮着

  function show() {
    if (!on) {
      on = true;
      el.classList.add('on');
      document.documentElement.classList.add('np');
    }
  }
  function hide() { if (on) { on = false; el.classList.remove('on'); } }

  // ★ 这里以前是 `if (!seen) { seen = true; el.classList.add('on'); }` —— 只有**第一次**
  //   移动才会点亮。于是只要指针被藏过一次(切窗口 blur、移到浏览器边框 mouseleave、
  //   弹了系统的选文件/打印对话框),后面再怎么动都不会再亮:光标永远隐身,
  //   看起来就是"全局指针没了"。现在每帧移动都会把 on 补回来(show() 幂等,不重排)。
  function place(x, y) {
    el.style.transform = `translate3d(${x - HOT}px,${y - HOT}px,0)`;
    seen = true;
    show();
  }

  function init() {
    el = build();
    document.addEventListener('mousemove', (e) => place(e.clientX, e.clientY), { passive: true });
    // 按下去的时候缩一点点,手感上有回应
    document.addEventListener('mousedown', () => el.classList.add('down'), true);
    document.addEventListener('mouseup', () => el.classList.remove('down'), true);
    // 指针移出窗口就把自己藏起来(否则会停在边上,像卡住了)
    document.addEventListener('mouseleave', hide);
    document.addEventListener('mouseenter', () => { if (seen) show(); });
    // ★ 切到别的窗口/标签时藏起来没问题,但**回来的时候必须有东西把它再点亮**。
    //   只靠 mouseenter 不够:从任务栏点回来、或用 Alt+Tab 回来时,鼠标可能已经停在窗口里,
    //   浏览器不会再补一次 mouseenter —— 结果就是指针一直隐身,看起来"全局指针没了"。
    const reshow = () => { if (seen) show(); };
    window.addEventListener('focus', reshow);
    window.addEventListener('pageshow', reshow);       // 从往返缓存(后退)回来
    document.addEventListener('visibilitychange', () => { if (!document.hidden) reshow(); });
    window.addEventListener('blur', hide);
    // 万一脚本跑在 body 还没出来的时候,等 DOM 就绪再挂
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
