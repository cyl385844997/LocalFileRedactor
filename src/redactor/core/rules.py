"""YAML 规则加载与严格校验。

设计要点（对应需求约束 C）：
    规则文件加载失败时**直接报错退出并指出具体行号**，
    绝不静默跳过某条规则——静默跳过等于漏脱敏。

实现方式：**双树并行**
    YAML 有两套 API，职责必须分清：
    1. ``yaml.compose(text)`` -> 节点树（Node）。节点只提供**位置信息**
       （行号、键顺序、重复键检测），其 ``ScalarNode.value`` 是**未做类型转换的字符串**
       （``enabled: false`` 拿到的是 ``'false'`` 而非 ``False``）。
    2. ``yaml.safe_load(text)`` -> Python 数据。类型正确（bool/int/str/list），
       但**没有任何位置信息**。

    因此：用 safe_load 取值，用 compose 建一张"路径 -> 行号"的索引表，
    两者并行遍历。这样既拿到真类型值，又能把每个字段的错误精确定位到行。
    绝不能拿 ``ScalarNode.value`` 当数据值用——那样 ``isinstance(x, bool)``
    永远为假，是系统性缺陷而非笔误。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from redactor.core.models import FLAG_WHITELIST, Rule, RuleError

# 规则文件中允许出现的字段（白名单，出现未知字段即报错，防止拼写错误被忽略）
_ALLOWED_KEYS = {
    "name",
    "pattern",
    "placeholder",
    "numbered",
    "enabled",
    "validator",
    "flags",
    "description",
}

# 允许的校验函数名，与 recognizers.validators 中注册的一致
_ALLOWED_VALIDATORS = {"id_card", "luhn"}

# 行号索引中的路径元素：字符串 = 映射键，整数 = 序列下标
PathKey = Tuple[Any, ...]


def _line_of(node: yaml.Node) -> int:
    """取 YAML 节点的行号（从 1 开始）。"""
    return node.start_mark.line + 1


def _index_lines(root: yaml.Node) -> Dict[PathKey, int]:
    """遍历节点树，建立"字段路径 -> 行号"的索引表。

    路径示例：顶层 ``rules`` 列表第 0 项的 ``pattern`` 字段
    对应路径 ``("rules", 0, "pattern")``。

    同时完成两件靠节点树才能做的事：
    - **重复键检测**：safe_load 对重复键是"后者静默覆盖前者"，
      必须靠节点树逐对检查才能发现。
    - **键顺序/未知字段检查**：节点的键是原始标量，不受覆盖影响。

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
        index[prefix] = _line_of(node)

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


def _find_duplicate_keys(root: yaml.Node) -> Optional[Tuple[str, int, int]]:
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
                    return key, seen[key], _line_of(key_node)
                seen[key] = _line_of(key_node)
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


def _require_str(value: Any, line: int, path: Path, field_name: str, rule_name: str) -> str:
    """校验字段为非空字符串。

    Args:
        value: 由 safe_load 得到的**真实类型**值。
        line: 该字段的行号（来自节点树索引）。
        path: 规则文件路径。
        field_name: 字段名，用于报错。
        rule_name: 规则名，用于报错。
    """
    if value is None:
        raise RuleError(
            f"规则 '{rule_name}' 缺少必填字段 '{field_name}'（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    if not isinstance(value, str) or not value.strip():
        raise RuleError(
            f"规则 '{rule_name}' 的字段 '{field_name}' 必须是非空字符串"
            f"（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    return value.strip()


def _require_bool(
    value: Any,
    line: int,
    path: Path,
    field_name: str,
    rule_name: str,
    *,
    default: bool,
) -> bool:
    """校验字段为布尔值。

    ``default`` 是**该字段自己的缺省语义**，必须由调用方显式传入：
    - ``enabled`` 缺省 True（规则默认生效）
    - ``numbered`` 缺省 False（默认不暴露同一性）

    早期版本在这里硬编码 ``return False``，导致两个语义相反的字段共用同一个
    缺省值：``enabled`` 未写时会被判为禁用，引擎一条规则都加载不到。

    Args:
        value: 由 safe_load 得到的真实类型值（None 表示字段缺省）。
        line: 字段所在行的行号（缺省时为规则所在行，仅用于报错兜底）。
        default: 字段缺省时的取值。
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuleError(
            f"规则 '{rule_name}' 的字段 '{field_name}' 必须是 true/false"
            f"（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )
    return value


def _validate_flags(values: Any, item_lines: Sequence[int], field_line: int, path: Path, rule_name: str) -> List[str]:
    """校验正则标志名列表，只接受白名单内的名字。

    Args:
        values: safe_load 得到的列表（元素已是真类型 str）。
        item_lines: 每个列表元素在节点树中的行号。
        field_line: ``flags:`` 这一行本身的行号。
        path: 规则文件路径。
        rule_name: 规则名。

    Returns:
        合法标志名列表。
    """
    if values is None:
        return []
    if not isinstance(values, list):
        raise RuleError(
            f"规则 '{rule_name}' 的字段 'flags' 必须是列表（{path} 第 {field_line} 行）",
            path=str(path),
            line=field_line,
        )

    for position, item in enumerate(values):
        item_line = item_lines[position] if position < len(item_lines) else field_line
        if not isinstance(item, str) or item not in FLAG_WHITELIST:
            raise RuleError(
                f"规则 '{rule_name}' 的 flags 含非法标志 '{item}'，"
                f"只允许 {sorted(FLAG_WHITELIST)}（{path} 第 {item_line} 行）",
                path=str(path),
                line=item_line,
            )
    return [str(item) for item in values]


def _build_rule(
    data: Any,
    prefix: PathKey,
    lines: Dict[PathKey, int],
    path: Path,
    index: int,
) -> Rule:
    """把一条规则的数据（safe_load）与位置索引（compose）合成 :class:`Rule`。

    Args:
        data: 该规则的 dict（真类型）。
        prefix: 该规则在行号索引中的路径前缀，如 ``("rules", 0)``。
        lines: 行号索引表。
        path: 规则文件路径。
        index: 规则序号（从 0 开始），用于错误提示。

    Raises:
        RuleError: 字段非法、正则编译失败等一切问题。
    """
    # 规则整体所在行（列表项自身是序列元素，通常无标量行号，回退到字段行）
    fallback_line = lines.get(prefix, lines.get(prefix[:-1], 1))

    if not isinstance(data, dict):
        raise RuleError(
            f"第 {index + 1} 条规则必须是键值映射（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )

    def line_of(field: str) -> int:
        """取某字段的行号；缺省时回退到规则所在行。"""
        return lines.get(prefix + (field,), fallback_line)

    # 未知字段检查：只能靠节点树，因为 safe_load 里看不到"被覆盖掉的"字段
    for field in data:
        if field not in _ALLOWED_KEYS:
            raise RuleError(
                f"规则含未知字段 '{field}'，允许的字段为 {sorted(_ALLOWED_KEYS)}"
                f"（{path} 第 {line_of(field)} 行）",
                path=str(path),
                line=line_of(field),
            )

    if "name" not in data:
        raise RuleError(
            f"规则缺少必填字段 'name'（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )
    name = _require_str(data.get("name"), line_of("name"), path, "name", f"#{index + 1}")

    if "pattern" not in data:
        raise RuleError(
            f"规则 '{name}' 缺少必填字段 'pattern'（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )
    pattern = _require_str(data.get("pattern"), line_of("pattern"), path, "pattern", name)

    # flags：值取 safe_load 的列表（元素是真类型 str），行号取节点树
    flag_item_lines = [
        lines.get(prefix + ("flags", position), line_of("flags"))
        for position in range(len(data["flags"]) if isinstance(data.get("flags"), list) else 0)
    ]
    flags = _validate_flags(data.get("flags"), flag_item_lines, line_of("flags"), path, name)

    # 正则编译：失败必须报出规则名 + 行号 + 原始错误
    pattern_line = line_of("pattern")
    try:
        flag_value = 0
        for flag_name in flags:
            flag_value |= FLAG_WHITELIST[flag_name]
        re.compile(pattern, flag_value)
    except re.error as exc:
        raise RuleError(
            f"规则 '{name}' 的正则编译失败：{exc}（{path} 第 {pattern_line} 行）",
            path=str(path),
            line=pattern_line,
        ) from exc

    validator = None
    if data.get("validator") is not None:
        validator = _require_str(data["validator"], line_of("validator"), path, "validator", name)
        if validator not in _ALLOWED_VALIDATORS:
            raise RuleError(
                f"规则 '{name}' 的 validator '{validator}' 不受支持，"
                f"只允许 {sorted(_ALLOWED_VALIDATORS)}（{path} 第 {line_of('validator')} 行）",
                path=str(path),
                line=line_of("validator"),
            )

    placeholder = ""
    if data.get("placeholder") is not None:
        placeholder = _require_str(data["placeholder"], line_of("placeholder"), path, "placeholder", name)

    description = ""
    if data.get("description") is not None:
        description = str(data["description"])

    return Rule(
        name=name,
        pattern=pattern,
        placeholder=placeholder or f"[{name}]",
        # 两个布尔字段语义相反，缺省值必须分别指定
        numbered=_require_bool(
            data.get("numbered"), line_of("numbered"), path, "numbered", name, default=False
        ),
        enabled=_require_bool(
            data.get("enabled"), line_of("enabled"), path, "enabled", name, default=True
        ),
        validator=validator,
        flags=flags,
        description=description,
        line=fallback_line,
    )


def _compose_and_load(text: str, path: Path) -> Tuple[yaml.Node, Any]:
    """双入口解析：节点树（定位）+ Python 数据（取值）。

    Args:
        text: YAML 文本。
        path: 用于报错的文件路径。

    Returns:
        ``(根节点, safe_load 结果)``。

    Raises:
        RuleError: YAML 语法错误、重复键、顶层结构非法。
    """
    try:
        root = yaml.compose(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = (mark.line + 1) if mark else 0
        raise RuleError(
            f"YAML 语法错误：{exc.problem or exc.context or exc}（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        ) from exc
    except yaml.YAMLError as exc:  # 极少数无 mark 的情况
        raise RuleError(f"YAML 解析失败：{exc}（{path}）", path=str(path)) from exc

    if root is None:
        raise RuleError(f"规则文件为空：{path}", path=str(path))

    # 重复键必须在节点树上查：safe_load 会静默取后者
    duplicate = _find_duplicate_keys(root)
    if duplicate is not None:
        key, first_line, again_line = duplicate
        raise RuleError(
            f"规则中出现重复字段 '{key}'（{path} 第 {again_line} 行，首次出现于第 {first_line} 行）",
            path=str(path),
            line=again_line,
        )

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # compose 已过一遍，这里只是兜底
        raise RuleError(f"YAML 解析失败：{exc}（{path}）", path=str(path)) from exc

    return root, data


def load_rules_from_text(text: str, *, path: Path) -> List[Rule]:
    """从 YAML 文本加载规则列表。

    Args:
        text: YAML 文本内容。
        path: 用于报错的文件路径（可以是虚拟名，便于测试）。

    Returns:
        全部规则（含 ``enabled: false`` 的），保持 YAML 中的顺序，顺序即命中冲突时的优先级。
        是否过滤由 :func:`load_rules` 决定。

    Raises:
        RuleError: YAML 语法错误、字段非法、正则编译失败等一切问题。
    """
    root, data = _compose_and_load(text, path)

    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        line = _line_of(root)
        raise RuleError(
            f"规则文件顶层必须是键值映射（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )

    if "rules" not in data:
        raise RuleError(f"规则文件缺少顶层 'rules' 列表（{path}）", path=str(path))

    lines = _index_lines(root)

    rules_data = data["rules"]
    if not isinstance(rules_data, list):
        raise RuleError(
            f"'rules' 必须是列表（{path} 第 {lines.get(('rules',), 1)} 行）",
            path=str(path),
            line=lines.get(("rules",), 1),
        )

    if not rules_data:
        raise RuleError(f"'rules' 列表为空，没有任何规则（{path}）", path=str(path))

    rules: List[Rule] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(rules_data):
        rule = _build_rule(item, ("rules", index), lines, path, index)
        # 规则名重复会导致占位符语义不明（例如两条都叫"号码"），直接报错
        if rule.name in seen:
            raise RuleError(
                f"规则名 '{rule.name}' 重复（{path} 第 {rule.line} 行，"
                f"首次出现于第 {seen[rule.name]} 行）",
                path=str(path),
                line=rule.line,
            )
        seen[rule.name] = rule.line
        rules.append(rule)

    return rules


def load_rules(path: str | Path) -> List[Rule]:
    """从 YAML 文件加载**启用中**的规则。

    Args:
        path: 规则文件路径。

    Returns:
        启用中的规则列表（保持 YAML 中的顺序，顺序决定命中冲突时的优先级）。

    Raises:
        RuleError: 文件不存在、编码错误、YAML 语法错误、正则编译失败。
    """
    rule_path = Path(path)
    if not rule_path.is_file():
        raise RuleError(f"规则文件不存在：{rule_path}", path=str(rule_path))

    try:
        text = rule_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuleError(f"规则文件不是 UTF-8 编码：{rule_path}", path=str(rule_path)) from exc
    except OSError as exc:
        raise RuleError(f"规则文件读取失败：{rule_path}（{exc}）", path=str(rule_path)) from exc

    all_rules = load_rules_from_text(text, path=rule_path)
    return [r for r in all_rules if r.enabled]


def load_rule_dir(directory: str | Path) -> List[Rule]:
    """加载目录下所有 ``*.yaml`` / ``*.yml`` 规则文件（按文件名排序，保证可复现）。

    Args:
        directory: 规则目录。

    Returns:
        合并后的启用规则列表；跨文件的同名规则视为错误。

    Raises:
        RuleError: 目录不存在、任一文件加载失败。
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise RuleError(f"规则目录不存在：{dir_path}", path=str(dir_path))

    files = sorted([*dir_path.glob("*.yaml"), *dir_path.glob("*.yml")])
    if not files:
        raise RuleError(f"规则目录内没有 .yaml/.yml 文件：{dir_path}", path=str(dir_path))

    merged: List[Rule] = []
    seen: Dict[str, str] = {}
    for file in files:
        for rule in load_rules(file):
            if rule.name in seen:
                raise RuleError(
                    f"规则名 '{rule.name}' 在 {seen[rule.name]} 与 {file} 中重复"
                    f"（{file} 第 {rule.line} 行）",
                    path=str(file),
                    line=rule.line,
                )
            seen[rule.name] = str(file)
            merged.append(rule)
    return merged