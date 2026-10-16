"""引擎与替换器单元测试：识别正确性、误报过滤、重叠消解、编号策略。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.make_samples import (
    FAKE_BANK_CARD,
    FAKE_BANK_CARD_BAD,
    FAKE_EMAIL,
    FAKE_ID_INVALID,
    FAKE_ID_VALID,
    FAKE_IP,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


@pytest.fixture()
def engine():
    """用内置规则构造引擎。"""
    return RedactionEngine(load_rules(BUILTIN_RULES))


@pytest.fixture()
def numbered_engine():
    """强制编号模式的引擎。"""
    return RedactionEngine(load_rules(BUILTIN_RULES), force_numbered=True)


class TestRecognition:
    """六类敏感信息均能识别。"""

    @pytest.mark.parametrize(
        "text, rule_name",
        [
            (FAKE_PHONE_1, "手机号"),
            (FAKE_ID_VALID, "身份证号"),
            (FAKE_BANK_CARD, "银行卡号"),
            (FAKE_EMAIL, "邮箱"),
            (FAKE_IP, "IP地址"),
        ],
    )
    def test_各类均可命中(self, engine, text, rule_name):
        found = engine.scan(text)
        assert [m.rule_name for m in found] == [rule_name]
        assert found[0].raw == text

    def test_一段文本多处命中(self, engine):
        text = f"手机 {FAKE_PHONE_1} 和 {FAKE_PHONE_2}，邮箱 {FAKE_EMAIL}"
        found = engine.scan(text)
        assert [m.rule_name for m in found] == ["手机号", "手机号", "邮箱"]

    def test_无敏感信息时空结果(self, engine):
        assert engine.scan("今天天气不错，适合出门。") == []


class TestValidatorReducesFalsePositives:
    """校验位必须真的拦下错误号码。"""

    def test_错误校验位身份证不被识别(self, engine):
        assert engine.scan(f"证件号 {FAKE_ID_INVALID}") == []

    def test_错误Luhn卡号不被识别(self, engine):
        assert engine.scan(f"卡号 {FAKE_BANK_CARD_BAD}") == []

    def test_合法号码仍然被识别(self, engine):
        assert len(engine.scan(f"证件 {FAKE_ID_VALID} 卡号 {FAKE_BANK_CARD}")) == 2

    def test_普通长数字不被当身份证(self, engine):
        """18 位设备编号（校验位随机）不应误判为身份证。"""
        assert engine.scan("设备编号 123456789012345678") == []


class TestReplacement:
    """替换行为。"""

    def test_占位符统一替换(self, engine):
        text = f"手机 {FAKE_PHONE_1}"
        out, matches = engine.redact(text)
        assert out == "手机 [手机号]"
        assert len(matches) == 1

    def test_无命中时原文不变(self, engine):
        text = "无敏感信息的一段话"
        out, matches = engine.redact(text)
        assert out == text
        assert matches == []

    def test_原文彻底消失(self, engine):
        text = f"联系 {FAKE_PHONE_1}，邮箱 {FAKE_EMAIL}"
        out, _ = engine.redact(text)
        assert FAKE_PHONE_1 not in out
        assert FAKE_EMAIL not in out

    def test_多行文本偏移量正确(self, engine):
        """第二行的命中偏移必须基于全文，而不是该行内的局部偏移。"""
        text = f"第一行\n第二行 {FAKE_PHONE_1}\n第三行"
        out, matches = engine.redact(text)
        assert out == "第一行\n第二行 [手机号]\n第三行"
        assert text[matches[0].start : matches[0].end] == FAKE_PHONE_1


class TestNumberedMode:
    """编号策略。"""

    def test_默认不编号(self, engine):
        text = f"{FAKE_PHONE_1} 与 {FAKE_PHONE_2}"
        out, _ = engine.redact(text)
        assert out == "[手机号] 与 [手机号]"

    def test_开启编号后不同值不同号(self, numbered_engine):
        text = f"{FAKE_PHONE_1} 与 {FAKE_PHONE_2}"
        out, _ = numbered_engine.redact(text)
        assert out == "[手机号#1] 与 [手机号#2]"

    def test_同一原文复用同一编号(self, numbered_engine):
        """同一手机号出现两次必须都是 #1 —— 这是编号模式的核心语义。"""
        text = f"{FAKE_PHONE_1} 再次出现 {FAKE_PHONE_1}"
        out, _ = numbered_engine.redact(text)
        assert out == "[手机号#1] 再次出现 [手机号#1]"

    def test_编号按规则分别计数(self, numbered_engine):
        text = f"{FAKE_PHONE_1} {FAKE_EMAIL} {FAKE_PHONE_2}"
        out, _ = numbered_engine.redact(text)
        assert out == "[手机号#1] [邮箱#1] [手机号#2]"

    def test_yaml中numbered为true才编号(self, tmp_path):
        """只有显式配置 numbered: true 的规则才编号，不能全局泄漏编号。"""
        path = tmp_path / "r.yaml"
        path.write_text(
            "rules:\n"
            "  - name: 甲\n    pattern: 'a'\n    numbered: true\n"
            "  - name: 乙\n    pattern: 'b'\n",
            encoding="utf-8",
        )
        engine = RedactionEngine(load_rules(path))
        out, _ = engine.redact("a b a")
        assert out == "[甲#1] [乙] [甲#1]"


class TestOverlapResolution:
    """重叠命中的消解规则。"""

    def test_更长区间优先(self):
        """长规则应吃掉被它完全覆盖的短规则命中。"""
        from redactor.core.models import Rule
        from redactor.redactors.replacer import resolve_overlaps

        long_rule = Rule(name="长", pattern="x", placeholder="[长]")
        short_rule = Rule(name="短", pattern="x", placeholder="[短]")
        matches = [
            type("M", (), {"rule_name": "短", "start": 0, "end": 5})(),
            type("M", (), {"rule_name": "长", "start": 0, "end": 10})(),
        ]
        kept = resolve_overlaps(matches, {"长": 0, "短": 1})
        assert [m.rule_name for m in kept] == ["长"]

    def test_同长度时按规则顺序优先(self):
        from redactor.core.models import Match
        from redactor.redactors.replacer import resolve_overlaps

        matches = [
            Match(rule_name="乙", placeholder="[乙]", start=0, end=4, raw="abcd"),
            Match(rule_name="甲", placeholder="[甲]", start=0, end=4, raw="abcd"),
        ]
        kept = resolve_overlaps(matches, {"甲": 0, "乙": 1})
        assert [m.rule_name for m in kept] == ["甲"]

    def test_不重叠的命中都保留(self):
        from redactor.core.models import Match
        from redactor.redactors.replacer import resolve_overlaps

        matches = [
            Match(rule_name="甲", placeholder="[甲]", start=0, end=3, raw="abc"),
            Match(rule_name="乙", placeholder="[乙]", start=5, end=8, raw="xyz"),
        ]
        assert len(resolve_overlaps(matches, {"甲": 0, "乙": 1})) == 2

    def test_身份证与银行卡重叠时身份证优先(self, engine):
        """18 位身份证也是 18 位数字，银行卡规则也会匹配；应保留身份证。"""
        found = engine.scan(f"证件 {FAKE_ID_VALID}")
        assert [m.rule_name for m in found] == ["身份证号"]


class TestEngineGuards:
    """引擎自身的安全护栏。"""

    def test_规则为空拒绝启动(self):
        with pytest.raises(ValueError, match="规则列表为空"):
            RedactionEngine([])

    def test_aggregate不泄漏原文(self, engine):
        """报告统计必须零敏感内容（补充约束 2）。

        ``locations`` 必须为**每一个**命中提供标签：``aggregate`` 的
        ``strict`` 守卫会在取不到标签时抛错（防"键不一致 -> 静默少报"）。
        本用例两个命中（手机号 + 邮箱）故给两个标签。
        """
        text = f"手机 {FAKE_PHONE_1} 邮箱 {FAKE_EMAIL}"
        _, matches = engine.redact(text)
        locations = {
            (m.rule_name, m.start): "第 1 行" for m in matches
        }
        stats = engine.aggregate(matches, locations)

        blob = str(stats)
        assert FAKE_PHONE_1 not in blob
        assert FAKE_EMAIL not in blob
        assert "手机号" in blob and "第 1 行" in blob