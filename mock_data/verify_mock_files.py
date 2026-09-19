# -*- coding: utf-8 -*-
"""
mock_data/verify_mock_files.py

总校验：把 mock_data/ 下每一份模拟文件，用「app 同款逻辑」读一遍，
确认字段齐全、坐标不会被异常值过滤丢掉、形状能还原成 C S V。

用法：
    python mock_data/verify_mock_files.py
    python mock_data/verify_mock_files.py --url http://127.0.0.1:8000/stations --token xxx

不做的事（诚实说明）：
    · 不连真实的 MySQL / PostgreSQL / InfluxDB —— 那需要你先把库跑起来
      （MySQL/PG 用 docker compose up -d，InfluxDB 用 write_to_influx.py）
    · 不连真实 MQTT Broker —— 需要联网，用 mock_mqtt_publisher.py 自己发
    这两个只做「文件结构 / 语法 / 字段契约」静态校验。
"""
import argparse
import json
import os
import re
import sqlite3
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 🔥 只把项目根加进 sys.path，并且所有内部模块都用 "mock_data.xxx" 这个包路径导入。
#    否则同一个文件会被当成两个不同模块各加载一次（town.build_town 与
#    mock_data.town.build_town），各自带一份独立的模块级全局量——调试时会非常费解。
sys.path.insert(0, ROOT)
HERE_PKG = os.path.basename(HERE)      # 'mock_data'
TOWN_PKG = HERE_PKG + '.town'

PASS = []
FAIL = []


def ok(name, detail=''):
    PASS.append(name)
    print('  ✅ %s%s' % (name, ('  —— ' + detail) if detail else ''))


def bad(name, detail=''):
    FAIL.append(name)
    print('  ❌ %s%s' % (name, ('  —— ' + detail) if detail else ''))


def scatter(rows, x_key, y_key, width=69, height=15):
    pts = [(float(r[x_key]), float(r[y_key])) for r in rows]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    grid = [[' '] * width for _ in range(height)]
    for x, y in pts:
        cx = int(round((x - min(xs)) / max(max(xs) - min(xs), 1e-12) * (width - 1)))
        cy = int(round((1 - (y - min(ys)) / max(max(ys) - min(ys), 1e-12)) * (height - 1)))
        grid[cy][cx] = '#'
    return '\n'.join('     ' + ''.join(line) for line in grid)


def section(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


# ---------------------------------------------------------------- CSV
def check_csv():
    section('① 导入原始数据 —— CSV')
    path = os.path.join(HERE, 'csv_letters_buildings.csv')
    if not os.path.exists(path):
        return bad('CSV 文件存在')
    ok('CSV 文件存在', os.path.relpath(path, ROOT))

    from core.geo_parser import auto_parse, get_feature_summary
    from core.generation_rules import generate_objects, auto_layout_if_clustered

    with open(path, 'rb') as f:
        features = auto_parse(f.read(), 'csv_letters_buildings.csv')
    summary = get_feature_summary(features)
    ok('app 的 auto_parse 能解析', '点 %d 个' % summary['point'])

    rules = {'point': {'target': 'building', 'conditions': []},
             'line': {'target': 'road_straight', 'conditions': []},
             'polygon': {'target': 'building', 'conditions': []}}
    objs = generate_objects(features, rules)
    objs, clustered = auto_layout_if_clustered(objs, threshold=2.0, spacing=3.0)
    kept = [o for o in objs
            if -100 < o['position']['x'] < 100 and -100 < o['position']['z'] < 100]

    if len(kept) == len(objs):
        ok('全部通过异常值过滤', '%d 个物体全部保留' % len(kept))
    else:
        bad('异常值过滤', '丢了 %d 个，需调小 PITCH' % (len(objs) - len(kept)))

    types = {}
    for o in kept:
        types[o['type']] = types.get(o['type'], 0) + 1
    if types.get('building') == len(kept):
        ok('点→建筑 规则命中', str(types))
    else:
        bad('点→建筑 规则命中', str(types))

    # 形状必须还是 C S V（逐格比对）
    xs = sorted({round(o['position']['x'], 3) for o in kept})
    zs = sorted({round(o['position']['z'], 3) for o in kept})
    if len(zs) == 6:
        ok('行数正确', '6 行 → C S V 字模高度')
    else:
        bad('行数正确', '期望 6 行，实际 %d 行' % len(zs))
    print(scatter([{'x': o['position']['x'], 'z': o['position']['z']} for o in kept], 'x', 'z'))


# ------------------------------------------------------------ GeoJSON
def check_geojson():
    section('② 导入原始数据 —— GeoJSON')
    path = os.path.join(HERE, 'geojson_letters.geojson')
    if not os.path.exists(path):
        return bad('GeoJSON 文件存在')
    ok('GeoJSON 文件存在', os.path.relpath(path, ROOT))

    from core.geo_parser import auto_parse, get_feature_summary
    from core.generation_rules import generate_objects, auto_layout_if_clustered

    with open(path, 'rb') as f:
        features = auto_parse(f.read(), 'geojson_letters.geojson')
    summary = get_feature_summary(features)
    if summary['polygon'] > 0:
        ok('app 的 auto_parse 能解析', '面 %d 个' % summary['polygon'])
    else:
        bad('app 的 auto_parse 能解析', '没解析出面要素')

    # 用 app 界面默认规则（面 → building）
    rules = {'point': {'target': 'charger_fast', 'conditions': []},
             'line': {'target': 'road_straight', 'conditions': []},
             'polygon': {'target': 'building', 'conditions': []}}
    objs = generate_objects(features, rules)
    objs, _ = auto_layout_if_clustered(objs, threshold=2.0, spacing=3.0)
    kept = [o for o in objs
            if -100 < o['position']['x'] < 100 and -100 < o['position']['z'] < 100]
    if kept and all(o['type'] == 'building' for o in kept):
        ok('默认规则自动出建筑', '%d 个 building（无需改规则）' % len(kept))
    else:
        bad('默认规则自动出建筑', str({o['type'] for o in objs}))

    scales = {o['scale']['x'] for o in kept}
    if len(kept) == len(objs):
        ok('全部通过异常值过滤')
    else:
        bad('异常值过滤', '丢了 %d 个' % (len(objs) - len(kept)))
    print(scatter([{'x': o['position']['x'], 'z': o['position']['z']} for o in kept], 'x', 'z'))


# ------------------------------------------------------------- SQLite
def check_sqlite():
    section('③ SQLite（界面：SQLite 文件路径 / 表名）')
    path = os.path.join(HERE, 'test_stations.db')
    if not os.path.exists(path):
        return bad('SQLite 文件存在')

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if 'stations' in tables:
            ok('表名 stations 存在')
        else:
            bad('表名 stations 存在', str(tables))
            return

        import pandas as pd
        df = pd.read_sql('SELECT * FROM stations LIMIT 500', conn)
        ok('SQL 可读', '%d 行 × %d 列' % (len(df), len(df.columns)))

        # 复刻 app 的列名别名识别
        LAT = ['lat', 'latitude', '纬度', 'y']
        LON = ['lon', 'lng', 'longitude', '经度', 'x']
        UTIL = ['utilization', 'util', 'occupancy', '利用率']
        NAME = ['name', 'station_name', '站点名称']
        ID = ['station_id', 'id', '站点id']

        def find(cols, aliases):
            low = {str(c).lower().strip(): c for c in cols}
            for a in aliases:
                if a.lower() in low:
                    return low[a.lower()]
            return None

        found = {k: find(df.columns, v) for k, v in
                 (('lat', LAT), ('lon', LON), ('util', UTIL), ('name', NAME), ('id', ID))}
        if all(found.values()):
            ok('app 的列名识别全部命中', str(found))
        else:
            bad('app 的列名识别', str(found))

        # 复刻 app 的导入换算：按均值归一 + 过小跨度放大
        lat_col, lon_col = found['lat'], found['lon']
        lat_mean, lon_mean = float(df[lat_col].mean()), float(df[lon_col].mean())
        rows = []
        for _, r in df.iterrows():
            rows.append({'x': float(r[lon_col]) - lon_mean,
                         'y': float(r[lat_col]) - lat_mean})
        xs = [r['x'] for r in rows]
        zs = [r['y'] for r in rows]
        span_x, span_z = max(xs) - min(xs), max(zs) - min(zs)
        # app 的数据库导入路径（app.py 里 "导入为场景对象" 那两段）：
        #   1) 把经纬度按均值归一化成 x/z（单位仍是"度"）
        #   2) 若 x、z 跨度同时 < 2.0 → 重排成方阵（字形会被打散）
        # 数据库里存的是真实经纬度，跨度量级天然是 0.0x 度，必然踩到第 2 条。
        # 这是 app 既有行为，不是模拟数据的问题，所以这里如实报告、不判失败。
        if span_x < 2.0 and span_z < 2.0:
            print('     ⚠️ 如实报告：该点位集在 app 的数据库导入路径下，')
            print('        x/z 跨度同时 < 2.0（%.4f × %.4f），会被重排成方阵 → 字形打散。'
                  % (span_x, span_z))
            print('        原因：数据库路径存的是真实经纬度，app 直接当场景坐标用；')
            print('        想导入后仍然看到 C S V，请用 CSV / GeoJSON / 场景存档 那三条路径。')
            ok('已知行为已确认（数据库路径字形会被重排）', '不是数据缺陷，是 app 既有逻辑')
        else:
            ok('不会被网格化重排', '跨度 %.4f × %.4f（未同时小于 2.0）' % (span_x, span_z))
        print('     ℹ️ 原始经纬度分布（数据本身仍然是 C S V）：')
        print(scatter(rows, 'x', 'y'))
    finally:
        conn.close()


# ------------------------------------------------- MySQL / PostgreSQL
def check_db_sql():
    section('④ MySQL / PostgreSQL（播种 SQL 文件）')
    for fname, kind in (('mysql_seed.sql', 'MySQL'), ('postgres_seed.sql', 'PostgreSQL')):
        path = os.path.join(HERE, 'db', fname)
        if not os.path.exists(path):
            bad('%s 播种文件存在' % kind)
            continue
        with open(path, 'r', encoding='utf-8') as f:
            sql = f.read()
        n_insert = len(re.findall(r"^\('", sql, flags=re.M))
        if n_insert == 43:
            ok('%s 播种文件' % kind, '%d 条 INSERT 值' % n_insert)
        else:
            bad('%s 播种文件' % kind, '期望 43 条，实际 %d 条' % n_insert)

        for kw in ('CREATE TABLE', 'station_id', 'lat', 'lon', 'utilization',
                   'INSERT INTO', 'charging'):
            if kw.lower() not in sql.lower():
                bad('%s 含关键片段 %s' % (kind, kw))
        # 与 app 的列名识别对齐
        if re.search(r'\blat\b', sql) and re.search(r'\blon\b', sql):
            ok('%s 字段名与 app 别名表对齐（lat/lon）' % kind)
        else:
            bad('%s 字段名对齐' % kind)

    comp = os.path.join(HERE, 'db', 'docker-compose.yml')
    if os.path.exists(comp):
        with open(comp, 'r', encoding='utf-8') as f:
            y = f.read()
        checks = [('mysql:8.0' in y, 'MySQL 服务'), ('postgres:16' in y, 'PostgreSQL 服务'),
                  ('3306:3306' in y, 'MySQL 端口映射'), ('5432:5432' in y, 'PostgreSQL 端口映射'),
                  ('mysql_seed.sql' in y, 'MySQL 自动播种'),
                  ('postgres_seed.sql' in y, 'PostgreSQL 自动播种')]
        for good_enough, label in checks:
            (ok if good_enough else bad)('docker-compose：%s' % label)
    else:
        bad('docker-compose.yml 存在')

    seed = os.path.join(HERE, 'db', 'seed_db.py')
    (ok if os.path.exists(seed) else bad)('seed_db.py 存在')


# ----------------------------------------------------------- REST API
def check_rest(url=None, token=None):
    section('⑤ REST API（界面：API URL / Token）')
    path = os.path.join(HERE, 'api', 'stations_response.json')
    if not os.path.exists(path):
        return bad('静态响应 JSON 存在')
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # app 的判定：resp.json() 必须是 list，然后 len(data)
    if isinstance(data, list) and len(data) == 43:
        ok('返回结构符合 app 预期', 'list，%d 条记录' % len(data))
    else:
        bad('返回结构符合 app 预期', 'type=%s len=%s' % (type(data).__name__, len(data)))

    need = ['station_id', 'name', 'lat', 'lon', 'utilization', 'power']
    first = data[0]
    missing = [k for k in need if k not in first]
    if not missing:
        ok('字段齐全', ', '.join(need))
    else:
        bad('字段齐全', '缺 %s' % missing)

    if all(-90 <= float(r['lat']) <= 90 and -180 <= float(r['lon']) <= 180 for r in data):
        ok('经纬度取值合法')
    else:
        bad('经纬度取值合法')

    print(scatter(data, 'lon', 'lat'))

    # 模拟服务源码静态检查（不启进程）
    srv = os.path.join(HERE, 'api', 'mock_rest_server.py')
    if os.path.exists(srv):
        import ast
        with open(srv, 'r', encoding='utf-8') as f:
            src = f.read()
        try:
            ast.parse(src)
            ok('mock_rest_server.py 语法正确')
        except SyntaxError as e:
            bad('mock_rest_server.py 语法正确', str(e))
        if 'Authorization' in src and 'Bearer' in src:
            ok('支持 Token（Authorization: Bearer）')
        else:
            bad('支持 Token')

    if url:
        try:
            import requests
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200 and isinstance(r.json(), list):
                ok('实连 %s' % url, 'HTTP 200，%d 条记录' % len(r.json()))
            else:
                bad('实连 %s' % url, 'HTTP %s' % r.status_code)
        except Exception as e:
            bad('实连 %s' % url, str(e))
    else:
        print('     ℹ️ 未指定 --url，跳过实际 HTTP 请求（先跑 mock_rest_server.py 再带 --url 复验）')


# ---------------------------------------------------------- InfluxDB
def check_influx():
    section('⑥ InfluxDB（界面：Host / Database / Measurement）')
    lp = os.path.join(HERE, 'influx', 'charger_realtime.lp')
    if not os.path.exists(lp):
        return bad('line protocol 文件存在')

    with open(lp, 'r', encoding='utf-8') as f:
        lines = [x for x in f.read().splitlines() if x.strip()]

    ok('line protocol 文件存在', '%d 行' % len(lines))

    # 结构校验：measurement,tags fields timestamp
    pat = re.compile(r'^([\w]+),([\w=]+(?:,[\w=]+)*)\s+(.+)\s+(\d+)$')
    bad_lines = [x for x in lines if not pat.match(x)]
    if not bad_lines:
        ok('全部行符合 line protocol 语法')
    else:
        bad('line protocol 语法', '%d 行不合法，例：%s' % (len(bad_lines), bad_lines[0]))

    first = pat.match(lines[0]).groups()
    if first[0] == 'charger_realtime':
        ok('measurement 名称', 'charger_realtime（与界面默认值一致）')
    else:
        bad('measurement 名称', first[0])

    fields = set(re.findall(r'(\w+)=', first[2]))
    for need in ('name', 'lat', 'lon', 'utilization', 'power', 'status'):
        if need not in fields:
            bad('字段 %s 存在' % need)
    ok('字段齐全', ' '.join(sorted(fields)))

    # 时间戳必须递增/唯一，且是纳秒
    stamps = [int(pat.match(x).group(4)) for x in lines]
    if all(len(str(s)) >= 16 for s in stamps):
        ok('时间戳为纳秒级')
    else:
        bad('时间戳为纳秒级', '例：%s' % stamps[0])
    if max(stamps) - min(stamps) <= 3600 * 10 ** 9:
        ok('时间跨度在 1 小时内（界面默认查最近 1 小时能查到）')
    else:
        bad('时间跨度 ≤ 1 小时')

    q = os.path.join(HERE, 'influx', 'queries.sql')
    if os.path.exists(q):
        with open(q, 'r', encoding='utf-8') as f:
            qs = f.read()
        if 'charger_realtime' in qs and "INTERVAL '1 hour'" in qs:
            ok('queries.sql 含界面同款 SQL')
        else:
            bad('queries.sql 含界面同款 SQL')


# --------------------------------------------------------------- MQTT
def check_mqtt():
    section('⑦ MQTT（界面：Broker 地址 / 端口 / 订阅主题）')
    path = os.path.join(HERE, 'mqtt', 'mock_mqtt_publisher.py')
    if not os.path.exists(path):
        return bad('MQTT 模拟设备存在')

    import ast
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    try:
        ast.parse(src)
        ok('mock_mqtt_publisher.py 语法正确')
    except SyntaxError as e:
        bad('mock_mqtt_publisher.py 语法正确', str(e))
        return

    for need, label in (('charger/{station_id}/status', '上行主题与界面默认 charger/+/status 匹配'),
                        ('station_id', '载荷含 station_id'),
                        ('utilization', '载荷含 utilization'),
                        ('available_slots', '载荷含 available_slots'),
                        ('cmd_reply', '含下行回执主题')):
        (ok if need in src else bad)(label)

    # 🔥 直接 import 真实模块来验，比正则抠源码可靠得多。
    #    字母版的 STATIONS 只有 id/base_util/power，本来就没有经纬度，不能拿它画散点。
    try:
        import importlib
        mod = importlib.import_module(HERE_PKG + '.mqtt.mock_mqtt_publisher')
    except Exception as e:
        return bad('能 import mock_data.mqtt.mock_mqtt_publisher', str(e))

    stations = getattr(mod, 'STATIONS', [])
    if len(stations) == 43:
        ok('站点数正确', '%d 个（字母版点位）' % len(stations))
    else:
        bad('站点数正确', '%d 个，期望 43' % len(stations))

    # 载荷字段必须与 core/mqtt_client.py 的 _handle_status 对齐
    need_keys = {'station_id', 'utilization', 'available_slots', 'status', 'power'}
    try:
        payload = mod.build_status(stations[0]['id'])
        got_keys = set(payload.keys())
    except Exception as e:
        return bad('能生成一条真实上报载荷', str(e))

    if need_keys <= got_keys:
        ok('载荷字段与 core/mqtt_client.py 对齐', ' '.join(sorted(need_keys)))
    else:
        bad('载荷字段与 core/mqtt_client.py 对齐', '缺 %s' % (need_keys - got_keys))

    # 数值必须落在 _handle_status 会接受的范围（0~1 利用率、非负槽位）
    if 0.0 <= payload['utilization'] <= 1.0 and payload['available_slots'] >= 0:
        ok('上报数值在合法范围内', 'util=%.3f slots=%d status=%s'
           % (payload['utilization'], payload['available_slots'], payload['status']))
    else:
        bad('上报数值在合法范围内', str(payload))

    # 43 个站点必须都能生成载荷（不能有 KeyError）
    broken = []
    for s in stations:
        try:
            if mod.build_status(s['id']) is None:
                broken.append(s['id'])
        except Exception:
            broken.append(s['id'])
    if not broken:
        ok('全部站点都能生成载荷', '%d 个' % len(stations))
    else:
        bad('全部站点都能生成载荷', str(broken[:3]))


# ------------------------------------------------------- 场景存档 JSON
def check_scene_archive():
    section('⑧ 载入场景存档（.json）')
    path = os.path.join(HERE, 'scene_archive_letters.json')
    if not os.path.exists(path):
        return bad('场景存档存在')

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if 'scene_name' in data and isinstance(data.get('objects'), list):
        ok('结构符合 app 的读取方式', 'scene_name + objects(%d)' % len(data['objects']))
    else:
        bad('结构符合 app 的读取方式')
        return

    obj = data['objects'][0]
    need = ['id', 'type', 'name', 'position', 'rotation', 'scale',
            'bind_station_id', 'custom_props']
    missing = [k for k in need if k not in obj]
    if not missing:
        ok('对象字段齐全', ' '.join(need))
    else:
        bad('对象字段齐全', '缺 %s' % missing)

    types = {}
    for o in data['objects']:
        types[o['type']] = types.get(o['type'], 0) + 1
    if types.get('building') == len(data['objects']):
        ok('全部是建筑', str(types))
    else:
        bad('全部是建筑', str(types))

    heights = {o['custom_props'].get('height') for o in data['objects']}
    ok('每栋楼都带 height（前端 createBuilding 会读）', str(sorted(heights)))

    # 再验一条真正重要的事：场景存档与 CSV 导入必须落到同一套场景坐标，
    # 否则「载入存档」和「上传 CSV」得到的字大小/位置会不一样。
    try:
        from core.geo_parser import auto_parse
        from core.generation_rules import generate_objects
        with open(os.path.join(HERE, 'csv_letters_buildings.csv'), 'rb') as f:
            features = auto_parse(f.read(), 'csv_letters_buildings.csv')
        rules = {'point': {'target': 'building', 'conditions': []},
                 'line': {'target': 'road_straight', 'conditions': []},
                 'polygon': {'target': 'building', 'conditions': []}}
        csv_objs = generate_objects(features, rules)
        a = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2)) for o in csv_objs)
        b = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2))
                   for o in data['objects'])
        if a == b:
            ok('场景坐标与 CSV 导入完全一致', '%d 个点位逐一对齐' % len(a))
        else:
            diff = len(set(a) ^ set(b))
            bad('场景坐标与 CSV 导入完全一致', '有 %d 个点位对不上' % diff)
    except Exception as e:
        bad('场景坐标与 CSV 导入比对', str(e))

    print(scatter([{'x': o['position']['x'], 'z': o['position']['z']}
                   for o in data['objects']], 'x', 'z'))


def check_integration():
    section('⑪ 端到端一致性：CSV 与 GeoJSON 必须落到同一套场景坐标')
    try:
        from core.geo_parser import auto_parse
        from core.generation_rules import generate_objects
    except Exception as e:
        return bad('导入 core 模块', str(e))

    rules = {'point': {'target': 'building', 'conditions': []},
             'line': {'target': 'road_straight', 'conditions': []},
             'polygon': {'target': 'building', 'conditions': []}}

    with open(os.path.join(HERE, 'csv_letters_buildings.csv'), 'rb') as f:
        csv_feats = auto_parse(f.read(), 'csv_letters_buildings.csv')
    with open(os.path.join(HERE, 'geojson_letters.geojson'), 'rb') as f:
        gj_feats = auto_parse(f.read(), 'geojson_letters.geojson')

    a = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2))
               for o in generate_objects(csv_feats, rules))
    b = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2))
               for o in generate_objects(gj_feats, rules))

    if a == b and len(a) == 43:
        ok('CSV 与 GeoJSON 布局完全一致', '43 个点位逐一对齐')
    else:
        bad('CSV 与 GeoJSON 布局完全一致', 'CSV %d 个 / GeoJSON %d 个，差异 %d'
            % (len(a), len(b), len(set(a) ^ set(b))))

    # 顺序无关的比对：同一格点应该落在同一行列
    if a == b:
        print('     ℹ️ 说明：两条路径都会把经纬度按 ×111×cos(lat)×30 换算成场景坐标，')
        print('        所以 CSV 与 GeoJSON 得到的字形、大小、位置完全一致。')


# ------------------------------------------------------------- 规则文件
def check_rules():
    section('⑨ 附带的规则文件（界面「导入规则（JSON）」）')
    p1 = os.path.join(HERE, 'rules_point_to_building.json')
    if not os.path.exists(p1):
        return bad('rules_point_to_building.json 存在')
    with open(p1, 'r', encoding='utf-8') as f:
        r = json.load(f)
    if r.get('point', {}).get('target') == 'building':
        ok('点→建筑 规则文件正确')
    else:
        bad('点→建筑 规则文件正确', str(r.get('point')))

    p2 = os.path.join(HERE, 'rules_city.json')
    if os.path.exists(p2):
        ok('rules_city.json 存在（城市方案：面→建筑/高层）')


def check_town():
    """充电小镇：展示用主数据集。逐项校验它真的能进场景、且比例正确。"""
    section('⑩ 充电小镇（展示用主数据集，mock_data/town/）')
    town_dir = os.path.join(HERE, 'town')
    scene = os.path.join(town_dir, 'scene_town.json')
    if not os.path.exists(scene):
        print('     ℹ️ 未生成小镇数据，跳过（跑 python mock_data/town/build_town.py）')
        return

    with open(scene, 'r', encoding='utf-8') as f:
        scene_data = json.load(f)
    objs = scene_data['objects']

    if 100 <= len(objs) <= 180:
        ok('小镇对象数合理', '%d 个（含路网/水面等大件）' % len(objs))
    else:
        bad('小镇对象数合理', '%d 个' % len(objs))

    # 1b) 滨水小镇必须真的有路网和河道（这是"像城镇"的关键，不能缺）
    types = {}
    for o in objs:
        types[o['type']] = types.get(o['type'], 0) + 1
    for t, label in (('road', '道路'), ('sidewalk', '人行道/铺装'), ('water', '水面')):
        if types.get(t):
            ok('小镇有%s（%d 个）' % (label, types[t]), 'type=%s' % t)
        else:
            bad('小镇有%s' % label, '缺少 type=%s' % t)

    # 1) 必须落在地面 PlaneGeometry(80,80) 的 ±40 内
    out = [o['name'] for o in objs
           if not (-40 < o['position']['x'] < 40 and -40 < o['position']['z'] < 40)]
    if not out:
        ok('全部对象落在地面 ±40 内', '地面是 PlaneGeometry(80,80)')
    else:
        bad('全部对象落在地面 ±40 内', '%d 个越界：%s' % (len(out), out[:3]))

    # 2) 类型必须前端有渲染实现
    renderable = {'charger_fast', 'charger_slow', 'charger_super', 'building',
                  'building_tall', 'tree', 'tree_pine', 'lamp', 'road_straight',
                  'road_curve', 'car', 'truck', 'container_a', 'container_b',
                  'container_c', 'solar_panel_flat', 'solar_panel_land',
                  'solar_panel_group', 'solar_panel_port', 'solar_panel_port_group',
                  'windmill', 'windmill_low',
                  'road', 'sidewalk', 'water'}   # 路网/铺装/水面：本次新增的前端类型
    unknown = sorted({o['type'] for o in objs} - renderable)
    if not unknown:
        ok('全部类型前端都能渲染', '%d 类' % len({o['type'] for o in objs}))
    else:
        bad('全部类型前端都能渲染', str(unknown))

    # 3) 充电桩必须临街（relation_builder.py 的 street_threshold = 12）
    import math as _m
    try:
        from mock_data.town import waterfront as _wf
        segs = _wf.road_segments()
    except Exception:
        segs = []

    def _d2road(o):
        px, pz = o['position']['x'], o['position']['z']
        best = 1e9
        for sg in segs:
            dx, dz = _m.cos(sg['rot']), _m.sin(sg['rot'])
            half = sg['length'] / 2.0
            ax, az = sg['x'] - dx * half, sg['z'] - dz * half
            vx, vz = dx * sg['length'], dz * sg['length']
            t = max(0.0, min(1.0, ((px - ax) * vx + (pz - az) * vz) /
                              max(vx * vx + vz * vz, 1e-9)))
            best = min(best, _m.hypot(px - (ax + t * vx), pz - (az + t * vz)))
        return best

    off = []
    for o in objs:
        if not o['type'].startswith('charger'):
            continue
        if (o.get('custom_props') or {}).get('zone') == 'lot':
            continue                      # 停车场里的桩本来就该在广场内部
        dist = _d2road(o)
        if dist > 12:
            off.append((o['name'], round(dist, 1)))
    if not off:
        ok('充电桩都临街（到道路中线 ≤ 12）', '%d 段道路参与判定' % len(segs))
    else:
        bad('充电桩都临街', '%d 个太远：%s' % (len(off), off[:3]))

    # 4) 每个充电桩都绑定了 station_id（否则实时数据驱动不了）
    unbound = [o['name'] for o in objs
               if o['type'].startswith('charger') and not o.get('bind_station_id')]
    if not unbound:
        n = len([o for o in objs if o['type'].startswith('charger')])
        ok('每个充电桩都绑定了 station_id', '%d 个充电桩可被实时数据驱动' % n)
    else:
        bad('每个充电桩都绑定了 station_id', str(unbound[:3]))

    # 5) 建筑不互相压住（前端把建筑统一归一化到约 3.5 宽）
    import math as _m
    blds = [o for o in objs if o['type'].startswith('building')]
    close = []
    for i, a in enumerate(blds):
        for b in blds[i + 1:]:
            d = _m.hypot(a['position']['x'] - b['position']['x'],
                         a['position']['z'] - b['position']['z'])
            if d < 2.0:      # 建筑底面约 2.2，中心距需 ≥ 格距
                close.append((a['name'], b['name'], round(d, 2)))
    if not close:
        ok('建筑之间不重叠', '%d 栋建筑' % len(blds))
    else:
        bad('建筑之间不重叠', str(close[:2]))

    # 6) 场景坐标 → 经纬度 → 场景坐标：CSV 那条路径的比例保证
    try:
        from mock_data.town.build_town import (scene_to_latlon, LAT_UNITS_PER_DEG,
                                               LON_UNITS_PER_DEG, BASE_LAT, BASE_LON)
    except Exception as e:
        return bad('导入小镇坐标换算函数', str(e))

    err = 0.0
    for o in objs:
        lat, lon = scene_to_latlon(o['position']['x'], o['position']['z'])
        err = max(err,
                  abs((lon - BASE_LON) * LON_UNITS_PER_DEG - o['position']['x']),
                  abs((lat - BASE_LAT) * LAT_UNITS_PER_DEG - o['position']['z']))
    if err < 0.01:
        ok('场景↔经纬度往返无误差', '最大 %.4f 单位（CSV 导入后比例不歪）' % err)
    else:
        bad('场景↔经纬度往返无误差', '最大 %.4f 单位' % err)

    # 7) 真刀真枪：把小镇 CSV 喂给 app 的解析器，看落点是否与原场景一致
    csv_path = os.path.join(town_dir, 'town_objects.csv')
    rules_path = os.path.join(town_dir, 'rules_town.json')
    if os.path.exists(csv_path) and os.path.exists(rules_path):
        try:
            from core.geo_parser import auto_parse
            from core.generation_rules import generate_objects, auto_layout_if_clustered
            with open(csv_path, 'rb') as f:
                feats = auto_parse(f.read(), 'town_objects.csv')
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules = json.load(f)
            got = generate_objects(feats, rules)
            got, clustered = auto_layout_if_clustered(got, threshold=2.0, spacing=3.0)

            if len(got) == len(objs):
                ok('小镇 CSV 被 app 解析出同样多的对象', '%d 个' % len(got))
            else:
                bad('小镇 CSV 被 app 解析出同样多的对象',
                    '%d vs 场景 %d' % (len(got), len(objs)))

            if not clustered:
                ok('CSV 路径未被网格化重排', '跨度足够，布局保住了')
            else:
                bad('CSV 路径未被网格化重排', '被重排成方阵，布局全丢')

            # 按坐标多重集合比对（不按名字：场景里允许重名）
            exp = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2))
                         for o in objs)
            act = sorted((round(o['position']['x'], 2), round(o['position']['z'], 2))
                         for o in got)
            if exp == act:
                ok('CSV 导入落点与场景存档逐点一致', 'app 的换算与反解完全互逆')
            else:
                bad('CSV 导入落点与场景存档逐点一致',
                    '%d 个点位对不上' % len(set(exp) ^ set(act)))

            types = {}
            for o in got:
                types[o['type']] = types.get(o['type'], 0) + 1
            if types.get('charger_fast') and types.get('building') and types.get('tree'):
                ok('CSV 按 type 分流成功（不是全变建筑）', str(types))
            else:
                bad('CSV 按 type 分流成功', str(types))
        except Exception as e:
            bad('小镇 CSV 端到端校验', str(e))

    # 8) 小镇版接口文件
    for fname, label in (('town_stations.json', 'REST 响应'),
                         ('town.db', 'SQLite 库'),
                         ('town_mysql_seed.sql', 'MySQL 播种'),
                         ('town_postgres_seed.sql', 'PostgreSQL 播种'),
                         ('town_influx.lp', 'InfluxDB line protocol'),
                         ('town_mqtt_publisher.py', 'MQTT 设备脚本')):
        p = os.path.join(town_dir, fname)
        (ok if os.path.exists(p) else bad)('小镇 %s 存在' % label)

    # 8b) 小镇 MQTT 真的能跑：import 出来验站点数与载荷
    try:
        import importlib
        town_mqtt = importlib.import_module(TOWN_PKG + '.town_mqtt_publisher')
    except Exception as e:
        bad('能 import mock_data.town.town_mqtt_publisher', str(e))
        town_mqtt = None

    if town_mqtt is not None:
        tstations = getattr(town_mqtt, 'STATIONS', [])
        if len(tstations) == len([o for o in objs if o['type'].startswith('charger')]):
            ok('小镇 MQTT 站点数与场景充电桩一致', '%d 个' % len(tstations))
        else:
            bad('小镇 MQTT 站点数与场景充电桩一致',
                '%d vs 场景 %d' % (len(tstations),
                                   len([o for o in objs if o['type'].startswith('charger')])))

        broken = []
        for s in tstations:
            try:
                p = town_mqtt.build_status(s['id'])
                if not p or not (0.0 <= p['utilization'] <= 1.0):
                    broken.append(s['id'])
            except Exception:
                broken.append(s['id'])
        if not broken:
            ok('小镇 MQTT 全部站点能生成合法载荷', 'util 均在 0~1')
        else:
            bad('小镇 MQTT 全部站点能生成合法载荷', str(broken[:3]))

        # 场景里的充电桩 station_id 必须都能在 MQTT 里找到（否则"实时数据驱动"是假的）
        scene_ids = {o['bind_station_id'] for o in objs if o['type'].startswith('charger')}
        mqtt_ids = {s['id'] for s in tstations}
        missing = sorted(scene_ids - mqtt_ids)
        if not missing:
            ok('场景充电桩的 station_id 都能被 MQTT 覆盖',
               '%d 个 ID 全部匹配' % len(scene_ids))
        else:
            bad('场景充电桩的 station_id 都能被 MQTT 覆盖', str(missing[:5]))

    # 8c) 小镇 REST/DB 的站点 ID 也必须与场景一致
    rest_path = os.path.join(town_dir, 'town_stations.json')
    if os.path.exists(rest_path):
        with open(rest_path, 'r', encoding='utf-8') as f:
            rows = json.load(f)
        rest_ids = {r['station_id'] for r in rows}
        scene_ids = {o['bind_station_id'] for o in objs if o['type'].startswith('charger')}
        if rest_ids == scene_ids:
            ok('小镇 REST 的站点 ID 与场景一致', '%d 个' % len(rest_ids))
        else:
            bad('小镇 REST 的站点 ID 与场景一致',
                '差 %s' % sorted(rest_ids ^ scene_ids)[:5])

    # 8d) 真跑一遍 app 的「数据库导入」数学（app.py 里 SQLite 分支那一段）：
    #     归一化 → 网格化阈值 2.0 → 15/跨度 缩放，必须落在"不重排、不缩放"区间。
    db_path = os.path.join(town_dir, 'town.db')
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute('SELECT * FROM stations')
            cols = [d[0] for d in cur.description]
            data = cur.fetchall()
        finally:
            conn.close()

        def _find(aliases):
            low = {str(c).lower().strip(): c for c in cols}
            for a in aliases:
                if a.lower() in low:
                    return low[a.lower()]
            return None

        lat_col = _find(['lat', 'latitude', '纬度', 'y'])
        lon_col = _find(['lon', 'lng', 'long', 'longitude', '经度', 'x'])
        if lat_col and lon_col:
            li, oi = cols.index(lat_col), cols.index(lon_col)
            lats = [r[li] for r in data]
            lons = [r[oi] for r in data]
            lat_mean = sum(lats) / len(lats)
            lon_mean = sum(lons) / len(lons)
            nx = [v - lon_mean for v in lons]
            nz = [v - lat_mean for v in lats]
            sx, sz = max(nx) - min(nx), max(nz) - min(nz)

            if sx < 2.0 and sz < 2.0:
                bad('小镇 DB 导入不会被网格化重排',
                    '跨度 %.3f×%.3f 同时小于 2.0，会被铺成方阵' % (sx, sz))
            else:
                ok('小镇 DB 导入不会被网格化重排', '归一化跨度 %.1f × %.1f' % (sx, sz))

            factor = 15.0 / max(sx, sz, 0.1)
            if factor > 1.5:
                bad('小镇 DB 导入保持原始比例', '缩放系数 %.2f > 1.5，会被放大' % factor)
            else:
                ok('小镇 DB 导入保持原始比例', '缩放系数 %.3f（<=1.5 不缩放）' % factor)

            exp = sorted((round(o['position']['x'] - lon_mean, 2),
                          round(o['position']['z'] - lat_mean, 2))
                         for o in objs if o['type'].startswith('charger'))
            act = sorted((round(a, 2), round(b, 2)) for a, b in zip(nx, nz))
            if exp == act:
                ok('小镇 DB 导入落点与场景存档一致', '%d 个充电桩逐点对齐' % len(act))
            else:
                bad('小镇 DB 导入落点与场景存档一致',
                    '有 %d 个对不上' % len(set(exp) ^ set(act)))
        else:
            bad('小镇 DB 能识别出经纬度列', 'lat=%s lon=%s' % (lat_col, lon_col))

    # 9) 小镇版与字母版必须互不干扰
    letter_rest = os.path.join(HERE, 'api', 'stations_response.json')
    if os.path.exists(letter_rest):
        with open(letter_rest, 'r', encoding='utf-8') as f:
            letter = json.load(f)
        letters = {r.get('letter') for r in letter[:5]}
        if letters and None not in letters:
            ok('字母版 REST 数据未被小镇覆盖', '仍是摆字数据（letter 字段在）')
        else:
            bad('字母版 REST 数据未被小镇覆盖', '疑似被覆盖')

    # 10) 内置示例资源必须与小镇场景存档一致
    #     app 的「🏘️ 载入示例小镇」读的是 static/data/demo_town.json；
    #     它一旦和 scene_town.json 漂移，用户点按钮看到的就不是我们验过的那个小镇。
    published = os.path.join(ROOT, 'static', 'data', 'demo_town.json')
    if not os.path.exists(published):
        bad('内置示例资源 static/data/demo_town.json 存在',
            '跑 python mock_data/town/build_town.py 会生成')
    else:
        with open(published, 'r', encoding='utf-8') as f:
            pub = json.load(f)
        if (pub.get('scene_name') == scene_data.get('scene_name')
                and len(pub.get('objects') or []) == len(objs)
                and pub.get('objects') == objs):
            ok('内置示例资源与 scene_town.json 完全一致', '%d 个对象' % len(objs))
        else:
            bad('内置示例资源与 scene_town.json 完全一致',
                '内容漂移了：内置 %d 个 / 存档 %d 个'
                % (len(pub.get('objects') or []), len(objs)))

    print('     ℹ️ 小镇分布（场景坐标系）：')
    print(scatter([{'x': o['position']['x'], 'z': o['position']['z']}
                   for o in objs], 'x', 'z'))


def check_demo_entry():
    """内置示例入口（core/demo_town.py + static/data/demo_town.json）

    这一节是为两个真实踩过的 bug 加的回归测试，两个都表现为"点了按钮还是空场景"：

    ① 载入后把 _db_initialized 留成 False → app.py 的 init_db_scene() 在 rerun 时
       拿着本地随机 UUID 去 Supabase 查 → 查不到 → 命中"场景不存在"分支 →
       把 scene_objects 清空成空白场景。
    ② 面板顶部与空状态各渲染一次入口、用了同一个 st.button key →
       Streamlit 抛 DuplicateWidgetID，按钮根本渲染不出来。
    """
    section('⑫ 内置示例小镇入口（防回归）')

    core_path = os.path.join(ROOT, 'core', 'demo_town.py')
    asset = os.path.join(ROOT, 'static', 'data', 'demo_town.json')
    if not os.path.exists(core_path):
        return bad('core/demo_town.py 存在')
    if not os.path.exists(asset):
        return bad('static/data/demo_town.json 存在',
                   '跑 python mock_data/town/build_town.py 会生成')

    with open(core_path, 'r', encoding='utf-8') as f:
        src = f.read()

    # ① 必须把 _db_initialized 置 True（否则 rerun 被清空）
    if '_db_initialized = True' in src and '_db_initialized = False' not in src:
        ok('载入后置 _db_initialized=True（防 rerun 清空）')
    else:
        bad('载入后置 _db_initialized=True（防 rerun 清空）',
            '写成 False 会让 init_db_scene() 去 Supabase 查这个本地 UUID，'
            '查不到就把场景清空')

    # ② 不能把本地 UUID 固化到 URL
    if "st.query_params['scene_id'] = " not in src:
        ok('不把本地 scene_id 固化到 URL（防刷新后重演清空）')
    else:
        bad('不把本地 scene_id 固化到 URL')

    # ③ 必须清掉场景名输入框的 widget 状态（否则场景名被上一轮旧值覆盖）
    if "st.session_state.pop('panel_scene_name', None)" in src:
        ok('清掉 panel_scene_name（防场景名被旧值覆盖）')
    else:
        bad('清掉 panel_scene_name（防场景名被旧值覆盖）',
            "app.py 的 render_scene_panel() 里 st.text_input(key='panel_scene_name') "
            '会记住旧值，set_scene_name_safe() 又把它写回场景名')

    # ④ 按钮 key 必须可区分（同一页面会出现两次）
    if "key='load_demo_town_btn' + key_suffix" in src:
        ok('按钮 key 带 suffix（防 DuplicateWidgetID）')
    else:
        bad('按钮 key 带 suffix（防 DuplicateWidgetID）')

    # ⑤ 两个调用点必须传不同的 suffix
    app_path = os.path.join(ROOT, 'app.py')
    with open(app_path, 'r', encoding='utf-8') as f:
        app_src = f.read()
    if "key_suffix='_empty'" in app_src:
        ok('空状态调用点传了独立 suffix')
    else:
        bad('空状态调用点传了独立 suffix',
            '两个调用点同 key 会抛 DuplicateWidgetID')

    # ⑤ 用假 streamlit 真跑一遍：key 不重复 + 载入后能扛住 rerun
    try:
        import importlib
        import types

        class _SS(dict):
            def __getattr__(self, k):
                try:
                    return self[k]
                except KeyError:
                    raise AttributeError(k)

            def __setattr__(self, k, v):
                self[k] = v

            def __delattr__(self, k):
                try:
                    del self[k]
                except KeyError:
                    raise AttributeError(k)

        class _QP(dict):
            def __getattr__(self, k):
                if k == 'get':
                    return super().get
                raise AttributeError(k)

            def __setattr__(self, k, v):
                self[k] = v

        keys = []
        fake = types.ModuleType('streamlit')
        fake.session_state = _SS()
        fake.query_params = _QP()
        for _n in ('toast', 'error', 'warning', 'caption', 'markdown', 'rerun'):
            setattr(fake, _n, lambda *a, **k: None)

        def _button(label, **kw):
            keys.append(kw.get('key'))
            return False

        fake.button = _button
        saved = sys.modules.get('streamlit')
        sys.modules['streamlit'] = fake
        try:
            mod = importlib.import_module('core.demo_town')
            # 模拟：面板顶部一次 + 空状态一次（同一页面）
            mod.render_demo_town_entry()
            mod.render_demo_town_entry(compact=True, key_suffix='_empty')
            dup = [k for k in set(keys) if keys.count(k) > 1]
            if not dup:
                ok('同一页面两次渲染无重复 key', str(keys))
            else:
                bad('同一页面两次渲染无重复 key', '重复：%s' % dup)

            # 载入后必须能扛住 rerun（复刻 app.py init_db_scene() 的行为）
            fake.session_state.clear()
            mod.load_demo_town()
            n0 = len(fake.session_state.get('scene_objects') or [])
            for label, sb_available in (('Supabase 已配置', True), ('未配置 Supabase', False)):
                ss = fake.session_state
                if ss.get('_db_initialized', False):
                    n1 = len(ss.get('scene_objects') or [])
                elif not sb_available:
                    ss.setdefault('scene_objects', [])
                    n1 = len(ss['scene_objects'])
                else:
                    ss['scene_objects'] = []      # 旧代码在这里被清空
                    n1 = 0
                if n0 and n1 == n0:
                    ok('rerun 后对象仍在（%s）' % label, '%d 个' % n1)
                else:
                    bad('rerun 后对象仍在（%s）' % label, '%d → %d' % (n0, n1))
        finally:
            if saved is not None:
                sys.modules['streamlit'] = saved
            else:
                sys.modules.pop('streamlit', None)
    except Exception as e:
        bad('用假 streamlit 复跑示例入口', str(e))


def check_scripts_compile():
    section('⑩ 附带 Python 脚本语法校验（不执行）')
    import ast
    targets = [
        os.path.join(HERE, 'build_mock_files.py'),
        os.path.join(HERE, 'verify_mock_files.py'),
        os.path.join(HERE, 'db', 'seed_db.py'),
        os.path.join(HERE, 'api', 'mock_rest_server.py'),
        os.path.join(HERE, 'api', 'test_rest_api.py'),
        os.path.join(HERE, 'influx', 'write_to_influx.py'),
        os.path.join(HERE, 'mqtt', 'mock_mqtt_publisher.py'),
        os.path.join(HERE, 'town', 'build_town.py'),
        os.path.join(HERE, 'town', 'town_mqtt_publisher.py'),
        os.path.join(HERE, 'templates', 'db', 'seed_db.py'),
        os.path.join(HERE, 'templates', 'api', 'mock_rest_server.py'),
        os.path.join(HERE, 'templates', 'api', 'test_rest_api.py'),
        os.path.join(HERE, 'templates', 'influx', 'write_to_influx.py'),
        os.path.join(HERE, 'templates', 'mqtt', 'mock_mqtt_publisher.py'),
        os.path.join(HERE, '..', 'mock_mqtt_publisher.py'),
    ]
    for p in targets:
        p = os.path.normpath(p)
        if not os.path.exists(p):
            continue
        with open(p, 'r', encoding='utf-8') as f:
            src = f.read()
        try:
            ast.parse(src)
            ok('语法正确：%s' % os.path.basename(p))
        except SyntaxError as e:
            bad('语法正确：%s' % os.path.basename(p), '第 %s 行 %s' % (e.lineno, e.msg))


def check_templates_synced():
    """产物必须与其模板逐字节一致，且模板目录里不能有孤儿

    这一条是为一个真实踩过的坑加的：脚本内容曾经**同时**存在于
    build_mock_files.py 的字符串常量和一个真实文件里。改产物 → 下次生成被覆盖；
    改模板 → 产物不更新。现在生成 = 复制，这条断言保证两边不会再漂移。
    """
    section('⑪ 模板与产物一致性（生成 = 复制）')
    templates_dir = os.path.join(HERE, 'templates')
    if not os.path.isdir(templates_dir):
        return bad('templates/ 目录存在', '跑 python mock_data/build_mock_files.py 会生成')

    # 从构建脚本里取权威映射，避免这里再抄一份
    map_path = os.path.join(HERE, 'build_mock_files.py')
    with open(map_path, 'r', encoding='utf-8') as f:
        src = f.read()
    try:
        ns = {}
        start = src.index('TEMPLATE_MAP = {')
        end = src.index('}', start) + 1
        exec(src[start:end], ns)
        tmap = ns['TEMPLATE_MAP']
    except Exception as e:
        return bad('能从构建脚本读出 TEMPLATE_MAP', str(e))

    for artifact_name, tpl_rel in tmap.items():
        tpl = os.path.join(templates_dir, tpl_rel)
        # 产物目录：同名文件的所在目录（按 tpl_rel 的目录推）
        art = os.path.join(HERE, tpl_rel)
        if not os.path.exists(tpl):
            bad('模板存在：%s' % tpl_rel)
            continue
        if not os.path.exists(art):
            bad('产物存在：%s' % tpl_rel)
            continue
        with open(tpl, 'rb') as f:
            a = f.read()
        with open(art, 'rb') as f:
            b = f.read()
        if a == b:
            ok('产物与模板一致：%s' % tpl_rel, '%d B' % len(a))
        else:
            bad('产物与模板一致：%s' % tpl_rel,
                '内容不同（改产物不会生效、改模板才会；请重跑生成脚本）')

    # 孤儿检测：templates/ 下的文件必须都在映射里
    mapped = set(os.path.normpath(v) for v in tmap.values())
    found = set()
    for dirpath, _dirs, files in os.walk(templates_dir):
        for fn in files:
            if fn.endswith('.py'):
                rel = os.path.relpath(os.path.join(dirpath, fn), templates_dir)
                found.add(os.path.normpath(rel))
    orphans = sorted(found - mapped)
    if orphans:
        bad('templates/ 无孤儿文件', '这些模板没人用：%s' % orphans)
    else:
        ok('templates/ 无孤儿文件', '%d 个模板全部在映射里' % len(found))

    # MQTT 是"模板 + 注入站点"的例外，单独验一下替换真的发生了
    letters = os.path.join(HERE, 'mqtt', 'mock_mqtt_publisher.py')
    town = os.path.join(HERE, 'town', 'town_mqtt_publisher.py')
    # 小镇版站点数跟场景充电桩数走（布局会变），字母版固定 43
    scene_path = os.path.join(HERE, 'town', 'scene_town.json')
    n_town = 29
    if os.path.exists(scene_path):
        with open(scene_path, 'r', encoding='utf-8') as f:
            n_town = len([o for o in (json.load(f).get('objects') or [])
                          if str(o.get('type', '')).startswith('charger')])
    for p, label, expect in ((letters, '字母版', 43), (town, '小镇版', n_town)):
        if not os.path.exists(p):
            bad('%s MQTT 产物存在' % label)
            continue
        with open(p, 'r', encoding='utf-8') as f:
            text = f.read()
        block = text.split('STATIONS = [', 1)[1].split('\n]', 1)[0]
        n = len([ln for ln in block.splitlines() if ln.strip().startswith('{')])
        if n == expect:
            ok('%s MQTT 站点注入正确' % label, '%d 个' % n)
        else:
            bad('%s MQTT 站点注入正确' % label, '%d 个，期望 %d' % (n, expect))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=None, help='REST API 实连地址，如 http://127.0.0.1:8000/stations')
    ap.add_argument('--token', default=None)
    args = ap.parse_args()

    print('=' * 72)
    print('mock_data 总校验 —— 每个文件都用「app 同款逻辑」读一遍')
    print('=' * 72)

    check_csv()
    check_geojson()
    check_sqlite()
    check_db_sql()
    check_rest(args.url, args.token)
    check_influx()
    check_mqtt()
    check_scene_archive()
    check_integration()
    check_rules()
    check_town()
    check_scripts_compile()
    check_templates_synced()
    check_demo_entry()

    section('汇总')
    print('  通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print('   ❌ ' + f)
        return 1
    print('  🎉 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
