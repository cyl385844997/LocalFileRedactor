"""身份证规则优先级加固（v0.7.1）。

**背景**：18 位号码既可能是身份证、也可能是 18 位银行卡。
两条 pattern 长度相同（都是 18），``resolve_overlaps`` 的
"区间更长者优先"完全打平，胜负只由 ``priority`` 决定 ——
而 ``priority`` 来自 ``rules/builtin.yaml`` 里的**下标**。
这意味着"谁写在 YAML 前面谁赢"，任何一次顺序调整都会静默退化。

**实测样例**：``110000000000000007`` 同时通过 ``id_card`` 与 ``luhn``
两种校验（已用GB 11643 加权算法验证）。它既能是身份证，也能是银行卡，
正是这个缺陷的判别样例。

守护两件事：
    1. 当前顺序下双重命中必须归给"身份证号"
    2. ★即使把 YAML 顺序调反（把银行卡挪到身份证前面），
       结果**依然**归给"身份证号" —— 靠的是代码里的显式覆盖，
       不是 YAML 的偶然顺序
"""
from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.models import Rule
from redactor.core.rules import load_rule_dir

#: 同时通过 id_card 与 luhn 的 18 位号码（既是身份证也是银行卡）
#: 用 GB 11643 加权算法生成，末尾 7 是校验位。
DOUBLE_HIT = "110000000000000007"

#: 只通过 id_card 的合法身份证（末尾 7 同样由算法生成）
ID_ONLY = "110101199001011237"


@pytest.fixture(scope="module")
def builtin_rules():
    return load_rule_dir(Path(__file__).resolve().parent.parent / "rules")


class TestIdCardPriority:
    def test_双重命中归身份证(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        names = {m.rule_name for m in eng.scan(f"身份证{DOUBLE_HIT}")}
        assert "身份证号" in names, f"双重命中应归身份证，实际 {names}"

    def test_双重命中脱敏为身份证占位符(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        out, _ = eng.redact(f"身份证{DOUBLE_HIT}")
        assert out == "身份证[身份证号]", out
        assert DOUBLE_HIT not in out

    def test_普通身份证正常命中(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        out, _ = eng.redact(f"身份证{ID_ONLY}")
        assert out == "身份证[身份证号]", out

    def test_优先级表里身份证明小于银行卡(self, builtin_rules):
        """identity 必须排在 bank 前面（数字越小越优先）。"""
        eng = RedactionEngine(builtin_rules)
        assert eng._priority["身份证号"] < eng._priority["银行卡号"], \
            "身份证明细于银行卡，priority 必须更小"

    def test_标记YAML顺序调反_结果依然正确(self, builtin_rules):
        """★核心加固：把银行卡挪到身份证**前面**，结果必须不变。

        没有这条覆盖时，交换 YAML 顺序就会让身份证被静默吞掉 ——
        而没有任何报错。这是本测试存在的唯一理由。
        """
        order = {r.name: r for r in builtin_rules}
        swapped = [
            order["手机号"],
            order["银行卡号"],      # ★故意挪到身份证前面
            order["身份证号"],
            order["邮箱"],
        ]
        eng = RedactionEngine(swapped)
        out, matches = eng.redact(f"身份证{DOUBLE_HIT}")
        print(f"  swapped YAML order -> out={out!r}")
        print(f"  rule_names={[m.rule_name for m in matches]}")
        assert out == "身份证[身份证号]", (
            f"YAML 顺序调反后身份证被吞了：{out}。"
            f"说明 _apply_specificity_overrides 没生效。"
        )

    def test_标记只含两条规则时也生效(self, builtin_rules):
        """裁剪到只剩身份证 + 银行卡（CLI --only 场景）也要正确。"""
        order = {r.name: r for r in builtin_rules}
        eng = RedactionEngine([order["银行卡号"], order["身份证号"]])
        out, _ = eng.redact(f"身份证{DOUBLE_HIT}")
        assert out == "身份证[身份证号]", out

    def test_无银行卡规则时不受影响(self, builtin_rules):
        """只有身份证、没有银行卡时，覆盖逻辑不应报错。"""
        id_rule = next(r for r in builtin_rules if r.name == "身份证号")
        eng = RedactionEngine([id_rule])
        out, _ = eng.redact(f"身份证{ID_ONLY}")
        assert out == "身份证[身份证号]", out


class TestIdCardValidatorNotBypassed:
    """反向确认：修的是优先级，不是放松校验。

    校验位错误的 18 位号码**仍然不该**被判为身份证 ——
    否则就是把"吞掉身份证"换成了"误报身份证"，问题更糟。
    """

    def test_校验位错误不被判为身份证(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        bad = "110101199001011230"      # 末位改成 0，校验不过
        names = {m.rule_name for m in eng.scan(f"身份证{bad}")}
        assert "身份证号" not in names, \
            f"校验位错误的号码不该判为身份证：{names}"

    def test_非18位数字不被判为身份证(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        names = {m.rule_name for m in eng.scan("编号 123456789012345")}
        assert "身份证号" not in names, names


class TestReverseValidation:
    def test_标记去掉覆盖_测试必须红(self, builtin_rules):
        """变异：把 ``_apply_specificity_overrides`` 变成空操作。

        这模拟"YAML 顺序被调反且没有代码兜底"的最坏情况，
        守护测试必须红 —— 否则说明加固形同虚设。
        """
        eng = RedactionEngine(builtin_rules)
        # 先证实真版会做覆盖：把顺序人为调反，覆盖方法应交换回来
        eng._priority["银行卡号"] = -1          # 人为让银行卡在前
        eng._apply_specificity_overrides()
        assert eng._priority["身份证号"] < eng._priority["银行卡号"], \
            "覆盖方法本身没起作用"

        # ★变异：直接模拟"没有覆盖 + YAML 顺序被调反"的最坏情况。
        #   （不能用 noop 替换方法 —— 覆盖方法会**交换**两个值，
        #     调反后它又交换回来，等于没变异。已在实现期踩过。）
        eng._priority["身份证号"] = 5     # 身份证被挪到很后面
        eng._priority["银行卡号"] = -1

        after = eng._priority
        assert after["身份证号"] > after["银行卡号"], \
            "变异无效：顺序没有真正调反"

        # 变异版的真实后果
        out, _ = eng.redact(f"身份证{DOUBLE_HIT}")
        print(f"  变异版输出: {out!r}")
        assert out != "身份证[身份证号]", "变异后竟然仍正确，说明用例失效"
        assert "身份证[银行卡号]" in out or "[银行卡号]" in out, \
            f"变异后应被银行卡吞掉，实际 {out}"