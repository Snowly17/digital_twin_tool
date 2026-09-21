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
        # ⚠️ 设计说明：这里**只保留纯视角类故事**。
        #
        # 原先还有「高峰时段」「低效识别」「预测未来」三条，它们依赖
        # highlight_type 让前端高亮物体，但实测**永远不生效**：
        #   · 「低效识别」用 highlight_type="low_util"，而前端只实现了 "low_load"
        #     （命名漂移）；「预测未来」用的 "predicted" 前端根本没有分支。
        #   · 更根本的是：前端按 objData.utilization 判定阈值，而这个字段与
        #     3D 光柱使用的值（前端 localStorage util_<id>）并不同源，
        #     新建场景里常见为 undefined -> 兜底 0.5 -> 既不低于 30% 也不高于 70%，
        #     于是**任何高亮分支都不会命中**。
        # 结果是这三条故事点了只有相机变化、物体毫无反应，界面在骗人。
        # 因此移除，只留下面两条确定生效的纯相机视角故事。
        #
        # 如果以后要让高亮故事回归，必须先把 utilization 统一成单一真值来源。
        return {
            "overview": StoryNode(
                id="overview",
                name="全貌概览",
                icon="🌐",
                description="拉远俯瞰全部充电站的空间分布",
                camera_position={"x": 20, "y": 18, "z": 20},
                camera_target={"x": 0, "y": 0, "z": 0},
                highlight_type=None,
                show_labels=True,
                auto_rotate=True
            ),
            "night_view": StoryNode(
                id="night_view",
                name="夜间模式",
                icon="🌙",
                description="较近视角、关闭标签，适合看夜间灯光效果",
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