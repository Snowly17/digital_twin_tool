# 部署到 Railway

本文档是用 **Docker 部署** Streamlit 应用的平台指南。适用于这种情况：

- Streamlit Community Cloud 上 **3D 场景全空、遮罩卡在「加载模型中… 0%」**
- 原因是该平台不支持 `server.enableStaticServing`（详见 [DEPLOY.md](DEPLOY.md) 坑 ①）
- 需要在**自己控制服务器**的环境里跑，让静态文件服务正常生效

Railway 与 Hugging Face Spaces 用的是**同一套 Docker 文件**，
区别只在于端口：Railway 用平台分配的 `$PORT`，HF Spaces 用 7860。
本仓库的入口脚本已同时兼容两者。

---

## 一、为什么在 Railway 上必须显式传 `--server.port`

这是 Railway 部署 Streamlit 最常见的翻车点，**官方模板专门为此做过修复**：

> Its start command is a bare `streamlit run streamlit_app.py`.
> Streamlit then listens on its own default port, which is not the port the
> platform assigns and routes to. The template does set a `PORT` variable —
> but **Streamlit does not read `PORT`**; it wants `--server.port` or
> `STREAMLIT_SERVER_PORT`.
>
> —— [Railway 官方模板说明](https://railway.com/deploy/llamaindex-apps-streamlit-on-the-port-the-platform-assigns--llamaindex-apps-or-streamlit-on-the-port)

所以正确写法是：

```sh
streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
```

本仓库的 `docker-entrypoint.sh` 已经这样处理：

```sh
PORT="${PORT:-7860}"        # Railway 给 $PORT；HF Spaces 没有则用 7860
exec streamlit run app.py \
    --server.port "$PORT" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
```

`--server.headless true` 在容器里同样必要：否则 Streamlit 会尝试打开浏览器、
首次运行还会提示输入邮箱。

---

## 二、部署步骤

### 1. 新建项目

1. 访问 <https://railway.com/new>
2. 选择 **Deploy from GitHub repo**
3. 首次使用需要授权 Railway 访问你的 GitHub 账号
4. 选中 `digital_twin_tool` 仓库

Railway 会自动识别根目录的 `Dockerfile` 并构建。
根目录的 `railway.json` 已经声明了构建方式和健康检查：

```json
{
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "sh /app/docker-entrypoint.sh",
    "healthcheckPath": "/_stcore/health",
    "healthcheckTimeout": 120
  }
}
```

### 2. 配置环境变量（凭据）

项目页面 → **Variables** → **New Variable**，逐个添加：

| 变量名 | 说明 |
|---|---|
| `SUPABASE_URL` | Supabase 项目地址 |
| `SUPABASE_ANON_KEY` | Supabase anon 公钥 |
| `AMAP_API_KEY` | 高德 Web 服务 key |
| `AMAP_SECRET_KEY` | 高德签名私钥 |

值从本地 `.streamlit/secrets.toml` 复制。

**机制说明**：Railway 把变量作为**环境变量**注入容器，而应用读的是 `st.secrets`。
`docker-entrypoint.sh` 负责转换 —— 启动时把它们写成 `/app/.streamlit/secrets.toml`
（Streamlit 从工作目录读取该文件）。这样密钥**不会进镜像层**
（`.streamlit/secrets.toml` 在 `.dockerignore` 里）。

四个键都是可选的，缺哪个就降级哪块功能：

| 缺失 | 影响 |
|---|---|
| `SUPABASE_*` | 云数据库功能关闭（发布公共库、Realtime 订阅），其余正常 |
| `AMAP_*` | 高德 POI 请求跳过，其余站点数据功能正常 |

### 3. 生成访问域名

**Settings** → **Networking** → **Generate Domain**。

Railway 不会默认分配域名，这一步不做的话部署成功也访问不到。

### 4. 等待构建

首次构建约 3–6 分钟（主要耗在 pip 安装 pandas / plotly / numpy）。
在 **Deployments** 标签可以看到实时日志。

---

## 三、部署后自检

打开 Railway 分配的域名，逐项确认：

| 检查项 | 期望结果 | 若不符 |
|---|---|---|
| 页面能打开 | 三栏驾驶舱布局 | 看 Deployments 日志 |
| **3D 场景有物体** | 充电桩/建筑/树是真实模型 | 看 F12 → Console 有无 MIME 报错 |
| 💭 按钮存在 | 右下角省略号气泡 | 静态资源没生效 |
| 点 💭 出课程目录 | 列出 11 门课件 | 看 Console 的 `[思索]` 日志 |
| 分镜墙可拖拽 | 每个分镜能拖动旋转 | 需 WebGL |

**最关键的判据** —— Console 里应该出现：

```
📦 模型源: /app/static/models/
✅ 模型加载成功: /app/static/models/xxx.glb
```

出现 `Failed to load module script ... MIME type of "text/html"` 说明静态服务仍未生效。

**容器日志判据** —— 应该看到：

```
[entrypoint] 已写入 SUPABASE_URL
[entrypoint] 已写入 SUPABASE_ANON_KEY
[entrypoint] secrets.toml 生成完毕: /app/.streamlit/secrets.toml
[entrypoint] 启动 Streamlit，端口 8080 ...
```

如果显示 `未提供 xxx（相关功能将自动降级）`，说明 Variables 没设对（注意大小写）。

---

## 四、费用预估与省额度操作

### 4.1 单价与套餐

Railway 按秒计费，**停止的服务不计费**。

| 资源 | 单价 |
|---|---|
| 内存 | **$10 / GB / 月** |
| CPU | **$20 / vCPU / 月** |
| 出网流量 | $0.05 / GB |

| 套餐 | 费用 | 包含额度 | 单服务内存上限 |
|---|---|---|---|
| Free Trial | $0 | **$5 一次性，30 天过期** | 1 GB |
| Free | $0/月 | **$1 / 月**（每月重置、不累积） | 0.5 GB |
| Hobby | $5/月 | $5 / 月 | 48 GB |

### 4.2 本项目的实际消耗（实测）

内存是关键成本 —— **只要容器在跑就持续计费**。实测数据：

| 阶段 | 内存 |
|---|---|
| Python 基线 | 13.2 MB |
| + streamlit / pandas / numpy / plotly | 117.9 MB |
| + core 各模块 | 118.9 MB |
| + 导入 app.py | 141.4 MB |
| + 生成场景 HTML | **149.8 MB** |
| 峰值 | 176.5 MB |

线上没有 `data/`（不加载那个 73 MB 的数据集），加上 Streamlit 运行时开销，
**稳态约 200–350 MB**。

据此估算（按 0.25 GB 计）：

| 项目 | 每月 |
|---|---|
| 内存 `0.25 GB × $10` | $2.50 |
| CPU（空闲 Streamlit 约 1–3%） | $0.40 |
| 出网流量（每月约 20 次访问） | ≈ $0.01 |
| **合计** | **约 $3 / 月** |

### 4.3 结论：$5 够不够

| 用途 | 够不够 |
|---|---|
| 演示 / 答辩 / 短期展示（几周内） | ✅ **够，用不完** |
| 长期 24 小时挂着 | ❌ 约 1.5～2 个月后耗尽（且额度 30 天就过期） |
| 偶尔演示 | ✅ 配合下面「不用时停服」，$1/月的 Free 套餐也能撑很久 |

> ⚠️ 免费试用的 $5 是**一次性、30 天过期**的券，不是每月重置。用不完也作废。

### 4.4 省额度：不用时停服

这是偶尔演示场景下最有效的省钱手段 —— **停止的服务完全不产生费用**。

**方式一：Dashboard（最直观）**

1. 打开 Railway 项目页面
2. 选中你的服务卡片
3. 右上角 **⋮** 或 Settings → 找到 **Pause** / **Stop**（暂停即停止计费）
4. 要用时点 **Resume** / **Deploy**

**方式二：CLI（适合写进脚本）**

```bash
npm install -g @railway/cli     # 首次需要安装
railway login                    # 浏览器授权

# 在项目目录里关联服务
railway link

# 停服（停止计费）
railway down

# 重新启动
railway up

# 查看当前状态
railway status
```

**建议的日常节奏**

| 场景 | 做法 |
|---|---|
| 明天要演示 | 今晚 `railway up`，演示完 `railway down` |
| 一整周都要用 | 保持运行，睡前停、早上启 |
| 长期挂线上 | 保持运行，直接上 Hobby（$5/月额度基本覆盖） |

**停服的副作用（本项目都可接受）**

- 下一个人访问时，需要先 `railway up` 才能打开（不会自动唤醒）
- 域名不变，重新启动后还是同一个地址
- `st.session_state` 里的场景状态会丢失 —— 本来就是每次会话重建，无影响
- 容器文件系统重置 —— 本项目不持久化任何数据，无影响
- 重新启动后**环境变量（Variables）仍然保留**，不需要重新配置

### 4.5 必须做的一件事：设 Spending Limit

在 Railway 的 **Settings → Usage** 里设置月度消费上限（例如 $3）。
超过上限会自动停服，而不是继续扣费。

> 这是防止意外账单的**唯一可靠手段**。没设上限时，如果服务一直开着、
> 或有人频繁访问，费用会持续累积。

---

## 五、Railway 的特性与限制

- **不会休眠**：Railway **不**因闲置而休眠（和 HF Spaces 不同），所以响应快、
  无冷启动，但也就意味着持续计费。想省额度就按 4.4 节手动停服
- **数据不持久**：容器文件系统是临时的，重新部署或停服重启都会重置。
  本项目不需要持久化（`data/` 是可选数据集），无影响
- **单副本**：本项目用 `st.session_state` 保存场景状态，不要开多副本
  （Free Trial 上限 2 个副本、Free 上限 1 个，保持默认即可）

---

## 六、已知限制

- **无 `data/`**：时间轴回放、真实站点数据不可用（自动降级为模拟数据）
- **无 torch**：LSTM 预测降级为模拟数据（`requirements.txt` 刻意移除了 502 MB 的 torch）
- **MQTT 闭环**：需在你自己的机器上运行 `python mock_mqtt_publisher.py`
  （独立脚本，指向公共 Broker `broker.emqx.io`）

---

## 七、常见问题

**部署显示成功，但网站打不开 / 健康检查 0%**
端口没绑对。确认 `docker-entrypoint.sh` 里是 `--server.port "$PORT"`，
而不是硬编码的端口号。这是 Railway 上 Streamlit 最常见的失败原因。

**停服之后网站打不开了 —— 怎么恢复？**
这是预期行为，停止的服务不会自动唤醒。去 Dashboard 点 **Resume**，
或命令行 `railway up`。域名和环境变量都保留，不需要重新配置。

**怎么知道已经花了多少额度？**
Dashboard 右上角账号菜单 → **Usage**，可以看到当前计费周期的实时用量。
同时建议在 **Settings → Usage** 里设 Spending Limit（见 4.5 节）。

**构建失败，提示磁盘不足**
`.dockerignore` 没生效，把 `data/`（8.8 GB）传上去了。
构建上下文应当只有约 **6 MB**。

**日志里 `secrets.toml 不可写`**
镜像里 `/app/.streamlit` 权限不足。Dockerfile 已加
`mkdir -p /app/.streamlit && chmod -R a+w /app/.streamlit`，
若你改过 Dockerfile 请保留这两条。

注意：即使这一步失败，**应用仍会正常启动**（入口脚本刻意没用 `set -e`），
只是 Supabase / 高德功能会降级。

**改了代码但线上没更新**
Railway 默认在 push 到关联分支时自动重新部署。若是手动部署，
去 Deployments 点 **Redeploy**。另外浏览器可能缓存了旧的 `ponder.js`
（URL 带 `?v=` 会击穿缓存，但强刷一次更稳）。

**`bad interpreter` 或 `not found`**
`docker-entrypoint.sh` 的换行符变成了 CRLF。本仓库用 `.gitattributes`
固定为 LF；若你在 Windows 上编辑过请改回 LF。
