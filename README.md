# 照译 ZhaoYi · doc-translate

一个给 Claude 用的**保留排版的文档翻译插件**。把 Word、PDF、Excel 这类文件交给 Claude，拿回一份排版不变的译文。

A Claude plugin that translates documents **while keeping their layout**: Word, PDF (including scans with seals and signatures), Excel, HTML, ePub, Markdown, JSON and TXT.

## 它怎么工作 / How it works

1. 脚本把文件拆成带位置的文字条目（`zt.py extract`）；
2. Claude 通读全文，定下术语，逐条翻译；
3. 写回前自动核对：缺译、串格、数字丢失、残留原文、格式标记丢失（`check.py`）；
4. 按编号写回原文件（`zt.py apply`），另存为 `<文件名>_译文.<扩展名>`，**原文件不会被覆盖**；
5. 生成原文和译文的逐页对照图（`render.py`），确认排版。

图片、表格、公式、印章、签名都保持原样。电子版 PDF 直接替换文字，保持矢量；扫描页用本地 OCR 认字，再把译文盖在原位置。

翻译由 Claude 自己完成，**不需要另外申请任何翻译 API key**。

## 包含什么 / What's inside

| 组成 | 作用 |
|---|---|
| `translate-document` 技能 | 保留排版的文档翻译流程 |
| `translation-feedback` 技能 | 按你的意见改译文，并记住术语和风格习惯 |
| `step-back-root-cause` 技能 | 同类问题反复出现时，先找共同的根本原因，不再一个个打补丁 |
| `scripts/` | 拆分、写回、核对、对照图脚本 |
| `glossaries/` | 电气行业中英、法英术语库（约 110 条） |

## 安装 / Install

在 Claude Code 里运行：

```
/plugin marketplace add tomxarch/zhaoyi
/plugin install doc-translate@zhaoyi
```

然后安装 Python 依赖（Python 3.10 或以上）：

```
pip install python-docx lxml pypdfium2 pillow reportlab beautifulsoup4 pdfplumber pikepdf pypdf rapidocr_onnxruntime
```

- `rapidocr_onnxruntime` 只在 PDF 里有扫描页时需要。
- 可选：装 LibreOffice，可以生成 Word 和 Excel 的对照图。

### 其他 agent（Codex、Gemini CLI、DeepSeek Harness 等）/ Other agents

照译的技能符合 [Agent Skills](https://agentskills.io) 标准，Codex、Gemini CLI 和 DeepSeek Harness（dsh）都会读取 `~/.agents/skills/` 文件夹。
The skills follow the Agent Skills standard; Codex, Gemini CLI and DeepSeek Harness (dsh) all read `~/.agents/skills/`.

**Windows（PowerShell）**
```
git clone https://github.com/tomxarch/zhaoyi $HOME\zhaoyi
New-Item -ItemType Directory -Force $HOME\.agents\skills
Copy-Item -Recurse $HOME\zhaoyi\plugins\doc-translate\skills\* $HOME\.agents\skills\
```

**macOS / Linux**
```
git clone https://github.com/tomxarch/zhaoyi ~/zhaoyi
mkdir -p ~/.agents/skills && cp -r ~/zhaoyi/plugins/doc-translate/skills/* ~/.agents/skills/
```

- 仓库要放在用户目录下的 `zhaoyi` 文件夹里，技能会从这里找脚本。
- Gemini CLI 装完后在会话里运行 `/skills reload`。
- DeepSeek Harness 装完后重启 dsh，技能会出现在命令面板里。
- 其他支持 Agent Skills 的工具，把 `skills` 里的三个文件夹复制到它的技能目录即可。
- Python 依赖和上面一样。

## 用法 / Usage

直接对 Claude 说：

> 把这个文件翻成英文
> Translate this PDF into Chinese

译文哪里不对，直接说或者贴截图，Claude 会改好，并记住这次的习惯。

## 已知限制 / Known limitations

- 图片里的文字、PDF 里转成曲线的文字不翻译。
- Word 的域代码（目录、页码）不翻译。
- PDF 里的衬线字体会换成无衬线体。
- `.xls` 要先另存为 `.xlsx`。
- 术语库没有经过行业专家审核，正式文件请人工复核。

## 第三方组件 / Third-party

本项目只依赖宽松许可的开源库（MIT、BSD、Apache-2.0、MPL-2.0）。
可选的 BabelDOC 路线采用 AGPL-3.0 协议，所以**没有打包**进来，只在你单独安装后通过命令行调用。

## 作者 / Authors

- **tom**：想法、需求、测试，以及每一次“这里还不对”。
- 与 **Wren**（Claude，Anthropic 出品）共同开发。
  Co-developed with **Wren** (Claude, by Anthropic).

## 许可证 / License

[MIT](LICENSE)
