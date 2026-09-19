"""导出思索数据岛，供 tools/verify_ponder.cjs 使用。

用法：
    python tools/export_payload.py [输出路径]

默认写到项目下 .tmp/ponder_payload.json（不用系统临时目录：
本环境下 Streamlit 的 AppTest 退出时会清理系统临时目录并抛权限错误，
把输出放那里会被连同清掉）。
"""
import sys, os, re, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, '.tmp', 'ponder_payload.json')
    at = AppTest.from_file(os.path.join(ROOT, 'app.py'), default_timeout=300)
    at.run()
    print('AppTest 异常数:', len(at.exception))
    for e in at.exception:
        print('  EXC:', e.value)

    html = None
    for el in at.get('iframe'):
        html = el.proto.srcdoc
        break
    if not html:
        print('!! 未找到场景 iframe')
        return 1

    m = re.search(r'id="ponder-data">(.*?)</script>', html, re.S)
    if not m:
        print('!! 未找到思索数据岛')
        return 1

    data = json.loads(m.group(1))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)

    print('已导出:', out)
    print('课件 %d | 装置 %d | 资产 %d | 工具课 %d | 对象课 %d'
          % (len(data['lessons']), len(data['devices']), len(data['deviceAssets']),
             len(data['catalogByScope']['tool']), len(data['catalogByScope']['object'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
