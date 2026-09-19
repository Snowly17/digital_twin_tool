---
title: 智孪 · 数字孪生快速建模工具
emoji: 🏗️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 智孪 · 数字孪生轻量化快速建模工具

面向**数字孪生方向从业者**的单页 Streamlit 应用：在浏览器里快速搭出一个可交互的
3D 数字孪生场景，并通过「思索 · 交互式教学」解释它背后的概念与数据链路。

> 上面的 YAML 头部供 **Hugging Face Spaces** 识别（Docker 模式）。
> GitHub 会把它渲染成一张小表格，不影响阅读。

---

## 它是什么

一个驾驶舱式三栏界面：

- **左栏**：资产树、组件库、生成规则、GeoJSON 导入
- **中栏**：Three.js 3D 场景（拖拽/缩放/选中/编辑），支持多种装置模型
- **右栏**：属性编辑、数据绑定、碳减排测算、思索教学

核心特色是 **💭 思索 · 交互式教学**：借鉴 Minecraft Create 模组的 Ponder，
用「多分镜故事板 + 真实装置模型 + 拆解/组装动画」讲清楚一个概念。

- **11 门课件**，**55 个分镜**，**379 个动作**
- 两个作用域：`tool`（工具类课程，留在右侧面板）与 `object`（对象强相关课程，以气泡形式跟随物体）
- 点选场景里的充电桩，气泡直接给出「🔧 拆开一台直流快充桩」这类强相关课程

课件清单：

| 课件 | 类型 |
|---|---|
| 从 0 到一个活着的孪生体 | 通用 |
| 这个孪生体的四层架构 | 通用 |
| 物理世界怎么变成孪生世界 | 通用 |
| 孪生体排查手册 | 通用 |
| 让孪生体真正控制物理世界 | 通用 |
| 拆开一台直流快充桩 | 装置·拆解组装 |
| 储能集装箱的内部 | 装置·拆解组装 |
| 光储充微电网怎么连起来 | 装置·拓扑 |
| 数字怎么变成颜色 | 可视化 |
| 光伏阵列：板子只是开始 | 装置 |
| 风机：机械能怎么变成电 | 装置 |

---

## 快速开始

```bash
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
streamlit run app.py
```

打开 <http://localhost:8501>，点右下角 **💭** 按钮进入思索。

> **默认不装 torch**（约 500 MB）。缺失时 LSTM 预测自动降级为模拟数据，
> 其余功能不受影响。需要真实预测时：`pip install -r requirements-lstm.txt`

> **`data/` 数据集（约 8.8 GB）不在仓库里**。缺失时应用照常启动，
> 仅「时间轴回放」与「真实站点数据」两块降级。恢复方式见 `DEPLOY.md`。

---

## 部署

### 先选平台（这一步决定了 3D 场景能不能用）

| 平台 | 费用 | 3D 场景 / 思索 | 说明 |
|---|---|---|---|
| Streamlit Community Cloud | 免费 | ❌ **全空** | 不支持 `server.enableStaticServing`，静态资源全部被 MIME 检查拒绝。改代码无法修复 |
| **Railway**（推荐） | 约 $3/月 | ✅ 完整可用 | 不休眠、响应快；有 $5 试用额度 |
| Hugging Face Spaces | 有免费层 | ✅ 完整可用 | 会休眠（约 30 秒冷启动）；主站国内访问受限 |
| 本地运行 | 免费 | ✅ 完整可用 | `streamlit run app.py` |

Railway 与 HF Spaces 使用**同一套 Docker 文件**，应用代码零改动。
平台选择的原因详见 **[DEPLOY.md](DEPLOY.md)** 坑 ①。

### 各平台文档

| 文档 | 内容 |
|---|---|
| **[DEPLOY.md](DEPLOY.md)** | 总览：仓库体积控制、四个真实踩过的坑、Streamlit Cloud 限制说明 |
| **[DEPLOY-RAILWAY.md](DEPLOY-RAILWAY.md)** | Railway 部署步骤、**费用预估与省额度操作**、常见问题 |
| **[DEPLOY-HF.md](DEPLOY-HF.md)** | Hugging Face Spaces 部署步骤 |

### 常用自检

```bash
# 提交到公开仓库前扫一遍明文凭据（应无「高危」命中）
python tools/scan_creds.py
```

部署后必看：浏览器 F12 → Console 应出现 `📦 模型源:` 和 `✅ 模型加载成功:`。
看到 `MIME type of "text/html"` 说明静态服务没生效。

---

## 项目结构

```
app.py                    单页应用主体（约 11.6k 行）
│                         3D 场景 HTML 由 f-string 生成后经 st.components 注入
├── core/                 业务核心
│   ├── ponder_library.py   思索课件数据（纯 dict，数据驱动）
│   ├── console.py          控制台编码健壮化（见 DEPLOY.md 坑 ④）
│   ├── model_predictor.py  LSTM 预测器（延迟导入 torch）
│   ├── mqtt_client.py      MQTT 上下行闭环控制
│   ├── scene_manager.py    场景序列化 / Supabase 同步
│   └── ...                 生成规则、关系构建、碳排计算、资产库等
├── common/               数据处理与 LSTM 工具
├── static/
│   ├── js/ponder.js        思索运行时（故事板播放器，约 101 KB）
│   ├── css/ponder.css      思索样式
│   ├── models/*.glb        26 个 3D 装置模型
│   └── vendor/             three.js / supabase-js（本地化，断网可用）
├── tools/                验证与生成工具（见下）
├── mock_data/            模拟数据源（REST / MQTT / DB / Influx）
└── .streamlit/config.toml  必须提交！enableStaticServing 是加载 GLB 的前提
```

---

## 验证工具

项目自带四个不依赖浏览器的验证/生成脚本：

```bash
# 1) 课件数据自检（资产存在性、零件引用、连线端点、id 唯一性…）
python -c "from core.ponder_library import get_diagnostics as g; print(g()['problems'] or '无问题')"

# 2) 导出思索数据岛
python tools/export_payload.py

# 3) 逐帧验证 11 门课件（真实 three.js + DOM 桩，含交互断言）
node tools/verify_ponder.cjs .tmp/ponder_payload.json

# 4) 量取任意 GLB 的包围盒（排装置布局用）
python tools/inspect_glb.py
```

第 3 项会输出每门课的节点/分镜/动作/对照/相机比例，以及拖拽旋转与滚轮缩放是否生效。

### 重新生成充电桩模型

`static/models/charger_dc.glb` 由脚本生成（项目原本没有充电桩模型），
零依赖（仅 numpy + 标准库）手写 GLB 导出：

```bash
python tools/make_charger_glb.py
```

---

## 技术要点

- **单页 Streamlit + iframe 注入**：3D 场景是一整段 HTML，每次 rerun 都会重建 iframe，
  因此思索的播放状态全部保存在浏览器侧 `localStorage`
- **多分镜各自持有独立的** `THREE.Scene` / `PerspectiveCamera` / `WebGLRenderer`，
  模块级 `lab` 指针按分镜切换，所有绘制原语与分镜无关
- **数据驱动**：课件是 `core/ponder_library.py` 里的纯 dict，新增课程不用改渲染代码
- **静态资源自托管**：three.js 与 GLB 全部本地化，不依赖 CDN
