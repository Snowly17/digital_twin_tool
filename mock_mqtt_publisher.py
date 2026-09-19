"""
mock_mqtt_publisher.py

模拟充电桩设备，向 MQTT Broker 上报数据，并接受孪生侧下发的指令。

用法：
    python mock_mqtt_publisher.py

🔥 本脚本同时扮演「设备端」的两个角色：
   1) 上行：每 3 秒发布一次 charger/{id}/status
   2) 下行：订阅 charger/{id}/cmd，执行后回 charger/{id}/cmd_reply，
      并立刻补发一条状态，让孪生侧看到状态收敛。
   有了 2)，孪生体的"反向控制"才是真闭环，而不是只画一条飞出去的箭头。

⚠️ 诚实说明：真实充电桩的固件不在这里。本脚本用软件模拟设备行为，
   目的是让"下发 → 执行 → 回执 → 状态收敛"这条链路可以端到端复现。
"""
import sys
import time
import json
import os
import random
from datetime import datetime

import paho.mqtt.client as mqtt

# Windows 控制台默认 GBK，本脚本大量 emoji 打印会直接抛 UnicodeEncodeError
# 导致「启动即崩溃」。统一走 core/console.py 的共享实现（app.py 亦同）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.console import enable_safe_console  # noqa: E402

enable_safe_console()

# ==================== 配置 ====================
BROKER = "broker.emqx.io"      # 公共 Broker
PORT = 1883
TOPIC_TEMPLATE = "charger/{station_id}/status"
CMD_TOPIC_TEMPLATE = "charger/{station_id}/cmd"
REPLY_TOPIC_TEMPLATE = "charger/{station_id}/cmd_reply"

# 模拟站点列表
#
# 🔥 点位改为复用 mock_data 里那套「摆成 C S V 三个字母」的 43 个坐标，
#    避免项目里出现两份互不一致的设备模拟器（app 的帮助文本点名的是本文件）。
#    坐标只是用来画设备分布，上报的载荷与主题与原来完全一致。
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from mock_data.build_mock_files import STATIONS as _LETTER_STATIONS
    STATIONS = [
        {"id": s["station_id"], "base_util": s["utilization"], "power": s["power"]}
        for s in _LETTER_STATIONS
    ]
except Exception as _e:      # 兜底：万一 mock_data 被单独搬走，退回原来的 8 站点
    print(f"⚠️ 未能加载 mock_data 的字母点位（{_e}），退回默认 8 个站点")
    STATIONS = [
        {"id": "1001", "base_util": 0.75, "power": 120},
        {"id": "1002", "base_util": 0.35, "power": 60},
        {"id": "1003", "base_util": 0.88, "power": 180},
        {"id": "1004", "base_util": 0.20, "power": 60},
        {"id": "1005", "base_util": 0.55, "power": 120},
        {"id": "1006", "base_util": 0.92, "power": 240},
        {"id": "1007", "base_util": 0.42, "power": 60},
        {"id": "1008", "base_util": 0.68, "power": 120},
    ]

PUBLISH_INTERVAL = 3   # 每 3 秒上报一次

# ==================== 设备本地状态 ====================
# 设备侧的"真实"状态：指令执行后改的就是这里，上报读的也是这里。
# 这正是数字孪生闭环的关键——孪生侧改的不是画面，而是设备状态。
DEVICE_STATE = {
    s["id"]: {
        "enabled": True,
        "limit_kw": None,          # None = 不限制
        "base_util": s["base_util"],
        "power": s["power"],
        "last_cmd": None,
    }
    for s in STATIONS
}


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"✅ 已连接到 Broker: {BROKER}")
        print(f"📡 开始上报 {len(STATIONS)} 个充电桩（每 {PUBLISH_INTERVAL}s 一次）")
        # 🔥 订阅所有站点的指令主题（真实设备只会订阅自己那一条）
        client.subscribe(CMD_TOPIC_TEMPLATE.format(station_id="+"), qos=1)
        print(f"🎧 已订阅指令主题: {CMD_TOPIC_TEMPLATE.format(station_id='+')}")
        print("-" * 64)
    else:
        print(f"❌ 连接失败，返回码：{rc}")


def _effective_util(state):
    """按设备当前状态算出实际上报的利用率（限功率会压低利用率）"""
    util = state["base_util"] + random.uniform(-0.15, 0.15)
    if state["limit_kw"] is not None:
        # 限功率后利用率按比例下降：60kW 限制对 120kW 桩意味着大约减半
        ratio = max(0.15, float(state["limit_kw"]) / max(1.0, float(state["power"])))
        util = util * ratio
    return max(0.02, min(0.95, round(util, 3)))


def build_status(station_id):
    """按设备真实状态生成一条上报报文"""
    state = DEVICE_STATE.get(station_id)
    if state is None:
        return None

    if not state["enabled"]:
        return {
            "station_id": station_id,
            "utilization": 0.0,
            "available_slots": 0,
            "status": "离线",
            "power": 0.0,
            "last_cmd": state["last_cmd"],
            "timestamp": datetime.now().isoformat(),
        }

    util = _effective_util(state)
    status = "在线"
    if util > 0.85:
        status = "高负载"
    elif random.random() < 0.02:
        status = "离线"
        util = 0.0

    power = state["power"] if state["limit_kw"] is None else min(state["power"], state["limit_kw"])
    return {
        "station_id": station_id,
        "utilization": util,
        "available_slots": max(0, int((1 - util) * 8)),
        "status": status,
        "power": round(float(power), 1),
        "last_cmd": state["last_cmd"],
        "timestamp": datetime.now().isoformat(),
    }


def handle_command(client, station_id, payload):
    """
    执行一条下行指令，并回执。

    指令 → 校验 → 改设备状态 → 回执 → 补发状态（让孪生侧看到收敛）
    """
    state = DEVICE_STATE.get(station_id)
    if state is None:
        client.publish(
            REPLY_TOPIC_TEMPLATE.format(station_id=station_id),
            json.dumps({"station_id": station_id, "cmd_id": payload.get("cmd_id"),
                        "ok": False, "detail": f"未知设备 {station_id}"}, ensure_ascii=False),
            qos=1)
        return

    cmd_id = payload.get("cmd_id")
    cmd = payload.get("cmd")
    params = payload.get("params") or {}
    ok, detail = False, ""

    try:
        if cmd == "set_limit":
            limit = float(params.get("limit_kw", 60))
            if not (0 <= limit <= 240):
                detail = f"limit_kw 越界: {limit}"
            else:
                state["limit_kw"] = None if limit >= state["power"] else limit
                state["last_cmd"] = f"set_limit={limit}kW"
                ok = True
                detail = (f"已限制到 {limit}kW" if state["limit_kw"] is not None
                          else f"{limit}kW ≥ 额定 {state['power']}kW，等效取消限制")
        elif cmd == "set_enable":
            enable = bool(params.get("enable", True))
            state["enabled"] = enable
            state["last_cmd"] = f"set_enable={enable}"
            ok = True
            detail = "已启用" if enable else "已停用"
        else:
            detail = f"未知指令 {cmd}"
    except Exception as e:
        detail = f"执行异常: {e}"

    reply = {
        "station_id": station_id,
        "cmd_id": cmd_id,
        "ok": ok,
        "detail": detail,
        "state": {
            "enabled": state["enabled"],
            "limit_kw": state["limit_kw"],
            "power": state["power"],
            "last_cmd": state["last_cmd"],
        },
        "executed_at": datetime.now().isoformat(),
    }
    client.publish(REPLY_TOPIC_TEMPLATE.format(station_id=station_id),
                   json.dumps(reply, ensure_ascii=False), qos=1)
    flag = "✅" if ok else "⚠️"
    print(f"{flag} 执行指令 [{station_id}] {cmd} {params} → {detail}")

    # 🔥 立刻补发一条状态：孪生侧不用等下一个 3 秒周期就能看到变化
    status = build_status(station_id)
    if status:
        client.publish(TOPIC_TEMPLATE.format(station_id=station_id),
                       json.dumps(status, ensure_ascii=False), qos=1)
        print(f"📤 [{station_id}] 状态已更新 util={status['utilization']:.2f} "
              f"status={status['status']} power={status['power']}kW")


def on_message(client, userdata, msg):
    """收到下行指令"""
    if not msg.topic.endswith('/cmd'):
        return
    parts = msg.topic.split('/')
    station_id = parts[1] if len(parts) >= 2 else None
    if not station_id:
        return
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
    except Exception as e:
        print(f"⚠️ 指令解析失败: {e}")
        return
    print(f"📥 收到指令 [{station_id}] {payload.get('cmd')} "
          f"{payload.get('params')} cmd_id={payload.get('cmd_id')}")
    handle_command(client, station_id, payload)


def main():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=f"mock_charger_{int(time.time())}")
    except AttributeError:
        client = mqtt.Client(client_id=f"mock_charger_{int(time.time())}")

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"🔌 正在连接 {BROKER} ...")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    time.sleep(2)

    try:
        while True:
            for station in STATIONS:
                topic = TOPIC_TEMPLATE.format(station_id=station['id'])
                payload = build_status(station['id'])
                if not payload:
                    continue
                client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
                print(f"📤 [{topic}] util={payload['utilization']:.2f} "
                      f"status={payload['status']} power={payload['power']}kW")

            print("-" * 64)
            time.sleep(PUBLISH_INTERVAL)

    except KeyboardInterrupt:
        print("\n⏹️ 手动停止")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()