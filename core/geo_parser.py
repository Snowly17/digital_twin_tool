"""
core/geo_parser.py

地理数据解析器 —— 支持 GeoJSON / CSV 识别 点、线、面
"""
import json
import pandas as pd
from typing import List, Dict, Any


class GeoFeature:
    """地理要素"""
    def __init__(self, geom_type: str, coords, properties: Dict = None):
        self.geom_type = geom_type          # 'point' | 'line' | 'polygon'
        self.coords = coords
        self.properties = properties or {}

    def to_dict(self):
        return {
            'geom_type': self.geom_type,
            'coords': self.coords,
            'properties': self.properties
        }


def parse_geojson(data: dict) -> List[GeoFeature]:
    """解析 GeoJSON"""
    features = []
    for f in data.get('features', []):
        geom = f.get('geometry', {})
        gtype = (geom.get('type') or '').lower()

        if gtype == 'point':
            norm_type = 'point'
        elif gtype in ('linestring', 'multilinestring'):
            norm_type = 'line'
        elif gtype in ('polygon', 'multipolygon'):
            norm_type = 'polygon'
        else:
            continue

        features.append(GeoFeature(
            geom_type=norm_type,
            coords=geom.get('coordinates'),
            properties=f.get('properties', {}) or {}
        ))
    return features


def parse_csv_points(df: pd.DataFrame) -> List[GeoFeature]:
    """CSV 数据 → 统一按点处理"""
    LAT_ALIASES = ['lat', 'latitude', '纬度', 'y']
    LON_ALIASES = ['lon', 'lng', 'long', 'longitude', '经度', 'x']

    def find_col(cols, aliases):
        cols_lower = {str(c).lower().strip(): c for c in cols}
        for a in aliases:
            if a.lower() in cols_lower:
                return cols_lower[a.lower()]
        return None

    lat_col = find_col(df.columns, LAT_ALIASES)
    lon_col = find_col(df.columns, LON_ALIASES)

    if not lat_col or not lon_col:
        raise ValueError("CSV 中未找到经纬度列（支持 lat/lon、latitude/longitude、纬度/经度）")

    features = []
    for _, row in df.iterrows():
        try:
            lon = float(row[lon_col])
            lat = float(row[lat_col])
        except (ValueError, TypeError):
            continue
        features.append(GeoFeature(
            geom_type='point',
            coords=[lon, lat],
            properties=row.to_dict()
        ))
    return features


def auto_parse(file_content, file_name: str) -> List[GeoFeature]:
    """
    自动识别文件类型并解析
    支持：.geojson / .json / .csv
    """
    file_name = file_name.lower()

    if file_name.endswith('.geojson') or file_name.endswith('.json'):
        if isinstance(file_content, bytes):
            data = json.loads(file_content.decode('utf-8'))
        else:
            data = json.loads(file_content)

        # GeoJSON
        if isinstance(data, dict) and 'features' in data:
            return parse_geojson(data)
        # 普通 JSON 列表 → 按点处理
        elif isinstance(data, list):
            df = pd.DataFrame(data)
            return parse_csv_points(df)
        # 普通 JSON 字典 → 可能是单点
        elif isinstance(data, dict):
            df = pd.DataFrame([data])
            return parse_csv_points(df)

    elif file_name.endswith('.csv'):
        if isinstance(file_content, bytes):
            from io import BytesIO
            df = pd.read_csv(BytesIO(file_content))
        else:
            df = pd.read_csv(file_content)
        return parse_csv_points(df)

    raise ValueError(f"不支持的文件类型: {file_name}")


def get_feature_summary(features: List[GeoFeature]) -> Dict[str, int]:
    """统计各类型要素的数量"""
    summary = {'point': 0, 'line': 0, 'polygon': 0}
    for f in features:
        if f.geom_type in summary:
            summary[f.geom_type] += 1
    return summary