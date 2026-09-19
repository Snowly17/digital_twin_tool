# -*- coding: utf-8 -*-
"""
mock_data/db/seed_db.py

不用 Docker，直接把 "C S V" 测试数据灌进已有的 MySQL / PostgreSQL。

用法：
    # MySQL（需要 pip install pymysql）
    python mock_data/db/seed_db.py --kind mysql --host localhost --port 3306 \
        --user root --password 123456 --database charging --table stations

    # PostgreSQL（需要 pip install psycopg2-binary）
    python mock_data/db/seed_db.py --kind postgres --host localhost --port 5432 \
        --user postgres --password postgres --database charging --table stations

    # 也可以直接用连接串
    python mock_data/db/seed_db.py --dsn "mysql+pymysql://root:123456@localhost:3306/charging"

    # 只灌 SQLite（不需要任何第三方库）
    python mock_data/db/seed_db.py --kind sqlite --database mock_data/test_stations.db

脚本里的站点数据与同目录下的 mysql_seed.sql / postgres_seed.sql 完全一致。
"""
import argparse
import math
import os
import sys

# 与 build_mock_files.py 保持同一套字模与坐标
PITCH = 0.0014
BASE_LAT = 22.5400
BASE_LON = 114.0500
GRID = [
    ".####...####...##....#",
    "#....#.#....#..#....#",
    "#.......####....#..#.",
    "#...........#....##..",
    "#....#.#....#....##..",
    ".####...####......##.",
]


def build_rows():
    n_cols = len(GRID[0])
    n_rows = len(GRID)
    rows = []
    idx = 0
    for r, line in enumerate(GRID):
        for c, ch in enumerate(line):
            if ch != '#':
                continue
            idx += 1
            letter = 'C' if c < 5 else ('S' if c < 11 else 'V')
            lat = BASE_LAT + ((n_rows - 1) / 2.0 - r) * PITCH
            lon = BASE_LON + (c - (n_cols - 1) / 2.0) * PITCH
            util = round(max(0.05, min(0.95, 0.5 + 0.42 * math.sin(idx * 0.7))), 3)
            status = '高负载' if util > 0.85 else ('离线' if util < 0.15 else '在线')
            power = 180 if util > 0.75 else (120 if util > 0.45 else 60)
            rows.append((
                "%s%03d" % (letter, idx),
                "测试站-%s%d%d" % (letter, r, c),
                round(lat, 6), round(lon, 6), util, power,
                max(0, int((1 - util) * 8)), status, letter,
            ))
    return rows


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  station_id      VARCHAR(32)  NOT NULL PRIMARY KEY,
  name            VARCHAR(64)  NOT NULL,
  lat             DOUBLE PRECISION NOT NULL,
  lon             DOUBLE PRECISION NOT NULL,
  utilization     DOUBLE PRECISION NOT NULL,
  power           INTEGER      NOT NULL,
  available_slots INTEGER      NOT NULL DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT '在线',
  letter          VARCHAR(4),
  updated_at      TIMESTAMP    NULL
)
"""

INSERT_SQL = (
    "INSERT INTO {table} (station_id, name, lat, lon, utilization, power,"
    " available_slots, status, letter) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def parse_dsn(dsn):
    """mysql+pymysql://user:pass@host:port/db  →  dict"""
    body = dsn.split('://', 1)[1]
    creds, rest = body.split('@', 1)
    user, _, password = creds.partition(':')
    hostport, _, database = rest.partition('/')
    host, _, port = hostport.partition(':')
    kind = 'postgres' if dsn.startswith('postgres') else 'mysql'
    return {
        'kind': kind, 'user': user, 'password': password, 'host': host,
        'port': int(port) if port else (5432 if kind == 'postgres' else 3306),
        'database': database,
    }


def seed_mysql(cfg, table):
    import pymysql
    conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                           password=cfg['password'], database=cfg['database'],
                           charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS " + table)
    cur.execute(CREATE_SQL.format(table=table))
    cur.executemany(INSERT_SQL.format(table=table), build_rows())
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM " + table)
    total = cur.fetchone()[0]
    conn.close()
    return total


def seed_postgres(cfg, table):
    import psycopg2
    conn = psycopg2.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                            password=cfg['password'], dbname=cfg['database'])
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS " + table)
    cur.execute(CREATE_SQL.format(table=table))
    cur.executemany(INSERT_SQL.format(table=table), build_rows())
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM " + table)
    total = cur.fetchone()[0]
    conn.close()
    return total


def seed_sqlite(db_path, table):
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS " + table)
    cur.execute(CREATE_SQL.format(table=table).replace('DOUBLE PRECISION', 'REAL')
                .replace('VARCHAR(32)', 'TEXT').replace('VARCHAR(64)', 'TEXT')
                .replace('VARCHAR(16)', 'TEXT').replace('VARCHAR(4)', 'TEXT')
                .replace('TIMESTAMP', 'TEXT'))
    cur.executemany(
        "INSERT INTO {t} (station_id, name, lat, lon, utilization, power,"
        " available_slots, status, letter) VALUES (?,?,?,?,?,?,?,?,?)".format(t=table),
        build_rows())
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM " + table)
    total = cur.fetchone()[0]
    conn.close()
    return total


def main():
    ap = argparse.ArgumentParser(description='把 "C S V" 测试数据灌进 MySQL / PostgreSQL / SQLite')
    ap.add_argument('--kind', choices=['mysql', 'postgres', 'sqlite'], default='mysql')
    ap.add_argument('--dsn', default=None, help='连接串，如 mysql+pymysql://root:123456@localhost:3306/charging')
    ap.add_argument('--host', default='localhost')
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--user', default=None)
    ap.add_argument('--password', default=None)
    ap.add_argument('--database', default=None)
    ap.add_argument('--table', default='stations')
    args = ap.parse_args()

    if args.dsn:
        cfg = parse_dsn(args.dsn)
    else:
        cfg = {
            'kind': args.kind,
            'host': args.host,
            'port': args.port or (5432 if args.kind == 'postgres' else 3306),
            'user': args.user or ('postgres' if args.kind == 'postgres' else 'root'),
            'password': args.password if args.password is not None else
                        ('postgres' if args.kind == 'postgres' else '123456'),
            'database': args.database or ('mock_data/test_stations.db'
                                          if args.kind == 'sqlite' else 'charging'),
        }

    kind = cfg['kind']
    print('▶ 目标：%s  %s:%s/%s  表=%s' % (
        kind, cfg['host'], cfg['port'], cfg['database'], args.table))
    try:
        if kind == 'sqlite':
            total = seed_sqlite(cfg['database'], args.table)
        elif kind == 'mysql':
            total = seed_mysql(cfg, args.table)
        else:
            total = seed_postgres(cfg, args.table)
    except ImportError as e:
        print('❌ 缺少依赖：%s' % e)
        print('   MySQL      → pip install pymysql')
        print('   PostgreSQL → pip install psycopg2-binary')
        return 1
    except Exception as e:
        print('❌ 灌数据失败：%s' % e)
        return 1

    print('✅ 完成，%s.%s 共 %d 行' % (cfg['database'], args.table, total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
