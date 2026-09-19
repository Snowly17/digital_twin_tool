"""
core/supabase_client.py

Supabase 数据库客户端封装
提供场景、物体、实时数据的CRUD操作
"""

import streamlit as st
from supabase import create_client, Client
from typing import Optional, Dict, Any, List
import json
import uuid

# 单例模式
_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """获取 Supabase 客户端单例"""
    global _supabase_client
    if _supabase_client is None:
        try:
            url = st.secrets["SUPABASE_URL"]
            key = st.secrets["SUPABASE_ANON_KEY"]
            _supabase_client = create_client(url, key)
        except Exception as e:
            st.error(f"❌ Supabase 连接失败: {e}")
            raise
    return _supabase_client


# ==================== 场景操作 ====================

def get_or_create_scene(scene_id: str = None) -> Optional[Dict]:
    """
    获取或创建场景
    如果 scene_id 存在则返回该场景，否则返回第一个场景或创建新场景
    """
    try:
        supabase = get_supabase_client()

        if scene_id:
            resp = supabase.table("scenes").select("*").eq("id", scene_id).execute()
            if resp.data:
                return resp.data[0]

        # 🔥 关键修复：取「最新创建」的场景，而不是无限定的 limit(1)
        # 原写法没有 ORDER BY，返回的是物理第一行（SQL 语义上是未定义的），
        # 默认打开哪个场景完全取决于数据库内部行序 —— 这正是"每次打开都进
        # 那个 1000 物体的旧场景"的根因，而且行一旦 UPDATE 发生位移，默认场景
        # 还会在你毫无改动的情况下漂移。
        # 注意：这里**不能**按 updated_at 排序 —— 业务代码从不维护
        # scenes.updated_at（实测 35 行里 32 行 created_at == updated_at），
        # 按它排序等于按创建时间排，但语义是假的。
        resp = (
            supabase.table("scenes")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]

        # 没有场景则创建默认场景
        resp = supabase.table("scenes").insert({
            "name": "我的充电站"
        }).execute()
        if resp.data:
            return resp.data[0]
        return None
    except Exception as e:
        print(f"⚠️ get_or_create_scene 失败: {e}")
        return None


def get_scene_objects(scene_id: str) -> List[Dict]:
    """
    获取场景中的所有物体。

    🔥 关键修复：必须分页。PostgREST 单次响应默认最多 1000 行，原实现只发一次
    select，于是 >1000 个物体的场景会被**静默截断**；而 sync_scene_objects 是
    「先全删再重新插入」，截断后再保存就会把多出来的物体永久删掉。
    （实测库里已有一个 1323 物体的场景正处于这个风险下。）
    """
    try:
        supabase = get_supabase_client()

        data: List[Dict] = []
        page = 1000
        start = 0
        while True:
            # order('id') 保证翻页顺序稳定，避免分页时重复/漏行
            resp = (
                supabase.table("scene_objects")
                .select("*")
                .eq("scene_id", scene_id)
                .order("id")
                .range(start, start + page - 1)
                .execute()
            )
            rows = resp.data or []
            data.extend(rows)
            if len(rows) < page:
                break
            start += page

        # 解析 JSONB 字段
        for obj in data:
            if 'position' in obj and isinstance(obj['position'], str):
                obj['position'] = json.loads(obj['position'])
            if 'rotation' in obj and isinstance(obj['rotation'], str):
                obj['rotation'] = json.loads(obj['rotation'])
            if 'scale' in obj and isinstance(obj['scale'], str):
                obj['scale'] = json.loads(obj['scale'])
            if 'custom_props' in obj and isinstance(obj['custom_props'], str):
                obj['custom_props'] = json.loads(obj['custom_props'])
        return data
    except Exception as e:
        print(f"⚠️ get_scene_objects 失败: {e}")
        return []


def sync_scene_objects(scene_id: str, objects: List[Dict]) -> bool:
    """
    同步场景物体到 Supabase
    严格过滤：只插入数据库中真实存在的列
    """
    try:
        supabase = get_supabase_client()

        # 🔥 只允许这些字段（严格对应你的表结构）
        ALLOWED_FIELDS = {
            'scene_id', 'object_uuid', 'type', 'name',
            'position', 'rotation', 'scale',
            'bind_station_id', 'custom_props', 'utilization'
        }

        # 1. 删除该场景所有物体
        supabase.table("scene_objects").delete().eq("scene_id", scene_id).execute()

        # 2. 如果有物体，插入新数据
        if objects:
            insert_data = []
            for obj in objects:
                new_obj = {}
                for k, v in obj.items():
                    # 跳过临时字段
                    if k.startswith('_'):
                        continue
                    # 跳过不在白名单里的字段
                    if k not in ALLOWED_FIELDS:
                        continue

                    # JSONB 字段：直接把 dict 交给 PostgREST 序列化，不要 json.dumps
                    # 🔥 修复（双重编码）：原来 json.dumps(v) 会把 dict 变成「内容为
                    #    JSON 文本的字符串」，写进 jsonb 列就成了双重编码，与前端 JS 路径
                    #    （直接传对象）不一致。实测全库 4820 个 JSON 字段中有 368 个是这种
                    #    字符串（92 行 × 4 字段），且按场景完全分化——Python 写过的场景
                    #    全是字符串，JS 同步过的场景全是对象。
                    #    历史数据归一化见 sql/002_normalize_json_columns.sql。
                    #    这里额外把"已经是字符串"的值解析回 dict，使本路径也能自愈。
                    if k in ('position', 'rotation', 'scale', 'custom_props'):
                        if isinstance(v, str):
                            try:
                                v = json.loads(v)
                            except Exception:
                                pass
                        new_obj[k] = v
                    else:
                        new_obj[k] = v

                # 补齐必填字段
                new_obj['scene_id'] = scene_id
                if 'object_uuid' not in new_obj:
                    new_obj['object_uuid'] = str(uuid.uuid4())
                if 'custom_props' not in new_obj:
                    new_obj['custom_props'] = {}   # 🔥 修复：原为字符串 '{}'，同样造成双重编码
                if 'utilization' not in new_obj:
                    new_obj['utilization'] = 0.5

                insert_data.append(new_obj)

            # 批量插入
            if insert_data:
                resp = supabase.table("scene_objects").insert(insert_data).execute()
                success = len(resp.data) == len(objects)
                if not success:
                    print(f"⚠️ 插入数量不匹配: 期望 {len(objects)}, 实际 {len(resp.data)}")
                return success

        return True

    except Exception as e:
        print(f"❌ sync_scene_objects 失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def delete_scene_object(scene_id: str, object_uuid: str) -> bool:
    """删除场景中的单个物体"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("scene_objects")\
            .delete()\
            .eq("scene_id", scene_id)\
            .eq("object_uuid", object_uuid)\
            .execute()
        # 如果返回数据长度为1，表示删除成功
        return len(resp.data) == 1
    except Exception as e:
        print(f"⚠️ delete_scene_object 失败: {e}")
        return False


def update_camera_state(scene_id: str, camera_position: Dict, camera_target: Dict):
    """更新相机状态"""
    try:
        supabase = get_supabase_client()
        supabase.table("scenes")\
            .update({
                # 🔥 修复：这两列是 jsonb，直接传 dict 即可；json.dumps 会造成双重编码
                #    （实测这 14 行的 camera_position / camera_target 都是正常的 dict）
                "camera_position": camera_position,
                "camera_target": camera_target
            })\
            .eq("id", scene_id)\
            .execute()
    except Exception as e:
        print(f"⚠️ update_camera_state 失败: {e}")


# ==================== 实时数据操作 ====================

def get_all_station_data() -> Dict[str, Dict]:
    """获取所有充电桩的实时数据，以 station_id 为 key"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("station_realtime_data")\
            .select("*")\
            .execute()

        result = {}
        for item in resp.data or []:
            result[item['station_id']] = item
        return result
    except Exception as e:
        print(f"⚠️ get_all_station_data 失败: {e}")
        return {}


def update_station_data(station_id: str, data: Dict) -> bool:
    """更新单个充电桩的实时数据"""
    try:
        supabase = get_supabase_client()

        # 检查是否存在
        resp = supabase.table("station_realtime_data")\
            .select("*")\
            .eq("station_id", station_id)\
            .execute()

        if resp.data:
            # 更新
            supabase.table("station_realtime_data")\
                .update(data)\
                .eq("station_id", station_id)\
                .execute()
        else:
            # 插入
            data['station_id'] = station_id
            supabase.table("station_realtime_data")\
                .insert(data)\
                .execute()
        return True
    except Exception as e:
        print(f"⚠️ update_station_data 失败: {e}")
        return False


def batch_update_station_data(data_list: List[Dict]) -> bool:
    """批量更新充电桩数据"""
    try:
        for item in data_list:
            station_id = item.pop('station_id')
            update_station_data(station_id, item)
        return True
    except Exception as e:
        print(f"⚠️ batch_update_station_data 失败: {e}")
        return False


# ==================== Realtime 订阅（简化版） ====================

def subscribe_realtime(callback):
    """
    订阅 station_realtime_data 表的实时更新
    注意：此功能需要 supabase-py 的 Realtime 支持
    简单起见，这里返回一个占位
    """
    try:
        supabase = get_supabase_client()
        channel = supabase.channel('public:station_realtime_data')

        def handle_change(payload):
            callback(payload)

        channel.on('*', callback=handle_change).subscribe()
        return channel
    except Exception as e:
        print(f"⚠️ subscribe_realtime 失败: {e}")
        return None


# ==================== 场景市场操作 ====================

def publish_scene(scene_id: str, author: str = "匿名", description: str = "", tags: list = None) -> bool:
    """将场景发布到公共场景库"""
    try:
        supabase = get_supabase_client()
        update_data = {
            "is_public": True,
            "author": author,
            "description": description,
            "tags": tags or [],
        }
        resp = supabase.table("scenes") \
            .update(update_data) \
            .eq("id", scene_id) \
            .execute()
        return len(resp.data) > 0
    except Exception as e:
        print(f"⚠️ 发布场景失败: {e}")
        return False


def unpublish_scene(scene_id: str) -> bool:
    """将场景从公共库撤回"""
    try:
        supabase = get_supabase_client()
        supabase.table("scenes") \
            .update({"is_public": False}) \
            .eq("id", scene_id) \
            .execute()
        return True
    except Exception as e:
        print(f"⚠️ 撤回场景失败: {e}")
        return False


def get_public_scenes(limit: int = 20, order_by: str = "likes") -> list:
    """获取公共场景列表"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("scenes") \
            .select("id, name, author, description, tags, likes, view_count, thumbnail, created_at") \
            .eq("is_public", True) \
            .order(order_by, desc=True) \
            .limit(limit) \
            .execute()
        return resp.data or []
    except Exception as e:
        print(f"⚠️ 获取公共场景失败: {e}")
        return []


def get_public_scenes_by_keyword(keyword: str, limit: int = 20) -> list:
    """按关键词搜索公共场景"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("scenes") \
            .select("id, name, author, description, tags, likes, view_count, thumbnail") \
            .eq("is_public", True) \
            .or_(f"name.ilike.%{keyword}%,description.ilike.%{keyword}%") \
            .order("likes", desc=True) \
            .limit(limit) \
            .execute()
        return resp.data or []
    except Exception as e:
        print(f"⚠️ 搜索公共场景失败: {e}")
        return []


def like_scene(scene_id: str) -> int:
    """点赞场景，返回最新点赞数"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("scenes").select("likes").eq("id", scene_id).execute()
        if not resp.data:
            return 0
        current = resp.data[0].get("likes", 0) or 0
        new_likes = current + 1
        supabase.table("scenes").update({"likes": new_likes}).eq("id", scene_id).execute()
        return new_likes
    except Exception as e:
        print(f"⚠️ 点赞失败: {e}")
        return -1


def increment_scene_view(scene_id: str) -> bool:
    """场景浏览量+1"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("scenes").select("view_count").eq("id", scene_id).execute()
        if not resp.data:
            return False
        current = resp.data[0].get("view_count", 0) or 0
        supabase.table("scenes").update({"view_count": current + 1}).eq("id", scene_id).execute()
        return True
    except Exception as e:
        print(f"⚠️ 更新浏览量失败: {e}")
        return False


def clone_scene(source_scene_id: str, new_name: str = None) -> str:
    """克隆公共场景为自己的新场景"""
    import uuid as uuid_module
    try:
        supabase = get_supabase_client()

        resp = supabase.table("scenes").select("*").eq("id", source_scene_id).execute()
        if not resp.data:
            return None
        source = resp.data[0]

        new_id = str(uuid_module.uuid4())
        new_scene = {
            "id": new_id,
            "name": new_name or f"{source.get('name', '场景')} (副本)",
            "is_public": False,
            "author": "我的克隆",
            "description": f"克隆自：{source.get('name', '未知场景')}",
        }
        supabase.table("scenes").insert(new_scene).execute()

        obj_resp = supabase.table("scene_objects") \
            .select("object_uuid, type, name, position, rotation, scale, bind_station_id, custom_props, utilization") \
            .eq("scene_id", source_scene_id) \
            .execute()

        if obj_resp.data:
            new_objects = []
            for obj in obj_resp.data:
                new_obj = dict(obj)
                new_obj['scene_id'] = new_id
                new_obj['object_uuid'] = str(uuid_module.uuid4())
                new_objects.append(new_obj)

            supabase.table("scene_objects").insert(new_objects).execute()
            print(f"✅ 克隆 {len(new_objects)} 个物体")

        increment_scene_view(source_scene_id)

        return new_id
    except Exception as e:
        print(f"⚠️ 克隆场景失败: {e}")
        return None


def get_geo_tile(region_name: str, tile_type: str = "center"):
    """获取地理底图数据"""
    try:
        supabase = get_supabase_client()
        resp = supabase.table("geo_tiles") \
            .select("*") \
            .eq("region_name", region_name) \
            .eq("tile_type", tile_type) \
            .execute()
        if resp.data:
            return resp.data[0].get("geojson")
        return None
    except Exception as e:
        print(f"⚠️ 获取地理底图失败: {e}")
        return None