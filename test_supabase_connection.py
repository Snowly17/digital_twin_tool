# test_supabase_connection.py
import sys
import os

# 🔧 强制设置编码
os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import json
from supabase import create_client

# 🔥 你的 Supabase 凭据
SUPABASE_URL = "https://gczvyxbfiawnjoviqncj.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_LzMmeKqLbPWrgT15jMWlBQ_wQSof4oz"

def test_connection():
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