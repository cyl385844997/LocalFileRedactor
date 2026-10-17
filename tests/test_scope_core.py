"""P7 范围脱敏 —— core/scopes.py 的加载与校验测试。

与 tests/test_scope_recognizer.py 分开：本文件只测**配置层**，
识别器层在 test_scope_recognizer.py。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.models import RuleError
from redactor.core.scopes import (
    DEFAULT_TIER,
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    ScopeEntry,
    TIERS,
    TIER_LABELS,
    TIER_SUFFIXES,
    default_scopes_path,
    expand_suffixes,
    load_scopes,
    load_scopes_from_text,
)


def _write(tmp_path: Path, text: str, name: str = "scopes.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


GOOD = """
scopes:
  - name: 单位
    suffixes: ["有限公司", "公司", "集团"]
    note: 泛化匹配所有带这些后缀的实体
"""


class TestLoadScopes:
    def test_最小配置可加载(self, tmp_path: Path):
        p = _write(tmp_path, GOOD)
        entries = load_scopes(p)
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "单位"
        assert e.suffixes == ("有限公司", "公司", "集团")
        assert e.enabled is True
        assert e.tier == DEFAULT_TIER

    def test_占位符按后缀不按组(self, tmp_path: Path):
        """★裁定 4：占位符按后缀（[公司]/[大学]），不是按组（[单位]）。"""
        p = _write(tmp_path, GOOD)
        e = load_scopes(p)[0]
        assert e.placeholder_for("公司") == "[公司]"
        assert e.placeholder_for("有限公司") == "[有限公司]"
        assert e.placeholder_for("大学") == "[大学]"

    def test_tier_可显式指定(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 单位
    suffixes: ["公司"]
    tier: conservative
""")
        assert load_scopes(p)[0].tier == "conservative"

    def test_enabled_false_仍加载但active为空(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 单位
    suffixes: ["公司"]
    enabled: false
""")
        e = load_scopes(p)[0]
        assert e.enabled is False
        assert e.active_suffixes == ()

    def test_strict_lead_可指定(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 单位
    suffixes: ["公司"]
    strict_lead: true
""")
        assert load_scopes(p)[0].strict_lead is True

    def test_行号记录(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 单位
    suffixes: ["公司"]
  - name: 院校
    suffixes: ["大学"]
""")
        entries = load_scopes(p)
        assert entries[0].line == 3, entries[0].line
        assert entries[1].line == 5, entries[1].line

    def test_多条目保持YAML顺序(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 甲组
    suffixes: ["公司"]
  - name: 乙组
    suffixes: ["大学"]
  - name: 丙组
    suffixes: ["项目"]
""")
        assert [e.name for e in load_scopes(p)] == ["甲组", "乙组", "丙组"]

    def test_空列表合法_返回空(self, tmp_path: Path):
        p = _write(tmp_path, "scopes: []\n")
        assert load_scopes(p) == []


class TestLoadScopesErrors:
    """绝不静默跳过 —— 任何问题都必须抛 RuleError。"""

    def test_文件不存在(self, tmp_path: Path):
        with pytest.raises(RuleError):
            load_scopes(tmp_path / "nope.yaml")

    def test_缺顶层键(self, tmp_path: Path):
        p = _write(tmp_path, "lexicon: []\n")
        with pytest.raises(RuleError, match="scopes"):
            load_scopes(p)

    def test_未知顶层键(self, tmp_path: Path):
        p = _write(tmp_path, "scopes: []\nextra: 1\n")
        with pytest.raises(RuleError, match="未知顶层键"):
            load_scopes(p)

    def test_条目不是映射(self, tmp_path: Path):
        p = _write(tmp_path, "scopes:\n  - 只是一个字符串\n")
        with pytest.raises(RuleError, match="键值映射"):
            load_scopes(p)

    def test_缺name(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - suffixes: ["公司"]\n')
        with pytest.raises(RuleError):
            load_scopes(p)

    def test_name为空(self, tmp_path: Path):
        """空串 / 纯空白必须被拒 —— ``require_str`` 自己就会抛。"""
        p = _write(tmp_path, 'scopes:\n  - name: "  "\n    suffixes: ["公司"]\n')
        with pytest.raises(RuleError):
            load_scopes(p)
        p2 = _write(tmp_path, 'scopes:\n  - name: ""\n    suffixes: ["公司"]\n')
        with pytest.raises(RuleError):
            load_scopes(p2)

    def test_name与内置规则重名(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 手机号\n    suffixes: ["公司"]\n')
        with pytest.raises(RuleError, match="重名"):
            load_scopes(p, reserved_names=["手机号"])

    def test_缺suffixes(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n')
        with pytest.raises(RuleError, match="suffixes"):
            load_scopes(p)

    def test_suffixes为空列表(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: []\n')
        with pytest.raises(RuleError, match="非空列表"):
            load_scopes(p)

    def test_suffixes不是列表(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: "公司"\n')
        with pytest.raises(RuleError, match="非空列表"):
            load_scopes(p)

    def test_后缀非字符串(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: [123]\n')
        with pytest.raises(RuleError, match="非空字符串"):
            load_scopes(p)

    def test_后缀重复(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: ["公司", "公司"]\n')
        with pytest.raises(RuleError, match="重复"):
            load_scopes(p)

    def test_后缀含首字黑名单字符(self, tmp_path: Path):
        """后缀本身含"这那每某该此各本"永远匹配不到，白占坑位 → 报错。"""
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: ["这家"]\n')
        with pytest.raises(RuleError, match="首字黑名单"):
            load_scopes(p)

    def test_未知字段(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: ["公司"]\n    xxx: 1\n')
        with pytest.raises(RuleError, match="未知字段"):
            load_scopes(p)

    def test_tier非法值(self, tmp_path: Path):
        p = _write(tmp_path, 'scopes:\n  - name: 单位\n    suffixes: ["公司"]\n    tier: crazy\n')
        with pytest.raises(RuleError, match="tier"):
            load_scopes(p)

    def test_条目名重复(self, tmp_path: Path):
        p = _write(tmp_path, """
scopes:
  - name: 单位
    suffixes: ["公司"]
  - name: 单位
    suffixes: ["大学"]
""")
        with pytest.raises(RuleError, match="重复"):
            load_scopes(p)

    def test_scopes不是列表(self, tmp_path: Path):
        p = _write(tmp_path, "scopes: notalist\n")
        with pytest.raises(RuleError, match="必须是列表"):
            load_scopes(p)

    def test_YAML语法错(self, tmp_path: Path):
        p = _write(tmp_path, "scopes:\n  - name: [unclosed\n")
        with pytest.raises(RuleError):
            load_scopes(p)


class TestExpandSuffixes:
    def test_按后缀长度降序(self):
        """★"有限公司"必须先于"公司"，否则"重庆某某有限公司"会被切在"公司"处。"""
        entries = [ScopeEntry(name="单位", suffixes=("公司", "有限公司", "集团"))]
        out = expand_suffixes(entries)
        got = [s for s, _, _ in out]
        # 排序键 = (-长度, 字符串)：长度降序，同长按字典序（公 < 集）
        assert got == ["有限公司", "公司", "集团"]
        # ★ 关键契约：长后缀必须排在它的短子串之前
        assert got.index("有限公司") < got.index("公司")

    def test_conservative档只出HIGH(self):
        entries = [ScopeEntry(
            name="单位", suffixes=HIGH_SUFFIXES + LOW_SUFFIXES
        )]
        out = expand_suffixes(entries, tier="conservative")
        got = {s for s, _, _ in out}
        assert set(HIGH_SUFFIXES) <= got
        assert not (got & set(LOW_SUFFIXES)), f"LOW 后缀不该出现：{got & set(LOW_SUFFIXES)}"

    def test_balanced档出全部(self):
        entries = [ScopeEntry(name="单位", suffixes=HIGH_SUFFIXES + LOW_SUFFIXES)]
        out = expand_suffixes(entries, tier="balanced")
        got = {s for s, _, _ in out}
        assert set(HIGH_SUFFIXES) <= got
        assert set(LOW_SUFFIXES) <= got

    def test_禁用条目被跳过(self):
        entries = [ScopeEntry(name="单位", suffixes=("公司",), enabled=False)]
        assert expand_suffixes(entries) == ()

    def test_后缀跨条目去重(self):
        entries = [
            ScopeEntry(name="甲组", suffixes=("公司",)),
            ScopeEntry(name="乙组", suffixes=("公司", "大学")),
        ]
        out = expand_suffixes(entries)
        sufs = [s for s, _, _ in out]
        assert sufs.count("公司") == 1
        assert "大学" in sufs

    def test_未知档位报错(self):
        entries = [ScopeEntry(name="单位", suffixes=("公司",))]
        with pytest.raises(RuleError, match="未知严格度"):
            expand_suffixes(entries, tier="nope")

    def test_占位符随三元组带出(self):
        entries = [ScopeEntry(name="单位", suffixes=("公司", "大学"))]
        out = expand_suffixes(entries)
        ph = {s: p for s, p, _ in out}
        assert ph["公司"] == "[公司]"
        assert ph["大学"] == "[大学]"


class TestTierConstants:
    def test_三档齐备(self):
        assert set(TIERS) == {"conservative", "balanced", "aggressive"}
        assert set(TIER_LABELS) == set(TIERS)

    def test_conservative是balanced的子集(self):
        assert set(TIER_SUFFIXES["conservative"]) <= set(TIER_SUFFIXES["balanced"])

    def test_HIGH_LOW不相交(self):
        assert not (set(HIGH_SUFFIXES) & set(LOW_SUFFIXES))

    def test_默认档是balanced(self):
        assert DEFAULT_TIER == "balanced"


class TestDefaultPath:
    def test_不存在返回None(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_scopes_path() is None

    def test_存在返回路径(self, tmp_path: Path, monkeypatch):
        d = tmp_path / ".redactor"
        d.mkdir()
        f = d / "scopes.yaml"
        f.write_text("scopes: []\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_scopes_path() == f