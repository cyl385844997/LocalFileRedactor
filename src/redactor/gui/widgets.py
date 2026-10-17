"""GUI 复用控件（v0.6.1 拖拽 + 批量版）。

每个控件都做**单一职责**的小型封装，方便 v0.6.2 / v0.6.3 渐进替换实现。

v0.6.0 → v0.6.1 增量:
- :class:`DropZoneList`：
    - 白名单从 ``redactor.handlers.supported_extensions()`` 同步（不再硬编码 .pptx）
    - 新 ``add_files()`` 统一入口，拖拽 / 测试 / 后续对话框全走此
    - 拖拽事件真正实现：dragEnter / dragMove / dropEvent
    - 文件大小格式化 ``_fmt_size``（纯函数，方便单测）
    - 规范化绝对路径 ``normalize_path``（Windows normcase 折叠大小写）
    - 重复检测基于"规范化绝对路径"，不依赖文件名 + 大小
    - 拖入目录不递归，仅计入 ``dirs`` 并由 MainWindow 写日志
    - 信号 ``files_added = Signal(dict)`` / ``files_cleared = Signal()`` 供 MainWindow 接到日志面板
- :class:`LogPanel` — v0.6.1 仍只读；v0.6.2 改滚动
- :class:`PreviewPanel` — v0.6.3 才真正接命中表
- :class:`StatusProgressBar` — 不动

被拒文件**不静默丢弃**——``add_files`` 返回 ``rejected_reasons`` 列表，
``MainWindow`` 据此写 LogPanel 提示用户。UI 层不重复判断业务逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets


# ============================================================ 工具函数
def _fmt_size(n: int) -> str:
    """字节数 → 人类可读字符串。

    规则:
        < 1024 B       → "N B"
        < 1 MB         → "X.X KB"（1 位小数）
        >= 1 MB        → "X.X MB"（1 位小数）

    设计为**纯函数**以便 pytest 直接单测。
    """
    if n < 0:
        raise ValueError(f"size must be non-negative, got {n}")
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def normalize_path(path: str) -> str:
    """规范化绝对路径：``resolve`` + Windows ``normcase`` 折叠大小写。

    用作去重键，避免 ``C:/a`` vs ``C:\\A`` vs ``./a/../a`` 被判为不同。
    """
    p = Path(path).resolve()
    if os.name == "nt":
        return os.path.normcase(str(p))
    return str(p)


# ============================================================ 拖拽区
class DropZoneList(QtWidgets.QListView):
    """拖拽文件占位列表（v0.6.1：真正接收 + 过滤 + 去重）。

    白名单默认从 :func:`redactor.handlers.supported_extensions` 同步，
    保证 GUI 与引擎一致；存在导入失败则退化到内置 11 元素 fallback。
    """

    #: fallback 白名单（handlers 不可用时启用），与 REGISTRY 当前 11 项一致
    #: 完整列表见 :data:`redactor.handlers.supported_extensions`
    _FALLBACK_SUFFIXES: Tuple[str, ...] = (
        ".csv", ".docx", ".json", ".log", ".markdown",
        ".md", ".pdf", ".txt", ".xlsx", ".yaml", ".yml",
    )

    #: add_files 完成时发射；参数是 add_files 的返回 dict
    files_added = QtCore.Signal(dict)
    #: clear() 完成时发射
    files_cleared = QtCore.Signal()
    #: v0.6.3：双击某行时发射；参数是 QModelIndex
    #: （MainWindow 从 UserRole 取 path / output_path 决定打开哪个）
    item_activated = QtCore.Signal(QtCore.QModelIndex)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)

        self._model = QtGui.QStandardItemModel(self)
        self.setModel(self._model)
        # v0.6.3：双击行 → 发射 item_activated（MainWindow 接了去开文件）
        try:
            self.activated.connect(self.item_activated)
        except (TypeError, AttributeError):
            # 极老版本 PySide6 的 activated 签名不同 —— 退化为不接信号
            # （双击只是附加能力，失败不影响拖拽 / 批量 / 处理）
            pass

        # 白名单（从 handlers 同步，失败回退）
        self._suffixes: Tuple[str, ...] = self._load_suffixes()

        # 占位文字：动态拼出支持格式
        joined = " / ".join(self._suffixes)
        self._placeholder = f"拖入文件到此处…\n（支持 {joined}）"
        # 占位文字拆成两行（小字提示）由 paintEvent 画

    # ----------------------------------------------------- 内部工具
    @staticmethod
    def _load_suffixes() -> Tuple[str, ...]:
        """从 handlers 同步白名单；失败回退到 _FALLBACK_SUFFIXES。"""
        try:
            from redactor.handlers import supported_extensions
            suffixes = tuple(supported_extensions())
            if suffixes:
                return tuple(s.lower() for s in suffixes)
        except Exception:
            pass
        return DropZoneList._FALLBACK_SUFFIXES

    def suffixes(self) -> Tuple[str, ...]:
        """公开：返回当前白名单（只读 tuple）。"""
        return self._suffixes

    def is_supported_suffix(self, path: str) -> bool:
        """公开：判断单文件是否在白名单内（大小写不敏感）。"""
        return Path(path).suffix.lower() in self._suffixes

    # ----------------------------------------------------- 拖拽事件
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: D401
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        # 至少一个 URL 是"白名单内的本地文件"才接受（目录与非白名单会被 Qt 拒绝）
        if any(self._url_is_acceptable(u) for u in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:  # noqa: D401
        # Windows 上必需，否则 drop 不触发；逻辑同 dragEnter
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        if any(self._url_is_acceptable(u) for u in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: D401
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        urls = event.mimeData().urls()
        local_paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        result = self._handle_local_paths(local_paths)
        if result["added"] > 0:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _handle_local_paths(self, local_paths: List[str]) -> Dict[str, object]:
        """dropEvent 的纯逻辑部分：处理一批本地路径。

        与 :meth:`add_files` 等价但为独立方法，便于测试跳过 QDropEvent 构造
        （PySide6 在 offscreen 模式下构造 QDropEvent 可能触发 access violation）。
        """
        return self.add_files(local_paths)

    @staticmethod
    def _url_is_acceptable(url: QtCore.QUrl) -> bool:
        """dragEnter 用：URL 是本地文件 + 后缀在白名单内。"""
        if not url.isLocalFile():
            return False
        path = url.toLocalFile()
        # 目录不算"可接受"——避免文件夹被错误地标记为 accepted
        if Path(path).is_dir():
            return False
        # 大小写不敏感：手写 lower
        from pathlib import Path as _P
        return _P(path).suffix.lower() in DropZoneList._load_suffixes()

    # ----------------------------------------------------- 核心 API
    def add_files(self, paths: Iterable[str]) -> Dict[str, object]:
        """接收一批原始路径（拖拽/对话框），过滤 + 去重 + 插入。

        Returns:
            {
                "added": int,            # 实际新增到列表的条目数
                "rejected": int,         # 不在白名单的拒绝数
                "duplicated": int,       # 已存在规范化路径的重复数
                "dirs": int,             # 跳过的目录数
                "rejected_reasons": [
                    {"file": str, "reason": str}, ...
                ],
            }
            - "rejected_reasons" 含**所有**被拒条目（rejected + 重复），
              方便 MainWindow 直接写日志，无需重判。

        行为约定:
            - 不存在的路径：忽略（不入计数）
            - 目录：计入 ``dirs``，不进 rejected_reasons
            - 不支持格式：计入 ``rejected`` + 写 rejected_reasons("不支持的格式: X")
            - 重复（规范化路径已在列表）：计入 ``duplicated`` + 写 rejected_reasons
        """
        # 已规范化路径集合，用于去重
        existing = {self._model.item(r).data(QtCore.Qt.ItemDataRole.UserRole)["normalized"]
                    for r in range(self._model.rowCount())}

        added = 0
        rejected = 0
        duplicated = 0
        dirs = 0
        rejected_reasons: List[Dict[str, str]] = []

        for raw in paths:
            if not raw:
                continue
            try:
                p = Path(raw)
            except (TypeError, ValueError):
                continue

            # 不存在的路径：忽略
            if not p.exists():
                continue

            # 目录：不递归（首版裁定，列已知边界）
            if p.is_dir():
                dirs += 1
                continue

            # 规范化路径
            try:
                norm = normalize_path(str(p))
            except OSError:
                # 路径解析失败（如盘符不存在）：忽略
                continue

            # 重复
            if norm in existing:
                duplicated += 1
                rejected_reasons.append({"file": str(p), "reason": "重复文件（已存在列表中）"})
                continue

            # 白名单
            if not self.is_supported_suffix(str(p)):
                rejected += 1
                reason = f"不支持的格式: {p.suffix or '(无扩展名)'}"
                rejected_reasons.append({"file": str(p), "reason": reason})
                continue

            # 通过：插入 model
            item = QtGui.QStandardItem()
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            display = f"{p.name}    {_fmt_size(size):>10}    待处理"
            item.setText(display)
            item.setToolTip(f"{p}\n{size} 字节")
            item.setData(
                {
                    "path": str(p),
                    "normalized": norm,
                    "size": size,
                    "status": "待处理",
                    # v0.6.3：处理成功后由 MainWindow 回填（用于双击打开输出）
                    "output_path": None,
                },
                QtCore.Qt.ItemDataRole.UserRole,
            )
            self._model.appendRow(item)
            existing.add(norm)
            added += 1

        result = {
            "added": added,
            "rejected": rejected,
            "duplicated": duplicated,
            "dirs": dirs,
            "rejected_reasons": rejected_reasons,
        }
        if added > 0 or rejected > 0 or duplicated > 0 or dirs > 0:
            self.files_added.emit(result)
        return result

    def file_paths(self) -> List[str]:
        """按列表顺序返回**原始**绝对路径（不做二次规范化）。"""
        out: List[str] = []
        for r in range(self._model.rowCount()):
            data = self._model.item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            if data:
                out.append(data["path"])
        return out

    def count(self) -> int:
        return self._model.rowCount()

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())
        self.files_cleared.emit()

    # ----------------------------------------------------- v0.6.3 双击打开
    def item_data(self, index: QtCore.QModelIndex) -> Dict[str, object]:
        """按索引取 UserRole 数据；无数据返回 {}（不抛）。"""
        try:
            item = self._model.itemFromIndex(index)
        except (RuntimeError, AttributeError):
            return {}
        if item is None:
            return {}
        data = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return dict(data) if data else {}

    def activate_row(self, row: int) -> Dict[str, object]:
        """公开（供测试 / 自动化）：触发第 row 行的"双击"效果。

        走 ``view.activated``（真实双击信号）而非直接发 :attr:`item_activated`，
        这样 MainWindow 接的信号链也会被触发 —— 与用户真双击行为一致。

        Returns:
            该行的 UserRole 数据（无数据返回 {}）。
        """
        if row < 0 or row >= self._model.rowCount():
            return {}
        index = self._model.index(row, 0)
        data = self.item_data(index)
        # QAbstractItemView.activated 只能由内部交互触发，这里用
        # QItemSelectionModel 模拟"双击选中"，再用 view.activated 桥接。
        # offscreen 下 view.activated 不由程序化选中触发，
        # 所以显式把 item_activated 也发一次 —— 两个出口 MainWindow 都接了。
        self.item_activated.emit(index)
        return data

    def set_output_path(self, source_path: str, output_path: str) -> bool:
        """回填某行对应的输出文件路径（MainWindow 在 file_done 时调）。

        Returns:
            True 找到并回填；False 未找到该源文件行。
        """
        target = normalize_path(source_path)
        for r in range(self._model.rowCount()):
            item = self._model.item(r)
            data = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if data and data.get("normalized") == target:
                item.setData({**data, "output_path": output_path},
                             QtCore.Qt.ItemDataRole.UserRole)
                return True
        return False

    def output_path_for(self, source_path: str) -> Optional[str]:
        """查某源文件行已回填的 output_path；未回填返回 None。"""
        target = normalize_path(source_path)
        for r in range(self._model.rowCount()):
            data = self._model.item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            if data and data.get("normalized") == target:
                return data.get("output_path")  # type: ignore[return-value]
        return None

    def display_text(self, row: int) -> str:
        """公开（供测试）：取第 row 行的显示文本。"""
        if row < 0 or row >= self._model.rowCount():
            return ""
        item = self._model.item(row)
        return item.text() if item is not None else ""

    def refresh_viewport(self) -> None:
        """公开（供测试）：强制重绘（占位文字自绘依赖 viewport）。"""
        self.viewport().update()

    # ----------------------------------------------------- 向后兼容
    def all_paths(self) -> List[str]:
        """v0.6.0 旧 API；保留为 ``file_paths()`` 的别名。"""
        return self.file_paths()

    def add_paths(self, paths: Iterable[str]) -> int:
        """v0.6.0 旧 API：只返回新增数；新代码请用 ``add_files()``。"""
        return self.add_files(paths)["added"]  # type: ignore[return-value]

    def set_placeholder(self, text: str) -> None:
        """v0.6.0 旧 API：覆盖占位文字。"""
        self._placeholder = text
        self.viewport().update()

    # ----------------------------------------------------- 绘制
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: D401
        super().paintEvent(event)
        if self._model.rowCount() != 0:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.save()
        color = self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
        painter.setPen(color)
        rect = self.viewport().rect()
        # 两行文字：第一行主提示，第二行小字支持格式
        lines = self._placeholder.split("\n")
        # 用一个临时 QRect 把第二行画在第一行下方
        first_rect = QtCore.QRect(rect)
        first_rect.setHeight(rect.height() // 2)
        painter.drawText(
            first_rect,
            QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignHCenter,
            lines[0],
        )
        if len(lines) > 1:
            second_rect = QtCore.QRect(rect)
            second_rect.setTop(first_rect.bottom())
            font = painter.font()
            font.setPointSizeF(max(font.pointSizeF() - 1.5, 7.0))
            painter.setFont(font)
            painter.drawText(
                second_rect,
                QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignHCenter,
                lines[1],
            )
        painter.restore()


# ============================================================ 日志区
class LogPanel(QtWidgets.QPlainTextEdit):
    """只读日志面板。

    v0.6.2 改为滚动追加 + 颜色（info/warn/error 三色）；
    v0.6.1 仍仅 ``append_line``，MainWindow 把 add_files 结果转成可读行写入。
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)  # 防止用户长时间挂日志导致 OOM
        self.setPlaceholderText("处理日志将在此处显示")

    # v0.6.2 worker 调用
    def append_line(self, line: str) -> None:
        self.appendPlainText(line)


# ============================================================ 预览区
class PreviewPanel(QtWidgets.QTabWidget):
    """预览面板 —— 两个 tab：命中表 + 打开输出。

    v0.6.3 接通：
        - 命中表 4 列：``#`` / ``文件`` / ``规则`` / ``位置``
          （"位置"列填命中次数，**不含原文**；详细 locations 收进 tooltip）
        - ``add_result()`` 每文件调用一次（v0.6.2 铁律：file_done 逐个发，
          所以表格是**逐行追加**，不是最后一次性灌）
        - ``set_hit_limit()`` 默认 500 行，超出截断并在顶部 QLabel 提示
        - 输出列表双击 → ``output_activated(str)`` 信号
    """

    #: 默认命中表行数上限（v0.6.3 §4 裁定）
    DEFAULT_HIT_LIMIT = 500

    #: 输出列表项被双击/回车激活；参数是路径字符串
    output_activated = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- tab 0：命中表 ----
        self._hits_wrap = QtWidgets.QWidget(self)
        hits_layout = QtWidgets.QBoxLayout(
            QtWidgets.QBoxLayout.Direction.TopToBottom, self._hits_wrap
        )
        hits_layout.setContentsMargins(0, 0, 0, 0)
        hits_layout.setSpacing(2)

        # 顶部提示条（超限时显示"仅展示前 500 条"）
        self._hit_notice = QtWidgets.QLabel("", self._hits_wrap)
        self._hit_notice.setWordWrap(True)
        self._hit_notice.setStyleSheet(
            "color: #8a5a00; background: #fff4d6; padding: 3px 6px;"
            "border: 1px solid #e0c060;"
        )
        self._hit_notice.setVisible(False)
        self._notice_shown = False      # 显式标记：offscreen 下 isVisible() 不可靠
        hits_layout.addWidget(self._hit_notice)

        self._hits_table = QtWidgets.QTableWidget(0, 4, self._hits_wrap)
        self._hits_table.setHorizontalHeaderLabels(["#", "文件", "规则", "位置"])
        header = self._hits_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hits_layout.addWidget(self._hits_table)
        self.addTab(self._hits_wrap, "命中表")

        self._hit_limit = self.DEFAULT_HIT_LIMIT
        self._hits_dropped = 0          # 因超限被丢弃的命中数
        self._hits_seen = 0             # 累计见到的命中数（含被丢弃的）

        # ---- tab 1：输出列表 ----
        self._open_output = QtWidgets.QListWidget(self)
        self._open_output.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self._open_output.itemActivated.connect(
            lambda item: self.output_activated.emit(item.text())
        )
        self.addTab(self._open_output, "打开输出")

    # ---------------------------------------------------------- 命中表
    def set_hit_limit(self, n: int) -> None:
        """设置命中表行数上限（<=0 表示不限制）。"""
        self._hit_limit = int(n)

    def hit_limit(self) -> int:
        return self._hit_limit

    def clear_hits(self) -> None:
        """清空命中表 + 重置计数 + 隐藏顶部提示。"""
        self._hits_table.setRowCount(0)
        self._hits_dropped = 0
        self._hits_seen = 0
        self._notice_shown = False
        self._hit_notice.setVisible(False)

    def hit_count(self) -> int:
        """当前表格实际行数。"""
        return self._hits_table.rowCount()

    def hits_dropped(self) -> int:
        """因超限被丢弃的命中数。"""
        return self._hits_dropped

    def notice_text(self) -> str:
        """顶部提示条文本（无提示时返回空串）。"""
        return self._hit_notice.text()

    def notice_visible(self) -> bool:
        """顶部提示条是否处于"提示中"状态。

        用显式标记而不是 ``QLabel.isVisible()`` —— 后者在 offscreen /
        窗口未 show 的情况下恒为 False，测试会误判。
        """
        return self._notice_shown

    def cell_text(self, row: int, col: int) -> str:
        """取单元格文本（测试用；越界返回空串）。"""
        if row < 0 or row >= self._hits_table.rowCount():
            return ""
        if col < 0 or col >= self._hits_table.columnCount():
            return ""
        item = self._hits_table.item(row, col)
        return item.text() if item is not None else ""

    def cell_tooltip(self, row: int, col: int) -> str:
        """取单元格 tooltip（测试用；越界返回空串）。"""
        if row < 0 or row >= self._hits_table.rowCount():
            return ""
        if col < 0 or col >= self._hits_table.columnCount():
            return ""
        item = self._hits_table.item(row, col)
        return item.toolTip() if item is not None else ""

    def add_hit(self, file_name: str, hit, row_no: Optional[int] = None) -> bool:
        """追加 1 行命中。

        Args:
            file_name: 源文件名（只写 ``Path.name``）。
            hit: :class:`~redactor.core.models.HitStat`。
            row_no: 显示用序号；None 时用累计计数。

        Returns:
            True 已追加；False 因超限被丢弃（此时计数 +1 但不建行）。

        安全：只写 ``rule_name`` / ``count`` / ``locations``，
        这些字段**本身不含任何原文**（见 :class:`HitStat` 的定义）。
        """
        self._hits_seen += 1
        if self._hit_limit > 0 and self._hits_table.rowCount() >= self._hit_limit:
            self._hits_dropped += 1
            self._refresh_notice()
            return False

        locations = tuple(getattr(hit, "locations", ()) or ())
        # ★ 裁定 1：位置列显示"命中 N 处"，**不展开 locations**；
        #   详细 locations 收进 tooltip，避免表格被几百个位置撑爆。
        pos_display = f"{getattr(hit, 'count', 0)} 处"
        tip = (
            f"规则：{getattr(hit, 'rule_name', '')}\n"
            f"命中：{getattr(hit, 'count', 0)} 处\n"
            f"位置：{', '.join(locations) if locations else '（未提供）'}"
        )

        r = self._hits_table.rowCount()
        self._hits_table.insertRow(r)
        values = [
            str(self._hits_seen if row_no is None else row_no),
            str(file_name),
            str(getattr(hit, "rule_name", "")),
            pos_display,
        ]
        for c, val in enumerate(values):
            item = QtWidgets.QTableWidgetItem(val)
            item.setToolTip(tip if c in (2, 3) else str(file_name))
            self._hits_table.setItem(r, c, item)
        return True

    def add_result(self, source: str, result) -> int:
        """把一个 :class:`FileResult` 的所有命中追加进表。

        Returns:
            实际追加的行数（不含被截断的）。
        """
        from pathlib import Path as _P
        name = _P(source).name
        added = 0
        for hit in getattr(result, "hits", ()) or ():
            if self.add_hit(name, hit):
                added += 1
        return added

    def _refresh_notice(self) -> None:
        """更新顶部提示条（超限时可见）。"""
        if self._hit_limit > 0 and self._hits_dropped > 0:
            self._hit_notice.setText(
                f"命中过多：共 {self._hits_seen} 条，仅展示前 {self._hit_limit} 条"
                f"（已省略 {self._hits_dropped} 条）。完整统计见日志与报告。"
            )
            self._notice_shown = True
            self._hit_notice.setVisible(True)
        else:
            self._notice_shown = False
            self._hit_notice.setVisible(False)

    # ---------------------------------------------------------- 输出列表
    def add_output_path(self, path: str) -> None:
        """登记一个输出文件 / 目录（双击用 :meth:`output_paths` 取回）。"""
        if not path:
            return
        self._open_output.addItem(str(path))

    def output_paths(self) -> List[str]:
        """输出列表当前所有路径（按插入顺序）。"""
        return [self._open_output.item(i).text()
                for i in range(self._open_output.count())]

    def clear_outputs(self) -> None:
        self._open_output.clear()

    def output_count(self) -> int:
        return self._open_output.count()

    def activate_output_row(self, row: int) -> str:
        """公开（供测试 / 自动化）：触发第 row 行的双击效果。"""
        if row < 0 or row >= self._open_output.count():
            return ""
        item = self._open_output.item(row)
        self.output_activated.emit(item.text())
        return item.text()


# ============================================================ 状态栏进度
class StatusProgressBar(QtWidgets.QProgressBar):
    """固定高度进度条 —— 用于状态栏右侧。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(14)
        self.setMinimumWidth(180)
        self.setRange(0, 1)
        self.setValue(0)
        self.setTextVisible(True)


__all__ = [
    "DEFAULT_SUFFIXES",  # 向后兼容（导出为 _FALLBACK_SUFFIXES）
    "DropZoneList",
    "LogPanel",
    "PreviewPanel",
    "StatusProgressBar",
    "_fmt_size",
    "normalize_path",
]


#: 向后兼容：原 DEFAULT_SUFFIXES 仍可 import
DEFAULT_SUFFIXES = DropZoneList._FALLBACK_SUFFIXES
