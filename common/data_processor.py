import requests
import pandas as pd
import numpy as np
import sqlite3
import time
import os
import hashlib
from datetime import datetime, timedelta
import math
import torch
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from common import baselines
from common.lstm_utils import build_lstm_input_features
import streamlit as st
from core.db_utils import safe_sqlite_connect
from core.console import read_secret
from core.path_manager import (
    DATA_DIR,  # ← 添加这一行
    STATION_OCC_PATH, STATION_INF_PATH, STATION_EPRICE_PATH,
    ZONE_OCC_PATH, ZONE_INF_PATH, ZONE_EPRICE_PATH, ZONE_WEATHER_PATH
)

# ======================================================================
# 电价 / 天气文件缓存
#
# e_price.csv 有 341MB / 1423 列：全量读入约 23s，且要占 594MB 内存。
# 但站点级预测每次只用到「被预测站点」的电价列（单列仅 0.83MB），
# 所以这里按需只读对应列并缓存，避免每次预测都重读整张表。
# ======================================================================
@st.cache_resource(show_spinner=False)
def _load_price_columns(station_ids: tuple):
    """只读取指定站点的电价列；文件缺失或列不存在时返回 None。"""
    if not os.path.exists(STATION_EPRICE_PATH):
        return None
    try:
        head = pd.read_csv(STATION_EPRICE_PATH, index_col=0, nrows=0)
        index_name = head.index.name
        avail = {str(c) for c in head.columns}
        keep = [str(c) for c in station_ids if str(c) in avail]
        if not keep:
            return None
        df = pd.read_csv(STATION_EPRICE_PATH, index_col=0, usecols=[index_name] + keep)
        df.index = pd.to_datetime(df.index)
        df.columns = df.columns.astype(str)
        return df
    except Exception as e:
        print(f"⚠️ 读取电价列失败，回退默认电价: {e}")
        return None


@st.cache_resource(show_spinner=False)
def _load_weather_cached():
    """天气文件只有 0.16MB，读一次缓存即可。"""
    if not os.path.exists(ZONE_WEATHER_PATH):
        return None
    try:
        df = pd.read_csv(ZONE_WEATHER_PATH, index_col='time')
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        print(f"⚠️ 读取天气文件失败: {e}")
        return None


class DataProcessor:
    def __init__(self, use_real_data=False, data_level='region'):
        # 高德地图 Web 服务 key 与签名私钥。
        # ⚠️ 严禁在此硬编码真实密钥（本仓库为公开仓库）。
        #    本地：写入 .streamlit/secrets.toml（已被 .gitignore 排除）
        #    云端：写入 Streamlit Community Cloud 的 Secrets 设置
        #    未配置时 _fetch_from_amap() 会跳过 API 请求，功能自动降级。
        self.api_key = read_secret("AMAP_API_KEY")
        self.secret_key = read_secret("AMAP_SECRET_KEY")
        self.cache_db = "charger_cache.db"
        self.db_path = self.cache_db
        self.cache_hours = 24
        self.use_real_data = use_real_data
        self.data_level = data_level
        self._init_db()

    def _init_db(self):
        try:
            conn = safe_sqlite_connect(self.cache_db)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS charger_cache (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    address TEXT,
                    lat REAL,
                    lon REAL,
                    type TEXT,
                    power INTEGER,
                    utilization REAL,
                    price REAL,
                    available_slots INTEGER,
                    distance REAL,
                    adcode TEXT,
                    city TEXT,
                    brand TEXT,
                    cache_lat REAL,
                    cache_lon REAL,
                    cache_radius REAL,
                    cache_time TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"数据库初始化失败: {e}")

    def _generate_sig(self, params):
        sorted_keys = sorted([k for k in params.keys() if k not in ('sig', 'key')])
        sign_str = ''
        for k in sorted_keys:
            sign_str += k + str(params[k])
        sign_str += self.secret_key
        return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

    def _haversine(self, lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c

    def _extract_price(self, business):
        try:
            if isinstance(business, dict) and 'charge_fee' in business:
                price_str = business['charge_fee']
                import re
                nums = re.findall(r'\d+\.?\d*', price_str)
                if nums:
                    return float(nums[0])
        except:
            pass
        return np.random.uniform(0.8, 2.2)

    def _simulate_utilization(self, distance, poi):
        base = max(0.1, 1 - distance / 20)
        noise = np.random.normal(0, 0.15)
        if '快充' in poi.get('name', ''):
            base += 0.1
        return min(0.95, max(0.1, base + noise))

    def _fetch_from_amap(self, user_lat, user_lon, radius_km):
        if not self.api_key or self.api_key == "你的高德API Key":
            print("API Key 未配置，跳过API请求")
            return pd.DataFrame()

        url = "https://restapi.amap.com/v3/place/around"
        params = {
            'key': self.api_key,
            'location': f"{user_lon},{user_lat}",
            'keywords': '充电站',
            'types': '150900',
            'radius': radius_km * 1000,
            'offset': 25,
            'page': 1,
            'extensions': 'all'
        }
        params['sig'] = self._generate_sig(params)

        try:
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get('status') != '1':
                print(f"API返回错误: {data.get('info')}")
                return pd.DataFrame()

            pois = data.get('pois', [])
            if not pois:
                print("附近没有充电站")
                return pd.DataFrame()

            stations = []
            for poi in pois:
                loc = poi.get('location', '').split(',')
                if len(loc) != 2:
                    continue
                lon, lat = float(loc[0]), float(loc[1])
                distance = self._haversine(user_lat, user_lon, lat, lon)
                if distance > radius_km:
                    continue

                name = poi.get('name', '')
                charger_type = '快充' if ('快充' in name or '超充' in name) else '慢充'
                power = np.random.choice([120, 180]) if charger_type == '快充' else np.random.choice([60, 80])
                price = self._extract_price(poi.get('business', {}))
                utilization = self._simulate_utilization(distance, poi)
                available = np.random.randint(0, 8)

                stations.append({
                    'id': poi.get('id', ''),
                    'name': name,
                    'address': poi.get('address', ''),
                    'lat': lat,
                    'lon': lon,
                    'type': charger_type,
                    'power': power,
                    'utilization': round(utilization, 2),
                    'price': round(price, 2),
                    'available_slots': available,
                    'distance': round(distance, 2),
                    'adcode': poi.get('adcode', ''),
                    'city': poi.get('cityname', ''),
                    'brand': poi.get('brand', '')
                })
            return pd.DataFrame(stations)
        except Exception as e:
            print(f"API请求异常: {e}")
            return pd.DataFrame()

    def _save_to_cache(self, df, cache_lat, cache_lon, cache_radius):
        if df.empty:
            return
        try:
            conn = safe_sqlite_connect(self.cache_db)
            df_copy = df.copy()
            df_copy['cache_lat'] = cache_lat
            df_copy['cache_lon'] = cache_lon
            df_copy['cache_radius'] = cache_radius
            df_copy['cache_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            df_copy.to_sql('charger_cache', conn, if_exists='append', index=False)
            conn.close()
        except Exception as e:
            print(f"缓存写入失败: {e}")

    def _get_cached_data(self, user_lat, user_lon, radius_km):
        try:
            conn = safe_sqlite_connect(self.cache_db)
            cutoff = (datetime.now() - timedelta(hours=self.cache_hours)).strftime('%Y-%m-%d %H:%M:%S')
            query = f"SELECT * FROM charger_cache WHERE cache_time > '{cutoff}' ORDER BY cache_time DESC"
            df = pd.read_sql_query(query, conn)
            conn.close()
            if df.empty:
                return None
            df['distance'] = df.apply(
                lambda r: self._haversine(user_lat, user_lon, r['lat'], r['lon']),
                axis=1
            )
            df = df[df['distance'] <= radius_km].sort_values('distance')
            return df.head(50)
        except Exception as e:
            print(f"缓存读取失败: {e}")
            return None

    def get_db_update_time(self):
        try:
            conn = safe_sqlite_connect(self.cache_db)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(cache_time) FROM charger_cache")
            result = cursor.fetchone()
            conn.close()
            if result and result[0]:
                return result[0]
            return None
        except Exception as e:
            print(f"获取更新时间失败: {e}")
            return None

    def get_nearby_chargers(self, user_lat, user_lon, radius_km=5):
        if not self.use_real_data:
            return self.generate_simulated_at_location(user_lat, user_lon, max(10, int(radius_km * 2)))

        cached = self._get_cached_data(user_lat, user_lon, radius_km)
        if cached is not None and not cached.empty:
            return cached

        df = self._fetch_from_amap(user_lat, user_lon, radius_km)
        if df.empty:
            df = self.generate_simulated_at_location(user_lat, user_lon, max(10, int(radius_km * 2)))
        else:
            self._save_to_cache(df, user_lat, user_lon, radius_km)
        return df

    def generate_simulated_at_location(self, lat, lon, n_points=15):
        np.random.seed(int(time.time()) % 1000)
        lats = lat + 0.02 * np.random.randn(n_points)
        lons = lon + 0.03 * np.random.randn(n_points)

        districts = ['东城区', '西城区', '朝阳区', '海淀区', '丰台区', '石景山区',
                     '通州区', '大兴区', '房山区', '门头沟区', '昌平区', '顺义区',
                     '密云区', '怀柔区', '平谷区', '延庆区']

        stations = []
        for i in range(n_points):
            dist = self._haversine(lat, lon, lats[i], lons[i])
            district = np.random.choice(districts)
            road = f"{np.random.choice(['路', '大街', '东路', '西路', '南路', '北路'])}"
            number = np.random.randint(1, 200)
            address = f"北京市{district}{road}{number}号"
            name = f"{district}充电站_{i:03d}"

            stations.append({
                'id': f'sim_{i}',
                'name': name,
                'address': address,
                'lat': lats[i],
                'lon': lons[i],
                'type': np.random.choice(['快充', '慢充'], p=[0.6, 0.4]),
                'power': np.random.choice([60, 120, 180]),
                'utilization': round(np.random.uniform(0.1, 0.9), 2),
                'price': round(np.random.uniform(0.9, 2.0), 2),
                'available_slots': np.random.randint(0, 6),
                'distance': round(dist, 2)
            })
        return pd.DataFrame(stations)

    def generate_simulated_data(self, center_lat=39.9, center_lon=116.4, n_points=150):
        np.random.seed(42)
        districts_info = {
            '朝阳区': {'weight': 1.2, 'centers': [(39.92, 116.46), (39.99, 116.48), (40.08, 116.48)]},
            '海淀区': {'weight': 1.1, 'centers': [(39.98, 116.31), (39.91, 116.23), (40.02, 116.27)]},
            '东城区': {'weight': 0.9, 'centers': [(39.90, 116.41), (39.93, 116.42)]},
            '西城区': {'weight': 0.9, 'centers': [(39.90, 116.37), (39.94, 116.36)]},
            '丰台区': {'weight': 0.8, 'centers': [(39.86, 116.29), (39.85, 116.35)]},
            '石景山区': {'weight': 0.6, 'centers': [(39.91, 116.22)]},
            '通州区': {'weight': 0.7, 'centers': [(39.90, 116.66)]},
            '大兴区': {'weight': 0.6, 'centers': [(39.73, 116.33)]},
            '房山区': {'weight': 0.5, 'centers': [(39.75, 116.14)]},
            '门头沟区': {'weight': 0.4, 'centers': [(39.94, 116.10)]},
            '昌平区': {'weight': 0.5, 'centers': [(40.22, 116.23)]},
            '顺义区': {'weight': 0.5, 'centers': [(40.13, 116.65)]},
            '密云区': {'weight': 0.3, 'centers': [(40.38, 116.85)]},
            '怀柔区': {'weight': 0.3, 'centers': [(40.32, 116.63)]},
            '平谷区': {'weight': 0.2, 'centers': [(40.14, 117.12)]},
            '延庆区': {'weight': 0.2, 'centers': [(40.46, 115.97)]},
        }

        all_lats, all_lons, all_districts = [], [], []
        all_addresses, all_names = [], []
        all_types, all_powers, all_utilizations, all_prices, all_available = [], [], [], [], []

        total_weights = sum(info['weight'] for info in districts_info.values())
        expected_counts = {district: max(5, int(n_points * info['weight'] / total_weights)) for district, info in districts_info.items()}

        for district, info in districts_info.items():
            expected_count = expected_counts[district]
            centers = info['centers']
            district_lats, district_lons = [], []
            n_centers = len(centers)
            points_per_center = expected_count // n_centers
            remainder = expected_count % n_centers

            for i, (c_lat, c_lon) in enumerate(centers):
                points_to_gen = points_per_center + (1 if i < remainder else 0)
                if points_to_gen <= 0:
                    continue
                lats = np.random.normal(c_lat, 0.03, points_to_gen)
                lons = np.random.normal(c_lon, 0.03, points_to_gen)
                district_lats.extend(lats)
                district_lons.extend(lons)

            all_lats.extend(district_lats)
            all_lons.extend(district_lons)
            all_districts.extend([district] * len(district_lats))

            for _ in range(len(district_lats)):
                charger_type = np.random.choice(['快充', '慢充'], p=[0.65, 0.35])
                power = np.random.choice([120, 150, 180, 240]) if charger_type == '快充' else np.random.choice([60, 80, 100])
                base_util = np.random.uniform(0.35, 0.85) if info['weight'] > 0.9 else np.random.uniform(0.2, 0.7)
                utilization = min(0.95, base_util + np.random.uniform(-0.15, 0.15))
                base_price = 1.2 if charger_type == '快充' else 0.9
                if info['weight'] > 0.9:
                    base_price += 0.15
                price = base_price + np.random.uniform(-0.2, 0.2)
                price = round(price, 2)

                if utilization > 0.7:
                    available = np.random.randint(0, 3)
                elif utilization > 0.4:
                    available = np.random.randint(2, 6)
                else:
                    available = np.random.randint(4, 10)

                all_types.append(charger_type)
                all_powers.append(power)
                all_utilizations.append(round(utilization, 2))
                all_prices.append(price)
                all_available.append(available)

                road = f"{np.random.choice(['路', '大街', '东路', '西路', '南路', '北路'])}"
                number = np.random.randint(1, 200)
                all_addresses.append(f"北京市{district}{road}{number}号")
                all_names.append(f"{district}充电站_{np.random.randint(100, 999)}")

        df = pd.DataFrame({
            'id': [f'sim_{i}' for i in range(len(all_lats))],
            'name': all_names,
            'address': all_addresses,
            'lat': all_lats,
            'lon': all_lons,
            'type': all_types,
            'power': all_powers,
            'utilization': all_utilizations,
            'price': all_prices,
            'available_slots': all_available,
            'distance': 0
        })

        def haversine(lon1, lat1, lon2, lat2):
            R = 6371
            lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
            dlon = lon2 - lon1
            dlat = lat2 - lat1
            a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            return R * c

        df['distance'] = df.apply(lambda r: haversine(116.397128, 39.916527, r['lon'], r['lat']), axis=1)
        return df.sort_values('distance')

    def load_shenzhen_data(self, data_dir=None):
        """
        加载深圳区域级数据（275个交通小区）
        Args:
            data_dir: 数据目录，默认使用 DATA_DIR
        Returns:
            DataFrame: 包含经度、纬度、利用率等字段
        """
        if data_dir is None:
            data_dir = DATA_DIR

        try:
            inf_path = os.path.join(data_dir, 'zone-level', 'inf.csv')
            occ_path = os.path.join(data_dir, 'zone-level', 'occupancy.csv')

            if not os.path.exists(inf_path) or not os.path.exists(occ_path):
                print(f"区域级数据文件不存在，请检查路径: {data_dir}")
                return pd.DataFrame()

            inf = pd.read_csv(inf_path, header=0)
            inf.rename(columns={'TAZID': 'zone_id', 'latitude': 'lat', 'longitude': 'lon'}, inplace=True)

            occ = pd.read_csv(occ_path, header=0, index_col=0)
            latest_util = occ.iloc[-1].values
            occ_columns = occ.columns.astype(str)

            inf['zone_id'] = inf['zone_id'].astype(str)

            util_series = pd.Series(latest_util, index=occ_columns)
            inf['utilization'] = inf['zone_id'].map(util_series)

            zone_agg = inf.groupby('zone_id').agg({
                'lat': 'mean',
                'lon': 'mean',
                'charge_count': 'sum',
                'utilization': 'mean'
            }).reset_index()

            df = pd.DataFrame({
                'id': zone_agg['zone_id'].astype(str),
                'name': zone_agg['zone_id'].astype(str).apply(lambda x: f"交通小区 {x}"),
                'address': zone_agg['zone_id'].astype(str).apply(lambda x: f"深圳交通小区 {x}"),
                'lat': zone_agg['lat'],
                'lon': zone_agg['lon'],
                'type': '快充',
                'power': zone_agg['charge_count'].fillna(1).astype(int),
                'utilization': zone_agg['utilization'].clip(0, 1),
                'price': 1.5,
                'available_slots': (zone_agg['charge_count'] * (1 - zone_agg['utilization'])).astype(int),
                'status': '在线',
                'distance': 0,
                'district': zone_agg['zone_id'].astype(str)
            })

            e_price_path = os.path.join(data_dir, 'zone-level', 'e_price.csv')
            if os.path.exists(e_price_path):
                try:
                    e_price = pd.read_csv(e_price_path, parse_dates=['time'])
                    e_price.set_index('time', inplace=True)
                    avg_price_latest = e_price.iloc[-1].mean()
                    df['price'] = round(avg_price_latest, 2)
                except:
                    pass

            df = df.dropna(subset=['lat', 'lon', 'utilization'])
            print(f"区域级数据加载成功：{len(df)} 个交通小区（应为275个）")
            return df

        except Exception as e:
            print(f"加载区域级数据失败: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

    def load_station_data(self, data_dir=None):
        """
        加载深圳站级数据（1423个充电站）
        Args:
            data_dir: 数据目录，默认使用 DATA_DIR
        Returns:
            DataFrame: 包含经度、纬度、利用率等字段
        """
        if data_dir is None:
            data_dir = DATA_DIR

        try:
            occ_path = os.path.join(data_dir, 'station-level', 'station_occupancy_1h.csv')
            inf_path_candidates = [
                os.path.join(data_dir, 'station-level', 'station_inf.csv'),
                os.path.join(data_dir, 'station-level', 'features', 'station_inf.csv')
            ]
            inf_path = None
            for p in inf_path_candidates:
                if os.path.exists(p):
                    inf_path = p
                    break
            if inf_path is None:
                print(f"站级静态信息文件不存在，尝试路径: {inf_path_candidates}")
                return pd.DataFrame()

            if not os.path.exists(occ_path):
                print(f"站级利用率宽表不存在: {occ_path}")
                return pd.DataFrame()

            occ_df = pd.read_csv(occ_path, index_col=0)
            latest_util = occ_df.iloc[-1].values
            station_ids = occ_df.columns.tolist()

            station_inf = pd.read_csv(inf_path)
            station_inf['station_id'] = station_inf['station_id'].astype(str)

            valid_stations = [sid for sid in station_inf['station_id'] if sid in station_ids]
            station_inf_filtered = station_inf[station_inf['station_id'].isin(valid_stations)].copy()

            station_inf_filtered['order'] = station_inf_filtered['station_id'].map(
                {sid: idx for idx, sid in enumerate(station_ids)}
            )
            station_inf_filtered = station_inf_filtered.sort_values('order').drop('order', axis=1)

            util_aligned = []
            for sid in station_inf_filtered['station_id']:
                col_idx = occ_df.columns.get_loc(sid)
                util_aligned.append(latest_util[col_idx])

            df = pd.DataFrame({
                'id': station_inf_filtered['station_id'],
                'name': station_inf_filtered['station_id'],
                'address': '',
                'lat': station_inf_filtered['latitude'],
                'lon': station_inf_filtered['longitude'],
                'type': '快充',
                'power': station_inf_filtered['charge_count'].fillna(1).astype(int),
                'utilization': np.clip(util_aligned, 0, 1),
                'price': 1.5,
                'available_slots': (station_inf_filtered['charge_count'] * (1 - np.clip(util_aligned, 0, 1))).astype(
                    int),
                'status': '在线',
                'distance': 0,
                'district': '深圳市'
            })

            df = df.dropna(subset=['lat', 'lon', 'utilization'])
            print(f"站级数据加载成功：{len(df)} 个充电站")
            return df

        except Exception as e:
            print(f"加载站级数据失败: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

    def load_station_occupancy(self, data_dir=None):
        if data_dir is None:
            data_dir = DATA_DIR
        occ_path = os.path.join(data_dir, 'station-level', 'station_occupancy_1h.csv')
        if not os.path.exists(occ_path):
            print(f"站级宽表不存在: {occ_path}")
            return pd.DataFrame()
        occ_df = pd.read_csv(occ_path, index_col=0)
        occ_df.index = pd.to_datetime(occ_df.index)
        return occ_df

    def load_charger_data(self, use_cache=True, cache_hours=24):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(base_dir)
        data_dir = os.path.join(project_root, 'data')

        if self.use_real_data:
            if self.data_level == 'station':
                print("政府端使用深圳站级数据")
                df = self.load_station_data(data_dir=data_dir)
            else:
                print("政府端使用深圳区域级数据")
                df = self.load_shenzhen_data(data_dir=data_dir)
            if not df.empty:
                return df
            else:
                print("深圳数据加载失败，回退到北京模拟数据")
                return self.generate_simulated_data(39.9, 116.4, 150)
        else:
            print("政府端使用北京模拟数据")
            return self.generate_simulated_data(39.9, 116.4, 150)

    def filter_data(self, data, selected_type):
        if selected_type in ['快充', '慢充']:
            return data[data['type'] == selected_type]
        return data

    def simple_demand_prediction(self, data):
        if data.empty:
            return []
        center_lat, center_lon = 22.5431, 114.0579
        preds = []
        for _, row in data.iterrows():
            d = self._haversine(center_lat, center_lon, row['lat'], row['lon'])
            factor = max(0.1, 1 - d / 10)
            pred = row['utilization'] * 0.6 + factor * 0.4
            preds.append(min(0.95, pred))
        return preds

    @st.cache_resource
    def _load_lstm_model(_self, model_path, seq_len, n_fea, num_nodes):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        model = baselines.Lstm(seq=seq_len, n_fea=n_fea, node=num_nodes).to(device)  # ← 改这里
        state_dict = torch.load(model_path, map_location=device, weights_only=True)
        model.load_state_dict(state_dict)
        model.eval()
        return model, device

    def predict_station_lstm(self, station_occ_df, model_path, seq_len=48, pred_len=3, stations=None):
        if not isinstance(station_occ_df.index, pd.DatetimeIndex):
            try:
                station_occ_df.index = pd.to_datetime(station_occ_df.index)
            except Exception:
                raise ValueError("输入数据的索引不是时间格式，请确保传入的是时间序列数据。")

        # 只算需要的站点：模型是「逐站点参数共享」的（无 node embedding），
        # 因此裁剪列后结果与全量计算完全一致（实测差异 ~3e-8，float32 噪声）。
        # 这一步同时把特征构造从 1423 列降到 1 列（0.57s -> 0.01s）。
        if stations is not None:
            cols = [c for c in stations if c in station_occ_df.columns]
            if cols:
                station_occ_df = station_occ_df[cols]

        if station_occ_df.shape[1] < 1:
            raise ValueError("无可用站点列，无法进行站级预测。")

        # 只读所需站点的电价列（按需缓存），避免每次重读 341MB 全表
        e_price_df = _load_price_columns(tuple(str(c) for c in station_occ_df.columns))
        if e_price_df is None or e_price_df.shape[1] == 0:
            e_price_df = pd.DataFrame(1.5, index=station_occ_df.index, columns=station_occ_df.columns)
            print("⚠️ 未取到电价列，使用默认电价 1.5 元/度")

        weather_df = _load_weather_cached()
        if weather_df is None:
            weather_df = pd.DataFrame({'nRAIN': 0.0}, index=station_occ_df.index)
            print("⚠️ 未找到天气文件，使用降雨量 0")

        occ_seq, extra_feat_seq, _ = build_lstm_input_features(
            station_occ_df, e_price_df, weather_df, seq_len
        )

        num_nodes = station_occ_df.shape[1]
        n_fea = 1 + 8
        model, device = self._load_lstm_model(model_path, seq_len, n_fea, num_nodes)

        occ_tensor = torch.Tensor(occ_seq).unsqueeze(0).permute(0, 2, 1)
        extra_tensor = torch.Tensor(extra_feat_seq).unsqueeze(0).permute(0, 2, 1, 3)

        with torch.no_grad():
            pred = model(occ_tensor, extra_tensor)

        pred_np = np.atleast_1d(pred.detach().cpu().numpy().reshape(-1))
        station_ids = station_occ_df.columns.tolist()
        current_utils = station_occ_df.iloc[-1].values

        result_df = pd.DataFrame({
            'station_id': station_ids,
            'current_utilization': current_utils,
            'predicted_utilization': pred_np
        })
        result_df['predicted_utilization'] = result_df['predicted_utilization'].clip(0, 1)
        return result_df

    def predict_station_to_region(self, station_pred_df, station_inf_df):
        merged = pd.merge(station_pred_df, station_inf_df[['station_id', 'TAZID']], on='station_id', how='left')
        region_agg = merged.groupby('TAZID').agg({
            'current_utilization': 'mean',
            'predicted_utilization': 'mean'
        }).reset_index()
        region_agg.rename(columns={'TAZID': 'zone_id'}, inplace=True)
        return region_agg

    def predict_region_lstm(self, region_occ_df, model_path, seq_len=48, pred_len=3):
        if not isinstance(region_occ_df.index, pd.DatetimeIndex):
            region_occ_df.index = pd.to_datetime(region_occ_df.index)

        # ✅ 使用 path_manager 常量
        e_price_path = ZONE_EPRICE_PATH
        weather_path = ZONE_WEATHER_PATH

        # 读取全市平均电价
        if os.path.exists(e_price_path):
            e_price_df = pd.read_csv(e_price_path, parse_dates=['time'])
            e_price_df.set_index('time', inplace=True)
            e_price_avg = e_price_df.mean(axis=1)
            e_price_avg = pd.DataFrame(e_price_avg, columns=['avg_price'])
            e_price_avg.index = pd.to_datetime(e_price_avg.index)
        else:
            e_price_avg = pd.DataFrame(1.5, index=region_occ_df.index, columns=['avg_price'])
            print("⚠️ 未找到电价文件，使用默认电价 1.5 元/度")

        if os.path.exists(weather_path):
            weather_df = pd.read_csv(weather_path, index_col='time')
            weather_df.index = pd.to_datetime(weather_df.index)
        else:
            weather_df = pd.DataFrame({'nRAIN': 0.0}, index=region_occ_df.index)
            print("⚠️ 未找到天气文件，使用降雨量 0")

        from common.lstm_utils import build_lstm_input_features_region
        occ_seq, extra_feat_seq, _ = build_lstm_input_features_region(
            region_occ_df, e_price_avg, weather_df, seq_len
        )

        num_nodes = region_occ_df.shape[1]
        n_fea = 1 + 2
        model, device = self._load_lstm_model(model_path, seq_len, n_fea, num_nodes)

        occ_tensor = torch.Tensor(occ_seq).unsqueeze(0).permute(0, 2, 1)
        extra_tensor = torch.Tensor(extra_feat_seq).unsqueeze(0).permute(0, 2, 1, 3)

        with torch.no_grad():
            pred = model(occ_tensor, extra_tensor)

        pred_np = pred.cpu().numpy()
        zone_ids = region_occ_df.columns.tolist()
        current_utils = region_occ_df.iloc[-1].values

        return pd.DataFrame({
            'zone_id': zone_ids,
            'current_utilization': current_utils,
            'predicted_utilization': pred_np
        })

    def load_zone_occupancy(self, data_dir='./data/'):
        """加载区域级时间序列宽表（275 个交通小区 × 时间）"""
        occ_path = os.path.join(data_dir, 'zone-level', 'occupancy.csv')
        if not os.path.exists(occ_path):
            print(f"区域级宽表不存在: {occ_path}")
            return pd.DataFrame()
        occ_df = pd.read_csv(occ_path, index_col=0)
        occ_df.index = pd.to_datetime(occ_df.index)
        return occ_df