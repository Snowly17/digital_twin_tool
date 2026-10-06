"""
从完整的 station_occupancy_1h.csv 生成演示用精简数据集。

产出：
  data/demo/occupancy_demo.parquet   ← 约 300KB，300 站 × 200 时间步
  data/demo/station_ids.json         ← 站点 id 列表，供自动绑定用

为什么不是 CSV：Parquet 体积是 CSV 的 1/5~1/10，且读取快 3~5 倍。
"""
import pandas as pd
import numpy as np
import json
import os

SRC = 'data/station-level/station_occupancy_1h.csv'
OUT_DIR = 'data/demo'
N_STATIONS = 300      # 站点数
N_STEPS = 200         # 时间步数（与前端时间轴一致）

os.makedirs(OUT_DIR, exist_ok=True)

print(f'📖 读取 {SRC} ...')
header = pd.read_csv(SRC, index_col=0, nrows=0)
all_cols = [str(c) for c in header.columns]
print(f'   完整数据：{len(all_cols)} 个站点')

# 只取前 N_STATIONS 个站点，避免读全量 72MB
keep = [header.index.name] + all_cols[:N_STATIONS]
df = pd.read_csv(SRC, index_col=0, usecols=keep)
df.index = pd.to_datetime(df.index)

# 时间采样：均匀取 N_STEPS 个点
total = len(df)
if total > N_STEPS:
    idx = np.linspace(0, total - 1, N_STEPS, dtype=int)
    df = df.iloc[idx]

df.to_parquet(os.path.join(OUT_DIR, 'occupancy_demo.parquet'))
print(f'✅ 已写出 {df.shape[0]} 步 × {df.shape[1]} 站')

with open(os.path.join(OUT_DIR, 'station_ids.json'), 'w') as f:
    json.dump([str(c) for c in df.columns], f)
print(f'✅ 站点 id 列表已写出')