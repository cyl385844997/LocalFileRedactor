"""YAML 双树解析与字段校验（规则文件与词库文件共用）。

**为什么要独立成模块**：``rules.py`` 与 ``lexicon.py`` 都需要同一套能力——
用``yaml.compose`` 建"字段路径 -> 行号"索引（用于精确定位），
用 ``yaml.safe_load`` 取真类型值（bool/int/str/list）。
早期 ``lexicon.py`` 直接 import ``rules._compose_and_load`` 这类私有函数，
导致词库的报错文案里出现"规则 '词库条目 'A''"这种双层引号嵌套与领域词错位。
把能力下沉到本模块并升为公开 API 后，两个调用方各自传入 ``kind`` 即可。

**双树并行原则（切勿简化）**：
    YAML 有两套 API，职责必须分清：
    1. ``yaml.compose(text)`` -> 节点树（Node）。节点只提供**位置信息**
       （行号、键顺序、重复键检测），其 ``ScalarNode.value`` 是**未做类型转换
       的字符串**（``enabled: false`` 拿到的是 ``'false'`` 而非 ``False``）。
    2. ``yaml.safe_load(text)`` -> Python 数据。类型正确（bool/int/str/list），
       但**没有任何位置信息**。

    绝不能拿 ``ScalarNode.value`` 当数据值用——那样 ``isinstance(x, bool)``
    永远为假，是系统性缺陷而非笔误。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from redactor.core.models import RuleError

# 行号索引中的路径元素：字符串 = 映射键，整数 = 序列下标
PathKey = Tuple[Any, ...]


# ================================================================ 节点树工具
def line_of(node: yaml.Node) -> int:
    """取 YAML 节点的行号（从 1 开始）。"""
    return node.start_mark.line + 1


def index_lines(root: yaml.Node) -> Dict[PathKey, int]:
    """遍历节点树，建立"字段路径 -> 行号"的索引表。

    路径示例：顶层 ``rules`` 列表第 0 项的 ``pattern`` 字段
    对应路径 ``("rules", 0, "pattern")``。

    同时完成两件靠节点树才能做的事：
    - **重复键检测**：safe_load 对重复键是"后者静默覆盖前者"，
      必须靠节点树逐对检查才能发现。
    - **键顺序检查**：节点的键是原始标量，不受覆盖影响。

    Args:
        root: ``yaml.compose`` 返回的根节点。

    Returns:
        路径到行号的映射。
    """
    index: Dict[PathKey, int] = {}

    def walk(node: yaml.Node, prefix: PathKey) -> None:
        """递归下降，把每个节点的起始行号记入索引。

        注意：集合节点（映射/序列）**也必须记行号**。
        序列里的一条规则本身是一个映射节点，它起始的那一行就是这条规则在
        YAML 里的位置（``Rule.line``），若只记标量则会缺失并回退成 1。
        """
        index[prefix] = line_of(node)

        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                walk(value_node, prefix + (str(key_node.value),))
        elif isinstance(node, yaml.SequenceNode):
            for position, item_node in enumerate(node.value):
                walk(item_node, prefix + (position,))

    walk(root, ())
    return index


def find_duplicate_keys(root: yaml.Node) -> Optional[Tuple[str, int, int]]:
    """在节点树中查找第一个重复键。

    Returns:
        ``(键名, 首次出现行号, 重复出现行号)``；无重复返回 None。
    """

    def walk(node: yaml.Node) -> Optional[Tuple[str, int, int]]:
        """深度优先查找，返回首个重复键。"""
        if isinstance(node, yaml.MappingNode):
            seen: Dict[str, int] = {}
            for key_node, _value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = str(key_node.value)
                if key in seen:
                    return key, seen[key], line_of(key_node)
                seen[key] = line_of(key_node)
            for _key_node, value_node in node.value:
                found = walk(value_node)
                if found is not None:
                    return found
        elif isinstance(node, yaml.SequenceNode):
            for item_node in node.value:
                found = walk(item_node)
                if found is not None:
                    return found
        return None

    return walk(root)


def compose_and_load(text: str, path: Path, *, kind: str = "规则") -> Tuple[yaml.Node, Any]:
    """双入口解析：节点树（定位）+ Python 数据（取值）。

    Args:
        text: YAML 文本。
        path: 用于报错的文件路径。
        kind: 配置类别名（"规则"/"词库"），只用于错误文案。

    Returns:
        ``(根节点, safe_load 结果)``。

    Raises:
        RuleError: YAML 语法错误、重复键。
    """
    try:
        root = yaml.compose(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = (mark.line + 1) if mark else 0
        raise RuleError(
            f"{kind}文件 YAML 语法错误：{exc.problem or exc.context or exc}"
            f"（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        ) from exc
    except yaml.YAMLError as exc:  # 极少数无 mark 的情况
        raise RuleError(
            f"{kind}文件 YAML 解析失败：{exc}（{path}）", path=str(path)
        ) from exc

    if root is None:
        raise RuleError(f"{kind}文件为空：{path}", path=str(path))

    # 重复键必须在节点树上查：safe_load 会静默取后者
    duplicate = find_duplicate_keys(root)
    if duplicate is not None:
        key, first_line, again_line = duplicate
        raise RuleError(
            f"{kind}中出现重复字段 '{key}'（{path} 第 {again_line} 行，"
            f"首次出现于第 {first_line} 行）",
            path=str(path),
            line=again_line,
        )

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # compose 已过一遍，这里只是兜底
        raise RuleError(
            f"{kind}文件 YAML 解析失败：{exc}（{path}）", path=str(path)
        ) from exc

    return root, data


# ================================================================ 字段校验
def require_str(
    value: Any,
    line: int,
    path: Path,
    field_name: str,
    label: str,
) -> str:
    """校验字段为非空字符串。

    Args:
        value: 由 safe_load 得到的**真实类型**值。
        line: 该字段的行号（来自节点树索引）。
        path: 配置文件路径。
        field_name: 字段名，用于报错。
        label: 错误文案中的主语，如 ``规则 '手机号'`` 或 ``词库条目 '甲方单位'``。
            调用方负责拼好引号，避免出现双层引号嵌套。

    Returns:
        去除首尾空白后的字符串。

    Raises:
        RuleError: 字段缺失或不是非空字符串。
    """
    if value is None:
        raise RuleError(
            f"{label} 缺少必填字段 '{field_name}'（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    if not isinstance(value, str) or not value.strip():
        raise RuleError(
            f"{label} 的字段 '{field_name}' 必须是非空字符串（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    return value.strip()


def require_bool(
    value: Any,
    line: int,
    path: Path,
    field_name: str,
    label: str,
    *,
    default: bool,
) -> bool:
    """校验字段为布尔值。

    ``default`` 是**该字段自己的缺省语义**，必须由调用方显式传入：
    - ``enabled`` 缺省 True（默认生效）
    - ``numbered`` 缺省 False（默认不暴露同一性）

    早期版本在这里硬编码 ``return False``，导致两个语义相反的字段共用同一个
    缺省值：``enabled`` 未写时会被判为禁用，引擎一条规则都加载不到。

    Args:
        value: 由 safe_load 得到的真实类型值（None 表示字段缺省）。
        line: 字段所在行的行号（缺省时为条目所在行，仅用于报错兜底）。
        path: 配置文件路径。
        field_name: 字段名。
        label: 错误文案主语（同 :func:`require_str`）。
        default: 字段缺省时的取值。

    Raises:
        RuleError: 值不是布尔类型。
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuleError(
            f"{label} 的字段 '{field_name}' 必须是 true/false（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    return value


def read_text(path: str | Path, *, kind: str = "规则") -> Tuple[str, Path]:
    """以 UTF-8 读取配置文件，失败时给出统一错误。

    Args:
        path: 文件路径。
        kind: 配置类别名，仅用于错误文案。

    Returns:
        ``(文件内容, Path 对象)``。

    Raises:
        RuleError: 文件不存在、非UTF-8、或读取失败。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise RuleError(f"{kind}文件不存在：{file_path}", path=str(file_path))
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuleError(
            f"{kind}文件不是 UTF-8 编码：{file_path}", path=str(file_path)
        ) from exc
    except OSError as exc:
        raise RuleError(
            f"{kind}文件读取失败：{file_path}（{exc}）", path=str(file_path)
        ) from exc
    return text, file_path
