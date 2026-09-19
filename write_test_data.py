"""
write_test_data.py
向 InfluxDB 3 Cloud Serverless 写入测试数据（v3 API 版）
"""
import requests
from datetime import datetime
import random

# ==================== 填入你的参数 ====================
INFLUX_HOST = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN = "apiv3_xxxxxxxxxxxxxxxxxxxx"   # ← 你的 v3 Token
INFLUX_DATABASE = "charging"
MEASUREMENT = "charger_realtime"

# ✅ v3 API 写入端点（precision 必须用 nanosecond，不能用 ns）
WRITE_URL = f"{INFLUX_HOST.rstrip('/')}/api/v3/write_lp?db={INFLUX_DATABASE}&precision=nanosecond"

HEADERS = {
    "Authorization": f"Bearer {INFLUX_TOKEN}",
    "Content-Type": "text/plain; charset=utf-8",
}

print(f"🔌 正在连接: {INFLUX_HOST}")
print(f"📦 Database: {INFLUX_DATABASE}")
print(f"📝 Measurement: {MEASUREMENT}")
print()

# ==================== 构造 Line Protocol ====================
lines = []
for i in range(20):
    station_id = f"station_{i:03d}"
    name = f"充电站_{i:03d}"
    utilization = round(random.uniform(0.1, 0.9), 3)
    power = random.choice([60, 120, 180])
    lat = 43.8 + random.uniform(-0.1, 0.1)
    lon = 125.3 + random.uniform(-0.1, 0.1)
    ts = int(datetime.now().timestamp() * 1e9)

    line = (f'{MEASUREMENT},station_id={station_id},name={name} '
            f'utilization={utilization},power={power}i,lat={lat},lon={lon} '
            f'{ts}')
    lines.append(line)

payload = "\n".join(lines)

# ==================== 发送写入请求 ====================
print(f"📤 准备写入 {len(lines)} 条数据...")
print(f"📍 URL: {WRITE_URL}")
print()

try:
    resp = requests.post(
        WRITE_URL,
        headers=HEADERS,
        data=payload.encode('utf-8'),
        timeout=30
    )

    if resp.status_code in (200, 204):
        print(f"✅ 写入成功！状态码: {resp.status_code}")
        print(f"   成功写入 {len(lines)} 条数据")
    elif resp.status_code == 401:
        print(f"❌ 401 未授权 —— Token 无效")
        print(f"   请检查：")
        print(f"   1. Token 是否以 apiv3_ 开头")
        print(f"   2. Token 是否具有 charging 的 Write 权限")
        print(f"   3. 响应详情: {resp.text}")
    elif resp.status_code == 404:
        print(f"❌ 404 未找到 —— Database 名称错误")
        print(f"   请检查 charging 是否存在")
        print(f"   响应详情: {resp.text}")
    else:
        print(f"❌ 写入失败！状态码: {resp.status_code}")
        print(f"   响应: {resp.text}")
except Exception as e:
    print(f"❌ 请求异常: {e}")