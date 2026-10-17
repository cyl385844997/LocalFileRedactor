"""v0.6.2 后台处理 Controller。

职责：
- 管理 :class:`QThread` + :class:`RedactionWorker` 的生命周期
- 每次 :meth:`start` 都 **新建** worker + thread（不复用，避免半死状态）
- :meth:`cancel` 调用 worker.request_cancel()（cooperative）
- 通过 Signal 把 Worker 信号转给 MainWindow

GUI 层只需：
    controller = RedactorController(window)
    controller.start(paths, output_root, dry_run)
    controller.cancel()

Controller 自己负责连接 Worker 信号 → MainWindow 槽；GUI 不需要直接处理 Worker。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from PySide6 import QtCore

from redactor.core.engine import RedactionEngine
from redactor.core.scopes import ScopeEntry

from redactor.gui.worker import RedactionWorker, build_engine_for_gui


class RedactorController(QtCore.QObject):
    """后台任务控制器。

    生命周期：
        new → start → （running）→ finished/cancelled → （可再次 start）

    线程模型：
        Worker 通过 moveToThread 投递到后台线程；UI 线程不阻塞。
    """

    # Controller 层信号：把 Worker 信号"原样转出"给 MainWindow
    # （让 MainWindow 仍可独立订阅，避免 Controller 死锁 Worker 信号链）
    started = QtCore.Signal(int)  # total
    progress = QtCore.Signal(int, int, str)  # (idx, total, name)
    file_done = QtCore.Signal(object)  # FileResult
    log_message = QtCore.Signal(str)
    finished = QtCore.Signal()  # 自然完成
    cancelled = QtCore.Signal()  # 用户取消

    def __init__(
        self,
        *,
        rules_dir: Optional[Path] = None,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._rules_dir = rules_dir
        self._thread: Optional[QtCore.QThread] = None
        self._worker: Optional[RedactionWorker] = None
        # P7 范围脱敏配置（由 GUI 设置对话框写入，start 时传给 engine）
        self._scope_entries: List[ScopeEntry] = []
        self._scope_tier: Optional[str] = None
        self._scope_strict_lead = False
        # P8 人名识别（默认关闭）
        self._enable_names = False
        self._strip_da_zi = True

    # ----------------------------------------------------- P7 范围脱敏
    def set_scope(
        self,
        entries: Sequence[ScopeEntry],
        *,
        tier: Optional[str] = None,
        strict_lead: bool = False,
    ) -> None:
        """设置范围脱敏配置（P7）。

        每次 :meth:`start` 都新建 engine，故这里只需**记住配置**，
        不必立即重建识别器。传空列表表示关闭范围脱敏。

        注意：**运行中不允许改** —— 已启动的 worker 用的是旧 engine，
        改了也要等下次 start 才生效，与用户直觉不符。运行中调用直接忽略
        并保持原配置。
        """
        if self.is_running():
            return
        self._scope_entries = list(entries or [])
        self._scope_tier = tier
        self._scope_strict_lead = strict_lead

    def scope_entries(self) -> List[ScopeEntry]:
        """当前范围脱敏配置（只读副本）。"""
        return list(self._scope_entries)

    # ----------------------------------------------------- P8 人名识别
    def set_names(self, *, enabled: bool, strip_da_zi: bool = True) -> None:
        """设置人名识别开关（P8）。

        与 :meth:`set_scope` 同样**运行中拒绝** —— 已启动的 worker
        用的是旧 engine，改了要等下次 start 才生效。
        """
        if self.is_running():
            return
        self._enable_names = bool(enabled)
        self._strip_da_zi = bool(strip_da_zi)

    def names_enabled(self) -> bool:
        """人名识别是否启用。"""
        return self._enable_names

    # ----------------------------------------------------- 状态查询
    def is_running(self) -> bool:
        """是否正在跑（含线程已建但未结束的所有阶段）。"""
        return self._thread is not None and self._thread.isRunning()

    def has_worker(self) -> bool:
        """是否有 Worker 对象（即使线程已结束）。"""
        return self._worker is not None

    # ----------------------------------------------------- 启动
    def start(
        self,
        paths: Sequence[str],
        output_root: Path,
        dry_run: bool,
        *,
        overwrite_output: bool = True,
    ) -> None:
        """启动后台任务。

        Raises:
            RuntimeError: 已有任务在跑（先 cancel 或等 finished）。
        """
        if self.is_running():
            raise RuntimeError("已有任务在跑；请先 cancel 或等 finished")

        # 1. 新建 engine（每个任务独立；engine 无状态但保证隔离）
        engine = build_engine_for_gui(
            rules_dir=self._rules_dir,
            scope_entries=self._scope_entries,
            scope_tier=self._scope_tier,
            scope_strict_lead=self._scope_strict_lead,
            enable_names=self._enable_names,
            strip_da_zi=self._strip_da_zi,
        )

        # 2. 新建 Worker + Thread
        self._thread = QtCore.QThread()
        self._worker = RedactionWorker(
            paths=paths,
            output_root=Path(output_root),
            dry_run=dry_run,
            engine=engine,
            overwrite_output=overwrite_output,
        )
        self._worker.moveToThread(self._thread)

        # 3. 信号连接（Worker → Controller → MainWindow）
        self._thread.started.connect(self._worker.process)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)

        # Worker 信号转发给 Controller 信号（MainWindow 订阅 Controller 信号）
        self._worker.progress.connect(self.progress)
        self._worker.file_done.connect(self.file_done)
        self._worker.log_message.connect(self.log_message)
        self._worker.cancelled.connect(self.cancelled)
        self._worker.finished.connect(self.finished)

        # 4. 启动
        self.started.emit(len(paths))
        self._thread.start()

    # ----------------------------------------------------- 取消
    def cancel(self) -> None:
        """请求取消（cooperative）。当前文件跑完后才退出循环。"""
        if self._worker is not None:
            self._worker.request_cancel()

    # ----------------------------------------------------- 内部
    def _on_thread_finished(self) -> None:
        """thread.finished 槽：清引用（让 is_running() 正确返回 False）。"""
        self._thread = None
        self._worker = None


__all__ = ["RedactorController"]
