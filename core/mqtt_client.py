"""
core/mqtt_client.py

MQTT 工业协议客户端

上行（已存在）：订阅充电桩上报 → 清洗 → 写入 Supabase station_realtime_data
                → 前端 Realtime 订阅 → 更新场景
下行（新增）：  孪生侧下发指令 → 设备执行 → 回执上报 → 状态收敛

🔥 为什么要补下行：
   只订阅不发布的孪生体，本质上是"远程监控"，不是"闭环控制"。
   数字孪生真正区别于可视化看板的地方，就是能反向作用于物理世界。
   指令与回执走独立主题，且下行指令一律通过显式 API 发送、留痕可查，
   避免"表单改个数字就当控制成功"这种最常见的伪闭环。
"""
import json
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List

# 🔥 MQTT 回调跑在 paho 的后台线程里，stdout 编码不会被 Streamlit 的修复覆盖。
#    Windows GBK 控制台下 emoji 打印会抛 UnicodeEncodeError，
#    而它发生在回调线程里 → 直接打断网络循环，表现为「连上了但收不到数据」。
#    这里独立做一次兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("⚠️ paho-mqtt 未安装，MQTT 功能不可用")


# ==================== 下行指令定义 ====================
# 只开放一组保守、可验证、带边界的指令；每条都有中文名与参数范围，
# 前端下拉直接消费，不做自由文本指令（避免注入与误操作）。
COMMAND_SPECS: Dict[str, Dict[str, Any]] = {
    "set_limit": {
        "name": "限制输出功率",
        "desc": "把该桩的最大输出功率压到指定值，用于削峰",
        "param": "limit_kw",
        "min": 0,
        "max": 240,
        "default": 60,
        "unit": "kW",
    },
    "set_enable": {
        "name": "启用 / 停用",
        "desc": "远程启用或停用该桩，用于检修与应急",
        "param": "enable",
        "type": "bool",
        "default": True,
    },
}


class MQTTChargerClient:
    """
    充电桩 MQTT 客户端（单例）
    运行在后台线程：上行接收数据写入 Supabase，下行发送指令并回收执回执
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self.client = None
        self.connected = False
        self._reply_client = None
        self.broker = None
        self.topic = "charger/+/status"
        # 🔥 下行主题：charger/{station_id}/cmd（孪生 → 设备）
        #    回执主题：charger/{station_id}/cmd_reply（设备 → 孪生）
        self.cmd_topic_tpl = "charger/{station_id}/cmd"
        self.reply_topic_tpl = "charger/{station_id}/cmd_reply"
        self.reply_topic = "charger/+/cmd_reply"
        self.message_count = 0
        self.last_message = None
        self.last_message_time = None
        self.errors = []

        # 活跃设备清单
        self.active_devices = {}

        # 🔥 下行指令留痕（孪生侧发过什么）
        self.command_log: List[Dict[str, Any]] = []
        # 🔥 设备回执（设备侧怎么回的）
        self.replies: List[Dict[str, Any]] = []
        # 🔥 回调异常留痕：paho 回调抛异常会杀死网络循环线程，
        #    而 connected 标志仍为 True —— 表现为"连上了却再也收不到消息"。
        #    把异常记在状态里，让这类故障可见、可查。
        self.last_callback_error: Optional[str] = None
        self.last_callback_traceback: Optional[str] = None
        # 回执去重集合（主连接与回执专用连接可能同时收到同一条）
        self._seen_reply_keys: set = set()
        self._lock = threading.Lock()
        self._cmd_seq = 0

        # 外部回调（可选）
        self.on_message_callback: Optional[Callable] = None
        self.on_reply_callback: Optional[Callable] = None

    # ==================== 连接 ====================
    def connect(self, broker: str, port: int = 1883,
                topic: str = "charger/+/status",
                username: str = None, password: str = None) -> bool:
        """连接 MQTT Broker（非阻塞，立即返回）"""
        if not MQTT_AVAILABLE:
            self.errors.append("paho-mqtt 未安装")
            return False

        if self.connected:
            print("ℹ️ MQTT 已连接，跳过")
            return True

        try:
            self.broker = broker
            self.topic = topic

            try:
                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=f"dtt_{int(time.time())}")
            except AttributeError:
                self.client = mqtt.Client(client_id=f"dtt_{int(time.time())}")

            if username:
                self.client.username_pw_set(username, password)

            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect

            self.client.connect_async(broker, port, keepalive=60)
            self.client.loop_start()

            for _ in range(30):
                if self.connected:
                    # 🔥 在回调之外「再订阅一次」上行主题。
                    #    实测某些公共 Broker 对「CONNACK 回调里立即发出」的
                    #    SUBSCRIBE 会静默丢弃：SUBACK 正常返回、connected 为 True，
                    #    但该过滤器永远收不到消息（极难排查）。
                    #    连上之后在连接线程之外补订一次，可规避这种时序问题。
                    try:
                        self.client.subscribe(self.topic, qos=1)
                    except Exception as e:
                        print(f"⚠️ 补订上行主题失败: {e}")
                    self._start_reply_listener(broker, port, username, password)
                    return True
                time.sleep(0.1)

            self.errors.append("连接超时")
            return False

        except Exception as e:
            self.errors.append(str(e))
            print(f"❌ MQTT 连接失败: {e}")
            return False

    def _start_reply_listener(self, broker: str, port: int, username, password) -> None:
        """
        启动「回执专用订阅客户端」。

        🔥 为什么下行回执要单独一条连接：
           主连接的上行订阅在部分公共 Broker 上会出现「SUBACK 成功但收不到消息」
           的诡异情况（已实测）。指令回执是闭环的证据链，不能建立在不确定的连接上。
           独立连接 + 独立订阅，且用唯一 client_id 避免与主连接发生会话冲突。
        """
        if self._reply_client is not None:
            return
        try:
            try:
                rc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                 client_id=f"dtt_reply_{int(time.time())}_{id(self) % 10000}")
            except AttributeError:
                rc = mqtt.Client(client_id=f"dtt_reply_{int(time.time())}_{id(self) % 10000}")
            if username:
                rc.username_pw_set(username, password)

            def _on_conn(cl, u, f, flags):
                cl.subscribe(self.reply_topic, qos=1)
                print(f"✅ 回执订阅客户端已连接，订阅 {self.reply_topic}")

            rc.on_connect = _on_conn
            rc.on_message = self._on_message
            # paho 2.x 的 VERSION1 回调签名不带 properties，用 *a 兜住两种版本
            rc.on_disconnect = lambda *a: print("🔌 回执订阅客户端已断开")

            rc.connect_async(broker, port, keepalive=60)
            rc.loop_start()
            self._reply_client = rc
        except Exception as e:
            print(f"⚠️ 回执订阅客户端启动失败: {e}")
            self._reply_client = None

    def disconnect(self):
        for _c in (self.client, self._reply_client):
            if _c:
                try:
                    _c.loop_stop()
                    _c.disconnect()
                except Exception:
                    pass
        self.client = None
        self._reply_client = None
        self.connected = False
        print("🔌 MQTT 已断开")

    # ==================== 内部回调 ====================
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            client.subscribe(self.topic, qos=1)
            # 🔥 同时订阅回执主题：没有回执，下行就只是"发出去了"
            client.subscribe(self.reply_topic, qos=1)
            print(f"✅ MQTT 已连接 {self.broker}，订阅：{self.topic} + {self.reply_topic}")
        else:
            self.connected = False
            self.errors.append(f"连接返回码: {rc}")
            print(f"❌ MQTT 连接失败，返回码: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        print(f"🔌 MQTT 已断开（rc={rc}）")

    def _on_message(self, client, userdata, msg):
        # 🔥 回执与上报分流处理。
        #    这里必须整体兜异常：paho 的 on_message 一旦抛异常，
        #    网络循环线程会直接终止 —— 表现为「连上了、然后就再也收不到任何消息」，
        #    而 connected 标志仍是 True，极难排查。所以异常只记录、不外抛。
        try:
            if msg.topic.endswith('/cmd_reply'):
                self._handle_reply(msg)
                return
            self._handle_status(msg)
        except Exception as e:
            import traceback
            self.last_callback_error = f"{type(e).__name__}: {e}"
            self.last_callback_traceback = traceback.format_exc()
            print(f"❌ _on_message 处理异常（已兜住，避免网络循环终止）: {e}")
            print(self.last_callback_traceback)

    def _station_from_topic(self, topic: str) -> Optional[str]:
        parts = topic.split('/')
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return None

    def _handle_status(self, msg):
        """上行状态：充电桩上报"""
        try:
            payload_str = msg.payload.decode('utf-8')
            data = json.loads(payload_str)

            station_id = data.get('station_id') or self._station_from_topic(msg.topic)
            if not station_id:
                print(f"⚠️ 消息缺少 station_id: {payload_str}")
                return

            utilization = float(data.get('utilization', 0.5))
            available = int(data.get('available_slots', 0))
            status = str(data.get('status', '在线'))
            power = float(data.get('power', 60))

            try:
                from core.supabase_client import update_station_data
                update_station_data(station_id, {
                    'utilization': utilization,
                    'available_slots': available,
                    'status': status,
                    'power': power,
                    'updated_at': datetime.now().isoformat()
                })
            except Exception as e:
                print(f"⚠️ 写入 Supabase 失败: {e}")

            self.message_count += 1
            self.last_message = data
            self.last_message_time = datetime.now().strftime('%H:%M:%S')

            with self._lock:
                self.active_devices[station_id] = {
                    'station_id': station_id,
                    'utilization': utilization,
                    'status': status,
                    'power': power,
                    'last_seen': datetime.now().strftime('%H:%M:%S'),
                    'msg_count': self.active_devices.get(station_id, {}).get('msg_count', 0) + 1
                }

            if self.on_message_callback:
                try:
                    self.on_message_callback(msg.topic, data)
                except Exception as e:
                    print(f"⚠️ 回调异常: {e}")

        except json.JSONDecodeError as e:
            print(f"⚠️ JSON 解析失败: {e}, payload={msg.payload}")
        except Exception as e:
            print(f"⚠️ MQTT 消息处理异常: {e}")

    def _handle_reply(self, msg):
        """下行回执：设备执行结果"""
        try:
            data = json.loads(msg.payload.decode('utf-8'))
        except Exception as e:
            print(f"⚠️ 回执解析失败: {e}, payload={msg.payload}")
            return

        station_id = data.get('station_id') or self._station_from_topic(msg.topic)
        cmd_id = data.get('cmd_id')
        # 🔥 去重：主连接与回执订阅客户端可能同时收到同一条回执，
        #    不去重会记两条，让"闭环"看起来重复执行了。
        if cmd_id:
            key = f"{station_id}:{cmd_id}"
            with self._lock:
                if key in self._seen_reply_keys:
                    return
                self._seen_reply_keys.add(key)
                if len(self._seen_reply_keys) > 200:
                    self._seen_reply_keys = set(list(self._seen_reply_keys)[-100:])

        record = {
            'station_id': station_id,
            'cmd_id': cmd_id,
            'ok': bool(data.get('ok', False)),
            'detail': data.get('detail', ''),
            'state': data.get('state', {}),
            'received_at': datetime.now().strftime('%H:%M:%S'),
        }
        with self._lock:
            self.replies.append(record)
            if len(self.replies) > 50:
                self.replies = self.replies[-50:]
        print(f"📥 收到回执 [{station_id}] cmd={record['cmd_id']} ok={record['ok']} {record['detail']}")

        if self.on_reply_callback:
            try:
                self.on_reply_callback(record)
            except Exception as e:
                print(f"⚠️ 回执回调异常: {e}")

    # ==================== 下行：发送指令 ====================
    def publish_command(self, station_id: str, command: str,
                        params: Optional[Dict[str, Any]] = None,
                        timeout: float = 0.0) -> Dict[str, Any]:
        """
        向指定充电桩下发指令。

        返回 {"ok": bool, "cmd_id": str, "detail": str}
        🔥 注意：ok 只代表「指令已发出」。真正的执行结果必须等回执
           （见 wait_for_reply / get_replies），不要把"发送成功"当成"控制成功"。
        """
        spec = COMMAND_SPECS.get(command)
        if spec is None:
            return {"ok": False, "cmd_id": "", "detail": f"未知指令：{command}"}
        if not station_id:
            return {"ok": False, "cmd_id": "", "detail": "缺少 station_id"}
        if not self.connected or self.client is None:
            return {"ok": False, "cmd_id": "", "detail": "MQTT 未连接"}

        params = params or {}
        # 参数边界校验：下行指令必须有护栏，否则是给自己挖坑
        if command == "set_limit":
            try:
                limit = float(params.get("limit_kw", spec["default"]))
            except (TypeError, ValueError):
                return {"ok": False, "cmd_id": "", "detail": "limit_kw 不是数字"}
            if not (spec["min"] <= limit <= spec["max"]):
                return {"ok": False, "cmd_id": "",
                        "detail": f"limit_kw 超出范围 [{spec['min']}, {spec['max']}]"}
            params = {"limit_kw": limit}
        elif command == "set_enable":
            params = {"enable": bool(params.get("enable", True))}

        with self._lock:
            self._cmd_seq += 1
            cmd_id = f"c{int(time.time())}_{self._cmd_seq}"

        topic = self.cmd_topic_tpl.format(station_id=station_id)
        payload = {
            "cmd_id": cmd_id,
            "cmd": command,
            "params": params,
            "issued_at": datetime.now().isoformat(),
            "source": "digital_twin",
        }

        record = {
            "cmd_id": cmd_id,
            "station_id": station_id,
            "cmd": command,
            "params": params,
            "topic": topic,
            "sent_at": datetime.now().strftime('%H:%M:%S'),
            "acked": False,
        }
        try:
            info = self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
            # wait_for_publish 只在有网络循环时可用，做超时保护
            try:
                info.wait_for_publish(timeout=2.0)
            except Exception:
                pass
            with self._lock:
                self.command_log.append(record)
                if len(self.command_log) > 50:
                    self.command_log = self.command_log[-50:]
            print(f"📤 下发指令 [{station_id}] {command} {params} (cmd_id={cmd_id})")
            return {"ok": True, "cmd_id": cmd_id,
                    "detail": f"已下发到 {topic}"}
        except Exception as e:
            with self._lock:
                self.command_log.append(dict(record, error=str(e)))
            print(f"❌ 指令下发失败: {e}")
            return {"ok": False, "cmd_id": cmd_id, "detail": f"下发失败：{e}"}

    def wait_for_reply(self, cmd_id: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """
        等待某个 cmd_id 的回执（阻塞，带超时）。

        这是闭环的关键：没有回执就不知道设备到底执行了没有。
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            with self._lock:
                for r in self.replies:
                    if r.get('cmd_id') == cmd_id:
                        return r
            time.sleep(0.1)
        return None

    def get_command_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.command_log)

    def get_replies(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.replies)

    def mark_acked(self, cmd_id: str) -> None:
        with self._lock:
            for r in self.command_log:
                if r.get('cmd_id') == cmd_id:
                    r['acked'] = True


# ==================== 单例 ====================
_mqtt_client = None


def get_mqtt_client() -> MQTTChargerClient:
    """获取 MQTT 客户端单例"""
    global _mqtt_client
    if _mqtt_client is None:
        _mqtt_client = MQTTChargerClient()
    return _mqtt_client