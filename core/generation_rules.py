"""
core/generation_rules.py

生成规则引擎 —— 根据几何类型自动映射到 3D 物体
"""
import uuid
from typing import List, Dict, Tuple


# ============================================================
# 默认规则表
# ============================================================
DEFAULT_RULES = {
    'point': {
        'target': 'charger_fast',
        'conditions': [
            {'field': 'type', 'op': '==', 'value': 'slow',  'target': 'charger_slow'},
            {'field': 'type', 'op': '==', 'value': 'super', 'target': 'charger_super'},
            {'field': 'type', 'op': '==', 'value': 'tree',  'target': 'tree'},
            {'field': 'type', 'op': '==', 'value': 'lamp',  'target': 'lamp'},
        ]
    },
    'line': {
        'target': 'road_straight',
        'conditions': [
            {'field': 'type', 'op': 'contains', 'value': 'curve', 'target': 'road_curve'},
        ]
    },
    'polygon': {
        'target': 'building',
        'conditions': [
            {'field': 'use', 'op': '==', 'value': 'commercial', 'target': 'building_tall'},
            {'field': 'use', 'op': '==', 'value': 'residential', 'target': 'building'},
        ]
    }
}


# ============================================================
# 规则匹配
# ============================================================
def match_target(feature, rules: Dict) -> str:
    """匹配规则，返回目标物体类型（支持多种运算符）"""
    rule = rules.get(feature.geom_type, {})
    if not rule:
        return None

    for cond in rule.get('conditions', []):
        field_val = feature.properties.get(cond['field'])
        if field_val is None:
            continue

        op = cond.get('op', '==')
        target_val = cond.get('value', '')

        try:
            if op == '==' and str(field_val) == str(target_val):
                return cond['target']
            elif op == '!=' and str(field_val) != str(target_val):
                return cond['target']
            elif op == 'contains' and str(target_val) in str(field_val):
                return cond['target']
            elif op == '>':
                if float(field_val) > float(target_val):
                    return cond['target']
            elif op == '<':
                if float(field_val) < float(target_val):
                    return cond['target']
            elif op == '>=':
                if float(field_val) >= float(target_val):
                    return cond['target']
            elif op == '<=':
                if float(field_val) <= float(target_val):
                    return cond['target']
        except (ValueError, TypeError):
            continue

    return rule.get('target')


# ============================================================
# 几何工具
# ============================================================
def _flatten(coords):
    """递归展平坐标"""
    if not coords:
        return []
    if isinstance(coords[0], (int, float)):
        return [coords]
    result = []
    for item in coords:
        result.extend(_flatten(item))
    return result


def calculate_centroid(coords, geom_type: str):
    """计算几何中心点"""
    if geom_type == 'point':
        return coords

    if geom_type == 'polygon':
        # 取外环
        ring = coords[0] if (coords and isinstance(coords[0][0], list)) else coords
        flat = _flatten(ring)
    else:
        flat = _flatten(coords)

    if not flat:
        return [0.0, 0.0]

    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


def calculate_polygon_size(coords):
    """计算多边形包围盒 (width, depth)，单位：经度/纬度差"""
    ring = coords[0] if (coords and isinstance(coords[0][0], list)) else coords
    flat = _flatten(ring)
    if not flat:
        return 1.0, 1.0
    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    return (max(xs) - min(xs)), (max(ys) - min(ys))


# ============================================================
# 核心：生成物体列表
# ============================================================
def generate_objects(features: List, rules: Dict) -> List[Dict]:
    """
    根据规则生成场景物体列表

    Args:
        features: GeoFeature 列表
        rules: 用户配置的规则表

    Returns:
        List[Dict]: 场景物体列表
    """
    if not features:
        return []

    # 计算所有要素的平均经纬度（用于归一化）
    centroids = []
    for f in features:
        c = calculate_centroid(f.coords, f.geom_type)
        centroids.append(c)

    if not centroids:
        return []

    avg_lon = sum(c[0] for c in centroids) / len(centroids)
    avg_lat = sum(c[1] for c in centroids) / len(centroids)

    # 经度缩放（适应纬度）
    import math
    cos_lat = math.cos(math.radians(avg_lat))

    objects = []
    for feat in features:
        target_type = match_target(feat, rules)
        if not target_type:
            continue

        centroid = calculate_centroid(feat.coords, feat.geom_type)
        lon, lat = centroid[0], centroid[1]

        # 经纬度 → 场景坐标
        scale = 30.0   # 缩放系数
        x = (lon - avg_lon) * 111 * cos_lat * scale
        z = (lat - avg_lat) * 111 * scale

        # 基础对象
        obj = {
            'id': str(uuid.uuid4()),
            'type': target_type,
            'name': str(feat.properties.get('name', f'{target_type}_{len(objects)}')),
            'position': {'x': round(x, 3), 'y': 0.0, 'z': round(z, 3)},
            'rotation': {'x': 0, 'y': 0, 'z': 0},
            'scale': {'x': 1, 'y': 1, 'z': 1},
            'bind_station_id': '',
            'custom_props': {},
            'utilization': float(feat.properties.get('utilization', 0.5) or 0.5)
        }

        # 面 → 建筑：根据多边形尺寸自动设置宽高
        if feat.geom_type == 'polygon' and target_type.startswith('building'):
            w, d = calculate_polygon_size(feat.coords)
            obj['scale'] = {
                'x': max(0.8, min(4.0, w * 111 * cos_lat * scale / 8)),
                'y': 1.0,
                'z': max(0.8, min(4.0, d * 111 * scale / 8))
            }
            h = feat.properties.get('height', 3.0)
            try:
                h = float(h)
            except (ValueError, TypeError):
                h = 3.0
            obj['custom_props']['height'] = max(1.5, min(15.0, h))

        objects.append(obj)

    return objects


# ============================================================
# 自动网格化（坐标过于集中时）
# ============================================================
def auto_layout_if_clustered(objects: List[Dict], threshold=2.0, spacing=3.0) -> Tuple[List[Dict], bool]:
    """如果物体过于集中，自动网格化排列"""
    if len(objects) < 2:
        return objects, False

    xs = [o['position']['x'] for o in objects]
    zs = [o['position']['z'] for o in objects]
    span_x = max(xs) - min(xs)
    span_z = max(zs) - min(zs)

    if span_x < threshold and span_z < threshold:
        cols = int(len(objects) ** 0.5) + 1
        for i, o in enumerate(objects):
            row = i // cols
            col = i % cols
            o['position']['x'] = round((col - cols / 2) * spacing, 3)
            o['position']['z'] = round((row - cols / 2) * spacing, 3)
        return objects, True

    return objects, False