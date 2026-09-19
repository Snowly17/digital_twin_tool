# 3D 模型素材出处与授权

本目录下 **24 个 `.glb` 模型**为「智孪」场景内置素材，模型总体积约 1.93 MB。

## 一、素材来源（候选）

| 来源 | 作者 | 授权 | 链接 |
|---|---|---|---|
| City Kit (Industrial) | Kenney | CC0 1.0（公共领域） | https://kenney.nl/assets/city-kit-industrial |
| Car Kit | Kenney | CC0 1.0（公共领域） | https://kenney.nl/assets/car-kit |
| City Pack | J-Toastie | CC0（公共领域） | https://poly.pizza/bundle/City-Pack-kJqRAIGsw0 |

作者主页：Kenney <https://kenney.nl/>；J-Toastie <https://poly.pizza/u/J-Toastie/Lists>

## 二、授权说明

以上素材均为 **CC0 1.0（公共领域献赠）**：允许自由使用、修改、再分发与商业使用，**不要求署名**。
本项目仍在此列明出处，以满足学术作品的引用规范。

## 三、格式转换说明

原始素材为 OBJ / FBX 等格式，统一经 **obj2gltf / FBX2glTF / UnityGLTF** 转换为 glTF 2.0（`.glb`）
后由前端 GLTFLoader 本地加载（`/app/static/models/`）。**原始几何、UV 与贴图未作修改。**

## 四、⚠️ 逐文件出处：仍待确认（附可用的判定线索）

`.glb` 文件内部**不含**版权 / 作者元数据。已用脚本解析全部 24 个文件的 `asset` 字段，
只能取到**转换器名**：

| 转换器 | 个数 | 对应批次 |
|---|---|---|
| obj2gltf | 8 | 批次 A |
| FBX2glTF v0.9.7 | 6 | 批次 B |
| UnityGLTF | 10 | 批次 C |

**这 3 组很可能对应 3 个不同的来源批次**（同一批素材通常走同一条转换流水线）。
因此核对时可以按批次分组确认，而不必逐个猜：

| 文件 | 体积 | 转换器（文件内记录） | 批次 | 来源（待确认） |
|---|---|---|---|---|
| `bench.glb` | 12 KB | obj2gltf | A | 待确认 |
| `bus.glb` | 45 KB | obj2gltf | A | 待确认 |
| `car.glb` | 161 KB | obj2gltf | A | 待确认 |
| `car-suv.glb` | 177 KB | obj2gltf | A | 待确认 |
| `road_curve.glb` | 14 KB | obj2gltf | A | 待确认 |
| `road_straight.glb` | 15 KB | obj2gltf | A | 待确认 |
| `tree.glb` | 5 KB | obj2gltf | A | 待确认 |
| `van.glb` | 43 KB | obj2gltf | A | 待确认 |
| `building-big.glb` | 346 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `building-green.glb` | 127 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `building-red.glb` | 147 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `building-red-corner.glb` | 142 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `pickup-truck.glb` | 267 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `traffic-light.glb` | 62 KB | FBX2glTF v0.9.7 | B | 待确认 |
| `container_a.glb` | 36 KB | UnityGLTF | C | 待确认 |
| `container_b.glb` | 36 KB | UnityGLTF | C | 待确认 |
| `container_c.glb` | 36 KB | UnityGLTF | C | 待确认 |
| `solar_panel_flat.glb` | 14 KB | UnityGLTF | C | 待确认 |
| `solar_panel_group.glb` | 80 KB | UnityGLTF | C | 待确认 |
| `solar_panel_land.glb` | 21 KB | UnityGLTF | C | 待确认 |
| `solar_panel_port.glb` | 21 KB | UnityGLTF | C | 待确认 |
| `solar_panel_port_group.glb` | 80 KB | UnityGLTF | C | 待确认 |
| `windmill.glb` | 45 KB | UnityGLTF | C | 待确认 |
| `windmill_low.glb` | 45 KB | UnityGLTF | C | 待确认 |

**批次 A 的命名线索（较强）**：`car-suv`、`pickup-truck`、`van`、`bus`、
`road_straight` / `road_curve`、`traffic-light` 这类**连字符命名**与 Kenney 素材库的命名习惯一致。
尤其本项目**早期线上 URL 用的就是带连字符的 `road-straight.glb` / `road-curve.glb`**，
本地才改成下划线——这强烈提示道路与车辆部分来自 **Kenney 的道路套件 / Car Kit**。

**批次 C（光伏板 / 储能集装箱 / 风机）**属于能源基础设施题材，
在 Kenney City Kit (Industrial) 中并不典型，来源需另行确认。

> 以上是**命名与转换流水线的间接线索，不是确证**。补全方式：
> 打开对应素材包页面逐项核对后填入「来源」列，或在后续替换素材时同步登记。

## 五、前端第三方库

| 库 | 版本 | 授权 |
|---|---|---|
| three.js（含 addons） | 0.160.0 | MIT |
| @supabase/supabase-js | 2.116.0 | Apache-2.0 |
| Streamlit | 1.63.0 | Apache-2.0 |

上述依赖已本地化到 `static/vendor/`，来源与字节数见 `static/vendor/manifest.json`。
