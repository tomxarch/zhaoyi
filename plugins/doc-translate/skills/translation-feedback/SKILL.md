---
name: translation-feedback
description: Fixes a finished document translation from the user's feedback (typed comments or screenshots) and remembers the lesson for next time. Use when the user says "这里译错了", "这个词应该译成…", "表头太小", "以后都…", "this translation is wrong", "always translate X as Y", or reviews a file made with translate-document.
---

# 按意见修改译文，并记住教训

## 1. 找到原来的那次翻译
- 找到 `<文件名>.segments.json` 和 `translations.json`。
- 找不到就重新 extract，把输出文件里的现有译文对回去。

## 2. 把意见分成四类

| 类型 | 例子 | 怎么处理 |
|---|---|---|
| 具体改正 | “第 3 页这句译错了”、截图圈出一处 | 只改相关的那几条 |
| 术语 | “合格一律译 Pass” | 存进习惯术语，只改含这个词的条目 |
| 风格规则 | “标题全大写”“用英式拼写” | 存进习惯规则。会影响全文，动手前告诉用户要重做多少 |
| 排版问题 | “表头字太小”“地址字号不一致” | 写进 `<文件名>_问题报告.md`，附截图；能调的直接调 |

**注意：规则里不要重复术语。** “合格一律译 Pass”只是一条术语，记成规则会让整份文件重翻，还会把不相干的句子改坏。

用户贴了截图时，先看图找出指的是哪几条（按文字对 segments），再动手。

## 3. 改并重新生成
1. 改 `translations.json`。
2. 跑 `check.py`。
3. 用 `zt.py apply` 生成新版。脚本会自动编号成 `_译文2`，旧版保留，方便用户对比。
4. 告诉用户改了哪几条、哪些没改以及为什么。

## 4. 记住习惯（自我优化）
- 存到 `translation-habits/<目标语言>.json`：放在用户的工作目录里，或者项目记忆里。用户指定了位置就用用户的。

  ```json
  {"terms": {"合格": "Pass"}, "rules": ["Headings in Title Case"],
   "notes": ["电气规范里 Cellule 指开关柜"], "updated": "2026-10-28"}
  ```

- 每条习惯都要用户同意，或者是用户明确说了的。用户要能查看、能删除。
- 每次翻译前（translate-document 的第 2 步）先读这个文件。
- 同一类意见出现第三次时，不要再一条条改，用 step-back-root-cause 技能找共同原因。比如反复出现“短词译错”，根因可能是表格缺上下文；反复出现“漏翻”，根因可能是判断原文语言出了错。
