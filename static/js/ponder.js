/* ============================================================================
 * static/js/ponder.js
 *
 * 「思索」教学动画运行时（Ponder-style interactive tutorial）
 *
 * 设计约束（务必先读，否则很容易改坏）：
 *   1) 主场景由 app.py 的 generate_scene_html() 用 f-string 拼出，每次 rerun
 *      都会重写 srcdoc → iframe 整块重建、脚本重跑。所以：
 *        · Python 只负责下发"课件数据"，绝不参与播放驱动；
 *        · 播放进度、已完成标记一律存 localStorage，与相机状态同等对待。
 *   2) 本文件是外部静态脚本（/app/static/js/ponder.js），不塞进那个
 *      6000 行 f-string —— 避免 {{ }} 转义地狱。
 *   3) 沙盒使用「简化示意几何体」而非真实 GLB：教学示意越抽象越清晰，
 *      且免去模型加载开销，并支持自由拆解 / 复位。
 *   4) 失败必须软着陆：任何异常都只打印警告，绝不阻断主场景渲染。
 *
 * 对外接口：
 *   window.__DTT_PONDER__.boot()        由 HTML 末尾的启动脚本调用
 *   window.__DTT_PONDER__.open(id)      打开某课件（入口按钮调用）
 *   window.__DTT_PONDER__.close()       关闭
 *   window.__DTT_PONDER__.isActive()    主场景 animate() 用它降频
 * ========================================================================== */
(function () {
  'use strict';

  var NS = 'ponder';
  var TAG = '%c[思索]';
  var CSS_TAG = 'color:#c9a227;font-weight:700';

  function log() {
    var a = Array.prototype.slice.call(arguments);
    // 排障用：把日志同时存进全局数组，便于在无法读控制台的环境里取证
    if (window.__PONDER_TRACE__) {
      try {
        (window.__PONDER_TRACES__ = window.__PONDER_TRACES__ || []).push(a.join(' '));
        if (window.__PONDER_TRACES__.length > 500) window.__PONDER_TRACES__.shift();
      } catch (e) { /* 忽略 */ }
    }
    console.log.apply(console, [TAG, CSS_TAG].concat(a));
  }
  function warn() {
    var a = Array.prototype.slice.call(arguments);
    console.warn.apply(console, [TAG, CSS_TAG].concat(a));
  }

  // ==========================================================
  // 0. 全局状态
  // ==========================================================
  var S = {
    booted: false,
    data: null,          // 数据岛解析结果
    lesson: null,        // 当前课件
    lessonId: '',
    stepIndex: 0,
    stepPos: 0,          // 当前步内已播放毫秒
    playing: false,
    active: false,       // 覆盖层是否打开
    speed: 1.0,
    autoplayNext: true,
    flows: [],           // 活跃的流动特效
    tweens: {},          // key -> {from,to,start,ms,unit,val}
    lastTs: 0,
    _saved: null,        // 进入前的相机 / 控件现场
    _raf: null,
    _timer: null,
    labels: {}           // nodeId -> DOM 元素
  };

  var lab = null;        // 沙盒 3D 环境
  var ui = null;         // 覆盖层 UI
  var prim = null;       // 原语执行器

  var PALETTE = {
    data:   { color: 0x4aa3ff, label: '数据流' },
    logic:  { color: 0xc98b2e, label: '逻辑' },
    render: { color: 0x51cf66, label: '渲染' },
    power:  { color: 0xffaa00, label: '能量流' },
    carbon: { color: 0x3fa88a, label: '碳流' }
  };

  // ==========================================================
  // 1. 数据岛读取
  // ==========================================================
  function readData() {
    var el = document.getElementById('ponder-data');
    if (!el) { warn('未找到 #ponder-data 数据岛'); return null; }
    try {
      var raw = el.textContent || el.innerText || '';
      if (!raw.trim()) { warn('数据岛为空'); return null; }
      return JSON.parse(raw);
    } catch (e) {
      warn('数据岛解析失败', e);
      return null;
    }
  }

  function dtt() { return window.__DTT__ || null; }

  // ==========================================================
  // 2. 沙盒 3D 环境
  // ==========================================================
  function createNodeMesh(spec) {
    var size = spec.size || [1, 1, 1];
    var color = new THREE.Color(spec.color || '#888888');
    var mat = new THREE.MeshStandardMaterial({
      color: color,
      roughness: 0.55,
      metalness: 0.25,
      emissive: color.clone().multiplyScalar(0.12),
      emissiveIntensity: 1.0,
      transparent: true,
      opacity: 1.0
    });
    var g, mesh;
    switch (spec.shape) {
      case 'slab':
        g = new THREE.BoxGeometry(size[0], size[1], size[2]);
        break;
      case 'cylinder':
        g = new THREE.CylinderGeometry(size[0] / 2, size[0] / 2, size[1], 24);
        break;
      case 'octa':
        g = new THREE.OctahedronGeometry(size[0] / 2, 0);
        break;
      case 'cone':
        g = new THREE.ConeGeometry(size[0] / 1.6, size[1], 20);
        break;
      case 'screen':
        g = new THREE.BoxGeometry(size[0], size[1], size[2]);
        break;
      case 'pillar':
        // 立式充电桩形态：落地底座 + 桩身 + 顶部提示灯带 + 侧面枪座。
        // 之前只是一块矩形，远看和"柜子"没有区别，装置课里辨识度太低。
        g = new THREE.Group();
        var pw = size[0], ph = size[1], pd = size[2] || pw;
        var base = new THREE.Mesh(new THREE.BoxGeometry(pw * 1.35, ph * 0.05, pd * 1.35), mat);
        base.position.y = ph * 0.025;
        var stem = new THREE.Mesh(new THREE.CylinderGeometry(pw * 0.34, pw * 0.4, ph * 0.24, 14), mat);
        stem.position.y = ph * 0.17;
        var body = new THREE.Mesh(new THREE.BoxGeometry(pw, ph * 0.6, pd), mat);
        body.position.y = ph * 0.59;
        var cap = new THREE.Mesh(new THREE.BoxGeometry(pw * 0.82, ph * 0.09, pd * 0.82), mat);
        cap.position.y = ph * 0.935;
        var holster = new THREE.Mesh(new THREE.BoxGeometry(pw * 0.3, ph * 0.22, pd * 0.28), mat);
        holster.position.set(pw * 0.62, ph * 0.52, 0);
        g.add(base); g.add(stem); g.add(body); g.add(cap); g.add(holster);
        g.userData.sharedMaterial = mat;
        return g;
      case 'skyline': {
        // ⚠️ 复合形状返回 Group，Group 没有 .material。
        //    为了让上层「一个节点 = 一个可着色物」的假设恒成立，
        //    这里显式把共享材质挂到 Group.userData.sharedMaterial，
        //    上层取材质时统一走 nodeMaterial()。
        g = new THREE.Group();
        var b1 = new THREE.Mesh(new THREE.BoxGeometry(size[0] * 0.6, size[1], size[2] * 0.6), mat);
        b1.position.y = size[1] / 2;
        var b2 = new THREE.Mesh(new THREE.BoxGeometry(size[0] * 0.45, size[1] * 0.7, size[2] * 0.45), mat);
        b2.position.set(size[0] * 0.45, size[1] * 0.35, size[2] * 0.3);
        g.add(b1); g.add(b2);
        g.userData.sharedMaterial = mat;
        return g;
      }
      case 'cloud': {
        g = new THREE.Group();
        var base = new THREE.Mesh(new THREE.BoxGeometry(size[0], size[1] * 0.5, size[2]), mat);
        base.position.y = 0.3;
        var top = new THREE.Mesh(new THREE.BoxGeometry(size[0] * 0.6, size[1] * 0.5, size[2] * 0.7), mat);
        top.position.y = size[1] * 0.6;
        g.add(base); g.add(top);
        g.userData.sharedMaterial = mat;
        return g;
      }
      case 'car': {
        g = new THREE.Group();
        var body = new THREE.Mesh(new THREE.BoxGeometry(size[0], size[1] * 0.7, size[2]), mat);
        body.position.y = size[1] * 0.45;
        var cab = new THREE.Mesh(new THREE.BoxGeometry(size[0] * 0.5, size[1] * 0.6, size[2] * 0.85), mat);
        cab.position.set(-size[0] * 0.1, size[1] * 0.95, 0);
        g.add(body); g.add(cab);
        g.userData.sharedMaterial = mat;
        return g;
      }
      default:
        g = new THREE.BoxGeometry(size[0], size[1], size[2]);
    }
    mesh = new THREE.Mesh(g, mat);
    mesh.position.y = size[1] / 2;
    return mesh;
  }

  // 兼容 Mesh 与 Group 两种节点形态，统一取共享材质
  function nodeMaterial(obj) {
    if (!obj) return null;
    if (obj.material) return Array.isArray(obj.material) ? obj.material[0] : obj.material;
    return (obj.userData && obj.userData.sharedMaterial) || null;
  }

  // 装置零件 → 节点定义。
  // 智能装置（device）与抽象装置（nodes）产出同一种节点结构，因此
  // focus / flow / label / tween / move 这些原语对两者完全通用。
  function deviceToNodes(deviceId) {
    var dev = (S.data.devices || {})[deviceId];
    if (!dev) { warn('未找到装置定义:', deviceId); return []; }
    var assets = S.data.deviceAssets || {};
    return (dev.parts || []).map(function (pt) {
      var a = assets[pt.asset] || {};
      return {
        id: pt.id,
        // 抽象零件走示意体；GLB 零件先用示意体占位，模型到了再替换
        type: a.kind === 'prim' ? (a.prim || 'chip') : 'chip',
        pos: pt.pos || [0, 0, 0],
        label: a.label || pt.id,
        brief: pt.brief || '',
        _asset: pt.asset,
        _assetSpec: a,
        _assembled: (pt.pos || [0, 0, 0]).slice(),
        _taken: (pt.take || pt.pos || [0, 0, 0]).slice()
      };
    });
  }

  // 异步把真实 GLB 换到节点上。
  // 失败必须软着陆：加载不到就保留示意体占位，绝不能整节课黑掉。
  function attachGlbToNode(node, assetSpec) {
    if (!assetSpec || assetSpec.kind !== 'glb') return;
    var Loader = window.GLTFLoader || THREE.GLTFLoader;
    if (typeof Loader !== 'function') {
      warn('GLTFLoader 未桥接到 window，装置保持示意体：', node.id);
      return;
    }
    try {
      var loader = new Loader();
      loader.load(assetSpec.url, function (gltf) {
        try {
          var obj = gltf.scene || (gltf.scenes && gltf.scenes[0]);
          // ⚠️ 这里只判 node.group 是否存在，**不能**判 node.group.parent：
          //    加载回调可能在节点刚建好、还没挂进场景时就同步返回（缓存命中或本地文件），
          //    那时 parent 还是 null，模型会被静默丢弃。
          if (!obj || !node.group) return;
          // 归一化到合适大小：GLB 尺寸差异很大（树是 300+ 单位）
          var box = new THREE.Box3().setFromObject(obj);
          var size = new THREE.Vector3();
          box.getSize(size);
          var maxDim = Math.max(size.x, size.y, size.z) || 1;
          var target = assetSpec.targetSize || 2.2;
          var k = target / maxDim;
          obj.scale.multiplyScalar(k);
          // 重新贴地：模型原点各不相同，按包围盒底部对齐
          var box2 = new THREE.Box3().setFromObject(obj);
          obj.position.y -= box2.min.y;
          obj.traverse(function (c) {
            if (c.isMesh) { c.castShadow = true; c.receiveShadow = true; }
          });
          // 替换占位示意体（保留高亮光环）。
          // 用「从 parent 移除」而不是依赖 group.parent 存在，兼容两者。
          if (node.mesh && node.mesh.parent) node.mesh.parent.remove(node.mesh);
          node.group.add(obj);
          node.mesh = obj;
          node.mat = null;           // GLB 原材质不参与 tween 染色
          node.isGlb = true;
        } catch (e) { warn('装配 GLB 失败:', node.id, e); }
      }, undefined, function (err) {
        warn('加载 GLB 失败（保持示意体）:', assetSpec.url, err && err.message);
      });
    } catch (e) {
      warn('创建 GLTFLoader 失败:', e);
    }
  }

  function ensureNodes() {
    var L = lab;
    var stage = S.lesson.stage || {};
    // 有 device 就用装置零件，否则用显式的 nodes
    var nodes = stage.device ? deviceToNodes(stage.device) : (stage.nodes || []);
    var specMap = S.data.primSpec || {};

    // 清掉旧的
    Object.keys(L.nodes).forEach(function (k) {
      var n = L.nodes[k];
      if (n.group && n.group.parent) n.group.parent.remove(n.group);
      if (n.labelEl && n.labelEl.parentNode) n.labelEl.parentNode.removeChild(n.labelEl);
    });
    L.nodes = {};
    lab.labelLayer.innerHTML = '';

    nodes.forEach(function (def) {
      var spec = specMap[def.type] || { shape: 'box', size: [1, 1, 1], color: '#888888' };
      var group = new THREE.Group();
      group.position.set(def.pos[0], def.pos[1], def.pos[2]);
      var mesh = createNodeMesh(spec);
      // 装置零件用它自己的 scale（内部模块比外壳小得多）
      var asc = def._assetSpec || {};
      var k = asc.scale || 1;
      if (k !== 1) mesh.scale.multiplyScalar(k);
      mesh.traverse(function (c) { if (c.isMesh) { c.castShadow = true; c.receiveShadow = true; } });
      group.add(mesh);

      // 底座光环，用于高亮
      var ring = new THREE.Mesh(
        new THREE.RingGeometry(0.85, 1.05, 40),
        new THREE.MeshBasicMaterial({
          color: new THREE.Color(spec.color || '#4aa3ff'),
          transparent: true, opacity: 0.0, side: THREE.DoubleSide
        })
      );
      ring.rotation.x = -Math.PI / 2;
      ring.position.y = 0.02;
      group.add(ring);

      L.root.add(group);

      // DOM 标签（手写投影，不依赖 CSS2DRenderer）
      var el = document.createElement('div');
      el.className = 'pd-node-label';
      el.innerHTML = '<span class="pd-node-dot"></span>' + escapeHtml(def.label || def.id);
      lab.labelLayer.appendChild(el);

      L.nodes[def.id] = {
        id: def.id,
        def: def,
        spec: spec,
        group: group,
        mesh: mesh,
        mat: nodeMaterial(mesh),
        ring: ring,
        baseColor: new THREE.Color(spec.color || '#4aa3ff'),
        labelEl: el,
        visible: true,
        phase: Math.random() * Math.PI * 2,
        // 拆装用：装配位置 / 拆开位置（装置零件才有）
        assembled: def._assembled || [group.position.x, group.position.y, group.position.z],
        taken: def._taken || [group.position.x, group.position.y, group.position.z],
        // isPart=true 表示"装置零件"：默认可见（装置课没有 stage 动作来点亮它）
        isPart: !!def._assembled,
        anim: null
      };
      // 真实模型异步替换占位示意体
      if (def._assetSpec) attachGlbToNode(L.nodes[def.id], def._assetSpec);
    });
    log('教学装置就绪：' + Object.keys(L.nodes).length + ' 个节点' +
      (stage.device ? '（智能装置：' + ((S.data.devices || {})[stage.device] || {}).name + '）' : ''));
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  // ==========================================================
  // 3. 原语执行器
  // ==========================================================
  function makePrim() {
    var P = {};

    // ---- stage：按顺序应用 show/hide，支持叠加 ----
    P.stage = function (act) {
      var show = act.show || [], hide = act.hide || [];
      show.forEach(function (id) { if (lab.nodes[id]) lab.nodes[id].visible = true; });
      hide.forEach(function (id) { if (lab.nodes[id]) lab.nodes[id].visible = false; });
      refreshVisibility();
      // 进出场后可见范围变了，重新适配相机（否则新增的节点可能落在画面外）
      refitCamera(true);
    };

    // ---- focus：聚光高亮 + 压暗其余 ----
    P.focus = function (act) {
      var targets = act.target || [];
      var dim = (act.dim === undefined) ? 0.25 : act.dim;
      Object.keys(lab.nodes).forEach(function (id) {
        var n = lab.nodes[id];
        var on = targets.indexOf(id) >= 0;
        n.focused = on;
        n.dim = on ? 1.0 : dim;
        n.ring.material.opacity = on ? 0.55 : 0.0;
        n.labelEl.classList.toggle('is-dim', !on);
        n.labelEl.classList.toggle('is-focus', on);
      });
    };

    // ---- flow：沿两点之间做一条弧线，跑粒子 ----
    P.flow = function (act) {
      var a = lab.nodes[act.from], b = lab.nodes[act.to];
      if (!a || !b) { warn('flow 端点不存在:', act.from, act.to); return; }

      // 🔥 同一对端点重复触发时先回收旧特效。
      //    否则 final step 里 6 条边会被反复叠加，粒子/管道/标签无限增长。
      //    ⚠️ 必须原地 splice，不能赋回新数组：S.flows 与 panel.flows 是
      //       同一个数组对象的引用，一旦换成新数组两边就断开了。
      for (var fi = S.flows.length - 1; fi >= 0; fi--) {
        var f0 = S.flows[fi];
        if (f0.from === act.from && f0.to === act.to) {
          disposeFlow(f0);
          S.flows.splice(fi, 1);
        }
      }

      var kind = PALETTE[act.kind] || PALETTE.data;
      var color = new THREE.Color(kind.color);
      var p0 = a.group.position.clone(); p0.y = 1.0;
      var p1 = b.group.position.clone(); p1.y = 1.0;

      // 弧线（中点抬高，形成拱形）
      var mid = p0.clone().lerp(p1, 0.5);
      mid.y += Math.max(0.8, p0.distanceTo(p1) * 0.18);
      var curve = new THREE.QuadraticBezierCurve3(p0, mid, p1);

      var tube = new THREE.Mesh(
        new THREE.TubeGeometry(curve, 40, 0.035, 8, false),
        new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: 0.28 })
      );
      lab.root.add(tube);

      // 粒子
      var count = 7;
      var mat = new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: 0.95 });
      var geo = new THREE.SphereGeometry(0.11, 10, 10);
      var group = new THREE.Group();
      for (var i = 0; i < count; i++) {
        var dot = new THREE.Mesh(geo, mat);
        dot.userData.offset = i / count;
        group.add(dot);
      }
      lab.root.add(group);

      // 流动标签
      var tagEl = null;
      if (act.label) {
        tagEl = document.createElement('div');
        tagEl.className = 'pd-flow-label';
        tagEl.textContent = act.label;
        tagEl.style.borderColor = '#' + color.getHexString();
        tagEl.style.color = '#' + color.getHexString();
        lab.labelLayer.appendChild(tagEl);
      }

      S.flows.push({
        from: act.from,
        to: act.to,
        curve: curve,
        group: group,
        tube: tube,
        tagEl: tagEl,
        rate: act.rate || 1.0,
        reverse: !!act.reverse,
        phase: 0,
        color: color
      });
    };

    // ---- tween：数值过渡，驱动节点外观 ----
    P.tween = function (act, animated) {
      var key = act.key || 'util';
      S.tweens[key] = {
        from: (act.from === undefined) ? 0 : act.from,
        to: (act.to === undefined) ? 1 : act.to,
        start: S.stepPos,
        ms: Math.max(1, act.ms || 1000),
        unit: act.unit || '',
        // 可选：染色目标节点与是否染色（缺省向后兼容旧课件）
        nodes: act.nodes || null,
        colorize: (act.colorize === undefined) ? true : !!act.colorize,
        val: (animated ? (act.from === undefined ? 0 : act.from) : (act.to === undefined ? 1 : act.to)),
        animated: animated
      };
      applyTween(key, S.tweens[key].val);
    };

    // ---- label：节点旁追加标注 ----
    P.label = function (act) {
      var n = lab.nodes[act.node];
      if (!n) return;
      var el = document.createElement('div');
      el.className = 'pd-callout pd-tone-' + (act.tone || 'info');
      el.textContent = act.text;
      lab.labelLayer.appendChild(el);
      n.callouts = n.callouts || [];
      n.callouts.push(el);
    };

    // ---- compare：左右对照面板 ----
    P.compare = function (act) {
      ui.showCompare(act);
    };

    // ---- branch：反事实提示 ----
    P.branch = function (act) {
      ui.showBranch(act);
    };

    // ---- move：把零件平滑移到某处（拆解 / 组装的基本动作） ----
    // act: {target: nodeId 或 [ids], to: [x,y,z] | 'taken' | 'assembled',
    //       rot: [x,y,z] 可选, ms, delay, instant}
    P.move = function (act, animated) {
      var ids = act.target ? (Array.isArray(act.target) ? act.target : [act.target]) : [];
      var ms = act.ms || 700;
      var delay = act.delay || 0;
      ids.forEach(function (id) {
        var n = lab.nodes[id];
        if (!n) { warn('move 目标不存在:', id); return; }
        var to = act.to;
        if (to === 'taken') to = n.taken;
        else if (to === 'assembled') to = n.assembled;
        if (!to) { warn('move 缺少 to:', id); return; }
        var from = [n.group.position.x, n.group.position.y, n.group.position.z];
        var fromRot = [n.group.rotation.x, n.group.rotation.y, n.group.rotation.z];
          var toRot = act.rot || fromRot;
        if (!animated || act.instant) {
          n.group.position.set(to[0], to[1], to[2]);
          n.group.rotation.set(toRot[0], toRot[1], toRot[2]);
          n.anim = null;
          return;
        }
        n.anim = {
          t0: performance.now(), ms: ms, delay: delay,
          p0: from, p1: to, r0: fromRot, r1: toRot
        };
      });
      refitCamera(true);
    };
    // ---- take_apart：沿装配树把零件依次拆开（stagger 制造"逐个松脱"的观感）----
    P.take_apart = function (act) {
      var ids = act.target ? (Array.isArray(act.target) ? act.target : [act.target])
                           : Object.keys(lab.nodes);
      var ms = act.ms || 620;
      var stagger = (act.stagger === undefined) ? 160 : act.stagger;
      // 拆解顺序：外壳/大件先走，内部模块后走 —— 与真实拆机一致
      var order = ids.slice().sort(function (a, b) {
        var na = lab.nodes[a], nb = lab.nodes[b];
        if (!na || !nb) return 0;
        return nb.group.position.y - na.group.position.y === 0 ? 0
             : (na.group.position.y - nb.group.position.y);
      });
      order.forEach(function (id, i) {
        P.move({ target: id, to: 'taken', ms: ms, delay: i * stagger }, true);
      });
    };

    // ---- assemble：装回整体 ----
    P.assemble = function (act) {
      var ids = act.target ? (Array.isArray(act.target) ? act.target : [act.target])
                           : Object.keys(lab.nodes);
      var ms = act.ms || 620;
      var stagger = (act.stagger === undefined) ? 130 : act.stagger;
      // 组装顺序与拆解相反：内部先就位，外壳最后合上
      var order = ids.slice().sort(function (a, b) {
        var na = lab.nodes[a], nb = lab.nodes[b];
        if (!na || !nb) return 0;
        return na.group.position.y - nb.group.position.y;
      }).reverse();
      order.forEach(function (id, i) {
        P.move({ target: id, to: 'assembled', ms: ms, delay: i * stagger }, true);
      });
    };

    // ---- parts_tween：把一批零件一起移到各自的位置（可选逆序回装）----
    P.parts_tween = function (act, animated) {
      var ids = act.target ? (Array.isArray(act.target) ? act.target : [act.target])
                           : Object.keys(lab.nodes);
      var to = act.to === 'taken' ? 'taken' : 'assembled';
      var ms = act.ms || 700;
      var stagger = act.stagger || 0;
      ids.forEach(function (id, i) {
        P.move({ target: id, to: to, ms: ms, delay: i * stagger }, animated);
      });
    };

    // ---- camera：缓动运镜 ----
    // 🔥 视口适配（关键）：
    //   课件里的 eye 只作为「方向 + 仰角」的意图，真实距离由 fitCameraDistance()
    //   按当前视口宽高比算出来。否则同一个绝对坐标在窄 iframe 里会把
    //   装置两端挤出画面（实测 535x600 时第 5 章的 sensor 和 view 都不见了）。
    P.camera = function (act, instant) {
      var L = lab;
      var eye = act.eye || [0, 9, 22];
      var look = act.look || [0, 1, 0];
      var lookV = new THREE.Vector3(look[0], look[1], look[2]);

      // 用作者的 eye 推出方向与仰角（保留运镜意图）
      var authored = new THREE.Vector3(eye[0], eye[1], eye[2]).sub(lookV);
      if (authored.lengthSq() < 1e-6) authored.set(0, 0.5, 1);
      var authoredLen = authored.length();
      var dir = authored.clone().normalize();

      // 按当前可见节点算出自适应距离
      var need = fitCameraDistance(lookV, dir, L.camera.aspect);

      // 取二者较大值：既尊重作者的远近意图，也保证画面装得下
      var dist = Math.max(authoredLen, need);

      // ⚠️ 必须每次 clone：Vector3 的 multiplyScalar 是原地修改。
      //    之前写成 dir.multiplyScalar(dist) 后再 dir.clone() 存进 _camFit，
      //    存进去的其实是「已乘过距离」的向量（长度=dist），
      //    refitCamera 再乘一次 need → 距离被平方级放大（实测涨到 523）。
      L._camFit = { look: lookV.clone(), dir: dir.clone(), need: dist };

      var p1 = lookV.clone().add(dir.clone().multiplyScalar(dist));

      if (instant) {
        L.camera.position.copy(p1);
        if (L.controls) { L.controls.target.copy(lookV); L.controls.update(); }
        return;
      }
      L._camAnim = {
        t0: performance.now(),
        ms: act.ms || 900,
        p0: L.camera.position.clone(),
        p1: p1,
        l0: L.controls ? L.controls.target.clone() : new THREE.Vector3(0, 1, 0),
        l1: lookV.clone()
      };
    };

    // ---- wait：空拍 ----
    P.wait = function () { /* 什么都不做，靠 at 间隔表达停顿 */ };

    return P;
  }

  // 计算「把当前可见节点全部装进画面」所需的相机距离
  // —— 这是窄 iframe 下不被裁切的关键：作者只给方向，距离按视口算。
  function fitCameraDistance(lookV, dir, aspect) {
    var pts = [];
    Object.keys(lab.nodes).forEach(function (id) {
      var n = lab.nodes[id];
      if (!n.visible) return;
      var h = nodeLabelHeight(n) + 0.75;   // 标签在节点上方，一并纳入包围盒
      var p = n.group.position;
      pts.push(new THREE.Vector3(p.x, p.y, p.z));
      pts.push(new THREE.Vector3(p.x, p.y + h, p.z));
    });
    if (pts.length < 2) return 12;

    // 相机空间基（right / up / forward）
    var forward = dir.clone().normalize();
    var worldUp = new THREE.Vector3(0, 1, 0);
    var right = new THREE.Vector3().crossVectors(forward, worldUp);
    if (right.lengthSq() < 1e-6) right.set(1, 0, 0);
    right.normalize();
    var up = new THREE.Vector3().crossVectors(right, forward).normalize();

    var vFovRad = lab.camera.fov * Math.PI / 180;
    var tanV = Math.tan(vFovRad / 2);
    var tanH = tanV * Math.max(0.35, aspect || 1);

    var need = 0;
    for (var i = 0; i < pts.length; i++) {
      var v = pts[i].clone().sub(lookV);
      var depth = v.dot(forward);                                  // 正数=在镜头前方
      var distH = Math.abs(v.dot(right)) / tanH - depth;
      var distV = Math.abs(v.dot(up)) / tanV - depth;
      if (distH > need) need = distH;
      if (distV > need) need = distV;
    }
    return Math.max(6, need * 1.22 + 1.8);   // 1.22 留标签边距，1.8 防贴脸
  }

  // 地面与网格跟随内容延伸。
  // ------------------------------------------------------------
  // ⚠️ 关键：GridHelper(1,1) 直接缩放会**同时**放大格子间距，
  //    导致格子巨大、看不出尺度参考。所以改成按"固定的世界空间格距"
  //    重建网格（divisions = 跨度 / 格距），并把材质设成世界坐标平铺，
  //    这样缩放后格子间距仍然恒定、纹理也不被拉伸。
  var GRID_CELL = 2.0;      // 每格的世界尺寸
  var GRID_MIN = 160;       // 最小铺开尺寸：要明显大于常见机位距离，否则地面"一眼望到头"
  var GRID_MAX = 520;       // 上限，避免极远时线太多

  function adaptGround(p) {
    var minX = 0, maxX = 0, minZ = 0, maxZ = 0, any = false;
    Object.keys(p.nodes).forEach(function (id) {
      var n = p.nodes[id];
      if (!n.visible || !n.group) return;
      var q = n.group.position;
      if (!any) { minX = maxX = q.x; minZ = maxZ = q.z; any = true; return; }
      minX = Math.min(minX, q.x); maxX = Math.max(maxX, q.x);
      minZ = Math.min(minZ, q.z); maxZ = Math.max(maxZ, q.z);
    });
    if (!any) return;

    // 留 2.6 倍边距：拆解态零件散得开，地面要跟得上（"延伸到视野尽头"）
    var spanX = Math.max(GRID_MIN, (maxX - minX) * 2.6);
    var spanZ = Math.max(GRID_MIN, (maxZ - minZ) * 2.6);
    spanX = Math.min(GRID_MAX, spanX);
    spanZ = Math.min(GRID_MAX, spanZ);
    // 取整到格距的整数倍，保证格线落在整格上
    spanX = Math.max(GRID_CELL, Math.round(spanX / GRID_CELL) * GRID_CELL);
    spanZ = Math.max(GRID_CELL, Math.round(spanZ / GRID_CELL) * GRID_CELL);
    var cx = (minX + maxX) / 2, cz = (minZ + maxZ) / 2;

    var oldGrid = p.scene.getObjectByName('pd-grid');
    var oldFloor = p.scene.getObjectByName('pd-floor');

    if (!oldGrid || oldGrid._spanX !== spanX || oldGrid._spanZ !== spanZ) {
      if (oldGrid) {
        p.scene.remove(oldGrid);
        if (oldGrid.geometry) oldGrid.geometry.dispose();
        if (oldGrid.material) oldGrid.material.dispose();
      }
      var divisions = Math.max(4, Math.round(Math.max(spanX, spanZ) / GRID_CELL));
      var grid = new THREE.GridHelper(Math.max(spanX, spanZ), divisions, 0x2a4a6a, 0x16283c);
      grid.name = 'pd-grid';
      // 网格线用半透明：太实会压过内容，太小又看不见
      grid.material.transparent = true;
      grid.material.opacity = 0.6;
      grid._spanX = spanX; grid._spanZ = spanZ;
      grid.userData.span = [spanX, spanZ];
      grid.userData.cell = GRID_CELL;
      // 非等比缩放：X/Z 各自铺开，但格子间距仍由 divisions 决定
      var maxSpan = Math.max(spanX, spanZ);
      grid.scale.set(spanX / maxSpan, 1, spanZ / maxSpan);
      p.scene.add(grid);
    }
    var g = p.scene.getObjectByName('pd-grid');
    if (g) g.position.set(cx, 0, cz);

    if (oldFloor) {
      // 地面略微超出网格，避免边缘露底
      oldFloor.scale.set(spanX * 1.6, spanZ * 1.6, 1);
      oldFloor.position.set(cx, -0.02, cz);
    }
  }

  // 当前视口下重新适配一次相机（不改机位方向，只改距离）
  // 触发时机：节点进出场后、数值过渡完成后、窗口尺寸变化时。
  // 用户手动拖拽过之后不再自动改动，把控制权交回用户。
  function refitCamera(animate) {
    if (!lab || !lab._camFit || lab._userOrbited) return;
    var L = lab;
    var need = fitCameraDistance(L._camFit.look, L._camFit.dir, L.camera.aspect);
    if (Math.abs(need - (L._camFit.need || 0)) < 0.35) return;   // 变化不大就不动，避免抖动
    // 可见范围变了，地面与网格也要跟着铺开（否则物体会站在地面外）
    adaptGround(L);
    // dir 必须是单位向量：长度偏离 1 说明有地方原地改了它，直接拒绝执行更安全
    if (Math.abs(L._camFit.dir.length() - 1) > 0.01) {
      warn('相机方向向量非单位长度（' + L._camFit.dir.length().toFixed(2) + '），已跳过自动适配');
      return;
    }
    var p1 = L._camFit.look.clone().add(L._camFit.dir.clone().multiplyScalar(need));
    L._camFit.need = need;
    // 同步放宽轨道距离上限：自适应距离只是"装得下"的下限，
    // 用户还应能继续往外拉（否则装置一拆开就顶到墙）。
    if (L.controls) L.controls.maxDistance = Math.max(240, need * 2.2);

    if (!animate) { L.camera.position.copy(p1); if (L.controls) L.controls.update(); return; }
    L._camAnim = {
      t0: performance.now(), ms: 620,
      p0: L.camera.position.clone(), p1: p1,
      l0: L.controls ? L.controls.target.clone() : L._camFit.look.clone(),
      l1: L._camFit.look.clone()
    };
  }

  function easeInOut(t) {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  }

  // 回收单条流动特效：从场景图中摘除几何体与 DOM 标签（幂等）
  function disposeFlow(f) {
    if (!f || f._disposed) return;
    f._disposed = true;
    try {
      if (f.group && f.group.parent) f.group.parent.remove(f.group);
      if (f.tube && f.tube.parent) f.tube.parent.remove(f.tube);
      if (f.tagEl && f.tagEl.parentNode) f.tagEl.parentNode.removeChild(f.tagEl);
      if (f.tube && f.tube.geometry) f.tube.geometry.dispose();
    } catch (e) { /* 静默 */ }
  }

  // 回收全部流动特效。
  // ⚠️ 同样必须是原地清空：S.flows 与 panel.flows 共享同一个数组引用，
  //    赋新数组会让两边断开（后续特效写进的那份不会被渲染循环看到）。
  function reapFlows() {
    S.flows.forEach(disposeFlow);
    S.flows.length = 0;
  }

  function refreshVisibility() {
    Object.keys(lab.nodes).forEach(function (id) {
      var n = lab.nodes[id];
      n.group.visible = n.visible;
      n.labelEl.style.display = n.visible ? '' : 'none';
    });
  }

  function applyTween(key, v) {
    var tw = S.tweens[key] || {};
    // 🔥 目标节点可配置：课件通过 {nodes:[...], colorize:true} 指定要染色的节点。
    //    缺省回落到第一门课使用的 obj / view，保证旧课件不受影响。
    //    没有这条，新课件（节点名不叫 obj）的数值动画会完全没有视觉表现。
    var colorTargets = tw.nodes && tw.nodes.length ? tw.nodes : ['obj'];
    var colorize = (tw.colorize !== false);

    if (key === 'util') {
      // 利用率 → 颜色语义（对齐主场景 updateChargerVisual 的三档逻辑）
      var color, status;
      if (v > 0.7) { color = new THREE.Color(0xffaa00); status = '高负载'; }
      else { color = new THREE.Color(0x22ff44); status = '正常'; }
      colorTargets.forEach(function (id) {
        var n = lab.nodes[id];
        if (!n || !n.mat) return;
        if (colorize) {
          n.mat.emissive = color.clone().multiplyScalar(0.55);
          n.mat.color.copy(n.baseColor).lerp(color, 0.35 + v * 0.5);
        }
        n.mat.emissiveIntensity = 0.6 + v * 1.6;
      });
      ui.setMetric('utilization', v.toFixed(2), status, '#' + color.getHexString());
    } else if (key === 'bind') {
      var o = lab.nodes['obj'];
      if (o && o.mat) {
        o.mat.emissiveIntensity = 0.4 + v * 1.2;
        o.ring.material.opacity = 0.15 + v * 0.5;
      }
      ui.setMetric('bind_station_id', v > 0.5 ? '1001' : '（未绑定）',
        v > 0.5 ? '已绑定' : '静默失效',
        v > 0.5 ? '#51cf66' : '#ff6b6b');
    }
  }

  function resetStep() {
    // 清空流动特效。
    // ⚠️ 这几个状态必须"清空内容"而不是"换成新对象"：
    //    分镜墙下 tick 读的是 panel 自己那份（cp.tweens / cp.executed / cp.flows），
    //    若这里换成新对象，S 与 panel 就会指向两份不同的数据 ——
    //    表现为"切换章节后动作不再重播、数值过渡不推进"，而且不报错，极难发现。
    //    让 S.* 与 lab.* 指向同一个对象，是这套"逐格切换 lab 指针"架构的前提。
    reapFlows();
    S.tweens = lab.tweens;
    S.flows = lab.flows;
    S.executed = lab.executed;
    Object.keys(S.tweens).forEach(function (k) { delete S.tweens[k]; });

    // 清标注 / 对照 / 提示
    ui.clearTransient();

    // 复位所有节点。
    // ⚠️ 抽象装置课靠 stage 动作决定谁出场，所以默认隐藏；
    //    但**装置课（device）没有 stage 动作**，零件必须默认可见，
    //    否则整台设备永远不出现（曾经的真实 bug：画布全空）。
    //    判据用 isPart：装置零件在 ensureNodes 里被标了 isPart。
    Object.keys(lab.nodes).forEach(function (id) {
      var n = lab.nodes[id];
      n.visible = !!n.isPart;
      n.focused = false;
      n.dim = 1.0;
      n.ring.material.opacity = 0.0;
      n.callouts = [];
      if (n.mat) {
        n.mat.emissive = n.baseColor.clone().multiplyScalar(0.12);
        n.mat.emissiveIntensity = 1.0;
        n.mat.color.copy(n.baseColor);
        n.mat.opacity = 1.0;
      }
      n.labelEl.classList.remove('is-dim', 'is-focus');
      // 装置零件复位到"装配态"：拆解状态是章节推进的产物，
      // 切换章节必须回到基准，否则跳章回看时会看到上一章拆散的残局。
      if (n.assembled && n.group) {
        n.group.position.set(n.assembled[0], n.assembled[1], n.assembled[2]);
        n.group.rotation.set(0, 0, 0);
        n.anim = null;
      }
    });
    // 清掉本格已执行标记，使时间轴可以从头重播
    Object.keys(S.executed).forEach(function (k) { delete S.executed[k]; });
    refreshVisibility();
  }

  // ==========================================================
  // 4. 播放器
  // ==========================================================
  // 每个原语从触发到"讲完"大致占用的时间。
  // 🔥 这里必须算「动作自身的展示时长」，不能只算 at 偏移：
  //    否则章节会在最后一个动作刚触发时就判定结束，
  //    表现为对照面板/结论提示一闪而过甚至完全不出现。
  var ACTION_TAIL = {
    tween: function (a) { return a.ms || 1000; },
    camera: function (a) { return a.ms || 900; },
    compare: function () { return 2800; },
    branch: function () { return 2400; },
    flow: function () { return 1600; },
    label: function () { return 900; },
    focus: function () { return 600; },
    stage: function () { return 500; }
  };

  function stepDuration(step) {
    var max = 0;
    (step.actions || []).forEach(function (a) {
      var tail = ACTION_TAIL[a.do] ? ACTION_TAIL[a.do](a) : 800;
      var end = (a.at || 0) + tail;
      if (end > max) max = end;
    });
    return Math.max(2200, max + 900);
  }

  function gotoStep(index, opts) {
    opts = opts || {};
    if (!S.lesson) return;
    var steps = S.lesson.steps || [];
    index = Math.max(0, Math.min(index, steps.length - 1));
    S.stepIndex = index;
    S.stepPos = 0;
    _lastTick = 0;

    var cp = panels[index];
    if (cp) usePanel(cp);
    // S.* 与 lab.* 必须是同一对象（见 resetStep 的说明）
    S.executed = lab.executed;
    resetStep();
    cp.localPos = 0;

    var step = steps[index];
    ui.setStep(index, steps.length, step);
    ui.setNarration(step.narration || '');
    ui.setProgress(0);

    // 当前格从零重播；其余格保持"已定格在各自章节终态"
    if (step.camera && cp) {
      var c = step.camera;
      prim.camera({ eye: c.eye, look: c.look, ms: opts.instant ? 0 : (c.ms || 900) },
        !!opts.instant);
    }

    // 记录播放进度（用于 iframe 重建后恢复）
    try {
      localStorage.setItem(NS + '_last', JSON.stringify({
        lesson: S.lessonId, step: index, ts: Date.now()
      }));
    } catch (e) { /* 忽略 */ }

    S.playing = !!opts.play;
    ui.setPlaying(S.playing);
    // 无论播放与否都要驱动渲染循环，否则暂停状态下沙盒无法交互
    kick();
  }

  function runAction(act, animated) {
    try {
      switch (act.do) {
        case 'stage': prim.stage(act); break;
        case 'focus': prim.focus(act); break;
        case 'flow': prim.flow(act); break;
        case 'tween': prim.tween(act, animated); break;
        case 'label': prim.label(act); break;
        case 'compare': prim.compare(act); break;
        case 'branch': prim.branch(act); break;
        case 'camera': prim.camera(act, !animated); break;
        case 'move': prim.move(act, animated); break;
        case 'take_apart': prim.take_apart(act); break;
        case 'assemble': prim.assemble(act); break;
        case 'parts_tween': prim.parts_tween(act, animated); break;
        case 'wait': prim.wait(act); break;
        default: warn('未知原语:', act.do);
      }
    } catch (e) {
      warn('执行原语失败 ' + act.do, e);
    }
  }

  // 旧的单场景循环已由 storyboardTick 取代：分镜墙要逐格切换 lab 指针，
  // 逻辑集中在 storyboardTick 里，避免两份推进逻辑漂移。
  function kick() {
    if (S._raf) return;
    _lastTick = 0;
    S._raf = requestAnimationFrame(storyboardTick);
  }

  function updateFlows(dt) {
    S.flows.forEach(function (f) {
      f.phase += dt * 0.00045 * f.rate;
      if (f.phase > 1) f.phase -= 1;
      f.group.children.forEach(function (dot) {
        var t = (dot.userData.offset + (f.reverse ? -f.phase : f.phase)) % 1;
        if (t < 0) t += 1;
        var p = f.curve.getPoint(t);
        dot.position.copy(p);
        var s = 0.7 + Math.sin(t * Math.PI) * 0.7;
        dot.scale.setScalar(s);
      });
      if (f.tube.material) {
        f.tube.material.opacity = 0.18 + 0.16 * Math.abs(Math.sin(f.phase * Math.PI));
      }
      if (f.tagEl) {
        var mp = f.curve.getPoint(0.5);
        // 流动标签挂在当前格上，投影必须用当前格的相机
        projectToScreenIn(lab, mp, f.tagEl, 0);
      }
    });
  }

  function updateCameraAnim() {
    if (!lab || !lab._camAnim) return;
    var a = lab._camAnim;
    var t = Math.min(1, (performance.now() - a.t0) / Math.max(1, a.ms));
    var e = easeInOut(t);
    lab.camera.position.lerpVectors(a.p0, a.p1, e);
    if (lab.controls) {
      lab.controls.target.lerpVectors(a.l0, a.l1, e);
      lab.controls.update();
    } else {
      lab.camera.lookAt(a.l1);
    }
    if (t >= 1) lab._camAnim = null;
  }

  function nodeLabelHeight(n) {
    var s = n.spec && n.spec.size ? n.spec.size : [1, 1, 1];
    return s[1] || 1;
  }

  // ==========================================================
  // 4.5 分镜舞台（Storyboard）
  // ----------------------------------------------------------
  // 对齐 Create 的 Ponder：屏幕上并排若干「分镜格」，每格是同一装置在
  // 不同时刻的示意场景，主轴贯穿全流程，当前格高亮并在播放。
  //
  // 架构取舍：不把面板对象穿透到几十个原语里，而是让模块级的 `lab`
  // 保持"当前操作对象"的语义，渲染循环逐格切换它。代价是切换有少量
  // 开销，收益是 createNodeMesh / ensureNodes / 7 个原语全部零改动。
  // ==========================================================
  var panels = [];
  var _activePanelIdx = 0;

  function panelStep(p) { return S.lesson.steps[p.stepIndex]; }

  function buildPanel(stepIndex, container) {
    var step = S.lesson.steps[stepIndex];
    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x080b12);
    // 🔥 雾的远近要和地面尺寸匹配。
    //    之前是 Fog(30, 62)，而地面只有 48~60 单位 —— 视野里几乎看不到地面，
    //    表现为"网格覆盖面太小"。拉远雾的终点，让地面自然延伸到视野尽头。
    scene.fog = new THREE.Fog(0x080b12, 70, 260);

    var camera = new THREE.PerspectiveCamera(38, 1, 0.1, 300);
    camera.position.set(0, 9, 22);

    var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.appendChild(renderer.domElement);

    // 每格可独立旋转查看（对齐 Create 的分镜也能转）
    // ⚠️ 必须同时认 window.OrbitControls 与 THREE.OrbitControls：
    //    主场景把 addons 桥接在 window 上（window.OrbitControls），
    //    而 three 的 addon 类不会自动挂到 THREE 命名空间。
    //    曾经只判 THREE.OrbitControls，导致 controls 恒为 null ——
    //    表现为"拖不动、拉不远、视角固定"，而且不报错。
    var OrbitCtor = (typeof window !== 'undefined' && typeof window.OrbitControls === 'function')
      ? window.OrbitControls
      : (typeof THREE.OrbitControls === 'function' ? THREE.OrbitControls : null);
    var controls = null;
    if (OrbitCtor) {
      controls = new OrbitCtor(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.09;
      controls.minDistance = 2;
      // 上限给足：装置拆开后零件会散得很开，还要允许用户自己拉远观察
      controls.maxDistance = 400;
      controls.maxPolarAngle = Math.PI / 2.02;
      controls.target.set(0, 1.0, 0);
      controls.addEventListener('start', function () { p_userOrbited(stepIndex); });
    }
    // 无论有没有 OrbitControls，都挂一层兜底手势：
    // 拖拽旋转 + 滚轮缩放，保证"3D 场景一定能动"
    scene.add(new THREE.AmbientLight(0x9fb4d8, 1.25));
    var key = new THREE.DirectionalLight(0xfff2dd, 2.1);
    key.position.set(9, 16, 11);
    key.castShadow = true;
    key.shadow.mapSize.width = 1024;
    key.shadow.mapSize.height = 1024;
    var dd = 16;
    key.shadow.camera.left = -dd; key.shadow.camera.right = dd;
    key.shadow.camera.top = dd; key.shadow.camera.bottom = -dd;
    key.shadow.camera.far = 60;
    scene.add(key);
    var rim = new THREE.DirectionalLight(0x4aa3ff, 0.85);
    rim.position.set(-12, 7, -9);
    scene.add(rim);

    var floor = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshStandardMaterial({ color: 0x0f1622, roughness: 0.95 })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -0.02;
    floor.receiveShadow = true;
    floor.name = 'pd-floor';
    scene.add(floor);

    var grid = new THREE.GridHelper(1, 1, 0x2a4a6a, 0x16283c);
    grid.name = 'pd-grid';
    scene.add(grid);

    // 首帧也要有像样的地面：adaptGround 尚未调用前，用一个大网格兜住
    var initFloor = floor; var initGrid = grid;
    initFloor.scale.set(150, 150, 1);
    initGrid.scale.set(150, 1, 150);

    var p = {
      stepIndex: stepIndex,
      dom: container,
      // ⚠️ labelLayer 不在 stage 里（它是 stage 的兄弟节点，由 buildStoryboard 传入），
      //    这里只占位，真正的赋值在 buildStoryboard 里立刻补上。
      labelLayer: null,
      scene: scene,
      camera: camera,
      renderer: renderer,
      controls: controls,
      nodes: {},
      root: new THREE.Group(),
      userOrbited: false,
      playIndex: 0,
      localPos: 0,
      flows: [],
      tweens: {},
      executed: {},
      renderCount: 0
    };
    scene.add(p.root);
    // 兜底手势要在 p 构造完之后挂
    attachFallbackDrag(p);
    return p;
  }

  function p_userOrbited(stepIndex) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].stepIndex === stepIndex) {
        panels[i].userOrbited = true;
        panels[i]._dirty = true;
      }
    }
  }

  // 兜底拖拽旋转：当 OrbitControls 没能桥接进来时，至少还能转。
  // 依赖外部类来提供最基本的手感，风险太大——桥接是跨模块的约定，
  // 任何一端改名都会静默失效（曾经就发生过）。
  function attachFallbackDrag(p) {
    var el = p.renderer.domElement;
    if (!el || !el.addEventListener) return;
    var dragging = false, lx = 0, ly = 0;
    // ⚠️ 基准必须在按下时**冻结**：
    //    如果每帧都用当前机位重算基准角，累计的旋转量每帧都会被重新吸收，
    //    表现为"拖了但视角不变"（曾经的真实缺陷）。
    var base = null;

    function camTarget() {
      return p.controls ? p.controls.target : new THREE.Vector3(0, 1, 0);
    }
    function freezeBase() {
      var t = camTarget();
      var off = p.camera.position.clone().sub(t);
      base = {
        r: off.length(),
        theta: Math.atan2(off.x, off.z),
        phi: Math.asin(Math.max(-1, Math.min(1, off.y / Math.max(0.001, off.length())))),
        target: t.clone()
      };
    }
    function applyOrbit(dTheta, dPhi) {
      if (!base) freezeBase();
      var phi = Math.max(-1.35, Math.min(1.35, base.phi + dPhi));
      var theta = base.theta + dTheta;
      var r = base.r;
      p.camera.position.set(
        base.target.x + r * Math.cos(phi) * Math.sin(theta),
        base.target.y + r * Math.sin(phi),
        base.target.z + r * Math.cos(phi) * Math.cos(theta)
      );
      p.camera.lookAt(base.target);
    }

    function onDown(e) {
      dragging = true;
      lx = (e.clientX || 0); ly = (e.clientY || 0);
      freezeBase();
      try { if (el.setPointerCapture && e.pointerId !== undefined) el.setPointerCapture(e.pointerId); } catch (err) {}
    }
    function onMove(e) {
      if (!dragging || !base) return;
      var dx = (e.clientX || 0) - lx;
      var dy = (e.clientY || 0) - ly;
      lx = e.clientX || 0; ly = e.clientY || 0;
      // 偏移量本身就是"相对基准角"的增量，直接累加到基准上
      base.theta += dx * 0.008;
      base.phi = Math.max(-1.35, Math.min(1.35, base.phi + dy * 0.006));
      applyOrbit(0, 0);
      p.userOrbited = true;
      p._dirty = true;
    }
    function onUp() { dragging = false; }

    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointerleave', onUp);
    el.addEventListener('wheel', function (e) {
      // 滚轮缩放：不依赖 OrbitControls 也能拉远拉近
      e.preventDefault();
      var t = camTarget();
      var off = p.camera.position.clone().sub(t);
      var factor = (e.deltaY > 0) ? 1.12 : 0.89;
      var len = Math.max(2, Math.min(400, off.length() * factor));
      off.setLength(len);
      p.camera.position.copy(t).add(off);
      freezeBase();          // 缩放后重新冻结基准
      p.userOrbited = true;
      p._dirty = true;
    }, { passive: false });
    p._fallbackDrag = true;
  }

  // 逐格切换 lab 指针后调用既有逻辑，实现"不改原语的多面板"
  function usePanel(p) {
    _activePanelIdx = p.stepIndex;
    lab = p;
    return p;
  }

  function buildStoryboard() {
    var wrap = ui.panelWrap;
    wrap.innerHTML = '';
    disposeAllPanels();
    var steps = S.lesson.steps || [];
    for (var i = 0; i < steps.length; i++) {
      var cell = document.createElement('div');
      cell.className = 'pd-panel';
      cell.setAttribute('data-step', String(i));
      cell.innerHTML =
        '<div class="pd-panel-stage"></div>' +
        '<div class="pd-panel-labels"></div>' +
        '<div class="pd-panel-tag"><span class="pd-panel-no"></span>' +
        '<span class="pd-panel-short"></span></div>' +
        '<div class="pd-panel-metrics"></div>' +
        '<div class="pd-panel-hover">' + escapeHtml(steps[i].brief || '') + '</div>';
      wrap.appendChild(cell);
      var stageHost = cell.querySelector('.pd-panel-stage');
      var p = buildPanel(i, stageHost);
      p.el = cell;
      // 标签层是 stage 的兄弟节点，必须从 cell 上取（buildPanel 里取不到）
      p.labelLayer = cell.querySelector('.pd-panel-labels');
      p.metricHost = cell.querySelector('.pd-panel-metrics');
      // 格子上标好序号与短标签（分镜墙的"分镜说明"）
      cell.querySelector('.pd-panel-no').textContent = String(i + 1);
      cell.querySelector('.pd-panel-short').textContent = steps[i].short || steps[i].title || '';
      usePanel(p);
      ensureNodes();
      resetStep();
      panels.push(p);
    }
    usePanel(panels[0]);
    log('分镜墙已构建：' + panels.length + ' 格');
  }

  function disposeAllPanels() {
    panels.forEach(function (p) {
      try {
        p.scene.traverse(function (o) {
          if (o.geometry) o.geometry.dispose();
          if (o.material) {
            if (Array.isArray(o.material)) o.material.forEach(function (m) { m.dispose(); });
            else o.material.dispose();
          }
        });
        p.renderer.dispose();
        if (p.renderer.domElement.parentNode) {
          p.renderer.domElement.parentNode.removeChild(p.renderer.domElement);
        }
      } catch (e) { /* 静默 */ }
    });
    panels = [];
  }

  function resizePanel(p, w, h) {
    if (!w || !h) return;
    p.camera.aspect = w / h;
    p.camera.updateProjectionMatrix();
    p.renderer.setSize(w, h);
  }

  // 让某格瞬间到达"该章结束"的状态：用非动画方式跑完所有动作
  function seekPanelToEnd(p) {
    usePanel(p);
    var step = panelStep(p);
    resetStep();
    p.playIndex = 0; p.localPos = 0;
    (step.actions || []).forEach(function (a, i) {
      p.executed[i] = true;
      runAction(a, false);
    });
    // 数值补间直接落到终点
    Object.keys(p.tweens).forEach(function (k) {
      var tw = p.tweens[k];
      tw.val = tw.to;
      tw.animated = false;
      tw.done = true;
      applyTween(k, tw.to);
    });
    // 终态格子不保留流动特效：静态截图里跑粒子会显得杂乱
    reapFlows();
    if (step.camera) prim.camera({ eye: step.camera.eye, look: step.camera.look, ms: 0 }, true);
    // 定格后按"最终可见范围"铺开地面（拆解态往往比装配态宽得多）
    adaptGround(p);
    refitCamera(false);
  }

  // 分镜主循环：当前格实时推进，其余格每 N 帧刷新一次
  var _lastTick = 0;
  var _fbCounter = 0;

  function storyboardTick(ts) {
    if (!S.active) return;
    var dt = Math.min(64, ts - (_lastTick || ts));
    _lastTick = ts;

    // ---- 当前格：正常推进 ----
    var cp = panels[S.stepIndex];
    if (!cp) return;
    usePanel(cp);

    if (S.playing) {
      S.stepPos += dt * S.speed;
      var step = panelStep(cp);
      var dur = stepDuration(step);

      (step.actions || []).forEach(function (a, i) {
        if (cp.executed[i]) return;
        if (S.stepPos >= (a.at || 0)) {
          cp.executed[i] = true;
          runAction(a, true);
        }
      });

      Object.keys(cp.tweens).forEach(function (k) {
        var tw = cp.tweens[k];
        if (!tw.animated || tw.done) return;
        var t = Math.min(1, (S.stepPos - tw.start) / tw.ms);
        tw.val = tw.from + (tw.to - tw.from) * easeInOut(t);
        applyTween(k, tw.val);
        if (t >= 1) { tw.done = true; refitCamera(true); }
      });

      ui.setProgress(S.stepPos / dur);

      if (S.stepPos >= dur) {
        var last = (S.stepIndex >= panels.length - 1);
        if (last) {
          S.playing = false;
          ui.setPlaying(false);
          ui.markDone(S.lessonId);
        } else if (S.autoplayNext) {
          gotoStep(S.stepIndex + 1, { play: true });   // gotoStep 内部会重设 _lastTick
        } else {
          S.playing = false;
          ui.setPlaying(false);
        }
      }
    }

    // 渲染帧序号：零件动画用"帧计数 + delay"做错峰，而不是 setTimeout
    // （setTimeout 不参与虚拟时间推进，在逐帧推演的测试环境里会失效）
    S._tickN = (S._tickN || 0) + 1;

    updateFlows(dt);
    updateCameraAnim();
    updatePartAnims();
    if (cp.controls && cp.controls.enableDamping) cp.controls.update();
    renderPanel(cp, ts);

    // ---- 其余格：降到约 10fps 刷新 ----
    // 分镜墙最多 5 个 WebGL 上下文，若每格每帧都渲染，GPU 开销约等于 5 倍单场景。
    // 非活动格画面基本是静止的，降频即可；但用户拖拽过的那一格要恢复满帧，
    // 否则"拖不动"的手感 bug 会回来。
    _fbCounter++;
    if (_fbCounter % 6 === 0) {
      for (var i = 0; i < panels.length; i++) {
        if (i === S.stepIndex) continue;
        var p = panels[i];
        usePanel(p);
        if (p.controls && p.controls.enableDamping) p.controls.update();
        renderPanel(p, ts);
      }
      usePanel(cp);
    } else {
      // 用户正在操作的非活动格：单独补渲染，保证跟手
      for (var j = 0; j < panels.length; j++) {
        if (j === S.stepIndex) continue;
        var q = panels[j];
        if (q.userOrbited && q._dirty) {
          usePanel(q);
          if (q.controls && q.controls.enableDamping) q.controls.update();
          renderPanel(q, ts);
          usePanel(cp);
        }
      }
    }

    if (S.active) S._raf = requestAnimationFrame(storyboardTick);
  }

  // 零件拆装动画推进。
  // delay 用"帧数"而不是毫秒：逐帧推演时 performance.now() 的步进可能与
  // 帧计数不一致，用帧计数能让"错峰拆开"在任何环境下都表现一致。
  function updatePartAnims() {
    var tick = S._tickN || 0;
    Object.keys(lab.nodes).forEach(function (id) {
      var n = lab.nodes[id];
      if (!n || !n.anim) return;
      var a = n.anim;
      if (a.delay && a._startTick === undefined) a._startTick = tick + Math.round(a.delay / 16);
      var t = (performance.now() - a.t0) / Math.max(1, a.ms);
      if (a._startTick !== undefined) {
        var elapsedTicks = tick - a._startTick;
        if (elapsedTicks < 0) return;               // 还没轮到它
        t = elapsedTicks * 16 / Math.max(1, a.ms);
      }
      var e = easeInOut(Math.min(1, t));
      n.group.position.set(
        a.p0[0] + (a.p1[0] - a.p0[0]) * e,
        a.p0[1] + (a.p1[1] - a.p0[1]) * e,
        a.p0[2] + (a.p1[2] - a.p0[2]) * e
      );
      n.group.rotation.set(
        a.r0[0] + (a.r1[0] - a.r0[0]) * e,
        a.r0[1] + (a.r1[1] - a.r0[1]) * e,
        a.r0[2] + (a.r1[2] - a.r0[2]) * e
      );
      if (t >= 1) n.anim = null;
    });
  }

  function renderPanel(p, ts) {
    // 节点呼吸 / 脉冲（只在当前格做，静态格保持定格，避免视觉噪音）
    var isActive = (p.stepIndex === S.stepIndex);
    if (isActive) {
      var t = (ts || performance.now()) * 0.001;
      Object.keys(p.nodes).forEach(function (id) {
        var n = p.nodes[id];
        if (!n.visible || !n.mat) return;
        var targetOpacity = n.dim === undefined ? 1.0 : n.dim;
        n.mat.opacity += (targetOpacity - n.mat.opacity) * 0.12;
        if (n.focused) {
          var pulse = 0.5 + 0.5 * Math.sin(t * 2.2 + n.phase);
          n.mat.emissiveIntensity = Math.max(n.mat.emissiveIntensity, 0.5 + pulse * 1.1);
          n.ring.material.opacity = 0.28 + pulse * 0.32;
        } else if (n.ring) {
          n.ring.rotation.z += 0.004;
        }
      });
    }
    // 标签跟随（每格都要，旋转后位置才正确）
    Object.keys(p.nodes).forEach(function (id) {
      var n = p.nodes[id];
      if (!n.visible) return;
      var pos = n.group.position.clone();
      pos.y += nodeLabelHeight(n) + 0.55;
      projectToScreenIn(p, pos, n.labelEl, 0);
      (n.callouts || []).forEach(function (el, i2) {
        var pp = n.group.position.clone();
        pp.y += 0.6 + i2 * 0.42;
        projectToScreenIn(p, pp, el, 0);
      });
    });
    p.renderCount++;
    p.renderer.render(p.scene, p.camera);
  }

  // 指定面板的投影（原 projectToScreen 依赖全局 lab，这里保持不变并复用）
  function projectToScreenIn(p, vec3, el, yOffset) {
    var v = vec3.clone().project(p.camera);
    var w = p.dom.clientWidth, h = p.dom.clientHeight;
    var x = (v.x * 0.5 + 0.5) * w;
    var y = (-v.y * 0.5 + 0.5) * h + (yOffset || 0);
    if (v.z > 1) { el.style.opacity = '0'; return; }
    el.style.opacity = '';
    el.style.transform = 'translate(-50%,-50%) translate(' + x + 'px,' + y + 'px)';
  }

  function resizeStoryboard() {
    panels.forEach(function (p) {
      var host = p.dom;
      resizePanel(p, host.clientWidth || 1, host.clientHeight || 1);
      usePanel(p);
      refitCamera(false);
    });
    if (panels[S.stepIndex]) usePanel(panels[S.stepIndex]);
    syncPanelWidth();
    scrollPanelIntoView(S.stepIndex);
  }

  // 分镜格尺寸：给一个"每格都得住得下"的宽度区间，宁可横向滚动也不压扁。
  // 压扁的后果是 3D 装置缩成一小团、标签互相重叠 —— 那就失去教学意义了。
  function syncPanelWidth() {
    if (!ui || !ui.panelWrap) return;
    var host = ui.panelWrap.clientWidth || 800;
    var count = panels.length || 1;
    var target;
    if (count <= 1) {
      target = Math.min(520, host);
    } else {
      // 目标：同屏约 2.4 格（当前格完整 + 左右各露一点），窄屏至少 1 格
      target = Math.max(300, Math.min(460, Math.round(host / 2.4)));
      if (host < 560) target = Math.max(240, Math.round(host * 0.86));
    }
    ui.panelWrap.style.setProperty('--pd-cell-w', target + 'px');
    panels.forEach(function (p) {
      var w = p.dom.clientWidth || target;
      var h = p.dom.clientHeight || 360;
      resizePanel(p, w, h);
    });
    if (panels[S.stepIndex]) {
      usePanel(panels[S.stepIndex]);
      refitCamera(false);
    }
  }

  // 把当前格滚到可视区中间（对齐 Create：主轴推进时镜头跟着分镜走）
  function scrollPanelIntoView(index) {
    if (!ui || !ui.panelWrap) return;
    var cell = ui.panelWrap.children[index];
    if (!cell) return;
    var wrap = ui.panelWrap;
    var cellW = cell.offsetWidth || 0;
    if (!cellW) return;
    var target = (cell.offsetLeft || 0) - (wrap.clientWidth - cellW) / 2;
    wrap.scrollLeft = Math.max(0, target);
  }

  // ==========================================================
  // 5. UI 覆盖层（分镜墙布局）
  // ==========================================================
  function buildUI() {
    var root = document.createElement('div');
    root.id = 'ponder-overlay';
    root.innerHTML = [
      '<div class="pd-backdrop"></div>',
      '<div class="pd-frame">',
      '  <div class="pd-topbar">',
      '    <div class="pd-title">',
      '      <span class="pd-title-icon">💭</span>',
      '      <div><div class="pd-title-main"></div><div class="pd-title-sub"></div></div>',
      '    </div>',
      '    <div class="pd-actions">',
      '      <button class="pd-btn" data-act="restart" title="重播 (R)">⟲ 重播</button>',
      '      <button class="pd-btn" data-act="close" title="退出 (Esc)">✕ 退出</button>',
      '    </div>',
      '  </div>',
      '  <div class="pd-stage">',
      '    <div class="pd-panelwrap"></div>',
      '    <div class="pd-compare" style="display:none;"></div>',
      '    <div class="pd-branch" style="display:none;"></div>',
      '  </div>',
      '  <div class="pd-bottom">',
      '    <div class="pd-axis"><div class="pd-axis-track"></div></div>',
      '    <div class="pd-caption">',
      '      <div class="pd-caption-head"></div>',
      '      <div class="pd-narration"></div>',
      '    </div>',
      '    <div class="pd-controls">',
      '      <button class="pd-ctrl" data-act="prev" title="上一格 (←)">◀</button>',
      '      <button class="pd-ctrl pd-ctrl-play" data-act="play" title="播放/暂停 (空格)">▶</button>',
      '      <button class="pd-ctrl" data-act="next" title="下一格 (→)">▶|</button>',
      '      <div class="pd-speed">',
      '        <span>速度</span>',
      '        <button class="pd-chip" data-speed="0.5">0.5×</button>',
      '        <button class="pd-chip is-on" data-speed="1">1×</button>',
      '        <button class="pd-chip" data-speed="1.5">1.5×</button>',
      '      </div>',
      '      <div class="pd-hint">拖拽任意分镜可单独旋转</div>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('\n');
    dtt().container.appendChild(root);

    var q = function (sel) { return root.querySelector(sel); };
    var qa = function (sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); };

    ui = {
      root: root,
      panelWrap: q('.pd-panelwrap'),
      stageWrap: q('.pd-stage'),
      compareEl: q('.pd-compare'),
      branchEl: q('.pd-branch'),
      axisTrack: q('.pd-axis-track'),
      captionHead: q('.pd-caption-head'),
      narrationEl: q('.pd-narration'),
      progressEl: q('.pd-progress-fill'),
      playBtn: q('.pd-ctrl-play'),
      titleMain: q('.pd-title-main'),
      titleSub: q('.pd-title-sub')
    };
    // --- 事件绑定 ---
    qa('[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var a = b.getAttribute('data-act');
        if (a === 'close') close();
        else if (a === 'restart') gotoStep(0, { play: true });
        else if (a === 'prev') gotoStep(S.stepIndex - 1, { play: S.playing });
        else if (a === 'next') gotoStep(S.stepIndex + 1, { play: S.playing });
        else if (a === 'play') togglePlay();
      });
    });
    qa('[data-speed]').forEach(function (b) {
      b.addEventListener('click', function () {
        qa('[data-speed]').forEach(function (x) { x.classList.remove('is-on'); });
        b.classList.add('is-on');
        S.speed = parseFloat(b.getAttribute('data-speed')) || 1;
      });
    });

    // 主轴：一串节点 + 进度轨，当前节点发光。替代原来的左侧章节列表。
    ui.setStep = function (index, total, step) {
      var track = ui.axisTrack;
      track.innerHTML = '';
      S.lesson.steps.forEach(function (s, i) {
        var node = document.createElement('div');
        node.className = 'pd-axis-node' + (i === index ? ' is-active' : (i < index ? ' is-done' : ''));
        node.setAttribute('data-step', String(i));
        node.innerHTML =
          '<span class="pd-axis-dot"></span>' +
          '<span class="pd-axis-label">' + escapeHtml(s.short || s.title) + '</span>' +
          '<span class="pd-axis-bar"><span class="pd-axis-fill"></span></span>';
        node.addEventListener('click', function () { gotoStep(i, { play: true }); });
        track.appendChild(node);
      });
      // 分镜格高亮与主轴同步
      if (ui.panelWrap) {
        Array.prototype.slice.call(ui.panelWrap.children).forEach(function (c, i) {
          c.classList.toggle('is-active', i === index);
          c.classList.toggle('is-done', i < index);
        });
        scrollPanelIntoView(index);
      }
      ui.captionHead.textContent = (index + 1) + '. ' + (step.title || '');
      ui.titleMain.textContent = S.lesson.title;
      ui.titleSub.textContent = (S.lesson.subtitle || '') + ' · ' + (index + 1) + '/' + total;
    };

    ui.setNarration = function (txt) {
      ui.narrationEl.style.opacity = '0';
      setTimeout(function () {
        ui.narrationEl.textContent = txt;
        ui.narrationEl.style.opacity = '1';
      }, 140);
    };

    // 进度不再是一条独立进度条，而是"当前主轴节点下方的填充条"——
    // 这样进度与它所属的分镜绑定，一眼能看出"第 3 格播到一半"。
    ui.setProgress = function (p) {
      var track = ui.axisTrack;
      if (!track) return;
      var nodes = track.querySelectorAll('.pd-axis-fill');
      var v = Math.max(0, Math.min(1, p || 0));
      Array.prototype.forEach.call(nodes, function (el, i) {
        el.style.width = (i < S.stepIndex ? 100 : (i === S.stepIndex ? v * 100 : 0)) + '%';
      });
    };

    ui.setPlaying = function (p) {
      ui.playBtn.textContent = p ? '❚❚' : '▶';
    };

    ui.clearTransient = function () {
      ui.compareEl.style.display = 'none';
      ui.compareEl.innerHTML = '';
      ui.branchEl.style.display = 'none';
      ui.branchEl.innerHTML = '';
      // 分镜墙下标签层是"每格一份"，要清的是各格 label 层里除节点标签外的元素
      panels.forEach(function (p) {
        if (!p.labelLayer) return;
        Array.prototype.slice.call(p.labelLayer.children).forEach(function (c) {
          if (!c.classList.contains('pd-node-label')) p.labelLayer.removeChild(c);
        });
      });
    };

    ui.showCompare = function (act) {
      var mk = function (side) {
        if (!side) return '';
        return '<div class="pd-cmp-side" style="border-color:' + side.color + '">' +
          '<div class="pd-cmp-title" style="color:' + side.color + '">' + escapeHtml(side.title) + '</div>' +
          (side.lines || []).map(function (l) {
            return '<div class="pd-cmp-line">' + escapeHtml(l) + '</div>';
          }).join('') + '</div>';
      };
      ui.compareEl.innerHTML =
        '<div class="pd-cmp-head">' + escapeHtml(act.title || '对照') + '</div>' +
        '<div class="pd-cmp-grid">' + mk(act.left) + mk(act.right) + '</div>';
      ui.compareEl.style.display = 'block';
    };

    ui.showBranch = function (act) {
      ui.branchEl.className = 'pd-branch pd-tone-' + (act.tone || 'info');
      ui.branchEl.textContent = act.text || '';
      ui.branchEl.style.display = 'block';
    };

    // 关键指标改挂在「当前分镜格」底部：分镜墙下没有侧栏，
    // 数值要和它所属的那一格待在一起才读得懂。
    ui.metricEls = {};
    ui.setMetric = function (key, value, status, color) {
      var host = panels[S.stepIndex] ? panels[S.stepIndex].metricHost : null;
      if (!host) return;
      var el = ui.metricEls[key];
      if (!el || el.parentNode !== host) {
        el = document.createElement('div');
        el.className = 'pd-metric';
        el.innerHTML = '<div class="pd-metric-k"></div><div class="pd-metric-v"></div>' +
          '<div class="pd-metric-s"></div>';
        host.appendChild(el);
        ui.metricEls[key] = el;
      }
      el.querySelector('.pd-metric-k').textContent = key;
      var vEl = el.querySelector('.pd-metric-v');
      vEl.textContent = value;
      vEl.style.color = color || '#eef2ff';
      el.querySelector('.pd-metric-s').textContent = status || '';
    };

    // 当前场景上下文（原来在右侧栏，现在无处安放）→ 只输出到 console，
    // 并作为标题副文本显示一行摘要，保持教学信息不丢但不占地方。
    ui.setContext = function (ctx) {
      var hero = (ctx && ctx.hero) || null;
      var brief = '';
      if (hero) {
        brief = '讲解对象：' + hero.name + '（' + hero.type + '，' +
          (hero.bound ? '已绑定 ' + hero.station_id : '未绑定') + '）';
      } else {
        brief = '场景中暂无对象，以下为通用讲解';
      }
      if (ctx && ctx.unboundCount > 0) {
        brief += ' · ⚠️ ' + ctx.unboundCount + ' 个充电桩未绑定，数据不会更新';
      }
      ui.contextBrief = brief;
      log('教学上下文：' + brief);
    };

    ui.markDone = function (lessonId) {
      try {
        var done = JSON.parse(localStorage.getItem(NS + '_done') || '{}');
        done[lessonId] = Date.now();
        localStorage.setItem(NS + '_done', JSON.stringify(done));
      } catch (e) { /* 忽略 */ }
    };

    return ui;
  }

  // ==========================================================
  // 5.5 对象气泡（对齐 Create 的 Ponder：入口贴物体，只放强相关的课）
  // ----------------------------------------------------------
  // 设计取舍：
  //   · 气泡只列 object 级课程，且只列这个类型真的相关的（数据由
  //     payload.byType 下发，Python 侧已过滤）。列表为空就不显示气泡 ——
  //     宁可没有，也不要把"四层架构"塞到一个充电桩旁边。
  //   · 气泡跟着物体走（每帧投影），所以物体被拖动/相机旋转时都贴得住。
  //   · 点选物体即可出现；再点一次收起（与主场景的选中行为一致）。
  // ==========================================================
  var bubble = null;
  var _lastSel = '';
  var _markerDelay = 0;

  function isDone(lid) {
    try {
      var done = JSON.parse(localStorage.getItem(NS + '_done') || '{}');
      return !!done[lid];
    } catch (e) { return false; }
  }

  function chapterText(lid) {
    var les = S.data.lessons[lid];
    return les ? (les.steps.length + ' 章') : '';
  }

  // 选中对象在屏幕上的位置（用于把气泡贴在物体旁）
  function selectedScreenPos() {
    var D = dtt();
    if (!D || !D.getObjectMeshes || !D.getSelectedId) return null;
    var id = D.getSelectedId();
    if (!id) return null;
    var meshes = D.getObjectMeshes() || [];
    for (var i = 0; i < meshes.length; i++) {
      var m = meshes[i];
      if (!m || !m.userData || m.userData.objectId !== id) continue;
      // ⚠️ 必须 clone：project() 是原地变换，直接传 m.position 会把主场景的
      //    物体位置改坏（同类坑在相机那段已经踩过一次）。
      var p = m.position.clone();
      // 抬高到物体顶部附近，避免压住物体本身
      p.y += 1.6;
      var v = p.project(D.camera);
      var host = D.container || document.body;
      var w = host.clientWidth || 800, h = host.clientHeight || 600;
      var x = (v.x * 0.5 + 0.5) * w;
      var y = (-v.y * 0.5 + 0.5) * h;
      return { x: x, y: y, behind: (v.z > 1), id: id, type: resolveObjectType(id, m.userData.type) };
    }
    return null;
  }

  // 🔥 类型解析：以场景数据里的 type 为唯一真值，渲染层挂的副本只作兜底。
  //    理由与项目其它地方一致——同一个信息只应有一个权威来源；
  //    渲染层是大函数里逐个分支手工挂 type 的，漏挂一处就会静默失效。
  function resolveObjectType(id, fallbackType) {
    try {
      var D = dtt();
      var objs = (D && D.getSceneData) ? (D.getSceneData() || {}).objects : null;
      if (objs && objs.length) {
        for (var i = 0; i < objs.length; i++) {
          if (String(objs[i].id) === String(id)) return objs[i].type || fallbackType || '';
        }
      }
    } catch (e) { /* 静默：解析失败就退回渲染层的类型 */ }
    return fallbackType || '';
  }

  function bubbleLessonsFor(type) {
    var map = S.data.byType || {};
    var max = S.data.bubbleMax || 3;
    // 规范化：去掉空白、统一小写，避免 "charger_fast " 这类带空格/大小写差异读不到映射
    var t = String(type == null ? '' : type).trim();
    if (!t) return [];
    var ids = map[t] || map[t.toLowerCase()] || [];
    // 兜底：同一大类（如 charger_*）里任一类型命中就用它 —— 语义一致，
    // 且能容忍新增的充电桩变体（charger_v2 之类）没来得及登记映射。
    if (!ids.length && t.indexOf('charger') === 0) {
      var keys = Object.keys(map);
      for (var i = 0; i < keys.length; i++) {
        if (keys[i].indexOf('charger') === 0) { ids = map[keys[i]]; break; }
      }
    }
    return ids.filter(function (lid) { return !!S.data.lessons[lid]; }).slice(0, max);
  }

  function buildBubble() {
    // 🔥 三重校验，任一不成立就重建：
    //    ① 有内存引用 ② 该元素仍在文档里（isConnected）③ 它是当前 DOM 里那个
    //    只判 ① 是不够的：内存引用与 DOM 一旦不一致（引用被重置 / 元素被摘除），
    //    后续写入就全写进了"看不见的那个元素"——表现为点了没反应或内容为空。
    var inDom = document.getElementById('ponder-bubble');
    if (bubble && bubble.el && bubble.el.isConnected && bubble.el === inDom) return bubble;
    if (inDom && inDom.parentNode) {
      bubble = { el: inDom, body: inDom.querySelector('.pb-body') };
      return bubble;
    }
    var el = document.createElement('div');
    el.id = 'ponder-bubble';
    el.style.display = 'none';
    el.innerHTML = [
      '<div class="pb-head">💭 思索</div>',
      '<div class="pb-body"></div>',
      '<div class="pb-foot">按 H 键开关 · 或点场景底部的 💭 看全部课程</div>'
    ].join('');
    // 气泡挂在场景容器上（而不是某个物体上），每帧重新定位
    dtt().container.appendChild(el);
    bubble = { el: el, body: el.querySelector('.pb-body') };
    return bubble;
  }

  function showBubbleFor(type) {
    var ids = bubbleLessonsFor(type);
    var b = buildBubble();
    b.body.innerHTML = '';
    if (!ids.length) {
      // 🔥 冷启动反馈：没有强相关课程时，不能让用户点了没反应（那看起来像坏了）。
      //    给一句明确的零结果提示 + 通往课程目录的指引，而不是静默失败。
      var empty = document.createElement('div');
      empty.className = 'pb-empty';
      empty.textContent = '这类物体暂无专属讲解';
      b.body.appendChild(empty);
      // 排障信息只进 console，不进界面（界面要保持干净，不能把调试文案给评委看）
      var dbg = bubbleDebug();
      log('气泡空态：类型=' + (dbg.resolvedType || '(未识别)') +
        ' · 可用映射=' + (dbg.byTypeKeys || []).join('/') +
        ' · 选中 id=' + (dbg.selectedId || '(无)'));
      b.el.style.display = 'block';
      positionBubble();
      return false;
    }
    ids.forEach(function (lid) {
      var les = S.data.lessons[lid] || {};
      var row = document.createElement('div');
      row.className = 'pb-item' + (isDone(lid) ? ' is-done' : '');
      row.innerHTML = '<span class="pb-icon">' + (les.icon || '💭') + '</span>' +
        '<span class="pb-title">' + escapeHtml(les.title || lid) + '</span>' +
        '<span class="pb-ch">' + chapterText(lid) + '</span>';
      row.addEventListener('click', function (ev) {
        ev.stopPropagation();
        hideBubble();
        open(lid);
      });
      b.body.appendChild(row);
    });
    b.el.style.display = 'block';
    // 立刻定位一次：tick() 里虽然每帧都会重新定位，但显示瞬间不能先闪在左上角
    positionBubble();
    return true;
  }

  function hideBubble() {
    if (bubble) bubble.el.style.display = 'none';
  }

  function positionBubble() {
    if (!bubble || bubble.el.style.display === 'none') return;
    var p = selectedScreenPos();
    if (!p || p.behind) { bubble.el.style.opacity = '0'; return; }
    bubble.el.style.opacity = '';
    var bw = bubble.el.offsetWidth || 200;
    var bh = bubble.el.offsetHeight || 80;
    var host = dtt().container || document.body;
    var w = host.clientWidth || 800, h = host.clientHeight || 600;
    // 贴右侧，超出视口就往左翻
    var x = p.x + 76;
    var y = p.y - bh / 2;
    if (x + bw > w - 8) x = p.x - bw - 76;
    x = Math.max(8, Math.min(x, Math.max(8, w - bw - 8)));
    y = Math.max(8, Math.min(y, Math.max(8, h - bh - 8)));
    bubble.el.style.transform = 'translate(' + Math.round(x) + 'px,' + Math.round(y) + 'px)';
  }

  function bindStageBubble() {
    var D = dtt();
    if (!D || !D.container) return;
    // 点击场景：选中变化后延迟一点再看（主场景的选中逻辑在它自己的监听里）
    D.container.addEventListener('click', function () {
      if (S.active) return;                 // 教学进行中不弹气泡
      var before = _lastSel;
      setTimeout(function () {
        var p = selectedScreenPos();
        var id = p ? p.id : '';
        // 点同一个已选中的物体 → 收起（等效开关）
        if (id && id === before) { hideBubble(); _lastSel = ''; return; }
        _lastSel = id;
        if (p && p.id) showBubbleFor(p.type);
        else hideBubble();
      }, 60);
    }, false);
    log('对象气泡已就绪（气泡仅显示与所选类型强相关的课程）');
  }

  // H 键：等价于"给我看当前选中物体的思索"
  function toggleBubbleForSelection() {
    if (bubble && bubble.el.style.display !== 'none') { hideBubble(); return; }
    var p = selectedScreenPos();
    if (!p || !p.id) { log('未选中任何物体'); return; }
    _lastSel = p.id;
    showBubbleFor(p.type);
  }

  // ==========================================================
  // 5.6 课程目录弹层（由 3D 场景底部的 💭 按钮触发）
  // ----------------------------------------------------------
  // 入口从右侧面板搬到场景工具条（与导览/回放同一排），原因：
  //   · 右侧面板是「对象检查器」，课程目录放在那里语义错位且很占地方；
  //   · 放在场景工具条上，与"看 / 听 / 导览"这些发生在场景里的动作同类。
  // 弹层用 fixed 定位（#ponder-menu 挂在 body 上，不在场景容器内）。
  // ==========================================================
  var menuEl = null;

  function positionMenu() {
    if (!menuEl || menuEl.style.display === 'none') return;
    var btn = document.getElementById('ponder-btn');
    if (!btn) { warn('定位失败：找不到 #ponder-btn'); return; }

    var r = btn.getBoundingClientRect();
    var mw = menuEl.offsetWidth || 260;
    var mh = menuEl.offsetHeight || 220;

    var D = dtt();
    var host = (D && D.container) ? D.container : null;
    var hostRect = host ? host.getBoundingClientRect() : null;
    var vw = (hostRect ? hostRect.width : window.innerWidth) || 800;
    var vh = (hostRect ? hostRect.height : window.innerHeight) || 600;

    var bx = r.left + r.width / 2 - (hostRect ? hostRect.left : 0);   // 相对宿主
    var by = r.top - (hostRect ? hostRect.top : 0);

    var x = bx - mw / 2;
    var y = by - mh - 12;                       // 默认贴按钮上方
    x = Math.max(8, Math.min(x, Math.max(8, vw - mw - 8)));
    if (y < 8) y = by + r.height + 12;          // 上方放不下就翻到下方
    y = Math.max(8, Math.min(y, Math.max(8, vh - mh - 8)));

    if (host) {
      // 🔥 关键修复：之前一律用 position:fixed。但只要 #container 或任一祖先
      //    带有 transform / filter / backdrop-filter / will-change，
      //    就会形成新的包含块，fixed 会退化成"相对那个祖先"定位，
      //    结果弹层被放到看不见的地方。改成锚在宿主容器里的 absolute，
      //    无论祖先有没有 transform 都成立。
      menuEl.style.position = 'absolute';
      menuEl.style.left = Math.round(x) + 'px';
      menuEl.style.top = Math.round(y) + 'px';
      if (menuEl.parentNode !== host) host.appendChild(menuEl);
    } else {
      menuEl.style.position = 'fixed';
      menuEl.style.left = Math.round(r.left + r.width / 2 - mw / 2) + 'px';
      menuEl.style.top = Math.round(Math.max(8, r.top - mh - 12)) + 'px';
    }
    S._menuDebug = {
      hostFound: !!host, mode: menuEl.style.position,
      button: { left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width) },
      hostSize: hostRect ? { w: Math.round(hostRect.width), h: Math.round(hostRect.height),
                             left: Math.round(hostRect.left), top: Math.round(hostRect.top) } : null,
      menuSize: { w: mw, h: mh },
      placedAt: { x: Math.round(x), y: Math.round(y) },
      viewport: { vw: Math.round(vw), vh: Math.round(vh) }
    };
  }

  function buildMenu() {
    if (menuEl) return menuEl;
    menuEl = document.getElementById('ponder-menu');
    if (!menuEl) {
      // HTML 里没有标记时自建，保证功能不依赖 index 改动
      menuEl = document.createElement('div');
      menuEl.id = 'ponder-menu';
      document.body.appendChild(menuEl);
    }
    return menuEl;
  }

  function renderMenu() {
    var el = buildMenu();
    var catalog = S.data.catalog || [];
    var objects = catalog.filter(function (l) { return l.scope === 'object'; });
    var tools = catalog.filter(function (l) { return l.scope !== 'object'; });

    function section(title, items) {
      if (!items.length) return '';
      return '<div class="pm-sec">' + escapeHtml(title) + '</div>' + items.map(function (l) {
        var done = isDone(l.id);
        return '<div class="pm-item' + (done ? ' is-done' : '') + '" data-lesson="' + l.id + '">' +
          '<span class="pm-icon">' + (l.icon || '💭') + '</span>' +
          '<span class="pm-title">' + escapeHtml(l.title) + '</span>' +
          '<span class="pm-ch">' + l.steps + '章' + (done ? ' ✓' : '') + '</span>' +
          '</div>';
      }).join('');
    }

    el.innerHTML =
      '<div class="pm-head">💭 思索 · 交互式教学</div>' +
      section('讲具体物体', objects) +
      section('讲整个工具', tools) +
      '<div class="pm-foot">选中物体后按 H 可只看相关课程</div>';
    Array.prototype.slice.call(el.querySelectorAll('.pm-item')).forEach(function (row) {
      row.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var lid = row.getAttribute('data-lesson');
        hideMenu();
        open(lid);
      });
    });
    return el;
  }

  function showMenu() {
    var el = renderMenu();
    el.style.display = 'block';
    var btn = document.getElementById('ponder-btn');
    if (btn) btn.classList.add('active');
    // 必须显示之后再定位：否则量不到 offsetWidth/Height
    positionMenu();
    log('课程目录已展开 (' + (S.data.catalog || []).length + ' 门课) ' +
      JSON.stringify(S._menuDebug || {}));
  }

  function hideMenu() {
    if (menuEl) menuEl.style.display = 'none';
    var btn = document.getElementById('ponder-btn');
    if (btn) btn.classList.remove('active');
  }

  function toggleMenu() {
    if (menuEl && menuEl.style.display === 'block') { hideMenu(); return; }
    hideBubble();
    showMenu();
  }

  function bindMenuButton() {
    var btn = document.getElementById('ponder-btn');
    if (!btn) {
      // 按钮找不到时把现场信息一并打出来：这是最容易"静默失效"的一环
      warn('未找到 #ponder-btn —— 入口按钮无法绑定。' +
        ' iframe 内按钮 id 列表: ' +
        Array.prototype.slice.call(document.querySelectorAll('button'))
          .map(function (b) { return b.id || '(无id)'; }).join(', '));
      return;
    }
    log('找到 #ponder-btn，开始绑定点击');
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleMenu();
    });
    // 点空白处收起（弹层内部与按钮自身不触发收起）
    document.addEventListener('click', function (ev) {
      if (!menuEl || menuEl.style.display !== 'block') return;
      var t = ev.target;
      if (menuEl.contains && menuEl.contains(t)) return;
      if (t === btn || (btn.contains && btn.contains(t))) return;
      hideMenu();
    });
    window.addEventListener('resize', positionMenu);
    log('思索入口按钮已绑定（场景工具条）');
  }

  function togglePlay() {
    S.playing = !S.playing;
    ui.setPlaying(S.playing);
    kick();
  }

  function open(lessonId) {
    try {
      if (!S.data) S.data = readData();
      if (!S.data) { warn('无课件数据，无法打开'); return; }
      var lessons = S.data.lessons || {};
      var id = lessonId || Object.keys(lessons)[0];
      var lesson = lessons[id];
      if (!lesson) { warn('课件不存在:', id); return; }

      S.lessonId = id;
      S.lesson = lesson;
      S.active = true;

      dtt().setPonderActive(true);
      buildUI();                  // 覆盖层骨架
      prim = prim || makePrim();
      buildStoryboard();          // 每格一套 scene/camera/renderer
      // 先把所有格定格在各自章节的终态，形成"分镜墙"的静态全貌…
      for (var i = 0; i < panels.length; i++) seekPanelToEnd(panels[i]);
      // …再把第 1 格交回实时播放
      resizeStoryboard();
      ui.setContext(S.data.context);
      gotoStep(0, { play: true });
      window.addEventListener('resize', resizeStoryboard);
      hideBubble();
      hideMenu();
      log('打开课件：' + lesson.title + '（分镜 ' + panels.length + ' 格）');
    } catch (e) {
      warn('打开课件失败', e);
      close();
    }
  }

  function close() {
    S.active = false;
    S.playing = false;
    if (S._raf) { cancelAnimationFrame(S._raf); S._raf = null; }
    if (ui && ui.root && ui.root.parentNode) ui.root.parentNode.removeChild(ui.root);
    ui = null;
    disposeAllPanels();
    lab = null;
    prim = null;
    S.flows = [];
    S.tweens = {};
    dtt().setPonderActive(false);
    window.removeEventListener('resize', resizeStoryboard);
    log('已退出教学');
  }

  function isActive() { return !!S.active; }

  // ==========================================================
  // 7. 键盘
  // ==========================================================
  function onKey(e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable)) return;

    // 🔥 H 键在教学之外也可用：它就是"给我看当前选中物体的思索"，
    //    是气泡的无鼠标入口（对齐 Create 里 Ponder 的快捷键心智）。
    if (!S.active) {
      if (e.key === 'h' || e.key === 'H') {
        e.preventDefault(); e.stopPropagation();
        hideMenu();
        toggleBubbleForSelection();
      } else if (e.key === 'Escape') {
        hideBubble(); hideMenu(); _lastSel = '';
      }
      return;
    }

    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === ' ') { e.preventDefault(); e.stopPropagation(); togglePlay(); return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); gotoStep(S.stepIndex + 1, { play: S.playing }); return; }
    if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); gotoStep(S.stepIndex - 1, { play: S.playing }); return; }
    if (e.key === 'r' || e.key === 'R') { e.preventDefault(); e.stopPropagation(); gotoStep(0, { play: true }); return; }
  }

  // ==========================================================
  // 8. 启动
  // ==========================================================
  // 🔥 主场景脚本是 type="module"（异步），思索是经典脚本（先执行）。
  //    两者执行顺序不保证，所以启动必须带重试，不能"同步试一次就放弃"。
  //    同理 openWhenReady 会把意图排队，等 __DTT__ 就绪后再真正打开。
  var _pendingOpen = null;
  var _bootTries = 0;

  function bootWithRetry() {
    if (S.booted) return;
    _bootTries++;
    var ready = (typeof THREE !== 'undefined') && dtt() && dtt().container;
    if (!ready) {
      if (_bootTries <= 40) {           // 最多约 6 秒
        setTimeout(bootWithRetry, 150);
      } else {
        warn('主场景就绪超时，思索功能未启用（已重试 ' + _bootTries + ' 次）');
      }
      return;
    }
    try {
      boot();
    } catch (e) {
      warn('启动异常', e);
      return;
    }
    if (_pendingOpen && S.booted) {
      var id = _pendingOpen;
      _pendingOpen = null;
      open(id);
    }
  }

  function openWhenReady(lessonId) {
    if (S.booted) { open(lessonId); return; }
    _pendingOpen = lessonId || '';
    bootWithRetry();
  }

  function boot() {
    if (S.booted) return;
    if (typeof THREE === 'undefined') { warn('THREE 未就绪'); return; }
    if (!dtt() || !dtt().container) { warn('未找到 __DTT__ 挂载点'); return; }
    S.booted = true;

    if (typeof window.OrbitControls !== 'function' && typeof THREE.OrbitControls !== 'function') {
      warn('OrbitControls 未桥接到 window，分镜将退化为固定视角');
    }

    S.data = readData();
    if (!S.data) return;

    document.addEventListener('keydown', onKey, true);
    log('[1/3] keydown 已挂');
    try {
      bindStageBubble();
      log('[2/3] 对象气泡已就绪');
    } catch (e) {
      warn('[2/3] 对象气泡绑定失败（已跳过，不影响后续）', e);
    }
    try {
      bindMenuButton();
      log('[3/3] 工具条入口已就绪');
    } catch (e) {
      warn('[3/3] 工具条入口绑定失败', e);
    }

    // 入口按钮已由 Streamlit 侧渲染，这里只做一次可用性标记
    log('运行时就绪 · 课件 ' + (S.data.catalog || []).length + ' 个 · 装置节点 ' +
      Object.keys(S.data.primSpec || {}).length + ' 种');

    // 恢复上次进度提示（不自动打开，避免每次 rerun 都弹）
    try {
      var last = JSON.parse(localStorage.getItem(NS + '_last') || 'null');
      if (last && last.lesson) {
        log('上次观看到：' + last.lesson + ' 第 ' + (last.step + 1) + ' 章');
      }
    } catch (e) { /* 忽略 */ }

    // 暴露入口：Streamlit 按钮通过 window 调用
    window.__DTT_PONDER__ = window.__DTT_PONDER__ || {};
    window.__DTT_PONDER__.open = open;

    // 🔥 打开请求：由右侧面板按钮设置 query param → Python 写入数据岛 →
    //    这里消费。iframe 每次 rerun 都会重建，所以这个"一次性意图"必须
    //    随数据岛一起来，不能靠前端自己记。
    if (S.data.autoOpen) {
      setTimeout(function () {
        try { open(S.data.autoOpen); }
        catch (e) { warn('自动打开课件失败', e); }
      }, 120);
    }
  }

  // 保证 API 在任何时机都存在（Streamlit 按钮可能先于 boot 调用）
  window.__DTT_PONDER__ = window.__DTT_PONDER__ || {};  window.__DTT_PONDER__.boot = bootWithRetry;
  window.__DTT_PONDER__.open = openWhenReady;
  window.__DTT_PONDER__.close = close;
  window.__DTT_PONDER__.isActive = isActive;
  // 气泡排障：把"气泡实际读到的类型 / 匹配到的课程 / 选中对象是谁"一并暴露。
  // 出现"明明是充电桩却说暂无专属讲解"时，一眼就能看出是类型读错了还是映射缺了。
  function bubbleDebug() {
    var p = selectedScreenPos();
    var type = p ? p.type : null;
    var map = S.data.byType || {};
    var D = dtt();
    // 渲染层挂的 type 副本，用来和权威来源对比
    var meshType = null;
    try {
      var id0 = D && D.getSelectedId ? D.getSelectedId() : null;
      var ms = (D && D.getObjectMeshes) ? (D.getObjectMeshes() || []) : [];
      for (var i = 0; i < ms.length; i++) {
        if (ms[i] && ms[i].userData && ms[i].userData.objectId === id0) {
          meshType = ms[i].userData.type;
          break;
        }
      }
    } catch (e) { meshType = '(读取失败)'; }
    return {
      selectedId: (D && D.getSelectedId) ? D.getSelectedId() : null,
      resolvedType: type,
      meshUserDataType: meshType,
      typeSource: (String(type) === String(meshType)) ? '两者一致' : '不一致（以场景数据为准）',
      seenAs: type ? (String(type) + (map[type] ? '（有映射）' : '（无映射）')) : '(未选中或找不到 mesh)',
      matchedLessons: type ? bubbleLessonsFor(type) : [],
      byTypeKeys: Object.keys(map),
      meshCount: (D && D.getObjectMeshes) ? (D.getObjectMeshes() || []).length : 0,
      // 有多少 mesh 带 objectId 却没挂 type —— 这类"半挂载"条目最容易读错
      meshesMissingType: (function () {
        var n = 0;
        try {
          var arr = (D && D.getObjectMeshes) ? (D.getObjectMeshes() || []) : [];
          for (var i = 0; i < arr.length; i++) {
            var u = arr[i] && arr[i].userData;
            if (u && u.objectId && !u.type) n++;
          }
        } catch (e) { /* 忽略 */ }
        return n;
      })()
    };
  }

  window.__DTT_PONDER__.bubbleDebug = bubbleDebug;

  // 当前分镜的现场快照：排障用（可见节点 / 地面网格尺寸 / 相机距离 / 控制器）
  window.__DTT_PONDER__.panelProbe = function () {
    if (!lab) return null;
    var vis = 0, total = 0, parts = 0;
    Object.keys(lab.nodes).forEach(function (id) {
      total++;
      if (lab.nodes[id].visible) vis++;
      if (lab.nodes[id].isPart) parts++;
    });
    var grid = lab.scene.getObjectByName('pd-grid');
    var floor = lab.scene.getObjectByName('pd-floor');
    // 网格尺寸存在 userData 上（按世界格距重建，不再靠 scale）
    var gridSpan = (grid && grid.userData && grid.userData.span) ? grid.userData.span : null;
    return {
      step: S.stepIndex,
      totalNodes: total,
      visibleNodes: vis,
      partNodes: parts,
      hasControls: !!lab.controls,
      hasFallbackDrag: !!lab._fallbackDrag,
      controlsMaxDist: lab.controls ? lab.controls.maxDistance : null,
      camDist: lab.controls
        ? Math.round(lab.camera.position.distanceTo(lab.controls.target) * 10) / 10
        : Math.round(lab.camera.position.length() * 10) / 10,
      // 相机世界坐标：旋转不改变 camDist，必须看坐标才能判断"是否转了"
      camPos: lab.camera.position.toArray().map(function (v) { return Math.round(v * 100) / 100; }),
      gridSpan: gridSpan,
      floorScale: floor ? [Math.round(floor.scale.x), Math.round(floor.scale.y)] : null
    };
  };

  window.__DTT_PONDER__.diagnostics = function () {
    var fired = 0;
    if (S.executed) Object.keys(S.executed).forEach(function (k) { if (S.executed[k]) fired++; });
    var btn = document.getElementById('ponder-btn');
    var m = menuEl || document.getElementById('ponder-menu');
    return {
      booted: S.booted,
      active: S.active,
      playing: !!S.playing,
      lesson: S.lessonId,
      step: S.stepIndex,
      stepPos: Math.round(S.stepPos),
      stepDuration: S.lesson ? stepDuration(S.lesson.steps[S.stepIndex]) : 0,
      firedActions: fired,
      flows: S.flows.length,
      nodes: lab ? Object.keys(lab.nodes).length : 0,
      // 排障用：相机距离 / 视口宽高比 / 自适应目标距离
      camDist: (lab && lab.controls)
        ? Math.round(lab.camera.position.distanceTo(lab.controls.target) * 10) / 10
        : null,
      camAspect: lab ? Math.round(lab.camera.aspect * 100) / 100 : null,
      fitNeed: (lab && lab._camFit && lab._camFit.need)
        ? Math.round(lab._camFit.need * 10) / 10 : null,
      // 当前可见节点在屏幕上的横向占比（0.9 以上就快贴边了）
      maxNdcX: maxVisibleNdcX(),
      // 入口自检：按钮是否找到 / 弹层是否在 DOM / 位置算到哪
      menu: {
        buttonFound: !!btn,
        menuFound: !!m,
        display: m ? String(m.style.display) : null,
        position: m ? String(m.style.position) : null,
        left: m ? String(m.style.left) : null,
        top: m ? String(m.style.top) : null,
        itemCount: m ? m.querySelectorAll('.pm-item').length : 0
      },
      menuDebug: S._menuDebug || null,
      bubbleVisible: !!(bubble && bubble.el && bubble.el.style.display === 'block'),
      // 排障：对照/提示面板的真实元素引用（用于和外部查询结果做 === 比较）
      _compareEl: ui ? ui.compareEl : null,
      _branchEl: ui ? ui.branchEl : null,
      _overlayRoot: ui ? ui.root : null,
      _panelCount: panels.length,
      // 排障计数：compare 触发了几次、当前 display、branch 触发次数
      _cmpDisplay: (ui && ui.compareEl) ? String(ui.compareEl.style.display) : '(no-ui)'
    };
  };

  // 排障辅助：可见节点里最大的 |ndc.x|，以及是否有节点出画
  function maxVisibleNdcX() {
    if (!lab) return null;
    var maxX = 0, out = 0;
    Object.keys(lab.nodes).forEach(function (id) {
      var n = lab.nodes[id];
      if (!n.visible) return;
      var p = n.group.position.clone();
      p.y += nodeLabelHeight(n) + 0.55;
      p.project(lab.camera);
      if (Math.abs(p.x) > maxX) maxX = Math.abs(p.x);
      if (Math.abs(p.x) > 1 || Math.abs(p.y) > 1 || p.z > 1) out++;
    });
    return { maxX: Math.round(maxX * 100) / 100, outOfFrame: out };
  }
})();
