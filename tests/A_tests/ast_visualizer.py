"""AST 可视化演示程序：在左侧输入 SQL，在右侧绘制真实 AST 树。

启动方式（必须从项目根目录执行）：

    python3 -m tests.A_tests.ast_visualizer

程序调用 ``compiler.parse``，因此展示的是模块 A 实际产生的 AST，而不是
界面中重新实现的一套 SQL 规则。解析成功时右侧 Canvas 绘制节点框与父子连线；
解析失败时展示 E_SYNTAX、行列号和错误消息，并在左侧定位出错位置。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font, ttk

from compiler import parse
from contracts.errors import ParseError
from tests.A_tests.ast_tree_adapter import TreeNode, ast_to_tree


# 此常量集中控制 Canvas 树图的视觉尺寸，便于答辩前统一调整而不改布局算法。
_CANVAS_PADDING = 36
_NODE_HORIZONTAL_GAP = 28
_NODE_VERTICAL_GAP = 88
_NODE_VERTICAL_PADDING = 12
_NODE_HORIZONTAL_PADDING = 16


@dataclass(frozen=True)
class _NodeLayout:
    """保存一个 TreeNode 在 Canvas 中的布局计算结果。

    Attributes:
        x: 节点矩形的左侧 x 坐标，未包含画布外边距。
        y: 节点矩形的上侧 y 坐标，未包含画布外边距。
        width: 节点矩形宽度，依据标签文本自动计算。
        height: 节点矩形高度，依据标签文本自动计算。
    """

    x: float
    y: float
    width: float
    height: float


class AstVisualizerApp:
    """管理 AST 可视化窗口、SQL 输入和 Canvas 树形渲染的应用控制器。

    UI 分为两个主要区域：左侧是可编辑 SQL 文本和解析按钮，右侧是支持水平、
    垂直滚动的 Canvas。控制器只调用 compiler.parse 和 ast_to_tree；不会访问
    Storage、Runner 或文件系统，确保它始终是模块 A 的独立演示工具。

    Args:
        root: tkinter 创建的根窗口。main() 负责创建它并启动事件循环。
    """

    # 此构造函数保存窗口引用，建立界面，并放入一条适合展示的默认 SQL。
    def __init__(self, root: tk.Tk) -> None:
        """初始化应用的视觉样式、控件布局、快捷键和默认 SQL 示例。

        构造阶段不自动解析默认 SQL，避免窗口尚未完成布局时提前绘制。用户点击
        按钮或按下快捷键后，才会调用真实 compiler.parse 并刷新右侧显示。
        """
        self._root = root
        self._root.title("hello-sql AST 可视化演示")
        self._root.geometry("1240x720")
        self._root.minsize(980, 560)

        self._tree_font = font.nametofont("TkDefaultFont")
        self._status_text = tk.StringVar(value="状态：等待输入 SQL")
        self._sql_input: tk.Text
        self._canvas: tk.Canvas
        self._status_label: ttk.Label

        self._build_layout()
        self._bind_shortcuts()
        self._insert_default_sql()

    # 此方法创建左右分栏、输入框、按钮、状态栏及 Canvas 滚动区域。
    def _build_layout(self) -> None:
        """构建窗口中的全部静态控件，并配置自适应的行列伸缩关系。

        左侧输入区保留较稳定的宽度，右侧 Canvas 可随窗口扩展。Canvas 与两条
        滚动条组成一个独立区域，确保复杂 AST 超出可见范围时仍可查看全部节点。
        此方法只创建控件，不包含解析或绘图逻辑。
        """
        outer = ttk.Frame(self._root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        self._root.rowconfigure(0, weight=1)
        self._root.columnconfigure(0, weight=1)

        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=0)
        outer.columnconfigure(1, weight=1)

        left_frame = ttk.LabelFrame(outer, text="SQL 输入", padding=10)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_frame.rowconfigure(1, weight=1)
        left_frame.columnconfigure(0, weight=1)

        instruction = ttk.Label(
            left_frame,
            text="输入一条 V2 SQL；点击按钮或按 Ctrl/⌘ + Enter 解析。",
            wraplength=320,
            justify="left",
        )
        instruction.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self._sql_input = tk.Text(
            left_frame,
            width=42,
            wrap="word",
            undo=True,
            font=("Menlo", 13),
            padx=8,
            pady=8,
        )
        self._sql_input.grid(row=1, column=0, sticky="nsew")
        self._sql_input.tag_configure("parse_error", background="#ffd9d9")

        parse_button = ttk.Button(
            left_frame,
            text="解析并生成 AST",
            command=self._on_parse,
        )
        parse_button.grid(row=2, column=0, sticky="ew", pady=(10, 6))

        self._status_label = ttk.Label(left_frame, textvariable=self._status_text)
        self._status_label.grid(row=3, column=0, sticky="ew")

        right_frame = ttk.LabelFrame(outer, text="AST Tree", padding=8)
        right_frame.grid(row=0, column=1, sticky="nsew")
        right_frame.rowconfigure(0, weight=1)
        right_frame.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            right_frame,
            background="#ffffff",
            highlightthickness=0,
            xscrollincrement=1,
            yscrollincrement=1,
        )
        vertical_scrollbar = ttk.Scrollbar(
            right_frame,
            orient="vertical",
            command=self._canvas.yview,
        )
        horizontal_scrollbar = ttk.Scrollbar(
            right_frame,
            orient="horizontal",
            command=self._canvas.xview,
        )
        self._canvas.configure(
            xscrollcommand=horizontal_scrollbar.set,
            yscrollcommand=vertical_scrollbar.set,
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

    # 此方法绑定常用快捷键，使答辩演示时无需频繁使用鼠标。
    def _bind_shortcuts(self) -> None:
        """绑定 Ctrl+Enter 与 macOS Command+Enter 到同一个解析动作。

        tkinter 在不同操作系统上对快捷键名称略有差异，因此同时注册 Control
        和 Command。回调返回 ``break``，防止 Text 控件把 Enter 继续插入 SQL。
        """
        self._sql_input.bind("<Control-Return>", self._on_parse_shortcut)
        self._sql_input.bind("<Command-Return>", self._on_parse_shortcut)

    # 此方法填入一条覆盖别名、限定列、JOIN、BOOLEAN 和逻辑优先级的 V2 SQL。
    def _insert_default_sql(self) -> None:
        """写入默认演示 SQL，帮助首次打开程序时立即理解预期输入。

        示例覆盖 SelectStmt 新增的 TableRef 与 joins 字段，并包含限定列、布尔
        字面量及 NOT、AND、OR 表达式，便于答辩时一次展示 V2 的主要 AST 层级。
        用户可直接编辑或替换为其他受支持的单条 SQL 语句。
        """
        self._sql_input.insert(
            "1.0",
            (
                "SELECT u.id, o.user_id\n"
                "FROM users u\n"
                "INNER JOIN orders o ON u.id = o.user_id AND NOT o.deleted\n"
                "WHERE u.active = TRUE OR o.total > 100;"
            ),
        )

    # 此快捷键回调复用按钮对应的解析操作，并阻止 Text 插入换行。
    def _on_parse_shortcut(self, _event: tk.Event[tk.Misc]) -> str:
        """处理 Ctrl/Command+Enter，调用 _on_parse 后返回 tkinter 的 break 标记."""
        self._on_parse()
        return "break"

    # 此按钮回调从输入框读取 SQL，调用真实编译器，并根据结果绘制树或显示错误。
    def _on_parse(self) -> None:
        """执行一次“SQL 文本 → AST → 可视化树”的完整演示流程。

        成功时，先把 Statement 转为 TreeNode，再清除旧内容并渲染新树；失败时，
        捕获 ParseError，清除旧树、在右侧展示错误信息，并高亮左侧对应位置。
        无论哪种情况，界面都不会调用 C/B 模块或产生磁盘数据。
        """
        sql = self._sql_input.get("1.0", "end-1c")
        self._clear_error_highlight()

        try:
            statement = parse(sql)
        except ParseError as error:
            self._show_parse_error(error)
            return

        tree = ast_to_tree(statement)
        self._render_tree(tree)
        self._status_text.set(f"状态：解析成功，根节点为 {type(statement).__name__}")
        self._status_label.configure(foreground="#167a3d")

    # 此方法在 Canvas 中展示 ParseError 的错误码、位置和详细消息。
    def _show_parse_error(self, error: ParseError) -> None:
        """清空旧树、显示错误摘要，并在 SQL 输入框中标记出错位置。

        ParseError 是模块 A 对外承诺的错误类型，含 code、line、col、message。
        可视化程序不修改这些内容，只将它们显示出来，使答辩时能说明词法/语法
        错误如何从 parser 精确传递到界面。
        """
        self._clear_canvas()
        self._highlight_error_position(error.line, error.col)
        self._status_text.set(f"状态：{error.code}（第 {error.line} 行，第 {error.col} 列）")
        self._status_label.configure(foreground="#b42318")

        self._canvas.create_text(
            _CANVAS_PADDING,
            _CANVAS_PADDING,
            anchor="nw",
            text="解析失败",
            font=("TkDefaultFont", 18, "bold"),
            fill="#b42318",
        )
        self._canvas.create_text(
            _CANVAS_PADDING,
            _CANVAS_PADDING + 42,
            anchor="nw",
            width=760,
            justify="left",
            text=(
                f"错误码：{error.code}\n"
                f"位置：第 {error.line} 行，第 {error.col} 列\n"
                f"消息：{error.message}"
            ),
            font=("TkDefaultFont", 12),
            fill="#5f1f1f",
        )
        self._canvas.configure(scrollregion=(0, 0, 840, 220))

    # 此方法将 TreeNode 的父子关系转换为 Canvas 上的连线、矩形节点和文本。
    def _render_tree(self, root_node: TreeNode) -> None:
        """清空 Canvas，计算树布局，并按“先边后节点”的顺序完成绘制。

        先绘制父子连线可避免线条压在节点文字上。布局算法会先递归计算所有叶子
        的横向位置，再将父节点放到其第一个和最后一个子节点的中点，保证树形
        结构清晰。节点超出右侧区域时由 Canvas 滚动条负责浏览。
        """
        self._clear_canvas()
        layouts, tree_width, tree_height = self._calculate_layout(root_node)
        self._draw_edges(root_node, layouts)
        self._draw_nodes(root_node, layouts)

        self._canvas.configure(
            scrollregion=(
                0,
                0,
                tree_width + _CANVAS_PADDING * 2,
                tree_height + _CANVAS_PADDING * 2,
            )
        )
        self._canvas.xview_moveto(0)
        self._canvas.yview_moveto(0)

    # 此方法以深度优先方式计算每个节点的矩形位置和整个树的边界尺寸。
    def _calculate_layout(
        self,
        root_node: TreeNode,
    ) -> tuple[dict[int, _NodeLayout], float, float]:
        """为 TreeNode 树生成 Canvas 布局坐标，不在此方法中执行实际绘制。

        叶子从左向右依次排列；拥有子节点的父节点放在最左、最右子节点中心的
        中点。方法返回以 ``id(TreeNode)`` 为键的布局表，因为同一棵不可变树中
        每个节点对象都唯一，渲染阶段可以据此快速查询父子坐标。

        Returns:
            三元组：节点布局字典、树内容宽度、树内容高度，三者均不含外边距。
        """
        layouts: dict[int, _NodeLayout] = {}
        next_leaf_x = 0.0
        max_depth = 0

        # 此嵌套辅助函数递归布局当前子树，并将父节点置于首尾子节点的中点。
        def place(node: TreeNode, depth: int) -> _NodeLayout:
            """递归放置一个节点，并返回该节点的布局信息给其父节点使用。"""
            nonlocal next_leaf_x, max_depth
            max_depth = max(max_depth, depth)
            width, height = self._measure_node(node.label)

            child_layouts = [place(child, depth + 1) for child in node.children]
            if child_layouts:
                first_center = child_layouts[0].x + child_layouts[0].width / 2
                last_center = child_layouts[-1].x + child_layouts[-1].width / 2
                x = (first_center + last_center - width) / 2
            else:
                x = next_leaf_x
                next_leaf_x += width + _NODE_HORIZONTAL_GAP

            layout = _NodeLayout(
                x=x,
                y=depth * _NODE_VERTICAL_GAP,
                width=width,
                height=height,
            )
            layouts[id(node)] = layout
            return layout

        place(root_node, 0)
        # 父节点可能比唯一子节点更宽，导致初始 x 为负数；整体右移可保证任何
        # 节点都不会越过 Canvas 左边界，同时不改变父子之间的相对位置。
        minimum_x = min((layout.x for layout in layouts.values()), default=0.0)
        if minimum_x < 0:
            layouts = {
                node_id: _NodeLayout(
                    x=layout.x - minimum_x,
                    y=layout.y,
                    width=layout.width,
                    height=layout.height,
                )
                for node_id, layout in layouts.items()
            }
        content_width = max(
            (layout.x + layout.width for layout in layouts.values()),
            default=0.0,
        )
        content_height = max(
            (layout.y + layout.height for layout in layouts.values()),
            default=(max_depth + 1) * _NODE_VERTICAL_GAP,
        )
        return layouts, content_width, content_height

    # 此方法按父子关系先绘制所有连线，使节点矩形始终位于连线之上。
    def _draw_edges(self, node: TreeNode, layouts: dict[int, _NodeLayout]) -> None:
        """递归绘制 node 到每个子节点的连接线，并继续处理更深层的边。

        线条从父节点底部中央连接到子节点顶部中央；坐标加入统一画布外边距，
        使根节点不会紧贴 Canvas 左上角。该方法不创建文字或矩形，职责仅限边。
        """
        parent = layouts[id(node)]
        parent_x = _CANVAS_PADDING + parent.x + parent.width / 2
        parent_y = _CANVAS_PADDING + parent.y + parent.height

        for child in node.children:
            child_layout = layouts[id(child)]
            child_x = _CANVAS_PADDING + child_layout.x + child_layout.width / 2
            child_y = _CANVAS_PADDING + child_layout.y
            self._canvas.create_line(parent_x, parent_y, child_x, child_y, fill="#778399", width=2)
            self._draw_edges(child, layouts)

    # 此方法递归绘制所有节点的圆角近似矩形与居中文字。
    def _draw_nodes(self, node: TreeNode, layouts: dict[int, _NodeLayout]) -> None:
        """根据布局表绘制节点外框和标签，并继续绘制所有子节点。

        tkinter Canvas 原生矩形没有圆角，因此使用简洁的浅色矩形配合深色边框，
        优先保证结构可读性。节点标签直接来自 TreeNode，不在 UI 层改写 AST
        数据，确保视觉内容可追溯到真实编译结果。
        """
        layout = layouts[id(node)]
        left = _CANVAS_PADDING + layout.x
        top = _CANVAS_PADDING + layout.y
        right = left + layout.width
        bottom = top + layout.height
        self._canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            fill="#edf4ff",
            outline="#336b9b",
            width=2,
        )
        self._canvas.create_text(
            (left + right) / 2,
            (top + bottom) / 2,
            text=node.label,
            font=self._tree_font,
            fill="#15253b",
        )

        for child in node.children:
            self._draw_nodes(child, layouts)

    # 此方法根据节点标签的实际像素宽度计算节点矩形尺寸。
    def _measure_node(self, label: str) -> tuple[float, float]:
        """测量标签文本并加上统一内边距，返回 Canvas 节点的宽和高。

        使用 tkinter 的真实字体测量值，而不是写死字符数，能让中文、英文、数字
        和引号字符串都得到合适宽度。高度保持统一，使不同层级的连线整齐。
        """
        width = self._tree_font.measure(label) + _NODE_HORIZONTAL_PADDING * 2
        height = self._tree_font.metrics("linespace") + _NODE_VERTICAL_PADDING * 2
        return float(max(width, 72)), float(height)

    # 此方法清除 Canvas 中旧的树或错误内容，为下一次渲染准备空白区域。
    def _clear_canvas(self) -> None:
        """删除 Canvas 的全部图元，并将滚动区域恢复为最小可见范围。"""
        self._canvas.delete("all")
        self._canvas.configure(scrollregion=(0, 0, 1, 1))

    # 此方法根据 ParseError 的 1-based 行列在输入框中高亮对应字符。
    def _highlight_error_position(self, line: int, column: int) -> None:
        """在 SQL Text 控件中标记错误位置，并自动滚动到可见范围。

        Text 的列号从 0 开始，而 ParseError 的列号从 1 开始，因此需要减一。
        EOF 位置可能位于行末，tkinter 会自动将索引限制到有效位置；高亮一个
        字符即可在答辩演示中清楚提示错误所在。
        """
        start = f"{line}.{max(column - 1, 0)}"
        end = f"{line}.{max(column, 1)}"
        self._sql_input.tag_add("parse_error", start, end)
        self._sql_input.see(start)

    # 此方法移除上一轮解析留下的错误高亮，避免成功后仍显示红色背景。
    def _clear_error_highlight(self) -> None:
        """清除输入框全文范围内的 parse_error 标记。"""
        self._sql_input.tag_remove("parse_error", "1.0", "end")


# 此入口函数创建 tkinter 根窗口、构造应用控制器，并启动本地事件循环。
def main() -> None:
    """启动 AST 可视化程序。

    必须通过 ``python -m tests.A_tests.ast_visualizer`` 从项目根目录启动，保证 Python
    能找到 compiler、contracts 和 tests 包。main 不接收参数，也不写入文件，
    关闭窗口后程序即结束。
    """
    root = tk.Tk()
    AstVisualizerApp(root)
    root.mainloop()


# 此标准模块入口保证“被 import 用于测试”时不自动弹窗，仅直接运行时才启动 UI。
if __name__ == "__main__":
    main()
