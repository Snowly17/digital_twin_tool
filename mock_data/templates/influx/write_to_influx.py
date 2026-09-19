# -*- coding: utf-8 -*-
"""
mock_data/influx/write_to_influx.py

把 charger_realtime.lp 写进 InfluxDB 3（Cloud Serverless 或本地 Core 均可）。

依赖：pip install influxdb3-python

用法：
    # 写入
    python mock_data/influx/write_to_influx.py \
        --host https://us-east-1-1.aws.cloud2.influxdata.com \
        --token <你的 API Token> \
        --database charging

    # 写完顺手查一下（复刻界面里的那条 SQL）
    python mock_data/influx/write_to_influx.py --host ... --token ... --database charging --query

界面里对应填：
    InfluxDB Host : https://us-east-1-1.aws.cloud2.influxdata.com
    Database      : charging
    Measurement   : charger_realtime
    时间范围      : 最近 1 小时
"""
import argparse
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
LP_PATH = os.path.join(HERE, 'charger_realtime.lp')
MEASUREMENT = 'charger_realtime'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True, help='InfluxDB 3 地址，去掉末尾斜杠')
    ap.add_argument('--token', required=True)
    ap.add_argument('--database', default='charging')
    ap.add_argument('--measurement', default=MEASUREMENT)
    ap.add_argument('--query', action='store_true', help='写入后按界面同款 SQL 查一次')
    args = ap.parse_args()

    try:
        from influxdb3 import InfluxDBClient3
    except ImportError:
        print('❌ 缺少依赖，请先：pip install influxdb3-python')
        return 1

    with open(LP_PATH, 'r', encoding='utf-8') as f:
        lp = f.read()
    print('📄 待写入 %d 行 line protocol' % len([x for x in lp.splitlines() if x.strip()]))

    client = InfluxDBClient3(host=args.host.rstrip('/'), token=args.token,
                             database=args.database)
    try:
        client.write(record=lp, measurement=args.measurement, precision='ns')
        print('✅ 已写入 measurement=%s / database=%s' % (args.measurement, args.database))

        if args.query:
            sql = ('SELECT * FROM "' + args.measurement + '" '
                   "WHERE time >= now() - INTERVAL '1 hour' "
                   "ORDER BY time DESC LIMIT 500")
            print('🔍 ' + sql)
            df = client.query(query=sql, language='sql').to_pandas()
            print('✅ 查到 %d 行，列：%s' % (len(df), list(df.columns)))
            print(df.head(10).to_string())
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
