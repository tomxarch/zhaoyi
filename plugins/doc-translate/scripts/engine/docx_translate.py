"""照译：翻译 .docx，尽量保持原来的排版。

做法：
1. 把正文、表格、文本框、页眉页脚、脚注里的每一段找出来。
2. 一段里格式相同的连续文字算一组；有多组时用 <x1>…</x1> 标记交给模型，
   译完再按标记把文字放回对应格式（第 4 天）。
3. 纯译文：直接替换原文；双语对照：在每段原文下面插一段译文（第 5 天）。
4. 可选润色：再用一个模型审一遍，改过的段落加 Word 批注写明原译和理由（第 9 天）。
图片、页码、分页符这些不含文字的内容不会被动到。
"""
import copy
import html
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docx import Document
from lxml import etree
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

# 需要翻译的部件：正文、页眉、页脚、脚注、尾注
PART_TYPES = ("document.main+xml", "header+xml", "footer+xml", "footnotes+xml", "endnotes+xml")
# 段里会被当成“文字”的元素
TEXT_TAGS = {qn("w:t"), qn("w:tab"), qn("w:cr")}
# 比较格式时忽略的属性（语言、拼写检查标记不算格式）
# 看不出来或不影响意思的格式不算“不同格式”：PDF 转出来的 Word 常常每个词的字距（spacing）、字偶距（kern）都不一样，
# 当成不同格式会把一句话拆成十几个标记，模型就容易原样返回、不翻
IGNORED_RPR = {qn("w:lang"), qn("w:noProof"), qn("w:rFonts"), qn("w:proofErr"), qn("w:spacing"), qn("w:kern"),
               qn("w:w"), qn("w:szCs"), qn("w:bCs"), qn("w:iCs")}
HAS_LETTER = re.compile(r"[^\W\d_]", re.U)
TAG_RE = re.compile(r"<x(\d+)>(.*?)</x\1>", re.S)


# ---------- 读取一段 ----------

def run_text(r):
    parts = []
    for child in r:
        if child.tag == qn("w:t"):
            parts.append(child.text or "")
        elif child.tag == qn("w:tab"):
            parts.append("\t")
        elif child.tag in (qn("w:br"), qn("w:cr")) and child.get(qn("w:type")) in (None, "textWrapping"):
            parts.append("\n")
    return "".join(parts)


def has_other_content(r):
    """run 里除了文字和格式之外还有别的东西（图片、域代码等）。"""
    for child in r:
        if child.tag == qn("w:rPr") or child.tag in TEXT_TAGS:
            continue
        if child.tag == qn("w:br") and child.get(qn("w:type")) in (None, "textWrapping"):
            continue
        return True
    return False


def format_key(r):
    rpr = r.find(qn("w:rPr"))
    if rpr is None:
        return ""
    rpr = copy.deepcopy(rpr)
    for child in list(rpr):
        if child.tag in IGNORED_RPR:
            rpr.remove(child)
    return rpr.xml if hasattr(rpr, "xml") else ""


def own_runs(p):
    """这一段自己的 run（含超链接、修订插入里的），不含文本框里嵌套的段落。"""
    runs = []
    for r in p.iter(qn("w:r")):
        parent = r.getparent()
        # 跳过文本框等嵌套内容里的 run，它们属于别的段落
        anc = parent
        nested = False
        while anc is not None and anc is not p:
            if anc.tag == qn("w:p"):
                nested = True
                break
            anc = anc.getparent()
        if not nested:
            runs.append(r)
    return runs


class Segment:
    """一段要翻译的文字，以及它对应的格式分组。"""

    def __init__(self, p, part):
        self.p = p
        self.part = part
        self.groups = []  # 每组：[run, run, ...]
        last_key = None
        for r in own_runs(p):
            text = run_text(r)
            if not text:
                if has_other_content(r):
                    last_key = None  # 图片、域代码会把前后分成两组
                continue
            key = (id(r.getparent()), format_key(r))
            if not text.strip() and self.groups and last_key is not None and not has_other_content(r):
                self.groups[-1].append(r)  # 只有空格的 run 并到前一组，不单独成一个标记
                continue
            if key == last_key and not has_other_content(r):
                self.groups[-1].append(r)
            else:
                self.groups.append([r])
            last_key = None if has_other_content(r) else key
        self.texts = ["".join(run_text(r) for r in g) for g in self.groups]
        self.plain = "".join(self.texts)

    @property
    def needs_translation(self):
        return bool(self.plain.strip()) and bool(HAS_LETTER.search(self.plain))

    @property
    def tagged(self):
        return len(self.groups) > 1

    @property
    def source(self):
        if not self.tagged:
            return self.plain
        return "".join(f"<x{i}>{html.escape(t, quote=False)}</x{i}>"
                       for i, t in enumerate(self.texts, 1))


# ---------- 写回一段 ----------

def set_run_text(r, text):
    for child in list(r):
        if child.tag in TEXT_TAGS or (child.tag == qn("w:br") and child.get(qn("w:type")) in (None, "textWrapping")):
            r.remove(child)
    for i, line in enumerate(text.split("\n")):
        if i:
            r.append(parse_xml('<w:br xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'))
        for j, piece in enumerate(line.split("\t")):
            if j:
                r.append(parse_xml('<w:tab xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'))
            if piece:
                t = parse_xml('<w:t xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xml:space="preserve"/>')
                t.text = piece
                r.append(t)


def parse_tagged(result, count):
    """把带标记的译文拆回各组，返回 (译文里的组顺序, {组号: 文字})。拆不开就返回 None。"""
    matches = list(TAG_RE.finditer(result))
    order = [int(m.group(1)) for m in matches]
    if sorted(order) != list(range(1, count + 1)):
        return None
    texts = {}
    pos = 0
    for k, m in enumerate(matches):
        gap = html.unescape(result[pos:m.start()])
        idx = order[k]
        texts[idx] = html.unescape(m.group(2))
        if gap:
            # 标记之外的文字（多半是空格）接到前一组后面；最前面的接到第一组前面
            if k:
                texts[order[k - 1]] += gap
            else:
                texts[idx] = gap + texts[idx]
        pos = m.end()
    if result[pos:]:
        texts[order[-1]] += html.unescape(result[pos:])
    return order, texts


def apply_translation(seg, p, result):
    """把译文写进段落 p（p 可以是原段落，也可以是双语模式里复制出来的那段）。"""
    seg_here = Segment(p, seg.part) if p is not seg.p else seg
    groups = seg_here.groups
    if not groups:
        return []
    if seg.tagged:
        parsed = parse_tagged(result, len(groups))
    else:
        parsed = None

    if parsed and parsed[0] == sorted(parsed[0]):
        # 顺序没变：每组放回原位
        _, texts = parsed
        for i, g in enumerate(groups, 1):
            set_run_text(g[0], texts[i])
            for r in g[1:]:
                set_run_text(r, "")
        return [g[0] for g in groups]

    if parsed and all(r.getparent() is p for g in groups for r in g):
        # 顺序变了（比如英文语序不同），按译文顺序重新排这些文字 run
        order, texts = parsed
        anchor = groups[0][0]
        templates = {i: g[0] for i, g in enumerate(groups, 1)}
        new_runs = []
        for idx in order:
            r = copy.deepcopy(templates[idx])
            for child in list(r):
                if child.tag != qn("w:rPr"):
                    r.remove(child)
            set_run_text(r, texts[idx])
            new_runs.append(r)
        for r in new_runs:
            anchor.addprevious(r)
        for g in groups:
            for r in g:
                r.getparent().remove(r)
        return new_runs

    # 没有标记，或者模型把标记弄乱了：整段用第一组的格式
    text = html.unescape(TAG_RE.sub(lambda m: m.group(2), result)) if seg.tagged else result
    text = re.sub(r"</?x\d+>", "", text)
    set_run_text(groups[0][0], text)
    for g in groups:
        for r in g:
            if r is not groups[0][0]:
                set_run_text(r, "")
    return [groups[0][0]]


def is_numbered(p, doc):
    """段落带列表编号（直接设置的，或者来自样式）。"""
    ppr = p.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:numPr")) is not None:
        return True
    style_id = None
    if ppr is not None and ppr.find(qn("w:pStyle")) is not None:
        style_id = ppr.find(qn("w:pStyle")).get(qn("w:val"))
    styles = doc.styles.element
    while style_id:
        style = next((st for st in styles.iter(qn("w:style")) if st.get(qn("w:styleId")) == style_id), None)
        if style is None:
            return False
        sppr = style.find(qn("w:pPr"))
        if sppr is not None and sppr.find(qn("w:numPr")) is not None:
            return True
        based = style.find(qn("w:basedOn"))
        style_id = based.get(qn("w:val")) if based is not None else None
    return False


def make_bilingual_copy(p):
    """在原段落下面复制一段，用来放译文。"""
    new_p = copy.deepcopy(p)
    # 分节符只留一个，放在译文段上，这样译文和原文在同一节
    ppr = p.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:sectPr")) is not None:
        ppr.remove(ppr.find(qn("w:sectPr")))
    for tag in ("w:bookmarkStart", "w:bookmarkEnd", "w:commentRangeStart", "w:commentRangeEnd"):
        for el in list(new_p.iter(qn(tag))):
            el.getparent().remove(el)
    for r in list(new_p.iter(qn("w:r"))):
        if r.find(qn("w:commentReference")) is not None:
            r.getparent().remove(r)
    # 分页符只留在原文段，免得译文跑到下一页
    for br in list(new_p.iter(qn("w:br"))):
        if br.get(qn("w:type")) == "page":
            br.getparent().remove(br)
    p.addnext(new_p)
    return new_p


def merge_into_original(p, new_p):
    """列表项不另起一段（否则会多出编号），把译文换行接在原文后面。"""
    br = parse_xml('<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:br/></w:r>')
    p.append(br)
    for child in list(new_p):
        if child.tag != qn("w:pPr"):
            p.append(child)
    new_p.getparent().remove(new_p)


# ---------- 整个文件 ----------

def collect_segments(doc):
    segments = []
    blob_parts = []
    for part in doc.part.package.iter_parts():
        if not part.content_type.endswith(PART_TYPES):
            continue
        element = getattr(part, "_element", None)
        if element is None:
            element = parse_xml(part.blob)
            blob_parts.append((part, element))
        for p in element.iter(qn("w:p")):
            # 文本框在新旧两种格式里各存一份，旧格式那份跳过
            if any(a.tag.endswith("}Fallback") for a in p.iterancestors()):
                continue
            seg = Segment(p, part)
            if seg.needs_translation:
                segments.append(seg)
    return segments, blob_parts


def translate_docx(src, translator, dst=None, bilingual=False, reviewer=None,
                   workers=None, on_progress=None):
    """翻译一个 .docx。返回 (输出文件, 提示信息列表)。"""
    src = Path(src)
    if dst is None:
        suffix = "_双语" if bilingual else "_译文"
        dst = src.with_name(f"{src.stem}{suffix}{src.suffix}")
    dst = Path(dst)
    doc = Document(src)
    segments, blob_parts = collect_segments(doc)

    # 同样的文字只翻一次（页眉页脚、重复的表头很常见）
    unique = list(dict.fromkeys((s.source, s.tagged) for s in segments))
    batched = getattr(translator, "kind", "") in ("openai", "mt")
    if batched:  # 大模型：几十段拼成一次请求，请求次数少一二十倍，也不容易被限流（每分钟次数有上限）
        from formats import translate_texts
        first = [0]

        def chunk_done(d, n):
            first[0] = n
            if on_progress:
                on_progress(d, n + (len(unique) if reviewer else 0))
        # 每批小一点（约一两千字），进度条走得勤，一批出错重翻也快
        places = {}
        for seg in segments:
            places.setdefault((seg.source, seg.tagged), seg_place(seg))
        drafts = dict(zip(unique, translate_texts(translator, [s for s, _ in unique], on_progress=chunk_done,
                                                  items=20, chars=1500, where=[places[u] for u in unique])))
        done = [first[0]]
        total = first[0] + (len(unique) if reviewer else 0)
    else:
        total = len(unique) * (2 if reviewer else 1)
        done = [0]

    def step():
        done[0] += 1
        if on_progress:
            on_progress(done[0], total)

    def do_translate(item):
        source, tagged = item
        result = translator.translate(source, tagged=tagged)
        step()
        return result

    with ThreadPoolExecutor(max_workers=workers or getattr(translator, "workers", 4)) as pool:
        if not batched:
            drafts = dict(zip(unique, pool.map(do_translate, unique)))

        reviews = {}
        if reviewer:
            def do_review(item):
                result = reviewer.review(item[0], drafts[item])
                step()
                return result
            reviews = dict(zip(unique, pool.map(do_review, unique)))

    notes = []
    main_part = doc.part
    for seg in segments:
        key = (seg.source, seg.tagged)
        final = drafts[key]
        changed, reason = False, ""
        if key in reviews:
            final, changed, reason = reviews[key]

        target = make_bilingual_copy(seg.p) if bilingual else seg.p
        new_runs = apply_translation(seg, target, final)
        if bilingual and is_numbered(seg.p, doc):
            merge_into_original(seg.p, target)
            target = seg.p

        if changed and new_runs and seg.part is main_part:
            para = Paragraph(target, doc._body)
            runs = [Run(r, para) for r in new_runs]
            old = re.sub(r"</?x\d+>", "", drafts[key])
            doc.add_comment(runs=(runs[0], runs[-1]),
                            text=f"原译：{old}\n理由：{reason or '润色'}",
                            author="照译润色", initials="ZY")

        missing = [t for s, t in seg_terms(seg, translator) if t.lower() not in final.lower()]
        if missing:
            notes.append(f"术语没按要求翻译：{'、'.join(missing)}（原文：{seg.plain[:30]}）")

    for part, element in blob_parts:
        part._blob = etree.tostring(element, xml_declaration=True, encoding="UTF-8", standalone=True)

    doc.save(dst)
    if reviewer:
        n = sum(1 for r in reviews.values() if r[1])
        notes.insert(0, f"润色改动了 {n} 处，已在 Word 里用批注标出。")
    return dst, notes


def plain_text(el):
    return "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()


def seg_place(seg):
    """这段在文件里的位置：(分组, 说明)。表格里的格子按表分组，说明里写表前面的标题和表头；正文按所在部件分组。"""
    tc = next((a for a in seg.p.iterancestors() if a.tag == qn("w:tc")), None)
    if tc is None:
        return ("body", id(seg.part)), ""
    tbl = next(a for a in tc.iterancestors() if a.tag == qn("w:tbl"))
    rows = tbl.findall(qn("w:tr"))
    head = " | ".join(filter(None, (plain_text(c) for c in rows[0].findall(qn("w:tc"))))) if rows else ""
    title = ""
    prev = tbl.getprevious()
    for _ in range(3):  # 表格前面最近的一段有字的：多半是表名或者章节标题
        if prev is None:
            break
        if prev.tag == qn("w:p") and plain_text(prev):
            title = plain_text(prev)
            break
        prev = prev.getprevious()
    parts = []
    if title:
        parts.append(f"表格“{title[:60]}”")
    if head:
        parts.append(f"表头：{head[:150]}")
    return ("table", id(seg.part), tbl.getroottree().getpath(tbl)), "，".join(parts) or "表格里"


def seg_terms(seg, translator):
    lower = seg.plain.lower()
    glossary = getattr(translator, "check_glossary", translator.glossary)
    return [(s, t) for s, t in (glossary or {}).items() if s.lower() in lower]
