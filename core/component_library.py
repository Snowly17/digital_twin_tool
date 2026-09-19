"""
core/component_library.py

组件库 - 定义所有可拖拽组件的参数
负责：定义每种组件的3D几何体生成函数、默认尺寸、颜色、图标等
"""

import uuid
from typing import Dict, Any, Callable, List, Optional
from dataclasses import dataclass, field


@dataclass
class ComponentDefinition:
    """组件的完整定义"""
    id: str                                      # 组件唯一标识
    type: str                                    # 组件类型 (charger_fast, charger_slow, building, tree, etc.)
    name: str                                    # 显示名称
    category: str                                # 分类: '充电设备', '建筑', '景观', '道路'
    icon: str                                    # 图标 (emoji 或 Unicode)
    description: str                             # 简短描述
    default_scale: Dict[str, float]              # 默认缩放 {x, y, z}
    default_color: str                           # 默认颜色 (hex)
    generate_func: Callable                      # 3D生成函数 (接收参数, 返回 THREE.Group)


class ComponentLibrary:
    """组件库 - 管理所有可拖拽组件"""

    def __init__(self):
        self._components: Dict[str, ComponentDefinition] = {}
        self._register_default_components()

    def _register_default_components(self):
        """注册所有默认组件"""
        # ========== 充电设备 ==========
        self.register(ComponentDefinition(
            id="charger_fast",
            type="charger_fast",
            name="快充桩",
            category="充电设备",
            icon="⚡",
            description="直流快充桩，功率 120kW",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#4a90d9",
            generate_func=self._create_charger_fast
        ))

        self.register(ComponentDefinition(
            id="charger_slow",
            type="charger_slow",
            name="慢充桩",
            category="充电设备",
            icon="🔋",
            description="交流慢充桩，功率 7kW",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#5cb85c",
            generate_func=self._create_charger_slow
        ))

        self.register(ComponentDefinition(
            id="charger_super",
            type="charger_super",
            name="超充桩",
            category="充电设备",
            icon="🚀",
            description="超级快充桩，功率 250kW",
            default_scale={"x": 1.2, "y": 1.2, "z": 1.2},
            default_color="#9b59b6",
            generate_func=self._create_charger_super
        ))

        # ========== 建筑 ==========
        self.register(ComponentDefinition(
            id="building",
            type="building",
            name="建筑",
            category="建筑",
            icon="🏢",
            description="标准建筑，可调整尺寸",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#6a8a9a",
            generate_func=self._create_building
        ))

        self.register(ComponentDefinition(
            id="building_tall",
            type="building_tall",
            name="高层建筑",
            category="建筑",
            icon="🏙️",
            description="高层办公楼",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#5a7a9a",
            generate_func=self._create_building_tall
        ))

        # ========== 景观 ==========
        self.register(ComponentDefinition(
            id="tree",
            type="tree",
            name="树木",
            category="景观",
            icon="🌳",
            description="景观树",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#3a8a3a",
            generate_func=self._create_tree
        ))

        self.register(ComponentDefinition(
            id="tree_pine",
            type="tree_pine",
            name="松树",
            category="景观",
            icon="🌲",
            description="塔形松树",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a6a2a",
            generate_func=self._create_tree_pine
        ))

        self.register(ComponentDefinition(
            id="lamp",
            type="lamp",
            name="路灯",
            category="景观",
            icon="💡",
            description="太阳能路灯",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#f1c40f",
            generate_func=self._create_lamp
        ))

        # ========== 道路 ==========
        self.register(ComponentDefinition(
            id="road_straight",
            type="road_straight",
            name="直线道路",
            category="道路",
            icon="🛣️",
            description="带虚线的标准直道",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#555555",
            generate_func=self._make_glb_func("road_straight")
        ))

        self.register(ComponentDefinition(
            id="road_curve",
            type="road_curve",
            name="弯道",
            category="道路",
            icon="↩️",
            description="弧形弯道",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#555555",
            generate_func=self._make_glb_func("road_curve")
        ))

        # ========== 车辆 ==========
        self.register(ComponentDefinition(
            id="car",
            type="car",
            name="轿车",
            category="车辆",
            icon="🚗",
            description="标准轿车模型",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#e74c3c",
            generate_func=self._create_car
        ))

        self.register(ComponentDefinition(
            id="truck",
            type="truck",
            name="货车",
            category="车辆",
            icon="🚚",
            description="厢式货车",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#3498db",
            generate_func=self._create_truck
        ))

        # ========== ☀️ 绿色能源 - 光伏板 ==========
        self.register(ComponentDefinition(
            id="solar_panel_flat",
            type="solar_panel_flat",
            name="平铺光伏板",
            category="绿色能源",
            icon="☀️",
            description="铺在车棚顶或地面的光伏板",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a5a8a",
            generate_func=self._make_glb_func("solar_panel_flat")
        ))

        self.register(ComponentDefinition(
            id="solar_panel_land",
            type="solar_panel_land",
            name="横向光伏板",
            category="绿色能源",
            icon="☀️",
            description="单块横向安装的光伏板",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a5a8a",
            generate_func=self._make_glb_func("solar_panel_land")
        ))

        self.register(ComponentDefinition(
            id="solar_panel_group",
            type="solar_panel_group",
            name="光伏阵列",
            category="绿色能源",
            icon="☀️",
            description="成排布置的光伏板阵列",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a5a8a",
            generate_func=self._make_glb_func("solar_panel_group")
        ))

        self.register(ComponentDefinition(
            id="solar_panel_port",
            type="solar_panel_port",
            name="纵向光伏板",
            category="绿色能源",
            icon="☀️",
            description="垂直安装的光伏板",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a5a8a",
            generate_func=self._make_glb_func("solar_panel_port")
        ))

        self.register(ComponentDefinition(
            id="solar_panel_port_group",
            type="solar_panel_port_group",
            name="纵向光伏阵列",
            category="绿色能源",
            icon="☀️",
            description="成排垂直安装的光伏阵列",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#2a5a8a",
            generate_func=self._make_glb_func("solar_panel_port_group")
        ))

        # ========== 📦 绿色能源 - 储能集装箱 ==========
        self.register(ComponentDefinition(
            id="container_a",
            type="container_a",
            name="储能集装箱 A",
            category="绿色能源",
            icon="📦",
            description="标准储能集装箱（蓝）",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#3a5a8a",
            generate_func=self._make_glb_func("container_a")
        ))

        self.register(ComponentDefinition(
            id="container_b",
            type="container_b",
            name="储能集装箱 B",
            category="绿色能源",
            icon="📦",
            description="标准储能集装箱（灰）",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#5a5a5a",
            generate_func=self._make_glb_func("container_b")
        ))

        self.register(ComponentDefinition(
            id="container_c",
            type="container_c",
            name="储能集装箱 C",
            category="绿色能源",
            icon="📦",
            description="标准储能集装箱（绿）",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#3a6a4a",
            generate_func=self._make_glb_func("container_c")
        ))

        # ========== 🌬️ 绿色能源 - 风机 ==========
        self.register(ComponentDefinition(
            id="windmill",
            type="windmill",
            name="风力发电机",
            category="绿色能源",
            icon="🌬️",
            description="标准风力发电机",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#cccccc",
            generate_func=self._make_glb_func("windmill")
        ))

        self.register(ComponentDefinition(
            id="windmill_low",
            type="windmill_low",
            name="风力发电机（低模）",
            category="绿色能源",
            icon="🌬️",
            description="低多边形版本，性能优先",
            default_scale={"x": 1.0, "y": 1.0, "z": 1.0},
            default_color="#cccccc",
            generate_func=self._make_glb_func("windmill_low")
        ))

    def register(self, component: ComponentDefinition):
        """注册组件"""
        self._components[component.id] = component

    def get(self, component_id: str) -> Optional[ComponentDefinition]:
        """获取组件定义"""
        return self._components.get(component_id)

    def get_all(self) -> Dict[str, ComponentDefinition]:
        """获取所有组件"""
        return self._components

    def get_by_category(self, category: str) -> List[ComponentDefinition]:
        """按分类获取组件列表"""
        return [c for c in self._components.values() if c.category == category]

    def get_categories(self) -> List[str]:
        """获取所有分类"""
        categories = set()
        for c in self._components.values():
            categories.add(c.category)
        return list(categories)

    def create_instance(self, component_id: str, **kwargs) -> Dict[str, Any]:
        """
        创建组件实例 (用于添加到场景中)

        Args:
            component_id: 组件ID
            **kwargs: 可覆盖的属性 (position, scale, color, bind_station_id)

        Returns:
            Dict: 场景对象字典
        """
        comp = self.get(component_id)
        if not comp:
            raise ValueError(f"组件 '{component_id}' 不存在")

        # 合并默认值
        result = {
            "id": str(uuid.uuid4()),
            "type": comp.type,
            "name": comp.name,
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": comp.default_scale.copy(),
            "bind_station_id": None,
            "custom_props": {
                "color": comp.default_color,
                "category": comp.category
            }
        }

        # 覆盖传入的参数
        for key, value in kwargs.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key].update(value)
            else:
                result[key] = value

        return result

    # ==================== 3D 生成函数 (供前端使用) ====================
    # 注意：这些函数返回的是 JSON 可序列化的 "场景对象描述"
    # 前端 Three.js 根据这些描述来实际创建 3D 模型

    def _create_charger_fast(self, **params) -> Dict:
        """快充桩 - 蓝色科技感"""
        return {
            "type": "charger_fast",
            "geometry": "box",
            "params": {
                "width": 0.3,
                "height": 2.0,
                "depth": 0.3,
                "color": "#4a90d9",
                "emissive": "#4a90d9",
                "emissiveIntensity": 0.15
            },
            "top": {
                "type": "sphere",
                "radius": 0.12,
                "color": "#4a90d9",
                "emissive": "#4a90d9",
                "emissiveIntensity": 0.3
            },
            "ring": {
                "type": "torus",
                "radius": 0.22,
                "tube": 0.025,
                "color": "#4a90d9",
                "emissive": "#4a90d9",
                "emissiveIntensity": 0.5,
                "opacity": 0.6
            }
        }

    def _create_charger_slow(self, **params) -> Dict:
        """慢充桩 - 绿色简约"""
        return {
            "type": "charger_slow",
            "geometry": "box",
            "params": {
                "width": 0.25,
                "height": 1.5,
                "depth": 0.25,
                "color": "#5cb85c",
                "emissive": "#5cb85c",
                "emissiveIntensity": 0.1
            },
            "top": {
                "type": "sphere",
                "radius": 0.1,
                "color": "#5cb85c",
                "emissive": "#5cb85c",
                "emissiveIntensity": 0.2
            },
            "ring": {
                "type": "torus",
                "radius": 0.18,
                "tube": 0.02,
                "color": "#5cb85c",
                "emissive": "#5cb85c",
                "emissiveIntensity": 0.3,
                "opacity": 0.5
            }
        }

    def _create_charger_super(self, **params) -> Dict:
        """超充桩 - 紫色豪华"""
        return {
            "type": "charger_super",
            "geometry": "box",
            "params": {
                "width": 0.35,
                "height": 2.5,
                "depth": 0.35,
                "color": "#9b59b6",
                "emissive": "#9b59b6",
                "emissiveIntensity": 0.2
            },
            "top": {
                "type": "sphere",
                "radius": 0.15,
                "color": "#9b59b6",
                "emissive": "#9b59b6",
                "emissiveIntensity": 0.4
            },
            "ring": {
                "type": "torus",
                "radius": 0.28,
                "tube": 0.03,
                "color": "#9b59b6",
                "emissive": "#9b59b6",
                "emissiveIntensity": 0.6,
                "opacity": 0.7
            }
        }

    def _create_building(self, **params) -> Dict:
        """标准建筑"""
        return {
            "type": "building",
            "geometry": "box",
            "params": {
                "width": 2.0,
                "height": 3.0,
                "depth": 1.5,
                "color": "#6a8a9a",
                "roughness": 0.6,
                "metalness": 0.2
            },
            "windows": True,  # 前端可选是否添加窗户纹理
            "window_color": "#f1c40f",
            "window_intensity": 0.3
        }

    def _create_building_tall(self, **params) -> Dict:
        """高层建筑"""
        return {
            "type": "building_tall",
            "geometry": "box",
            "params": {
                "width": 1.5,
                "height": 6.0,
                "depth": 1.5,
                "color": "#5a7a9a",
                "roughness": 0.4,
                "metalness": 0.3
            },
            "windows": True,
            "window_color": "#f1c40f",
            "window_intensity": 0.4
        }

    def _create_tree(self, **params) -> Dict:
        """树木 - 球形树冠"""
        return {
            "type": "tree",
            "trunk": {
                "geometry": "cylinder",
                "params": {
                    "radius": 0.08,
                    "height": 0.6,
                    "color": "#5a3a2a",
                    "roughness": 0.9
                }
            },
            "crown": {
                "geometry": "sphere",
                "params": {
                    "radius": 0.5,
                    "color": "#3a8a3a",
                    "roughness": 0.8
                }
            }
        }

    def _create_tree_pine(self, **params) -> Dict:
        """松树 - 塔形"""
        return {
            "type": "tree_pine",
            "trunk": {
                "geometry": "cylinder",
                "params": {
                    "radius": 0.06,
                    "height": 0.8,
                    "color": "#5a3a2a",
                    "roughness": 0.9
                }
            },
            "crowns": [
                {
                    "geometry": "cone",
                    "params": {
                        "radius": 0.5,
                        "height": 0.6,
                        "color": "#2a6a2a",
                        "roughness": 0.8
                    },
                    "y_offset": 0.6
                },
                {
                    "geometry": "cone",
                    "params": {
                        "radius": 0.35,
                        "height": 0.5,
                        "color": "#2a7a2a",
                        "roughness": 0.8
                    },
                    "y_offset": 1.0
                },
                {
                    "geometry": "cone",
                    "params": {
                        "radius": 0.2,
                        "height": 0.4,
                        "color": "#3a8a3a",
                        "roughness": 0.8
                    },
                    "y_offset": 1.3
                }
            ]
        }

    def _create_lamp(self, **params) -> Dict:
        """路灯"""
        return {
            "type": "lamp",
            "pole": {
                "geometry": "cylinder",
                "params": {
                    "radius": 0.03,
                    "height": 1.2,
                    "color": "#888888",
                    "metalness": 0.6
                }
            },
            "arm": {
                "geometry": "box",
                "params": {
                    "width": 0.3,
                    "height": 0.02,
                    "depth": 0.02,
                    "color": "#888888",
                    "metalness": 0.6
                },
                "x_offset": 0.15
            },
            "light": {
                "geometry": "sphere",
                "params": {
                    "radius": 0.06,
                    "color": "#f1c40f",
                    "emissive": "#f1c40f",
                    "emissiveIntensity": 0.8
                },
                "x_offset": 0.3
            }
        }

    def _create_road_straight(self, **params) -> Dict:
        """直线道路"""
        return {
            "type": "road_straight",
            "geometry": "box",
            "params": {
                "width": 1.0,
                "height": 0.05,
                "depth": 0.3,
                "color": "#555555",
                "roughness": 0.9
            },
            "line": {
                "type": "box",
                "params": {
                    "width": 0.02,
                    "height": 0.06,
                    "depth": 0.25,
                    "color": "#ffffff"
                },
                "z_offset": 0
            }
        }

    def _create_road_curve(self, **params) -> Dict:
        """弧形弯道"""
        return {
            "type": "road_curve",
            "geometry": "torus_segment",
            "params": {
                "radius": 1.0,
                "tube": 0.15,
                "arc": 1.57,  # 90度
                "color": "#555555",
                "roughness": 0.9
            }
        }

    def _create_car(self, **params) -> Dict:
        """轿车"""
        return {
            "type": "car",
            "body": {
                "geometry": "box",
                "params": {
                    "width": 0.8,
                    "height": 0.2,
                    "depth": 0.4,
                    "color": "#e74c3c",
                    "roughness": 0.3,
                    "metalness": 0.6
                }
            },
            "cabin": {
                "geometry": "box",
                "params": {
                    "width": 0.4,
                    "height": 0.15,
                    "depth": 0.25,
                    "color": "#3498db",
                    "roughness": 0.2,
                    "metalness": 0.1
                },
                "y_offset": 0.175
            },
            "wheels": {
                "radius": 0.06,
                "width": 0.04,
                "color": "#222222"
            }
        }

    def _create_truck(self, **params) -> Dict:
        """货车"""
        return {
            "type": "truck",
            "cab": {
                "geometry": "box",
                "params": {
                    "width": 0.5,
                    "height": 0.2,
                    "depth": 0.3,
                    "color": "#3498db",
                    "roughness": 0.3,
                    "metalness": 0.4
                }
            },
            "cargo": {
                "geometry": "box",
                "params": {
                    "width": 0.7,
                    "height": 0.35,
                    "depth": 0.5,
                    "color": "#ecf0f1",
                    "roughness": 0.7
                },
                "x_offset": 0.25
            }
        }

    # ==================== GLB 组件辅助函数 ====================

    def _make_glb_func(self, comp_type: str) -> Callable:
        """
        为 GLB 模型组件创建生成函数（前端会优先从 modelMap 加载 GLB）
        这里返回的只是一个占位描述，仅在 GLB 加载失败时作为 fallback 使用
        """

        def _func(**params) -> Dict:
            return self._create_glb_placeholder(_type=comp_type, **params)

        return _func

    def _create_glb_placeholder(self, _type: str = "glb_component", **params) -> Dict:
        """
        GLB 组件的占位描述
        前端加载顺序：modelMap[type] → GLB → 失败则用本函数返回的描述程序化生成
        """
        return {
            "type": _type,
            "geometry": "box",
            "params": {
                "width": 1.0,
                "height": 1.0,
                "depth": 1.0,
                "color": "#888888",
                "roughness": 0.6,
                "metalness": 0.2
            }
        }




# ==================== 全局实例 ====================
_component_library = None


def get_component_library() -> ComponentLibrary:
    """获取组件库单例"""
    global _component_library
    if _component_library is None:
        _component_library = ComponentLibrary()
    return _component_library


# ==================== 使用示例 ====================
if __name__ == "__main__":
    lib = get_component_library()

    print("📦 组件库分类:")
    for category in lib.get_categories():
        print(f"  - {category}")
        comps = lib.get_by_category(category)
        for c in comps:
            print(f"      {c.icon} {c.name}: {c.description}")

    print(f"\n📊 总计: {len(lib.get_all())} 个组件")

    # 创建组件实例
    instance = lib.create_instance("charger_fast", position={"x": 5, "y": 0, "z": 3})
    print(f"\n🔌 快充桩实例: {instance}")