"""
core/relation_builder.py

语义关系构建器 —— 城市规划视角
真正考虑：充电桩沿道路、服务建筑、道路连接拓扑
"""
import math
from typing import List, Dict, Tuple


# ============================================================
# 几何算法
# ============================================================

def _dist_point_to_point(a, b):
    ax, ay, az = a.get('x', 0), a.get('y', 0), a.get('z', 0)
    bx, by, bz = b.get('x', 0), b.get('y', 0), b.get('z', 0)
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def _dist_point_to_segment(p, a, b):
    """
    点到线段的距离（2D，XZ 平面）
    p: 点
    a, b: 线段两端点
    """
    px, pz = p.get('x', 0), p.get('z', 0)
    ax, az = a.get('x', 0), a.get('z', 0)
    bx, bz = b.get('x', 0), b.get('z', 0)

    dx, dz = bx - ax, bz - az
    seg_len_sq = dx * dx + dz * dz
    if seg_len_sq < 1e-6:
        return math.sqrt((px - ax) ** 2 + (pz - az) ** 2)

    t = ((px - ax) * dx + (pz - az) * dz) / seg_len_sq
    t = max(0.0, min(1.0, t))
    proj_x = ax + t * dx
    proj_z = az + t * dz
    return math.sqrt((px - proj_x) ** 2 + (pz - proj_z) ** 2)


def _get_building_footprint(obj):
    """
    获取建筑轮廓的近似四边形顶点
    利用 position + scale.x/y/z + rotation.y
    """
    pos = obj.get('position', {'x': 0, 'y': 0, 'z': 0})
    scale = obj.get('scale', {'x': 1, 'y': 1, 'z': 1})
    rot = obj.get('rotation', {'x': 0, 'y': 0, 'z': 0})

    # 建筑默认尺寸 2.0 x 1.5（在 createBuilding 里）
    w = 2.0 * scale.get('x', 1)
    d = 1.5 * scale.get('z', 1)

    angle = rot.get('y', 0)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    # 局部坐标下的四角
    corners_local = [
        (-w / 2, -d / 2), (w / 2, -d / 2),
        (w / 2, d / 2), (-w / 2, d / 2),
    ]
    corners_world = []
    for cx, cz in corners_local:
        # 旋转
        rx = cx * cos_a - cz * sin_a
        rz = cx * sin_a + cz * cos_a
        corners_world.append({
            'x': pos['x'] + rx,
            'z': pos['z'] + rz
        })
    return corners_world


def _get_road_segments(obj):
    """
    获取道路的中心线段端点
    简化：把道路当作一条线段，从起点到终点（沿长边）
    """
    pos = obj.get('position', {'x': 0, 'y': 0, 'z': 0})
    scale = obj.get('scale', {'x': 1, 'y': 1, 'z': 1})
    rot = obj.get('rotation', {'x': 0, 'y': 0, 'z': 0})

    # 道路默认尺寸 1.0 x 0.3（在 createRoadStraight 里）
    length = 1.0 * scale.get('x', 1)
    angle = rot.get('y', 0)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    half = length / 2
    # 沿局部 X 轴方向
    start = {
        'x': pos['x'] - half * cos_a,
        'z': pos['z'] - half * sin_a
    }
    end = {
        'x': pos['x'] + half * cos_a,
        'z': pos['z'] + half * sin_a
    }
    return [start, end]


# ============================================================
# 分类
# ============================================================
def _get_category(obj_type: str) -> str:
    if obj_type.startswith('charger'): return 'charger'
    if obj_type.startswith('building'): return 'building'
    if obj_type.startswith('tree'): return 'tree'
    if obj_type.startswith('road'): return 'road'
    if obj_type == 'lamp': return 'lamp'
    if obj_type in ('car', 'truck'): return 'vehicle'
    return 'other'


def _cat_zh(cat):
    return {
        'charger': '充电桩', 'building': '建筑', 'tree': '树木',
        'road': '道路', 'lamp': '路灯', 'vehicle': '车辆', 'other': '物体'
    }.get(cat, '物体')


# ============================================================
# 主函数
# ============================================================
def build_relations(objects: List[Dict],
                    street_threshold: float = 12.0,    # 沿街判定
                    serve_threshold: float = 25.0,      # 服务范围
                    adjacent_threshold: float = 10.0):  # 相邻判定
    """
    城市规划视角的语义关系构建

    关系类型：
    - along_street    充电桩 → 道路（临街，充电桩沿此路摆放）
    - serves          充电桩 → 建筑（服务此建筑）
    - connects        道路 → 建筑（道路连接此建筑）
    - adjacent        充电桩 → 充电桩（集群）
    """
    if not objects:
        return objects

    # 🔥 清空所有物体的旧关系
    for obj in objects:
        if 'custom_props' not in obj:
            obj['custom_props'] = {}
        obj['custom_props']['relations'] = []


    # 先分类
    chargers = [o for o in objects if _get_category(o.get('type', '')) == 'charger']
    buildings = [o for o in objects if _get_category(o.get('type', '')) == 'building']
    roads = [o for o in objects if _get_category(o.get('type', '')) == 'road']

    # 预处理：道路的线段
    road_segments = {}
    for r in roads:
        road_segments[r['id']] = _get_road_segments(r)

    # 预处理：建筑轮廓
    building_footprints = {}
    for b in buildings:
        building_footprints[b['id']] = _get_building_footprint(b)

    # ---------- 2.1 充电桩 → 沿街（最近道路）----------
    for charger in chargers:
        pos = charger['position']
        best_road = None
        best_dist = float('inf')
        best_projection = None

        for road in roads:
            seg = road_segments[road['id']]
            d = _dist_point_to_segment(pos, seg[0], seg[1])
            if d < best_dist:
                best_dist = d
                best_road = road
                # 计算投影点（最近的线段上点）
                # 简化：用中点近似
                best_projection = {
                    'x': (seg[0]['x'] + seg[1]['x']) / 2,
                    'z': (seg[0]['z'] + seg[1]['z']) / 2
                }

        rels = charger.setdefault('custom_props', {}).setdefault('relations', [])

        if best_road and best_dist < street_threshold:
            rels.append({
                'type': 'along_street',
                'target': best_road['id'],
                'target_name': best_road.get('name', '道路'),
                'distance': round(best_dist, 2),
                'label': '临街',
                'is_on_street': True,
                'projection': best_projection  # 用于前端画"到道路"的投影线
            })

    # ---------- 2.2 充电桩 → 服务建筑 ----------
    for charger in chargers:
        pos = charger['position']
        candidates = []
        for b in buildings:
            d = _dist_point_to_point(pos, b['position'])
            if d < serve_threshold:
                candidates.append((d, b))

        candidates.sort(key=lambda x: x[0])
        rels = charger.setdefault('custom_props', {}).setdefault('relations', [])

        for d, b in candidates[:2]:   # 最多服务2栋建筑
            rels.append({
                'type': 'serves',
                'target': b['id'],
                'target_name': b.get('name', '建筑'),
                'distance': round(d, 2),
                'label': '服务建筑'
            })

    # ---------- 2.3 道路 → 连接的建筑 ----------
    for road in roads:
        seg = road_segments[road['id']]
        # 道路中点
        mid = {
            'x': (seg[0]['x'] + seg[1]['x']) / 2,
            'z': (seg[0]['z'] + seg[1]['z']) / 2
        }
        candidates = []
        for b in buildings:
            # 建筑中心到道路线段距离
            d = _dist_point_to_segment(b['position'], seg[0], seg[1])
            if d < street_threshold * 1.5:
                candidates.append((d, b))

        candidates.sort(key=lambda x: x[0])
        rels = road.setdefault('custom_props', {}).setdefault('relations', [])

        for d, b in candidates[:3]:
            rels.append({
                'type': 'connects',
                'target': b['id'],
                'target_name': b.get('name', '建筑'),
                'distance': round(d, 2),
                'label': '连接建筑'
            })

    # ---------- 2.4 充电桩集群（相邻）----------
    for i, c1 in enumerate(chargers):
        rels1 = c1.setdefault('custom_props', {}).setdefault('relations', [])
        nearby_chargers = []
        for j, c2 in enumerate(chargers):
            if i >= j: continue
            d = _dist_point_to_point(c1['position'], c2['position'])
            if d < adjacent_threshold:
                nearby_chargers.append((d, c2))

        nearby_chargers.sort(key=lambda x: x[0])
        for d, c2 in nearby_chargers[:2]:
            rels1.append({
                'type': 'adjacent',
                'target': c2['id'],
                'target_name': c2.get('name', '充电桩'),
                'distance': round(d, 2),
                'label': '同组充电桩'
            })

    # ---------- 2.5 建筑间关系 ----------
    for i, b1 in enumerate(buildings):
        rels1 = b1.setdefault('custom_props', {}).setdefault('relations', [])

        # 2.5.1 相邻建筑
        neighbors = []
        for j, b2 in enumerate(buildings):
            if i >= j:
                continue
            d = _dist_point_to_point(b1['position'], b2['position'])
            if d < 25.0:  # 25m 内视为相邻
                neighbors.append((d, b2))

        neighbors.sort(key=lambda x: x[0])
        for d, b2 in neighbors[:3]:
            rels1.append({
                'type': 'neighbors',
                'target': b2['id'],
                'target_name': b2.get('name', '建筑'),
                'distance': round(d, 2),
                'label': '相邻建筑'
            })

        # 2.5.2 同街建筑（两栋建筑都临同一条路）
        for j, b2 in enumerate(buildings):
            if i >= j:
                continue

            # 找 b1 和 b2 各自最近的道路
            def _nearest_road(b):
                best_id, best_d = None, float('inf')
                for road in roads:
                    seg = road_segments[road['id']]
                    dd = _dist_point_to_segment(b['position'], seg[0], seg[1])
                    if dd < best_d:
                        best_d = dd
                        best_id = road['id']
                return best_id, best_d

            r1_id, r1_d = _nearest_road(b1)
            r2_id, r2_d = _nearest_road(b2)

            # 都临街 且 是同一条路 → 标记为同街
            if (r1_id and r2_id and r1_id == r2_id
                    and r1_d < street_threshold * 1.5
                    and r2_d < street_threshold * 1.5):
                rels1.append({
                    'type': 'same_street',
                    'target': b2['id'],
                    'target_name': b2.get('name', '建筑'),
                    'distance': round(_dist_point_to_point(b1['position'], b2['position']), 2),
                    'label': '同街建筑'
                })

    total = sum(len(o.get('custom_props', {}).get('relations', [])) for o in objects)
    print(f"✅ 关系构建完成: {len(objects)} 个物体, {total} 条关系")
    print(f"   - 充电桩 {len(chargers)} · 建筑 {len(buildings)} · 道路 {len(roads)}")
    return objects