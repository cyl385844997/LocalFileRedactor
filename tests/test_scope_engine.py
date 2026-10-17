"""P7 范围脱敏 —— 引擎级集成测试。

重点：**范围脱敏与内置规则共存时不能互相吞掉**（Q4 的端到端验证）。
这里跑的是真实的 :class:`RedactionEngine`（含真实内置规则），
不是孤立的识别器。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rule_dir
from redactor.core.scopes import HIGH_SUFFIXES, LOW_SUFFIXES, ScopeEntry

ALL_SUFFIXES = HIGH_SUFFIXES + LOW_SUFFIXES
PHONE = "13812345678"
ID_CARD = "110101199001011233"   # 校验位合法（已实测能过 id_card validator）


@pytest.fixture(scope="module")
def builtin_rules():
    """加载内置正则规则（与 CLI 同一份 rules/builtin.yaml）。"""
    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    return load_rule_dir(rules_dir)


def _engine(rules, *, scopes=None, tier=None, strict_lead=False):
    return RedactionEngine(
        rules,
        scope_entries=scopes if scopes is not None else [
            ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)
        ],
        scope_tier=tier,
        scope_strict_lead=strict_lead,
    )


class TestEngineQ4:
    """★Q4 端到端：范围脱敏绝不能吞掉内置手机号 / 身份证规则。"""

    def test_公司加手机号_两者分别正确脱敏(self, builtin_rules):
        eng = _engine(builtin_rules)
        text = f"联系人张三，电话{PHONE}，重庆创锐电气技术有限公司"
        out, matches = eng.redact(text)
        print(f"  in : {text}")
        print(f"  out: {out}")
        # 手机号必须被替换成手机号占位符
        assert PHONE not in out, f"手机号残留：{out}"
        assert "[手机号]" in out
        # 公司名必须被替换成公司占位符（按后缀）
        assert "重庆创锐电气技术有限公司" not in out
        assert "[有限公司]" in out
        # 两条规则都产出命中
        names = {m.rule_name for m in matches}
        assert "手机号" in names, names
        assert "有限公司" in names, names

    def test_公司加身份证_两者分别正确脱敏(self, builtin_rules):
        eng = _engine(builtin_rules)
        text = f"身份证{ID_CARD}，重庆大学的学生"
        out, _ = eng.redact(text)
        print(f"  out: {out}")
        assert ID_CARD not in out, f"身份证号残留：{out}"
        assert "重庆大学" not in out
        assert "[大学]" in out

    def test_公司紧跟手机号_不跨界(self, builtin_rules):
        """最危险的紧邻场景：'...公司13812345678'"""
        eng = _engine(builtin_rules)
        text = f"重庆水务集团{PHONE}"
        out, matches = eng.redact(text)
        print(f"  out: {out}")
        assert PHONE not in out
        # 公司命中的区间绝不能含数字
        for m in matches:
            if m.rule_name in ALL_SUFFIXES:
                assert not any(c.isdigit() for c in m.raw), \
                    f"范围命中含数字：{m.raw!r}"

    def test_编号后紧跟公司(self, builtin_rules):
        eng = _engine(builtin_rules)
        text = f"项目编号2024ABC，公司名称重庆某某公司"
        out, _ = eng.redact(text)
        print(f"  out: {out}")
        assert "重庆某某公司" not in out
        assert "[公司]" in out

    def test_范围命中与手机号命中区间不重叠(self, builtin_rules):
        eng = _engine(builtin_rules)
        text = f"电话{PHONE}，重庆某某有限公司"
        _out, matches = eng.redact(text)
        spans = [(m.start, m.end, m.rule_name) for m in matches]
        print(f"  spans: {spans}")
        for i in range(len(spans) - 1):
            assert spans[i][1] <= spans[i + 1][0], f"区间重叠：{spans}"


class TestEngineTiers:
    def test_保守档不脱敏公司大学(self, builtin_rules):
        eng = _engine(builtin_rules, tier="conservative")
        out, _ = eng.redact("重庆某某科技公司与重庆大学")
        print(f"  保守档 out: {out}")
        assert "重庆某某科技公司" in out, "保守档不该脱敏'公司'后缀实体"
        assert "重庆大学" in out

    def test_平衡档脱敏公司大学(self, builtin_rules):
        eng = _engine(builtin_rules, tier="balanced")
        out, _ = eng.redact("重庆某某科技公司与重庆大学")
        print(f"  平衡档 out: {out}")
        assert "[公司]" in out
        assert "[大学]" in out

    def test_保守档仍脱敏集团(self, builtin_rules):
        eng = _engine(builtin_rules, tier="conservative")
        out, _ = eng.redact("本项目由重庆水务集团实施")
        print(f"  保守档 out: {out}")
        assert "[集团]" in out

    def test_激进档拦下这家公司(self, builtin_rules):
        eng_strict = _engine(builtin_rules, strict_lead=True)
        out_s, _ = eng_strict.redact("这家公司实力不错")
        eng_loose = _engine(builtin_rules, strict_lead=False)
        out_l, _ = eng_loose.redact("这家公司实力不错")
        print(f"  激进: {out_s}")
        print(f"  平衡: {out_l}")
        assert "[公司]" in out_l, "平衡档应命中（已知歧义）"
        assert "[公司]" not in out_s, "激进档应拦下"


class TestEnginePriority:
    def test_词库优先于范围(self, builtin_rules):
        """★裁定 6：词库 > 范围脱敏 > 内置正则。"""
        from redactor.core.lexicon import LexiconEntry
        entity = "重庆创锐电气技术有限公司"
        eng = RedactionEngine(
            builtin_rules,
            lexicon_entries=[
                LexiconEntry(name="甲方单位", words=(entity,), normalized=(entity,))
            ],
            scope_entries=[ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)],
        )
        out, matches = eng.redact(f"甲方为{entity}")
        print(f"  out: {out}")
        names = {m.rule_name for m in matches}
        print(f"  rule_names: {names}")
        # 词库条目名优先（而非"有限公司"）
        assert "甲方单位" in names, names
        assert "有限公司" not in names, f"范围规则不该压过词库：{names}"
        assert "[甲方单位]" in out


class TestEngineNoScope:
    def test_无范围配置时行为不变(self, builtin_rules):
        """不传 scope_entries 时，引擎行为与引入 P7 前完全一致。"""
        eng = RedactionEngine(builtin_rules)
        out, _ = eng.redact("重庆创锐电气技术有限公司，电话13812345678")
        print(f"  out: {out}")
        assert "重庆创锐电气技术有限公司" in out, "未启用范围脱敏时不该动公司名"
        assert "[手机号]" in out

    def test_空范围条目不造识别器(self, builtin_rules):
        eng = RedactionEngine(builtin_rules, scope_entries=[])
        assert len(eng.recognizers) == len(builtin_rules)