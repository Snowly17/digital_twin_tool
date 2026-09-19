"""
core/path_manager.py
统一管理项目中的所有路径，避免硬编码
"""

import os

# 尽早放宽控制台编码错误策略：core.* / common.* 里大量 emoji print 在
# GBK 控制台上会抛 UnicodeEncodeError，详见 core/console.py 的说明。
try:
    from core.console import enable_safe_console
except ModuleNotFoundError:  # 以脚本方式直接运行本文件时
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.console import enable_safe_console

enable_safe_console()

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据目录
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

# 站级数据
STATION_LEVEL_DIR = os.path.join(DATA_DIR, 'station-level')
STATION_OCC_PATH = os.path.join(STATION_LEVEL_DIR, 'station_occupancy_1h.csv')
STATION_INF_PATH = os.path.join(STATION_LEVEL_DIR, 'features', 'station_inf.csv')
STATION_EPRICE_PATH = os.path.join(STATION_LEVEL_DIR, 'features', 'e_price.csv')

# 区域级数据
ZONE_LEVEL_DIR = os.path.join(DATA_DIR, 'zone-level')
ZONE_OCC_PATH = os.path.join(ZONE_LEVEL_DIR, 'occupancy.csv')
ZONE_INF_PATH = os.path.join(ZONE_LEVEL_DIR, 'inf.csv')
ZONE_EPRICE_PATH = os.path.join(ZONE_LEVEL_DIR, 'e_price.csv')
ZONE_WEATHER_PATH = os.path.join(ZONE_LEVEL_DIR, 'weather_airport.csv')

# 模型权重
CHECKPOINTS_DIR = os.path.join(PROJECT_ROOT, 'checkpoints')
STATION_LSTM_PATH = os.path.join(CHECKPOINTS_DIR, 'station_lstm.pth')
REGION_LSTM_PATH = os.path.join(CHECKPOINTS_DIR, 'region_lstm.pth')

# 模板目录
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, 'templates')

# 导出目录
EXPORTS_DIR = os.path.join(PROJECT_ROOT, 'exports')

# 确保目录存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

# 便捷函数
def get_path(filename: str, subdir: str = '') -> str:
    """根据文件名和子目录获取完整路径"""
    if subdir:
        return os.path.join(PROJECT_ROOT, subdir, filename)
    return os.path.join(PROJECT_ROOT, filename)