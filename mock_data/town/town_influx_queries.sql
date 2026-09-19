-- mock_data/influx/queries.sql（充电小镇版）
-- 界面：设置 → 多源数据接入 → InfluxDB
--   Database    : charging
--   Measurement : charger_realtime
--   时间范围    : 最近 1 小时
-- 生成时间：2026-09-19T04:51:02+00:00

-- ① 界面里自动执行的那条
SELECT * FROM "charger_realtime"
WHERE time >= now() - INTERVAL '1 hour'
ORDER BY time DESC
LIMIT 500;

-- ② 每个站点取最新一条（20 个站点）
SELECT station_id, name, lat, lon, utilization, power
FROM (
  SELECT station_id, name, lat, lon, utilization, power,
         ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY time DESC) AS rn
  FROM "charger_realtime"
  WHERE time >= now() - INTERVAL '24 hours'
)
WHERE rn = 1
ORDER BY station_id;

-- ③ 总记录数（应为 300）
SELECT COUNT(*) AS total FROM "charger_realtime";
