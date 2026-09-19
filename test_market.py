from core.supabase_client import get_public_scenes, get_geo_tile

# 测试1：公共场景
scenes = get_public_scenes()
print(f"✅ 公共场景数: {len(scenes)}")

# 测试2：地理底图
tile = get_geo_tile("长春市")
print(f"✅ 长春市底图: {tile}")

# 测试3：其他区域
for region in ["吉林市", "四平市", "松原市"]:
    t = get_geo_tile(region)
    print(f"   {region}: {'✅' if t else '❌ 无数据'}")