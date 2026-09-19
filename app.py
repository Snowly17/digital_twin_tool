"""
app.py

智孪 · 数字孪生轻量化快速建模工具
单页应用 · 驾驶舱布局
"""

import sys
import os
import json
import uuid
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# 放宽控制台编码错误策略：非 UTF-8 控制台（简体中文 Windows 的 GBK）上，
# 本项目 200+ 处 emoji print 会抛 UnicodeEncodeError 使应用崩溃。详见 core/console.py
try:
    from core.console import enable_safe_console
except ModuleNotFoundError:
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from core.console import enable_safe_console

enable_safe_console()

from core.planned_layout import apply_planned_layout
from core.mqtt_client import get_mqtt_client, MQTT_AVAILABLE

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 🔥 修复：Windows 控制台/管道默认 GBK 编码时，本项目多处含 emoji 的 print
# （core/relation_builder.py、本文件的 print）会抛 UnicodeEncodeError 直接中断启动。
# 保持原编码、只把无法编码的字符替换掉：中文照常显示，个别 emoji 变 '?'。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from core.scene_manager import SceneManager, SceneData
from core.component_library import get_component_library
from core.style_manager import get_style_manager
from core.story_manager import get_story_manager
from core.realtime_manager import update_stream_data
from core.geo_parser import auto_parse, get_feature_summary
from core.relation_builder import build_relations
from core.generation_rules import DEFAULT_RULES, generate_objects, auto_layout_if_clustered
from core.emission_calculator import calculate_emission, calculate_storage_emission, get_carbon_rating

# ==================== 导入数据库模块 ====================
try:
    from core.supabase_client import (
        get_supabase_client,
        get_or_create_scene,
        get_scene_objects,
        sync_scene_objects,
        update_camera_state,
        get_all_station_data,
        update_station_data,
        subscribe_realtime,
        # 🔥 任务10新增
        publish_scene,
        unpublish_scene,
        get_public_scenes,
        get_public_scenes_by_keyword,
        like_scene,
        increment_scene_view,
        clone_scene,
        get_geo_tile
    )
    SUPABASE_AVAILABLE = True
except (ImportError, ModuleNotFoundError) as e:
    SUPABASE_AVAILABLE = False
    # 定义空函数占位
    def get_or_create_scene(*args, **kwargs): return None
    def get_scene_objects(*args, **kwargs): return []
    def sync_scene_objects(*args, **kwargs): return False
    def update_camera_state(*args, **kwargs): pass
    def get_all_station_data(*args, **kwargs): return {}
    def update_station_data(*args, **kwargs): return False
    def subscribe_realtime(*args, **kwargs): return None
    # 🔥 任务10占位
    def publish_scene(*args, **kwargs): return False
    def unpublish_scene(*args, **kwargs): return False
    def get_public_scenes(*args, **kwargs): return []
    def get_public_scenes_by_keyword(*args, **kwargs): return []
    def like_scene(*args, **kwargs): return 0
    def increment_scene_view(*args, **kwargs): return False
    def clone_scene(*args, **kwargs): return None
    def get_geo_tile(*args, **kwargs): return None
    print(f"⚠️ Supabase 模块未安装，部分功能不可用: {e}")



# ==================== 全刷新追踪（flag 模式） ====================
def mark_dirty(reason: str = "unknown"):
    """标记一次全 app 刷新，并记录原因。fragment 内改数据后调用。"""
    st.session_state['_dirty_reason'] = reason
    st.session_state['_dirty_time'] = datetime.now().strftime('%H:%M:%S')


def consume_dirty() -> str:
    """在 main 里消费脏标记，返回原因（无则空字符串）。"""
    return st.session_state.pop('_dirty_reason', '')

# ==================== 页面配置 ====================
st.set_page_config(
    page_title="智孪 · 数字孪生快速建模工具",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="collapsed"
)


# ==================== 懒加载预测器（延迟导入 torch，加速启动） ====================
# 用模块级全局而不是 st.session_state：后者在没有 ScriptRunContext 时会抛
# StreamlitAPIException（例如工具脚本里裸调 _get_predictor()）。
_predictor_warned = False


@st.cache_resource(show_spinner=False)
def _get_predictor():
    """
    延迟加载预测器：只有真正用到 LSTM/站点数据时才导入 torch。

    返回 None 表示当前环境没有 torch（部署环境默认不装，见 requirements.txt）。
    调用方必须处理 None，走模拟数据分支；否则云端会因 ModuleNotFoundError 崩溃。
    """
    global _predictor_warned
    try:
        from core.model_predictor import get_predictor
    except (ImportError, ModuleNotFoundError) as exc:
        # 只提示一次，不刷屏；不影响其余功能
        if not _predictor_warned:
            print(f"[提示] 预测器不可用（{exc}），站点数据相关功能降级为模拟数据")
            _predictor_warned = True
        return None
    return get_predictor()


_OCC_PATH = os.path.join(project_root, 'data', 'station-level', 'station_occupancy_1h.csv')


@st.cache_data(show_spinner=False)
def _occupancy_header():
    """只读 CSV 表头拿站点列名（约 0.1s），避免解析 72MB 全量数据。"""
    if not os.path.exists(_OCC_PATH):
        return None
    head = pd.read_csv(_OCC_PATH, index_col=0, nrows=0)
    return {"index_name": head.index.name, "columns": [str(c) for c in head.columns]}


@st.cache_data(show_spinner=False)
def _build_timeline(station_cols: tuple, sample_steps: int = 200):
    """
    只读所需站点列并采样，返回轻量时间轴数据。
    缓存的是最终小结果（约几百 KB），而不是 72MB 的原始 DataFrame。
    """
    info = _occupancy_header()
    if not info:
        return None
    keep = [info["index_name"]] + [c for c in station_cols if c in set(info["columns"])]
    occ_df = pd.read_csv(_OCC_PATH, index_col=0, usecols=keep)
    occ_df.index = pd.to_datetime(occ_df.index)
    total = len(occ_df)
    if total == 0:
        return None
    if total > sample_steps:
        idx = np.linspace(0, total - 1, sample_steps, dtype=int)
    else:
        idx = list(range(total))
    sampled = occ_df.iloc[idx]
    return {
        "timeline": [t.strftime('%m-%d %H:%M') for t in sampled.index],
        "stationIds": [str(c) for c in sampled.columns],
        "matrix": sampled.values.round(3).tolist(),
        "maxSteps": len(sampled),
    }


# ==================== 深色科技风 CSS ====================
st.markdown("""
<style>
    /* 全局背景 */
    .stApp {
        background: #0a0e17;
        color: #eef2ff;
    }
    .main .block-container {
        padding: 0.5rem 1rem !important;
        max-width: 100% !important;
    }

    /* 面板容器 */
    .panel {
        background: rgba(16, 22, 40, 0.85);
        backdrop-filter: blur(12px);
        border: 1px solid #1a2a44;
        border-radius: 16px;
        padding: 1rem;
        margin-bottom: 0.75rem;
        height: fit-content;
    }
    .panel-title {
        color: #88aadd;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.5rem;
        font-weight: 600;
        border-bottom: 1px solid #1a2a44;
        padding-bottom: 0.4rem;
    }

    /* 组件按钮 */
    .comp-btn {
        background: rgba(26, 42, 68, 0.6);
        border: 1px solid #1a2a44;
        border-radius: 10px;
        padding: 0.4rem 0.6rem;
        margin: 0.2rem 0;
        color: #c8d6e5;
        font-size: 0.8rem;
        cursor: pointer;
        transition: 0.15s;
        width: 100%;
        text-align: left;
        display: flex;
        align-items: center;
        gap: 0.4rem;
    }
    .comp-btn:hover {
        background: rgba(26, 42, 68, 0.9);
        border-color: #4477aa;
        color: #ffffff;
    }
    .comp-btn .icon {
        font-size: 1.1rem;
        width: 1.6rem;
        text-align: center;
    }
    .comp-btn .label {
        flex: 1;
    }
    .comp-btn .badge {
        font-size: 0.6rem;
        background: #1a2a44;
        padding: 0.1rem 0.5rem;
        border-radius: 10px;
        color: #88aadd;
    }

    /* 指标条 */
    .metric-bar {
        display: flex;
        gap: 2rem;
        padding: 0.4rem 1.5rem;
        background: rgba(16, 22, 40, 0.7);
        border-top: 1px solid #1a2a44;
        border-radius: 0 0 16px 16px;
        flex-wrap: wrap;
    }
    .metric-item {
        display: flex;
        align-items: baseline;
        gap: 0.4rem;
        color: #88aadd;
        font-size: 0.8rem;
    }
    .metric-item .value {
        color: #eef2ff;
        font-weight: 700;
        font-size: 1rem;
    }
    .metric-item .value.high {
        color: #ff6b6b;
    }
    .metric-item .value.good {
        color: #51cf66;
    }
    .metric-item .value.warn {
        color: #fcc419;
    }

    /* 下拉框/按钮样式适配深色 */
    .stSelectbox > div, .stButton button {
        background: rgba(26, 42, 68, 0.6) !important;
        border-color: #1a2a44 !important;
        color: #eef2ff !important;
        border-radius: 10px !important;
    }
    .stButton button {
        width: 100%;
    }
    .stButton button:hover {
        background: rgba(26, 42, 68, 0.9) !important;
        border-color: #4477aa !important;
    }

    /* 隐藏Streamlit默认边距 */
    div[data-testid="stVerticalBlock"] {
        gap: 0.3rem;
    }
    hr {
        border-color: #1a2a44;
        margin: 0.5rem 0;
    }

    /* 右侧详情面板的图表容器 */
    .detail-chart {
        background: rgba(10, 14, 23, 0.5);
        border-radius: 12px;
        padding: 0.25rem;
        margin-top: 0.5rem;
    }

    /* 滚动条 */
    ::-webkit-scrollbar { width: 4px; height: 4px; }
    ::-webkit-scrollbar-track { background: #0a0e17; }
    ::-webkit-scrollbar-thumb { background: #1a2a44; border-radius: 4px; }

    /* 确保3D容器无额外边距 */
    iframe {
        border: none !important;
        border-radius: 16px !important;
    }
    .stPlotlyChart {
        background: transparent !important;
    }
    /* ========== 数字孪生驾驶舱 UI 增强（追加） ========== */

    /* 标题科技蓝发光 */
    h1, h2, h3 {
        background: linear-gradient(135deg, #4a90d9, #88ccff) !important;
        -webkit-background-clip: text !important;
        background-clip: text !important;
        color: transparent !important;
        text-shadow: 0 0 40px rgba(74, 144, 217, 0.15) !important;
    }
    
    /* 面板边框发光 */
    .panel {
        border: 1px solid transparent !important;
        background: rgba(16, 22, 40, 0.75) !important;
        backdrop-filter: blur(16px) !important;
        box-shadow: 0 0 30px rgba(74, 144, 217, 0.05), inset 0 0 30px rgba(74, 144, 217, 0.02) !important;
        position: relative;
        overflow: hidden;
    }
    .panel::before {
        content: '';
        position: absolute;
        top: -1px;
        left: -1px;
        right: -1px;
        bottom: -1px;
        border-radius: 16px;
        padding: 1px;
        background: linear-gradient(135deg, rgba(74,144,217,0.2), rgba(136,204,255,0.05), rgba(74,144,217,0.2));
        -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        pointer-events: none;
        z-index: 0;
    }
    .panel > * {
        position: relative;
        z-index: 1;
    }
    
    /* 按钮渐变 + 发光 */
    .stButton button {
        background: linear-gradient(135deg, #1a3a5a, #2a5a7a) !important;
        border: 1px solid rgba(74, 144, 217, 0.3) !important;
        box-shadow: 0 0 20px rgba(74, 144, 217, 0.05) !important;
        transition: all 0.3s ease !important;
    }
    .stButton button:hover {
        background: linear-gradient(135deg, #2a5a8a, #4a8aba) !important;
        box-shadow: 0 0 40px rgba(74, 144, 217, 0.15) !important;
        transform: translateY(-1px) !important;
        border-color: rgba(74, 144, 217, 0.5) !important;
    }
    .stButton button:active {
        transform: translateY(0px) !important;
    }
    
    /* 指标卡片数字放大 + 彩色 */
    .stMetric .stMetric-value {
        font-size: 2.2rem !important;
        background: linear-gradient(135deg, #88ccff, #4a90d9) !important;
        -webkit-background-clip: text !important;
        background-clip: text !important;
        color: transparent !important;
    }
    .stMetric label {
        color: #8899bb !important;
        font-weight: 400 !important;
        letter-spacing: 0.05em !important;
    }
    
    /* 组件按钮优化 */
    .comp-btn {
        background: rgba(26, 42, 68, 0.4) !important;
        border: 1px solid rgba(74, 144, 217, 0.1) !important;
        transition: all 0.3s ease !important;
    }
    .comp-btn:hover {
        background: rgba(26, 42, 68, 0.7) !important;
        border-color: rgba(74, 144, 217, 0.3) !important;
        box-shadow: 0 0 30px rgba(74, 144, 217, 0.05) !important;
    }
    
    /* 滚动条发光 */
    ::-webkit-scrollbar-track {
        background: #0a0e17 !important;
    }
    ::-webkit-scrollbar-thumb {
        background: #1a3a5a !important;
        border-radius: 4px !important;
        box-shadow: 0 0 10px rgba(74, 144, 217, 0.1) !important;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #2a5a7a !important;
    }
    
    /* 分割线发光 */
    hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(74, 144, 217, 0.2), transparent) !important;
        margin: 0.8rem 0 !important;
    }
    
    /* 信息框科技感 */
    .stAlert, .stInfo, .stSuccess, .stWarning, .stError {
        border-left: 3px solid #4a90d9 !important;
        background: rgba(16, 22, 40, 0.6) !important;
        backdrop-filter: blur(8px) !important;
        box-shadow: 0 0 20px rgba(74, 144, 217, 0.03) !important;
    }
    
    /* panel-title 发光文字 */
    .panel-title {
        color: #88ccff !important;
        text-shadow: 0 0 20px rgba(74, 144, 217, 0.1) !important;
        letter-spacing: 0.08em !important;
    }
    
   /* ========== 图标导航栏（fixed 固定版 + Step3 增强） ========== */
    .dtt-icon-nav {
        position: fixed !important;
        left: 0 !important;
        top: 60px !important;
        width: 56px !important;
        height: calc(100vh - 60px) !important;
        background: rgba(10, 14, 23, 0.95) !important;
        border-right: 1px solid rgba(74, 144, 217, 0.12) !important;
        z-index: 999 !important;
        display: flex !important;
        flex-direction: column !important;
        padding-top: 8px !important;
        backdrop-filter: blur(10px) !important;
        pointer-events: auto !important;
    }
    
    .dtt-icon-btn {
        width: 56px !important;
        height: 62px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        font-size: 22px !important;
        color: #8899bb !important;
        text-decoration: none !important;
        position: relative !important;
        border-bottom: 1px solid rgba(74, 144, 217, 0.08) !important;
        transition: all 0.2s ease !important;
        cursor: pointer !important;
        line-height: 1 !important;
        font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif !important;
        flex-shrink: 0 !important;
    }
    
    /* 左侧竖条 */
    .dtt-icon-btn::before {
        content: '' !important;
        position: absolute !important;
        left: 0 !important;
        top: 50% !important;
        width: 3px !important;
        height: 0 !important;
        background: linear-gradient(180deg, #4a90d9, #88ccff) !important;
        border-radius: 0 3px 3px 0 !important;
        transform: translateY(-50%) !important;
        transition: height 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
    }
    
    .dtt-icon-btn:hover {
        background: rgba(26, 42, 68, 0.6) !important;
        color: #88ccff !important;
        text-decoration: none !important;
    }
    
    .dtt-icon-btn:hover::before {
        height: 24px !important;
        opacity: 0.5 !important;
    }
    
    .dtt-icon-btn.active {
        background: linear-gradient(90deg, rgba(74, 144, 217, 0.22), rgba(74, 144, 217, 0.03)) !important;
        color: #ffffff !important;
        box-shadow: inset 0 0 40px rgba(74, 144, 217, 0.08) !important;
        animation: icon-glow 2.5s ease-in-out infinite !important;
    }
    
    .dtt-icon-btn.active::before {
        height: 36px !important;
        background: linear-gradient(180deg, #4a90d9, #88ccff) !important;
        box-shadow: 0 0 12px rgba(74, 144, 217, 0.6) !important;
    }
    
    @keyframes icon-glow {
        0%, 100% { text-shadow: 0 0 8px rgba(136, 204, 255, 0.4); }
        50% { text-shadow: 0 0 16px rgba(136, 204, 255, 0.8); }
    }
    
    /* 主内容区左侧留出图标栏空间 */
    section.main .block-container {
        padding-left: 80px !important;
    }
    
    /* 主内容区左侧留出图标栏空间 */
    section.main .block-container {
        padding-left: 80px !important;
    }
    
    /* ===== 图标栏（fragment 按钮版）===== */
    .dtt-icon-nav-wrapper {
        margin-bottom: 15px;
        padding-bottom: 8px;
        border-bottom: 1px solid rgba(74, 144, 217, 0.12);
    }
    
    /* 让图标按钮紧凑、圆角、居中 */
    .dtt-icon-nav-wrapper .stButton > button {
        background: rgba(26, 42, 68, 0.4) !important;
        border: 1px solid rgba(74, 144, 217, 0.15) !important;
        border-radius: 10px !important;
        font-size: 22px !important;
        padding: 6px 0 !important;
        height: 52px !important;
        min-height: 52px !important;
        line-height: 1 !important;
        transition: all 0.2s ease !important;
        color: #8899bb !important;
    }
    
    .dtt-icon-nav-wrapper .stButton > button:hover {
        background: rgba(74, 144, 217, 0.25) !important;
        border-color: rgba(74, 144, 217, 0.4) !important;
        color: #88ccff !important;
        transform: translateY(-1px) !important;
    }
    
    /* 激活状态（primary 类型按钮）*/
    .dtt-icon-nav-wrapper .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, rgba(74, 144, 217, 0.6), rgba(74, 144, 217, 0.3)) !important;
        border-color: rgba(136, 204, 255, 0.6) !important;
        color: #ffffff !important;
        box-shadow: 0 0 24px rgba(74, 144, 217, 0.3) !important;
    }
    
    .dtt-icon-nav-wrapper .stButton > button[kind="primary"]:hover {
        box-shadow: 0 0 32px rgba(74, 144, 217, 0.5) !important;
    }
    
    /* 隐藏图标按钮的默认文字溢出 */
    .dtt-icon-nav-wrapper .stButton > button p {
        font-size: 22px !important;
        margin: 0 !important;
        line-height: 1 !important;
    }
    

</style>
""", unsafe_allow_html=True)

# ==================== 初始化 Session State ====================
if 'scene_manager' not in st.session_state:
    st.session_state.scene_manager = SceneManager()

# 🔥 强制重置 current_scene（如果存在且不是 SceneData 对象）
if 'current_scene' not in st.session_state or not hasattr(st.session_state.current_scene, 'scene_name'):
    mgr = st.session_state.scene_manager
    st.session_state.current_scene = mgr.create_from_template('charging_station', '我的充电站')

if 'selected_object_id' not in st.session_state:
    st.session_state.selected_object_id = None

if 'scene_objects' not in st.session_state or not st.session_state.scene_objects:
    scene = st.session_state.current_scene
    st.session_state.scene_objects = scene.objects if scene else []
if 'last_update' not in st.session_state:
    st.session_state.last_update = time.time()
if 'binding_station_id' not in st.session_state:
    st.session_state.binding_station_id = None

component_lib = get_component_library()


def _clear_transient_query_params():
    """
    清除一次性事件参数（selected / update_pos / delete / copy / exit_big ...），
    但**保留 scene_id**。

    🔥 修复：原代码在这些位置直接整锅清空 query params，会把 scene_id
    一起清掉。结果是"新建空白场景"后 scene_id 根本留不住，刷新/重开又回到
    默认场景。这里逐键删除，确保 scene_id 存活。
    """
    for key in list(st.query_params.keys()):
        if key != 'scene_id':
            del st.query_params[key]


# ==================== 初始化数据库 ====================
def init_db_scene():
    """初始化场景：从数据库加载或创建新场景"""
    # 🔥 防止重复初始化导致无限刷新
    if st.session_state.get('_db_initialized', False):
        return

    # ===== 如果 Supabase 不可用，降级到本地模板 =====
    if not SUPABASE_AVAILABLE:
        if 'current_scene' not in st.session_state:
            mgr = st.session_state.scene_manager
            st.session_state.current_scene = mgr.create_from_template('charging_station', '我的充电站')
        if 'scene_objects' not in st.session_state:
            st.session_state.scene_objects = st.session_state.current_scene.objects
        st.session_state._db_initialized = True
        return

    # ===== 尝试从 session_state 恢复 scene_id =====
    if 'scene_id' in st.session_state and st.session_state.scene_id:
        scene_id = st.session_state.scene_id
    else:
        scene_id = st.query_params.get('scene_id', None)

    try:
        from core.scene_manager import SceneData

        # =========================================================
        # 情况A：没有 scene_id（首次访问），从 Supabase 获取或创建默认场景
        # =========================================================
        if not scene_id:
            scene = get_or_create_scene(None)
            if scene:
                st.session_state.scene_id = scene['id']
                # 🔥 把当前场景固化到 URL：刷新/重开后仍停留在同一场景
                st.query_params['scene_id'] = scene['id']
                objects = get_scene_objects(scene['id'])
                if objects:
                    # 有物体：正常加载
                    scene_data = SceneData(
                        scene_name=scene.get('name', '我的充电站'),
                        created_at=scene.get('created_at', ''),
                        updated_at=scene.get('updated_at', ''),
                        objects=objects,
                        metadata=scene.get('metadata', {})
                    )
                    st.session_state.current_scene = scene_data
                    st.session_state.scene_objects = objects
                    st.session_state.scene_objects = build_relations(st.session_state.scene_objects)
                    st.session_state.current_scene.objects = st.session_state.scene_objects
                else:
                    # 🔥 场景存在但无物体：保持为空场景（空白网格）
                    st.session_state.current_scene = SceneData(
                        scene_name=scene.get('name', '空场景'),
                        objects=[]
                    )
                    st.session_state.scene_objects = []
                    print(f"✅ 加载空场景: {scene['id']}")
            else:
                # 无法连接 Supabase：降级到本地模板
                mgr = st.session_state.scene_manager
                st.session_state.current_scene = mgr.create_from_template('charging_station', '我的充电站')
                st.session_state.scene_objects = st.session_state.current_scene.objects
                print("⚠️ Supabase 无场景，使用本地模板")

            st.session_state._db_initialized = True
            return

        # =========================================================
        # 情况B：有 scene_id（例如刚点过"新建空白场景"），精准查询
        # 关键：直接查 scenes 表，不用 get_or_create_scene（避免回退到旧场景的陷阱）
        # =========================================================
        # 🔥 scene_id 可能只存在于 session_state（新建场景之后），把它固化到 URL，
        #    否则刷新时 session_state 丢失，又会回到"默认场景"
        st.query_params['scene_id'] = scene_id
        supabase = get_supabase_client()
        resp = supabase.table("scenes").select("*").eq("id", scene_id).execute()

        if resp.data and len(resp.data) > 0:
            # ✅ 场景存在于数据库
            scene = resp.data[0]
            objects = get_scene_objects(scene['id'])
            if objects:
                # 有物体：正常加载
                scene_data = SceneData(
                    scene_name=scene.get('name', '我的充电站'),
                    created_at=scene.get('created_at', ''),
                    updated_at=scene.get('updated_at', ''),
                    objects=objects,
                    metadata=scene.get('metadata', {})
                )
                st.session_state.current_scene = scene_data
                st.session_state.scene_objects = objects
                st.session_state.scene_objects = build_relations(st.session_state.scene_objects)
                st.session_state.current_scene.objects = st.session_state.scene_objects
                print(f"✅ 加载场景 {scene_id}: {len(objects)} 个物体")
            else:
                # 🔥 场景存在但无物体：保持为空场景，不回退到模板
                st.session_state.current_scene = SceneData(
                    scene_name=scene.get('name', '空场景'),
                    objects=[]
                )
                st.session_state.scene_objects = []
                print(f"✅ 加载空场景: {scene_id}")
        else:
            # ❌ 场景不存在（例如 Supabase 插入失败或新 ID 未成功注册）
            # 🔥 关键：保持为空场景，绝不回退到旧场景
            print(f"⚠️ 场景 {scene_id} 不存在于数据库，保持为空场景")
            st.session_state.current_scene = SceneData(
                scene_name="空白场景",
                objects=[]
            )
            st.session_state.scene_objects = []

            # 尝试补创建这个场景记录（下次加载就有记录了）
            try:
                supabase.table("scenes").insert({
                    "id": scene_id,
                    "name": "空白场景"
                }).execute()
                print(f"✅ 已补创建场景记录: {scene_id}")
            except Exception as e:
                print(f"⚠️ 补创建场景记录失败: {e}")

        st.session_state._db_initialized = True

    except Exception as e:
        print(f"⚠️ 数据库加载失败，使用本地模板: {e}")
        mgr = st.session_state.scene_manager
        st.session_state.current_scene = mgr.create_from_template('charging_station', '我的充电站')
        st.session_state.scene_objects = st.session_state.current_scene.objects
        st.session_state._db_initialized = True

    # ===== 订阅 Realtime 变更（可选，失败不影响核心功能） =====
    #_subscribe_realtime_changes()

def save_scene_to_db():
    """保存当前场景到数据库"""
    if not SUPABASE_AVAILABLE:
        return
    if 'scene_id' in st.session_state and 'scene_objects' in st.session_state:
        try:
            sync_scene_objects(
                st.session_state.scene_id,
                st.session_state.scene_objects
            )
            st.toast("✅ 场景已保存到云端", icon="☁️")
        except Exception as e:
            st.error(f"❌ 保存到数据库失败: {e}")


def _subscribe_realtime_changes():
    """
    订阅 scene_objects 表的实时变更（内部辅助函数）

    ⚠️ 已禁用：避免 Realtime 收到自己的写入事件 → 触发 st.rerun() → 陷入无限刷新循环
    如需重新启用，请注释掉第一行的 return，并添加防抖/去重逻辑
    """
    # 🔥 直接返回，禁用整个 Realtime 订阅
    return

    # ===== 以下代码不会执行，保留作为参考 =====
    if not SUPABASE_AVAILABLE:
        return
    if 'scene_id' not in st.session_state or not st.session_state.scene_id:
        return
    try:
        supabase = get_supabase_client()
        channel = supabase.channel(f'scene_{st.session_state.scene_id}')

        # 防止重复刷新的节流器
        import time as _time
        _last_rerun_time = {'t': 0}

        def handle_change(payload):
            print(f"🔄 收到数据库变更: {payload}")

            # 节流：2 秒内最多 rerun 一次
            now = _time.time()
            if now - _last_rerun_time['t'] < 2.0:
                print("⏸️ 节流跳过（避免循环刷新）")
                return
            _last_rerun_time['t'] = now

            objects = get_scene_objects(st.session_state.scene_id)
            if objects is not None:
                st.session_state.scene_objects = objects
                if st.session_state.current_scene:
                    st.session_state.current_scene.objects = objects
                st.rerun()

        channel.on('postgres_changes',
                   {'event': '*', 'schema': 'public', 'table': 'scene_objects'},
                   handle_change)
        channel.subscribe()
        print("✅ 已订阅 Supabase Realtime（scene_objects）")
    except Exception as e:
        print(f"ℹ️ Realtime 订阅跳过（同步客户端不支持）: {e}")

# ==================== 辅助函数 ====================

def refresh_scene():
    if st.session_state.current_scene:
        st.session_state.scene_objects = st.session_state.current_scene.objects
        st.session_state.last_update = time.time()

def get_object_by_id(obj_id: str) -> Optional[Dict]:
    for obj in st.session_state.scene_objects:
        if obj.get('id') == obj_id:
            return obj
    return None

def delete_object(obj_id: str):
    try:
        obj = get_object_by_id(obj_id)
        obj_name = obj.get('name', '物体') if obj else '未知'

        # 1. 从本地移除
        st.session_state.scene_objects = [o for o in st.session_state.scene_objects if o.get('id') != obj_id]
        if st.session_state.current_scene:
            st.session_state.current_scene.objects = st.session_state.scene_objects
        if st.session_state.selected_object_id == obj_id:
            st.session_state.selected_object_id = None
        st.session_state.last_update = time.time()

        # 2. 同步到 Supabase（如果可用）
        if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
            try:
                ok = sync_scene_objects(
                    st.session_state.scene_id,
                    st.session_state.scene_objects
                )
                if ok:
                    st.toast("☁️ 已从云端同步删除", icon="☁️")
                else:
                    st.error("❌ 云端同步删除失败！请检查网络或数据库连接")
                    print(f"❌ delete_object 同步失败: scene_id={st.session_state.scene_id}")
            except Exception as e:
                st.error(f"❌ 同步删除到云端异常: {e}")
                import traceback
                traceback.print_exc()

        # 3. 记录操作历史
        if 'operation_history' not in st.session_state:
            st.session_state.operation_history = []
        st.session_state.operation_history.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": "删除",
            "target": obj_name
        })
        if len(st.session_state.operation_history) > 50:
            st.session_state.operation_history = st.session_state.operation_history[-50:]

        # 🔥 清空旧的导出缓存
        st.session_state.export_html_content = None

        st.toast(f"🗑️ 已删除: {obj_name}", icon="🗑️")
        st.rerun()
        return True
    except Exception as e:
        st.error(f"❌ 删除失败: {e}")
        return False

def add_object_to_scene(component_id: str, position: Dict = None):
    try:
        if position is None:
            position = {"x": 0, "y": 0, "z": 0}
        import random
        offset = random.uniform(-2, 2)
        position = {"x": offset, "y": 0, "z": random.uniform(-2, 2)}
        instance = component_lib.create_instance(component_id, position=position)
        st.session_state.scene_objects.append(instance)
        if st.session_state.current_scene:
            st.session_state.current_scene.objects = st.session_state.scene_objects
        st.session_state.last_update = time.time()
        st.session_state.selected_object_id = instance['id']

        # 🔥 同步到 Supabase（确保刷新后数据一致）
        if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
            ok = sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)
            if not ok:
                st.warning("⚠️ 云端同步失败")

        # 记录操作历史
        if 'operation_history' not in st.session_state:
            st.session_state.operation_history = []
        st.session_state.operation_history.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": "添加",
            "target": instance.get('name', '新物体')
        })
        if len(st.session_state.operation_history) > 50:
            st.session_state.operation_history = st.session_state.operation_history[-50:]

        # 🔥 更新最近使用组件（与上面 if 同级，不在其内部）
        recent = st.session_state.get('recent_components',
                                      ['charger_fast', 'charger_slow', 'building', 'tree'])
        if component_id in recent:
            recent.remove(component_id)
        recent.insert(0, component_id)
        st.session_state.recent_components = recent[:4]

        # 🔥 清空旧的导出缓存
        st.session_state.export_html_content = None

        st.toast(f"✅ 已添加: {instance.get('name', '新物体')}", icon="➕")
    except Exception as e:
        st.error(f"❌ 添加失败: {e}")

def apply_template(template_name: str):
    try:
        mgr = st.session_state.scene_manager
        scene = mgr.create_from_template(template_name, f"模板场景_{datetime.now().strftime('%H%M')}")
        # 直接替换，而不是追加
        st.session_state.current_scene = scene
        st.session_state.scene_objects = scene.objects
        st.session_state.selected_object_id = None
        st.session_state.last_update = time.time()
        # 同步到数据库（如果已初始化场景ID）
        if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
            sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)
        # 🔥 清空旧的导出缓存
        st.session_state.export_html_content = None
        st.toast(f"✅ 模板加载成功: {template_name}", icon="📁")
        st.rerun()
    except Exception as e:
        st.error(f"❌ 模板加载失败: {e}")

def create_empty_scene():
    """创建一个空白场景（无任何物体，仅显示网格）"""
    try:
        from core.scene_manager import SceneData
        import uuid as uuid_module

        # 🔥 关键：生成新的 scene_id，避免前端读取旧的 localStorage
        new_scene_id = str(uuid_module.uuid4())

        empty_scene = SceneData(
            scene_name=f"空白场景_{datetime.now().strftime('%Y%m%d_%H%M')}",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            objects=[],
            metadata={}
        )

        # 1. 更新 session_state（使用新 ID）
        st.session_state.current_scene = empty_scene
        st.session_state.scene_objects = []
        st.session_state.selected_object_id = None
        st.session_state.scene_id = new_scene_id
        st.session_state._db_initialized = False

        # 2. 如果 Supabase 可用，在新 ID 下创建场景记录（空物体）
        if SUPABASE_AVAILABLE:
            try:
                supabase = get_supabase_client()
                # 用 upsert 而不是 insert，避免 ID 冲突
                resp = supabase.table("scenes").upsert({
                    "id": new_scene_id,
                    "name": empty_scene.scene_name
                }).execute()
                print(f"✅ 已在 Supabase 创建新场景: {new_scene_id}")
            except Exception as e:
                print(f"⚠️ 创建 Supabase 场景失败（不影响本地使用）: {e}")
                # 🔥 关键：即使失败也保持 scene_id 为本地新 ID，不要回退

        # 用 URL 参数通知下次加载使用新 ID
        # 🔥 修复：原来写的是 st.query_params['new_scene_id']，而加载时读的是
        #    st.query_params.get('scene_id')——键名不一致，且全项目没有任何地方
        #    读 new_scene_id。所以"新建空白场景"的 id 从来没被加载路径读到过。
        st.query_params['scene_id'] = new_scene_id
        st.toast("✅ 已创建空白场景", icon="🆕")
        st.rerun(scope="app")
    except Exception as e:
        st.error(f"❌ 创建空白场景失败: {e}")

# ==================== 左侧面板 ====================

# ============================================================
# 可视化规则编辑器（第5步：从"硬编码"到"规则配置面板"）
# ============================================================

TARGET_OBJECTS = [
    'charger_fast', 'charger_slow', 'charger_super',
    'building', 'building_tall',
    'tree', 'tree_pine', 'lamp',
    'road_straight', 'road_curve',
    'car', 'truck'
]

GEOM_TYPE_LABELS = {
    'point':   ('📍', '点数据'),
    'line':    ('〰️', '线数据'),
    'polygon': ('🔲', '面数据'),
}

OP_LABELS = {
    '==':       '等于',
    '!=':       '不等于',
    'contains': '包含',
    '>':        '大于',
    '<':        '小于',
    '>=':       '大于等于',
    '<=':       '小于等于',
}


def _init_rules_if_needed():
    if 'gen_rules' not in st.session_state:
        st.session_state.gen_rules = {
            'point':   {'target': 'charger_fast',  'conditions': []},
            'line':    {'target': 'road_straight', 'conditions': []},
            'polygon': {'target': 'building',      'conditions': []},
        }
    if '_rule_uid_counter' not in st.session_state:
        st.session_state._rule_uid_counter = 100


def _next_rule_uid():
    st.session_state._rule_uid_counter += 1
    return st.session_state._rule_uid_counter


def _preset_charging_station():
    return {
        'point': {
            'target': 'charger_fast',
            'conditions': [
                {'field': 'type', 'op': '==', 'value': 'slow',  'target': 'charger_slow',  'uid': 1},
                {'field': 'type', 'op': '==', 'value': 'super', 'target': 'charger_super', 'uid': 2},
            ]
        },
        'line': {'target': 'road_straight', 'conditions': []},
        'polygon': {
            'target': 'building',
            'conditions': [
                {'field': 'use', 'op': '==', 'value': 'commercial', 'target': 'building_tall', 'uid': 3},
            ]
        },
    }


def _preset_city():
    return {
        'point': {
            'target': 'charger_fast',
            'conditions': [
                {'field': 'type', 'op': '==', 'value': 'tree', 'target': 'tree', 'uid': 4},
                {'field': 'type', 'op': '==', 'value': 'lamp', 'target': 'lamp', 'uid': 5},
                {'field': 'utilization', 'op': '>', 'value': '0.8', 'target': 'charger_super', 'uid': 6},
            ]
        },
        'line': {
            'target': 'road_straight',
            'conditions': [
                {'field': 'highway', 'op': 'contains', 'value': 'primary', 'target': 'road_straight', 'uid': 7},
            ]
        },
        'polygon': {
            'target': 'building',
            'conditions': [
                {'field': 'use', 'op': '==', 'value': 'commercial', 'target': 'building_tall', 'uid': 8},
                {'field': 'height', 'op': '>', 'value': '10', 'target': 'building_tall', 'uid': 9},
            ]
        },
    }


def _render_condition_row(gtype: str, idx: int, cond: dict, to_delete: list):
    """渲染单条条件规则"""
    uid = cond.get('uid', idx)
    key_prefix = f"cond_{gtype}_{uid}"

    col1, col2, col3, col4 = st.columns([2.5, 1.8, 2, 3])

    with col1:
        cond['field'] = st.text_input(
            "字段",
            value=cond.get('field', 'type'),
            key=f"{key_prefix}_field",
            label_visibility="collapsed",
            placeholder="字段名（如 type）"
        )

    with col2:
        op_options = list(OP_LABELS.keys())
        current_op = cond.get('op', '==')
        op_idx = op_options.index(current_op) if current_op in op_options else 0
        cond['op'] = st.selectbox(
            "操作符",
            options=op_options,
            index=op_idx,
            format_func=lambda x: OP_LABELS[x],
            key=f"{key_prefix}_op",
            label_visibility="collapsed"
        )

    with col3:
        cond['value'] = st.text_input(
            "值",
            value=str(cond.get('value', '')),
            key=f"{key_prefix}_value",
            label_visibility="collapsed",
            placeholder="匹配值"
        )

    with col4:
        col_a, col_b = st.columns([5, 1])
        with col_a:
            current_t = cond.get('target', TARGET_OBJECTS[0])
            cond['target'] = st.selectbox(
                "目标",
                options=TARGET_OBJECTS,
                index=TARGET_OBJECTS.index(current_t) if current_t in TARGET_OBJECTS else 0,
                key=f"{key_prefix}_target",
                label_visibility="collapsed"
            )
        with col_b:
            if st.button("🗑️", key=f"{key_prefix}_del", help="删除此条件"):
                to_delete.append(idx)


def _render_rule_preview(rules: dict):
    """规则预览"""
    st.markdown("**📋 当前规则逻辑**")
    for gtype in ['point', 'line', 'polygon']:
        icon, label = GEOM_TYPE_LABELS[gtype]
        rule = rules.get(gtype, {})
        target = rule.get('target', '—')
        conds = rule.get('conditions', [])

        st.markdown(
            f'<div style="padding:8px 12px;background:rgba(26,42,68,0.4);'
            f'border-left:3px solid #4a90d9;border-radius:8px;margin:6px 0;">'
            f'<div style="color:#88ccff;font-weight:600;font-size:13px;">'
            f'{icon} {label}</div>'
            f'<div style="color:#8899bb;font-size:12px;margin-top:4px;">'
            f'默认 → <b style="color:#51cf66;">{target}</b></div>'
            f'</div>',
            unsafe_allow_html=True
        )
        for cond in conds:
            f_field = cond.get('field', '')
            f_op = OP_LABELS.get(cond.get('op', '=='), cond.get('op', '=='))
            f_val = cond.get('value', '')
            f_target = cond.get('target', '')
            st.markdown(
                f'<div style="margin-left:20px;padding:4px 10px;font-size:11.5px;'
                f'color:#c8d6e5;border-left:2px solid #2a3a55;">'
                f'如果 <code style="color:#fcc419;">{f_field}</code> '
                f'<span style="color:#88aadd;">{f_op}</span> '
                f'<code style="color:#ff9f43;">{f_val}</code> → '
                f'<b style="color:#51cf66;">{f_target}</b>'
                f'</div>',
                unsafe_allow_html=True
            )


def render_rule_editor():
    """可视化规则编辑器（核心UI）"""
    _init_rules_if_needed()
    rules = st.session_state.gen_rules

    st.markdown("**🎯 生成规则配置**")
    st.caption("按顺序匹配条件，命中即停；未命中则使用默认映射")

    # ===== 预设方案 =====
    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        if st.button("⚡ 充电站方案", key="preset_station",
                     use_container_width=True, help="点→充电桩（含慢充/超充）"):
            st.session_state.gen_rules = _preset_charging_station()
            st.toast("✅ 已加载「充电站方案」", icon="⚡")
            st.rerun()
    with col_p2:
        if st.button("🏙️ 城市方案", key="preset_city",
                     use_container_width=True, help="点/线/面 → 完整城市要素"):
            st.session_state.gen_rules = _preset_city()
            st.toast("✅ 已加载「城市方案」", icon="🏙️")
            st.rerun()
    with col_p3:
        if st.button("🔄 重置", key="reset_rules",
                     use_container_width=True, help="恢复默认规则"):
            st.session_state.gen_rules = {
                'point':   {'target': 'charger_fast',  'conditions': []},
                'line':    {'target': 'road_straight', 'conditions': []},
                'polygon': {'target': 'building',      'conditions': []},
            }
            st.toast("🔄 规则已重置", icon="🔄")
            st.rerun()

    # ===== 每种几何类型的规则卡片 =====
    for gtype in ['point', 'line', 'polygon']:
        icon, label = GEOM_TYPE_LABELS[gtype]
        rule = rules.setdefault(gtype, {'target': TARGET_OBJECTS[0], 'conditions': []})

        with st.expander(f"{icon} {label} → 物体映射", expanded=False):
            # 默认目标
            current_target = rule.get('target', TARGET_OBJECTS[0])
            new_target = st.selectbox(
                "默认映射（无匹配条件时使用）",
                options=TARGET_OBJECTS,
                index=TARGET_OBJECTS.index(current_target) if current_target in TARGET_OBJECTS else 0,
                key=f"rule_default_{gtype}",
            )
            rule['target'] = new_target

            st.markdown("**条件规则**（从上到下匹配）")

            conditions = rule.get('conditions', [])
            if not conditions:
                st.caption("暂无条件规则 — 所有该类型数据都将映射为默认物体")

            # 表头
            if conditions:
                h1, h2, h3, h4 = st.columns([2.5, 1.8, 2, 3])
                with h1:
                    st.markdown('<span style="font-size:10px;color:#667;">字段</span>', unsafe_allow_html=True)
                with h2:
                    st.markdown('<span style="font-size:10px;color:#667;">操作</span>', unsafe_allow_html=True)
                with h3:
                    st.markdown('<span style="font-size:10px;color:#667;">值</span>', unsafe_allow_html=True)
                with h4:
                    st.markdown('<span style="font-size:10px;color:#667;">目标物体 / 删除</span>', unsafe_allow_html=True)

            # 渲染条件行
            to_delete = []
            for idx, cond in enumerate(conditions):
                _render_condition_row(gtype, idx, cond, to_delete)

            # 处理删除
            if to_delete:
                for idx in sorted(to_delete, reverse=True):
                    conditions.pop(idx)
                rule['conditions'] = conditions
                st.rerun()

            # 添加按钮
            if st.button(f"➕ 添加条件规则", key=f"add_cond_{gtype}",
                         use_container_width=True):
                conditions.append({
                    'field': 'type',
                    'op': '==',
                    'value': '',
                    'target': TARGET_OBJECTS[0],
                    'uid': _next_rule_uid(),
                })
                rule['conditions'] = conditions
                st.rerun()

    # 保存回 session_state
    st.session_state.gen_rules = rules

    # ===== 规则预览 =====
    with st.expander("👁️ 规则逻辑预览", expanded=False):
        _render_rule_preview(rules)

    # ===== 导入 / 导出规则（平铺，一行一个） =====
    import json as _json

    st.markdown("<hr style='margin: 12px 0;'>", unsafe_allow_html=True)
    st.markdown("**💾 规则存档**")

    # 第 1 行：导出规则
    st.download_button(
        label="📤 导出规则(JSON)",
        data=_json.dumps(rules, ensure_ascii=False, indent=2),
        file_name=f"rules_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
        mime="application/json",
        use_container_width=True,
        key="export_rules_btn"
    )

    # 第 2 行：导入规则
    uploaded_rule = st.file_uploader(
        "📥 导入规则（JSON）",
        type=['json'],
        key="upload_rules_file",
        label_visibility="collapsed",
        help="选择包含 point / line / polygon 三个字段的规则 JSON 文件"
    )
    if uploaded_rule is not None:
        try:
            loaded = _json.loads(uploaded_rule.read().decode('utf-8'))
            if all(k in loaded for k in ['point', 'line', 'polygon']):
                st.session_state.gen_rules = loaded
                st.toast("✅ 规则已导入", icon="📥")
                st.rerun()
            else:
                st.error("❌ 规则文件格式不正确，缺少必要字段")
        except Exception as e:
            st.error(f"❌ 导入失败：{e}")

def fuzzy_match(keyword: str, text: str) -> bool:
    """
    轻量级模糊匹配：
    - 空关键词 → 全部匹配
    - 完全包含 → 匹配
    - 所有字符按序出现 → 匹配（如 "快充" 匹配 "快速充电桩"）
    """
    if not keyword:
        return True
    kw = keyword.lower().strip()
    t = text.lower()
    # 1. 直接包含
    if kw in t:
        return True
    # 2. 按序匹配所有字符
    idx = 0
    for ch in kw:
        idx = t.find(ch, idx)
        if idx == -1:
            return False
        idx += 1
    return True


def empty_state(icon: str = "🔍", title: str = "未找到匹配项", hint: str = ""):
    """统一的空状态提示（纯 HTML，无新 CSS 类）"""
    hint_html = (
        f'<div style="font-size:12px;color:#556;opacity:0.7;margin-top:4px;">{hint}</div>'
        if hint else ''
    )
    st.markdown(
        f'<div style="display:flex;flex-direction:column;align-items:center;'
        f'justify-content:center;padding:40px 20px;text-align:center;color:#556;">'
        f'<div style="font-size:42px;opacity:0.5;margin-bottom:12px;">{icon}</div>'
        f'<div style="font-size:14px;font-weight:500;color:#8899bb;margin-bottom:6px;">{title}</div>'
        f'{hint_html}'
        f'</div>',
        unsafe_allow_html=True
    )

def panel_header(icon, title, count=None):
    """统一的面板头部：用行内样式，不受 Streamlit 全局 CSS 影响"""
    count_html = ''
    if count is not None:
        count_html = (
            f'<span style="margin-left:auto;font-size:11px;color:#667;'
            f'background:rgba(74,144,217,0.1);padding:2px 8px;border-radius:10px;">'
            f'{count}</span>'
        )
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:8px;'
        f'padding:10px 0 12px 0;'
        f'margin-bottom:16px;'
        f'border-bottom:1px solid rgba(74,144,217,0.15);">'
        f'<span style="font-size:18px;line-height:1;">{icon}</span>'
        f'<span style="font-size:14px;font-weight:600;color:#88ccff;'
        f'letter-spacing:0.05em;">{title}</span>'
        f'{count_html}'
        f'</div>',
        unsafe_allow_html=True
    )

@st.fragment
def render_left_panel():
    # ===== 初始化状态 =====
    if 'active_panel' not in st.session_state:
        st.session_state.active_panel = 'scene'

    # ===== 图标栏（使用 st.button，支持 fragment 局部刷新）=====
    # 🔥 P2 Step3/4：最终 5 个模块。模板库→组件；资产树 + 场景库→场景；
    # 故事→外观；碳减排→数据。
    # 子面板函数保持独立（在分发层组合渲染），此处只减导航项，不动面板代码。
    panels = [
        ('scene', '🏠', '场景'),
        ('component', '🧩', '组件'),
        ('style', '🎨', '外观'),
        ('data', '📊', '数据'),
        ('settings', '⚙️', '设置'),
    ]
    current = st.session_state.active_panel

    # 🔥 关键：图标栏用 st.columns + st.button 实现
    # 加 CSS 让按钮看起来和原来的图标栏一致
    st.markdown('<div class="dtt-icon-nav-wrapper">', unsafe_allow_html=True)
    nav_cols = st.columns(len(panels), gap="small")
    for i, (pid, icon, label) in enumerate(panels):
        with nav_cols[i]:
            is_active = (pid == current)
            # 用 type 参数区分激活状态
            btn_type = "primary" if is_active else "secondary"
            if st.button(
                    icon,
                    key=f"nav_{pid}",
                    help=label,
                    use_container_width=True,
                    type=btn_type
            ):
                if st.session_state.active_panel != pid:
                    st.session_state.active_panel = pid
                    st.rerun()  # 🔥 fragment 内 rerun，只重跑本函数
    st.markdown('</div>', unsafe_allow_html=True)

    # ===== 内容面板 =====
    panel_id = current

    # 🔥 P0 精简 + P2 更新：只有真正支持过滤的面板才渲染搜索框。
    # 支持搜索的：场景（资产树 + 场景库都会过滤）、组件（组件库+模板库）、
    # 外观（风格+故事）。
    # 数据 / 设置 不使用 keyword，因此不渲染搜索框（避免死控件）。
    SEARCHABLE_PANELS = {'scene', 'component', 'style'}
    if panel_id in SEARCHABLE_PANELS:
        keyword = st.text_input(
            "搜索",
            key=f"search_{panel_id}",
            placeholder="🔍 搜索...",
            label_visibility="collapsed"
        )
    else:
        keyword = ''

    # 🔥 P2 Step2：面板合并在分发层完成——子面板仍是独立函数，便于维护。
    # ⚠️ 顺序约定：会提前 return 的子块（模板库 1597-1599、资产树 1664-1666）
    #    必须放在最后，否则会把同面板里前面的内容一起截断。
    if panel_id == 'scene':
        render_scene_panel(keyword)
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        render_asset_tree_panel(keyword)
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        render_scene_library_panel(keyword)
    elif panel_id == 'component':
        render_component_panel(keyword)
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        render_template_panel(keyword)
    elif panel_id == 'style':
        render_style_panel(keyword)
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        render_story_panel(keyword)
    elif panel_id == 'data':
        render_data_panel(keyword)
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        render_emission_panel(keyword)
    elif panel_id == 'settings':
        render_settings_panel(keyword)

    # ===== 底部固定操作条 =====
    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("💾 保存", key="fixed_save", use_container_width=True, help="保存当前场景"):
            if st.session_state.current_scene:
                mgr = st.session_state.scene_manager
                mgr.save_scene(st.session_state.current_scene)
                save_scene_to_db()
                st.toast("✅ 场景已保存", icon="💾")
    with col2:
        if st.button("📤 导出", key="fixed_export", use_container_width=True, help="导出为 HTML"):
            if st.session_state.current_scene:
                mgr = st.session_state.scene_manager
                style_mgr = get_style_manager()
                style = style_mgr.get_style(st.session_state.get('current_style', 'tech_blue'))
                story_mgr = get_story_manager()
                story_id = st.session_state.get('current_story', None)
                story = story_mgr.get_story(story_id) if story_id else None
                st.session_state.export_html_content = mgr.export_to_html(
                    st.session_state.current_scene, style=style, story=story
                )
                st.toast("📤 已生成导出文件", icon="📤")
    with col3:
        if st.button("🆕 新建", key="fixed_new", use_container_width=True, help="新建空白场景"):
            create_empty_scene()

    if st.session_state.get('export_html_content'):
        st.download_button(
            label="📥 下载 HTML",
            data=st.session_state.export_html_content,
            file_name=f"{get_scene_name_safe().replace(' ', '_')}_export.html",
            mime="text/html",
            use_container_width=True,
            key="fixed_download"
        )


# ==================== 面板1：场景 ====================

def render_scene_panel(keyword=''):
    """场景面板：场景名 + 大屏展示入口

    注：keyword 参数保留是为了与 render_left_panel 的统一分发签名一致，
    本面板不使用搜索（P0 起不再为它渲染搜索框）。
    """
    panel_header("🏠", "当前场景")

    # 场景名
    scene_name = st.text_input(
        "场景名称",
        value=get_scene_name_safe(),
        key="panel_scene_name",
        label_visibility="collapsed",
        placeholder="输入场景名称..."
    )
    if st.session_state.current_scene:
        set_scene_name_safe(scene_name)

    # 🔥 P0 精简：此处原有「场景统计」指标组（对象/充电桩/已绑定，st.columns+st.metric）
    # 和「⚡ 快速操作」3 个按钮，已一并删除：
    #   · 指标与底部常驻指标条 render_bottom_bar(4694-4702) 重复，后者已含
    #     "🏗️ 总对象 / ⚡ 充电桩 (N 已绑定)"；
    #   · 3 个"快速操作"按钮唯一作用是把 active_panel 切到别的面板，与顶部图标栏
    #     (render_left_panel 的 8 个导航按钮) 是同一导航的第二套实现。

    # 🆕 大屏展示模式入口
    st.markdown("**🖥️ 展示模式**")
    if st.button("📺 进入大屏模式", key="enter_big_screen", use_container_width=True, type="primary"):
        st.session_state.big_screen_mode = True
        st.rerun(scope="app")

    # 🔥 P0 精简：原「📝 最近操作」已删除——render_right_panel(4595-4602) 已用
    # 同一份 operation_history[-5:][::-1] 渲染完全相同的内容，属重复展示。


# ==================== 面板2：模板 ====================

def render_template_panel(keyword=''):
    """模板面板：按类别分组的模板库"""
    templates_all = [
        ("charging_station", "充电站(标准)", "🔋", "10快充+5慢充", "充电设施"),
        ("charging_station_large", "充电站(大型)", "⚡", "25快充+10慢充", "充电设施"),
        ("charging_station_highway", "高速服务区", "🛣️", "20快充+5超充", "充电设施"),
        ("factory_workshop", "工厂车间", "🏗️", "厂房+设备", "工业场景"),
        ("smart_factory", "智能工厂", "🤖", "AGV+设备", "工业场景"),
        ("simple_office", "办公楼", "🏢", "主楼+停车", "商业办公"),
        ("commercial_complex", "商业综合体", "🏙️", "主楼+裙楼", "商业办公"),
        ("industrial_park", "工业园区", "🏭", "多栋厂房", "商业办公"),
    ]

    # 搜索过滤
    if keyword:
        templates = [t for t in templates_all if fuzzy_match(keyword, t[1] + ' ' + t[3])]
    else:
        templates = templates_all

    panel_header("📁", "模板库", count=f"{len(templates)} 个模板")

    if not templates:
        empty_state("🔍", "未找到匹配的模板", "试试搜索「充电」或「工厂」")
        return

    # 按类别分组
    categories = {}
    for t in templates:
        cat = t[4]
        categories.setdefault(cat, []).append(t)

    for cat, items in categories.items():
        st.markdown(f"**{cat}**")
        cols = st.columns(2)
        for idx, (key, name, icon, desc, _) in enumerate(items):
            with cols[idx % 2]:
                if st.button(
                        f"{icon} {name}",
                        key=f"tpl_{key}",
                        use_container_width=True,
                        help=desc
                ):
                    apply_template(key)
                    st.rerun(scope="app")
                st.caption(desc)


# ==================== 面板：资产树 ====================

def render_asset_tree_panel(keyword=''):
    """资产树面板：按类型分组的树形资产清单"""
    objects = st.session_state.scene_objects

    # ===== 分类映射 =====
    category_map = {
        'charger_fast':   ('⚡ 充电设备', '快充桩'),
        'charger_slow':   ('⚡ 充电设备', '慢充桩'),
        'charger_super':  ('⚡ 充电设备', '超充桩'),
        'building':       ('🏢 建筑',     '标准建筑'),
        'building_tall':  ('🏢 建筑',     '高层建筑'),
        'tree':           ('🌳 景观',     '树木'),
        'tree_pine':      ('🌳 景观',     '松树'),
        'lamp':           ('🌳 景观',     '路灯'),
        'road_straight':  ('🛣️ 道路',     '直线道路'),
        'road_curve':     ('🛣️ 道路',     '弯道'),
        'car':            ('🚗 车辆',     '轿车'),
        'truck':          ('🚗 车辆',     '货车'),
    }

    # ===== 分组 =====
    tree = {}
    for obj in objects:
        obj_type = obj.get('type', 'unknown')
        cat_key, _ = category_map.get(obj_type, ('📦 其他', obj_type))
        tree.setdefault(cat_key, []).append(obj)

    # ===== 搜索过滤 =====
    if keyword:
        tree = {
            cat: [o for o in objs
                  if fuzzy_match(keyword, f"{o.get('name', '')} {o.get('type', '')}")]
            for cat, objs in tree.items()
        }
        tree = {k: v for k, v in tree.items() if v}

    total_count = sum(len(v) for v in tree.values())
    panel_header("🌲", "资产树", count=f"{total_count} 个")

    if not tree:
        empty_state("🔍", "未找到资产", "试试搜索「充」或「建筑」")
        return

    selected_id = st.session_state.get('selected_object_id', None)

    # ===== 分类折叠显示 =====
    for cat, objs in sorted(tree.items()):
        # 该分类平均利用率
        chargers = [o for o in objs if o.get('type', '').startswith('charger')]
        if chargers:
            avg_u = sum(c.get('utilization', 0.5) for c in chargers) / len(chargers)
            avg_str = f" · 平均 {avg_u:.0%}"
        else:
            avg_str = ""

        # 🔥 关键：给 expander 加 key，让它记住用户的展开/收起状态
        # key 用分类名（去掉特殊字符）保证稳定
        expander_key = "tree_exp_v2_" + cat.replace(' ', '_').replace('·', '').strip()

        with st.expander(f"{cat} ({len(objs)}{avg_str})",
                         expanded=False,
                         key=expander_key):

            for obj in objs:
                obj_id = obj.get('id', '')
                obj_name = obj.get('name', '未命名')
                util = obj.get('utilization', 0)
                bound = obj.get('bind_station_id', '')
                is_charger = obj.get('type', '').startswith('charger')
                is_selected = (obj_id == selected_id)

                # 利用率颜色
                if util > 0.7:
                    util_color = "#ff6b6b"
                elif util > 0.4:
                    util_color = "#fcc419"
                else:
                    util_color = "#51cf66"

                # 选中高亮
                bg = "rgba(74,144,217,0.25)" if is_selected else "rgba(26,42,68,0.4)"
                border = "#4a90d9" if is_selected else "#1a2a44"

                # 利用率 / 类型显示
                if is_charger:
                    util_html = f'<span style="color:{util_color};font-weight:700;">{util:.0%}</span>'
                else:
                    util_html = f'<span style="color:#667;font-size:11px;">{obj.get("type", "")}</span>'

                # 绑定状态
                if is_charger:
                    if bound:
                        bind_html = f'<span style="color:#51cf66;">✅ {bound}</span>'
                    else:
                        bind_html = '<span style="color:#667;">❌ 未绑定</span>'
                else:
                    bind_html = ""

                # ===== 关键修复：HTML 压成单行 =====
                card_html = (
                    f'<div style="background:{bg};border:1px solid {border};'
                    f'border-radius:8px;padding:6px 10px;margin-bottom:4px;">'
                    f'<div style="display:flex;justify-content:space-between;'
                    f'align-items:center;gap:8px;">'
                    f'<span style="color:#eef2ff;font-size:13px;font-weight:500;'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                    f'{obj_name}</span>'
                    f'{util_html}'
                    f'</div>'
                    f'<div style="font-size:11px;margin-top:2px;">{bind_html}</div>'
                    f'</div>'
                )

                col_info, col_btn = st.columns([5, 1])
                with col_info:
                    st.markdown(card_html, unsafe_allow_html=True)

                with col_btn:
                    btn_label = "🎯" if not is_selected else "✓"
                    if st.button(btn_label, key=f"tree_sel_{obj_id}",
                                 help="选中此物体" if not is_selected else "已选中"):
                        st.session_state.selected_object_id = obj_id
                        st.rerun(scope="app")

    # ===== 汇总统计（关键修复：改用 HTML 表格，不用 st.dataframe） =====
    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)
    st.markdown("**📊 分类统计**")

    rows_html = ""
    for cat, objs in sorted(tree.items()):
        total_in_cat = len(objs)
        bound_in_cat = len([o for o in objs
                            if o.get('type', '').startswith('charger')
                            and o.get('bind_station_id')])
        bound_str = str(bound_in_cat) if bound_in_cat > 0 else "—"
        rows_html += (
            f'<tr>'
            f'<td style="text-align:left;">{cat}</td>'
            f'<td>{total_in_cat}</td>'
            f'<td>{bound_str}</td>'
            f'</tr>'
        )

    table_html = (
        f'<table class="custom-table">'
        f'<thead><tr>'
        f'<th style="text-align:left;">分类</th>'
        f'<th>数量</th>'
        f'<th>已绑定</th>'
        f'</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
    )
    st.markdown(table_html, unsafe_allow_html=True)

# ==================== 面板3：组件 ====================

def render_component_panel(keyword=''):
    """组件面板：常用组件 + 全部分类 + 我的资产"""
    # 常用组件（首次默认）
    recent = st.session_state.get('recent_components',
                                  ['charger_fast', 'charger_slow', 'building', 'tree'])

    panel_header("🧩", "组件库")

    # ===== 常用组件 4 宫格 =====
    st.markdown("**⭐ 常用**")
    cols = st.columns(4)
    for i, cid in enumerate(recent):
        comp = component_lib.get(cid)
        if not comp:
            continue
        with cols[i % 4]:
            if st.button(
                    comp.icon,
                    key=f"recent_{cid}",
                    use_container_width=True,
                    help=comp.name
            ):
                add_object_to_scene(cid)
                st.rerun(scope="app")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 全部组件按类别 =====
    total_shown = 0
    for category in component_lib.get_categories():
        comps = component_lib.get_by_category(category)
        # 搜索过滤
        if keyword:
            comps = [c for c in comps if fuzzy_match(keyword, f"{c.name} {c.description}")]
        if not comps:
            continue
        total_shown += len(comps)
        with st.expander(f"📦 {category} ({len(comps)})", expanded=False):
            cols = st.columns(2)
            for idx, comp in enumerate(comps):
                with cols[idx % 2]:
                    if st.button(
                            f"{comp.icon} {comp.name}",
                            key=f"comp_{comp.id}_{idx}",
                            use_container_width=True,
                            help=comp.description
                    ):
                        add_object_to_scene(comp.id)
                        st.rerun(scope="app")
    # 🔥 循环到此结束

    # ============================================================
    # 🎨 我的资产（自定义模型库）—— 🔴 在循环外面，只渲染一次
    # ============================================================
    st.markdown("<hr style='margin: 12px 0;'>", unsafe_allow_html=True)

    from core.asset_library import (
        get_my_assets, upload_asset, delete_asset,
        get_session_user_id, ALLOWED_CATEGORIES
    )

    my_assets = get_my_assets(include_public=True)
    my_count = len([a for a in my_assets if a['user_id'] == get_session_user_id()])

    with st.expander(f"🎨 我的资产 ({my_count})", expanded=False):

        # ===== 上传区 =====
        st.markdown("**📤 上传自定义模型**")
        st.caption("支持 `.glb` / `.gltf`，单文件 ≤ 20 MB")

        # 🔥 动态 key：使用 session 内计数器，避免 fragment rerun 冲突
        if 'asset_ui_counter' not in st.session_state:
            st.session_state.asset_ui_counter = 0
        st.session_state.asset_ui_counter += 1
        _dynamic_key = f"asset_upload_file_{st.session_state.asset_ui_counter}"

        uploaded_file = st.file_uploader(
            "选择模型文件",
            type=['glb', 'gltf'],
            key=_dynamic_key,
            label_visibility="collapsed"
        )

        if uploaded_file is not None:
            col_up1, col_up2 = st.columns([3, 2])
            with col_up1:
                asset_name = st.text_input(
                    "模型名称",
                    value=uploaded_file.name.rsplit('.', 1)[0][:40],
                    key="asset_upload_name",
                    placeholder="给模型起个名字"
                )
            with col_up2:
                asset_cat = st.selectbox(
                    "分类",
                    options=ALLOWED_CATEGORIES,
                    key="asset_upload_cat"
                )

            is_public = st.checkbox(
                "🌐 分享到公共库（其他用户可见）",
                value=False,
                key="asset_upload_public"
            )

            file_size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
            st.caption(f"📦 文件大小：{file_size_mb:.2f} MB")

            if st.button("✅ 确认上传", key="asset_confirm_upload",
                         use_container_width=True, type="primary"):
                if not asset_name.strip():
                    st.error("请填写模型名称")
                elif file_size_mb > 20:
                    st.error("文件超过 20 MB，请压缩后再上传")
                else:
                    with st.spinner("正在上传到云端..."):
                        record = upload_asset(
                            file_bytes=uploaded_file.getvalue(),
                            original_filename=uploaded_file.name,
                            display_name=asset_name.strip(),
                            category=asset_cat,
                            is_public=is_public
                        )
                    if record:
                        st.toast(f"✅ 已上传：{record['name']}", icon="📤")
                        st.rerun()
                    else:
                        st.error("❌ 上传失败，请检查网络或 Supabase 配置")

        st.markdown("---")

        # ===== 资产列表 =====
        st.markdown("**🗂️ 可用模型**")

        if not my_assets:
            st.markdown(
                '<div style="text-align:center;padding:20px;color:#667;font-size:12px;">'
                '还没有上传任何模型<br>点击上方「选择模型文件」开始'
                '</div>',
                unsafe_allow_html=True
            )
        else:
            current_uid = get_session_user_id()

            for asset in my_assets:
                asset_id = asset.get('id', '')
                asset_name_ui = asset.get('name', '未命名')
                asset_cat_ui = asset.get('category', '其他')
                is_mine = (asset.get('user_id') == current_uid)
                is_pub = asset.get('is_public', False)
                file_size_kb = asset.get('file_size', 0) / 1024

                # 分类图标
                cat_icon_map = {
                    '充电设备': '⚡', '建筑': '🏢', '景观': '🌳',
                    '道路': '🛣️', '车辆': '🚗', '其他': '📦'
                }
                cat_icon = cat_icon_map.get(asset_cat_ui, '📦')
                owner_tag = '👤 我的' if is_mine else '🌐 公共'

                # 🔥 尺寸与缩放：上传时已量好，这里直接展示 —— 用户终于能看见
                #    "模型本来多大 / 会被缩到多大"，而不是盲调 scale
                rs = (asset.get('raw_size_x'), asset.get('raw_size_y'), asset.get('raw_size_z'))
                has_size = all(isinstance(v, (int, float)) and v for v in rs)
                tgt_h = asset.get('target_height')
                size_line = ''
                if has_size:
                    if tgt_h:
                        size_line = (f'📏 原始 {rs[0]:.2f}×{rs[1]:.2f}×{rs[2]:.2f} m '
                                     f'→ 目标高 {tgt_h:.1f} m · '
                                     f'scale ({asset.get("scale_x", 1):.3f}, '
                                     f'{asset.get("scale_y", 1):.3f}, '
                                     f'{asset.get("scale_z", 1):.3f})')
                    else:
                        size_line = f'📏 原始 {rs[0]:.2f}×{rs[1]:.2f}×{rs[2]:.2f} m'

                # 卡片
                st.markdown(
                    f'<div style="padding:8px 12px;background:rgba(26,42,68,0.4);'
                    f'border-left:3px solid {"#51cf66" if is_mine else "#88aadd"};'
                    f'border-radius:8px;margin:6px 0 4px 0;">'
                    f'<div style="color:#eef2ff;font-weight:600;font-size:13px;">'
                    f'{cat_icon} {asset_name_ui}</div>'
                    f'<div style="color:#667;font-size:11px;margin-top:3px;">'
                    f'{owner_tag} · {asset_cat_ui} · {file_size_kb:.0f} KB</div>'
                    f'{f"<div style=\'color:#8899bb;font-size:10.5px;margin-top:3px;\'>{size_line}</div>" if size_line else ""}'
                    f'</div>',
                    unsafe_allow_html=True
                )

                col_use, col_del = st.columns([4, 1])
                with col_use:
                    if st.button(
                            "➕ 添加到场景",
                            key=f"asset_add_{asset_id}",
                            use_container_width=True,
                            help="在场景中生成一个实例"
                    ):
                        import random as _random
                        glb_url = asset.get('file_url', '')
                        if not glb_url:
                            st.error("❌ 该资产没有可用的文件 URL")
                        else:
                            new_obj = {
                                'id': str(uuid.uuid4()),
                                'type': 'custom_model',
                                'name': asset_name_ui,
                                'position': {
                                    'x': _random.uniform(-2, 2),
                                    'y': 0.0,
                                    'z': _random.uniform(-2, 2)
                                },
                                'rotation': {'x': 0, 'y': 0, 'z': 0},
                                # scale 用上传时按类别算好的系数：导进去就是合适体量
                                'scale': {
                                    'x': asset.get('scale_x', 1.0),
                                    'y': asset.get('scale_y', 1.0),
                                    'z': asset.get('scale_z', 1.0)
                                },
                                'bind_station_id': '',
                                'custom_props': {
                                    'glb_url': glb_url,
                                    'asset_id': asset_id,
                                    'category': asset_cat_ui,
                                    'is_custom_asset': True,
                                    # 目标尺寸：前端据此再做一次按轴归一化，
                                    # 老资产（没有这些字段）则由前端按包围盒兜底
                                    'target_height': asset.get('target_height'),
                                    'target_length': asset.get('target_length'),
                                    'target_width': asset.get('target_width'),
                                },
                                'utilization': 0.5
                            }
                            st.session_state.scene_objects.append(new_obj)
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects

                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                sync_scene_objects(
                                    st.session_state.scene_id,
                                    st.session_state.scene_objects
                                )

                            st.session_state.selected_object_id = new_obj['id']
                            st.toast(f"✅ 已添加：{asset_name_ui}", icon="➕")
                            st.rerun()

                with col_del:
                    if is_mine:
                        if st.button("🗑️", key=f"asset_del_{asset_id}",
                                     help="删除此资产"):
                            if delete_asset(asset_id):
                                st.toast(f"🗑️ 已删除：{asset_name_ui}", icon="🗑️")
                                st.rerun()
                            else:
                                st.error("删除失败")
                    else:
                        st.markdown(
                            '<div style="text-align:center;color:#445;font-size:18px;'
                            'padding-top:6px;">🔒</div>',
                            unsafe_allow_html=True
                        )

    # ===== 空状态提示（🔴 保留在最后，在循环外） =====
    if keyword and total_shown == 0:
        empty_state("🔍", "未找到匹配的组件", "试试搜索「充」或「树」")


# ==================== 面板4：外观 ====================

def render_style_panel(keyword=''):
    """外观面板：视觉风格切换 + 场景配色调节"""
    style_mgr = get_style_manager()
    styles = style_mgr.get_style_list()
    current_style_id = st.session_state.get('current_style', 'tech_blue')

    # 搜索过滤
    # 搜索过滤（模糊匹配）
    if keyword:
        styles = [s for s in styles if fuzzy_match(keyword, f"{s['name']} {s.get('description', '')}")]

    panel_header("🎨", "视觉风格", count=f"{len(styles)} 种")

    if not styles:
        empty_state("🔍", "未找到匹配的风格")
        return

    # ===== 风格预设（4 宫格） =====
    st.markdown("**🎨 风格预设**")
    cols = st.columns(2)
    for i, style in enumerate(styles):
        with cols[i % 2]:
            is_active = (style['id'] == current_style_id)
            btn_label = f"{style['icon']} {style['name']}"
            if st.button(
                btn_label,
                key=f"style_{style['id']}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
                help=style.get('description', '')
            ):
                if not is_active:
                    st.session_state.current_style = style['id']
                    st.query_params['style'] = style['id']
                    st.rerun()
            if is_active:
                st.caption("✅ 当前使用")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 场景配色调节（实时预览） =====
    st.markdown("**🎭 场景配色**")

    current_style = style_mgr.get_style(current_style_id)

    # 环境光强度
    new_ambient = st.slider(
        "环境光强度",
        min_value=0.0, max_value=3.0,
        value=float(current_style.ambient_intensity),
        step=0.1,
        key="style_ambient"
    )
    if abs(new_ambient - current_style.ambient_intensity) > 0.05:
        current_style.ambient_intensity = new_ambient
        st.session_state._style_dirty = True

    # 太阳光强度
    new_sun = st.slider(
        "太阳光强度",
        min_value=0.0, max_value=5.0,
        value=float(current_style.sun_intensity),
        step=0.1,
        key="style_sun"
    )
    if abs(new_sun - current_style.sun_intensity) > 0.05:
        current_style.sun_intensity = new_sun
        st.session_state._style_dirty = True

    # 泛光强度
    new_bloom = st.slider(
        "泛光强度",
        min_value=0.0, max_value=2.0,
        value=float(current_style.bloom_strength),
        step=0.05,
        key="style_bloom"
    )
    if abs(new_bloom - current_style.bloom_strength) > 0.01:
        current_style.bloom_strength = new_bloom
        st.session_state._style_dirty = True

    if st.session_state.get('_style_dirty'):
        if st.button("✅ 应用配色", key="apply_style", use_container_width=True, type="primary"):
            st.session_state._style_dirty = False
            st.rerun()

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 场景背景 =====
    st.markdown("**🖼️ 场景背景**")
    bg_options = ["深空蓝（默认）", "纯黑", "深紫", "深绿", "自定义"]
    bg_idx = st.selectbox(
        "背景色调",
        options=range(len(bg_options)),
        format_func=lambda i: bg_options[i],
        key="bg_select",
        label_visibility="collapsed"
    )
    if bg_options[bg_idx] == "纯黑":
        st.session_state._bg_override = "#000000"
    elif bg_options[bg_idx] == "深紫":
        st.session_state._bg_override = "#0f0a1a"
    elif bg_options[bg_idx] == "深绿":
        st.session_state._bg_override = "#0a1a0f"
    else:
        st.session_state._bg_override = None


# ==================== 面板5：故事 ====================

def render_story_panel(keyword=''):
    """故事面板：一键切换场景视角 + 自动导览"""
    story_mgr = get_story_manager()
    stories = story_mgr.get_story_list()
    current_story_id = st.session_state.get('current_story', None)

    # 搜索过滤
    if keyword:
        stories = [s for s in stories if fuzzy_match(keyword, f"{s['name']} {s.get('description', '')}")]

    panel_header("🎬", "场景故事", count=f"{len(stories)} 个")

    # ===== 当前故事状态 =====
    if current_story_id:
        current_story = story_mgr.get_story(current_story_id)
        if current_story:
            st.success(f"▶️ 正在播放：**{current_story.name}**")
            st.caption(f"📖 {current_story.description}")
            col_p1, col_p2 = st.columns(2)
            with col_p1:
                if st.button("⏹️ 退出故事", use_container_width=True):
                    st.session_state.current_story = None
                    if 'story' in st.query_params:
                        del st.query_params['story']
                    st.rerun()
            with col_p2:
                if st.button("🔄 重播", use_container_width=True):
                    st.query_params['story'] = current_story_id
                    st.rerun()
            st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    if not stories:
        empty_state("🔍", "未找到匹配的故事", "试试搜索「全貌」或「高峰」")
        return

    # ===== 故事列表 =====
    st.markdown("**📖 预设故事**")
    for story in stories:
        is_active = (story['id'] == current_story_id)
        btn_label = f"{story['icon']} {story['name']}"
        if st.button(
            btn_label,
            key=f"story_{story['id']}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
            help=story.get('description', '')
        ):
            if not is_active:
                st.session_state.current_story = story['id']
                st.query_params['story'] = story['id']
                st.rerun()
        if is_active:
            st.caption("✅ 正在播放")
        else:
            st.caption(story.get('description', ''))

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 自动导览 =====
    st.markdown("**🎥 自动导览**")
    st.caption("按顺序播放所有故事，适合演示")
    if st.button("▶️ 开始自动导览", key="auto_tour", use_container_width=True):
        st.session_state.auto_tour = True
        st.session_state.auto_tour_index = 0
        if stories:
            st.session_state.current_story = stories[0]['id']
            st.query_params['story'] = stories[0]['id']
            st.rerun()

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 自定义故事 =====
    st.markdown("**✏️ 自定义故事**")
    st.caption("保存当前视角为故事")
    story_name = st.text_input(
        "故事名称",
        key="custom_story_name",
        placeholder="如：我的视角",
        label_visibility="collapsed"
    )
    if st.button("💾 保存当前视角", key="save_story", use_container_width=True):
        if story_name:
            st.toast(f"✅ 已保存故事：{story_name}", icon="🎬")
        else:
            st.warning("请输入故事名称")


# ===== 场景面板的子块：场景库（P2 Step4 起并入"场景"面板） =====

def render_scene_library_panel(keyword=''):
    """场景库面板：我的云端场景 / 公共场景市场 / 已保存场景 / 云端同步

    🔥 P2 Step1：由原 render_saved_panel 拆分而来（纯搬家，逻辑未改动）。
    数据导入导出 → render_data_panel；数据源 / MQTT 配置 → render_settings_panel。
    """
    mgr = st.session_state.scene_manager
    saved_scenes = mgr.get_scene_list()

    # 搜索过滤
    if keyword:
        saved_scenes = [s for s in saved_scenes if fuzzy_match(keyword, s['name'])]

    panel_header("📂", "场景库", count=f"{len(saved_scenes)} 个")

    # ============================================================
    # 🔥 内置示例小镇（零文件 / 零服务 / 零配置）
    #   部署后新用户打开就是空场景，这个入口让他一步看到完整小镇，
    #   不必先下载 JSON 上传、也不必本地起数据库。
    # ============================================================
    try:
        from core.demo_town import render_demo_town_entry
        render_demo_town_entry()
        st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)
    except Exception as e:
        print(f"⚠️ 示例小镇入口渲染失败（已跳过）: {e}")

    # ============================================================
    # 🔥 预览模式顶部提示
    # ============================================================
    if st.session_state.get('_preview_original'):
        st.warning("👁️ 当前为**预览模式**，你的浏览不会影响原场景")
        col_prev1, col_prev2 = st.columns(2)
        with col_prev1:
            if st.button("✕ 退出预览", key="exit_preview_top", use_container_width=True, type="primary"):
                orig = st.session_state.pop('_preview_original', None)
                if orig:
                    st.session_state.current_scene = orig['current_scene']
                    st.session_state.scene_objects = orig['scene_objects']
                    st.session_state.selected_object_id = orig['selected_object_id']
                    st.session_state.scene_id = orig['scene_id']
                    st.session_state._db_initialized = orig['_db_initialized']
                    st.toast("✅ 已退出预览，返回原场景", icon="↩️")
                    st.rerun()
        with col_prev2:
            if st.button("📥 克隆为我的场景", key="clone_preview_top", use_container_width=True):
                cur_sid = st.session_state.get('scene_id')
                if cur_sid:
                    new_id = clone_scene(cur_sid)
                    if new_id:
                        st.session_state.pop('_preview_original', None)
                        st.session_state.scene_id = new_id
                        st.session_state._db_initialized = False
                        st.toast("✅ 已克隆为你的场景", icon="📥")
                        st.rerun()
                    else:
                        st.error("克隆失败")
        st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ============================================================
    # 🔥 任务10：场景市场 + 地理底图
    # ============================================================

    # ===== 📤 我的云端场景 =====
    with st.expander("📤 我的云端场景", expanded=False):
        if SUPABASE_AVAILABLE:
            scene_id = st.session_state.get('scene_id', None)
            if scene_id:
                st.success(f"✅ 当前场景已连接（ID: {scene_id[:8]}...）")

                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    if st.button("🌐 发布到公共库", key="publish_btn", use_container_width=True, type="primary"):
                        st.session_state['_show_publish_form'] = True
                        st.rerun()
                with col_s2:
                    if st.button("🔄 立即同步", key="sync_now_market", use_container_width=True):
                        try:
                            sync_scene_objects(scene_id, st.session_state.scene_objects)
                            st.toast("☁️ 已同步到云端", icon="☁️")
                        except Exception as e:
                            st.error(f"❌ 同步失败：{e}")

                # 发布表单
                if st.session_state.get('_show_publish_form', False):
                    with st.form("publish_form"):
                        st.caption("发布后其他用户可以在公共市场看到并克隆此场景")
                        author = st.text_input("作者名", value="匿名", key="publish_author")
                        desc = st.text_area("场景描述", placeholder="简单描述用途，如：长春市朝阳区充电站布局方案", key="publish_desc", height=80)
                        tags_input = st.text_input("标签（用逗号分隔）", placeholder="充电站,长春,高速服务区", key="publish_tags")

                        col_f1, col_f2 = st.columns(2)
                        with col_f1:
                            if st.form_submit_button("✅ 确认发布", use_container_width=True, type="primary"):
                                tags_list = [t.strip() for t in tags_input.split(',') if t.strip()]
                                sync_scene_objects(scene_id, st.session_state.scene_objects)
                                if publish_scene(scene_id, author, desc, tags_list):
                                    st.toast("🌐 已发布到公共场景库", icon="🌐")
                                    st.session_state['_show_publish_form'] = False
                                    st.rerun()
                                else:
                                    st.error("发布失败，请检查网络")
                        with col_f2:
                            if st.form_submit_button("❌ 取消", use_container_width=True):
                                st.session_state['_show_publish_form'] = False
                                st.rerun()
            else:
                st.warning("⚠️ 未关联云端场景")
        else:
            st.info("ℹ️ Supabase 未配置，公共市场不可用")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 🌐 公共场景市场 =====
    with st.expander("🌐 公共场景市场", expanded=False):
        st.caption("浏览其他用户的场景，一键克隆到我的场景库")

        market_search = st.text_input(
            "搜索场景",
            placeholder="🔍 输入关键词，如：长春 / 充电站",
            key="market_search",
            label_visibility="collapsed"
        )

        sort_by = st.radio(
            "排序方式",
            ["🔥 热门", "🆕 最新", "👁️ 浏览量"],
            horizontal=True,
            key="market_sort"
        )
        order_map = {"🔥 热门": "likes", "🆕 最新": "created_at", "👁️ 浏览量": "view_count"}
        order_field = order_map[sort_by]

        if SUPABASE_AVAILABLE:
            try:
                if market_search:
                    public_scenes = get_public_scenes_by_keyword(market_search, limit=20)
                else:
                    public_scenes = get_public_scenes(limit=20, order_by=order_field)

                if not public_scenes:
                    if market_search:
                        empty_state("🔍", "未找到匹配场景", "试试其他关键词")
                    else:
                        empty_state("🌐", "公共市场暂无场景", "成为第一个发布者吧！")
                else:
                    with st.container(height=520):
                        for scene in public_scenes:
                            sid = scene.get('id')
                            if not sid:
                                continue

                            is_current = (sid == st.session_state.get('scene_id'))
                            is_previewing = (
                                st.session_state.get('_preview_original') is not None
                                and st.session_state.get('scene_id') == sid
                            )

                            name = str(scene.get('name') or '未命名场景')
                            author = str(scene.get('author') or '匿名')
                            likes = scene.get('likes', 0) or 0
                            views = scene.get('view_count', 0) or 0
                            desc = (scene.get('description') or '')[:50]

                            if is_previewing:
                                badge = '<span style="color:#fcc419;font-size:10px;margin-left:4px;">[预览中]</span>'
                            elif is_current:
                                badge = '<span style="color:#51cf66;font-size:10px;margin-left:4px;">[当前]</span>'
                            else:
                                badge = ''

                            desc_html = (
                                f'<div style="color:#667;font-size:11px;margin-top:4px;">{desc}</div>'
                                if desc else ''
                            )

                            card_html = (
                                f'<div style="background:rgba(16,22,40,0.6);'
                                f'border:1px solid rgba(74,144,217,0.15);'
                                f'border-radius:12px;padding:12px;margin-bottom:8px;">'
                                f'<div style="color:#eef2ff;font-weight:600;font-size:13px;'
                                f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                                f'{name}{badge}</div>'
                                f'<div style="color:#8899bb;font-size:11px;margin-top:4px;">'
                                f'👤 {author} · ❤️ {likes} · 👁️ {views}</div>'
                                f'{desc_html}'
                                f'</div>'
                            )
                            st.markdown(card_html, unsafe_allow_html=True)

                            col_a, col_b, col_c = st.columns([1, 1, 2])
                            with col_a:
                                if st.button("❤️", key=f"like_{sid}", help="点赞", use_container_width=True):
                                    new_likes = like_scene(sid)
                                    if new_likes >= 0:
                                        st.toast(f"已点赞（{new_likes}）", icon="❤️")
                                        st.rerun()
                            with col_b:
                                if st.button("📥", key=f"clone_{sid}", help="克隆为我的场景", use_container_width=True):
                                    new_id = clone_scene(sid)
                                    if new_id:
                                        st.session_state.pop('_preview_original', None)
                                        st.session_state.scene_id = new_id
                                        st.session_state._db_initialized = False
                                        st.session_state.selected_object_id = None
                                        st.toast("✅ 已克隆到我的场景库", icon="📥")
                                        st.rerun()
                                    else:
                                        st.error("克隆失败")
                            with col_c:
                                if is_previewing:
                                    if st.button("✕ 退出预览", key=f"exit_{sid}", use_container_width=True, type="primary"):
                                        orig = st.session_state.pop('_preview_original', None)
                                        if orig:
                                            st.session_state.current_scene = orig['current_scene']
                                            st.session_state.scene_objects = orig['scene_objects']
                                            st.session_state.selected_object_id = orig['selected_object_id']
                                            st.session_state.scene_id = orig['scene_id']
                                            st.session_state._db_initialized = orig['_db_initialized']
                                            st.toast("✅ 已退出预览", icon="↩️")
                                            st.rerun()
                                else:
                                    if st.button("👁️ 预览", key=f"view_{sid}", use_container_width=True):
                                        increment_scene_view(sid)
                                        try:
                                            objs = get_scene_objects(sid)
                                            scene_resp = get_supabase_client().table("scenes").select("*").eq("id", sid).execute()
                                            if scene_resp.data:
                                                from core.scene_manager import SceneData
                                                st.session_state['_preview_original'] = {
                                                    'current_scene': st.session_state.current_scene,
                                                    'scene_objects': list(st.session_state.scene_objects),
                                                    'selected_object_id': st.session_state.selected_object_id,
                                                    'scene_id': st.session_state.scene_id,
                                                    '_db_initialized': st.session_state.get('_db_initialized', False),
                                                }
                                                preview_scene = SceneData(
                                                    scene_name=f"[预览] {scene_resp.data[0]['name']}",
                                                    objects=objs
                                                )
                                                st.session_state.current_scene = preview_scene
                                                st.session_state.scene_objects = objs
                                                st.session_state.selected_object_id = None
                                                st.session_state.scene_id = sid
                                                st.session_state._db_initialized = False
                                                st.toast("👁️ 已加载预览，从上方或此按钮退出", icon="👁️")
                                                st.rerun()
                                        except Exception as e:
                                            st.error(f"预览失败：{e}")

                            st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
            except Exception as e:
                st.warning(f"公共市场加载失败：{e}")
        else:
            st.info("ℹ️ Supabase 未配置，公共市场不可用")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 已保存场景列表 =====
    st.markdown("**💾 已保存场景**")
    if not saved_scenes:
        if keyword:
            empty_state("🔍", "未找到匹配的场景", "试试其他关键词")
        else:
            empty_state("📂", "暂无已保存场景", "用下面的示例小镇起步，或点底部「💾 保存」创建")
            try:
                from core.demo_town import render_demo_town_entry as _demo_entry
                _demo_entry(compact=True, key_suffix='_empty')
            except Exception as e:
                print(f"⚠️ 空场景下的示例入口渲染失败（已跳过）: {e}")
    else:
        for scene in saved_scenes[:10]:
            filename = scene['filename']
            name = scene['name']
            obj_count = scene.get('object_count', 0)
            updated = scene.get('updated_at', '')[:16].replace('T', ' ')

            with st.container():
                col_info, col_act = st.columns([3, 1.35])
                with col_info:
                    st.markdown(f"**{name}**")
                    st.caption(f"{obj_count} 个物体 · {updated}")
                with col_act:
                    c_load, c_del = st.columns(2)
                    with c_load:
                        if st.button("📂", key=f"load_{filename}", help="加载此场景",
                                     use_container_width=True):
                            filepath = os.path.join(mgr.data_dir, filename)
                            scene_obj = mgr.load_scene(filepath)
                            st.session_state.current_scene = scene_obj
                            st.session_state.scene_objects = scene_obj.objects
                            st.session_state.selected_object_id = None
                            if SUPABASE_AVAILABLE:
                                try:
                                    new_scene = get_or_create_scene(None)
                                    if new_scene:
                                        st.session_state.scene_id = new_scene['id']
                                        sync_scene_objects(
                                            st.session_state.scene_id,
                                            st.session_state.scene_objects
                                        )
                                except Exception:
                                    pass
                            st.session_state._db_initialized = False
                            st.toast(f"✅ 已加载：{name}", icon="📂")
                            mark_dirty("load_scene")
                            st.rerun(scope="app")
                    with c_del:
                        # 🔥 删除按钮：真删文件（调 SceneManager.delete_scene）
                        if st.button("🗑️", key=f"del_{filename}",
                                     help="删除这个场景存档（会真删文件）",
                                     use_container_width=True):
                            st.session_state['_confirm_del_scene'] = filename
                            st.rerun()

            # 删除确认：默认不删，必须勾选才允许（防误点）
            if st.session_state.get('_confirm_del_scene') == filename:
                st.warning(f"⚠️ 确认删除「{name}」？文件 `{filename}` 会被**永久删除**，无法撤销。")
                _ok_del = st.checkbox("我确认删除这个场景文件", key=f"_ok_del_{filename}")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("✅ 确认删除", key=f"del_ok_{filename}",
                                 type="primary", use_container_width=True, disabled=not _ok_del):
                        _fp = os.path.join(mgr.data_dir, filename)
                        # 若删的正是当前加载的场景，顺带清掉会话引用，避免指向不存在的文件
                        _was_current = (
                            st.session_state.get('current_scene') is not None
                            and getattr(st.session_state.current_scene, 'scene_name', '') == name
                        )
                        if mgr.delete_scene(filename):
                            st.session_state.pop('_confirm_del_scene', None)
                            if _was_current:
                                st.session_state.current_scene = None
                                st.session_state.scene_objects = []
                                st.session_state.selected_object_id = None
                                st.session_state.scene_id = None
                                st.session_state._db_initialized = False
                            st.toast(f"🗑️ 已删除：{name}", icon="🗑️")
                            st.rerun(scope="app")
                        else:
                            st.error(f"❌ 删除失败：{filename}（文件可能已被移除或无权限）")
                with _c2:
                    if st.button("✕ 取消", key=f"del_cancel_{filename}",
                                 use_container_width=True):
                        st.session_state.pop('_confirm_del_scene', None)
                        st.rerun()

    # ===== 其他文件（备份等）：以前它们被当成"0 个物体的场景"，现在单独列出并可删除 =====
    try:
        _other_files = mgr.list_other_files()
    except Exception as e:
        _other_files = []
        print(f"⚠️ 读取其它文件失败: {e}")

    if _other_files:
        st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)
        with st.expander(f"🗂️ 其他文件（{len(_other_files)}）", expanded=False):
            st.caption("这些不是场景存档（无法加载），是备份/导出类文件。可以在这里删掉腾空间。")
            for _f in _other_files:
                _fn = _f['filename']
                with st.container():
                    _ci, _ca = st.columns([3, 1])
                    with _ci:
                        st.markdown(f"**{_fn}**")
                        st.caption(f"{_f['desc']} · {_f['size_kb']:.0f} KB")
                    with _ca:
                        if st.button("🗑️", key=f"del_other_{_fn}", help="删除此文件",
                                     use_container_width=True):
                            st.session_state['_confirm_del_other'] = _fn
                            st.rerun()

                if st.session_state.get('_confirm_del_other') == _fn:
                    st.warning(f"⚠️ 确认删除 `{_fn}`？此操作不可撤销。")
                    _ok = st.checkbox("我确认删除该文件", key=f"_ok_other_{_fn}")
                    _oc1, _oc2 = st.columns(2)
                    with _oc1:
                        if st.button("✅ 确认删除", key=f"del_other_ok_{_fn}",
                                     type="primary", use_container_width=True, disabled=not _ok):
                            if mgr.delete_scene(_fn):
                                st.session_state.pop('_confirm_del_other', None)
                                st.toast(f"🗑️ 已删除：{_fn}", icon="🗑️")
                                st.rerun(scope="app")
                            else:
                                st.error(f"❌ 删除失败：{_fn}")
                    with _oc2:
                        if st.button("✕ 取消", key=f"del_other_cancel_{_fn}",
                                     use_container_width=True):
                            st.session_state.pop('_confirm_del_other', None)
                            st.rerun()

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 云端同步状态 =====
    st.markdown("**☁️ 云端同步**")
    if SUPABASE_AVAILABLE:
        scene_id = st.session_state.get('scene_id', None)
        if scene_id:
            st.success(f"✅ 已连接云端（ID: {scene_id[:8]}...）")
            if st.button("🔄 立即同步", key="sync_now", use_container_width=True):
                try:
                    sync_scene_objects(scene_id, st.session_state.scene_objects)
                    st.toast("☁️ 已同步到云端", icon="☁️")
                except Exception as e:
                    st.error(f"❌ 同步失败：{e}")
        else:
            st.warning("⚠️ 未关联云端场景")
    else:
        st.info("ℹ️ Supabase 未配置，使用本地存储")


def render_data_panel(keyword=''):
    """数据面板：原始数据导入（CSV/GeoJSON）、场景存档、导出与场景状态打印

    🔥 P2 Step1：由原 render_saved_panel 拆分而来（纯搬家）。
    本面板不使用 keyword，保留签名是为了与统一分发签名一致。
    """
    # ===== 📥 数据导入与导出 =====
    st.markdown("**📥 数据导入与导出**")

    # ============================================================
    # 方式1：导入原始数据（支持规则引擎）
    # ============================================================
    with st.expander("🔨 导入原始数据（CSV / GeoJSON）", expanded=False):
        st.caption("💡 支持 **CSV**（点数据）和 **GeoJSON**（点/线/面），自动识别几何类型并映射为 3D 物体")

        # 规则配置
        render_rule_editor()

        st.markdown("<hr style='margin: 12px 0;'>", unsafe_allow_html=True)

        # 数据文件上传（独立一行）
        st.markdown("<hr style='margin: 12px 0;'>", unsafe_allow_html=True)
        st.markdown("**📂 上传数据文件**")

        data_file = st.file_uploader(
            "选择数据文件",
            type=['csv', 'json', 'geojson'],
            key="upload_data_file",
            label_visibility="collapsed",
            help="支持 CSV / JSON / GeoJSON，文件大小 ≤ 200MB"
        )

        if data_file is not None:
            try:
                file_bytes = data_file.read()
                features = auto_parse(file_bytes, data_file.name)
                summary = get_feature_summary(features)

                st.markdown(
                    '<div style="background:rgba(26,42,68,0.6);border-radius:8px;'
                    'padding:10px 14px;font-size:12px;color:#8899bb;margin:8px 0;">'
                    '🔍 <b style="color:#88ccff;">识别结果</b><br>'
                    f'📍 点：<b style="color:#51cf66;">{summary["point"]}</b> 个 · '
                    f'〰️ 线：<b style="color:#51cf66;">{summary["line"]}</b> 条 · '
                    f'🔲 面：<b style="color:#51cf66;">{summary["polygon"]}</b> 个'
                    '</div>',
                    unsafe_allow_html=True
                )

                if not features:
                    st.warning("⚠️ 未识别到任何有效要素")
                else:
                    new_objects = generate_objects(features, st.session_state.gen_rules)

                    new_objects, was_clustered = auto_layout_if_clustered(
                        new_objects, threshold=2.0, spacing=3.0
                    )
                    if was_clustered:
                        st.info(f"📍 坐标过于集中，已网格化排列 {len(new_objects)} 个物体")

                    etl_log = []
                    original_count = len(new_objects)

                    seen = set()
                    deduped = []
                    for o in new_objects:
                        key = (o['name'], round(o['position']['x'], 2), round(o['position']['z'], 2))
                        if key not in seen:
                            seen.add(key)
                            deduped.append(o)
                    if original_count - len(deduped) > 0:
                        etl_log.append(f"🗑️ 去重：删除 {original_count - len(deduped)} 个重复物体")
                    new_objects = deduped

                    filtered = []
                    outlier_count = 0
                    for o in new_objects:
                        x, z = o['position']['x'], o['position']['z']
                        if -100 < x < 100 and -100 < z < 100:
                            filtered.append(o)
                        else:
                            outlier_count += 1
                    if outlier_count > 0:
                        etl_log.append(f"🚫 异常值：过滤 {outlier_count} 个坐标异常物体")
                    new_objects = filtered

                    if new_objects:
                        utils = [o.get('utilization', 0.5) for o in new_objects]
                        avg_u = sum(utils) / len(utils)
                        high_u = sum(1 for u in utils if u > 0.7)
                        etl_log.append(f"📊 统计：平均利用率 {avg_u:.1%}，高负载 {high_u} 个")

                    if etl_log:
                        etl_html = '<br>'.join(etl_log)
                        st.markdown(
                            '<div style="background:rgba(26,42,68,0.6);border-left:3px solid #4a90d9;'
                            'border-radius:8px;padding:10px 14px;margin-top:8px;font-size:12px;color:#aabbcc;">'
                            '<b style="color:#88ccff;">🧹 数据清洗报告</b><br>'
                            f'{etl_html}'
                            '</div>',
                            unsafe_allow_html=True
                        )

                    st.success(f"✅ 解析成功：{len(new_objects)} 个物体")

                    col_u1, col_u2 = st.columns(2)

                    with col_u1:
                        if st.button("➕ 追加到当前场景", key="append_upload",
                                     use_container_width=True):
                            st.session_state.scene_objects.extend(new_objects)
                            st.session_state.scene_objects = apply_planned_layout(
                                st.session_state.scene_objects
                            )
                            st.session_state.scene_objects = build_relations(
                                st.session_state.scene_objects
                            )
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects

                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                ok = sync_scene_objects(
                                    st.session_state.scene_id,
                                    st.session_state.scene_objects
                                )
                                if ok:
                                    st.toast(f"✅ 已追加 {len(new_objects)} 个物体（已同步云端）", icon="📊")
                                else:
                                    st.error("❌ 云端同步失败！数据仅保存在本地会话中，刷新后会丢失")
                                    st.caption("💡 请检查：1) Supabase 是否连通 2) scene_id 是否存在 3) scene_objects 表结构")
                                    st.stop()
                            else:
                                st.warning(f"⚠️ 已追加 {len(new_objects)} 个物体（Supabase 未启用，仅本地）")
                                st.toast(f"✅ 已追加 {len(new_objects)} 个物体", icon="📊")

                            mark_dirty("import_append")
                            st.rerun()

                    with col_u2:
                        if st.button("🔄 替换当前场景", key="replace_upload",
                                     use_container_width=True, type="primary"):
                            st.session_state.scene_objects = apply_planned_layout(new_objects)
                            st.session_state.scene_objects = build_relations(
                                st.session_state.scene_objects
                            )
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects

                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                ok = sync_scene_objects(
                                    st.session_state.scene_id,
                                    st.session_state.scene_objects
                                )
                                if ok:
                                    st.toast(f"✅ 已替换为 {len(new_objects)} 个物体（已同步云端）", icon="📊")
                                else:
                                    st.error("❌ 云端同步失败！数据仅保存在本地会话中，刷新后会丢失")
                                    st.caption("💡 请检查：1) Supabase 是否连通 2) scene_id 是否存在 3) scene_objects 表结构")
                                    st.stop()
                            else:
                                st.warning(f"⚠️ 已替换为 {len(new_objects)} 个物体（Supabase 未启用，仅本地）")
                                st.toast(f"✅ 已替换为 {len(new_objects)} 个物体", icon="📊")

                            mark_dirty("import_replace")
                            st.rerun(scope="app")

            except Exception as e:
                st.error(f"❌ 解析失败：{e}")
                import traceback
                with st.expander("查看详细错误"):
                    st.code(traceback.format_exc())

    # --- 方式2：载入场景存档（.json） ---
    with st.expander("💾 载入场景存档（.json）", expanded=False):
        st.caption("💡 恢复**本工具导出**的完整场景（含位置、绑定关系、场景名）")

        uploaded = st.file_uploader(
            "上传场景 JSON",
            type=['json'],
            key="upload_scene",
            label_visibility="collapsed"
        )
        if uploaded is not None:
            try:
                import json as _json
                data = _json.loads(uploaded.read().decode('utf-8'))
                scene_name = data.get('scene_name', '导入场景')
                objects = data.get('objects', [])
                if objects:
                    scene = SceneData(
                        scene_name=scene_name,
                        objects=objects
                    )
                    st.session_state.current_scene = scene
                    st.session_state.scene_objects = objects
                    st.session_state.selected_object_id = None
                    st.session_state.export_html_content = None
                    st.toast(f"✅ 已导入：{scene_name}（{len(objects)} 个对象）", icon="📥")
                    mark_dirty("import_scene_json")
                    st.rerun(scope="app")
                else:
                    st.warning("场景文件为空")
            except Exception as e:
                st.error(f"❌ 导入失败：{e}")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 📤 导出为 JSON =====
    if st.button("📤 导出当前场景为 JSON", key="export_json", use_container_width=True):
        try:
            import json as _json
            scene_dict = {
                'scene_name': get_scene_name_safe(),
                'objects': st.session_state.scene_objects,
                'exported_at': datetime.now().isoformat()
            }
            st.download_button(
                label="⬇️ 下载 JSON",
                data=_json.dumps(scene_dict, ensure_ascii=False, indent=2),
                file_name=f"{get_scene_name_safe().replace(' ', '_')}.json",
                mime="application/json",
                use_container_width=True,
                key="download_json_btn"
            )
        except Exception as e:
            st.error(f"❌ 导出失败：{e}")

    # ===== 🖨️ 打印场景状态 =====
    if st.button(
            "🖨️ 打印场景状态 (Console)",
            key="dev_print_console",
            use_container_width=True,
            help="在浏览器控制台打印所有物体的位置/旋转/缩放，用于调试"
    ):
        st.session_state['_dev_print_console'] = True
        st.toast("✅ 指令已发送，请按 F12 查看浏览器控制台", icon="🖨️")
        st.rerun()

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)



def render_settings_panel(keyword=''):
    """设置面板：多源数据接入（SQLite / MySQL / PostgreSQL / API / InfluxDB）与 MQTT

    🔥 P2 Step1：由原 render_saved_panel 拆分而来（纯搬家）。
    这类基础设施配置原先被塞在"场景库"面板里，属职责错配。
    """
    # ===== 多源数据接入 =====
    st.markdown("**🔌 多源数据接入**")
    with st.expander("配置数据源", expanded=False):
        source_type = st.selectbox(
            "数据源类型",
            ["SQLite", "MySQL", "PostgreSQL", "REST API", "InfluxDB"],
            key="ds_type"
        )

        # ========== SQLite 分支 ==========
        if source_type == "SQLite":
            st.info("💡 SQLite 无需安装服务器，读取本地 .db 文件")

            col_sq1, col_sq2 = st.columns([3, 2])
            with col_sq1:
                sqlite_path = st.text_input(
                    "SQLite 文件路径",
                    value="test_stations.db",
                    key="ds_sqlite_path",
                    help="相对于项目根目录的路径"
                )
            with col_sq2:
                sqlite_table = st.text_input(
                    "表名",
                    value="stations",
                    key="ds_sqlite_table"
                )

            if st.button("🔗 测试连接", key="test_sqlite", use_container_width=True):
                try:
                    import sqlite3
                    if not os.path.exists(sqlite_path):
                        st.error(f"❌ 文件不存在: {sqlite_path}")
                        st.caption("💡 请先运行 create_test_db.py 创建测试数据库")
                    else:
                        conn = sqlite3.connect(sqlite_path)
                        df_db = pd.read_sql(f"SELECT * FROM {sqlite_table} LIMIT 500", conn)
                        conn.close()

                        st.session_state['_sqlite_df_cache'] = df_db
                        st.session_state['_sqlite_table'] = sqlite_table
                        st.success(f"✅ SQLite 连接成功，读取到 {len(df_db)} 条记录")

                        LAT_ALIASES = ['lat', 'latitude', '纬度', 'y']
                        LON_ALIASES = ['lon', 'lng', 'longitude', '经度', 'x']
                        UTIL_ALIASES = ['utilization', 'util', 'occupancy', '利用率']
                        NAME_ALIASES = ['name', 'station_name', '站点名称']
                        ID_ALIASES = ['station_id', 'id', '站点id']

                        def find_col(cols, aliases):
                            cols_lower = {str(c).lower().strip(): c for c in cols}
                            for a in aliases:
                                if a.lower() in cols_lower:
                                    return cols_lower[a.lower()]
                            return None

                        lat_col = find_col(df_db.columns, LAT_ALIASES)
                        lon_col = find_col(df_db.columns, LON_ALIASES)
                        util_col = find_col(df_db.columns, UTIL_ALIASES)
                        name_col = find_col(df_db.columns, NAME_ALIASES)
                        id_col = find_col(df_db.columns, ID_ALIASES)

                        st.session_state['_sqlite_cols'] = {
                            'lat': lat_col, 'lon': lon_col, 'util': util_col,
                            'name': name_col, 'id': id_col
                        }

                        st.markdown(
                            '<div style="background:rgba(26,42,68,0.6);border-radius:8px;'
                            'padding:8px 12px;font-size:12px;color:#8899bb;margin:8px 0;">'
                            '🔍 识别到列名：纬度→<b style="color:#51cf66;">' + str(lat_col or '❌') + '</b> · '
                            '经度→<b style="color:#51cf66;">' + str(lon_col or '❌') + '</b> · '
                            '利用率→<b style="color:#51cf66;">' + str(util_col or '默认0.5') + '</b>'
                            '</div>',
                            unsafe_allow_html=True
                        )

                        st.dataframe(df_db.head(5), use_container_width=True)
                except Exception as e:
                    st.error(f"❌ 连接失败：{e}")

            sqlite_df_cache = st.session_state.get('_sqlite_df_cache')
            sqlite_cols = st.session_state.get('_sqlite_cols')
            if sqlite_df_cache is not None and sqlite_cols is not None:
                lat_col = sqlite_cols.get('lat')
                lon_col = sqlite_cols.get('lon')
                util_col = sqlite_cols.get('util')
                name_col = sqlite_cols.get('name')
                id_col = sqlite_cols.get('id')

                if lat_col and lon_col:
                    col_imp, col_city = st.columns(2)

                    with col_imp:
                        if st.button(
                                "📥 导入为场景对象",
                                key="import_sqlite",
                                use_container_width=True,
                                type="primary"
                        ):
                            df_db = sqlite_df_cache
                            lat_mean = float(df_db[lat_col].mean())
                            lon_mean = float(df_db[lon_col].mean())

                            new_objs = []
                            for _, row in df_db.iterrows():
                                util = float(row[util_col]) if util_col else 0.5
                                util = max(0.05, min(0.95, util))
                                obj_type = 'charger_slow' if util < 0.4 else 'charger_fast'
                                new_objs.append({
                                    'id': str(uuid.uuid4()),
                                    'type': obj_type,
                                    'name': str(row[name_col]) if name_col else f'DB站点_{len(new_objs)}',
                                    'position': {
                                        'x': float(row[lon_col]) - lon_mean,
                                        'y': 0.0,
                                        'z': float(row[lat_col]) - lat_mean
                                    },
                                    'rotation': {'x': 0, 'y': 0, 'z': 0},
                                    'scale': {'x': 1, 'y': 1, 'z': 1},
                                    'bind_station_id': str(row[id_col]) if id_col else '',
                                    'custom_props': {},
                                    'utilization': util
                                })

                            if len(new_objs) > 1:
                                min_x = min(o['position']['x'] for o in new_objs)
                                max_x = max(o['position']['x'] for o in new_objs)
                                min_z = min(o['position']['z'] for o in new_objs)
                                max_z = max(o['position']['z'] for o in new_objs)
                                span_x = max_x - min_x
                                span_z = max_z - min_z

                                if span_x < 2.0 and span_z < 2.0:
                                    cols_n = int(len(new_objs) ** 0.5) + 1
                                    spacing = 3.0
                                    for i, o in enumerate(new_objs):
                                        row_i = i // cols_n
                                        col_i = i % cols_n
                                        o['position']['x'] = (col_i - cols_n / 2) * spacing
                                        o['position']['z'] = (row_i - cols_n / 2) * spacing
                                    st.info(f"📍 坐标过于集中，已网格化排列 {len(new_objs)} 个站点")
                                else:
                                    scale_factor = max(1.0, 15.0 / max(span_x, span_z, 0.1))
                                    if scale_factor > 1.5:
                                        for o in new_objs:
                                            o['position']['x'] *= scale_factor
                                            o['position']['z'] *= scale_factor
                                        st.info(f"📍 坐标跨度过小，已放大 {scale_factor:.1f} 倍间距")

                            st.session_state.scene_objects.extend(new_objs)
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects
                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)

                            st.session_state.pop('_sqlite_df_cache', None)
                            st.session_state.pop('_sqlite_cols', None)

                            st.toast(f"✅ 已导入 {len(new_objs)} 个对象", icon="📥")
                            mark_dirty("import_sqlite")
                            st.rerun(scope="app")

                    with col_city:
                        if st.button(
                                "🏙️ 生成模拟城市",
                                key="gen_mock_city_btn",
                                use_container_width=True,
                                type="secondary"
                        ):
                            st.query_params['action'] = 'mock_city'
                            st.toast("⏳ 正在生成模拟城市路网...", icon="🏙️")
                            st.rerun()
                else:
                    st.warning("⚠️ 未识别到经纬度列，无法转换为场景")

        # ========== MySQL / PostgreSQL 分支 ==========
        if source_type in ["MySQL", "PostgreSQL"]:
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                db_host = st.text_input("主机", value="localhost", key="ds_host")
                db_port = st.text_input("端口", value="3306" if source_type == "MySQL" else "5432", key="ds_port")
            with col_d2:
                db_user = st.text_input("用户名", value="root", key="ds_user")
                db_pass = st.text_input("密码", type="password", key="ds_pass")
            db_name = st.text_input("数据库名", value="charging", key="ds_db")
            db_table = st.text_input("表名", value="stations", key="ds_table")

            if st.button("🔗 测试连接", key="test_db", use_container_width=True):
                try:
                    if source_type == "MySQL":
                        import pymysql
                        conn = pymysql.connect(
                            host=db_host, port=int(db_port),
                            user=db_user, password=db_pass,
                            database=db_name, connect_timeout=5
                        )
                    else:
                        import psycopg2
                        conn = psycopg2.connect(
                            host=db_host, port=int(db_port),
                            user=db_user, password=db_pass,
                            dbname=db_name, connect_timeout=5
                        )
                    st.success(f"✅ {source_type} 连接成功")

                    query = f"SELECT * FROM {db_table} LIMIT 500"
                    df_db = pd.read_sql(query, conn)
                    conn.close()

                    st.session_state['_mysql_df_cache'] = df_db
                    st.write(f"📊 读取到 {len(df_db)} 条记录")
                    st.dataframe(df_db.head(5), use_container_width=True)

                    lat_col = next((c for c in df_db.columns if c.lower() in ['lat', 'latitude']), None)
                    lon_col = next((c for c in df_db.columns if c.lower() in ['lon', 'lng', 'longitude']), None)
                    util_col = next((c for c in df_db.columns if c.lower() in ['utilization', 'util', 'occupancy']), None)
                    st.session_state['_mysql_cols'] = {'lat': lat_col, 'lon': lon_col, 'util': util_col}
                except ImportError as e:
                    st.error(f"❌ 缺少依赖库：{e}")
                    st.caption("MySQL 需 `pip install pymysql`，PostgreSQL 需 `pip install psycopg2-binary`")
                except Exception as e:
                    st.error(f"❌ 连接失败：{e}")

            mysql_df_cache = st.session_state.get('_mysql_df_cache')
            mysql_cols = st.session_state.get('_mysql_cols')
            if mysql_df_cache is not None and mysql_cols is not None:
                lat_col = mysql_cols.get('lat')
                lon_col = mysql_cols.get('lon')
                util_col = mysql_cols.get('util')
                if lat_col and lon_col:
                    if st.button("📥 导入为场景对象", key="import_db", use_container_width=True, type="primary"):
                        df_db = mysql_df_cache
                        lat_mean = float(df_db[lat_col].mean())
                        lon_mean = float(df_db[lon_col].mean())
                        new_objs = []
                        for _, row in df_db.iterrows():
                            util = float(row[util_col]) if util_col else 0.5
                            util = max(0.05, min(0.95, util))
                            obj_type = 'charger_slow' if util < 0.4 else 'charger_fast'
                            new_objs.append({
                                'id': str(uuid.uuid4()),
                                'type': obj_type,
                                'name': f"DB站点_{len(new_objs)}",
                                'position': {
                                    'x': float(row[lon_col]) - lon_mean,
                                    'y': 0.0,
                                    'z': float(row[lat_col]) - lat_mean
                                },
                                'rotation': {'x': 0, 'y': 0, 'z': 0},
                                'scale': {'x': 1, 'y': 1, 'z': 1},
                                'bind_station_id': '',
                                'custom_props': {},
                                'utilization': util
                            })
                        st.session_state.scene_objects.extend(new_objs)
                        if st.session_state.current_scene:
                            st.session_state.current_scene.objects = st.session_state.scene_objects
                        if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                            sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)
                        st.session_state.pop('_mysql_df_cache', None)
                        st.session_state.pop('_mysql_cols', None)
                        st.toast(f"✅ 已导入 {len(new_objs)} 个对象", icon="📥")
                        mark_dirty("import_mysql")
                        st.rerun(scope="app")
                else:
                    st.warning("⚠️ 未识别到经纬度列，无法转换为场景")

        elif source_type == "REST API":
            api_url = st.text_input("API URL", value="https://api.example.com/stations", key="ds_api")
            api_token = st.text_input("Token（可选）", type="password", key="ds_token")

            if st.button("🔗 测试连接", key="test_api", use_container_width=True):
                try:
                    import requests
                    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
                    resp = requests.get(api_url, headers=headers, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        st.success(f"✅ API 连接成功，返回 {len(data)} 条记录")
                        st.json(data[:2] if isinstance(data, list) else data)
                    else:
                        st.error(f"❌ HTTP {resp.status_code}")
                except Exception as e:
                    st.error(f"❌ 请求失败：{e}")

        # ========== InfluxDB 3 Cloud Serverless 分支 ==========
        if source_type == "InfluxDB":
            st.info("💡 InfluxDB 3 Cloud Serverless · 时序数据库，适合存储充电桩实时利用率、功率等数据")

            col_if1, col_if2 = st.columns(2)
            with col_if1:
                influx_url = st.text_input(
                    "InfluxDB Host",
                    value="https://us-east-1-1.aws.cloud2.influxdata.com",
                    key="ds_influx_url",
                    help="InfluxDB Cloud Serverless 的区域 URL，去掉末尾斜杠"
                )
                influx_database = st.text_input(
                    "Database（数据库）",
                    value="charging",
                    key="ds_influx_db",
                    help="InfluxDB 3 中 database 就是 bucket 名称"
                )
            with col_if2:
                influx_token = st.text_input(
                    "API Token",
                    type="password",
                    key="ds_influx_token",
                    help="在 InfluxDB Cloud 控制台 → Load Data → API Tokens 生成"
                )
                influx_measurement = st.text_input(
                    "Measurement（测量名称）",
                    value="charger_realtime",
                    key="ds_influx_measurement",
                    help="时序数据的 measurement 名称，如 charger_realtime"
                )

            col_r1, col_r2 = st.columns(2)
            with col_r1:
                time_range = st.selectbox(
                    "查询时间范围",
                    ["最近 1 小时", "最近 24 小时", "最近 7 天", "最近 30 天"],
                    key="ds_influx_range"
                )
                range_map = {
                    "最近 1 小时": "1 hour",
                    "最近 24 小时": "24 hours",
                    "最近 7 天": "7 days",
                    "最近 30 天": "30 days",
                }
                sql_time_range = range_map[time_range]
            with col_r2:
                row_limit = st.number_input(
                    "最大返回行数",
                    min_value=100, max_value=10000, value=500, step=100,
                    key="ds_influx_limit"
                )

            if st.button("🔗 测试连接", key="test_influx", use_container_width=True):
                try:
                    from influxdb3 import InfluxDBClient3

                    with st.spinner("正在连接 InfluxDB 3 Cloud..."):
                        client = InfluxDBClient3(
                            host=influx_url.rstrip('/'),
                            token=influx_token,
                            database=influx_database
                        )

                        query = (
                            'SELECT * FROM "' + influx_measurement + '" '
                            "WHERE time >= now() - INTERVAL '" + sql_time_range + "' "
                            "ORDER BY time DESC LIMIT " + str(int(row_limit))
                        )

                        df_influx = client.query(query=query, language="sql").to_pandas()
                        client.close()

                        if df_influx is None or df_influx.empty:
                            st.warning("⚠️ 查询无数据，请检查 measurement 名称、database 或时间范围")
                            return

                        st.session_state['_influx_df_cache'] = df_influx
                        st.success(f"✅ InfluxDB 3 连接成功，读取到 {len(df_influx)} 条记录")

                        all_cols = list(df_influx.columns)
                        col_preview = ', '.join([str(c) for c in all_cols[:15]])
                        more_hint = ' ...' if len(all_cols) > 15 else ''
                        st.markdown(
                            '<div style="background:rgba(26,42,68,0.6);border-radius:8px;'
                            'padding:8px 12px;font-size:12px;color:#8899bb;margin:8px 0;">'
                            '🔍 识别到列名：<b style="color:#88ccff;">' + col_preview + '</b>'
                            + more_hint +
                            '</div>',
                            unsafe_allow_html=True
                        )

                        LAT_ALIASES = ['lat', 'latitude', '纬度', 'y']
                        LON_ALIASES = ['lon', 'lng', 'longitude', '经度', 'x']
                        UTIL_ALIASES = ['utilization', 'util', 'occupancy', '利用率', 'load', 'usage']
                        NAME_ALIASES = ['name', 'station_name', '站点名称', 'station', 'title']
                        ID_ALIASES = ['station_id', 'id', 'stationid', 'station', 'device_id', 'device']
                        POWER_ALIASES = ['power', '功率', 'kw']
                        TIME_ALIASES = ['time', '_time', 'timestamp', '时间']

                        def find_col_influx(cols, aliases):
                            cols_lower = {str(c).lower().strip(): c for c in cols}
                            for a in aliases:
                                if a.lower() in cols_lower:
                                    return cols_lower[a.lower()]
                            return None

                        lat_col = find_col_influx(df_influx.columns, LAT_ALIASES)
                        lon_col = find_col_influx(df_influx.columns, LON_ALIASES)
                        util_col = find_col_influx(df_influx.columns, UTIL_ALIASES)
                        name_col = find_col_influx(df_influx.columns, NAME_ALIASES)
                        id_col = find_col_influx(df_influx.columns, ID_ALIASES)
                        power_col = find_col_influx(df_influx.columns, POWER_ALIASES)
                        time_col = find_col_influx(df_influx.columns, TIME_ALIASES)

                        tag_cols = [c for c in df_influx.columns
                                    if not str(c).startswith('_') and df_influx[c].dtype == 'object']
                        if not id_col and tag_cols:
                            id_col = tag_cols[0]
                            st.caption(f"ℹ️ 自动使用字段 `{id_col}` 作为站点 ID")

                        st.session_state['_influx_cols'] = {
                            'lat': lat_col, 'lon': lon_col, 'util': util_col,
                            'name': name_col, 'id': id_col, 'power': power_col,
                            'time': time_col
                        }

                        st.markdown(
                            '<div style="background:rgba(26,42,68,0.6);border-radius:8px;'
                            'padding:8px 12px;font-size:12px;color:#8899bb;margin:8px 0;">'
                            '🎯 字段映射：<br>'
                            '• 站点ID → <b style="color:#51cf66;">' + str(id_col or '❌ 未找到') + '</b><br>'
                            '• 利用率 → <b style="color:#51cf66;">' + str(util_col or '默认 0.5') + '</b><br>'
                            '• 纬度 → <b style="color:#51cf66;">' + str(lat_col or '（可选，用网格布局）') + '</b><br>'
                            '• 经度 → <b style="color:#51cf66;">' + str(lon_col or '（可选，用网格布局）') + '</b><br>'
                            '• 功率 → <b style="color:#51cf66;">' + str(power_col or '按类型默认') + '</b><br>'
                            '• 时间 → <b style="color:#51cf66;">' + str(time_col or '未识别') + '</b>'
                            '</div>',
                            unsafe_allow_html=True
                        )

                        preview_cols = [c for c in
                                        [id_col, name_col, util_col, power_col, lat_col, lon_col, time_col]
                                        if c and c in df_influx.columns]
                        if preview_cols:
                            st.markdown("**📋 原始数据预览（前5行）**")
                            st.dataframe(df_influx[preview_cols].head(5), use_container_width=True)

                        st.markdown("**📊 数据聚合**")
                        st.caption("时序数据按站点聚合，取每个站点的最新利用率")

                        if id_col and util_col:
                            df_sorted = df_influx.sort_values(time_col, ascending=False) if time_col else df_influx
                            agg_df = df_sorted.groupby(id_col, as_index=False).first()

                            keep_cols = [id_col]
                            if name_col and name_col in agg_df.columns:
                                keep_cols.append(name_col)
                            if util_col in agg_df.columns:
                                keep_cols.append(util_col)
                            if power_col and power_col in agg_df.columns:
                                keep_cols.append(power_col)
                            if lat_col and lat_col in agg_df.columns:
                                keep_cols.append(lat_col)
                            if lon_col and lon_col in agg_df.columns:
                                keep_cols.append(lon_col)

                            agg_df = agg_df[keep_cols]

                            st.success(f"✅ 聚合后：{len(agg_df)} 个站点")
                            st.dataframe(agg_df.head(5), use_container_width=True)

                            st.session_state['_influx_agg_df'] = agg_df
                        else:
                            st.warning("⚠️ 缺少站点 ID 或利用率字段，无法聚合")
                            st.session_state['_influx_agg_df'] = df_influx

                except ImportError:
                    st.error("❌ 缺少依赖库，请运行：`pip install influxdb3-python`")
                except Exception as e:
                    st.error(f"❌ 连接失败：{e}")
                    st.caption("💡 常见原因：")
                    st.caption("  • Host 地址错误（去掉末尾斜杠）")
                    st.caption("  • Token 无效或权限不足")
                    st.caption("  • Database 名称错误")
                    st.caption("  • Measurement 不存在")
                    st.caption("  • SQL 语法错误（字段名需用双引号）")

            influx_agg_df = st.session_state.get('_influx_agg_df')
            influx_cols = st.session_state.get('_influx_cols')

            if influx_agg_df is not None and influx_cols is not None and not influx_agg_df.empty:
                id_col = influx_cols.get('id')
                util_col = influx_cols.get('util')
                lat_col = influx_cols.get('lat')
                lon_col = influx_cols.get('lon')
                power_col = influx_cols.get('power')
                name_col = influx_cols.get('name')

                st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

                col_imp, col_clean = st.columns(2)

                with col_imp:
                    if st.button(
                            "📥 导入为场景对象",
                            key="import_influx",
                            use_container_width=True,
                            type="primary"
                    ):
                        try:
                            if lat_col and lon_col and lat_col in influx_agg_df.columns:
                                lat_mean = float(influx_agg_df[lat_col].mean())
                                lon_mean = float(influx_agg_df[lon_col].mean())
                                use_geo = True
                            else:
                                use_geo = False

                            new_objs = []
                            for idx, row in influx_agg_df.iterrows():
                                try:
                                    util = float(row[util_col]) if util_col else 0.5
                                except:
                                    util = 0.5
                                util = max(0.05, min(0.95, util))

                                power = None
                                if power_col and power_col in influx_agg_df.columns:
                                    try:
                                        power = float(row[power_col])
                                    except:
                                        power = None

                                if power:
                                    if power >= 150:
                                        obj_type = 'charger_super'
                                    elif power >= 60:
                                        obj_type = 'charger_fast'
                                    else:
                                        obj_type = 'charger_slow'
                                else:
                                    obj_type = 'charger_slow' if util < 0.4 else 'charger_fast'

                                if use_geo:
                                    pos_x = float(row[lon_col]) - lon_mean
                                    pos_z = float(row[lat_col]) - lat_mean
                                else:
                                    cols_n = int(len(influx_agg_df) ** 0.5) + 1
                                    spacing = 3.0
                                    pos_x = (idx % cols_n - cols_n / 2) * spacing
                                    pos_z = (idx // cols_n - cols_n / 2) * spacing

                                if name_col and name_col in influx_agg_df.columns:
                                    station_name = str(row[name_col])
                                elif id_col:
                                    station_name = f"站点_{row[id_col]}"
                                else:
                                    station_name = f"Influx站点_{idx}"

                                station_id = str(row[id_col]) if id_col else ''

                                new_objs.append({
                                    'id': str(uuid.uuid4()),
                                    'type': obj_type,
                                    'name': station_name,
                                    'position': {'x': pos_x, 'y': 0.0, 'z': pos_z},
                                    'rotation': {'x': 0, 'y': 0, 'z': 0},
                                    'scale': {'x': 1, 'y': 1, 'z': 1},
                                    'bind_station_id': station_id,
                                    'custom_props': {
                                        'power': power if power else 60,
                                        'source': 'InfluxDB3',
                                        'measurement': influx_measurement,
                                    },
                                    'utilization': util
                                })

                            if use_geo and len(new_objs) > 1:
                                xs = [o['position']['x'] for o in new_objs]
                                zs = [o['position']['z'] for o in new_objs]
                                span_x = max(xs) - min(xs)
                                span_z = max(zs) - min(zs)

                                if span_x < 2.0 and span_z < 2.0:
                                    cols_n = int(len(new_objs) ** 0.5) + 1
                                    spacing = 3.0
                                    for i, o in enumerate(new_objs):
                                        o['position']['x'] = (i % cols_n - cols_n / 2) * spacing
                                        o['position']['z'] = (i // cols_n - cols_n / 2) * spacing
                                    st.info(f"📍 坐标过于集中，已网格化排列 {len(new_objs)} 个站点")
                                else:
                                    scale_factor = max(1.0, 15.0 / max(span_x, span_z, 0.1))
                                    if scale_factor > 1.5:
                                        for o in new_objs:
                                            o['position']['x'] *= scale_factor
                                            o['position']['z'] *= scale_factor
                                        st.info(f"📍 坐标跨度过小，已放大 {scale_factor:.1f} 倍间距")

                            st.session_state.scene_objects.extend(new_objs)
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects

                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)

                            st.session_state.pop('_influx_df_cache', None)
                            st.session_state.pop('_influx_agg_df', None)
                            st.session_state.pop('_influx_cols', None)

                            st.toast(f"✅ 已从 InfluxDB 3 导入 {len(new_objs)} 个站点", icon="📥")
                            mark_dirty("import_influxdb")
                            st.rerun(scope="app")

                        except Exception as e:
                            st.error(f"❌ 导入失败：{e}")
                            import traceback
                            traceback.print_exc()

                with col_clean:
                    if st.button(
                            "🗑️ 清除查询缓存",
                            key="clear_influx_cache",
                            use_container_width=True
                    ):
                        st.session_state.pop('_influx_df_cache', None)
                        st.session_state.pop('_influx_agg_df', None)
                        st.session_state.pop('_influx_cols', None)
                        st.toast("已清除 InfluxDB 查询缓存", icon="🗑️")
                        st.rerun()

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ============================================================
    # 🔥 工业协议接入（MQTT 实时流）
    # ============================================================
    st.markdown("**📡 工业协议接入（MQTT 实时流）**")

    with st.expander("配置 MQTT 连接", expanded=False):
        st.caption("💡 连接 MQTT Broker 订阅充电桩实时上报数据，自动更新 3D 场景")

        if not MQTT_AVAILABLE:
            st.error("❌ 请先安装：`pip install paho-mqtt`")
        else:
            mqtt_client = get_mqtt_client()

            col_m1, col_m2 = st.columns(2)
            with col_m1:
                mqtt_broker = st.text_input(
                    "Broker 地址",
                    value=st.session_state.get('mqtt_broker', 'broker.emqx.io'),
                    key="mqtt_broker_input",
                    help="公共测试：broker.emqx.io / test.mosquitto.org"
                )
                mqtt_port = st.number_input(
                    "端口", value=1883, min_value=1, max_value=65535,
                    key="mqtt_port_input"
                )
            with col_m2:
                mqtt_topic = st.text_input(
                    "订阅主题",
                    value=st.session_state.get('mqtt_topic', 'charger/+/status'),
                    key="mqtt_topic_input",
                    help="+ 是通配符，charger/+/status 匹配所有站点"
                )
                mqtt_user = st.text_input(
                    "用户名（可选）", value="",
                    key="mqtt_user_input"
                )

            # 状态显示
            if mqtt_client.connected:
                st.success(
                    f"✅ 已连接 `{mqtt_client.broker}` · "
                    f"已接收 **{mqtt_client.message_count}** 条消息 · "
                    f"最近更新：{mqtt_client.last_message_time or '--'}"
                )
            else:
                st.info("🔌 未连接")

            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if not mqtt_client.connected:
                    if st.button("🔗 连接 MQTT", key="mqtt_connect_btn",
                                 use_container_width=True, type="primary"):
                        with st.spinner("正在连接 MQTT Broker..."):
                            ok = mqtt_client.connect(
                                mqtt_broker, int(mqtt_port), mqtt_topic,
                                username=mqtt_user or None
                            )
                            if ok:
                                st.session_state.mqtt_broker = mqtt_broker
                                st.session_state.mqtt_topic = mqtt_topic
                                st.toast("✅ MQTT 已连接", icon="📡")
                                st.rerun()
                            else:
                                err_msg = mqtt_client.errors[-1] if mqtt_client.errors else '未知错误'
                                st.error(f"❌ 连接失败：{err_msg}")
                else:
                    if st.button("🔌 断开 MQTT", key="mqtt_disconnect_btn",
                                 use_container_width=True):
                        mqtt_client.disconnect()
                        st.toast("已断开 MQTT", icon="🔌")
                        st.rerun()

            with col_b2:
                if st.button("🔄 刷新状态", key="mqtt_refresh_btn",
                             use_container_width=True):
                    st.rerun()

            # 最近消息预览
            if mqtt_client.last_message:
                st.markdown("**📨 最近一条消息**")
                st.json(mqtt_client.last_message)

            # ============================================================
            # 🎯 三个方案整合：设备管理 + 自动绑定
            # ============================================================
            if mqtt_client.connected and mqtt_client.active_devices:
                st.markdown("---")
                st.markdown(f"**📟 活跃设备清单（{len(mqtt_client.active_devices)} 个）**")
                st.caption("MQTT 上报的设备，可通过以下三种方式接入场景")

                # ============ 【方案B】设备列表 + 单个绑定 ============
                st.markdown("##### 🔗 逐个绑定")
                device_items = list(mqtt_client.active_devices.items())[:8]
                for sid, info in device_items:
                    col_d1, col_d2, col_d3 = st.columns([3, 2, 2])

                    with col_d1:
                        util = info['utilization']
                        util_color = '#ff6b6b' if util > 0.7 else ('#fcc419' if util > 0.4 else '#51cf66')
                        st.markdown(
                            f"**`{sid}`**  "
                            f"<span style='color:{util_color};font-weight:700'>{util:.0%}</span>  "
                            f"<span style='color:#667;font-size:11px'>{info['last_seen']}</span>",
                            unsafe_allow_html=True
                        )

                    with col_d2:
                        matched = [o for o in st.session_state.scene_objects
                                   if o.get('bind_station_id') == sid
                                   and o.get('type', '').startswith('charger')]
                        if matched:
                            st.caption(f"✅ 已绑定 {len(matched)} 个")
                        else:
                            st.caption("❌ 未绑定")

                    with col_d3:
                        if st.button("🔗 绑定", key=f"bind_mqtt_{sid}",
                                     use_container_width=True):
                            unbound = [o for o in st.session_state.scene_objects
                                       if o.get('type', '').startswith('charger')
                                       and not o.get('bind_station_id')]
                            if unbound:
                                unbound[0]['bind_station_id'] = sid
                                unbound[0]['utilization'] = info['utilization']
                                if st.session_state.current_scene:
                                    for i, so in enumerate(st.session_state.current_scene.objects):
                                        if so.get('id') == unbound[0]['id']:
                                            st.session_state.current_scene.objects[i] = unbound[0]
                                            break
                                if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                    sync_scene_objects(
                                        st.session_state.scene_id,
                                        st.session_state.scene_objects
                                    )
                                st.toast(f"✅ 已绑定 {sid}", icon="🔗")
                                st.rerun()
                            else:
                                st.warning("⚠️ 场景中没有未绑定的充电桩")

                st.markdown("---")

                # ============ 【方案A + C】快速接入 ============
                st.markdown("##### ⚡ 快速接入")

                col_q1, col_q2 = st.columns(2)

                # 【方案A】一键绑定
                with col_q1:
                    if st.button("🚀 一键绑定全部设备",
                                 key="bulk_bind_all",
                                 use_container_width=True,
                                 type="primary",
                                 help="把 MQTT 上报的设备批量绑到未绑定的充电桩上"):
                        unbound_chargers = [o for o in st.session_state.scene_objects
                                            if o.get('type', '').startswith('charger')
                                            and not o.get('bind_station_id')]
                        device_ids = list(mqtt_client.active_devices.keys())

                        if not unbound_chargers:
                            st.warning("⚠️ 场景中没有未绑定的充电桩")
                        elif not device_ids:
                            st.warning("⚠️ MQTT 尚未上报任何设备")
                        else:
                            bind_count = 0
                            for i, charger in enumerate(unbound_chargers):
                                if i >= len(device_ids):
                                    break
                                sid = device_ids[i]
                                charger['bind_station_id'] = sid
                                charger['utilization'] = mqtt_client.active_devices[sid]['utilization']
                                bind_count += 1

                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects
                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                sync_scene_objects(
                                    st.session_state.scene_id,
                                    st.session_state.scene_objects
                                )

                            st.toast(f"✅ 已批量绑定 {bind_count} 个充电桩", icon="🚀")
                            st.rerun()

                # 【方案C】自动创建
                with col_q2:
                    if st.button("🤖 自动创建充电桩",
                                 key="auto_create_chargers",
                                 use_container_width=True,
                                 help="为每个 MQTT 设备新建一个充电桩，自动绑定"):
                        device_ids = list(mqtt_client.active_devices.keys())

                        new_chargers = []
                        for i, sid in enumerate(device_ids):
                            info = mqtt_client.active_devices[sid]
                            util = info['utilization']
                            p = info.get('power', 60)

                            if p >= 150:
                                ctype = 'charger_super'
                            elif p >= 60:
                                ctype = 'charger_fast'
                            else:
                                ctype = 'charger_slow'

                            cols = 4
                            row = i // cols
                            col = i % cols
                            new_chargers.append({
                                'id': str(uuid.uuid4()),
                                'type': ctype,
                                'name': f"MQTT设备_{sid}",
                                'position': {
                                    'x': (col - cols / 2) * 3.0,
                                    'y': 0.0,
                                    'z': (row - 2) * 3.0
                                },
                                'rotation': {'x': 0, 'y': 0, 'z': 0},
                                'scale': {'x': 1, 'y': 1, 'z': 1},
                                'bind_station_id': sid,
                                'custom_props': {
                                    'power': p,
                                    'source': 'MQTT',
                                },
                                'utilization': util
                            })

                        if new_chargers:
                            st.session_state.scene_objects.extend(new_chargers)
                            if st.session_state.current_scene:
                                st.session_state.current_scene.objects = st.session_state.scene_objects
                            if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                                sync_scene_objects(
                                    st.session_state.scene_id,
                                    st.session_state.scene_objects
                                )
                            st.toast(f"✅ 已创建 {len(new_chargers)} 个充电桩", icon="🤖")
                            st.rerun()
                        else:
                            st.warning("⚠️ MQTT 尚未上报任何设备")

                # 帮助信息
                with st.expander("💡 三种方式怎么选？", expanded=False):
                    st.markdown("""
                    - **🔗 单个绑定**：在已有场景中，精确控制每个设备对应的充电桩
                    - **🚀 一键绑定**：快速把 8 个 MQTT 设备接入现有场景（自动找空位）
                    - **🤖 自动创建**：场景是空的，从零开始为每个 MQTT 设备建一个充电桩
                    """)

                # ============================================================
                # 🔥 下行：孪生 → 设备 反向控制（闭环）
                # ------------------------------------------------------------
                # 只订阅不发布的孪生体是"远程监控"；这里补上下行通道后，
                # 孪生侧才能真正作用于物理世界：指令 → 执行 → 回执 → 状态收敛。
                # 设备端由 mock_mqtt_publisher.py 扮演（软件模拟，非真实固件）。
                # ============================================================
                st.markdown("---")
                st.markdown("**🎮 反向控制（孪生 → 设备）**")
                st.caption("下发指令 → 设备执行 → 回执确认 → 状态收敛")

                _cmd_ids = list(mqtt_client.active_devices.keys())
                if not _cmd_ids:
                    st.info("ℹ️ 暂无可控设备。先运行 `python mock_mqtt_publisher.py` 并连接 MQTT。")
                else:
                    from core.mqtt_client import COMMAND_SPECS
                    _c1, _c2 = st.columns([1, 1])
                    with _c1:
                        _target = st.selectbox("目标设备", _cmd_ids, key="mqtt_cmd_target")
                    with _c2:
                        _cmd_key = st.selectbox(
                            "指令",
                            list(COMMAND_SPECS.keys()),
                            format_func=lambda k: COMMAND_SPECS[k]["name"],
                            key="mqtt_cmd_kind",
                        )
                    _spec = COMMAND_SPECS[_cmd_key]
                    st.caption(f"💡 {_spec['desc']}")

                    _param_val = None
                    if _spec.get("type") == "bool":
                        _param_val = st.checkbox("启用", value=bool(_spec["default"]),
                                                 key="mqtt_cmd_bool")
                    else:
                        _param_val = st.slider(
                            f"{_spec['param']}（{_spec['unit']}）",
                            int(_spec["min"]), int(_spec["max"]), int(_spec["default"]),
                            key="mqtt_cmd_num",
                        )

                    if st.button("📤 下发指令", key="mqtt_cmd_send", use_container_width=True,
                                 type="primary"):
                        _params = ({_spec["param"]: _param_val} if _spec.get("type") == "bool"
                                   else {_spec["param"]: _param_val})
                        _res = mqtt_client.publish_command(_target, _cmd_key, _params)
                        if _res["ok"]:
                            st.success(f"✅ 已下发（cmd_id={_res['cmd_id']}）")
                            # 🔥 关键：等待回执才算闭环完成。
                            #    只报"已下发"而不等回执，就是伪闭环。
                            with st.spinner("等待设备回执…"):
                                _reply = mqtt_client.wait_for_reply(_res["cmd_id"], timeout=6.0)
                            if _reply:
                                mqtt_client.mark_acked(_res["cmd_id"])
                                if _reply["ok"]:
                                    st.success(f"📥 设备已执行：{_reply['detail']}")
                                else:
                                    st.warning(f"📥 设备拒绝执行：{_reply['detail']}")
                                if _reply.get("state"):
                                    st.json(_reply["state"])
                            else:
                                st.error("⏱️ 未收到回执 —— 指令可能已发出但设备无响应。"
                                         "这正是闭环里最需要监控的一环。")
                        else:
                            st.error(f"❌ 下发失败：{_res['detail']}")

                    # 指令留痕：发过什么、回没回
                    _log = mqtt_client.get_command_log()
                    if _log:
                        with st.expander(f"📜 指令记录（{len(_log)} 条）", expanded=False):
                            for _r in reversed(_log[-8:]):
                                _mark = "✅" if _r.get("acked") else "⏳"
                                st.markdown(
                                    f"{_mark} `{_r['sent_at']}` **{_r['station_id']}** "
                                    f"{_r['cmd']} {_r['params']} · cmd_id=`{_r['cmd_id']}`"
                                )
                        _reps = mqtt_client.get_replies()
                        if _reps:
                            st.caption(f"已收到 {len(_reps)} 条回执，最近："
                                       f"{_reps[-1]['received_at']} {_reps[-1]['detail']}")

            # 使用说明（字符串拼接，避免三引号冲突）
            with st.expander("📖 如何测试？", expanded=False):
                st.markdown(
                    "**步骤 1**：在本机运行模拟器脚本（终端）：\n\n"
                    "```bash\n"
                    "python mock_mqtt_publisher.py\n"
                    "```\n\n"
                    "**步骤 2**：在上方点击「🔗 连接 MQTT」\n\n"
                    "**步骤 3**：观察 3D 场景中的充电桩颜色随数据变化\n\n"
                    "**公共 Broker 说明**：\n"
                    "- `broker.emqx.io` —— EMQ 提供的免费公共 Broker\n"
                    "- `test.mosquitto.org` —— Mosquitto 官方测试 Broker"
                )

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)



def render_emission_panel(keyword=''):
    with st.expander("📖 参数说明与数据来源", expanded=False):
        st.markdown("""
        **✅ 权威系数（不可调）**
        - 燃油替代减排因子：**0.6 kg CO₂/度**（国家发改委）
        - 汽油碳排放因子：**2.3 kg CO₂/L**（IPCC）
        - 树木年碳汇：**21.77 kg CO₂/棵·年**（联合国粮农组织）
        - 标准煤换算：**0.3 kg 标煤/度**（GB/T 2589）

        **⚠️ 估算参数（可调）**
        - 快充日均时长：**4 小时**（行业经验值）
        - 慢充日均时长：**8 小时**（行业经验值）
        - 储能配比：**20%**（吉林省"十五五"政策鼓励）
        - 储能循环：**1.5 次/天**（行业经验值）

        **📊 精度说明**：当前基于场景模拟数据，误差约 ±30%。
        接入真实运营数据后，可达到碳交易核算精度要求。
        """)
    """碳减排面板：绿色评级 + 详细指标 + 政策对标"""
    objects = st.session_state.scene_objects
    chargers = [o for o in objects if o.get('type', '').startswith('charger')]

    panel_header("🌿", "碳减排分析", count=f"{len(chargers)} 个充电桩")

    if not chargers:
        empty_state("🌿", "暂无充电桩", "添加充电桩后即可查看碳减排贡献")
        return

    # ===== 参数调节区 =====
    with st.expander("⚙️ 参数调节（调整后实时重算）", expanded=False):
        st.caption("标【权威】的系数来自国家发改委/IPCC，不可调。以下为行业估算参数。")

        col_p1, col_p2 = st.columns(2)
        with col_p1:
            fast_hours = st.slider(
                "⚡ 快充日均有效时长 (h)",
                min_value=0.5, max_value=6.0,
                value=float(st.session_state.get('carbon_fast_hours', 2.0)),
                step=0.5,
                key="carbon_fast_hours_slider",
                help="充电桩每天平均实际充电的时长。保守值1-2小时，繁忙站点可达4小时以上。"
            )
            st.session_state.carbon_fast_hours = fast_hours

            slow_hours = st.slider(
                "🔋 慢充日均有效时长 (h)",
                min_value=1.0, max_value=12.0,
                value=float(st.session_state.get('carbon_slow_hours', 4.0)),
                step=0.5,
                key="carbon_slow_hours_slider",
                help="慢充桩每天平均实际充电时长。"
            )
            st.session_state.carbon_slow_hours = slow_hours

        with col_p2:
            storage_ratio = st.slider(
                "🔋 储能配比 (%)",
                min_value=0, max_value=50,
                value=int(st.session_state.get('carbon_storage_ratio', 0.2) * 100),
                step=5,
                key="carbon_storage_ratio_slider",
                help="吉林省'十五五'规划鼓励充换电站建设用户侧储能。参考值20%。"
            )
            st.session_state.carbon_storage_ratio = storage_ratio / 100.0

            storage_cycles = st.slider(
                "🔄 储能日均循环 (次)",
                min_value=0.5, max_value=3.0,
                value=float(st.session_state.get('carbon_storage_cycles', 1.5)),
                step=0.1,
                key="carbon_storage_cycles_slider",
                help="储能系统每天充放电循环次数。行业经验值1-2次。"
            )
            st.session_state.carbon_storage_cycles = storage_cycles

        # 快捷预设
        st.markdown("**🚀 快捷预设**")
        col_pre1, col_pre2, col_pre3 = st.columns(3)
        with col_pre1:
            if st.button("保守估算", key="preset_conservative", use_container_width=True):
                st.session_state.carbon_fast_hours = 1.0
                st.session_state.carbon_slow_hours = 3.0
                st.session_state.carbon_storage_ratio = 0.15
                st.session_state.carbon_storage_cycles = 1.0
                st.rerun()
        with col_pre2:
            if st.button("行业基准", key="preset_baseline", use_container_width=True):
                st.session_state.carbon_fast_hours = 2.0
                st.session_state.carbon_slow_hours = 4.0
                st.session_state.carbon_storage_ratio = 0.20
                st.session_state.carbon_storage_cycles = 1.5
                st.rerun()
        with col_pre3:
            if st.button("理想工况", key="preset_ideal", use_container_width=True):
                st.session_state.carbon_fast_hours = 4.0
                st.session_state.carbon_slow_hours = 8.0
                st.session_state.carbon_storage_ratio = 0.30
                st.session_state.carbon_storage_cycles = 2.0
                st.rerun()

    # ===== 计算 =====
    emission = calculate_emission(objects)
    storage = calculate_storage_emission(objects)
    avg_util = emission['avg_utilization']
    rating = get_carbon_rating(avg_util)
    total_co2 = emission['co2_reduction_tons'] + storage['storage_co2_tons']

    # ===== 绿色评级卡片 =====
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:16px;padding:20px;
                background:linear-gradient(135deg,rgba(81,207,102,0.2),rgba(136,204,255,0.1));
                border-radius:16px;border:1px solid rgba(81,207,102,0.3);margin-bottom:16px;">
        <div style="font-size:48px;font-weight:900;color:{rating['color']};
                    width:90px;height:90px;display:flex;align-items:center;justify-content:center;
                    background:rgba(81,207,102,0.15);border-radius:50%;
                    border:3px solid {rating['color']};
                    box-shadow:0 0 30px {rating['color']}44;">
            {rating['grade']}
        </div>
        <div style="flex:1;">
            <div style="color:{rating['color']};font-weight:800;font-size:18px;">
                {rating['label']}
            </div>
            <div style="color:#8899bb;font-size:13px;margin-top:6px;">
                平均利用率 {avg_util:.1%}
            </div>
            <div style="color:#556;font-size:11px;margin-top:4px;">
                基于 {len(chargers)} 个充电桩的综合评定
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ===== 核心指标 =====
    st.markdown("**📊 核心减排指标**")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("综合碳减排", f"{total_co2} 吨", delta="CO₂/年")
        st.metric("等效植树", f"{emission['tree_equivalent']} 棵", delta="碳汇")
    with col2:
        st.metric("减少里程", f"{emission['car_km_reduction']} 万km", delta="燃油车替代")
        st.metric("节约标煤", f"{emission['coal_saved_tons']} 吨", delta="能源替代")

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 详细数据 =====
    st.markdown("**📈 详细数据**")
    st.markdown(f"""
    <div style="background:rgba(16,22,40,0.6);border-radius:12px;padding:14px;
                border:1px solid rgba(74,144,217,0.15);">
        <div style="font-size:12px;color:#8899bb;line-height:2.2;">
            <div style="display:flex;justify-content:space-between;">
                <span>⚡ 年充电总量</span>
                <b style="color:#88ccff;">{emission['total_kwh']:,.1f} 度</b>
            </div>
            <div style="display:flex;justify-content:space-between;">
                <span>🚗 替代燃油减排</span>
                <b style="color:#51cf66;">{emission['co2_reduction_tons']} 吨 CO₂</b>
            </div>
            <div style="display:flex;justify-content:space-between;">
                <span>🔋 配套储能减排</span>
                <b style="color:#51cf66;">{storage['storage_co2_tons']} 吨 CO₂</b>
            </div>
            <div style="display:flex;justify-content:space-between;">
                <span>🔋 储能配套容量</span>
                <b style="color:#88ccff;">{storage['storage_capacity_kw']} kW</b>
            </div>
            <div style="display:flex;justify-content:space-between;">
                <span>⛽ 等效减少汽油</span>
                <b style="color:#fcc419;">{emission['oil_saved_liters']:,.1f} L</b>
            </div>
            <div style="display:flex;justify-content:space-between;">
                <span>⚡ 充电桩总功率</span>
                <b style="color:#88ccff;">{emission['total_power']} kW</b>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 政策对标 =====
    st.markdown("**🏛️ 政策对标**")
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,rgba(155,89,182,0.1),rgba(74,144,217,0.08));
                border-radius:12px;padding:14px;
                border:1px solid rgba(155,89,182,0.2);">
        <div style="color:#c39bd3;font-weight:700;font-size:13px;margin-bottom:10px;">
            📋 吉林省"十五五"规划对接
        </div>
        <div style="font-size:12px;color:#c8d6e5;line-height:1.9;">
            ✅ <b>新型储能300万千瓦</b> —— 本场景配套储能 {storage['storage_capacity_kw']} kW<br>
            ✅ <b>充换电站配储</b> —— 已按总功率 20% 估算配置<br>
            ✅ <b>绿色能源产业高地</b> —— 年碳减排 {total_co2} 吨<br>
            ✅ <b>汽车产业新能源转型</b> —— 等效替代燃油车 {emission['car_km_reduction']} 万km<br>
            ✅ <b>生态强省建设</b> —— 等效植树 {emission['tree_equivalent']} 棵
        </div>
        <div style="font-size:10px;color:#556;margin-top:10px;
                    border-top:1px solid rgba(155,89,182,0.15);padding-top:8px;">
            数据来源：吉林省"十五五"规划纲要 · 国家发改委碳排放因子
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)

    # ===== 可视化图表 =====
    st.markdown("**📊 减排构成**")
    try:
        import plotly.graph_objects as go

        fig = go.Figure(data=[
            go.Bar(
                x=['替代燃油', '配套储能'],
                y=[emission['co2_reduction_tons'], storage['storage_co2_tons']],
                marker=dict(
                    color=['#51cf66', '#88ccff'],
                    line=dict(color='rgba(255,255,255,0.2)', width=1)
                ),
                text=[f"{emission['co2_reduction_tons']}吨", f"{storage['storage_co2_tons']}吨"],
                textposition='outside',
                textfont=dict(color='#eef2ff', size=12)
            )
        ])
        fig.update_layout(
            height=200,
            margin=dict(l=10, r=10, t=20, b=20),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(showgrid=False, tickfont=dict(color='#8899bb', size=11)),
            yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)',
                       tickfont=dict(color='#667', size=10)),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
    except Exception as e:
        st.caption(f"图表加载失败：{e}")

    # ===== 导出 =====
    st.markdown("<hr style='margin: 8px 0;'>", unsafe_allow_html=True)
    if st.button("📥 导出碳减排报告 (CSV)", key="export_emission", use_container_width=True):
        import io
        import csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['指标', '数值', '单位'])
        writer.writerow(['年充电总量', emission['total_kwh'], '度'])
        writer.writerow(['替代燃油减排', emission['co2_reduction_tons'], '吨CO₂'])
        writer.writerow(['配套储能减排', storage['storage_co2_tons'], '吨CO₂'])
        writer.writerow(['综合碳减排', total_co2, '吨CO₂'])
        writer.writerow(['等效节约标准煤', emission['coal_saved_tons'], '吨'])
        writer.writerow(['等效减少汽油', emission['oil_saved_liters'], 'L'])
        writer.writerow(['等效减少里程', emission['car_km_reduction'], '万km'])
        writer.writerow(['等效植树造林', emission['tree_equivalent'], '棵'])
        writer.writerow(['储能配套容量', storage['storage_capacity_kw'], 'kW'])
        writer.writerow(['充电桩总数', emission['total_chargers'], '个'])
        writer.writerow(['平均利用率', f"{avg_util:.1%}", ''])
        writer.writerow(['碳减排评级', rating['grade'], ''])

        st.download_button(
            label="⬇️ 下载 CSV",
            data=output.getvalue().encode('utf-8-sig'),
            file_name=f"carbon_report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_emission_csv"
        )

# ===== 辅助函数（安全获取/设置场景名称） =====
def get_scene_name_safe():
    scene = st.session_state.current_scene
    if hasattr(scene, 'scene_name'):
        return scene.scene_name
    elif isinstance(scene, dict):
        return scene.get('scene_name', '未命名场景')
    else:
        return '未命名场景'

def set_scene_name_safe(name):
    scene = st.session_state.current_scene
    if hasattr(scene, 'scene_name'):
        scene.scene_name = name
    elif isinstance(scene, dict):
        scene['scene_name'] = name

@st.fragment
def render_onboarding_fragment():
    """新手引导 - 局部刷新版本（修正：去掉多余 rerun）"""
    if 'onboarding_step' not in st.session_state:
        st.session_state.onboarding_step = 0

    # ===== 教程已完成 =====
    if st.session_state.onboarding_step >= 4:
        st.success("🎉 恭喜你完成新手教程！")
        if st.button("重置教程", use_container_width=True, key="onboarding_reset"):
            st.session_state.onboarding_step = 0
            # 按钮点击会自动触发 fragment 刷新，无需 st.rerun()
        return

    # ===== 教程进行中 =====
    st.markdown('<div class="panel-title">🚀 新手教程</div>', unsafe_allow_html=True)
    st.progress(st.session_state.onboarding_step / 4.0)

    # 步骤 0：欢迎
    if st.session_state.onboarding_step == 0:
        st.info("欢迎使用智孪！本教程将引导你完成第一个数字孪生场景。")
        if st.button("▶️ 开始教程", use_container_width=True, type="primary", key="onboarding_start"):
            st.session_state.onboarding_step = 1
            # 🔥 P2 Step2：模板库已并入"组件"面板
            st.session_state.active_panel = 'component'
            # 切左侧面板必须全刷新
            st.rerun(scope="app")

    # 步骤 1：选择模板
    elif st.session_state.onboarding_step == 1:
        st.success("步骤 1/4：选择模板")
        st.markdown("👉 请在左侧 **🧩 组件** 面板底部的「📁 模板库」中点击一个模板")
        if st.button("✅ 我已选择模板", use_container_width=True, key="onboarding_step1"):
            st.session_state.onboarding_step = 2
            st.session_state.active_panel = 'component'
            # 切左侧面板必须全刷新
            st.rerun(scope="app")

    # 步骤 2：添加组件（无需切换面板，仅局部刷新）
    elif st.session_state.onboarding_step == 2:
        st.success("步骤 2/4：添加组件")
        st.markdown("👉 请在左侧 **🧩 组件** 中添加充电桩或建筑")
        if st.button("✅ 我已添加组件", use_container_width=True, key="onboarding_step2"):
            st.session_state.onboarding_step = 3
            # ✅ 不调用 st.rerun()，button 自动触发 fragment 重绘

    # 步骤 3：绑定数据（无需切换面板，仅局部刷新）
    elif st.session_state.onboarding_step == 3:
        st.success("步骤 3/4：绑定数据")
        st.markdown("👉 请点击场景中的充电桩，绑定站点数据")
        if st.button("✅ 我已绑定数据", use_container_width=True, key="onboarding_step3"):
            st.session_state.onboarding_step = 4
            # ✅ 不调用 st.rerun()，button 自动触发 fragment 重绘

# ==================== 右侧面板 ====================
def render_right_panel():
    with st.container():
        st.markdown('<div class="panel">', unsafe_allow_html=True)

        # ===== 任务13：分步新手引导（局部刷新版） =====
        render_onboarding_fragment()
        st.markdown("<hr>", unsafe_allow_html=True)

        # ===== 所有物体下拉列表 =====
        all_objects = st.session_state.scene_objects
        if all_objects:
            obj_options = {}
            for obj in all_objects:
                obj_id = obj.get('id')
                obj_name = obj.get('name', '未命名')
                obj_type = obj.get('type', '')
                icon_map = {
                    'charger_fast': '⚡', 'charger_slow': '🔋', 'charger_super': '🚀',
                    'building': '🏢', 'building_tall': '🏙️', 'tree': '🌳', 'tree_pine': '🌲',
                    'lamp': '💡', 'road_straight': '🛣️', 'road_curve': '↩️',
                    'car': '🚗', 'truck': '🚚'
                }
                icon = icon_map.get(obj_type, '📦')
                label = f"{icon} {obj_name} ({obj_type})"
                obj_options[obj_id] = label

            current_selected = st.session_state.selected_object_id
            options_list = list(obj_options.keys())

            if 'object_selectbox' not in st.session_state:
                st.session_state['object_selectbox'] = current_selected
            elif st.session_state['object_selectbox'] != current_selected:
                st.session_state['object_selectbox'] = current_selected

            if st.session_state['object_selectbox'] not in options_list:
                st.session_state['object_selectbox'] = options_list[0] if options_list else None

            selected = st.selectbox(
                "📋 选择物体",
                options=options_list,
                format_func=lambda x: obj_options.get(x, x),
                key="object_selectbox"
            )

            if selected != current_selected:
                st.session_state.selected_object_id = selected
                st.rerun()

            st.markdown("<hr>", unsafe_allow_html=True)
        else:
            # 🔥 空白场景提示
            st.markdown("""
            <div style="
                background: rgba(26,42,68,0.5);
                border: 1px dashed #2a3a55;
                border-radius: 12px;
                padding: 20px;
                text-align: center;
                color: #8899bb;
                font-size: 13px;
                line-height: 1.8;
                margin: 8px 0;
            ">
                <div style="font-size: 32px; opacity: 0.5; margin-bottom: 8px;">🏗️</div>
                <div style="font-weight: 600; color: #88aadd; margin-bottom: 6px;">当前是空白场景</div>
                <div style="font-size: 12px; color: #667;">
                    从左侧 🧩 <b>组件</b> 面板的「📁 模板库」选择模板<br>
                    或在同一面板的「组件库」添加物体
                </div>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<hr>", unsafe_allow_html=True)

        # ===== 显示选中的物体详情 =====
        selected_id = st.session_state.selected_object_id
        obj = get_object_by_id(selected_id) if selected_id else None

        if obj is None:
            st.markdown(
                '<div style="color:#667;text-align:center;padding:2rem 0;">点击场景中的物体<br>或从上方下拉列表选择</div>',
                unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
            return

        # ----- 对象基本信息 -----
        obj_type = obj.get('type', 'unknown')
        obj_name = obj.get('name', '未命名')
        icon_map = {
            'charger_fast': '⚡', 'charger_slow': '🔋', 'charger_super': '🚀',
            'building': '🏢', 'building_tall': '🏙️', 'tree': '🌳', 'tree_pine': '🌲',
            'lamp': '💡', 'road_straight': '🛣️', 'road_curve': '↩️',
            'car': '🚗', 'truck': '🚚'
        }
        icon = icon_map.get(obj_type, '📦')
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:0.5rem;font-size:1.1rem;font-weight:600;color:#eef2ff;margin-bottom:0.5rem;">{icon} {obj_name}</div>',
            unsafe_allow_html=True)
        st.markdown(f'<span style="color:#667;font-size:0.7rem;">类型: {obj_type}</span>', unsafe_allow_html=True)

        pos = obj.get('position', {"x": 0, "y": 0, "z": 0})
        rot = obj.get('rotation', {"x": 0, "y": 0, "z": 0})
        scl = obj.get('scale', {"x": 1, "y": 1, "z": 1})
        st.markdown(
            f'<span style="color:#667;font-size:0.7rem;">位置: ({pos["x"]:.2f}, {pos["y"]:.2f}, {pos["z"]:.2f})</span>',
            unsafe_allow_html=True)
        st.markdown(
            f'<span style="color:#667;font-size:0.7rem;">旋转: ({rot["x"]:.2f}, {rot["y"]:.2f}, {rot["z"]:.2f})</span>',
            unsafe_allow_html=True)
        st.markdown(
            f'<span style="color:#667;font-size:0.7rem;">缩放: ({scl["x"]:.2f}, {scl["y"]:.2f}, {scl["z"]:.2f})</span>',
            unsafe_allow_html=True)

        # ===== 如果是充电桩，显示数据绑定和实时数据 =====
        if obj_type in ['charger_fast', 'charger_slow', 'charger_super']:
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown('<div class="panel-title">🔗 数据绑定</div>', unsafe_allow_html=True)

            bound_station = obj.get('bind_station_id', '')
            _pred = _get_predictor()
            stations = _pred.get_station_list() if _pred else []
            station_options = [""] + [s['id'] for s in stations]
            station_labels = {s['id']: f"{s['id']} - {s.get('name', '')}" for s in stations}
            station_labels[""] = "未绑定"

            selected_station = st.selectbox(
                "绑定站点",
                options=station_options,
                format_func=lambda x: station_labels.get(x, x),
                index=station_options.index(bound_station) if bound_station in station_options else 0,
                key=f"bind_{obj['id']}"
            )

            if selected_station != bound_station:
                # 1. 直接改本地 obj（不走服务端同步函数，避免其中的 st.rerun() 干扰）
                obj['bind_station_id'] = selected_station
                if st.session_state.current_scene:
                    for i, so in enumerate(st.session_state.current_scene.objects):
                        if so.get('id') == obj['id']:
                            st.session_state.current_scene.objects[i] = obj
                            break

                # 2. 显式同步到 Supabase（关键：确保绑定关系落库）
                if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                    try:
                        sync_scene_objects(
                            st.session_state.scene_id,
                            st.session_state.scene_objects
                        )
                        st.toast(f"☁️ 已绑定站点 {selected_station} 并保存到云端", icon="☁️")
                    except Exception as e:
                        st.error(f"❌ 同步失败: {e}")

                st.session_state.binding_station_id = selected_station
                st.rerun(scope="app")

            # ===== 模拟数据生成器 =====
            st.markdown('<div style="margin-top:0.3rem;">', unsafe_allow_html=True)
            col_mock1, col_mock2 = st.columns(2)
            with col_mock1:
                if st.button("📊 生成模拟数据", key=f"mock_{obj['id']}", use_container_width=True):
                    try:
                        obj['bind_station_id'] = ''
                        import random
                        util = round(random.uniform(0.2, 0.85), 2)
                        history_vals = [util]
                        for _ in range(47):
                            change = random.uniform(-0.08, 0.08)
                            new_val = max(0.05, min(0.95, history_vals[-1] + change))
                            history_vals.append(round(new_val, 2))
                        pred_vals = [history_vals[-1]]
                        for _ in range(3):
                            change = random.uniform(-0.05, 0.05)
                            new_val = max(0.05, min(0.95, pred_vals[-1] + change))
                            pred_vals.append(round(new_val, 2))
                        if 'custom_props' not in obj:
                            obj['custom_props'] = {}
                        obj['custom_props']['_mock_data'] = {
                            "utilization": util,
                            "history": history_vals,
                            "prediction": pred_vals,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        if st.session_state.current_scene:
                            for i, so in enumerate(st.session_state.current_scene.objects):
                                if so.get('id') == obj['id']:
                                    st.session_state.current_scene.objects[i] = obj
                                    break
                        st.toast("📊 模拟数据已生成", icon="📊")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 生成模拟数据失败: {e}")

            with col_mock2:
                if obj.get('custom_props', {}).get('_mock_data'):
                    if st.button("🗑️ 清除模拟", key=f"clear_mock_{obj['id']}", use_container_width=True):
                        if 'custom_props' in obj and '_mock_data' in obj['custom_props']:
                            del obj['custom_props']['_mock_data']
                            if st.session_state.current_scene:
                                for i, so in enumerate(st.session_state.current_scene.objects):
                                    if so.get('id') == obj['id']:
                                        st.session_state.current_scene.objects[i] = obj
                                        break
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

            # ===== 数据展示部分（统一逻辑，无重复） =====
            use_mock = (not bound_station) and obj.get('custom_props', {}).get('_mock_data')
            mock_data = obj.get('custom_props', {}).get('_mock_data') if use_mock else None
            has_obj_util = obj.get('utilization') is not None
            use_obj_util = has_obj_util and not bound_station and not use_mock

            # 变量初始化（避免后面的分支引用未定义）
            util = 0.0
            timestamp = '--'
            available = 0
            is_mock = False
            hist_times = []
            hist_values = []
            pred_times = []
            pred_values = []
            current = 0.0
            show_data = False

            if bound_station and not use_mock and _get_predictor() is not None:
                # 优先：真实站点数据
                detail = _get_predictor().get_station_detail(bound_station)
                realtime = detail.get('realtime', {})
                util = realtime.get('utilization', 0)

                # 🔥 反向同步：把实时利用率写回 obj
                # 让左侧资产树和 3D 场景能拿到同一个值
                if abs(obj.get('utilization', 0) - util) > 0.01:
                    obj['utilization'] = util
                    # 同步到 current_scene
                    if st.session_state.current_scene:
                        for i, so in enumerate(st.session_state.current_scene.objects):
                            if so.get('id') == obj['id']:
                                st.session_state.current_scene.objects[i] = obj
                                break
                    # 清空缓存，强制下次刷新使用新值
                    st.session_state.last_update = time.time()

                # 🔥 同步到 Supabase station_realtime_data 表
                # 让前端的 Realtime 订阅能收到更新 → 触发告警弹窗
                if SUPABASE_AVAILABLE and bound_station:
                    try:
                        update_station_data(bound_station, {
                            'utilization': util,
                            'available_slots': available,
                            'status': '高负载' if util > 0.7 else '在线',
                            'updated_at': datetime.now().isoformat()
                        })
                    except Exception as e:
                        print(f"⚠️ 同步实时数据到 Supabase 失败: {e}")

                timestamp = realtime.get('timestamp', '--')
                available = realtime.get('available', 0)
                is_mock = realtime.get('is_mock', False)
                history = detail.get('history', {})
                hist_times = history.get('timestamps', [])
                hist_values = history.get('values', [])
                pred = detail.get('prediction', {})
                pred_times = pred.get('timestamps', [])
                pred_values = pred.get('values', [])
                current = pred.get('current', util)
                show_data = True
                data_title = "📊 实时数据"

            elif use_mock:
                # 次优先：模拟数据
                util = mock_data['utilization']
                timestamp = mock_data.get('timestamp', '--')
                available = int((1 - util) * 10)
                hist_values = mock_data['history']
                hist_times = [f"{(datetime.now() - timedelta(hours=47 - i)).strftime('%H:%M')}" for i in range(48)]
                pred_values = mock_data['prediction']
                pred_times = [(datetime.now() + timedelta(hours=i + 1)).strftime('%H:%M') for i in range(3)]
                current = util
                is_mock = True
                show_data = True
                data_title = "📊 模拟数据"

            elif use_obj_util:
                # 兜底：使用 obj['utilization']（与3D光柱同源）
                util = obj['utilization']
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                available = int((1 - util) * 10)
                hist_values = []
                hist_times = []
                pred_values = []
                pred_times = []
                current = util
                is_mock = True
                show_data = True
                data_title = "📊 场景数据"

            if show_data:
                st.markdown("<hr>", unsafe_allow_html=True)
                st.markdown(f'<div class="panel-title">{data_title}</div>', unsafe_allow_html=True)

                util_color = "#51cf66" if util < 0.4 else ("#fcc419" if util < 0.7 else "#ff6b6b")
                st.markdown(f"""
                <div style="display:flex;justify-content:space-between;align-items:center;padding:0.2rem 0;">
                    <span style="color:#88aadd;">利用率</span>
                    <span style="color:{util_color};font-size:1.4rem;font-weight:700;">{util:.1%}</span>
                </div>
                <div style="background:#1a2a44;border-radius:6px;height:6px;overflow:hidden;margin:0.2rem 0 0.5rem 0;">
                    <div style="background:{util_color};width:{util * 100:.0f}%;height:100%;border-radius:6px;"></div>
                </div>
                """, unsafe_allow_html=True)

                col1, col2 = st.columns(2)
                with col1:
                    st.metric("空闲插槽", available)
                with col2:
                    st.metric("更新时间", timestamp[-5:] if len(timestamp) > 5 else timestamp)

                if is_mock and not bound_station:
                    st.caption("⚠️ 模拟数据 (未绑定真实站点)")

                # ----- 站点画像 -----
                st.markdown('<div class="panel-title" style="margin-top:0.5rem;">🏷️ 站点画像</div>',
                            unsafe_allow_html=True)
                labels = []
                if obj_type == 'charger_fast':
                    labels.append(("⚡ 快充", "#4a90d9"))
                elif obj_type == 'charger_slow':
                    labels.append(("🔋 慢充", "#5cb85c"))
                elif obj_type == 'charger_super':
                    labels.append(("🚀 超充", "#9b59b6"))
                if util > 0.7:
                    labels.append(("🔴 高负载", "#ff6b6b"))
                elif util > 0.4:
                    labels.append(("🟡 适中", "#fcc419"))
                else:
                    labels.append(("🟢 低负载", "#51cf66"))
                if bound_station:
                    labels.append(("✅ 已绑定", "#51cf66"))
                else:
                    labels.append(("❌ 未绑定", "#ff6b6b"))

                label_html = ""
                for text, color in labels:
                    label_html += f'<span style="background:{color};color:white;padding:2px 10px;border-radius:12px;font-size:11px;margin-right:6px;">{text}</span>'
                st.markdown(f'<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px;">{label_html}</div>',
                            unsafe_allow_html=True)

                # ----- 周边站点 -----
                st.markdown('<div class="panel-title" style="margin-top:0.5rem;">📍 周边站点</div>',
                            unsafe_allow_html=True)
                all_chargers_list = [o for o in st.session_state.scene_objects if
                                     o.get('type', '').startswith('charger')]
                nearby = []
                for c in all_chargers_list:
                    if c['id'] == obj['id']:
                        continue
                    cpos = c.get('position', {"x": 0, "y": 0, "z": 0})
                    dx = pos['x'] - cpos.get('x', 0)
                    dz = pos['z'] - cpos.get('z', 0)
                    distance = (dx * dx + dz * dz) ** 0.5
                    nearby.append({
                        "id": c['id'],
                        "name": c.get('name', '未知'),
                        "utilization": c.get('utilization', 0.5),
                        "distance": distance,
                        "type": c.get('type', 'charger_fast')
                    })
                nearby = sorted(nearby, key=lambda x: x['distance'])[:5]
                if nearby:
                    for item in nearby:
                        btn_label = f"{item['name']} ({item['utilization']:.0%}) 距离 {item['distance']:.1f}"
                        if st.button(btn_label, key=f"nearby_{item['id']}", use_container_width=True):
                            st.session_state.selected_object_id = item['id']
                            st.rerun()
                    st.caption("点击按钮可切换选中")
                else:
                    st.caption("附近没有其他充电桩")

                # ----- 历史趋势图 -----
                st.markdown('<div class="panel-title" style="margin-top:0.5rem;">📈 历史趋势 (48h)</div>',
                            unsafe_allow_html=True)
                if hist_values:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=hist_times,
                        y=hist_values,
                        mode='lines+markers',
                        name='利用率',
                        line=dict(color='#4a90d9', width=2),
                        marker=dict(size=3, color='#4a90d9'),
                        fill='tozeroy',
                        fillcolor='rgba(74,144,217,0.15)'
                    ))
                    fig.update_layout(
                        height=150,
                        margin=dict(l=0, r=0, t=10, b=10),
                        paper_bgcolor='rgba(0,0,0,0)',
                        plot_bgcolor='rgba(0,0,0,0)',
                        xaxis=dict(showgrid=False, tickfont=dict(color='#667', size=8),
                                   tickangle=45, showticklabels=True, ticks=''),
                        yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)',
                                   tickfont=dict(color='#667', size=8), range=[0, 1],
                                   tickformat='.0%', showticklabels=True),
                        hovermode='x'
                    )
                    fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1,
                                                  xanchor="right", x=1, font=dict(color='#667', size=8)))
                    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
                else:
                    st.caption("暂无历史数据")

                # ----- 预测 -----
                st.markdown('<div class="panel-title" style="margin-top:0.5rem;">🔮 预测 (未来12h)</div>',
                            unsafe_allow_html=True)
                if pred_values:
                    show_prediction = st.checkbox("显示预测预览", key=f"show_pred_{obj['id']}", value=True)
                    if show_prediction:
                        all_times = [f"当前\n{datetime.now().strftime('%H:%M')}"] + pred_times
                        all_values = [current] + pred_values

                        fig2 = go.Figure()
                        fig2.add_trace(go.Scatter(
                            x=[all_times[0]], y=[all_values[0]],
                            mode='markers', name='当前',
                            marker=dict(color='#51cf66', size=10, symbol='diamond')
                        ))
                        fig2.add_trace(go.Scatter(
                            x=all_times, y=all_values,
                            mode='lines+markers', name='预测',
                            line=dict(color='#9b59b6', width=2, dash='dot'),
                            marker=dict(size=5, color='#9b59b6')
                        ))
                        fig2.update_layout(
                            height=120,
                            margin=dict(l=0, r=0, t=10, b=10),
                            paper_bgcolor='rgba(0,0,0,0)',
                            plot_bgcolor='rgba(0,0,0,0)',
                            xaxis=dict(showgrid=False, tickfont=dict(color='#667', size=7),
                                       showticklabels=True, ticks=''),
                            yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)',
                                       tickfont=dict(color='#667', size=7), range=[0, 1],
                                       tickformat='.0%', showticklabels=True),
                            hovermode='x'
                        )
                        fig2.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1,
                                                       xanchor="right", x=1, font=dict(color='#667', size=7)))
                        st.plotly_chart(fig2, use_container_width=True, config={'displayModeBar': False})
                else:
                    st.caption("暂无预测数据，请先生成模拟数据或绑定真实站点")

                # ===== 任务14：预测数据导出 =====
                if pred_values and pred_times:
                    import io
                    import csv
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(['时间戳', '预测利用率'])
                    # 加上当前时间点
                    writer.writerow([f"当前 ({datetime.now().strftime('%H:%M')})", f"{current:.4f}"])
                    for t, v in zip(pred_times, pred_values):
                        writer.writerow([t, f"{v:.4f}"])

                    st.download_button(
                        label="📥 导出预测数据 (CSV)",
                        data=output.getvalue().encode('utf-8-sig'),
                        file_name=f"prediction_{obj.get('name', 'station')}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                        mime="text/csv",
                        use_container_width=True,
                        key=f"export_pred_{obj['id']}"
                    )

                # ===== What-if 情景分析 =====
                st.markdown('<div class="panel-title" style="margin-top:0.5rem;">🎯 What-if 情景分析</div>', unsafe_allow_html=True)

                if 'whatif_active' not in st.session_state:
                    st.session_state.whatif_active = False

                col_w1, col_w2 = st.columns(2)
                with col_w1:
                    if st.button("🎬 启用模拟", key=f"whatif_on_{obj['id']}", use_container_width=True,
                                 type="primary" if st.session_state.whatif_active else "secondary"):
                        st.session_state.whatif_active = True
                        st.rerun()
                with col_w2:
                    if st.button("⏹ 重置", key=f"whatif_off_{obj['id']}", use_container_width=True):
                        st.session_state.whatif_active = False
                        if 'whatif_params' in st.session_state:
                            del st.session_state.whatif_params
                        st.rerun()

                if st.session_state.whatif_active:
                    st.caption("调整以下参数，模拟政策效果")

                    # 参数滑块
                    new_chargers = st.slider("🚀 新建充电桩数量", 0, 20, 5, key=f"whatif_new_{obj['id']}")
                    subsidy = st.slider("💰 电价补贴比例 (%)", 0, 50, 20, key=f"whatif_sub_{obj['id']}") / 100.0
                    peak_shift = st.slider("⏰ 错峰引导强度 (%)", 0, 100, 30, key=f"whatif_peak_{obj['id']}") / 100.0

                    # 计算模拟结果
                    base_util = util
                    sim_util = base_util

                    # 新桩分流
                    if new_chargers > 0:
                        sim_util *= (1 - min(0.3, new_chargers * 0.02))
                    # 补贴提升
                    if subsidy > 0 and sim_util < 0.4:
                        sim_util = min(0.9, sim_util * (1 + subsidy * 0.3))
                    # 错峰转移高峰
                    if peak_shift > 0 and sim_util > 0.6:
                        sim_util *= (1 - peak_shift * 0.15)

                    sim_util = max(0.05, min(0.95, sim_util))
                    delta = sim_util - base_util

                    # 显示对比
                    st.markdown(f"""
                    <div style="background:rgba(26,42,68,0.6);border-radius:12px;padding:12px;margin-top:8px;">
                        <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
                            <span style="color:#88aadd;">当前利用率</span>
                            <span style="color:#eef2ff;font-weight:700;">{base_util:.1%}</span>
                        </div>
                        <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
                            <span style="color:#88aadd;">模拟后利用率</span>
                            <span style="color:{'#51cf66' if delta < 0 else '#ff6b6b'};font-weight:700;">{sim_util:.1%}</span>
                        </div>
                        <div style="display:flex;justify-content:space-between;">
                            <span style="color:#88aadd;">变化</span>
                            <span style="color:{'#51cf66' if delta < 0 else '#ff6b6b'};font-weight:700;">{delta:+.1%}</span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                    # 同步到3D场景
                    if st.button("✅ 应用到3D场景", key=f"whatif_apply_{obj['id']}", use_container_width=True):
                        obj['utilization'] = sim_util
                        if st.session_state.current_scene:
                            for i, so in enumerate(st.session_state.current_scene.objects):
                                if so.get('id') == obj['id']:
                                    st.session_state.current_scene.objects[i] = obj
                                    break
                        st.toast(f"✅ 已应用模拟结果：{sim_util:.1%}", icon="🎯")
                        st.rerun()
            else:
                st.caption("💡 从下拉列表中选择一个深圳站点进行绑定，或点击「生成模拟数据」预览效果")

        # ===== 语义关系（关联物体）=====
        relations = obj.get('custom_props', {}).get('relations', [])
        if relations:
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown('<div class="panel-title">🔗 关联物体</div>', unsafe_allow_html=True)

            type_icons = {
                'along_street': '🛣️',
                'serves': '🏢',
                'connects': '🔗',
                'adjacent': '⚡',
                'neighbors': '🏘️',
                'same_street': '🛤️',
            }
            type_labels = {
                'along_street': '临街',
                'serves': '服务',
                'connects': '连接',
                'adjacent': '同组',
                'neighbors': '相邻',
                'same_street': '同街',
            }
            type_colors = {
                'along_street': '#51cf66',
                'serves': '#fcc419',
                'connects': '#88aadd',
                'adjacent': '#9b59b6',
                'neighbors': '#ff9f43',
                'same_street': '#00d2d3',
            }

            for idx, rel in enumerate(relations[:8]):
                target_id = rel.get('target')
                target_name = rel.get('target_name', '未知')
                rel_type = rel.get('type', 'nearby')
                distance = rel.get('distance', 0)

                icon = type_icons.get(rel_type, '🔗')
                label = type_labels.get(rel_type, '关联')
                color = type_colors.get(rel_type, '#88aadd')

                col_rel1, col_rel2 = st.columns([4, 1])
                with col_rel1:
                    st.markdown(
                        f'<div style="padding:4px 8px;background:rgba(26,42,68,0.4);'
                        f'border-left:3px solid {color};border-radius:6px;margin:2px 0;">'
                        f'<span style="color:{color};font-size:11px;font-weight:600;">'
                        f'{icon} {label}</span> '
                        f'<span style="color:#eef2ff;font-size:12px;">{target_name}</span> '
                        f'<span style="color:#667;font-size:10px;">{distance:.1f}m</span>'
                        f'</div>',
                        unsafe_allow_html=True
                    )
                with col_rel2:
                    if st.button("→", key=f"jump_{obj['id']}_{rel_type}_{idx}", help="跳转"):
                        st.session_state.selected_object_id = target_id
                        st.session_state['object_selectbox'] = target_id
                        st.rerun()

        # ===== 操作历史 =====
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown('<div class="panel-title">📋 操作历史</div>', unsafe_allow_html=True)
        if 'operation_history' not in st.session_state:
            st.session_state.operation_history = []
        history_list = st.session_state.operation_history[-5:][::-1]
        if history_list:
            for entry in history_list:
                st.caption(f"{entry['time']} - {entry['action']} {entry.get('target', '')}")
        else:
            st.caption("暂无操作记录")

        # ----- 操作模式（仅移动） -----
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown('<div class="panel-title">🛠️ 操作模式</div>', unsafe_allow_html=True)
        st.caption("点击物体后可用拖拽移动")

        # 🔥 P1 精简：删除「📌 移动模式（拖拽物体）」按钮及其状态。
        # 它是空操作——只写 st.session_state.transform_mode 与 query_params['mode']，
        # 而前端 transformControls.setMode('translate') 是硬编码的（7142），
        # 从未读取过这两个值。全项目再无 transform_mode 的其他消费者。
        # 🔥 P1 / 清单 #13：那句"旋转和缩放请在 3D 场景下方的滑块面板操作"已改写。
        # 原因是它属于"跨表面指引"：现在变换只有一个归属地（3D 画布 + 左下角 ☰ 变换面板），
        # 指引不再把用户推向另一个界面。
        st.caption("💡 位置 / 旋转 / 缩放：在 3D 中直接拖拽物体，或用左下角 ☰ 变换面板（X/Y/Z、旋转、缩放）")

        # ----- 操作按钮（删除、复制） -----
        st.markdown("<hr>", unsafe_allow_html=True)
        col_del1, col_del2 = st.columns(2)
        with col_del1:
            if st.button("🗑️ 删除", key=f"delete_{obj['id']}", use_container_width=True):
                delete_object(obj['id'])
                st.rerun()
        with col_del2:
            if st.button("📋 复制", key=f"copy_{obj['id']}", use_container_width=True):
                new_obj = obj.copy()
                new_obj['id'] = str(uuid.uuid4())
                new_obj['name'] = f"{obj.get('name', '')}_副本"
                new_obj['position'] = {
                    "x": obj.get('position', {}).get('x', 0) + 0.5,
                    "y": obj.get('position', {}).get('y', 0),
                    "z": obj.get('position', {}).get('z', 0) + 0.5
                }
                st.session_state.scene_objects.append(new_obj)
                if st.session_state.current_scene:
                    st.session_state.current_scene.objects = st.session_state.scene_objects
                st.session_state.selected_object_id = new_obj['id']
                st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


# ==================== 底部指标条 ====================
def render_bottom_bar():
    objects = st.session_state.scene_objects
    chargers = [obj for obj in objects if obj.get('type', '').startswith('charger')]
    total_chargers = len(chargers)
    bound_chargers = len([c for c in chargers if c.get('bind_station_id')])

    utils = []
    if any(c.get('bind_station_id') for c in chargers):
        _pred = _get_predictor()
        for c in (chargers if _pred else []):
            sid = c.get('bind_station_id')
            if sid:
                data = _pred.get_realtime_util(sid)
                utils.append(data.get('utilization', 0))
    avg_util = np.mean(utils) if utils else 0
    # 🔥 修复：avg_util 一直被计算却从未显示（大屏版与碳减排面板都有显示）；
    #    顺手删掉同样算了却没人用的 high_count（与 high_load_count 重复）。
    if utils:
        _avg_cls = 'good' if avg_util < 0.4 else ('warn' if avg_util < 0.7 else 'high')
        avg_html = f'<div class="metric-item">📊 平均利用率 <span class="value {_avg_cls}">{avg_util:.1%}</span></div>'
    else:
        avg_html = '<div class="metric-item">📊 平均利用率 <span class="value">—</span> <span style="color:#667;font-size:0.7rem;">未绑定站点</span></div>'

    buildings = len([obj for obj in objects if obj.get('type') == 'building'])
    trees = len([obj for obj in objects if obj.get('type', '').startswith('tree')])

    # 🔥 三色分类统计
    offline_count = sum(1 for c in chargers if c.get('status') == '离线')
    high_load_count = sum(
        1 for c in chargers
        if c.get('status') != '离线' and c.get('utilization', 0) > 0.7
    )
    normal_count = total_chargers - offline_count - high_load_count

    # 🔥 碳减排计算（读取用户可调参数）
    _fast_h = st.session_state.get('carbon_fast_hours', 2.0)
    _slow_h = st.session_state.get('carbon_slow_hours', 4.0)
    _ratio = st.session_state.get('carbon_storage_ratio', 0.2)
    _cycles = st.session_state.get('carbon_storage_cycles', 1.5)

    emission = calculate_emission(objects, fast_hours=_fast_h, slow_hours=_slow_h)
    storage = calculate_storage_emission(objects, storage_ratio=_ratio, cycles=_cycles)
    total_co2 = emission['co2_reduction_tons'] + storage['storage_co2_tons']

    st.markdown(f"""
    <div class="metric-bar">
        <div class="metric-item">🏗️ 总对象 <span class="value">{len(objects)}</span></div>
        <div class="metric-item">⚡ 充电桩 <span class="value">{total_chargers}</span> <span style="color:#667;font-size:0.7rem;">({bound_chargers} 已绑定)</span></div>
        <div class="metric-item">🟢 正常 <span class="value good">{normal_count}</span></div>
        <div class="metric-item">🟡 高负载 <span class="value warn">{high_load_count}</span></div>
        <div class="metric-item">🔴 离线 <span class="value high">{offline_count}</span></div>
        {avg_html}
        <div class="metric-item">🌿 年碳减排 <span class="value good">{total_co2}</span> <span style="color:#667;font-size:0.7rem;">吨CO₂</span></div>
        <div class="metric-item">🌳 等效植树 <span class="value good">{emission['tree_equivalent']}</span> <span style="color:#667;font-size:0.7rem;">棵</span></div>
        <div class="metric-item">🏢 建筑 <span class="value">{buildings}</span></div>
        <div class="metric-item">🌳 树木 <span class="value">{trees}</span></div>
    </div>
    """, unsafe_allow_html=True)


def render_bottom_bar_big():
    """大屏模式下的超大指标条（含碳减排可调参数）"""
    objects = st.session_state.scene_objects
    chargers = [obj for obj in objects if obj.get('type', '').startswith('charger')]
    total_chargers = len(chargers)
    bound_chargers = len([c for c in chargers if c.get('bind_station_id')])

    utils = []
    if any(c.get('bind_station_id') for c in chargers):
        _pred = _get_predictor()
        for c in (chargers if _pred else []):
            sid = c.get('bind_station_id')
            if sid:
                data = _pred.get_realtime_util(sid)
                utils.append(data.get('utilization', 0))
    avg_util = np.mean(utils) if utils else 0
    high_count = len([u for u in utils if u > 0.7])

    # 🔥 从 session_state 读取可调参数（与左侧碳减排面板同步）
    _fast_h = st.session_state.get('carbon_fast_hours', 2.0)
    _slow_h = st.session_state.get('carbon_slow_hours', 4.0)
    _ratio = st.session_state.get('carbon_storage_ratio', 0.2)
    _cycles = st.session_state.get('carbon_storage_cycles', 1.5)

    emission = calculate_emission(objects, fast_hours=_fast_h, slow_hours=_slow_h)
    storage = calculate_storage_emission(objects, storage_ratio=_ratio, cycles=_cycles)
    total_co2 = emission['co2_reduction_tons'] + storage['storage_co2_tons']

    st.markdown(f"""
    <div style="
        display: flex; justify-content: space-around; align-items: center;
        padding: 20px 40px; margin-top: 16px;
        background: linear-gradient(90deg, rgba(16,22,40,0.9), rgba(26,42,68,0.9), rgba(16,22,40,0.9));
        border: 1px solid rgba(74,144,217,0.3);
        border-radius: 20px;
        box-shadow: 0 0 60px rgba(74,144,217,0.15);
        flex-wrap: wrap; gap: 20px;
    ">
        <div style="text-align:center;">
            <div style="font-size:12px; color:#88aadd; letter-spacing:0.1em; margin-bottom:4px;">总对象</div>
            <div style="font-size:36px; font-weight:800; color:#88ccff;">{len(objects)}</div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#88aadd; letter-spacing:0.1em; margin-bottom:4px;">充电桩</div>
            <div style="font-size:36px; font-weight:800; color:#88ccff;">{total_chargers}</div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#88aadd; letter-spacing:0.1em; margin-bottom:4px;">已绑定</div>
            <div style="font-size:36px; font-weight:800; color:#51cf66;">{bound_chargers}</div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#88aadd; letter-spacing:0.1em; margin-bottom:4px;">平均利用率</div>
            <div style="font-size:36px; font-weight:800; color:{'#51cf66' if avg_util < 0.4 else '#fcc419' if avg_util < 0.7 else '#ff6b6b'};">{avg_util:.1%}</div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#88aadd; letter-spacing:0.1em; margin-bottom:4px;">高负载</div>
            <div style="font-size:36px; font-weight:800; color:#ff6b6b;">{high_count}</div>
        </div>
        <div style="text-align:center; padding-left:20px; border-left:1px solid rgba(81,207,102,0.3);">
            <div style="font-size:12px; color:#51cf66; letter-spacing:0.1em; margin-bottom:4px;">🌿 年碳减排</div>
            <div style="font-size:36px; font-weight:800; color:#51cf66;">{total_co2}<span style="font-size:14px;color:#88aadd;">吨</span></div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#51cf66; letter-spacing:0.1em; margin-bottom:4px;">🌳 等效植树</div>
            <div style="font-size:36px; font-weight:800; color:#51cf66;">{emission['tree_equivalent']}<span style="font-size:14px;color:#88aadd;">棵</span></div>
        </div>
        <div style="text-align:center;">
            <div style="font-size:12px; color:#fcc419; letter-spacing:0.1em; margin-bottom:4px;">🚗 减少里程</div>
            <div style="font-size:36px; font-weight:800; color:#fcc419;">{emission['car_km_reduction']}<span style="font-size:14px;color:#88aadd;">万km</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ==================== 3D 场景 HTML ====================
def generate_scene_html():
    """生成 Three.js 场景 HTML（含只读的悬浮信息面板；删除/复制在右侧面板）"""
    objects = st.session_state.scene_objects
    # 🔥 P3：不再从这里下发告警阈值。它只有一个真值存放处——浏览器 localStorage
    # （由工具箱里的阈值滑块写入），前端统一用 getAlertThreshold() 读取。
    # 原先这行读的 st.session_state['alert_threshold'] 从未被任何控件写入过，
    # 永远是默认 0.7，却会被当作"当前告警阈值"显示出来。
    selected_id = st.session_state.selected_object_id or ""

    # 🔥 P3：删除 _runtime_position / _runtime_rotation / _runtime_scale 的影子覆盖。
    # 这三个字段的两个写入点都是死代码（update_object_props 全项目 0 调用；
    # update_pos 查询参数分支没有任何生产者），而这里却是用「部分」字典整体覆盖
    # obj['position']（例如 {{'x': 5}} 会覆盖掉 y/z）→ 前端拿到 undefined 变 NaN。
    # 现在 position / rotation / scale 各自只有一个真值：对象自身的字段。

    from core.style_manager import get_style_manager
    style_mgr = get_style_manager()
    style_id = st.session_state.get('current_style', 'tech_blue')
    style = style_mgr.get_style(style_id)

    from core.story_manager import get_story_manager
    story_mgr = get_story_manager()
    story_id = st.session_state.get('current_story', None)
    story = story_mgr.get_story(story_id) if story_id else None

    import json
    supabase_url = st.secrets.get("SUPABASE_URL", "")
    supabase_anon_key = st.secrets.get("SUPABASE_ANON_KEY", "")
    has_supabase = bool(supabase_url and supabase_anon_key)
    supabase_url_escaped = json.dumps(supabase_url)
    supabase_anon_key_escaped = json.dumps(supabase_anon_key)

    scene_id = st.session_state.get('scene_id', None)
    scene_id_escaped = json.dumps(scene_id)

    # ===== 加载时序数据（用于时间轴回放）=====
    timeline_data = None
    try:
        info = _occupancy_header()
        if info:
            all_cols = set(info["columns"])
            bound_ids = {
                str(obj.get('bind_station_id', ''))
                for obj in objects
                if str(obj.get('bind_station_id', '')) in all_cols
            }
            # 优先用已绑定的站点，太少就用前 100 个
            station_cols = tuple(sorted(bound_ids)) if len(bound_ids) >= 10 else tuple(info["columns"][:100])
            timeline_data = _build_timeline(station_cols)
            if timeline_data:
                print(f"✅ 时间轴数据：{timeline_data['maxSteps']} 步 × {len(timeline_data['stationIds'])} 站点")
    except Exception as e:
        print(f"⚠️ 时间轴加载失败: {e}")
        timeline_data = None

    scene_data = {
        "objects": objects,
        "selectedId": selected_id,
        "hasSupabase": has_supabase,
        "sceneId": scene_id,
        # P3：不再下发 alertThreshold（告警阈值唯一真值在浏览器 localStorage）
        "timeline": timeline_data,
        "lodConfig": {
            "enabled": st.session_state.get('lod_enabled', True),
            "nearDistance": 15.0,
            "farDistance": 35.0,
        },

        "customAssetUrls": {
            obj.get('custom_props', {}).get('asset_id'): obj.get('custom_props', {}).get('glb_url')
            for obj in objects
            if obj.get('type') == 'custom_model'
               and obj.get('custom_props', {}).get('asset_id')
               and obj.get('custom_props', {}).get('glb_url')
        },

        "style": {
            "scene_bg": style.scene_bg,
            "fog_color": style.fog_color,
            "grid_color": style.grid_color,
            "ground_color": style.ground_color,
            "ambient_color": style.ambient_color,
            "ambient_intensity": style.ambient_intensity,
            "sun_color": style.sun_color,
            "sun_intensity": style.sun_intensity,
            "charger_color": style.charger_color,
            "building_color": style.building_color,
            "tree_color": style.tree_color,
            "bloom_strength": style.bloom_strength,
            "bloom_radius": style.bloom_radius,
            "bloom_threshold": style.bloom_threshold,
        },
        "story": {
            "id": story.id if story else None,
            "camera_position": story.camera_position if story else None,
            "camera_target": story.camera_target if story else None,
            "highlight_type": story.highlight_type if story else None,
            "filter_threshold": story.filter_threshold if story else None,
            "auto_rotate": story.auto_rotate if story else False,
            "show_labels": story.show_labels if story else True
        } if story else None,
        "devOptions": {
            "printConsole": st.session_state.get('_dev_print_console', False)
        }
    }

    scene_json = json.dumps(scene_data, ensure_ascii=False)

    comp_defs = {}
    for cid, comp in component_lib.get_all().items():
        comp_defs[cid] = {
            "name": comp.name,
            "type": comp.type,
            "default_color": comp.default_color,
            "default_scale": comp.default_scale
        }
    comp_defs_json = json.dumps(comp_defs, ensure_ascii=False)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ overflow: hidden; background: #0a0e17; font-family: 'Segoe UI', Arial, sans-serif; }}
            #container {{ width: 100vw; height: 100vh; position: relative; }}
            #info {{
                position: absolute; top: 16px; left: 50%; transform: translateX(-50%);
                color: #667; font-size: 13px; z-index: 10;
                background: rgba(10,14,23,0.6); padding: 4px 20px;
                border-radius: 20px; border: 1px solid #1a2a44;
                pointer-events: none;
                backdrop-filter: blur(4px);
            }}
            #info span {{ color: #88aadd; }}
            #status {{
                position: absolute; bottom: 16px; right: 20px;
                color: #445; font-size: 11px; z-index: 10;
                background: rgba(10,14,23,0.5); padding: 2px 12px;
                border-radius: 12px;
            }}
            #realtime-status {{
                position: absolute; top: 70px; right: 20px;
                color: #51cf66; font-size: 11px; z-index: 10;
                background: rgba(10,14,23,0.7); padding: 3px 12px;
                border-radius: 12px;
                border: 1px solid rgba(81,207,102,0.2);
                opacity: 0;
                transition: opacity 0.5s;
                pointer-events: none;
            }}
            #realtime-status.visible {{ opacity: 1; }}
            #drag-hint {{
                display: none;
                position: absolute; top: 55px; left: 50%; transform: translateX(-50%);
                color: #88aadd; font-size: 12px; z-index: 10;
                background: rgba(10,14,23,0.7); padding: 4px 16px;
                border-radius: 20px; border: 1px solid #1a2a44;
                opacity: 0.85;
                pointer-events: none;
                transition: opacity 0.3s;
            }}
            #drag-hint.hidden {{ opacity: 0; }}

                        /* ============ 变换抽屉（左下角） ============ */
            #slider-drawer {{
                position: absolute;
                left: 0;
                bottom: 80px;
                z-index: 30;
                display: flex;
                flex-direction: row;
                align-items: flex-end;
                pointer-events: none;
            }}
            #slider-drawer > * {{
                pointer-events: auto;
            }}

            .drawer-toggle {{
                width: 44px;
                height: 44px;
                border-radius: 0 12px 12px 0;
                background: rgba(10, 14, 23, 0.92);
                backdrop-filter: blur(12px);
                border: 1px solid #1a2a44;
                border-left: none;
                color: #88aadd;
                cursor: pointer;
                font-size: 20px;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all 0.25s;
                box-shadow: 4px 0 20px rgba(0, 0, 0, 0.5);
            }}
            .drawer-toggle:hover {{
                background: #1a2a44;
                color: #ffffff;
                border-color: #4477aa;
                box-shadow: 4px 0 24px rgba(74, 144, 217, 0.3);
            }}
            #slider-drawer.open .drawer-toggle {{
                background: #1a3a5a;
                color: #88ccff;
                border-color: #4a90d9;
            }}

            .drawer-body {{
                width: 0;
                overflow: hidden;
                background: rgba(10, 14, 23, 0.94);
                backdrop-filter: blur(16px);
                border: 1px solid #1a2a44;
                border-left: none;
                border-radius: 0 16px 16px 0;
                padding: 0;
                transition: width 0.3s ease, padding 0.3s ease;
                box-shadow: 4px 0 30px rgba(0, 0, 0, 0.6);
                margin-left: -1px;
            }}
            #slider-drawer.open .drawer-body {{
                width: 260px;
                padding: 16px 20px;
            }}

            .drawer-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding-bottom: 10px;
                margin-bottom: 12px;
                border-bottom: 1px solid #1a2a44;
                white-space: nowrap;
            }}
            .drawer-title {{
                color: #88ccff;
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 0.03em;
            }}
            .drawer-close {{
                background: none;
                border: none;
                color: #667;
                cursor: pointer;
                font-size: 14px;
                padding: 0 4px;
                transition: 0.15s;
            }}
            .drawer-close:hover {{
                color: #ff6b6b;
            }}

            .slider-row {{
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 6px 0;
                white-space: nowrap;
            }}
            .slider-row label {{
                color: #88aadd;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                min-width: 38px;
                letter-spacing: 0.05em;
            }}
            .slider-row input[type="range"] {{
                flex: 1;
                height: 4px;
                -webkit-appearance: none;
                background: #1a2a44;
                border-radius: 2px;
                outline: none;
            }}
            .slider-row input[type="range"]::-webkit-slider-thumb {{
                -webkit-appearance: none;
                width: 14px;
                height: 14px;
                background: #88aadd;
                border-radius: 50%;
                cursor: pointer;
                box-shadow: 0 0 10px rgba(136, 170, 221, 0.4);
                transition: 0.15s;
            }}
            .slider-row input[type="range"]::-webkit-slider-thumb:hover {{
                transform: scale(1.15);
                background: #aaccee;
            }}
            .slider-row .value {{
                color: #eef2ff;
                font-size: 11px;
                font-weight: 500;
                min-width: 38px;
                text-align: right;
                font-family: 'Consolas', 'Monaco', monospace;
            }}

            /* 悬浮信息面板 */
            #info-panel {{
                position: absolute;
                top: 80px;
                right: 20px;
                width: 260px;
                background: rgba(10, 14, 23, 0.92);
                backdrop-filter: blur(12px);
                border: 1px solid #1a2a44;
                border-radius: 16px;
                padding: 16px 20px;
                color: #eef2ff;
                font-size: 13px;
                z-index: 50;
                display: none;
                box-shadow: 0 8px 32px rgba(0,0,0,0.7);
                pointer-events: auto;
                max-height: 70vh;
                overflow-y: auto;
            }}
            #info-panel .panel-title {{
                font-weight: 700;
                font-size: 16px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
            }}
            #info-panel .panel-title button {{
                background: none;
                border: none;
                color: #88aadd;
                cursor: pointer;
                font-size: 18px;
            }}
            #info-panel .panel-content {{
                font-size: 13px;
                line-height: 1.6;
            }}

            #loading-overlay {{
                position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(10,14,23,0.9); z-index: 100;
                display: flex; flex-direction: column; align-items: center; justify-content: center;
                transition: opacity 0.5s;
            }}
            #loading-overlay .spinner {{
                width: 40px; height: 40px;
                border: 3px solid #1a2a44;
                border-top-color: #88aadd;
                border-radius: 50%;
                animation: spin 0.8s linear infinite;
                margin-bottom: 20px;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
            #loading-overlay .label {{
                color: #88aadd; font-size: 16px; margin-bottom: 20px;
            }}
            #loading-overlay .bar-bg {{
                width: 300px; height: 4px; background: #1a2a44; border-radius: 2px; overflow: hidden;
            }}
            #loading-overlay .bar-fill {{
                width: 0%; height: 100%; background: linear-gradient(90deg, #4a90d9, #88aadd);
                border-radius: 2px; transition: width 0.3s;
            }}
            #loading-overlay .bar-text {{
                color: #667; font-size: 12px; margin-top: 8px;
            }}
            
            /* ============ 工具箱（与变换面板风格统一） ============ */
        .tb-fab {{
            position: absolute;
            top: 20px;
            left: 20px;
            z-index: 100;
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: 1px solid #1a2a44;
            background: rgba(10, 14, 23, 0.88);
            backdrop-filter: blur(10px);
            color: #88aadd;
            cursor: pointer;
            font-size: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 0;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
            transition: transform 0.25s, background 0.2s, border-color 0.2s, color 0.2s;
        }}
        .tb-fab:hover {{
            background: rgba(26, 42, 68, 0.95);
            border-color: #4477aa;
            color: #ffffff;
        }}
        .tb-fab.active {{
            transform: rotate(45deg);
            background: rgba(26, 42, 68, 0.95);
            border-color: #4477aa;
            color: #ffffff;
            box-shadow: 0 0 24px rgba(74, 144, 217, 0.3);
        }}
        
        .tb-panel {{
            position: absolute;
            top: 76px;
            left: 20px;
            z-index: 99;
            width: 220px;
            background: rgba(10, 14, 23, 0.88);
            backdrop-filter: blur(10px);
            border: 1px solid #1a2a44;
            border-radius: 16px;
            padding: 14px 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
            opacity: 0;
            pointer-events: none;
            transform: translateY(-8px);
            transition: opacity 0.25s, transform 0.25s;
            color: #eef2ff;
            font-family: 'Segoe UI', Arial, sans-serif;
            /* 🔥 面板内容比 iframe 高时，之前会被直接裁掉且无法滚动。
               给一个随视口收缩的高度上限 + 纵向滚动，底部选项才够得着。 */
            max-height: calc(100vh - 100px);
            overflow-y: auto;
            overscroll-behavior: contain;
            scrollbar-width: thin;
            scrollbar-color: #24405f transparent;
        }}
        .tb-panel::-webkit-scrollbar {{ width: 6px; }}
        .tb-panel::-webkit-scrollbar-thumb {{
            background: #24405f;
            border-radius: 3px;
        }}
        .tb-panel::-webkit-scrollbar-track {{ background: transparent; }}
        .tb-panel.visible {{
            opacity: 1;
            pointer-events: auto;
            transform: translateY(0);
        }}
        
        .tb-panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid #1a2a44;
        }}
        .tb-panel-title {{
            color: #88aadd;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }}
        .tb-panel-close {{
            background: none;
            border: none;
            color: #667;
            cursor: pointer;
            font-size: 14px;
            padding: 0;
            width: 22px;
            height: 22px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            transition: 0.15s;
            font-family: inherit;
        }}
        .tb-panel-close:hover {{
            background: rgba(255, 107, 107, 0.15);
            color: #ff6b6b;
        }}
        
        .tb-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 0;
        }}
        .tb-row-label {{
            color: #eef2ff;
            font-size: 13px;
            user-select: none;
        }}
        
        .tb-switch {{
            position: relative;
            display: inline-block;
            width: 38px;
            height: 22px;
            flex-shrink: 0;
        }}
        .tb-switch input {{
            opacity: 0;
            width: 0;
            height: 0;
            position: absolute;
            margin: 0;
            padding: 0;
            border: 0;
            -webkit-appearance: none;
            appearance: none;
        }}
        .tb-switch-track {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: #2a3a55;
            border-radius: 22px;
            cursor: pointer;
            transition: background 0.25s;
            display: block;
        }}
        .tb-switch-track::before {{
            content: '';
            position: absolute;
            width: 16px;
            height: 16px;
            left: 3px;
            top: 3px;
            background: #8899bb;
            border-radius: 50%;
            transition: transform 0.25s, background 0.25s;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }}
        .tb-switch input:checked + .tb-switch-track {{
            background: #2a7a4a;
        }}
        .tb-switch input:checked + .tb-switch-track::before {{
            transform: translateX(16px);
            background: #51cf66;
        }}
        
        /* ============ 6.2 第一人称漫游指示器 ============ */
        #fpv-indicator {{
            position: absolute;
            top: 70px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(10, 14, 23, 0.9);
            border: 1px solid #51cf66;
            border-radius: 20px;
            padding: 6px 18px;
            color: #51cf66;
            font-size: 13px;
            font-weight: 600;
            z-index: 100;
            display: none;
            backdrop-filter: blur(10px);
            box-shadow: 0 0 24px rgba(81, 207, 102, 0.3);
        }}
        #fpv-indicator.visible {{ display: block; }}
        #fpv-indicator span {{ color: #88aadd; font-weight: 400; margin-left: 8px; font-size: 11px; }}
        
        /* ============ 6.3 阈值告警弹窗 ============ */
        #alert-container {{
            position: absolute;
            top: 110px;
            right: 20px;
            width: 320px;
            z-index: 200;
            display: flex;
            flex-direction: column;
            gap: 10px;
            pointer-events: none;
        }}
        .alert-toast {{
            background: linear-gradient(135deg, rgba(255, 68, 68, 0.95), rgba(200, 30, 30, 0.95));
            border: 1px solid #ff4444;
            border-radius: 14px;
            padding: 12px 16px;
            color: #fff;
            box-shadow: 0 8px 32px rgba(255, 68, 68, 0.5);
            animation: alert-slide-in 0.4s ease-out;
            pointer-events: auto;
            backdrop-filter: blur(10px);
        }}
        .alert-toast.warning {{
            background: linear-gradient(135deg, rgba(252, 196, 25, 0.95), rgba(220, 160, 10, 0.95));
            border-color: #fcc419;
            box-shadow: 0 8px 32px rgba(252, 196, 25, 0.5);
        }}
        .alert-toast .alert-title {{
            font-weight: 700;
            font-size: 13px;
            margin-bottom: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .alert-toast .alert-body {{
            font-size: 12px;
            opacity: 0.95;
            line-height: 1.4;
        }}
        .alert-toast .alert-close {{
            background: none;
            border: none;
            color: #fff;
            cursor: pointer;
            font-size: 16px;
            padding: 0 4px;
            opacity: 0.7;
        }}
        .alert-toast .alert-close:hover {{ opacity: 1; }}
        @keyframes alert-slide-in {{
            from {{ opacity: 0; transform: translateX(40px); }}
            to {{ opacity: 1; transform: translateX(0); }}
        }}
        .alert-toast.fade-out {{
            animation: alert-fade-out 0.5s ease-in forwards;
        }}
        @keyframes alert-fade-out {{
            to {{ opacity: 0; transform: translateX(40px); }}
        }}
        
        /* ============ 6.9 自动导览按钮 ============ */
        .tour-btn {{
            position: absolute;
            bottom: 25px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 100;
            background: rgba(10, 14, 23, 0.88);
            border: 1px solid #9b59b6;
            border-radius: 30px;
            padding: 8px 20px;
            color: #c39bd3;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: 0.25s;
            box-shadow: 0 4px 20px rgba(155, 89, 182, 0.3);
            backdrop-filter: blur(10px);
        }}
        .tour-btn:hover {{
            background: rgba(155, 89, 182, 0.3);
            color: #fff;
            box-shadow: 0 0 30px rgba(155, 89, 182, 0.5);
        }}
        .tour-btn.active {{
            background: rgba(155, 89, 182, 0.5);
            color: #fff;
            border-color: #fff;
        }}
        #tour-progress {{
            position: absolute;
            bottom: 8px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 99;
            display: none;
            gap: 6px;
        }}
        #tour-progress.visible {{ display: flex; }}
        #tour-progress .dot {{
            width: 8px; height: 8px;
            border-radius: 50%;
            background: rgba(155, 89, 182, 0.3);
            transition: 0.3s;
        }}
        #tour-progress .dot.active {{
            background: #c39bd3;
            box-shadow: 0 0 12px #c39bd3;
            transform: scale(1.3);
        }}
        
        /* ============ 导览路径编辑面板 ============ */
        #tour-editor {{
        position: absolute;
        top: 70%;
        left: 50%;
        transform: translate(-50%, -50%) scale(0.98);
        width: 420px;
        max-height: 70vh;
        background: rgba(10, 14, 23, 0.96);
        backdrop-filter: blur(16px);
        border: 1px solid #1a2a44;
        border-radius: 18px;
        padding: 20px;
        z-index: 300;
        display: flex;
        flex-direction: column;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.8);
        /* 🔥 新增：透明化过渡 */
        opacity: 0;
        visibility: hidden;
        pointer-events: none;
        transition: opacity 0.4s ease, transform 0.4s ease, visibility 0s linear 0.4s;
    }}
    #tour-editor.visible {{
        opacity: 0.12;                    /* 🔥 鼠标移出时的透明度 */
        visibility: visible;
        pointer-events: auto;
        transform: translate(-50%, -50%) scale(1);
        transition: opacity 0.4s ease, transform 0.4s ease, visibility 0s;
    }}
    #tour-editor.visible:hover {{
        opacity: 1;                       /* 🔥 鼠标悬停时恢复完全可见 */
        box-shadow: 0 24px 80px rgba(74, 144, 217, 0.2), 0 20px 60px rgba(0, 0, 0, 0.8);
    }}
    #tour-editor.pinned {{
        opacity: 1 !important;            /* 🔥 固定模式：始终可见 */
        box-shadow: 0 24px 80px rgba(155, 89, 182, 0.25), 0 20px 60px rgba(0, 0, 0, 0.8);
    }}
    #tour-editor.pinned .pin-btn {{
        color: #c39bd3 !important;
        transform: rotate(-45deg);
    }}
    
    /* 固定按钮 */
        #tour-editor .pin-btn {{
        background: none;
        border: none;
        color: #667;
        cursor: pointer;
        font-size: 16px;
        padding: 0 6px;
        transition: 0.2s;
        line-height: 1;
        flex-shrink: 0;          
    }}
    #tour-editor .pin-btn:hover {{
        color: #c39bd3;
    }}
        #tour-editor .editor-header {{
            display: flex;
            align-items: center;
            gap: 6px;               
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid #1a2a44;
        }}
        #tour-editor .editor-title {{
            flex: 1; 
            color: #c39bd3;
            font-size: 15px;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        #tour-editor .editor-close {{
            background: none;
            border: none;
            color: #667;
            cursor: pointer;
            font-size: 20px;
            padding: 0 6px;
            line-height: 1;
            flex-shrink: 0;                    
            transition: 0.2s;
        }}
        #tour-editor .editor-close:hover {{ color: #ff6b6b; }}
        
        #tour-editor .add-stop-btn {{
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #2a4a7a, #1a3a5a);
            border: 1px solid #4a90d9;
            border-radius: 12px;
            color: #88ccff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            margin-bottom: 16px;
            transition: 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }}
        #tour-editor .add-stop-btn:hover {{
            background: linear-gradient(135deg, #4a6a9a, #2a5a7a);
            box-shadow: 0 0 24px rgba(74, 144, 217, 0.3);
            color: #fff;
        }}
        
        #tour-editor .stop-list {{
            flex: 1;
            overflow-y: auto;
            max-height: 40vh;
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding-right: 4px;
        }}
        #tour-editor .stop-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 12px;
            background: rgba(26, 42, 68, 0.5);
            border: 1px solid #1a2a44;
            border-radius: 10px;
            transition: 0.2s;
        }}
        #tour-editor .stop-item:hover {{
            background: rgba(26, 42, 68, 0.8);
            border-color: #4477aa;
        }}
        #tour-editor .stop-item .stop-index {{
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: linear-gradient(135deg, #9b59b6, #c39bd3);
            color: #fff;
            font-size: 11px;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }}
        #tour-editor .stop-item .stop-name {{
            flex: 1;
            color: #eef2ff;
            font-size: 13px;
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        #tour-editor .stop-item .stop-name:hover {{
            background: rgba(74, 144, 217, 0.15);
        }}
        #tour-editor .stop-item .stop-actions {{
            display: flex;
            gap: 3px;
            flex-shrink: 0;
        }}
        #tour-editor .stop-item .stop-actions button {{
            width: 24px;
            height: 24px;
            border-radius: 6px;
            background: rgba(26, 42, 68, 0.6);
            border: 1px solid #2a3a55;
            color: #88aadd;
            cursor: pointer;
            font-size: 12px;
            padding: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.15s;
        }}
        #tour-editor .stop-item .stop-actions button:hover {{
            background: rgba(74, 144, 217, 0.3);
            color: #fff;
        }}
        #tour-editor .stop-item .stop-actions button.danger:hover {{
            background: rgba(255, 107, 107, 0.3);
            color: #ff6b6b;
            border-color: #ff6b6b;
        }}
        #tour-editor .empty-tip {{
            text-align: center;
            color: #556;
            font-size: 13px;
            padding: 30px 0;
        }}
        #tour-editor .editor-footer {{
            display: flex;
            gap: 8px;
            margin-top: 16px;
            padding-top: 12px;
            border-top: 1px solid #1a2a44;
        }}
        #tour-editor .editor-footer button {{
            flex: 1;
            padding: 10px;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: 0.2s;
        }}
        #tour-editor .editor-footer .btn-primary {{
            background: linear-gradient(135deg, #9b59b6, #7d3c98);
            border: 1px solid #c39bd3;
            color: #fff;
        }}
        #tour-editor .editor-footer .btn-primary:hover {{
            box-shadow: 0 0 24px rgba(155, 89, 182, 0.5);
        }}
        #tour-editor .editor-footer .btn-secondary {{
            background: rgba(26, 42, 68, 0.6);
            border: 1px solid #2a3a55;
            color: #88aadd;
        }}
        #tour-editor .editor-footer .btn-secondary:hover {{
            background: rgba(255, 107, 107, 0.2);
            color: #ff6b6b;
            border-color: #ff6b6b;
        }}
        
        /* 编辑按钮 */
        #tour-edit-btn {{
            position: absolute;
            bottom: 25px;
            left: calc(50% + 90px);
            transform: translateX(-50%);
            z-index: 100;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(10, 14, 23, 0.88);
            border: 1px solid #4a90d9;
            color: #4a90d9;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.25s;
            backdrop-filter: blur(10px);
        }}
        #tour-edit-btn:hover {{
            background: rgba(74, 144, 217, 0.3);
            color: #fff;
            box-shadow: 0 0 20px rgba(74, 144, 217, 0.5);
        }}

        /* ============ 💭 思索入口（与导览按钮同排，编辑导览右侧） ============ */
        #ponder-btn {{
            position: absolute;
            bottom: 25px;
            left: calc(50% + 135px);
            transform: translateX(-50%);
            z-index: 100;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(10, 14, 23, 0.88);
            border: 1px solid #c9a227;
            color: #c9a227;
            cursor: pointer;
            font-size: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.25s;
            backdrop-filter: blur(10px);
            padding: 0;
        }}
        #ponder-btn:hover {{
            background: rgba(201, 162, 39, 0.28);
            color: #fff;
            box-shadow: 0 0 20px rgba(201, 162, 39, 0.5);
        }}
        #ponder-btn.active {{
            background: rgba(201, 162, 39, 0.42);
            color: #fff;
            box-shadow: 0 0 22px rgba(201, 162, 39, 0.65);
        }}
        
        /* ============ 任务9 时间轴回放 ============ */
        .timeline-open-btn {{
            position: absolute;
            bottom: 25px;
            left: calc(50% - 90px);
            transform: translateX(-50%);
            z-index: 100;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(10, 14, 23, 0.88);
            border: 1px solid #51cf66;
            color: #51cf66;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.25s;
            backdrop-filter: blur(10px);
        }}
        .timeline-open-btn:hover {{
            background: rgba(81, 207, 102, 0.3);
            color: #fff;
            box-shadow: 0 0 20px rgba(81, 207, 102, 0.5);
        }}
        .timeline-open-btn.active {{
            background: rgba(81, 207, 102, 0.5);
            color: #fff;
            border-color: #fff;
            box-shadow: 0 0 20px rgba(81, 207, 102, 0.7);
        }}

        #timeline-panel {{
            position: absolute;
            bottom: 70px;
            left: 50%;
            transform: translateX(-50%);
            width: 620px;
            max-width: 92%;
            background: rgba(10, 14, 23, 0.95);
            backdrop-filter: blur(16px);
            border: 1px solid #1a2a44;
            border-radius: 16px;
            padding: 12px 20px;
            z-index: 150;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.75);
        }}

        #timeline-panel .timeline-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 10px;
            margin-bottom: 10px;
            border-bottom: 1px solid #1a2a44;
        }}
        #timeline-panel .timeline-title {{
            color: #51cf66;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.05em;
        }}
        #timeline-panel .timeline-time {{
            color: #88ccff;
            font-size: 13px;
            font-family: 'Consolas', 'Monaco', monospace;
            font-weight: 600;
        }}
        #timeline-panel .timeline-close {{
            background: none;
            border: none;
            color: #667;
            cursor: pointer;
            font-size: 16px;
            padding: 0 4px;
            transition: 0.15s;
        }}
        #timeline-panel .timeline-close:hover {{
            color: #ff6b6b;
        }}

        #timeline-panel .timeline-controls {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        #timeline-panel .timeline-play {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, #2a7a4a, #1a5a3a);
            border: 1px solid #51cf66;
            color: #fff;
            font-size: 14px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.2s;
            flex-shrink: 0;
        }}
        #timeline-panel .timeline-play:hover {{
            background: linear-gradient(135deg, #3a8a5a, #2a6a4a);
            box-shadow: 0 0 20px rgba(81, 207, 102, 0.5);
        }}
        #timeline-panel .timeline-play.playing {{
            background: linear-gradient(135deg, #ff6b6b, #c0392b);
            border-color: #ff6b6b;
        }}

        #timeline-panel input[type="range"] {{
            flex: 1;
            height: 6px;
            -webkit-appearance: none;
            background: linear-gradient(90deg, #1a2a44, #2a4a6a, #51cf66);
            border-radius: 3px;
            outline: none;
            cursor: pointer;
        }}
        #timeline-panel input[type="range"]::-webkit-slider-thumb {{
            -webkit-appearance: none;
            width: 16px;
            height: 16px;
            background: #51cf66;
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 0 12px rgba(81, 207, 102, 0.8);
            transition: 0.15s;
        }}
        #timeline-panel input[type="range"]::-webkit-slider-thumb:hover {{
            transform: scale(1.2);
            background: #6eff8e;
        }}

        #timeline-panel .timeline-step {{
            color: #8899bb;
            font-size: 11px;
            font-family: 'Consolas', 'Monaco', monospace;
            min-width: 70px;
            text-align: right;
        }}
        
        /* ============ 工具箱分区（任务：设置迁移） ============ */
        .tb-section {{
            margin-bottom: 12px;
            padding-bottom: 10px;
            border-bottom: 1px solid rgba(74, 144, 217, 0.1);
        }}
        .tb-section:last-of-type {{
            border-bottom: none;
        }}
        .tb-section-title {{
            color: #88ccff;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.05em;
            margin-bottom: 6px;
            text-transform: uppercase;
            opacity: 0.85;
        }}
        .tb-divider {{
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(74,144,217,0.3), transparent);
            margin: 12px 0;
        }}
        
        /* 工具箱内的滑块 */
        .tb-slider-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 4px 0;
        }}
        .tb-slider-row input[type="range"] {{
            flex: 1;
            height: 4px;
            -webkit-appearance: none;
            background: #1a2a44;
            border-radius: 2px;
            outline: none;
        }}
        .tb-slider-row input[type="range"]::-webkit-slider-thumb {{
            -webkit-appearance: none;
            width: 14px;
            height: 14px;
            background: #88aadd;
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 0 10px rgba(136, 170, 221, 0.4);
        }}
        .tb-slider-value {{
            color: #eef2ff;
            font-size: 12px;
            font-weight: 600;
            min-width: 40px;
            text-align: right;
            font-family: 'Consolas', monospace;
        }}
        
        /* 工具箱内的操作按钮 */
        .tb-action-btn {{
            width: 100%;
            padding: 9px 14px;
            margin-top: 6px;
            border-radius: 10px;
            border: 1px solid #1a2a44;
            background: rgba(26, 42, 68, 0.6);
            color: #c8d6e5;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            font-family: inherit;
        }}
        .tb-action-btn:hover {{
            background: rgba(74, 144, 217, 0.25);
            border-color: #4a90d9;
            color: #fff;
        }}
        .tb-action-primary {{
            background: linear-gradient(135deg, rgba(74, 144, 217, 0.4), rgba(42, 90, 138, 0.4));
            border-color: rgba(74, 144, 217, 0.5);
            color: #88ccff;
        }}
        .tb-action-primary:hover {{
            background: linear-gradient(135deg, rgba(74, 144, 217, 0.6), rgba(42, 90, 138, 0.6));
            box-shadow: 0 0 20px rgba(74, 144, 217, 0.3);
        }}
        .tb-action-danger {{
            color: #ff8888;
            border-color: rgba(255, 107, 107, 0.2);
        }}
        .tb-action-danger:hover {{
            background: rgba(255, 107, 107, 0.15);
            border-color: #ff6b6b;
            color: #fff;
        }}
        /* 🔥 P4-②：视角快捷按钮（一行三个） */
        .tb-view-row {{
            display: flex;
            gap: 6px;
        }}
        .tb-view-row .tb-action-btn {{
            flex: 1;
            min-width: 0;
            padding: 6px 4px;
            font-size: 12px;
        }}
        
        /* ============ 用户手册弹窗 ============ */
        #user-manual-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(6px);
            z-index: 9999;
            display: none;
            align-items: center;
            justify-content: center;
            animation: manual-fade-in 0.25s ease;
        }}
        #user-manual-overlay.visible {{
            display: flex;
        }}
        @keyframes manual-fade-in {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}
        
        .manual-panel {{
            width: 760px;
            max-width: 92vw;
            max-height: 85vh;
            background: rgba(16, 22, 40, 0.98);
            border: 1px solid rgba(74, 144, 217, 0.3);
            border-radius: 20px;
            display: flex;
            flex-direction: column;
            box-shadow: 0 20px 80px rgba(0, 0, 0, 0.9), 0 0 60px rgba(74, 144, 217, 0.1);
            animation: manual-slide-up 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        @keyframes manual-slide-up {{
            from {{ opacity: 0; transform: translateY(30px) scale(0.96); }}
            to {{ opacity: 1; transform: translateY(0) scale(1); }}
        }}
        
        .manual-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 18px 26px;
            border-bottom: 1px solid rgba(74, 144, 217, 0.15);
            color: #88ccff;
            font-size: 18px;
            font-weight: 700;
            letter-spacing: 0.03em;
            flex-shrink: 0;
        }}
        .manual-header button {{
            background: none;
            border: none;
            color: #667;
            cursor: pointer;
            font-size: 20px;
            padding: 4px 10px;
            border-radius: 8px;
            transition: 0.2s;
            font-family: inherit;
        }}
        .manual-header button:hover {{
            background: rgba(255, 107, 107, 0.15);
            color: #ff6b6b;
        }}
        
        .manual-body {{
            flex: 1;
            overflow-y: auto;
            padding: 20px 30px 30px 30px;
            color: #c8d6e5;
            font-size: 13px;
            line-height: 1.7;
        }}
        .manual-body::-webkit-scrollbar {{
            width: 6px;
        }}
        .manual-body::-webkit-scrollbar-thumb {{
            background: rgba(74, 144, 217, 0.3);
            border-radius: 3px;
        }}
        .manual-body::-webkit-scrollbar-thumb:hover {{
            background: rgba(74, 144, 217, 0.5);
        }}
        
        .manual-section {{
            margin-bottom: 22px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(74, 144, 217, 0.08);
        }}
        .manual-section:last-of-type {{
            border-bottom: none;
        }}
        .manual-section h3 {{
            color: #88ccff;
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 10px;
            letter-spacing: 0.02em;
        }}
        .manual-section p {{
            margin: 6px 0;
            color: #8899bb;
        }}
        .manual-section ul,
        .manual-section ol {{
            margin: 6px 0 6px 20px;
            padding: 0;
        }}
        .manual-section li {{
            margin: 5px 0;
            color: #c8d6e5;
        }}
        .manual-section li strong {{
            color: #88ccff;
        }}
        .manual-section code {{
            background: rgba(74, 144, 217, 0.15);
            color: #88ccff;
            padding: 1px 6px;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 12px;
        }}
        
        .manual-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 8px 0;
        }}
        .manual-table td {{
            padding: 7px 10px;
            border-bottom: 1px solid rgba(74, 144, 217, 0.08);
            font-size: 13px;
        }}
        .manual-table td:first-child {{
            width: 45%;
            color: #88aadd;
        }}
        .manual-table td:last-child {{
            color: #c8d6e5;
        }}
        
        .manual-footer {{
            margin-top: 20px;
            padding-top: 16px;
            border-top: 1px solid rgba(74, 144, 217, 0.15);
            text-align: center;
            color: #556;
            font-size: 12px;
        }}
        .manual-footer p {{
            margin: 3px 0;
        }}
        
        /* ============ 6.6 AI语音播报按钮 ============ */
        .voice-btn {{
            position: absolute;
            bottom: 25px;
            left: calc(50% - 135px);
            transform: translateX(-50%);
            z-index: 100;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(10, 14, 23, 0.88);
            border: 1px solid #fcc419;
            color: #fcc419;
            cursor: pointer;
            font-size: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.25s;
            backdrop-filter: blur(10px);
            padding: 0;
        }}
        .voice-btn:hover {{
            background: rgba(252, 196, 25, 0.3);
            color: #fff;
            box-shadow: 0 0 20px rgba(252, 196, 25, 0.5);
        }}
        .voice-btn.speaking {{
            animation: voice-pulse 1s ease-in-out infinite;
            background: rgba(252, 196, 25, 0.5);
            color: #fff;
        }}
        @keyframes voice-pulse {{
            0%, 100% {{ box-shadow: 0 0 20px rgba(252, 196, 25, 0.5); }}
            50% {{ box-shadow: 0 0 40px rgba(252, 196, 25, 0.9); }}
        }}
        
        /* ============ LOD 性能监控 ============ */
        #fps-monitor {{
            position: absolute;
            top: 20px;
            left: 80px;
            z-index: 200;
            background: rgba(10, 14, 23, 0.85);
            backdrop-filter: blur(10px);
            border: 1px solid #1a2a44;
            border-radius: 10px;
            padding: 6px 12px;
            color: #88aadd;
            font-size: 11px;
            font-family: 'Consolas', 'Monaco', monospace;
            display: none;
            line-height: 1.5;
            pointer-events: none;
        }}
        #fps-monitor.visible {{ display: block; }}
        #fps-monitor .fps-good {{ color: #51cf66; font-weight: 700; }}
        #fps-monitor .fps-ok {{ color: #fcc419; font-weight: 700; }}
        #fps-monitor .fps-bad {{ color: #ff6b6b; font-weight: 700; }}
            
        </style>
        <!-- 「思索」教学动画样式：独立静态文件，不塞进本 f-string（见 static/js/ponder.js 顶部说明） -->
        <link rel="stylesheet" href="/app/static/css/ponder.css">
    </head>
    <body>
        <div id="container">
            <div id="info">🔄 拖拽旋转 · 滚轮缩放 · <span>点击物体</span> · 拖拽箭头移动</div>
            <div id="realtime-status" class="visible">🔴 实时数据已连接</div>
            <div id="drag-hint">📌 选中物体后拖动彩色箭头移动位置</div>
            <div id="status">⚡ {len(objects)} 个对象 · 🎨 {sum(1 for o in objects if o.get('type') == 'custom_model')} 个自定义</div>
            
            <!-- LOD 性能监控 -->
            <!-- LOD 性能监控 -->
            <div id="fps-monitor">
                <div>🎯 FPS: <span id="fps-value" class="fps-good">--</span></div>
                <div>📦 可见: <span id="lod-visible">--</span></div>
                <div>🔍 近景: <span id="lod-l2" style="color:#51cf66;">--</span> | 中景: <span id="lod-l1" style="color:#fcc419;">--</span> | 远景: <span id="lod-l0" style="color:#ff6b6b;">--</span></div>
            </div>
        
            <!-- 🔥 工具箱（Figma 风格） -->

            <button id="toolbox-btn" class="tb-fab" title="工具箱">⚙️</button>
            <div id="toolbox-panel" class="tb-panel">
                <div class="tb-panel-header">
                    <span class="tb-panel-title">🛠️ 场景工具</span>
                    <button id="toolbox-close-btn" class="tb-panel-close" title="关闭">✕</button>
                </div>
                
                <div class="tb-section">
                    <div class="tb-section-title">🎛️ 显示控制</div>
                    <div class="tb-row">
                        <span class="tb-row-label">信标光柱</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-beacon-input">
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">脉冲扩散环</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-pulse-ring-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">自动旋转</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-autorotate-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">显示网格</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-grid-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">显示标签</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-labels-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">LOD 性能优化</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-lod-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">FPS 监控</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-fps-input">
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">高负载高亮</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-highload-input">
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                </div>
                
                <!-- 🔥 P4-②：承接原自然语言「显示控制」能力 -->
                <div class="tb-section">
                    <div class="tb-section-title">📦 物体显示</div>
                    <div class="tb-row">
                        <span class="tb-row-label">树木</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-vis-tree" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">建筑</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-vis-building" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">充电桩</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-vis-charger" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">路灯</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-vis-lamp" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">车辆</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-vis-vehicle" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <button id="show-all-objs-btn" class="tb-action-btn" style="margin-top:6px;">全部显示</button>
                </div>
                
                <!-- 🔥 P4-②：承接原自然语言「视角切换」能力 -->
                <div class="tb-section">
                    <div class="tb-section-title">🎥 视角</div>
                    <div class="tb-view-row">
                        <button id="view-reset-btn" class="tb-action-btn">复位</button>
                        <button id="view-pano-btn" class="tb-action-btn">全景</button>
                        <button id="view-closeup-btn" class="tb-action-btn">特写</button>
                    </div>
                </div>
                
                <div class="tb-section">
                    <div class="tb-section-title">🔔 通知</div>
                    <div class="tb-row">
                        <span class="tb-row-label">操作提示</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-toast-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                    <div class="tb-row">
                        <span class="tb-row-label">告警提醒</span>
                        <label class="tb-switch">
                            <input type="checkbox" id="toggle-alert-input" checked>
                            <span class="tb-switch-track"></span>
                        </label>
                    </div>
                </div>
                
                <div class="tb-section">
                    <div class="tb-section-title">⚠️ 告警阈值</div>
                    <div class="tb-slider-row">
                        <input type="range" id="alert-threshold-slider" min="0.5" max="0.95" step="0.05" value="0.7">
                        <span class="tb-slider-value" id="alert-threshold-value">70%</span>
                    </div>
                </div>
                
                <div class="tb-divider"></div>
                
                <button id="open-manual-btn" class="tb-action-btn tb-action-primary">📖 用户手册</button>
                <button id="clear-cache-btn" class="tb-action-btn tb-action-danger">🗑️ 清除本地缓存</button>
            </div>
            
            <!-- 用户手册弹窗 -->
            <div id="user-manual-overlay">
                <div class="manual-panel">
                    <div class="manual-header">
                        <span>📖 用户手册</span>
                        <button id="manual-close-btn" title="关闭">✕</button>
                    </div>
                    <div class="manual-body">
                        
                        <!-- 快捷键 -->
                        <div class="manual-section">
                            <h3>⌨️ 快捷键</h3>
                            <table class="manual-table">
                                <tr><td><code>W A S D</code></td><td>第一人称漫游视角</td></tr>
                                <tr><td><code>Q / E</code></td><td>升 / 降</td></tr>
                                <tr><td><code>Shift</code></td><td>加速移动</td></tr>
                                <tr><td><code>F</code></td><td>进入 / 退出第一人称漫游</td></tr>
                                <tr><td><code>ESC</code></td><td>取消选中</td></tr>
                                <tr><td><code>Del</code></td><td>删除选中物体</td></tr>
                                <tr><td><code>双击</code></td><td>切换自动旋转</td></tr>
                            </table>
                        </div>
                        
                        <!-- 基础操作 -->
                        <div class="manual-section">
                            <h3>🎯 基础操作</h3>
                            <ul>
                                <li><strong>左键拖拽</strong>：旋转场景视角</li>
                                <li><strong>滚轮</strong>：缩放场景</li>
                                <li><strong>右键拖拽</strong>：平移场景</li>
                                <li><strong>点击物体</strong>：选中并查看详情</li>
                                <li><strong>拖拽彩色箭头</strong>：移动选中物体</li>
                                <li><strong>左下角 ☰</strong>：展开变换抽屉，精确调整位置/旋转/缩放</li>
                            </ul>
                        </div>
                        
                        <!-- 核心功能 -->
                        <div class="manual-section">
                            <h3>🚀 核心功能</h3>
                            <ul>
                                <li>🧩 <strong>组件</strong>：组件库 + 📁 模板库（一键加载典型场景：充电站、车间、办公楼）</li>
                                <li>🏠 <strong>场景</strong>：场景名 + 🌲 资产树 + 📂 场景库（已保存场景、公共市场、云端同步）</li>
                                <li>🎨 <strong>外观</strong>：4 种视觉风格 + 光照配色 + 🎬 故事导览</li>
                                <li>📊 <strong>数据</strong>：原始数据导入（CSV/GeoJSON）、场景存档、导出、🌿 碳减排</li>
                                <li>⚙️ <strong>设置</strong>：数据源接入（数据库 / InfluxDB）、MQTT</li>
                            </ul>
                        </div>
                        
                        <!-- P4-②：自然语言输入框已移除，能力迁到工具箱 -->
                        <div class="manual-section">
                            <h3>🧰 原来的 AI 指令去哪了</h3>
                            <p>自然语言输入框已移除，相关能力改为工具箱 ⚙️ 里的开关与按钮（更直观，也少一处悬浮界面）：</p>
                            <ul>
                                <li>隐藏 / 显示 树木·建筑·充电桩·路灯·车辆 → <strong>工具箱 → 📦 物体显示</strong></li>
                                <li>显示高负载 → <strong>工具箱 → 高负载高亮</strong>（阈值即工具箱里的告警阈值）</li>
                                <li>全景 / 特写 / 复位视角 → <strong>工具箱 → 🎥 视角</strong></li>
                                <li>风格与背景 → <strong>外观面板</strong>（原本就在这里）</li>
                                <li>光柱 / 扩散环 / 自动旋转 → <strong>工具箱 → 🎛️ 显示控制</strong>（原本就在这里）</li>
                                <li>统计与平均利用率 → <strong>底部指标条 / 碳减排面板</strong>（原本就在这里）</li>
                                <li>语音播报与自动导览 → <strong>右下角语音按钮 / 故事面板</strong>（原本就在这里）</li>
                            </ul>
                        </div>
                        
                        <!-- 高级功能 -->
                        <div class="manual-section">
                            <h3>🎥 高级功能</h3>
                            <ul>
                                <li>📽️ <strong>历史回放</strong>：底部绿色按钮，播放历史利用率变化</li>
                                <li>🎥 <strong>自动导览</strong>：底部紫色按钮，按预设路径漫游</li>
                                <li>✏️ <strong>编辑导览</strong>：自定义导览路径站点</li>
                                <li>🔊 <strong>语音播报</strong>：右下角图标，播报场景概况</li>
                                <li>🤖 <strong>虚拟解说员</strong>：点击场景中的蓝色小人听讲解</li>
                            </ul>
                        </div>
                        
                        <!-- 数据绑定 -->
                        <div class="manual-section">
                            <h3>🔗 数据绑定</h3>
                            <ol>
                                <li>点击场景中的<b>充电桩</b></li>
                                <li>右侧面板下拉选择<b>深圳站点 ID</b></li>
                                <li>自动显示<b>实时利用率</b>、<b>历史趋势</b>、<b>LSTM 预测</b></li>
                                <li>利用率 > 70% 时自动触发<b>告警弹窗</b></li>
                            </ol>
                        </div>
                        
                        <!-- 导出分享 -->
                        <div class="manual-section">
                            <h3>📤 导出与分享</h3>
                            <ul>
                                <li><strong>导出 HTML</strong>：左侧底部按钮，生成独立可交互 3D 网页</li>
                                <li><strong>导出 JSON</strong>：数据面板，存档备份用</li>
                                <li><strong>导出预测 CSV</strong>：右侧面板预测图下方</li>
                                <li><strong>大屏模式</strong>：场景面板入口，适合演示汇报</li>
                            </ul>
                        </div>
                        
                        <!-- 常见问题 -->
                        <div class="manual-section">
                            <h3>❓ 常见问题</h3>
                            <ul>
                                <li><b>场景为空？</b> → 从模板库加载，或检查云端连接</li>
                                <li><b>物体堆在一起？</b> → 系统会自动网格化排列导入数据</li>
                                <li><b>找不到变换滑块？</b> → 点击左下角 ☰ 图标展开</li>
                                <li><b>数据没保存？</b> → 点击左侧底部「💾 保存」按钮</li>
                            </ul>
                        </div>
                        
                        <!-- 3D 模型引用与致谢 -->
                        <div class="manual-section">
                            <h3>📦 3D 模型引用与致谢</h3>
                            <p>场景中的 GLB 模型取自以下公开素材库，均为 <strong>CC0 1.0（公共领域）</strong>授权，可自由使用与再分发；此处仍按学术规范列明出处：</p>
                            <ul>
                                <li><strong>City Kit (Industrial)</strong> 等城市建筑套件 —— 作者 <strong>Kenney</strong>（<code>kenney.nl</code>），授权 CC0 1.0</li>
                                <li><strong>City Pack</strong> —— 作者 <strong>J-Toastie</strong>，发布于 <code>poly.pizza</code>，授权 CC0（公共领域）</li>
                            </ul>
                            <p>模型经 obj2gltf / FBX2glTF / UnityGLTF 统一转换为 glTF 2.0（.glb）后本地化加载，未改动原始几何与贴图。</p>
                            <p style="opacity:0.7;">逐文件出处与完整授权清单见项目内 <code>static/models/CREDITS.md</code>。</p>
                        </div>

                        <div class="manual-footer">
                            <p>智孪 v1.0 · 数字孪生轻量化快速建模工具</p>
                            <p>2026 全国大学生数字媒体科技作品及创意竞赛</p>
                        </div>
                        
                    </div>
                </div>
            </div>
            
            <!-- 6.2 第一人称漫游指示器 -->
            <div id="fpv-indicator">🎮 第一人称漫游 <span>WASD 移动 · QE 升降 · F 退出</span></div>
            
            <!-- 6.3 阈值告警容器 -->
            <div id="alert-container"></div>
            
            <!-- 6.9 自动导览按钮 -->
            <button id="tour-btn" class="tour-btn">🎥 自动导览</button>
            <!-- 6.6 AI语音播报按钮 -->
            <button id="voice-btn" class="voice-btn" title="播报场景概况">🔊</button>
            <div id="tour-progress">
                <div class="dot" data-idx="0"></div>
                <div class="dot" data-idx="1"></div>
                <div class="dot" data-idx="2"></div>
                <div class="dot" data-idx="3"></div>
            </div>
        
            <!-- 悬浮信息面板 -->
            <div id="info-panel">
                <div class="panel-title">
                    <span id="panel-title-text">物体信息</span>
                    <button id="info-close-btn" title="关闭">✕</button>
                </div>
                <div class="panel-content" id="panel-content"></div>
            </div>
            
                        <!-- 变换抽屉（左下角） -->
            <div id="slider-drawer" class="closed">
                <button id="toggle-slider-btn" class="drawer-toggle" title="变换控制">☰</button>
                <div id="slider-panel" class="drawer-body">
                    <div class="drawer-header">
                        <span class="drawer-title">🎛️ 变换控制</span>
                        <button id="slider-close-btn" class="drawer-close" title="收起">✕</button>
                    </div>
                    <div class="slider-row">
                        <label>X</label>
                        <input type="range" id="slider-x" min="-10" max="10" step="0.05" value="0">
                        <span class="value" id="val-x">0.00</span>
                    </div>
                    <div class="slider-row">
                        <label>Y</label>
                        <input type="range" id="slider-y" min="0" max="10" step="0.05" value="0">
                        <span class="value" id="val-y">0.00</span>
                    </div>
                    <div class="slider-row">
                        <label>Z</label>
                        <input type="range" id="slider-z" min="-10" max="10" step="0.05" value="0">
                        <span class="value" id="val-z">0.00</span>
                    </div>
                    <div class="slider-row">
                        <label>Rot</label>
                        <input type="range" id="slider-rot" min="-180" max="180" step="1" value="0">
                        <span class="value" id="val-rot">0°</span>
                    </div>
                    <div class="slider-row">
                        <label>Scale</label>
                        <input type="range" id="slider-scale" min="0.1" max="3.0" step="0.05" value="1.0">
                        <span class="value" id="val-scale">1.00</span>
                    </div>
                </div>
            </div>

            <div id="loading-overlay">
                <div class="spinner"></div>
                <div class="label">⏳ 加载模型中...</div>
                <div class="bar-bg">
                    <div id="progress-bar" class="bar-fill"></div>
                </div>
                <div id="progress-text" class="bar-text">0%</div>
            </div>
            
            <!-- 任务9 时间轴回放 -->
            <div id="timeline-panel" style="display: none;">
                <div class="timeline-header">
                    <span class="timeline-title">📽️ 历史回放</span>
                    <span class="timeline-time" id="timeline-time">--</span>
                    <button class="timeline-close" id="timeline-close" title="关闭">✕</button>
                </div>
                <div class="timeline-controls">
                    <button class="timeline-play" id="timeline-play" title="播放/暂停">▶</button>
                    <input type="range" id="timeline-slider" min="0" max="199" value="0" step="1">
                    <span class="timeline-step" id="timeline-step">0 / 199</span>
                </div>
            </div>
            <button id="timeline-open-btn" class="timeline-open-btn" title="历史回放" style="display: none;">📽️</button>
            
            <!-- 6.9+ 编辑导览路径按钮 -->
            <button id="tour-edit-btn" title="编辑导览路径">✏️</button>

            <!-- 💭 思索：交互式教学入口（与导览/回放按钮同排，放在编辑导览右侧） -->
            <button id="ponder-btn" title="思索 · 交互式教学课程目录">💭</button>

            <!-- 思索课程目录弹层（由 static/js/ponder.js 动态填充，故不含 f-string 逻辑） -->
            <div id="ponder-menu" style="display: none;"></div>
            
            <!-- 导览路径编辑面板 -->
            <div id="tour-editor">
                <div class="editor-header">
                    <span class="editor-title">🎥 自定义导览路径</span>
                    <button class="pin-btn" id="tour-editor-pin" title="固定面板（鼠标移出后不透明）">📌</button>
                    <button class="editor-close" id="tour-editor-close">✕</button>
                </div>
                <button class="add-stop-btn" id="add-tour-stop">📸 保存当前视角为新站点</button>
                <div class="stop-list" id="tour-stop-list">
                    <!-- 动态填充 -->
                </div>
                <div class="editor-footer">
                    <button class="btn-secondary" id="reset-tour">🔄 恢复默认</button>
                    <button class="btn-primary" id="save-tour">▶️ 保存并播放</button>
                </div>
            </div>
        </div>

        <!-- 🔥 本地化依赖：three@0.160.0 + addons + supabase-js，断网也能跑（见 static/vendor/manifest.json） -->
        <script src="/app/static/vendor/supabase/supabase-js.umd.js"></script>
        <script type="importmap">
        {{
            "imports": {{
                "three": "/app/static/vendor/three/three.module.js",
                "three/addons/": "/app/static/vendor/three/addons/"
            }}
        }}
        </script>

        <script type="module">
            import * as THREE from 'three';
            import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
            import {{ TransformControls }} from 'three/addons/controls/TransformControls.js';
            import {{ CSS2DRenderer, CSS2DObject }} from 'three/addons/renderers/CSS2DRenderer.js';
            import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';
            import {{ EffectComposer }} from 'three/addons/postprocessing/EffectComposer.js';
            import {{ RenderPass }} from 'three/addons/postprocessing/RenderPass.js';
            import {{ UnrealBloomPass }} from 'three/addons/postprocessing/UnrealBloomPass.js';
            // 🔥 supabase-js 改为本地 UMD 单文件（自包含、无 import），不再走 importmap/CDN
            const createClient = (window.supabase && window.supabase.createClient) || null;

            // 🔥 把 THREE 及其 addons 桥接到 window，供外部经典脚本（static/js/ponder.js）使用。
            //    本脚本是 type="module"，其导入名不会挂到 window 上；而思索沙盒需要
            //    另起一套 scene/camera/OrbitControls，必须能拿到这些类。
            //    这是「模块脚本」与「经典脚本」之间唯一的一处桥接，只增不改。
            window.THREE = THREE;
            window.OrbitControls = OrbitControls;
            window.CSS2DRenderer = CSS2DRenderer;
            window.CSS2DObject = CSS2DObject;
            // 🔥 GLTFLoader 也要桥接：思索的"智能装置"要加载真实 GLB 模型
            //    （与主场景同一批 /app/static/models/*.glb），否则教学装置只能
            //    停留在抽象示意体，做不到"拆解真实设备"。
            window.GLTFLoader = GLTFLoader;
            
            // ============================================================
            // 🔥 三栏独立滚动：3D 视图固定居中
            // ============================================================
            (function fixThreeColumnLayout() {{
                function applyLayoutFix() {{
                    try {{
                        const pDoc = window.parent.document;
                        const pWin = window.parent;
            
                        // 1. 修改所有 iframe 的 sandbox
                        // ⚠️ 必须判断属性是否存在：iframe.sandbox 在没有该属性时仍是
                        //    真值（空 DOMTokenList），直接赋值会「凭空加上」一个只含
                        //    allow-top-navigation 的沙箱，反而把场景 iframe 弄坏
                        pDoc.querySelectorAll('iframe').forEach(function(iframe) {{
                            if (iframe.hasAttribute('sandbox') && !iframe.sandbox.contains('allow-top-navigation')) {{
                                iframe.sandbox = iframe.sandbox + ' allow-top-navigation';
                            }}
                        }});
            
                        // 2. 找到包含本 3D iframe 的三栏容器
                        const myIframe = window.frameElement;
                        if (!myIframe) return;
            
                        // 🔥 修复：判断是否为大屏模式（大屏模式独占整行，iframe 宽度接近视口宽度）
                        const iframeWidth = myIframe.getBoundingClientRect().width;
                        const isBigScreen = iframeWidth > pWin.innerWidth * 0.85;
            
                        if (isBigScreen) {{
                            // 大屏模式：不锁定滚动，让页面可以自然滚动查看底部指标
                            const bc = pDoc.querySelector('section.main .block-container');
                            if (bc) {{
                                bc.style.overflow = 'visible';
                                bc.style.paddingBottom = '2rem';
                                bc.style.height = 'auto';
                            }}
                            console.log('✅ 大屏模式：已恢复页面滚动');
                            return;
                        }}
            
                        let row = myIframe.closest('[data-testid="stHorizontalBlock"]');
                        if (!row) {{
                            let el = myIframe.parentElement;
                            while (el && el !== pDoc.body) {{
                                if (el.children.length === 3 && el.clientWidth > pWin.innerWidth * 0.5) {{
                                    row = el;
                                    break;
                                }}
                                el = el.parentElement;
                            }}
                        }}
                        if (!row) return;
            
                        // 缓存防止重复操作
                        const currentW = pWin.innerWidth;
                        if (row.getAttribute('data-dtt-fixed-w') === String(currentW)) return;
                        row.setAttribute('data-dtt-fixed-w', String(currentW));
            
                        const col = myIframe.closest('[data-testid="stColumn"]');
            
                        // 三栏容器：锁定高度，禁止整体滚动
                        row.style.height = 'calc(100vh - 70px)';
                        row.style.overflow = 'hidden';
                        row.style.alignItems = 'flex-start';
            
                        // 每一列独立滚动
                        Array.from(row.children).forEach(function(c) {{
                            c.style.height = 'calc(100vh - 70px)';
                            c.style.overflowY = 'auto';
                            c.style.overflowX = 'hidden';
                            c.style.paddingRight = '8px';
                            c.style.boxSizing = 'border-box';
                        }});
            
                        // 中间列禁止滚动
                        if (col) {{
                            col.style.overflow = 'hidden';
                            col.style.paddingRight = '0';
                            col.style.display = 'flex';
                            col.style.flexDirection = 'column';
                        }}
            
                        // 外层容器禁止滚动
                        const bc = pDoc.querySelector('section.main .block-container');
                        if (bc) {{
                            bc.style.overflow = 'hidden';
                            bc.style.paddingBottom = '0.5rem';
                        }}
            
                        console.log('✅ 三栏独立滚动已应用');
                    }} catch (e) {{
                        console.warn('DTT layout fix error:', e);
                    }}
                }}
            
                // 首次执行
                applyLayoutFix();
            
                // 定时兜底（Streamlit rerun 后 DOM 会重建）
                // 只在必要时执行（每次执行前检查 DOM 是否变化）
                let _lastLayoutHash = '';
                function _safeLayoutFix() {{
                    const iframeCount = window.parent.document.querySelectorAll('iframe').length;
                    const hash = iframeCount + '_' + window.parent.innerWidth;
                    if (hash !== _lastLayoutHash) {{
                        _lastLayoutHash = hash;
                        applyLayoutFix();
                    }}
                }}
                setInterval(_safeLayoutFix, 3000);  // 频率降低到 3 秒
            
                // 监听 DOM 变化
                try {{
                    const observer = new MutationObserver(function() {{
                        setTimeout(applyLayoutFix, 100);
                    }});
                    observer.observe(window.parent.document.body, {{ childList: true, subtree: true }});
                }} catch (e) {{}}
            
                // 窗口 resize 时清除缓存
                try {{
                    window.parent.addEventListener('resize', function() {{
                        try {{
                            window.parent.document.querySelectorAll('[data-dtt-fixed-w]').forEach(function(el) {{
                                el.removeAttribute('data-dtt-fixed-w');
                            }});
                            applyLayoutFix();
                        }} catch (e) {{}}
                    }});
                }} catch (e) {{}}
            }})();
            
            
            // ---------- 全局工具函数（供 HTML onclick 调用） ----------
            window.jumpToUrl = function(objectId) {{
                // 🔥 沙箱禁止 iframe 导航，改为纯前端处理
                if (!objectId) return;
                // 可选：在前端高亮一下对应物体
                if (typeof highlightRelationsFor === 'function') {{
                    highlightRelationsFor(objectId);
                }}
            }};


            // ---------- 数据 ----------
            const sceneData = {scene_json};
            const compDefs = {comp_defs_json};
            const objects = sceneData.objects || [];
            const selectedId = sceneData.selectedId || '';
            const hasSupabase = sceneData.hasSupabase || false;
            const sceneId = {scene_id_escaped};
            // 🔥 P3：告警阈值改为单一真值——唯一存储是 localStorage（工具箱滑块写入）。
            // 原先这里读 sceneData.alertThreshold（来自 Python，恒为 0.7），而真正生效的
            // 是 localStorage 那份，于是告警弹窗显示的阈值与实际判断阈值不一致。
            const DEFAULT_ALERT_THRESHOLD = 0.7;
            function getAlertThreshold() {{
                const v = parseFloat(localStorage.getItem('alert_threshold'));
                return Number.isFinite(v) ? v : DEFAULT_ALERT_THRESHOLD;
            }}
            
            let tourBtn = null;
            let tourProgress = null;
            let tourDots = [];

            console.log(`✅ 加载了 ${{objects.length}} 个对象`);

            // ---------- 本地存储 ----------
            const STORAGE_KEY = sceneId ? `scene_${{sceneId}}` : 'scene_default';

            function loadFromLocalStorage() {{
                try {{
                    const stored = localStorage.getItem(STORAGE_KEY);
                    if (stored) {{
                        const parsed = JSON.parse(stored);
                        if (Array.isArray(parsed) && parsed.length > 0) {{
                            console.log(`✅ 从 localStorage 加载 ${{parsed.length}} 个对象`);
                            return parsed;
                        }}
                    }}
                }} catch (e) {{
                    console.warn('⚠️ 读取 localStorage 失败:', e);
                }}
                return null;
            }}

            function saveToLocalStorage(objectsData) {{
                try {{
                    const toStore = objectsData.map(obj => ({{
                        id: obj.id,
                        type: obj.type,
                        name: obj.name,
                        position: obj.position,
                        rotation: obj.rotation,
                        scale: obj.scale,
                        bind_station_id: obj.bind_station_id || null,
                        custom_props: obj.custom_props || {{}},
                        utilization: obj.utilization || 0.5,
                    }}));
                    localStorage.setItem(STORAGE_KEY, JSON.stringify(toStore));
                    console.log('💾 已保存到 localStorage');
                }} catch (e) {{
                    console.warn('⚠️ 保存到 localStorage 失败:', e);
                }}
            }}

            // upsert 可用性：null=尚未探测 / true=有唯一索引 / false=退化为插入+清理
            let _upsertMode = null;

            async function syncToSupabase(objectsData) {{
                if (!hasSupabase || !sceneId) return;
                // 🔥 非破坏式同步 + 自动升级：
                //   · 数据库若有 (scene_id, object_uuid) 唯一索引 → upsert 就地更新，
                //     旧行只在「本地已删除」时才清理，完全不产生重复行；
                //   · 若还没有该索引（PostgreSQL 报 42P10）→ 自动退化为「先插入新行、
                //     再删除旧行」：任何时刻都有一份完整数据，插入失败也不丢数据。
                //   迁移脚本见 sql/001_scene_objects_unique_index.sql。
                try {{
                    // 1) 取现有行（必须分页：PostgREST 单次上限 1000 行）
                    const existing = [];
                    let from = 0;
                    while (true) {{
                        const r = await supabaseClient
                            .from('scene_objects')
                            .select('id,object_uuid')
                            .eq('scene_id', sceneId)
                            .range(from, from + 999);
                        if (r && r.error) {{
                            console.error('❌ 读取旧行失败，已中止同步（未做任何写入）:', r.error);
                            return;
                        }}
                        const rows = (r && r.data) ? r.data : [];
                        rows.forEach(x => existing.push(x));
                        if (rows.length < 1000) break;
                        from += 1000;
                    }}

                    const payload = objectsData.map(obj => ({{
                        scene_id: sceneId,
                        object_uuid: obj.id,
                        type: obj.type,
                        name: obj.name,
                        position: obj.position,
                        rotation: obj.rotation,
                        scale: obj.scale,
                        bind_station_id: obj.bind_station_id || null,
                        custom_props: obj.custom_props || {{}},
                        utilization: obj.utilization || 0.5,
                    }}));
                    const currentIds = new Set(objectsData.map(o => o.id));

                    // 2) 写入
                    let mode = _upsertMode;
                    if (payload.length > 0) {{
                        if (mode !== false) {{
                            const upRes = await supabaseClient
                                .from('scene_objects')
                                .upsert(payload, {{ onConflict: 'scene_id,object_uuid' }});
                            if (!upRes.error) {{
                                mode = true;
                            }} else if (upRes.error.code === '42P10') {{
                                mode = false;
                                console.warn('ℹ️ 未找到唯一索引 (scene_id, object_uuid)，改用兼容写入路径；建议执行 sql/001_scene_objects_unique_index.sql');
                            }} else {{
                                console.error('❌ upsert 失败，本次同步中止（旧数据保留）:', upRes.error);
                                return;
                            }}
                        }}
                        if (mode === false) {{
                            const insRes = await supabaseClient.from('scene_objects').insert(payload);
                            if (insRes && insRes.error) {{
                                console.error('❌ 插入失败，旧数据已原样保留，本次同步中止:', insRes.error);
                                return;
                            }}
                        }}
                    }}
                    _upsertMode = mode;

                    // 3) 清理多余旧行
                    //    upsert 模式 → 只删「本地已不存在」的行（同 uuid 的已被就地更新）
                    //    兼容模式   → 旧行全部删除（新数据已插入，不删就会重复）
                    const toDelete = (mode === true)
                        ? existing.filter(x => !currentIds.has(x.object_uuid))
                        : existing;
                    const delIds = toDelete.map(x => x.id);
                    for (let i = 0; i < delIds.length; i += 200) {{
                        const batch = delIds.slice(i, i + 200);
                        const delRes = await supabaseClient
                            .from('scene_objects')
                            .delete()
                            .in_('id', batch);
                        if (delRes && delRes.error) {{
                            console.warn('⚠️ 旧行删除失败（下次同步会自动清理）:', delRes.error);
                        }}
                    }}
                    console.log('☁️ 已同步（' + (mode === true ? 'upsert' : '插入+清理') + '）：写入 ' + payload.length + ' 行，删除旧行 ' + delIds.length + ' 行');
                }} catch (e) {{
                    console.warn('⚠️ 同步到 Supabase 异常:', e);
                }}
            }}

            // ---------- 持久化调度（P1.5）----------
            // 原问题：变换滑块绑的是 'input'，拖动中每个像素都触发 saveToLocalStorage
            // + syncToSupabase，而后者是「先删整场景、再整场景插入」→ 一次拖动产生
            // 几十次全量删插。既是卡顿来源，也是数据丢失窗口。
            // 方案：本地 mesh 依旧即时跟手；持久化改为 400ms 尾防抖 + 松手立即提交，
            // 并且串行化，避免两次删插互相穿插。
            let _persistTimer = null;
            let _syncInFlight = false;
            let _syncPending = false;

            async function persistNow() {{
                if (_syncInFlight) {{ _syncPending = true; return; }}
                _syncInFlight = true;
                try {{
                    saveToLocalStorage(objects);
                    await syncToSupabase(objects);
                }} finally {{
                    _syncInFlight = false;
                    if (_syncPending) {{ _syncPending = false; persistNow(); }}
                }}
            }}

            function persistDebounced(delay = 400) {{
                clearTimeout(_persistTimer);
                _persistTimer = setTimeout(() => {{ persistNow(); }}, delay);
            }}
            // ---------- 兜底：若服务器场景为空，清空所有 scene_ 缓存 ----------
            if (objects.length === 0) {{
                const keysToRemove = [];
                for (let i = 0; i < localStorage.length; i++) {{
                    const key = localStorage.key(i);
                    if (key && (key.startsWith('scene_') || key.startsWith('deleted_'))) {{
                        keysToRemove.push(key);
                    }}
                }}
                keysToRemove.forEach(k => localStorage.removeItem(k));
                console.log('🧹 空场景：已清除所有缓存');
            }}

            // ---------- 合并数据 ----------
            const localData = loadFromLocalStorage();
            if (localData) {{
                // 只用 localStorage 覆盖已有对象的位置/旋转/缩放
                const localMap = new Map();
                localData.forEach(obj => localMap.set(obj.id, obj));
                objects.forEach(obj => {{
                    const local = localMap.get(obj.id);
                    if (local) {{
                        // 只覆盖位置/旋转/缩放，其他字段以服务器为准
                        obj.position = local.position || obj.position;
                        obj.rotation = local.rotation || obj.rotation;
                        obj.scale = local.scale || obj.scale;
                    }}
                }});
                console.log('📂 已用本地缓存覆盖位置数据（不新增对象）');
            }} else {{
                console.log('📂 无本地缓存，使用服务器数据');
            }}

            // ---------- Supabase 客户端 ----------
            // 🔥 可降级：本地 supabase-js 没加载 / secrets 未配置 / 无网络时，
            //    只关闭「实时数据」，3D 场景和其余功能照常工作
            const supabaseUrl = {supabase_url_escaped};
            const supabaseAnonKey = {supabase_anon_key_escaped};
            const SUPABASE_LIB_READY = typeof createClient === 'function';
            let supabaseClient = null;
            if (!SUPABASE_LIB_READY) {{
                console.warn('⚠️ 本地 supabase-js 未加载，实时数据功能已关闭（3D 场景不受影响）');
            }} else if (hasSupabase && supabaseUrl && supabaseAnonKey) {{
                try {{
                    supabaseClient = createClient(supabaseUrl, supabaseAnonKey);
                    console.log('✅ Supabase 客户端初始化成功');
                }} catch (e) {{
                    console.warn('⚠️ Supabase 客户端初始化失败:', e);
                }}
            }} else {{
                console.log('ℹ️ 未配置 Supabase（secrets 缺失），实时数据功能已关闭');
            }}

            // ---------- 场景 ----------
            const container = document.getElementById('container');
            const width = container.clientWidth || window.innerWidth;
            const height = container.clientHeight || window.innerHeight;

            const scene = new THREE.Scene();

            const style = sceneData.style || {{}};

            // 🔥 渐变天空盒（ShaderMaterial，无需外部资源）
            const skyTopColor = new THREE.Color(style.scene_bg || 0x0a1a3a);   // 顶部：深蓝
            const skyBottomColor = new THREE.Color(0x1a2a4a);                   // 底部：略亮的蓝
            const skyGeo = new THREE.SphereGeometry(500, 32, 15);
            const skyMat = new THREE.ShaderMaterial({{
                uniforms: {{
                    topColor: {{ value: skyTopColor }},
                    bottomColor: {{ value: skyBottomColor }},
                    offset: {{ value: 33 }},
                    exponent: {{ value: 0.6 }}
                }},
                vertexShader: `
                    varying vec3 vWorldPosition;
                    void main() {{
                        vec4 worldPosition = modelMatrix * vec4(position, 1.0);
                        vWorldPosition = worldPosition.xyz;
                        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
                    }}
                `,
                fragmentShader: `
                    uniform vec3 topColor;
                    uniform vec3 bottomColor;
                    uniform float offset;
                    uniform float exponent;
                    varying vec3 vWorldPosition;
                    void main() {{
                        float h = normalize(vWorldPosition + offset).y;
                        gl_FragColor = vec4(
                            mix(bottomColor, topColor, max(pow(max(h, 0.0), exponent), 0.0)),
                            1.0
                        );
                    }}
                `,
                side: THREE.BackSide
            }});
            const sky = new THREE.Mesh(skyGeo, skyMat);
            sky.name = 'gradient-sky';
            scene.add(sky);

            // 雾效保留
            const fogColor = style.fog_color || 0x0a0e17;
            scene.fog = new THREE.Fog(fogColor, 30, 70);

            const camera = new THREE.PerspectiveCamera(40, width/height, 0.1, 200);
            camera.position.set(12, 10, 15);

            const renderer = new THREE.WebGLRenderer({{ antialias: true }});
            renderer.setSize(width, height);
            renderer.shadowMap.enabled = true;
            renderer.shadowMap.type = THREE.PCFSoftShadowMap;
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
            container.appendChild(renderer.domElement);

            const labelRenderer = new CSS2DRenderer();
            labelRenderer.setSize(width, height);
            labelRenderer.domElement.style.position = 'absolute';
            labelRenderer.domElement.style.top = '0';
            labelRenderer.domElement.style.left = '0';
            labelRenderer.domElement.style.pointerEvents = 'none';
            container.appendChild(labelRenderer.domElement);

                        const controls = new OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.08;
            controls.autoRotate = true;
            controls.autoRotateSpeed = 0.4;
            controls.target.set(0, 1.5, 0);
            controls.maxPolarAngle = Math.PI / 2.1;
            controls.minDistance = 3;
            controls.maxDistance = 50;

            // ---------- 相机位置持久化（localStorage） ----------
            try {{
                const savedCam = localStorage.getItem('dtt_camera_state');
                if (savedCam) {{
                    const cam = JSON.parse(savedCam);
                    if (cam.position) camera.position.set(cam.position.x, cam.position.y, cam.position.z);
                    if (cam.target) controls.target.set(cam.target.x, cam.target.y, cam.target.z);
                    controls.update();
                    console.log('📷 已恢复相机位置');
                }}
            }} catch (e) {{
                console.warn('恢复相机位置失败:', e);
            }}

            // 监听相机变化，节流保存（500ms）
            let _camSaveTimer = null;
            function _saveCamState() {{
                if (_camSaveTimer) return;
                _camSaveTimer = setTimeout(() => {{
                    try {{
                        const state = {{
                            position: {{ x: camera.position.x, y: camera.position.y, z: camera.position.z }},
                            target: {{ x: controls.target.x, y: controls.target.y, z: controls.target.z }}
                        }};
                        localStorage.setItem('dtt_camera_state', JSON.stringify(state));
                    }} catch (e) {{}}
                    _camSaveTimer = null;
                }}, 500);
            }}
            controls.addEventListener('change', _saveCamState);

            // ============================================================
            // 「思索」教学运行时挂载点
            // ------------------------------------------------------------
            // ponder.js 是独立外部脚本，看不到本模块作用域里的
            // scene / camera / controls 等变量，因此在这里暴露一个显式接口。
            // 刻意「只读为主」：主场景的 transform 权威仍在本脚本内，
            // 教学动画跑在自己的沙盒里，不需要也不允许改主场景。
            // ============================================================
            window.__DTT__ = {{
                container: container,
                scene: scene,
                camera: camera,
                controls: controls,
                renderer: renderer,
                labelRenderer: labelRenderer,
                THREE: THREE,
                // 延迟求值：这些标识符在后面的代码里才初始化
                getObjectMeshes: () => objectMeshes,
                getSceneData: () => sceneData,
                getSelectedId: () => selectedId,
                isPonderActive: false,
                setPonderActive: function (on) {{
                    window.__DTT__.isPonderActive = !!on;
                    // 🔥 冻结主场景交互与自动旋转，避免教学时镜头被抢
                    try {{
                        if (controls) {{
                            if (on) {{
                                controls.autoRotate = false;
                                controls.enabled = false;
                            }} else {{
                                controls.enabled = true;
                                controls.update();
                            }}
                        }}
                    }} catch (e) {{ console.warn('切换思索模式失败:', e); }}
                }}
            }};

            const story = sceneData.story || null;
            if (story && story.id) {{
                setTimeout(() => {{
                    if (story.camera_position) {{
                        const pos = story.camera_position;
                        camera.position.set(pos.x, pos.y, pos.z);
                        if (story.camera_target) controls.target.set(story.camera_target.x, story.camera_target.y, story.camera_target.z);
                        controls.update();
                    }}
                    if (story.auto_rotate !== undefined) controls.autoRotate = story.auto_rotate;
                }}, 200);
            }}

            const transformControls = new TransformControls(camera, renderer.domElement);
            transformControls.setMode('translate');
            transformControls.setSize(0.8);
            scene.add(transformControls);

            transformControls.addEventListener('dragging-changed', (event) => {{
                controls.enabled = !event.value;
                if (!event.value) {{
                    const obj = transformControls.object;
                    if (obj && obj.userData && obj.userData.objectId) {{
                        const objIndex = objects.findIndex(o => o.id === obj.userData.objectId);
                        if (objIndex !== -1) {{
                            const pos = obj.position;
                            const rot = obj.rotation;
                            const scl = obj.scale;
                            objects[objIndex].position = {{ x: pos.x, y: pos.y, z: pos.z }};
                            objects[objIndex].rotation = {{ x: rot.x, y: rot.y, z: rot.z }};
                            objects[objIndex].scale = {{ x: scl.x, y: scl.y, z: scl.z }};
                        }}
                    }}
                    // 🔥 P1.5：改走串行化持久化（拖拽结束只触发一次）
                    persistNow();
                }}
            }});
            
            // ===== Y 轴地界限制：拖拽过程中实时检测 =====
            transformControls.addEventListener('objectChange', () => {{
                const obj = transformControls.object;
                if (!obj) return;
                // 不允许移到地下
                if (obj.position.y < 0) {{
                    obj.position.y = 0;
                }}
            }});

            // ---------- 滑块 ----------
            // ===== 变换抽屉：变量声明 =====
            const sliderDrawer = document.getElementById('slider-drawer');
            const toggleBtn = document.getElementById('toggle-slider-btn');
            const closeBtn = document.getElementById('slider-close-btn');
            const infoCloseBtn = document.getElementById('info-close-btn');
            const sliderX = document.getElementById('slider-x');
            const sliderY = document.getElementById('slider-y');
            const sliderZ = document.getElementById('slider-z');
            const sliderRot = document.getElementById('slider-rot');
            const sliderScale = document.getElementById('slider-scale');
            const valX = document.getElementById('val-x');
            const valY = document.getElementById('val-y');
            const valZ = document.getElementById('val-z');
            const valRot = document.getElementById('val-rot');
            const valScale = document.getElementById('val-scale');

            let currentObjectId = null;
            let currentObjectGroup = null;

            // ===== 抽屉开关 =====
            if (localStorage.getItem('sliderDrawerOpen') === 'true') {{
                sliderDrawer.classList.remove('closed');
                sliderDrawer.classList.add('open');
            }}

            toggleBtn.addEventListener('click', () => {{
                const isOpen = sliderDrawer.classList.contains('open');
                if (isOpen) {{
                    sliderDrawer.classList.remove('open');
                    sliderDrawer.classList.add('closed');
                    localStorage.setItem('sliderDrawerOpen', 'false');
                }} else {{
                    sliderDrawer.classList.remove('closed');
                    sliderDrawer.classList.add('open');
                    localStorage.setItem('sliderDrawerOpen', 'true');
                }}
            }});

            closeBtn.addEventListener('click', () => {{
                sliderDrawer.classList.remove('open');
                sliderDrawer.classList.add('closed');
                localStorage.setItem('sliderDrawerOpen', 'false');
            }});

            if (infoCloseBtn) {{
                infoCloseBtn.addEventListener('click', () => {{
                    hideInfoPanel();
                }});
            }}

            // ===== 滑块 → 物体 同步 =====
            function updateSlidersFromObject(group) {{
                if (!group || group.isInstancedMesh) return;
                const pos = group.position;
                const rot = group.rotation;
                const scl = group.scale;
                sliderX.value = pos.x.toFixed(3);
                sliderY.value = pos.y.toFixed(3);
                sliderZ.value = pos.z.toFixed(3);
                valX.textContent = pos.x.toFixed(2);
                valY.textContent = pos.y.toFixed(2);
                valZ.textContent = pos.z.toFixed(2);
                const deg = THREE.MathUtils.radToDeg(rot.y);
                sliderRot.value = deg.toFixed(1);
                valRot.textContent = deg.toFixed(1) + '°';
                const avgScale = (scl.x + scl.y + scl.z) / 3;
                sliderScale.value = avgScale.toFixed(3);
                valScale.textContent = avgScale.toFixed(2);
            }}

            function updateObjectFromSliders(group) {{
                if (!group || group.isInstancedMesh) return;
                const x = parseFloat(sliderX.value);
                let y = parseFloat(sliderY.value);
                const z = parseFloat(sliderZ.value);
                const rotDeg = parseFloat(sliderRot.value);
                const scaleVal = parseFloat(sliderScale.value);
                if (y < 0) {{
                    y = 0;
                    sliderY.value = 0;
                }}
                group.position.set(x, y, z);
                group.rotation.y = THREE.MathUtils.degToRad(rotDeg);
                group.scale.set(scaleVal, scaleVal, scaleVal);
                valX.textContent = x.toFixed(2);
                valY.textContent = y.toFixed(2);
                valZ.textContent = z.toFixed(2);
                valRot.textContent = rotDeg.toFixed(1) + '°';
                valScale.textContent = scaleVal.toFixed(2);
            }}

            const sliderChange = () => {{
                if (currentObjectGroup && !currentObjectGroup.isInstancedMesh) {{
                    updateObjectFromSliders(currentObjectGroup);
                    const objIndex = objects.findIndex(o => o.id === currentObjectId);
                    if (objIndex !== -1) {{
                        const pos = currentObjectGroup.position;
                        const rot = currentObjectGroup.rotation;
                        const scl = currentObjectGroup.scale;
                        objects[objIndex].position = {{ x: pos.x, y: pos.y, z: pos.z }};
                        objects[objIndex].rotation = {{ x: rot.x, y: rot.y, z: rot.z }};
                        objects[objIndex].scale = {{ x: scl.x, y: scl.y, z: scl.z }};
                    }}
                    // 🔥 P1.5：原先每个 input 事件都整场景删+插（拖一次几十回），
                    //    现在改为 400ms 尾防抖；松手由下面的 change 监听立即提交。
                    //    本地 mesh 已在 updateObjectFromSliders() 即时更新，跟手不受影响。
                    persistDebounced();
                }}
            }};

            // 松手（change）= 立即提交，确保最终值一定落盘
            const sliderCommit = () => {{
                if (currentObjectGroup && !currentObjectGroup.isInstancedMesh) {{
                    persistNow();
                }}
            }};
            sliderX.addEventListener('input', sliderChange);
            sliderY.addEventListener('input', sliderChange);
            sliderZ.addEventListener('input', sliderChange);
            sliderRot.addEventListener('input', sliderChange);
            sliderScale.addEventListener('input', sliderChange);
            sliderX.addEventListener('change', sliderCommit);
            sliderY.addEventListener('change', sliderCommit);
            sliderZ.addEventListener('change', sliderCommit);
            sliderRot.addEventListener('change', sliderCommit);
            sliderScale.addEventListener('change', sliderCommit);

            // ---------- GLTF 加载 ----------
            const loader = new GLTFLoader();
            const modelCache = new Map();
            // 🔥 按 type 缓存 autoScale：包围盒与目标尺寸只与模型类型有关（与具体物体无关）。
            //    原先每个物体都算一次 Box3.setFromObject 并打 4 行日志 —— 1000 物体时
            //    就是 1000 次全网格遍历 + 4000 行 console.log（日志本身也会拖慢渲染）。
            const autoScaleByType = new Map();
            let loadedModels = 0;
            let totalModels = 0;

            function loadModel(url) {{
                return new Promise((resolve) => {{
                    if (modelCache.has(url)) {{
                        resolve(modelCache.get(url));
                        return;
                    }}
                    totalModels++;
                    let resolved = false;
                    const timeoutId = setTimeout(() => {{
                        if (!resolved) {{
                            resolved = true;
                            loadedModels++;
                            updateProgress();
                            console.warn('⚠️ 模型加载超时，使用程序化生成:', url);
                            resolve(null);
                        }}
                    }}, 3000);
                    loader.load(
                        url,
                        (gltf) => {{
                            if (!resolved) {{
                                resolved = true;
                                clearTimeout(timeoutId);
                    
                                // 🔥 修复：克隆原始场景，彻底重置变换，确保 bbox 计算准确
                                const model = gltf.scene.clone();
                                model.position.set(0, 0, 0);
                                model.rotation.set(0, 0, 0);
                                model.scale.set(1, 1, 1);
                                model.updateMatrixWorld(true);
                    
                                model.traverse((child) => {{
                                    if (child.isMesh) {{
                                        child.castShadow = true;
                                        child.receiveShadow = true;
                    
                                        // 🔥 修复：克隆材质，避免多个实例共享材质导致颜色/发光互相影响
                                        if (child.material) {{
                                            if (Array.isArray(child.material)) {{
                                                child.material = child.material.map(m => m.clone());
                                            }} else {{
                                                child.material = child.material.clone();
                                            }}
                                            const mats = Array.isArray(child.material) ? child.material : [child.material];
                                            mats.forEach(m => {{
                                                if (m.isMeshStandardMaterial) {{
                                                    if (m.metalness === undefined || m.metalness === 0) m.metalness = 0.3;
                                                    if (m.roughness === undefined || m.roughness === 1) m.roughness = 0.4;
                                                }}
                                            }});
                                        }}
                                    }}
                                }});
                    
                                modelCache.set(url, model);
                                loadedModels++;
                                updateProgress();
                                console.log(`✅ 模型加载成功: ${{url}}`);
                                resolve(model);
                            }}
                        }},
                    );
                }});
            }}

            function updateProgress(percent) {{
                const bar = document.getElementById('progress-bar');
                const text = document.getElementById('progress-text');
                if (percent !== undefined) {{
                    const p = Math.min(100, percent);
                    bar.style.width = p + '%';
                    text.textContent = Math.round(p) + '%';
                }} else {{
                    const p = Math.min(100, (loadedModels / totalModels) * 100);
                    bar.style.width = p + '%';
                    text.textContent = Math.round(p) + '%';
                    if (loadedModels >= totalModels && totalModels > 0) {{
                        setTimeout(() => {{
                            const overlay = document.getElementById('loading-overlay');
                            overlay.style.opacity = '0';
                            setTimeout(() => overlay.style.display = 'none', 500);
                        }}, 300);
                    }}
                }}
            }}

            // ---------- 程序化生成 ----------
            function getColor(hex) {{
                return new THREE.Color(hex);
            }}

            function createCharger(params) {{
                const group = new THREE.Group();
                const color = getColor(params.color || '#4a90d9');
                const height = params.height || 2.0;
                const width = params.width || 0.3;

                // ===== L0：主柱体（所有 LOD 级别都保留） =====
                const baseGeo = new THREE.CylinderGeometry(width * 0.7, width * 0.9, 0.12, 6);
                const baseMat = new THREE.MeshStandardMaterial({{ color: 0x4a6a8a, roughness: 0.5, metalness: 0.3 }});
                const base = new THREE.Mesh(baseGeo, baseMat);
                base.position.y = 0.06;
                base.castShadow = true;
                base.receiveShadow = true;
                base.userData.lodLevel = 0;   // 🔥 保留：所有级别
                group.add(base);

                const pillarGeo = new THREE.BoxGeometry(width, height, width);
                const pillarMat = new THREE.MeshStandardMaterial({{
                    color: color,
                    emissive: color,
                    emissiveIntensity: 0.1,
                    roughness: 0.3,
                    metalness: 0.2
                }});
                const pillar = new THREE.Mesh(pillarGeo, pillarMat);
                pillar.position.y = height / 2 + 0.12;
                pillar.castShadow = true;
                pillar.receiveShadow = true;
                pillar.userData.lodLevel = 0;
                pillar.userData.isMainPillar = true;   // 标记主柱体
                group.add(pillar);

                // ===== L1：细节元素（中景以下隐藏） =====
                for (let i = 0; i < 4; i++) {{
                    const stripe = new THREE.Mesh(
                        new THREE.BoxGeometry(width * 1.1, 0.02, 0.05),
                        new THREE.MeshStandardMaterial({{ color: 0x6a8a9a, metalness: 0.6, roughness: 0.3 }})
                    );
                    stripe.position.set(0, 0.3 + i * 0.35, width / 2 + 0.01);
                    stripe.userData.lodLevel = 1;   // 🔥 中景以下隐藏
                    group.add(stripe);
                    const stripe2 = stripe.clone();
                    stripe2.position.z = -width / 2 - 0.01;
                    stripe2.userData.lodLevel = 1;
                    group.add(stripe2);
                }}

                const screen = new THREE.Mesh(
                    new THREE.PlaneGeometry(width * 0.6, height * 0.25),
                    new THREE.MeshStandardMaterial({{
                        color: 0x00ccff,
                        emissive: 0x00ccff,
                        emissiveIntensity: 0.3,
                        transparent: true,
                        opacity: 0.6,
                        side: THREE.DoubleSide
                    }})
                );
                screen.position.set(0, height * 0.6, width / 2 + 0.01);
                screen.userData.lodLevel = 1;
                group.add(screen);

                const gunGroup = new THREE.Group();
                const gunBody = new THREE.Mesh(
                    new THREE.CylinderGeometry(0.05, 0.07, 0.2, 8),
                    new THREE.MeshStandardMaterial({{ color: 0x6a8a9a, metalness: 0.5, roughness: 0.3 }})
                );
                gunBody.rotation.x = Math.PI / 2;
                gunBody.position.set(0, 0, 0.1);
                gunGroup.add(gunBody);
                const gunTip = new THREE.Mesh(
                    new THREE.SphereGeometry(0.04, 6, 6),
                    new THREE.MeshStandardMaterial({{ color: 0xff6633, emissive: 0xff4400, emissiveIntensity: 0.2 }})
                );
                gunTip.position.set(0, 0, 0.22);
                gunGroup.add(gunTip);
                gunGroup.position.set(width * 0.5, height * 0.7, width * 0.5);
                gunGroup.rotation.z = -0.3;
                gunGroup.userData.lodLevel = 1;
                group.add(gunGroup);

                const curve = new THREE.CatmullRomCurve3([
                    new THREE.Vector3(width * 0.5, height * 0.5, width * 0.5),
                    new THREE.Vector3(width * 0.8, height * 0.3, width * 0.8),
                    new THREE.Vector3(width * 0.5, 0.1, width * 0.5)
                ]);
                const tubeGeo = new THREE.TubeGeometry(curve, 12, 0.025, 6, false);
                const tubeMat = new THREE.MeshStandardMaterial({{ color: 0x555555, roughness: 0.9 }});
                const tube = new THREE.Mesh(tubeGeo, tubeMat);
                tube.userData.lodLevel = 1;
                group.add(tube);

                // ===== L0：顶部光环（保留） =====
                const ring = new THREE.Mesh(
                    new THREE.TorusGeometry(width * 0.8, 0.025, 8, 16),
                    new THREE.MeshStandardMaterial({{
                        color: color,
                        emissive: color,
                        emissiveIntensity: 0.5,
                        transparent: true,
                        opacity: 0.7
                    }})
                );
                ring.position.y = height + 0.05;
                ring.rotation.x = Math.PI / 2;
                ring.userData.lodLevel = 0;
                group.add(ring);

                const indicator = new THREE.Mesh(
                    new THREE.SphereGeometry(0.04, 8, 8),
                    new THREE.MeshStandardMaterial({{ color: 0x00ff44, emissive: 0x00ff44, emissiveIntensity: 0.5 }})
                );
                indicator.position.set(0, height + 0.12, 0);
                indicator.userData.lodLevel = 0;
                group.add(indicator);

                // ===== 标签（远景隐藏） =====
                const div = document.createElement('div');
                div.textContent = params.label || '⚡';
                div.style.cssText = `
                    color: #eef2ff; font-size: 10px; font-weight: 600;
                    text-shadow: 0 0 8px rgba(0,0,0,0.9);
                    background: rgba(10,14,23,0.6);
                    padding: 1px 8px; border-radius: 12px;
                    border: 1px solid rgba(255,255,255,0.05);
                    backdrop-filter: blur(2px);
                `;
                const label = new CSS2DObject(div);
                label.position.set(0, height + 0.45, 0);
                label.userData = {{ stationId: params.station_id || null }};
                label.userData.lodLevel = 2;   // 🔥 仅近景/中景显示
                group.add(label);

                group.userData = {{ ring, speed: 0.5 + Math.random() * 0.5 }};
                return group;
            }}
            
            function createRealtimeVisuals(baseHeight) {{
                const layer = new THREE.Group();
                layer.name = 'realtime-visuals';
            
                // LED 指示灯
                const led = new THREE.Mesh(
                    new THREE.SphereGeometry(0.08, 12, 12),
                    new THREE.MeshStandardMaterial({{
                        color: 0x00ff44,
                        emissive: 0x00ff44,
                        emissiveIntensity: 1.5
                    }})
                );
            
                // LED 晕圈
                const ledGlow = new THREE.Mesh(
                    new THREE.SphereGeometry(0.15, 12, 12),
                    new THREE.MeshBasicMaterial({{
                        color: 0x00ff44,
                        transparent: true,
                        opacity: 0.35,
                        blending: THREE.AdditiveBlending,
                        depthWrite: false
                    }})
                );
                ledGlow.position.y = baseHeight + 0.15;
                ledGlow.userData.isLedGlow = true;
                layer.add(ledGlow);
            
                // ===== 信标光柱（Minecraft 风格） =====
                const beaconBeam = createBeaconBeam(8.0);  // 固定 8 米高
                beaconBeam.position.y = baseHeight + 0.15;
                beaconBeam.userData.isBeacon = true;
                beaconBeam.visible = (localStorage.getItem('beaconEnabled') === 'true');
                layer.add(beaconBeam);
            
                // 顶部光环
                const ring = new THREE.Mesh(
                    new THREE.TorusGeometry(0.22, 0.025, 8, 16),
                    new THREE.MeshStandardMaterial({{
                        color: 0x00ff44,
                        emissive: 0x00ff44,
                        emissiveIntensity: 0.5,
                        transparent: true,
                        opacity: 0.7
                    }})
                );
                ring.position.y = baseHeight + 0.05;
                ring.rotation.x = Math.PI / 2;
                ring.userData.isRing = true;
                layer.add(ring);
            
                layer.userData = {{
                    baseHeight: baseHeight,
                    ring: ring,
                    speed: 0.5 + Math.random() * 0.5
                }};
                
                // ===== 脉冲扩散环（地面上的扩散光环） =====
                const pulseRing = new THREE.Mesh(
                    new THREE.RingGeometry(0.3, 0.42, 32),
                    new THREE.MeshBasicMaterial({{
                        color: 0x00ff44,
                        transparent: true,
                        opacity: 0.6,
                        side: THREE.DoubleSide,
                        blending: THREE.AdditiveBlending,
                        depthWrite: false
                    }})
                );
                pulseRing.rotation.x = -Math.PI / 2;
                pulseRing.position.y = 0.02;
                pulseRing.userData.isPulseRing = true;
                pulseRing.userData.pulsePhase = Math.random();  // 每个环错开相位
                pulseRing.visible = (localStorage.getItem('pulseRingEnabled') !== 'false');
                layer.add(pulseRing);
            
                return layer;
            }}
            
            function createBeaconBeam(height) {{
                const beamGroup = new THREE.Group();
                beamGroup.userData.isBeaconBeam = true;
            
                // 渐变纹理：底实顶虚
                const canvas = document.createElement('canvas');
                canvas.width = 16;
                canvas.height = 256;
                const ctx = canvas.getContext('2d');
                const gradient = ctx.createLinearGradient(0, 0, 0, 256);
                gradient.addColorStop(0.0, 'rgba(255,255,255,1.0)');   // 底（canvas 顶部对应纹理顶部，这里需要反一下）
                gradient.addColorStop(0.3, 'rgba(255,255,255,0.7)');
                gradient.addColorStop(0.7, 'rgba(255,255,255,0.25)');
                gradient.addColorStop(1.0, 'rgba(255,255,255,0)');     // 顶
                ctx.fillStyle = gradient;
                ctx.fillRect(0, 0, 16, 256);
                const texture = new THREE.CanvasTexture(canvas);
                texture.wrapS = THREE.RepeatWrapping;
                texture.wrapT = THREE.RepeatWrapping;
            
                const beamMat = new THREE.MeshBasicMaterial({{
                    map: texture,
                    color: 0x00ff44,
                    transparent: true,
                    opacity: 0.85,
                    blending: THREE.AdditiveBlending,
                    depthWrite: false,
                    side: THREE.DoubleSide
                }});
            
                // 两个交叉平面
                const plane1 = new THREE.Mesh(new THREE.PlaneGeometry(0.8, height), beamMat);
                plane1.position.y = height / 2;
            
                const plane2 = new THREE.Mesh(new THREE.PlaneGeometry(0.8, height), beamMat.clone());
                plane2.rotation.y = Math.PI / 2;
                plane2.position.y = height / 2;
            
                beamGroup.add(plane1);
                beamGroup.add(plane2);
            
                beamGroup.userData.plane1 = plane1;
                beamGroup.userData.plane2 = plane2;
            
                return beamGroup;
            }}
            
            function createBuilding(params) {{
                const group = new THREE.Group();
                const w = params.width || 2.0;
                const h = params.height || 3.0;
                const d = params.depth || 1.5;
                const color = getColor(params.color || '#8a9aaa');

                // ===== L0：主体（所有级别） =====
                const body = new THREE.Mesh(
                    new THREE.BoxGeometry(w, h, d),
                    new THREE.MeshStandardMaterial({{ color, roughness: 0.6, metalness: 0.2 }})
                );
                body.position.y = h / 2;
                body.castShadow = true;
                body.receiveShadow = true;
                body.userData.lodLevel = 0;
                group.add(body);

                // ===== L1：窗户（中景以下隐藏） =====
                const winColor = getColor('#6a9ad4');
                const winRows = 4;
                const winCols = 3;
                const winW = 0.12;
                const winH = 0.18;

                for (let row = 0; row < winRows; row++) {{
                    for (let col = 0; col < winCols; col++) {{
                        const spacing = w / (winCols + 1);
                        const xPos = -w / 2 + spacing * (col + 1);
                        const yPos = h * 0.25 + row * (h * 0.18);

                        const win = new THREE.Mesh(
                            new THREE.PlaneGeometry(winW, winH),
                            new THREE.MeshStandardMaterial({{
                                color: winColor,
                                emissive: winColor,
                                emissiveIntensity: 0.3 + Math.random() * 0.2,
                                transparent: true,
                                opacity: 0.4 + Math.random() * 0.3,
                                side: THREE.DoubleSide
                            }})
                        );
                        win.position.set(xPos, yPos, d / 2 + 0.01);
                        win.userData.lodLevel = 1;
                        group.add(win);

                        const winBack = win.clone();
                        winBack.position.z = -d / 2 - 0.01;
                        winBack.userData.lodLevel = 1;
                        group.add(winBack);
                    }}
                }}

                // ===== L0：屋顶（保留） =====
                const roof = new THREE.Mesh(
                    new THREE.BoxGeometry(w + 0.2, 0.08, d + 0.2),
                    new THREE.MeshStandardMaterial({{ color: 0x6a7a8a, roughness: 0.8, metalness: 0.1 }})
                );
                roof.position.y = h + 0.04;
                roof.castShadow = true;
                roof.userData.lodLevel = 0;
                group.add(roof);

                // ===== L1：屋顶设备（中景以下隐藏） =====
                const equip = new THREE.Mesh(
                    new THREE.BoxGeometry(0.3, 0.2, 0.3),
                    new THREE.MeshStandardMaterial({{ color: 0x7a8a9a, metalness: 0.3, roughness: 0.5 }})
                );
                equip.position.set(w * 0.2, h + 0.2, 0);
                equip.userData.lodLevel = 1;
                group.add(equip);

                // ===== L1：入口雨棚（中景以下隐藏） =====
                const canopy = new THREE.Mesh(
                    new THREE.BoxGeometry(0.4, 0.04, 0.3),
                    new THREE.MeshStandardMaterial({{ color: 0x9aabbc, metalness: 0.4, roughness: 0.3 }})
                );
                canopy.position.set(0, 0.15, d / 2 + 0.02);
                canopy.userData.lodLevel = 1;
                group.add(canopy);

                // ===== L1：建筑轮廓发光线条（中景以下隐藏） =====
                const edgeGeo = new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d));
                const edgeMat = new THREE.LineBasicMaterial({{
                    color: 0x4488ff,
                    transparent: true,
                    opacity: 0.2
                }});
                const edgeLine = new THREE.LineSegments(edgeGeo, edgeMat);
                edgeLine.position.y = h / 2;
                edgeLine.userData.lodLevel = 1;
                group.add(edgeLine);

                return group;
            }}


            function createTree(params) {{
                const group = new THREE.Group();
                const scale = params.scale || 1.0;

                const trunkMat = new THREE.MeshStandardMaterial({{ color: 0x7a5a4a, roughness: 0.9 }});
                const trunk = new THREE.Mesh(
                    new THREE.CylinderGeometry(0.06 * scale, 0.1 * scale, 0.6 * scale, 6),
                    trunkMat
                );
                trunk.position.y = 0.3 * scale;
                trunk.castShadow = true;
                group.add(trunk);

                const branch1 = new THREE.Mesh(
                    new THREE.CylinderGeometry(0.04 * scale, 0.06 * scale, 0.3 * scale, 5),
                    trunkMat
                );
                branch1.position.set(0.08 * scale, 0.5 * scale, 0);
                branch1.rotation.z = 0.4;
                group.add(branch1);

                const branch2 = new THREE.Mesh(
                    new THREE.CylinderGeometry(0.04 * scale, 0.06 * scale, 0.3 * scale, 5),
                    trunkMat
                );
                branch2.position.set(-0.08 * scale, 0.5 * scale, 0);
                branch2.rotation.z = -0.4;
                group.add(branch2);

                const crownMat = new THREE.MeshStandardMaterial({{ color: params.color || 0x5a9a5a, roughness: 0.8 }});
                const crown1 = new THREE.Mesh(
                    new THREE.SphereGeometry(0.35 * scale, 7, 7),
                    crownMat
                );
                crown1.position.set(0, 0.9 * scale, 0);
                crown1.castShadow = true;
                group.add(crown1);

                const crown2 = new THREE.Mesh(
                    new THREE.SphereGeometry(0.25 * scale, 7, 7),
                    new THREE.MeshStandardMaterial({{ color: 0x6aaa6a, roughness: 0.8 }})
                );
                crown2.position.set(0.2 * scale, 0.75 * scale, 0.15 * scale);
                crown2.castShadow = true;
                group.add(crown2);
                crown2.userData.lodLevel = 1;


                const crown3 = new THREE.Mesh(
                    new THREE.SphereGeometry(0.25 * scale, 7, 7),
                    new THREE.MeshStandardMaterial({{ color: 0x4a8a4a, roughness: 0.8 }})
                );
                crown3.position.set(-0.18 * scale, 0.8 * scale, -0.12 * scale);
                crown3.castShadow = true;
                group.add(crown3);
                crown3.userData.lodLevel = 1;

                return group;
            }}

            function createLamp(params) {{
                const group = new THREE.Group();
                const pole = new THREE.Mesh(
                    new THREE.CylinderGeometry(0.02, 0.025, 1.2, 6),
                    new THREE.MeshStandardMaterial({{ color: 0x999999, metalness: 0.6 }})
                );
                pole.position.y = 0.6;
                group.add(pole);

                const arm = new THREE.Mesh(
                    new THREE.BoxGeometry(0.3, 0.02, 0.02),
                    new THREE.MeshStandardMaterial({{ color: 0x999999, metalness: 0.6 }})
                );
                arm.position.set(0.15, 1.2, 0);
                group.add(arm);

                const light = new THREE.Mesh(
                    new THREE.SphereGeometry(0.06, 6, 6),
                    new THREE.MeshStandardMaterial({{ color: 0xf1c40f, emissive: 0xf1c40f, emissiveIntensity: 0.8 }})
                );
                light.position.set(0.3, 1.18, 0);
                group.add(light);

                return group;
            }}

            function createRoadStraight(params) {{
                const group = new THREE.Group();
                const w = params.width || 1.0;
                const d = params.depth || 0.3;
                const road = new THREE.Mesh(
                    new THREE.BoxGeometry(w, 0.05, d),
                    new THREE.MeshStandardMaterial({{ color: 0x666666, roughness: 0.9 }})
                );
                road.position.y = 0.025;
                road.receiveShadow = true;
                group.add(road);

                const line = new THREE.Mesh(
                    new THREE.BoxGeometry(0.02, 0.06, d * 0.8),
                    new THREE.MeshStandardMaterial({{ color: 0xffffff }})
                );
                line.position.y = 0.08;
                group.add(line);
                return group;
            }}

            function createRoad(params) {{
                // 🔥 为什么新加这个类型、以及为什么不用 road_straight.glb：
                //    road_straight.glb 原始尺寸 19 × 0.75 × 20.3，前端对它的缩放是
                //    autoScale = 12 / max(size.x, size.z) —— 按「最长边」等比缩放。
                //    结果是一块 12×12 的方块：既太大（小镇格距才 2.5），
                //    又没法沿一个方向拉长（拉长会同时变宽）。
                //    本函数把长度和宽度**分开控制**，路面才能拼成路网。
                const group = new THREE.Group();
                const width  = params.width  || 4.0;    // 路宽（垂直于长度方向）
                const length = params.length || 24.0;   // 路长（沿本地 X 轴）
                const color  = getColor(params.color || '#3c4148');

                // ===== 路面 =====
                const surface = new THREE.Mesh(
                    new THREE.BoxGeometry(length, 0.04, width),
                    new THREE.MeshStandardMaterial({{ color, roughness: 0.95, metalness: 0.0 }})
                );
                surface.position.y = 0.02;
                surface.receiveShadow = true;
                surface.userData.lodLevel = 0;
                group.add(surface);

                // ===== 中线（虚线）=====
                // 短路段没有中线，避免小路上出现一根白条
                if (params.center_line !== false && length > 8) {{
                    const dashLen = 1.4, gap = 1.4, dashW = 0.12;
                    const lineMat = new THREE.MeshStandardMaterial({{
                        "color": 0xf0e6c8, "roughness": 0.7,
                        "emissive": 0x2a2620, "emissiveIntensity": 0.25
                    }});
                    const count = Math.max(1, Math.floor(length / (dashLen + gap)));
                    const span = count * (dashLen + gap);
                    for (let i = 0; i < count; i++) {{
                        const dash = new THREE.Mesh(
                            new THREE.BoxGeometry(dashLen, 0.02, dashW), lineMat);
                        dash.position.set(-span / 2 + dashLen / 2 + i * (dashLen + gap),
                                          0.045, 0);
                        dash.userData.lodLevel = 1;
                        group.add(dash);
                    }}
                }}

                // ===== 人行道（两侧浅色路缘）=====
                if (params.sidewalk) {{
                    const swW = 0.5;
                    const swMat = new THREE.MeshStandardMaterial({{
                        "color": 0x767d88, "roughness": 0.85
                    }});
                    [-1, 1].forEach(function (side) {{
                        const sw = new THREE.Mesh(
                            new THREE.BoxGeometry(length, 0.10, swW), swMat);
                        sw.position.set(0, 0.05, side * (width / 2 + swW / 2));
                        sw.receiveShadow = true;
                        sw.userData.lodLevel = 1;
                        group.add(sw);
                    }});
                }}
                return group;
            }}

            function createCar(params) {{
                const group = new THREE.Group();
                const color = getColor(params.color || '#e74c3c');
                const body = new THREE.Mesh(
                    new THREE.BoxGeometry(0.8, 0.2, 0.4),
                    new THREE.MeshStandardMaterial({{ color, roughness: 0.3, metalness: 0.6 }})
                );
                body.position.y = 0.15;
                body.castShadow = true;
                group.add(body);

                const cabin = new THREE.Mesh(
                    new THREE.BoxGeometry(0.4, 0.15, 0.25),
                    new THREE.MeshStandardMaterial({{ color: 0x3498db, roughness: 0.2, metalness: 0.1 }})
                );
                cabin.position.set(0.05, 0.32, 0);
                group.add(cabin);

                const wheelPos = [[-0.25, 0.05, 0.15], [0.25, 0.05, 0.15], [-0.25, 0.05, -0.15], [0.25, 0.05, -0.15]];
                wheelPos.forEach(pos => {{
                    const wheel = new THREE.Mesh(
                        new THREE.CylinderGeometry(0.06, 0.06, 0.04, 8),
                        new THREE.MeshStandardMaterial({{ color: 0x333333 }})
                    );
                    wheel.rotation.x = Math.PI / 2;
                    wheel.position.set(pos[0], pos[1], pos[2]);
                    group.add(wheel);
                }});
                return group;
            }}

            // ---------- 对象渲染映射 ----------
            // 🔥 本地静态资源
            const STATIC_MODELS = '/app/static/models/';

            // 🔥 Supabase Storage 模型源（云端，PUBLIC bucket）
            const SUPABASE_BASE = {supabase_url_escaped};
            const SUPABASE_MODELS = (SUPABASE_BASE && SUPABASE_BASE.startsWith('http'))
                ? SUPABASE_BASE + '/storage/v1/object/public/models/'
                : STATIC_MODELS;
            console.log('📦 模型源:', SUPABASE_MODELS);
            const modelMap = {{
                // 🏢 建筑
                'building':       STATIC_MODELS + 'building-big.glb',
                'building_tall':  STATIC_MODELS + 'building-red-corner.glb',
                // 🚗 车辆
                'car':            STATIC_MODELS + 'car.glb',
                'truck':          STATIC_MODELS + 'pickup-truck.glb',
                // 🌳 树木
                'tree':           STATIC_MODELS + 'tree.glb',
                'tree_pine':      STATIC_MODELS + 'tree.glb',
                // 🚦 路灯
                'lamp':           STATIC_MODELS + 'traffic-light.glb',
                // 🛣️ 道路（本地文件名用下划线，注意不是 road-straight / road-curve）
                'road_straight':  STATIC_MODELS + 'road_straight.glb',
                'road_curve':     STATIC_MODELS + 'road_curve.glb',
                // ☀️ 光伏板
                'solar_panel_flat':       STATIC_MODELS + 'solar_panel_flat.glb',
                'solar_panel_land':       STATIC_MODELS + 'solar_panel_land.glb',
                'solar_panel_group':      STATIC_MODELS + 'solar_panel_group.glb',
                'solar_panel_port':       STATIC_MODELS + 'solar_panel_port.glb',
                'solar_panel_port_group': STATIC_MODELS + 'solar_panel_port_group.glb',
                // 📦 储能集装箱
                'container_a':            STATIC_MODELS + 'container_a.glb',
                'container_b':            STATIC_MODELS + 'container_b.glb',
                'container_c':            STATIC_MODELS + 'container_c.glb',
                // 🌬️ 风机
                'windmill':               STATIC_MODELS + 'windmill.glb',
                'windmill_low':           STATIC_MODELS + 'windmill_low.glb',
                // 🤖 智能解说员（数字人）
                'narrator':       SUPABASE_MODELS + 'narrator.glb',
                'avatar':         SUPABASE_MODELS + 'narrator.glb',
            }};

            const proceduralMap = {{
                'charger_fast': (obj, style) => createCharger({{
                    "color": style?.charger_color || '#4a90d9',
                    "height": 2.0,
                    "width": 0.3,
                    "label": '快充',
                    "station_id": obj.bind_station_id || null
                }}),
                'charger_slow': (obj, style) => createCharger({{
                    "color": style?.charger_color || '#5cb85c',
                    "height": 1.5,
                    "width": 0.25,
                    "label": '慢充',
                    "station_id": obj.bind_station_id || null
                }}),
                'charger_super': (obj, style) => createCharger({{
                    "color": style?.charger_color || '#9b59b6',
                    "height": 2.5,
                    "width": 0.35,
                    "label": '超充',
                    "station_id": obj.bind_station_id || null
                }}),
                'building': (obj, style) => createBuilding({{
                    ...(obj.custom_props || {{}}),
                    "color": style?.building_color || '#8a9aaa'
                }}),
                'building_tall': (obj, style) => createBuilding({{
                    "width": 1.5,
                    "height": 6.0,
                    "depth": 1.5,
                    "color": style?.building_color || '#7a9aaa'
                }}),
                'tree': (obj, style) => createTree({{
                    "scale": 1.0,
                    "color": style?.tree_color || 0x5a9a5a
                }}),
                'tree_pine': (obj, style) => createTree({{
                    "scale": 0.8,
                    "color": style?.tree_color || 0x4a8a4a
                }}),
                'lamp': (obj) => createLamp({{}}),
                // 🔥 数据驱动路网：路宽/路长都由 custom_props 给，rotation.y 转方向
                'road': (obj, style) => createRoad({{
                    ...(obj.custom_props || {{}}),
                    "color": (obj.custom_props || {{}}).color || '#3c4148'
                }}),
                'sidewalk': (obj, style) => createRoad({{
                    ...(obj.custom_props || {{}}),
                    "color": (obj.custom_props || {{}}).color || '#767d88',
                    "center_line": false
                }}),
                'water': (obj, style) => {{
                    // 水面：一块略低于地面的平面，带一点透明与高光
                    const w = (obj.custom_props || {{}}).width || 20;
                    const l = (obj.custom_props || {{}}).length || 20;
                    const mesh = new THREE.Mesh(
                        new THREE.PlaneGeometry(l, w),
                        new THREE.MeshStandardMaterial({{
                            "color": (obj.custom_props || {{}}).color || '#1d4f6e',
                            "roughness": 0.15,
                            "metalness": 0.6,
                            "transparent": true,
                            "opacity": 0.92
                        }})
                    );
                    mesh.rotation.x = -Math.PI / 2;
                    mesh.position.y = 0.012;
                    mesh.receiveShadow = true;
                    const g = new THREE.Group();
                    g.add(mesh);
                    return g;
                }},
                'road_straight': (obj) => createRoadStraight({{}}),
                'car': (obj) => createCar({{ "color": '#e74c3c' }}),
                'truck': (obj) => {{
                    const group = new THREE.Group();
                    const cab = new THREE.Mesh(
                        new THREE.BoxGeometry(0.5, 0.2, 0.3),
                        new THREE.MeshStandardMaterial({{ "color": 0x5a8ab5, "roughness": 0.3, "metalness": 0.4 }})
                    );
                    cab.position.y = 0.15;
                    cab.castShadow = true;
                    group.add(cab);
                    const cargo = new THREE.Mesh(
                        new THREE.BoxGeometry(0.7, 0.35, 0.5),
                        new THREE.MeshStandardMaterial({{ "color": 0xecf0f1, "roughness": 0.7 }})
                    );
                    cargo.position.set(0.25, 0.3, 0);
                    cargo.castShadow = true;
                    group.add(cargo);
                    return group;
                }}
            }};

            const defaultGenerator = (obj) => {{
                console.warn('未知物体类型，使用灰色盒体兜底:', obj.type, obj.id);
                // 🔥 兜底：显示一个灰色盒体，避免"物体消失"
                const group = new THREE.Group();
                const box = new THREE.Mesh(
                    new THREE.BoxGeometry(1, 1, 1),
                    new THREE.MeshStandardMaterial({{
                        color: 0x888888,
                        roughness: 0.6,
                        metalness: 0.2,
                        transparent: true,
                        opacity: 0.6
                    }})
                );
                box.position.y = 0.5;
                box.castShadow = true;
                box.receiveShadow = true;
                group.add(box);
                return group;
            }};

            // ---------- InstancedMesh ----------
            function createInstancedMeshes(type, group) {{
                const config = {{
                    'charger_fast': {{ width: 0.3, height: 2.0, color: '#4a90d9' }},
                    'charger_slow': {{ width: 0.25, height: 1.5, color: '#5cb85c' }},
                    'charger_super': {{ width: 0.35, height: 2.5, color: '#9b59b6' }}
                }}[type] || {{ width: 0.3, height: 2.0, color: '#4a90d9' }};

                const count = group.length;
                if (count === 0) return;

                const geometry = new THREE.BoxGeometry(config.width, config.height, config.width);
                const material = new THREE.MeshStandardMaterial({{
                    roughness: 0.4,
                    metalness: 0.2,
                    emissive: new THREE.Color(config.color),
                    emissiveIntensity: 0.2,
                }});

                const mesh = new THREE.InstancedMesh(geometry, material, count);
                mesh.castShadow = true;
                mesh.receiveShadow = true;
                mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

                const dummy = new THREE.Object3D();
                const instanceData = [];
                const color = new THREE.Color();

                group.forEach((obj, idx) => {{
                    const pos = obj.position || {{ x: 0, y: 0, z: 0 }};
                    const scale = obj.scale || {{ x: 1, y: 1, z: 1 }};
                    const rot = obj.rotation || {{ x: 0, y: 0, z: 0 }};
                    dummy.position.set(pos.x, pos.y || 0, pos.z);
                    dummy.scale.set(scale.x, scale.y, scale.z);
                    dummy.rotation.set(rot.x || 0, rot.y || 0, rot.z || 0);
                    dummy.updateMatrix();
                    mesh.setMatrixAt(idx, dummy.matrix);

                    let util = obj.utilization || 0.5;
                    const hue = 0.6 - util * 0.6;
                    color.setHSL(hue, 0.9, 0.5);
                    mesh.setColorAt(idx, color);

                    instanceData.push({{
                        id: obj.id,
                        type: obj.type,
                        position: pos,
                        scale: scale,
                        rotation: rot,
                        bind_station_id: obj.bind_station_id || null,
                        name: obj.name,
                        utilization: util,
                        color: color.clone()
                    }});
                }});
                mesh.instanceMatrix.needsUpdate = true;
                mesh.instanceColor.needsUpdate = true;

                mesh.userData.instanceData = instanceData;
                mesh.userData.type = type;
                mesh.userData.isInstanced = true;
                mesh.userData.groupType = type;
                mesh.name = 'chargerInstancedMesh';

                scene.add(mesh);
                objectMeshes.push(mesh);

                mesh.userData.hasPulse = true;
                mesh.userData.pulseSpeed = 0.3 + Math.random() * 0.4;
                mesh.userData.originalEmissive = new THREE.Color(config.color);
                mesh.userData.originalEmissiveIntensity = 0.2;

                clickables.push(mesh);
            }}

            // ---------- 渲染 ----------
            const objectMeshes = [];
            let clickables = [];

            const stationToObjectMap = new Map();
            const stationToInstanceDataMap = new Map();

            objects.forEach(obj => {{
                if (obj.type && obj.type.startsWith('charger') && obj.bind_station_id) {{
                    stationToObjectMap.set(obj.bind_station_id, obj.id);
                    stationToInstanceDataMap.set(obj.bind_station_id, {{
                        objectId: obj.id,
                        stationId: obj.bind_station_id,
                        utilization: obj.utilization || 0.5
                    }});
                }}
            }});

            console.log(`📊 已建立 ${{stationToObjectMap.size}} 个充电桩的 station_id 映射`);

                        function updateChargerVisual(group, utilization) {{
                const visualLayer = group.userData.visualLayer;
                if (!visualLayer) return;
            
                // 🔥 新的合并颜色逻辑：离线 > 高负载 > 正常
                let mainColor, pulseSpeed, labelColor, statusText;
                
                // 先读取 status
                let status = '在线';
                if (group.userData && group.userData.objectId) {{
                    const objData = objects.find(o => o.id === group.userData.objectId);
                    if (objData) status = objData.status || '在线';
                }}
                
                if (status === '离线') {{
                    // 优先级 1：离线 → 红色
                    mainColor = new THREE.Color(0xff2222);
                    pulseSpeed = 1.0;      // 慢速呼吸（表示异常）
                    labelColor = '#ff4444';
                    statusText = '离线';
                }} else if (utilization > 0.7) {{
                    // 优先级 2：高负载 → 黄色
                    mainColor = new THREE.Color(0xffaa00);
                    pulseSpeed = 8.0;      // 快速脉冲（表示告警）
                    labelColor = '#fcc419';
                    statusText = '高负载';
                }} else {{
                    // 优先级 3：正常 → 绿色
                    mainColor = new THREE.Color(0x22ff44);
                    pulseSpeed = 1.5;
                    labelColor = '#51cf66';
                    statusText = '正常';
                }}
            
                const baseHeight = visualLayer.userData.baseHeight;
            
                visualLayer.traverse((child) => {{
                    // LED 灯
                    if (child.userData.isIndicator) {{
                        child.material.color.copy(mainColor);
                        child.material.emissive.copy(mainColor);
                        child.material.emissiveIntensity = 2.0;
                    }}
                    // LED 晕圈
                    if (child.userData.isLedGlow) {{
                        child.material.color.copy(mainColor);
                    }}
                    // 光柱
                    if (child.userData.isBeacon) {{
                        child.userData.plane1.material.color.copy(mainColor);
                        child.userData.plane2.material.color.copy(mainColor);
                        const opacity = utilization > 0.7 ? 1.0 : (utilization > 0.4 ? 0.8 : 0.6);
                        child.userData.plane1.material.opacity = opacity;
                        child.userData.plane2.material.opacity = opacity;
                    }}
                    // 顶部光环
                    if (child.userData.isRing) {{
                        child.material.color.copy(mainColor);
                        child.material.emissive.copy(mainColor);
                        child.userData.pulseSpeed = pulseSpeed;
                    }}
                    // 脉冲扩散环
                    if (child.userData.isPulseRing) {{
                        child.material.color.copy(mainColor);
                    }}
                    // 🔥 状态信号球（跟随主色）
                    if (child.userData.isStatusBall) {{
                        child.material.color.copy(mainColor);
                        child.material.emissive.copy(mainColor);
                    }}
                    if (child.userData.isStatusGlow) {{
                        child.material.color.copy(mainColor);
                    }}
                }});
            
                // 更新标签文字和颜色
                group.traverse((child) => {{
                    if (child.isCSS2DObject && child.element) {{
                        child.element.style.color = labelColor;
                        child.element.style.borderColor = labelColor + '55';
                        child.element.style.boxShadow = '0 0 12px ' + labelColor + '66';
                    }}
                }});
            
                // 记录状态，供 animate() 使用
                group.userData.currentUtilization = utilization;
                group.userData.currentColor = mainColor;
                group.userData.currentPulseSpeed = pulseSpeed;
                group.userData.currentStatus = statusText;
            }}

                        function updateChargerStatus(stationId, utilization, availableSlots, status) {{
                // 原有逻辑
                let targetObjId = null;
                objects.forEach(obj => {{
                    if (obj.type && obj.type.startsWith('charger') && obj.bind_station_id === stationId) {{
                        obj.utilization = utilization;
                        targetObjId = obj.id;
                    }}
                }});
            
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.objectId) return;
                    if (targetObjId && group.userData.objectId !== targetObjId) return;
                    updateChargerVisual(group, utilization);
                }});
            
                saveToLocalStorage(objects);
                console.log('✅ 实时更新 ' + stationId + ': ' + (utilization * 100).toFixed(0) + '%');
            
                // 🔥 新增：触发告警（直接写在这里，不再重写函数）
                if (utilization > getAlertThreshold()) {{
                    let objName = stationId;
                    objects.forEach(obj => {{
                        if (obj.bind_station_id === stationId) {{
                            objName = obj.name || stationId;
                        }}
                    }});
                    showAlert(stationId, utilization, objName);
                }}
            }}


            // ---------- Supabase Realtime ----------
            let realtimeChannel = null;
            const realtimeStatus = document.getElementById('realtime-status');

            function subscribeRealtime() {{
                if (!supabaseClient) {{
                    console.warn('⚠️ Supabase 客户端未初始化，跳过 Realtime 订阅');
                    if (realtimeStatus) {{
                        realtimeStatus.textContent = '⚠️ 实时数据未连接';
                        realtimeStatus.style.color = '#fcc419';
                        realtimeStatus.style.borderColor = 'rgba(252,196,25,0.2)';
                    }}
                    return;
                }}

                // 🔥 离线演示：无网络时直接跳过，不做无谓的实时连接尝试
                if (navigator.onLine === false) {{
                    console.warn('📴 浏览器处于离线状态，跳过 Realtime 订阅');
                    if (realtimeStatus) {{
                        realtimeStatus.textContent = '📴 离线模式：实时数据已停用';
                        realtimeStatus.style.color = '#fcc419';
                        realtimeStatus.style.borderColor = 'rgba(252,196,25,0.2)';
                    }}
                    return;
                }}

                if (realtimeChannel) realtimeChannel.unsubscribe();

                realtimeChannel = supabaseClient
                    .channel('station-realtime')
                    .on('postgres_changes', {{ event: 'UPDATE', schema: 'public', table: 'station_realtime_data' }}, (payload) => {{
                        const newData = payload.new;
                        if (!newData) return;
                        const stationId = newData.station_id;
                        const utilization = newData.utilization || 0.5;
                        if (stationToObjectMap.has(stationId)) {{
                            updateChargerStatus(stationId, utilization, newData.available_slots || 0, newData.status || '在线');
                            if (realtimeStatus) {{
                                realtimeStatus.textContent = '🟢 实时数据更新中';
                                realtimeStatus.style.color = '#51cf66';
                                realtimeStatus.style.borderColor = 'rgba(81,207,102,0.2)';
                                clearTimeout(realtimeStatus._timeout);
                                realtimeStatus._timeout = setTimeout(() => {{
                                    realtimeStatus.textContent = '🔴 实时数据已连接';
                                }}, 2000);
                            }}
                        }}
                    }})
                    .on('postgres_changes', {{ event: 'INSERT', schema: 'public', table: 'station_realtime_data' }}, (payload) => {{
                        const newData = payload.new;
                        if (!newData) return;
                        const stationId = newData.station_id;
                        if (stationToObjectMap.has(stationId)) {{
                            updateChargerStatus(stationId, newData.utilization || 0.5, newData.available_slots || 0, newData.status || '在线');
                        }}
                    }})
                    .subscribe((status, err) => {{
                        if (status === 'SUBSCRIBED') {{
                            console.log('✅ Supabase Realtime 已订阅');
                            if (realtimeStatus) {{
                                realtimeStatus.textContent = '🔴 实时数据已连接';
                                realtimeStatus.style.color = '#51cf66';
                                realtimeStatus.classList.add('visible');
                            }}
                        }} else if (status === 'CHANNEL_ERROR') {{
                            console.error('❌ Realtime 订阅失败:', err);
                            if (realtimeStatus) {{
                                realtimeStatus.textContent = '⚠️ 实时数据连接失败';
                                realtimeStatus.style.color = '#ff6b6b';
                                realtimeStatus.style.borderColor = 'rgba(255,107,107,0.2)';
                            }}
                        }}
                    }});
            }}
            
            // ---------- 悬浮信息面板（只读：显示类型/位置/旋转/缩放/绑定站点） ----------
            function showInfoPanel(objectId) {{
                const objData = objects.find(o => o.id === objectId);
                if (!objData) return;
                const panel = document.getElementById('info-panel');
                const content = document.getElementById('panel-content');
                const title = document.getElementById('panel-title-text');
                title.textContent = objData.name || '物体';
                let html = `
                    <div><strong>类型：</strong>${{objData.type}}</div>
                    <div><strong>位置：</strong>(${{objData.position.x.toFixed(2)}}, ${{objData.position.y.toFixed(2)}}, ${{objData.position.z.toFixed(2)}})</div>
                    <div><strong>旋转：</strong>(${{objData.rotation.x.toFixed(2)}}, ${{objData.rotation.y.toFixed(2)}}, ${{objData.rotation.z.toFixed(2)}})</div>
                    <div><strong>缩放：</strong>(${{objData.scale.x.toFixed(2)}}, ${{objData.scale.y.toFixed(2)}}, ${{objData.scale.z.toFixed(2)}})</div>
                `;
                if (objData.bind_station_id) {{
                    html += `<div><strong>绑定站点：</strong>${{objData.bind_station_id}}</div>`;
                }}
                content.innerHTML = html;
                panel.style.display = 'block';
            
            }}
            
            // ============================================================
            // 6.7 场景中浮动画板（2D 迷你图悬浮在充电桩上方）
            // ============================================================
            function createMiniChartTexture(data, color) {{
                const canvas = document.createElement('canvas');
                canvas.width = 200;
                canvas.height = 100;
                const ctx = canvas.getContext('2d');
            
                // 背景
                ctx.fillStyle = 'rgba(10, 14, 23, 0.9)';
                ctx.fillRect(0, 0, 200, 100);
            
                // 边框
                ctx.strokeStyle = color;
                ctx.lineWidth = 2;
                ctx.strokeRect(1, 1, 198, 98);
            
                // 折线图
                if (data && data.length > 1) {{
                    ctx.beginPath();
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 2;
                    const stepX = 190 / (data.length - 1);
                    data.forEach((val, i) => {{
                        const x = 5 + i * stepX;
                        const y = 90 - val * 80;
                        if (i === 0) ctx.moveTo(x, y);
                        else ctx.lineTo(x, y);
                    }});
                    ctx.stroke();
            
                    // 填充
                    ctx.lineTo(195, 95);
                    ctx.lineTo(5, 95);
                    ctx.closePath();
                    ctx.fillStyle = color + '22';
                    ctx.fill();
                }}
            
                // 文字
                ctx.fillStyle = '#88aadd';
                ctx.font = 'bold 12px Arial';
                ctx.fillText('利用率趋势', 8, 18);
            
                const texture = new THREE.CanvasTexture(canvas);
                return texture;
            }}
            
            // 为选中的充电桩添加浮动画板
            function showFloatingChart(group, historyData, color) {{
                // 移除已有的浮动画板
                const existing = group.getObjectByName('floating-chart');
                if (existing) group.remove(existing);
            
                const texture = createMiniChartTexture(historyData, color);
                const material = new THREE.SpriteMaterial({{ map: texture, transparent: true, depthTest: false }});
                const sprite = new THREE.Sprite(material);
                sprite.name = 'floating-chart';
                sprite.scale.set(1.5, 0.75, 1);
                sprite.position.set(0, 3.5, 0);
                sprite.userData.isFloatingChart = true;
                group.add(sprite);
            }}
            
            // 隐藏浮动画板
            function hideFloatingChart(group) {{
                if (!group) return;
                const existing = group.getObjectByName('floating-chart');
                if (existing) group.remove(existing);
            }}
            
            // 🔥 P1 精简：原 deleteObjectInFrontend()（前端本地删除）已删除。
            // 两个理由：
            //   1) 它是死代码——全项目没有任何调用点，信息面板早已是只读的；
            //   2) 语义有害——它只改浏览器内存 + localStorage，DB 里那条记录仍在，
            //      下次从 Supabase 加载就会"复活"。它也是唯一一处本地 .splice 删物体。
            // 删除 / 复制的唯一入口是右侧面板（render_right_panel 的 🗑️ 删除 / 📋 复制）。
            // 保留原删除逻辑用到的 saveToLocalStorage / hideInfoPanel（别处在用）。
            
            
            // ---------- 关闭信息面板 ----------
            function hideInfoPanel() {{
                document.getElementById('info-panel').style.display = 'none';
            }}
            
            // ============================================================
            // 阶段二：语义关系可视化
            // ============================================================
            let relationsGroup = null;
            let relationLineData = [];

            const RELATION_COLORS = {{
                'along_street': 0x51cf66,  // 绿
                'serves':       0xfcc419,  // 黄
                'connects':     0x88aadd,  // 蓝
                'adjacent':     0x9b59b6,  // 紫
            }};

            function buildRelationLines() {{
                // 清理旧线
                if (relationsGroup) {{
                    scene.remove(relationsGroup);
                    relationsGroup.traverse(c => {{
                        if (c.geometry) c.geometry.dispose();
                        if (c.material) c.material.dispose();
                    }});
                }}
                relationsGroup = new THREE.Group();
                relationsGroup.name = 'relations-group';
                relationLineData = [];

                objects.forEach(obj => {{
                    const rels = (obj.custom_props && obj.custom_props.relations) || [];
                    const fromPos = obj.position;
                    if (!fromPos || rels.length === 0) return;

                    rels.forEach(rel => {{
                        const target = objects.find(o => o.id === rel.target);
                        if (!target) return;
                        const toPos = target.position;
                        if (!toPos) return;

                        // 避免重复绘制（A→B 和 B→A 只画一条）
                        const pairKey = [obj.id, rel.target].sort().join('|');
                        if (relationLineData.some(item => item.pairKey === pairKey)) return;

                        const p1 = new THREE.Vector3(fromPos.x, (fromPos.y || 0) + 0.5, fromPos.z);
                        const p2 = new THREE.Vector3(toPos.x, (toPos.y || 0) + 0.5, toPos.z);

                        const geometry = new THREE.BufferGeometry().setFromPoints([p1, p2]);
                        const material = new THREE.LineBasicMaterial({{
                            color: RELATION_COLORS[rel.type] || 0x666666,
                            transparent: true,
                            opacity: 0.4,
                            depthTest: false,
                        }});
                        const line = new THREE.Line(geometry, material);
                        line.userData.fromId = obj.id;
                        line.userData.toId = rel.target;
                        line.userData.relType = rel.type;
                        line.userData.origColor = RELATION_COLORS[rel.type] || 0x666666;
                        relationsGroup.add(line);
                        relationLineData.push({{
                            pairKey: pairKey,
                            fromId: obj.id,
                            toId: rel.target,
                            type: rel.type,
                            line: line
                        }});
                    }});
                }});

                // 默认显示状态从 localStorage 读取
                const showRel = (localStorage.getItem('showRelations') !== 'false');
                relationsGroup.visible = showRel;
                scene.add(relationsGroup);
                console.log(`🔗 已绘制 ${{relationLineData.length}} 条关系线`);
            }}

            function highlightRelationsFor(objectId) {{
                if (!relationLineData.length) return;
                relationLineData.forEach(item => {{
                    const isRelated = (item.fromId === objectId || item.toId === objectId);
                    item.line.material.opacity = isRelated ? 1.0 : 0.08;
                    if (isRelated) {{
                        item.line.material.color.setHex(0xff3366);
                    }} else {{
                        item.line.material.color.setHex(item.line.userData.origColor);
                    }}
                }});
            }}

            function clearRelationHighlight() {{
                relationLineData.forEach(item => {{
                    item.line.material.opacity = 0.4;
                    item.line.material.color.setHex(item.line.userData.origColor);
                }});
            }}
            
            // ---------- 开始渲染 ----------
            async function renderObjects() {{
                const typeGroups = {{}};
                objects.forEach(obj => {{
                    const type = obj.type || 'default';
                    if (!typeGroups[type]) typeGroups[type] = [];
                    typeGroups[type].push(obj);
                }});
            
                const processedIds = new Set();
                console.log('📌 InstancedMesh 已禁用，所有物体使用程序化生成');
            
                const remainingObjects = objects.filter(obj => !processedIds.has(obj.id));
                const renderPromises = [];
                
                
            
                remainingObjects.forEach((obj) => {{
                    const type = obj.type;
                    const pos = obj.position || {{ x: 0, y: 0, z: 0 }};
                    const scale = obj.scale || {{ x: 1, y: 1, z: 1 }};
                    const rot = obj.rotation || {{ x: 0, y: 0, z: 0 }};

                    // ============================================================
                    // 🔥 分支1：自定义上传的模型（custom_model）
                    // ============================================================
                    if (type === 'custom_model') {{
                        const glbUrl = (obj.custom_props || {{}}).glb_url;
                        if (!glbUrl) {{
                            console.warn('⚠️ 自定义模型缺少 glb_url:', obj.id);
                            useProcedural(obj, 'building', pos, scale, rot, style);
                            return;
                        }}
                        const promise = loadModel(glbUrl).then((model) => {{
                            if (model) {{
                                const cloned = model.clone();

                                // 计算原始包围盒
                                const bbox = new THREE.Box3().setFromObject(cloned);
                                const size = bbox.getSize(new THREE.Vector3());
                                const center = bbox.getCenter(new THREE.Vector3());
                                console.log(`🎨 自定义模型 ${{obj.name}} 尺寸: ${{size.x.toFixed(2)}} × ${{size.y.toFixed(2)}} × ${{size.z.toFixed(2)}}`);

                                // 自动底部对齐：将模型中心移到原点后，再把底部抬到 y=0
                                cloned.position.sub(center);
                                cloned.position.y += size.y / 2;

                                // 🔥 按轴归一化：把导入的模型缩放到一个已知尺寸，
                                //    否则它要么比房子还大、要么小到看不见（"导入了但没真正用上"）。
                                //    目标尺寸优先取对象上带的（由上传时算好并写进资产），
                                //    没有就按包围盒兜一个 2 米见方。
                                const cp = obj.custom_props || {{}};
                                const targetH = Number(cp.target_height) || 0;
                                const targetL = Number(cp.target_length) || 0;
                                const targetW = Number(cp.target_width) || 0;
                                let unitScale;
                                if (targetH > 0) {{
                                    const k = targetH / Math.max(size.y, 0.001);
                                    unitScale = [
                                        (targetW > 0 ? targetW / Math.max(size.x, 0.001) : k),
                                        k,
                                        (targetL > 0 ? targetL / Math.max(size.z, 0.001) : k),
                                    ];
                                }} else {{
                                    // 没给目标：按最长边归一到 2 米，保持原比例
                                    const m = Math.max(size.x, size.y, size.z, 0.001);
                                    const k = 2.0 / m;
                                    unitScale = [k, k, k];
                                }}
                                console.log(`🎨 自定义模型 ${{obj.name}} 原始=${{size.x.toFixed(2)}}×${{size.y.toFixed(2)}}×${{size.z.toFixed(2)}} → 系数=${{unitScale.map(v => v.toFixed(3)).join(', ')}}`);

                                // 应用归一化后再交给 wrapper 应用位置/旋转/缩放
                                cloned.scale.set(unitScale[0], unitScale[1], unitScale[2]);
                                cloned.updateMatrixWorld(true);

                                // 用 Wrapper Group 包装，便于统一应用位置/旋转/缩放
                                const wrapper = new THREE.Group();
                                wrapper.add(cloned);

                                wrapper.position.set(pos.x, pos.y || 0, pos.z);
                                wrapper.scale.set(scale.x, scale.y, scale.z);
                                wrapper.rotation.set(rot.x || 0, rot.y || 0, rot.z || 0);
                                wrapper.userData = {{
                                    objectId: obj.id,
                                    type: 'custom_model',
                                    isCustomAsset: true,
                                }};

                                scene.add(wrapper);
                                objectMeshes.push(wrapper);
                                wrapper.traverse((child) => {{
                                    if (child.isMesh) {{
                                        child.castShadow = true;
                                        child.receiveShadow = true;
                                        child.userData.parentId = obj.id;
                                        clickables.push(child);
                                    }}
                                }});

                                console.log(`✅ 自定义模型加载成功: ${{obj.name}}`);
                            }} else {{
                                console.warn(`⚠️ 自定义模型加载失败，使用占位: ${{obj.name}}`);
                                useProcedural(obj, 'building', pos, scale, rot, style);
                            }}
                        }});
                        renderPromises.push(promise);
                        return;
                    }}

                    // ============================================================
                    // 🔥 分支2：内置模型（原有逻辑保持不变）
                    // ============================================================
                    const modelUrl = modelMap[type];
                    if (modelUrl) {{
                        const promise = loadModel(modelUrl).then((model) => {{
                            if (model) {{
                                const cloned = model.clone();
                        
                                // 🔥 修复：先重置变换，保证 bbox 计算干净
                                cloned.position.set(0, 0, 0);
                                cloned.rotation.set(0, 0, 0);
                                cloned.scale.set(1, 1, 1);
                                cloned.updateMatrixWorld(true);
                        
                                // 🔥 按 type 缓存：包围盒 / 归一化系数只与模型类型有关，同一类型只算一次
                                //    ⚠️ 从"标量 autoScale"改成"按轴归一化（数组）"，原因：
                                //      道路模型 road_straight.glb 的世界尺寸是 19×0.75×20.33，
                                //      长度在 Z 轴。旧逻辑 autoScale = 12/max(x,z) 会把它变成
                                //      11.2×12 的**方块**——既不是路，也没法单向拉长
                                //      （后期只乘一个标量，拉长会同时变宽）。
                                //      改成按轴缩放后，长度和宽度各归各的，模型才真正可用；
                                //      这条同样让「用户导入的任意模型」能按目标尺寸自动适配。
                                let unitScale = autoScaleByType.get(type);
                                if (unitScale === undefined) {{
                                    const bbox = new THREE.Box3().setFromObject(cloned);
                                    const size = bbox.getSize(new THREE.Vector3());
                                    console.log(`📏 [首次] ${{type}} 原始尺寸: ${{size.x.toFixed(2)}} × ${{size.y.toFixed(2)}} × ${{size.z.toFixed(2)}}`);

                                    // 各类模型的**目标尺寸**（单位：场景米）
                                    //   ⚠️ 用 [宽,高,长] 三轴独立拉伸只对"本来就该被拉"的
                                    //      类型成立（道路要铺长）。车辆/树木/建筑这类
                                    //      有机形状**必须等比**，否则会像车那样被拉成
                                    //      又高又长的怪物（踩过：Y 被拉 1.36 倍）。
                                    let target = null;
                                    let uniform = false;      // true = 等比缩放
                                    if (type === 'building')           target = [4.0, 3.5, 4.0];
                                    else if (type === 'building_tall') target = [5.0, 6.0, 5.0];
                                    else if (type === 'tree' || type === 'tree_pine') target = [2.5, 2.5, 2.5];
                                    else if (type === 'lamp')          target = [1.2, 4.0, 1.2];
                                    else if (type === 'car')           {{ target = [4.5, 1.6, 1.8]; uniform = true; }}
                                    else if (type === 'truck')         {{ target = [6.0, 2.6, 2.2]; uniform = true; }}
                                    else if (type === 'container_a' || type === 'container_b' || type === 'container_c') {{
                                        target = [6.0, 2.6, 2.6];
                                    }} else if (type.startsWith('solar_panel')) {{
                                        const d = type.includes('group') ? 6.0 : 2.0;
                                        target = [d, 0.6, d];
                                    }} else if (type === 'windmill' || type === 'windmill_low') {{
                                        target = [5.0, 15.0, 5.0];
                                    }} else if (type === 'road_straight' || type === 'road_curve') {{
                                        // 🔥 只有道路需要单向拉伸：road_straight.glb 长度在 Z 轴
                                        //    （19 宽 × 20.33 长），所以长度缩 Z、宽度缩 X。
                                        //    默认给 12 长 × 5.4 宽；想铺 60 米长把 scale.z 设 5，
                                        //    scale.x 仍只影响路宽，不会跟着变宽。
                                        target = [5.4, 0.6, 12.0];
                                    }}

                                    // 按轴归一化：target / 原始尺寸（逐轴）
                                    unitScale = [1.0, 1.0, 1.0];
                                    if (target) {{
                                        if (uniform) {{
                                            // 等比：用"最长轴对齐目标最长轴"的方式定一个系数，
                                            // 三轴共用，形状才不会被拉变形
                                            const srcMax = Math.max(size.x, size.y, size.z);
                                            const tgtMax = Math.max(target[0], target[1], target[2]);
                                            const k = tgtMax / Math.max(srcMax, 0.001);
                                            unitScale = [k, k, k];
                                        }} else {{
                                            unitScale[0] = target[0] / Math.max(size.x, 0.001);
                                            unitScale[1] = target[1] / Math.max(size.y, 0.001);
                                            unitScale[2] = target[2] / Math.max(size.z, 0.001);
                                        }}
                                    }}

                                    // 兜底：防未来换模型出现特大/极小
                                    for (let i = 0; i < 3; i++) {{
                                        if (!isFinite(unitScale[i]) || unitScale[i] <= 0.0001 || unitScale[i] > 1000) {{
                                            console.warn(`⚠️ ${{type}} 轴${{i}} 归一化异常 (${{unitScale[i]}})，回退为 1.0`);
                                            unitScale[i] = 1.0;
                                        }}
                                    }}
                                    console.log(`📐 ${{type}}: 原始=${{size.x.toFixed(2)}}×${{size.y.toFixed(2)}}×${{size.z.toFixed(2)}}m → 系数=${{unitScale.map(v => v.toFixed(4)).join(', ')}}`);
                                    autoScaleByType.set(type, unitScale);
                                }}

                                // 🔥 合并用户 scale 与归一化系数：**逐轴相乘**（关键修复点）
                                const sArr = Array.isArray(unitScale) ? unitScale : [unitScale, unitScale, unitScale];
                                cloned.scale.set(
                                    (scale.x || 1) * sArr[0],
                                    (scale.y || 1) * sArr[1],
                                    (scale.z || 1) * sArr[2]
                                );
                                cloned.rotation.set(rot.x || 0, rot.y || 0, rot.z || 0);
                        
                                // 🔥 修复：先设置 position.y=0，计算 bbox 得到贴地偏移，再补 pos.y
                                cloned.position.set(pos.x, 0, pos.z);
                                cloned.updateMatrixWorld(true);
                                const finalBbox = new THREE.Box3().setFromObject(cloned);
                                cloned.position.y = (pos.y || 0) - finalBbox.min.y;
                        
                                cloned.userData = {{ objectId: obj.id, type: type }};
                        
                                if (type.startsWith('charger')) {{
                                    const baseHeight = type === 'charger_slow' ? 1.5 : (type === 'charger_super' ? 2.5 : 2.0);
                                    const visualLayer = createRealtimeVisuals(baseHeight);
                                    cloned.add(visualLayer);
                                    cloned.userData.visualLayer = visualLayer;
                                }}
                        
                                scene.add(cloned);
                                objectMeshes.push(cloned);
                                cloned.traverse((child) => {{
                                    if (child.isMesh) {{
                                        child.userData.parentId = obj.id;
                                        clickables.push(child);
                                    }}
                                }});
                            }} else {{
                                useProcedural(obj, type, pos, scale, rot, style);
                            }}
                        }});
                        renderPromises.push(promise);
                    }} else {{
                        useProcedural(obj, type, pos, scale, rot, style);
                    }}
                }});
            
                await Promise.all(renderPromises);
                console.log('✅ 生成了 ' + objectMeshes.length + ' 个对象组');
            
                setTimeout(() => {{
                    const overlay = document.getElementById('loading-overlay');
                    if (overlay && overlay.style.display !== 'none') {{
                        overlay.style.opacity = '0';
                        setTimeout(() => overlay.style.display = 'none', 500);
                        console.log('🧹 加载遮罩层已隐藏');
                    }}
                }}, 800);
            
                if (hasSupabase && supabaseClient) {{
                    setTimeout(() => {{
                        subscribeRealtime();
                    }}, 1500);
                }} else {{
                    console.log('ℹ️ Supabase Realtime 未启用');
                }}
            }}

            function useProcedural(obj, type, pos, scale, rot, style) {{
                // 🔥 新增：GLB 组件加载失败时的兜底盒体
                const generator = proceduralMap[type] || defaultGenerator;
                const model = generator(obj, style);
                model.position.set(pos.x, pos.y || 0, pos.z);
                model.scale.set(scale.x, scale.y, scale.z);
                model.rotation.set(rot.x || 0, rot.y || 0, rot.z || 0);
                model.userData = {{ objectId: obj.id, type: type }};
                
                if (type.startsWith('charger')) {{
                    const baseHeight = type === 'charger_slow' ? 1.5 : (type === 'charger_super' ? 2.5 : 2.0);
                    const visualLayer = createRealtimeVisuals(baseHeight);
                    model.add(visualLayer);
                    model.userData.visualLayer = visualLayer;
                }}
                
                scene.add(model);
                objectMeshes.push(model);
                model.traverse((child) => {{
                    if (child.isMesh) {{
                        child.userData.parentId = obj.id;
                        clickables.push(child);
                    }}
                }});
            }}
            
            // ============================================================
            // 故事高亮功能（补全：之前缺失）
            // ============================================================
            function applyStoryHighlight(highlightType, threshold) {{
                if (!highlightType) return;
            
                console.log(`🎯 应用故事高亮: ${{highlightType}}, 阈值: ${{threshold}}`);
            
                // 先重置所有物体
                objectMeshes.forEach(group => {{
                    if (!group || group.isInstancedMesh) return;
                    group.scale.set(1, 1, 1);
                    group.traverse(child => {{
                        if (child.isMesh && child.material && child.material.emissive) {{
                            // 恢复原始发光
                            if (child.material._origEmissive) {{
                                child.material.emissive.copy(child.material._origEmissive);
                            }}
                            child.material.emissiveIntensity = 0.2;
                        }}
                    }});
                }});
            
                // 按类型高亮
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.objectId) return;
            
                    const objData = objects.find(o => o.id === group.userData.objectId);
                    if (!objData) return;
            
                    const util = objData.utilization || 0.5;
                    const isCharger = objData.type && objData.type.startsWith('charger');
                    const isBuilding = objData.type === 'building';
                    const isTree = objData.type && objData.type.startsWith('tree');
            
                    let matched = false;
            
                    if (highlightType === 'high_load' && isCharger && util > (threshold || 0.7)) {{
                        // 高负载充电桩 → 放大 + 红色发光
                        group.scale.set(1.5, 1.5, 1.5);
                        group.traverse(child => {{
                            if (child.isMesh && child.material && child.material.emissive) {{
                                if (!child.material._origEmissive) {{
                                    child.material._origEmissive = child.material.emissive.clone();
                                }}
                                child.material.emissive.setHex(0xff2222);
                                child.material.emissiveIntensity = 0.8;
                            }}
                        }});
                        matched = true;
                    }}
                    else if (highlightType === 'low_load' && isCharger && util < (threshold || 0.3)) {{
                        // 低负载充电桩 → 放大 + 绿色发光
                        group.scale.set(1.3, 1.3, 1.3);
                        group.traverse(child => {{
                            if (child.isMesh && child.material && child.material.emissive) {{
                                if (!child.material._origEmissive) {{
                                    child.material._origEmissive = child.material.emissive.clone();
                                }}
                                child.material.emissive.setHex(0x22ff44);
                                child.material.emissiveIntensity = 0.6;
                            }}
                        }});
                        matched = true;
                    }}
                    else if (highlightType === 'all_chargers' && isCharger) {{
                        // 所有充电桩 → 稍微放大
                        group.scale.set(1.2, 1.2, 1.2);
                        matched = true;
                    }}
                    else if (highlightType === 'buildings' && isBuilding) {{
                        // 建筑 → 蓝色发光
                        group.scale.set(1.15, 1.15, 1.15);
                        group.traverse(child => {{
                            if (child.isMesh && child.material && child.material.emissive) {{
                                if (!child.material._origEmissive) {{
                                    child.material._origEmissive = child.material.emissive.clone();
                                }}
                                child.material.emissive.setHex(0x4488ff);
                                child.material.emissiveIntensity = 0.5;
                            }}
                        }});
                        matched = true;
                    }}
                    else if (highlightType === 'trees' && isTree) {{
                        // 树木 → 绿色发光
                        group.traverse(child => {{
                            if (child.isMesh && child.material && child.material.emissive) {{
                                if (!child.material._origEmissive) {{
                                    child.material._origEmissive = child.material.emissive.clone();
                                }}
                                child.material.emissive.setHex(0x22cc22);
                                child.material.emissiveIntensity = 0.4;
                            }}
                        }});
                        matched = true;
                    }}
                }});
            
                console.log(`✅ 高亮完成: ${{highlightType}}`);
            }}

            renderObjects().then(() => {{
            
                // 初始化：首次随机生成并持久化到 localStorage，后续不再变
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.objectId) return;
                    const objData = objects.find(o => o.id === group.userData.objectId);
                    if (!objData || !objData.type.startsWith('charger')) return;
                
                    let util = objData.utilization;
                
                    // 如果后端没有值，从 localStorage 恢复
                    if (util === undefined || util === null) {{
                        const stored = localStorage.getItem('util_' + objData.id);
                        if (stored !== null) {{
                            util = parseFloat(stored);
                        }} else {{
                            // 🔥 首次：随机生成一次，立即保存
                            const r = Math.random();
                            if (r < 0.4) util = 0.10 + Math.random() * 0.20;
                            else if (r < 0.7) util = 0.50 + Math.random() * 0.15;
                            else util = 0.80 + Math.random() * 0.15;
                            util = Math.round(util * 1000) / 1000;
                            localStorage.setItem('util_' + objData.id, util.toString());
                            console.log('🎲 首次生成利用率: ' + objData.id + ' = ' + util);
                        }}
                        // 同步到内存里的 objects（供本次会话使用）
                        objData.utilization = util;
                    }}
                
                    updateChargerVisual(group, util);
                    // 🔥 页面加载时检查是否超阈值，超了就弹告警
                    const dynamicThreshold = getAlertThreshold();
                    if (util > dynamicThreshold && objData.bind_station_id) {{
                        showAlert(objData.bind_station_id, util, objData.name);
                    }}
                }});
            
                if (selectedId) {{
                    const group = objectMeshes.find(g => g.userData && g.userData.objectId === selectedId);
                    if (group && !group.isInstancedMesh) {{
                        transformControls.attach(group);
                        currentObjectId = selectedId;
                        currentObjectGroup = group;
                        updateSlidersFromObject(group);
                        // 🔥 用抽屉变量（sliderDrawer），而不是 sliderPanel
                        if (typeof sliderDrawer !== 'undefined' && sliderDrawer) {{
                            sliderDrawer.classList.remove('closed');
                            sliderDrawer.classList.add('open');
                            localStorage.setItem('sliderDrawerOpen', 'true');
                        }}
                        showInfoPanel(selectedId);
                    }}
                }}

                if (story && story.highlight_type) {{
                    setTimeout(() => {{
                        if (typeof applyStoryHighlight === 'function') {{
                            applyStoryHighlight(story.highlight_type, story.filter_threshold);
                        }} else {{
                            console.warn('⚠️ applyStoryHighlight 未定义，跳过高亮');
                        }}
                    }}, 300);
                }}
                
                // 6.9 自动导览按钮默认显示
                if (tourBtn) {{
                    tourBtn.style.display = 'block';
                }}
                }});
                
                
               
            // ============================================================
            // 6.10 数字人/虚拟解说员（GLTF 优先 + 程序化兜底）
            // ============================================================
            function createAvatarProcedural() {{
                // 程序化兜底模型（当 GLTF 加载失败时使用）
                const group = new THREE.Group();
                group.name = 'virtual-avatar-procedural';
            
                // 身体（胶囊形）
                const bodyMat = new THREE.MeshStandardMaterial({{
                    color: 0x4a90d9,
                    emissive: 0x2a5a8a,
                    emissiveIntensity: 0.3,
                    roughness: 0.4,
                    metalness: 0.3
                }});
                const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.3, 0.6, 8, 16), bodyMat);
                body.position.y = 1.0;
                body.castShadow = true;
                group.add(body);
            
                // 头部（球）
                const headMat = new THREE.MeshStandardMaterial({{
                    color: 0x88ccff,
                    emissive: 0x4a90d9,
                    emissiveIntensity: 0.4,
                    roughness: 0.3,
                    metalness: 0.2
                }});
                const head = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 16), headMat);
                head.position.y = 1.55;
                head.castShadow = true;
                group.add(head);
            
                // 眼睛（发光小球）
                const eyeMat = new THREE.MeshBasicMaterial({{ color: 0x00ffaa }});
                const eyeL = new THREE.Mesh(new THREE.SphereGeometry(0.04, 8, 8), eyeMat);
                eyeL.position.set(-0.08, 1.58, 0.18);
                group.add(eyeL);
                const eyeR = eyeL.clone();
                eyeR.position.x = 0.08;
                group.add(eyeR);
            
                return group;
            }}
            
            async function createAvatar() {{
                const group = new THREE.Group();
                group.name = 'virtual-avatar';
            
                let avatarModel = null;
                let modelLoaded = false;
            
                // ---------- 1. 尝试加载 GLTF 模型 ----------
                const avatarUrl = SUPABASE_MODELS + 'narrator.glb';
                console.log('🤖 尝试加载解说员模型:', avatarUrl);
            
                try {{
                    const avatarLoader = new GLTFLoader();
                    avatarModel = await new Promise((resolve) => {{
                        let resolved = false;
                        // 5 秒超时（比通用模型长，因为解说员模型可能较大）
                        const timeoutId = setTimeout(() => {{
                            if (!resolved) {{
                                resolved = true;
                                console.warn('⚠️ 解说员模型加载超时（5 秒），使用程序化兜底');
                                resolve(null);
                            }}
                        }}, 5000);
            
                        avatarLoader.load(
                            avatarUrl,
                            (gltf) => {{
                                if (!resolved) {{
                                    resolved = true;
                                    clearTimeout(timeoutId);
                                    console.log('✅ 解说员模型加载成功');
                                    resolve(gltf.scene);
                                }}
                            }},
                            undefined,
                            (error) => {{
                                if (!resolved) {{
                                    resolved = true;
                                    clearTimeout(timeoutId);
                                    console.warn('⚠️ 解说员模型加载失败，使用程序化兜底:', error);
                                    resolve(null);
                                }}
                            }}
                        );
                    }});
                }} catch (e) {{
                    console.warn('⚠️ 加载解说员模型异常:', e);
                }}
            
                // ---------- 2. 处理加载结果 ----------
                if (avatarModel) {{
                    // 自动缩放到高度 1.8 米
                    const bbox = new THREE.Box3().setFromObject(avatarModel);
                    const size = bbox.getSize(new THREE.Vector3());
                    const targetHeight = 1.8;
                    const scaleFactor = size.y > 0 ? targetHeight / size.y : 1.0;
                    avatarModel.scale.set(scaleFactor, scaleFactor, scaleFactor);
            
                    // 底部对齐到 y=0
                    const bbox2 = new THREE.Box3().setFromObject(avatarModel);
                    avatarModel.position.y = -bbox2.min.y;
            
                    // 阴影
                    avatarModel.traverse((child) => {{
                        if (child.isMesh) {{
                            child.castShadow = true;
                            child.receiveShadow = true;
                        }}
                    }});
            
                    group.add(avatarModel);
                    modelLoaded = true;
            
                    // 记录模型实际高度，供标签和光环定位
                    group.userData.modelHeight = 1.8;
                }} else {{
                    // 兜底：程序化模型
                    console.warn('🔄 使用程序化解说员兜底');
                    const fallback = createAvatarProcedural();
                    while (fallback.children.length > 0) {{
                        group.add(fallback.children[0]);
                    }}
                    modelLoaded = false;
                    group.userData.modelHeight = 1.6;
                }}
            
                // ---------- 3. 头顶光环（GLTF / 程序化 都加） ----------
                const haloY = modelLoaded ? 1.95 : 1.85;
                const ringMat = new THREE.MeshBasicMaterial({{
                    color: 0x88ccff,
                    transparent: true,
                    opacity: 0.6,
                    side: THREE.DoubleSide
                }});
                const halo = new THREE.Mesh(new THREE.RingGeometry(0.25, 0.32, 24), ringMat);
                halo.rotation.x = Math.PI / 2;
                halo.position.y = haloY;
                halo.userData.isHalo = true;
                group.add(halo);
            
                // ---------- 4. 悬浮标签 ----------
                const labelY = modelLoaded ? 2.2 : 2.1;
                const div = document.createElement('div');
                div.textContent = '🤖 智能解说员';
                div.style.cssText = `
                    color: #88ccff; font-size: 11px; font-weight: 600;
                    text-shadow: 0 0 10px rgba(136,204,255,0.8);
                    background: rgba(10,14,23,0.8);
                    padding: 2px 10px; border-radius: 12px;
                    border: 1px solid rgba(136,204,255,0.3);
                    backdrop-filter: blur(4px);
                `;
                const label = new CSS2DObject(div);
                label.position.set(0, labelY, 0);
                label.userData.isAvatarLabel = true;
                group.add(label);
            
                // ---------- 5. 元数据 ----------
                group.userData.isAvatar = true;
                group.userData.baseY = 0;
                group.userData.floatPhase = 0;
                group.userData.modelLoaded = modelLoaded;
            
                console.log(`🤖 解说员创建完成（${{modelLoaded ? 'GLTF 模型' : '程序化兜底'}}）`);
                return group;
            }}
            
            // 添加虚拟解说员到场景
            let avatarGroup = null;
            
            async function addAvatarToScene() {{
                if (avatarGroup) return;
                try {{
                    avatarGroup = await createAvatar();
                    avatarGroup.position.set(-5, 0, 5);
                    scene.add(avatarGroup);
                    console.log('🤖 虚拟解说员已加入场景');
                }} catch (e) {{
                    console.error('❌ 添加解说员失败:', e);
                }}
            }}
            
            // 点击解说员触发讲解（捕获阶段优先）
            renderer.domElement.addEventListener('click', (event) => {{
                if (!avatarGroup) return;
                const rect = renderer.domElement.getBoundingClientRect();
                const pointer2 = new THREE.Vector2(
                    ((event.clientX - rect.left) / rect.width) * 2 - 1,
                    -((event.clientY - rect.top) / rect.height) * 2 + 1
                );
                const ray2 = new THREE.Raycaster();
                ray2.setFromCamera(pointer2, camera);
                const hits = ray2.intersectObject(avatarGroup, true);
                if (hits.length > 0) {{
                    event.stopPropagation();
            
                    // 走到相机前
                    const camDir = new THREE.Vector3();
                    camera.getWorldDirection(camDir);
                    const targetPos = camera.position.clone().add(camDir.multiplyScalar(3));
                    targetPos.y = 0;
                    avatarGroup.position.copy(targetPos);
            
                    // 讲解
                    if (typeof speakText === 'function') {{
                        speakText(buildSceneReport());
                    }}
                    console.log('🤖 解说员开始讲解');
                }}
            }}, true);  // 使用捕获阶段，优先处理
            
            // 在场景加载完成后添加
            setTimeout(() => {{
                addAvatarToScene();
            }}, 2000);    

            // ---------- 灯光 ----------
            const ambientColor = style.ambient_color ? new THREE.Color(style.ambient_color) : new THREE.Color(0x8899bb);
            const ambient = new THREE.AmbientLight(ambientColor, style.ambient_intensity || 1.2);
            scene.add(ambient);

            const sunColor = style.sun_color ? new THREE.Color(style.sun_color) : new THREE.Color(0xffeedd);
            const sun = new THREE.DirectionalLight(sunColor, style.sun_intensity || 2.5);
            sun.position.set(10, 25, 8);
            sun.castShadow = true;
            sun.shadow.mapSize.width = 2048;
            sun.shadow.mapSize.height = 2048;
            const d = 25;
            sun.shadow.camera.left = -d;
            sun.shadow.camera.right = d;
            sun.shadow.camera.top = d;
            sun.shadow.camera.bottom = -d;
            sun.shadow.camera.near = 1;
            sun.shadow.camera.far = 70;
            scene.add(sun);

            const fill = new THREE.DirectionalLight(0x4488ff, 0.6);
            fill.position.set(-15, 8, -10);
            scene.add(fill);

            // ---------- Bloom ----------
            const composer = new EffectComposer(renderer);
            const renderPass = new RenderPass(scene, camera);
            composer.addPass(renderPass);

            const bloomPass = new UnrealBloomPass(
                new THREE.Vector2(width, height),
                style.bloom_strength || 0.6,
                style.bloom_radius || 0.3,
                style.bloom_threshold || 0.15
            );
            composer.addPass(bloomPass);

            // ---------- 地面 ----------
            const gridColor1 = style.grid_color ? new THREE.Color(style.grid_color) : new THREE.Color(0x6699cc);
            const gridColor2 = style.grid_color ? new THREE.Color(style.grid_color).multiplyScalar(0.5) : new THREE.Color(0x2a4a6a);
            const grid = new THREE.GridHelper(20, 20, gridColor1, gridColor2);
            grid.position.y = -0.05;
            scene.add(grid);

            const groundColor = style.ground_color ? new THREE.Color(style.ground_color) : new THREE.Color(0x1a2a3a);

            // 🔥 新增：大范围草地平面（在最底层，比圆盘更大）
            const grassGeo = new THREE.PlaneGeometry(80, 80);
            const grassMat = new THREE.MeshStandardMaterial({{
                color: 0x152a25,          // 深墨绿，与深空主题融合
                roughness: 0.95,
                metalness: 0.0
            }});
            const grass = new THREE.Mesh(grassGeo, grassMat);
            grass.rotation.x = -Math.PI / 2;
            grass.position.y = -0.15;      // 比圆盘低，避免 z-fighting
            grass.receiveShadow = true;
            grass.name = 'grass-plane';
            scene.add(grass);

            // 原有圆盘地面
            const ground = new THREE.Mesh(
                new THREE.CircleGeometry(12, 64),
                new THREE.MeshStandardMaterial({{
                    "color": groundColor,
                    "transparent": true,
                    "opacity": 0.7,
                    "roughness": 0.9,
                    "side": THREE.DoubleSide
                }})
            );
            ground.rotation.x = -Math.PI / 2;
            ground.position.y = -0.05;
            ground.receiveShadow = true;
            scene.add(ground);

            // 相机飞行指令（消费后立即清除）
            const flyTo = sceneData.flyTo;
            if (flyTo) {{
                setTimeout(() => {{
                    // 平滑飞行到目标位置
                    const startPos = camera.position.clone();
                    const endPos = new THREE.Vector3(flyTo.x, flyTo.y, flyTo.z);
                    const startTime = performance.now();
                    const duration = 1200;

                    function flyAnimate() {{
                        const elapsed = performance.now() - startTime;
                        const t = Math.min(elapsed / duration, 1);
                        const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
                        camera.position.lerpVectors(startPos, endPos, ease);
                        controls.target.set(0, 0, 0);
                        controls.update();
                        if (t < 1) {{
                            requestAnimationFrame(flyAnimate);
                        }}
                    }}
                    flyAnimate();
                    console.log(`🎯 相机已飞到 (${{flyTo.x}}, ${{flyTo.y}}, ${{flyTo.z}})`);
                }}, 300);
            }}

            // ---------- 原点 ----------
            const origin = new THREE.Mesh(
                new THREE.SphereGeometry(0.08, 8, 8),
                new THREE.MeshStandardMaterial({{ color: 0xff4444, emissive: 0xff0000, emissiveIntensity: 0.2 }})
            );
            origin.position.set(0, 0.08, 0);
            scene.add(origin);

            // ---------- 粒子 ----------
            const particleCount = 400;
            const particleGeometry = new THREE.BufferGeometry();
            const positions = new Float32Array(particleCount * 3);
            const sizes = new Float32Array(particleCount);
            const speeds = new Float32Array(particleCount);

            for (let i = 0; i < particleCount; i++) {{
                positions[i * 3] = (Math.random() - 0.5) * 36;
                positions[i * 3 + 1] = 1 + Math.random() * 9;
                positions[i * 3 + 2] = (Math.random() - 0.5) * 36;
                sizes[i] = 0.03 + Math.random() * 0.08;
                speeds[i] = 0.3 + Math.random() * 0.7;
            }}
            particleGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
            particleGeometry.setAttribute('size', new THREE.BufferAttribute(sizes, 1));

            const canvas = document.createElement('canvas');
            canvas.width = 32;
            canvas.height = 32;
            const ctx = canvas.getContext('2d');
            const gradient = ctx.createRadialGradient(16, 16, 0, 16, 16, 16);
            gradient.addColorStop(0, 'rgba(136, 170, 221, 1)');
            gradient.addColorStop(0.3, 'rgba(136, 170, 221, 0.8)');
            gradient.addColorStop(1, 'rgba(136, 170, 221, 0)');
            ctx.fillStyle = gradient;
            ctx.fillRect(0, 0, 32, 32);
            const particleTexture = new THREE.CanvasTexture(canvas);

            const particleMaterial = new THREE.PointsMaterial({{
                color: style.charger_color || 0x88aadd,
                map: particleTexture,
                size: 0.15,
                transparent: true,
                blending: THREE.AdditiveBlending,
                depthWrite: false,
                sizeAttenuation: true
            }});

            const particles = new THREE.Points(particleGeometry, particleMaterial);
            scene.add(particles);

            particles.userData = {{
                initialPositions: positions.slice(),
                speeds: speeds,
                time: 0
            }};

            // ---------- 点击拾取 ----------
            const raycaster = new THREE.Raycaster();
            const pointer = new THREE.Vector2();
            const dragHint = document.getElementById('drag-hint');

                        renderer.domElement.addEventListener('click', (event) => {{
                const rect = renderer.domElement.getBoundingClientRect();
                pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
                pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

                raycaster.setFromCamera(pointer, camera);
                const meshes = [];
                clickables.forEach(item => {{ meshes.push(item); }});

                const intersects = raycaster.intersectObjects(meshes, false);

                if (intersects.length > 0) {{
                    const hit = intersects[0].object;
                    let parentId = null;

                    if (hit.isInstancedMesh) {{
                        const instanceId = intersects[0].instanceId;
                        const instanceData = hit.userData.instanceData;
                        if (instanceData && instanceData[instanceId]) {{
                            if (instanceData[instanceId].deleted) return;
                            parentId = instanceData[instanceId].id;
                        }}
                    }} else if (hit.userData && hit.userData.parentId) {{
                        parentId = hit.userData.parentId;
                    }}

                    if (parentId) {{
                        const selectedGroup = objectMeshes.find(g =>
                            g.userData && g.userData.objectId === parentId
                        );

                        if (selectedGroup) {{
                            const previousGroup = currentObjectGroup;

                            if (previousGroup && previousGroup !== selectedGroup) {{
                                hideFloatingChart(previousGroup);
                            }}

                            transformControls.attach(selectedGroup);
                            currentObjectId = parentId;
                            currentObjectGroup = selectedGroup;
                            updateSlidersFromObject(selectedGroup);
                            // 🔥 高亮该物体的关系线
                            highlightRelationsFor(parentId);

                            // 🔥 用抽屉变量
                            if (typeof sliderDrawer !== 'undefined' && sliderDrawer) {{
                                sliderDrawer.classList.remove('closed');
                                sliderDrawer.classList.add('open');
                                localStorage.setItem('sliderDrawerOpen', 'true');
                            }}

                            showInfoPanel(parentId);

                            const objData = objects.find(o => o.id === parentId);
                            if (objData && objData.type && objData.type.startsWith('charger')) {{
                                const historyData = [];
                                let v = objData.utilization || 0.5;
                                for (let i = 0; i < 48; i++) {{
                                    v += (Math.random() - 0.5) * 0.1;
                                    v = Math.max(0.05, Math.min(0.95, v));
                                    historyData.push(v);
                                }}
                                const chartColor = v > 0.7 ? '#ff4444' : (v > 0.4 ? '#fcc419' : '#51cf66');

                                hideFloatingChart(selectedGroup);
                                showFloatingChart(selectedGroup, historyData, chartColor);
                            }}
                        }}
                    }}
                }} else {{
                    transformControls.detach();
                    currentObjectId = null;
                    // 🔥 清除关系线高亮
                    clearRelationHighlight();

                    if (currentObjectGroup) {{
                        hideFloatingChart(currentObjectGroup);
                    }}
                    currentObjectGroup = null;

                    hideInfoPanel();
                    dragHint.classList.add('hidden');

                    // 🔥 纯前端处理，不改 URL
                    hideInfoPanel();
                    if (typeof clearRelationHighlight === 'function') {{
                        clearRelationHighlight();
                    }}
                }}
            }});

            // ---------- 双击切换自动旋转 ----------
            renderer.domElement.addEventListener('dblclick', () => {{
                controls.autoRotate = !controls.autoRotate;
            }});

            // ---------- 窗口自适应 ----------
            function resize() {{
                const w = container.clientWidth || window.innerWidth;
                const h = container.clientHeight || window.innerHeight;
                camera.aspect = w / h;
                camera.updateProjectionMatrix();
                renderer.setSize(w, h);
                labelRenderer.setSize(w, h);
                composer.setSize(w, h);
            }}
            window.addEventListener('resize', resize);
            
            // ============================================================
            // 🔥 阶段3：LOD 与性能优化
            // ============================================================
            const LOD_CONFIG = sceneData.lodConfig || {{ enabled: true, nearDistance: 15.0, farDistance: 35.0 }};
            let lodEnabled = LOD_CONFIG.enabled;
            const LOD_NEAR = LOD_CONFIG.nearDistance;
            const LOD_FAR = LOD_CONFIG.farDistance;

            // LOD 统计
            let lodStats = {{ l0: 0, l1: 0, l2: 0, visible: 0 }};

            // 每个对象的 LOD 缓存（避免每帧重复遍历）
            const lodObjectCache = [];

            function buildLODCache() {{
                lodObjectCache.length = 0;
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.objectId) return;
                    // 收集所有带 lodLevel 标记的子对象
                    const lodChildren = [];
                    group.traverse(child => {{
                        if (child.userData && child.userData.lodLevel !== undefined) {{
                            lodChildren.push({{
                                obj: child,
                                level: child.userData.lodLevel
                            }});
                        }}
                    }});
                    lodObjectCache.push({{
                        group: group,
                        children: lodChildren,
                        currentLevel: -1
                    }});
                }});
                console.log(`📊 LOD 缓存已建立：${{lodObjectCache.length}} 个对象`);
            }}

            function updateLOD() {{
                if (!lodEnabled) return;

                let stats = {{ l0: 0, l1: 0, l2: 0, visible: 0 }};

                lodObjectCache.forEach(item => {{
                    const group = item.group;
                    const dist = camera.position.distanceTo(group.position);

                    // 🔥 修正：改为"最大可见细节级别"
                    //   近景 → 显示全部细节
                    //   远景 → 只显示核心部件
                    let maxVisibleLevel;
                    if (dist < LOD_NEAR) maxVisibleLevel = 2;      // 近景：显示 L0+L1+L2
                    else if (dist < LOD_FAR) maxVisibleLevel = 1;  // 中景：显示 L0+L1
                    else maxVisibleLevel = 0;                       // 远景：只显示 L0

                    // 更新统计
                    if (maxVisibleLevel === 2) stats.l2++;
                    else if (maxVisibleLevel === 1) stats.l1++;
                    else stats.l0++;
                    if (dist < 60) stats.visible++;

                    // 只在级别变化时切换可见性
                    if (item.currentLevel === maxVisibleLevel) return;
                    item.currentLevel = maxVisibleLevel;

                    // 规则：lodLevel <= maxVisibleLevel 的子对象才显示
                    item.children.forEach(child => {{
                        child.obj.visible = (child.level <= maxVisibleLevel);
                    }});
                }});

                lodStats = stats;

                // 更新 UI
                const lodL0 = document.getElementById('lod-l0');
                const lodL1 = document.getElementById('lod-l1');
                const lodL2 = document.getElementById('lod-l2');
                const lodVisible = document.getElementById('lod-visible');
                if (lodL0) lodL0.textContent = stats.l0;
                if (lodL1) lodL1.textContent = stats.l1;
                if (lodL2) lodL2.textContent = stats.l2;
                if (lodVisible) lodVisible.textContent = stats.visible;
            }}

            // ===== FPS 监控 =====
            let fpsCounter = 0;
            let fpsLastUpdate = performance.now();
            let fpsCurrent = 60;

            function updateFPS() {{
                fpsCounter++;
                const now = performance.now();
                if (now - fpsLastUpdate >= 1000) {{
                    fpsCurrent = Math.round(fpsCounter * 1000 / (now - fpsLastUpdate));
                    fpsCounter = 0;
                    fpsLastUpdate = now;

                    const fpsEl = document.getElementById('fps-value');
                    if (fpsEl) {{
                        fpsEl.textContent = fpsCurrent;
                        fpsEl.className = fpsCurrent >= 50 ? 'fps-good'
                                        : fpsCurrent >= 30 ? 'fps-ok' : 'fps-bad';
                    }}
                }}
            }}

            // ===== LOD 开关 =====
            const lodInput = document.getElementById('toggle-lod-input');
            if (lodInput) {{
                lodInput.checked = lodEnabled;
                lodInput.addEventListener('change', () => {{
                    lodEnabled = lodInput.checked;
                    if (lodEnabled) {{
                        buildLODCache();
                        updateLOD();
                    }} else {{
                        // 关闭 LOD：全部显示
                        objectMeshes.forEach(group => {{
                            group.traverse(child => {{
                                if (child.userData && child.userData.lodLevel !== undefined) {{
                                    child.visible = true;
                                }}
                            }});
                        }});
                    }}
                }});
            }}

            // ===== FPS 监控开关 =====
            const fpsInput = document.getElementById('toggle-fps-input');
            const fpsMonitor = document.getElementById('fps-monitor');
            if (fpsInput) {{
                const fpsEnabled = localStorage.getItem('pref_show_fps') === 'true';
                fpsInput.checked = fpsEnabled;
                fpsMonitor.classList.toggle('visible', fpsEnabled);
                fpsInput.addEventListener('change', () => {{
                    const enabled = fpsInput.checked;
                    fpsMonitor.classList.toggle('visible', enabled);
                    localStorage.setItem('pref_show_fps', enabled);
                }});
            }}

            // 初始化时建立 LOD 缓存
            setTimeout(() => {{
                buildLODCache();
            }}, 1500);

            // ---------- 动画循环 ----------
            function animate() {{
                requestAnimationFrame(animate);

                // 🔥 思索教学进行中：主场景降频为静默背景（保留渲染，避免黑屏）
                //    跳过日光巡游 / 粒子 / 呼吸灯等全部重计算，把 GPU 让给教学沙盒。
                if (window.__DTT__ && window.__DTT__.isPonderActive) {{
                    try {{
                        if (typeof controls !== 'undefined' && controls && controls.enabled) controls.update();
                        renderer.render(scene, camera);
                        if (labelRenderer) labelRenderer.render(scene, camera);
                    }} catch (e) {{ /* 静默 */ }}
                    return;
                }}

                // 🔥 LOD 性能优化（每 5 帧更新一次，降低 CPU 开销）
                if (window._lodFrameCounter === undefined) window._lodFrameCounter = 0;
                window._lodFrameCounter++;
                if (window._lodFrameCounter % 5 === 0) {{
                    if (typeof updateLOD === 'function') updateLOD();
                }}
                if (typeof updateFPS === 'function') updateFPS();
                
                if (fpvMode) {{
                    updateFPVCamera();
                }} else {{
                    controls.update();
                }}
                
                const time = Date.now() * 0.00015;
                const radius = 25;
                sun.position.x = Math.cos(time) * radius;
                sun.position.z = Math.sin(time) * radius;
                sun.position.y = 12 + Math.sin(time * 0.8) * 8;
                sun.target.position.set(0, 0, 0);
                sun.target.updateMatrixWorld();

                const hue = (Math.sin(time * 0.2) * 0.08 + 0.08);
                sun.color.setHSL(hue, 0.8, 0.9);

                ambient.intensity = 1.0 + 0.4 * Math.sin(time * 0.8);

                fill.position.x = -15 + Math.sin(time * 0.5) * 5;
                fill.position.z = -10 + Math.cos(time * 0.6) * 5;
                
                                // 6.10 虚拟解说员浮动动画
                if (avatarGroup) {{
                    avatarGroup.userData.floatPhase += 0.02;
                    avatarGroup.position.y = Math.sin(avatarGroup.userData.floatPhase) * 0.1;

                    const halo = avatarGroup.children.find(c => c.userData && c.userData.isHalo);
                    if (halo) halo.rotation.z += 0.02;
                }}

                objectMeshes.forEach(group => {{
                    // ----- 原有的光环旋转（如果存在） -----
                    if (group.userData && group.userData.ring) {{
                        const ring = group.userData.ring;
                        const speed = group.userData.speed || 0.5;
                        ring.rotation.z += 0.03 * speed;
                        const pulse = Math.sin(Date.now() * 0.003 * speed) * 0.4 + 0.5;
                        ring.material.opacity = pulse;
                    }}
                
                    // ----- 新增：实时视觉层的脉冲 + 扩散动画 -----
                    if (group.userData && group.userData.visualLayer) {{
                        const vl = group.userData.visualLayer;
                        const now = Date.now();
                        const baseColor = group.userData.currentColor || new THREE.Color(0x22ff44);
                        const pulseSpeed = group.userData.currentPulseSpeed || 2.0;
                
                        vl.traverse((child) => {{
                            // LED 呼吸灯
                            if (child.userData.isLedGlow) {{
                                const p = Math.sin(now * 0.005 * pulseSpeed) * 0.5 + 0.5;
                                child.material.opacity = 0.25 + 0.5 * p;
                                child.scale.setScalar(1 + 0.2 * p);
                            }}
                
                            // 光环发光强度
                            if (child.userData.isRing) {{
                                const p = Math.sin(now * 0.001 * pulseSpeed) * 0.5 + 0.5;
                                child.material.emissiveIntensity = 0.3 + 2.0 * p;
                                child.rotation.z += 0.02 * pulseSpeed;
                            }}
                
                            // LED 灯本身脉冲
                            if (child.userData.isIndicator) {{
                                const p = Math.sin(now * 0.008 * pulseSpeed) * 0.5 + 0.5;
                                child.material.emissiveIntensity = 1.0 + 2.0 * p;
                            }}
                
                            // 🔥 脉冲扩散环
                            if (child.userData.isPulseRing) {{
                                child.userData.pulsePhase += 0.016 * 1.5;
                                if (child.userData.pulsePhase > 1.0) child.userData.pulsePhase -= 1.0;
                                const t = child.userData.pulsePhase;
                                const scale = 1 + t * 5;
                                child.scale.set(scale, scale, 1);
                                child.material.opacity = (1 - t) * 0.6;
                                child.material.color.copy(baseColor);
                            }}
                            
                            // 🔥 信标光柱（独立处理）
                            if (child.userData.isBeacon) {{
                                child.rotation.y += 0.005;
                                const p = Math.sin(now * 0.002 * pulseSpeed) * 0.5 + 0.5;
                                if (child.userData.plane1) child.userData.plane1.material.opacity = (0.5 + 0.4 * p);
                                if (child.userData.plane2) child.userData.plane2.material.opacity = (0.5 + 0.4 * p);
                            }}
                        }});
                    }}
                
                    // ----- InstancedMesh 脉冲（保留原有逻辑） -----
                    if (group.isInstancedMesh && group.userData && group.userData.hasPulse) {{
                        const speed = group.userData.pulseSpeed || 0.5;
                        const pulse = Math.sin(Date.now() * 0.003 * speed) * 0.5 + 0.5;
                        if (group.material) {{
                            group.material.emissiveIntensity = 1.5 * pulse;
                        }}
                    }}
                }});

                if (particles) {{
                    const pos = particles.geometry.attributes.position.array;
                    const initial = particles.userData.initialPositions;
                    const spds = particles.userData.speeds;
                    const t = Date.now() * 0.0002;
                    for (let i = 0; i < pos.length / 3; i++) {{
                        const idx = i * 3;
                        pos[idx] = initial[idx] + Math.sin(t * spds[i] + i) * 0.6;
                        pos[idx + 1] = initial[idx + 1] + Math.sin(t * spds[i] * 0.7 + i * 2) * 0.4;
                        pos[idx + 2] = initial[idx + 2] + Math.cos(t * spds[i] * 0.5 + i * 1.5) * 0.6;
                    }}
                    particles.geometry.attributes.position.needsUpdate = true;
                }}

                composer.render();
                labelRenderer.render(scene, camera);
            }}
            
            // ============================================================
            // 6.9+ 导览路径编辑器
            // ============================================================
            const tourEditor = document.getElementById('tour-editor');
            const tourEditBtn = document.getElementById('tour-edit-btn');
            const tourEditorClose = document.getElementById('tour-editor-close');
            const tourEditorPin = document.getElementById('tour-editor-pin');
            
            // 🔥 固定状态持久化
            let tourEditorPinned = localStorage.getItem('tourEditorPinned') === 'true';
            if (tourEditorPinned && tourEditor) {{
                tourEditor.classList.add('pinned');
            }}
            
            // 🔥 固定按钮切换
            if (tourEditorPin) {{
                tourEditorPin.addEventListener('click', (e) => {{
                    e.stopPropagation();
                    tourEditorPinned = !tourEditorPinned;
                    tourEditor.classList.toggle('pinned', tourEditorPinned);
                    localStorage.setItem('tourEditorPinned', tourEditorPinned);
                    console.log(tourEditorPinned ? '📌 导览面板已固定' : '📍 导览面板已取消固定');
                }});
            }}
            const addTourStopBtn = document.getElementById('add-tour-stop');
            const tourStopList = document.getElementById('tour-stop-list');
            const resetTourBtn = document.getElementById('reset-tour');
            const saveTourBtn = document.getElementById('save-tour');
            
            let editingStops = [];
            
            function openTourEditor() {{
                // 🔥 互斥 1：关闭时间轴
                const tlPanel = document.getElementById('timeline-panel');
                const tlClose = document.getElementById('timeline-close');
                if (tlPanel && tlPanel.style.display === 'block' && tlClose) {{
                    tlClose.click();
                    console.log('🔀 编辑面板打开 → 已关闭时间轴');
                }}

                // 🔥 互斥 2：停止导览
                if (typeof tourActive !== 'undefined' && tourActive) {{
                    stopTour();
                    console.log('🔀 编辑面板打开 → 已停止导览');
                }}

                editingStops = JSON.parse(JSON.stringify(tourStops));
                renderTourEditor();
                tourEditor.classList.add('visible');
            }}
            
            function closeTourEditor() {{
                tourEditor.classList.remove('visible');
            }}
            
            function renderTourEditor() {{
                if (editingStops.length === 0) {{
                    tourStopList.innerHTML = '<div class="empty-tip">还没有导览站点<br>调整视角后点击上方按钮添加</div>';
                    return;
                }}
            
                tourStopList.innerHTML = '';
                editingStops.forEach((stop, idx) => {{
                    const item = document.createElement('div');
                    item.className = 'stop-item';
                    item.innerHTML = `
                        <div class="stop-index">${{idx + 1}}</div>
                        <div class="stop-name" data-idx="${{idx}}" title="双击重命名">${{stop.name}}</div>
                        <div class="stop-actions">
                            <button data-action="up" data-idx="${{idx}}" title="上移" ${{idx === 0 ? 'disabled' : ''}}>↑</button>
                            <button data-action="down" data-idx="${{idx}}" title="下移" ${{idx === editingStops.length - 1 ? 'disabled' : ''}}>↓</button>
                            <button data-action="goto" data-idx="${{idx}}" title="跳转到此视角">🎯</button>
                            <button data-action="delete" data-idx="${{idx}}" class="danger" title="删除">✕</button>
                        </div>
                    `;
                    tourStopList.appendChild(item);
                }});
            
                tourStopList.querySelectorAll('.stop-actions button').forEach(btn => {{
                    btn.addEventListener('click', (e) => {{
                        e.stopPropagation();
                        const action = btn.dataset.action;
                        const idx = parseInt(btn.dataset.idx);
                        handleStopAction(action, idx);
                    }});
                }});
            
                tourStopList.querySelectorAll('.stop-name').forEach(el => {{
                    el.addEventListener('dblclick', () => {{
                        const idx = parseInt(el.dataset.idx);
                        const currentName = editingStops[idx].name;
                        const input = document.createElement('input');
                        input.type = 'text';
                        input.value = currentName;
                        input.style.cssText = 'width:100%;background:rgba(74,144,217,0.2);border:1px solid #4a90d9;border-radius:4px;color:#fff;padding:2px 6px;font-size:13px;outline:none;';
                        el.replaceWith(input);
                        input.focus();
                        input.select();
            
                        const finishEdit = () => {{
                            const newName = input.value.trim() || currentName;
                            editingStops[idx].name = newName;
                            renderTourEditor();
                        }};
                        input.addEventListener('blur', finishEdit);
                        input.addEventListener('keydown', (e) => {{
                            if (e.key === 'Enter') input.blur();
                            if (e.key === 'Escape') {{ input.value = currentName; input.blur(); }}
                        }});
                    }});
                }});
            }}
            
            function handleStopAction(action, idx) {{
                if (action === 'up' && idx > 0) {{
                    [editingStops[idx - 1], editingStops[idx]] = [editingStops[idx], editingStops[idx - 1]];
                }} else if (action === 'down' && idx < editingStops.length - 1) {{
                    [editingStops[idx], editingStops[idx + 1]] = [editingStops[idx + 1], editingStops[idx]];
                }} else if (action === 'delete') {{
                    editingStops.splice(idx, 1);
                }} else if (action === 'goto') {{
                    const stop = editingStops[idx];
                    const startPos = camera.position.clone();
                    const startTarget = controls.target.clone();
                    const endPos = new THREE.Vector3(stop.position.x, stop.position.y, stop.position.z);
                    const endTarget = new THREE.Vector3(stop.target.x, stop.target.y, stop.target.z);
                    const startTime = performance.now();
                    const duration = 800;
            
                    function jumpAnimate() {{
                        const elapsed = performance.now() - startTime;
                        const t = Math.min(elapsed / duration, 1);
                        const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
                        camera.position.lerpVectors(startPos, endPos, ease);
                        controls.target.lerpVectors(startTarget, endTarget, ease);
                        camera.lookAt(controls.target);
                        if (t < 1) requestAnimationFrame(jumpAnimate);
                    }}
                    jumpAnimate();
                    return;
                }}
                renderTourEditor();
            }}
            
            if (addTourStopBtn) {{
                addTourStopBtn.addEventListener('click', () => {{
                    const name = `视角 ${{editingStops.length + 1}}`;
                    editingStops.push({{
                        name: name,
                        position: {{
                            x: parseFloat(camera.position.x.toFixed(2)),
                            y: parseFloat(camera.position.y.toFixed(2)),
                            z: parseFloat(camera.position.z.toFixed(2))
                        }},
                        target: {{
                            x: parseFloat(controls.target.x.toFixed(2)),
                            y: parseFloat(controls.target.y.toFixed(2)),
                            z: parseFloat(controls.target.z.toFixed(2))
                        }},
                        duration: 2500,
                        stay: 2000
                    }});
                    renderTourEditor();
                }});
            }}
            
            if (tourEditBtn) {{
                tourEditBtn.addEventListener('click', openTourEditor);
            }}
            if (tourEditorClose) {{
                tourEditorClose.addEventListener('click', closeTourEditor);
            }}
            
            if (resetTourBtn) {{
                resetTourBtn.addEventListener('click', () => {{
                    if (confirm('确定恢复为默认导览路径？当前自定义路径将被清除。')) {{
                        editingStops = JSON.parse(JSON.stringify(DEFAULT_TOUR_STOPS));
                        renderTourEditor();
                        tourStops = JSON.parse(JSON.stringify(DEFAULT_TOUR_STOPS));
                        saveTourStops(tourStops);
                        updateTourDots();
                    }}
                }});
            }}
            
            if (saveTourBtn) {{
                saveTourBtn.addEventListener('click', () => {{
                    if (editingStops.length === 0) {{
                        alert('请至少添加一个导览站点');
                        return;
                    }}
                    tourStops = JSON.parse(JSON.stringify(editingStops));
                    saveTourStops(tourStops);
                    updateTourDots();
                    closeTourEditor();
                    setTimeout(() => startTour(), 300);
                }});
            }}
            
            // ===== 工具箱开关逻辑（Figma 风格） =====
            const toolboxBtn = document.getElementById('toolbox-btn');
            const toolboxPanel = document.getElementById('toolbox-panel');
            const toolboxCloseBtn = document.getElementById('toolbox-close-btn');
            
            function openToolbox() {{
                toolboxPanel.classList.add('visible');
                toolboxBtn.classList.add('active');
            }}
            function closeToolbox() {{
                toolboxPanel.classList.remove('visible');
                toolboxBtn.classList.remove('active');
            }}
            
            if (toolboxBtn && toolboxPanel) {{
                // 点击齿轮切换开/关
                toolboxBtn.addEventListener('click', (e) => {{
                    e.stopPropagation();
                    if (toolboxPanel.classList.contains('visible')) {{
                        closeToolbox();
                    }} else {{
                        openToolbox();
                    }}
                }});
            
                // 点击面板内部不关闭
                toolboxPanel.addEventListener('click', (e) => {{
                    e.stopPropagation();
                }});
            
                // 点击叉号关闭
                if (toolboxCloseBtn) {{
                    toolboxCloseBtn.addEventListener('click', (e) => {{
                        e.stopPropagation();
                        closeToolbox();
                    }});
                }}
            
                // ✅ 已删除"点击外部关闭"逻辑
                // ✅ 已删除"ESC 关闭"逻辑
                // 只有点叉或再次点击齿轮按钮才会关闭
            }}
            
                        // ============================================================
            // 任务：设置迁移到工具箱
            // ============================================================
            
            // ---------- 自动旋转开关 ----------
            const autorotateInput = document.getElementById('toggle-autorotate-input');
            let autorotateEnabled = localStorage.getItem('pref_auto_rotate') !== 'false';
            if (autorotateInput) {{
                autorotateInput.checked = autorotateEnabled;
                controls.autoRotate = autorotateEnabled;
                autorotateInput.addEventListener('change', () => {{
                    autorotateEnabled = autorotateInput.checked;
                    controls.autoRotate = autorotateEnabled;
                    localStorage.setItem('pref_auto_rotate', autorotateEnabled);
                }});
            }}
            
            // ---------- 显示网格 ----------
            const gridInput = document.getElementById('toggle-grid-input');
            let gridEnabled = localStorage.getItem('pref_show_grid') !== 'false';
            if (gridInput) {{
                gridInput.checked = gridEnabled;
                grid.visible = gridEnabled;
                gridInput.addEventListener('change', () => {{
                    gridEnabled = gridInput.checked;
                    grid.visible = gridEnabled;
                    localStorage.setItem('pref_show_grid', gridEnabled);
                }});
            }}
            
            // ---------- 显示标签 ----------
            const labelsInput = document.getElementById('toggle-labels-input');
            let labelsEnabled = localStorage.getItem('pref_show_labels') !== 'false';
            if (labelsInput) {{
                labelsInput.checked = labelsEnabled;
                labelRenderer.domElement.style.display = labelsEnabled ? 'block' : 'none';
                labelsInput.addEventListener('change', () => {{
                    labelsEnabled = labelsInput.checked;
                    labelRenderer.domElement.style.display = labelsEnabled ? 'block' : 'none';
                    localStorage.setItem('pref_show_labels', labelsEnabled);
                }});
            }}
            
            // ---------- 操作提示 ----------
            const toastInput = document.getElementById('toggle-toast-input');
            let toastEnabled = localStorage.getItem('pref_toast') !== 'false';
            if (toastInput) {{
                toastInput.checked = toastEnabled;
                toastInput.addEventListener('change', () => {{
                    toastEnabled = toastInput.checked;
                    localStorage.setItem('pref_toast', toastEnabled);
                }});
            }}
            
            // ---------- 告警提醒 ----------
            const alertInput = document.getElementById('toggle-alert-input');
            let alertEnabled = localStorage.getItem('pref_alert') !== 'false';
            if (alertInput) {{
                alertInput.checked = alertEnabled;
                alertInput.addEventListener('change', () => {{
                    alertEnabled = alertInput.checked;
                    localStorage.setItem('pref_alert', alertEnabled);
                }});
            }}
            
            // ---------- 告警阈值滑块 ----------
            const thresholdSlider = document.getElementById('alert-threshold-slider');
            const thresholdValue = document.getElementById('alert-threshold-value');
            let currentThreshold = getAlertThreshold();
            if (thresholdSlider) {{
                thresholdSlider.value = currentThreshold;
                thresholdValue.textContent = Math.round(currentThreshold * 100) + '%';
                thresholdSlider.addEventListener('input', () => {{
                    currentThreshold = parseFloat(thresholdSlider.value);
                    thresholdValue.textContent = Math.round(currentThreshold * 100) + '%';
                    localStorage.setItem('alert_threshold', currentThreshold);
                }});
            }}
            
            // ---------- 用户手册弹窗 ----------
            const manualOverlay = document.getElementById('user-manual-overlay');
            const openManualBtn = document.getElementById('open-manual-btn');
            const manualCloseBtn = document.getElementById('manual-close-btn');
            
            if (openManualBtn) {{
                openManualBtn.addEventListener('click', () => {{
                    manualOverlay.classList.add('visible');
                }});
            }}
            if (manualCloseBtn) {{
                manualCloseBtn.addEventListener('click', () => {{
                    manualOverlay.classList.remove('visible');
                }});
            }}
            if (manualOverlay) {{
                manualOverlay.addEventListener('click', (e) => {{
                    if (e.target === manualOverlay) {{
                        manualOverlay.classList.remove('visible');
                    }}
                }});
                window.addEventListener('keydown', (e) => {{
                    if (e.key === 'Escape' && manualOverlay.classList.contains('visible')) {{
                        manualOverlay.classList.remove('visible');
                    }}
                }});
            }}
            
            // ---------- 清除本地缓存 ----------
            const clearCacheBtn = document.getElementById('clear-cache-btn');
            if (clearCacheBtn) {{
                clearCacheBtn.addEventListener('click', () => {{
                    if (confirm('⚠️ 将清空相机位置、UI 偏好等本地缓存，确定继续？')) {{
                        const keysToKeep = ['tour_stops_', 'scene_', 'dtt_camera_state'];
                        const keysToRemove = [];
                        for (let i = 0; i < localStorage.length; i++) {{
                            const key = localStorage.key(i);
                            if (!keysToKeep.some(k => key.startsWith(k))) {{
                                keysToRemove.push(key);
                            }}
                        }}
                        keysToRemove.forEach(k => localStorage.removeItem(k));
                        alert('✅ 缓存已清空，页面将刷新');
                        location.reload();
                    }}
                }});
            }}
            
            // --- 信标光柱开关 ---
            let beaconEnabled = localStorage.getItem('beaconEnabled') === 'true';
            const beaconInput = document.getElementById('toggle-beacon-input');
            
            function updateBeaconVisibility() {{
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.visualLayer) return;
                    group.userData.visualLayer.traverse(child => {{
                        if (child.userData.isBeacon) child.visible = beaconEnabled;
                    }});
                }});
            }}
            
            if (beaconInput) {{
                beaconInput.checked = beaconEnabled;
                beaconInput.addEventListener('change', () => {{
                    beaconEnabled = beaconInput.checked;
                    localStorage.setItem('beaconEnabled', beaconEnabled);
                    updateBeaconVisibility();
                }});
            }}
            
            // --- 扩散环开关 ---
            let pulseRingEnabled = localStorage.getItem('pulseRingEnabled') !== 'false';
            const pulseRingInput = document.getElementById('toggle-pulse-ring-input');
            
            function updatePulseRingVisibility() {{
                objectMeshes.forEach(group => {{
                    if (!group.userData || !group.userData.visualLayer) return;
                    group.userData.visualLayer.traverse(child => {{
                        if (child.userData.isPulseRing) child.visible = pulseRingEnabled;
                    }});
                }});
            }}
            
            if (pulseRingInput) {{
                pulseRingInput.checked = pulseRingEnabled;
                pulseRingInput.addEventListener('change', () => {{
                    pulseRingEnabled = pulseRingInput.checked;
                    localStorage.setItem('pulseRingEnabled', pulseRingEnabled);
                    updatePulseRingVisibility();
                }});
            }}

            // --- 关系线开关 ---
            let relationsEnabled = localStorage.getItem('showRelations') !== 'false';
            const relationsInput = document.getElementById('toggle-relations-input');

            if (relationsInput) {{
                relationsInput.checked = relationsEnabled;
                relationsInput.addEventListener('change', () => {{
                    relationsEnabled = relationsInput.checked;
                    localStorage.setItem('showRelations', relationsEnabled);
                    if (relationsGroup) relationsGroup.visible = relationsEnabled;
                }});
            }}
            
            // ============================================================
            // 6.2 第一人称漫游（FPV）
            // ============================================================
            let fpvMode = false;
            const fpvIndicator = document.getElementById('fpv-indicator');
            const keys = {{ w: false, a: false, s: false, d: false, q: false, e: false, shift: false }};
            const fpvSpeed = 0.15;
            let fpvYaw = 0, fpvPitch = 0;
            let isMouseDown = false;
            let lastMouseX = 0, lastMouseY = 0;
            
            window.addEventListener('keydown', (e) => {{
                // 🔥 如果焦点在输入框/文本域里，忽略快捷键
                const tag = (e.target && e.target.tagName) || '';
                if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) {{
                    return;
                }}
                
                const k = e.key.toLowerCase();
                if (k === 'f') {{
                    toggleFPV();
                    return;
                }}
                if (k === 'w') keys.w = true;
                if (k === 'a') keys.a = true;
                if (k === 's') keys.s = true;
                if (k === 'd') keys.d = true;
                if (k === 'q') keys.q = true;
                if (k === 'e') keys.e = true;
                if (e.shiftKey) keys.shift = true;
            }});
            window.addEventListener('keyup', (e) => {{
                const k = e.key.toLowerCase();
                if (k === 'w') keys.w = false;
                if (k === 'a') keys.a = false;
                if (k === 's') keys.s = false;
                if (k === 'd') keys.d = false;
                if (k === 'q') keys.q = false;
                if (k === 'e') keys.e = false;
                if (e.key === 'Shift') keys.shift = false;
            }});
            
            renderer.domElement.addEventListener('mousedown', (e) => {{
                if (!fpvMode) return;
                isMouseDown = true;
                lastMouseX = e.clientX;
                lastMouseY = e.clientY;
            }});
            window.addEventListener('mouseup', () => {{ isMouseDown = false; }});
            window.addEventListener('mousemove', (e) => {{
                if (!fpvMode || !isMouseDown) return;
                const dx = e.clientX - lastMouseX;
                const dy = e.clientY - lastMouseY;
                lastMouseX = e.clientX;
                lastMouseY = e.clientY;
                fpvYaw -= dx * 0.003;
                fpvPitch -= dy * 0.003;
                fpvPitch = Math.max(-Math.PI / 2.5, Math.min(Math.PI / 2.5, fpvPitch));
            
                const lookDir = new THREE.Vector3(
                    Math.sin(fpvYaw) * Math.cos(fpvPitch),
                    Math.sin(fpvPitch),
                    Math.cos(fpvYaw) * Math.cos(fpvPitch)
                );
                const target = camera.position.clone().add(lookDir);
                camera.lookAt(target);
            }});
            
            function toggleFPV() {{
                fpvMode = !fpvMode;
                if (fpvMode) {{
                    const dir = new THREE.Vector3();
                    camera.getWorldDirection(dir);
                    fpvPitch = Math.asin(dir.y);
                    fpvYaw = Math.atan2(dir.x, dir.z);
            
                    controls.enabled = false;
                    controls.autoRotate = false;
                    fpvIndicator.classList.add('visible');
                    renderer.domElement.style.cursor = 'grab';
                    console.log('🎮 进入第一人称漫游模式');
                }} else {{
                    controls.enabled = true;
                    controls.autoRotate = false;
                    fpvIndicator.classList.remove('visible');
                    renderer.domElement.style.cursor = 'default';
                    const dir = new THREE.Vector3();
                    camera.getWorldDirection(dir);
                    controls.target.copy(camera.position.clone().add(dir.multiplyScalar(5)));
                    controls.update();
                    console.log('🎥 退出第一人称漫游模式');
                }}
            }}
            
            function updateFPVCamera() {{
                if (!fpvMode) return;
            
                const speed = keys.shift ? fpvSpeed * 3 : fpvSpeed;
                const forward = new THREE.Vector3();
                camera.getWorldDirection(forward);
                const right = new THREE.Vector3();
                right.crossVectors(forward, new THREE.Vector3(0, 1, 0)).normalize();
            
                if (keys.w) camera.position.addScaledVector(forward, speed);
                if (keys.s) camera.position.addScaledVector(forward, -speed);
                if (keys.a) camera.position.addScaledVector(right, -speed);
                if (keys.d) camera.position.addScaledVector(right, speed);
                if (keys.q) camera.position.y -= speed;
                if (keys.e) camera.position.y += speed;
            
                if (camera.position.y < 0.5) camera.position.y = 0.5;
            }}
            
            // ============================================================
            // 🔥 P4-②：工具箱扩展 —— 承接原自然语言输入框的三项能力
            //   1) 按类型显示/隐藏   2) 高负载高亮   3) 视角预设
            // 其余指令能力已确认无需迁移（外观面板 / 本工具箱 / 底部指标条 / 故事面板已有）。
            // ============================================================
            const OBJ_TYPE_FILTERS = {{
                tree:     g => g.userData.type && g.userData.type.startsWith('tree'),
                building: g => g.userData.type === 'building',
                charger:  g => g.userData.type && g.userData.type.startsWith('charger'),
                lamp:     g => g.userData.type === 'lamp',
                vehicle:  g => g.userData.type === 'car' || g.userData.type === 'truck',
            }};

            function setTypeVisible(key, visible) {{
                const match = OBJ_TYPE_FILTERS[key];
                if (!match) return;
                objectMeshes.forEach(g => {{
                    if (g.userData && match(g)) g.visible = visible;
                }});
            }}

            Object.keys(OBJ_TYPE_FILTERS).forEach(key => {{
                const el = document.getElementById('toggle-vis-' + key);
                if (el) el.addEventListener('change', () => setTypeVisible(key, el.checked));
            }});

            const showAllObjsBtn = document.getElementById('show-all-objs-btn');
            if (showAllObjsBtn) {{
                showAllObjsBtn.addEventListener('click', () => {{
                    objectMeshes.forEach(g => {{ if (g.visible !== undefined) g.visible = true; }});
                    document.querySelectorAll('[id^="toggle-vis-"]').forEach(el => {{ el.checked = true; }});
                    if (typeof showToast === 'function') showToast('✅ 已显示所有物体');
                }});
            }}

            // 高负载高亮：复刻原「显示高负载」的缩放高亮，但会记录并还原原始缩放
            // （原实现复位时直接写 scale=1，会把非 1 缩放的物体改坏）
            const _hlOrigScale = new Map();
            function applyHighLoadHighlight(on) {{
                const th = getAlertThreshold();
                let n = 0;
                objectMeshes.forEach(g => {{
                    if (g.isInstancedMesh || !g.userData || !g.userData.objectId) return;
                    if (on) {{
                        const obj = objects.find(o => o.id === g.userData.objectId);
                        if (obj && (obj.utilization || 0) > th) {{
                            if (!_hlOrigScale.has(g)) _hlOrigScale.set(g, g.scale.clone());
                            g.scale.set(1.3, 1.3, 1.3);
                            n++;
                        }}
                    }} else {{
                        const s = _hlOrigScale.get(g);
                        if (s) {{ g.scale.copy(s); _hlOrigScale.delete(g); }}
                    }}
                }});
                return n;
            }}
            const hlInput = document.getElementById('toggle-highload-input');
            if (hlInput) {{
                hlInput.addEventListener('change', () => {{
                    const n = applyHighLoadHighlight(hlInput.checked);
                    if (hlInput.checked && typeof showToast === 'function') {{
                        showToast('✅ 已高亮 ' + n + ' 个高负载站点');
                    }}
                }});
            }}

            // 视角预设（坐标与原指令一致；侧视 / 低角度 / 默认视角按计划舍弃）
            const VIEW_PRESETS = {{
                reset:   [[12, 10, 15], [0, 1.5, 0]],
                pano:    [[0, 25, 20],  [0, 0, 0]],
                closeup: [[8, 5, 8],    [0, 1, 0]],
            }};
            Object.keys(VIEW_PRESETS).forEach(key => {{
                const btn = document.getElementById('view-' + key + '-btn');
                if (!btn) return;
                btn.addEventListener('click', () => {{
                    const p = VIEW_PRESETS[key];
                    camera.position.set(p[0][0], p[0][1], p[0][2]);
                    controls.target.set(p[1][0], p[1][1], p[1][2]);
                    controls.update();
                }});
            }});

            // ============================================================
            // 6.3 阈值告警与视觉反馈
            // ============================================================
            const alertContainer = document.getElementById('alert-container');
            const alertedStations = new Map();
            const ALERT_COOLDOWN = 30000;
            
            function showAlert(stationId, utilization, objName) {{
                if (localStorage.getItem('pref_alert') === 'false') return;
                const now = Date.now();
                const lastAlert = alertedStations.get(stationId) || 0;
                if (now - lastAlert < ALERT_COOLDOWN) return;
                alertedStations.set(stationId, now);
            
                const toast = document.createElement('div');
                toast.className = 'alert-toast';
                toast.innerHTML = `
                    <div class="alert-title">
                        <span>⚠️ 高负载告警</span>
                        <button class="alert-close">✕</button>
                    </div>
                        <div class="alert-body">
                        <strong>${{objName || stationId}}</strong> 利用率达 <strong>${{(utilization * 100).toFixed(1)}}%</strong><br>
                        <span style="opacity:0.75;font-size:11px;">当前告警阈值：${{(getAlertThreshold() * 100).toFixed(0)}}%</span><br>
                        <span style="opacity:0.85;">建议：关注该站点充电排队情况</span>
                    </div>
                `;
            
                toast.querySelector('.alert-close').addEventListener('click', () => {{
                    toast.classList.add('fade-out');
                    setTimeout(() => toast.remove(), 500);
                }});
            
                alertContainer.appendChild(toast);
            
                setTimeout(() => {{
                    if (toast.parentNode) {{
                        toast.classList.add('fade-out');
                        setTimeout(() => toast.remove(), 500);
                    }}
                }}, 8000);
            
                console.log(`⚠️ 告警: ${{objName || stationId}} 利用率 ${{(utilization * 100).toFixed(1)}}%`);
            }}
            
            // ============================================================
            // 6.9 自动导览 / 镜头动画
            // ============================================================
            tourBtn = document.getElementById('tour-btn');
            tourProgress = document.getElementById('tour-progress');
            tourDots = tourProgress ? tourProgress.querySelectorAll('.dot') : [];
            
            const TOUR_STORAGE_KEY = `tour_stops_${{sceneId || 'default'}}`;

            const DEFAULT_TOUR_STOPS = [
                {{ name: '全景俯瞰', position: {{ x: 0, y: 25, z: 20 }}, target: {{ x: 0, y: 0, z: 0 }}, duration: 2500, stay: 2000 }},
                {{ name: '充电站特写', position: {{ x: 8, y: 5, z: 8 }}, target: {{ x: 0, y: 1, z: 0 }}, duration: 2500, stay: 2000 }},
                {{ name: '侧视视角', position: {{ x: -15, y: 8, z: 0 }}, target: {{ x: 0, y: 1, z: 0 }}, duration: 2500, stay: 2000 }},
                {{ name: '低角度漫游', position: {{ x: 5, y: 2, z: -12 }}, target: {{ x: 0, y: 1.5, z: 0 }}, duration: 2500, stay: 2000 }}
            ];
            
            function loadTourStops() {{
                try {{
                    const stored = localStorage.getItem(TOUR_STORAGE_KEY);
                    if (stored) {{
                        const parsed = JSON.parse(stored);
                        if (Array.isArray(parsed) && parsed.length > 0) {{
                            console.log(`✅ 从 localStorage 加载 ${{parsed.length}} 个导览站点`);
                            return parsed;
                        }}
                    }}
                }} catch (e) {{
                    console.warn('⚠️ 加载导览路径失败:', e);
                }}
                return JSON.parse(JSON.stringify(DEFAULT_TOUR_STOPS));
            }}
            
            function saveTourStops(stops) {{
                try {{
                    localStorage.setItem(TOUR_STORAGE_KEY, JSON.stringify(stops));
                    console.log(`💾 已保存 ${{stops.length}} 个导览站点`);
                }} catch (e) {{
                    console.warn('⚠️ 保存导览路径失败:', e);
                }}
            }}
            
            let tourStops = loadTourStops();
            
            function updateTourDots() {{
                const progress = document.getElementById('tour-progress');
                if (!progress) return;
                progress.innerHTML = '';
                tourStops.forEach((_, i) => {{
                    const dot = document.createElement('div');
                    dot.className = 'dot';
                    dot.dataset.idx = i;
                    progress.appendChild(dot);
                }});
            }}
            
            updateTourDots();
            
            let tourActive = false;
            let tourCurrentIndex = 0;
            
            function startTour() {{
                // 🔥 互斥 1：关闭时间轴
                const tlPanel = document.getElementById('timeline-panel');
                const tlClose = document.getElementById('timeline-close');
                if (tlPanel && tlPanel.style.display === 'block' && tlClose) {{
                    tlClose.click();
                    console.log('🔀 导览启动 → 已关闭时间轴');
                }}

                // 🔥 互斥 2：关闭导览编辑面板
                const editPanel = document.getElementById('tour-editor');
                const editClose = document.getElementById('tour-editor-close');
                if (editPanel && editPanel.classList.contains('visible') && editClose) {{
                    editClose.click();
                    console.log('🔀 导览启动 → 已关闭编辑面板');
                }}

                if (tourActive) {{
                    stopTour();
                    return;
                }}
                if (fpvMode) toggleFPV();

                tourActive = true;
                tourCurrentIndex = 0;
                tourBtn.classList.add('active');
                tourBtn.textContent = '⏹ 停止导览';
                tourProgress.classList.add('visible');
                controls.autoRotate = false;
                controls.enabled = false;

                playTourStop(0);
            }}
            
            function stopTour() {{
                tourActive = false;
                tourBtn.classList.remove('active');
                tourBtn.textContent = '🎥 自动导览';
                tourProgress.classList.remove('visible');
                // 🔥 重新查询，避免过期引用
                const dots = document.querySelectorAll('#tour-progress .dot');
                dots.forEach(d => d.classList.remove('active'));
                controls.enabled = true;
                controls.update();
            }}
            
            function playTourStop(index) {{
                if (!tourActive || index >= tourStops.length) {{
                    if (tourActive) stopTour();
                    return;
                }}
            
                tourCurrentIndex = index;
                const dots = document.querySelectorAll('#tour-progress .dot');
                dots.forEach((d, i) => {{
                    d.classList.toggle('active', i === index);
                }});
            
                const stop = tourStops[index];
                const startPos = camera.position.clone();
                const startTarget = controls.target.clone();
                const endPos = new THREE.Vector3(stop.position.x, stop.position.y, stop.position.z);
                const endTarget = new THREE.Vector3(stop.target.x, stop.target.y, stop.target.z);
            
                const startTime = performance.now();
                const duration = stop.duration;
            
                function animateTour() {{
                    if (!tourActive) return;
                    const elapsed = performance.now() - startTime;
                    const t = Math.min(elapsed / duration, 1);
                    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
            
                    camera.position.lerpVectors(startPos, endPos, ease);
                    controls.target.lerpVectors(startTarget, endTarget, ease);
                    camera.lookAt(controls.target);
            
                    if (t < 1) {{
                        requestAnimationFrame(animateTour);
                    }} else {{
                        setTimeout(() => {{
                            if (tourActive) playTourStop(index + 1);
                        }}, stop.stay);
                    }}
                }}
            
                animateTour();
                console.log(`🎥 导览：${{stop.name}}（${{index + 1}}/${{tourStops.length}}）`);
            }}
            
            if (tourBtn) {{
                tourBtn.addEventListener('click', startTour);
            }}
            
            // ============================================================
            // 6.6 AI语音播报（Web Speech API）
            // ============================================================
            const voiceBtn = document.getElementById('voice-btn');
            
            function speakText(text) {{
                if (!('speechSynthesis' in window)) {{
                    console.warn('⚠️ 当前浏览器不支持语音播报');
                    return;
                }}
                window.speechSynthesis.cancel();
                const utterance = new SpeechSynthesisUtterance(text);
                utterance.lang = 'zh-CN';
                utterance.rate = 1.0;
                utterance.pitch = 1.0;
                utterance.onstart = () => voiceBtn.classList.add('speaking');
                utterance.onend = () => voiceBtn.classList.remove('speaking');
                window.speechSynthesis.speak(utterance);
            }}
            
            function buildSceneReport() {{
                const total = objects.length;
                const chargers = objects.filter(o => o.type && o.type.startsWith('charger'));
                const utils = chargers.map(c => c.utilization || 0.5);
                const avgU = utils.length > 0 ? utils.reduce((a, b) => a + b, 0) / utils.length : 0;
                const high = utils.filter(u => u > 0.7).length;
            
                let text = `当前场景共 ${{total}} 个物体，其中充电桩 ${{chargers.length}} 个。`;
                text += `平均利用率 ${{(avgU * 100).toFixed(0)}}%。`;
                if (high > 0) {{
                    text += `有 ${{high}} 个站点处于高负载状态，建议关注。`;
                }} else {{
                    text += `整体运行平稳。`;
                }}
                return text;
            }}
            
            if (voiceBtn) {{
                voiceBtn.addEventListener('click', () => {{
                    if (window.speechSynthesis && window.speechSynthesis.speaking) {{
                        window.speechSynthesis.cancel();
                        voiceBtn.classList.remove('speaking');
                    }} else {{
                        speakText(buildSceneReport());
                    }}
                }});
            }}
            
                        // ============================================================
            // 任务9：3D 时间轴回放
            // ============================================================
            (function initTimeline() {{
                const timelineData = sceneData.timeline;
                const timelinePanel = document.getElementById('timeline-panel');
                const timelineOpenBtn = document.getElementById('timeline-open-btn');
                const timelineCloseBtn = document.getElementById('timeline-close');
                const timelineSlider = document.getElementById('timeline-slider');
                const timelinePlayBtn = document.getElementById('timeline-play');
                const timelineTime = document.getElementById('timeline-time');
                const timelineStep = document.getElementById('timeline-step');

                // 无数据：不启用
                if (!timelineData || !timelineData.timeline || timelineData.timeline.length === 0) {{
                    if (timelineOpenBtn) timelineOpenBtn.style.display = 'none';
                    console.log('ℹ️ 时间轴无数据，功能未启用');
                    return;
                }}

                const maxSteps = timelineData.maxSteps || timelineData.timeline.length;
                timelineSlider.max = maxSteps - 1;

                // 站点 ID → 索引 映射
                const stationIndexMap = {{}};
                timelineData.stationIds.forEach((sid, idx) => {{
                    stationIndexMap[String(sid)] = idx;
                }});

                let timelinePlaying = false;
                let timelineTimer = null;
                let timelineCurrentStep = 0;
                // 记录播放前的原始利用率，退出时恢复
                const originalUtils = {{}};
                objects.forEach(o => {{
                    originalUtils[o.id] = o.utilization || 0.5;
                }});

                // ===== 核心：应用某一帧 =====
                function applyTimelineFrame(stepIndex) {{
                    const step = Math.max(0, Math.min(stepIndex, maxSteps - 1));
                    const utils = timelineData.matrix[step] || [];

                    objectMeshes.forEach(group => {{
                        if (!group.userData || !group.userData.objectId) return;
                        const objData = objects.find(o => o.id === group.userData.objectId);
                        if (!objData) return;

                        const sid = String(objData.bind_station_id || '');
                        let util = null;

                        if (sid && stationIndexMap[sid] !== undefined) {{
                            util = utils[stationIndexMap[sid]];
                        }}

                        if (util === null || util === undefined) {{
                            // 未绑定：保持原值
                            util = objData.utilization || 0.5;
                        }}

                        // 调用现有的视觉更新函数
                        updateChargerVisual(group, util);
                    }});

                    // 更新 UI
                    timelineTime.textContent = timelineData.timeline[step] || '--';
                    timelineStep.textContent = step + ' / ' + (maxSteps - 1);
                    timelineSlider.value = step;
                    timelineCurrentStep = step;
                }}

                // ===== 播放 =====
                function playTimeline() {{
                    if (timelinePlaying) {{
                        stopTimeline();
                        return;
                    }}
                    timelinePlaying = true;
                    timelinePlayBtn.textContent = '⏸';
                    timelinePlayBtn.classList.add('playing');

                    timelineTimer = setInterval(() => {{
                        timelineCurrentStep += 1;
                        if (timelineCurrentStep >= maxSteps) {{
                            timelineCurrentStep = 0;
                        }}
                        applyTimelineFrame(timelineCurrentStep);
                    }}, 150);
                }}

                function stopTimeline() {{
                    if (!timelinePlaying) return;
                    timelinePlaying = false;
                    timelinePlayBtn.textContent = '▶';
                    timelinePlayBtn.classList.remove('playing');
                    if (timelineTimer) {{
                        clearInterval(timelineTimer);
                        timelineTimer = null;
                    }}
                }}

                // ===== 恢复实时状态 =====
                function restoreRealtime() {{
                    objectMeshes.forEach(group => {{
                        if (!group.userData || !group.userData.objectId) return;
                        const objData = objects.find(o => o.id === group.userData.objectId);
                        if (!objData) return;
                        // 恢复到对象当前的 utilization
                        const util = objData.utilization || 0.5;
                        updateChargerVisual(group, util);
                    }});
                }}

                // ===== 事件绑定 =====
                timelineOpenBtn.addEventListener('click', () => {{
                    const isOpening = (timelinePanel.style.display === 'none' || !timelinePanel.style.display);

                    if (isOpening) {{
                        // 🔥 互斥 1：关闭导览
                        const tourBtnEl = document.getElementById('tour-btn');
                        if (tourBtnEl && tourBtnEl.classList.contains('active')) {{
                            tourBtnEl.click();
                            console.log('🔀 时间轴打开 → 已停止导览');
                        }}

                        // 🔥 互斥 2：关闭导览编辑面板
                        const editPanel = document.getElementById('tour-editor');
                        const editClose = document.getElementById('tour-editor-close');
                        if (editPanel && editPanel.classList.contains('visible') && editClose) {{
                            editClose.click();
                            console.log('🔀 时间轴打开 → 已关闭编辑面板');
                        }}

                        // 打开时间轴
                        timelinePanel.style.display = 'block';
                        timelineOpenBtn.classList.add('active');
                        controls.autoRotate = false;
                        applyTimelineFrame(timelineCurrentStep);
                    }} else {{
                        // 关闭时间轴
                        timelinePanel.style.display = 'none';
                        timelineOpenBtn.classList.remove('active');
                        stopTimeline();
                        restoreRealtime();
                    }}
                }});

                timelineCloseBtn.addEventListener('click', () => {{
                    timelinePanel.style.display = 'none';
                    timelineOpenBtn.classList.remove('active');
                    stopTimeline();
                    restoreRealtime();
                }});

                timelineSlider.addEventListener('input', (e) => {{
                    stopTimeline();
                    const step = parseInt(e.target.value);
                    applyTimelineFrame(step);
                }});

                timelinePlayBtn.addEventListener('click', playTimeline);

                // 显示触发按钮
                timelineOpenBtn.style.display = 'flex';
                console.log(`✅ 时间轴已就绪：${{maxSteps}} 步`);
                // 🔥 暴露给外部：关闭时间轴并恢复实时状态
                window.closeTimelineFromOutside = function() {{
                    if (timelinePanel.style.display !== 'none') {{
                        timelinePanel.style.display = 'none';
                        timelineOpenBtn.classList.remove('active');
                        stopTimeline();
                        restoreRealtime();
                    }}
                }};
            }})();
            
            // ===== 任务6：开发者选项 =====
            const devOptions = sceneData.devOptions || {{}};
            if (devOptions.printConsole) {{
                console.log('📋 场景状态导出:', JSON.stringify(objects, null, 2));
            }}
            
            // ===== 任务11：传入数据，生成模拟城市 =====
            window.generateMockCity = function() {{
                // 1. 清除旧的辅助物体（避免多次点击叠加）
                scene.children.filter(c => c.userData.isHelper).forEach(c => scene.remove(c));

                const chargers = objects.filter(o => o.type && o.type.startsWith('charger'));
                if (chargers.length < 2) {{
                    console.warn('⚠️ 充电桩数量太少，无法生成城市路网');
                    if (typeof showToast === 'function') showToast('⚠️ 充电桩数量不足，无法生成城市');
                    return;
                }}

                // 2. 根据充电桩坐标计算包围盒
                const xs = chargers.map(c => c.position.x);
                const zs = chargers.map(c => c.position.z);
                const minX = Math.min(...xs) - 8;
                const maxX = Math.max(...xs) + 8;
                const minZ = Math.min(...zs) - 8;
                const maxZ = Math.max(...zs) + 8;
                const spacing = 5.0;

                // 3. 生成路网
                const roadMaterial = new THREE.MeshStandardMaterial({{ color: 0x333333, roughness: 0.9 }});
                const lineMaterial = new THREE.MeshBasicMaterial({{ color: 0xffffff }});

                // 横向道路
                for (let z = minZ; z <= maxZ; z += spacing) {{
                    const roadGeo = new THREE.BoxGeometry(maxX - minX, 0.02, 2.0);
                    const road = new THREE.Mesh(roadGeo, roadMaterial);
                    road.position.set((minX + maxX) / 2, -0.01, z);
                    road.userData.isHelper = true;
                    scene.add(road);

                    const lineGeo = new THREE.BoxGeometry(maxX - minX, 0.03, 0.1);
                    const line = new THREE.Mesh(lineGeo, lineMaterial);
                    line.position.set((minX + maxX) / 2, 0.01, z);
                    line.userData.isHelper = true;
                    scene.add(line);
                }}

                // 纵向道路
                for (let x = minX; x <= maxX; x += spacing) {{
                    const roadGeo = new THREE.BoxGeometry(2.0, 0.02, maxZ - minZ);
                    const road = new THREE.Mesh(roadGeo, roadMaterial);
                    road.position.set(x, -0.01, (minZ + maxZ) / 2);
                    road.userData.isHelper = true;
                    scene.add(road);

                    const lineGeo = new THREE.BoxGeometry(0.1, 0.03, maxZ - minZ);
                    const line = new THREE.Mesh(lineGeo, lineMaterial);
                    line.position.set(x, 0.01, (minZ + maxZ) / 2);
                    line.userData.isHelper = true;
                    scene.add(line);
                }}

                // 4. 生成低多边形建筑
                const buildingMaterial = new THREE.MeshStandardMaterial({{ color: 0x4a5a6a, roughness: 0.8 }});
                for (let x = minX; x <= maxX; x += spacing * 2) {{
                    for (let z = minZ; z <= maxZ; z += spacing * 2) {{
                        // 避开充电桩已有的位置（简单距离检测）
                        let tooClose = false;
                        for (const c of chargers) {{
                            const dx = x - c.position.x;
                            const dz = z - c.position.z;
                            if (Math.sqrt(dx * dx + dz * dz) < 3.0) {{
                                tooClose = true;
                                break;
                            }}
                        }}
                        if (tooClose) continue;

                        if (Math.random() > 0.5) {{
                            const w = 1.5 + Math.random() * 1.5;
                            const h = 2.0 + Math.random() * 4.0;
                            const d = 1.5 + Math.random() * 1.5;
                            const bGeo = new THREE.BoxGeometry(w, h, d);
                            const building = new THREE.Mesh(bGeo, buildingMaterial);
                            building.position.set(
                                x + (Math.random() - 0.5) * 1.5,
                                h / 2,
                                z + (Math.random() - 0.5) * 1.5
                            );
                            building.castShadow = true;
                            building.receiveShadow = true;
                            building.userData.isHelper = true;
                            scene.add(building);
                        }}
                    }}
                }}

                console.log('🏙️ 模拟城市已生成');
                if (typeof showToast === 'function') showToast('✅ 已生成模拟城市路网');
            }};
            
            // ===== 任务11：接收生成模拟城市的指令 =====
            const urlParams = new URLSearchParams(window.top.location.search);
            if (urlParams.get('action') === 'mock_city') {{
                console.log('🔍 接收到生成模拟城市指令');
                setTimeout(() => {{
                    if (typeof window.generateMockCity === 'function') {{
                        window.generateMockCity();
                    }} else {{
                        console.warn('⚠️ generateMockCity 函数未定义');
                    }}
                }}, 800);  // 延迟800毫秒，确保场景已加载完毕
            }}

            animate();

            console.log('🚀 场景加载完成 (含只读悬浮信息面板)');
        </script>
        <!--PONDER_INJECT-->
    </body>
    </html>
    """
    return _inject_ponder(html)


def _inject_ponder(html: str) -> str:
    """
    在生成好的场景 HTML 里注入「思索」运行时。

    🔥 为什么用字符串替换而不是写进上面的 f-string：
       该 f-string 有近 6000 行、满屏 {{ }} 转义，塞入新逻辑极易破坏原有代码；
       且课件 JSON 里的 {{ }} 也必须转义。用 replace 注入可完全规避这两点。

    🔥 为什么用注入而不是让前端回传：
       st.iframe 每次 rerun 都会重写 srcdoc → iframe 整块重建、脚本重跑。
       所以 Python 只下发"课件数据"，播放进度由前端 localStorage 自持，
       与相机状态（dtt_camera_state）同等对待。
    """
    try:
        from core.ponder_library import build_ponder_json
        payload = build_ponder_json(
            st.session_state.get('scene_objects', []) or [],
            st.session_state.get('selected_object_id') or None,
        )
    except Exception as e:
        print(f"⚠️ 思索课件数据生成失败，已跳过注入: {e}")
        return html

    # 🔥 打开请求：右侧面板按钮写入 query param 后 rerun，这里读取并让前端自动打开。
    #    走 query param 而不是"一次性 session 标记"，是因为 iframe 重建与 rerun
    #    的先后顺序不保证，query param 是稳定可见的。
    try:
        _want = st.query_params.get('ponder_open', '')
        if _want:
            payload = payload.replace(
                '"prefs":{',
                '"autoOpen":' + json.dumps(_want, ensure_ascii=False) + ',"prefs":{',
                1,
            )
            # 消费后立即清除，避免后续任何 rerun 都重复自动打开
            del st.query_params['ponder_open']
    except Exception as e:
        print(f"⚠️ 思索打开请求解析失败: {e}")

    # 🔥 JSON 里若出现 </script 会提前关闭 script 标签，必须打断
    safe = payload.replace('</', '<\\/')

    marker = '        <!--PONDER_INJECT-->'
    block = (
        '        <script type="application/json" id="ponder-data">' + safe + '</script>\n'
        '        <script src="/app/static/js/ponder.js"></script>\n'
        '        <script>\n'
        '            (function () {\n'
        '                function start() {\n'
        '                    try {\n'
        '                        if (window.__DTT_PONDER__ && window.__DTT_PONDER__.boot) {\n'
        '                            window.__DTT_PONDER__.boot();\n'
        '                        }\n'
        '                    } catch (e) { console.warn("[思索] 启动失败:", e); }\n'
        '                }\n'
        '                if (document.readyState === "loading") {\n'
        '                    document.addEventListener("DOMContentLoaded", start);\n'
        '                } else { start(); }\n'
        '            })();\n'
        '        </script>'
    )
    if marker not in html:
        print("⚠️ 未找到思索注入标记，已跳过（请检查 generate_scene_html 的标记）")
        return html
    version = _ponder_asset_version()
    html = html.replace(marker, block, 1)
    # 🔥 自动缓存击穿：静态资源被浏览器缓存后，改了代码用户却还在跑旧版，
    #    排查半天以为是自己写错了。这里按文件内容算版本号拼在 URL 上，
    #    文件一变 URL 就变，浏览器必然重新拉取。
    html = html.replace('/app/static/js/ponder.js', f'/app/static/js/ponder.js?v={version}')
    html = html.replace('/app/static/css/ponder.css', f'/app/static/css/ponder.css?v={version}')
    return html


@st.cache_data(show_spinner=False)
def _ponder_asset_version() -> str:
    """
    思索静态资源的版本号 = js+css 内容的短哈希。

    只看 mtime 在部署/回滚时不可靠，直接哈希内容最稳；
    文件不大（约 70KB），且带 cache_data 缓存，每个进程只算一次。
    """
    import hashlib
    h = hashlib.md5()
    # CSS 的 <link> 在 f-string 里写死、且出现得比占位符早，
    # 所以它的版本号必须在这里一起算出来，注入时再替换。
    for rel in ('static/js/ponder.js', 'static/css/ponder.css'):
        try:
            with open(os.path.join(project_root, rel), 'rb') as f:
                h.update(f.read())
        except OSError:
            h.update(b'missing')
    return h.hexdigest()[:10]

def sync_bound_chargers_utilization():
    """
    在页面渲染前，批量同步已绑定充电桩的实时利用率到 scene_objects。
    作用：让左侧资产树和 3D 场景能拿到与右侧面板一致的实时数据。
    """
    objects = st.session_state.get('scene_objects', [])
    if not objects:
        return

    # 先收集「已绑定且无模拟数据」的充电桩，避免无绑定时无谓加载预测器(torch)
    targets = [
        obj for obj in objects
        if obj.get('type', '').startswith('charger')
        and obj.get('bind_station_id')
        and not obj.get('custom_props', {}).get('_mock_data')
    ]
    if not targets:
        return

    predictor = _get_predictor()
    if predictor is None:
        return  # 无 torch（云端默认）：跳过实时数据回写，界面显示模拟数据
    updated_count = 0

    for obj in targets:
        sid = obj.get('bind_station_id', '')
        try:
            data = predictor.get_realtime_util(sid)
            util = data.get('utilization', obj.get('utilization', 0.5))
            if abs(obj.get('utilization', -1) - util) > 0.001:
                obj['utilization'] = util
                updated_count += 1
        except Exception as e:
            print(f"⚠️ 同步站点 {sid} 失败: {e}")

    if updated_count > 0 and st.session_state.current_scene:
        st.session_state.current_scene.objects = objects
        print(f"✅ 已同步 {updated_count} 个充电桩的实时利用率")

# ==================== 主布局 ====================
def main():
    # 消费上一次 fragment 触发的全刷新原因（仅用于调试和日志）
    _dirty = consume_dirty()
    if _dirty:
        print(f"🔄 [全刷新] 原因: {_dirty} | 时间: {st.session_state.get('_dirty_time', '--')}")

    # 🔥 处理退出大屏请求（来自浮动退出按钮的 URL 参数）
    if st.query_params.get('exit_big') == '1':
        st.session_state.big_screen_mode = False
        _clear_transient_query_params()
        st.rerun()
        return

    # 2. 处理 URL 参数（风格、故事、选中、位置更新、删除、复制、新建空白）
    query_params = st.query_params

    # 处理新建空白场景时传递的新 ID
    if "selected" in query_params:
        selected_id = query_params["selected"]
        if selected_id:
            st.session_state.selected_object_id = selected_id
            # 🔥 关键：同步通知 selectbox 更新它的值
            st.session_state['object_selectbox'] = selected_id
            _clear_transient_query_params()
            st.rerun()
            return

    # ===== 处理图标栏切换面板 =====
    if "panel" in query_params:
        panel_from_url = query_params["panel"]
        # 🔥 P2 Step3/4：与导航项保持一致（template/asset_tree/story/saved/emission 均已并入）
        valid_panels = ['scene', 'component', 'style', 'data', 'settings']
        if panel_from_url in valid_panels:
            st.session_state.active_panel = panel_from_url
        _clear_transient_query_params()
        st.rerun()
        return

    # ===== 🔥 强制修复 current_scene 类型（字典 → SceneData 对象） =====
    if not hasattr(st.session_state.get('current_scene', None), 'scene_name'):
        mgr = st.session_state.scene_manager
        st.session_state.current_scene = mgr.create_from_template('charging_station', '我的充电站')
        st.session_state.scene_objects = st.session_state.current_scene.objects
        st.session_state.scene_id = None
        st.rerun()
        return

    # 处理风格切换
    if "style" in query_params:
        style_id = query_params["style"]
        if style_id:
            st.session_state.current_style = style_id
            _clear_transient_query_params()
            st.rerun()
            return

    # 处理故事切换
    if "story" in query_params:
        story_id = query_params["story"]
        if story_id:
            st.session_state.current_story = story_id
            _clear_transient_query_params()
            st.rerun()
            return

    # 处理选中
    if "selected" in query_params:
        selected_id = query_params["selected"]
        if selected_id:
            st.session_state.selected_object_id = selected_id
            _clear_transient_query_params()
            st.rerun()
            return

    # 处理位置更新
    if "update_pos" in query_params:
        obj_id = query_params["update_pos"]
        try:
            x = float(query_params.get("px", 0.0))
            y = float(query_params.get("py", 0.0))
            z = float(query_params.get("pz", 0.0))
            # 🔥 Y 轴地界限制
            if y < 0:
                y = 0.0
            rx = float(query_params.get("rx", 0.0))
            ry = float(query_params.get("ry", 0.0))
            rz = float(query_params.get("rz", 0.0))
            sx = float(query_params.get("sx", 1.0))
            sy = float(query_params.get("sy", 1.0))
            sz = float(query_params.get("sz", 1.0))
        except:
            x, y, z = 0.0, 0.0, 0.0
            rx, ry, rz = 0.0, 0.0, 0.0
            sx, sy, sz = 1.0, 1.0, 1.0

        for obj in st.session_state.scene_objects:
            if obj.get('id') == obj_id:
                obj['position'] = {"x": x, "y": y, "z": z}
                obj['rotation'] = {"x": rx, "y": ry, "z": rz}
                obj['scale'] = {"x": sx, "y": sy, "z": sz}
                # P3：不再写 _runtime_* 影子字段（该机制已移除）
                break
        if st.session_state.current_scene:
            st.session_state.current_scene.objects = st.session_state.scene_objects.copy()
        _clear_transient_query_params()
        st.rerun()
        return

    # ===== 处理删除 =====
    if "delete" in query_params:
        obj_id = query_params["delete"]
        if obj_id:
            _clear_transient_query_params()
            success = delete_object(obj_id)
            if not success:
                st.error("❌ 删除失败，请检查控制台错误")
            return

    # ===== 处理复制 =====
    if "copy" in query_params:
        obj_id = query_params["copy"]
        if obj_id:
            obj = get_object_by_id(obj_id)
            if obj:
                new_obj = obj.copy()
                new_obj['id'] = str(uuid.uuid4())
                new_obj['name'] = f"{obj.get('name', '')}_副本"
                new_obj['position'] = {
                    "x": obj.get('position', {}).get('x', 0) + 0.5,
                    "y": obj.get('position', {}).get('y', 0),
                    "z": obj.get('position', {}).get('z', 0) + 0.5
                }
                st.session_state.scene_objects.append(new_obj)
                if st.session_state.current_scene:
                    st.session_state.current_scene.objects = st.session_state.scene_objects
                st.session_state.selected_object_id = new_obj['id']
                if SUPABASE_AVAILABLE and 'scene_id' in st.session_state:
                    sync_scene_objects(st.session_state.scene_id, st.session_state.scene_objects)
                st.toast(f"✅ 已复制: {new_obj.get('name', '物体')}")
                _clear_transient_query_params()
                st.rerun()
            return

    # 启用数据库加载
    init_db_scene()

    # 🔥 新增：在渲染前同步实时利用率，让左侧资产树显示最新数据
    sync_bound_chargers_utilization()

    # 4. 布局（支持大屏模式）
    if st.session_state.get('big_screen_mode', False):
        # 🔥 大屏模式：全屏3D + 底部指标条 + 右上角浮动退出按钮
        # 用 HTML + JS 触发 URL 参数，避免 st.columns 占用文档流导致 3D 场景被下推
        if st.session_state.get('big_screen_mode', False):
            # CSS：隐藏顶部留白 + 把第一个按钮固定到右上角
            st.markdown("""
            <style>
                section.main .block-container {
                    padding-top: 0 !important;
                    padding-bottom: 0.5rem !important;
                    margin-top: 0 !important;
                }
                header[data-testid="stHeader"] {
                    height: 0 !important;
                    min-height: 0 !important;
                    background: transparent !important;
                }
                /* 把大屏模式下第一个 st.button 固定到右上角 */
                .big-screen-exit-marker ~ div[data-testid="stButton"] button,
                .big-screen-exit-marker + div button {
                    position: fixed !important;
                    top: 12px !important;
                    right: 20px !important;
                    z-index: 99999 !important;
                    background: rgba(26, 42, 68, 0.9) !important;
                    border: 1px solid #4a90d9 !important;
                    color: #88ccff !important;
                    border-radius: 20px !important;
                    padding: 6px 16px !important;
                    width: auto !important;
                    min-width: 100px !important;
                    height: auto !important;
                }
            </style>
            """, unsafe_allow_html=True)

            # 标记（触发上面的 CSS）
            st.markdown('<div class="big-screen-exit-marker"></div>', unsafe_allow_html=True)

            # 真正的 Streamlit 退出按钮（视觉上被 CSS 移到右上角）
            if st.button("✕ 退出大屏", key="exit_big_screen"):
                st.session_state.big_screen_mode = False
                st.rerun()

        html_content = generate_scene_html()
        # 🔥 从 850 降到 750，配合底部指标条
        # 🔥 修复：大屏模式此前在这里渲染了两遍（生成两遍 6000 行 HTML + 挂两个 iframe），
        #         现在只保留这一处
        st.iframe(html_content, height=750)
        render_bottom_bar_big()
    else:
        # 常规模式
        left_col, center_col, right_col = st.columns([2.5, 5.0, 2.7], gap="small")
        with left_col:
            render_left_panel()
        with center_col:
            html_content = generate_scene_html()
            st.iframe(html_content, height=800)
            render_bottom_bar()
        with right_col:
            render_right_panel()

    # 5. 实时数据流
    if 'scene_objects' in st.session_state:
        chargers = [obj for obj in st.session_state.scene_objects
                    if obj.get('type', '').startswith('charger') and obj.get('bind_station_id')]
        station_ids = [c['bind_station_id'] for c in chargers if c.get('bind_station_id')]
        # if station_ids:
        #     update_stream_data(station_ids)


if __name__ == "__main__":
    main()
