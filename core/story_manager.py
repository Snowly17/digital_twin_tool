# core/story_manager.py

from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class StoryNode:
    id: str
    name: str
    icon: str
    description: str
    camera_position: Dict[str, float]
    camera_target: Dict[str, float]
    highlight_type: Optional[str] = None
    filter_threshold: Optional[float] = None
    show_labels: bool = True
    auto_rotate: bool = False


class StoryManager:
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
        self._current_story = None
        self._stories = self._build_stories()

    def _build_stories(self) -> Dict[str, StoryNode]:
        return {
            "overview": StoryNode(
                id="overview",
                name="全貌概览",
                icon="🌐",
                description="展示所有充电站的空间分布",
                camera_position={"x": 20, "y": 18, "z": 20},
                camera_target={"x": 0, "y": 0, "z": 0},
                highlight_type=None,
                show_labels=True,
                auto_rotate=True
            ),
            "peak_hour": StoryNode(
                id="peak_hour",
                name="高峰时段",
                icon="🔥",
                description="识别高负载站点（利用率 > 70%）",
                camera_position={"x": 10, "y": 8, "z": 15},
                camera_target={"x": 0, "y": 1, "z": 0},
                highlight_type="high_load",
                filter_threshold=0.7,
                show_labels=True,
                auto_rotate=False
            ),
            "low_efficiency": StoryNode(
                id="low_efficiency",
                name="低效识别",
                icon="💡",
                description="识别低利用率站点（< 30%），建议优化",
                camera_position={"x": -12, "y": 6, "z": 10},
                camera_target={"x": 0, "y": 1, "z": 0},
                highlight_type="low_util",
                filter_threshold=0.3,
                show_labels=True,
                auto_rotate=False
            ),
            "prediction": StoryNode(
                id="prediction",
                name="预测未来",
                icon="🔮",
                description="LSTM 预测未来 12 小时利用率趋势",
                camera_position={"x": 5, "y": 4, "z": 8},
                camera_target={"x": 0, "y": 1, "z": 0},
                highlight_type="predicted",
                filter_threshold=0.6,
                show_labels=True,
                auto_rotate=False
            ),
            "night_view": StoryNode(
                id="night_view",
                name="夜间模式",
                icon="🌙",
                description="夜间充电需求分布",
                camera_position={"x": 15, "y": 12, "z": 15},
                camera_target={"x": 0, "y": 0, "z": 0},
                highlight_type=None,
                show_labels=False,
                auto_rotate=True
            )
        }

    def get_story(self, story_id: str) -> Optional[StoryNode]:
        return self._stories.get(story_id)

    def get_all_stories(self) -> Dict[str, StoryNode]:
        return self._stories

    def get_story_list(self) -> List[Dict[str, str]]:
        return [
            {"id": sid, "name": s.name, "icon": s.icon, "description": s.description}
            for sid, s in self._stories.items()
        ]

    def set_current_story(self, story_id: str) -> bool:
        if story_id in self._stories:
            self._current_story = story_id
            return True
        return False

    def get_current_story(self) -> Optional[str]:
        return self._current_story


_story_manager = None


def get_story_manager() -> StoryManager:
    global _story_manager
    if _story_manager is None:
        _story_manager = StoryManager()
    return _story_manager