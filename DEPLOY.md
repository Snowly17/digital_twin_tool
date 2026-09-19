# 部署指南：GitHub + Streamlit Community Cloud

本文档只讲**怎么把这个项目部署出去**，以及路上会撞到的三个坑。
所有数字都来自本仓库实测，不是估的。

---

## 0. 先看结论：仓库里能放什么

| 内容 | 体积 | 是否提交 | 说明 |
|---|---|---|---|
| 源码（`app.py` / `core/` / `component/` …） | ~1.5 MB | ✅ 提交 | 核心 |
| `static/models/*.glb`（26 个模型） | 4.1 MB（含 `CREDITS.md`） | ✅ 提交 | 3D 场景必需 |
| `static/vendor/`（three.js / supabase-js） | 1.7 MB | ✅ 提交 | 断网可用，需保留 |
| `static/js`、`static/css`（思索运行时） | ~120 KB | ✅ 提交 | 必需 |
| `checkpoints/station_lstm.pth` | 60 KB | ✅ 提交 | LSTM 权重（用需装 torch） |
| `core/console.py` | 3 KB | ✅ 提交 | 控制台编码健壮化（坑 ④） |
| `.streamlit/config.toml` | 337 B | ✅ **必须提交** | 见坑 ① |
| `.streamlit/secrets.toml` | 127 B | ❌ 禁止 | 含 Supabase 密钥，已在 `.gitignore` |
| `.env` | — | ❌ 禁止 | 已加入 `.gitignore`（依赖含 python-dotenv） |
| `exports/`（场景备份 JSON、app.py 备份） | 3.5 MB | ❌ 排除 | 开发过程产物，含完整场景数据 |
| `data/`（数据集） | **8.8 GB** | ❌ 排除 | 见坑 ② |
| `venv/` | 1.2 GB | ❌ 已在 `.gitignore` | |
| `requirements-lstm.txt` | 810 B | ❌ 排除 | 云端只认一个依赖文件，见坑 ③ |

**提交后仓库实际大小约 7 MB**，完全在 GitHub 与 Streamlit Cloud 的舒适区内。

---

## 1. 四个必须知道的坑

### 坑 ① `.streamlit/` 不能整个忽略（否则线上模型全挂）

`.streamlit/config.toml` 里有：

```toml
[server]
enableStaticServing = true
```

**它决定了 `/app/static/*`（three.js、26 个 GLB、思索运行时）能不能被访问。**

本仓库原本的 `.gitignore` 里写的是 `/.streamlit/`，把 `config.toml` 一起忽略了——
已修正为只忽略 `**/secrets.toml`。

> 官方也要求：Community Cloud 只识别**仓库根目录**下的 `.streamlit/config.toml`
> （见 [Status and limitations](https://docs.streamlit.io/deploy/streamlit-community-cloud/status#repository-file-structure)）。

#### ⚠️ 但更要紧的是：这个选项在 Streamlit Cloud 上不生效

**实测结论（2026-09）**：即使 `config.toml` 正确提交、位置也合规，
**Streamlit Community Cloud 仍然不支持 `server.enableStaticServing`**。
请求 `/app/static/*` 会落到 SPA 兜底路由、返回 `index.html`（`text/html`），
于是浏览器按 MIME 检查全部拒绝：

```
/app/static/vendor/three/three.module.js
  Failed to load module script: Expected a JavaScript-or-Wasm module script
  but the server responded with a MIME type of "text/html"
```

连带后果：three.js 全部 8 个模块加载失败 → module script 不执行 →
**3D 场景全空、加载遮罩永久卡在 0%**；`ponder.js` / `ponder.css` /
`supabase-js` / 26 个 GLB 同样失效。**思索按钮也不会出现。**

这不是仓库配置问题，改代码解决不了。同类报告：
[静态文件服务不工作](https://discuss.streamlit.io/t/static-file-serving-not-working-also-not-the-example-from-the-documentation/73525)、
[`<img src="app/static/...>` 不工作](https://discuss.streamlit.io/t/img-src-app-static-does-not-work/119356)。

**解决办法：改用能自己控制服务器的平台**。本仓库已备好两套配置：

| 平台 | 文档 | 特点 |
|---|---|---|
| **Railway**（推荐） | **[DEPLOY-RAILWAY.md](DEPLOY-RAILWAY.md)** | 不休眠、响应快、按用量计费（有试用额度） |
| Hugging Face Spaces | [DEPLOY-HF.md](DEPLOY-HF.md) | 有免费层，但会休眠；主站在国内访问受限 |

两者用的是**同一套 Docker 文件**（`Dockerfile` + `docker-entrypoint.sh`），
区别只在端口：Railway 用平台分配的 `$PORT`，HF Spaces 用 7860，
入口脚本已同时兼容。**应用代码一行都不用改。**

如果你只需要界面和思索文字部分、不在乎 3D 场景，继续用 Streamlit Cloud 也可以。

### 坑 ② `data/` 有 8.8 GB，不能进 Git

实测构成：

| 文件 | 体积 | 谁在用 |
|---|---|---|
| `data/station-level/features/volume.csv` | 898 MB | LSTM 训练样本 |
| `data/station-level/features/duration.csv` | 822 MB | LSTM 训练样本 |
| `data/station-level/features/s_price.csv` | 513 MB | LSTM 训练样本 |
| `data/station-level/features/e_price.csv` | 326 MB | LSTM 训练样本 |
| `data/station-level/features/occupancy.csv` | 290 MB | LSTM 训练样本 |
| `data/station-level/station_occupancy_1h.csv` | **73 MB** | **app.py 唯一直接读取的数据文件** |
| 其余（zone-level 等） | 合计 ~50 MB | 建模脚本 |

GitHub 单文件上限 100 MB、仓库建议 <1 GB；Streamlit Cloud 也不适合携带这个体量。
所以 **`data/` 整个目录排除**。

**关键点：删掉数据后应用仍能正常启动。** 降级链是验证过的：

```
core/model_predictor.py
  ├─ station_inf.csv 存在？ → 用它列出站点
  ├─ 否则 occ_df 存在？    → 从 occupancy 列名推断站点（限 100 个）
  └─ 都没有               → get_station_list() 返回 []，实时数据走 mock 分支
                            （_generate_mock_history / _predict_mock）
app.py
  └─ _occupancy_header() 发现文件不存在 → 返回 None → 时间轴面板隐藏
```

除了"时间轴回放"和"真实站点数据"两块，其余功能（3D 建模、思索全部 11 门课件、
碳减排测算、MQTT 闭环控制）**都不依赖 `data/`**。

### 坑 ③ torch 占 500 MB，默认不装

实测 `venv/Lib/site-packages/torch` = **502 MB**（加上 torchvision/torchaudio 约 511 MB），
是整个依赖里最重的一块，而它只被 `core/model_predictor.py` 使用。

该模块是**延迟加载**的（`app.py` 里经 `_get_predictor()` 按需导入，外层有
`try/except (ImportError, ModuleNotFoundError)` 兜底），所以：

- 从 `requirements.txt` 移除 torch → 应用照常启动，LSTM 预测降级为模拟数据
- 需要本地跑 LSTM 时：`pip install -r requirements-lstm.txt`

> Community Cloud 只使用**一个**依赖文件（优先入口文件同目录，其次仓库根目录）。
> 所以 `requirements-lstm.txt` 已加入 `.gitignore`，避免云端误判。

### 坑 ④ emoji print 在非 UTF-8 控制台上会让应用崩溃

项目源码里有 **200+ 处带 emoji 的 `print()`**（`✅ ⚠️ ❌ ℹ️`）。在简体中文 Windows 上，
Python 把 stdout 编码设为控制台代码页 GBK（cp936），而 GBK 编不出 emoji：

```
print("⚠️ 未找到 station_inf.csv")
UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'
```

**危险之处在于触发位置**：这些 print 大量出现在「文件缺失」的降级分支里，
也就是本文档推荐的「不带 `data/` 运行」路径上。本该只打一句警告，实际会让应用启动失败。

本项目已修复，两处生效点：

| 位置 | 作用 |
|---|---|
| `core/console.py` | `enable_safe_console()`：把 stdout/stderr 的错误策略由 `strict` 放宽为 `replace`，**编码不变** |
| `app.py` 顶部 | Streamlit 入口调用它 |
| `core/path_manager.py` 顶部 | `core.*` / `common.*` 的公共依赖，覆盖最广 |

> UTF-8 环境下（Streamlit Cloud、Linux、macOS）行为**完全不变**——永远走不到 `replace` 分支。
> 只有非 UTF-8 控制台会退化成把 emoji 显示为 `?`，而不再崩溃。
>
> 实测结论（`AppTest` 真实跑 `app.py`）：屏蔽 torch + 屏蔽 `data/` 后，
> 应用启动**零异常**，降级提示正常打印。

---

## 2. 部署到 GitHub

### 2.1 确认忽略规则生效

本项目当前**还不是 git 仓库**，先初始化并检查：

```bash
cd digital_twin_tool
git init
git add -A
git status --short | wc -l        # 看将要提交的文件数（应远小于总文件数）
git status --short | head -30
```

务必确认以下路径**没有**出现在 `git status` 里：

```bash
git status --short | grep -E "venv/|data/|secrets\.toml|\.pytest_tmp"   # 应无输出
```

再反向确认**必须提交**的两个文件在列表里：

```bash
git status --short | grep -E "\.streamlit/config\.toml|static/models/charger_dc\.glb"
```

### 2.2 提交与推送

```bash
git add -A
git commit -m "初始化：数字孪生轻量化快速建模工具（含思索交互式教学引擎）"

# 在 GitHub 上新建一个空仓库（不要勾选 Add README / .gitignore）
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

推送后确认仓库大小合理：

```bash
git count-objects -vH | grep size-pack     # 应在 10 MB 量级
```

### 2.3 如果误提交了大文件

```bash
git rm --cached -r data venv
git commit -m "移除误提交的数据集与虚拟环境"
```

如果已经把大文件推送成功，仓库历史里仍有它们，需要用
[git-filter-repo](https://github.com/newren/git-filter-repo) 或 BFG 清理历史。

---

## 3. 部署到 Streamlit Community Cloud

### 3.1 操作步骤

1. 访问 <https://share.streamlit.io/>，用 GitHub 账号登录
2. **New app** → 选择刚推送的仓库
3. **Main file path** 填 `app.py`
4. 展开 **Advanced settings**：
   - **Python version**：选 **3.12**（本地 venv 实测 3.12.10 + Streamlit 1.63.0，
     与 Cloud 默认版本一致；选错版本只能删除应用后重建，无法原地改）
   - **Secrets**：把本地 `.streamlit/secrets.toml` 的内容**粘贴进去**
5. **Deploy**

> 私有仓库需要授予 `repo` scope，且部署者需有该仓库的管理权限
> （见 [Status and limitations](https://docs.streamlit.io/deploy/streamlit-community-cloud/status#github-oauth-scope)）。

### 3.2 Secrets 配置

本地 `.streamlit/secrets.toml`（**不会**进仓库）内容形如：

```toml
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_xxxx"

# 高德地图 Web 服务 key 与签名私钥（不配则跳过高德 POI 请求，功能自动降级）
AMAP_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
AMAP_SECRET_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

在 Streamlit Cloud 的 Secrets 输入框里**原样粘贴这段 TOML**即可。

> ⚠️ 只填 `anon` 公钥。**不要**把 `service_role` 密钥放进前端可访问的应用——
> 它会绕过行级安全策略。

**不配 Secrets 也能部署**，各功能独立降级：

| 缺失的键 | 影响 |
|---|---|
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | 云数据库功能关闭（发布到公共库、Realtime 订阅）；`core/supabase_client.py` 导入有 `try/except` 保护 |
| `AMAP_API_KEY` / `AMAP_SECRET_KEY` | 高德 POI 请求跳过，`_fetch_from_amap()` 返回空表；其余站点数据功能照常 |

读取逻辑统一在 `core/console.py` 的 `read_secret()`：**st.secrets → 环境变量 → 默认值**。

#### 关于凭据轮换（重要）

本项目曾有 **3 处真实凭据被硬编码在源码里**，已全部改为 `read_secret()` 读取：

| 文件 | 原硬编码内容 |
|---|---|
| `common/data_processor.py` | 高德 `api_key` + `secret_key`（用于请求签名） |
| `test_supabase.py` | Supabase URL + anon key |
| `test_supabase_connection.py` | Supabase URL + anon key |

> **提交前务必确认这些值没被推上去。** 首次提交后再删文件是无效的——
> 密钥会永久留在 Git 历史里，只能去对应平台后台轮换。
>
> 如果这些密钥**曾经**出现在任何已推送的仓库或公开位置，请立即轮换：
> 高德控制台重签 key，Supabase 后台重置 anon key（并检查 RLS 策略）。
>
> 自检命令（提交前跑一次，应无任何"高危"命中）：
> ```bash
> python .tmp/scan_creds.py
> ```

### 3.3 部署后自检清单

打开线上地址，逐项确认：

| 检查项 | 期望结果 | 若不符 |
|---|---|---|
| 页面能打开 | 看到三栏驾驶舱布局 | 看 Cloud 构建日志 |
| **3D 场景有物体** | 能看到建筑/树/充电桩模型 | **在 Streamlit Cloud 上必然失败**，见坑 ①。Console 会报 MIME 错误 |
| 底部 💭 按钮存在 | 是 | 同上，`ponder.js` 被 MIME 检查拒绝 |
| 点 💭 能出课程目录 | 列出 11 门课 | 同上；或看 Console 的 `[思索]` 日志 |
| 点充电桩 → 气泡 | 第一门是「🔧 拆开一台直流快充桩」 | —— |
| 分镜墙可拖拽 | 每个分镜能拖动旋转 | 见下方"已知限制" |
| 时间轴按钮 | 隐藏（因为 `data/` 未提交） | 这是预期行为 |

> ⚠️ **在 Streamlit Community Cloud 上，前四项中的 3D 场景和 💭 按钮一定不会出现**，
> 原因见坑 ① —— 该平台不支持静态文件服务。要完整功能请用
> **[DEPLOY-HF.md](DEPLOY-HF.md)** 的 Hugging Face Spaces 方案。

### 3.4 已知限制（Streamlit Cloud）

- **静态资源全部不可用** —— three.js / 26 个 GLB / `ponder.js` / `ponder.css` / `supabase-js`。
  这是平台限制，**无法通过改仓库修复**。直接后果：
  - 3D 场景全空
  - 加载遮罩「⏳ 加载模型中… 0%」永久卡住
  - 💭 思索按钮不出现
- **时间轴回放不可用** —— 依赖 73 MB 的 `station_occupancy_1h.csv`
- **站点真实数据不可用** —— 依赖 `data/`，自动降级为模拟数据
- **LSTM 预测不可用** —— `requirements.txt` 刻意移除了 torch，降级为模拟数据
- **MQTT 闭环控制需要外部 Broker** —— 需自行运行 `python mock_mqtt_publisher.py`
  （它是独立脚本，可以跑在你自己的机器上，指向公共 Broker `broker.emqx.io`）
- **Community Cloud 会覆盖部分 config.toml 设置** ——
  `client.showErrorDetails`、`runner.fastReruns`、`server.runOnSave`、
  `server.enableXsrfProtection`、`browser.gatherUsageStats` 由平台接管，
  但 `server.enableStaticServing` 与 `[theme]` 会保留
- **应用托管在美国**，且 GitHub 更新有每分钟 5 次的上限

---

## 4. 数据集准备（可选，恢复完整功能）

若要让线上或本地拥有完整功能，需要 `data/`。有两种做法：

### 方案 A：本地放置（推荐）

在项目根目录重建目录并放入数据文件：

```bash
mkdir -p data/station-level/features data/zone-level
# 将 station_occupancy_1h.csv 放到 data/station-level/
# 将 station_inf.csv 放到 data/station-level/features/
# 将 zone-level 各 csv 放到 data/zone-level/
```

最少只需要一个文件即可恢复时间轴：

```
data/station-level/station_occupancy_1h.csv   （约 73 MB）
```

### 方案 B：外部对象存储

把数据放到 Supabase Storage / S3，启动时按需下载到本地缓存目录。
本项目未实现该逻辑；如需可在 `core/path_manager.py` 里统一改写数据根路径。

> LSTM 模型权重 `checkpoints/station_lstm.pth` 只有 **60 KB**，已经随仓库提交，
> 不需要额外准备。但它是 `torch.load()` 格式，**必须装 torch 才能用**（见坑 ③）；
> 同时 `data/` 里的序列数据也是推理输入，两者缺一不可。

---

## 5. 本地开发环境

```bash
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
# 需要 LSTM 预测功能时再装：
# pip install -r requirements-lstm.txt

streamlit run app.py
```

### 本地验证工具

项目自带四个工具脚本，改完代码建议跑一遍：
 
```bash
# 1. 课件数据自检（装置资产/零件引用/连线闭合等）
python -c "import sys; sys.path.insert(0,'.'); from core.ponder_library import get_diagnostics as g; print(g()['problems'] or '无问题')"

# 2. 导出思索数据岛
python tools/export_payload.py

# 3. 无浏览器验证 11 门课（逐帧执行 + 交互断言）
node tools/verify_ponder.cjs .tmp/ponder_payload.json

# 4. 量取任意 GLB 的包围盒（排装置布局用）
python tools/inspect_glb.py
```

### 重新生成充电桩模型

`static/models/charger_dc.glb` 是由脚本生成的（项目原本没有充电桩模型）：

```bash
python tools/make_charger_glb.py
```

修改脚本里的尺寸/材质后重跑即可，产物约 21 KB。

---

## 6. 排障速查

| 症状 | 最可能的原因 |
|---|---|
| **3D 场景全空 + 遮罩卡在「加载模型中… 0%」** | **Streamlit Cloud 不支持静态文件服务**（坑 ①）。Console 会报 MIME 为 `text/html`。改用 [DEPLOY-HF.md](DEPLOY-HF.md) |
| 💭 思索按钮不出现 | 同上一行，`ponder.js` 被 MIME 检查拒绝 |
| Console 报 `MIME type ('text/html') is not executable` | 同上一行。请求落到了 SPA 兜底路由 |
| 构建超时 / 内存不足 | `requirements.txt` 里还在装 torch（坑 ③） |
| `push` 被拒绝，提示文件过大 | `data/` 没排除（坑 ②） |
| 启动报 `UnicodeEncodeError: 'gbk' codec` | 非 UTF-8 控制台 + emoji print（坑 ④），已修 |
| `ModuleNotFoundError: No module named 'torch'` | `_get_predictor()` 的兜底被改坏了（坑 ③），应返回 `None` 而非抛出 |
| 时间轴按钮不见了 | `data/` 未提供，属预期降级 |
| `st.secrets` 报找不到键 | Secrets 没填，或键名拼写不一致 |
| 改完代码线上没变化 | 浏览器缓存了旧 `ponder.js`（URL 带 `?v=` 会自动击穿缓存），或该文件本身就没加载成功 |
