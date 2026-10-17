"""v0.6.2 后台处理 Worker。

设计要点：
- :class:`RedactionWorker` 是 :class:`QObject`（**不**继承 :class:`QThread`），
  通过 :meth:`moveToThread` 投递到后台线程。Qt 官方推荐这种"线程对象 vs 业务对象"分离。
- 与 CLI 的 :func:`redactor.cli._process_one` **同源但不共用**：本模块独立实现
  :func:`process_one_for_gui`，避免 GUI 依赖 CLI 的 argparse.Namespace 与私有函数。
  两份实现的语义必须保持一致（参见 TestProcessOneForGui 反向验证）。
- 取消机制：**cooperative**（协作式）。Worker 在每个文件开头检查
  ``self._cancel_requested``，不打断正在处理的文件。GUI 层 Cancel 按钮触发
  :meth:`request_cancel`，当前文件跑完后退出循环。

Signal 接口（跨线程自动 QueuedConnection）：
    - ``finished()`` —— 所有文件处理完（包括被取消的循环退出）
    - ``cancelled()`` —— 用户取消后发射（区别于自然 finished）
    - ``progress(int, int, str)`` —— (current_idx, total, current_filename)
    - ``file_done(object)`` —— :class:`FileResult`（**每个文件跑完立刻发**，不等汇总）
    - ``log_message(str)`` —— 日志行（MainWindow 接住写到 LogPanel）

铁律：
- :class:`FileResult` 不存敏感原文，跨线程传递安全。
- ``_cancel_requested`` 是 bool 而非 threading.Event：跨线程靠 Qt Signal
  QueuedConnection 的 happens-before 保证（每个 Signal 投递都会形成 memory barrier）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Sequence

from PySide6 import QtCore

from redactor.core.engine import RedactionEngine
from redactor.core.lexicon import default_lexicon_path, load_lexicon
from redactor.core.models import FileResult
from redactor.core.rules import load_rule_dir, load_rules
from redactor.core.scopes import ScopeEntry
from redactor.cli import _default_rules_dir  # noqa: F401  —— 复用 CLI 的"内置规则目录定位"
from redactor.handlers import get_handler
from redactor.handlers.base import HandlerError


# ============================================================ engine 构造
def build_engine_for_gui(
    *,
    rules_dir: Optional[Path] = None,
    force_numbered: bool = False,
    use_lexicon: bool = True,
    scope_entries: Optional[Sequence[ScopeEntry]] = None,
    scope_tier: Optional[str] = None,
    scope_strict_lead: bool = False,
) -> RedactionEngine:
    """为 GUI 构造 RedactionEngine（无 argparse.Namespace 依赖）。

    与 :func:`redactor.cli._build_engine` 同源；GUI 层不构造 Namespace，
    直接传 keyword args。规则目录定位复用 ``_default_rules_dir``（含
    REDACTOR_RULES_DIR 环境变量回退）。

    Args:
        rules_dir: 规则目录；None 时用内置 ``rules/``。
        force_numbered: 强制编号占位符。
        use_lexicon: 是否加载默认词库。
        scope_entries: P7 范围脱敏条目（空列表 = 不启用）。
        scope_tier: 范围脱敏严格度覆盖。
        scope_strict_lead: 范围脱敏首字黑名单。
    """
    rules_root = rules_dir if rules_dir is not None else _default_rules_dir()
    if rules_root.is_dir():
        rules = load_rule_dir(rules_root)
    else:
        rules = load_rules(rules_root)

    lexicon_entries = []
    if use_lexicon:
        default_path = default_lexicon_path()
        if default_path is not None:
            lexicon_entries = load_lexicon(
                default_path, reserved_names=[r.name for r in rules]
            )

    return RedactionEngine(
        rules,
        force_numbered=force_numbered,
        lexicon_entries=lexicon_entries,
        scope_entries=scope_entries,
        scope_tier=scope_tier,
        scope_strict_lead=scope_strict_lead,
    )


# ============================================================ 单文件处理
def process_one_for_gui(
    source: Path,
    output_root: Path,
    input_root: Path,
    engine: RedactionEngine,
    dry_run: bool,
    *,
    overwrite_output: bool = True,
) -> FileResult:
    """处理单个文件，捕获所有异常转 :class:`FileResult`（保证批量任务不中断）。

    与 :func:`redactor.cli._process_one` **语义相同**：

    - 找不到 handler → ``status="failed", reason=HandlerError.UNSUPPORTED``
    - 源 == 输出 → ``status="failed", reason=HandlerError.PERMISSION``
    - 源不存在 → ``status="failed", reason=HandlerError.IO_ERROR``
    - HandlerError 抛出 → 捕获转 FileResult（带原 code/message）
    - 通用 Exception → ``status="failed", reason="UNKNOWN", message=str(exc)``

    实现差异：直接调 ``handler.process``（5 个参数），不构造 argparse.Namespace。
    """
    started = time.perf_counter()
    source_str = str(source)
    try:
        handler = get_handler(source)
        output = _relative_output_path(source, input_root, output_root)
        result = handler.process(
            source,
            output,
            engine,
            dry_run=dry_run,
            overwrite_output=overwrite_output,
        )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result
    except HandlerError as exc:
        return FileResult(
            source=source_str,
            status="failed",
            reason=exc.code,
            message=exc.message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except PermissionError as exc:
        return FileResult(
            source=source_str,
            status="failed",
            reason=HandlerError.PERMISSION,
            message=str(exc),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except FileNotFoundError as exc:
        return FileResult(
            source=source_str,
            status="failed",
            reason=HandlerError.IO_ERROR,
            message=str(exc),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 —— Worker 内兜底，保证批量任务不中断
        return FileResult(
            source=source_str,
            status="failed",
            reason="UNKNOWN",
            message=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _relative_output_path(source: Path, input_root: Path, output_root: Path) -> Path:
    """与 :func:`redactor.cli._relative_output_path` 同源；保持 GUI 独立于 CLI。

    行为：
        - 把 ``source`` 相对 ``input_root`` 的相对路径保留到 ``output_root`` 下；
        - 加 ``_redacted`` 后缀（与 CLI 一致）。
    """
    try:
        relative = source.resolve().relative_to(input_root.resolve())
    except ValueError:
        relative = Path()

    if relative == Path() or relative.name == "":
        relative = Path(source.name)

    out_path = (output_root / relative).resolve()
    # 加 _redacted 后缀（与 CLI 一致：xxx.docx → xxx_redacted.docx）
    return out_path.with_name(out_path.stem + "_redacted" + out_path.suffix)


# ============================================================ Worker
class RedactionWorker(QtCore.QObject):
    """后台处理 Worker（QObject，moveToThread 用）。

    信号（见模块级 docstring）。
    """

    # 所有文件处理完（包括取消循环退出）。**只发一次**。
    finished = QtCore.Signal()
    # 用户主动取消时发（区别于自然 finished）。
    cancelled = QtCore.Signal()
    # (current_idx, total, current_filename) —— current_idx 从 0 开始
    progress = QtCore.Signal(int, int, str)
    # FileResult —— **每个文件跑完立刻发**，不等汇总（防崩溃丢结果）
    file_done = QtCore.Signal(object)
    # 日志行（MainWindow 接到写到 LogPanel）
    log_message = QtCore.Signal(str)

    def __init__(
        self,
        paths: Sequence[str],
        output_root: Path,
        dry_run: bool,
        engine: RedactionEngine,
        *,
        overwrite_output: bool = True,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._paths: List[str] = [str(p) for p in paths]
        self._output_root = Path(output_root)
        self._dry_run = dry_run
        self._engine = engine
        self._overwrite_output = overwrite_output
        self._cancel_requested = False

        # input_root：单文件批次取首项父目录，多文件批次取共同父目录；
        # 简化方案：统一取首项父目录（与 CLI 单文件输入行为一致）。
        if self._paths:
            self._input_root = Path(self._paths[0]).parent
        else:
            self._input_root = self._output_root

    # ----------------------------------------------------- 公开 API
    @QtCore.Slot()
    def request_cancel(self) -> None:
        """请求取消。Worker 在每个文件开头检查此标志，**不打断正在处理的文件**。"""
        self._cancel_requested = True

    def is_cancel_requested(self) -> bool:
        """公开：仅供测试与调试观察。"""
        return self._cancel_requested

    # ----------------------------------------------------- 主循环
    @QtCore.Slot()
    def process(self) -> None:
        """处理 self._paths 中的每个文件。

        每个文件跑完：
            1. emit progress(idx+1, total, name)
            2. emit log_message("开始：...")
            3. 调 process_one_for_gui()
            4. **emit file_done(result) —— 不等汇总**
            5. emit log_message("完成：...")

        取消判定在循环开头；当前文件跑完才退出，避免半成品。
        """
        total = len(self._paths)
        if total == 0:
            self.log_message.emit("无文件可处理")
            self.finished.emit()
            return

        for idx, p in enumerate(self._paths):
            if self._cancel_requested:
                self.log_message.emit(f"已取消（完成 {idx} / 共 {total}）")
                self.cancelled.emit()
                self.finished.emit()
                return

            name = Path(p).name
            self.progress.emit(idx, total, name)
            self.log_message.emit(f"[{idx + 1}/{total}] 开始：{name}")

            result = process_one_for_gui(
                Path(p),
                self._output_root,
                self._input_root,
                self._engine,
                self._dry_run,
                overwrite_output=self._overwrite_output,
            )

            # **每文件跑完立刻发 file_done**（防崩溃丢结果 + 实时进度）
            self.file_done.emit(result)

            # 日志：根据 status 分色（MainWindow 据此上色，留 hook）
            status_zh = {
                "ok": "成功",
                "redacted": "成功",
                "skipped": "跳过",
                "failed": "失败",
            }.get(result.status, result.status)
            msg_extra = result.message or "OK"
            self.log_message.emit(
                f"[{idx + 1}/{total}] {status_zh}：{name}"
                f"（命中 {result.total_hits} 条，{result.elapsed_ms}ms；{msg_extra}）"
            )

        self.log_message.emit(f"完成：{total} / {total}")
        self.finished.emit()


__all__ = [
    "RedactionWorker",
    "build_engine_for_gui",
    "process_one_for_gui",
]
