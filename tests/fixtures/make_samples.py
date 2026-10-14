"""合成测试样例生成器。

全部数据为**虚构**，不含任何真实个人信息。
生成的样例仅用于本地验证，会写入 samples/（已在 .gitignore 中忽略，不入库）。

用法：
    python tests/fixtures/make_samples.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

# 项目根：tests/fixtures -> 父级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = PROJECT_ROOT / "samples"

# ---------------------------------------------------------------------------
# 基准数据生成：用一份**独立于 src 的参考实现**现算校验位。
#
# 为什么不硬编码校验位：如果把 "11010119900307551X" 直接写死，一旦这个数字的
# 校验位其实是错的，样例就会变成"永远不该被识别"，而测试会通过——
# 变成一个假绿基准。这里现算，保证样例的"应当命中/不应命中"是数学确定的。
# ---------------------------------------------------------------------------

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _ref_id_check_char(first17: str) -> str:
    """参考实现：计算身份证前 17 位的校验码字符。"""
    total = sum(int(ch) * w for ch, w in zip(first17, _ID_WEIGHTS))
    return _ID_CHECK_CHARS[total % 11]


def _ref_luhn_check_digit(first19: str) -> str:
    """参考实现：计算银行卡号末位 Luhn 校验位。"""
    total = 0
    for index, ch in enumerate(reversed(first19)):
        digit = int(ch)
        if index % 2 == 0:  # 末位是第 0 位，翻倍
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


#: 身份证：前 17 位固定（虚构的重庆渝中区码 + 1990-03-07 序列号 51X），校验码现算
_ID_BODY17 = "50010319900307551"
FAKE_ID_VALID = _ID_BODY17 + _ref_id_check_char(_ID_BODY17)
#: 把校验位改成另一个值 -> 格式对但校验错，应被校验位逻辑拦下
FAKE_ID_INVALID = _ID_BODY17 + ("0" if FAKE_ID_VALID[-1] != "0" else "1")

#: 银行卡：前 19 位固定（虚构），校验位现算
_CARD_BODY19 = "622202123456789012"
FAKE_BANK_CARD = _CARD_BODY19 + _ref_luhn_check_digit(_CARD_BODY19)
#: 篡改末位 -> Luhn 不通过，应被拦下
FAKE_BANK_CARD_BAD = _CARD_BODY19 + ("7" if FAKE_BANK_CARD[-1] != "7" else "3")

FAKE_PHONE_1 = "13800138000"
FAKE_PHONE_2 = "13912345678"
FAKE_EMAIL = "zhangsan@example.com"
FAKE_IP = "192.168.10.254"


def make_txt_sample() -> Path:
    """生成纯文本样例，覆盖 6 类敏感信息 + 干扰项。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "demo.txt"
    content = f"""项目联系人清单（合成测试数据）
================================
姓名：張三（虚构）
手机：{FAKE_PHONE_1}
备用手机：{FAKE_PHONE_2}
身份证：{FAKE_ID_VALID}
错误校验位身份证（不应被识别）：{FAKE_ID_INVALID}
银行卡：{FAKE_BANK_CARD}
错误校验位卡号（不应被识别）：{FAKE_BANK_CARD_BAD}
邮箱：{FAKE_EMAIL}
服务器 IP：{FAKE_IP}
非法 IP（不应被识别）：999.1.1.1

备注：本文件仅用于本地自动化测试，全部数据为虚构。
"""
    path.write_text(content, encoding="utf-8")
    return path


def make_gbk_txt_sample() -> Path:
    """生成 GBK 编码样例，用于验证编码自动识别。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "demo_gbk.txt"
    content = f"中文编码测试：手机号 {FAKE_PHONE_1}，邮箱 {FAKE_EMAIL}。\n"
    path.write_bytes(content.encode("gb18030"))
    return path


def make_edge_case_samples() -> list[Path]:
    """生成边界与异常样例。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    # 空文件
    empty = SAMPLES_DIR / "empty.txt"
    empty.write_text("", encoding="utf-8")
    created.append(empty)

    # 二进制伪装成 txt
    binary = SAMPLES_DIR / "fake.txt"
    binary.write_bytes(b"\x00\x01\x02binary")
    created.append(binary)

    # 无命中
    clean = SAMPLES_DIR / "clean.txt"
    clean.write_text("这一段文字里没有任何敏感信息。\n", encoding="utf-8")
    created.append(clean)

    return created


def make_unsupported_sample() -> Path:
    """生成当前阶段不支持的格式（.doc），用于验证友好提示。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0legacy-doc-placeholder")
    return path


def build_all() -> list[Path]:
    """生成全部样例，返回路径列表。"""
    paths = [make_txt_sample(), make_gbk_txt_sample(), *make_edge_case_samples(), make_unsupported_sample()]
    return paths


def clean() -> None:
    """清空 samples 目录（测试前调用，避免脏数据影响结果）。"""
    if SAMPLES_DIR.exists():
        shutil.rmtree(SAMPLES_DIR)


if __name__ == "__main__":
    for created in build_all():
        print(f"已生成：{created}")