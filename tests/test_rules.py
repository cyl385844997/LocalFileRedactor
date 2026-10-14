"""规则加载与严格校验的单元测试。

重点覆盖需求补充约束 C：规则加载失败必须报错并指出行号，绝不静默跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.rules import load_rule_dir, load_rules, load_rules_from_text
from redactor.core.models import RuleError

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


def write_rules(tmp_path: Path, text: str, name: str = "r.yaml") -> Path:
    """把 YAML 文本写到临时文件并返回路径。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestBuiltinRules:
    """内置规则文件必须始终可用。"""

    def test_内置规则可加载(self):
        rules = load_rules(BUILTIN_RULES)
        assert len(rules) >= 5

    def test_内置规则包含六类敏感信息(self):
        names = {r.name for r in load_rules(BUILTIN_RULES)}
        for expected in ("手机号", "身份证号", "银行卡号", "邮箱", "IP地址"):
            assert expected in names, f"内置规则缺少 {expected}"

    def test_禁用规则不返回但仍校验通过(self):
        """IPv6 默认 enabled: false，应被过滤掉，但文件本身必须能加载。"""
        names = {r.name for r in load_rules(BUILTIN_RULES)}
        assert "IPv6地址" not in names

    def test_默认占位符格式正确(self):
        for rule in load_rules(BUILTIN_RULES):
            assert rule.placeholder == f"[{rule.name}]"


class TestStrictValidation:
    """错误必须显式抛出，且带行号。"""

    def test_yaml语法错误报行号(self, tmp_path):
        path = write_rules(tmp_path, "version: 1\nrules:\n  - name: A\n     pattern: 'x'\n")
        with pytest.raises(RuleError) as excinfo:
            load_rules(path)
        message = str(excinfo.value)
        assert "YAML 语法错误" in message
        assert "第" in message and "行" in message

    def test_正则编译失败报行号与规则名(self, tmp_path):
        text = (
            "version: 1\n"
            "rules:\n"
            "  - name: 手机号\n"
            "    pattern: '(?<\\d)1[3-9]'\n"  # 非法分组，编译必失败
        )
        path = write_rules(tmp_path, text)
        with pytest.raises(RuleError) as excinfo:
            load_rules(path)
        message = str(excinfo.value)
        assert "正则编译失败" in message
        assert "手机号" in message
        assert "第 4 行" in message

    def test_缺少name报错(self, tmp_path):
        path = write_rules(tmp_path, "rules:\n  - pattern: 'x'\n")
        with pytest.raises(RuleError, match="name"):
            load_rules(path)

    def test_缺少pattern报错(self, tmp_path):
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n")
        with pytest.raises(RuleError, match="pattern"):
            load_rules(path)

    def test_未知字段报错防止拼写错误被忽略(self, tmp_path):
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'x'\n    enabeld: true\n")
        with pytest.raises(RuleError, match="未知字段"):
            load_rules(path)

    def test_重复字段报错(self, tmp_path):
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n    name: 乙\n    pattern: 'x'\n")
        with pytest.raises(RuleError, match="重复字段"):
            load_rules(path)

    def test_重复规则名报错(self, tmp_path):
        text = "rules:\n  - name: 甲\n    pattern: 'x'\n  - name: 甲\n    pattern: 'y'\n"
        path = write_rules(tmp_path, text)
        with pytest.raises(RuleError, match="重复"):
            load_rules(path)

    def test_非法validator报错(self, tmp_path):
        text = "rules:\n  - name: 甲\n    pattern: 'x'\n    validator: magic\n"
        path = write_rules(tmp_path, text)
        with pytest.raises(RuleError, match="validator"):
            load_rules(path)

    def test_非法flag报错(self, tmp_path):
        text = "rules:\n  - name: 甲\n    pattern: 'x'\n    flags: [NOT_A_FLAG]\n"
        path = write_rules(tmp_path, text)
        with pytest.raises(RuleError, match="非法标志"):
            load_rules(path)

    def test_empty字段报错(self, tmp_path):
        path = write_rules(tmp_path, "rules:\n  - name: ''\n    pattern: 'x'\n")
        with pytest.raises(RuleError, match="name"):
            load_rules(path)

    def test_规则列表为空报错(self, tmp_path):
        path = write_rules(tmp_path, "rules: []\n")
        with pytest.raises(RuleError, match="为空"):
            load_rules(path)

    def test_缺少rules顶层键报错(self, tmp_path):
        path = write_rules(tmp_path, "version: 1\n")
        with pytest.raises(RuleError, match="rules"):
            load_rules(path)

    def test_文件不存在报错(self, tmp_path):
        with pytest.raises(RuleError, match="不存在"):
            load_rules(tmp_path / "nope.yaml")

    def test_空文件报错(self, tmp_path):
        path = write_rules(tmp_path, "")
        with pytest.raises(RuleError):
            load_rules(path)


class TestRuleDir:
    """目录加载。"""

    def test_加载目录内全部文件(self, tmp_path):
        write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n", "a.yaml")
        write_rules(tmp_path, "rules:\n  - name: 乙\n    pattern: 'b'\n", "b.yml")
        rules = load_rule_dir(tmp_path)
        assert {r.name for r in rules} == {"甲", "乙"}

    def test_目录为空报错(self, tmp_path):
        with pytest.raises(RuleError, match="没有"):
            load_rule_dir(tmp_path)

    def test_目录不存在报错(self, tmp_path):
        with pytest.raises(RuleError, match="不存在"):
            load_rule_dir(tmp_path / "nope")

    def test_跨文件重名报错(self, tmp_path):
        write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n", "a.yaml")
        write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'b'\n", "b.yaml")
        with pytest.raises(RuleError, match="重复"):
            load_rule_dir(tmp_path)


class TestYamlTypeConversion:
    """回归测试：compose 不做类型转换，取值必须走 safe_load。

    这组用例针对一个曾导致 60 条测试失败的缺陷：
    ``yaml.compose()`` 的 ``ScalarNode.value`` 是**未转换的字符串**，
    ``enabled: false`` 拿到的是 ``'false'``，导致 ``isinstance(x, bool)`` 恒假。
    """

    def test_enabled_false被正确识别为布尔假(self, tmp_path):
        """曾经的核心 bug：'false' 字符串触发类型报错。"""
        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'a'\n    enabled: false\n",
        )
        rules = load_rules(path)
        assert rules == [], "enabled: false 的规则必须被过滤掉"

    def test_enabled_true被正确识别为布尔真(self, tmp_path):
        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'a'\n    enabled: true\n",
        )
        assert len(load_rules(path)) == 1

    def test_enabled缺省默认为启用(self, tmp_path):
        """缺省值语义：enabled 未写 = True（规则默认生效）。"""
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n")
        assert len(load_rules(path)) == 1

    def test_numbered缺省默认为不编号(self, tmp_path):
        """缺省值语义：numbered 未写 = False（不暴露同一性）。"""
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n")
        all_rules = load_rules_from_text(
            path.read_text(encoding="utf-8"), path=path
        )
        assert all_rules[0].numbered is False

    def test_enabled与numbered缺省语义相反(self, tmp_path):
        """两个布尔字段语义相反，必须各有各的缺省值。"""
        path = write_rules(tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n")
        rule = load_rules_from_text(path.read_text(encoding="utf-8"), path=path)[0]
        assert (rule.enabled, rule.numbered) == (True, False)

    def test_字符串false仍应报错(self, tmp_path):
        """引号包裹的 'false' 是字符串，不是布尔，必须报错而非被当假值。"""
        path = write_rules(
            tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n    enabled: 'false'\n"
        )
        with pytest.raises(RuleError, match="true/false"):
            load_rules(path)

    def test_flags列表被正确解析(self, tmp_path):
        """SequenceNode.value 是子节点列表，早期版本按 str 判断必然失败。"""
        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'a'\n    flags: [IGNORECASE]\n",
        )
        rule = load_rules_from_text(path.read_text(encoding="utf-8"), path=path)[0]
        assert rule.flags == ["IGNORECASE"]

    def test_flags多个值(self, tmp_path):
        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'a'\n    flags: [IGNORECASE, MULTILINE]\n",
        )
        rule = load_rules_from_text(path.read_text(encoding="utf-8"), path=path)[0]
        assert rule.flags == ["IGNORECASE", "MULTILINE"]

    def test_flags真正生效(self, tmp_path):
        """IGNORECASE 必须真的影响匹配，而不只是被解析出来。"""
        from redactor.core.engine import RedactionEngine

        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'abc'\n    placeholder: '[甲]'\n"
            "    flags: [IGNORECASE]\n",
        )
        engine = RedactionEngine(load_rules(path))
        out, _ = engine.redact("ABC abc AbC")
        assert out == "[甲] [甲] [甲]"

    def test_flags非法值报出元素行号(self, tmp_path):
        path = write_rules(
            tmp_path,
            "rules:\n  - name: 甲\n    pattern: 'a'\n    flags: [IGNORECASE, BOGUS]\n",
        )
        with pytest.raises(RuleError) as excinfo:
            load_rules(path)
        assert "BOGUS" in str(excinfo.value)
        assert "第 4 行" in str(excinfo.value)

    def test_version字段被容忍(self, tmp_path):
        """version 是合法的顶层元信息，不该被当成未知规则字段。"""
        path = write_rules(tmp_path, "version: 1\nrules:\n  - name: 甲\n    pattern: 'a'\n")
        assert len(load_rules(path)) == 1

    def test_重复字段报出两次行号(self, tmp_path):
        path = write_rules(
            tmp_path, "rules:\n  - name: 甲\n    pattern: 'a'\n    name: 乙\n"
        )
        with pytest.raises(RuleError) as excinfo:
            load_rules(path)
        message = str(excinfo.value)
        assert "重复字段" in message
        # name 首次出现在第 2 行（"- name: 甲"），重复出现在第 4 行（"    name: 乙"）
        assert "第 4 行" in message and "第 2 行" in message


class TestLoadFromText:
    """直接传文本的加载接口。"""

    def test_正常加载(self):
        rules = load_rules_from_text(
            "rules:\n  - name: 甲\n    pattern: 'a'\n    numbered: true\n", path=Path("虚拟.yaml")
        )
        assert rules[0].numbered is True

    def test_行号被记录(self):
        rules = load_rules_from_text(
            "rules:\n\n  - name: 甲\n    pattern: 'a'\n", path=Path("虚拟.yaml")
        )
        assert rules[0].line == 3

    def test_顶层不是映射报错(self):
        with pytest.raises(RuleError):
            load_rules_from_text("- a\n- b\n", path=Path("虚拟.yaml"))