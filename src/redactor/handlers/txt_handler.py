"""纯文本 / Markdown / 日志文件处理器。

作为 P0 的参考实现：它演示了 handler 的标准骨架
（只读源文件 -> 逐块送引擎 -> 写新文件 -> 汇总零敏感报告）。
docx/xlsx/pdf handler 都沿用同一骨架，只是"拆块"和"写回"方式不同。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult, Match
from redactor.handlers.base import BaseHandler, HandlerError, prepare_output

# 按优先级尝试的编码。UTF-8 覆盖绝大多数场景，GB18030 兼容 Windows 中文环境，
# latin-1 作为最后兜底（保证不因编码问题导致整个文件被跳过）。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "latin-1")


def read_text_safe(path: Path) -> Tuple[str, str]:
    """以只读方式读取文本，自动尝试常见编码。

    Args:
        path: 文件路径。

    Returns:
        ``(文本内容, 实际使用的编码)``。

    Raises:
        HandlerError: 文件不存在、无法读取、为空、或含 NUL 字节（二进制文件）。
    """
    if not path.is_file():
        raise HandlerError(HandlerError.IO_ERROR, f"文件不存在：{path}")

    try:
        raw = path.read_bytes()
    except PermissionError as exc:
        raise HandlerError(HandlerError.PERMISSION, f"无权限读取：{path}") from exc
    except OSError as exc:
        raise HandlerError(HandlerError.IO_ERROR, f"读取失败：{path}（{exc}）") from exc

    if not raw:
        raise HandlerError(HandlerError.EMPTY, f"文件为空：{path}")

    # 含 NUL 字节基本可判定为二进制（如误拖入的 xlsx/docx），直接拒绝而不是乱码处理
    if b"\x00" in raw[:8192]:
        raise HandlerError(
            HandlerError.UNSUPPORTED,
            f"疑似二进制文件，已跳过：{path}（若为 Office/PDF 文件请确认扩展名与格式匹配）",
        )

    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise HandlerError(HandlerError.IO_ERROR, f"无法解码文件（尝试过 {len(_ENCODINGS)} 种编码）：{path}")


class TxtHandler(BaseHandler):
    """纯文本类文件处理器。"""

    extensions = (".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml")
    display_name = "纯文本"

    def process(
        self,
        source: Path,
        output: Path,
        engine: RedactionEngine,
        *,
        dry_run: bool = False,
        overwrite_output: bool = False,
    ) -> FileResult:
        """逐行脱敏文本文件。

        逐行处理的好处：命中位置能精确报告到"第 N 行"，便于人工核对。
        """
        text, _encoding = read_text_safe(source)

        # 先做一次写前检查，dry_run 也要检查（否则 dry-run 通过、真跑时才发现冲突，
        # 会让人误以为 dry-run 已经验证过安全性）
        prepare_output(source, output, overwrite_output=overwrite_output)

        lines = text.splitlines(keepends=True)
        out_lines: List[str] = []
        all_matches = []
        locations = {}
        line_offset_base = 0

        for index, line in enumerate(lines, start=1):
            redacted, kept = engine.redact(line)
            out_lines.append(redacted)
            for match in kept:
                # 记录该命中在整篇文本中的起始偏移，便于聚合时定位
                key_start = line_offset_base + match.start
                all_matches.append(
                    Match(
                        rule_name=match.rule_name,
                        placeholder=match.placeholder,
                        start=key_start,
                        end=line_offset_base + match.end,
                        raw=match.raw,
                    )
                )
                locations[(match.rule_name, key_start)] = f"第 {index} 行"
            line_offset_base += len(line)

        stats = engine.aggregate(all_matches, locations)

        if not dry_run:
            output.parent.mkdir(parents=True, exist_ok=True)
            # 保持与源文件一致的换行风格：写入时统一用 \n，末尾补齐
            try:
                output.write_text("".join(out_lines), encoding="utf-8", newline="")
            except PermissionError as exc:
                raise HandlerError(HandlerError.PERMISSION, f"无权限写入：{output}") from exc
            except OSError as exc:
                raise HandlerError(HandlerError.IO_ERROR, f"写入失败：{output}（{exc}）") from exc

        return FileResult(
            source=str(source),
            output=None if dry_run else str(output),
            status="ok",
            hits=stats,
        )