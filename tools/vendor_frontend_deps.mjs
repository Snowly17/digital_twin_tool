/**
 * 把前端第三方依赖镜像到 static/vendor/，用于彻底离线演示。
 *
 * 为什么需要本脚本：browser 端 importmap 原来指向 unpkg / jsdelivr，
 * 断网或 CDN 抖动时整个 3D 场景起不来。此脚本把依赖及其「相对 import 闭包」
 * 抓到本地，保持 three/addons/ 的目录结构，使相对 import 依然成立。
 *
 * 用法：node tools/vendor_frontend_deps.mjs
 * 说明：three 版本锁在 0.160.0（与现有代码一致，未擅自升级）；
 *       addons 之间存在相互 import，必须递归抓取闭包，不能只拷入口文件。
 */
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';

const THREE_VERSION = '0.160.0';
const SUPABASE_VERSION = '2.116.0';

const THREE_BASE = `https://unpkg.com/three@${THREE_VERSION}/`;
const JSM_REMOTE = `${THREE_BASE}examples/jsm/`;
const VENDOR = path.resolve('static/vendor');

// 入口：app.py 里实际 import 的 addons
const ADDON_ENTRIES = [
  'controls/OrbitControls.js',
  'controls/TransformControls.js',
  'renderers/CSS2DRenderer.js',
  'loaders/GLTFLoader.js',
  'postprocessing/EffectComposer.js',
  'postprocessing/RenderPass.js',
  'postprocessing/UnrealBloomPass.js',
];

function get(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'user-agent': 'node-vendor-script' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(get(new URL(res.headers.location, url).href));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    });
    req.on('error', reject);
    req.setTimeout(45000, () => req.destroy(new Error(`timeout: ${url}`)));
  });
}

/** 抽取 ESM 里的静态与动态 import 说明符 */
function importSpecifiers(code) {
  const specs = new Set();
  const patterns = [
    /(?:^|[\s;}])(?:import|export)\s[^;'"]*?from\s*['"]([^'"]+)['"]/g,
    /(?:^|[\s;}])import\s*['"]([^'"]+)['"]/g,
    /import\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  ];
  for (const re of patterns) {
    for (const m of code.matchAll(re)) specs.add(m[1]);
  }
  return [...specs];
}

const written = [];
const skippedBare = new Set();
const failures = [];

async function fetchAddon(relPath) {
  const localPath = path.join(VENDOR, 'three', 'addons', relPath);
  if (fs.existsSync(localPath)) return; // 已抓过
  const url = JSM_REMOTE + relPath;
  let buf;
  try {
    buf = await get(url);
  } catch (e) {
    failures.push(`${relPath}: ${e.message}`);
    return;
  }
  fs.mkdirSync(path.dirname(localPath), { recursive: true });
  fs.writeFileSync(localPath, buf);
  written.push({ remote: url, local: path.relative(process.cwd(), localPath), bytes: buf.length });

  const code = buf.toString('utf8');
  for (const spec of importSpecifiers(code)) {
    if (spec === 'three') continue;
    // 裸前缀 three/addons/xxx 由 importmap 前缀映射到 addons 根，同样要落盘
    let target = null;
    if (spec.startsWith('three/addons/')) target = spec.slice('three/addons/'.length);
    else if (spec.startsWith('.')) {
      target = path.posix.normalize(path.posix.join(path.posix.dirname(relPath), spec));
    } else if (spec.startsWith('/')) {
      failures.push(`${relPath}: 无法本地化的绝对 import ${spec}`);
      continue;
    } else {
      skippedBare.add(spec);
      continue;
    }
    if (!target.endsWith('.js')) {
      skippedBare.add(`${spec} (非 js，未处理)`);
      continue;
    }
    await fetchAddon(target);
  }
}

(async () => {
  fs.mkdirSync(VENDOR, { recursive: true });

  // 1) three 核心（自包含，无 import）
  const threeBuf = await get(`${THREE_BASE}build/three.module.js`);
  const threeLocal = path.join(VENDOR, 'three', 'three.module.js');
  fs.mkdirSync(path.dirname(threeLocal), { recursive: true });
  fs.writeFileSync(threeLocal, threeBuf);
  written.push({
    remote: `${THREE_BASE}build/three.module.js`,
    local: path.relative(process.cwd(), threeLocal),
    bytes: threeBuf.length,
  });

  // 2) addons 相对 import 闭包
  for (const entry of ADDON_ENTRIES) await fetchAddon(entry);

  // 3) supabase-js UMD（自包含，无 import；挂在 window.supabase）
  const sbRemote = `https://cdn.jsdelivr.net/npm/@supabase/supabase-js@${SUPABASE_VERSION}/dist/umd/supabase.js`;
  const sbBuf = await get(sbRemote);
  const sbLocal = path.join(VENDOR, 'supabase', 'supabase-js.umd.js');
  fs.mkdirSync(path.dirname(sbLocal), { recursive: true });
  fs.writeFileSync(sbLocal, sbBuf);
  written.push({ remote: sbRemote, local: path.relative(process.cwd(), sbLocal), bytes: sbBuf.length });

  // 4) 自检：落盘文件里不允许再出现外部 http(s) import
  const external = [];
  for (const w of written) {
    const code = fs.readFileSync(w.local, 'utf8');
    for (const spec of importSpecifiers(code)) {
      if (/^(https?:)?\/\//.test(spec)) external.push(`${w.local} -> ${spec}`);
    }
  }

  const manifest = {
    generatedBy: 'tools/vendor_frontend_deps.mjs',
    three: THREE_VERSION,
    supabaseJs: SUPABASE_VERSION,
    files: written.map((w) => ({ local: w.local.replace(/\\/g, '/'), bytes: w.bytes, remote: w.remote })),
  };
  fs.writeFileSync(path.join(VENDOR, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');

  const total = written.reduce((s, w) => s + w.bytes, 0);
  console.log(`downloaded files: ${written.length}`);
  console.log(`total bytes: ${total} (${(total / 1024 / 1024).toFixed(2)} MB)`);
  console.log('external imports remaining:', external.length ? external : 'NONE');
  console.log('unhandled bare specifiers:', skippedBare.size ? [...skippedBare] : 'NONE');
  console.log('failures:', failures.length ? failures : 'NONE');
  console.log('\naddons tree:');
  for (const w of written.filter((w) => w.local.includes('addons'))) {
    console.log(`  ${w.local.replace(/\\/g, '/')}  ${w.bytes} B`);
  }
})().catch((e) => {
  console.error('FATAL:', e.message);
  process.exit(1);
});
