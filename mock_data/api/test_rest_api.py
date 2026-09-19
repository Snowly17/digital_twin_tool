# -*- coding: utf-8 -*-
"""
mock_data/api/test_rest_api.py

在不打开界面的情况下，先验证 REST API 模拟数据能不能被「界面同款逻辑」读懂。

用法：
    # 1) 先启动服务
    python mock_data/api/mock_rest_server.py
    # 2) 再跑本脚本（带上 --url 与可选 --token）
    python mock_data/api/test_rest_api.py
    python mock_data/api/test_rest_api.py --url http://127.0.0.1:8000/stations --token demo-token

    # 也可以直接读本地 JSON 文件，不需要起服务
    python mock_data/api/test_rest_api.py --file mock_data/api/stations_response.json
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(url, token=None):
    import requests
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError('HTTP %s' % resp.status_code)
    return resp.json()


def load(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def render_ascii(rows, x_key='lon', y_key='lat'):
    """把经纬度画成字符画，用来肉眼确认是不是 C S V"""
    pts = [(float(r[x_key]), float(r[y_key])) for r in rows]
    if not pts:
        print('（无数据）')
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w, h = 64, 16
    grid = [[' '] * w for _ in range(h)]
    for x, y in pts:
        cx = int(round((x - min(xs)) / max(max(xs) - min(xs), 1e-12) * (w - 1)))
        cy = int(round((1 - (y - min(ys)) / max(max(ys) - min(ys), 1e-12)) * (h - 1)))
        grid[cy][cx] = '#'
    for line in grid:
        print('  ' + ''.join(line))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://127.0.0.1:8000/stations')
    ap.add_argument('--token', default=None)
    ap.add_argument('--file', default=None, help='直接读本地 JSON，跳过网络')
    args = ap.parse_args()

    if args.file:
        data = load(args.file if os.path.isabs(args.file)
                    else os.path.join(os.getcwd(), args.file))
        print('📂 已读取 ' + args.file)
    else:
        print('🌐 请求 ' + args.url)
        try:
            data = fetch(args.url, args.token)
        except Exception as e:
            print('❌ 请求失败：%s' % e)
            print('💡 确认服务已启动：python mock_data/api/mock_rest_server.py')
            return 1

    if not isinstance(data, list):
        print('❌ 返回不是列表（app 会按 list 处理）：%r' % type(data))
        return 1

    print('✅ 返回 %d 条记录（app 会显示“API 连接成功，返回 %d 条记录”）' % (len(data), len(data)))
    print('   首条：' + json.dumps(data[0], ensure_ascii=False))

    need = ['lat', 'lon']
    missing = [k for k in need if k not in data[0]]
    if missing:
        print('❌ 缺少字段：%s（界面无法识别经纬度）' % missing)
        return 1
    print('✅ 经纬度字段齐全')

    print('\n📍 数据分布（应该能看出 C S V）：')
    render_ascii(data)
    return 0


if __name__ == '__main__':
    sys.exit(main())
