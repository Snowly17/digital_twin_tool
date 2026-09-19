"""
core/ponder_library.py

「思索」教学课件库 —— 纯数据层，不含任何渲染逻辑。

设计原则（与项目其余部分解耦）：
  · 本模块只负责"教什么"，前端 static/js/ponder.js 负责"怎么演"。
  · 每个课件都跑在真实代码路径上，不做编造的演示。
  · 输出必须是 json.dumps 可序列化的纯 dict，由 app.py 注入前端数据岛。

数据结构三层：
  1) stage  —— 教学装置：nodes（节点）+ edges（连线），沙盒里现搭示意几何体
  2) steps  —— 编排：每步一组 time-anchored actions + duration + narration
  3) actions —— 原语调用：focus / flow / tween / label / camera / compare

原语清单（前端必须实现这些 do 值）：
  camera  运镜        {eye:[x,y,z], look:[x,y,z], ms:int}
  focus   聚光高亮    {target:[nodeId...], dim:float, pulsate:bool}
  compare 左右对照    {left:{...}, right:{...}, title:str}
  flow    能量/数据流  {from:nodeId, to:nodeId, kind, rate, reverse}
  tween   数值过渡    {key:str, from:float, to:float, ms:int, unit:str}
  label   悬浮标注    {node:nodeId, text:str, tone:str}
  stage   节点进出场   {show:[...], hide:[...]}
  wait    空拍        {ms:int}
  branch  反事实分叉   {text:str, tone:str}
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ============================================================
# 教学装置：示意几何体的类型映射（供前端 prim 工厂使用）
# ============================================================
# 说明：工具级课程（数据链路 / 四层架构 / 排查手册）刻意使用"简化示意几何体"。
# 理由：这些课讲的是数据与逻辑关系，装置越抽象越清晰，也省掉 GLB 加载开销。
# 但**设备级课程**（拆解一台真实设备）不能停在示意体上——见下方 DEVICE_ASSETS。
PRIM_SPEC: Dict[str, Dict[str, Any]] = {
    "grid":     {"shape": "box",      "size": [1.4, 2.4, 1.4], "color": "#33475f", "name": "电网"},
    "solar":    {"shape": "slab",     "size": [2.2, 0.14, 1.6], "color": "#2f6ea8", "name": "光伏阵列"},
    "battery":  {"shape": "box",      "size": [1.6, 1.0, 0.9],  "color": "#3f6f52", "name": "储能"},
    "charger":  {"shape": "pillar",   "size": [0.42, 2.0, 0.36], "color": "#4a90d9", "name": "充电桩"},
    "car":      {"shape": "car",      "size": [1.5, 0.55, 0.8],  "color": "#c0554f", "name": "车辆"},
    "city":     {"shape": "skyline",  "size": [1.8, 1.6, 1.8],  "color": "#6a8a9a", "name": "城市/建筑"},
    "cloud":    {"shape": "cloud",    "size": [2.0, 0.8, 1.4],  "color": "#5a6b85", "name": "云平台"},
    "database": {"shape": "cylinder", "size": [0.9, 1.2, 0.9],  "color": "#7a5ca8", "name": "时序库"},
    "model":    {"shape": "octa",     "size": [1.0, 1.0, 1.0],  "color": "#c98b2e", "name": "预测模型"},
    "sensor":   {"shape": "cone",     "size": [0.6, 0.7, 0.6],  "color": "#3fa88a", "name": "传感器"},
    "screen":   {"shape": "screen",   "size": [2.4, 1.5, 0.1],  "color": "#2a3f5f", "name": "孪生视图"},
    # 语义层 / 数据层用的"抽象节点"示意体：扁平方块，与实体模型明显区分
    "chip":     {"shape": "box",      "size": [1.7, 0.55, 0.7], "color": "#2f7f8f", "name": "语义节点"},
    # 回转体：齿轮箱 / 发电机这类"圆柱形部件"用它，形态比方块更贴切
    "cylinder": {"shape": "cylinder", "size": [1.2, 0.9, 1.2],  "color": "#7a7a86", "name": "回转部件"},
}

# ============================================================
# 分镜墙配置
# ============================================================
# 对齐 Create 的 Ponder：屏幕上并排若干"分镜格"，每格是同一装置在
# 不同时刻的示意场景，主轴贯穿全流程，当前格高亮播放。
# 为什么不给每章都开一格：WebGL 上下文数量有限（浏览器一般 8~16 个），
# 而且要保证每格看得清。超出上限的章节会被合并进同一格播放。
STORYBOARD_MAX_PANELS = 4



def get_prim_spec() -> Dict[str, Dict[str, Any]]:
    """返回示意几何体规格（前端 prim 工厂消费）。"""
    return PRIM_SPEC


# ============================================================
# 智能教学装置（Device）
# ============================================================
# 与 Ponder 的差距正在这里：Create 的教学装置是**真实方块拼出来的机构**，
# 能拆、能装、能讲"零件如何组成整体"。抽象示意体做不到这一点。
#
# 本项目用「真实 GLB 部件 + 抽象零件」混合拼装：
#   · 外壳 / 集装箱 / 光伏阵列 / 车辆 → 复用 static/models 下真实模型
#   · 内部模块（功率模块 / 电池簇 / 散热）→ 抽象方块
#     （真实设备内部本来就看不见，示意体反而更清楚）
# 关键不在"看起来真"，而在**每个零件都是独立对象**，因此可以：
#   take_apart（按 take 偏移拆开）→ 逐个讲解 → assemble（回到 pos 装回整体）
#
# 坐标：origin = 装置中心地面；part.pos / part.take 为 [x, y, z]（y 为高度）
DEVICE_ASSETS: Dict[str, Dict[str, Any]] = {
    # ⚠️ 只用 static/models 下**真实存在**的文件（已逐个核对）；
    #    引用不存在的模型会静默变成空装置。自检里有一条专门查这个。
    "container_a":  {"kind": "glb", "url": "/app/static/models/container_a.glb"},
    "container_b":  {"kind": "glb", "url": "/app/static/models/container_b.glb"},
    "container_c":  {"kind": "glb", "url": "/app/static/models/container_c.glb"},
    "solar_group":  {"kind": "glb", "url": "/app/static/models/solar_panel_group.glb"},
    "solar_port":   {"kind": "glb", "url": "/app/static/models/solar_panel_port.glb"},
    "car":          {"kind": "glb", "url": "/app/static/models/car.glb"},
    "windmill":     {"kind": "glb", "url": "/app/static/models/windmill.glb"},
    "lamp":         {"kind": "glb", "url": "/app/static/models/traffic-light.glb"},
    # 🔥 整机充电桩：由 tools/make_charger_glb.py 程序化生成（项目原本没有这个模型）。
    #    有了它，装置课的外壳就用**真实模型**，而不是方块拼装。
    #    重新生成：python tools/make_charger_glb.py
    "charger_dc":   {"kind": "glb", "url": "/app/static/models/charger_dc.glb",
                     "targetSize": 3.2},
    # 充电桩的**抽象拼装件**：用于"纯拆装演示"（零件全部可见、位置分明）。
    # 与真实整机是两种讲法，各有用处，所以两套都保留。
    "pile_pillar":  {"kind": "prim", "prim": "grid",    "scale": 0.30, "color": "#3d5a7a",
                     "label": "立柱"},
    "pile_body":    {"kind": "prim", "prim": "charger", "scale": 1.0,  "label": "桩体"},
    "pile_screen":  {"kind": "prim", "prim": "screen",  "scale": 0.32, "label": "显示屏"},
    # 内部模块：真实设备内部看不见，示意体更清楚
    "pm_box":       {"kind": "prim", "prim": "grid",    "scale": 0.45, "color": "#8a5a3a",
                     "label": "功率模块"},
    "ctrl_box":     {"kind": "prim", "prim": "chip",    "scale": 0.80, "label": "控制单元"},
    "cool_box":     {"kind": "prim", "prim": "battery", "scale": 0.55, "color": "#3a6a8a",
                     "label": "散热单元"},
    "batt_cluster": {"kind": "prim", "prim": "battery", "scale": 0.85, "label": "电池簇"},
    "pcs_box":      {"kind": "prim", "prim": "model",   "scale": 0.65, "label": "变流器"},
    "gun":          {"kind": "prim", "prim": "sensor",  "scale": 0.55, "color": "#c98b2e",
                     "label": "充电枪"},
    # ---- 光伏 / 风机的内部零件（同样属于"看不见的部分"，用示意体） ----
    "combiner_box": {"kind": "prim", "prim": "chip",    "scale": 0.75, "color": "#4a6a8a",
                     "label": "汇流箱"},
    "inverter_box": {"kind": "prim", "prim": "model",   "scale": 0.70, "color": "#c98b2e",
                     "label": "逆变器"},
    "ac_cabinet":   {"kind": "prim", "prim": "grid",    "scale": 0.50, "color": "#8a5a3a",
                     "label": "交流配电柜"},
    "meter_box":    {"kind": "prim", "prim": "screen",  "scale": 0.30, "color": "#3fa88a",
                     "label": "计量与并网"},
    "gearbox":      {"kind": "prim", "prim": "cylinder","scale": 0.55, "color": "#7a7a86",
                     "label": "齿轮箱"},
    "generator":    {"kind": "prim", "prim": "cylinder","scale": 0.60, "color": "#c98b2e",
                     "label": "发电机"},
    "converter":    {"kind": "prim", "prim": "model",   "scale": 0.65, "color": "#4a6a8a",
                     "label": "变流柜"},
    "nacelle_ctrl": {"kind": "prim", "prim": "chip",    "scale": 0.70, "color": "#3f6f52",
                     "label": "主控"},
}

DEVICES: Dict[str, Dict[str, Any]] = {
    # ---------- 直流快充桩（零件拼装，可完整拆解） ----------
    "dc_charger": {
        "name": "直流快充桩",
        "summary": "从外壳到功率模块，拆开看一台快充桩由什么组成",
        "parts": [
            {"id": "pillar", "asset": "pile_pillar", "pos": [0, 0, 0],       "take": [-2.2, 0, 0],
             "brief": "立柱：把桩体架到操作高度"},
            {"id": "shell",  "asset": "pile_body",   "pos": [0, 1.5, 0],     "take": [-2.2, 2.2, 0],
             "brief": "桩体外壳：防护与散热风道"},
            {"id": "screen", "asset": "pile_screen", "pos": [0, 2.6, 0.35],  "take": [-0.3, 3.4, 1.9],
             "brief": "显示屏：计费与状态"},
            {"id": "pm",     "asset": "pm_box",      "pos": [0, 1.9, -0.2],  "take": [1.4, 2.4, -2.0],
             "brief": "功率模块：交流变直流"},
            {"id": "ctrl",   "asset": "ctrl_box",    "pos": [0, 1.2, -0.2],  "take": [2.6, 1.2, -1.2],
             "brief": "控制单元：计量、通信、计费"},
            {"id": "cool",   "asset": "cool_box",    "pos": [0, 0.7, -0.2],  "take": [2.2, 0.4, 1.6],
             "brief": "散热单元：大功率必须主动散热"},
            {"id": "gun",    "asset": "gun",         "pos": [1.3, 0.6, 0.5], "take": [3.4, 0.6, 1.6],
             "brief": "充电枪：电流的最后一段"},
        ],
    },
    # ---------- 直流快充桩（真实整机模型 + 抽出内部模块） ----------
    # 与上面 dc_charger 的区别：外壳用真实 GLB 整机，只把**内部模块**抽出来讲。
    # 讲"这台设备长什么样、里面有什么"用这个；讲"零件怎么装起来"用 dc_charger。
    "dc_charger_real": {
        "name": "直流快充桩（整机）",
        "summary": "以真实整机为基准，把内部模块逐个抽出来看",
        "parts": [
            {"id": "shell", "asset": "charger_dc", "pos": [0, 0, 0],      "take": [-3.4, 0, 0],
             "brief": "整机：外壳 / 面板 / 枪座 / 线缆"},
            {"id": "pm",    "asset": "pm_box",     "pos": [0, 1.05, 0],   "take": [0, 2.3, 2.2],
             "brief": "功率模块：交流变直流"},
            {"id": "ctrl",  "asset": "ctrl_box",   "pos": [1.5, 1.10, 0], "take": [3.0, 1.6, 1.4],
             "brief": "控制单元：计量、通信、计费"},
            {"id": "cool",  "asset": "cool_box",   "pos": [-1.5, 1.05, 0], "take": [-3.0, 1.4, 1.8],
             "brief": "散热单元：大功率必须主动散热"},
            {"id": "gun",   "asset": "gun",        "pos": [2.8, 0.5, 0.6], "take": [4.4, 0.5, 2.0],
             "brief": "充电枪：电流的最后一段"},
        ],
    },
    # ---------- 光伏阵列（真实阵列模型 + 抽出电气链路） ----------
    # 光伏阵列本体是一个模型（多块板 + 支架），看不出"电是怎么出去的"；
    # 把汇流箱 / 逆变器 / 配电柜抽出来，链路就可见了。
    "pv_array": {
        "name": "光伏阵列",
        "summary": "板子只是开始，电要经过汇流、逆变、配电才能并网",
        "parts": [
            {"id": "panel",  "asset": "solar_group",  "pos": [-3.6, 0, 0],    "take": [-5.0, 0, 1.6],
             "brief": "光伏阵列：把光变成直流电"},
            {"id": "combine","asset": "combiner_box", "pos": [0, 0.9, 0],     "take": [0, 1.5, 2.6],
             "brief": "汇流箱：多路直流汇成一路"},
            {"id": "inv",    "asset": "inverter_box", "pos": [3.0, 0.95, 0],   "take": [3.0, 1.7, 2.6],
             "brief": "逆变器：直流变交流"},
            {"id": "ac",     "asset": "ac_cabinet",   "pos": [5.6, 0.85, 0],   "take": [6.0, 1.4, 2.6],
             "brief": "交流配电柜：分配与保护"},
            {"id": "meter",  "asset": "meter_box",    "pos": [7.6, 1.0, 0],    "take": [8.4, 1.6, 2.6],
             "brief": "计量与并网"},
        ],
    },
    # ---------- 风机（真实机舱叶片 + 抽出传动链） ----------
    "wind_turbine": {
        "name": "风力发电机组",
        "summary": "从叶片到并网，机械能是怎么变成电的",
        "parts": [
            {"id": "blades", "asset": "windmill",   "pos": [-3.0, 0, 0],    "take": [-4.6, 0, 1.8],
             "brief": "叶片与轮毂：捕获风能"},
            {"id": "gear",   "asset": "gearbox",    "pos": [0, 1.6, 0],      "take": [0, 2.6, 2.6],
             "brief": "齿轮箱：把低转速提到发电转速"},
            {"id": "gen",    "asset": "generator",  "pos": [2.4, 1.6, 0],    "take": [2.4, 2.6, 2.6],
             "brief": "发电机：机械能变电能"},
            {"id": "conv",   "asset": "converter",  "pos": [4.9, 0.95, 0],   "take": [5.6, 1.6, 2.6],
             "brief": "变流柜：频率与电压适配"},
            {"id": "ctrl",   "asset": "nacelle_ctrl","pos": [7.2, 1.0, 0],   "take": [8.2, 1.6, 2.6],
             "brief": "主控：偏航、变桨与并网策略"},
        ],
    },
    # ---------- 储能集装箱（真实箱体 + 抽象内部） ----------
    "bess_container": {
        "name": "储能集装箱",
        "summary": "拆开集装箱：电池簇、变流器、控制与散热如何协同",
        "parts": [
            {"id": "shell", "asset": "container_a",  "pos": [0, 0, 0],      "take": [-3.0, 0, 0],
             "brief": "集装箱体：防护与消防"},
            {"id": "batt",  "asset": "batt_cluster", "pos": [0, 0.6, 0],    "take": [0, 1.8, 2.4],
             "brief": "电池簇：能量的载体"},
            {"id": "pcs",   "asset": "pcs_box",      "pos": [1.8, 0.8, 0],  "take": [3.0, 1.6, 1.2],
             "brief": "变流器：交直流互转"},
            {"id": "ctrl",  "asset": "ctrl_box",     "pos": [0, 2.6, 0],    "take": [0, 3.4, -2.0],
             "brief": "控制单元：SOC 与保护策略"},
            {"id": "cool",  "asset": "cool_box",     "pos": [-1.8, 0.6, 0], "take": [-2.8, 1.1, 1.6],
             "brief": "散热：温度决定电池寿命"},
        ],
    },
    # ---------- 光储充微网（全部真实模型） ----------
    "micro_grid": {
        "name": "光储充微电网",
        "summary": "三台设备如何组成一个能自洽运行的微网",
        "parts": [
            {"id": "pv",   "asset": "solar_group", "pos": [-4.6, 0, 0],   "take": [-5.8, 0, 2.0],
             "brief": "光伏：发电侧"},
            {"id": "bess", "asset": "container_b", "pos": [0, 0, 0],      "take": [0, 0, 2.8],
             "brief": "储能：缓冲与削峰"},
            {"id": "pile", "asset": "container_c", "pos": [4.6, 0, 0],    "take": [5.8, 0, 2.0],
             "brief": "充电侧：能量出口"},
            {"id": "car",  "asset": "car",         "pos": [6.6, 0, -1.0], "take": [8.2, 0, -2.0],
             "brief": "负荷：车辆"},
        ],
    },
}

# 装置内部的能量/数据走向（装回整体后展示"它们怎么连起来"）
DEVICE_LINKS: Dict[str, List[Dict[str, str]]] = {
    "micro_grid": [
        {"from": "pv",   "to": "bess", "kind": "power", "label": "直流母线"},
        {"from": "bess", "to": "pile", "kind": "power", "label": "削峰供电"},
        {"from": "pile", "to": "car",  "kind": "power", "label": "充电"},
    ],
    "dc_charger": [
        {"from": "pm",   "to": "ctrl", "kind": "logic", "label": "指令"},
        {"from": "ctrl", "to": "gun",  "kind": "power", "label": "直流输出"},
    ],
    "dc_charger_real": [
        {"from": "ctrl", "to": "pm",   "kind": "logic", "label": "控制"},
        {"from": "pm",   "to": "gun",  "kind": "power", "label": "直流输出"},
    ],
    "pv_array": [
        {"from": "panel",   "to": "combine", "kind": "power", "label": "直流汇流"},
        {"from": "combine", "to": "inv",     "kind": "power", "label": "直流"},
        {"from": "inv",     "to": "ac",      "kind": "power", "label": "交流"},
        {"from": "ac",      "to": "meter",   "kind": "render","label": "并网"},
    ],
    "wind_turbine": [
        {"from": "blades", "to": "gear", "kind": "logic", "label": "机械转矩"},
        {"from": "gear",   "to": "gen",  "kind": "logic", "label": "提速"},
        {"from": "gen",    "to": "conv", "kind": "power", "label": "电能"},
        {"from": "conv",   "to": "ctrl", "kind": "render","label": "并网"},
    ],
    "bess_container": [
        {"from": "batt", "to": "pcs",  "kind": "power", "label": "直流侧"},
        {"from": "pcs",  "to": "ctrl", "kind": "logic", "label": "调度"},
    ],
}


def get_devices() -> Dict[str, Dict[str, Any]]:
    return DEVICES


def get_device_assets() -> Dict[str, Dict[str, Any]]:
    return DEVICE_ASSETS


# ============================================================
# 课件 01：从 0 到一个活着的孪生体
# ============================================================
# 受众：数字孪生建模/集成/运维人员。
# 不以"科普充电原理"为目标，而以"讲清本工具的数据链路"为目标——
# 因为从业者不缺行业知识，缺的是这个工具的心智模型，
# 尤其是"绑定错了会静默失效"这类真实坑。
# ------------------------------------------------------------
LESSON_TWIN_PIPELINE: Dict[str, Any] = {
    "id": "twin_pipeline",
    "icon": "💭",
    "title": "从 0 到一个活着的孪生体",
    "subtitle": "数字孪生数据链路全解析",
    "audience": "数字孪生建模 / 集成 / 运维人员",
    "summary": "一个物体从摆进场景到活起来，中间经过了什么。",
    # scope=tool：工具级课程，讲的是整个工具的心智模型。
    # 这类课挂在「某一个物体」旁边不成立（充电桩的属性面板里为什么会出现
    # "四层架构"？语义错位），所以只在右侧面板目录里出现，不进对象气泡。
    "scope": "tool",
    "stage": {
        "nodes": [
            {"id": "sensor", "type": "sensor",  "pos": [-9.0, 0, 0],   "label": "物理设备"},
            {"id": "edge",   "type": "cloud",   "pos": [-6.0, 0, 0],   "label": "边缘/云"},
            {"id": "db",     "type": "database","pos": [-3.0, 0, 0],   "label": "时序库"},
            {"id": "model",  "type": "model",   "pos": [0.0, 0, 0],    "label": "预测模型"},
            {"id": "norm",   "type": "grid",    "pos": [3.0, 0, 0],    "label": "归一化"},
            {"id": "obj",    "type": "charger", "pos": [6.0, 0, 0],    "label": "孪生对象"},
            {"id": "view",   "type": "screen",  "pos": [9.0, 0, 0],    "label": "孪生视图"},
        ],
        "edges": [
            {"from": "sensor", "to": "edge",  "kind": "data",   "label": "MQTT 上报"},
            {"from": "edge",   "to": "db",    "kind": "data",   "label": "清洗入库"},
            {"from": "db",     "to": "model", "kind": "data",   "label": "历史样本"},
            {"from": "model",  "to": "norm",  "kind": "logic",  "label": "预测利用率"},
            {"from": "norm",   "to": "obj",   "kind": "logic",  "label": "写回对象"},
            {"from": "obj",    "to": "view",  "kind": "render", "label": "视觉映射"},
        ],
    },
    "steps": [
        # ---------- ① 摆进场景 ≠ 活着 ----------
        {
            "id": "step_geometry",
            "short": "摆进场景",
            "brief": "只有几何位置",
            "title": "① 摆进场景 ≠ 活着",
            "narration": "拖进场景的物体，此刻只有几何位置。它看起来在孪生体里，但其实和真实世界没有任何连接。",
            "camera": {"eye": [0, 8.5, 19], "look": [0, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["obj"], "hide": ["sensor", "edge", "db", "model", "norm", "view"]},
                {"at": 200, "do": "focus", "target": ["obj"], "dim": 0.18, "pulsate": False},
                {"at": 600, "do": "label", "node": "obj", "text": "position / rotation / scale", "tone": "info"},
                {"at": 1400, "do": "compare",
                 "title": "孪生体的定义",
                 "left": {"title": "只有几何", "color": "#8a94a6", "lines": ["位置、旋转、缩放", "模型外观", "无数据来源", "无状态更新"]},
                 "right": {"title": "活着的孪生体", "color": "#51cf66", "lines": ["绑定真实站点", "实时数据驱动", "状态可预测", "可反向控制"]}},
                {"at": 3000, "do": "branch", "text": "静默失效：物体看着没问题，但数据永远不更新", "tone": "warn"},
            ],
        },
        # ---------- ② 绑定：孪生体的身份证 ----------
        {
            "id": "step_bind",
            "short": "绑定站点",
            "brief": "连上真实世界",
            "title": "② 绑定：孪生体的身份证",
            "narration": "绑定站点 ID，是这个物体与真实世界之间唯一的那根线。没有它，孪生体就只是一个模型。",
            "camera": {"eye": [1.5, 5.5, 14], "look": [3.5, 1.0, 0], "ms": 800},
            "actions": [
                {"at": 0, "do": "stage", "show": ["obj", "view"], "hide": ["sensor", "edge", "db", "model", "norm"]},
                {"at": 100, "do": "focus", "target": ["obj", "view"], "dim": 0.3},
                {"at": 300, "do": "flow", "from": "obj", "to": "view", "kind": "render", "rate": 1.0, "label": "bind_station_id"},
                {"at": 900, "do": "tween", "key": "bind", "from": 0.0, "to": 1.0, "ms": 900, "unit": ""},
                {"at": 2000, "do": "label", "node": "obj", "text": "station_id = 1001", "tone": "ok"},
                {"at": 2400, "do": "compare",
                 "title": "有没有这根线，差别在哪",
                 "left": {"title": "未绑定", "color": "#ff6b6b", "lines": ["数据源：模拟 / 兜底值", "利用率：恒定不刷新", "告警：永不触发", "碳减排：按默认估算"]},
                 "right": {"title": "已绑定", "color": "#51cf66", "lines": ["数据源：真实站点实时值", "利用率：随 MQTT 刷新", "告警：阈值触发推送", "碳减排：真实充电量核算"]}},
                {"at": 4600, "do": "branch", "text": "排查第一问：这个对象的 bind_station_id 是空的吗？", "tone": "info"},
            ],
        },
        # ---------- ③ 数据怎么变成 utilization ----------
        {
            "id": "step_pipeline",
            "short": "数据流转",
            "brief": "清洗与归一化",
            "title": "③ 数据怎么变成 utilization",
            "narration": "从物理传感器上报，到变成一个能驱动画面的利用率数值，中间要经过采集、清洗、映射三道关。",
            "camera": {"eye": [-4.5, 7.5, 17], "look": [-4.5, 1.0, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage", "show": ["sensor", "edge", "db", "model", "norm"], "hide": ["obj", "view"]},
                {"at": 200, "do": "focus", "target": ["sensor", "edge", "db", "model", "norm"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "sensor", "to": "edge", "kind": "data", "rate": 1.4, "label": "采集"},
                {"at": 900, "do": "flow", "from": "edge", "to": "db", "kind": "data", "rate": 1.2, "label": "清洗入库"},
                {"at": 1500, "do": "flow", "from": "db", "to": "model", "kind": "data", "rate": 1.0, "label": "历史样本"},
                {"at": 2100, "do": "flow", "from": "model", "to": "norm", "kind": "logic", "rate": 1.0, "label": "预测"},
                {"at": 2400, "do": "tween", "key": "util", "from": 0.0, "to": 0.83, "ms": 1400, "unit": ""},
                {"at": 4000, "do": "label", "node": "norm", "text": "utilization = 0.83", "tone": "ok"},
                {"at": 4400, "do": "compare",
                 "title": "三道关各自解决什么",
                 "left": {"title": "常见错误", "color": "#ff6b6b", "lines": ["直接用原始报文渲染", "单位 / 量纲不统一", "断连时数值归零", "离线设备仍显在线"]},
                 "right": {"title": "本工具的做法", "color": "#51cf66", "lines": ["统一归一化到 0~1", "本地缓存兜底", "断连保持最后有效值", "status 独立字段判定"]}},
                {"at": 6600, "do": "branch", "text": "排查第二问：数据停了，画面会不会静默篡改真实状态？", "tone": "info"},
            ],
        },
        # ---------- ④ 一个数值，三种视觉语义 ----------
        {
            "id": "step_visual",
            "short": "视觉映射",
            "brief": "数值变成颜色",
            "title": "④ 一个数值，三种视觉语义",
            "narration": "归一化后的利用率驱动颜色的变化。同一个数字，在不同的区间里表达完全不同的运维含义。",
            "camera": {"eye": [7.5, 5.0, 12], "look": [7.0, 1.2, 0], "ms": 800},
            "actions": [
                {"at": 0, "do": "stage", "show": ["obj", "view"], "hide": ["sensor", "edge", "db", "model", "norm"]},
                {"at": 100, "do": "focus", "target": ["obj", "view"], "dim": 0.22},
                {"at": 300, "do": "tween", "key": "util", "from": 0.0, "to": 0.95, "ms": 2600, "unit": ""},
                {"at": 600, "do": "label", "node": "obj", "text": "util → 颜色 / 脉冲 / 告警", "tone": "info"},
                {"at": 3200, "do": "compare",
                 "title": "三档视觉语义",
                 "left": {"title": "util < 0.7", "color": "#51cf66", "lines": ["绿色", "低速呼吸脉冲", "状态：正常", "无告警"]},
                 "right": {"title": "util > 0.7 或离线", "color": "#ffaa00", "lines": ["黄色 / 红色", "高频脉冲", "状态：高负载 / 离线", "触发告警推送"]}},
                {"at": 5400, "do": "branch", "text": "排查第三问：阈值是单一真值吗？显示和判断用的是同一个数吗？", "tone": "info"},
            ],
        },
        # ---------- ⑤ 收束 ----------
        {
            "id": "step_recap",
            "short": "全链路",
            "brief": "采集到驱动",
            "title": "⑤ 一条链路，全长这样",
            "narration": "把这几步接起来，就是完整的数据链路。任何一个环节断开，孪生体都会失去意义。",
            "camera": {"eye": [0, 9.0, 21], "look": [0, 1.0, 0], "ms": 1100},
            "actions": [
                {"at": 0, "do": "stage", "show": ["sensor", "edge", "db", "model", "norm", "obj", "view"], "hide": []},
                {"at": 300, "do": "flow", "from": "sensor", "to": "edge", "kind": "data", "rate": 1.5, "label": "采集"},
                {"at": 700, "do": "flow", "from": "edge", "to": "db", "kind": "data", "rate": 1.3, "label": "清洗"},
                {"at": 1100, "do": "flow", "from": "db", "to": "model", "kind": "data", "rate": 1.1, "label": "预测"},
                {"at": 1500, "do": "flow", "from": "model", "to": "norm", "kind": "logic", "rate": 1.1, "label": "映射"},
                {"at": 1900, "do": "flow", "from": "norm", "to": "obj", "kind": "logic", "rate": 1.1, "label": "写回"},
                {"at": 2300, "do": "flow", "from": "obj", "to": "view", "kind": "render", "rate": 1.1, "label": "驱动"},
                {"at": 2800, "do": "tween", "key": "util", "from": 0.2, "to": 0.86, "ms": 1600, "unit": ""},
                {"at": 4600, "do": "label", "node": "view", "text": "采集 → 清洗 → 映射 → 驱动", "tone": "ok"},
                # 🔥 收束：把前面三章埋的排查问题归位成一张清单。
                #    前四章各有对照面板，唯独收束章没有会让这一章视觉密度明显偏空；
                #    更重要的是，"排查入口"才是这门课最该被带走的东西。
                {"at": 5200, "do": "compare",
                 "title": "链路环节 × 排查入口",
                 "left": {"title": "环节", "color": "#88aadd", "lines": ["采集", "清洗 / 入库", "映射 / 归一化", "驱动 / 视觉"]},
                 "right": {"title": "出问题时查这里", "color": "#51cf66", "lines": ["设备是否在上报", "时间戳是否新鲜", "绑定与阈值是否一致", "颜色 / 告警是否生效"]}},
                {"at": 8400, "do": "branch", "text": "✅ 记住这条链路，排查时从任意一环切入都能定位", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 02：这个孪生体的四层架构
# ============================================================
# 受众同上，但这门课回答的是"孪生体由什么构成"。
# 四层与代码的真实对应：
#   几何层 → scene_objects 的 position/rotation/scale + GLB 模型
#   数据层 → utilization(归一化 0~1) / status(在线·高负载·离线)
#   语义层 → custom_props.relations（relation_builder.py 自动推导）
#   行为层 → updateChargerVisual 的颜色/脉冲/告警/碳核算
# 语义层的三个阈值取自 core/relation_builder.py:128-131 的真实默认值，
# 不是编的——这也是本课最有说服力的一点：关系是"算"出来的，不是"画"出来的。
# ------------------------------------------------------------
LESSON_HIERARCHY: Dict[str, Any] = {
    "id": "hierarchy",
    "icon": "🧱",
    "title": "这个孪生体的四层架构",
    "subtitle": "几何 / 数据 / 语义 / 行为",
    "audience": "数字孪生建模 / 集成 / 运维人员",
    "summary": "同一个物体，在四个层面分别是什么。",
    # scope=tool：讲的是"孪生体由什么构成"这个通用框架，与具体设备无关
    "scope": "tool",
    "stage": {
        # 两层排布：前排 x=-2 承载"几何 + 数据"，后排 x=+2.5 承载"语义 + 行为"。
        # 这样 9 个节点的横向跨度只有约 9 个单位，窄 iframe 下也能看清；
        # 竖直方向由 y=0/3/6/9 四档表达"层"的概念。
        "nodes": [
            # —— 几何层（y=0）——
            {"id": "geo_pile",     "type": "charger",  "pos": [-4.5, 0, 0], "label": "充电桩"},
            {"id": "geo_road",     "type": "city",     "pos": [-0.5, 0, 0], "label": "道路"},
            {"id": "geo_building", "type": "city",     "pos": [2.5, 0, 0],  "label": "建筑"},
            {"id": "geo_tree",     "type": "solar",    "pos": [6.0, 0, 0],  "label": "景观"},
            # —— 数据层（y=3）——
            {"id": "data_util",    "type": "database", "pos": [-4.5, 3, 0], "label": "utilization"},
            {"id": "data_status",  "type": "sensor",   "pos": [-0.5, 3, 0], "label": "status"},
            # —— 语义层（y=6）——
            {"id": "sem_pile",     "type": "chip",     "pos": [0.5, 6, 0],  "label": "语义节点"},
            {"id": "sem_rel",      "type": "chip",     "pos": [5.0, 6, 0],  "label": "关系"},
            # —— 行为层（y=9）——
            {"id": "behav",        "type": "screen",   "pos": [2.5, 9, 0],  "label": "行为 / 告警"},
        ],
        "edges": [
            {"from": "geo_pile",    "to": "data_util",   "kind": "data",   "label": "读取指标"},
            {"from": "geo_road",    "to": "data_status", "kind": "data",   "label": "状态"},
            {"from": "data_util",   "to": "sem_pile",    "kind": "logic",  "label": "附着到对象"},
            {"from": "sem_pile",    "to": "sem_rel",     "kind": "logic",  "label": "自动推导关系"},
            {"from": "sem_rel",     "to": "behav",       "kind": "render", "label": "语义驱动行为"},
        ],
    },
    "steps": [
        # ---------- ① 四层全景 ----------
        {
            "id": "hier_overview",
            "short": "四层全景",
            "brief": "几何数据语义行为",
            "title": "① 四层全景",
            "narration": "同一个物体，在四个层面同时存在：几何层描述它在哪，数据层描述它是什么状态，语义层描述它和谁有关，行为层描述它会做什么。",
            "camera": {"eye": [4, 9.5, 18], "look": [0.5, 4.5, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "geo_road", "geo_building", "geo_tree",
                          "data_util", "data_status", "sem_pile", "sem_rel", "behav"],
                 "hide": []},
                {"at": 200, "do": "focus", "target": [], "dim": 0.4},
                {"at": 500, "do": "compare",
                 "title": "四个层次各自的职责",
                 "left": {"title": "看得见的", "color": "#88aadd", "lines": ["几何层：位置 / 旋转 / 缩放", "几何层：模型外观", "数据层：实时指标", "数据层：在线状态"]},
                 "right": {"title": "看不见但更关键", "color": "#c98b2e", "lines": ["语义层：和谁有关系", "语义层：属于哪个片区", "行为层：越界会怎样", "行为层：能预测什么"]}},
                {"at": 3500, "do": "branch", "text": "多数孪生体只做了下面两层，上面两层才是分水岭", "tone": "info"},
            ],
        },
        # ---------- ② 几何层 ----------
        {
            "id": "hier_geometry",
            "short": "几何层",
            "brief": "它在哪",
            "title": "② 几何层：它在哪",
            "narration": "几何层是最基础的一层，只回答空间问题：这个物体在哪里、多大、朝向如何。它不关心这个物体是干什么的。",
            "camera": {"eye": [4, 6.5, 14], "look": [0.5, 1.5, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "geo_road", "geo_building", "geo_tree"],
                 "hide": ["data_util", "data_status", "sem_pile", "sem_rel", "behav"]},
                {"at": 150, "do": "focus", "target": ["geo_pile", "geo_road", "geo_building", "geo_tree"], "dim": 0.25},
                {"at": 400, "do": "label", "node": "geo_pile", "text": "position / rotation / scale", "tone": "info"},
                {"at": 1100, "do": "label", "node": "geo_building", "text": "模型外观（GLB）", "tone": "info"},
                {"at": 2200, "do": "compare",
                 "title": "几何层能回答与不能回答的",
                 "left": {"title": "能回答", "color": "#51cf66", "lines": ["位置坐标", "旋转角度", "缩放比例", "模型外观"]},
                 "right": {"title": "回答不了", "color": "#ff6b6b", "lines": ["运行是否正常", "数据来自哪里", "和谁有关联", "异常时该做什么"]}},
                {"at": 4400, "do": "branch", "text": "只有几何层 = 一个好看的模型，不是孪生体", "tone": "warn"},
            ],
        },
        # ---------- ③ 数据层 ----------
        {
            "id": "hier_data",
            "short": "数据层",
            "brief": "它怎么样",
            "title": "③ 数据层：它现在怎么样",
            "narration": "数据层把真实世界的测量值搬进孪生体。关键是两个字段：归一化到 0 到 1 的利用率，和独立的在线状态。",
            "camera": {"eye": [4, 5.5, 12], "look": [0.5, 2.5, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "data_util", "data_status"],
                 "hide": ["geo_road", "geo_building", "geo_tree", "sem_pile", "sem_rel", "behav"]},
                {"at": 150, "do": "focus", "target": ["data_util", "data_status"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "geo_pile", "to": "data_util", "kind": "data", "rate": 1.3, "label": "实时上报"},
                {"at": 1200, "do": "tween", "key": "util", "from": 0.0, "to": 0.83, "ms": 1500,
                 "nodes": ["data_util", "geo_pile"]},
                {"at": 3000, "do": "label", "node": "data_util", "text": "utilization = 0.83（归一化）", "tone": "ok"},
                {"at": 3400, "do": "label", "node": "data_status", "text": "status = 在线", "tone": "ok"},
                {"at": 4200, "do": "compare",
                 "title": "两个字段为什么要分开",
                 "left": {"title": "利用率 utilization", "color": "#4aa3ff", "lines": ["连续数值 0~1", "描述忙不忙", "驱动颜色深浅", "可做趋势预测"]},
                 "right": {"title": "状态 status", "color": "#51cf66", "lines": ["离散枚举", "描述通不通", "驱动在线 / 离线", "优先于利用率"]}},
                {"at": 6400, "do": "branch", "text": "离线设备的利用率是 0，但「0」和「离线」是两件完全不同的事", "tone": "warn"},
            ],
        },
        # ---------- ④ 语义层（本课重点）----------
        {
            "id": "hier_semantic",
            "short": "语义层",
            "brief": "它和谁有关",
            "title": "④ 语义层：它和谁有关",
            "narration": "语义层是孪生体开始变聪明的地方。关系不是手工画的，而是根据空间距离自动推导出来的。",
            "camera": {"eye": [3.5, 8.5, 15], "look": [1.5, 5.0, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "geo_road", "geo_building", "sem_pile", "sem_rel"],
                 "hide": ["geo_tree", "data_util", "data_status", "behav"]},
                {"at": 150, "do": "focus", "target": ["sem_pile", "sem_rel"], "dim": 0.28},
                {"at": 400, "do": "flow", "from": "geo_pile", "to": "sem_pile", "kind": "logic", "rate": 1.0, "label": "附着语义"},
                {"at": 1200, "do": "flow", "from": "sem_pile", "to": "sem_rel", "kind": "logic", "rate": 1.1, "label": "推导关系"},
                {"at": 2000, "do": "label", "node": "geo_road", "text": "临街：≤ 12m", "tone": "ok"},
                {"at": 2700, "do": "label", "node": "geo_building", "text": "服务建筑：≤ 25m", "tone": "ok"},
                {"at": 3400, "do": "label", "node": "geo_pile", "text": "相邻集群：≤ 10m", "tone": "ok"},
                {"at": 4200, "do": "compare",
                 "title": "语义关系从哪来",
                 "left": {"title": "手工画（常见做法）", "color": "#ff6b6b", "lines": ["拖进去一个画一条", "物体移动后失效", "新增物体要补画", "规模一大就维护不动"]},
                 "right": {"title": "自动推导（本工具做法）", "color": "#51cf66", "lines": ["按距离阈值算出来", "物体移动后自动重算", "新增物体自动入网", "规模增长无需人工"]}},
                {"at": 6600, "do": "branch", "text": "关系一旦可推导，孪生体就能自己回答「这个桩服务于哪栋楼」", "tone": "info"},
            ],
        },
        # ---------- ⑤ 行为层 ----------
        {
            "id": "hier_behavior",
            "short": "行为层",
            "brief": "它会做什么",
            "title": "⑤ 行为层：它会做什么",
            "narration": "行为层把前面三层串起来：几何提供位置，数据提供触发条件，语义提供影响范围，最后表现为一次告警或者一次调度。",
            "camera": {"eye": [4, 9.0, 14], "look": [1.5, 6.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "data_util", "sem_pile", "sem_rel", "behav"],
                 "hide": ["geo_road", "geo_building", "geo_tree", "data_status"]},
                {"at": 150, "do": "focus", "target": ["behav"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "sem_rel", "to": "behav", "kind": "render", "rate": 1.2, "label": "语义驱动"},
                {"at": 1100, "do": "flow", "from": "data_util", "to": "behav", "kind": "data", "rate": 1.2, "label": "阈值触发"},
                {"at": 1800, "do": "tween", "key": "util", "from": 0.3, "to": 0.95, "ms": 2000,
                 "nodes": ["data_util"]},
                {"at": 4200, "do": "label", "node": "behav", "text": "util > 0.7 → 告警", "tone": "warn"},
                {"at": 5000, "do": "compare",
                 "title": "同一个越界，不同层的反应",
                 "left": {"title": "只有数据层", "color": "#8a94a6", "lines": ["知道数值越界", "不知道该通知谁", "不知道影响哪些建筑", "无法做区域调度"]},
                 "right": {"title": "四层齐备", "color": "#51cf66", "lines": ["定位到具体设备", "沿语义找到服务建筑", "评估片区整体负载", "给出调度建议"]}},
                {"at": 7400, "do": "branch", "text": "行为层的价值，取决于下面三层是否都在", "tone": "info"},
            ],
        },
        # ---------- ⑥ 收束 ----------
        {
            "id": "hier_recap",
            "short": "四层齐全",
            "brief": "缺一层就塌",
            "title": "⑥ 四层缺一不可",
            "narration": "把四层摞起来，才是一个能用的孪生体。缺任何一层，能力都会明显塌陷。",
            "camera": {"eye": [4, 10.5, 19], "look": [0.5, 4.5, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["geo_pile", "geo_road", "geo_building", "geo_tree",
                          "data_util", "data_status", "sem_pile", "sem_rel", "behav"],
                 "hide": []},
                {"at": 300, "do": "flow", "from": "geo_pile", "to": "data_util", "kind": "data", "rate": 1.4, "label": "几何 → 数据"},
                {"at": 800, "do": "flow", "from": "data_util", "to": "sem_pile", "kind": "logic", "rate": 1.3, "label": "数据 → 语义"},
                {"at": 1300, "do": "flow", "from": "sem_rel", "to": "behav", "kind": "render", "rate": 1.3, "label": "语义 → 行为"},
                {"at": 1900, "do": "tween", "key": "util", "from": 0.2, "to": 0.9, "ms": 1800,
                 "nodes": ["data_util", "geo_pile"]},
                {"at": 4200, "do": "compare",
                 "title": "缺一层会怎样",
                 "left": {"title": "缺失的层", "color": "#ff6b6b", "lines": ["缺几何 → 没法定位", "缺数据 → 静态模型", "缺语义 → 孤岛设备", "缺行为 → 只能看不能用"]},
                 "right": {"title": "补上之后", "color": "#51cf66", "lines": ["三维空间可定位", "实时状态可跟踪", "关联对象可推理", "异常可告警可调度"]}},
                {"at": 7000, "do": "branch", "text": "✅ 判断一个孪生体成熟度，就看这四层齐不齐", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 03：物理世界怎么变成孪生世界
# ============================================================
# 对应真实链路（全部可在代码里查到）：
#   物理设备 → mock_mqtt_publisher.py 上行（charger/{id}/status，3s 一次）
#   → MQTTChargerClient._handle_status 清洗（缺字段走默认值）
#   → update_station_data 落库 → 前端 Realtime 订阅
#   → obj['utilization'] 归一化 → updateChargerVisual 驱动画面
# ✅ 已实测通过：主题通配订阅 → 8 台模拟设备上报 → 写入 → 场景更新
# ------------------------------------------------------------
LESSON_DATA_FLOW: Dict[str, Any] = {
    "id": "data_flow",
    "icon": "📡",
    "title": "物理世界怎么变成孪生世界",
    "subtitle": "采集 → 清洗 → 映射 → 驱动",
    "audience": "数字孪生集成 / 数据接入人员",
    "summary": "一条数据从设备出发，到画面上变色，中间经过了什么。",
    # scope=object：这张课直接解释"你眼前这个物体的数据从哪来、为什么这样显示"，
    # 对充电桩/传感器类对象是强相关的就地说明，所以进气泡。
    "scope": "object",
    "stage": {
        "nodes": [
            {"id": "device",  "type": "sensor",   "pos": [-8.0, 0, 0], "label": "物理设备"},
            {"id": "gateway", "type": "cloud",    "pos": [-4.0, 0, 0], "label": "MQTT Broker"},
            {"id": "raw",     "type": "chip",     "pos": [0.0, 0, 0],  "label": "清洗"},
            {"id": "store",   "type": "database", "pos": [4.0, 0, 0],  "label": "实时表"},
            {"id": "twin",    "type": "charger",  "pos": [8.0, 0, 0],  "label": "孪生对象"},
            # 反例：与孪生对象并排，用来展示"数据来了但没接上"
            {"id": "broken",  "type": "charger",  "pos": [8.0, 0, -3.5], "label": "未绑定对象"},
            {"id": "dirty",   "type": "chip",     "pos": [0.0, 2.6, 0], "label": "脏数据"},
        ],
        "edges": [
            {"from": "device",  "to": "gateway", "kind": "data",   "label": "MQTT 上报"},
            {"from": "gateway", "to": "raw",     "kind": "data",   "label": "订阅消费"},
            {"from": "raw",     "to": "store",   "kind": "data",   "label": "清洗入库"},
            {"from": "store",   "to": "twin",    "kind": "logic",  "label": "归一化映射"},
            {"from": "raw",     "to": "broken",  "kind": "logic",  "label": "未绑定则断链"},
        ],
    },
    "steps": [
        {
            "id": "flow_intake",
            "short": "采集入口",
            "brief": "一台一主题",
            "title": "① 数据的入口：一台设备一个主题",
            "narration": "每台设备把自己的状态发到属于它的主题上。孪生侧用一条通配订阅，就能一次接住全部设备。",
            "camera": {"eye": [-4, 8.5, 19], "look": [-4, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["device", "gateway"], "hide": ["raw", "store", "twin", "broken", "dirty"]},
                {"at": 150, "do": "focus", "target": ["device", "gateway"], "dim": 0.2},
                {"at": 400, "do": "flow", "from": "device", "to": "gateway", "kind": "data", "rate": 1.5, "label": "charger/{id}/status"},
                {"at": 1600, "do": "label", "node": "gateway", "text": "一次订阅全部设备", "tone": "ok"},
                {"at": 2400, "do": "compare",
                 "title": "接入方式对比",
                 "left": {"title": "每台设备单独接", "color": "#ff6b6b", "lines": ["新增设备要改代码", "主题写死在配置里", "设备多了一团乱麻"]},
                 "right": {"title": "通配订阅（本工具做法）", "color": "#51cf66", "lines": ["新增设备自动进来", "主题用通配符统一匹配", "设备数量不影响架构"]}},
                {"at": 4600, "do": "branch", "text": "先解决「能不能接住」，再谈「接得准不准」", "tone": "info"},
            ],
        },
        {
            "id": "flow_clean",
            "short": "清洗",
            "brief": "挡住脏数据",
            "title": "② 清洗：数据不能直接信",
            "narration": "设备报文会因为固件差异、掉线重连、字段缺失而残缺。清洗层负责把不可信的数据挡在孪生体之外。",
            "camera": {"eye": [0, 8.0, 17], "look": [0, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["device", "gateway", "raw", "dirty"], "hide": ["store", "twin", "broken"]},
                {"at": 150, "do": "focus", "target": ["raw"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "device", "to": "gateway", "kind": "data", "rate": 1.4, "label": "上报"},
                {"at": 900, "do": "flow", "from": "gateway", "to": "dirty", "kind": "data", "rate": 1.2, "label": "缺字段的报文"},
                {"at": 1600, "do": "label", "node": "dirty", "text": "缺 utilization / status", "tone": "warn"},
                {"at": 2200, "do": "flow", "from": "gateway", "to": "raw", "kind": "logic", "rate": 1.4, "label": "清洗"},
                {"at": 3000, "do": "label", "node": "raw", "text": "补齐默认值 → 定长结构", "tone": "ok"},
                {"at": 3800, "do": "compare",
                 "title": "清洗层挡住什么",
                 "left": {"title": "脏数据直接入库", "color": "#ff6b6b", "lines": ["缺字段 → 画面 NaN", "类型不符 → 前端崩", "脏值污染历史序列", "预测模型学到噪声"]},
                 "right": {"title": "先清洗再入库", "color": "#51cf66", "lines": ["字段缺失走默认值", "数值统一转 float", "异常报文直接丢弃", "历史序列保持干净"]}},
                {"at": 6200, "do": "branch", "text": "宁可在入口丢掉一条，也不要把脏数据放进孪生体", "tone": "warn"},
            ],
        },
        {
            "id": "flow_map",
            "short": "映射",
            "brief": "归一化",
            "title": "③ 映射：从原始值到利用率",
            "narration": "原始数值没有统一量纲，不能直接画。归一化成 0 到 1 之后，才有了统一的视觉语义。",
            "camera": {"eye": [4, 7.5, 16], "look": [4, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["raw", "store", "twin"], "hide": ["device", "gateway", "broken", "dirty"]},
                {"at": 150, "do": "focus", "target": ["store", "twin"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "raw", "to": "store", "kind": "data", "rate": 1.3, "label": "入库"},
                {"at": 1100, "do": "flow", "from": "store", "to": "twin", "kind": "logic", "rate": 1.3, "label": "实时订阅"},
                {"at": 1800, "do": "tween", "key": "util", "from": 0.0, "to": 0.83, "ms": 1600, "nodes": ["twin"]},
                {"at": 3800, "do": "label", "node": "twin", "text": "utilization = 0.83", "tone": "ok"},
                {"at": 4400, "do": "compare",
                 "title": "同一个物理量，两种表示",
                 "left": {"title": "原始值", "color": "#8a94a6", "lines": ["功率 96.5 kW", "插槽余 2 个", "电压 750.2 V", "单位各不相同"]},
                 "right": {"title": "归一化后", "color": "#51cf66", "lines": ["utilization = 0.83", "统一落在 0~1", "可直接比大小", "可直接驱动颜色"]}},
                {"at": 6600, "do": "branch", "text": "可视化要的不是数字本身，而是数字之间的可比性", "tone": "info"},
            ],
        },
        {
            "id": "flow_drive",
            "short": "驱动",
            "brief": "数值变画面",
            "title": "④ 驱动：数值怎么变成画面",
            "narration": "归一化后的利用率是唯一进入视觉层的数字。同一份数据，驱动颜色、脉冲和告警三件事。",
            "camera": {"eye": [4.5, 6.0, 13], "look": [4, 1.2, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["store", "twin"], "hide": ["device", "gateway", "raw", "broken", "dirty"]},
                {"at": 150, "do": "focus", "target": ["twin"], "dim": 0.2},
                {"at": 400, "do": "flow", "from": "store", "to": "twin", "kind": "render", "rate": 1.4, "label": "驱动画面"},
                {"at": 1200, "do": "tween", "key": "util", "from": 0.1, "to": 0.95, "ms": 2400, "nodes": ["twin"]},
                {"at": 4000, "do": "label", "node": "twin", "text": "util > 0.7 → 告警", "tone": "warn"},
                {"at": 4600, "do": "compare",
                 "title": "一个数字，三件事",
                 "left": {"title": "数值本身", "color": "#4aa3ff", "lines": ["0.83", "连续、可比较", "可以做趋势预测", "可以设阈值"]},
                 "right": {"title": "驱动的表现", "color": "#51cf66", "lines": ["颜色：绿 / 黄 / 红", "脉冲：频率随负载变化", "告警：越界推送", "碳减排：联动核算"]}},
                {"at": 6800, "do": "branch", "text": "驱动层只读一个字段，这是它稳定的原因", "tone": "ok"},
            ],
        },
        {
            "id": "flow_chain",
            "short": "全程链路",
            "brief": "五段串联",
            "title": "⑤ 全程连起来",
            "narration": "把五段接起来，就是一条完整的孪生数据链。每一段的输入输出都是确定的。",
            "camera": {"eye": [0, 9.5, 22], "look": [0, 1.2, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage", "show": ["device", "gateway", "raw", "store", "twin"], "hide": ["broken", "dirty"]},
                {"at": 300, "do": "flow", "from": "device", "to": "gateway", "kind": "data", "rate": 1.5, "label": "采集"},
                {"at": 700, "do": "flow", "from": "gateway", "to": "raw", "kind": "data", "rate": 1.4, "label": "清洗"},
                {"at": 1100, "do": "flow", "from": "raw", "to": "store", "kind": "data", "rate": 1.3, "label": "入库"},
                {"at": 1500, "do": "flow", "from": "store", "to": "twin", "kind": "logic", "rate": 1.3, "label": "映射"},
                {"at": 2200, "do": "tween", "key": "util", "from": 0.2, "to": 0.88, "ms": 1800, "nodes": ["twin"]},
                {"at": 4400, "do": "compare",
                 "title": "这条链的本质",
                 "left": {"title": "孪生体不是", "color": "#8a94a6", "lines": ["不是 3D 模型展示", "不是数据大屏", "不是定时刷新截图", "不是录屏回放"]},
                 "right": {"title": "孪生体是", "color": "#51cf66", "lines": ["持续同步的映射", "可追溯的数据链", "可解释的状态推断", "可反向作用的闭环"]}},
                {"at": 6600, "do": "branch", "text": "✅ 数据链的每一段都能单独验证，这就是孪生体可运维的前提", "tone": "ok"},
            ],
        },
        {
            "id": "flow_freshness",
            "short": "历史回放",
            "brief": "时序数据",
            "title": "⑥ 数据新鲜度与时序回放",
            "narration": "孪生体不只关心当前值，还关心历史。历史数据让孪生体可以回放过去、预测未来。",
            "camera": {"eye": [2, 6.5, 16], "look": [2, 1.5, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["store", "twin"], "hide": ["device", "gateway", "raw", "broken", "dirty"]},
                {"at": 150, "do": "focus", "target": ["store"], "dim": 0.2},
                {"at": 400, "do": "label", "node": "store", "text": "历史样本：4344 小时 × 1423 站点", "tone": "info"},
                {"at": 1200, "do": "flow", "from": "store", "to": "twin", "kind": "data", "rate": 1.2, "label": "时间轴回放"},
                {"at": 2000, "do": "tween", "key": "util", "from": 0.25, "to": 0.75, "ms": 2200, "nodes": ["twin"]},
                {"at": 4400, "do": "compare",
                 "title": "只有实时值 vs 有历史",
                 "left": {"title": "只有当前值", "color": "#8a94a6", "lines": ["不知道刚才发生了什么", "无法判断趋势", "预测没有输入", "异常无法复盘"]},
                 "right": {"title": "有历史序列", "color": "#51cf66", "lines": ["可回放任意时刻", "可算趋势与峰值", "可训练预测模型", "可事后归因"]}},
                {"at": 6600, "do": "branch", "text": "历史数据是孪生体从「看现在」走向「算未来」的燃料", "tone": "info"},
            ],
        },
    ],
}


# ============================================================
# 课件 04：孪生体排查手册
# ============================================================
# 面向运维。课件里的每个数值都取自真实代码，不是编的：
#   · 绑定缺失 → 数据不再更新（app.py:4075 绑定下拉 / _hero_object 兜底）
#   · 类型未注册 → 灰色盒体兜底（app.py:7927 defaultGenerator，type:custom_model）
#   · 告警阈值单一真值 0.7 存 localStorage（app.py:6718 getAlertThreshold）
#   · LOD 近 15 / 远 35（app.py:9312 LOD_CONFIG 默认值）
# ------------------------------------------------------------
LESSON_DEBUGGING: Dict[str, Any] = {
    "id": "debugging",
    "icon": "🔍",
    "title": "孪生体排查手册",
    "subtitle": "四类静默故障的定位方法",
    "audience": "数字孪生运维 / 值班人员",
    "summary": "最可怕的故障不是报错，而是看起来一切正常。",
    # scope=tool：排查手册是运维的通用方法论，不针对某个具体物体
    "scope": "tool",
    "stage": {
        "nodes": [
            {"id": "ok_device",   "type": "charger", "pos": [-5.0, 0, 0], "label": "正常设备"},
            {"id": "bad_device",  "type": "charger", "pos": [-5.0, 0, -3.6], "label": "异常设备"},
            {"id": "ok_label",    "type": "screen",  "pos": [1.0, 0, 0],  "label": "正常表现"},
            {"id": "bad_label",   "type": "screen",  "pos": [1.0, 0, -3.6], "label": "异常表现"},
            {"id": "unknown",     "type": "grid",    "pos": [6.5, 0, 0],  "label": "未注册类型"},
        ],
        "edges": [
            {"from": "ok_device",  "to": "ok_label",  "kind": "render", "label": "全部正常"},
            {"from": "bad_device", "to": "bad_label", "kind": "logic",  "label": "静默失效"},
        ],
    },
    "steps": [
        {
            "id": "dbg_overview",
            "short": "先分类",
            "brief": "四类故障",
            "title": "① 排查的第一原则：先分类",
            "narration": "孪生体的故障分四类：接不上、对不准、看不见、跑不动。分错类会导致你在错误的地方查半天。",
            "camera": {"eye": [0, 8.5, 19], "look": [0, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["ok_device", "bad_device", "ok_label", "bad_label", "unknown"], "hide": []},
                {"at": 200, "do": "focus", "target": [], "dim": 0.38},
                {"at": 500, "do": "compare",
                 "title": "四类故障",
                 "left": {"title": "看得见的", "color": "#ffaa00", "lines": ["报警了", "红了", "跳数字了", "卡住了"]},
                 "right": {"title": "看不见但更危险", "color": "#ff6b6b", "lines": ["数据根本不更新", "绑错了站点没人知道", "模型没显示但也没报错", "降级兜底悄悄生效"]}},
                {"at": 3400, "do": "branch", "text": "静默失效最危险：画面完全正常，但它在骗你", "tone": "warn"},
            ],
        },
        {
            "id": "dbg_binding",
            "short": "接不上",
            "brief": "绑定缺失",
            "title": "② 分类一：接不上（绑定缺失）",
            "narration": "物体看着好好的，数据却永远是同一个数。这是最典型的静默失效：绑定站点 ID 是空的。",
            "camera": {"eye": [0, 7.0, 15], "look": [0, 1.2, -1.5], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["ok_device", "bad_device", "ok_label", "bad_label"], "hide": ["unknown"]},
                {"at": 150, "do": "focus", "target": ["ok_device", "bad_device"], "dim": 0.25},
                {"at": 400, "do": "label", "node": "ok_device", "text": "station_id = 1001", "tone": "ok"},
                {"at": 1000, "do": "label", "node": "bad_device", "text": "station_id = （空）", "tone": "warn"},
                {"at": 1600, "do": "flow", "from": "ok_device", "to": "ok_label", "kind": "render", "rate": 1.4, "label": "数据持续更新"},
                {"at": 2400, "do": "label", "node": "bad_label", "text": "数值恒定，永不刷新", "tone": "warn"},
                {"at": 3400, "do": "compare",
                 "title": "定位方法",
                 "left": {"title": "现象", "color": "#ffaa00", "lines": ["利用率长期一个值", "告警从不触发", "颜色永远不变", "看板显示「有数据」"]},
                 "right": {"title": "怎么查", "color": "#51cf66", "lines": ["看 bind_station_id 是否为空", "与站点清单比对是否错配", "确认数据源是否上报该 ID", "查是否有兜底默认值在顶上"]}},
                {"at": 5600, "do": "branch", "text": "排查第一问永远是：这个对象的 bind_station_id 是空的吗？", "tone": "info"},
            ],
        },
        {
            "id": "dbg_stale",
            "short": "对不准",
            "brief": "数据陈旧",
            "title": "③ 分类二：对不准（数据陈旧）",
            "narration": "绑定没问题，但数据源停了。孪生体如果继续显示旧值而且不做提示，就是在篡改现实。",
            "camera": {"eye": [0, 7.0, 15], "look": [0, 1.2, -1.5], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["ok_device", "bad_device", "ok_label", "bad_label"], "hide": ["unknown"]},
                {"at": 150, "do": "focus", "target": ["ok_device", "bad_device"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "ok_device", "to": "ok_label", "kind": "data", "rate": 1.4, "label": "时间戳持续刷新"},
                {"at": 1200, "do": "flow", "from": "bad_device", "to": "bad_label", "kind": "data", "rate": 0.3, "label": "上报已停止"},
                {"at": 2200, "do": "label", "node": "bad_label", "text": "沿用最后有效值", "tone": "warn"},
                {"at": 3000, "do": "tween", "key": "util", "from": 0.0, "to": 0.62, "ms": 2000, "nodes": ["ok_device"]},
                {"at": 5200, "do": "compare",
                 "title": "陈旧数据怎么暴露",
                 "left": {"title": "错误做法", "color": "#ff6b6b", "lines": ["静默沿用旧值", "界面上不做任何标记", "把「无数据」当成 0", "让离线设备看起来在线"]},
                 "right": {"title": "正确做法", "color": "#51cf66", "lines": ["显示时间戳与数据年龄", "超期显著提示", "status 独立字段判定", "断连时保留但标注"]}},
                {"at": 7400, "do": "branch", "text": "数据停了不是问题，假装数据没停才是问题", "tone": "warn"},
            ],
        },
        {
            "id": "dbg_render",
            "short": "看不见",
            "brief": "渲染失效",
            "title": "④ 分类三：看不见（渲染与类型）",
            "narration": "数据全对，画面没反应。要么是类型没注册走了兜底，要么是视觉映射没接上。",
            "camera": {"eye": [2, 6.5, 14], "look": [2, 1.2, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["bad_device", "bad_label", "unknown"], "hide": ["ok_device", "ok_label"]},
                {"at": 150, "do": "focus", "target": ["unknown"], "dim": 0.25},
                {"at": 400, "do": "label", "node": "unknown", "text": "type 未注册 → 灰色盒体兜底", "tone": "warn"},
                {"at": 1300, "do": "flow", "from": "bad_device", "to": "bad_label", "kind": "render", "rate": 1.3, "label": "数值更新"},
                {"at": 2200, "do": "tween", "key": "util", "from": 0.15, "to": 0.92, "ms": 1800,
                 "nodes": ["bad_device"], "colorize": False},
                {"at": 4200, "do": "label", "node": "bad_device", "text": "util 变了，颜色没变", "tone": "warn"},
                {"at": 5000, "do": "compare",
                 "title": "画面不动的两种原因",
                 "left": {"title": "类型层", "color": "#ffaa00", "lines": ["type 拼错或未注册", "自定义模型 asset_id 失效", "走了灰色盒体兜底", "看起来「有个东西」但不认识"]},
                 "right": {"title": "映射层", "color": "#51cf66", "lines": ["数值更新了但没接视觉", "颜色阈值写死未生效", "视觉层被开关关掉了", "实例化对象不支持单独改色"]}},
                {"at": 7200, "do": "branch", "text": "兜底逻辑是好事，但它必须留痕，否则会掩盖真实故障", "tone": "warn"},
            ],
        },
        {
            "id": "dbg_perf",
            "short": "跑不动",
            "brief": "性能定位",
            "title": "⑤ 分类四：跑不动（性能定位）",
            "narration": "帧率掉下来时，先看是场景太重还是主循环被拖住。本工具用实例化与 LOD 两级手段控制开销。",
            "camera": {"eye": [1.5, 8.0, 17], "look": [1.5, 1.2, -1.0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["ok_device", "bad_device", "ok_label", "bad_label", "unknown"], "hide": []},
                {"at": 200, "do": "focus", "target": [], "dim": 0.35},
                {"at": 500, "do": "label", "node": "ok_label", "text": "LOD：近 15 / 远 35", "tone": "info"},
                {"at": 1300, "do": "label", "node": "bad_label", "text": "充电桩走实例化渲染", "tone": "info"},
                {"at": 2200, "do": "compare",
                 "title": "帧率低先查这三处",
                 "left": {"title": "常见原因", "color": "#ffaa00", "lines": ["同类物体逐个建模", "高模不带 LOD", "每帧重算全部统计", "阴影 / 后期特效过重"]},
                 "right": {"title": "本工具的做法", "color": "#51cf66", "lines": ["充电桩用实例化网格", "LOD 缓存只在跨级时切换", "LOD 每 5 帧更新一次", "内置 FPS 监控可开关"]}},
                {"at": 4400, "do": "label", "node": "unknown", "text": "FPS 监控：绿 > 50 / 黄 > 30 / 红", "tone": "info"},
                {"at": 5200, "do": "branch", "text": "把性能当功能做：能度量，才能优化", "tone": "ok"},
            ],
        },
        {
            "id": "dbg_checklist",
            "short": "排查清单",
            "brief": "固定顺序",
            "title": "⑥ 一页排查清单",
            "narration": "把这四类固化成一个固定顺序，遇到问题照着走，比凭经验猜快得多。",
            "camera": {"eye": [0, 9.0, 20], "look": [0, 1.2, -1.5], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage", "show": ["ok_device", "bad_device", "ok_label", "bad_label", "unknown"], "hide": []},
                {"at": 300, "do": "flow", "from": "ok_device", "to": "ok_label", "kind": "data", "rate": 1.4, "label": "1 接没接上"},
                {"at": 900, "do": "flow", "from": "ok_device", "to": "ok_label", "kind": "logic", "rate": 1.2, "label": "2 对不对得准"},
                {"at": 1500, "do": "flow", "from": "bad_device", "to": "bad_label", "kind": "render", "rate": 1.2, "label": "3 看得见吗"},
                {"at": 2100, "do": "flow", "from": "bad_device", "to": "unknown", "kind": "render", "rate": 1.0, "label": "4 跑得动吗"},
                {"at": 3000, "do": "compare",
                 "title": "固定排查顺序",
                 "left": {"title": "顺序", "color": "#88aadd", "lines": ["① 数据源有没有在发", "② 绑定与映射是否正确", "③ 视觉映射是否接上", "④ 性能是否拖住了更新"]},
                 "right": {"title": "每一步的判定", "color": "#51cf66", "lines": ["时间戳是否刷新", "ID 是否匹配、单位是否一致", "数值变了画面是否跟着变", "FPS 与 LOD 是否正常"]}},
                {"at": 5400, "do": "branch", "text": "✅ 按顺序查，永远比凭直觉猜快", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 05：让孪生体真正控制物理世界
# ============================================================
# 这条课件讲的是本次新增的能力（见 core/mqtt_client.py 的下行部分）：
#   孪生侧 publish_command → charger/{id}/cmd
#   → 设备执行 → charger/{id}/cmd_reply 回执 + 补发 charger/{id}/status
#   → 孪生侧状态收敛
# ✅ 已端到端实测：限功率 30kW 后利用率 0.723 → 0.286；停用后状态变「离线」
# ⚠️ 诚实标注：设备端由 mock_mqtt_publisher.py 模拟，非真实固件。
# ------------------------------------------------------------
LESSON_REVERSE_CONTROL: Dict[str, Any] = {
    "id": "reverse_control",
    "icon": "🎮",
    "title": "让孪生体真正控制物理世界",
    "subtitle": "指令 → 执行 → 回执 → 收敛",
    "audience": "数字孪生集成 / 控制策略人员",
    "summary": "从「远程监控」到「闭环控制」缺的那一环。",
    # scope=object：这是唯一"某个物体能不能被控制"的课，直接挂在充电桩上
    "scope": "object",
    "stage": {
        "nodes": [
            {"id": "cmd_ui",    "type": "screen",   "pos": [-7.0, 4.5, 0], "label": "孪生侧：下发"},
            {"id": "cmd_link",  "type": "cloud",    "pos": [-7.0, 0, 0],   "label": "下行通道"},
            {"id": "device",    "type": "charger",  "pos": [7.0, 0, 0],    "label": "物理设备"},
            {"id": "actuator",  "type": "battery",  "pos": [7.0, 4.5, 0],  "label": "执行器"},
            {"id": "ack_link",  "type": "cloud",    "pos": [7.0, 9.0, 0],  "label": "上行回执"},
            {"id": "state_sync","type": "screen",   "pos": [-7.0, 9.0, 0], "label": "孪生侧：收敛"},
        ],
        "edges": [
            {"from": "cmd_ui",    "to": "cmd_link",   "kind": "logic",  "label": "指令下发"},
            {"from": "cmd_link",  "to": "device",     "kind": "power",  "label": "charger/{id}/cmd"},
            {"from": "device",    "to": "actuator",   "kind": "power",  "label": "执行"},
            {"from": "actuator",  "to": "ack_link",   "kind": "data",   "label": "回执 + 状态"},
            {"from": "ack_link",  "to": "state_sync", "kind": "render", "label": "cmd_reply"},
        ],
    },
    "steps": [
        {
            "id": "rc_why",
            "short": "为什么要闭环",
            "brief": "订阅不等于控制",
            "title": "① 只订阅不发布，那不叫孪生",
            "narration": "如果孪生体只能看、不能动，它就是一块远程仪表盘。闭环控制才是数字孪生区别于可视化的地方。",
            "camera": {"eye": [0, 9.0, 20], "look": [0, 4.5, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage", "show": ["cmd_ui", "cmd_link", "device", "actuator", "ack_link", "state_sync"], "hide": []},
                {"at": 200, "do": "focus", "target": [], "dim": 0.35},
                {"at": 500, "do": "compare",
                 "title": "两种孪生体",
                 "left": {"title": "远程监控", "color": "#8a94a6", "lines": ["只订阅设备上报", "只在屏幕上显示", "异常只通知人", "人再手动去处理"]},
                 "right": {"title": "闭环控制", "color": "#51cf66", "lines": ["同时具备下行通道", "能向设备下发指令", "能回收执行回执", "状态自动收敛"]}},
                {"at": 3400, "do": "branch", "text": "有下行、有回执、状态能收敛，三者缺一不可", "tone": "info"},
            ],
        },
        {
            "id": "rc_downlink",
            "short": "下行",
            "brief": "指令发出去",
            "title": "② 下行：指令怎么发出去",
            "narration": "指令走独立的主题，携带唯一编号。编号是后面认领回执的唯一凭据。",
            "camera": {"eye": [-6, 7.0, 16], "look": [-5, 3.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["cmd_ui", "cmd_link", "device"], "hide": ["actuator", "ack_link", "state_sync"]},
                {"at": 150, "do": "focus", "target": ["cmd_ui", "cmd_link"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "cmd_ui", "to": "cmd_link", "kind": "logic", "rate": 1.4, "label": "set_limit"},
                {"at": 1100, "do": "flow", "from": "cmd_link", "to": "device", "kind": "power", "rate": 1.4, "label": "charger/1001/cmd"},
                {"at": 2200, "do": "label", "node": "cmd_ui", "text": "cmd_id 唯一编号", "tone": "ok"},
                {"at": 3000, "do": "compare",
                 "title": "指令必须带的三样东西",
                 "left": {"title": "缺了就出问题", "color": "#ff6b6b", "lines": ["没有编号 → 回执认不出是谁", "没有参数边界 → 可能下发危险值", "没有留痕 → 出事后无法复盘"]},
                 "right": {"title": "本工具的做法", "color": "#51cf66", "lines": ["每条指令带 cmd_id", "参数范围校验后才发出", "指令记录全量留痕", "只开放受控指令集"]}},
                {"at": 5200, "do": "branch", "text": "下行通道是双刃剑：护栏必须在下发之前，不能在下发之后", "tone": "warn"},
            ],
        },
        {
            "id": "rc_execute",
            "short": "执行",
            "brief": "设备真实状态",
            "title": "③ 执行：设备侧发生了什么",
            "narration": "设备收到指令后校验、执行、改变自己的真实状态。注意：改的是设备状态，不是画面。",
            "camera": {"eye": [6, 7.0, 16], "look": [7, 2.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["device", "actuator"], "hide": ["cmd_ui", "cmd_link", "ack_link", "state_sync"]},
                {"at": 150, "do": "focus", "target": ["device", "actuator"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "device", "to": "actuator", "kind": "power", "rate": 1.4, "label": "执行指令"},
                {"at": 1200, "do": "label", "node": "actuator", "text": "limit_kw = 30", "tone": "ok"},
                {"at": 2000, "do": "label", "node": "device", "text": "设备真实状态已改变", "tone": "ok"},
                {"at": 3000, "do": "compare",
                 "title": "改的到底是哪里",
                 "left": {"title": "伪闭环", "color": "#ff6b6b", "lines": ["只改了孪生侧的画面", "设备实际功率没变", "下一个上报周期就被覆盖", "看起来成功其实是假的"]},
                 "right": {"title": "真闭环", "color": "#51cf66", "lines": ["指令抵达设备", "设备改变真实状态", "设备主动上报新状态", "孪生侧跟着收敛"]}},
                {"at": 5200, "do": "branch", "text": "判断真假的唯一标准：设备有没有主动上报新状态", "tone": "warn"},
            ],
        },
        {
            "id": "rc_ack",
            "short": "回执",
            "brief": "确认执行成功",
            "title": "④ 回执：怎么知道执行成功了",
            "narration": "指令发出去了不等于执行成功了。只有收到带同一个编号的回执，才算闭环完成。",
            "camera": {"eye": [0, 8.5, 18], "look": [0, 4.5, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["device", "actuator", "ack_link", "state_sync"], "hide": ["cmd_ui", "cmd_link"]},
                {"at": 150, "do": "focus", "target": ["ack_link", "state_sync"], "dim": 0.25},
                {"at": 400, "do": "flow", "from": "actuator", "to": "ack_link", "kind": "data", "rate": 1.4, "label": "cmd_reply + status"},
                {"at": 1200, "do": "flow", "from": "ack_link", "to": "state_sync", "kind": "render", "rate": 1.4, "label": "按 cmd_id 认领"},
                {"at": 2200, "do": "label", "node": "state_sync", "text": "ok = true / 执行详情", "tone": "ok"},
                {"at": 3000, "do": "compare",
                 "title": "只看「已发出」会漏掉什么",
                 "left": {"title": "只看发送结果", "color": "#ff6b6b", "lines": ["网络丢了也不知道", "设备拒绝执行也不知道", "参数被设备改写也不知道", "无法统计执行成功率"]},
                 "right": {"title": "等回执再判定", "color": "#51cf66", "lines": ["能区分「没收到」与「执行失败」", "能读到设备返回的详情", "能统计闭环成功率", "超时能给明确结论"]}},
                {"at": 5200, "do": "branch", "text": "没有回执的指令，等于没有发过", "tone": "warn"},
            ],
        },
        {
            "id": "rc_loop",
            "short": "收敛",
            "brief": "闭环完成",
            "title": "⑤ 闭环：状态最终收敛",
            "narration": "最后一步是确认孪生侧真的跟着变了。这条回路走完，一次控制才算完成。",
            "camera": {"eye": [0, 10.5, 22], "look": [0, 4.5, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage", "show": ["cmd_ui", "cmd_link", "device", "actuator", "ack_link", "state_sync"], "hide": []},
                {"at": 300, "do": "flow", "from": "cmd_ui", "to": "cmd_link", "kind": "logic", "rate": 1.4, "label": "① 下发"},
                {"at": 800, "do": "flow", "from": "cmd_link", "to": "device", "kind": "power", "rate": 1.4, "label": "② 抵达"},
                {"at": 1300, "do": "flow", "from": "device", "to": "actuator", "kind": "power", "rate": 1.4, "label": "③ 执行"},
                {"at": 1800, "do": "flow", "from": "actuator", "to": "ack_link", "kind": "data", "rate": 1.4, "label": "④ 回执"},
                {"at": 2300, "do": "flow", "from": "ack_link", "to": "state_sync", "kind": "render", "rate": 1.4, "label": "⑤ 收敛"},
                {"at": 3600, "do": "compare",
                 "title": "实测结果（真实跑通）",
                 "left": {"title": "下发限功率 30kW 之前", "color": "#ffaa00", "lines": ["utilization = 0.723", "status = 在线", "power = 120 kW"]},
                 "right": {"title": "执行后收敛", "color": "#51cf66", "lines": ["utilization = 0.286", "回执 ok = true", "power = 30 kW"]}},
                {"at": 6000, "do": "branch", "text": "✅ 这五步全走完，才叫一次成功的闭环控制", "tone": "ok"},
            ],
        },
        {
            "id": "rc_boundary",
            "short": "边界",
            "brief": "什么没做",
            "title": "⑥ 诚实边界与安全",
            "narration": "必须说清楚哪些是真的、哪些是模拟的，以及为什么下行通道需要一整套护栏。",
            "camera": {"eye": [0, 9.0, 20], "look": [0, 4.5, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["cmd_ui", "cmd_link", "device", "actuator", "ack_link", "state_sync"], "hide": []},
                {"at": 200, "do": "focus", "target": ["device"], "dim": 0.3},
                {"at": 500, "do": "label", "node": "device", "text": "设备端由模拟器扮演", "tone": "warn"},
                {"at": 1400, "do": "compare",
                 "title": "哪些是真的",
                 "left": {"title": "本轮真实跑通的部分", "color": "#51cf66", "lines": ["MQTT 下行指令链路", "参数边界校验", "指令留痕与回执认领", "状态收敛与超时判定"]},
                 "right": {"title": "仍是模拟的部分", "color": "#ffaa00", "lines": ["设备固件为模拟器", "未接真实充电桩", "未做鉴权与加密", "未做权限分级"]}},
                {"at": 3600, "do": "label", "node": "cmd_link", "text": "生产环境需补：鉴权 / 加密 / 权限分级 / 审计", "tone": "warn"},
                {"at": 4600, "do": "compare",
                 "title": "下行通道的必备护栏",
                 "left": {"title": "缺失护栏的风险", "color": "#ff6b6b", "lines": ["任何人都能下发", "可下发危险参数", "无法追溯是谁发的", "误操作无法回滚"]},
                 "right": {"title": "应具备的能力", "color": "#51cf66", "lines": ["受控指令集（非自由文本）", "参数范围强校验", "指令全量留痕", "回执 + 超时双判定"]}},
                {"at": 7000, "do": "branch", "text": "✅ 能控制，也要能解释「谁在什么时候让它做了什么」", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 06：拆开一台直流快充桩
# ============================================================
# 这是"真正的思索"那一步：装置由**独立零件**拼成，因此能拆、能装、
# 能讲"零件如何组成整体"——抽象示意体做不到这一点。
# stage 用 device 引用，零件自动成为节点，于是 focus/flow/label/move
# 这些既有原语全部通用。
# ------------------------------------------------------------
LESSON_DEVICE_CHARGER: Dict[str, Any] = {
    "id": "device_charger",
    "icon": "🔧",
    "title": "拆开一台直流快充桩",
    "subtitle": "七个零件，一个能量通道",
    "audience": "数字孪生建模 / 设备讲解",
    "summary": "把它拆到只剩零件，再装回去。",
    "scope": "object",
    "stage": {"device": "dc_charger_real", "nodes": [], "edges": []},
    "steps": [
        {
            "id": "dc_whole",
            "title": "① 先看整体：一台快充桩",
            "short": "整体",
            "brief": "真实整机 + 内部模块",
            "narration": "先看整体。这是一台按真实比例建模的直流快充桩：外壳、面板、枪座、线缆都在。真正的价值在里面。",
            "camera": {"eye": [3.5, 4.6, 9.5], "look": [0, 1.6, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "assemble", "ms": 400, "stagger": 0},
                {"at": 200, "do": "focus", "target": [], "dim": 0.42},
                {"at": 700, "do": "label", "node": "shell", "text": "整机：外壳 + 面板 + 枪座", "tone": "info"},
                {"at": 1600, "do": "compare",
                 "title": "为什么要拆开看",
                 "left": {"title": "只看整体", "color": "#8a94a6", "lines": ["知道它是充电桩", "不知道里面有什么", "坏了不知道换哪个", "改功率不知道动哪"]},
                 "right": {"title": "拆开看", "color": "#51cf66", "lines": ["看清每个零件的作用", "知道故障对应哪个件", "知道升级要换哪个件", "为数字孪生建语义"]}},
                {"at": 3800, "do": "branch", "text": "孪生体能讲到零件级，才算真正理解了这台设备", "tone": "info"},
            ],
        },
        {
            "id": "dc_take",
            "title": "② 抽出内部：模块各就各位",
            "short": "拆解",
            "brief": "内部模块逐个抽出",
            "narration": "把内部模块从整机里抽出来。现场维修也是这个顺序：先打开外壳，再逐个更换模块。",
            "camera": {"eye": [7.5, 6.5, 12], "look": [0, 1.6, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.3},
                {"at": 200, "do": "take_apart", "ms": 700, "stagger": 190},
                {"at": 2600, "do": "label", "node": "pm", "text": "功率模块", "tone": "ok"},
                {"at": 3100, "do": "label", "node": "ctrl", "text": "控制单元", "tone": "ok"},
                {"at": 3600, "do": "label", "node": "cool", "text": "散热单元", "tone": "ok"},
                {"at": 4400, "do": "compare",
                 "title": "拆出来的是哪些",
                 "left": {"title": "整机自带的", "color": "#88aadd", "lines": ["外壳与立柱", "操作面板 / 屏幕", "充电枪座与线缆"]},
                 "right": {"title": "抽出来讲的", "color": "#c98b2e", "lines": ["功率模块（核心）", "控制单元（大脑）", "散热单元（保障）", "充电枪（末端）"]}},
                {"at": 6600, "do": "branch", "text": "现场能换的是模块，不是整机 —— 拆解顺序就是这个依据", "tone": "info"},
            ],
        },
        {
            "id": "dc_flow",
            "title": "③ 电流怎么走：从电网到枪",
            "short": "能量流",
            "brief": "交流 → 直流 → 车辆",
            "narration": "拆开之后就能讲清能量通道：交流电进来，功率模块把它变成直流，控制单元指挥，最后从充电枪出去。",
            "camera": {"eye": [6.5, 5.5, 11], "look": [0, 1.6, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": ["pm", "ctrl", "gun"], "dim": 0.22},
                {"at": 300, "do": "flow", "from": "pm", "to": "ctrl", "kind": "logic", "rate": 1.3, "label": "控制指令"},
                {"at": 1000, "do": "flow", "from": "ctrl", "to": "gun", "kind": "power", "rate": 1.5, "label": "直流输出"},
                {"at": 2200, "do": "label", "node": "pm", "text": "AC → DC 变换", "tone": "ok"},
                {"at": 3200, "do": "compare",
                 "title": "交流桩 vs 直流桩",
                 "left": {"title": "交流慢充", "color": "#5cb85c", "lines": ["车上整流", "功率小（7kW 级）", "桩体简单、成本低", "充电数小时"]},
                 "right": {"title": "直流快充", "color": "#4a90d9", "lines": ["桩内整流（功率模块）", "功率大（120kW 级）", "必须主动散热", "充电数十分钟"]}},
                {"at": 5400, "do": "branch", "text": "看懂这条通道，就知道为什么快充桩必须带散热", "tone": "info"},
            ],
        },
        {
            "id": "dc_back",
            "title": "④ 装回去：验证你真的懂了",
            "short": "回装",
            "brief": "内部先就位，外壳最后合上",
            "narration": "把零件装回去。顺序与拆开相反：内部模块先就位，外壳最后合上——装错了设备就合不上。",
            "camera": {"eye": [4.0, 5.0, 10], "look": [0, 1.6, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.32},
                {"at": 300, "do": "assemble", "ms": 650, "stagger": 150},
                {"at": 2600, "do": "label", "node": "shell", "text": "装回整机", "tone": "ok"},
                {"at": 3200, "do": "compare",
                 "title": "装反了会怎样",
                 "left": {"title": "顺序错误", "color": "#ff6b6b", "lines": ["外壳挡住内部模块", "线束无法接入", "散热风道对不上", "柜门合不上"]},
                 "right": {"title": "正确顺序", "color": "#51cf66", "lines": ["内部模块先就位", "接线与风道对齐", "外壳最后合上", "屏幕与枪复位"]}},
                {"at": 5400, "do": "branch", "text": "✅ 能拆开又能装回，说明这台设备的结构已经在你脑子里了", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 07：储能集装箱的内部
# ============================================================
LESSON_DEVICE_BESS: Dict[str, Any] = {
    "id": "device_bess",
    "icon": "📦",
    "title": "储能集装箱的内部",
    "subtitle": "电池簇、变流器、控制与散热",
    "audience": "数字孪生建模 / 储能运维",
    "summary": "一个铁箱子，凭什么能当电网的缓冲。",
    "scope": "object",
    "stage": {"device": "bess_container", "nodes": [], "edges": []},
    "steps": [
        {
            "id": "bess_whole",
            "title": "① 外表只是一个铁箱子",
            "short": "整体",
            "brief": "看不出它做什么",
            "narration": "储能集装箱从外面看就是个铁箱子。真正的价值全在里面。",
            "camera": {"eye": [4.5, 4.4, 10], "look": [0, 1.3, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "assemble", "ms": 400, "stagger": 0},
                {"at": 200, "do": "focus", "target": ["shell"], "dim": 0.35},
                {"at": 700, "do": "label", "node": "shell", "text": "防护 / 消防 / 温控边界", "tone": "info"},
                {"at": 1600, "do": "compare",
                 "title": "同一个箱子，两种理解",
                 "left": {"title": "当它是箱子", "color": "#8a94a6", "lines": ["一个长方体", "占地方", "不知道容量", "出问题只能整柜换"]},
                 "right": {"title": "当它是系统", "color": "#51cf66", "lines": ["容量 = 电池簇组合", "功率 = 变流器能力", "寿命 = 温控水平", "可定位到簇级"]}},
                {"at": 3800, "do": "branch", "text": "数字孪生的第一件事，就是把箱子变成可解释的系统", "tone": "info"},
            ],
        },
        {
            "id": "bess_take",
            "title": "② 拆开：四类部件",
            "short": "拆解",
            "brief": "电池 / 变流 / 控制 / 散热",
            "narration": "拆开箱体，里面是四类部件：电池簇存能量，变流器换形态，控制单元做决策，散热保寿命。",
            "camera": {"eye": [8.0, 6.5, 13], "look": [0, 1.4, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.28},
                {"at": 200, "do": "take_apart", "ms": 700, "stagger": 180},
                {"at": 2400, "do": "label", "node": "batt", "text": "电池簇：容量", "tone": "ok"},
                {"at": 3000, "do": "label", "node": "pcs", "text": "变流器：功率", "tone": "ok"},
                {"at": 3600, "do": "label", "node": "ctrl", "text": "控制：策略", "tone": "ok"},
                {"at": 4200, "do": "label", "node": "cool", "text": "散热：寿命", "tone": "ok"},
                {"at": 5000, "do": "compare",
                 "title": "四个部件各自的 KPI",
                 "left": {"title": "部件", "color": "#88aadd", "lines": ["电池簇", "变流器", "控制单元", "散热单元"]},
                 "right": {"title": "它决定什么", "color": "#51cf66", "lines": ["能存多少（MWh）", "能放多快（MW）", "什么时候充放", "能用多少年"]}},
                {"at": 7200, "do": "branch", "text": "容量、功率、策略、寿命 —— 四个正交的指标", "tone": "info"},
            ],
        },
        {
            "id": "bess_flow",
            "title": "③ 充放电：两个方向",
            "short": "充放电",
            "brief": "同一套硬件，正反两用",
            "narration": "储能最容易被误解的一点：充电和放电走的是同一套硬件，只是方向相反。",
            "camera": {"eye": [7.0, 5.5, 12], "look": [0, 1.4, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": ["batt", "pcs", "ctrl"], "dim": 0.22},
                {"at": 300, "do": "flow", "from": "pcs", "to": "batt", "kind": "power", "rate": 1.4, "label": "充电：AC→DC"},
                {"at": 1600, "do": "flow", "from": "batt", "to": "pcs", "kind": "power", "rate": 1.4, "label": "放电：DC→AC", "reverse": True},
                {"at": 3000, "do": "label", "node": "batt", "text": "SOC 在变，硬件没变", "tone": "ok"},
                {"at": 3800, "do": "compare",
                 "title": "两个方向各自的价值",
                 "left": {"title": "充电（低谷）", "color": "#4a90d9", "lines": ["电价低时存电", "消纳光伏弃电", "平衡电网峰谷", "电池成本最低时买入"]},
                 "right": {"title": "放电（高峰）", "color": "#c98b2e", "lines": ["电价高时卖电", "支撑快充瞬时功率", "参与需求响应", "收益最高时卖出"]}},
                {"at": 6000, "do": "branch", "text": "套利只是表象，真正的价值是「把不可控变成可控」", "tone": "info"},
            ],
        },
        {
            "id": "bess_safety",
            "title": "④ 装回去：为什么安全是第一约束",
            "short": "回装",
            "brief": "热失控是底线问题",
            "narration": "把部件装回去。储能和别的设备不同：它的第一约束不是效率，而是安全。",
            "camera": {"eye": [4.5, 5.2, 11], "look": [0, 1.4, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.3},
                {"at": 300, "do": "assemble", "ms": 650, "stagger": 150},
                {"at": 2800, "do": "focus", "target": ["cool", "ctrl"], "dim": 0.22},
                {"at": 3200, "do": "label", "node": "cool", "text": "温控是安全的第一道防线", "tone": "warn"},
                {"at": 4200, "do": "compare",
                 "title": "安全相关的两道防线",
                 "left": {"title": "热管理", "color": "#4a90d9", "lines": ["液冷 / 风冷", "电芯温差控制", "温度直接决定寿命", "失控前兆是温升"]},
                 "right": {"title": "控制与保护", "color": "#51cf66", "lines": ["SOC / SOH 估计", "过充过放保护", "簇级投切隔离", "联动消防"]}},
                {"at": 6400, "do": "branch", "text": "✅ 装回去之后再看：孪生体最先要监视的就是温度与 SOC", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 08：光储充微电网怎么连起来
# ============================================================
LESSON_DEVICE_MICROGRID: Dict[str, Any] = {
    "id": "device_microgrid",
    "icon": "🔗",
    "title": "光储充微电网怎么连起来",
    "subtitle": "三台设备，一条能量母线",
    "audience": "数字孪生建模 / 系统集成",
    "summary": "单机都会用，连起来才是微网。",
    "scope": "object",
    "stage": {"device": "micro_grid", "nodes": [], "edges": []},
    "steps": [
        {
            "id": "mg_parts",
            "title": "① 三台设备各自独立",
            "short": "三台设备",
            "brief": "发电 / 缓冲 / 用电",
            "narration": "光伏、储能、充电侧，三台设备摆在一起。它们各干各的，还没有关系。",
            "camera": {"eye": [2.5, 7.5, 17], "look": [1, 1.2, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "assemble", "ms": 400, "stagger": 0},
                {"at": 200, "do": "focus", "target": [], "dim": 0.4},
                {"at": 800, "do": "label", "node": "pv", "text": "发电侧", "tone": "ok"},
                {"at": 1300, "do": "label", "node": "bess", "text": "缓冲", "tone": "ok"},
                {"at": 1800, "do": "label", "node": "pile", "text": "用电侧", "tone": "ok"},
                {"at": 2600, "do": "compare",
                 "title": "各自为政的问题",
                 "left": {"title": "三台独立设备", "color": "#ff6b6b", "lines": ["光伏看天吃饭", "储能不知道该存不该存", "快充瞬时功率冲击电网", "没有协同就没有价值"]},
                 "right": {"title": "组成微网", "color": "#51cf66", "lines": ["光伏优先直供", "多余进储能", "快充由储能兜底", "整体可孤岛运行"]}},
                {"at": 4800, "do": "branch", "text": "微网的价值不在设备本身，而在它们之间的连接", "tone": "info"},
            ],
        },
        {
            "id": "mg_bus",
            "title": "② 连上母线",
            "short": "连接",
            "brief": "一条母线串起来",
            "narration": "用一条直流母线把三者连起来。连接一旦建立，能量就有了统一的调度口径。",
            "camera": {"eye": [2.0, 6.5, 15], "look": [1, 1.2, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.26},
                {"at": 300, "do": "flow", "from": "pv", "to": "bess", "kind": "power", "rate": 1.4, "label": "直流母线"},
                {"at": 1200, "do": "flow", "from": "bess", "to": "pile", "kind": "power", "rate": 1.4, "label": "削峰供电"},
                {"at": 2100, "do": "flow", "from": "pile", "to": "car", "kind": "power", "rate": 1.4, "label": "充电"},
                {"at": 3200, "do": "compare",
                 "title": "为什么用直流母线",
                 "left": {"title": "交流耦合", "color": "#8a94a6", "lines": ["各自变流后并网", "转换环节多", "效率损失叠加", "协同控制复杂"]},
                 "right": {"title": "直流母线", "color": "#51cf66", "lines": ["光伏直流直接接入", "储能天然是直流", "转换环节最少", "调度口径统一"]}},
                {"at": 5400, "do": "branch", "text": "架构选择决定了效率上限，这是设计阶段就要定的", "tone": "info"},
            ],
        },
        {
            "id": "mg_dispatch",
            "title": "③ 调度：谁先谁后",
            "short": "调度",
            "brief": "光伏优先，储能兜底",
            "narration": "微网真正难的不是连起来，而是决定每一度电从哪来：光伏优先、储能兜底、电网最后。",
            "camera": {"eye": [3.0, 6.0, 14], "look": [1, 1.2, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": ["pv", "bess", "pile"], "dim": 0.22},
                {"at": 300, "do": "tween", "key": "util", "from": 0.15, "to": 0.9, "ms": 2200, "nodes": ["pile"]},
                {"at": 2800, "do": "flow", "from": "pv", "to": "pile", "kind": "power", "rate": 1.6, "label": "光伏直供"},
                {"at": 3800, "do": "flow", "from": "bess", "to": "pile", "kind": "power", "rate": 1.3, "label": "不足部分由储能补"},
                {"at": 5000, "do": "compare",
                 "title": "调度优先级",
                 "left": {"title": "顺序", "color": "#88aadd", "lines": ["① 光伏直供", "② 储能放电", "③ 电网补足", "④ 反向给储能充电"]},
                 "right": {"title": "为什么这个顺序", "color": "#51cf66", "lines": ["光伏边际成本≈0", "储能避免峰段高价", "电网是兜底而非首选", "低谷时反向补能"]}},
                {"at": 7200, "do": "branch", "text": "调度策略才是微网的大脑，孪生体要能验证它", "tone": "info"},
            ],
        },
        {
            "id": "mg_recap",
            "title": "④ 拆开再装回：一个能自洽的系统",
            "short": "收束",
            "brief": "设备 + 连接 + 策略",
            "narration": "把设备拆开再装回去，你会发现微网 = 设备 + 连接 + 策略，三者缺一不可。",
            "camera": {"eye": [2.5, 8.0, 18], "look": [1, 1.2, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.32},
                {"at": 200, "do": "take_apart", "ms": 600, "stagger": 150},
                {"at": 2200, "do": "assemble", "ms": 600, "stagger": 130},
                {"at": 4200, "do": "flow", "from": "pv", "to": "bess", "kind": "power", "rate": 1.4, "label": "发"},
                {"at": 4600, "do": "flow", "from": "bess", "to": "pile", "kind": "power", "rate": 1.4, "label": "储"},
                {"at": 5000, "do": "flow", "from": "pile", "to": "car", "kind": "power", "rate": 1.4, "label": "用"},
                {"at": 6000, "do": "compare",
                 "title": "微网的三层",
                 "left": {"title": "看得见", "color": "#88aadd", "lines": ["光伏板", "储能集装箱", "充电桩与车辆"]},
                 "right": {"title": "决定成败", "color": "#51cf66", "lines": ["母线拓扑", "调度优先级", "故障时的孤岛策略", "设备间的数据契约"]}},
                {"at": 8200, "do": "branch", "text": "✅ 拆开能讲清每一台，装回能讲清它们怎么协同 —— 这才是系统级理解", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 09：数字怎么变成颜色
# ============================================================
# 这是**数据可视化赛道**的靶心课件：讲的不是"数据长什么样"，
# 而是"数据凭什么被这样画出来"。前面八门课讲的是工具怎么用，
# 这一门讲的是可视化本身的判断依据。
# scope=tool：它是方法论课，不绑定某个具体物体。
# ------------------------------------------------------------
LESSON_VISUALIZATION: Dict[str, Any] = {
    "id": "visualization",
    "icon": "🎨",
    "title": "数字怎么变成颜色",
    "subtitle": "可视化的五个判断",
    "audience": "数据可视化 / 分析人员",
    "summary": "同样的数字，为什么这样画就不一样。",
    "scope": "tool",
    "stage": {
        "nodes": [
            {"id": "raw",    "type": "chip",     "pos": [-7.0, 0, 0], "label": "原始值"},
            {"id": "norm",   "type": "battery",  "pos": [-2.4, 0, 0], "label": "归一化"},
            {"id": "channel","type": "grid",     "pos": [7.0, 0, 0],  "label": "编码通道"},
            {"id": "time_s", "type": "screen",   "pos": [-6.0, 0, -5.0], "label": "4344 小时"},
            {"id": "time_d", "type": "screen",   "pos": [0.5, 0, -5.0],  "label": "200 步"},
            {"id": "c_a",    "type": "charger",  "pos": [-6.0, 0, 5.0],  "label": "站点 A"},
            {"id": "c_b",    "type": "charger",  "pos": [-0.5, 0, 5.0],  "label": "站点 B"},
            {"id": "c_c",    "type": "charger",  "pos": [4.5, 0, 5.0],   "label": "站点 C"},
        ],
        "edges": [
            {"from": "raw",   "to": "norm",  "kind": "logic", "label": "归一化"},
            {"from": "norm",  "to": "channel","kind": "render","label": "编码"},
            {"from": "time_s","to": "time_d","kind": "data",  "label": "降采样"},
        ],
    },
    "steps": [
        {
            "id": "vis_normalize",
            "title": "① 为什么必须先归一化",
            "short": "归一化",
            "brief": "不可比的数字画不出图",
            "narration": "功率、插槽数、电压，单位各不相同。不先归一到同一量纲，它们之间根本无法比较，也就画不到一张图上。",
            "camera": {"eye": [-3, 6.5, 13], "look": [-4, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["raw", "norm"], "hide": ["channel", "time_s", "time_d", "c_a", "c_b", "c_c"]},
                {"at": 150, "do": "focus", "target": ["raw"], "dim": 0.25},
                {"at": 500, "do": "label", "node": "raw", "text": "96.5 kW · 2 个 · 750.2 V", "tone": "warn"},
                {"at": 1200, "do": "flow", "from": "raw", "to": "norm", "kind": "logic", "rate": 1.3, "label": "归一化"},
                {"at": 2000, "do": "focus", "target": ["norm"], "dim": 0.25},
                {"at": 2400, "do": "tween", "key": "util", "from": 0.0, "to": 0.83, "ms": 1500, "nodes": ["norm"]},
                {"at": 4200, "do": "label", "node": "norm", "text": "utilization = 0.83", "tone": "ok"},
                {"at": 4800, "do": "compare",
                 "title": "归一化解决了什么",
                 "left": {"title": "原始量纲", "color": "#ff6b6b", "lines": ["96.5 kW", "2 个插槽", "750.2 V", "彼此不可比"]},
                 "right": {"title": "归一化后", "color": "#51cf66", "lines": ["0.83", "0.20", "0.91", "同一把尺子"]}},
                {"at": 7000, "do": "branch", "text": "归一化不是美化步骤，它是可视化成不成立的前提", "tone": "info"},
            ],
        },
        {
            "id": "vis_channel",
            "title": "② 同一个数，用哪个通道表达",
            "short": "选通道",
            "brief": "颜色 / 高度 / 大小",
            "narration": "同一个 0.83，可以用颜色深浅表示，也可以用高度或大小。选哪个通道，取决于你想让读者比较什么。",
            "camera": {"eye": [7, 6.5, 13], "look": [7, 1.2, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["channel"], "hide": ["raw", "norm", "time_s", "time_d", "c_a", "c_b", "c_c"]},
                {"at": 150, "do": "focus", "target": ["channel"], "dim": 0.2},
                {"at": 600, "do": "tween", "key": "util", "from": 0.1, "to": 0.95, "ms": 2200, "nodes": ["channel"]},
                {"at": 3200, "do": "label", "node": "channel", "text": "颜色深浅 = 数值大小", "tone": "ok"},
                {"at": 4000, "do": "compare",
                 "title": "通道选择的取舍",
                 "left": {"title": "颜色编码", "color": "#4aa3ff",
                          "lines": ["适合：状态分类", "弱项：精确读数难", "弱项：色盲不友好", "弱项：类别多了会糊"]},
                 "right": {"title": "位置 / 长度编码", "color": "#51cf66",
                           "lines": ["适合：精确比较", "适合：排序与趋势", "弱项：占空间大", "弱项：类别多了挤"]}},
                {"at": 6200, "do": "branch", "text": "颜色用于「分类」，长度用于「比较」—— 这是最稳的分工", "tone": "info"},
            ],
        },
        {
            "id": "vis_color",
            "title": "③ 颜色的语义：不是装饰",
            "short": "颜色语义",
            "brief": "阈值 + 一致的映射",
            "narration": "颜色一旦承载语义，就必须三档分明、跨设备一致。绿黄红不是配色方案，是运维共识。",
            "camera": {"eye": [0, 6.8, 13], "look": [-1, 1.0, 3.5], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["c_a", "c_b", "c_c"], "hide": ["raw", "norm", "channel", "time_s", "time_d"]},
                {"at": 150, "do": "focus", "target": ["c_a", "c_b", "c_c"], "dim": 0.22},
                {"at": 500, "do": "tween", "key": "util", "from": 0.0, "to": 0.45, "ms": 1400, "nodes": ["c_a"]},
                {"at": 2100, "do": "tween", "key": "util", "from": 0.0, "to": 0.72, "ms": 1400, "nodes": ["c_b"]},
                {"at": 3700, "do": "tween", "key": "util", "from": 0.0, "to": 0.93, "ms": 1400, "nodes": ["c_c"]},
                {"at": 5400, "do": "label", "node": "c_c", "text": "util > 0.7 → 告警", "tone": "warn"},
                {"at": 6000, "do": "compare",
                 "title": "颜色映射必须一致",
                 "left": {"title": "常见错误", "color": "#ff6b6b", "lines": ["同一数值两个颜色", "阈值写死在前端", "不同设备配色不同", "红绿对比色盲难辨"]},
                 "right": {"title": "本工具的做法", "color": "#51cf66", "lines": ["阈值单一真值（0.7）", "三档语义固定", "颜色与告警同源", "可叠加图标冗余编码"]}},
                {"at": 8200, "do": "branch", "text": "颜色要能被「读懂」，不只是被「看到」", "tone": "info"},
            ],
        },
        {
            "id": "vis_space",
            "title": "④ 空间关系决定理解速度",
            "short": "空间关系",
            "brief": "位置本身就带信息",
            "narration": "位置不是随便摆的。站点之间的相对位置、层级的高低，本身就在传递结构信息。",
            "camera": {"eye": [0, 7.5, 14], "look": [-1, 1.2, 3.0], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["c_a", "c_b", "c_c"], "hide": ["raw", "norm", "channel", "time_s", "time_d"]},
                {"at": 200, "do": "focus", "target": ["c_a", "c_b", "c_c"], "dim": 0.25},
                {"at": 600, "do": "flow", "from": "c_a", "to": "c_b", "kind": "logic", "rate": 1.1, "label": "相邻"},
                {"at": 1300, "do": "flow", "from": "c_b", "to": "c_c", "kind": "logic", "rate": 1.1, "label": "同片区"},
                {"at": 2400, "do": "compare",
                 "title": "位置编码的两种做法",
                 "left": {"title": "随意排布", "color": "#ff6b6b", "lines": ["看不出片区关系", "无法判断影响范围", "读者要自己找规律", "每次刷新位置都变"]},
                 "right": {"title": "按语义排布", "color": "#51cf66", "lines": ["相邻即物理相邻", "同色即同片区", "影响范围一眼可见", "位置稳定可比对"]}},
                {"at": 4600, "do": "branch", "text": "空间位置的稳定性，是跨时间对比的前提", "tone": "info"},
            ],
        },
        {
            "id": "vis_time",
            "title": "⑤ 时间维度：降采样不是偷懒",
            "short": "时间降采样",
            "brief": "4344 小时 → 200 步",
            "narration": "历史数据有四千多小时。全画出来既看不清也跑不动，关键是降采样要保住趋势而不是保住每一个点。",
            "camera": {"eye": [-2, 6.5, 11], "look": [-3, 1.0, -2.5], "ms": 900},
            "actions": [
                {"at": 0, "do": "stage", "show": ["time_s", "time_d"], "hide": ["raw", "norm", "channel", "c_a", "c_b", "c_c"]},
                {"at": 150, "do": "focus", "target": ["time_s"], "dim": 0.25},
                {"at": 500, "do": "label", "node": "time_s", "text": "4344 小时 × 1423 站点", "tone": "warn"},
                {"at": 1200, "do": "flow", "from": "time_s", "to": "time_d", "kind": "data", "rate": 1.2, "label": "降采样"},
                {"at": 2200, "do": "focus", "target": ["time_d"], "dim": 0.25},
                {"at": 2600, "do": "label", "node": "time_d", "text": "200 步 · 保住趋势", "tone": "ok"},
                {"at": 3600, "do": "compare",
                 "title": "降采样要保住什么",
                 "left": {"title": "错误做法", "color": "#ff6b6b", "lines": ["随便抽 200 个点", "把峰值抽没了", "丢掉早晚高峰", "轴标签与数据错位"]},
                 "right": {"title": "正确做法", "color": "#51cf66", "lines": ["按时间等距聚合", "保留峰值特征", "回放时轴标签同步", "可回看原始点"]}},
                {"at": 5800, "do": "branch", "text": "时间轴的目标是「看见趋势」，不是「复刻数据」", "tone": "info"},
            ],
        },
        {
            "id": "vis_recap",
            "title": "⑥ 可视化 = 五个判断的叠加",
            "short": "五个判断",
            "brief": "归一化/通道/语义/空间/时间",
            "narration": "把五个判断叠起来，才是一次完整的可视化决策。它决定了读者能不能在两秒内看懂你想让他看懂的东西。",
            "camera": {"eye": [0, 8.5, 18], "look": [-1, 1.2, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "stage",
                 "show": ["raw", "norm", "channel", "c_a", "c_b", "c_c", "time_s", "time_d"], "hide": []},
                {"at": 300, "do": "flow", "from": "raw", "to": "norm", "kind": "logic", "rate": 1.3, "label": "① 归一化"},
                {"at": 700, "do": "flow", "from": "norm", "to": "channel", "kind": "render", "rate": 1.3, "label": "② 选通道"},
                {"at": 1100, "do": "tween", "key": "util", "from": 0.15, "to": 0.9, "ms": 1800,
                 "nodes": ["norm", "channel", "c_a", "c_b", "c_c"]},
                {"at": 3200, "do": "flow", "from": "time_s", "to": "time_d", "kind": "data", "rate": 1.2, "label": "⑤ 时间聚合"},
                {"at": 4200, "do": "compare",
                 "title": "看得见的 vs 决定成败的",
                 "left": {"title": "读者看到的", "color": "#88aadd", "lines": ["颜色深浅", "柱子高低", "位置远近", "时间轴进度"]},
                 "right": {"title": "背后必须先定的", "color": "#51cf66", "lines": ["归一化口径", "编码通道选择", "阈值与色彩语义", "空间布局规则", "时间聚合方式"]}},
                {"at": 6400, "do": "branch", "text": "✅ 可视化的水平，体现在「看不见的那五个判断」上", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 10：光伏阵列：板子只是开始
# ============================================================
LESSON_DEVICE_PV: Dict[str, Any] = {
    "id": "device_pv",
    "icon": "☀️",
    "title": "光伏阵列：板子只是开始",
    "subtitle": "汇流 → 逆变 → 配电 → 并网",
    "audience": "数字孪生建模 / 新能源系统",
    "summary": "板子发的是直流电，而你用的是交流电。",
    "scope": "object",
    "stage": {"device": "pv_array", "nodes": [], "edges": []},
    "steps": [
        {
            "id": "pv_whole",
            "title": "① 一整排板子，看不出电怎么走",
            "short": "整体",
            "brief": "阵列只是入口",
            "narration": "光伏阵列本身只是一排板子。它把光变成直流电，但电从哪出去、怎么变成能用的电，从外观上完全看不出来。",
            "camera": {"eye": [2.0, 5.0, 11], "look": [2.0, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "assemble", "ms": 400, "stagger": 0},
                {"at": 200, "do": "focus", "target": ["panel"], "dim": 0.30},
                {"at": 600, "do": "label", "node": "panel", "text": "输出：直流电", "tone": "info"},
                {"at": 1500, "do": "compare",
                 "title": "只看到阵列会漏掉什么",
                 "left": {"title": "只看板子", "color": "#8a94a6", "lines": ["知道它能发电", "不知道发的是什么电", "不知道能接多少负载", "不知道并网要什么条件"]},
                 "right": {"title": "看到整条链", "color": "#51cf66", "lines": ["直流 → 交流的转换点", "每级的容量瓶颈", "并网的保护与计量", "故障定位到级"]}},
                {"at": 3700, "do": "branch", "text": "光伏系统的关键不在板子，在板子之后", "tone": "info"},
            ],
        },
        {
            "id": "pv_take",
            "title": "② 抽出来：一条直流到交流的链",
            "short": "拆解",
            "brief": "四台设备依次排列",
            "narration": "把链路抽出来：汇流箱、逆变器、交流配电柜、计量并网。它们各自解决一个具体问题。",
            "camera": {"eye": [3.2, 6.2, 13], "look": [3.2, 1.0, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.26},
                {"at": 200, "do": "take_apart", "ms": 700, "stagger": 170},
                {"at": 2200, "do": "label", "node": "combine", "text": "汇流箱", "tone": "ok"},
                {"at": 2700, "do": "label", "node": "inv", "text": "逆变器", "tone": "ok"},
                {"at": 3200, "do": "label", "node": "ac", "text": "交流配电柜", "tone": "ok"},
                {"at": 3700, "do": "label", "node": "meter", "text": "计量并网", "tone": "ok"},
                {"at": 4400, "do": "compare",
                 "title": "每一级解决一个问题",
                 "left": {"title": "设备", "color": "#88aadd", "lines": ["汇流箱", "逆变器", "交流配电柜", "计量与并网"]},
                 "right": {"title": "它解决什么", "color": "#51cf66", "lines": ["多路直流合成一路", "直流变交流（核心）", "分配、保护、隔离", "合规计量与防逆流"]}},
                {"at": 6600, "do": "branch", "text": "逆变器是整条链的技术核心，也是效率损失最大的环节", "tone": "info"},
            ],
        },
        {
            "id": "pv_flow",
            "title": "③ 能量怎么走：逐级变换",
            "short": "能量流",
            "brief": "直流 → 交流 → 并网",
            "narration": "沿着链路走一遍：光在板子上变成直流，汇流后进逆变器变成交流，再经配电柜送出、计量后并网。",
            "camera": {"eye": [3.2, 5.6, 12], "look": [3.2, 1.0, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": ["combine", "inv", "ac", "meter"], "dim": 0.22},
                {"at": 300, "do": "flow", "from": "panel",   "to": "combine", "kind": "power",  "rate": 1.5, "label": "直流汇流"},
                {"at": 1100, "do": "flow", "from": "combine", "to": "inv",    "kind": "power",  "rate": 1.4, "label": "直流"},
                {"at": 1900, "do": "flow", "from": "inv",     "to": "ac",     "kind": "power",  "rate": 1.4, "label": "交流"},
                {"at": 2700, "do": "flow", "from": "ac",      "to": "meter",  "kind": "render", "rate": 1.3, "label": "并网"},
                {"at": 3800, "do": "compare",
                 "title": "两种典型方案",
                 "left": {"title": "集中式", "color": "#4aa3ff", "lines": ["一台大逆变器", "成本低、便于管理", "单点故障影响大", "适合大型地面电站"]},
                 "right": {"title": "组串式", "color": "#51cf66", "lines": ["多台小逆变器", "单机故障影响小", "MPPT 更细、发电量高", "适合分布式与车棚"]}},
                {"at": 6000, "do": "branch", "text": "选集中式还是组串式，本质是在「成本」与「可用率」之间取舍", "tone": "info"},
            ],
        },
        {
            "id": "pv_recap",
            "title": "④ 装回去：孪生体该盯哪几个点",
            "short": "收束",
            "brief": "容量 / 效率 / 并网",
            "narration": "装回整体。对孪生体来说，这条链上有三个必须监视的点：每级容量、逆变效率、并网状态。",
            "camera": {"eye": [2.4, 6.4, 13], "look": [2.4, 1.0, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.30},
                {"at": 300, "do": "assemble", "ms": 650, "stagger": 150},
                {"at": 2600, "do": "flow", "from": "combine", "to": "inv", "kind": "power", "rate": 1.4, "label": "持续发电"},
                {"at": 3600, "do": "label", "node": "inv", "text": "效率与温度是首要监视项", "tone": "warn"},
                {"at": 4400, "do": "compare",
                 "title": "孪生体要盯的三个点",
                 "left": {"title": "看不出来的风险", "color": "#ff6b6b", "lines": ["某一串组件失效", "逆变器降额运行", "并网点防逆流动作", "积灰导致出力下降"]},
                 "right": {"title": "对应的监视量", "color": "#51cf66", "lines": ["组串电流离散度", "逆变器效率 / 机温", "并网功率方向", "辐照与出力比（PR）"]}},
                {"at": 6600, "do": "branch", "text": "✅ 光伏孪生体的价值：把「发电量下降」拆解到具体是哪一串出了问题", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 课件 11：风机：机械能怎么变成电
# ============================================================
LESSON_DEVICE_WIND: Dict[str, Any] = {
    "id": "device_wind",
    "icon": "🌬️",
    "title": "风机：机械能怎么变成电",
    "subtitle": "叶片 → 齿轮箱 → 发电机 → 变流",
    "audience": "数字孪生建模 / 新能源系统",
    "summary": "叶尖转得很慢，发电机却要转得很快。",
    "scope": "object",
    "stage": {"device": "wind_turbine", "nodes": [], "edges": []},
    "steps": [
        {
            "id": "wind_whole",
            "title": "① 看到的只有叶片在转",
            "short": "整体",
            "brief": "机械到电的转换看不见",
            "narration": "从外面看，风机就是三支叶片在慢慢转。但发电所需的高转速、以及后续的电能变换，全都在机舱和塔底里。",
            "camera": {"eye": [2.0, 5.6, 12], "look": [2.0, 1.4, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "assemble", "ms": 400, "stagger": 0},
                {"at": 200, "do": "focus", "target": ["blades"], "dim": 0.30},
                {"at": 600, "do": "label", "node": "blades", "text": "叶尖速度慢，转矩大", "tone": "info"},
                {"at": 1600, "do": "compare",
                 "title": "看得见 vs 看不见",
                 "left": {"title": "看得见的", "color": "#88aadd", "lines": ["叶片转速", "偏航朝向", "塔筒高度", "机组外观"]},
                 "right": {"title": "决定发电的", "color": "#c98b2e", "lines": ["齿轮箱速比", "发电机转速与扭矩", "变流器频率适配", "变桨与偏航策略"]}},
                {"at": 3800, "do": "branch", "text": "风机的核心矛盾：叶尖转速低，发电机要转速高", "tone": "info"},
            ],
        },
        {
            "id": "wind_take",
            "title": "② 抽出来：一条传动链",
            "short": "拆解",
            "brief": "机械侧 → 电气侧",
            "narration": "把传动链抽出来看：叶片、齿轮箱、发电机属于机械侧；变流柜、主控属于电气侧。分界线就是发电机。",
            "camera": {"eye": [3.4, 6.4, 14], "look": [3.4, 1.4, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.26},
                {"at": 200, "do": "take_apart", "ms": 700, "stagger": 170},
                {"at": 2200, "do": "label", "node": "gear", "text": "齿轮箱（机械侧）", "tone": "ok"},
                {"at": 2700, "do": "label", "node": "gen",  "text": "发电机（分界点）", "tone": "warn"},
                {"at": 3200, "do": "label", "node": "conv", "text": "变流柜（电气侧）", "tone": "ok"},
                {"at": 3700, "do": "label", "node": "ctrl", "text": "主控：偏航 / 变桨", "tone": "ok"},
                {"at": 4500, "do": "compare",
                 "title": "发电机两侧的区别",
                 "left": {"title": "机械侧", "color": "#7a7a86", "lines": ["转速低、转矩大", "靠齿轮箱提速", "磨损件：轴承 / 齿轮", "维护成本高"]},
                 "right": {"title": "电气侧", "color": "#c98b2e", "lines": ["频率需与电网匹配", "靠变流器适配", "电子件为主", "可远程诊断"]}},
                {"at": 6700, "do": "branch", "text": "维护重点在机械侧，诊断重点在电气侧 —— 两侧的运维逻辑完全不同", "tone": "info"},
            ],
        },
        {
            "id": "wind_flow",
            "title": "③ 能量怎么走：两次变换",
            "short": "能量流",
            "brief": "风能 → 机械能 → 电能",
            "narration": "沿链路走一遍：风推动叶片产生转矩，齿轮箱提速，发电机把机械能变成电能，变流柜适配后并网。",
            "camera": {"eye": [3.4, 5.8, 13], "look": [3.4, 1.4, 0], "ms": 900},
            "actions": [
                {"at": 0, "do": "focus", "target": ["gear", "gen", "conv"], "dim": 0.22},
                {"at": 300, "do": "flow", "from": "blades", "to": "gear", "kind": "logic", "rate": 1.3, "label": "机械转矩"},
                {"at": 1100, "do": "flow", "from": "gear", "to": "gen",   "kind": "logic", "rate": 1.4, "label": "提速后"},
                {"at": 1900, "do": "flow", "from": "gen",   "to": "conv",  "kind": "power", "rate": 1.4, "label": "电能"},
                {"at": 2700, "do": "flow", "from": "conv",  "to": "ctrl",  "kind": "render","rate": 1.2, "label": "并网"},
                {"at": 3800, "do": "compare",
                 "title": "两种主流机型",
                 "left": {"title": "双馈式", "color": "#4aa3ff", "lines": ["齿轮箱 + 部分功率变流", "变流器容量小（约 30%）", "成本低", "低电压穿越较弱"]},
                 "right": {"title": "直驱式", "color": "#51cf66", "lines": ["无齿轮箱", "全功率变流", "可靠性高、维护少", "体积大、成本高"]}},
                {"at": 6000, "do": "branch", "text": "有没有齿轮箱，决定了两类风机完全不同的运维画像", "tone": "info"},
            ],
        },
        {
            "id": "wind_recap",
            "title": "④ 装回去：孪生体要盯什么",
            "short": "收束",
            "brief": "振动 / 温度 / 功率曲线",
            "narration": "装回整体。风机孪生体最有价值的不是实时转速，而是能提前发现机械侧的劣化趋势。",
            "camera": {"eye": [2.2, 6.6, 13], "look": [2.2, 1.4, 0], "ms": 1000},
            "actions": [
                {"at": 0, "do": "focus", "target": [], "dim": 0.30},
                {"at": 300, "do": "assemble", "ms": 650, "stagger": 150},
                {"at": 2600, "do": "focus", "target": ["gear", "gen"], "dim": 0.22},
                {"at": 3200, "do": "label", "node": "gear", "text": "振动趋势 = 劣化前兆", "tone": "warn"},
                {"at": 4400, "do": "compare",
                 "title": "孪生体要盯的四个量",
                 "left": {"title": "机械侧", "color": "#7a7a86", "lines": ["齿轮箱振动频谱", "轴承温度", "润滑状态", "叶片不平衡"]},
                 "right": {"title": "电气侧与整体", "color": "#c98b2e", "lines": ["发电机绕组温度", "功率曲线偏离", "变桨响应", "可用率与发电量"]}},
                {"at": 6600, "do": "branch", "text": "✅ 风机的价值在「提前」：等它停机再修，损失的是整段发电量", "tone": "ok"},
            ],
        },
    ],
}


# ============================================================
# 注册表
# ============================================================
# 后续可继续追加：性能剖析 / 数据质量 / 多场景协同 等
LESSONS: Dict[str, Dict[str, Any]] = {
    LESSON_TWIN_PIPELINE["id"]: LESSON_TWIN_PIPELINE,
    LESSON_HIERARCHY["id"]: LESSON_HIERARCHY,
    LESSON_DATA_FLOW["id"]: LESSON_DATA_FLOW,
    LESSON_DEBUGGING["id"]: LESSON_DEBUGGING,
    LESSON_REVERSE_CONTROL["id"]: LESSON_REVERSE_CONTROL,
    # 智能装置课（拆解 / 组装）：走 DEVICES，零件即节点
    LESSON_DEVICE_CHARGER["id"]: LESSON_DEVICE_CHARGER,
    LESSON_DEVICE_BESS["id"]: LESSON_DEVICE_BESS,
    LESSON_DEVICE_MICROGRID["id"]: LESSON_DEVICE_MICROGRID,
    # 可视化方法论课（数据可视化赛道靶心）
    LESSON_VISUALIZATION["id"]: LESSON_VISUALIZATION,
    # 更多智能装置课（真实模型 + 抽出内部链路）
    LESSON_DEVICE_PV["id"]: LESSON_DEVICE_PV,
    LESSON_DEVICE_WIND["id"]: LESSON_DEVICE_WIND,
}


# 组件类型 → 课件 ID
# ------------------------------------------------------------
# 🔥 设计原则（与 Create 的 Ponder 对齐）：**气泡只放真正跟这个物体强相关的课**。
#    工具级课程（scope=tool）一律不进气泡 —— 否则"一个充电桩的属性面板里
#    为什么会出现四层架构"这种语义错位会原样搬到 3D 气泡里，气泡也就不轻了。
TYPE_LESSON_MAP: Dict[str, List[str]] = {
    # 充电桩：① 先拆开看它由什么组成 ② 数据从哪来 ③ 怎么被控制
    "charger_fast":  ["device_charger", "data_flow", "reverse_control"],
    "charger_slow":  ["device_charger", "data_flow", "reverse_control"],
    "charger_super": ["device_charger", "data_flow", "reverse_control"],
    # 传感/采集类：它的存在意义就是产生数据
    "sensor": ["data_flow"],
    # 储能集装箱：拆开讲内部
    "container_a": ["device_bess"],
    "container_b": ["device_bess"],
    "container_c": ["device_bess"],
    # 绿色能源：系统级（光储充怎么连起来）+ 各自的内部链路
    "solar_panel_flat":       ["device_pv", "device_microgrid"],
    "solar_panel_land":       ["device_pv", "device_microgrid"],
    "solar_panel_group":      ["device_pv", "device_microgrid"],
    "solar_panel_port":       ["device_pv", "device_microgrid"],
    "solar_panel_port_group": ["device_pv", "device_microgrid"],
    "windmill":     ["device_wind", "device_microgrid"],
    "windmill_low": ["device_wind", "device_microgrid"],
    # 车辆：它在微网里是"负荷"，也只有系统视角讲得通
    "car":   ["device_microgrid"],
    "truck": ["device_microgrid"],
}

# 气泡里最多列几门课：超过就不再往下堆，宁可少而准
BUBBLE_MAX = 3

# scope 取值说明
#   tool   工具级：整个工具的心智模型，只在右侧面板目录出现
#   object 对象级：解释"眼前这个物体"，会出现在对象气泡里
VALID_SCOPES = ("tool", "object")


def get_lesson(lesson_id: str) -> Optional[Dict[str, Any]]:
    return LESSONS.get(lesson_id)


def get_lesson_list() -> List[Dict[str, str]]:
    """返回课件目录（供右侧面板展示），标注 scope 以便前端分组。"""
    return [
        {
            "id": lid,
            "icon": les.get("icon", "💭"),
            "title": les.get("title", lid),
            "subtitle": les.get("subtitle", ""),
            "summary": les.get("summary", ""),
            "steps": str(len(les.get("steps", []))),
            "scope": les.get("scope", "tool"),
        }
        for lid, les in LESSONS.items()
    ]


def get_lessons_for_type(obj_type: str) -> List[str]:
    """
    给定组件类型，返回**气泡里**该展示的课件 ID。

    🔥 关键：这里返回的是空列表就是空列表 —— 不再"兜底到全部课件"。
       兜底看着友好，实际会把工具级课程硬塞进气泡，语义错位、气泡变重，
       正好抵消掉「气泡只放强相关」的设计目的。宁可气泡不出现。
    """
    hits = [lid for lid in TYPE_LESSON_MAP.get(obj_type, []) if lid in LESSONS]
    # 只保留对象级课程（双保险：即使有人往 map 里误加了工具课也会被滤掉）
    hits = [lid for lid in hits if LESSONS[lid].get("scope") == "object"]
    return hits[:BUBBLE_MAX]


def _hero_object(objects: List[Dict[str, Any]],
                 selected_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    选出课件要讲解的"主角对象"。

    优先级：当前选中 → 已绑定站点的充电桩 → 任意充电桩 → 场景第一个对象。

    🔥 注意最后一级是「兜底」而非「默认」：app.py 初始化时会把场景第一个对象
    放进 selected_object_id，若直接采信，未选择时 hero 会变成一棵树或一条路，
    讲解上下文就变得莫名其妙。所以先按"是不是充电桩"过滤一遍。
    """
    if not objects:
        return None

    if selected_id:
        for o in objects:
            if str(o.get("id")) == str(selected_id):
                # 选中的正好是充电桩 → 直接用
                if str(o.get("type", "")).startswith("charger"):
                    return o
                # 选中的不是充电桩 → 押后，先看看有没有更合适的充电桩
                break

    chargers = [o for o in objects if str(o.get("type", "")).startswith("charger")]
    bound = [c for c in chargers if c.get("bind_station_id")]
    if bound:
        return bound[0]

    if selected_id:
        for o in objects:
            if str(o.get("id")) == str(selected_id):
                return o

    if chargers:
        return chargers[0]
    return objects[0]


def build_ponder_payload(objects: List[Dict[str, Any]],
                         selected_id: Optional[str] = None) -> Dict[str, Any]:
    """
    组装下发给前端的完整思索数据包。

    结构：
      {
        "primSpec": {...},            # 示意几何体规格
        "lessons":  {...},            # 全部课件（按 id 索引）
        "catalog":  [...],            # 课件目录（入口列表）
        "byType":   {...},            # 类型 → 课件 id 列表（悬停气泡用）
        "context":  {...},            # 当前场景上下文（主角对象 + 统计）
        "prefs":    {...},            # 前端持久化用的 storage key 前缀
      }

    这是纯粹的数据，前端不做任何"回填"——因为 iframe 会在每次 rerun 时重建，
    Python 无法实时驱动动画（详见 static/js/ponder.js 顶部说明）。
    """
    hero = _hero_object(objects, selected_id)

    hero_ctx: Optional[Dict[str, Any]] = None
    if hero:
        hero_ctx = {
            "id": str(hero.get("id", "")),
            "name": str(hero.get("name") or hero.get("type") or "未命名对象"),
            "type": str(hero.get("type", "")),
            "station_id": str(hero.get("bind_station_id") or ""),
            "utilization": float(hero.get("utilization") or 0.5),
            "status": str(hero.get("status") or "在线"),
            "bound": bool(hero.get("bind_station_id")),
        }

    charger_count = sum(1 for o in objects if str(o.get("type", "")).startswith("charger"))
    bound_count = sum(
        1 for o in objects
        if str(o.get("type", "")).startswith("charger") and o.get("bind_station_id")
    )

    # 气泡用的类型→课件索引：前端直接查，不用自己过滤
    by_type_resolved: Dict[str, List[str]] = {}
    try:
        for _t in sorted({n["type"] for les in LESSONS.values()
                          for n in les.get("stage", {}).get("nodes", [])} | set(TYPE_LESSON_MAP.keys())):
            _hit = get_lessons_for_type(_t)
            if _hit:
                by_type_resolved[_t] = _hit
    except Exception:
        by_type_resolved = {}

    return {
        "primSpec": PRIM_SPEC,
        "lessons": LESSONS,
        "catalog": get_lesson_list(),
        "byType": by_type_resolved,
        # 智能装置：零件定义 + 资产（GLB / 示意体）
        "devices": DEVICES,
        "deviceAssets": DEVICE_ASSETS,
        "deviceLinks": DEVICE_LINKS,
        "catalogByScope": {
            "tool": [l["id"] for l in get_lesson_list() if l["scope"] == "tool"],
            "object": [l["id"] for l in get_lesson_list() if l["scope"] == "object"],
        },
        "bubbleMax": BUBBLE_MAX,
        "context": {
            "hero": hero_ctx,
            "objectCount": len(objects),
            "chargerCount": charger_count,
            "boundCount": bound_count,
            "unboundCount": max(0, charger_count - bound_count),
        },
        "prefs": {
            "storagePrefix": "ponder_",
            "cameraKey": "dtt_camera_state",
        },
    }


def build_ponder_json(objects: List[Dict[str, Any]],
                      selected_id: Optional[str] = None) -> str:
    """序列化为 JSON 字符串（供字符串替换方式注入 HTML）。"""
    return json.dumps(
        build_ponder_payload(objects, selected_id),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def get_diagnostics() -> Dict[str, Any]:
    """自检信息（供开发期 / 排障使用）。"""
    problems: List[str] = []
    for lid, les in LESSONS.items():
        _stage = les.get("stage", {})
        node_ids = {n["id"] for n in _stage.get("nodes", [])}
        # 装置课没有显式 nodes（零件来自 DEVICES），据此放宽两条校验
        _is_device = bool(_stage.get("device"))
        if not node_ids and not _is_device:
            problems.append(f"{lid}: stage.nodes 为空且未指定 device")
        if _is_device and _stage["device"] not in DEVICES:
            problems.append(f"{lid}: stage.device 未定义 -> {_stage['device']}")
        for e in _stage.get("edges", []):
            if e.get("from") not in node_ids:
                problems.append(f"{lid}: edge.from 未定义 -> {e.get('from')}")
            if e.get("to") not in node_ids:
                problems.append(f"{lid}: edge.to 未定义 -> {e.get('to')}")
        if not les.get("steps"):
            problems.append(f"{lid}: 没有 steps")
        # scope 必须是合法值：写错了会导致课件既不在面板也不在气泡里
        sc = les.get("scope")
        if sc not in VALID_SCOPES:
            problems.append(f"{lid}: scope 非法（{sc!r}），应为 {VALID_SCOPES}")
        for st in les.get("steps", []):
            for act in st.get("actions", []):
                if not isinstance(act.get("at"), (int, float)):
                    problems.append(f"{lid}/{st.get('id')}: action 缺少 at")
                if not act.get("do"):
                    problems.append(f"{lid}/{st.get('id')}: action 缺少 do")
                # 装置课里引用零件 id 的动作，必须是该装置真实存在的零件
                if _is_device and act.get("node"):
                    _parts = {p["id"] for p in DEVICES[_stage["device"]].get("parts", [])} \
                        if _stage["device"] in DEVICES else set()
                    if act["node"] not in _parts:
                        problems.append(
                            f"{lid}/{st.get('id')}: label 指向不存在的零件 -> {act['node']}")
    # 类型映射必须指向存在的课件；且气泡里只应有对象级课程
    for t, ids in TYPE_LESSON_MAP.items():
        for lid in ids:
            if lid not in LESSONS:
                problems.append(f"TYPE_LESSON_MAP[{t}]: 引用了不存在的课件 {lid}")
            elif LESSONS[lid].get("scope") != "object":
                problems.append(
                    f"TYPE_LESSON_MAP[{t}]: {lid} 是 {LESSONS[lid].get('scope')} 级课程，"
                    f"不应进气泡（气泡只放 object 级）"
                )
    # 每个对象级课程至少要出现在一个类型的映射里，否则永远进不了气泡
    mapped = {lid for ids in TYPE_LESSON_MAP.values() for lid in ids}
    for lid, les in LESSONS.items():
        if les.get("scope") == "object" and lid not in mapped:
            problems.append(f"{lid}: 对象级课程却没有任何类型映射，气泡里永远不会出现")

    # ---- 智能装置自检 ----
    import os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for dk, dev in DEVICES.items():
        seen_parts = set()
        for pt in dev.get("parts", []):
            pid = pt.get("id")
            if not pid:
                problems.append(f"DEVICES[{dk}]: 存在无 id 的零件")
                continue
            if pid in seen_parts:
                problems.append(f"DEVICES[{dk}]: 零件 id 重复 -> {pid}")
            seen_parts.add(pid)
            asset = DEVICE_ASSETS.get(pt.get("asset"))
            if asset is None:
                problems.append(f"DEVICES[{dk}].{pid}: 引用了未定义的 asset -> {pt.get('asset')}")
                continue
            if asset.get("kind") == "glb":
                url = str(asset.get("url", ""))
                # 引用不存在的模型会静默变成空装置，必须在这里拦住
                if url.startswith("/app/static/"):
                    rel = url[len("/app/static/"):]
                    if not _os.path.exists(_os.path.join(_root, "static", rel)):
                        problems.append(f"DEVICES[{dk}].{pid}: GLB 文件不存在 -> {url}")
            elif asset.get("kind") == "prim":
                if asset.get("prim") not in PRIM_SPEC:
                    problems.append(
                        f"DEVICES[{dk}].{pid}: 引用了未定义的示意体 -> {asset.get('prim')}")
        for lk in DEVICE_LINKS.get(dk, []):
            for k in ("from", "to"):
                if lk.get(k) not in seen_parts:
                    problems.append(f"DEVICE_LINKS[{dk}]: {k} 指向不存在的零件 -> {lk.get(k)}")
    return {
        "lessonCount": len(LESSONS),
        "stepCount": sum(len(l.get("steps", [])) for l in LESSONS.values()),
        "actionCount": sum(
            len(s.get("actions", []))
            for l in LESSONS.values() for s in l.get("steps", [])
        ),
        "toolLessons": [lid for lid, l in LESSONS.items() if l.get("scope") == "tool"],
        "objectLessons": [lid for lid, l in LESSONS.items() if l.get("scope") == "object"],
        "bubbleByType": {t: get_lessons_for_type(t) for t in TYPE_LESSON_MAP},
        "problems": problems,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_diagnostics())
