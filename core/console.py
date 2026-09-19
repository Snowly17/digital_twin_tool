"""
core/console.py
控制台输出的健壮化 + 凭据读取。

第一部分：控制台编码
--------------------
本项目的 print() 里有大量 emoji（✅ ⚠️ ❌ ℹ️）与中文提示，散落在 200+ 处。
在宿主机**非 UTF-8 控制台**上（简体中文 Windows 的默认代码页是 GBK/cp936），
Python 会把 sys.stdout.encoding 设为 GBK，而 GBK 无法编码 emoji，于是：

    print("⚠️ 未找到 station_inf.csv")
    -> UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'

这条异常发生在「数据文件缺失」这类**降级提示**上——也就是不带 data/ 运行时的
正常路径。它会让本该只打印一句警告的启动流程直接崩溃。

        ⚠️ 注意：这个坑在 Linux / Streamlit Cloud（UTF-8）上不会出现，
        只在 Windows 本地或非 UTF-8 容器里复现。

处理方式：把标准流的编码错误策略由 'strict' 放宽为 'replace'，**编码本身不变**：

    · UTF-8 环境：行为完全不变（永远走不到 replace 分支）
    · GBK 环境：emoji 显示为 '?'，但应用继续运行，不崩溃

调用点（见各文件顶部）
----------------------
    app.py                    -> Streamlit 入口
    core/path_manager.py      -> core.* / common.* 模块的公共依赖，覆盖最广
    mock_mqtt_publisher.py    -> 独立脚本入口

第二部分：凭据读取
------------------
`read_secret()` 统一从 st.secrets / 环境变量取密钥，供各处调用。
**严禁把真实密钥写死在源码里**——本仓库是公开仓库，硬编码等于公开发布。
"""

import os
import sys


def read_secret(name: str, default: str = "") -> str:
    """
    按优先级读取一个凭据：st.secrets -> 环境变量 -> default。

    **不要把真实密钥写进源码**——本仓库要推到公开仓库，硬编码等于公开发布。
    本地开发放进 .streamlit/secrets.toml（已被 .gitignore 排除），
    Streamlit Community Cloud 放进应用的 Secrets 设置里。

    这里延迟导入 streamlit：core/console.py 会被 core/path_manager.py 导入，
    而后者可能在没有 Streamlit 运行时的场景（脚本、测试）下被使用。
    """
    # 1) Streamlit secrets（云端与本地 secrets.toml 都走这里）
    try:
        import streamlit as st

        if name in st.secrets:
            value = str(st.secrets[name]).strip()
            if value:
                return value
    except Exception:
        # 没有 secrets.toml、没有 ScriptRunContext、或 streamlit 未安装
        pass

    # 2) 环境变量
    value = os.environ.get(name, "").strip()
    if value:
        return value

    return default


def enable_safe_console() -> None:
    """
    让 print() 在编码能力不足的控制台上不抛 UnicodeEncodeError。
    幂等，可重复调用；任何情况下都不会抛异常。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 被重定向成普通对象（如 Streamlit 的日志包装器）时无需处理
            continue
        try:
            # 只放宽错误策略，不动编码，避免影响上游的日志解析
            reconfigure(errors="replace")
        except Exception:
            pass
