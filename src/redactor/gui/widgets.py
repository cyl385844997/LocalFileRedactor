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

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)

        self._model = QtGui.QStandardItemModel(self)
        self.setModel(self._model)

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
                {"path": str(p), "normalized": norm, "size": size, "status": "待处理"},
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
    """预览面板 —— 两个 tab 占位（命中表 + 打开输出）。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._hits_table = QtWidgets.QTableWidget(0, 3, self)
        self._hits_table.setHorizontalHeaderLabels(["文件", "规则", "位置"])
        self._hits_table.horizontalHeader().setStretchLastSection(True)
        self.addTab(self._hits_table, "命中表")

        self._open_output = QtWidgets.QListWidget(self)
        self._open_output.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.addTab(self._open_output, "打开输出")

    # v0.6.3 调用
    def add_output_path(self, path: str) -> None:
        self._open_output.addItem(path)


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
