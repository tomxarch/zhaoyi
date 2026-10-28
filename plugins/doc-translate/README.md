# doc-translate

doc-translate 是一个保留排版的文档翻译插件，由文档翻译工具“照译”的经验整理而来。

翻译时，agent 用脚本把文件拆成带位置的文字条目，自己翻译并核对，再按编号写回原文件。
原文件的图片、表格、格式、公式、印章和签名都保持原样，原文件也不会被覆盖。

## 包含什么
| 组成 | 作用 |
|---|---|
| translate-document 技能 | 翻译 Word、PDF（电子版直接换字保持矢量；扫描件认字盖字，保留印章签名）、Excel、HTML、ePub、Markdown、JSON、TXT |
| translation-feedback 技能 | 按用户意见修改译文，并把术语和风格习惯记下来，下次翻译时用上 |
| step-back-root-cause 技能 | 同类问题反复出现时，不再一个个打补丁，先找共同的根本原因 |
| scripts/zt.py | 拆分（extract）和写回（apply）。复用照译引擎，结果另存新文件，不覆盖原文件 |
| scripts/check.py | 写回前核对：缺译、串格、数字丢失、残留原文、标记丢失、一条给了多个译法 |
| scripts/render.py | 生成原文和译文的逐页对照图 |
| glossaries/ | 电气行业的中英、法英术语库 |

## 需要安装
- Python 3.10 或以上。
- 依赖包：`pip install python-docx lxml pypdfium2 pillow reportlab beautifulsoup4 pdfplumber pikepdf pypdf rapidocr_onnxruntime`
  - rapidocr_onnxruntime 只在 PDF 里有扫描页时需要。
- 可选：LibreOffice，用来生成 Word 和 Excel 的对照图。

## 用法
直接对 agent 说“把这个文件翻成英文”，就会按 translate-document 技能的流程走：
1. 拆分；
2. 通读，定下术语；
3. 翻译；
4. 核对；
5. 写回；
6. 看对照图；
7. 交付。

译文有问题时，直接说哪里不对或贴截图，agent 会改好，并记住这次的习惯。

## 已知限制
- PDF 按页处理：电子版页直接替换文字；扫描页先认字，再把译文盖在原位置。电子版页里转曲的文字、图片里的字不翻；衬线字体会换成无衬线体。
- 图片里的文字、Word 的域代码（目录、页码）不翻。
- .xls 要先另存为 .xlsx。
- 术语库没有经过行业专家审核。
