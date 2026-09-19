# test_supabase.py
#
# ⚠️ 本文件原先硬编码了真实的 Supabase URL 与 anon key（本仓库为公开仓库）。
#    现已改为从 .streamlit/secrets.toml 或环境变量读取，密钥不进版本库。
#
# 本地运行前，在 .streamlit/secrets.toml 里配置：
#     SUPABASE_URL      = "https://xxxx.supabase.co"
#     SUPABASE_ANON_KEY = "sb_publishable_xxxx"
#
# 用法：python test_supabase.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.console import enable_safe_console, read_secret

enable_safe_console()

from supabase import create_client  # noqa: E402

SUPABASE_URL = read_secret("SUPABASE_URL")
SUPABASE_ANON_KEY = read_secret("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    print("❌ 未配置 SUPABASE_URL / SUPABASE_ANON_KEY")
    print("   请写入 .streamlit/secrets.toml，或设置同名环境变量。")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# 测试查询
resp = supabase.table("scenes").select("*").execute()
print("✅ 连接成功！场景数：", len(resp.data))

# 测试插入实时数据
test_data = {
    "station_id": "test_001",
    "utilization": 0.75,
    "available_slots": 3,
    "status": "在线",
    "power": 60.0,
}
supabase.table("station_realtime_data").upsert(test_data).execute()
print("✅ 实时数据插入成功！")
