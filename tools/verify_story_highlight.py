"""校验「故事定义的高亮类型」与「前端实现的分支」是否一一对应。

为什么需要这个检查
------------------
core/story_manager.py 里 low_efficiency 故事用 highlight_type="low_util"，
prediction 故事用 "predicted"，但 app.py 的 applyStoryHighlight 只实现了
high_load / low_load / all_chargers / buildings / trees。

后果是**静默失效**：点这两条故事只有相机视角变化、没有任何高亮，
不报错、不告警，看日志也发现不了 —— 只有人工对比代码才能察觉。

本脚本从 story_manager 抽出所有 highlight_type，再从**真实生成的场景 HTML**
里抽出 applyStoryHighlight 实现的分支，比对两者。以后改故事或改 JS 都能立刻发现。

用法：python tools/verify_story_highlight.py

退出码：0 = 一一对应，1 = 有漂移（便于接进 CI）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core.story_manager import get_story_manager  # noqa: E402
from app import generate_scene_html  # noqa: E402

# ---------------------------------------------------------------- 1) 故事侧
mgr = get_story_manager()
stories = mgr.get_all_stories()

story_types = {}   # type -> [故事名]
for sid, s in stories.items():
    ht = getattr(s, "highlight_type", None)
    if ht:
        story_types.setdefault(ht, []).append(getattr(s, "name", sid))

print("=" * 66)
print("故事定义里的 highlight_type")
print("=" * 66)
if story_types:
    for ht, names in sorted(story_types.items()):
        print(f"  {ht:16s} <- {', '.join(names)}")
else:
    print("  （当前没有任何故事使用高亮 —— 只保留纯视角类故事）")
no_hl = [getattr(s, 'name', k) for k, s in stories.items() if not getattr(s, 'highlight_type', None)]
print(f"  纯视角故事: {', '.join(no_hl) if no_hl else '无'}")

# ---------------------------------------------------------------- 2) JS 侧
html = generate_scene_html()

# 用固定长度切片，不用正则跨行匹配函数体：
#   re.S + 贪婪 ``.*?`` 在跨行匹配函数体时会失败/错位。
_start = html.find("function applyStoryHighlight")
if _start < 0:
    print("\n[FAIL] 生成的 HTML 里找不到 applyStoryHighlight 函数。")
    print("       要么函数被改名/删除，要么提取逻辑失效 —— 都值得检查。")
    sys.exit(1)
func = html[_start:_start + 8000]

# 匹配 highlightType === 'xxx'（含 `a || b` 的组合写法）
impl = set(re.findall(r"highlightType\s*===\s*'([a-z_]+)'", func))

print()
print("=" * 66)
print("applyStoryHighlight 实际实现的分支")
print("=" * 66)
for t in sorted(impl):
    print(f"  {t}")

# ⚠️ 必须先确认提取到了东西。否则 impl 为空集 -> missing 也为空 -> 假阳性 PASS。
if not impl:
    print()
    print("[FAIL] 没能从 applyStoryHighlight 里提取到任何 highlightType 分支。")
    print("       这不代表没问题 —— 是提取逻辑失效了（例如函数被改名、")
    print("       或正则与写法不匹配）。请检查提取规则。")
    sys.exit(1)

# ---------------------------------------------------------------- 3) 比对
missing = {ht: names for ht, names in story_types.items() if ht not in impl}

print()
print("=" * 66)
if missing:
    print("[FAIL] 以下故事的高亮类型在前端没有实现分支：")
    for ht, names in sorted(missing.items()):
        print(f"  {ht:16s}  <- 故事: {', '.join(names)}")
    print()
    print("  表现：点这些故事只有相机视角变化，没有任何高亮（静默失效）。")
    print("  修法：要么补上前端分支，要么按 story_manager.py 的说明移除该故事。")
    sys.exit(1)

print("[PASS] 故事引用的高亮类型都有对应的前端实现分支")

# 顺带列出没人用的分支（不算错，但提示可以清理）
unused = impl - set(story_types.keys())
if unused:
    print()
    print(f"  [提示] 前端实现了但当前没有故事使用的分支: {sorted(unused)}")
    print("         (applyStoryHighlight 作为通用能力保留；若确认不再需要可一并清理)")
