"""
core/realtime_manager.py

实时数据流管理
"""

import streamlit as st
import random
import time
from datetime import datetime
from core.supabase_client import update_station_data, get_all_station_data


def generate_mock_data(station_id: str) -> dict:
    """生成模拟的充电桩数据"""
    util = random.uniform(0.1, 0.95)
    status = '在线'
    if util > 0.85:
        status = '高负载'
    elif random.random() < 0.03:  # 3% 概率离线
        status = '离线'

    return {
        'utilization': round(util, 3),
        'available_slots': max(0, int((1 - util) * 8)),
        'status': status,
        'power': round(random.uniform(20, 120), 1),
        'updated_at': datetime.now().isoformat()
    }


def update_stream_data(station_ids: list):
    """
    更新实时数据（在每次页面渲染时调用）
    建议在 main() 中调用
    """
    if not station_ids:
        return

    if 'stream_data' not in st.session_state:
        st.session_state.stream_data = {}

    current_time = time.time()
    last_update = st.session_state.get('stream_last_update', 0)

    # 每 2 秒更新一次
    if current_time - last_update >= 2.0:
        for sid in station_ids:
            if sid:  # 确保 station_id 非空
                mock_data = generate_mock_data(sid)
                st.session_state.stream_data[sid] = mock_data
                # 写入 Supabase（可选，如果不希望写入数据库可以注释）
                update_station_data(sid, mock_data)

        st.session_state.stream_last_update = current_time
        # 触发页面刷新以更新前端显示（如果需要）
        # st.rerun()  # 如果你希望实时刷新，取消注释