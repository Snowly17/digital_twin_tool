#!/usr/bin/env python
# check_hardcode.py - 扫描硬编码路径

import os
import re

# 要检查的文件扩展名
EXTENSIONS = ('.py', '.json', '.yaml', '.yml', '.toml')

# 正则模式
PATTERNS = [
    # Windows 绝对路径 (C:\xxx)
    (r'[\'"`]([A-Za-z]:\\[^\'"`]+)[\'"`]', 'Windows绝对路径'),
    # Linux 绝对路径 (/xxx/yyy)
    (r'[\'"`](/[a-zA-Z0-9_/\.]+)[\'"`]', 'Linux绝对路径'),
    # 包含特定关键词的硬编码字符串
    (r'[\'"`]([^"\'`]*data[^"\'`]*\.csv)[\'"`]', '硬编码CSV文件名'),
    (r'[\'"`]([^"\'`]*station_inf\.csv)[\'"`]', '硬编码站信息文件'),
    (r'[\'"`]([^"\'`]*occupancy\.csv)[\'"`]', '硬编码利用率文件'),
    (r'[\'"`]([^"\'`]*\.pth)[\'"`]', '硬编码模型文件路径'),
]

def check_file(filepath):
    issues = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except:
        return issues

    for line_no, line in enumerate(content.splitlines(), 1):
        for pattern, desc in PATTERNS:
            matches = re.findall(pattern, line)
            if matches:
                for m in matches:
                    # 过滤掉一些常见误报（如 __file__, os.path.join 等）
                    if not any(x in line for x in ['__file__', 'os.path', 'Path(', 'PROJECT_ROOT', 'sys.path']):
                        issues.append((line_no, desc, m.strip()))
    return issues

def main():
    root_dir = os.getcwd()
    print(f"🔍 扫描目录: {root_dir}\n")
    total_issues = 0
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # 跳过虚拟环境和缓存目录
        if 'venv' in dirpath or '__pycache__' in dirpath or '.git' in dirpath:
            continue
        for f in filenames:
            if f.endswith(EXTENSIONS):
                filepath = os.path.join(dirpath, f)
                issues = check_file(filepath)
                if issues:
                    print(f"📄 {filepath}")
                    for line_no, desc, matched in issues:
                        print(f"  行 {line_no}: {desc} -> '{matched}'")
                        total_issues += 1
                    print()

    if total_issues == 0:
        print("✅ 未发现明显的硬编码路径问题！")
    else:
        print(f"⚠️ 发现 {total_issues} 个可能的问题，请检查。")

if __name__ == '__main__':
    main()