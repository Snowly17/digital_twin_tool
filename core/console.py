"""
core/console.py
控制台输出的健壮化处理。

问题背景
--------
本项目的 print() 里有大量 emoji（✅ ⚠️ ❌ ℹ️）与中文提示，散落在 200+ 处。
在宿主机**非 UTF-8 控制台**上（简体中文 Windows 的默认代码页是 GBK/cp936），
Python 会把 sys.stdout.encoding 设为 GBK，而 GBK 无法编码 emoji，于是：

    print("⚠️ 未找到 station_inf.csv")
    -> UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'

这条异常发生在「数据文件缺失」这类**降级提示**上——也就是不带 data/ 运行时的
正常路径。它会让本该只打印一句警告的启动流程直接崩溃。

        ⚠️ 注意：这个坑在 Linux / Streamlit Cloud（UTF-8）上不会出现，
        只在 Windows 本地或非 UTF-8 容器里复现。

处理方式
--------
把标准流的编码错误策略由 'strict' 放宽为 'replace'，**编码本身不变**：

    · UTF-8 环境：行为完全不变（永远走不到 replace 分支）
    · GBK 环境：emoji 显示为 '?'，但应用继续运行，不崩溃

调用点（见各文件顶部）
----------------------
    app.py                    -> Streamlit 入口
    core/path_manager.py      -> core.* / common.* 模块的公共依赖，覆盖最广
    mock_mqtt_publisher.py    -> 独立脚本入口
"""

import sys


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
