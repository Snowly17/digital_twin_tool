/* ============================================================================
 * tools/verify_ponder.cjs
 *
 * 思索运行时的无浏览器验证：用项目自带的真实 three.module.js 逐帧执行
 * static/js/ponder.js，检查全部课件的动作触发、可见性、相机与网格。
 *
 * 用法：
 *   node tools/verify_ponder.cjs <payload.json>
 *
 * payload.json 由 Python 侧导出（见文件末尾说明）：
 *   python -c "..." 或 AppTest 抽取 #ponder-data 数据岛内容
 *
 * 为什么不用真浏览器：本环境无法驱动 headless 浏览器的 CDP（命令超时），
 * 所以用 DOM 桩 + 真实 three 来跑逻辑。像素级观感仍需人工确认。
 * ========================================================================== */
const fs = require('fs');
const path = require('path');

const ROOT = path.dirname(__dirname);
const OUT = [];
function log(s) { OUT.push(s); }
const realLog = console.log;
console.log = () => {};
const WARN = [];
console.warn = function () {
  WARN.push(Array.prototype.map.call(arguments, a => (a && a.message) ? a.message : String(a)).join(' '));
};
console.error = console.warn;

/* ---------------- 最小 DOM 桩 ---------------- */
function El(tag) {
  this.tagName = String(tag || 'div').toUpperCase();
  this.children = []; this.parentNode = null;
  this._classes = new Set(); this._id = ''; this._text = ''; this._style = {};
  this._listeners = {}; this._attrs = {}; this.dataset = {};
  this.clientWidth = 1400; this.clientHeight = 420;
  this.offsetWidth = 420; this.offsetHeight = 420; this.offsetLeft = 0;
  this.scrollLeft = 0; this.isContentEditable = false;
  const self = this;
  this.classList = {
    add: (...c) => c.forEach(x => self._classes.add(x)),
    remove: (...c) => c.forEach(x => self._classes.delete(x)),
    toggle: (c, on) => {
      if (on === undefined) { self._classes.has(c) ? self._classes.delete(c) : self._classes.add(c); }
      else if (on) self._classes.add(c); else self._classes.delete(c);
    },
    contains: c => self._classes.has(c)
  };
  this.style = new Proxy(this._style, {
    get: (t, k) => (k in t ? t[k] : ''),
    set: (t, k, v) => { t[k] = v; return true; }
  });
  this.style.setProperty = (k, v) => { self._style[k] = v; };
  this.style.getPropertyValue = (k) => self._style[k] || '';
}
Object.defineProperty(El.prototype, 'className', {
  get() { return Array.from(this._classes).join(' '); },
  set(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
});
Object.defineProperty(El.prototype, 'id', { get() { return this._id; }, set(v) { this._id = v; } });
Object.defineProperty(El.prototype, 'textContent', { get() { return this._text; }, set(v) { this._text = String(v); } });
Object.defineProperty(El.prototype, 'innerHTML', {
  get() { return this._html || ''; },
  set(v) { this._html = String(v); this.children = []; parseInto(this, String(v)); }
});
El.prototype.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) {
  const i = this.children.indexOf(c);
  if (i >= 0) this.children.splice(i, 1);
  c.parentNode = null; return c;
};
El.prototype.setAttribute = function (k, v) {
  this._attrs[k] = String(v);
  if (k.indexOf('data-') === 0) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
};
El.prototype.getAttribute = function (k) { return (k in this._attrs) ? this._attrs[k] : null; };
El.prototype.hasAttribute = function (k) { return k in this._attrs; };
El.prototype.removeAttribute = function (k) { delete this._attrs[k]; };
El.prototype.addEventListener = function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function () {};
El.prototype.contains = function (t) { let p = t; while (p) { if (p === this) return true; p = p.parentNode; } return false; };
El.prototype.getBoundingClientRect = function () { return { width: this.clientWidth, height: this.clientHeight, left: 0, top: 0 }; };
El.prototype.querySelector = function (s) { return this.querySelectorAll(s)[0] || null; };
El.prototype.querySelectorAll = function (sel) {
  const o = []; walk(this, n => { if (n !== this && matches(n, sel)) o.push(n); }); return o;
};
function walk(n, fn) { fn(n); n.children.forEach(c => walk(c, fn)); }
function matches(node, sel) {
  sel = sel.trim();
  if (sel.startsWith('.')) return node._classes.has(sel.slice(1).split(/[.\s]/)[0]);
  if (sel.startsWith('#')) return node._id === sel.slice(1);
  return false;
}
function parseInto(root, html) {
  const stack = [root];
  const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    if (m[5] !== undefined) { const t = m[5].trim(); if (t) stack[stack.length - 1]._text += t; continue; }
    if (m[1] === '/') { if (stack.length > 1) stack.pop(); continue; }
    const el = new El(m[2]);
    const ar = /([\w-]+)(?:="([^"]*)")?/g; let a;
    while ((a = ar.exec(m[3] || '')) !== null) {
      const k = a[1], v = a[2] === undefined ? '' : a[2];
      if (k === 'class') el.className = v;
      else if (k === 'id') el.id = v;
      else if (k.startsWith('data-')) el.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      else el.setAttribute(k, v);
    }
    stack[stack.length - 1].appendChild(el);
    if (m[4] !== '/' && !['br', 'img', 'input', 'hr', 'meta', 'link'].includes(m[2].toLowerCase())) stack.push(el);
  }
}

const btnEl = new El('button'); btnEl.id = 'ponder-btn';
const menuEl = new El('div'); menuEl.id = 'ponder-menu'; menuEl.style.display = 'none';
const byId = { 'ponder-btn': btnEl, 'ponder-menu': menuEl };
const docHandlers = {};
global.document = {
  readyState: 'complete',
  createElement(t) { return new El(t); },
  createElementNS() { return new El('svg'); },
  getElementById(id) { return id === 'ponder-data' ? { textContent: global.__PONDER_JSON__ } : (byId[id] || null); },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener(t, fn) { (docHandlers[t] = docHandlers[t] || []).push(fn); },
  removeEventListener() {}, body: new El('body')
};
const storage = {};
global.localStorage = {
  getItem: k => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: k => { delete storage[k]; }
};
let virtualNow = 1000;
global.performance = { now: () => virtualNow };
let rafQueue = [];
global.requestAnimationFrame = fn => { rafQueue.push(fn); return rafQueue.length; };
global.cancelAnimationFrame = () => { rafQueue = []; };
global.window = global;
global.innerWidth = 1920; global.innerHeight = 900;
global.addEventListener = () => {}; global.removeEventListener = () => {};
global.setTimeout = fn => { try { fn(); } catch (e) {} return 0; };
global.clearTimeout = () => {};

/* ---------------- 主流程 ---------------- */
(async function main() {
  const payloadFile = process.argv[2];
  if (!payloadFile) { realLog('用法: node tools/verify_ponder.cjs <payload.json>'); process.exit(2); }
  const payload = JSON.parse(fs.readFileSync(payloadFile, 'utf8'));

  const mod = await import('file://' + path.join(ROOT, 'static', 'vendor', 'three', 'three.module.js').replace(/\\/g, '/'));
  const THREE = Object.assign({}, mod.default || mod);
  global.THREE = THREE;
  let renderCount = 0;
  THREE.WebGLRenderer = class {
    constructor() { this.domElement = new El('canvas'); this.shadowMap = { enabled: false, type: 0 }; }
    setPixelRatio() {} setSize() {} render() { renderCount++; } dispose() {}
  };
  let orbitCount = 0;
  // 🔥 只挂 window（复现主场景的真实桥接状态），THREE 上不挂
  global.OrbitControls = function () {
    orbitCount++;
    this.target = new THREE.Vector3(); this.enabled = true; this.enableDamping = true;
    this.autoRotate = false; this.minDistance = 0; this.maxDistance = 0; this.maxPolarAngle = 0;
    this.update = () => {}; this.addEventListener = () => {};
  };
  global.CSS2DRenderer = function () { this.domElement = new El('div'); this.setSize = () => {}; this.render = () => {}; };
  global.CSS2DObject = function () { this.position = new THREE.Vector3(); };
  // GLB 用假加载器：只验证"装配流程不崩"，文件本身由 tools/make_charger_glb.py 与
  // 真实 GLTFLoader 单独校验过
  let glbCalls = 0;
  global.GLTFLoader = function () {
    this.load = function (url, ok) { glbCalls++; try { ok({ scene: new THREE.Group() }); } catch (e) {} };
  };

  const container = new El('div');
  global.__DTT__ = {
    container, scene: new THREE.Scene(), camera: new THREE.PerspectiveCamera(40, 1.5, 0.1, 200),
    controls: { enabled: true, update() {}, target: new THREE.Vector3(), autoRotate: false },
    renderer: { render() {} }, labelRenderer: { render() {}, domElement: new El('div') }, THREE,
    getObjectMeshes: () => [], getSelectedId: () => '',
    isPonderActive: false, setPonderActive(o) { global.__DTT__.isPonderActive = !!o; }
  };
  container.appendChild = c => { c.parentNode = container; container.children.push(c); return c; };

  global.__PONDER_JSON__ = JSON.stringify(payload);
  (0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ponder.js'), 'utf8'));
  const api = global.__DTT_PONDER__;
  api.boot();

  let allPass = true;
  log('课件'.padEnd(4) + ' | 节点 | 分镜 | 动作 | 对照 | 相机 | 首帧可见 | 网格');
  log('-'.repeat(78));

  for (const lid of Object.keys(payload.lessons)) {
    const lesson = payload.lessons[lid];
    const st = lesson.stage || {};
    const isDevice = !!st.device;
    const expectNodes = isDevice
      ? ((payload.devices[st.device] || {}).parts || []).length
      : (st.nodes || []).length;
    const totalActions = lesson.steps.reduce((a, s) => a + s.actions.length, 0);

    api.open(lid);
    // 先推进若干帧，让第 1 步的 stage 动作生效（抽象装置课靠它点亮节点）
    for (let f = 0; f < 5; f++) {
      virtualNow += 16;
      const q = rafQueue; rafQueue = [];
      for (const fn of q) { try { fn(virtualNow); } catch (e) {} }
    }
    const probe0 = api.panelProbe();
    const overlay = container.children.filter(c => c.id === 'ponder-overlay').pop();
    const wrap = overlay ? overlay.querySelector('.pd-panelwrap') : null;
    const cells = wrap ? wrap.querySelectorAll('.pd-panel') : [];
    const axisNodes = overlay ? overlay.querySelectorAll('.pd-axis-node') : [];

    const per = {}; const errors = [];
    const LAST = lesson.steps.length - 1;
    let settle = 0, frames = 0;

    for (let f = 0; f < 30000; f++) {
      virtualNow += 16;
      const q = rafQueue; rafQueue = [];
      for (const fn of q) {
        try { fn(virtualNow); } catch (e) { if (errors.length < 6) errors.push('f' + f + ': ' + (e && e.message)); }
      }
      frames++;
      const dd = api.diagnostics();
      if (!dd.active) break;
      const k = dd.step + 1;
      const want = lesson.steps[dd.step].actions.length;
      const s = per[k] = per[k] || { fired: 0, want, cmp: 0, dist: 0, fit: 0 };
      s.fired = Math.max(s.fired, dd.firedActions || 0);
      if (s.fired >= want) {
        if (typeof dd.camDist === 'number') s.dist = dd.camDist;
        s.fit = dd.fitNeed || s.fit;
      }
      if (overlay) {
        const cmpNow = overlay.querySelector('.pd-compare');
        if (cmpNow && String(cmpNow.style.display) === 'block') s.cmp++;
      }
      if (dd.step === LAST && !dd.playing && dd.stepPos >= dd.stepDuration) {
        settle++; if (settle > 80) break;
      }
      if (f === 29999) errors.push('帧上限');
    }

    let tot = 0, maxRatio = 0, cmpCount = 0;
    Object.keys(per).forEach(k => {
      const s = per[k];
      tot += s.fired;
      const r = s.fit > 0 ? s.dist / s.fit : 1;
      maxRatio = Math.max(maxRatio, r);
      if (s.cmp > 0) cmpCount++;
    });
    const relWarn = WARN.filter(w => /flow 端点不存在|未知原语|打开课件失败|move 目标不存在|未找到装置定义|GLTFLoader 未桥接/.test(w));
    const ok = errors.length === 0 && tot === totalActions &&
      maxRatio < 1.6 && cmpCount === lesson.steps.length &&
      relWarn.length === 0 && cells.length === lesson.steps.length &&
      axisNodes.length === lesson.steps.length && probe0.totalNodes === expectNodes &&
      probe0.visibleNodes > 0;

    if (!ok) allPass = false;
    log((ok ? '✅ ' : '❌ ') + lesson.title.slice(0, 14).padEnd(15) +
      ' | ' + String(probe0.totalNodes).padStart(2) + '/' + String(expectNodes).padEnd(2) +
      ' | ' + String(cells.length).padStart(2) + '/' + String(lesson.steps.length).padEnd(2) +
      ' | ' + String(tot).padStart(3) + '/' + String(totalActions).padEnd(3) +
      ' | ' + String(cmpCount).padStart(2) + '/' + String(lesson.steps.length).padEnd(2) +
      ' | ' + maxRatio.toFixed(2) +
      ' | ' + String(probe0.visibleNodes).padStart(3) +
      ' | ' + JSON.stringify(probe0.gridSpan));
    if (errors.length) log('     !! 异常: ' + errors.join(' | '));
    if (relWarn.length) log('     !! 警告: ' + relWarn.join(' | '));
    api.close();
  }

  // 交互兜底：拖拽 + 滚轮必须能改变相机
  api.open('hierarchy');
  for (let f = 0; f < 5; f++) { virtualNow += 16; const q = rafQueue; rafQueue = []; for (const fn of q) { try { fn(virtualNow); } catch (e) {} } }
  let canvas = null;
  (function find(n) { if (n.tagName === 'CANVAS') canvas = canvas || n; n.children.forEach(find); })(container);
  let dragOk = false, wheelOk = false;
  if (canvas && canvas._listeners.pointerdown) {
    // ⚠️ 旋转不改变 camDist（球面半径不变）——必须比较相机坐标
    const p0 = api.panelProbe().camPos.join(',');
    const d0 = api.panelProbe().camDist;
    canvas._listeners.pointerdown.forEach(f => f({ clientX: 100, clientY: 100, pointerId: 1 }));
    canvas._listeners.pointermove.forEach(f => f({ clientX: 240, clientY: 150 }));
    canvas._listeners.pointerup.forEach(f => f({}));
    const p1 = api.panelProbe().camPos.join(',');
    dragOk = (p0 !== p1);
    // 滚轮改变的是距离
    (canvas._listeners.wheel || []).forEach(f => f({ deltaY: 300, preventDefault() {} }));
    wheelOk = api.panelProbe().camDist !== d0;
  }
  api.close();

  log('-'.repeat(78));
  log('OrbitControls 构造 ' + orbitCount + ' 次 | GLB 装配尝试 ' + glbCalls + ' 次 | 渲染 ' + renderCount + ' 次');
  log('拖拽旋转: ' + (dragOk ? '✅ 生效' : '❌ 无效') + ' | 滚轮缩放: ' + (wheelOk ? '✅ 生效' : '❌ 无效'));
  log('退出残留覆盖层: ' + container.children.filter(c => c.id === 'ponder-overlay').length);
  log(allPass && dragOk && wheelOk ? '\n✅ 全部通过' : '\n❌ 存在未通过项');

  realLog(OUT.join('\n'));
  process.exit(allPass && dragOk && wheelOk ? 0 : 1);
})().catch(e => { log('FATAL: ' + (e && e.stack || e)); realLog(OUT.join('\n')); process.exit(1); });
