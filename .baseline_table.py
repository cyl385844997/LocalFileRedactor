"""基线表生成器 —— 唯一权威来源，任何人核对基线一律跑本脚本，禁止手写字面量。

用法（仓库根目录下）：
    PYTHONPATH=src python .baseline_table.py

设计要点：
1. 所有值从 fixtures.make_samples **import**，脚本内不出现任何手写数字字面量。
   这一点是硬约束：手写字面量正是此前"转录层污染"的入口。
2. 同时给出文档内实测出现次数，让基线与实物一次对齐，避免二次转录。
3. 只读 samples/ 与 fixtures/，不写任何文件。
"""
import re
import sys
import zipfile

sys.path.insert(0, "tests")

# ---- 唯一的值来源：import，不手写 ----
from fixtures.make_samples import (  # noqa: E402
    FAKE_BANK_CARD,
    FAKE_EMAIL,
    FAKE_ID_VALID,
    FAKE_IP,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)

#: (变量名, 规则名, 值)—— 值全部来自 import
BASELINE = [
    ("FAKE_PHONE_1", "手机号", FAKE_PHONE_1),
    ("FAKE_PHONE_2", "手机号", FAKE_PHONE_2),
    ("FAKE_EMAIL", "邮箱", FAKE_EMAIL),
    ("FAKE_ID_VALID", "身份证号", FAKE_ID_VALID),
    ("FAKE_BANK_CARD", "银行卡号", FAKE_BANK_CARD),
    ("FAKE_IP", "IP地址", FAKE_IP),
]

DOCX = "samples/docx_demo_full_regression.docx"
T_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)


def doc_blob(path: str) -> str:
    """拼接文档内所有 <w:t> 文本流——跨 run 号码在原始 XML 里不连续，
    直接搜 XML 对不上账，必须先拼接。"""
    out = []
    z = zipfile.ZipFile(path)
    for name in z.namelist():
        if name.endswith(".xml"):
            out.append("".join(T_RE.findall(z.read(name).decode("utf-8"))))
    return "".join(out)


def main() -> int:
    blob = doc_blob(DOCX)

    print("=" * 88)
    print("全容器回归样例基线表（全部由 import 产出，脚本内无手写字面量）")
    print(f"文档：{DOCX}")
    print("=" * 88)
    print(f"{'变量':<20}{'规则名':<10}{'值':<24}{'长度':<6}{'文档内出现':<10}")
    print("-" * 88)

    total = 0
    for name, rule, value in BASELINE:
        count = blob.count(value)
        total += count
        print(f"{name:<20}{rule:<10}{value:<24}{len(value):<6}{count:<10}")

    print("-" * 88)
    print(f"敏感值出现次数合计：{total}")
    print()
    print("说明：")
    print("  - 「文档内出现」按 <w:t> 文本流拼接后统计，跨 run 号码也能计全。")
    print("  - 核对基线请重跑本脚本，不要从任何文档/对话中转录数字。")
    print("  - 本脚本只读，不写文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
