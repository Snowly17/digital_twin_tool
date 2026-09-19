# common/lstm_utils.py
import numpy as np
import pandas as pd

def build_lstm_input_features(occ_df, e_price_df, weather_df, seq_len=48):
    """
    构造 LSTM 输入特征（纯 NumPy 计算，无文件 I/O）

    参数：
        occ_df: DataFrame (时间索引, 站点列) 利用率宽表
        e_price_df: DataFrame (时间索引, 站点列) 电价宽表，需与 occ_df 时间对齐
        weather_df: DataFrame (时间索引, 列名包括 'nRAIN')
        seq_len: 序列长度（默认 48）

    返回：
        occ_seq: np.array (seq_len, num_stations) —— 利用率序列
        extra_feat_seq: np.array (seq_len, num_stations, 8) —— 8 个外部特征
        time_index: 对应的时间索引
    """
    # 1. 确保所有索引都是 datetime 类型
    if not isinstance(occ_df.index, pd.DatetimeIndex):
        occ_df.index = pd.to_datetime(occ_df.index)
    if not isinstance(e_price_df.index, pd.DatetimeIndex):
        e_price_df.index = pd.to_datetime(e_price_df.index)
    if not isinstance(weather_df.index, pd.DatetimeIndex):
        weather_df.index = pd.to_datetime(weather_df.index)

    common_index = occ_df.index

    # 2. 对齐 e_price_df 和 weather_df 的时间索引
    e_price_aligned = e_price_df.reindex(common_index, method='ffill').bfill()
    weather_aligned = weather_df.reindex(common_index, method='ffill').bfill()

    # 3. 确保 e_price_aligned 的列与 occ_df 的列完全一致
    # 如果缺少某些列，用该列均值填充；如果多出列，只保留 occ_df 的列
    common_cols = occ_df.columns.intersection(e_price_aligned.columns)
    if len(common_cols) != occ_df.shape[1]:
        # 缺失列用每行均值填充（避免 NaN）
        for col in occ_df.columns:
            if col not in common_cols:
                e_price_aligned[col] = e_price_aligned.mean(axis=1)
        # 按 occ_df 的列顺序重排
        e_price_aligned = e_price_aligned[occ_df.columns]
    else:
        e_price_aligned = e_price_aligned[occ_df.columns]

    # 4. 确保 weather 有 nRAIN 列
    if 'nRAIN' not in weather_aligned.columns:
        weather_aligned['nRAIN'] = 0.0

    # 5. 检查数据长度
    if len(occ_df) < seq_len + 72:
        raise ValueError(f"数据长度不足 {seq_len + 72}，当前仅 {len(occ_df)} 行")

    # 6. 取最后 seq_len + 72 行（用于构造 lag72）
    occ_slice = occ_df.iloc[-(seq_len + 72):].copy()
    e_price_slice = e_price_aligned.iloc[-(seq_len + 72):].copy()
    weather_slice = weather_aligned.iloc[-(seq_len + 72):].copy()

    # 7. 构造滞后特征
    lag_24 = occ_slice.shift(24).fillna(0).values
    lag_48 = occ_slice.shift(48).fillna(0).values
    lag_72 = occ_slice.shift(72).fillna(0).values

    # 8. 电价归一化（训练集范围 0.8~2.0）
    e_price_vals = e_price_slice.values
    e_price_norm = (e_price_vals - 0.8) / (2.0 - 0.8)
    e_price_norm = np.clip(e_price_norm, 0, 1)

    # 9. 降雨量归一化（深圳常见范围 0~30mm）
    rain_vals = weather_slice['nRAIN'].values.reshape(-1, 1)
    rain_norm = rain_vals / 30.0
    rain_norm = np.clip(rain_norm, 0, 1)
    rain_expanded = np.tile(rain_norm, (1, occ_slice.shape[1]))

    # 10. 时间特征
    hour = occ_slice.index.hour.values / 24.0
    dayofweek = occ_slice.index.dayofweek.values / 7.0
    is_weekend = (occ_slice.index.dayofweek >= 5).astype(int)

    hour_expanded = np.tile(hour.reshape(-1, 1), (1, occ_slice.shape[1]))
    dayofweek_expanded = np.tile(dayofweek.reshape(-1, 1), (1, occ_slice.shape[1]))
    weekend_expanded = np.tile(is_weekend.reshape(-1, 1), (1, occ_slice.shape[1]))

    # 11. 合并所有额外特征（8个特征）
    extra_feat = np.stack([
        e_price_norm,
        rain_expanded,
        lag_24,
        lag_48,
        lag_72,
        hour_expanded,
        dayofweek_expanded,
        weekend_expanded
    ], axis=2)

    # 12. 只取最后 seq_len 行作为模型输入
    occ_seq = occ_slice.iloc[-seq_len:].values
    extra_feat_seq = extra_feat[-seq_len:]
    time_index = occ_slice.index[-seq_len:]

    return occ_seq, extra_feat_seq, time_index


def build_lstm_input_features_region(occ_df, e_price_avg_df, weather_df, seq_len=48):
    """
    构造区域级 LSTM 输入特征（nRAIN + avg_price）
    返回：occ_seq (seq_len, num_zones), extra_feat_seq (seq_len, num_zones, 2), time_index
    """
    common_index = occ_df.index
    e_price_aligned = e_price_avg_df.reindex(common_index, method='ffill').bfill()
    weather_aligned = weather_df.reindex(common_index, method='ffill').bfill()

    if len(occ_df) < seq_len:
        raise ValueError(f"数据长度不足 {seq_len}，当前仅 {len(occ_df)} 行")

    # 🔥 关键修复：所有数据统一取最后 seq_len 行
    occ_slice = occ_df.iloc[-seq_len:].copy()

    # 电价：取对应时间段的最后 seq_len 行
    if isinstance(e_price_aligned, pd.Series):
        e_price_slice = e_price_aligned.iloc[-seq_len:].values.reshape(-1, 1)
    else:
        e_price_slice = e_price_aligned['avg_price'].iloc[-seq_len:].values.reshape(-1, 1)

    # 降雨量：取对应时间段的最后 seq_len 行
    rain_slice = weather_aligned['nRAIN'].iloc[-seq_len:].values.reshape(-1, 1)

    # 电价归一化（0.8~2.0）
    e_price_norm = (e_price_slice - 0.8) / (2.0 - 0.8)
    e_price_norm = np.clip(e_price_norm, 0, 1)
    e_price_expanded = np.tile(e_price_norm, (1, occ_slice.shape[1]))

    # 降雨量归一化（0~30mm）
    rain_norm = rain_slice / 30.0
    rain_norm = np.clip(rain_norm, 0, 1)
    rain_expanded = np.tile(rain_norm, (1, occ_slice.shape[1]))

    # 合并 extra_feat (2个特征)
    extra_feat = np.stack([rain_expanded, e_price_expanded], axis=2)

    occ_seq = occ_slice.values
    time_index = occ_slice.index

    return occ_seq, extra_feat, time_index