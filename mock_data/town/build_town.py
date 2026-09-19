# -*- coding: utf-8 -*-
"""
mock_data/town/build_town.py

「展示用模拟数据」—— 一座充电小镇（约 100 个对象），一套布局分发到全部 8 个接口。

为什么单独做一份，而不是继续用 CSV 摆字：
    摆字是"数据集演示"，小镇才是"场景演示"。观众看到沿街充电桩在变颜色，
    才会相信这是个数字孪生，而不是 43 个方块排成三个字母。

设计依据（都是从代码里量出来的，不是拍脑袋）：
    · 地面   ：app.py 里 new THREE.PlaneGeometry(80, 80) → 可用范围 ±40
    · 相机   ：默认位置 (12, 10, 15)，所以主要看中心 ~30 单位
    · 建筑   ：前端统一 autoScale 到高 3.5（building_tall 为 6.0），底面约 3.5 宽
    · 树/车/灯：2.5 / 4.5 / 4.0，尺度自洽
    · 临街判定：core/relation_builder.py 的 street_threshold = 12
    · 服务建筑：serve_threshold = 25
    · 沿街充电桩必须离道路 ≤12，否则"临街"关系建立不起来

布局：30 列 × 22 行字符地图，格距 2.5 单位 → 场景 75 × 55 单位（±37.5 / ±27.5），
      刚好压在 ±40 地面内。字符含义见 CELL_TYPES。

用法：
    python mock_data/town/build_town.py            # 生成小镇全部文件
    python mock_data/town/build_town.py --check    # 只做校验，不写文件
    python mock_data/verify_mock_files.py          # 连同字母版一起总校验
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# ============================================================
# 路径
# ============================================================
HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.dirname(HERE)
ROOT = os.path.dirname(MOCK)

SCENE_JSON = os.path.join(HERE, 'scene_town.json')
CSV_OUT = os.path.join(HERE, 'town_objects.csv')
GEOJSON_OUT = os.path.join(HERE, 'town_district.geojson')
SQLITE_OUT = os.path.join(HERE, 'town.db')

# 🔥 随应用发布的副本：app 的「🏘️ 载入示例小镇」按钮读的就是这个文件
#    （static 目录被 enableStaticServing 挂在 /app/static/ 下）。
#    每次生成都会同步过来，保证内置示例不会过期。
PUBLISHED_JSON = os.path.join(ROOT, 'static', 'data', 'demo_town.json')
REPORT_OUT = os.path.join(HERE, 'town_report.json')

# 🔥 小镇的接口数据全部写在自己的目录里，不覆盖 mock_data/ 下那份「字母版」。
#    两个数据集各跑各的，谁都不会把对方悄悄改掉。
TOWN_REST_JSON = os.path.join(HERE, 'town_stations.json')
TOWN_MYSQL_SQL = os.path.join(HERE, 'town_mysql_seed.sql')
TOWN_PG_SQL = os.path.join(HERE, 'town_postgres_seed.sql')
TOWN_INFLUX_LP = os.path.join(HERE, 'town_influx.lp')
TOWN_INFLUX_SQL = os.path.join(HERE, 'town_influx_queries.sql')
TOWN_MQTT_PY = os.path.join(HERE, 'town_mqtt_publisher.py')

# 只用于「字母版 MQTT 脚本」做模板，不往这里写任何东西
MQTT_DIR = os.path.join(MOCK, 'mqtt')

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

# ============================================================
# 1. 字符地图 —— 唯一数据源
# ============================================================
CELL_TYPES = {
    'C': 'charger_fast',
    'U': 'charger_super',
    'S': 'charger_slow',
    'B': 'building',
    'M': 'building_tall',
    'H': 'building',
    'T': 'tree',
    'L': 'lamp',
    'E': 'container_b',
    'F': 'solar_panel_land',
    'W': 'windmill_low',
}

# 中文名与外号（外号用于生成稳定 ID：快充站-C01）
CELL_META = {
    'C': ('charger_fast', '快充站', 'C'),
    'U': ('charger_super', '超充站', 'U'),
    'S': ('charger_slow', '慢充站', 'S'),
    'B': ('building', '商业楼', 'B'),
    'M': ('building_tall', '商业综合体', 'M'),
    'H': ('building', '住宅楼', 'H'),
    'T': ('tree', '景观树', 'T'),
    'L': ('lamp', '路灯', 'L'),
    'E': ('container_b', '储能柜', 'E'),
    'F': ('solar_panel_land', '光伏板', 'F'),
    'W': ('windmill_low', '微风发电机', 'W'),
}

# 商业楼按用途细分高度（前端 createBuilding 读 custom_props.height）
HEIGHT_BY_CELL = {'M': 6.0, 'B': 4.5, 'H': 3.0}

# 30 列 × 22 行。北在主视图上方 → 行号 0 在最北（z 最小）。
# 主街在 row 11（z = 0），南北两侧临街布置充电带。
TOWN_MAP = [
    "..............................",   # r0  北侧留白
    "....FFFF........TTTT......W...",   # r1  西北光伏 + 北公园 + 东北风机
    "................TTTT..........",   # r2
    "................TTTT..........",   # r3
    "................TTTT..........",   # r4
    "..............................",   # r5
    "....EEEE......BBBBBBB.....HHHH",   # r6  储能柜 / 商业街 / 住宅楼
    "..............BBBBBBB.....HHHH",   # r7
    "..CCCCCCCCCC..BBBBBBB.....HHHH",   # r8  ★ 西侧沿街充电带（10 个）
    "..CCCCCCCCCC..MMMMMMM.....HHHH",   # r9  ★ 西侧沿街充电带（10 个）
    "..............................",   # r10 主街北侧人行道
    "==============================",   # r11 主街（地面网格可见）
    "..............................",   # r12 主街南侧人行道
    "...SSSSS.......TTTT.......UUUU",   # r13 ★ 南侧沿街充电带（5 + 4）
    "...............TTTT...........",   # r14
    "..HHHH...HHHH...L.......L....T",   # r15 住宅区（西）
    "..............................",   # r16
    "....W.........................",   # r17 西南角风机
    "..............................",   # r18
    "..............................",   # r19
    "..............................",   # r20
    "..............................",   # r21
]

# ============================================================
# 2. 坐标换算
# ============================================================
N_COLS = 30
N_ROWS = 22
PITCH = 2.5                      # 单位：场景单位（≈ 米）

# CSV / GeoJSON 那条路径要反向补偿 app 的 ×111×cos(lat)×30 换算，
# 并且横纵比例必须分开算，否则小镇会被拉扁（这正是第一版字母数据的问题）。
SCALE = 30.0
COS_LAT = math.cos(math.radians(22.54))           # 与 BASE_LAT 同纬度，够用
LAT_UNITS_PER_DEG = 111.0 * SCALE                 # z 方向
LON_UNITS_PER_DEG = 111.0 * COS_LAT * SCALE       # x 方向

# 🔥 关键：app 的 CSV/GeoJSON 导入会按「所有要素的均值」把数据居中
#    （core/generation_rules.py: x = (lon - avg_lon) * 111 * cos(lat) * 30）。
#    所以要让导入落点零偏差，必须让「经纬度的均值」正好等于 BASE。
#
#    ⚠️ 千万不要反过来「把 BASE 挪到数据重心上」——那样 lon 与 avg_lon 会是
#       两个几乎相等的 7 位小数，相减吃掉有效位，再乘 3075/3330 会把
#       1e-6 的舍入误差放大成 1.7 个场景单位（实测踩过这个坑）。
#    正确做法：BASE 固定在整数锚点上，改为「把场景坐标本身居中」。
ANCHOR_LAT = 22.5400    # 固定锚点（深圳一带）
ANCHOR_LON = 114.0500
BASE_LAT = ANCHOR_LAT
BASE_LON = ANCHOR_LON
DEG_PER_ROW = PITCH / LAT_UNITS_PER_DEG           # 每行多少纬度
DEG_PER_COL = PITCH / LON_UNITS_PER_DEG           # 每列多少经度


def cell_to_scene(col, row):
    """字符地图坐标 → 场景坐标（先做再定案，所有接口都从这里派生）"""
    x = (col - (N_COLS - 1) / 2.0) * PITCH
    z = (row - (N_ROWS - 1) / 2.0) * PITCH
    return round(x, 3), round(z, 3)


def scene_to_latlon(x, z):
    """场景坐标 → 经纬度（严格反解 app 的换算，保证 CSV 导入后比例不歪）"""
    lat = BASE_LAT + z / LAT_UNITS_PER_DEG
    lon = BASE_LON + x / LON_UNITS_PER_DEG
    return round(lat, 9), round(lon, 9)


# ============================================================
# 3. 展开地图 → 对象列表
# ============================================================
def parse_map():
    """字符地图 → 待生成清单。会顺手校验每行长度。"""
    errors = []
    for i, line in enumerate(TOWN_MAP):
        if len(line) != N_COLS:
            errors.append('第 %d 行长度 %d，应为 %d' % (i, len(line), N_COLS))
    if len(TOWN_MAP) != N_ROWS:
        errors.append('行数 %d，应为 %d' % (len(TOWN_MAP), N_ROWS))
    if errors:
        raise ValueError('字符地图不合法：\n  ' + '\n  '.join(errors))

    counters = {}
    items = []
    for row, line in enumerate(TOWN_MAP):
        for col, ch in enumerate(line):
            if ch in ('=', '.'):
                continue
            if ch not in CELL_TYPES:
                raise ValueError('第 %d 行第 %d 列出现未知字符 %r' % (row, col, ch))
            counters[ch] = counters.get(ch, 0) + 1
            x, z = cell_to_scene(col, row)
            # 注意：经纬度要等 BASE_LAT/BASE_LON 按重心重定之后才能算，
            #       所以这里只记场景坐标，lat/lon 在 build_town() 里补。
            items.append({
                'cell': ch,
                'row': row,
                'col': col,
                'index': counters[ch],
                'x': x,
                'z': z,
            })
    return items


def utilization_for(cell, idx, row):
    """给每个对象一个确定性、且分区合理的利用率，保证画面有层次。"""
    if cell == 'U':                                    # 超充站：枢纽，忙
        base = 0.78
    elif cell == 'C':                                  # 快充站：商业区，中等偏忙
        base = 0.58
    elif cell == 'S':                                  # 慢充站：公园/住宅，闲
        base = 0.26
    else:
        base = 0.5
    wave = 0.12 * math.sin(idx * 1.7 + row * 0.6)
    return round(max(0.05, min(0.95, base + wave)), 3)


def power_for(cell, util):
    if cell == 'U':
        return 180
    if cell == 'C':
        return 120
    if cell == 'S':
        return 60 if util < 0.4 else 120
    # 非充电对象也给一个基准功率，方便 InfluxDB/数据库演示
    return 60


def status_for(util):
    if util > 0.85:
        return '高负载'
    if util < 0.15:
        return '离线'
    return '在线'


def build_town():
    """展开成完整对象清单（充电对象带实时数据，其它对象只带静态属性）"""
    items = parse_map()

    # 🔥 把场景坐标本身居中（而不是去挪地理基准，见文件上方注释里的精度坑）。
    #    居中后：x/z 以原点为中心、经纬度均值正好等于 BASE_LAT/BASE_LON，
    #    app 导入 CSV 时减掉均值即为原坐标——零平移、零精度损失。
    mean_x = sum(it['x'] for it in items) / len(items)
    mean_z = sum(it['z'] for it in items) / len(items)
    for it in items:
        it['x'] = round(it['x'] - mean_x, 3)
        it['z'] = round(it['z'] - mean_z, 3)

    objects = []
    stations = []

    for it in items:
        lat, lon = scene_to_latlon(it['x'], it['z'])
        it['lat'], it['lon'] = lat, lon
        obj_type, name_zh, prefix = CELL_META[it['cell']]
        name = '%s-%s%02d' % (name_zh, prefix, it['index'])
        obj = {
            'id': 'town-%s%02d-r%02dc%02d' % (prefix, it['index'], it['row'], it['col']),
            'type': obj_type,
            'name': name,
            'position': {'x': it['x'], 'y': 0.0, 'z': it['z']},
            'rotation': {'x': 0, 'y': 0, 'z': 0},
            'scale': {'x': 1, 'y': 1, 'z': 1},
            'bind_station_id': '',
            'custom_props': {'town': 'charging_town', 'cell': it['cell']},
            'utilization': 0.5,
        }

        # 建筑：给高度（前端 createBuilding 读 custom_props.height）
        if obj_type.startswith('building'):
            obj['custom_props']['height'] = HEIGHT_BY_CELL.get(it['cell'], 3.5)
            obj['custom_props']['use'] = ('commercial'
                                          if it['cell'] in ('M', 'B') else 'residential')
            obj['custom_props']['floor'] = (int(HEIGHT_BY_CELL.get(it['cell'], 3.5) / 1.5) or 2)

        # 充电站：绑定 station_id，让"实时数据驱动"这条链路能直接跑
        if obj_type.startswith('charger'):
            util = utilization_for(it['cell'], it['index'], it['row'])
            power = power_for(it['cell'], util)
            sid = '%s%02d' % (prefix, it['index'])
            obj['bind_station_id'] = sid
            obj['utilization'] = util
            obj['custom_props'].update({
                'power': power,
                'zone': ('energy' if it['cell'] == 'U' else
                         ('commercial' if it['cell'] == 'C' else 'park')),
                'source': 'town',
            })
            stations.append({
                'station_id': sid,
                'name': name,
                'lat': it['lat'],          # 真实经纬度（给人看）
                'lon': it['lon'],
                'x': it['x'],              # 场景坐标（给 app 用）
                'z': it['z'],
                'utilization': util,
                'power': power,
                'available_slots': max(0, int((1 - util) * 8)),
                'status': status_for(util),
                'zone': obj['custom_props']['zone'],
                'letter': None,
            })

        objects.append(obj)

    return objects, stations, items


# ============================================================
# 4. 各接口输出
# ============================================================
NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.isoformat()
NOW_SQL = NOW.strftime('%Y-%m-%d %H:%M:%S')


def write_scene_json(objects, scene_name='充电小镇（演示）', extra_meta=None):
    """① 场景存档 —— 展示主入口，载入就是完整小镇（比例 1:1）

    同一份内容会再复制到 static/data/demo_town.json，供应用内置的
    「🏘️ 载入示例小镇」按钮使用（部署后用户零文件、零服务即可体验）。
    """
    counts = {}
    for o in objects:
        counts[o['type']] = counts.get(o['type'], 0) + 1
    meta = {
        'description': '带路网与河道的滨水小镇：主街 + 滨河路 + 商业/住宅街区 + 充电广场 + 能源区',
        'object_count': len(objects),
        'type_counts': counts,
        'ground_limit': '地面 PlaneGeometry(80,80)，对象须落在 ±40 内',
    }
    if extra_meta:
        meta.update(extra_meta)
    data = {
        'version': '1.0',
        'scene_name': scene_name,
        'created_at': NOW_ISO,
        'updated_at': NOW_ISO,
        'author': 'mock_data/town',
        'metadata': meta,
        'objects': objects,
    }
    with open(SCENE_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 同步给应用内置的示例入口（静态资源，随镜像发布）
    try:
        os.makedirs(os.path.dirname(PUBLISHED_JSON), exist_ok=True)
        with open(PUBLISHED_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('  ⚠️ 同步内置示例资源失败（不影响其它产物）：%s' % e)

    return SCENE_JSON


def write_csv_and_geojson(objects):
    """② CSV / ③ GeoJSON —— 走 app 的地理编码路径（经纬度已反向补偿）"""
    header = ['name', 'type', 'use', 'height', 'floor', 'zone',
              'lat', 'lon', 'station_id', 'geometry']
    with open(CSV_OUT, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(header)
        for o in objects:
            lat, lon = scene_to_latlon(o['position']['x'], o['position']['z'])
            cp = o.get('custom_props', {})
            # 给每个对象一个多边形轮廓（WKT），方便在 GIS 工具里看形状
            half = PITCH * 0.28
            ring = [(lon - half, lat - half), (lon + half, lat - half),
                    (lon + half, lat + half), (lon - half, lat + half),
                    (lon - half, lat - half)]
            wkt = 'POLYGON ((' + ', '.join('%.7f %.7f' % p for p in ring) + '))'
            w.writerow([
                o['name'], o['type'], cp.get('use', ''), cp.get('height', ''),
                cp.get('floor', ''), cp.get('zone', ''),
                '%.9f' % lat, '%.9f' % lon,
                o.get('bind_station_id', ''), wkt,
            ])

    feats = []
    for o in objects:
        lat, lon = scene_to_latlon(o['position']['x'], o['position']['z'])
        half = PITCH * 0.28
        ring = [[round(lon - half, 7), round(lat - half, 7)],
                [round(lon + half, 7), round(lat - half, 7)],
                [round(lon + half, 7), round(lat + half, 7)],
                [round(lon - half, 7), round(lat + half, 7)],
                [round(lon - half, 7), round(lat - half, 7)]]
        props = {'name': o['name'], 'type': o['type']}
        props.update({k: v for k, v in o.get('custom_props', {}).items()
                      if k in ('use', 'height', 'floor', 'zone')})
        if o.get('bind_station_id'):
            props['station_id'] = o['bind_station_id']
            props['utilization'] = o['utilization']
        feats.append({
            'type': 'Feature',
            'properties': props,
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
        })
    data = {
        'type': 'FeatureCollection',
        'name': 'charging_town',
        'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}},
        'features': feats,
    }
    with open(GEOJSON_OUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return CSV_OUT, GEOJSON_OUT


def write_rules():
    """④ CSV 导入用规则 —— 一行里混着充电桩/建筑/树，必须按 type 分流"""
    rules = {
        'point': {
            'target': 'building',
            'conditions': [
                {'field': 'type', 'op': '==', 'value': 'charger_slow',
                 'target': 'charger_slow', 'uid': 201},
                {'field': 'type', 'op': '==', 'value': 'charger_fast',
                 'target': 'charger_fast', 'uid': 202},
                {'field': 'type', 'op': '==', 'value': 'charger_super',
                 'target': 'charger_super', 'uid': 203},
                {'field': 'type', 'op': '==', 'value': 'tree',
                 'target': 'tree', 'uid': 204},
                {'field': 'type', 'op': '==', 'value': 'lamp',
                 'target': 'lamp', 'uid': 205},
                {'field': 'type', 'op': '==', 'value': 'container_b',
                 'target': 'container_b', 'uid': 206},
                {'field': 'type', 'op': '==', 'value': 'solar_panel_land',
                 'target': 'solar_panel_land', 'uid': 207},
                {'field': 'type', 'op': '==', 'value': 'windmill_low',
                 'target': 'windmill_low', 'uid': 208},
                {'field': 'type', 'op': '==', 'value': 'building_tall',
                 'target': 'building_tall', 'uid': 209},
                {'field': 'zone', 'op': '==', 'value': 'commercial',
                 'target': 'building_tall', 'uid': 210},
            ],
        },
        'line': {'target': 'road_straight', 'conditions': []},
        'polygon': {
            'target': 'building',
            'conditions': [
                {'field': 'use', 'op': '==', 'value': 'commercial',
                 'target': 'building_tall', 'uid': 211},
            ],
        },
    }
    path = os.path.join(HERE, 'rules_town.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return path


def write_sqlite(stations):
    """⑤ SQLite

    🔥 列名就是 lat/lon（app 只按名字取这两列，x/z 之类的别名救不了）。
       所以约定：
         · lat / lon         = **场景坐标**（app 实际使用的，导入后 1:1 还原小镇）
         · geo_lat / geo_lon = 真实经纬度（给人看、给 GIS 用的辅助列，app 不读）
       如果反过来把真实经纬度放 lat/lon，app 导入后整座小镇会缩在
       太平洋上一小块地方（纬度 22.5 / 经度 114.05 被直接当米用）。
    """
    if os.path.exists(SQLITE_OUT):
        os.remove(SQLITE_OUT)
    conn = sqlite3.connect(SQLITE_OUT)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE stations (
            station_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            lat             REAL NOT NULL,
            lon             REAL NOT NULL,
            geo_lat         REAL,
            geo_lon         REAL,
            utilization     REAL NOT NULL,
            power           INTEGER NOT NULL,
            available_slots INTEGER NOT NULL,
            status          TEXT NOT NULL,
            zone            TEXT,
            updated_at      TEXT
        )
    ''')
    cur.executemany(
        'INSERT INTO stations (station_id, name, lat, lon, geo_lat, geo_lon,'
        ' utilization, power, available_slots, status, zone, updated_at)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        [(s['station_id'], s['name'], s['z'], s['x'], s['lat'], s['lon'],
          s['utilization'], s['power'], s['available_slots'], s['status'],
          s['zone'], NOW_SQL)
         for s in stations])
    conn.commit()
    conn.close()
    return SQLITE_OUT


def _sql_rows(stations):
    rows = []
    for s in stations:
        rows.append("('%s', '%s', %.3f, %.3f, %.7f, %.7f, %.3f, %d, %d, '%s', '%s', '%s')" % (
            s['station_id'], s['name'].replace("'", "''"), s['z'], s['x'],
            s['lat'], s['lon'], s['utilization'], s['power'], s['available_slots'],
            s['status'], s['zone'], NOW_SQL))
    return ',\n'.join(rows)


def write_db_sql(stations):
    """⑥⑦ MySQL / PostgreSQL 播种 SQL"""
    cols = ('station_id, name, lat, lon, geo_lat, geo_lon, utilization, power,'
            ' available_slots, status, zone, updated_at')
    coord_note = """-- ⚠️ 坐标列怎么读（很重要）：
--    · lat / lon         = **场景坐标**（app 实际使用的；导入后 1:1 还原小镇）
--    · geo_lat / geo_lon = 真实经纬度（给人看、给 GIS 用的辅助列，app 不读）
--    app 的数据库导入会把 lat/lon 直接当场景坐标，并且只按名字取这两列
--    （x/z、别名都救不了），所以必须这么放。反过来放真实经纬度的话，
--    导进 3D 只会得到太平洋上缩成一团的小镇。"""
    mysql = """-- MySQL 测试数据：充电小镇的 %d 个充电站
-- 界面：设置 → 多源数据接入 → MySQL
--   localhost / 3306 / root / 123456 / charging / stations
-- 生成时间：%s
--
%s

CREATE DATABASE IF NOT EXISTS charging
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE charging;

DROP TABLE IF EXISTS stations;
CREATE TABLE stations (
  station_id      VARCHAR(32) NOT NULL PRIMARY KEY,
  name            VARCHAR(64) NOT NULL,
  lat             DOUBLE      NOT NULL,
  lon             DOUBLE      NOT NULL,
  geo_lat         DOUBLE,
  geo_lon         DOUBLE,
  utilization     DOUBLE      NOT NULL,
  power           INT         NOT NULL,
  available_slots INT         NOT NULL DEFAULT 0,
  status          VARCHAR(16) NOT NULL DEFAULT '在线',
  zone            VARCHAR(16),
  updated_at      DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO stations (%s) VALUES
%s;

SELECT COUNT(*) AS total FROM stations;
""" % (len(stations), NOW_ISO, coord_note, cols, _sql_rows(stations))

    pg = """-- PostgreSQL 测试数据：充电小镇的 %d 个充电站
-- 界面：设置 → 多源数据接入 → PostgreSQL
--   localhost / 5432 / postgres / postgres / charging / stations
-- 生成时间：%s
--
%s

DROP TABLE IF EXISTS stations;
CREATE TABLE stations (
  station_id      VARCHAR(32) PRIMARY KEY,
  name            VARCHAR(64) NOT NULL,
  lat             DOUBLE PRECISION NOT NULL,
  lon             DOUBLE PRECISION NOT NULL,
  geo_lat         DOUBLE PRECISION,
  geo_lon         DOUBLE PRECISION,
  utilization     DOUBLE PRECISION NOT NULL,
  power           INTEGER     NOT NULL,
  available_slots INTEGER     NOT NULL DEFAULT 0,
  status          VARCHAR(16) NOT NULL DEFAULT '在线',
  zone            VARCHAR(16),
  updated_at      TIMESTAMP
);

INSERT INTO stations (%s) VALUES
%s;

SELECT COUNT(*) AS total FROM stations;
""" % (len(stations), NOW_ISO, coord_note, cols, _sql_rows(stations))

    p1 = TOWN_MYSQL_SQL
    p2 = TOWN_PG_SQL
    with open(p1, 'w', encoding='utf-8') as f:
        f.write(mysql)
    with open(p2, 'w', encoding='utf-8') as f:
        f.write(pg)
    return p1, p2


def write_rest(stations):
    """⑧ REST API 静态响应

    🔥 与数据库同一套约定：lat/lon 放**场景坐标**（app 真正读的就是这两个
    名字），真实经纬度放 geo_lat/geo_lon 作为辅助列。这样这份 JSON 既能在
    界面上做连接测试，也能被导入流程正确落点。
    """
    payload = [{
        'station_id': s['station_id'],
        'name': s['name'],
        'lat': s['z'],              # 场景坐标 Z（app 当纬度用）
        'lon': s['x'],              # 场景坐标 X（app 当经度用）
        'geo_lat': s['lat'],        # 真实经纬度（辅助）
        'geo_lon': s['lon'],
        'utilization': s['utilization'],
        'power': s['power'],
        'available_slots': s['available_slots'],
        'status': s['status'],
        'zone': s['zone'],
        'updated_at': NOW_ISO,
    } for s in stations]
    path = TOWN_REST_JSON
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def write_influx(stations):
    """⑨ InfluxDB line protocol + 查询示例"""
    lines = []
    ts_list = [NOW - timedelta(minutes=5 * (11 - i)) for i in range(12)]
    for k, ts in enumerate(ts_list):
        ns = int(ts.timestamp() * 1e9)
        for s in stations:
            wave = 0.05 * math.sin(k * 0.6 + s['utilization'] * 5)
            util = round(max(0.05, min(0.95, s['utilization'] + wave)), 3)
            lines.append(
                'charger_realtime,station_id=%s,zone=%s '
                'name="%s",lat=%s,lon=%s,utilization=%s,power=%di,'
                'available_slots=%di,status="%s" %d' % (
                    s['station_id'], s['zone'], s['name'],
                    s['lat'], s['lon'], util, power_for('C', util),
                    max(0, int((1 - util) * 8)), status_for(util), ns))
    lp = TOWN_INFLUX_LP
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    q = TOWN_INFLUX_SQL
    with open(q, 'w', encoding='utf-8') as f:
        f.write("""-- mock_data/influx/queries.sql（充电小镇版）
-- 界面：设置 → 多源数据接入 → InfluxDB
--   Database    : charging
--   Measurement : charger_realtime
--   时间范围    : 最近 1 小时
-- 生成时间：%s

-- ① 界面里自动执行的那条
SELECT * FROM "charger_realtime"
WHERE time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 500;

-- ② 每个站点取最新一条（20 个站点）
SELECT station_id, name, lat, lon, utilization, power
FROM (
  SELECT station_id, name, lat, lon, utilization, power,
         ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY time DESC) AS rn
  FROM "charger_realtime"
  WHERE time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY station_id;

-- ③ 总记录数（应为 %d）
SELECT COUNT(*) AS total FROM "charger_realtime";
""" % (NOW_ISO, len(lines)))
    return lp, q


def set_stations_block(text, stations_py):
    """把脚本文本里的 STATIONS = [...] 整块换成给定内容

    🔥 不能用 content.replace 找旧块：模板里的站点块与产物里的可能内容不同，
       一旦对不上就会**静默不替换**（脚本看着生成成功，其实还是旧站点）。
       这里按"块"定位，替换不到就直接抛错。
    """
    head = 'STATIONS = ['
    start = text.index(head)
    inner = start + len(head)
    end = text.index(']\n', inner)
    return text[:inner] + '\n' + stations_py + '\n' + text[end:]


def write_mqtt(stations):
    """⑩ MQTT 模拟设备 —— 从 templates/ 的模板生成，换掉站点清单与说明

    🔥 模板的唯一真相是 mock_data/templates/mqtt/mock_mqtt_publisher.py，
       小镇版和字母版都从它生成，只是注入的站点清单不同。
       这样做成独立文件（town_mqtt_publisher.py）是为了两份数据集并存，
       谁都不会把对方悄悄改掉。

    ⚠️ 不要拿"已经生成好的产物"当模板：产物里的站点块与模板不同，
       一旦哪天换了生成顺序，就会把上一版的内容滚进来（原来的写法有此风险）。
    """
    template = os.path.join(MOCK, 'templates', 'mqtt', 'mock_mqtt_publisher.py')
    if not os.path.exists(template):
        return None
    with open(template, 'r', encoding='utf-8') as f:
        src = f.read()

    block = ',\n'.join(
        '    {"id": "%s", "base_util": %.3f, "power": %d, "zone": "%s"}'
        % (s['station_id'], s['utilization'], s['power'], s['zone'])
        for s in stations)
    src = set_stations_block(src, block)

    src = src.replace(
        'MQTT 测试用模拟设备：向 broker 上报充电桩实时数据，并接受下行指令。',
        'MQTT 测试用模拟设备：上报「充电小镇」29 个充电站的实时数据，并接受下行指令。\n'
        '\n'
        '由 mock_data/town/build_town.py 生成（站点清单来自小镇布局）。\n'
        '点位：小镇沿街充电带 —— 快充 C01~C20 / 超充 U01~U04 / 慢充 S01~S05。')
    src = src.replace('python mock_data/mqtt/mock_mqtt_publisher.py',
                      'python mock_data/town/town_mqtt_publisher.py')

    with open(TOWN_MQTT_PY, 'w', encoding='utf-8') as f:
        f.write(src)
    return TOWN_MQTT_PY


# ============================================================
# 5. 校验（设计意图 → 可测断言）
# ============================================================
def verify(objects, stations, items):
    """把"这个小镇设计得对不对"变成一组可测断言"""
    results = []

    def chk(name, ok, detail=''):
        results.append({'name': name, 'ok': bool(ok), 'detail': detail})
        print('  %s %s%s' % ('✅' if ok else '❌', name, ('  —— ' + detail) if detail else ''))

    print('\n' + '=' * 72)
    print('🏘️  充电小镇校验')
    print('=' * 72)

    # 1) 全部对象必须落在地面 PlaneGeometry(80,80) 的 ±40 内
    out = [o for o in objects
           if not (-40 < o['position']['x'] < 40 and -40 < o['position']['z'] < 40)]
    chk('全部对象落在地面 ±40 内', not out,
        '' if not out else '%d 个越界，例 %s' % (len(out), out[0]['name']))

    # 2) 对象类型必须是前端有渲染实现的类型
    #    🔥 比 modelMap/proceduralMap 多出 road / sidewalk / water 三个：
    #       它们是本次为「路网 + 河道」新增的前端类型（见 app.py 的 createRoad）。
    renderable = {'charger_fast', 'charger_slow', 'charger_super', 'building',
                  'building_tall', 'tree', 'tree_pine', 'lamp', 'road_straight',
                  'road_curve', 'car', 'truck', 'container_a', 'container_b',
                  'container_c', 'solar_panel_flat', 'solar_panel_land',
                  'solar_panel_group', 'solar_panel_port', 'solar_panel_port_group',
                  'windmill', 'windmill_low',
                  'road', 'sidewalk', 'water'}
    unknown = sorted({o['type'] for o in objects} - renderable)
    chk('全部类型前端都能渲染', not unknown, str(unknown))

    # 3) 充电桩要么临街、要么在充电广场内（停车场里的桩本来就该在里面）
    def _road_segments_local():
        """道路长条清单：滨水小镇用 waterfront 的，字母版没有道路"""
        try:
            from mock_data.town import waterfront as _wf
            return _wf.road_segments()
        except Exception:
            return []

    def _wf_layout_nodes():
        """路口坐标（x, z）清单，用于校验红绿灯位置"""
        try:
            from mock_data.town import waterfront as _wf
            return _wf.intersection_points()
        except Exception:
            return []

    def _dist_to_road(o):
        px, pz = o['position']['x'], o['position']['z']
        best = 1e9
        for s in _road_segments_local():
            dx, dz = math.cos(s['rot']), math.sin(s['rot'])
            half = s['length'] / 2.0
            ax, az = s['x'] - dx * half, s['z'] - dz * half
            vx, vz = dx * s['length'], dz * s['length']
            t = max(0.0, min(1.0, ((px - ax) * vx + (pz - az) * vz) /
                              max(vx * vx + vz * vz, 1e-9)))
            best = min(best, math.hypot(px - (ax + t * vx), pz - (az + t * vz)))
        return best

    far = []
    for o in objects:
        if not o['type'].startswith('charger'):
            continue
        d = _dist_to_road(o)
        in_lot = (o.get('custom_props') or {}).get('zone') == 'lot'
        if d > 12 and not in_lot:
            far.append((o['name'], round(d, 1)))
    if not _road_segments_local():
        # 字母版布局没有道路对象，这条不适用
        chk('充电桩临街（或位于充电广场内）', True, '该布局没有道路，跳过')
    elif not far:
        chk('充电桩临街（或位于充电广场内）', True, '')
    else:
        chk('充电桩临街（或位于充电广场内）', False, str(far[:3]))

    # 4) 建筑之间不能互相压住（建筑底面约 3.5，中心距需 ≥ 格距）
    blds = [o for o in objects if o['type'].startswith('building')]
    min_gap = min([PITCH, 2.2])
    too_close = []
    for i, a in enumerate(blds):
        for b in blds[i + 1:]:
            d = math.hypot(a['position']['x'] - b['position']['x'],
                           a['position']['z'] - b['position']['z'])
            if d < min_gap * 0.95:
                too_close.append((a['name'], b['name'], round(d, 2)))
    chk('建筑之间不重叠', not too_close, str(too_close[:2]))

    # 5) 充电桩不能叠在一起
    ch = [o for o in objects if o['type'].startswith('charger')]
    dup = []
    for i, a in enumerate(ch):
        for b in ch[i + 1:]:
            d = math.hypot(a['position']['x'] - b['position']['x'],
                           a['position']['z'] - b['position']['z'])
            if d < 0.1:
                dup.append((a['name'], b['name']))
    chk('充电桩位置互不重合', not dup, str(dup[:2]))

    # 6) 每个充电桩都绑定了 station_id（否则实时数据驱动不了）
    unbound = [o['name'] for o in objects
               if o['type'].startswith('charger') and not o['bind_station_id']]
    chk('每个充电桩都绑定了 station_id', not unbound, str(unbound[:3]))

    # 7) 站点 ID 唯一
    ids = [s['station_id'] for s in stations]
    chk('站点 ID 唯一', len(ids) == len(set(ids)), '%d 个站点' % len(ids))

    # 8) 利用率分区合理（商业区该比公园忙）
    com = [s['utilization'] for s in stations if s['zone'] == 'commercial']
    park = [s['utilization'] for s in stations if s['zone'] == 'park']
    if com and park:
        chk('商业区利用率 > 公园区', sum(com) / len(com) > sum(park) / len(park),
            '商业 %.2f vs 公园 %.2f' % (sum(com) / len(com), sum(park) / len(park)))
    energy = [s['utilization'] for s in stations if s['zone'] == 'energy']
    if energy:
        chk('能源区超充最忙', sum(energy) / len(energy) >= sum(com) / len(com),
            '能源 %.2f vs 商业 %.2f' % (sum(energy) / len(energy), sum(com) / len(com)))

    # 9) 对象数量与配比
    counts = {}
    for o in objects:
        counts[o['type']] = counts.get(o['type'], 0) + 1
    total = len(objects)
    chk('对象总数在 100~170 之间（含路网/水面等大件）', 100 <= total <= 170, '%d 个' % total)
    chk('充电桩占比 >= 15%（以实时数据为主）',
        len(ch) >= total * 0.15,
        '%d / %d = %.0f%%' % (len(ch), total, 100.0 * len(ch) / total))

    # 10) 每个接口的站点清单必须一致
    chk('站点数与场景对象数一致', len(stations) == len(ch),
        '%d 站 / %d 桩' % (len(stations), len(ch)))

    # 11) 场景坐标 → 经纬度 → 场景坐标 必须能往返（CSV 路径不歪的保证）
    err = 0.0
    for o in objects[:20]:
        lat, lon = scene_to_latlon(o['position']['x'], o['position']['z'])
        z = (lat - BASE_LAT) * LAT_UNITS_PER_DEG
        x = (lon - BASE_LON) * LON_UNITS_PER_DEG
        err = max(err, abs(x - o['position']['x']), abs(z - o['position']['z']))
    chk('场景↔经纬度往返无误差', err < 0.01, '最大误差 %.4f 单位' % err)

    # 12) 路网配套：人行道成条、红绿灯落在路口、车辆在路上
    walks = [o for o in objects if o['name'].startswith('人行道')]
    if walks:
        chk('人行道独立成条', len(walks) >= 2,
            '%d 条，例 %s 长 %.1f×宽 %.1f' % (
                len(walks), walks[0]['name'],
                walks[0]['custom_props'].get('length', 0),
                walks[0]['custom_props'].get('width', 0)))
    else:
        chk('人行道独立成条', False, '没有独立的人行道对象')

    lights = [o for o in objects if o['type'] == 'lamp' and o['name'].startswith('红绿灯')]
    if lights:
        # 每个红绿灯都必须落在某个"主街 × 纵向路"路口附近
        bad_lights = []
        # 容差按"路口尺度"给：主街宽 5.4，四角的灯距路口中心约 2.6，
        # 所以 4.0 以内都算在路口里（给太紧会把正确数据判成错的）
        for o in lights:
            near = False
            for ix, iz in _wf_layout_nodes():
                if (abs(o['position']['x'] - ix) <= 4.0
                        and abs(o['position']['z'] - iz) <= 4.0):
                    near = True
                    break
            if not near:
                bad_lights.append(o['name'])
        chk('红绿灯都在路口附近', not bad_lights,
            '%d 盏，分布在 %d 个路口' % (len(lights), len(_wf_layout_nodes())))
    else:
        chk('红绿灯都在路口附近', False, '没有红绿灯')

    cars = [o for o in objects if o['type'] in ('car', 'truck')]
    if cars:
        try:
            from mock_data.town import waterfront as _wf2
            segs2 = _wf2.road_segments()
        except Exception:
            segs2 = []
        far_cars = []
        for o in cars:
            px, pz = o['position']['x'], o['position']['z']
            best = 1e9
            for sg in segs2:
                dx, dz = math.cos(sg['rot']), math.sin(sg['rot'])
                half = sg['length'] / 2.0
                ax, az = sg['x'] - dx * half, sg['z'] - dz * half
                vx, vz = dx * sg['length'], dz * sg['length']
                t = max(0.0, min(1.0, ((px - ax) * vx + (pz - az) * vz) /
                                  max(vx * vx + vz * vz, 1e-9)))
                best = min(best, math.hypot(px - (ax + t * vx), pz - (az + t * vz)))
            if best > 4.0:
                far_cars.append((o['name'], round(best, 1)))
        chk('车辆都停在车道上', not far_cars,
            '%d 辆，例 %s' % (len(cars), cars[0]['name']) if not far_cars else str(far_cars[:3]))
    else:
        chk('车辆都停在车道上', False, '没有车辆')

    return results


# ============================================================
# 6. 终端预览
# ============================================================
def print_map(highlight=True):
    print('\n📍 小镇平面图（%d×%d 字符，格距 %.1f 单位；"1 格 = %.1f 单位"）'
          % (N_COLS, N_ROWS, PITCH, PITCH))
    legend = ('  C 快充  U 超充  S 慢充  B 商业楼  M 商业综合体  H 住宅楼\n'
              '  T 景观树  L 路灯  E 储能柜  F 光伏板  W 风电  = 主街  · 空地')
    for i, line in enumerate(TOWN_MAP):
        mark = ' ← 主街' if line.startswith('=') else ''
        print('   %s%s' % (line, mark))
    print(legend)


# ============================================================
# 7. main
# ============================================================
def main():
    ap = argparse.ArgumentParser(description='生成「充电小镇」展示用模拟数据')
    ap.add_argument('--check', action='store_true', help='只校验，不写文件')
    ap.add_argument('--layout', choices=['waterfront', 'letter'], default='waterfront',
                    help='waterfront=滨水小镇（默认，带路网/河道）；letter=旧的 C S V 点位')
    args = ap.parse_args()

    scene_name = '滨水小镇（演示）'
    extra_meta = {}
    if args.layout == 'waterfront':
        # 🔥 滨水小镇：地块 + 路网 + 河道，由 waterfront.py 按真实规划顺序生成
        #    （用同目录导入，保证"直接跑脚本"和"当包导入"都能work）
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from mock_data.town import waterfront as _wf
        objects, stations, occ = _wf.build_objects()
        # 补经纬度：CSV / 数据库 / API 这些出口需要地理坐标
        for s in stations:
            s['lat'], s['lon'] = _wf.scene_to_latlon(s['x'], s['z'])
        items = None
        plan_text = '\n'.join('   ' + ln for ln in _wf.plan_lines(occ))
        extra_meta = {
            'layout': 'waterfront：%d×%d 网格，格距 %.1f' % (_wf.COLS, _wf.ROWS, _wf.PITCH),
            'features': '主街/滨河路/滨水步道 + 城市河道 + 中央广场 + 商业/住宅街区 + 充电广场 + 能源区',
        }
    else:
        objects, stations, items = build_town()
        plan_text = None
        extra_meta = {'layout': '%d×%d 字符地图，格距 %.1f' % (N_COLS, N_ROWS, PITCH)}

    counts = {}
    for o in objects:
        counts[o['type']] = counts.get(o['type'], 0) + 1

    print('=' * 72)
    print('🏘️  充电小镇 —— 一套布局，分发到全部接口（layout=%s）' % args.layout)
    print('=' * 72)
    print('  对象总数：%d 个（%d 类）' % (len(objects), len(counts)))
    for t, n in sorted(counts.items(), key=lambda x: -x[1]):
        print('     · %-20s %3d' % (t, n))
    xs = [o['position']['x'] for o in objects]
    zs = [o['position']['z'] for o in objects]
    print('  场景占地：x %.1f ~ %.1f（%.1f 宽） z %.1f ~ %.1f（%.1f 深）'
          % (min(xs), max(xs), max(xs) - min(xs), min(zs), max(zs), max(zs) - min(zs)))
    print('  充电站  ：%d 个（快充 %d / 超充 %d / 慢充 %d）'
          % (len(stations),
             counts.get('charger_fast', 0), counts.get('charger_super', 0),
             counts.get('charger_slow', 0)))

    if plan_text:
        print('\n📍 小镇平面图（M 商业 H 住宅 P 广场 C 充电广场 c 快充 U 超充 S 慢充')
        print('   E 储能 F 光伏 T 绿地 = 桥 - 道路 ~ 水面 · 空地）\n')
        print(plan_text)
    else:
        print_map()

    results = verify(objects, stations, items)

    if args.check:
        print('\n（--check：未写入任何文件）')
        return 0 if all(r['ok'] for r in results) else 1

    csv_path, geojson_path = write_csv_and_geojson(objects)
    mysql_path, pg_path = write_db_sql(stations)
    lp_path, query_path = write_influx(stations)

    made = [
        ('场景存档（展示主入口）', write_scene_json(objects, scene_name, extra_meta)),
        ('CSV 导出', csv_path),
        ('GeoJSON 导出', geojson_path),
        ('导入规则（按 type 分流）', write_rules()),
        ('SQLite 小镇库', write_sqlite(stations)),
        ('MySQL 播种 SQL', mysql_path),
        ('PostgreSQL 播种 SQL', pg_path),
        ('REST API 静态响应', write_rest(stations)),
        ('InfluxDB line protocol', lp_path),
        ('InfluxDB 查询示例', query_path),
        ('MQTT 设备清单（覆盖字母版）', write_mqtt(stations)),
    ]

    print('\n' + '=' * 72)
    print('生成结果')
    print('=' * 72)
    for label, path in made:
        if not path:
            print('  ⚠️ %-28s（跳过）' % label)
            continue
        rel = os.path.relpath(path, ROOT)
        print('  ✅ %-28s %-46s %8d B' % (label, rel, os.path.getsize(path)))

    with open(REPORT_OUT, 'w', encoding='utf-8') as f:
        json.dump({
            'generated_at': NOW_ISO,
            'objects': len(objects),
            'stations': len(stations),
            'type_counts': counts,
            'scene_bounds': {'x': [min(xs), max(xs)], 'z': [min(zs), max(zs)]},
            'checks': results,
            'all_passed': all(r['ok'] for r in results),
        }, f, ensure_ascii=False, indent=2)
    print('\n  📄 校验报告：%s' % os.path.relpath(REPORT_OUT, ROOT))

    print('\n💡 展示用主入口：数据导入与导出 → 载入场景存档（.json）→ scene_town.json')
    print('   各接口怎么填：见 mock_data/README.md')

    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
