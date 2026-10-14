"""命令行入口。

子命令：
    run      对文件或目录执行脱敏
    dry-run  只统计会命中多少处、在什么位置，不写任何文件（需求补充约束 B）
    rules    查看当前生效的规则

设计原则：
    * 绝不联网；本文件不 import 任何网络库。
    * 规则加载失败立即以非零码退出，并把行号打在 stderr（需求补充约束 C）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult, RuleError
from redactor.core.rules import load_rule_dir, load_rules
from redactor.handlers import get_handler, supported_extensions
from redactor.handlers.base import HandlerError
from redactor.report import REASON_LABELS, Report

# 默认输出目录名（需求第 8 条口径）
DEFAULT_OUTPUT_DIRNAME = "redacted_output"


def _default_rules_dir() -> Path:
    """定位内置规则目录。

    优先级：环境变量 ``REDACTOR_RULES_DIR`` > 项目内 ``rules/``（含打包后的 _MEIPASS）。
    这样 exe 打包后与源码运行都能找到规则，且允许用户整体替换规则集。
    """
    import os

    env_dir = os.environ.get("REDACTOR_RULES_DIR")
    if env_dir:
        return Path(env_dir)

    # 源码布局：src/redactor/cli.py -> 项目根/rules
    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / "rules"
    if candidate.is_dir():
        return candidate

    # PyInstaller 单文件模式：规则被解包到 _MEIPASS/rules
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "rules"
    return candidate


def _collect_files(inputs: Sequence[str], *, recursive: bool) -> Tuple[List[Path], List[str]]:
    """把命令行输入展开为文件列表。

    Args:
        inputs: 文件或目录路径列表。
        recursive: 目录是否递归。

    Returns:
        ``(文件列表, 无法访问的路径列表)``。
    """
    files: List[Path] = []
    problems: List[str] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            globber = path.rglob("*") if recursive else path.glob("*")
            for child in sorted(globber):
                if child.is_file():
                    files.append(child)
        else:
            problems.append(f"路径不存在或不可访问：{raw}")
    return files, problems


def _relative_output_path(source: Path, input_root: Path, output_root: Path) -> Path:
    """计算输出路径，保留相对目录结构（需求第 8 条口径：不污染源目录）。

    Args:
        source: 待处理文件。
        input_root: 输入根路径（目录输入时为该目录，单文件输入时为其父目录）。
        output_root: 输出根目录。

    Returns:
        输出文件路径（已加 ``_redacted`` 后缀）。

    注意 ``relative_to`` 在单文件输入下的两种落点（均已实测）：
        - ``relative_to`` 成功时返回 ``Path('.')``（相对自身），其 ``.name`` 为空串；
        - ``relative_to`` 抛 ValueError 时走except 分支，得到同样是空名的 ``Path()``。
        两者对 ``with_name`` 都会抛 ``ValueError: has an empty name``。
        因此下面统一按"空名"来兜住，改用源文件名。
    """
    try:
        relative = source.resolve().relative_to(input_root.resolve())
    except ValueError:
        relative = Path()

    # 空名路径（. 或 Path()）不能做 with_name，退回用源文件名
    if relative == Path() or relative.name == "":
        relative = Path(source.name)

    # 文件名加 _redacted 后缀，避免与源文件同名造成混淆
    return output_root / relative.with_name(f"{relative.stem}_redacted{relative.suffix}")


def _build_engine(args: argparse.Namespace) -> RedactionEngine:
    """按参数构造引擎；规则加载失败直接抛 RuleError。"""
    rules_dir = Path(args.rules) if args.rules else _default_rules_dir()
    if rules_dir.is_dir():
        rules = load_rule_dir(rules_dir)
    else:
        rules = load_rules(rules_dir)

    enabled_names = {r.name for r in rules}
    if getattr(args, "only", None):
        wanted = set(args.only)
        unknown = wanted - enabled_names
        if unknown:
            raise RuleError(f"以下规则名不存在：{sorted(unknown)}；可用规则：{sorted(enabled_names)}")
        rules = [r for r in rules if r.name in wanted]

    if not rules:
        raise RuleError("没有生效的规则，拒绝执行（宁可不脱敏，也不能静默放行）")

    return RedactionEngine(rules, force_numbered=args.numbered)


def _process_one(
    source: Path,
    output_root: Path,
    input_root: Path,
    engine: RedactionEngine,
    args: argparse.Namespace,
) -> FileResult:
    """处理单个文件，捕获所有异常转成 FileResult，保证批量任务不中断。"""
    started = time.perf_counter()
    try:
        handler = get_handler(source)
        output = _relative_output_path(source, input_root, output_root)
        result = handler.process(
            source,
            output,
            engine,
            dry_run=args.dry_run,
            overwrite_output=args.overwrite_output,
        )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result
    except HandlerError as exc:
        return FileResult(
            source=str(source),
            status="failed",
            reason=exc.code,
            message=exc.message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except PermissionError as exc:
        return FileResult(
            source=str(source),
            status="failed",
            reason=HandlerError.PERMISSION,
            message=str(exc),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 批量任务必须兜住所有异常
        return FileResult(
            source=str(source),
            status="failed",
            reason=HandlerError.UNKNOWN,
            message=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _cmd_run(args: argparse.Namespace) -> int:
    """执行 run / dry-run 的实际逻辑。"""
    report = Report(dry_run=args.dry_run)

    engine = _build_engine(args)

    # 目录输入时以目录本身为相对路径根；多输入时各自为根
    roots: List[Tuple[Path, Path]] = []
    for raw in args.inputs:
        path = Path(raw)
        roots.append((path, path if path.is_dir() else path.parent))

    output_root = Path(args.output).resolve()

    for input_root, _ in roots:
        files, problems = _collect_files([str(input_root)], recursive=args.recursive)
        for problem in problems:
            report.add(FileResult(source=problem, status="failed", reason=HandlerError.IO_ERROR, message=problem))
        for source in files:
            report.add(_process_one(source, output_root, input_root, engine, args))

    if args.report:
        report.save(args.report, as_json=args.report_format == "json")

    print(report.to_json() if args.report_format == "json" else report.to_text())

    # 有文件失败时返回非零码，便于脚本/CI 判断
    return 1 if report.failed else 0


def _cmd_rules(args: argparse.Namespace) -> int:
    """列出当前生效的规则。"""
    rules_dir = Path(args.rules) if args.rules else _default_rules_dir()
    rules = load_rule_dir(rules_dir) if rules_dir.is_dir() else load_rules(rules_dir)

    print(f"规则目录：{rules_dir}")
    print(f"规则总数：{len(rules)}")
    print("-" * 70)
    for rule in rules:
        numbered = "是" if rule.numbered else "否"
        validator = rule.validator or "无"
        print(f"[{rule.name}] -> {rule.placeholder}")
        print(f"    正则：{rule.pattern}")
        print(f"    校验位：{validator}    编号：{numbered}    行号：{rule.line}")
        if rule.description:
            print(f"    说明：{rule.description}")
    print("-" * 70)
    print(f"已支持的文件类型：{', '.join(supported_extensions())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="redactor",
        description="本地离线文件脱敏工具（不联网、不上传、绝不覆盖原文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  redactor dry-run 待脱敏目录/\n"
            "  redactor run 待脱敏目录/ -o 输出目录/\n"
            "  redactor run a.docx b.xlsx --only 手机号 --numbered\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        """run 与 dry-run 共用的参数。"""
        sub.add_argument("inputs", nargs="+", help="待处理的文件或目录（可多个）")
        sub.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIRNAME, help=f"输出根目录（默认 {DEFAULT_OUTPUT_DIRNAME}/）")
        sub.add_argument("--rules", help="规则文件或规则目录（默认使用内置 rules/）")
        sub.add_argument("--only", nargs="+", help="只启用指定名称的规则")
        sub.add_argument("--numbered", action="store_true", help="强制使用编号占位符（如 [手机号#1]，会暴露同一性，默认关闭）")
        sub.add_argument("-r", "--recursive", action="store_true", help="递归处理子目录")
        sub.add_argument("--report", help="把报告另存到指定文件")
        sub.add_argument("--report-format", choices=["text", "json"], default="text", help="报告格式（默认 text）")

    run_parser = subparsers.add_parser("run", help="执行脱敏并输出到新文件/新目录")
    add_common(run_parser)
    run_parser.add_argument("--overwrite-output", action="store_true", help="允许覆盖已存在的输出文件（仍永不覆盖原文件）")
    run_parser.set_defaults(func=_cmd_run, dry_run=False)

    dry_parser = subparsers.add_parser("dry-run", help="只统计将命中的位置，不写任何文件")
    add_common(dry_parser)
    dry_parser.set_defaults(func=_cmd_run, dry_run=True, overwrite_output=False)

    rules_parser = subparsers.add_parser("rules", help="列出当前生效的规则")
    rules_parser.add_argument("--rules", help="规则文件或规则目录（默认使用内置 rules/）")
    rules_parser.set_defaults(func=_cmd_rules)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 主入口。

    Returns:
        进程退出码：0 成功；1 有文件处理失败；2 参数或规则错误。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except RuleError as exc:
        # 规则错误：明确报错退出，绝不静默跳过（需求补充约束 C）
        print(f"[规则错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已被用户中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())