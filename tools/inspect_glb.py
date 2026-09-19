"""量取 GLB 的包围盒与网格数（只依赖标准库）"""
import json
import os
import struct
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def read_glb(path):
    with open(path, 'rb') as f:
        buf = f.read()
    magic, ver, total = struct.unpack_from('<III', buf, 0)
    assert magic == 0x46546C67, '不是 GLB'
    jlen, jtype = struct.unpack_from('<II', buf, 12)
    js = json.loads(buf[20:20 + jlen].decode('utf-8'))
    boff = 20 + jlen
    blen, btype = struct.unpack_from('<II', buf, boff)
    bin_data = buf[boff + 8: boff + 8 + blen]
    return js, bin_data


def accessor_minmax(js, bin_data, idx):
    acc = js['accessors'][idx]
    if 'min' in acc and 'max' in acc:
        return acc['min'], acc['max']
    # 没写 min/max 就自己算
    bv = js['bufferViews'][acc['bufferView']]
    off = bv.get('byteOffset', 0) + acc.get('byteOffset', 0)
    stride = bv.get('byteStride') or 12
    n = acc['count']
    lo = [1e30] * 3
    hi = [-1e30] * 3
    for i in range(n):
        p = struct.unpack_from('<fff', bin_data, off + i * stride)
        for k in range(3):
            lo[k] = min(lo[k], p[k])
            hi[k] = max(hi[k], p[k])
    return lo, hi


def inspect(path):
    js, bin_data = read_glb(path)
    name = os.path.basename(path)
    meshes = js.get('meshes', [])
    prims = sum(len(m.get('primitives', [])) for m in meshes)
    tris = 0
    lo = [1e30] * 3
    hi = [-1e30] * 3
    for m in meshes:
        for p in m.get('primitives', []):
            ai = p['attributes'].get('POSITION')
            if ai is None:
                continue
            mn, mx = accessor_minmax(js, bin_data, ai)
            for k in range(3):
                lo[k] = min(lo[k], mn[k])
                hi[k] = max(hi[k], mx[k])
            if 'indices' in p:
                tris += js['accessors'][p['indices']]['count'] // 3
            else:
                tris += js['accessors'][ai]['count'] // 3
    size = [round(hi[k] - lo[k], 3) for k in range(3)]
    print('%-26s 网格%2d prim%2d 三角%5d  尺寸(w,h,d)=%-24s 底=%.3f  min=%s'
          % (name, len(meshes), prims, tris, size, lo[1],
             [round(v, 2) for v in lo]))


if __name__ == '__main__':
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = sys.argv[1:] or [
        'solar_panel_group.glb', 'solar_panel_land.glb',
        'windmill.glb', 'container_a.glb', 'car.glb',
        'charger_dc.glb', 'traffic-light.glb',
    ]
    for t in targets:
        p = t if os.path.isabs(t) else os.path.join(root, 'static', 'models', t)
        try:
            inspect(p)
        except Exception as e:
            print('%-26s !! %s' % (os.path.basename(p), e))
