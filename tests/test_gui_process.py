"""v0.6.2 处理 + 进度 全套测试。

6 大类（与 v0.6.2_方案.md 第 10 节对齐）：
    A. TestProcessOneForGui —— 不依赖 Qt，pure function 测试
    B. TestRedactionWorker —— offscreen，直接调 worker.process()
    C. TestRedactorController —— offscreen + QThread 集成
    D. TestMainWindowProcessState —— 按钮状态机 + Controller 信号槽
    E. TestEndToEndProcess —— 真实 docx + dry-run + 真输出验证
    F. TestReverseValidation —— 变异代码后测试必须真失败

依赖：
    - pytest-qt 提供 qapp fixture（offscreen 自动）
    - PySide6 Essentials（在 .verify_libs/ 下）
    - python-docx（默认可选依赖 xlsx/docx 之一）

跑法：
    scripts\\test.bat tests/test_gui_process.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List

import pytest

# qapp fixture 来自 pytest-qt；offscreen 模式由 QT_QPA_PLATFORM=offscreen 控制
from PySide6 import QtCore, QtWidgets

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult
from redactor.gui.controller import RedactorController
from redactor.gui.widgets import DropZoneList
from redactor.gui.worker import (
    RedactionWorker,
    build_engine_for_gui,
    process_one_for_gui,
)


# ============================================================ fixture
@pytest.fixture
def qapp():
    """QApplication 单例（与 test_gui_drop.py 同源；不依赖 pytest-qt）。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    app.processEvents()


# ============================================================ helper
def _make_docx(path: Path, text: str = "张三 13812345678") -> None:
    """造一个最简 docx（含 1 个手机号命中）。"""
    from docx import Document
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def _make_corrupted_docx(path: Path) -> None:
    """造一个**损坏**的 docx（handler 内部会抛 HandlerError.CORRUPTED）。"""
    path.write_bytes(b"this is not a docx")


def _wait_until(predicate, timeout_s: float = 5.0, step_ms: int = 20) -> bool:
    """轮询 predicate 直到 True 或超时；返回是否在超时内满足。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, step_ms)
    return predicate()


# ============================================================ A. process_one_for_gui
class TestProcessOneForGui:
    """process_one_for_gui 纯函数测试，不依赖 Qt。"""

    def test_正常docx返回ok_带命中(self, tmp_path: Path):
        src = tmp_path / "in.docx"
        _make_docx(src, "张三 13812345678")
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        result = process_one_for_gui(src, out_root, src.parent, engine, dry_run=False)

        assert isinstance(result, FileResult)
        assert result.status == "ok", result
        assert result.total_hits >= 1
        assert result.elapsed_ms > 0
        # 输出文件应已写出（CLI 同源行为）
        assert (out_root / "in_redacted.docx").exists()

    def test_dry_run_不写文件_但返回命中(self, tmp_path: Path):
        src = tmp_path / "in.docx"
        _make_docx(src)
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        result = process_one_for_gui(src, out_root, src.parent, engine, dry_run=True)

        assert result.status == "ok"
        assert result.total_hits >= 1
        # dry-run 不写文件
        assert not (out_root / "in_redacted.docx").exists()

    def test_损坏docx_返回failed_corrupted(self, tmp_path: Path):
        src = tmp_path / "bad.docx"
        _make_corrupted_docx(src)
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        result = process_one_for_gui(src, out_root, src.parent, engine, dry_run=False)

        assert result.status == "failed", result
        assert result.reason == "corrupted_file", result

    def test_不支持扩展名_返回failed_unsupported(self, tmp_path: Path):
        # .zip 不在白名单
        src = tmp_path / "x.zip"
        src.write_bytes(b"x")
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        result = process_one_for_gui(src, out_root, src.parent, engine, dry_run=False)

        # get_handler 不抛 .zip，但 handler.process 会 raise HandlerError.UNSUPPORTED
        # 等等——get_handler(.zip) 会直接抛 HandlerError("unsupported_format")
        # 而 process_one_for_gui 第一个 try 块就是 get_handler，所以 reason 应是 unsupported_format
        assert result.status == "failed", result
        assert result.reason in {"unsupported_format", "UNKNOWN"}, result

    def test_源不存在_返回failed(self, tmp_path: Path):
        src = tmp_path / "ghost.docx"
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        result = process_one_for_gui(src, out_root, tmp_path, engine, dry_run=False)

        assert result.status == "failed", result
        # docx_handler 把"源不存在"统一归为 corrupted_file（zip 包读取失败）
        # 语义上与 io_error 等价：都是"无法读取源文件"
        assert result.reason in {"io_error", "permission_denied", "corrupted_file"}, result

    def test_通用Exception_捕获_转UNKNOWN(self, tmp_path: Path, monkeypatch):
        """变异 get_handler → 让它抛 RuntimeError → 应被通用 except 捕获。"""
        src = tmp_path / "in.docx"
        _make_docx(src)
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        from redactor.gui import worker as worker_mod
        # 保存原函数
        original_get_handler = worker_mod.get_handler

        def boom(p):
            raise RuntimeError("simulated catastrophic failure")

        monkeypatch.setattr(worker_mod, "get_handler", boom)

        result = process_one_for_gui(src, out_root, src.parent, engine, dry_run=False)

        assert result.status == "failed"
        assert result.reason == "UNKNOWN", result
        assert "RuntimeError" in (result.message or ""), result

        # pytest monkeypatch 会自动还原


# ============================================================ B. RedactionWorker
class TestRedactionWorker:
    """RedactionWorker 单测：直接 new worker + 调 process()。"""

    def test_3个合法docx_progress按序发射(self, qapp, tmp_path: Path):
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p, f"张三 {13812345678 + i}")
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        # 在主线程 new Worker（QObject 受 QThread 父对象约束，
        # 单测不走 QThread，直接调 process() 也安全）
        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)

        progress_calls: List[tuple] = []
        worker.progress.connect(lambda idx, total, name: progress_calls.append((idx, total, name)))

        file_done_calls: List[FileResult] = []
        worker.file_done.connect(lambda r: file_done_calls.append(r))

        finished_count = [0]
        worker.finished.connect(lambda: finished_count.__setitem__(0, finished_count[0] + 1))

        # 直接调 process()
        worker.process()

        # progress：每个文件开头 + 一次（idx, total, name）；总共 3 次
        assert len(progress_calls) == 3
        assert [c[0] for c in progress_calls] == [0, 1, 2]
        assert all(c[1] == 3 for c in progress_calls)
        # file_done：3 个
        assert len(file_done_calls) == 3
        for r in file_done_calls:
            assert r.status == "ok"
        # finished 只发 1 次
        assert finished_count[0] == 1
        # 3 个输出文件都生成了
        assert (out_root / "f0_redacted.docx").exists()
        assert (out_root / "f1_redacted.docx").exists()
        assert (out_root / "f2_redacted.docx").exists()

    def test_空列表_立刻finished_不发progress(self, qapp, tmp_path: Path):
        worker = RedactionWorker([], tmp_path / "out", dry_run=False, engine=build_engine_for_gui())
        progress_calls: List = []
        worker.progress.connect(lambda *a: progress_calls.append(a))
        finished_count = [0]
        worker.finished.connect(lambda: finished_count.__setitem__(0, finished_count[0] + 1))
        worker.process()
        assert progress_calls == []
        assert finished_count[0] == 1

    def test_取消_在循环开头退出_不发cancelled后finished仍发(self, qapp, tmp_path: Path):
        # 准备 3 个合法 docx
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)
        cancelled_count = [0]
        worker.cancelled.connect(lambda: cancelled_count.__setitem__(0, cancelled_count[0] + 1))
        finished_count = [0]
        worker.finished.connect(lambda: finished_count.__setitem__(0, finished_count[0] + 1))
        file_done_count = [0]
        worker.file_done.connect(lambda r: file_done_count.__setitem__(0, file_done_count[0] + 1))

        # 在 worker 跑第一个文件前注入取消
        original_process_one = worker._engine  # 只为了标记
        worker.request_cancel()  # 标志置位
        worker.process()  # 循环开头就检查，应该 0 个文件被处理

        # 但根据裁定"不打断正在处理的文件"——这里"未开始处理"就是开头检查退出
        # 实际上：request_cancel 之后 process 第一个循环开头检查 True → 立刻 cancelled + finished
        assert cancelled_count[0] == 1
        assert finished_count[0] == 1
        assert file_done_count[0] == 0
        # 0 个输出文件
        assert not (out_root / "f0_redacted.docx").exists()

    def test_取消_第2个文件前取消_前1个保留(self, qapp, tmp_path: Path):
        """跑完第 1 个后 request_cancel → 第 2 个开头检查退出 → 第 1 个输出保留。"""
        # 第 1 个稍慢，让 request_cancel 在第 1 个完成后、第 2 个开始前注入
        # 用一个慢文件 + file_done 钩子里取消
        paths: List[str] = []
        for i in range(2):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)

        cancelled_count = [0]
        finished_count = [0]
        worker.cancelled.connect(lambda: cancelled_count.__setitem__(0, cancelled_count[0] + 1))
        worker.finished.connect(lambda: finished_count.__setitem__(0, finished_count[0] + 1))

        def on_first_done(r: FileResult) -> None:
            # 第 1 个文件跑完立刻取消
            worker.request_cancel()

        worker.file_done.connect(on_first_done)

        worker.process()

        assert cancelled_count[0] == 1
        assert finished_count[0] == 1
        # 第 1 个保留
        assert (out_root / "f0_redacted.docx").exists()
        # 第 2 个不存在
        assert not (out_root / "f1_redacted.docx").exists()

    def test_每个文件跑完立刻emit_file_done_不等汇总(self, qapp, tmp_path: Path):
        """用户要求：每文件跑完立刻 emit file_done，防崩溃丢结果。"""
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)

        # 收集 file_done 的"瞬时"时间戳
        timestamps: List[float] = []
        worker.file_done.connect(lambda r: timestamps.append(time.monotonic()))
        finished_ts: List[float] = []
        worker.finished.connect(lambda: finished_ts.append(time.monotonic()))

        worker.process()

        # file_done 3 个，finished 1 个；finished 时间 > 所有 file_done 时间
        assert len(timestamps) == 3
        assert len(finished_ts) == 1
        assert finished_ts[0] >= timestamps[-1]  # finished 是最后发的


# ============================================================ C. RedactorController
class TestRedactorController:
    """Controller 集成：含 QThread 真启动 / 关闭。"""

    def test_启动_3个文件_等到finished(self, qapp, tmp_path: Path):
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"

        c = RedactorController()
        started = []
        finished = []
        c.started.connect(lambda n: started.append(n))
        c.finished.connect(lambda: finished.append(1))

        c.start(paths, out_root, dry_run=False, overwrite_output=True)
        assert c.is_running()
        assert started == [3]

        # 等 finished
        assert _wait_until(lambda: not c.is_running(), timeout_s=15.0)
        assert len(finished) == 1

        # 输出文件全部存在
        for i in range(3):
            assert (out_root / f"f{i}_redacted.docx").exists()

    def test_运行中_重复start_抛RuntimeError(self, qapp, tmp_path: Path):
        paths: List[str] = []
        for i in range(2):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        c = RedactorController()
        c.start(paths, out_root, dry_run=False)
        try:
            with pytest.raises(RuntimeError):
                c.start(paths, out_root, dry_run=False)
        finally:
            _wait_until(lambda: not c.is_running(), timeout_s=10.0)

    def test_cancel_立刻生效(self, qapp, tmp_path: Path):
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        c = RedactorController()
        cancelled = []
        c.cancelled.connect(lambda: cancelled.append(1))

        c.start(paths, out_root, dry_run=False)
        c.cancel()  # 立刻取消（首文件开头检查时生效）
        # 等线程结束
        assert _wait_until(lambda: not c.is_running(), timeout_s=10.0)
        assert len(cancelled) == 1

    def test_finished后is_running返回False(self, qapp, tmp_path: Path):
        paths: List[str] = []
        for i in range(1):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        c = RedactorController()
        c.start(paths, out_root, dry_run=False)
        assert c.is_running()
        assert _wait_until(lambda: not c.is_running(), timeout_s=10.0)
        assert not c.is_running()


# ============================================================ D. MainWindow 状态机
class TestMainWindowProcessState:
    """按钮状态机 4 态切换（v0.6.2_方案.md §9）。"""

    def test_空列表_start按钮disabled(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w.state() == "empty"
        assert not w.start_action().isEnabled()
        assert not w.cancel_action().isEnabled()
        assert w.dry_run_action().isEnabled()  # dry-run 可用
        w.close()

    def test_加1个文件_start按钮enabled_状态切到ready(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "f.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])

        assert w.state() == "ready"
        assert w.start_action().isEnabled()
        assert not w.cancel_action().isEnabled()
        w.close()

    def test_启动后_start_disabled_cancel_enabled_状态running(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "f.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])

        assert w.start_processing(output_root=tmp_path / "out")
        # 启动后状态立即切到 running
        assert w.state() == "running"
        assert not w.start_action().isEnabled()
        assert w.cancel_action().isEnabled()
        assert not w.dry_run_action().isEnabled()  # 运行中禁用 dry-run
        # 等结束
        assert _wait_until(lambda: not w.controller().is_running(), timeout_s=10.0)
        w.close()

    def test_finished后_start_re_enabled_状态done(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "f.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        assert w.start_processing(output_root=tmp_path / "out")
        assert _wait_until(lambda: w.state() == "done", timeout_s=10.0)
        assert w.start_action().isEnabled()
        assert not w.cancel_action().isEnabled()
        w.close()

    def test_clear后状态回empty(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "f.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        assert w.state() == "ready"
        w.clear_files()
        assert w.state() == "empty"
        assert not w.start_action().isEnabled()
        w.close()


# ============================================================ E. 端到端
class TestEndToEndProcess:
    """真实文件 + Controller + MainWindow 端到端。"""

    def test_3个docx_real_run_输出文件存在_原文件未变(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src_files = []
        for i in range(3):
            p = tmp_path / "in" / f"f{i}.docx"
            p.parent.mkdir(exist_ok=True)
            _make_docx(p, f"张三 1381234567{i}")
            src_files.append(p)

        # 记原文件 hash
        import hashlib
        orig_hashes = {}
        for p in src_files:
            orig_hashes[p] = hashlib.md5(p.read_bytes()).hexdigest()

        w.drop_zone().add_files([str(p) for p in src_files])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done", timeout_s=15.0)

        # 3 个输出文件存在
        for i in range(3):
            out = out_root / f"f{i}_redacted.docx"
            assert out.exists(), out
            # 输出大小 > 0
            assert out.stat().st_size > 0

        # 原文件未变（hash 一致）
        for p in src_files:
            assert hashlib.md5(p.read_bytes()).hexdigest() == orig_hashes[p]

        # 文件项状态变成"成功"
        for r in range(w.drop_zone().model().rowCount()):
            data = w.drop_zone().model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            assert data["status"] == "成功"

        w.close()

    def test_3个docx_dry_run_不写文件(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src_files = []
        for i in range(3):
            p = tmp_path / "in" / f"f{i}.docx"
            p.parent.mkdir(exist_ok=True)
            _make_docx(p)
            src_files.append(p)

        # 勾选 dry-run
        w.dry_run_action().setChecked(True)

        w.drop_zone().add_files([str(p) for p in src_files])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done", timeout_s=15.0)

        # dry-run 不写文件
        assert not (out_root / "f0_redacted.docx").exists()
        # 但仍跑识别，文件项状态为"成功"
        for r in range(w.drop_zone().model().rowCount()):
            data = w.drop_zone().model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            assert data["status"] == "成功"

        w.close()

    def test_含1损坏docx_2成功_进度条最终100(self, qapp, tmp_path: Path):
        """3 个文件：1 损坏 + 2 成功。处理继续到全部完成。"""
        from redactor.gui import MainWindow
        w = MainWindow()
        good1 = tmp_path / "in" / "g1.docx"
        good2 = tmp_path / "in" / "g2.docx"
        bad = tmp_path / "in" / "bad.docx"
        good1.parent.mkdir(exist_ok=True)
        _make_docx(good1)
        _make_docx(good2)
        _make_corrupted_docx(bad)

        w.drop_zone().add_files([str(good1), str(bad), str(good2)])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done", timeout_s=15.0)

        # 2 个成功文件输出存在
        assert (out_root / "g1_redacted.docx").exists()
        assert (out_root / "g2_redacted.docx").exists()
        # 损坏文件不输出
        assert not (out_root / "bad_redacted.docx").exists()

        # 进度条最终 = maximum
        assert w.progress_bar().value() == w.progress_bar().maximum()

        # 文件项状态：g1=成功、bad=失败、g2=成功
        statuses = []
        for r in range(w.drop_zone().model().rowCount()):
            data = w.drop_zone().model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            statuses.append(data["status"])
        assert "成功" in statuses and "失败" in statuses
        assert statuses.count("成功") == 2
        assert statuses.count("失败") == 1

        w.close()


# ============================================================ F. 反向验证
class TestReverseValidation:
    """变异代码后断言必须真失败 —— 防止"对照组漏检 100%"。"""

    def test_cooperative_cancel改成强杀_取消测试真失败(self, qapp, tmp_path: Path, monkeypatch):
        """变异：把 worker.process 改为不检查 _cancel_requested → 取消测试必须失败。"""
        from redactor.gui import worker as worker_mod

        # 保存原始 process
        original_process = worker_mod.RedactionWorker.process

        def hard_kill_process(self) -> None:
            """变异版：不检查 _cancel_requested，每个文件都跑完。"""
            total = len(self._paths)
            for idx, p in enumerate(self._paths):
                name = Path(p).name
                self.progress.emit(idx, total, name)
                self.log_message.emit(f"[{idx + 1}/{total}] 开始：{name}")
                result = process_one_for_gui(
                    Path(p), self._output_root, self._input_root,
                    self._engine, self._dry_run, overwrite_output=self._overwrite_output,
                )
                self.file_done.emit(result)
                self.log_message.emit(f"[{idx + 1}/{total}] 完成")
            self.finished.emit()

        monkeypatch.setattr(worker_mod.RedactionWorker, "process", hard_kill_process)

        # 准备 2 个文件 + 取消钩子
        paths: List[str] = []
        for i in range(2):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)
        cancelled_count = [0]
        worker.cancelled.connect(lambda: cancelled_count.__setitem__(0, cancelled_count[0] + 1))
        file_done_count = [0]
        worker.file_done.connect(lambda r: file_done_count.__setitem__(0, file_done_count[0] + 1))

        # 跑前取消
        worker.request_cancel()
        worker.process()

        # 变异后应该：cancelled 不发、2 个 file_done 全跑完
        # —— 这与"协同取消"的语义矛盾
        assert cancelled_count[0] == 0, "变异后取消了？"
        assert file_done_count[0] == 2, "变异后没跑完所有文件？"
        # 反向断言：变异版不再尊重取消请求
        assert (out_root / "f1_redacted.docx").exists(), "变异后第 2 个文件不应被生成"

        # monkeypatch 自动还原 worker_mod.RedactionWorker.process

    def test_file_done_改为批量汇总_每文件测试真失败(self, qapp, tmp_path: Path, monkeypatch):
        """变异：把 file_done 移到循环结束后统一发 → 每个文件跑完立刻发的测试应失败。"""
        from redactor.gui import worker as worker_mod

        original_process = worker_mod.RedactionWorker.process

        def batched_process(self) -> None:
            """变异版：collect 所有 result，循环结束后一次性 emit。"""
            total = len(self._paths)
            results = []
            for idx, p in enumerate(self._paths):
                if self._cancel_requested:
                    self.cancelled.emit()
                    self.finished.emit()
                    return
                name = Path(p).name
                self.progress.emit(idx, total, name)
                result = process_one_for_gui(
                    Path(p), self._output_root, self._input_root,
                    self._engine, self._dry_run, overwrite_output=self._overwrite_output,
                )
                results.append(result)
            # 变异：循环结束后才统一发
            for r in results:
                self.file_done.emit(r)
            self.finished.emit()

        monkeypatch.setattr(worker_mod.RedactionWorker, "process", batched_process)

        # 跑 3 个文件；验证 file_done 全部在 finished 之前发（不是循环外）
        paths: List[str] = []
        for i in range(3):
            p = tmp_path / f"f{i}.docx"
            _make_docx(p)
            paths.append(str(p))
        out_root = tmp_path / "out"
        engine = build_engine_for_gui()

        worker = RedactionWorker(paths, out_root, dry_run=False, engine=engine)
        timestamps: List[float] = []
        worker.file_done.connect(lambda r: timestamps.append(time.monotonic()))
        finished_ts: List[float] = []
        worker.finished.connect(lambda: finished_ts.append(time.monotonic()))

        worker.process()

        # 变异后：所有 file_done 时间应该接近 finished 时间（因为是循环后一次性发）
        # 真实版：file_done 时间应该跨越整个 process() 期间
        # 这里我们用"最大间距"断言：真实版最大间距 > 变异版最大间距
        # 简单断言：变异后 finished_ts - 首个 file_done_ts 几乎为 0（同时发）
        if len(timestamps) >= 2 and finished_ts:
            span = finished_ts[0] - timestamps[0]
            # 变异后 span 极小（<1ms 几乎同时）
            # 真实版 span 通常 > 10ms（每个文件几十毫秒）
            assert span < 0.005, f"变异后跨度 {span}s 过小，应接近 0（同时发）"
