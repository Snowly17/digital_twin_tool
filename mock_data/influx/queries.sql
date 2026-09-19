-- mock_data/influx/queries.sql
--
-- 对应界面：设置 → 多源数据接入 → 数据源类型 = InfluxDB
--   在 InfluxDB 3 里 database 就是 bucket 名，界面上的 SQL 是：
--     SELECT * FROM "measurement" WHERE time >= now() - INTERVAL '<range>' ORDER BY time DESC LIMIT <n>
--
-- 本项目的模拟数据：
--   measurement : charger_realtime
--   database    : charging
--   时间跨度    : 最近 1 小时（43 个站点 × 12 个时间点 = 516 行）
--
-- 生成时间：2026-09-19T04:20:50+00:00

-- ① 界面里自动执行的那条（可直接粘到 InfluxDB 3 Query 界面）
SELECT * FROM "charger_realtime"
WHERE time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 500;

-- ② 自检：每个站点取最新一条，结果应仍是 C S V 三个字母的形状
SELECT station_id, name, lat, lon, utilization, power
FROM (
  SELECT station_id, name, lat, lon, utilization, power,
         ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY time DESC) AS rn
  FROM "charger_realtime"
  WHERE time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY station_id;

-- ③ 记录总数
SELECT COUNT(*) AS total FROM "charger_realtime";
