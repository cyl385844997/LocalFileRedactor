"""``python -m redactor.gui`` 入口。

调用 :func:`redactor.gui.main.main` 启动 GUI。
"""

from __future__ import annotations

import sys

from redactor.gui.main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))