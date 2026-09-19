import os
import streamlit as st
from streamlit.components.v1 import declare_component

# ⚠️ 阶段一仅修正入口路径：index.html 在 component/frontend/ 下，
# 之前指向 component/ 目录，declare_component 找不到入口。
#
# ⚠️ 但此模块目前仍是「不能用」的状态，两点原因：
#   1) 全项目没有任何地方 import 它（app.py 一直用的是 st.components.v1.html）——死代码；
#   2) frontend/index.html 用 innerHTML 注入 HTML，而 innerHTML 插入的
#      <script> 按 HTML 规范不会执行，所以就算路径修对了场景也不会渲染。
#   3) Streamlit 1.63 起 st.components.v1 已废弃，官方要求新组件用
#      st.components.v2.component()（见阶段二）。
_COMPONENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

def threejs_scene(html_content: str, key=None, height=800):
    # declare_component 返回一个可调用对象，path 指向组件目录
    component_func = declare_component("threejs_scene", path=_COMPONENT_DIR)
    # 调用返回的函数，传递前端参数
    return component_func(html_content=html_content, height=height, key=key)