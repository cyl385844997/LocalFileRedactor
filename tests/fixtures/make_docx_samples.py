"""Word(.docx) 测试样例构造器 —— P1 第一版。

用途：让你能**本地打开 Word 亲眼确认**跨run 替换效果与格式保留情况，
而不是只信测试报告。

全部数据为**虚构合成**，不含任何真实个人信息。

运行方式::

    python tests/fixtures/make_docx_samples.py
    # 生成到 samples/ 下，然后用 Word 打开 samples/docx_demo_split_runs.docx

三个核心样例：

* ``docx_demo_split_runs.docx``  手机号被拆在 3 个 run 里（加粗/斜体 / 常规），
  并在同段落放一个"未被涉及"的加粗 run 作为格式对照
* ``docx_demo_single_run.docx``   手机号完整落在单个 run 内（对照组）
* ``docx_demo_complex.docx``      含 ``<w:br/>`` 与 ``<w:tab/>`` 的复杂段落，
  用于验证 br/tab 现在能被正常处理且元素不被破坏
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List

from docx import Document
from docx.enum.text import WD_BREAK

# 允许 `python tests/fixtures/make_docx_samples.py` 直接运行：
# 把 tests/ 目录加入 sys.path，才能 `import fixtures.make_samples`。
_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures.make_samples import (  # noqa: E402
    FAKE_BANK_CARD,
    FAKE_EMAIL,
    FAKE_ID_VALID,
    FAKE_IP,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = PROJECT_ROOT / "samples"


def _set_split_phones(paragraph, phone: str) -> None:
    """把一个手机号**故意拆成 3 个 run**，并赋予不同格式。

    拆分点刻意选在号码中间（而不是首尾），确保每个 run 都是"部分号码"，
    任何单run 匹配都不可能命中——这正是跨 run 处理要解决的场景。

    格式：第1 段加粗、第 2 段斜体、第 3 段常规。

    Args:
        paragraph: 目标段落。
        phone: 完整手机号（11 位）。
    """
    cut1 = len(phone) // 3
    cut2 = len(phone) * 2 // 3
    part1, part2, part3 = phone[:cut1], phone[cut1:cut2], phone[cut2:]

    run1 = paragraph.add_run(part1)
    run1.bold = True

    run2 = paragraph.add_run(part2)
    run2.italic = True

    run3 = paragraph.add_run(part3)  # 常规格式


def make_split_runs_sample() -> Path:
    """生成"跨 run 手机号"样例——本阶段的核心验收对象。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_split_runs.docx"

    document = Document()

    # 标题
    document.add_heading("Word 跨 run 脱敏样例（合成数据）", level=1)

    # 段落 1：手机号拆成 3 个 run，分别加粗 / 斜体 / 常规
    paragraph = document.add_paragraph()
    paragraph.add_run("联系人手机：")
    _set_split_phones(paragraph, FAKE_PHONE_1)
    tail = paragraph.add_run("（该号码被刻意拆成 3 个 run）")
    tail.font.color.rgb = None  # 不设颜色，保持默认

    # 段落 2：格式对照——一个"不会被命中"的加粗 run，验证其格式保持不变
    paragraph2 = document.add_paragraph()
    marker = paragraph2.add_run("【格式对照组：这段加粗文字不含敏感信息，应保持加粗】")
    marker.bold = True
    marker.font.size = None

    # 段落 3：多个敏感字段 + 其中两个邮箱拆 run
    paragraph3 = document.add_paragraph()
    paragraph3.add_run("邮箱：")
    email = FAKE_EMAIL
    cut = email.index("@")  # 在 @ 处拆开，跨 run 的邮箱
    run_a = paragraph3.add_run(email[:cut])
    run_a.bold = True
    run_b = paragraph3.add_run(email[cut:])
    run_b.underline = True

    # 段落 4：完整落在单个 run 内的多个敏感字段（对照组）
    paragraph4 = document.add_paragraph()
    paragraph4.add_run(
        f"身份证 {FAKE_ID_VALID}，银行卡 {FAKE_BANK_CARD}，服务器 {FAKE_IP}，备用手机 {FAKE_PHONE_2}"
    )

    # 段落 5：无敏感信息，应完全不被改动
    document.add_paragraph("本段落不含任何敏感信息，用于验证未被改动的段落保持原样。")

    document.save(str(path))
    return path


def make_single_run_sample() -> Path:
    """生成"单个 run 内完整手机号"样例，作为跨 run 样例的对照组。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_single_run.docx"

    document = Document()
    document.add_heading("Word 单 run 脱敏样例（对照组）", level=1)

    # 手机号完整落在一个 run 内
    paragraph = document.add_paragraph()
    paragraph.add_run("联系人手机：")
    run = paragraph.add_run(FAKE_PHONE_1)  # 整个号码在同一个 run
    run.bold = True

    document.add_paragraph(f"邮箱 {FAKE_EMAIL}，服务器 {FAKE_IP}")

    document.save(str(path))
    return path


def make_complex_sample() -> Path:
    """生成含 ``<w:br/>`` 与 ``<w:tab/>`` 的复杂段落样例。

    这类段落从 P1 优先级 1 起**正常处理**（br/tab 各占 1 个占位字符对齐偏移，
    替换时元素原样保留），本样例用于验证该行为。
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_complex.docx"

    document = Document()
    document.add_heading("Word 复杂结构样例（本版应跳过）", level=1)

    # 含换行符的段落：手机号前后有 <w:br/>
    paragraph = document.add_paragraph()
    paragraph.add_run("第一行文字")
    paragraph.add_run().add_break(WD_BREAK.LINE)
    paragraph.add_run(f"换行符后面的手机 {FAKE_PHONE_1}")
    paragraph.add_run().add_break(WD_BREAK.LINE)
    paragraph.add_run("最后一行")

    # 含制表符的段落
    paragraph2 = document.add_paragraph()
    paragraph2.add_run("制表符前")
    paragraph2.add_run().add_tab()
    paragraph2.add_run(f"制表符后手机 {FAKE_PHONE_2}")

    # 纯 run 段落：这一段应当正常脱敏
    document.add_paragraph(f"这一段是纯 run，应当正常脱敏：手机 {FAKE_PHONE_1}")

    document.save(str(path))
    return path


def build_all() -> List[Path]:
    """生成全部 docx 样例。"""
    return [make_split_runs_sample(), make_single_run_sample(), make_complex_sample()]


def clean() -> None:
    """删除生成的样例。"""
    if SAMPLES_DIR.exists():
        shutil.rmtree(SAMPLES_DIR)


if __name__ == "__main__":
    for created in build_all():
        print(f"已生成：{created}")
    print()
    print("请用 Word 打开 samples/docx_demo_split_runs.docx 确认：")
    print("  1) 原手机号已变为 [手机号]")
    print("  2) 段落 2 的加粗对照文字仍是加粗")
    print("  3) 未含敏感信息的段落完全没变")