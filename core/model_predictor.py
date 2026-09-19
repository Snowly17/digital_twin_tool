"""
core/model_predictor.py

模型预测器 - LSTM 预测封装
负责：加载预训练模型、单站预测、实时数据获取、历史趋势查询
"""

import os
import sys
import pandas as pd
import numpy as np
import torch
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.data_processor import DataProcessor
from common.lstm_utils import build_lstm_input_features
from common import baselines
from core.path_manager import STATION_OCC_PATH, STATION_INF_PATH, STATION_LSTM_PATH

# 模型训练时的预测步长（小时）。
# 实测该数据集 1h/3h 尺度上无可利用信号（persistence 即最优）；
# 6h 提升约 7.7%，12h 提升约 21.3%，故取 12。
PREDICT_HORIZON = 12


class ModelPredictor:
    """
    LSTM 预测器 - 单例模式
    负责：模型加载、单站预测、实时数据获取
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = None
        self.model_loaded = False
        self.data_processor = DataProcessor(use_real_data=True, data_level='station')
        self.station_inf = None
        self.occ_df = None
        self._load_station_info()
        self._load_occupancy_data()

    def _load_station_info(self):
        """加载站点静态信息 (经纬度、名称等)"""
        inf_path = STATION_INF_PATH
        if os.path.exists(inf_path):
            self.station_inf = pd.read_csv(inf_path)
            self.station_inf['station_id'] = self.station_inf['station_id'].astype(str)
            print(f"✅ 加载站点信息: {len(self.station_inf)} 个站点")
        else:
            print("⚠️ 未找到 station_inf.csv")

    def _load_occupancy_data(self):
        occ_path = STATION_OCC_PATH
        if os.path.exists(occ_path):
            self.occ_df = pd.read_csv(occ_path, index_col=0)
            self.occ_df.index = pd.to_datetime(self.occ_df.index)
            print(f"✅ 加载时间序列: {len(self.occ_df)} 小时 × {len(self.occ_df.columns)} 站点")
        else:
            print("⚠️ 未找到 station_occupancy_1h.csv")

    def _load_model(self):
        """懒加载 LSTM 模型"""
        if self.model_loaded:
            return True

        model_path = STATION_LSTM_PATH
        if not os.path.exists(model_path):
            print(f"⚠️ 模型文件不存在: {model_path}")
            return False

        try:
            # 获取站点数量
            if self.occ_df is not None:
                num_nodes = len(self.occ_df.columns)
            else:
                num_nodes = 1423  # 深圳站级默认数量

            seq_len = 48
            n_fea = 1 + 8  # 利用率 + 8个外部特征

            self.model = baselines.Lstm(seq=seq_len, n_fea=n_fea, node=num_nodes).to(self.device)
            state_dict = torch.load(model_path, map_location=self.device, weights_only=True)
            self.model.load_state_dict(state_dict)
            self.model.eval()
            self.model_loaded = True
            print(f"✅ LSTM 模型加载成功 (节点数: {num_nodes})")
            return True
        except Exception as e:
            print(f"❌ 模型加载失败: {e}")
            return False

    def get_station_list(self) -> List[Dict[str, str]]:
        """
        获取所有可用站点列表 (用于下拉选择)

        Returns:
            List[Dict]: [{"id": "1001", "name": "充电站-001", "lat": 22.5, "lon": 114.0}]
        """
        stations = []
        if self.station_inf is not None:
            for _, row in self.station_inf.iterrows():
                stations.append({
                    "id": row['station_id'],
                    "name": f"站-{row['station_id']}",
                    "lat": float(row.get('latitude', 0)),
                    "lon": float(row.get('longitude', 0))
                })
            return stations

        # 降级: 从 occ_df 中获取站点列表
        if self.occ_df is not None:
            for col in self.occ_df.columns[:100]:  # 限制数量
                stations.append({
                    "id": col,
                    "name": f"站-{col}",
                    "lat": 0,
                    "lon": 0
                })
        return stations

    def get_realtime_util(self, station_id: str) -> Dict[str, float]:
        """
        获取站点当前最新利用率

        Args:
            station_id: 站点ID (字符串)

        Returns:
            Dict: {"utilization": 0.67, "timestamp": "2023-02-01 12:00:00"}
        """
        if self.occ_df is None or station_id not in self.occ_df.columns:
            # 降级: 返回模拟值
            return {
                "utilization": round(np.random.uniform(0.2, 0.8), 3),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "available": np.random.randint(1, 8),
                "is_mock": True
            }

        try:
            latest = self.occ_df.iloc[-1]
            util = latest.get(station_id, 0.0)
            if pd.isna(util):
                util = 0.0
            return {
                "utilization": round(float(util), 3),
                "timestamp": self.occ_df.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
                "available": max(0, int((1 - util) * 10)),
                "is_mock": False
            }
        except Exception as e:
            print(f"⚠️ 获取实时数据失败: {e}")
            return {
                "utilization": round(np.random.uniform(0.2, 0.8), 3),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "available": np.random.randint(1, 8),
                "is_mock": True
            }

    def get_history(self, station_id: str, hours: int = 48) -> Dict[str, List]:
        """
        获取站点历史利用率趋势

        Args:
            station_id: 站点ID
            hours: 查询小时数 (默认48)

        Returns:
            Dict: {"timestamps": [...], "values": [...]}
        """
        if self.occ_df is None or station_id not in self.occ_df.columns:
            # 降级: 生成模拟历史数据
            return self._generate_mock_history(hours)

        try:
            data = self.occ_df[station_id].iloc[-hours:].dropna()
            timestamps = data.index.strftime("%Y-%m-%d %H:%M").tolist()
            values = [float(v) for v in data.values]
            return {
                "timestamps": timestamps,
                "values": values,
                "is_mock": False
            }
        except Exception as e:
            print(f"⚠️ 获取历史数据失败: {e}")
            return self._generate_mock_history(hours)

    def _generate_mock_history(self, hours: int) -> Dict[str, List]:
        """生成模拟历史数据 (降级使用)"""
        now = datetime.now()
        timestamps = []
        values = []
        for i in range(hours - 1, -1, -1):
            t = now - timedelta(hours=i)
            timestamps.append(t.strftime("%Y-%m-%d %H:%M"))
            # 模拟: 早晚高峰高, 凌晨低
            hour = t.hour
            base = 0.3 + 0.4 * np.sin((hour - 6) / 12 * np.pi)
            values.append(round(max(0.05, min(0.95, base + np.random.uniform(-0.08, 0.08))), 3))
        return {"timestamps": timestamps, "values": values, "is_mock": True}

    def predict_station(self, station_id: str, pred_len: int = PREDICT_HORIZON) -> Optional[Dict[str, List]]:
        """
        预测单个站点的未来利用率

        Args:
            station_id: 站点ID
            pred_len: 预测时长（小时），默认与训练步长 PREDICT_HORIZON 一致

        Returns:
            Dict: {"timestamps": [...], "values": [...], "current": 0.6}
        """
        # 确保数据已加载
        if self.occ_df is None or self.station_inf is None:
            return self._predict_mock(station_id, pred_len)

        # 确保站点存在
        if station_id not in self.occ_df.columns:
            return self._predict_mock(station_id, pred_len)

        # 加载模型
        if not self._load_model():
            return self._predict_mock(station_id, pred_len)

        try:
            # 准备数据: 只取该站点的序列 (但模型需要全量节点，这里需要适配)
            # 简化: 使用 data_processor 的批量预测方法，然后提取单个站点
            base_dir = os.path.join(project_root, 'data')
            model_path = os.path.join(project_root, 'checkpoints', 'station_lstm.pth')

            # 使用 DataProcessor 的批量预测
            pred_df = self.data_processor.predict_station_lstm(
                self.occ_df, model_path, seq_len=48, pred_len=pred_len,
                stations=[station_id],
            )

            # 提取指定站点
            station_row = pred_df[pred_df['station_id'] == station_id]
            if station_row.empty:
                return self._predict_mock(station_id, pred_len)

            # 构造返回结果
            current = float(station_row.iloc[0]['current_utilization'])
            pred_value = float(station_row.iloc[0]['predicted_utilization'])

            # 生成时间戳：从当前到 pred_len 小时后（即模型真正预测的那个时刻）。
            # progress 用 i/pred_len，保证最后一个点严格等于模型输出 predicted_final。
            now = datetime.now()
            timestamps = []
            values = []
            for i in range(1, pred_len + 1):
                t = now + timedelta(hours=i)
                timestamps.append(t.strftime("%Y-%m-%d %H:%M"))
                progress = i / pred_len
                values.append(round(current + (pred_value - current) * progress, 3))

            return {
                "timestamps": timestamps,
                "values": values,
                "current": current,
                "predicted_final": pred_value,
                "is_mock": False
            }

        except Exception as e:
            print(f"❌ 预测失败: {e}")
            return self._predict_mock(station_id, pred_len)

    def _predict_mock(self, station_id: str, pred_len: int = PREDICT_HORIZON) -> Dict:
        """模拟预测 (降级使用)"""
        current_util = np.random.uniform(0.3, 0.7)
        now = datetime.now()
        timestamps = []
        values = []
        for i in range(1, pred_len + 1):
            t = now + timedelta(hours=i)
            timestamps.append(t.strftime("%Y-%m-%d %H:%M"))
            # 随机变化
            delta = np.random.uniform(-0.15, 0.15)
            val = max(0.05, min(0.95, current_util + delta))
            values.append(round(val, 3))
        return {
            "timestamps": timestamps,
            "values": values,
            "current": current_util,
            "predicted_final": values[-1] if values else current_util,
            "is_mock": True
        }

    def get_station_detail(self, station_id: str) -> Dict:
        """
        获取站点完整信息 (用于右侧面板展示)

        Returns:
            Dict: 包含基本信息、实时数据、历史趋势、预测数据
        """
        # 基本信息
        info = {"id": station_id, "name": f"充电站-{station_id}"}
        if self.station_inf is not None:
            row = self.station_inf[self.station_inf['station_id'] == station_id]
            if not row.empty:
                info["name"] = f"站-{station_id}"
                info["lat"] = float(row.iloc[0].get('latitude', 0))
                info["lon"] = float(row.iloc[0].get('longitude', 0))

        # 实时数据
        realtime = self.get_realtime_util(station_id)

        # 历史趋势 (48小时)
        history = self.get_history(station_id, hours=48)

        # 预测 (3小时)
        prediction = self.predict_station(station_id, pred_len=PREDICT_HORIZON)

        return {
            "info": info,
            "realtime": realtime,
            "history": history,
            "prediction": prediction
        }


# ==================== 全局单例 ====================
_predictor = None


def get_predictor() -> ModelPredictor:
    """获取预测器单例"""
    global _predictor
    if _predictor is None:
        _predictor = ModelPredictor()
    return _predictor


# ==================== 使用示例 ====================
if __name__ == "__main__":
    predictor = get_predictor()

    # 1. 获取站点列表
    stations = predictor.get_station_list()
    print(f"📋 共 {len(stations)} 个站点")
    if stations:
        print(f"   示例: {stations[0]}")

        # 2. 获取第一个站点的实时数据
        station_id = stations[0]['id']
        realtime = predictor.get_realtime_util(station_id)
        print(f"📊 站点 {station_id} 实时利用率: {realtime}")

        # 3. 获取历史数据
        history = predictor.get_history(station_id, hours=24)
        print(f"📈 历史数据: {len(history['timestamps'])} 个点")

        # 4. 预测
        pred = predictor.predict_station(station_id)
        if pred:
            print(f"🔮 预测: {pred}")