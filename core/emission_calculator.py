"""
core/emission_calculator.py

碳排放核算模块
对标吉林省"十五五"规划：新型储能300万千瓦、充换电站配储、绿色能源产业高地

⚠️ 参数说明：
- 标"【权威】"的系数来自国家发改委/IPCC/GB国标，不可调整
- 标"【估算】"的参数为行业经验值，可在UI中调节
"""

# ==================== 【权威系数】不可调 ====================
CO2_PER_KWH_OIL = 0.6           # kg CO2 / 度电（替代燃油车）【权威：国家发改委】
CO2_PER_KWH_STORAGE = 0.15      # kg CO2 / 度电（储能额外减排）【权威：行业标准】
TREE_ABSORB_PER_YEAR = 21.77    # kg CO2 / 棵树·年【权威：联合国粮农组织】
COAL_PER_KWH = 0.0003           # 吨标煤 / 度电【权威：GB/T 2589】
OIL_PER_KG_CO2 = 1 / 2.3        # L汽油 / kg CO2（1L汽油≈2.3kg CO2）【权威：IPCC】
KM_PER_LITER = 12               # km / L汽油【权威：乘用车平均油耗】

# ==================== 【估算参数】默认值（保守） ====================
DEFAULT_FAST_HOURS = 2.0        # 快充日均有效时长（保守：2小时）
DEFAULT_SLOW_HOURS = 4.0        # 慢充日均有效时长（保守：4小时）
DEFAULT_STORAGE_RATIO = 0.2     # 储能配比（吉林省政策鼓励20%）
DEFAULT_STORAGE_CYCLES = 1.5    # 储能日均循环次数


def calculate_emission(scene_objects, days=365,
                       fast_hours=None, slow_hours=None):
    """
    计算场景碳减排量（替代燃油车部分）

    Args:
        scene_objects: 场景物体列表
        days: 计算周期
        fast_hours: 快充日均时长（None时用默认）
        slow_hours: 慢充日均时长（None时用默认）

    Returns:
        dict: 碳减排指标
    """
    fast_hours = fast_hours if fast_hours is not None else DEFAULT_FAST_HOURS
    slow_hours = slow_hours if slow_hours is not None else DEFAULT_SLOW_HOURS

    chargers = [o for o in scene_objects if o.get('type', '').startswith('charger')]

    total_chargers = len(chargers)
    bound_chargers = len([c for c in chargers if c.get('bind_station_id')])

    # 总充电量估算
    total_kwh = 0
    total_power = 0
    for c in chargers:
        util = c.get('utilization', 0.5)
        power = c.get('custom_props', {}).get('power')
        if not power:
            ctype = c.get('type', '')
            if ctype == 'charger_super':
                power = 180
            elif ctype == 'charger_fast':
                power = 120
            else:
                power = 7
        total_power += power
        # 快充用 fast_hours，慢充用 slow_hours
        daily_hours = slow_hours if power < 30 else fast_hours
        daily_kwh = power * util * daily_hours
        total_kwh += daily_kwh * days

    # 碳减排（替代燃油车）
    co2_reduction = total_kwh * CO2_PER_KWH_OIL / 1000  # 吨
    tree_equivalent = co2_reduction * 1000 / TREE_ABSORB_PER_YEAR
    coal_saved = total_kwh * COAL_PER_KWH
    oil_saved_liters = co2_reduction * 1000 * OIL_PER_KG_CO2
    car_km_reduction = oil_saved_liters * KM_PER_LITER
    avg_util = sum([c.get('utilization', 0.5) for c in chargers]) / max(len(chargers), 1)

    return {
        'total_kwh': round(total_kwh, 1),
        'co2_reduction_tons': round(co2_reduction, 1),
        'tree_equivalent': int(tree_equivalent),
        'coal_saved_tons': round(coal_saved, 1),
        'oil_saved_liters': round(oil_saved_liters, 1),
        'car_km_reduction': round(car_km_reduction / 10000, 1),
        'total_chargers': total_chargers,
        'bound_chargers': bound_chargers,
        'total_power': total_power,
        'avg_utilization': avg_util,
    }


def calculate_storage_emission(scene_objects, days=365,
                                storage_ratio=None, cycles=None):
    """
    估算配套储能带来的额外碳减排

    Args:
        scene_objects: 场景物体列表
        days: 计算周期
        storage_ratio: 储能配比（None时用默认）
        cycles: 日均循环次数（None时用默认）

    Returns:
        dict: 储能相关指标
    """
    storage_ratio = storage_ratio if storage_ratio is not None else DEFAULT_STORAGE_RATIO
    cycles = cycles if cycles is not None else DEFAULT_STORAGE_CYCLES

    chargers = [o for o in scene_objects if o.get('type', '').startswith('charger')]

    if not chargers:
        return {'storage_kwh': 0, 'storage_co2_tons': 0, 'storage_capacity_kw': 0}

    # 按站点聚合
    station_power = {}
    for c in chargers:
        sid = c.get('bind_station_id', '') or c.get('id', '')
        power = c.get('custom_props', {}).get('power')
        if not power:
            ctype = c.get('type', '')
            if ctype == 'charger_super':
                power = 180
            elif ctype == 'charger_fast':
                power = 120
            else:
                power = 7
        station_power[sid] = station_power.get(sid, 0) + power

    total_storage_capacity = 0
    total_throughput = 0

    for sid, power in station_power.items():
        storage_capacity = power * storage_ratio
        total_storage_capacity += storage_capacity
        daily_throughput = storage_capacity * cycles * 0.9  # 90%充放效率
        total_throughput += daily_throughput * days

    storage_co2 = total_throughput * CO2_PER_KWH_STORAGE / 1000

    return {
        'storage_kwh': round(total_throughput, 1),
        'storage_co2_tons': round(storage_co2, 1),
        'storage_capacity_kw': round(total_storage_capacity, 1),
    }


def get_carbon_rating(avg_utilization):
    """根据平均利用率返回绿色评级"""
    if avg_utilization > 0.7:
        return {'grade': 'A+', 'color': '#51cf66', 'label': '高效低碳'}
    elif avg_utilization > 0.5:
        return {'grade': 'A', 'color': '#88ccff', 'label': '低碳运行'}
    elif avg_utilization > 0.3:
        return {'grade': 'B', 'color': '#fcc419', 'label': '正常运行'}
    else:
        return {'grade': 'C', 'color': '#ff6b6b', 'label': '待优化'}


def get_current_params():
    """
    从 session_state 读取当前碳减排参数（供底部指标条使用）
    返回 dict
    """
    import streamlit as st
    return {
        'fast_hours': st.session_state.get('carbon_fast_hours', DEFAULT_FAST_HOURS),
        'slow_hours': st.session_state.get('carbon_slow_hours', DEFAULT_SLOW_HOURS),
        'storage_ratio': st.session_state.get('carbon_storage_ratio', DEFAULT_STORAGE_RATIO),
        'storage_cycles': st.session_state.get('carbon_storage_cycles', DEFAULT_STORAGE_CYCLES),
    }