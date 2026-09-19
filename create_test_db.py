"""一次性脚本：创建 SQLite 测试数据库"""
import sqlite3
import pandas as pd
import numpy as np
import os

DB_PATH = "test_stations.db"

# 删除旧的
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)

# 模拟深圳充电站数据
np.random.seed(42)
n = 20
df = pd.DataFrame({
    'station_id': [f'SZ{i:04d}' for i in range(1, n+1)],
    'name': [f'深圳充电站_{i:02d}' for i in range(1, n+1)],
    'lat': 22.54 + np.random.randn(n) * 0.03,
    'lon': 114.05 + np.random.randn(n) * 0.03,
    'utilization': np.clip(np.random.uniform(0.15, 0.9, n), 0.05, 0.95),
    'power': np.random.choice([60, 120, 180], n),
})

df.to_sql('stations', conn, if_exists='replace', index=False)
conn.close()

print(f"✅ 已创建 {DB_PATH}")
print(f"   表名: stations")
print(f"   记录数: {len(df)}")
print(f"   字段: {list(df.columns)}")