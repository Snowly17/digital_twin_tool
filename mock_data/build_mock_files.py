# -*- coding: utf-8 -*-
"""
mock_data/build_mock_files.py

一次性生成「多源数据接入」各接口的测试用模拟文件。

设计约定（与截图中的每个接口一一对应）：

| 界面接口            | 生成的文件                                   |
|---------------------|----------------------------------------------|
| 导入原始数据 CSV    | mock_data/csv_letters_buildings.csv          |
| 导入原始数据 GeoJSON| mock_data/geojson_letters.geojson            |
| SQLite              | mock_data/test_stations.db                   |
| MySQL               | mock_data/db/mysql_seed.sql                  |
| PostgreSQL          | mock_data/db/postgres_seed.sql               |
| MySQL + PostgreSQL  | mock_data/db/docker-compose.yml              |
| MySQL + PostgreSQL  | mock_data/db/seed_db.py                      |
| REST API            | mock_data/api/stations_response.json         |
| REST API            | mock_data/api/mock_rest_server.py            |
| REST API            | mock_data/api/test_rest_api.py               |
| InfluxDB            | mock_data/influx/charger_realtime.lp          |
| InfluxDB            | mock_data/influx/queries.sql                 |
| InfluxDB            | mock_data/influx/write_to_influx.py          |
| MQTT                | mock_data/mqtt/mock_mqtt_publisher.py        |
| 载入场景存档 (.json)| mock_data/scene_archive_letters.json         |

造型约定：**所有接口的测试数据都摆成 "C S V" 三个字母**。
  - CSV / GeoJSON / 场景存档 → 用「建筑」(building) 摆字
  - SQLite / MySQL / PostgreSQL / REST API / InfluxDB / MQTT → 用「充电站」摆字
    这样从任何一个接口导入，3D 场景里都会出现同样一个 CSV 字样，方便对比各条链路。

用法：
    python mock_data/build_mock_files.py
"""
import csv
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# ============================================================
# 0. 路径
# ============================================================
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CSV_PATH = os.path.join(HERE, 'csv_letters_buildings.csv')
GEOJSON_PATH = os.path.join(HERE, 'geojson_letters.geojson')
SQLITE_PATH = os.path.join(HERE, 'test_stations.db')
SCENE_PATH = os.path.join(HERE, 'scene_archive_letters.json')
DB_DIR = os.path.join(HERE, 'db')
API_DIR = os.path.join(HERE, 'api')
INFLUX_DIR = os.path.join(HERE, 'influx')
MQTT_DIR = os.path.join(HERE, 'mqtt')

# ============================================================
# 0b. 脚本模板
# ============================================================
# 🔥 为什么不再把脚本内容写成字符串常量：
#    原先 seed_db.py / mock_rest_server.py / test_rest_api.py /
#    write_to_influx.py / mock_mqtt_publisher.py 这 5 个文件，内容是**同样的
#    一大段字符串**同时存在于本文件和一个真实文件里——两处逐字节相同。
#    直接改产物，下次生成就被悄悄覆盖回去（已经踩过）；改模板，产物又不会更新。
#    现在唯一真相是 mock_data/templates/ 下的真实文件：生成 = 复制。
#    所以：**要改这些脚本，直接改 templates/ 里的对应文件**。
TEMPLATES_DIR = os.path.join(HERE, 'templates')

# 产物 → 模板的相对路径
TEMPLATE_MAP = {
    'seed_db.py':            'db/seed_db.py',
    'mock_rest_server.py':   'api/mock_rest_server.py',
    'test_rest_api.py':      'api/test_rest_api.py',
    'write_to_influx.py':    'influx/write_to_influx.py',
    'mock_mqtt_publisher.py': 'mqtt/mock_mqtt_publisher.py',
}

# MQTT 脚本里要被替换掉的站点清单块（模板与产物都长这样）
_STATIONS_HEAD = 'STATIONS = ['
_STATIONS_TAIL = ']\n'

# ============================================================
# 1. 字模：14 列 × 6 行，用 # 拼出 "C S V"
#    每列间距 1 个 cell，字母之间空 2 列
# ============================================================
LETTERS = {
    'C': [
        ".####.",
        "#....#",
        "#.....",
        "#.....",
        "#....#",
        ".####.",
    ],
    'S': [
        ".####.",
        "#....#",
        ".####.",
        ".....#",
        "#....#",
        ".####.",
    ],
    'V': [
        "#....#",
        "#....#",
        "#....#",
        "#....#",
        ".#..#.",
        "..##..",
    ],
}

# 拼成整幅图案：C + 空2列 + S + 空2列 + V  → 4+2+4+2+4 = 14 列
GRID = []
for r in range(6):
    row = LETTERS['C'][r] + '..' + LETTERS['S'][r] + '..' + LETTERS['V'][r]
    GRID.append(row)
N_COLS = len(GRID[0])   # 14
N_ROWS = len(GRID)      # 6

# ============================================================
# 2. 地理坐标参数
# ============================================================
BASE_LAT = 22.5400          # 图案中心纬度（深圳一带）
BASE_LON = 114.0500         # 图案中心经度
PITCH = 0.0014              # 相邻格子的经纬度间距（度）

# 说明：app 的导入逻辑用 x=(lon-lon均值)*111*cos(lat)*30 换算场景坐标，
#      并且会过滤 |x|>100 或 |z|>100 的"异常值"。
#      按 PITCH=0.0014 计算，整幅字横向 13 格 → 场景里约 90 个单位宽、
#      纵向 5 格 → 约 23 个单位深，都远小于 100 的过滤阈值，因此不会被丢掉。

# 单位换算（与 core/generation_rules.py 完全一致）
SCALE = 30.0
COS_LAT = math.cos(math.radians(BASE_LAT))


def cell_lon(col):
    """第 col 列的中心经度"""
    return BASE_LON + (col - (N_COLS - 1) / 2.0) * PITCH


def cell_lat(row):
    """第 row 行的中心纬度（row=0 在最北，屏幕上就是最上面一行）"""
    return BASE_LAT + ((N_ROWS - 1) / 2.0 - row) * PITCH


def cells():
    """产出所有被点亮的格子：(字母, 行, 列, 纬度, 经度, 序号)"""
    out = []
    for r, line in enumerate(GRID):
        for c, ch in enumerate(line):
            if ch == '#':
                letter = 'C' if c < 5 else ('S' if c < 11 else 'V')
                out.append({
                    'letter': letter,
                    'row': r,
                    'col': c,
                    'lat': cell_lat(r),
                    'lon': cell_lon(c),
                })
    for i, cell in enumerate(out):
        cell['index'] = i + 1
    return out


def lat_lon_to_scene(lat, lon, avg_lat, avg_lon):
    """经纬度 → 场景坐标（复刻 core/generation_rules.py 的换算）"""
    x = (lon - avg_lon) * 111 * COS_LAT * SCALE
    z = (lat - avg_lat) * 111 * SCALE
    return round(x, 3), round(z, 3)


def utilization_for(idx):
    """0.05~0.95 之间的确定性利用率，做成一条平缓的波，方便肉眼看出颜色差异"""
    u = 0.5 + 0.42 * math.sin(idx * 0.7)
    return round(max(0.05, min(0.95, u)), 3)


def status_for(util):
    if util > 0.85:
        return '高负载'
    if util < 0.15:
        return '离线'
    return '在线'


def power_for(util):
    return 180 if util > 0.75 else (120 if util > 0.45 else 60)


CELLS = cells()
AVG_LAT = sum(c['lat'] for c in CELLS) / len(CELLS)
AVG_LON = sum(c['lon'] for c in CELLS) / len(CELLS)

# 每条充电站记录（各数据库 / API / InfluxDB 共用同一份）
STATIONS = []
for c in CELLS:
    util = utilization_for(c['index'])
    sid = f"{c['letter']}{c['index']:03d}"
    STATIONS.append({
        'station_id': sid,
        'name': f"测试站-{c['letter']}{c['row']}{c['col']}",
        'lat': round(c['lat'], 6),
        'lon': round(c['lon'], 6),
        'utilization': util,
        'power': power_for(util),
        'available_slots': max(0, int((1 - util) * 8)),
        'status': status_for(util),
        'letter': c['letter'],
    })

# 场景存档：每栋楼的位置直接复用 core/generation_rules.py 的换算公式，
# 保证「载入场景存档」与「CSV 导入」得到完全一致的空间布局。
for c, s in zip(CELLS, STATIONS):
    s['scene_x'], s['scene_z'] = lat_lon_to_scene(  # noqa: E305  (见下方定义)
        c['lat'], c['lon'], AVG_LAT, AVG_LON)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.isoformat()
NOW_SQL = NOW.strftime('%Y-%m-%d %H:%M:%S')


def ensure_dirs():
    for d in (HERE, DB_DIR, API_DIR, INFLUX_DIR, MQTT_DIR):
        os.makedirs(d, exist_ok=True)


# ============================================================
# 3. CSV：建筑点数据（经纬度）+ WKT 面几何
#    app 的 core/geo_parser.py 对 CSV 统一按「点」处理，
#    几何列只是为了让人/其它工具能还原出建筑轮廓。
# ============================================================
def write_csv():
    header = ['name', 'type', 'use', 'height', 'floor',
              'lat', 'lon', 'station_id', 'geometry']

    polygon_w = PITCH * 0.65   # 建筑在经度方向的边长
    polygon_d = PITCH * 0.65   # 建筑在纬度方向的边长

    with open(CSV_PATH, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(header)
        for c in CELLS:
            lat, lon = c['lat'], c['lon']
            half_w = polygon_w / 2.0
            half_d = polygon_d / 2.0
            # 外环 5 点闭合（WKT 用逗号分隔，csv.writer 会自动加引号）
            ring = [
                (lon - half_w, lat - half_d),
                (lon + half_w, lat - half_d),
                (lon + half_w, lat + half_d),
                (lon - half_w, lat + half_d),
                (lon - half_w, lat - half_d),
            ]
            wkt = 'POLYGON ((' + ', '.join(
                f'{x:.6f} {y:.6f}' for x, y in ring) + '))'
            name = f"建筑-{c['letter']}{c['row']}{c['col']}"
            w.writerow([
                name,
                'building',
                'commercial' if (c['row'] + c['col']) % 2 == 0 else 'residential',
                12 if c['letter'] == 'S' else (10 if c['letter'] == 'V' else 8),
                (c['row'] % 3) + 1,
                f'{lat:.6f}',
                f'{lon:.6f}',
                f"BLD-{c['letter']}{c['index']:03d}",
                wkt,
            ])
    return CSV_PATH


# ============================================================
# 4. GeoJSON：同样的建筑，用真正的 Polygon 几何
# ============================================================
def write_geojson():
    feats = []
    for c in CELLS:
        lat, lon = c['lat'], c['lon']
        half_w = PITCH * 0.65 / 2.0
        half_d = PITCH * 0.65 / 2.0
        # 注意 GeoJSON 是 [经度, 纬度]
        ring = [
            [round(lon - half_w, 6), round(lat - half_d, 6)],
            [round(lon + half_w, 6), round(lat - half_d, 6)],
            [round(lon + half_w, 6), round(lat + half_d, 6)],
            [round(lon - half_w, 6), round(lat + half_d, 6)],
            [round(lon - half_w, 6), round(lat - half_d, 6)],
        ]
        feats.append({
            'type': 'Feature',
            'properties': {
                'name': f"建筑-{c['letter']}{c['row']}{c['col']}",
                'use': 'commercial' if (c['row'] + c['col']) % 2 == 0 else 'residential',
                'height': 12 if c['letter'] == 'S' else (10 if c['letter'] == 'V' else 8),
                'floor': (c['row'] % 3) + 1,
                'station_id': f"BLD-{c['letter']}{c['index']:03d}",
                'letter': c['letter'],
            },
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
        })

    data = {
        'type': 'FeatureCollection',
        'name': 'csv_letters_buildings',
        'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}},
        'features': feats,
    }
    with open(GEOJSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return GEOJSON_PATH


# ============================================================
# 5. SQLite：表名 stations，字段用 lat/lon（app 的别名表直接命中）
# ============================================================
def write_sqlite():
    if os.path.exists(SQLITE_PATH):
        os.remove(SQLITE_PATH)
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE stations (
            station_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            lat             REAL NOT NULL,
            lon             REAL NOT NULL,
            utilization     REAL NOT NULL,
            power           INTEGER NOT NULL,
            available_slots INTEGER NOT NULL,
            status          TEXT NOT NULL,
            letter          TEXT,
            updated_at      TEXT
        )
    ''')
    cur.executemany(
        'INSERT INTO stations (station_id, name, lat, lon, utilization, power,'
        ' available_slots, status, letter, updated_at)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?)',
        [(s['station_id'], s['name'], s['lat'], s['lon'], s['utilization'],
          s['power'], s['available_slots'], s['status'], s['letter'], NOW_SQL)
         for s in STATIONS]
    )
    conn.commit()
    conn.close()
    return SQLITE_PATH


# ============================================================
# 6. MySQL / PostgreSQL 播种 SQL
# ============================================================
def _sql_values_rows(rows):
    return ',\n'.join(rows)


def write_mysql_sql():
    rows = []
    for s in STATIONS:
        rows.append(
            "('{sid}', '{name}', {lat}, {lon}, {util}, {power}, {slots}, "
            "'{status}', '{letter}', '{ts}')".format(
                sid=s['station_id'],
                name=s['name'].replace("'", "''"),
                lat=s['lat'], lon=s['lon'], util=s['utilization'],
                power=s['power'], slots=s['available_slots'],
                status=s['status'], letter=s['letter'], ts=NOW_SQL)
        )
    sql = f"""-- MySQL 测试数据：充电站摆成 "C S V" 三个字母
-- 对应界面：设置 → 多源数据接入 → 数据源类型 = MySQL
--   主机 localhost / 端口 3306 / 用户名 root / 密码 123456 / 数据库 charging / 表名 stations
--
-- 生成时间：{NOW_ISO}

CREATE DATABASE IF NOT EXISTS charging
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE charging;

DROP TABLE IF EXISTS stations;
CREATE TABLE stations (
  station_id      VARCHAR(32)  NOT NULL PRIMARY KEY,
  name            VARCHAR(64)  NOT NULL,
  lat             DOUBLE       NOT NULL,
  lon             DOUBLE       NOT NULL,
  utilization     DOUBLE       NOT NULL,
  power           INT          NOT NULL,
  available_slots INT          NOT NULL DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT '在线',
  letter          VARCHAR(4),
  updated_at      DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO stations
  (station_id, name, lat, lon, utilization, power, available_slots, status, letter, updated_at)
VALUES
{_sql_values_rows(rows)};

-- 自检：应返回 {len(STATIONS)} 行，且 lat/lon 呈 "C S V" 三个字母的分布
SELECT COUNT(*) AS total FROM stations;
SELECT station_id, name, lat, lon, utilization FROM stations ORDER BY station_id LIMIT 10;
"""
    path = os.path.join(DB_DIR, 'mysql_seed.sql')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(sql)
    return path


def write_postgres_sql():
    rows = []
    for s in STATIONS:
        rows.append(
            "('{sid}', '{name}', {lat}, {lon}, {util}, {power}, {slots}, "
            "'{status}', '{letter}', '{ts}')".format(
                sid=s['station_id'],
                name=s['name'].replace("'", "''"),
                lat=s['lat'], lon=s['lon'], util=s['utilization'],
                power=s['power'], slots=s['available_slots'],
                status=s['status'], letter=s['letter'], ts=NOW_SQL)
        )
    sql = f"""-- PostgreSQL 测试数据：充电站摆成 "C S V" 三个字母
-- 对应界面：设置 → 多源数据接入 → 数据源类型 = PostgreSQL
--   主机 localhost / 端口 5432 / 用户名 postgres / 密码 postgres / 数据库 charging / 表名 stations
--
-- 生成时间：{NOW_ISO}

DROP TABLE IF EXISTS stations;
CREATE TABLE stations (
  station_id      VARCHAR(32)  PRIMARY KEY,
  name            VARCHAR(64)  NOT NULL,
  lat             DOUBLE PRECISION NOT NULL,
  lon             DOUBLE PRECISION NOT NULL,
  utilization     DOUBLE PRECISION NOT NULL,
  power           INTEGER      NOT NULL,
  available_slots INTEGER      NOT NULL DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT '在线',
  letter          VARCHAR(4),
  updated_at      TIMESTAMP
);

INSERT INTO stations
  (station_id, name, lat, lon, utilization, power, available_slots, status, letter, updated_at)
VALUES
{_sql_values_rows(rows)};

-- 自检：应返回 {len(STATIONS)} 行
SELECT COUNT(*) AS total FROM stations;
SELECT station_id, name, lat, lon, utilization FROM stations ORDER BY station_id LIMIT 10;
"""
    path = os.path.join(DB_DIR, 'postgres_seed.sql')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(sql)
    return path


def write_docker_compose():
    text = f"""# mock_data/db/docker-compose.yml
#
# 一条命令起好 MySQL + PostgreSQL，并自动灌入 "C S V" 测试数据。
#
#   cd mock_data/db
#   docker compose up -d
#
# 起来之后在界面里填：
#   MySQL      : localhost 3306 root 123456  charging  stations
#   PostgreSQL : localhost 5432 postgres postgres charging stations
#
# 停止并清空：
#   docker compose down -v
#
# 生成时间：{NOW_ISO}

services:
  mysql:
    image: mysql:8.0
    container_name: dtt-mock-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: "123456"
      MYSQL_DATABASE: "charging"
      TZ: "Asia/Shanghai"
    ports:
      - "3306:3306"
    command: >
      --character-set-server=utf8mb4
      --collation-server=utf8mb4_unicode_ci
      --default-authentication-plugin=mysql_native_password
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-p123456"]
      interval: 5s
      timeout: 5s
      retries: 30
    volumes:
      - ./mysql_seed.sql:/docker-entrypoint-initdb.d/10_mysql_seed.sql:ro
      - dtt_mysql_data:/var/lib/mysql

  postgres:
    image: postgres:16
    container_name: dtt-mock-postgres
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: "postgres"
      POSTGRES_USER: "postgres"
      POSTGRES_DB: "charging"
      TZ: "Asia/Shanghai"
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d charging"]
      interval: 5s
      timeout: 5s
      retries: 30
    volumes:
      - ./postgres_seed.sql:/docker-entrypoint-initdb.d/10_postgres_seed.sql:ro
      - dtt_pg_data:/var/lib/postgresql/data

volumes:
  dtt_mysql_data:
  dtt_pg_data:
"""
    path = os.path.join(DB_DIR, 'docker-compose.yml')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path




# ============================================================
# 7. REST API 模拟
# ============================================================
def write_rest_payload():
    payload = []
    for s in STATIONS:
        payload.append({
            'station_id': s['station_id'],
            'name': s['name'],
            'lat': s['lat'],
            'lon': s['lon'],
            'utilization': s['utilization'],
            'power': s['power'],
            'available_slots': s['available_slots'],
            'status': s['status'],
            'letter': s['letter'],
            'updated_at': NOW_ISO,
        })
    path = os.path.join(API_DIR, 'stations_response.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path






# ============================================================
# 8. InfluxDB：line protocol + SQL 查询 + 写入脚本
# ============================================================
def write_influx_lp():
    """生成 InfluxDB 3 的 line protocol。

    字段名刻意与 app 的别名表对齐：
        station_id / name / lat / lon / utilization / power  → 全部能自动识别
    时间戳用纳秒整数，保证 order by time desc + groupby first 能取到最新值。
    """
    lines = []
    # 造 12 个时间点（每 5 分钟一个，覆盖最近 1 小时）
    ts_list = [NOW - timedelta(minutes=5 * (11 - i)) for i in range(12)]

    for k, ts in enumerate(ts_list):
        ns = int(ts.timestamp() * 1e9)
        for s in STATIONS:
            # 用序号和时间点做一点平滑波动，让"最新值"依然保持 C S V 的形状
            wave = 0.06 * math.sin((k + s['letter'].__len__()) * 0.5 + s['utilization'] * 6)
            util = round(max(0.05, min(0.95, s['utilization'] + wave)), 3)
            status = status_for(util)
            power = power_for(util)
            slots = max(0, int((1 - util) * 8))
            lines.append(
                'charger_realtime,station_id={sid},letter={lt} '
                'name="{name}",lat={lat},lon={lon},utilization={util},'
                'power={power}i,available_slots={slots}i,status="{status}" {ns}'.format(
                    sid=s['station_id'], lt=s['letter'], name=s['name'],
                    lat=s['lat'], lon=s['lon'], util=util, power=power,
                    slots=slots, status=status, ns=ns)
            )

    path = os.path.join(INFLUX_DIR, 'charger_realtime.lp')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path, len(ts_list)


def write_influx_queries():
    text = f"""-- mock_data/influx/queries.sql
--
-- 对应界面：设置 → 多源数据接入 → 数据源类型 = InfluxDB
--   在 InfluxDB 3 里 database 就是 bucket 名，界面上的 SQL 是：
--     SELECT * FROM "measurement" WHERE time >= now() - INTERVAL '<range>' ORDER BY time DESC LIMIT <n>
--
-- 本项目的模拟数据：
--   measurement : charger_realtime
--   database    : charging
--   时间跨度    : 最近 1 小时（{len(STATIONS)} 个站点 × 12 个时间点 = {len(STATIONS) * 12} 行）
--
-- 生成时间：{NOW_ISO}

-- ① 界面里自动执行的那条（可直接粘到 InfluxDB 3 Query 界面）
SELECT * FROM "charger_realtime"
WHERE time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 500;

-- ② 自检：每个站点取最新一条，结果应仍是 C S V 三个字母的形状
SELECT station_id, name, lat, lon, utilization, power
FROM (
  SELECT station_id, name, lat, lon, utilization, power,
         ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY time DESC) AS rn
  FROM "charger_realtime"
  WHERE time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY station_id;

-- ③ 记录总数
SELECT COUNT(*) AS total FROM "charger_realtime";
"""
    path = os.path.join(INFLUX_DIR, 'queries.sql')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path




# ============================================================
# 9. 从模板生成脚本（生成 = 复制，不再内嵌字符串）
# ============================================================
def template_text(name):
    """读取模板内容"""
    path = os.path.join(TEMPLATES_DIR, TEMPLATE_MAP[name])
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def copy_template(name, dest_path):
    """模板原样复制到产物位置"""
    text = template_text(name)
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(text)
    return dest_path


def set_stations_block(text, stations_py):
    """把脚本文本里的 STATIONS = [...] 整块换成给定内容

    🔥 不能用 content.replace 找旧块：模板里的站点块与产物里的可能内容不同
       （小镇版就是这样），一旦对不上就会静默不替换。这里按"块"定位。
    """
    start = text.index(_STATIONS_HEAD)
    inner_start = start + len(_STATIONS_HEAD)
    end = text.index(_STATIONS_TAIL, inner_start)
    return text[:inner_start] + '\n' + stations_py + '\n' + text[end:]


def write_mqtt_publisher():
    """MQTT 模拟设备：模板 + 注入字母版站点清单"""
    station_py = ',\n'.join(
        '    {"id": "%s", "lat": %.6f, "lon": %.6f, "base_util": %.3f, "power": %d}'
        % (s['station_id'], s['lat'], s['lon'], s['utilization'], s['power'])
        for s in STATIONS
    )
    text = set_stations_block(template_text('mock_mqtt_publisher.py'), station_py)
    path = os.path.join(MQTT_DIR, 'mock_mqtt_publisher.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path




# ============================================================
# 10. 场景存档 JSON（app 的「载入场景存档」直接吃这个格式）
# ============================================================
def write_scene_archive():
    objects = []
    for c in CELLS:
        x, z = lat_lon_to_scene(c['lat'], c['lon'], AVG_LAT, AVG_LON)
        letter = c['letter']
        height = 12 if letter == 'S' else (10 if letter == 'V' else 8)
        objects.append({
            'id': 'mock-csv-%s-%02d-%02d' % (letter, c['row'], c['col']),
            'type': 'building',
            'name': '建筑-%s%d%d' % (letter, c['row'], c['col']),
            'position': {'x': x, 'y': 0.0, 'z': z},
            'rotation': {'x': 0, 'y': 0, 'z': 0},
            'scale': {'x': 1, 'y': 1, 'z': 1},
            'bind_station_id': '',
            'custom_props': {
                'height': height,
                'floor': (c['row'] % 3) + 1,
                'use': 'commercial' if (c['row'] + c['col']) % 2 == 0 else 'residential',
                'mock_source': 'scene_archive_letters',
            },
            'utilization': 0.5,
        })

    data = {
        'version': '1.0',
        'scene_name': '测试场景-CSV字母建筑',
        'created_at': NOW_ISO,
        'updated_at': NOW_ISO,
        'author': 'mock_data',
        'metadata': {
            'description': '用 34 栋建筑摆成 "C S V" 三个字母，'
                           '用于测试「数据导入与导出 → 载入场景存档（.json）」',
            'letters': 'CSV',
            'building_count': len(objects),
        },
        'objects': objects,
    }
    with open(SCENE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return SCENE_PATH


# ============================================================
# 10b. 规则文件（app「生成规则配置」可直接导入）
#      重要：app 的 core/geo_parser.py 对 CSV 一律按「点」解析，
#      而界面默认规则是「点 → 充电桩、面 → 建筑」。
#      所以想让 CSV 里的建筑摆字成立，必须把「点 → 建筑」。
#      这两个 json 就是给用户点「📥 导入规则（JSON）」用的。
# ============================================================
def write_rule_files():
    point_to_building = {
        "point": {
            "target": "building",
            "conditions": [
                {"field": "type", "op": "==", "value": "tree", "target": "tree", "uid": 101},
                {"field": "type", "op": "==", "value": "lamp", "target": "lamp", "uid": 102},
            ],
        },
        "line": {"target": "road_straight", "conditions": []},
        "polygon": {"target": "building", "conditions": []},
    }
    city = {
        "point": {
            "target": "charger_fast",
            "conditions": [
                {"field": "type", "op": "==", "value": "tree", "target": "tree", "uid": 4},
                {"field": "type", "op": "==", "value": "lamp", "target": "lamp", "uid": 5},
                {"field": "utilization", "op": ">", "value": "0.8", "target": "charger_super", "uid": 6},
            ],
        },
        "line": {
            "target": "road_straight",
            "conditions": [
                {"field": "highway", "op": "contains", "value": "primary",
                 "target": "road_straight", "uid": 7},
            ],
        },
        "polygon": {
            "target": "building",
            "conditions": [
                {"field": "use", "op": "==", "value": "commercial", "target": "building_tall", "uid": 8},
                {"field": "height", "op": ">", "value": "10", "target": "building_tall", "uid": 9},
            ],
        },
    }
    paths = []
    for fname, data in (('rules_point_to_building.json', point_to_building),
                        ('rules_city.json', city)):
        p = os.path.join(HERE, fname)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        paths.append(p)
    return paths


# ============================================================
# 11. 生成 ASCII 预览 + 回读自检
# ============================================================
def ascii_grid_preview():
    """按字模本身画字符画 —— 这是唯一不会骗人的预览方式"""
    lines = []
    for r, line in enumerate(GRID):
        cells_txt = []
        for ch in line:
            cells_txt.append('███' if ch == '#' else '   ')
        lines.append('  ' + ''.join(cells_txt))
    return '\n'.join(lines)


def ascii_scatter(rows, x_key='x', z_key='z', width=69, height=15):
    """把坐标散点画成字符画（只用于确认"形状还在"，不做逐格对齐）"""
    pts = [(r[x_key], r[z_key]) for r in rows]
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]
    grid = [[' '] * width for _ in range(height)]
    for x, z in pts:
        cx = int(round((x - min(xs)) / max(max(xs) - min(xs), 1e-12) * (width - 1)))
        cy = int(round((1 - (z - min(zs)) / max(max(zs) - min(zs), 1e-12)) * (height - 1)))
        grid[cy][cx] = '#'
    return '\n'.join('  ' + ''.join(line) for line in grid)


def self_check():
    """用 app 自己的解析器回读 CSV，确认坐标不会被当成异常值丢掉、类型正确"""
    print('\n' + '=' * 70)
    print('🔎 自检：用 app 的真实解析链路回读 CSV')
    print('=' * 70)

    sys.path.insert(0, ROOT)
    try:
        from core.geo_parser import auto_parse, get_feature_summary
        from core.generation_rules import (generate_objects,
                                           auto_layout_if_clustered)
    except Exception as e:
        print('⚠️ 无法导入 core 模块（%s），跳过回读自检' % e)
        return

    # 用 app 界面默认的规则（点→充电桩、面→建筑），并额外演示「点→建筑」
    app_default_rules = {
        'point': {'target': 'charger_fast', 'conditions': []},
        'line': {'target': 'road_straight', 'conditions': []},
        'polygon': {'target': 'building', 'conditions': []},
    }
    rules_building = {
        'point': {'target': 'building', 'conditions': []},
        'line': {'target': 'road_straight', 'conditions': []},
        'polygon': {'target': 'building', 'conditions': []},
    }

    with open(CSV_PATH, 'rb') as f:
        features = auto_parse(f.read(), os.path.basename(CSV_PATH))
    summary = get_feature_summary(features)
    print('  解析到要素：点 %d / 线 %d / 面 %d'
          % (summary['point'], summary['line'], summary['polygon']))
    print('  ⚠️ CSV 一律按「点」处理，所以要出建筑，规则里必须把「点」映射为 building')

    for label, rules in (('默认规则（点→充电桩）', app_default_rules),
                         ('本文件配套规则（点→建筑）', rules_building)):
        objs = generate_objects(features, rules)
        objs, clustered = auto_layout_if_clustered(objs, threshold=2.0, spacing=3.0)
        kept = [o for o in objs
                if -100 < o['position']['x'] < 100 and -100 < o['position']['z'] < 100]
        types = {}
        for o in kept:
            types[o['type']] = types.get(o['type'], 0) + 1
        print('  · %-24s 物体 %d 个（网格化:%s，异常值过滤后 %d 个）类型=%s'
              % (label, len(objs), '是' if clustered else '否', len(kept), types))

        if label.startswith('本文件配套'):
            xs = [o['position']['x'] for o in kept]
            zs = [o['position']['z'] for o in kept]
            print('    场景跨度：x %.1f~%.1f（宽 %.1f） z %.1f~%.1f（深 %.1f）'
                  % (min(xs), max(xs), max(xs) - min(xs),
                     min(zs), max(zs), max(zs) - min(zs)))
            print('    📍 场景坐标还原（应该能看出 C S V）：')
            rows = [{'x': o['position']['x'], 'z': o['position']['z']} for o in kept]
            print(ascii_scatter(rows))
            if len(kept) != len(objs):
                print('    ❌ 有物体被当成异常值丢掉了！请调小 PITCH')
            else:
                print('    ✅ 全部通过异常值过滤（|x|,|z| < 100）')


def main():
    ensure_dirs()
    print('=' * 70)
    print('生成「多源数据接入」测试用模拟文件 —— 全部摆成 C S V')
    print('=' * 70)

    made = [
        ('CSV（建筑/多边形摆字）', write_csv()),
        ('GeoJSON（建筑/多边形摆字）', write_geojson()),
        ('SQLite 数据库', write_sqlite()),
        ('MySQL 播种 SQL', write_mysql_sql()),
        ('PostgreSQL 播种 SQL', write_postgres_sql()),
        ('Docker Compose（MySQL+PG）', write_docker_compose()),
        # 以下 5 个是「模板 → 复制」，模板在 mock_data/templates/ 下
        ('数据库灌数据脚本', copy_template('seed_db.py',
                                          os.path.join(DB_DIR, 'seed_db.py'))),
        ('REST API 静态响应', write_rest_payload()),
        ('REST API 模拟服务', copy_template('mock_rest_server.py',
                                           os.path.join(API_DIR, 'mock_rest_server.py'))),
        ('REST API 自测脚本', copy_template('test_rest_api.py',
                                           os.path.join(API_DIR, 'test_rest_api.py'))),
        ('InfluxDB 查询示例', write_influx_queries()),
        ('InfluxDB 写入脚本', copy_template('write_to_influx.py',
                                           os.path.join(INFLUX_DIR, 'write_to_influx.py'))),
        ('MQTT 模拟设备', write_mqtt_publisher()),
        ('场景存档 JSON', write_scene_archive()),
    ]
    lp_path, n_ts = write_influx_lp()
    made.append(('InfluxDB line protocol', lp_path))
    for p in write_rule_files():
        made.append(('导入规则（点→建筑等）', p))

    for label, path in made:
        rel = os.path.relpath(path, ROOT)
        size = os.path.getsize(path)
        print('  ✅ %-26s %-46s %8d B' % (label, rel, size))

    print('\n  📊 站点数 %d 个 · Influx 时间点 %d 个 · 建筑/点位跨 %.4f°×%.4f°'
          % (len(STATIONS), n_ts, (N_COLS - 1) * PITCH, (N_ROWS - 1) * PITCH))

    print('\n  📍 字模预览（C S V，6 行 × 14 格）：')
    print(ascii_grid_preview())

    self_check()

    print('\n' + '=' * 70)
    print('全部生成完毕。各接口怎么填，见 mock_data/README.md')
    print('=' * 70)


if __name__ == '__main__':
    main()
