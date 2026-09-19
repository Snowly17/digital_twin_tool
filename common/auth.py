import sqlite3
import bcrypt
import pandas as pd
import json
import numpy as np
from core.db_utils import safe_sqlite_connect

DB_NAME = "users.db"

def init_db():
    """初始化数据库表，并为 users 表添加 email 列（如果不存在）"""
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    # 创建 users 表（基础）
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL)''')
    # 尝试添加 email 列（兼容旧数据库）
    try:
        c.execute("ALTER TABLE users ADD COLUMN email TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 创建 favorites 表
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (user_id INTEGER,
                  station_name TEXT,
                  station_data TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(user_id) REFERENCES users(id))''')
    # 创建 history 表
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (user_id INTEGER,
                  station_name TEXT,
                  station_data TEXT,
                  viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(user_id) REFERENCES users(id))''')
    conn.commit()
    conn.close()

def hash_password(password):
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def check_password(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_user(username, password, email=None):
    """注册新用户，支持可选邮箱"""
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    try:
        hashed = hash_password(password)
        c.execute("INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)",
                  (username, hashed, email))
        conn.commit()
        return True, "注册成功"
    except sqlite3.IntegrityError:
        return False, "用户名已存在"
    finally:
        conn.close()

def login_user(username, password):
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row and check_password(password, row[1]):
        return row[0]
    return None

def remove_favorite(user_id, station_name):
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM favorites WHERE user_id=? AND station_name=?", (user_id, station_name))
    conn.commit()
    conn.close()

def get_favorites(user_id):
    conn = safe_sqlite_connect(DB_NAME)
    df = pd.read_sql_query("SELECT station_data FROM favorites WHERE user_id=? ORDER BY created_at DESC", conn, params=(user_id,))
    conn.close()
    if df.empty:
        return pd.DataFrame()
    import json
    stations = [json.loads(row['station_data']) for _, row in df.iterrows()]
    return pd.DataFrame(stations)

def get_history(user_id):
    conn = safe_sqlite_connect(DB_NAME)
    df = pd.read_sql_query("SELECT station_data FROM history WHERE user_id=? ORDER BY viewed_at DESC", conn, params=(user_id,))
    conn.close()
    if df.empty:
        return pd.DataFrame()
    import json
    stations = [json.loads(row['station_data']) for _, row in df.iterrows()]
    return pd.DataFrame(stations)

def _convert_to_serializable(obj):
    """递归将 NumPy 类型转换为 Python 原生类型"""
    if isinstance(obj, (np.integer, np.int64)):
        return int(obj)
    if isinstance(obj, (np.floating, np.float64)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _convert_to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert_to_serializable(item) for item in obj]
    return obj

def save_favorite(user_id, station):
    """保存收藏（自动转换类型）"""
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favorites WHERE user_id=? AND station_name=?", (user_id, station['name']))
    if c.fetchone():
        conn.close()
        return False
    # 转换 station 为可 JSON 序列化的字典
    station_clean = _convert_to_serializable(station)
    data_json = json.dumps(station_clean, ensure_ascii=False)
    c.execute("INSERT INTO favorites (user_id, station_name, station_data) VALUES (?, ?, ?)",
              (user_id, station['name'], data_json))
    conn.commit()
    conn.close()
    return True

def add_history(user_id, station):
    """添加历史记录（自动转换类型）"""
    conn = safe_sqlite_connect(DB_NAME)
    c = conn.cursor()
    station_clean = _convert_to_serializable(station)
    data_json = json.dumps(station_clean, ensure_ascii=False)
    c.execute("INSERT INTO history (user_id, station_name, station_data) VALUES (?, ?, ?)",
              (user_id, station['name'], data_json))
    # 保留最近20条
    c.execute('''DELETE FROM history WHERE user_id = ? AND viewed_at NOT IN
                 (SELECT viewed_at FROM history WHERE user_id = ? ORDER BY viewed_at DESC LIMIT 20)''',
              (user_id, user_id))
    conn.commit()
    conn.close()