"""
core/scene_manager.py

场景管理器 - 数字孪生建模工具的数据底座
负责：场景数据的序列化/反序列化、模板加载、场景保存/加载、导出HTML
"""

import json
import uuid
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from core.path_manager import TEMPLATES_DIR, EXPORTS_DIR


# ==================== 数据结构定义 ====================

@dataclass
class SceneObject:
    """场景中的单个3D对象"""
    id: str  # 唯一标识 (UUID)
    type: str  # 对象类型: charger_fast, charger_slow, building, tree, road, etc.
    name: str  # 显示名称
    position: Dict[str, float]  # {x, y, z} 场景坐标
    rotation: Dict[str, float]  # {x, y, z} 旋转角度 (弧度)
    scale: Dict[str, float]  # {x, y, z} 缩放比例
    bind_station_id: Optional[str] = None  # 绑定的深圳站点ID (用于数据驱动)
    custom_props: Dict[str, Any] = field(default_factory=dict)  # 额外属性

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SceneObject":
        return cls(**data)


@dataclass
class SceneData:
    """完整的场景数据"""
    version: str = "1.0"  # 场景格式版本
    scene_name: str = "未命名场景"  # 场景名称
    created_at: str = ""  # 创建时间
    updated_at: str = ""  # 最后修改时间
    author: str = ""  # 作者
    objects: List[Dict] = field(default_factory=list)  # 对象列表 (存储为dict便于JSON序列化)
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SceneData":
        return cls(**data)


# ==================== 场景管理器 ====================

class SceneManager:
    """场景管理器 - 负责场景的创建、保存、加载、导出"""

    # 当前支持的场景格式版本
    CURRENT_VERSION = "1.0"

    def __init__(self, data_dir: str = "./exports"):
        """
        初始化场景管理器

        Args:
            data_dir: 场景文件保存目录
        """
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._current_scene: Optional[SceneData] = None

    # ==================== 场景创建 ====================

    def create_empty_scene(self, name: str = "新场景") -> SceneData:
        """创建一个空场景"""
        scene = SceneData(
            scene_name=name,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat()
        )
        self._current_scene = scene
        return scene

    def create_from_template(self, template_name: str, scene_name: str = None) -> SceneData:
        """
        从模板创建场景

        Args:
            template_name: 模板名称 (charging_station, factory_workshop, simple_office)
            scene_name: 新场景名称

        Returns:
            SceneData: 创建的场景
        """
        templates = self._load_templates()
        if template_name not in templates:
            raise ValueError(f"模板 '{template_name}' 不存在。可用模板: {list(templates.keys())}")

        template_data = templates[template_name]
        objects = template_data.get("objects", [])

        # 🔥 关键修复：如果外部模板没有定义对象（空数组），回退到内置生成器
        if not objects:
            builtin = self._get_builtin_templates()
            if template_name in builtin:
                objects = builtin[template_name].get("objects", [])
                print(f"📦 模板 '{template_name}' 使用内置对象定义（{len(objects)} 个对象）")
            else:
                print(f"⚠️ 模板 '{template_name}' 没有对象定义，创建空场景")

        scene = SceneData(
            scene_name=scene_name or template_data.get("name", "模板场景"),
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            objects=objects,
            metadata=template_data.get("metadata", {})
        )
        self._current_scene = scene
        return scene

    def _load_templates(self) -> Dict[str, Dict]:
        """
        加载所有模板
        先加载内置模板，再用外部 JSON 文件覆盖同名模板（如果有）
        """
        # 1. 先获取内置模板（包含全部 8 个）
        templates = self._get_builtin_templates()

        # 2. 加载外部 JSON 文件，覆盖或新增
        templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
        if os.path.exists(templates_dir):
            for filename in os.listdir(templates_dir):
                if filename.endswith(".json"):
                    try:
                        with open(os.path.join(templates_dir, filename), "r", encoding="utf-8") as f:
                            template = json.load(f)
                            name = filename.replace(".json", "")
                            templates[name] = template  # 覆盖同名模板，或新增
                            print(f"✅ 加载外部模板: {name}")
                    except Exception as e:
                        print(f"⚠️ 加载模板 {filename} 失败: {e}")

        return templates

    def _get_builtin_templates(self) -> Dict[str, Dict]:
        """内置模板 - 当外部模板文件不存在时使用"""
        return {
            # ===== 已有模板 =====
            "charging_station": {
                "name": "充电站（标准）",
                "description": "10个快充桩、5个慢充桩、1个管理建筑",
                "metadata": {"industry": "能源", "type": "充电设施", "size": "small", "icon": "🔋"},
                "objects": self._generate_charging_station_objects()
            },
            "factory_workshop": {
                "name": "工厂车间",
                "description": "厂房建筑、生产线设备、工位",
                "metadata": {"industry": "制造", "type": "车间", "size": "medium", "icon": "🏗️"},
                "objects": self._generate_factory_objects()
            },
            "simple_office": {
                "name": "办公楼",
                "description": "主办公楼、停车位、景观树木",
                "metadata": {"industry": "办公", "type": "园区", "size": "small", "icon": "🏢"},
                "objects": self._generate_office_objects()
            },

            # ===== 新增模板 1：大型充电站 =====
            "charging_station_large": {
                "name": "充电站（大型）",
                "description": "25个快充桩、10个慢充桩、2栋建筑",
                "metadata": {"industry": "能源", "type": "充电设施", "size": "large", "icon": "⚡"},
                "objects": self._generate_large_charging_station()
            },

            # ===== 新增模板 2：高速服务区充电站 =====
            "charging_station_highway": {
                "name": "高速服务区充电站",
                "description": "20个快充桩、5个超充桩、服务区建筑",
                "metadata": {"industry": "能源", "type": "充电设施", "size": "highway", "icon": "🛣️"},
                "objects": self._generate_highway_charging_station()
            },

            # ===== 新增模板 3：智能工厂 =====
            "smart_factory": {
                "name": "智能工厂",
                "description": "厂房、AGV小车、自动化设备",
                "metadata": {"industry": "制造", "type": "智能工厂", "size": "large", "icon": "🤖"},
                "objects": self._generate_smart_factory()
            },

            # ===== 新增模板 4：商业综合体 =====
            "commercial_complex": {
                "name": "商业综合体",
                "description": "主楼、裙楼、停车场、绿化",
                "metadata": {"industry": "商业", "type": "综合体", "size": "large", "icon": "🏙️"},
                "objects": self._generate_commercial_complex()
            },

            # ===== 新增模板 5：工业园区 =====
            "industrial_park": {
                "name": "工业园区",
                "description": "多栋厂房、道路、停车场",
                "metadata": {"industry": "制造", "type": "园区", "size": "large", "icon": "🏭"},
                "objects": self._generate_industrial_park()
            }
        }

    # ==================== 内置场景生成器 ====================

    def _generate_charging_station_objects(self) -> List[Dict]:
        """生成充电站场景的默认对象列表"""
        objects = []

        # 1. 管理建筑 (中心)
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building",
            "name": "管理用房",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 2.0, "y": 0.8, "z": 1.5},
            "bind_station_id": None,
            "custom_props": {"color": "#4a6a8a", "height": 2.5}
        })

        # 2. 快充桩 (围绕建筑环形分布)
        charger_positions = [
            (-3, 0, -4), (3, 0, -4), (-4.5, 0, -1.5), (4.5, 0, -1.5),
            (-4.5, 0, 1.5), (4.5, 0, 1.5), (-3, 0, 4), (3, 0, 4)
        ]
        for i, (x, y, z) in enumerate(charger_positions):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_fast",
                "name": f"快充桩 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,  # 用户自行绑定
                "custom_props": {"power": 120, "color": "#4a90d9"}
            })

        # 3. 慢充桩 (外围)
        slow_positions = [
            (-6, 0, -5), (6, 0, -5), (-6, 0, 5), (6, 0, 5)
        ]
        for i, (x, y, z) in enumerate(slow_positions):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_slow",
                "name": f"慢充桩 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 7, "color": "#5cb85c"}
            })

        # 4. 树木 (点缀)
        tree_positions = [
            (-7, 0, 0), (7, 0, 0), (0, 0, -7), (0, 0, 7),
            (-5, 0, -6), (5, 0, 6), (-5, 0, 6), (5, 0, -6)
        ]
        for i, (x, y, z) in enumerate(tree_positions):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "tree",
                "name": f"树木 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.6 + i * 0.05, "y": 0.6 + i * 0.05, "z": 0.6 + i * 0.05},
                "bind_station_id": None,
                "custom_props": {"variety": "乔木"}
            })

        return objects

    def _generate_factory_objects(self) -> List[Dict]:
        """生成工厂车间场景的默认对象列表"""
        objects = []

        # 1. 厂房建筑 (大)
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building",
            "name": "主厂房",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 8.0, "y": 1.2, "z": 5.0},
            "bind_station_id": None,
            "custom_props": {"color": "#7a8a9a", "height": 4.0}
        })

        # 2. 生产线设备 (沿厂房布置)
        equipment_positions = [
            (-2, 0, -2), (2, 0, -2), (-2, 0, 2), (2, 0, 2),
            (0, 0, -3.5), (0, 0, 3.5)
        ]
        for i, (x, y, z) in enumerate(equipment_positions):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "equipment",
                "name": f"设备 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.8, "y": 0.6, "z": 0.6},
                "bind_station_id": None,
                "custom_props": {"status": "运行中", "temperature": 45}
            })

        # 3. 工位 (小)
        work_positions = [(-4, 0, -1), (4, 0, -1), (-4, 0, 1), (4, 0, 1)]
        for i, (x, y, z) in enumerate(work_positions):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "workstation",
                "name": f"工位 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.4, "y": 0.3, "z": 0.4},
                "bind_station_id": None,
                "custom_props": {"operator": f"工人{i + 1}"}
            })

        return objects

    def _generate_office_objects(self) -> List[Dict]:
        """生成办公楼场景的默认对象列表"""
        objects = []

        # 1. 主办公楼
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building",
            "name": "办公楼A座",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 3.0, "y": 1.8, "z": 2.5},
            "bind_station_id": None,
            "custom_props": {"color": "#5a7a9a", "floors": 6}
        })

        # 2. 停车位 (充电桩)
        parking_positions = [
            (-3.5, 0, -3), (3.5, 0, -3), (-3.5, 0, 3), (3.5, 0, 3),
            (-5, 0, -4), (5, 0, -4), (-5, 0, 4), (5, 0, 4)
        ]
        for i, (x, y, z) in enumerate(parking_positions):
            obj_type = "charger_fast" if i < 4 else "charger_slow"
            objects.append({
                "id": str(uuid.uuid4()),
                "type": obj_type,
                "name": f"{'快充' if i < 4 else '慢充'}桩 #{i + 1:02d}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 120 if i < 4 else 7}
            })

        # 3. 绿化 (树木)
        for i in range(8):
            angle = i * 3.14 / 4
            r = 5.5
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "tree",
                "name": f"景观树 #{i + 1:02d}",
                "position": {"x": r * 1.0 * 0.7, "y": 0, "z": r * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.8, "y": 0.8, "z": 0.8},
                "bind_station_id": None,
                "custom_props": {"variety": "景观树"}
            })

        return objects

    def _generate_large_charging_station(self) -> List[Dict]:
        """生成大型充电站场景 - 25个快充 + 10个慢充 + 2建筑"""
        import uuid
        objects = []

        # 1. 两栋管理建筑
        for i, (x, z) in enumerate([(-2, 0), (2, 0)]):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "building",
                "name": f"管理用房 #{i + 1}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.2, "y": 0.6, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"color": "#4a6a8a", "height": 2.0}
            })

        # 2. 25个快充桩 (环形 + 放射状)
        for i in range(25):
            angle = (i / 25) * 2 * 3.14159
            ring = i // 8
            radius = 2.5 + ring * 1.5
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_fast",
                "name": f"快充桩 #{i + 1:02d}",
                "position": {"x": radius * 0.7 * 0.7, "y": 0, "z": radius * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 120}
            })

        # 3. 10个慢充桩 (外围)
        for i in range(10):
            angle = (i / 10) * 2 * 3.14159 + 0.3
            radius = 5.5
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_slow",
                "name": f"慢充桩 #{i + 1:02d}",
                "position": {"x": radius * 0.7 * 0.7, "y": 0, "z": radius * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 7}
            })

        # 4. 树木 (点缀)
        for i in range(12):
            angle = (i / 12) * 2 * 3.14159 + 0.5
            radius = 6.5
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "tree",
                "name": f"树木 #{i + 1:02d}",
                "position": {"x": radius * 0.7 * 0.7, "y": 0, "z": radius * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.8 + i * 0.02, "y": 0.8 + i * 0.02, "z": 0.8 + i * 0.02},
                "bind_station_id": None,
                "custom_props": {"variety": "乔木"}
            })

        return objects

    def _generate_highway_charging_station(self) -> List[Dict]:
        """生成高速服务区充电站 - 20个快充 + 5个超充 + 服务区建筑"""
        import uuid
        objects = []

        # 1. 服务区主建筑
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building",
            "name": "服务区大厅",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 3.0, "y": 0.8, "z": 1.8},
            "bind_station_id": None,
            "custom_props": {"color": "#5a7a8a", "height": 2.5}
        })

        # 2. 20个快充桩 (两侧分布)
        for i in range(20):
            side = -1 if i < 10 else 1
            idx = i % 10
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_fast",
                "name": f"快充桩 #{i + 1:02d}",
                "position": {"x": side * 2.5, "y": 0, "z": -3 + idx * 0.8},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 120}
            })

        # 3. 5个超充桩 (中间)
        for i in range(5):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_super",
                "name": f"超充桩 #{i + 1:02d}",
                "position": {"x": -1.0 + i * 0.5, "y": 0, "z": 2.5},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.2, "y": 1.2, "z": 1.2},
                "bind_station_id": None,
                "custom_props": {"power": 250}
            })

        # 4. 路灯
        for i in range(8):
            angle = (i / 8) * 2 * 3.14159
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "lamp",
                "name": f"路灯 #{i + 1:02d}",
                "position": {"x": 4.5 * 0.7 * 0.7, "y": 0, "z": 4.5 * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {}
            })

        return objects

    def _generate_smart_factory(self) -> List[Dict]:
        """生成智能工厂 - 厂房 + AGV小车 + 自动化设备"""
        import uuid
        objects = []

        # 1. 主厂房 (大)
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building",
            "name": "智能厂房",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 5.0, "y": 1.2, "z": 3.5},
            "bind_station_id": None,
            "custom_props": {"color": "#6a8a9a", "height": 4.0}
        })

        # 2. 自动化设备 (沿厂房布置)
        for i in range(8):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "equipment",
                "name": f"设备 #{i + 1:02d}",
                "position": {"x": -1.5 + i * 0.4, "y": 0, "z": -1.0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.5, "y": 0.4, "z": 0.4},
                "bind_station_id": None,
                "custom_props": {"status": "运行中", "temperature": 45}
            })

        # 3. AGV小车 (模拟路径)
        for i in range(4):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "car",
                "name": f"AGV #{i + 1:02d}",
                "position": {"x": -2.0 + i * 1.3, "y": 0, "z": 1.5},
                "rotation": {"x": 0, "y": 0.3, "z": 0},
                "scale": {"x": 0.5, "y": 0.5, "z": 0.5},
                "bind_station_id": None,
                "custom_props": {"color": "#4a90d9"}
            })

        # 4. 工位
        for i in range(4):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "workstation",
                "name": f"工位 #{i + 1:02d}",
                "position": {"x": -1.0 + i * 0.7, "y": 0, "z": -2.0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.3, "y": 0.2, "z": 0.3},
                "bind_station_id": None,
                "custom_props": {"operator": f"工人{i + 1}"}
            })

        return objects

    def _generate_commercial_complex(self) -> List[Dict]:
        """生成商业综合体 - 主楼 + 裙楼 + 停车场 + 绿化"""
        import uuid
        objects = []

        # 1. 主楼 (高层)
        objects.append({
            "id": str(uuid.uuid4()),
            "type": "building_tall",
            "name": "主楼",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "scale": {"x": 1.5, "y": 2.5, "z": 1.5},
            "bind_station_id": None,
            "custom_props": {"color": "#5a7a9a"}
        })

        # 2. 裙楼 (两侧)
        for i, (x, z) in enumerate([(-2.2, 0), (2.2, 0)]):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "building",
                "name": f"裙楼 #{i + 1}",
                "position": {"x": x, "y": 0, "z": z},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.8, "y": 0.6, "z": 1.2},
                "bind_station_id": None,
                "custom_props": {"color": "#7a8a9a"}
            })

        # 3. 停车场 (充电桩)
        for i in range(12):
            angle = (i / 12) * 2 * 3.14159
            radius = 3.5
            obj_type = "charger_fast" if i < 6 else "charger_slow"
            objects.append({
                "id": str(uuid.uuid4()),
                "type": obj_type,
                "name": f"{'快充' if i < 6 else '慢充'}桩 #{i + 1:02d}",
                "position": {"x": radius * 0.7 * 0.7, "y": 0, "z": radius * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 120 if i < 6 else 7}
            })

        # 4. 树木 (景观)
        for i in range(8):
            angle = (i / 8) * 2 * 3.14159 + 0.2
            radius = 4.5
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "tree",
                "name": f"景观树 #{i + 1:02d}",
                "position": {"x": radius * 0.7 * 0.7, "y": 0, "z": radius * 0.7},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.7, "y": 0.7, "z": 0.7},
                "bind_station_id": None,
                "custom_props": {"variety": "景观树"}
            })

        return objects

    def _generate_industrial_park(self) -> List[Dict]:
        """生成工业园区 - 多栋厂房 + 道路 + 停车场"""
        import uuid
        objects = []

        # 1. 多栋厂房 (网格布局)
        for i in range(4):
            row = i // 2
            col = i % 2
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "building",
                "name": f"厂房 {chr(65 + i)}",
                "position": {"x": -2.5 + col * 5.0, "y": 0, "z": -2.0 + row * 4.0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 2.2, "y": 0.8, "z": 1.6},
                "bind_station_id": None,
                "custom_props": {"color": "#7a8a9a", "height": 2.8}
            })

        # 2. 中央道路 (用 road_straight 表示)
        for i in range(3):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "road_straight",
                "name": f"道路 #{i + 1}",
                "position": {"x": -4.0 + i * 4.0, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 0.05, "z": 0.3},
                "bind_station_id": None,
                "custom_props": {}
            })

        # 3. 停车场 (充电桩)
        for i in range(6):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "charger_fast" if i < 3 else "charger_slow",
                "name": f"{'快充' if i < 3 else '慢充'}桩 #{i + 1:02d}",
                "position": {"x": 3.5, "y": 0, "z": -1.5 + i * 0.6},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "bind_station_id": None,
                "custom_props": {"power": 120 if i < 3 else 7}
            })

        # 4. 树木 (绿化)
        for i in range(6):
            objects.append({
                "id": str(uuid.uuid4()),
                "type": "tree",
                "name": f"树木 #{i + 1:02d}",
                "position": {"x": -3.5, "y": 0, "z": -1.0 + i * 0.8},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 0.8, "y": 0.8, "z": 0.8},
                "bind_station_id": None,
                "custom_props": {"variety": "乔木"}
            })

        return objects

    # ==================== 场景保存与加载 ====================

    def save_scene(self, scene_data: SceneData, filename: str = None) -> str:
        """
        保存场景到文件

        Args:
            scene_data: 场景数据
            filename: 文件名 (不含扩展名)，默认使用场景名称

        Returns:
            str: 保存的文件路径
        """
        if filename is None:
            filename = scene_data.scene_name.replace(" ", "_")

        # 更新时间戳
        scene_data.updated_at = datetime.now().isoformat()

        filepath = os.path.join(self.data_dir, f"{filename}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(scene_data.to_dict(), f, ensure_ascii=False, indent=2)

        return filepath

    def load_scene(self, filepath: str) -> SceneData:
        """
        从文件加载场景

        Args:
            filepath: 场景文件路径

        Returns:
            SceneData: 加载的场景数据
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        scene = SceneData.from_dict(data)
        self._current_scene = scene
        return scene

    @staticmethod
    def _classify_json(data):
        """判断一份 JSON 是不是"可直接加载的场景文件"

        背景：exports/ 目录里同时存在真正的场景存档和两类**备份文件**
        （`scenes_backup_FULL_*.json` 是 scenes 表批量导出，
          `scene_objects_dup_backup_*.json` 是 scene_objects 行备份）。
        它们没有 `objects` 字段，旧代码却把它们也列进"已保存场景"，
        于是界面上出现两条名字是文件名、物体数是 0 的假场景（用户实际遇到）。

        返回 (kind, name, object_count)：
          kind='scene'   → 正常场景存档
          kind='backup'  → 能认出来的备份文件
          kind='other'   → 其它 JSON（也列出来，但归到"其他文件"）
        """
        if not isinstance(data, dict):
            return 'other', None, 0
        has_objects = 'objects' in data
        name = data.get('scene_name') or None
        count = len(data.get('objects') or []) if has_objects else 0
        if has_objects:
            return 'scene', name, count
        # 备份文件的特征字段
        if 'scenes' in data or 'rows' in data or 'scene_count' in data:
            return 'backup', name, 0
        return 'other', name, 0

    def get_scene_list(self) -> List[Dict[str, str]]:
        """获取所有已保存的场景列表（**只含真正的场景文件**）

        备份/其它 JSON 不再混进来（它们由 list_other_files() 单独列出并支持删除）。
        """
        scenes = []
        for filename in os.listdir(self.data_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.data_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    kind, name, count = self._classify_json(data)
                    if kind != 'scene':
                        continue
                    scenes.append({
                        "filename": filename,
                        "name": name or filename,
                        "updated_at": data.get("updated_at", ""),
                        "object_count": count
                    })
                except Exception as e:
                    print(f"读取场景 {filename} 失败: {e}")

        return sorted(scenes, key=lambda x: x.get("updated_at", ""), reverse=True)

    def list_other_files(self) -> List[Dict]:
        """列出 exports/ 下的**非场景** JSON（备份文件等），用于单独管理/删除

        每个文件都带上 kind / size_kb / 描述，界面上可以解释"这是什么"，
        而不是像以前那样伪装成"0 个物体的场景"。
        """
        out = []
        for filename in os.listdir(self.data_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(self.data_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                kind, _name, _count = self._classify_json(data)
                if kind == 'scene':
                    continue
                if kind == 'backup':
                    if isinstance(data, dict) and 'scenes' in data:
                        desc = '场景批量备份（%s 条）' % data.get('scene_count',
                                                                  len(data.get('scenes') or []))
                    elif isinstance(data, dict) and 'rows' in data:
                        desc = '场景物体行备份（%s 行）' % data.get('count',
                                                                 len(data.get('rows') or []))
                    else:
                        desc = '备份文件'
                else:
                    desc = '其它 JSON（非场景存档）'
                out.append({
                    'filename': filename,
                    'kind': kind,
                    'size_kb': os.path.getsize(filepath) / 1024.0,
                    'desc': desc,
                    'mtime': os.path.getmtime(filepath),
                })
            except Exception as e:
                print(f"读取 {filename} 失败: {e}")
        return sorted(out, key=lambda x: -x['mtime'])

    def delete_scene(self, filename: str) -> bool:
        """删除已保存的场景文件"""
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                return True
            except Exception as e:
                print(f"删除场景失败: {e}")
                return False
        return False

    # ==================== 场景导出 ====================

    def export_to_html(self, scene_data: SceneData, style=None, story=None) -> str:
        """
        导出场景为独立HTML文件（增强版：含风格、故事、统计信息）

        Args:
            scene_data: 场景数据
            style: 风格配置 (StyleConfig 对象)
            story: 故事节点 (StoryNode 对象)

        Returns:
            str: 生成的HTML内容
        """
        # 构建导出数据
        export_data = scene_data.to_dict()

        # 添加风格和故事
        if style:
            export_data['style'] = {
                "scene_bg": style.scene_bg,
                "fog_color": style.fog_color,
                "grid_color": style.grid_color,
                "ground_color": style.ground_color,
                "ambient_color": style.ambient_color,
                "ambient_intensity": style.ambient_intensity,
                "sun_color": style.sun_color,
                "sun_intensity": style.sun_intensity,
                "charger_color": style.charger_color,
                "building_color": style.building_color,
                "tree_color": style.tree_color,
            }
        else:
            export_data['style'] = {}

        if story:
            export_data['story'] = {
                "id": story.id,
                "name": story.name,
                "camera_position": story.camera_position,
                "camera_target": story.camera_target,
                "highlight_type": story.highlight_type,
                "filter_threshold": story.filter_threshold,
                "auto_rotate": story.auto_rotate,
                "show_labels": story.show_labels,
            }
        else:
            export_data['story'] = None

        # 计算统计信息
        objects = scene_data.objects
        total = len(objects)
        chargers = [o for o in objects if o.get('type', '').startswith('charger')]
        fast = len([o for o in chargers if o.get('type') == 'charger_fast'])
        slow = len([o for o in chargers if o.get('type') == 'charger_slow'])
        super_c = len([o for o in chargers if o.get('type') == 'charger_super'])
        bound = len([o for o in chargers if o.get('bind_station_id')])
        utils = [o.get('utilization', 0) for o in chargers if o.get('bind_station_id')]
        avg_util = sum(utils) / len(utils) if utils else 0
        high = len([u for u in utils if u > 0.7])

        stats = {
            'total': total,
            'chargers': len(chargers),
            'fast': fast,
            'slow': slow,
            'super': super_c,
            'bound': bound,
            'avg_util': avg_util,
            'high': high
        }

        # 生成 HTML
        html_template = f'''<!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>智孪 · 数字孪生场景导出</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ overflow: hidden; background: #0a0e17; font-family: 'Segoe UI', Arial, sans-serif; }}
            #container {{ width: 100vw; height: 100vh; position: relative; }}
            #info {{
                position: absolute; top: 20px; left: 20px; 
                color: #c8d6e5; background: rgba(0,0,0,0.7); 
                padding: 8px 20px; border-radius: 20px;
                font-size: 14px; z-index: 100;
                border: 1px solid #1a2a44;
                pointer-events: none;
                backdrop-filter: blur(4px);
            }}
            #stats {{
                position: absolute; bottom: 80px; left: 50%; transform: translateX(-50%);
                color: #88aadd; background: rgba(0,0,0,0.75);
                padding: 8px 24px; border-radius: 30px;
                font-size: 13px; z-index: 100;
                border: 1px solid #1a2a44;
                backdrop-filter: blur(4px);
                display: flex;
                gap: 24px;
                flex-wrap: wrap;
                justify-content: center;
                pointer-events: none;
                user-select: none;
            }}
            .stat-item {{
                display: flex;
                align-items: baseline;
                gap: 4px;
            }}
            .stat-item .label {{ color: #8899bb; font-weight: 300; }}
            .stat-item .value {{ color: #eef2ff; font-weight: 700; }}
            .stat-item .value.high {{ color: #ff6b6b; }}
            #loading {{
                position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
                color: #88aadd; font-size: 18px; z-index: 0;
            }}
            #demo-toggle {{
                position: absolute; bottom: 30px; right: 30px;
                background: rgba(10,14,23,0.8);
                border: 1px solid #1a2a44;
                border-radius: 30px;
                padding: 6px 16px;
                color: #88aadd;
                cursor: pointer;
                z-index: 100;
                font-size: 12px;
                backdrop-filter: blur(4px);
                transition: 0.2s;
            }}
            #demo-toggle:hover {{
                background: #1a2a44;
                border-color: #4477aa;
                color: #ffffff;
            }}
        </style>
    </head>
    <body>
        <div id="container">
            <div id="info">🏗️ {scene_data.scene_name} · {len(objects)} 个对象</div>
            <div id="loading">⏳ 加载场景中...</div>
            <div id="stats">
                <div class="stat-item"><span class="label">总对象</span> <span class="value">{stats['total']}</span></div>
                <div class="stat-item"><span class="label">充电桩</span> <span class="value">{stats['chargers']}</span> (⚡{stats['fast']} · 🔋{stats['slow']} · 🚀{stats['super']})</div>
                <div class="stat-item"><span class="label">已绑定</span> <span class="value">{stats['bound']}</span></div>
                <div class="stat-item"><span class="label">平均利用率</span> <span class="value {'high' if stats['avg_util'] > 0.7 else ''}">{stats['avg_util']:.1%}</span></div>
                <div class="stat-item"><span class="label">高负载</span> <span class="value high">{stats['high']}</span></div>
            </div>
            <button id="demo-toggle">⏸ 暂停演示</button>
        </div>

        <script type="importmap">
        {{
            "imports": {{
                "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
                "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
            }}
        }}
        </script>

        <script type="module">
            import * as THREE from 'three';
            import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
            import {{ CSS2DRenderer, CSS2DObject }} from 'three/addons/renderers/CSS2DRenderer.js';

            // ---------- 数据 ----------
            const sceneData = {json.dumps(export_data, ensure_ascii=False)};
            const objects = sceneData.objects || [];
            const style = sceneData.style || {{}};
            const story = sceneData.story || null;

            console.log(`✅ 加载场景: ${{sceneData.scene_name}}, ${{objects.length}} 个对象`);

            document.getElementById('loading').style.display = 'none';

            // ---------- 场景 ----------
            const container = document.getElementById('container');
            const width = container.clientWidth || window.innerWidth;
            const height = container.clientHeight || window.innerHeight;

            const scene = new THREE.Scene();
            scene.background = new THREE.Color(style.scene_bg || '#0a0e17');
            scene.fog = new THREE.Fog(style.fog_color || '#0a0e17', 30, 70);

            const camera = new THREE.PerspectiveCamera(40, width/height, 0.1, 200);
            camera.position.set(12, 10, 15);

            const renderer = new THREE.WebGLRenderer({{ antialias: true }});
            renderer.setSize(width, height);
            renderer.shadowMap.enabled = true;
            renderer.shadowMap.type = THREE.PCFSoftShadowMap;
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
            container.appendChild(renderer.domElement);

            const labelRenderer = new CSS2DRenderer();
            labelRenderer.setSize(width, height);
            labelRenderer.domElement.style.position = 'absolute';
            labelRenderer.domElement.style.top = '0';
            labelRenderer.domElement.style.left = '0';
            labelRenderer.domElement.style.pointerEvents = 'none';
            container.appendChild(labelRenderer.domElement);

            const controls = new OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.08;
            controls.autoRotate = true;
            controls.autoRotateSpeed = 0.6;
            controls.target.set(0, 1, 0);
            controls.maxPolarAngle = Math.PI / 2.1;
            controls.minDistance = 3;
            controls.maxDistance = 50;

            // ---------- 应用故事（视角） ----------
            if (story && story.camera_position) {{
                camera.position.set(story.camera_position.x, story.camera_position.y, story.camera_position.z);
                if (story.camera_target) {{
                    controls.target.set(story.camera_target.x, story.camera_target.y, story.camera_target.z);
                }}
                if (story.auto_rotate !== undefined) {{
                    controls.autoRotate = story.auto_rotate;
                }}
                controls.update();
            }}

            // ---------- 灯光 ----------
            const ambientColor = style.ambient_color ? new THREE.Color(style.ambient_color) : new THREE.Color(0x334466);
            const ambient = new THREE.AmbientLight(ambientColor, style.ambient_intensity || 0.8);
            scene.add(ambient);

            const sunColor = style.sun_color ? new THREE.Color(style.sun_color) : new THREE.Color(0xffeedd);
            const sun = new THREE.DirectionalLight(sunColor, style.sun_intensity || 1.8);
            sun.position.set(10, 20, 8);
            sun.castShadow = true;
            sun.shadow.mapSize.width = 1024;
            sun.shadow.mapSize.height = 1024;
            const d = 15;
            sun.shadow.camera.left = -d;
            sun.shadow.camera.right = d;
            sun.shadow.camera.top = d;
            sun.shadow.camera.bottom = -d;
            scene.add(sun);

            // ---------- 地面 ----------
            const gridColor1 = style.grid_color ? new THREE.Color(style.grid_color) : new THREE.Color(0x4477aa);
            const gridColor2 = style.grid_color ? new THREE.Color(style.grid_color).multiplyScalar(0.5) : new THREE.Color(0x1a2a44);
            const grid = new THREE.GridHelper(20, 20, gridColor1, gridColor2);
            grid.position.y = -0.1;
            scene.add(grid);

            const groundColor = style.ground_color ? new THREE.Color(style.ground_color) : new THREE.Color(0x111a2a);
            const ground = new THREE.Mesh(
                new THREE.CircleGeometry(12, 64),
                new THREE.MeshStandardMaterial({{
                    color: groundColor,
                    transparent: true,
                    opacity: 0.6,
                    roughness: 0.9,
                    side: THREE.DoubleSide
                }})
            );
            ground.rotation.x = -Math.PI / 2;
            ground.position.y = -0.1;
            ground.receiveShadow = true;
            scene.add(ground);

            // ---------- 生成对象（简化版，仅展示） ----------
            const objectTypes = {{
                'charger_fast': {{ color: style.charger_color || '#4a90d9', height: 2.0, label: '快充' }},
                'charger_slow': {{ color: style.charger_color || '#5cb85c', height: 1.5, label: '慢充' }},
                'charger_super': {{ color: style.charger_color || '#9b59b6', height: 2.5, label: '超充' }},
                'building': {{ color: style.building_color || '#6a8a9a', height: 3.0, label: '建筑' }},
                'tree': {{ color: style.tree_color || '#3a8a3a', height: 1.8, label: '树木' }},
            }};

            const defaultConfig = {{ color: '#888888', height: 1.0, label: '物体' }};

            objects.forEach(obj => {{
                const config = objectTypes[obj.type] || defaultConfig;
                const pos = obj.position || {{ x: 0, y: 0, z: 0 }};
                const scale = obj.scale || {{ x: 1, y: 1, z: 1 }};
                const height = config.height * scale.y;

                let geometry;
                if (obj.type === 'tree') {{
                    const trunk = new THREE.Mesh(
                        new THREE.CylinderGeometry(0.06, 0.1, height * 0.4, 6),
                        new THREE.MeshStandardMaterial({{ color: 0x5a3a2a, roughness: 0.9 }})
                    );
                    trunk.position.set(pos.x, height * 0.2, pos.z);
                    trunk.castShadow = true;
                    scene.add(trunk);
                    const crown = new THREE.Mesh(
                        new THREE.ConeGeometry(0.5 * scale.x, height * 0.7, 8),
                        new THREE.MeshStandardMaterial({{ color: config.color, roughness: 0.8 }})
                    );
                    crown.position.set(pos.x, height * 0.7, pos.z);
                    crown.castShadow = true;
                    scene.add(crown);
                    return;
                }}

                if (obj.type === 'building') {{
                    geometry = new THREE.BoxGeometry(1.0 * scale.x, height, 0.8 * scale.z);
                }} else if (obj.type.startsWith('charger')) {{
                    const pillar = new THREE.Mesh(
                        new THREE.BoxGeometry(0.25 * scale.x, height, 0.25 * scale.z),
                        new THREE.MeshStandardMaterial({{
                            color: config.color,
                            emissive: config.color,
                            emissiveIntensity: 0.15,
                            roughness: 0.3,
                            metalness: 0.1
                        }})
                    );
                    pillar.position.set(pos.x, height/2, pos.z);
                    pillar.castShadow = true;
                    pillar.receiveShadow = true;
                    scene.add(pillar);
                    const top = new THREE.Mesh(
                        new THREE.SphereGeometry(0.1 * scale.x, 8, 8),
                        new THREE.MeshStandardMaterial({{ color: config.color, emissive: config.color, emissiveIntensity: 0.3 }})
                    );
                    top.position.set(pos.x, height + 0.05, pos.z);
                    scene.add(top);
                    const ring = new THREE.Mesh(
                        new THREE.TorusGeometry(0.2 * scale.x, 0.02, 8, 16),
                        new THREE.MeshStandardMaterial({{ color: config.color, emissive: config.color, emissiveIntensity: 0.5, transparent: true, opacity: 0.6 }})
                    );
                    ring.position.set(pos.x, height + 0.03, pos.z);
                    ring.rotation.x = Math.PI / 2;
                    scene.add(ring);
                    return;
                }} else {{
                    geometry = new THREE.BoxGeometry(0.6 * scale.x, height, 0.6 * scale.z);
                }}

                const material = new THREE.MeshStandardMaterial({{
                    color: config.color,
                    roughness: 0.5,
                    metalness: 0.2
                }});
                const mesh = new THREE.Mesh(geometry, material);
                mesh.position.set(pos.x, height/2, pos.z);
                mesh.castShadow = true;
                mesh.receiveShadow = true;
                scene.add(mesh);
            }});

            // ---------- 装饰粒子 ----------
            const particleCount = 200;
            const positions = new Float32Array(particleCount * 3);
            for (let i = 0; i < particleCount * 3; i += 3) {{
                const r = 5 + Math.random() * 8;
                const theta = Math.random() * Math.PI * 2;
                positions[i] = Math.cos(theta) * r;
                positions[i+1] = Math.random() * 6 + 1;
                positions[i+2] = Math.sin(theta) * r;
            }}
            const pGeo = new THREE.BufferGeometry();
            pGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
            const pMat = new THREE.PointsMaterial({{
                color: style.charger_color || 0x4488ff,
                size: 0.05,
                transparent: true,
                opacity: 0.2,
                blending: THREE.AdditiveBlending
            }});
            const particles = new THREE.Points(pGeo, pMat);
            scene.add(particles);

            // ---------- 演示模式切换 ----------
            const demoBtn = document.getElementById('demo-toggle');
            demoBtn.addEventListener('click', () => {{
                controls.autoRotate = !controls.autoRotate;
                demoBtn.textContent = controls.autoRotate ? '⏸ 暂停演示' : '▶ 开始演示';
            }});

            // ---------- 动画 ----------
            function animate() {{
                requestAnimationFrame(animate);
                controls.update();
                renderer.render(scene, camera);
                labelRenderer.render(scene, camera);
            }}
            animate();

            // 窗口自适应
            window.addEventListener('resize', () => {{
                const w = window.innerWidth, h = window.innerHeight;
                camera.aspect = w / h;
                camera.updateProjectionMatrix();
                renderer.setSize(w, h);
                labelRenderer.setSize(w, h);
            }});
        </script>
    </body>
    </html>'''

        return html_template

    def export_html_to_file(self, scene_data: SceneData, filename: str = None) -> str:
        """
        导出场景为HTML文件并保存到磁盘

        Args:
            scene_data: 场景数据
            filename: 文件名 (不含扩展名)

        Returns:
            str: 保存的文件路径
        """
        if filename is None:
            filename = scene_data.scene_name.replace(" ", "_")

        html_content = self.export_to_html(scene_data)
        filepath = os.path.join(self.data_dir, f"{filename}_export.html")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)

        return filepath

    # ==================== 工具函数 ====================

    def get_current_scene(self) -> Optional[SceneData]:
        """获取当前正在编辑的场景"""
        return self._current_scene

    def set_current_scene(self, scene: SceneData):
        """设置当前场景"""
        self._current_scene = scene

    def validate_scene(self, scene_data: SceneData) -> List[str]:
        """
        验证场景数据的有效性

        Returns:
            List[str]: 错误信息列表，为空表示验证通过
        """
        errors = []

        # 检查版本
        if scene_data.version != self.CURRENT_VERSION:
            errors.append(f"版本不匹配: {scene_data.version} (当前支持: {self.CURRENT_VERSION})")

        # 检查对象
        for i, obj in enumerate(scene_data.objects):
            if 'id' not in obj:
                errors.append(f"对象 #{i} 缺少 id")
            if 'type' not in obj:
                errors.append(f"对象 #{i} 缺少 type")
            if 'position' not in obj:
                errors.append(f"对象 #{i} 缺少 position")

        return errors


# ==================== 使用示例 ====================

if __name__ == "__main__":
    # 测试场景管理器
    manager = SceneManager()

    # 1. 从模板创建场景
    scene = manager.create_from_template("charging_station", "我的充电站")
    print(f"✅ 创建场景: {scene.scene_name}, 对象数: {len(scene.objects)}")

    # 2. 验证场景
    errors = manager.validate_scene(scene)
    if errors:
        print("❌ 验证错误:", errors)
    else:
        print("✅ 场景验证通过")

    # 3. 保存场景
    filepath = manager.save_scene(scene, "test_scene")
    print(f"✅ 保存场景: {filepath}")

    # 4. 导出一份HTML
    html = manager.export_to_html(scene)
    print(f"✅ 导出HTML, 长度: {len(html)} 字符")