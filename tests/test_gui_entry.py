"""测试 GUI 入口链（python -m redactor.gui 与 redactor-gui 子命令）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYTHON = Path(os.environ.get("PYTHON_BIN", "E:/Program Files/python/python.exe"))
VERIFY_LIBS = REPO / ".verify_libs"


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # 强制 offscreen + 把 .verify_libs 注入 PYTHONPATH
    env["QT_QPA_PLATFORM"] = "offscreen"
    existing_pp = env.get("PYTHONPATH", "")
    pp_parts = [str(REPO / "src"), str(VERIFY_LIBS)]
    if existing_pp:
        pp_parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(pp_parts)
    return subprocess.run(
        cmd, env=env, cwd=str(REPO),
        capture_output=True, text=True, timeout=timeout,
    )


class TestEntry:
    @pytest.mark.skipif(
        not Path("E:/Program Files/python/python.exe").exists(),
        reason="system Python not present",
    )
    def test_python_dash_m_redactor_gui_offscreen_退出码0(self):
        """python -m redactor.gui 在 offscreen 模式下应正常退出。"""
        result = _run(
            [str(PYTHON), "-m", "redactor.gui"],
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_import_包_不抛异常(self):
        """从子进程导入 redactor.gui 不应崩。"""
        result = _run(
            [str(PYTHON), "-c", "import redactor.gui; print(redactor.gui.APP_VERSION)"],
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        # 校验当前版本前缀（v0.6.x 系列）
        assert result.stdout.startswith("0.6."), result.stdout