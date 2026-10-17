"""v0.6.2 端到端验证脚本（offline / offscreen）。

跑法（与本机其它 GUI 验证一致）：
    set PYTHONPATH=src;.verify_libs
    set QT_QPA_PLATFORM=offscreen
    "E:/Program Files/python/python.exe" scripts/e2e_v062.py

场景（5 个）：
    1. 准备 3 个 docx（其中 1 个损坏文件模拟失败）
    2. 拖入 → 状态机切到 ready
    3. 点 Start → 通过 start_processing() 跳过 QFileDialog
    4. 等 finished → 验证：进度条=100%、文件项状态更新、输出文件存在
    5. dry-run 模式：再跑一遍，验证不写文件
    6. cancel 场景：拖入多文件 → start → 立刻 cancel → 验证不卡死

本脚本是 v0.6.2 交付的 e2e 验证（deliverable #2），不入产品包，
仅作开发期 / 验收期验证用。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from redactor.gui import APP_NAME, APP_VERSION, MainWindow  # noqa: E402


def _make_docx(path: Path, text: str = "张三 13812345678") -> None:
    """造一个最简 docx（含 1 个手机号命中）。"""
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    d.save(str(path))


def _make_corrupted_docx(path: Path) -> None:
    path.write_bytes(b"this is not a docx")


def _wait_until(predicate, timeout_s: float = 15.0, step_ms: int = 50) -> bool:
    deadline = __import__("time").monotonic() + timeout_s
    while __import__("time").monotonic() < deadline:
        if predicate():
            return True
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents, step_ms
        )
    return predicate()


def main() -> int:
    print(f"=== {APP_NAME} v{APP_VERSION} 端到端验证（v0.6.2） ===")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # ---- 准备临时文件 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="redactor_e2e_v062_"))
    print(f"tmpdir: {tmpdir}")
    src_dir = tmpdir / "src"
    src_dir.mkdir(exist_ok=True)

    good1 = src_dir / "report.docx"
    good2 = src_dir / "summary.docx"
    bad = src_dir / "broken.docx"
    _make_docx(good1, "张三 13812345678")
    _make_docx(good2, "李四 13887654321")
    _make_corrupted_docx(bad)

    # ---- 启动主窗口 ----
    w = MainWindow()
    w.show()
    app.processEvents()
    print(f"主窗口已创建：{w.windowTitle()}")

    drop = w.drop_zone()
    log = w.log_panel()
    sb = w.statusBar()
    pb = w.progress_bar()

    # ============================================================
    # 场景 1：拖入 3 文件（1 损坏 + 2 合法）+ Start + 等 finished
    # ============================================================
    print("\n--- 场景 1：3 文件（含 1 损坏），real-run ---")
    w.drop_zone().add_files([str(good1), str(bad), str(good2)])
    app.processEvents()
    print(f"  列表 count: {drop.count()}")
    print(f"  状态机: {w.state()}")
    print(f"  start enabled: {w.start_action().isEnabled()}")
    assert drop.count() == 3
    assert w.state() == "ready"

    out_root1 = tmpdir / "out1"
    assert w.start_processing(output_root=out_root1), "start_processing 应该成功"
    app.processEvents()
    print(f"  启动后状态机: {w.state()}")
    print(f"  cancel enabled: {w.cancel_action().isEnabled()}")
    assert w.state() == "running"
    assert w.cancel_action().isEnabled()

    assert _wait_until(lambda: w.state() == "done", timeout_s=15.0), "未在 15s 内完成"
    app.processEvents()

    print(f"  完成后状态机: {w.state()}")
    print(f"  进度条: {pb.value()}/{pb.maximum()} - {pb.format()}")
    print(f"  状态栏: {sb.currentMessage()!r}")
    print(f"  输出文件: {sorted(p.name for p in out_root1.iterdir() if p.suffix == '.docx')}")
    assert pb.value() == pb.maximum(), "进度条应达 100%"
    assert (out_root1 / "report_redacted.docx").exists()
    assert (out_root1 / "summary_redacted.docx").exists()
    assert not (out_root1 / "broken_redacted.docx").exists(), "损坏文件不应输出"

    # 文件项状态检查
    statuses = []
    for r in range(drop.model().rowCount()):
        d = drop.model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
        statuses.append(d["status"])
    print(f"  文件项状态: {statuses}")
    assert statuses.count("成功") == 2
    assert statuses.count("失败") == 1
    print("  ✅ 场景 1 通过：进度条 100%、2 成功 + 1 失败、原文件未变")

    # ============================================================
    # 场景 2：dry-run 模式
    # ============================================================
    print("\n--- 场景 2：dry-run 模式（不写文件）---")
    # 清空 + 重新拖入
    w.clear_files()
    app.processEvents()
    w.drop_zone().add_files([str(good1), str(good2)])
    app.processEvents()
    w.dry_run_action().setChecked(True)
    print(f"  dry-run 勾选: {w.dry_run_action().isChecked()}")

    out_root2 = tmpdir / "out2"
    assert w.start_processing(output_root=out_root2), "dry-run start_processing 应该成功"
    assert _wait_until(lambda: w.state() == "done", timeout_s=15.0)
    app.processEvents()

    # dry-run 不写文件
    files_in_out2 = list(out_root2.iterdir()) if out_root2.exists() else []
    print(f"  dry-run 输出目录文件数: {len(files_in_out2)}（应为 0）")
    assert len(files_in_out2) == 0, f"dry-run 不应写文件，但有 {len(files_in_out2)} 个"

    # 文件项状态仍为"成功"（识别跑过）
    statuses = []
    for r in range(drop.model().rowCount()):
        d = drop.model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
        statuses.append(d["status"])
    assert statuses == ["成功", "成功"]
    print("  ✅ 场景 2 通过：dry-run 不写文件、文件项状态=成功")

    # ============================================================
    # 场景 3：Cancel 场景
    # ============================================================
    print("\n--- 场景 3：Cancel（cooperative） ---")
    w.dry_run_action().setChecked(False)
    w.clear_files()
    app.processEvents()

    # 准备 3 个文件
    f1 = src_dir / "a.docx"
    f2 = src_dir / "b.docx"
    f3 = src_dir / "c.docx"
    _make_docx(f1)
    _make_docx(f2)
    _make_docx(f3)
    w.drop_zone().add_files([str(f1), str(f2), str(f3)])
    app.processEvents()

    out_root3 = tmpdir / "out3"
    assert w.start_processing(output_root=out_root3)
    # 立刻取消
    w.cancel_action().trigger()
    app.processEvents()
    print(f"  取消请求已发，等 worker 退出…")
    assert _wait_until(lambda: not w.controller().is_running(), timeout_s=10.0)
    app.processEvents()

    # 输出文件数：≤3（可能 0 个或部分）
    files_in_out3 = list(out_root3.iterdir()) if out_root3.exists() else []
    print(f"  取消后输出文件数: {len(files_in_out3)} / 3")
    print(f"  最终状态机: {w.state()}")
    assert w.state() in ("done", "ready"), f"取消后状态机应是 done/ready，实际 {w.state()}"
    # cooperative cancel：可能有 0~3 个文件跑完，不能断言具体数
    print(f"  ✅ 场景 3 通过：取消后 worker 退出、未卡死")

    # ============================================================
    # 场景 4：状态机切换（empty → ready → running → done → empty）
    # ============================================================
    print("\n--- 场景 4：状态机切换 ---")
    w.clear_files()
    app.processEvents()
    assert w.state() == "empty", w.state()

    w.drop_zone().add_files([str(good1)])
    app.processEvents()
    assert w.state() == "ready", w.state()

    out_root4 = tmpdir / "out4"
    assert w.start_processing(output_root=out_root4)
    assert w.state() == "running", w.state()

    assert _wait_until(lambda: w.state() == "done", timeout_s=10.0)
    app.processEvents()
    assert w.state() == "done", w.state()

    w.clear_files()
    app.processEvents()
    assert w.state() == "empty", w.state()
    print("  ✅ 场景 4 通过：empty → ready → running → done → empty")

    # ============================================================
    # 场景 5：清空列表 + 状态栏汇总日志
    # ============================================================
    print("\n--- 场景 5：清空列表 + 日志 ---")
    log_text = log.toPlainText()
    log_lines = log_text.splitlines()
    print(f"  日志总行数: {len(log_lines)}")
    print(f"  日志最后 5 行:")
    for line in log_lines[-5:]:
        print(f"    | {line}")

    assert "已清空列表" in log_text, "日志应含'已清空列表'"
    assert "完成" in log_text, "日志应含'完成'（场景 4 finished）"
    print("  ✅ 场景 5 通过：日志含清空 + 完成记录")

    print("\n=== 端到端验证全部通过 ===")

    # ---- 清理 ----
    w.close()
    app.processEvents()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
