# -*- coding: utf-8 -*-
"""
mock_data/api/mock_rest_server.py

零依赖（只用 Python 标准库）的 REST API 模拟服务，用来测界面里的
「多源数据接入 → REST API → API URL」这一栏。

启动：
    python mock_data/api/mock_rest_server.py
    python mock_data/api/mock_rest_server.py --port 8000 --token demo-token

启动后界面里这样填：
    API URL : http://127.0.0.1:8000/stations
    Token   : （留空，除非启动时带了 --token）

自带的路由：
    GET /stations               → JSON 数组（app 里 len(data) 直接可用）
    GET /stations?format=geojson→ 同一批数据转成 GeoJSON（可喂给 CSV/GeoJSON 导入）
    GET /health                 → {"status": "ok"}
"""
import argparse
import json
import os
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD_PATH = os.path.join(HERE, 'stations_response.json')

# 让 Windows 控制台不因 emoji / 中文 崩掉
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass


def load_payload():
    """读取要返回的数据文件。

    PAYLOAD_PATH 默认是字母版（43 条）；用 --payload 可以切到小镇版
    （mock_data/town/town_stations.json，29 条），或任何同结构的 JSON 数组。
    """
    with open(PAYLOAD_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError('%s 不是 JSON 数组（app 按 list 处理）' % PAYLOAD_PATH)
    return data


def to_geojson(rows):
    feats = []
    for r in rows:
        feats.append({
            'type': 'Feature',
            'properties': {k: v for k, v in r.items() if k not in ('lat', 'lon')},
            'geometry': {'type': 'Point', 'coordinates': [r['lon'], r['lat']]},
        })
    return {'type': 'FeatureCollection', 'features': feats}


class Handler(BaseHTTPRequestHandler):
    server_version = 'DTTMockREST/1.0'
    token = ''

    def log_message(self, fmt, *args):
        sys.stdout.write('  %s - %s\n' % (self.address_string(), fmt % args))

    def _send(self, code, obj, ctype='application/json; charset=utf-8'):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if self.token:
            auth = self.headers.get('Authorization', '')
            if auth != 'Bearer ' + self.token:
                self._send(401, {'error': 'unauthorized',
                                 'hint': '请求头需要 Authorization: Bearer <token>'})
                return

        if parsed.path in ('/stations', '/api/stations', '/'):
            rows = load_payload()
            if query.get('format', [''])[0] == 'geojson':
                self._send(200, to_geojson(rows))
            else:
                self._send(200, rows)
        elif parsed.path == '/health':
            self._send(200, {'status': 'ok', 'records': len(load_payload()),
                             'time': time.strftime('%Y-%m-%d %H:%M:%S')})
        else:
            self._send(404, {'error': 'not found', 'path': parsed.path})


def main():
    ap = argparse.ArgumentParser(description='REST API 模拟服务')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--token', default='', help='设置后请求必须带 Authorization: Bearer <token>')
    ap.add_argument('--payload', default=None,
                    help='要返回的 JSON 数组文件；默认 api/stations_response.json（字母版 43 条）。'
                         '想看充电小镇就传 mock_data/town/town_stations.json（29 条）')
    args = ap.parse_args()

    global PAYLOAD_PATH
    if args.payload:
        PAYLOAD_PATH = (args.payload if os.path.isabs(args.payload)
                        else os.path.join(os.getcwd(), args.payload))
        if not os.path.exists(PAYLOAD_PATH):
            print('❌ 找不到数据文件：%s' % PAYLOAD_PATH)
            return 1

    Handler.token = args.token
    try:
        rows = load_payload()
    except Exception as e:
        print('❌ 读取数据失败：%s' % e)
        return 1
    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    print('=' * 60)
    print('REST API 模拟服务已启动')
    print('  数据文件: %s' % PAYLOAD_PATH)
    print('  API URL : http://%s:%d/stations' % (args.host, args.port))
    print('  健康检查: http://%s:%d/health' % (args.host, args.port))
    print('  Token   : %s' % (args.token if args.token else '（未启用，留空即可）'))
    print('  记录数  : %d 条' % len(rows))
    print('=' * 60)
    print('按 Ctrl+C 停止')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n⏹️ 已停止')
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    main()
