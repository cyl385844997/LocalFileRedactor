"""GUI 层（PySide6）—— P4 阶段实现。

**唯一允许 import PySide6 的包**。
core / recognizers / redactors / handlers / report 均不得依赖 GUI，
以保证核心库可被 CLI、GUI、以及将来的其他前端复用。

模块清单（v0.6.2 处理 + 进度接通版）:
- :mod:`redactor.gui.widgets` — 复用控件（拖拽区 / 日志区 / 预览区）
- :mod:`redactor.gui.main_window` — :class:`QMainWindow` 子类
- :mod:`redactor.gui.controller` — :class:`RedactorController`（线程 + 信号桥）
- :mod:`redactor.gui.worker` — :class:`RedactionWorker`（后台处理 + cooperative cancel）
- :mod:`redactor.gui.main` — :func:`main` 入口（QApplication 启动）
- :mod:`redactor.gui.__main__` — ``python -m redactor.gui`` 入口

裁定点（T1~T7，见 ``.verify/probe_gui/GUI_方案.md`` v1）:
- T1 PySide6、 T3 QThread+Signal/Slot、 T5 白名单、 T6 dry-run+打开输出
- 入口 ``redactor-gui``（pyproject.toml ``[project.scripts]``）

📌 v0.6.2 = 处理 + 进度：
    - RedactionWorker（QObject，moveToThread）调 handler.process
    - process_one_for_gui 与 cli._process_one 同源，独立实现
    - cooperative cancel：每文件开头检查 _cancel_requested，不打断正在处理的文件
    - Signal: started / progress / file_done / log_message / cancelled / finished
    - 关键：**每个文件跑完立刻 emit file_done**，不等汇总（防崩溃丢结果）
    - MainWindow 按钮状态机 4 态：empty / ready / running / done
    - 进度条接 progress 信号（setMaximum + setValue + setFormat）
    预览留 v0.6.3。
"""
from __future__ import annotations

__all__ = [
    "APP_NAME",
    "APP_ORG",
    "APP_VERSION",
    "MainWindow",
    "RedactorController",
    "RedactionWorker",
    "main",
]

APP_NAME = "LocalFileRedactor"
APP_ORG = "LocalFileRedactor"
APP_VERSION = "0.6.2"

# PySide6 imports are deferred to submodules so this package import never crashes
# even if PySide6 is not installed. The submodule-level imports fail loudly only
# when the GUI is actually launched or GUI call fails.
from redactor.gui.controller import RedactorController  # noqa: E402, F401
from redactor.gui.main_window import MainWindow  # noqa: E402, F401
from redactor.gui.main import main  # noqa: E402, F401
from redactor.gui.worker import RedactionWorker  # noqa: E402, F401
