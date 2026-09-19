"""
core/planned_layout.py

规划化摆放 —— 让生成阶段就符合城市规划逻辑
"""
import math
import hashlib
from typing import List, Dict


# ============================================================
# 几何工具
# ============================================================
def _dist_2d(a, b):
    return math.sqrt(
        (a.get('x', 0) - b.get('x', 0)) ** 2 +
        (a.get('z', 0) - b.get('z', 0)) ** 2
    )


def _road_segment(road):
    """获取道路线段端点（沿长边）"""
    pos = road.get('position', {'x': 0, 'y': 0, 'z': 0})
    scale = road.get('scale', {'x': 1, 'y': 1, 'z': 1})
    rot = road.get('rotation', {'x': 0, 'y': 0, 'z': 0})

    length = 1.0 * scale.get('x', 1)
    angle = rot.get('y', 0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    half = length / 2

    return [
        {'x': pos['x'] - half * cos_a, 'z': pos['z'] - half * sin_a},
        {'x': pos['x'] + half * cos_a, 'z': pos['z'] + half * sin_a},
    ]


def _nearest_road_info(obj, roads):
    """找最近道路，返回 (road, distance, projection_point)"""
    pos = obj['position']
    best_road, best_dist, best_proj = None, float('inf'), None

    for road in roads:
        p1, p2 = _road_segment(road)
        dx, dz = p2['x'] - p1['x'], p2['z'] - p1['z']
        seg_len_sq = dx * dx + dz * dz
        if seg_len_sq < 1e-6:
            continue

        t = ((pos['x'] - p1['x']) * dx + (pos['z'] - p1['z']) * dz) / seg_len_sq
        t = max(0.0, min(1.0, t))
        proj_x = p1['x'] + t * dx
        proj_z = p1['z'] + t * dz
        d = math.sqrt((pos['x'] - proj_x) ** 2 + (pos['z'] - proj_z) ** 2)

        if d < best_dist:
            best_dist = d
            best_road = road
            best_proj = {'x': proj_x, 'z': proj_z}

    return best_road, best_dist, best_proj


def _cat(obj_type):
    if obj_type.startswith('charger'): return 'charger'
    if obj_type.startswith('building'): return 'building'
    if obj_type.startswith('road'): return 'road'
    return 'other'


# ============================================================
# 主函数
# ============================================================
def apply_planned_layout(objects: List[Dict],
                         retreat_distance: float = 3.0,
                         snap_distance: float = 15.0,
                         min_building_gap: float = 3.0) -> List[Dict]:
    """
    规划化摆放：

    1. 建筑正立面朝街 + 保持退界
    2. 建筑之间不重叠
    3. 充电桩吸附到路边（沿街摆放）
    """
    if not objects:
        return objects

    chargers = [o for o in objects if _cat(o.get('type', '')) == 'charger']
    buildings = [o for o in objects if _cat(o.get('type', '')) == 'building']
    roads = [o for o in objects if _cat(o.get('type', '')) == 'road']

    log = []

    # ----------------------------------------------------------
    # 1. 建筑：朝向最近道路 + 退界
    # ----------------------------------------------------------
    for b in buildings:
        nearest_road, dist, proj = _nearest_road_info(b, roads)
        if not nearest_road or dist > 60:
            continue

        # 建筑到道路的方向
        dx = b['position']['x'] - proj['x']
        dz = b['position']['z'] - proj['z']

        # 建筑朝街：正立面（默认 -Z 方向）朝向道路
        target_angle = math.atan2(-dx, -dz)
        b.setdefault('rotation', {})['y'] = round(target_angle, 4)

        # 退界：如果太近，推远
        if dist < retreat_distance:
            d = max(dist, 0.1)
            scale = retreat_distance / d
            b['position']['x'] = round(proj['x'] + dx * scale, 3)
            b['position']['z'] = round(proj['z'] + dz * scale, 3)
            log.append(f"🏢 {b.get('name', '建筑')} 退界至 {retreat_distance}m")

    # ----------------------------------------------------------
    # 2. 建筑重叠解决（迭代推开）
    # ----------------------------------------------------------
    for _ in range(15):
        moved = False
        for i in range(len(buildings)):
            for j in range(i + 1, len(buildings)):
                b1, b2 = buildings[i], buildings[j]
                d = _dist_2d(b1['position'], b2['position'])
                if d >= min_building_gap:
                    continue

                dx = b1['position']['x'] - b2['position']['x']
                dz = b1['position']['z'] - b2['position']['z']
                if d < 0.1:
                    dx, dz, d = 1.0, 0.0, 1.0
                nx, nz = dx / d, dz / d
                push = (min_building_gap - d) / 2

                b1['position']['x'] = round(b1['position']['x'] + nx * push, 3)
                b1['position']['z'] = round(b1['position']['z'] + nz * push, 3)
                b2['position']['x'] = round(b2['position']['x'] - nx * push, 3)
                b2['position']['z'] = round(b2['position']['z'] - nz * push, 3)
                moved = True
                log.append(f"📐 {b1.get('name', '')} ↔ {b2.get('name', '')} 间距调整")

        if not moved:
            break

    # ----------------------------------------------------------
    # 3. 充电桩沿街吸附
    # ----------------------------------------------------------
    for c in chargers:
        nearest_road, dist, proj = _nearest_road_info(c, roads)
        if not nearest_road or dist > snap_distance:
            continue

        # 沿道路方向（用对象ID哈希保证位置稳定）
        p1, p2 = _road_segment(nearest_road)
        dx, dz = p2['x'] - p1['x'], p2['z'] - p1['z']
        seg_len = math.sqrt(dx * dx + dz * dz)
        if seg_len < 0.1:
            continue

        nx, nz = dx / seg_len, dz / seg_len
        perp_x, perp_z = -nz, nx

        h = int(hashlib.md5(c['id'].encode()).hexdigest()[:8], 16)
        offset_along = ((h % 1000) / 1000 - 0.5) * seg_len * 0.8
        side = 1 if (h % 2 == 0) else -1

        new_x = proj['x'] + nx * offset_along + perp_x * 1.5 * side
        new_z = proj['z'] + nz * offset_along + perp_z * 1.5 * side

        old_x, old_z = c['position']['x'], c['position']['z']
        c['position']['x'] = round(new_x, 3)
        c['position']['z'] = round(new_z, 3)

        # 充电桩朝向路边
        c.setdefault('rotation', {})['y'] = round(math.atan2(-perp_x, -perp_z), 4)

        move_dist = math.sqrt((new_x - old_x) ** 2 + (new_z - old_z) ** 2)
        if move_dist > 0.5:
            log.append(f"⚡ {c.get('name', '充电桩')} 沿街吸附")

    if log:
        print(f"📐 规划化摆放完成 ({len(log)} 次调整):")
        for line in log[:8]:
            print(f"   {line}")

    return objects