"""处理器层。

本包按扩展名把文件分派给对应 handler。P0 只有 txt handler，
P1/P2/P3 会往 REGISTRY 里追加 docx / xlsx / pdf。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Type

from redactor.handlers.base import BaseHandler, HandlerError, prepare_output
from redactor.handlers.txt_handler import TxtHandler, read_text_safe

#: 扩展名 -> handler 类
REGISTRY: Dict[str, Type[BaseHandler]] = {}

#: 当前阶段明确不支持的扩展名（给出更友好的提示，而不是笼统的"格式不支持"）
KNOWN_UNSUPPORTED = {
    ".doc": "旧版 .doc 二进制格式暂不支持，请先另存为 .docx",
    ".xls": "旧版 .xls 二进制格式暂不支持，请先另存为 .xlsx",
    ".ppt": "旧版 .ppt 二进制格式暂不支持，请先另存为 .pptx",
    ".rtf": "RTF 暂不支持",
    ".wps": "WPS 格式暂不支持，请先另存为 .docx/.xlsx",
    ".et": "WPS 表格暂不支持，请先另存为 .xlsx",
}


def _register(handler_cls: Type[BaseHandler]) -> None:
    """把 handler 的所有扩展名登记到 REGISTRY。"""
    for ext in handler_cls.extensions:
        REGISTRY[ext] = handler_cls


_register(TxtHandler)


def get_handler(path: str | Path) -> BaseHandler:
    """按文件扩展名获取 handler 实例。

    Args:
        path: 文件路径。

    Returns:
        handler 实例。

    Raises:
        HandlerError: 扩展名未注册（unsupported_format）。
    """
    file_path = Path(path)
    ext = file_path.suffix.lower()
    handler_cls = REGISTRY.get(ext)
    if handler_cls is None:
        hint = KNOWN_UNSUPPORTED.get(ext)
        message = f"暂不支持的文件类型：{ext or '(无扩展名)'}"
        if hint:
            message += f"；{hint}"
        raise HandlerError(HandlerError.UNSUPPORTED, message)
    return handler_cls()


def supported_extensions() -> List[str]:
    """返回当前已支持的扩展名列表（排序）。"""
    return sorted(REGISTRY)


__all__ = [
    "REGISTRY",
    "KNOWN_UNSUPPORTED",
    "BaseHandler",
    "HandlerError",
    "TxtHandler",
    "read_text_safe",
    "get_handler",
    "supported_extensions",
    "prepare_output",
]