# -*- coding: utf-8 -*-
"""
mock_data/mqtt/mock_mqtt_publisher.py

MQTT 测试用模拟设备：向 broker 上报充电桩实时数据，并接受下行指令。
点位摆成 "C S V" 三个字母，方便在 3D 场景里一眼看出数据来自哪个文件。

对应界面：设置 → 工业协议接入（MQTT 实时流）
    Broker 地址 : broker.emqx.io
    端口        : 1883
    订阅主题    : charger/+/status

用法：
    python mock_data/mqtt/mock_mqtt_publisher.py
    python mock_data/mqtt/mock_mqtt_publisher.py --broker broker.emqx.io --interval 3
    python mock_data/mqtt/mock_mqtt_publisher.py --once     # 只发一轮，方便脚本化验证

依赖：pip install paho-mqtt
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print('❌ 缺少依赖，请先：pip install paho-mqtt')
    sys.exit(1)

BROKER = 'broker.emqx.io'
PORT = 1883
TOPIC_TEMPLATE = 'charger/{station_id}/status'
CMD_TOPIC_TEMPLATE = 'charger/{station_id}/cmd'
REPLY_TOPIC_TEMPLATE = 'charger/{station_id}/cmd_reply'
PUBLISH_INTERVAL = 3

# ===== 34 个点位，摆成 C S V（与 csv_letters_buildings.csv 同一套经纬度）=====
STATIONS = [
    {"id": "C001", "lat": 22.543500, "lon": 114.036700, "base_util": 0.771, "power": 180},
    {"id": "C002", "lat": 22.543500, "lon": 114.038100, "base_util": 0.914, "power": 180},
    {"id": "C003", "lat": 22.543500, "lon": 114.039500, "base_util": 0.863, "power": 180},
    {"id": "C004", "lat": 22.543500, "lon": 114.040900, "base_util": 0.641, "power": 120},
    {"id": "S005", "lat": 22.543500, "lon": 114.047900, "base_util": 0.353, "power": 60},
    {"id": "S006", "lat": 22.543500, "lon": 114.049300, "base_util": 0.134, "power": 60},
    {"id": "V007", "lat": 22.543500, "lon": 114.050700, "base_util": 0.087, "power": 60},
    {"id": "V008", "lat": 22.543500, "lon": 114.052100, "base_util": 0.235, "power": 60},
    {"id": "V009", "lat": 22.543500, "lon": 114.057700, "base_util": 0.507, "power": 120},
    {"id": "V010", "lat": 22.543500, "lon": 114.064700, "base_util": 0.776, "power": 180},
    {"id": "C011", "lat": 22.542100, "lon": 114.035300, "base_util": 0.915, "power": 180},
    {"id": "S012", "lat": 22.542100, "lon": 114.042300, "base_util": 0.859, "power": 180},
    {"id": "S013", "lat": 22.542100, "lon": 114.046500, "base_util": 0.634, "power": 120},
    {"id": "V014", "lat": 22.542100, "lon": 114.053500, "base_util": 0.346, "power": 60},
    {"id": "V015", "lat": 22.542100, "lon": 114.057700, "base_util": 0.131, "power": 60},
    {"id": "V016", "lat": 22.542100, "lon": 114.064700, "base_util": 0.089, "power": 60},
    {"id": "C017", "lat": 22.540700, "lon": 114.035300, "base_util": 0.240, "power": 60},
    {"id": "S018", "lat": 22.540700, "lon": 114.047900, "base_util": 0.514, "power": 120},
    {"id": "S019", "lat": 22.540700, "lon": 114.049300, "base_util": 0.781, "power": 180},
    {"id": "V020", "lat": 22.540700, "lon": 114.050700, "base_util": 0.916, "power": 180},
    {"id": "V021", "lat": 22.540700, "lon": 114.052100, "base_util": 0.855, "power": 180},
    {"id": "V022", "lat": 22.540700, "lon": 114.057700, "base_util": 0.627, "power": 120},
    {"id": "V023", "lat": 22.540700, "lon": 114.064700, "base_util": 0.340, "power": 60},
    {"id": "C024", "lat": 22.539300, "lon": 114.035300, "base_util": 0.127, "power": 60},
    {"id": "V025", "lat": 22.539300, "lon": 114.053500, "base_util": 0.090, "power": 60},
    {"id": "V026", "lat": 22.539300, "lon": 114.057700, "base_util": 0.246, "power": 60},
    {"id": "V027", "lat": 22.539300, "lon": 114.064700, "base_util": 0.521, "power": 120},
    {"id": "C028", "lat": 22.537900, "lon": 114.035300, "base_util": 0.786, "power": 180},
    {"id": "S029", "lat": 22.537900, "lon": 114.042300, "base_util": 0.917, "power": 180},
    {"id": "S030", "lat": 22.537900, "lon": 114.046500, "base_util": 0.851, "power": 180},
    {"id": "V031", "lat": 22.537900, "lon": 114.053500, "base_util": 0.621, "power": 120},
    {"id": "V032", "lat": 22.537900, "lon": 114.059100, "base_util": 0.333, "power": 60},
    {"id": "V033", "lat": 22.537900, "lon": 114.063300, "base_util": 0.124, "power": 60},
    {"id": "C034", "lat": 22.536500, "lon": 114.036700, "base_util": 0.092, "power": 60},
    {"id": "C035", "lat": 22.536500, "lon": 114.038100, "base_util": 0.252, "power": 60},
    {"id": "C036", "lat": 22.536500, "lon": 114.039500, "base_util": 0.528, "power": 120},
    {"id": "C037", "lat": 22.536500, "lon": 114.040900, "base_util": 0.792, "power": 180},
    {"id": "S038", "lat": 22.536500, "lon": 114.047900, "base_util": 0.918, "power": 180},
    {"id": "S039", "lat": 22.536500, "lon": 114.049300, "base_util": 0.847, "power": 180},
    {"id": "V040", "lat": 22.536500, "lon": 114.050700, "base_util": 0.614, "power": 120},
    {"id": "V041", "lat": 22.536500, "lon": 114.052100, "base_util": 0.327, "power": 60},
    {"id": "V042", "lat": 22.536500, "lon": 114.060500, "base_util": 0.121, "power": 60},
    {"id": "V043", "lat": 22.536500, "lon": 114.061900, "base_util": 0.094, "power": 60}
]

DEVICE_STATE = {
    s['id']: {'enabled': True, 'limit_kw': None,
              'base_util': s['base_util'], 'power': s['power'],
              'last_cmd': None}
    for s in STATIONS
}


def effective_util(state):
    util = state['base_util'] + random.uniform(-0.12, 0.12)
    if state['limit_kw'] is not None:
        ratio = max(0.15, float(state['limit_kw']) / max(1.0, float(state['power'])))
        util = util * ratio
    return max(0.02, min(0.95, round(util, 3)))


def build_status(sid):
    state = DEVICE_STATE.get(sid)
    if state is None:
        return None
    if not state['enabled']:
        return {'station_id': sid, 'utilization': 0.0, 'available_slots': 0,
                'status': '离线', 'power': 0.0, 'last_cmd': state['last_cmd'],
                'timestamp': datetime.now().isoformat()}

    util = effective_util(state)
    status = '在线'
    if util > 0.85:
        status = '高负载'
    elif random.random() < 0.02:
        status = '离线'
        util = 0.0
    power = state['power'] if state['limit_kw'] is None else min(state['power'], state['limit_kw'])
    return {
        'station_id': sid,
        'utilization': util,
        'available_slots': max(0, int((1 - util) * 8)),
        'status': status,
        'power': round(float(power), 1),
        'last_cmd': state['last_cmd'],
        'timestamp': datetime.now().isoformat(),
    }


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print('✅ 已连接 Broker: %s' % BROKER)
        print('📡 开始上报 %d 个充电桩（每 %ds 一次），主题 %s'
              % (len(STATIONS), PUBLISH_INTERVAL, TOPIC_TEMPLATE))
        client.subscribe(CMD_TOPIC_TEMPLATE.format(station_id='+'), qos=1)
        print('🎧 已订阅指令主题: %s' % CMD_TOPIC_TEMPLATE.format(station_id='+'))
        print('-' * 64)
    else:
        print('❌ 连接失败，返回码：%s' % rc)


def handle_command(client, sid, payload):
    state = DEVICE_STATE.get(sid)
    if state is None:
        client.publish(REPLY_TOPIC_TEMPLATE.format(station_id=sid),
                       json.dumps({'station_id': sid, 'cmd_id': payload.get('cmd_id'),
                                   'ok': False, 'detail': '未知设备 %s' % sid},
                                  ensure_ascii=False), qos=1)
        return
    cmd_id = payload.get('cmd_id')
    cmd = payload.get('cmd')
    params = payload.get('params') or {}
    ok, detail = False, ''
    try:
        if cmd == 'set_limit':
            limit = float(params.get('limit_kw', 60))
            if not (0 <= limit <= 240):
                detail = 'limit_kw 越界: %s' % limit
            else:
                state['limit_kw'] = None if limit >= state['power'] else limit
                state['last_cmd'] = 'set_limit=%skW' % limit
                ok = True
                detail = ('已限制到 %skW' % limit if state['limit_kw'] is not None
                          else '%skW ≥ 额定 %skW，等效取消限制' % (limit, state['power']))
        elif cmd == 'set_enable':
            enable = bool(params.get('enable', True))
            state['enabled'] = enable
            state['last_cmd'] = 'set_enable=%s' % enable
            ok = True
            detail = '已启用' if enable else '已停用'
        else:
            detail = '未知指令 %s' % cmd
    except Exception as e:
        detail = '执行异常: %s' % e

    client.publish(REPLY_TOPIC_TEMPLATE.format(station_id=sid),
                   json.dumps({'station_id': sid, 'cmd_id': cmd_id, 'ok': ok,
                               'detail': detail,
                               'state': {'enabled': state['enabled'],
                                         'limit_kw': state['limit_kw'],
                                         'power': state['power'],
                                         'last_cmd': state['last_cmd']},
                               'executed_at': datetime.now().isoformat()},
                              ensure_ascii=False), qos=1)
    print('%s 执行指令 [%s] %s %s → %s' % ('✅' if ok else '⚠️', sid, cmd, params, detail))

    status = build_status(sid)
    if status:
        client.publish(TOPIC_TEMPLATE.format(station_id=sid),
                       json.dumps(status, ensure_ascii=False), qos=1)


def on_message(client, userdata, msg):
    if not msg.topic.endswith('/cmd'):
        return
    parts = msg.topic.split('/')
    sid = parts[1] if len(parts) >= 2 else None
    if not sid:
        return
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
    except Exception as e:
        print('⚠️ 指令解析失败: %s' % e)
        return
    print('📥 收到指令 [%s] %s %s' % (sid, payload.get('cmd'), payload.get('params')))
    handle_command(client, sid, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--broker', default=BROKER)
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--interval', type=float, default=PUBLISH_INTERVAL)
    ap.add_argument('--topic', default=TOPIC_TEMPLATE)
    ap.add_argument('--once', action='store_true', help='只发一轮后退出')
    args = ap.parse_args()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id='mock_csv_%d' % int(time.time()))
    except AttributeError:
        client = mqtt.Client(client_id='mock_csv_%d' % int(time.time()))
    client.on_connect = on_connect
    client.on_message = on_message

    print('🔌 正在连接 %s:%s ...' % (args.broker, args.port))
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()
    time.sleep(2)

    try:
        while True:
            for s in STATIONS:
                topic = args.topic.format(station_id=s['id'])
                payload = build_status(s['id'])
                if not payload:
                    continue
                client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
                print('📤 [%s] util=%.2f status=%s power=%skW'
                      % (s['id'], payload['utilization'], payload['status'], payload['power']))
            print('-' * 64)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n⏹️ 手动停止')
    finally:
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()


if __name__ == '__main__':
    main()
