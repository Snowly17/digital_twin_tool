# core/db_utils.py
"""
通用 SQLite 连接工具：带损坏自愈机制
"""
import sqlite3
import os


def safe_sqlite_connect(db_path: str) -> sqlite3.Connection:
    """
    安全的 SQLite 连接：
    - 如果数据库文件损坏，自动删除并重建
    - 如果文件不存在，正常创建
    """
    if os.path.exists(db_path):
        try:
            # 尝试验证数据库有效性
            conn = sqlite3.connect(db_path)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            conn.commit()
            return conn
        except sqlite3.DatabaseError as e:
            # 文件损坏，删除并重建
            print(f"⚠️ 检测到损坏的数据库 {db_path}：{e}")
            try:
                conn.close()
            except Exception:
                pass
            try:
                os.remove(db_path)
                print(f"🗑️ 已删除损坏文件：{db_path}")
            except Exception as e2:
                print(f"❌ 删除失败：{e2}")

    # 正常连接（不存在则自动创建）
    return sqlite3.connect(db_path)