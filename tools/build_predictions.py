"""
一次性预计算所有站点的 12h 后利用率预测值。

产物：data/demo/predictions.json
  { "1": 0.42, "2": 0.68, ... }   约 45 KB

为什么离线：单次 predict_station_lstm 就是一次全量 1423 节点推理
（约 1~3 秒），前端不可能实时调。离线跑一次，前端读 JSON，< 50ms。
"""
import os
import sys
import json
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from core.model_predictor import get_predictor, PREDICT_HORIZON

OUT_DIR = os.path.join(project_root, 'data', 'demo')
OUT_PATH = os.path.join(OUT_DIR, 'predictions.json')
os.makedirs(OUT_DIR, exist_ok=True)

print('🔮 加载预测器...')
predictor = get_predictor()

result = {}
t0 = time.time()

# ============================================================
# 路径 1：一次全量推理（快，推荐）
#   predict_station_lstm 不带 stations 参数 = 返回全部 1423 个站点的预测
# ============================================================
print(f'🚀 路径 1：全量推理 {len(predictor.occ_df.columns)} 个站点...')
try:
    model_path = os.path.join(project_root, 'checkpoints', 'station_lstm.pth')
    pred_df = predictor.data_processor.predict_station_lstm(
        predictor.occ_df, model_path, seq_len=48, pred_len=PREDICT_HORIZON,
    )
    pred_df['station_id'] = pred_df['station_id'].astype(str)
    result = dict(zip(
        pred_df['station_id'],
        pred_df['predicted_utilization'].clip(0, 1).round(4),
    ))
    print(f'✅ 完成，耗时 {time.time() - t0:.1f} 秒，得到 {len(result)} 个预测')
except Exception as e:
    print(f'⚠️ 全量推理失败: {e}')
    print('   改用逐站预测（会慢很多）...')

# ============================================================
# 路径 2：逐站预测（兜底）
# ============================================================
if not result:
    stations = predictor.get_station_list()
    print(f'🚀 路径 2：逐站预测 {len(stations)} 个站点...')
    t1 = time.time()
    for i, s in enumerate(stations):
        try:
            r = predictor.predict_station(s['id'])
            if r and not r.get('is_mock'):
                result[s['id']] = round(float(r['predicted_final']), 4)
        except Exception as e:
            print(f'⚠️ {s["id"]} 失败: {e}')
        if (i + 1) % 100 == 0:
            print(f'   进度 {i+1}/{len(stations)}  ({time.time()-t1:.0f}s)')

if not result:
    print('❌ 没有任何预测值，脚本终止')
    sys.exit(1)

# ============================================================
# 数据合理性检查
# ============================================================
vals = list(result.values())
print(f'\n📊 统计：')
print(f'   站点数：{len(result)}')
print(f'   范围：{min(vals):.3f} ~ {max(vals):.3f}')
print(f'   均值：{sum(vals)/len(vals):.3f}')

if min(vals) == max(vals):
    print('⚠️ 所有预测值完全相同！模型可能没真的跑')
if sum(1 for v in vals if abs(v - 0.5) < 1e-6) > len(vals) * 0.8:
    print('⚠️ 80% 以上预测值都是 0.5！可疑')

# ============================================================
# 写出
# ============================================================
with open(OUT_PATH, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)

print(f'\n💾 已写出: {OUT_PATH}')
print(f'   大小: {os.path.getsize(OUT_PATH)/1024:.1f} KB')
print(f'   样例: {list(result.items())[:5]}')