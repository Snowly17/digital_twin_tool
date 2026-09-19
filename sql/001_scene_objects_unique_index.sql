-- ============================================================================
-- 001_scene_objects_unique_index.sql
--
-- 目的：给 public.scene_objects 加上 (scene_id, object_uuid) 唯一索引，
--       使前端可以用 upsert(onConflict) 做「就地更新」，彻底消除
--       「先删整场景、再整场景插入」造成的空窗与并发重复。
--
-- 背景（实测）：
--   · 客户端调用 upsert 时，PostgREST 返回
--       42P10: there is no unique or exclusion constraint matching the ON CONFLICT specification
--     即当前**没有**任何唯一约束可以当冲突目标。
--   · 旧版前端在滑块 input 事件里反复发起「删除全部 + 插入全部」，且当时**没有串行化**，
--     并发结果就是同一 object_uuid 多份并存。实测某场景 1323 行其实是
--     49 个物体 × 27 份重复（27 行在 0.82 秒内插入）。
--     → 因此**必须先清理重复行，否则 CREATE UNIQUE INDEX 会直接失败**。
--
-- 执行位置：Supabase 控制台 → SQL Editor（REST API 不暴露 DDL，只能在控制台执行）
-- 执行顺序：第 1 步 → 确认返回 0 → 第 2 步 → 第 3 步
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 第 1 步：先体检。确认没有重复组（返回 0 行才继续）
-- ---------------------------------------------------------------------------
SELECT scene_id, object_uuid, count(*) AS copies
FROM public.scene_objects
GROUP BY scene_id, object_uuid
HAVING count(*) > 1
ORDER BY copies DESC;


-- ---------------------------------------------------------------------------
-- 第 2 步（仅在体检有结果时执行）：去重
--   同一 (scene_id, object_uuid) 只保留 created_at 最新的一行；
--   若 created_at 相同，则保留 id 最大的一行（保证结果确定）。
--   建议先备份：本仓库 exports/scene_objects_dup_backup_*.json 是 2026-09-18
--   对重复场景的完整导出，可作为回滚依据。
-- ---------------------------------------------------------------------------
DELETE FROM public.scene_objects a
USING public.scene_objects b
WHERE a.scene_id     = b.scene_id
  AND a.object_uuid  = b.object_uuid
  AND (a.created_at < b.created_at
       OR (a.created_at = b.created_at AND a.id < b.id));


-- ---------------------------------------------------------------------------
-- 第 3 步：创建唯一索引
--   表很小（约 1.2 千行），直接建即可，无需 CONCURRENTLY。
--   ※ 若以后数据量很大要用 CONCURRENTLY，注意它不能在事务块内执行
--     （Supabase SQL Editor 会把整段当一次提交，届时分两条语句单独跑）。
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS scene_objects_scene_uuid_key
  ON public.scene_objects (scene_id, object_uuid);


-- ---------------------------------------------------------------------------
-- 第 4 步：验证。应返回 1 行，索引名为 scene_objects_scene_uuid_key
-- ---------------------------------------------------------------------------
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'scene_objects'
  AND indexname = 'scene_objects_scene_uuid_key';


-- ============================================================================
-- 建好之后：前端无需再改代码。
-- app.py 的 syncToSupabase() 会在首次写入时尝试 upsert，失败代码为 42P10 时
-- 自动退化为「插入 + 清理旧行」；一旦本索引存在，同一次调用就会走 upsert 路径，
-- 并在控制台打印：
--     ☁️ 已同步（upsert）：写入 N 行，删除旧行 M 行
-- 若仍打印「插入+清理」，说明索引未生效，请回到第 4 步核对。
-- ============================================================================
