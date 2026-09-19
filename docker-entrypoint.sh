#!/bin/sh
# =============================================================================
# 容器入口脚本（Railway / Hugging Face Spaces 通用）
#
# 做两件事：
#   1) 把环境变量里的凭据写成 .streamlit/secrets.toml
#   2) 启动 Streamlit，绑定平台分配的端口
#
# 关于端口（这是个很常见的坑）：
#   Railway 通过 $PORT 环境变量指定端口并只对那个端口做健康检查。
#   但 **Streamlit 不读 PORT**，必须显式传 --server.port。
#   只设 PORT 变量而不传参数的话，应用会监听 8501，
#   平台探测不到 -> 部署显示 "0% health" / 网站打不开。
#
# 关于容错：
#   本脚本刻意**不用 set -e**。生成 secrets 属于「锦上添花」，
#   任何一步失败都不应该阻止应用启动 —— 应用对缺失的密钥有完整降级逻辑。
#   否则一个权限问题就会让整个网站起不来。
# =============================================================================

SECRETS_DIR="${APP_HOME:-/app}/.streamlit"
SECRETS_FILE="$SECRETS_DIR/secrets.toml"

# 平台分配的端口：Railway 用 $PORT；HF Spaces 用 7860；本地默认 8501
PORT="${PORT:-7860}"

# TOML 转义，用于「基本字符串」（双引号形式）。
#
# ⚠️ 这里踩过一个坑：最初用单引号的「字面量字符串」并想把 ' 转义成 ''，
#    那是 SQL 的规则，TOML 不支持 —— 字面量字符串里单引号无法转义，
#    遇到含单引号的值会生成非法 TOML 导致 st.secrets 解析失败。
#    改用基本字符串后，需要转义的只有 \ 和 " 两个字符。
#
# 先删掉 CR/LF：既是防御脏输入，也让命令替换 $() 不会丢掉尾部内容。
toml_escape() {
    printf '%s' "$1" \
        | tr -d '\r\n' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# ---------------------------------------------------------------- 生成 secrets
# 整段容错处理：失败只告警，绝不中断启动
if mkdir -p "$SECRETS_DIR" 2>/dev/null; then
    : > "$SECRETS_FILE" 2>/dev/null || echo "[entrypoint] 警告: secrets.toml 不可写，跳过凭据注入"

    if [ -w "$SECRETS_FILE" ]; then
        for key in SUPABASE_URL SUPABASE_ANON_KEY AMAP_API_KEY AMAP_SECRET_KEY; do
            eval "value=\${$key:-}"
            if [ -n "$value" ]; then
                printf '%s = "%s"\n' "$key" "$(toml_escape "$value")" >> "$SECRETS_FILE"
                echo "[entrypoint] 已写入 $key"
            else
                echo "[entrypoint] 未提供 $key（相关功能将自动降级）"
            fi
        done
        echo "[entrypoint] secrets.toml 生成完毕: $SECRETS_FILE"
    fi
else
    echo "[entrypoint] 警告: 无法创建 $SECRETS_DIR，跳过凭据注入（应用会降级运行）"
fi

echo "[entrypoint] 启动 Streamlit，端口 $PORT ..."

# enableStaticServing 由 .streamlit/config.toml 提供，这里不要覆盖它
exec streamlit run app.py \
    --server.port "$PORT" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
