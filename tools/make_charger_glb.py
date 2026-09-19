"""
tools/make_charger_glb.py

程序化生成「直流快充桩」GLB 模型 —— 只依赖 numpy 与标准库。

为什么需要它
------------
项目里 static/models/ 下有 26 个真实模型（建筑 / 车辆 / 储能箱 / 光伏板…），
**唯独没有充电桩**（主场景的充电桩一直是程序化生成的）。
结果是思索的装置课里，储能箱能用真实模型，充电桩却只能用抽象方块拼，
两种质感混在一起，观感不统一。

本脚本把充电桩也变成一个**真实 GLB 文件**，于是：
  · 装置课（dc_charger）的零件可以换成真实模型件；
  · 主场景也可以直接引用它，教学与业务场景形态一致。

输出：static/models/charger_dc.glb（二进制 glTF 2.0，PBR 材质，含包围盒）

用法
----
    python tools/make_charger_glb.py            # 生成到 static/models/
    python tools/make_charger_glb.py --out x.glb

设计说明
--------
· 尺寸按真实直流快充桩取：整机高约 1.6 m，宽 0.45 m，深 0.32 m；
· 单个 mesh 内用**材质分组**切分 primitive，因此枪座、灯带、屏幕颜色不同，
  但仍是**一个文件、一个节点**，前端加载后就是一个对象；
· 不引入 trimesh/pygltflib 等依赖：glTF 2.0 的最小可用子集很短，
  自己写反而更可控，也不会给项目增加安装负担。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys

import numpy as np

# 🔥 Windows 控制台默认 GBK，本脚本的 emoji 输出会抛 UnicodeEncodeError
#    （项目其余脚本有同样的修复，这里不能漏）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# ============================================================
# 材质（PBR metallic-roughness）
# ============================================================
MATERIALS = [
    {"name": "shell_white", "color": [0.88, 0.90, 0.93, 1.0], "metallic": 0.15, "rough": 0.45},
    {"name": "body_blue",   "color": [0.16, 0.42, 0.72, 1.0], "metallic": 0.30, "rough": 0.40},
    {"name": "dark_panel",  "color": [0.10, 0.12, 0.16, 1.0], "metallic": 0.20, "rough": 0.55},
    {"name": "led_green",   "color": [0.24, 0.85, 0.45, 1.0], "metallic": 0.0,  "rough": 0.30,
     "emissive": [0.10, 0.60, 0.24]},
    {"name": "cable_black", "color": [0.09, 0.09, 0.11, 1.0], "metallic": 0.05, "rough": 0.80},
    {"name": "metal_grey",  "color": [0.55, 0.58, 0.62, 1.0], "metallic": 0.70, "rough": 0.35},
]
MAT_INDEX = {m["name"]: i for i, m in enumerate(MATERIALS)}


# ============================================================
# 几何累加器
# ============================================================
class MeshBuilder:
    """累积按材质分组的三角网格，最后合并成一个 glTF mesh。"""

    def __init__(self):
        self.groups: dict[int, list[np.ndarray]] = {}

    def add(self, mat_name: str, verts: np.ndarray, faces: np.ndarray):
        idx = MAT_INDEX[mat_name]
        pos = np.asarray(verts, dtype=np.float64)
        f = np.asarray(faces, dtype=np.int32)
        tri = pos[f]                                   # (n, 3, 3)
        n = len(tri)
        normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.divide(normals, np.where(ln == 0, 1, ln))
        # 展开成独立三角形（最简单、也最稳，不必处理顶点合并）
        flat_pos = tri.reshape(-1, 3)
        flat_nrm = np.repeat(normals, 3, axis=0)
        self.groups.setdefault(idx, []).append(np.concatenate([flat_pos, flat_nrm], axis=1))

    def add_box(self, mat, center, size, rot_y=0.0):
        cx, cy, cz = center
        sx, sy, sz = size
        hx, hy, hz = sx / 2, sy / 2, sz / 2
        v = np.array([
            [-hx, -hy, -hz], [hx, -hy, -hz], [hx, hy, -hz], [-hx, hy, -hz],
            [-hx, -hy, hz], [hx, -hy, hz], [hx, hy, hz], [-hx, hy, hz],
        ], dtype=np.float64)
        if rot_y:
            c, s = np.cos(rot_y), np.sin(rot_y)
            v = v @ np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]).T
        v += np.array([cx, cy, cz])
        f = np.array([
            [0, 1, 2], [0, 2, 3],   # -Z
            [4, 6, 5], [4, 7, 6],   # +Z
            [0, 4, 5], [0, 5, 1],   # -Y
            [3, 2, 6], [3, 6, 7],   # +Y
            [0, 3, 7], [0, 7, 4],   # -X
            [1, 5, 6], [1, 6, 2],   # +X
        ], dtype=np.int32)
        self.add(mat, v, f)

    def add_cylinder(self, mat, center, radius, height, seg=20, rot=None):
        cx, cy, cz = center
        ang = np.linspace(0, 2 * np.pi, seg, endpoint=False)
        ring = np.stack([np.cos(ang) * radius, np.zeros(seg), np.sin(ang) * radius], axis=1)
        top = ring + np.array([0, height / 2, 0])
        bot = ring + np.array([0, -height / 2, 0])
        verts = np.concatenate([top, bot,
                                np.array([[0, height / 2, 0], [0, -height / 2, 0]])])
        ti, bi = seg * 2, seg * 2 + 1
        faces = []
        for i in range(seg):
            j = (i + 1) % seg
            faces.append([i, j, bi])          # 底面
            faces.append([seg + j, seg + i, ti])  # 顶面
            faces.append([i, seg + i, seg + j])   # 侧面
            faces.append([i, seg + j, j])
        v = verts.astype(np.float64)
        if rot is not None:
            v = v @ np.asarray(rot, dtype=np.float64).T
        v += np.array([cx, cy, cz])
        self.add(mat, v, np.asarray(faces, dtype=np.int32))

    def merged(self):
        """返回 [(materialIndex, interleaved(pos+normal) float32 数组)]"""
        out = []
        for idx in sorted(self.groups):
            out.append((idx, np.concatenate(self.groups[idx], axis=0).astype(np.float32)))
        return out


# ============================================================
# 充电桩外形
# ============================================================
def build_charger():
    """
    参考真实直流快充桩的比例：
      整机：宽 0.45 · 高 1.60 · 深 0.32（米）
      底座 → 桩身 → 面板/屏幕 → 顶部灯带 → 侧面枪座 + 线缆
    """
    b = MeshBuilder()
    W, H, D = 0.45, 1.60, 0.32

    # 底座（略宽，稳）
    b.add_box("metal_grey", (0, 0.03, 0), (W * 1.25, 0.06, D * 1.25))
    # 立柱（连接底座与桩身）
    b.add_box("metal_grey", (0, 0.18, 0), (W * 0.42, 0.24, D * 0.42))
    # 主桩身
    body_h = H * 0.72
    body_cy = 0.30 + body_h / 2
    b.add_box("shell_white", (0, body_cy, 0), (W, body_h, D))
    # 正面深色面板（屏幕区）
    b.add_box("dark_panel", (0, body_cy + body_h * 0.18, D / 2 + 0.005),
              (W * 0.72, body_h * 0.30, 0.012))
    # 面板下方的品牌色条
    b.add_box("body_blue", (0, body_cy - body_h * 0.12, D / 2 + 0.005),
              (W * 0.86, body_h * 0.10, 0.010))
    # 顶部灯带（发光）
    b.add_box("led_green", (0, 0.30 + body_h + 0.035, 0), (W * 0.80, 0.05, D * 0.80))
    # 顶盖
    b.add_box("shell_white", (0, 0.30 + body_h + 0.085, 0), (W * 0.92, 0.05, D * 0.92))
    # 侧面枪座
    b.add_box("body_blue", (W / 2 + 0.03, body_cy - body_h * 0.22, 0),
              (0.06, 0.22, D * 0.55))
    # 充电枪（插在枪座上）
    b.add_box("cable_black", (W / 2 + 0.075, body_cy - body_h * 0.22, 0),
              (0.07, 0.13, 0.09))
    # 线缆（从枪座垂到地面，用细长圆柱近似）
    b.add_cylinder("cable_black", (W / 2 + 0.06, body_cy - body_h * 0.22 - 0.30, 0.02),
                   0.022, 0.46, seg=10)
    # 背面散热格栅
    for k in range(4):
        b.add_box("dark_panel", (0, body_cy - body_h * 0.28 + k * 0.10, -D / 2 - 0.004),
                  (W * 0.70, 0.045, 0.008))
    return b


# ============================================================
# glTF 2.0 写出（最小实现：单 buffer / 单 mesh / 多 primitive）
# ============================================================
def _pad4(buf: bytes, fill: bytes = b"\x00") -> bytes:
    while len(buf) % 4:
        buf += fill
    return buf


def build_glb(prims, bounds_min, bounds_max) -> bytes:
    """
    prims: [(materialIndex, interleaved float32 (N,6) 数组)]
    顶点属性交错存放：POSITION(3) + NORMAL(3)，各 4 字节
    """
    blobs = []
    buffer_views = []
    accessors = []
    primitives = []
    offset = 0

    for mat_idx, data in prims:
        data = np.ascontiguousarray(data, dtype=np.float32)
        n_verts = data.shape[0]
        raw = data.tobytes()
        raw = _pad4(raw)
        blobs.append(raw)

        stride = 6 * 4
        # 顶点属性一个 view（交错）
        view_v = len(buffer_views)
        buffer_views.append({
            "buffer": 0, "byteOffset": offset, "byteLength": len(raw),
            "byteStride": stride, "target": 34962,          # ARRAY_BUFFER
        })
        pos_acc = len(accessors)
        accessors.append({
            "bufferView": view_v, "byteOffset": 0, "componentType": 5126,
            "count": n_verts, "type": "VEC3",
            "min": [float(x) for x in data[:, 0:3].min(axis=0)],
            "max": [float(x) for x in data[:, 0:3].max(axis=0)],
        })
        nrm_acc = len(accessors)
        accessors.append({
            "bufferView": view_v, "byteOffset": 3 * 4, "componentType": 5126,
            "count": n_verts, "type": "VEC3",
        })
        offset += len(raw)

        # 索引
        idx = np.arange(n_verts, dtype=np.uint32)
        idx_raw = _pad4(idx.tobytes())
        blobs.append(idx_raw)
        view_i = len(buffer_views)
        buffer_views.append({
            "buffer": 0, "byteOffset": offset, "byteLength": len(idx_raw),
            "target": 34963,                                 # ELEMENT_ARRAY_BUFFER
        })
        idx_acc = len(accessors)
        accessors.append({
            "bufferView": view_i, "byteOffset": 0, "componentType": 5125,
            "count": int(n_verts), "type": "SCALAR",
        })
        offset += len(idx_raw)

        primitives.append({
            "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
            "indices": idx_acc, "material": mat_idx, "mode": 4,
        })

    bin_chunk = b"".join(blobs)
    bin_chunk = _pad4(bin_chunk)

    gltf = {
        "asset": {"version": "2.0", "generator": "digital_twin_tool/make_charger_glb.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "dc_charger"}],
        "meshes": [{"name": "dc_charger", "primitives": primitives}],
        "materials": [
            {
                "name": m["name"],
                "pbrMetallicRoughness": {
                    "baseColorFactor": m["color"],
                    "metallicFactor": m["metallic"],
                    "roughnessFactor": m["rough"],
                },
                **({"emissiveFactor": m["emissive"]} if "emissive" in m else {}),
            }
            for m in MATERIALS
        ],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunk)}],
    }

    json_chunk = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")

    out = io.BytesIO()
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    out.write(struct.pack("<III", 0x46546C67, 2, total))        # magic 'glTF', ver 2
    out.write(struct.pack("<II", len(json_chunk), 0x4E4F534A))  # JSON
    out.write(json_chunk)
    out.write(struct.pack("<II", len(bin_chunk), 0x004E4942))   # BIN
    out.write(bin_chunk)
    return out.getvalue()


def main():
    ap = argparse.ArgumentParser(description="生成直流快充桩 GLB")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default=os.path.join(root, "static", "models", "charger_dc.glb"))
    args = ap.parse_args()

    b = build_charger()
    prims = b.merged()
    all_pts = np.concatenate([p[1][:, 0:3] for p in prims], axis=0)
    lo, hi = all_pts.min(axis=0), all_pts.max(axis=0)

    data = build_glb(prims, lo, hi)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(data)

    tris = sum(p[1].shape[0] // 3 for p in prims)
    print(f"✅ 已生成 {args.out}")
    print(f"   大小 {len(data) / 1024:.1f} KB | primitive {len(prims)} 个 | 三角形 {tris}")
    print(f"   包围盒 min={np.round(lo, 3).tolist()} max={np.round(hi, 3).tolist()}")
    print(f"   尺寸 {np.round(hi - lo, 3).tolist()} 米（宽 × 高 × 深）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
