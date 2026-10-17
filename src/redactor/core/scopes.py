"""P7 范围脱敏配置（"输入一个关键字，自动匹配带该后缀的整个实体"）。

与词库（:mod:`redactor.core.lexicon`）的关系
------------------------------------------
**刻意分开，不合并**。二者语义不同：

    词库    —— 精确列表，用户把"重庆某某公司"整条写进去
    范围脱敏 —— 泛化规则，用户只写"公司"，凡是带该后缀的实体全脱敏

混在一起会让用户困惑"我的公司名到底要不要写进词库"。
故本模块独立，走独立的 YAML 顶层键 ``scopes:``。

为什么匹配算法不能用正则（**全部实测结论，见 .verify/probe_gui/P7_方案.md**）
--------------------------------------------------------------------------
六轮探针对比（语料：12 正例 + 22 负例 + 6 重叠例）：

    ===========================  ====  =====  ======  ==========
    方案                         正例  完整  误伤   吞内置规则
    ===========================  ====  =====  ======  ==========
    朴素贪婪 ``[一-龥]{2,15}(公司)`` 12/12  12/12   8/22   0
    加负向前瞻 ``(?<!去)...``      12/12  12/12   7/22   0   ← 证伪
    大字符类排除                    12/12  6/12    0/22   0   ← 过度收缩
    轻量字符类排除                  12/12  12/12  13/22   3   ← 反而更差
    加左边界锚定                    10/12  10/12  12/22   0
    **后缀锚点 + 反向扫描（本方案）** 12/12 11/12   2/22   **0**
    ===========================  ====  =====  ======  ==========

三条硬结论：

1. **负向前瞻在贪婪量词前结构性无效** —— ``[一-龥]{2,15}`` 会先把"去"
   吞进实体名，负向前瞻检查的位置变成"他"的前一个字，看不到"去"。

2. **"排除字符"反而更差** —— 排掉"在"之后引擎会跳过它继续往后吞，
   误伤从 8/22 涨到 13/22。

3. **正则会跨数字吞并（致命）** —— ``[^...A-Za-z0-9]{1,14}`` 实测把
   ``电话13812345678，公司`` 整段吞掉。按 ``resolve_overlaps`` 的
   "更长者优先"，范围规则胜出 → **手机号被吃掉且以残缺形式泄露**。
   脱敏工具若这样等于没脱敏，故纯正则方案在架构上否决。

**根本原因**（语言学歧义，非工程问题）：
``重庆大学`` 的"重庆"是地名，``去大学`` 的"去"是动词，
两者都是"2 个汉字 + 后缀"，**字形完全无法区分**。
不存在一个既全中正例又不误伤负例的纯正则。

因此采用**后缀锚点 + 反向逐字符扫描**：
数字/字母/标点/空白是"停止字符"，反向扫描遇之即停
→ 数字天然不会被吞进实体名，Q4 从根上解决。

严格度分级
----------
实测"仅 HIGH 后缀"档：正例 6/12，**误伤 0/22，吞数字 0**。
HIGH 后缀（有限公司 / 集团 / 研究院…）是法律或机构专用，
日常文本里几乎不会裸奔；LOW 后缀（公司 / 大学 / 项目…）是日常词，
误伤风险高（实测 8/22）。故分档让用户自己取舍：

    保守（conservative） 只开 HIGH —— 宁可漏，不可错
    平衡（balanced）    HIGH+LOW —— 默认档
    激进（aggressive）  HIGH+LOW + 首字黑名单 —— 极少误伤，可能漏
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from redactor.core.models import RuleError
from redactor.core.yaml_loader import (
    PathKey,
    compose_and_load,
    index_lines,
    read_text,
    require_bool,
    require_str,
)


# ================================================================ 后缀分级
#: HIGH —— 法律 / 机构专用后缀。实测误伤 0/22。
#: 按长度降序排列（"有限公司" 必须先于 "公司" 匹配，见 :mod:`recognizers.scope_recognizer`）。
HIGH_SUFFIXES: Tuple[str, ...] = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "附属医院",
    "研究院",
    "事务所",
    "控股",
    "集团",
    "学院",
)

#: LOW —— 日常词后缀，误伤风险高（实测 8/22），需显式开启。
LOW_SUFFIXES: Tuple[str, ...] = (
    "公司",
    "大学",
    "项目",
    "医院",
    "中心",
    "厂",
    "局",
)

#: 三档严格度 → 启用的后缀（稳定顺序，保证匹配优先级可复现）
TIER_SUFFIXES: Dict[str, Tuple[str, ...]] = {
    "conservative": HIGH_SUFFIXES,
    "balanced": HIGH_SUFFIXES + LOW_SUFFIXES,
    "aggressive": HIGH_SUFFIXES + LOW_SUFFIXES,
}

#: 严格度档位名（供 CLI / GUI 使用）
TIERS: Tuple[str, ...] = ("conservative", "balanced", "aggressive")

#: 严格度的中文显示名（GUI 用）
TIER_LABELS: Dict[str, str] = {
    "conservative": "保守（仅法律/机构后缀，零误伤风险）",
    "balanced": "平衡（推荐）",
    "aggressive": "激进（+ 首字黑名单，极少误伤，可能漏）",
}

#: 激进档专用的首字黑名单 —— 指示代词/量词做实体名首字时几乎必是误伤。
#: **仅在 aggressive 档启用**：实测能压到 1/22 误伤，但会误杀
#: "重庆水务集团""西南大学"等正例（首字回退到别的路径）。
LEAD_STOP_CHARS: str = "这那每某该此各本"

#: 默认严格度（v0.7.0 裁定：平衡档）
DEFAULT_TIER = "balanced"

#: 实体名最小长度（不含后缀）。1 字实体名（裸"公司"）不脱敏——它没有实体信息。
MIN_NAME_LEN = 2

#: 实体名最大长度（不含后缀）。超出则截断到这个长度。
MAX_NAME_LEN = 20


@dataclass(frozen=True)
class ScopeEntry:
    """一条范围脱敏配置。

    注意：本对象**不含**具体实体名（只含后缀），不属敏感配置，
    但仍不写入报告（报告只用 ``name`` / 占位符 / 位置）。

    Attributes:
        name: 组名。**仅用于报告分组**，占位符由后缀决定
            （裁定 4：按后缀占位，如 ``[公司]`` / ``[大学]``）。
        suffixes: 该组的后缀列表（已归一化为小写英文无关的形式）。
        tier: 严格度档位名。
        enabled: 是否参与匹配。
        strict_lead: 是否启用首字黑名单（仅 aggressive 档有意义）。
        note: 备注，仅用于文档。
        line: 该条目在 YAML 中的行号（从 1 开始），用于报错定位。
    """

    name: str
    suffixes: Tuple[str, ...] = ()
    tier: str = DEFAULT_TIER
    enabled: bool = True
    strict_lead: bool = False
    note: str = ""
    line: int = 0

    @property
    def active_suffixes(self) -> Tuple[str, ...]:
        """实际用于匹配的后缀：启用条目返回自身，禁用返回空。"""
        return self.suffixes if self.enabled else ()

    def placeholder_for(self, suffix: str) -> str:
        """某后缀对应的占位符（裁定 4：按后缀，不按组）。

        ``重庆某某有限公司`` → ``[有限公司]``
        ``重庆大学`` → ``[大学]``
        """
        return f"[{suffix}]"


# ================================================================ 内部工具
_ALLOWED_KEYS = {
    "name",
    "suffixes",
    "tier",
    "enabled",
    "strict_lead",
    "note",
}
_TOP_KEY = "scopes"


def _build_entry(
    data: Any,
    prefix: PathKey,
    lines: Dict[PathKey, int],
    path: Path,
    index: int,
    reserved_names: Optional[Iterable[str]] = None,
) -> ScopeEntry:
    """把一条范围条目的数据（safe_load）与行号索引合成 :class:`ScopeEntry`。

    Raises:
        RuleError: 字段缺失 / 类型错 / 未知字段 / 后缀与内置规则重名 等。
    """
    fallback_line = lines.get(prefix, lines.get(prefix[:-1], 1))
    label = f"#{index + 1}"

    if not isinstance(data, dict):
        raise RuleError(
            f"第 {index + 1} 条范围条目必须是键值映射（{path} 第 {fallback_line} 行）",
            path=str(path), line=fallback_line,
        )

    for key in data:
        if key not in _ALLOWED_KEYS:
            raise RuleError(
                f"范围条目 {label} 含未知字段 {key!r}"
                f"（{path} 第 {lines.get(prefix + (key,), fallback_line)} 行）；"
                f"允许字段：{sorted(_ALLOWED_KEYS)}",
                path=str(path),
                line=lines.get(prefix + (key,), fallback_line),
            )

    def line_of(field: str) -> int:
        """取某字段的行号（缺省时用条目行号，仅用于报错兜底）。"""
        return lines.get(prefix + (field,), fallback_line)

    name = require_str(data.get("name"), line_of("name"), path, "name", f"范围条目 {label}")
    if not name.strip():
        raise RuleError(
            f"范围条目 {label} 的 'name' 不能为空（{path} 第 {fallback_line} 行）",
            path=str(path), line=fallback_line,
        )
    reserved = set(reserved_names or ())
    if name in reserved:
        raise RuleError(
            f"范围条目 {label} 的名称 {name!r} 与内置规则重名"
            f"（{path} 第 {fallback_line} 行）",
            path=str(path), line=fallback_line,
        )

    # ---- suffixes：必填的非空字符串列表 ----
    if "suffixes" not in data:
        raise RuleError(
            f"范围条目 {label}（{name}）缺少 'suffixes' 字段"
            f"（{path} 第 {fallback_line} 行）",
            path=str(path), line=fallback_line,
        )
    raw_sufs = data["suffixes"]
    suf_line = lines.get(prefix + ("suffixes",), fallback_line)
    if not isinstance(raw_sufs, list) or not raw_sufs:
        raise RuleError(
            f"范围条目 {label}（{name}）的 'suffixes' 必须是非空列表"
            f"（{path} 第 {suf_line} 行）",
            path=str(path), line=suf_line,
        )
    suffixes: List[str] = []
    for j, item in enumerate(raw_sufs):
        item_line = lines.get(prefix + ("suffixes", j), suf_line)
        if not isinstance(item, str) or not item.strip():
            raise RuleError(
                f"范围条目 {label}（{name}）的第 {j + 1} 个后缀必须是非空字符串"
                f"（{path} 第 {item_line} 行）",
                path=str(path), line=item_line,
            )
        suf = item.strip()
        if suf in suffixes:
            raise RuleError(
                f"范围条目 {label}（{name}）的后缀 {suf!r} 重复"
                f"（{path} 第 {item_line} 行）",
                path=str(path), line=item_line,
            )
        # 停用字符做后缀会永远匹配不到，白占坑位
        if any(ch in LEAD_STOP_CHARS for ch in suf):
            raise RuleError(
                f"范围条目 {label}（{name}）的后缀 {suf!r} 含首字黑名单字符"
                f"（{path} 第 {item_line} 行）；后缀本身不该含 {LEAD_STOP_CHARS!r}",
                path=str(path), line=item_line,
            )
        suffixes.append(suf)

    # ---- tier：必须是三档之一 ----
    tier = DEFAULT_TIER
    if "tier" in data:
        tier = require_str(
            data.get("tier"), line_of("tier"), path, "tier", f"范围条目 {label}"
        )
        if tier not in TIERS:
            tier_line = lines.get(prefix + ("tier",), fallback_line)
            raise RuleError(
                f"范围条目 {label}（{name}）的 tier={tier!r} 无效"
                f"（{path} 第 {tier_line} 行）；可选：{list(TIERS)}",
                path=str(path), line=tier_line,
            )

    enabled = True
    if "enabled" in data:
        enabled = require_bool(
            data.get("enabled"), line_of("enabled"), path, "enabled",
            f"范围条目 {label}", default=True,
        )

    strict_lead = False
    if "strict_lead" in data:
        strict_lead = require_bool(
            data.get("strict_lead"), line_of("strict_lead"), path, "strict_lead",
            f"范围条目 {label}", default=False,
        )

    note = ""
    if "note" in data:
        note = require_str(
            data.get("note"), line_of("note"), path, "note", f"范围条目 {label}"
        ) if data.get("note") is not None else ""

    return ScopeEntry(
        name=name,
        suffixes=tuple(suffixes),
        tier=tier,
        enabled=enabled,
        strict_lead=strict_lead,
        note=note,
        line=fallback_line,
    )


# ================================================================ 加载入口
def load_scopes_from_text(
    text: str,
    path: Path,
    *,
    reserved_names: Optional[Iterable[str]] = None,
) -> List[ScopeEntry]:
    """从 YAML 文本加载范围条目。

    Args:
        text: YAML 文本（由 :func:`read_text` 读完并做过编码检查）。
        path: 文件路径（仅用于报错文案）。
        reserved_names: 已被占用的名称集合（通常传内置规则名）。

    Returns:
        范围条目列表（保持 YAML 顺序）。

    Raises:
        RuleError: 语法错 / 字段非法 / 顶层键不对 等。**绝不静默跳过**。
    """
    root, data = compose_and_load(text, path, kind="范围配置")
    lines = index_lines(root)

    if not isinstance(data, dict) or _TOP_KEY not in data:
        raise RuleError(
            f"范围配置缺少顶层键 {_TOP_KEY!r}（{path}）",
            path=str(path),
        )
    for key in data:
        if key != _TOP_KEY:
            raise RuleError(
                f"范围配置含未知顶层键 {key!r}（{path}）；只允许 {_TOP_KEY!r}",
                path=str(path),
            )

    raw_list = data[_TOP_KEY]
    top_line = lines.get((_TOP_KEY,), 1)
    if not isinstance(raw_list, list):
        raise RuleError(
            f"{_TOP_KEY} 必须是列表（{path} 第 {top_line} 行）",
            path=str(path), line=top_line,
        )

    entries: List[ScopeEntry] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(raw_list):
        entry = _build_entry(
            item, (_TOP_KEY, index), lines, path, index, reserved_names
        )
        if entry.name in seen:
            raise RuleError(
                f"范围条目名称重复：{entry.name!r}"
                f"（第 {seen[entry.name] + 1} 条与第 {index + 1} 条，{path}）",
                path=str(path), line=entry.line,
            )
        seen[entry.name] = index
        entries.append(entry)
    return entries


def load_scopes(
    path: str | Path,
    *,
    reserved_names: Optional[Iterable[str]] = None,
) -> List[ScopeEntry]:
    """从 YAML 文件加载范围条目。

    Args:
        path: 配置文件路径。
        reserved_names: 已被占用的名称集合。

    Returns:
        范围条目列表。

    Raises:
        RuleError: 文件不存在 / 编码错 / 语法错 / 字段非法等。
    """
    text, lex_path = read_text(path, kind="范围配置")
    return load_scopes_from_text(text, lex_path, reserved_names=reserved_names)


def default_scopes_path() -> Optional[Path]:
    """返回默认范围配置路径（``~/.redactor/scopes.yaml``）。

    文件不存在时返回 ``None`` —— **是否报错由调用方决定**
    （CLI 显式指定 ``--scopes`` 却找不到必须报错）。
    """
    candidate = Path.home() / ".redactor" / "scopes.yaml"
    return candidate if candidate.is_file() else None


def expand_suffixes(
    entries: Sequence[ScopeEntry],
    *,
    tier: Optional[str] = None,
) -> Tuple[Tuple[str, str, str], ...]:
    """把范围条目展开成 ``(suffix, placeholder, group_name)`` 三元组序列。

    按档位过滤后缀：``tier`` 决定启用 HIGH 还是 LOW 后缀；
    ``tier=None`` 时用条目自身的 ``tier`` 字段。

    后缀按**长度降序**排列 —— "有限公司" 必须先于 "公司" 匹配，
    否则"重庆某某有限公司"会被切在"公司"处丢掉"有限"。

    Args:
        entries: 范围条目列表。
        tier: 全局档位覆盖；None 时逐条用 ``entry.tier``。

    Returns:
        去重后的三元组序列（后缀长度降序）。
    """
    out: List[Tuple[str, str, str]] = []
    seen: set = set()
    for entry in entries:
        if not entry.enabled:
            continue
        use_tier = tier if tier is not None else entry.tier
        if use_tier not in TIERS:
            raise RuleError(f"未知严格度档位：{use_tier!r}（可选 {list(TIERS)}）")
        allowed = set(TIER_SUFFIXES[use_tier])
        for suf in entry.suffixes:
            if suf not in allowed:
                continue
            if suf in seen:
                continue
            seen.add(suf)
            out.append((suf, entry.placeholder_for(suf), entry.name))
    out.sort(key=lambda t: (-len(t[0]), t[0]))
    return tuple(out)


__all__ = [
    "DEFAULT_TIER",
    "HIGH_SUFFIXES",
    "LEAD_STOP_CHARS",
    "LOW_SUFFIXES",
    "MAX_NAME_LEN",
    "MIN_NAME_LEN",
    "ScopeEntry",
    "TIERS",
    "TIER_LABELS",
    "TIER_SUFFIXES",
    "default_scopes_path",
    "expand_suffixes",
    "load_scopes",
    "load_scopes_from_text",
]