from supabase import create_client

# ===== 从 Supabase Dashboard 复制以下两个值 =====
# 位置：Settings → API → Project URL 和 anon public 密钥
SUPABASE_URL = "https://gczvyxbfiawnjoviqncj.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_LzMmeKqLbPWrgT15jMWlBQ_wQSof4oz"

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
    "power": 60.0
}
supabase.table("station_realtime_data").upsert(test_data).execute()
print("✅ 实时数据插入成功！")