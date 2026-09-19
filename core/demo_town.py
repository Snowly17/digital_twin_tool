# -*- coding: utf-8 -*-
"""
core/demo_town.py

「一键载入示例小镇」—— 让部署后的用户**零文件、零服务、零配置**看到一座完整小镇。

背景（为什么需要这个模块）：
    原来演示场景只能靠用户自己上传 JSON，或者依赖本地跑 MySQL / InfluxDB / MQTT。
    对纯浏览器用户来说这些都不成立，"生成模拟城市"又只是 iframe 里的 isHelper
    装饰物（不进 scene_objects、不落库）。结果是新用户打开就是空场景。

本模块把示例小镇作为**静态资源随应用发布**（static/data/demo_town.json），
由服务端读盘并写进 session_state，所以：
    · 不需要用户上传任何文件
    · 不需要任何外部服务
    · 对象是真实的场景对象（会进 scene_objects、能保存、能被实时数据驱动）

数据来源：mock_data/town/scene_town.json（由 mock_data/town/build_town.py 生成）。
两者内容一致，改动请改生成脚本后重新同步，不要手改这个副本。
"""
import json
import os
import uuid
from datetime import datetime

import streamlit as st

# 项目根 / static / data
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
DEMO_TOWN_PATH = os.path.join(_PROJECT_ROOT, 'static', 'data', 'demo_town.json')

# 载入后必须存在的字段（缺了前端会渲染不出来）
_REQUIRED_KEYS = ('id', 'type', 'name', 'position', 'rotation', 'scale', 'custom_props')


def demo_town_available() -> bool:
    """示例小镇资源是否就位（供 UI 决定要不要显示入口）"""
    return os.path.exists(DEMO_TOWN_PATH)


def load_demo_town_data():
    """读盘并做最小校验。返回 (scene_name, objects, meta) 或抛异常。"""
    with open(DEMO_TOWN_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    objects = data.get('objects') or []
    if not objects:
        raise ValueError('示例小镇文件里没有对象')

    for i, obj in enumerate(objects):
        missing = [k for k in _REQUIRED_KEYS if k not in obj]
        if missing:
            raise ValueError('第 %d 个对象缺少字段 %s' % (i, missing))

    meta = data.get('metadata') or {}
    return data.get('scene_name') or '示例小镇', objects, meta


def _demo_description(meta):
    """把元数据拼成一句人话，显示在界面上"""
    counts = meta.get('type_counts') or {}
    chargers = sum(v for k, v in counts.items() if k.startswith('charger'))
    buildings = sum(v for k, v in counts.items() if k.startswith('building'))
    parts = []
    if chargers:
        parts.append('%d 个充电桩' % chargers)
    if buildings:
        parts.append('%d 栋建筑' % buildings)
    return ' · '.join(parts) if parts else (meta.get('description') or '内置演示场景')


def load_demo_town(unsaved_hint: bool = True) -> bool:
    """把示例小镇载入当前会话。成功返回 True。

    注意：这里**故意不写 Supabase**。原因是示例小镇属于"给大家随手看看"的公共
    内容，如果每个用户点一下就往云端插一份，场景市场很快会被几十份一模一样的
    "示例小镇"淹没。用户想留着，界面上有"另存为我的场景"。
    """
    try:
        from core.scene_manager import SceneData
    except Exception as e:
        st.error('❌ 场景模块不可用：%s' % e)
        return False

    try:
        scene_name, objects, meta = load_demo_town_data()
    except Exception as e:
        st.error('❌ 示例小镇加载失败：%s' % e)
        st.caption('💡 检查 %s 是否存在、是否被改坏' % os.path.relpath(DEMO_TOWN_PATH, _PROJECT_ROOT))
        return False

    # 每次载入都换新 scene_id：避免前端 localStorage 里的旧相机/旧状态串到演示场景上。
    new_scene_id = str(uuid.uuid4())

    st.session_state.current_scene = SceneData(
        scene_name=scene_name,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        objects=objects,
        metadata={'source': 'demo_town', 'builtin': True},
    )
    st.session_state.scene_objects = objects
    st.session_state.selected_object_id = None
    st.session_state.export_html_content = None
    st.session_state.scene_id = new_scene_id

    # 🔥 关键修复（踩过的坑）：必须把 _db_initialized 置为 True。
    #    app.py 的 init_db_scene() 每次 rerun 都会跑；它看到 _db_initialized=False
    #    就会拿着 scene_id 去 Supabase 精确查询，而示例小镇是纯本地的随机 UUID，
    #    库里当然没有 → 命中「场景不存在」分支 → **把 scene_objects 清空成空白场景**。
    #    现象就是：点了按钮一阵闪烁，然后空场景。
    st.session_state._db_initialized = True

    # 示例小镇只存在于当前会话，不往 Supabase 写（否则场景市场会被几十份一样的
    # "示例小镇"淹没）。因此这里**不把 scene_id 固化到 URL**——否则刷新后
    # 又会拿着这个库里不存在的 ID 去查，重演上面那个清空。
    try:
        if 'scene_id' in st.query_params:
            del st.query_params['scene_id']
    except Exception:
        pass

    # 预览态要一并清掉：_preview_original 里存着"进预览前"的场景快照，
    # 若不清，界面上那个「✕ 退出预览」会把用户拉回另一个场景，把小镇顶掉。
    st.session_state.pop('_preview_original', None)

    # 🔥 场景名输入框也要一起重置（踩过的坑）：
    #    app.py 的 render_scene_panel() 里有个 st.text_input(key='panel_scene_name')，
    #    它的值会被 Streamlit 记住并**优先于** value 参数；紧接着那段的
    #    set_scene_name_safe(scene_name) 又把这个旧值写回 current_scene.scene_name。
    #    结果：载入示例小镇后，场景名会被上一轮的旧值覆盖
    #    （实测被改成"空白场景"，而对象其实已经正确载入 118 个）。
    #    删掉这个 widget key，下次渲染就会用 get_scene_name_safe() 拿到新名字。
    st.session_state.pop('panel_scene_name', None)

    chargers = [o for o in objects if str(o.get('type', '')).startswith('charger')]
    bound = [o for o in chargers if o.get('bind_station_id')]
    st.toast('✅ 已载入示例小镇（%d 个对象）' % len(objects), icon='🏘️')

    if unsaved_hint and bound:
        st.session_state['_demo_town_loaded'] = True

    return True


def render_demo_town_entry(compact: bool = False, key_suffix: str = ''):
    """示例小镇的入口卡片。放在「场景库」面板顶部。

    compact=True 时只出一个按钮（用于空状态里，不重复铺说明）。

    ⚠️ key_suffix 必须区分调用点：同一个页面里出现两个相同 key 的 st.button，
       Streamlit 会抛 DuplicateWidgetID，按钮直接渲染不出来
       （踩过：面板顶部和空状态各调了一次，两边都用默认 key）。
    """
    if not demo_town_available():
        return

    try:
        _, objects, meta = load_demo_town_data()
    except Exception as e:
        st.warning('⚠️ 示例小镇资源异常：%s' % e)
        return

    counts = meta.get('type_counts') or {}
    charger_n = sum(v for k, v in counts.items() if k.startswith('charger'))

    if not compact:
        st.markdown(
            '<div style="background:linear-gradient(135deg,rgba(74,144,217,0.16),'
            'rgba(81,207,102,0.10));border:1px solid rgba(74,144,217,0.35);'
            'border-radius:12px;padding:12px 14px;margin-bottom:10px;">'
            '<div style="color:#eef2ff;font-weight:700;font-size:13px;">🏘️ 示例小镇</div>'
            '<div style="color:#8899bb;font-size:11.5px;margin-top:4px;line-height:1.6;">'
            '内置演示场景，<b style="color:#51cf66;">不需要上传任何文件</b>，'
            '也不需要本地起数据库。<br>'
            f'{len(objects)} 个对象 · {_demo_description(meta)}；'
            f'其中 {charger_n} 个充电桩<b style="color:#51cf66;">已绑好 station_id</b>，'
            '可直接看实时数据、演示 MQTT 反向控制。</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    if st.button('🏘️ 载入示例小镇', key='load_demo_town_btn' + key_suffix,
                 use_container_width=True, type='primary'):
        if load_demo_town():
            st.rerun()
