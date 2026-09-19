# 部署到 Hugging Face Spaces（Docker 模式）

本文档是 **Streamlit Community Cloud 的替代方案**。如果你还在纠结用哪个平台，
先看下面的原因说明。

---

## 为什么不能继续用 Streamlit Community Cloud

3D 场景在 Cloud 上完全空白，浏览器 Console 报：

```
/app/static/vendor/three/three.module.js
  Failed to load module script: Expected a JavaScript-or-Wasm module script
  but the server responded with a MIME type of "text/html"
```

服务器返回 `text/html` 说明请求**没被静态文件处理器接住**，而是落到了 SPA
兜底路由返回了 `index.html`。受影响的是全部静态资源：

| 资源 | 后果 |
|---|---|
| `three.module.js` + 7 个 addons | module script 整体不执行 → 场景全空、遮罩卡在 0% |
| `ponder.js` / `ponder.css` | 思索按钮与全部课件缺失 |
| `supabase-js.umd.js` | 云数据库客户端缺失 |
| 26 个 `*.glb` | 模型全部加载失败 |

**这无法通过改仓库修复**：`.streamlit/config.toml` 已正确提交、位置也符合要求，
但 Streamlit Community Cloud 不支持 `server.enableStaticServing`。
参考同类报告：[静态文件服务不工作](https://discuss.streamlit.io/t/static-file-serving-not-working-also-not-the-example-from-the-documentation/73525)、
[`<img src="app/static/...>` 不工作](https://discuss.streamlit.io/t/img-src-app-static-does-not-work/119356)。

**Hugging Face Spaces 的 Docker 模式**在我们自己的容器里跑 `streamlit run`，
静态文件服务正常生效 —— **应用代码一行都不用改**。

---

## 部署步骤

### 1. 新建 Space

访问 <https://huggingface.co/new-space>：

| 字段 | 填什么 |
|---|---|
| Owner | 你的账号 |
| Space name | `digital-twin-tool` |
| License | 随意（例如 `mit`） |
| **Select the Space SDK** | **Docker** → **Blank** |
| Space hardware | CPU basic（免费） |
| Visibility | Public 或 Private 都可以 |

### 2. 关联仓库

本仓库根目录已包含全部必需文件：

```
Dockerfile                  ← Docker 镜像定义
.dockerignore               ← 排除 data/ 与 venv/（关键，否则上传 10 GB）
docker-entrypoint.sh        ← 启动时用环境变量生成 secrets.toml，再启动 Streamlit
README.md                   ← 已带 Spaces 所需的 YAML 头部
.streamlit/config.toml      ← enableStaticServing = true（静态服务的前提）
```

把代码推到 Space 仓库：

```bash
# 建议把 Space 加为第二个 remote，GitHub 保持不动
git remote add space https://huggingface.co/spaces/<你的用户名>/digital-twin-tool

git push space main
```

> 首次推送会要求认证。Hugging Face 用 **Access Token** 作为密码：
> 在 <https://huggingface.co/settings/tokens> 建一个 **Write** 权限的 token，
> 用户名填你的 HF 用户名，密码位置粘贴 token。
> （这一点和 GitHub 不一样 —— GitHub 不接受密码，HF 接受 token。）

推送后 Spaces 会自动开始构建。

### 3. 配置 Secrets（否则 Supabase / 高德功能降级）

Space 页面 → **Settings** → **Variables and secrets** → 逐个 **New secret** 添加：

| Name | Value |
|---|---|
| `SUPABASE_URL` | 你的 Supabase 项目地址 |
| `SUPABASE_ANON_KEY` | Supabase anon 公钥 |
| `AMAP_API_KEY` | 高德 Web 服务 key |
| `AMAP_SECRET_KEY` | 高德签名私钥 |

值从本地 `.streamlit/secrets.toml` 复制。

**机制说明**：HF Spaces 的 secret 会以**环境变量**注入容器，而应用读的是
`st.secrets`。`docker-entrypoint.sh` 负责转换：启动时把这些环境变量写成
`/app/.streamlit/secrets.toml`（Streamlit 会从工作目录读取该文件）。

这样做的好处是 **`secrets.toml` 不会被写进镜像层** —— 它在 `.dockerignore` 里，
密钥只存在于运行中的容器内。

四个键都是可选的，缺哪个就降级哪块功能：

| 缺失的键 | 影响 |
|---|---|
| `SUPABASE_*` | 云数据库功能关闭（发布到公共库、Realtime 订阅），其余正常 |
| `AMAP_*` | 高德 POI 请求跳过，其余站点数据功能正常 |

### 4. 等待构建并访问

首次构建约 3–6 分钟（主要耗在 pip 安装 pandas / plotly / numpy 等）。
构建完成后 Space 页面会显示应用，端口是 7860。

---

## 部署后自检清单

| 检查项 | 期望结果 | 若不符 |
|---|---|---|
| 页面能打开 | 三栏驾驶舱布局 | 看 Space 的 **Logs** 标签 |
| **3D 场景有物体** | 充电桩、建筑、树都是真实模型 | 看浏览器 F12 → Console 有无 MIME 报错 |
| 💭 按钮存在 | 右下角有省略号气泡 | 静态资源没生效 |
| 点 💭 出课程目录 | 列出 11 门课件 | 看 Console 的 `[思索]` 日志 |
| 点充电桩 → 气泡 | 第一门是「🔧 拆开一台直流快充桩」 | —— |
| 分镜墙可拖拽 | 每个分镜能拖动旋转 | 需要 WebGL，检查浏览器是否禁用 |
| 时间轴按钮 | 隐藏（`data/` 未部署） | 这是预期行为 |

**核心判据**：Console 里应该出现

```
📦 模型源: /app/static/models/
✅ 模型加载成功: /app/static/models/xxx.glb
```

如果看到 `⚠️ 模型加载失败` 或 MIME 报错，说明静态服务仍未生效。

---

## 已知限制

- **无 `data/`**：时间轴回放、真实站点数据不可用（自动降级为模拟数据）
- **无 torch**：LSTM 预测降级为模拟数据（`requirements.txt` 里刻意移除了 502 MB 的 torch）
- **免费层会休眠**：闲置一段时间后首次访问需等待约 30 秒唤醒。Streamlit Cloud 同样如此
- **不支持 `torch`**：如确需 LSTM，可在 `requirements.txt` 里恢复 torch，但镜像会增大约 2 GB，
  免费层的构建时间和磁盘配额可能吃紧

---

## 与 Streamlit Cloud 的对比

| | Streamlit Cloud | HF Spaces (Docker) |
|---|---|---|
| 静态文件服务 | ❌ 不支持 | ✅ 支持 |
| 3D 场景 / 思索 | ❌ 全空 | ✅ 完整可用 |
| 应用代码改动 | —— | **不需要** |
| 免费层休眠 | 有 | 有 |
| 国内访问 | 一般 | 较好 |
| 密钥注入 | Secrets UI → `st.secrets` | Secrets → 环境变量 → 入口脚本转换 |

Streamlit Cloud 上的实例可以保留不动，两个并行对比后再决定。

---

## 常见问题

**构建失败，提示 `no space left on device`**
`.dockerignore` 没生效，把 `data/`（8.8 GB）传上去了。确认该文件已提交，
且 `data/` 行存在。构建上下文应当只有约 **6 MB**。

**构建成功但页面 404**
`app_port` 与 Streamlit 监听端口不一致。`README.md` 头部是 `app_port: 7860`，
`Dockerfile` 里 `EXPOSE 7860`、入口脚本 `--server.port 7860`，三者必须一致。

**`entrypoint.sh: not found` 或 `bad interpreter`**
`docker-entrypoint.sh` 的换行符变成了 CRLF。本仓库里它是纯 LF，
如果你在 Windows 上编辑过，需要改回 LF（Git 可用 `.gitattributes` 强制）。
本仓库已在 `.gitattributes` 中固定该文件的换行符。

**`st.secrets` 报找不到键**
去 Space 的 Settings 检查 secret 名称拼写是否完全一致（区分大小写），
改完需要 **Restart Space**（环境变量只在启动时读取）。
