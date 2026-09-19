# =============================================================================
# 数字孪生轻量化快速建模工具 —— Docker 部署镜像
#
# 适用平台：Railway（推荐，用 $PORT）/ Hugging Face Spaces（用 7860）
#           任何支持 Dockerfile 的托管平台都可以
#
# 为什么不用 Streamlit Community Cloud：
#   该平台不支持 server.enableStaticServing，
#   导致 /app/static/* 请求落到 SPA 兜底路由、返回 text/html，
#   于是 three.js / 26 个 GLB / ponder.js 全部被浏览器按 MIME 检查拒绝，
#   表现为「3D 场景全空 + 加载遮罩卡在 0%」。
#   在自控容器里跑 streamlit，静态文件服务正常生效，应用代码无需改动。
#
# 基础镜像：python:3.12-slim（与本地 venv 的 3.12.10 一致）
#   requirements.txt 里全是预编译 wheel（含 psycopg2-binary、bcrypt），
#   因此不需要 gcc / build-essential，镜像可以保持精简。
# =============================================================================
FROM python:3.12-slim

# Python 输出不缓冲，日志能实时出现在 Spaces 的 Logs 里
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# -----------------------------------------------------------------------------
# 依赖层：先只复制 requirements.txt，这样改代码不会让 pip 重新装一遍
# -----------------------------------------------------------------------------
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# 代码层
#   static/      3D 模型 + three.js + 思索运行时（静态服务从这里提供）
#   checkpoints/ LSTM 权重（仅 60KB；无 torch 时不会用到）
#   .streamlit/config.toml  必须存在，enableStaticServing 就在这里
#   注意：data/ 与 venv/ 由 .dockerignore 排除
# -----------------------------------------------------------------------------
COPY . .

# 入口脚本负责在容器启动时用环境变量生成 secrets.toml（见该文件说明）
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/.streamlit \
    && chmod -R a+w /app/.streamlit

# 端口：
#   Railway 通过 $PORT 指定并只对那个端口做健康检查
#   Hugging Face Spaces 用 7860
#   实际绑定哪个端口由 docker-entrypoint.sh 读取 $PORT 决定，
#   EXPOSE 只是文档性声明。
EXPOSE 7860

# 注意：不要在此处加 --server.enableStaticServing=false 之类的覆盖参数，
#       静态服务是本镜像能跑起来的前提。
ENTRYPOINT ["/app/docker-entrypoint.sh"]
