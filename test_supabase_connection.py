# test_supabase_connection.py
#
# ⚠️ 本文件原先硬编码了真实的 Supabase URL 与 anon key（本仓库为公开仓库）。
#    现已改为从 .streamlit/secrets.toml 或环境变量读取，密钥不进版本库。
#
# 本地运行前，在 .streamlit/secrets.toml 里配置：
#     SUPABASE_URL      = "https://xxxx.supabase.co"
#     SUPABASE_ANON_KEY = "sb_publishable_xxxx"
#
# 用法：python test_supabase_connection.py
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.console import enable_safe_console, read_secret

enable_safe_console()

from supabase import create_client  # noqa: E402

SUPABASE_URL = read_secret("SUPABASE_URL")
SUPABASE_ANON_KEY = read_secret("SUPABASE_ANON_KEY")


def test_connection():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("❌ 未配置 SUPABASE_URL / SUPABASE_ANON_KEY")
        print("   请写入 .streamlit/secrets.toml，或设置同名环境变量。")
        return

    try:
        print("🔌 正在连接 Supabase...")
        client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        print("✅ 客户端创建成功")

        # 查询 station_realtime_data 表
        try:
            response = client.table("station_realtime_data").select("*").limit(1).execute()
            print(f"✅ 查询成功，返回 {len(response.data)} 条记录")
            if response.data:
                print(f"   数据: {json.dumps(response.data, ensure_ascii=False)}")
        except Exception as e:
            print(f"⚠️ 查询 station_realtime_data 表失败: {e}")

        # 查询 scenes 表
        try:
            response = client.table("scenes").select("*").limit(1).execute()
            print(f"✅ 查询 scenes 表成功，返回 {len(response.data)} 条记录")
        except Exception as e:
            print(f"⚠️ 查询 scenes 表失败: {e}")

        print("\n🎉 连接测试完成！")

    except Exception as e:
        print(f"❌ 连接失败: {e}")
        print("请检查：")
        print("  1. SUPABASE_URL 是否正确")
        print("  2. SUPABASE_ANON_KEY 是否正确（不是 service_role）")
        print("  3. 网络是否通畅（可能需要代理）")


if __name__ == "__main__":
    test_connection()
