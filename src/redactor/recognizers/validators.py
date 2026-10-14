"""校验函数：用于在正则匹配后做二次校验，显著降低误报。

纯正则的固有弱点：18 位数字既可能是身份证，也可能是 18 位设备编号。
加入校验位验证后，只有"格式合法且校验位正确"的才会被判为身份证。

本模块零第三方依赖，可单测。
"""

from __future__ import annotations

import re

# 身份证 17 位数字的加权因子（GB 11643-1999）
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
# 加权求和对 11 取模所得的校验码字符表，索引即余数
_ID_CHECK_CHARS = "10X98765432"

# 纯数字串（用于 Luhn 前的快速筛选）
_DIGITS_ONLY = re.compile(r"^\d+$")


def is_valid_id_card(value: str) -> bool:
    """校验 18 位居民身份证号的校验位。

    Args:
        value: 待校验字符串，允许末位为 ``X``/``x``。

    Returns:
        校验位正确返回 True。
    """
    if len(value) != 18:
        return False
    body, check = value[:17], value[17].upper()
    if not _DIGITS_ONLY.match(body):
        return False
    if not (check.isdigit() or check == "X"):
        return False

    total = sum(int(ch) * w for ch, w in zip(body, _ID_WEIGHTS))
    return _ID_CHECK_CHARS[total % 11] == check


def is_valid_luhn(value: str) -> bool:
    """校验银行卡号的 Luhn 校验位。

    Args:
        value: 待校验字符串，必须为纯数字。

    Returns:
        校验通过返回 True。长度不在 13-19 之间直接判否（覆盖主流卡号长度）。
    """
    if not _DIGITS_ONLY.match(value):
        return False
    if not (13 <= len(value) <= 19):
        return False

    total = 0
    # 从右往左，奇数位（倒数第 1、3、5…）不翻倍
    for index, ch in enumerate(reversed(value)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# 名称 -> 函数 的注册表，供 core.rules 做白名单校验
VALIDATORS = {
    "id_card": is_valid_id_card,
    "luhn": is_valid_luhn,
}