"""
core/asset_library.py

自定义资产库 —— 用户上传自定义 .glb 模型并跨场景复用
存储：Supabase Storage（models 桶）+ Supabase Table（custom_assets）
"""

import os
import uuid
import streamlit as st
from datetime import datetime
from typing import Optional, Dict, List


BUCKET_NAME = "custom_models"
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
ALLOWED_EXT = ['.glb', '.gltf']
ALLOWED_CATEGORIES = ['充电设备', '建筑', '景观', '道路', '车辆', '其他']


def _get_client():
    """获取 Supabase 客户端"""
    try:
        from core.supabase_client import get_supabase_client
        return get_supabase_client()
    except Exception as e:
        print(f"⚠️ Supabase 客户端获取失败: {e}")
        return None


def get_session_user_id() -> str:
    """
    获取当前用户的稳定标识符
    - 优先使用已登录用户 ID
    - 否则使用 session_state 中持久化的 UUID
    """
    if 'session_uid' not in st.session_state:
        st.session_state.session_uid = f"anon_{str(uuid.uuid4())[:12]}"
    return st.session_state.session_uid


def _sanitize_filename(name: str) -> str:
    """清洗文件名，保留中英文、数字、下划线"""
    import re
    return re.sub(r'[^\w\u4e00-\u9fa5\-]', '_', name)[:60]


def _ensure_bucket_exists(client) -> bool:
    """确保 models 桶存在且公开可读"""
    try:
        client.storage.get_bucket(BUCKET_NAME)
        return True
    except Exception:
        try:
            client.storage.create_bucket(
                BUCKET_NAME,
                options={"public": True, "file_size_limit": MAX_FILE_SIZE}
            )
            print(f"✅ 已创建 Storage 桶: {BUCKET_NAME}")
            return True
        except Exception as e:
            print(f"❌ 创建 Storage 桶失败: {e}")
            return False


def measure_glb_size(file_bytes: bytes):
    """读 GLB 的包围盒，返回 (size_x, size_y, size_z)；失败返回 None。

    🔥 为什么必须量这个：
        GLB 里没有"这是车/是楼"的语义，只有一个包围盒。不量尺寸就不知道该把模型
        缩放多少倍——前端旧逻辑只在 maxDim>20 或 <0.5 时才兜底，落在中间的模型
        就是原始尺寸直接进场景（比房子还大，或小到看不见），这就是
        "导入了但没真正用上"的根本原因。

    纯标准库实现（GLB = 12 字节头 + JSON chunk + BIN chunk），不引第三方依赖。
    会把节点上的 TRS 变换也算进去，避免像 road_straight.glb 那样
    "顶点尺寸"与"世界尺寸"不一致导致判断错长度方向。
    """
    import json
    import struct

    try:
        data = file_bytes
        magic, _ver, _length = struct.unpack('<III', data[:12])
        if magic != 0x46546C67:      # 'glTF'
            return None
        off = 12
        js = None
        while off + 8 <= len(data):
            clen, ctype = struct.unpack('<II', data[off:off + 8])
            off += 8
            if ctype == 0x4E4F534A:  # 'JSON'
                js = json.loads(data[off:off + clen].decode('utf-8'))
            off += clen
        if not js:
            return None

        accs = js.get('accessors', [])

        def _quat_matrix(q):
            x, y, z, w = q
            return [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ]

        mn = [float('inf')] * 3
        mx = [float('-inf')] * 3
        found = False
        for node in js.get('nodes', []):
            if 'mesh' not in node:
                continue
            mesh = js['meshes'][node['mesh']]
            s = node.get('scale', [1, 1, 1])
            r = node.get('rotation', [0, 0, 0, 1])
            t = node.get('translation', [0, 0, 0])
            R = _quat_matrix(r)
            for prim in mesh.get('primitives', []):
                ai = (prim.get('attributes') or {}).get('POSITION')
                if ai is None or ai >= len(accs):
                    continue
                acc = accs[ai]
                a_min, a_max = acc.get('min'), acc.get('max')
                if not a_min or not a_max:
                    continue
                found = True
                for cx in (a_min[0], a_max[0]):
                    for cy in (a_min[1], a_max[1]):
                        for cz in (a_min[2], a_max[2]):
                            p = [cx * s[0], cy * s[1], cz * s[2]]
                            p = [sum(R[i][j] * p[j] for j in range(3)) for i in range(3)]
                            p = [p[i] + t[i] for i in range(3)]
                            for i in range(3):
                                mn[i] = min(mn[i], p[i])
                                mx[i] = max(mx[i], p[i])
        if not found:
            return None
        return tuple(round(mx[i] - mn[i], 6) for i in range(3))
    except Exception as e:
        print(f"⚠️ GLB 尺寸解析失败（不影响上传）: {e}")
        return None


# 分类 → 目标尺寸（米）。与前端 app.py 里内置类型的归一化目标保持一致。
#   uniform=True  : 等比缩放（有机形状：车、树、建筑、设备…必须等比，否则会变形）
#   uniform=False : 三轴独立拉伸（只适合"本来就该被拉"的道路）
#   ⚠️ 车辆曾经用过三轴拉伸，结果 Y 被拉 1.36 倍、车被拉成又高又长（用户实测反馈）。
CATEGORY_TARGET_SIZE = {
    '充电设备': {'height': 2.0, 'uniform': True},
    '建筑':     {'height': 3.5, 'uniform': True},
    '景观':     {'height': 2.5, 'uniform': True},
    '道路':     {'height': 0.6, 'width': 5.4, 'length': 12.0, 'uniform': False},
    '车辆':     {'height': 1.6, 'width': 1.8, 'length': 4.5, 'uniform': True},
    '其他':     {'height': 2.0, 'uniform': True},
}


def compute_scale_for_target(size, category='其他'):
    """把 GLB 原始尺寸换算成 scale 三元组 + 目标尺寸说明

    返回 (scale_x, scale_y, scale_z, target_info)。

    等比口径（uniform=True）：按**最长轴对齐目标最长轴**定一个系数，三轴共用。
      为什么不用"高度对齐"：车这类模型最长轴是车长，用车长对齐才符合直觉，
      用高度对齐会得到一个整体偏大的车。
    拉伸口径（uniform=False）：仅道路使用，三轴分别算，路才铺得出来。
    """
    if not size:
        return 1.0, 1.0, 1.0, {}
    sx, sy, sz = [max(v, 0.001) for v in size]
    tgt = CATEGORY_TARGET_SIZE.get(category) or CATEGORY_TARGET_SIZE['其他']

    if tgt.get('uniform', True):
        # 等比：最长轴对齐
        src_max = max(sx, sy, sz)
        tgt_max = max(float(tgt.get('height', 2.0)),
                      float(tgt.get('width', 0) or 0),
                      float(tgt.get('length', 0) or 0))
        k = tgt_max / src_max
        info = {'target_height': float(tgt.get('height', 2.0))}
        if tgt.get('length'):
            info['target_length'] = float(tgt['length'])
        if tgt.get('width'):
            info['target_width'] = float(tgt['width'])
        return (round(k, 6), round(k, 6), round(k, 6), info)

    # 三轴独立拉伸（道路）
    scale = (float(tgt.get('width', sx)) / sx,
             float(tgt.get('height', 0.6)) / sy,
             float(tgt.get('length', sz)) / sz)
    info = {'target_height': float(tgt.get('height', 0.6)),
            'target_width': float(tgt.get('width', sx)),
            'target_length': float(tgt.get('length', sz))}
    return (round(scale[0], 6), round(scale[1], 6), round(scale[2], 6), info)


def upload_asset(
    file_bytes: bytes,
    original_filename: str,
    display_name: str,
    category: str = '其他',
    is_public: bool = False,
) -> Optional[Dict]:
    """
    上传自定义模型

    Returns:
        Dict: 成功时返回资产记录，失败返回 None
    """
    client = _get_client()
    if not client:
        return None

    # 1. 文件校验
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_EXT:
        print(f"❌ 不支持的文件格式: {ext}")
        return None

    if len(file_bytes) > MAX_FILE_SIZE:
        print(f"❌ 文件过大: {len(file_bytes) / 1024 / 1024:.1f} MB")
        return None

    if not _ensure_bucket_exists(client):
        return None

    # 2. 生成存储路径
    user_id = get_session_user_id()
    asset_uuid = str(uuid.uuid4())
    safe_name = _sanitize_filename(display_name or os.path.splitext(original_filename)[0])
    storage_path = f"user_upload/{user_id}/{asset_uuid}_{safe_name}{ext}"

    # 3. 上传到 Storage
    content_type = 'model/gltf-binary' if ext == '.glb' else 'model/gltf+json'
    try:
        client.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"}
        )
        print(f"✅ 已上传到 Storage: {storage_path}")
    except Exception as e:
        print(f"❌ Storage 上传失败: {e}")
        return None

    # 4. 获取公开 URL
    try:
        url_resp = client.storage.from_(BUCKET_NAME).get_public_url(storage_path)
        public_url = url_resp if isinstance(url_resp, str) else url_resp.get('publicURL', '')
    except Exception as e:
        print(f"⚠️ 获取公开URL失败: {e}")
        public_url = ""

    # 5. 写入数据库
    # 🔥 上传时就量出模型尺寸并算好缩放系数：
    #    1) 用户能在资产卡片上看到"原始尺寸"，终于知道该调多少倍；
    #    2) 添加进场景时直接带上 scale，导进去就是合适的体量，而不是原始尺寸。
    raw_size = measure_glb_size(file_bytes)
    sc_x, sc_y, sc_z, tgt_info = compute_scale_for_target(raw_size, category)
    if raw_size:
        print(f"📏 资产尺寸: {raw_size[0]:.3f} × {raw_size[1]:.3f} × {raw_size[2]:.3f} "
              f"→ scale=({sc_x:.4f}, {sc_y:.4f}, {sc_z:.4f})")
    else:
        print("⚠️ 未能解析模型尺寸（可能是 .gltf 外部引用或加密文件），scale 用 1.0")

    record = {
        "id": asset_uuid,
        "user_id": user_id,
        "name": display_name or safe_name,
        "category": category if category in ALLOWED_CATEGORIES else '其他',
        "file_path": storage_path,
        "file_url": public_url,
        "file_size": len(file_bytes),
        "scale_x": sc_x,
        "scale_y": sc_y,
        "scale_z": sc_z,
        "is_public": bool(is_public),
    }
    # 尺寸与目标尺寸是后加的列。若目标库还没建这些列，写入会被拒——
    # 这时退回到"只写原有列"，不让整个上传失败。
    extra = {}
    if raw_size:
        extra.update({
            'raw_size_x': raw_size[0], 'raw_size_y': raw_size[1], 'raw_size_z': raw_size[2],
        })
    extra.update(tgt_info)
    record_with_extra = dict(record)
    record_with_extra.update(extra)

    for attempt in (record_with_extra, record):
        try:
            resp = client.table("custom_assets").insert(attempt).execute()
            if resp.data:
                print(f"✅ 资产已入库: {asset_uuid}"
                      + ("" if attempt is record_with_extra else "（旧表结构，尺寸列未写入）"))
                return resp.data[0]
        except Exception as e:
            if attempt is record_with_extra and extra:
                print(f"ℹ️ 尺寸列写入失败（可能表里还没这几列），改用原有列重试: {e}")
                continue
            print(f"❌ 数据库写入失败: {e}")
            break

    # 回滚：删除已上传的文件
    try:
        client.storage.from_(BUCKET_NAME).remove([storage_path])
    except Exception:
        pass
    return None


def get_my_assets(include_public: bool = True) -> List[Dict]:
    """
    获取当前用户可用的资产列表

    Args:
        include_public: 是否包含他人分享的公开资产
    """
    client = _get_client()
    if not client:
        return []

    user_id = get_session_user_id()

    try:
        # 先查自己的
        resp = client.table("custom_assets")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
        my_assets = resp.data or []

        if include_public:
            # 再查公开的（排除自己的）
            pub_resp = client.table("custom_assets")\
                .select("*")\
                .eq("is_public", True)\
                .neq("user_id", user_id)\
                .order("created_at", desc=True)\
                .limit(50)\
                .execute()
            public_assets = pub_resp.data or []
            return my_assets + public_assets

        return my_assets

    except Exception as e:
        print(f"⚠️ 获取资产列表失败: {e}")
        return []


def delete_asset(asset_id: str) -> bool:
    """删除资产（同时清理 Storage 和数据库）"""
    client = _get_client()
    if not client:
        return False

    user_id = get_session_user_id()

    try:
        # 1. 权限校验：只能删自己的
        resp = client.table("custom_assets")\
            .select("user_id, file_path")\
            .eq("id", asset_id)\
            .execute()

        if not resp.data:
            print(f"⚠️ 资产不存在: {asset_id}")
            return False

        asset = resp.data[0]
        if asset['user_id'] != user_id:
            print(f"⚠️ 无权删除他人资产")
            return False

        # 2. 删除 Storage 文件
        try:
            client.storage.from_(BUCKET_NAME).remove([asset['file_path']])
            print(f"🗑️ 已删除文件: {asset['file_path']}")
        except Exception as e:
            print(f"⚠️ Storage 删除失败（继续）: {e}")

        # 3. 删除数据库记录
        client.table("custom_assets").delete().eq("id", asset_id).execute()
        print(f"🗑️ 已删除资产记录: {asset_id}")
        return True

    except Exception as e:
        print(f"❌ 删除失败: {e}")
        return False


def get_asset_by_id(asset_id: str) -> Optional[Dict]:
    """按 ID 获取资产详情"""
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.table("custom_assets").select("*").eq("id", asset_id).execute()
        return resp.data[0] if resp.data else None
    except Exception as e:
        print(f"⚠️ 查询资产失败: {e}")
        return None


def update_asset_scale(asset_id: str, scale_x: float, scale_y: float, scale_z: float) -> bool:
    """更新资产默认缩放"""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("custom_assets")\
            .update({"scale_x": scale_x, "scale_y": scale_y, "scale_z": scale_z})\
            .eq("id", asset_id)\
            .execute()
        return True
    except Exception as e:
        print(f"⚠️ 更新缩放失败: {e}")
        return False


def get_asset_count() -> int:
    """获取当前用户资产总数"""
    return len(get_my_assets(include_public=False))