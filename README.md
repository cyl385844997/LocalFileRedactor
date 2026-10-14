# LocalFileRedactor（本地文件脱敏工具）

一个**完全离线**的文件脱敏工具：把文档中的手机号、身份证号、银行卡号、邮箱、IP 等敏感信息，
**不可逆地**替换为占位符，输出到新文件。**绝不联网、绝不上传、绝不覆盖原文件。**

> ⚠️ 当前状态：**P0 + P1 第一步已完成并通过测试（185 passed）**。
> P1 第一步 = docx **正文段落**（含跨run 匹配）。表格/页眉页脚/批注/文本框/脚注尾注**尚未处理**，
> xlsx / pdf / pptx / OCR / GUI 尚未实现。详见下方「能力边界」与「路线图」。

---

## 这个工具解决什么问题

把一份含敏感信息的文件交给 AI 分析、或交给第三方处理之前，先把敏感信息抹掉。
抹得干净、且**不可逆**——不是画个黑框盖住，而是把底层字符真正删掉。

## 核心承诺

| 承诺 | 实现方式 |
|---|---|
| **绝不联网** | 依赖里无任何网络库；测试 `TestOfflineGuarantee` 扫描源码禁止 `requests`/`urllib`/`socket` 等导入 |
| **绝不覆盖原文件** | 源文件只读打开；输出路径与源路径相同则直接拒绝；输出已存在默认拒绝 |
| **不可逆脱敏** | PDF 走 PyMuPDF 真正的 `add_redact_annot + apply_redactions`（P3）；其余格式直接改写文本 |
| **报告零敏感内容** | 报告只记录规则名 + 命中次数 + 位置（页/段/行），**不记录任何原文**；不生成任何映射文件 |
| **规则加载失败即中止** | YAML 语法错 / 正则编译错 → 报错退出并指明**行号**，绝不静默跳过某条规则 |
| **部分未处理必须可见** | 因结构复杂而跳过的段落会在报告中单列"需人工复核"，不会藏在"成功"里 |

## 能力边界（重要，请先读）

**能做**（纯正则 + 校验位 + 字典）：

- 手机号、18 位身份证号（带校验位验证）、银行卡号（带 Luhn 校验）、邮箱、IPv4 / IPv6
- 校验位验证能显著降低误报：18 位设备编号不会被误判为身份证

**不能做**（本工具不覆盖）：

- ❌ **人名**（张三、李四）——纯正则做不到，需 NER 模型
- ❌ **地址**（XX 路 XX 号）
- ❌ **单位名**（XX 有限公司）
- ❌ **车牌号、社保号、mac 地址**（尚未内置规则，可自行在 YAML 里加）
- ❌ **间接识别**（如"张工"这种称谓）

如果你需要识别中文人名/地址/单位名，**本工具不能替代人工审查**。
这一点会写进 README 免责声明，也是开源时必须诚实说明的部分。

---

## 已知限制（Known Limitations）

### 通用

- **规则型识别**：只认 YAML 里配置的格式。人名、地址、单位名一律不识别。
- **不做语义判断**：一串 16–19 位数字若 Luhn 校验通过就会被当成银行卡号；
  校验位只能过滤格式错误，过不了"语义是否合理"这一关。

### docx（P1 第一步 + 优先级 1 现状）

**已处理**：正文段落（`document.paragraphs`），含跨 run 的敏感信息，以及含 `<w:br/>` 换行符、`<w:tab/>` 制表符的段落。

**未处理**（本版**不脱敏**，需人工核查）：

| 位置 | 说明 |
|---|---|
| **表格** | `document.paragraphs` 不含表格内段落，本版完全跳过 |
| **页眉/ 页脚** | 未遍历 |
| **批注** | 未遍历 |
| **文本框** | 未遍历 |
| **脚注 / 尾注** | 未遍历 |
| **超链接** | `<w:hyperlink>` 内的 run 未处理（优先级 2） |
| **域代码 / 修订标记 / 书签** | 含这些结构时整段跳过 |

**br/tab 的处理方式**：这类元素在文本流里各占 1 个占位字符（`\n` / `\t`），
使偏移量与真实字符位置保持一致；替换时元素本身原样保留，因此换行、制表符不会被破坏。

> 注意：号码**本身**被换行拆开时（`1380013⏎8000`）**不会**被识别。
> 这是有意为之——跨行的数字串不构成有效手机号，强行匹配反而会误伤。
> 真实文档中不存在这种写法。

**格式保留的三个要点**：

1. **占位符继承首个 run 的格式**。手机号原本拆成"加粗 + 斜体 + 常规"三段，
   脱敏后 `[手机号]` 显示为**加粗**（跟随首个 run）。
2. **未被命中的 run 完全不被触碰**，格式自然保留。
3. **被删空的 run 会从 XML 中移除**。跨 run 替换会让中间/末尾的 run 文本变空，
   若保留空壳，它在 Word 里是**零宽度**的——不渲染也选不中，
   用户会看到"格式似乎存在却点不中"的困惑。移除后空白自然并入相邻 run。

---

## 安装

需要 Python 3.10+。

```bash
# 1. 建议用虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 2. 安装 P0 所需依赖（只有 PyYAML 一个）
pip install -e ".[dev]"

# 后续阶段按需：
pip install -e ".[docx,xlsx,pdf]"    # P1-P3
pip install -e ".[gui]"              # P4
pip install -e ".[pptx,ocr]"         # P5
```

开发时也可以不安装，直接 `PYTHONPATH=src` 运行（pytest 已配置 `pythonpath`）。

## 快速开始

```bash
# 看看内置了哪些规则
redactor rules

# 先看会发生什么，不写任何文件（推荐第一步）
redactor dry-run 待脱敏文件.txt

# 确认无误后正式执行，输出到 redacted_output/
redactor run 待脱敏文件.txt

# 批量处理整个目录，保留子目录结构
redactor run 待脱敏目录/ -r -o 输出目录/

# 只用指定规则 + 编号占位符
redactor run a.txt --only 手机号 --numbered

# 出一份 JSON 报告
redactor run 待脱敏目录/ -r --report 报告.json --report-format json
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --output` | 输出根目录，默认 `redacted_output/` |
| `-r, --recursive` | 递归子目录 |
| `--only 规则名 ...` | 只启用指定规则 |
| `--numbered` | 强制编号占位符（`[手机号#1]`）。**默认关闭**：编号会暴露"这两处是同一个号" |
| `--report` | 报告另存为文件 |
| `--overwrite-output` | 允许覆盖已存在的**输出**文件（仍永不覆盖原文件） |
| `REDACTOR_RULES_DIR` | 环境变量，整体替换规则目录 |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部成功 |
| 1 | 有文件未能处理（详见报告「需要人工处理的文件」） |
| 2 | 参数错误或规则文件错误 |
| 130 | 用户中断 |

## 扩展规则

编辑 `rules/builtin.yaml`，或新建 `rules/myrules.yaml`（同一目录下的多个文件会按文件名顺序合并）：

```yaml
rules:
  - name: 内部工号
    description: 公司内部工号格式 EMP-12345
    pattern: 'EMP-\d{5}'
    placeholder: '[工号]'
    validator: null      # 可选：id_card / luhn
    flags: [IGNORECASE]  # 可选：IGNORECASE/MULTILINE/DOTALL/VERBOSE/UNICODE
    numbered: false      # true 则输出 [内部工号#1]
    enabled: true        # false 则本条不参与匹配（但仍会被校验）
```

**加载是严格的**：字段拼错、重复规则名、非法正则、非法校验位名——任何一处出错都会
报错退出并指出行号。这是刻意设计：静默跳过一条规则，等于给用户制造"已脱敏"的错觉。

## 项目结构

```
LocalFileRedactor/
├── pyproject.toml
├── rules/builtin.yaml              # 规则外置
├── src/redactor/
│   ├── core/        models.py / rules.py / engine.py    # 零第三方依赖
│   ├── recognizers/ base.py / regex_recognizer.py / validators.py
│   ├── redactors/   replacer.py
│   ├── handlers/    base.py / txt_handler.py           # P1 起追加 docx/xlsx/pdf
│   ├── report.py                                    # 零敏感内容报告
│   ├── cli.py
│   └── gui/                                            # P4，唯一 import PySide6 处
├── tests/          镜像 src 结构
└── samples/        合成测试样例（.gitignore）
```

## 验证状态

| 项 | 状态 |
|---|---|
| 全部单元测试 | ✅ **185 passed** |
| CLI 冒烟（dry-run / run / 规则加载） | ✅ 通过，退出码正确 |
| 源文件不变性 | ✅ **SHA-256 前后一致** |
| 跨 run 识别 | ✅ 拆在 3 个 run 的手机号正确命中 |
| 报告展示未处理内容 | ✅ 复杂段落单列"需人工复核" |

### 人工核对样例

```bash
python tests/fixtures/make_docx_samples.py
# 用 Word 打开 samples/docx_demo_split_runs.docx
```

| 样例 | 用途 |
|---|---|
| `docx_demo_split_runs.docx` | 手机号拆在 3 个 run（加粗/斜体/常规）+ 格式对照组 |
| `docx_demo_single_run.docx` | 单 run 完整手机号（对照组） |
| `docx_demo_complex.docx` | 含 `<w:br/>` `<w:tab/>` 的复杂段落（本版应跳过） |

处理后用 Word 打开 `_redacted.docx` 确认：

1. 原手机号已变为 `[手机号]`
2. 段落 2 的**加粗**对照文字仍是加粗
3. 未含敏感信息的段落完全没变
4. `docx_demo_complex.docx` 里的换行符段落**仍是原手机号**（本版明确跳过）

## 路线图

- [x] P0 核心引擎 + 规则外置 + CLI + txt
- [x] P1-1 docx 正文段落 + 跨 run 匹配
- [x] P1-1a docx `<w:br/>` / `<w:tab/>` 支持（优先级 1）
- [ ] P1-1b docx 超链接 `<w:hyperlink>`（优先级 2）
- [ ] P1-2 docx 表格（优先级 3）
- [ ] P1-3 docx 页眉页脚（优先级 4）
- [ ] P1-4 docx 批注（优先级 5）
- [ ] P1-5 docx 文本框、脚注尾注（优先级 6）
- [ ] P2 xlsx（数值单元格 str() 后匹配 + 批注 + 页眉页脚）
- [ ] P3 文本型 PDF（真 redaction）
- [ ] P4 GUI（PySide6 拖拽批量）
- [ ] P5 pptx + 扫描件 PDF（RapidOCR ONNX）
- [ ] P6 PyInstaller 打包 + 开源准备

## 开源协议

**AGPL-3.0-or-later**。

原因：本项目依赖 PyMuPDF（PDF 脱敏的核心），PyMuPDF 是 AGPL / 商业双授权。
若项目整体走 MIT，分发时会与 PyMuPDF 的 AGPL 条款冲突。AGPL-3.0 是
**无需购买商业授权即可合规分发**的选择。个人自用与开源发布均无影响。

（正式开源前需补上完整的 `LICENSE` 文件正文。）

## 安全提示

- 本工具只能处理它"认识"的敏感信息。**脱敏后仍需人工抽查**，尤其是能力边界内的内容。
- 不要用本工具处理涉密文件后仅依赖自动结果对外提供。
- 生成的 `redacted_output/` 目录内的文件已脱敏，但**原始文件仍在原处**，请自行妥善保管。