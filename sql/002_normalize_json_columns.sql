-- ============================================================================
-- 002_normalize_json_columns.sql
--
-- 目的：修复 public.scene_objects 中 4 个 JSON 列的「双重编码」问题。
--
-- 现象（实测 2026-09-18，全库 14 个场景 / 1205 行）：
--   · position / rotation / scale / custom_props 共 4820 个字段值，
--     其中 4452 个是 JSON **对象**，368 个是 JSON **字符串**（双重编码）。
--   · 而且**按场景完全分化**：同一场景里 4 个字段要么全是对象、要么全是字符串。
--
-- 根因（两条写入路径不一致）：
--   · 前端 JS 路径：直接把对象写进 jsonb 列            → 存成 JSON 对象 ✓
--   · Python core/supabase_client.py 的 sync_scene_objects():
--         if k in ('position','rotation','scale','custom_props'):
--             if isinstance(v, dict): new_obj[k] = json.dumps(v)   # ← 又序列化了一次
--     把 dict 先 json.dumps 成字符串再交给 PostgREST → jsonb 列里存的是
--     「一个内容为 JSON 文本的字符串」，读出来就是带引号的字符串。
--     → 92 行 × 4 字段 = 368 个字符串值，全部来自 Python 写入的场景。
--
-- 影响：
--   · 应用本身不受影响：core/supabase_client.py:get_scene_objects() 里有
--       if isinstance(v, str): v = json.loads(v)
--     的兼容处理，所以读得回来。
--   · 但其它消费者会踩坑：SQL 里 `position->>'x'` 对字符串行返回 NULL，
--     外部报表 / 数据导出 / 其它客户端拿到的类型不一致。
--   · 前端 upsert 会把经过它的行**自动纠正**为对象（增量自愈），
--     但只覆盖被同步过的场景；未同步的仍是字符串。
--
-- 执行位置：Supabase 控制台 → SQL Editor
-- 建议顺序：第 1 步体检 → 第 2 步归一化 → 第 3 步复核
-- 回滚：执行前请先导出（本仓库 exports/ 下已有 scenes 与重复行的完整备份）
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 第 1 步：体检。看每个列各有多少个「字符串」值（预期 92 行 × 4 列）
-- ---------------------------------------------------------------------------
SELECT
  count(*) FILTER (WHERE jsonb_typeof(position)     = 'string') AS position_str,
  count(*) FILTER (WHERE jsonb_typeof(rotation)     = 'string') AS rotation_str,
  count(*) FILTER (WHERE jsonb_typeof(scale)        = 'string') AS scale_str,
  count(*) FILTER (WHERE jsonb_typeof(custom_props) = 'string') AS custom_props_str,
  count(*)                                                      AS total_rows
FROM public.scene_objects;


-- ---------------------------------------------------------------------------
-- 第 2 步：归一化。只处理确实是字符串的值（jsonb_typeof = 'string'），
--         用 #>> '{}' 取出被包住的那层文本，再转回 jsonb 对象。
--         对已经是对象的行不做任何改动。
-- ---------------------------------------------------------------------------
UPDATE public.scene_objects SET position     = (position     #>> '{}')::jsonb WHERE jsonb_typeof(position)     = 'string';
UPDATE public.scene_objects SET rotation     = (rotation     #>> '{}')::jsonb WHERE jsonb_typeof(rotation)     = 'string';
UPDATE public.scene_objects SET scale        = (scale        #>> '{}')::jsonb WHERE jsonb_typeof(scale)        = 'string';
UPDATE public.scene_objects SET custom_props = (custom_props #>> '{}')::jsonb WHERE jsonb_typeof(custom_props) = 'string';


-- ---------------------------------------------------------------------------
-- 第 3 步：复核。4 个 *_str 列应全部为 0，total_rows 不变。
-- ---------------------------------------------------------------------------
-- （重跑第 1 步的 SELECT 即可）


-- ============================================================================
-- 重要：数据库归一化只是「治标」。
-- 若不同时修掉 Python 的写入路径，下次用 Python 保存场景时又会写回字符串。
-- 代码侧的修法（app 内 core/supabase_client.py 的 sync_scene_objects）：
--   删除对 position/rotation/scale/custom_props 的 json.dumps，
--   直接把 dict 交给 PostgREST —— 与前端 JS 的行为保持一致。
-- 该修改尚未执行，需确认后再改。
-- ============================================================================
