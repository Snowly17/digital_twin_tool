# mock_data —— 模拟数据（展示用「充电小镇」+ 测试用「CSV 摆字」）

这里有两套数据集，**互不干扰**，各跑各的：

| 数据集 | 用途 | 目录/文件前缀 |
|---|---|---|
| 🏘️ **充电小镇**（推荐做展示） | 约 118 个对象的小镇：沿街充电带 + 商业/住宅 + 光伏储能风电。看着像场景，不像数据集 | `town/` 下全部文件 |
| 🔤 **CSV 摆字**（测试用） | 所有数据摆成 `C S V` 三个字母，一眼判断"这条链路通了没有" | `mock_data/` 根下的字母版文件 |

两套的接口文件是分开的（小镇全部带 `town_` 前缀），所以**谁都不会覆盖谁**。

> ⚠️ 顺序无关：先跑 `build_mock_files.py` 再跑 `build_town.py`，或者反过来，结果都一致。
> 总校验脚本 `verify_mock_files.py` 会同时检查两套、以及"有没有互相覆盖"。

---

## 〇、展示用主数据集：滨水小镇

**展示时走这条链路**：数据导入与导出 → **载入场景存档（.json）** → 选 `town/scene_town.json`
→ 立刻得到一座完整小镇：**137 个对象** = 25 个充电桩 + 41 栋建筑 + 24 棵树
+ 8 段道路 + 5 条人行道 + 1 片水面 + 4 盏红绿灯（1 个主路口）+ 5 辆车
+ 广场铺装 + 15 块光伏 + 9 个储能柜。
充电桩**已经绑好 station_id**，所以右侧面板能直接看到实时利用率、也能演示 MQTT 反向控制。

```bash
python mock_data/town/build_town.py      # 生成 + 自检（15 项断言全过才退出 0）
python mock_data/town/build_town.py --layout letter --check   # 旧版 C S V 点位布局
```

### 路网配套（本轮新增）

| 要素 | 类型 | 说明 |
|---|---|---|
| 车行道 | `road` | `width` 路宽 / `length` 路长 / `center_line` 自动虚线 / `sidewalk` 路缘 |
| **人行道** | `sidewalk` | **独立成条**，贴街道两侧（长 59.4 × 宽 1.4），可单独选中/样式化 |
| 红绿灯 | `lamp` | 摆在**交叉口四个角**（主街 × c5 / c17，2 个路口共 8 盏，到路口中心距离一致 2.42） |
| 车辆 | `car` | 贴右侧车道：east 走南半边、west 走北半边并自动掉头 |
| 水面 | `water` | 河道，半透明 + 金属反光 |

### ⚠️ 前端需要新增 3 个物体类型 + 改缩放机制

原来的 `road_straight.glb` **没法铺路网**：它世界尺寸 19 × 0.75 × 20.33，**长度在 Z 轴**，
而旧的缩放规则是 `autoScale = 12 / max(size.x, size.z)`（按最长边等比），结果是一块
**11.2 × 12 的方块**——既不是路，也没法单向拉长（旧代码后期只乘一个标量，拉长会同时变宽）。

`app.py` 因此做了两件事：

1. 新增 `road` / `sidewalk` / `water` 三个类型（见 `createRoad()` 与 `proceduralMap`）
2. **把模型缩放从标量改成按轴归一化**（`target = [宽, 高, 长]`，逐轴 `target / 原始尺寸`）

第 2 条是关键——它让"长度"和"宽度"各归各的，**同一个模型可以被拉成一条街**。
实测 `road_straight.glb` 从方块变成 **5.40 × 0.60 × 12.00** 的真实路段：
想铺 60 米长就把 `scale.z` 设成 5，此时 `scale.x` 仍只影响路宽，不会跟着变宽。

> 这也让"临街关系"真正生效：`core/relation_builder.py` 会把 `road` 识别为道路，
> 实测 164 个物体建立 **500+ 条关系**，其中约 40 条是充电桩的「临街」。

### 导入的模型现在会"真正被使用"

之前导入的 `.glb` **要么比房子还大、要么小到看不见**——GLB 里没有"这是车/是楼"的语义，
只有包围盒，而前端旧逻辑只在 `maxDim > 20` 或 `< 0.5` 时才兜底，落在中间的模型
就是**原始尺寸直接进场景**。

现在补齐了整条尺寸链路：

| 环节 | 改动 |
|---|---|
| **上传时**（`core/asset_library.py`） | `measure_glb_size()` 纯标准库解析 GLB 包围盒（含节点 TRS 变换），按分类算出 `scale` 与目标尺寸入库。**没建新列也能用**（写失败自动退回原有列） |
| **资产卡片** | 显示「原始 19.00×0.75×20.33 m → 目标高 0.6 m · scale (0.284, 0.799, 0.590)」——用户终于能看见该调多少倍 |
| **进场景时** | `scale` 直接带上；`target_height/length/width` 写进对象，前端据此再按轴归一化一次（老资产没这些字段则由前端按包围盒兜底） |
| **前端** | `custom_model` 分支改成按轴归一化，不再只在极端尺寸时兜底 |

分类 → 目标尺寸（`CATEGORY_TARGET_SIZE`，与前端内置类型的归一化目标一致）：

| 分类 | 目标尺寸 | 方式 |
|---|---|---|
| 道路 | 宽 5.4 / 高 0.6 / 长 12.0 | **三轴独立拉伸**（只有路需要，才铺得出来） |
| 车辆 | 最长轴 4.5 | **等比**（曾经用拉伸，Y 被拉 1.36 倍、车变形——用户反馈后改回等比） |
| 建筑 | 高 3.5 | 等比 |
| 景观 | 高 2.5 | 等比 |
| 充电设备 | 高 2.0 | 等比 |
| 其他 | 高 2.0 | 等比 |

> 实测（拿内置模型当"用户上传"跑一遍）：
> `road_straight.glb` 19×0.75×20.33 → **5.40×0.60×12.00**；
> `car.glb` 1.81×1.18×4.22 → **1.80×1.60×4.50**；`bench.glb` → 高 2.0。
> 顺带修掉一处死代码：`update_asset_scale()` 之前全项目零调用。

### 小镇布局怎么改

改 `mock_data/town/waterfront.py` 顶部的这几张表，重跑生成即可，8 个接口一起变：

| 表 | 内容 |
|---|---|
| `H_ROADS` / `V_ROADS` / `MAIN_STREET` | 路网（每条街在占用表里只占 1 行，视觉宽度单独给） |
| `DISTRICTS` | 地块：用途、行列范围、建筑高度区间 |
| `CHARGING_RUNS` | 沿街充电带与停车位（按"街 + 侧 + 列区间"描述） |
| `SIDEWALK_RUNS` | 人行道独立成条的位置与宽度 |
| `CARS` | 路上车辆（按"街 + 车道 + 沿街坐标"） |

红绿灯位置由 `intersection_points()` 自动推出（主街与纵向路的真实交叉），不需要手填。

改完先跑 `python mock_data/town/waterfront.py` 单独看平面图和重叠校验。

| 界面位置 | 界面里要填 | 小镇用哪个文件 |
|---|---|---|
| **载入场景存档（.json）** | 上传文件 | `town/scene_town.json` ⭐ 展示主入口 |
| **导入原始数据（CSV）** | 上传文件 + 导入规则 `town/rules_town.json` | `town/town_objects.csv` |
| **导入原始数据（GeoJSON）** | 上传文件 | `town/town_district.geojson` |
| **SQLite** | 路径 `mock_data/town/town.db`，表 `stations` | `town/town.db`（48 行） |
| **MySQL** | localhost / 3306 / root / 123456 / charging / stations | `town/town_mysql_seed.sql` |
| **PostgreSQL** | localhost / 5432 / postgres / postgres / charging / stations | `town/town_postgres_seed.sql` |
| **REST API** | 用 `api/mock_rest_server.py --payload town/town_stations.json` | `town/town_stations.json` |
| **InfluxDB** | Database `charging` / Measurement `charger_realtime` | `town/town_influx.lp` |
| **MQTT** | broker.emqx.io / 1883 / `charger/+/status` | `python mock_data/town/town_mqtt_publisher.py` |

**为什么小镇的 SQLite / MySQL / PG / REST "导入后比例正确"？**

app 的数据库导入是把 `lat`/`lon` 两列**直接当场景坐标**用（不做 ×3330 的地理换算）。
所以小镇的库/接口文件里：

| 列名 | 存什么 | 谁用 |
|---|---|---|
| `lat` / `lon` | **场景坐标** | app 真正读的就是这两列（它只按名字取，改列顺序也没用） |
| `geo_lat` / `geo_lon` | 真实经纬度 | 给人看 / 给 GIS 用的辅助列，app 不读 |

反过来把真实经纬度放进 `lat`/`lon`，导进 3D 就会得到**太平洋上缩成一团**的小镇
（22.5 / 114.05 会被直接当成米）。校验脚本会真的跑一遍 app 的导入数学
（归一化 → 网格化阈值 → 15/跨度 缩放），确认落点与场景存档**逐点一致**。

**为什么 CSV 那条路径要单独写一套经纬度？**

CSV 走的是 `×111 × cos(lat) × 30` 的地理换算，横纵系数不同（3075 vs 3330）。
`build_town.py` 会把场景坐标**反解**成经纬度写进 CSV，并把场景坐标本身先居中，
所以 CSV 导入的落点与场景存档**逐点零误差**（校验脚本会真的跑一遍 app 的解析器来确认）。

> ⚠️ 一个踩过的坑，改这个脚本时别重犯：**不要"把地理基准挪到数据重心上"**。
> 那样 `lon` 与 `avg_lon` 会是两个几乎相等的 7 位小数，相减吃掉有效位，
> 再乘 3075/3330 会把 1e-6 的舍入误差放大成 **1.7 个场景单位**的平移。
> 正确做法是基准固定在整数锚点（114.05 / 22.54），改为把**场景坐标**居中。

---

## 一、测试用数据集：CSV 摆字 → 界面对应关系

造型约定：**所有数据都摆成 `C S V` 三个字母**。

- **CSV / GeoJSON / 场景存档** → 用「建筑」摆字
- **SQLite / MySQL / PostgreSQL / REST API / InfluxDB / MQTT** → 用「充电站」摆字

这样从任何一个接口导入，3D 场景里都会出现同一个 `CSV` 字样，一眼就能看出这条链路通了没有。

| 界面位置 | 界面里要填 | 用哪个文件 |
|---|---|---|
| 数据导入与导出 → **导入原始数据（CSV / GeoJSON）** | 上传文件 | `csv_letters_buildings.csv`（推荐）或 `geojson_letters.geojson` |
| 数据导入与导出 → **载入场景存档（.json）** | 上传文件 | `scene_archive_letters.json` |
| 设置 → 多源数据接入 → **SQLite** | 文件路径 `mock_data/test_stations.db`，表名 `stations` | `test_stations.db`（已建好，43 行） |
| 设置 → 多源数据接入 → **MySQL** | localhost / 3306 / root / 123456 / charging / stations | `db/mysql_seed.sql` + `db/docker-compose.yml` |
| 设置 → 多源数据接入 → **PostgreSQL** | localhost / 5432 / postgres / postgres / charging / stations | `db/postgres_seed.sql` + `db/docker-compose.yml` |
| 设置 → 多源数据接入 → **REST API** | `http://127.0.0.1:8000/stations`，Token 留空 | `api/mock_rest_server.py`（自带服务） |
| 设置 → 多源数据接入 → **InfluxDB** | Host / Token / Database `charging` / Measurement `charger_realtime` | `influx/charger_realtime.lp` + `influx/write_to_influx.py` |
| 设置 → **工业协议接入（MQTT 实时流）** | broker.emqx.io / 1883 / `charger/+/status` | `mqtt/mock_mqtt_publisher.py` |

另外两份是给「生成规则配置 → 导入规则（JSON）」用的：`rules_point_to_building.json`、`rules_city.json`。

---

## 二、30 秒快速验证（不需要任何数据库、不需要联网）

```bash
# 1) 重新生成全部模拟文件（改了字母/坐标后跑这个）
python mock_data/build_mock_files.py

# 2) 总校验：把每份文件都用「app 同款逻辑」读一遍
python mock_data/verify_mock_files.py
```

第 2 步会打印 `C S V` 的字符画，并逐项检查字段名、坐标跨度、会不会被异常值过滤丢掉。
**43 项通过 / 0 项失败** 才算这份模拟数据是可用的。

只想验 REST API 的实连（需要先进界面那条链路）：

```bash
# 窗口 A
python mock_data/api/mock_rest_server.py
# 窗口 B
python mock_data/verify_mock_files.py --url http://127.0.0.1:8000/stations
```

---

## 三、CSV / GeoJSON 这份要注意一件事（重要）

app 的 `core/geo_parser.py` 里，**CSV 一律按「点」解析**（面要素只认 GeoJSON）。
而界面「生成规则配置」的默认规则是：

```
点 → 充电桩(charger_fast)     面 → 建筑(building)
```

所以 CSV 直接导入会得到 **43 个充电桩**，不是建筑。

要得到 **43 栋建筑摆成的 CSV 字样**，二选一：

- **省事**：改用 `geojson_letters.geojson`——它是真正的 Polygon，按默认规则直接出建筑，什么都不用改。
- **坚持用 CSV**：在「导入原始数据 → 生成规则配置」里把 `📍 点数据 → 物体映射` 的默认映射改成 `building`，
  或者点 `📥 导入规则（JSON）` 选 `mock_data/rules_point_to_building.json`。

两种方式 `verify_mock_files.py` 都验过：都是 43 个物体，且全部通过 ±100 的异常值过滤。

> CSV 里同时带了 `lat/lon`（给程序读）和 `geometry`（WKT 多边形，给人/其它 GIS 工具看轮廓）。
> GeoJSON 的环和 CSV 的 WKT 是同一套坐标。

---

## 四、各接口怎么跑起来

### SQLite（最简单，零依赖）

界面里填：

```
SQLite 文件路径 : mock_data/test_stations.db
表名            : stations
```

点「测试连接」，再点「📥 导入为场景对象」。

### MySQL / PostgreSQL

**方式 A：Docker（推荐，起库 + 自动灌数据一步到位）**

```bash
cd mock_data/db
docker compose up -d          # 起 MySQL(3306) + PostgreSQL(5432)，自动执行播种 SQL
docker compose logs -f mysql  # 看 MySQL 是否初始化完成
docker compose down -v        # 用完清空
```

**方式 B：灌进你自己的库**

```bash
# MySQL（需 pip install pymysql）
python mock_data/db/seed_db.py --kind mysql --host localhost --port 3306 \
    --user root --password 123456 --database charging --table stations

# PostgreSQL（需 pip install psycopg2-binary）
python mock_data/db/seed_db.py --kind postgres --host localhost --port 5432 \
    --user postgres --password postgres --database charging --table stations
```

也可以直接把 `db/mysql_seed.sql` / `db/postgres_seed.sql` 丢给任何 SQL 客户端执行。

### REST API

```bash
python mock_data/api/mock_rest_server.py                 # 默认 127.0.0.1:8000
python mock_data/api/mock_rest_server.py --token demo   # 开启 Token 校验
```

界面里填 `API URL = http://127.0.0.1:8000/stations`（开了 Token 就同时填 Token）。

- `GET /stations` → JSON 数组（app 里 `len(data)` 直接可用）
- `GET /stations?format=geojson` → 同一批数据的 GeoJSON，可以直接喂给 CSV/GeoJSON 导入
- `GET /health` → 健康检查

不想起服务也行：`api/stations_response.json` 就是它返回的原文。

### InfluxDB 3

```bash
# 需 pip install influxdb3-python
python mock_data/influx/write_to_influx.py \
    --host https://<你的区域>.aws.cloud2.influxdata.com \
    --token <你的 API Token> \
    --database charging \
    --query          # 写完顺手按界面同款 SQL 查一次
```

界面里填：Host（去掉末尾斜杠）/ Database `charging` / Measurement `charger_realtime` / 时间范围「最近 1 小时」。

`influx/queries.sql` 里存了三条 SQL：界面同款查询、取每站最新值的自检查询、总数查询。

### MQTT

```bash
python mock_data/mqtt/mock_mqtt_publisher.py            # 43 个点位，每 3 秒上报一轮
python mock_data/mqtt/mock_mqtt_publisher.py --once     # 只发一轮就退出
python mock_data/mqtt/mock_mqtt_publisher.py --broker broker.emqx.io --interval 3
```

界面里点「🔗 连接 MQTT」，Broker `broker.emqx.io`、端口 `1883`、主题 `charger/+/status`。

它同时扮演设备端：订阅 `charger/{id}/cmd`，执行 `set_limit` / `set_enable`，回 `cmd_reply` 并补发状态，
所以「下发 → 执行 → 回执 → 状态收敛」这条闭环能整条跑通（和根目录的 `mock_mqtt_publisher.py` 同协议，
只是点位数从 8 个换成了摆成 C S V 的 43 个）。

> 默认连的是公共 Broker。自己发的消息全网可见，**别往上面发任何真实/敏感数据**。

---

## 五、已知的真实差异（不粉饰）

1. **走数据库导入，字形会被打散成方阵——这是 app 的既有逻辑，不是数据的问题。**

   app 的数据库导入路径（SQLite / MySQL / PostgreSQL 的「📥 导入为场景对象」）做两件事：

   ```
   ① 把经纬度按均值归一化成 x/z（单位仍然是"度"）
   ② 若 x、z 跨度同时 < 2.0 → 把所有站点重排成方阵
   ```

   数据库里存的是**真实经纬度**，跨度量级天生就是 `0.03° × 0.007°`，必然踩到第 ② 条，
   于是 43 个点被铺成 7×7 方阵，`C S V` 就看不出来了。

   要规避只有两条路，都有代价：把坐标伪造成「非法地理值」去骗过阈值，或者把点距拉到 3 公里级。
   我选择**保留真实经纬度**，因为这份文件的首要用途是测「连接是否通、字段是否认得、能不能导入」，
   这三点它完全胜任；`verify_mock_files.py` 也把这件事当作已知行为如实报告，而不是假装通过。

   **想看 `C S V` 字形，请用这三条路径**（都验过、布局逐点一致）：

   - `csv_letters_buildings.csv`（需把「点」规则映射为 `building`，或用配套规则 JSON）
   - `geojson_letters.geojson`（**最省事**，默认规则直接出建筑）
   - `scene_archive_letters.json`（载入场景存档，位置与上面两者完全相同）

2. **CSV 的经纬度是「摆位坐标」，不是真实地理位置。**
   为了 25 米见方的字形不被 CSV 路径的 ±100 场景坐标过滤丢掉，格距取了 `0.0014°`（约 155 米），
   整幅字约 2 公里见方。落在深圳 114.05E / 22.54N 附近，但别当成真实站点分布去用。
   场景里整幅字约 90 × 23 个单位（因为 CSV 路径有 ×111×cos(lat)×30 的换算，会被压扁一些）。

3. **`verify_mock_files.py` 不连真实 MySQL / PostgreSQL / InfluxDB / MQTT Broker。**
   这四类只做「文件结构 / SQL 语法 / 字段契约」的静态校验——真连需要你先把库或 Broker 跑起来。
   REST API 是唯一支持 `--url` 实连的。

4. **MySQL/PG 的播种 SQL 用的是 `DROP TABLE IF EXISTS` + `CREATE TABLE`。**
   目标库里的同名表会被重建，别指向有真实数据的库。

5. **MQTT 用的是公共 Broker（broker.emqx.io）。**
   消息全网可见，别往上面发真实或敏感数据。

---

## 六、重新生成

```bash
python mock_data/build_mock_files.py     # 字母版全量重新生成（含自检）
python mock_data/town/build_town.py      # 充电小镇全量重新生成（含 13 项自检）
python mock_data/verify_mock_files.py    # 总校验（当前 103 项全过）
```

### ⚠️ 要改那 5 个「服务脚本」，请改 `templates/`，不要改产物

`seed_db.py` / `mock_rest_server.py` / `test_rest_api.py` / `write_to_influx.py` /
`mock_mqtt_publisher.py` 这 5 个脚本是**从 `mock_data/templates/` 复制出来的**：

| 你想改 | 改哪里 |
|---|---|
| 脚本逻辑（端口、参数、提示文案…） | ✅ `mock_data/templates/<同名路径>` |
| 生成时的站点清单 | ✅ `mock_data/town/build_town.py`（小镇）/ `build_mock_files.py`（字母） |
| ~~产物文件本身~~ | ❌ 改了下次生成就被覆盖 |

> 这个约束是补一个真实踩过的坑：脚本内容曾经**同时**存在于
> `build_mock_files.py` 的字符串常量和真实文件里（逐字节相同、约 700 行）。
> 改产物 → 下次生成被悄悄覆盖；改模板 → 产物不更新。现在生成 = 复制，
> 总校验里的「⑪ 模板与产物一致性」会在两边不一致时直接报错。

### 字模在哪

字模在 `build_mock_files.py` 顶部的 `LETTERS` 里，想换成别的字直接改那三个 6×4 的字符矩阵：

```python
LETTERS = {
    'C': [".####.", "#....#", "#.....", "#.....", "#....#", ".####."],
    ...
}
```

小镇的布局则是 `town/build_town.py` 顶部的 `TOWN_MAP`（30×22 字符地图）。

两个生成脚本都会把变化同步到**全部 8 个接口**的数据文件，所以改一处、全线一致。
