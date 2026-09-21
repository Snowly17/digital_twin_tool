"""扫描「widget key 与 session_state 赋值键同名」这一类 bug。

为什么需要这个检查
------------------
线上实测过一次崩溃：

    streamlit.errors.StreamlitWidgetAlreadyInstantiatedError:
    `st.session_state.auto_tour` cannot be modified after the widget with
    key `auto_tour` is instantiated.
      File "app.py", line 2200, in render_story_panel
        st.session_state.auto_tour = True

当时的代码是：

    if st.button("▶️ 开始自动导览", key="auto_tour"):
        st.session_state.auto_tour = True      # ← 撞自己的 key

Streamlit 规定：widget 一旦实例化，就不能再修改以它 `key` 命名的
session_state 项。这类 bug 只在**用户点击时**才暴露，
平时跑测试、看日志都发现不了 —— 所以值得用静态检查兜住。

它检查什么
----------
在同一个语句块（函数体 / if / for / with / try）里：

    · 先出现 st.<widget>(..., key='X')
    · 之后又出现 st.session_state.X = ...

就报一处命中。注意这是**启发式**检查：它可能对「同名但确实无冲突」的
写法误报（例如 widget 在块首、赋值在完全独立的执行路径上），
命中后请人工确认。

用法
----
    python tools/scan_widget_key_conflict.py

退出码：0 = 干净，1 = 有命中（便于接进 CI）。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会产生 widget 且接受 key= 的 st.* 调用
WIDGET_FUNCS = {
    "button", "checkbox", "radio", "selectbox", "multiselect", "slider",
    "select_slider", "text_input", "text_area", "number_input", "date_input",
    "time_input", "color_picker", "file_uploader", "download_button",
    "toggle", "form_submit_button", "camera_input", "audio_input",
    "segmented_control", "pills", "data_editor", "chat_input",
}

ASSIGN_NODES = (ast.Assign, ast.AnnAssign, ast.AugAssign)


def widget_key_of(node: ast.Call):
    """如果这是 st.<widget>(..., key='X')，返回 X。"""
    f = node.func
    if not isinstance(f, ast.Attribute):
        return None
    if not (isinstance(f.value, ast.Name) and f.value.id == "st"):
        return None
    if f.attr not in WIDGET_FUNCS:
        return None
    for kw in node.keywords:
        if (kw.arg == "key"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)):
            return kw.value.value
    return None


def session_state_target(node):
    """从赋值类节点里提取 st.session_state.X 的 X，没有则返回 None。"""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return None
    for t in targets:
        if (isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Attribute)
                and isinstance(t.value.value, ast.Name)
                and t.value.value.id == "st"
                and t.value.attr == "session_state"):
            return t.attr
    return None


class Scanner(ast.NodeVisitor):
    def __init__(self, path, src_lines):
        self.path = path
        self.lines = src_lines
        self.hits = []

    def _collect(self, body, keys):
        """递归收集块内的 widget key（含嵌套子块）。

        ⚠️ 覆盖面要点：widget 的定义与「撞名的赋值」不一定在同一个子块里
        （例如 selectbox 在块首、赋值紧随其后但被包在同一个 if 里）。
        所以先对整块做一次全量收集，再遍历整块所有赋值节点比对。
        """
        for node in body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    k = widget_key_of(sub)
                    if k:
                        keys.setdefault(k, sub.lineno)

    def _scan_block(self, body, since_line):
        keys = {}
        self._collect(body, keys)
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ASSIGN_NODES):
                t = session_state_target(node)
                if t and t in keys:
                    self.hits.append((node.lineno, t, keys[t], since_line))
        # 递归进子块，覆盖更内层的作用域
        for node in body:
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(node, field, None)
                if isinstance(sub, list) and sub:
                    self._scan_block(sub, getattr(node, "lineno", since_line))
            for handler in getattr(node, "handlers", []) or []:
                self._scan_block(handler.body, handler.lineno)

    def visit_FunctionDef(self, node):
        self._scan_block(node.body, node.lineno)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef


def collect_targets():
    targets = ["app.py"]
    for extra in ("core", "common", "tools"):
        d = os.path.join(ROOT, extra)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".py"):
                    targets.append(os.path.join(extra, fn))
    return targets


def main() -> int:
    total = 0
    scanned = 0
    skipped = 0

    for rel in collect_targets():
        full = os.path.join(ROOT, rel)
        try:
            with open(full, encoding="utf-8") as fh:
                src = fh.read()
            lines = src.splitlines()
            tree = ast.parse(src)
        except (OSError, SyntaxError) as e:
            print(f"  [跳过] {rel}: {e}")
            skipped += 1
            continue

        scanned += 1
        sc = Scanner(rel, lines)
        sc.visit(tree)

        seen = set()
        for lineno, key, key_line, _ in sc.hits:
            sig = (rel, lineno, key)
            if sig in seen:
                continue
            seen.add(sig)
            total += 1
            print(f"  [命中] {rel}:{lineno}  写入 st.session_state.{key}")
            print(f"         widget key 定义于 L{key_line}")
            print(f"         {lines[lineno-1].strip()}")
            print()

    print(f"扫描文件数: {scanned}" + (f"（跳过 {skipped}）" if skipped else ""))
    print("=" * 68)
    if total:
        print(f"[WARN] 发现 {total} 处「widget key 与赋值键同名」的潜在冲突")
        print()
        print("  修法：把 widget 的 key 与状态变量改成两个不同的名字，例如")
        print("        key=\"auto_tour_start_btn\"  +  st.session_state.auto_tour = True")
        print()
        print("  注意这是启发式检查，命中后请人工确认是否真的冲突。")
        return 1
    print("[PASS] 未发现 widget key 与 session_state 赋值键同名的冲突")
    return 0


if __name__ == "__main__":
    sys.exit(main())
