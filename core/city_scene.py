"""
core/city_scene.py

深圳市场景的分层加载 —— 核心（充电站）与可选城市肌理（路网 + 楼块）。

为什么分层
----------
实测场景 HTML 里 sceneData 占 82%，而它每次加载都要经 WebSocket 传输、
再作为 iframe 的 srcdoc 属性注入并二次解析。1.91 MB 的 HTML 让页面长期卡在
加载遮罩上。

按用途拆分后：

    shenzhen_core_scene.json   1436 个物体 / 0.56 MB   充电站 + 区界
    shenzhen_layer_scene.json  5494 个物体 / 1.79 MB   路网 + 楼块

首次只加载核心，HTML 从 1.91 MB 降到约 0.6 MB（降约 70%）；
需要看城市肌理时再点「加载路网与楼块」按需追加。

设计约束（都是踩过的坑）
------------------------
· 追加城市层时**必须保留**已有物体，不能覆盖（否则用户的充电站场景会丢）
· 追加后要合并 `current_scene.objects`，否则导出/保存会丢新增的物体
· 追加是纯本地的，不写 Supabase（与示例小镇同理）
"""
import json
import os
from datetime import datetime

import streamlit as st

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_PATH = os.path.join(_PROJECT_ROOT, 'static', 'data', 'shenzhen_core_scene.json')
LAYER_PATH = os.path.join(_PROJECT_ROOT, 'static', 'data', 'shenzhen_layer_scene.json')

# 与核心场景区分：城市层只加这些类型，避免重复或冲突
LAYER_TYPES = {'road', 'building', 'building_tall'}


def core_available() -> bool:
    return os.path.isfile(CORE_PATH)


def layer_available() -> bool:
    return os.path.isfile(LAYER_PATH)


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _layer_type_counts(objects):
    counts = {}
    for o in objects:
        t = o.get('type', '?')
        counts[t] = counts.get(t, 0) + 1
    return counts


def load_core_scene() -> bool:
    """载入核心深圳市场景（充电站 + 区界）。成功返回 True。"""
    if not core_available():
        st.error('❌ 未找到核心城市场景文件')
        st.caption('💡 需要先运行：`python tools/split_city_scene.py`')
        return False

    try:
        from core.scene_manager import SceneData
    except Exception as e:
        st.error('❌ 场景模块不可用：%s' % e)
        return False

    try:
        data = _read(CORE_PATH)
    except Exception as e:
        st.error('❌ 核心场景损坏：%s' % e)
        return False

    objects = data.get('objects', [])
    if not objects:
        st.error('❌ 核心场景为空')
        return False

    import uuid

    st.session_state.current_scene = SceneData(
        scene_name=data.get('scene_name', '深圳市充电站'),
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        objects=objects,
        metadata=dict(data.get('metadata', {}), builtin=True),
    )
    st.session_state.scene_objects = objects
    st.session_state.selected_object_id = None
    st.session_state.export_html_content = None
    st.session_state.scene_id = str(uuid.uuid4())
    # 🔥 必须置 True，否则 init_db_scene() 会去 Supabase 查这个本地随机 UUID，
    #    查不到就把场景清空（示例小镇踩过同样的坑）
    st.session_state._db_initialized = True
    # 记录城市层是否已加载，供按钮状态判断
    st.session_state['_city_layer_loaded'] = False

    try:
        if 'scene_id' in st.query_params:
            del st.query_params['scene_id']
    except Exception:
        pass
    st.session_state.pop('_preview_original', None)

    st.toast(f"✅ 已载入深圳市充电站（{len(objects)} 个物体）", icon='🏙️')
    return True


def append_city_layer() -> bool:
    """把城市肌理（路网 + 楼块）追加到当前场景。成功返回 True。

    ⚠️ 是「追加」不是「替换」：先扣掉已有的同类物体，再并入，避免重复点击
       导致物体翻倍。
    """
    if not layer_available():
        st.error('❌ 未找到城市肌理文件')
        st.caption('💡 需要先运行：`python tools/split_city_scene.py`')
        return False

    try:
        data = _read(LAYER_PATH)
    except Exception as e:
        st.error('❌ 城市肌理文件损坏：%s' % e)
        return False

    layer_objects = data.get('objects', [])
    if not layer_objects:
        st.error('❌ 城市肌理文件为空')
        return False

    current = list(st.session_state.get('scene_objects', []) or [])
    # 扣掉已有的同类物体（支持反复点击/重新加载而不翻倍）
    kept = [o for o in current if o.get('type') not in LAYER_TYPES]
    removed = len(current) - len(kept)

    merged = kept + layer_objects
    st.session_state.scene_objects = merged

    # 🔥 同步到 current_scene，否则导出/保存/发布都会丢掉新加的物体
    cur = st.session_state.get('current_scene')
    if cur is not None and hasattr(cur, 'objects'):
        cur.objects = merged

    st.session_state['_city_layer_loaded'] = True
    st.session_state.selected_object_id = None

    counts = _layer_type_counts(layer_objects)
    detail = ' · '.join(f'{k} {v}' for k, v in sorted(counts.items()))
    if removed:
        st.toast(f"✅ 城市肌理已更新（{detail}；替换掉旧的 {removed} 个）", icon='🛣️')
    else:
        st.toast(f"✅ 已追加城市肌理（{detail}）", icon='🛣️')
    return True


def render_city_entry(key_suffix: str = ''):
    """深圳市场景的入口卡片，放在「场景库」面板里。

    ⚠️ key_suffix 用于区分不同调用点 —— 同一页面出现两个相同 key 的
       st.button 会抛 DuplicateWidgetID，按钮直接渲染不出来（示例小镇踩过）。
    """
    if not core_available():
        return

    try:
        core = _read(CORE_PATH)
    except Exception as e:
        st.warning('⚠️ 城市场景资源异常：%s' % e)
        return

    n_station = sum(1 for o in core.get('objects', [])
                    if o.get('type', '').startswith('charger'))
    core_mb = os.path.getsize(CORE_PATH) / 1024 / 1024

    layer_counts = {}
    layer_mb = 0.0
    if layer_available():
        try:
            layer = _read(LAYER_PATH)
            layer_counts = _layer_type_counts(layer.get('objects', []))
            layer_mb = os.path.getsize(LAYER_PATH) / 1024 / 1024
        except Exception:
            pass

    loaded_layer = st.session_state.get('_city_layer_loaded', False)

    st.markdown(
        '<div style="background:linear-gradient(135deg,rgba(74,144,217,0.16),'
        'rgba(155,89,182,0.10));border:1px solid rgba(74,144,217,0.35);'
        'border-radius:12px;padding:12px 14px;margin-bottom:10px;">'
        '<div style="color:#eef2ff;font-weight:700;font-size:13px;">🏙️ 深圳市充电站分布</div>'
        '<div style="color:#8899bb;font-size:11.5px;margin-top:4px;line-height:1.6;">'
        f'真实经纬度的 <b style="color:#51cf66;">{n_station}</b> 个充电站'
        f'（覆盖深圳 9 个区）。<br>'
        f'核心场景 {core_mb:.2f} MB；城市肌理（路网/楼块）'
        f'<b style="color:#fcc419;">{layer_mb:.2f} MB</b>，按需加载。</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button('🏙️ 载入深圳充电站', key='load_city_core' + key_suffix,
                     use_container_width=True, type='primary'):
            if load_core_scene():
                st.rerun()

    with col_b:
        label = '🛣️ 更新城市肌理' if loaded_layer else '🛣️ 加载路网与楼块'
        if st.button(label, key='load_city_layer' + key_suffix,
                     use_container_width=True,
                     disabled=not layer_available(),
                     help=(f"追加 {layer_counts.get('road', 0)} 条路段与 "
                           f"{layer_counts.get('building', 0)} 个楼块；"
                           f"物体数会明显增加，加载需要几秒")
                     if layer_available() else '缺少城市肌理文件'):
            if append_city_layer():
                st.rerun()

    if loaded_layer:
        st.caption('✅ 当前场景已包含路网与楼块')
