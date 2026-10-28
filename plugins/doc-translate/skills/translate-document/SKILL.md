---
name: translate-document
description: Translates a document file (Word .docx, PDF including scans with seals and signatures, Excel .xlsx, HTML, ePub, Markdown, JSON, TXT) into another language while keeping its layout. Use when the user says "translate this file/document/PDF/contract/report/spec", "翻译这个文件", "把这份 Word/PDF 翻成英文", or asks for a translated copy that looks like the original.
---

# 保留排版的文档翻译

照译这个工具里验证过的流程：用脚本把文件拆成带位置的文字条目，你自己逐块翻译并核对，再用脚本按编号写回原文件。
图片、表格、格式、公式、印章、签名都保持原样。原文件绝不覆盖。

脚本目录：`${CLAUDE_PLUGIN_ROOT}/scripts`。在 Codex、Gemini CLI、DeepSeek Harness 等其他 agent 里这个变量没有值，下文的 `${CLAUDE_PLUGIN_ROOT}` 一律换成 `~/zhaoyi/plugins/doc-translate`（Windows 是 `%USERPROFILE%\zhaoyi\plugins\doc-translate`）。依赖（缺什么装什么）：
`pip install python-docx lxml pypdfium2 pillow reportlab beautifulsoup4 pdfplumber pikepdf pypdf rapidocr_onnxruntime`
（pdfplumber、pikepdf、pypdf 用于 PDF；rapidocr 只在 PDF 里有扫描页时需要。）

## 流程

### 1. 拆分
```
python ${CLAUDE_PLUGIN_ROOT}/scripts/zt.py extract <文件> --lang <en|zh|fr|de|es|ja|ko|ru>
```
这一步生成 `<文件名>.segments.json`，每条格式为 `{"id", "text", "where", "limit"}`：
- 数字、型号、单位，以及已经是目标语言的文字，已经自动排除。
- 脚本会打印原文语言。确认它对得上，判断错了要告诉用户。
- `where` 是位置说明，比如“表格‘X’，表头：A | B | C”或“page 3”，只用来理解意思，不翻译。
- `limit` 是 PDF 表格格子、短标注里大致能放下的字符数，尽量不超过。
- PDF 会按页自动区分：电子版页直接替换文字（保持矢量、可选中），扫描页才认字盖字。脚本会打印各有几页。

### 2. 通读，定术语
读一遍所有条目，再动笔：
- 判断行业和文件类型。
- 列出全文必须统一的术语和译法。用户给了术语表就优先用；电气类文件可参考 `${CLAUDE_PLUGIN_ROOT}/glossaries/` 里的中英、法英术语库。
- 有以前记下的翻译习惯就先读进来（见 translation-feedback 技能）。
- 遇到拿不准、会影响全文的术语，先问用户。

### 3. 翻译
按 `where` 分块翻：同一张表的条目放在一起，连续的正文放在一起。结果写进 `translations.json`，格式为 `{"s1": "译文", ...}`。

规则：
- 每条只给一种译法：不用斜杠并列，不用“或”，不加括号注释。
- 短词（表头、格子）结合表头和行业理解。例子：电气文件里 Cellule 是开关柜，Unité 是单位，arrivée 是进线。
- `<x1>…</x1>` 这样的格式标记全部保留，可以调整先后顺序。
- 数字、单位、型号、网址、代码原样保留；原文没有的编号不要加。
- 术语按表统一，在句子里按语法变形（Pass → passed）。
- 正文忠实完整，不缩写；只有带 `limit` 的短标注可以用通行缩写，不许自造缩写。

文件很大时分几轮写，每轮追加到 `translations.json`。
用户要求用某个外部模型翻时，先读 `references/batching.md`。

### 4. 核对（不能跳过）
```
python ${CLAUDE_PLUGIN_ROOT}/scripts/check.py <文件名>.segments.json translations.json --lang <目标>
```
这一步会查出这些问题：
- 缺译；
- 值不是纯文字；
- 数字丢失；
- 还是原文语言；
- 格式标记丢失；
- 一条里给了多个译法；
- 相邻两格译文相同（可能串格）；
- 长度异常。

全部修好或确认没问题，再进行下一步。

### 5. 写回
```
python ${CLAUDE_PLUGIN_ROOT}/scripts/zt.py apply <文件> translations.json --lang <目标> [--bilingual]
```
- 输出在原文件旁边，名为 `<名>_译文.<扩展名>`；同名文件已存在时自动编号，不会覆盖。
- `--bilingual` 生成原文、译文对照版，支持 docx、txt、md。
- 报告里列出的“没给译文、保留原文”的条目，要逐条确认。

### 6. 看排版
```
python ${CLAUDE_PLUGIN_ROOT}/scripts/render.py <原文件> <译文文件> -o compare
```
- 这一步生成左右对照图。逐页看：有没有溢出、重叠、字太小，印章和签名是否还在。
- Word 和 Excel 需要装 LibreOffice。没装的话，就抽查 docx 里的表格文字。

### 7. 交付
告诉用户这几件事：
- 输出文件在哪；
- 译了多少条，保留原文的有哪些；
- 定下来的关键术语；
- 需要人工看一眼的地方。

用户提意见时，用 translation-feedback 技能。

## 各格式说明
写回规则和已知限制见 `references/formats.md`，按文件类型查。
同一类问题反复出现时，不要一个个打补丁，用 step-back-root-cause 技能。
