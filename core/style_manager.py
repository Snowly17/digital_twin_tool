"""
core/style_manager.py

风格管理器 - 管理 4 种视觉风格
"""

from typing import Dict, Any
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class StyleConfig:
    """风格配置"""
    name: str
    icon: str
    description: str
    # 场景颜色
    scene_bg: str
    fog_color: str
    grid_color: str
    ground_color: str
    ambient_color: str
    ambient_intensity: float
    sun_color: str
    sun_intensity: float
    # 材质颜色
    charger_color: str
    building_color: str
    tree_color: str
    # UI 颜色（由 CSS 类控制）
    ui_class: str
    # Bloom 参数
    bloom_strength: float
    bloom_radius: float
    bloom_threshold: float


class StyleManager:
    """风格管理器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._current_style = "tech_blue"
        self._styles = self._build_styles()

    def _build_styles(self) -> Dict[str, StyleConfig]:
        """构建 4 种风格"""
        return {
            # ===== 1. 科技蓝（默认） =====
            "tech_blue": StyleConfig(
                name="科技蓝",
                icon="🌐",
                description="专业、科技感",
                scene_bg="#0a0e17",
                fog_color="#0a0e17",
                grid_color="#4477aa",
                ground_color="#111a2a",
                ambient_color="#334466",
                ambient_intensity=0.8,
                sun_color="#ffeedd",
                sun_intensity=1.8,
                charger_color="#4a90d9",
                building_color="#6a8a9a",
                tree_color="#3a8a3a",
                ui_class="style-tech-blue",
                bloom_strength=0.6,
                bloom_radius=0.3,
                bloom_threshold=0.15
            ),
            # ===== 2. 赛博朋克 =====
            "cyberpunk": StyleConfig(
                name="赛博朋克",
                icon="🌆",
                description="霓虹、炫酷",
                scene_bg="#1a0a1a",
                fog_color="#1a0a1a",
                grid_color="#ff00ff",
                ground_color="#1a0a20",
                ambient_color="#442266",
                ambient_intensity=0.6,
                sun_color="#ff44ff",
                sun_intensity=1.2,
                charger_color="#ff00ff",
                building_color="#aa44aa",
                tree_color="#66aa44",
                ui_class="style-cyberpunk",
                bloom_strength=1.2,
                bloom_radius=0.5,
                bloom_threshold=0.1
            ),
            # ===== 3. 暗夜金 =====
            "dark_gold": StyleConfig(
                name="暗夜金",
                icon="🌙",
                description="奢华、高端",
                scene_bg="#0a0a0a",
                fog_color="#0a0a0a",
                grid_color="#d4af37",
                ground_color="#1a1a0a",
                ambient_color="#332211",
                ambient_intensity=0.5,
                sun_color="#ffdd88",
                sun_intensity=1.5,
                charger_color="#d4af37",
                building_color="#8a7a5a",
                tree_color="#5a7a3a",
                ui_class="style-dark-gold",
                bloom_strength=0.8,
                bloom_radius=0.4,
                bloom_threshold=0.12
            ),
            # ===== 4. 极简白 =====
            "minimal_white": StyleConfig(
                name="极简白",
                icon="☀️",
                description="清爽、商务",
                scene_bg="#f0f0f0",
                fog_color="#f0f0f0",
                grid_color="#888899",
                ground_color="#e8e8e8",
                ambient_color="#ddeeff",
                ambient_intensity=1.0,
                sun_color="#ffffff",
                sun_intensity=2.0,
                charger_color="#667788",
                building_color="#8899aa",
                tree_color="#5a8a5a",
                ui_class="style-minimal-white",
                bloom_strength=0.3,
                bloom_radius=0.2,
                bloom_threshold=0.2
            )
        }

    def get_style(self, style_id: str = None) -> StyleConfig:
        """获取风格配置"""
        if style_id is None:
            style_id = self._current_style
        return self._styles.get(style_id, self._styles["tech_blue"])

    def get_current_style(self) -> StyleConfig:
        """获取当前风格"""
        return self._styles.get(self._current_style, self._styles["tech_blue"])

    def set_style(self, style_id: str):
        """切换风格"""
        if style_id in self._styles:
            self._current_style = style_id
            return True
        return False

    def get_all_styles(self) -> Dict[str, StyleConfig]:
        """获取所有风格"""
        return self._styles

    def get_style_list(self) -> List[Dict[str, str]]:
        """获取风格列表（用于 UI）"""
        return [
            {"id": sid, "name": s.name, "icon": s.icon, "description": s.description}
            for sid, s in self._styles.items()
        ]


# ==================== 全局单例 ====================
_style_manager = None


def get_style_manager() -> StyleManager:
    """获取风格管理器单例"""
    global _style_manager
    if _style_manager is None:
        _style_manager = StyleManager()
    return _style_manager