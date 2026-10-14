"""校验函数单元测试（身份证校验位 + Luhn）。"""

from __future__ import annotations

import pytest

from fixtures.make_samples import (
    FAKE_BANK_CARD,
    FAKE_BANK_CARD_BAD,
    FAKE_ID_INVALID,
    FAKE_ID_VALID,
    _ref_id_check_char,
    _ref_luhn_check_digit,
)
from redactor.recognizers.validators import is_valid_id_card, is_valid_luhn


class TestIdCard:
    """身份证校验位。"""

    def test_样例身份证应通过(self):
        """用参考实现现算的号码，校验位必须正确。"""
        assert is_valid_id_card(FAKE_ID_VALID) is True

    def test_篡改校验位应被拒(self):
        assert is_valid_id_card(FAKE_ID_INVALID) is False

    def test_末位小写x也接受(self):
        """身份证末位 X 常被手工输入为小写 x。"""
        assert is_valid_id_card(FAKE_ID_VALID[:-1] + FAKE_ID_VALID[-1].lower()) is True

    @pytest.mark.parametrize("value", ["", "123", "1101011990030755", "110101199003075511234"])
    def test_长度不符直接拒(self, value):
        assert is_valid_id_card(value) is False

    @pytest.mark.parametrize("value", ["1101011990030755A", "X1010119900307551", "11010119900307 51"])
    def test_含非法字符直接拒(self, value):
        assert is_valid_id_card(value) is False

    def test_参考实现与被测实现一致(self):
        """参考实现（算校验码）与被测实现（验校验码）必须在多个号码上一致。

        这是交叉校验：若两套实现出现分歧，测试立刻失败，避免"自证自洽"。
        """
        for body in ("11010119900307551", "31011519851210123", "44030119920215111"):
            assert is_valid_id_card(body + _ref_id_check_char(body)) is True


class TestLuhn:
    """银行卡 Luhn 校验。"""

    def test_样例卡号应通过(self):
        assert is_valid_luhn(FAKE_BANK_CARD) is True

    def test_篡改末位应被拒(self):
        assert is_valid_luhn(FAKE_BANK_CARD_BAD) is False

    def test_知名测试卡号(self):
        """公开的测试卡号，Luhn 校验通过。"""
        assert is_valid_luhn("4539578763621486") is True
        assert is_valid_luhn("4111111111111111") is True

    @pytest.mark.parametrize("value", ["", "4111111111111", "41111111111111111111"])
    def test_长度不符直接拒(self, value):
        assert is_valid_luhn(value) is False

    def test_含非数字直接拒(self):
        assert is_valid_luhn("4111-1111-1111-1111") is False

    def test_参考实现与被测实现一致(self):
        for body in ("622202123456789012", "411111111111111", "453957876362148"):
            assert is_valid_luhn(body + _ref_luhn_check_digit(body)) is True