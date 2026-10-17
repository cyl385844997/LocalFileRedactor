"""GUI 入口（v0.6.0 骨架）。

- ``redactor-gui`` 子命令调用 :func:`main`
- :func:`main` 创建 :class:`QApplication` + :class:`MainWindow` 并启动事件循环
- ``QT_QPA_PLATFORM=offscreen`` 时不创建真实窗口（CI 测试用）

为什么 main() 单独一个文件：
- :mod:`redactor.gui.__init__` 只导出符号，不能有副作用
- :func:`main` 是唯一允许 ``sys.exit`` 的入口
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from PySide6 import QtWidgets

from redactor.gui import APP_NAME, APP_ORG, APP_VERSION
from redactor.gui.main_window import MainWindow

#: 产物自检配置文件名（放系统 TEMP，见 _run_probe_if_requested 的说明）
_PROBE_CONFIG_NAME = "_redactor_probe.json"


def _build_app(argv: Sequence[str]) -> QtWidgets.QApplication:
    """构造 :class:`QApplication`，每个进程只允许一个实例。"""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationVersion(APP_VERSION)
    return app


def _run_probe_if_requested() -> Optional[int]:
    """若配置了产物自检探针，则运行它并返回退出码。

    ★为什么需要它：打包后的 exe 是**windowed**（无控制台），
    它只跑 :func:`main`，**不会**执行命令行参数里的脚本路径。
    所以"在打包产物内验证"必须有一条注入通道。

    **双通道读取**（环境变量优先，其次配置文件）：

        1. 环境变量 ``REDACTOR_PROBE_SCRIPT`` / ``REDACTOR_PROBE_REPORT``
        2. 配置文件 ``_redactor_probe.json``，按以下顺序找：
           a. 环境变量 ``REDACTOR_PROBE_CONFIG`` 指定的路径
           b. ``tempfile.gettempdir()``（**onefile 模式下唯一可行的位置**，见下）
           c. ``sys.executable`` 同目录

    ★为什么必须用 TEMP 而不是 exe 同目录（实测踩过的坑）：
        PyInstaller **onefile** 模式启动时会把整个 exe 解包到
        ``%TEMP%\_MEIxxxxxx\``，``sys.executable`` 指向的是
        **解包目录里的副本**，不是用户双击的那个 exe。
        所以放在原 exe 旁边的配置文件，打包后的 exe **根本看不到**。

    ★为什么环境变量不可靠（实测）：
        用 ``subprocess.run(env=...)`` 启动打包后的 windowed exe，
        ``os.environ`` 里拿不到自定义变量 —— 钩子静默不触发，
        exe 直接进 GUI 事件循环，验证脚本只能等到超时。
        （同一段 ``env=`` 传给普通 ``python.exe`` 是正常的，
        问题出在打包产物这一侧。）故文件通道才是主路径。

    安全性：仅当两条通道之一有配置时激活；正常双击运行不受影响。

    Returns:
        探针的退出码；未配置时返回 ``None``（继续正常启动）。
    """
    script = os.environ.get("REDACTOR_PROBE_SCRIPT")
    report = os.environ.get("REDACTOR_PROBE_REPORT")

    # ---- 通道 2：配置文件（不依赖环境继承）----
    try:
        import tempfile
        candidates = []
        explicit = os.environ.get("REDACTOR_PROBE_CONFIG")
        if explicit:
            candidates.append(Path(explicit))
        # ★onefile 模式下 TEMP 是唯一 exe 真能读到的外部位置
        candidates.append(Path(tempfile.gettempdir()) / _PROBE_CONFIG_NAME)
        candidates.append(
            Path(sys.executable).resolve().parent / _PROBE_CONFIG_NAME
        )
        for cfg_path in candidates:
            if not cfg_path.is_file():
                continue
            import json
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            # ★读完立刻删 —— 否则它会**污染之后的每一次正常启动**：
            #   用户（或后续测试）双击 exe 时仍会去跑那个探针，
            #   程序既不显示窗口也不做正事。实测踩过：
            #   scripts/test.bat 的 GUI 入口测试因此 rc=3 假红。
            try:
                cfg_path.unlink()
            except OSError:
                pass
            script = script or cfg.get("script")
            report = report or cfg.get("report")
            if script:
                break
    except Exception:
        pass

    if not script:
        return None

    # ★先落一个"到达标记"，再跑探针。
    #   windowed exe 没有 stderr，崩了什么都看不见 —— 这个标记让外部
    #   能区分"exe 根本没进到这里"与"进来了但里面崩了"。
    if report:
        try:
            with open(report, "w", encoding="utf-8") as fp:
                fp.write('{"stage": "hook-entered"}')
        except Exception:
            pass

    try:
        # runpy 必须在包内；PyInstaller 默认排除它，故 .spec 显式声明了
        # hiddenimports（漏了会静默失败 —— 这里有 except 兜住）。
        import runpy
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException as exc:                # noqa: BLE001 —— 要兜住一切
        if report:
            try:
                import json as _json
                import traceback as _tb
                with open(report, "w", encoding="utf-8") as fp:
                    fp.write(_json.dumps({
                        "error": f"{type(exc).__name__}: {exc}",
                        "stage": "hook-crashed",
                        "traceback": _tb.format_exc()[-3000:],
                    }, ensure_ascii=False))
            except Exception:
                pass
        return 3
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """GUI 主入口。

    Args:
        argv: 命令行参数；None 时用 :data:`sys.argv`。

    Returns:
        进程退出码（由 :class:`QApplication.exec` 返回）。
    """
    if argv is None:
        argv = sys.argv

    # 产物自检钩子（正常双击不受影响，见 _run_probe_if_requested）
    probe_rc = _run_probe_if_requested()
    if probe_rc is not None:
        return probe_rc

    app = _build_app(argv)

    # 允许 offscreen 模式（CI 烟雾测试）：不创建真实窗口
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        # 不 show，调用 processEvents 让 layout 生效即可
        win = MainWindow()
        win.show()
        app.processEvents()
        win.close()
        return 0

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))