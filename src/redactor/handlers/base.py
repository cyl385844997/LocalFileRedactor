"""文件处理器抽象契约。

安全底线（需求约束 4）：**永远不覆盖原文件**。
本模块用三重机制保证：
    1. 输出路径必须与源路径不同（:func:`ensure_output_distinct`）；
    2. 输出文件若已存在，默认直接拒绝（``overwrite_output=False``），
       避免二次运行把上一次结果覆盖掉、也避免误改用户已有文件；
    3. 源文件一律以只读方式打开（``open(path, "rb")`` 或库层面的只读 load）。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Sequence

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult


class HandlerError(Exception):
    """文件处理错误。

    Attributes:
        code: 机器可读原因码，用于报告分类。
        message: 面向用户的中文说明。
    """

    # 原因码：统一在 report.REASON_LABELS 中有中文映射
    UNSUPPORTED = "unsupported_format"
    ENCRYPTED = "encrypted_file"
    CORRUPTED = "corrupted_file"
    PERMISSION = "permission_denied"
    IO_ERROR = "io_error"
    OUTPUT_EXISTS = "output_exists"
    OUTPUT_SAME = "output_same_as_source"
    EMPTY = "empty_file"
    UNKNOWN = "unknown_error"

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def ensure_output_distinct(source: Path, output: Path) -> None:
    """确保输出路径与源路径不同，否则抛错。

    比较时做 ``resolve()`` + Windows 下大小写归一化，避免 ``A.docx`` 与 ``a.docx``
    这类同文件不同大小写的绕过。

    Raises:
        HandlerError: 输出路径与源路径指向同一文件。
    """
    src = source.resolve()
    dst = output.resolve()
    if os.name == "nt":
        src_key = str(src).lower()
        dst_key = str(dst).lower()
    else:
        src_key, dst_key = str(src), str(dst)
    if src_key == dst_key:
        raise HandlerError(
            HandlerError.OUTPUT_SAME,
            f"拒绝执行：输出路径与源文件相同（{source}）。本工具永不覆盖原文件。",
        )


def prepare_output(source: Path, output: Path, *, overwrite_output: bool = False) -> None:
    """写文件前的统一前置检查。

    Args:
        source: 源文件路径。
        output: 输出文件路径。
        overwrite_output: 是否允许覆盖已存在的输出文件。

    Raises:
        HandlerError: 输出与源同路径，或输出已存在而未允许覆盖。
    """
    ensure_output_distinct(source, output)
    if output.exists() and not overwrite_output:
        raise HandlerError(
            HandlerError.OUTPUT_EXISTS,
            f"输出文件已存在：{output}（如需覆盖请显式使用 --overwrite-output）",
        )


class BaseHandler(ABC):
    """文件处理器基类。"""

    #: 本处理器支持的文件扩展名（小写，含点）
    extensions: Sequence[str] = ()
    #: 面向用户的中文名
    display_name: str = "未知格式"

    @classmethod
    def can_handle(cls, path: Path) -> bool:
        """判断本处理器是否支持该路径。"""
        return path.suffix.lower() in cls.extensions

    @abstractmethod
    def process(
        self,
        source: Path,
        output: Path,
        engine: RedactionEngine,
        *,
        dry_run: bool = False,
        overwrite_output: bool = False,
    ) -> FileResult:
        """处理单个文件。

        Args:
            source: 源文件路径（只读）。
            output: 输出文件路径。
            engine: 脱敏引擎。
            dry_run: True 时只统计不写文件。
            overwrite_output: 是否允许覆盖已存在的输出。

        Returns:
            处理结果；命中统计**不含原文**。

        Raises:
            HandlerError: 加密、损坏、权限不足、输出冲突等。
        """
        raise NotImplementedError