"""其他格式的文件：TXT、Markdown、HTML、ePub、JSON、Excel（.xlsx）。

做法都一样：先把文件里要翻的文字一条条找出来，同样的文字只翻一次，按批交给模型（带场景、术语表），
再把译文写回原来的位置。格式、结构、代码、链接、公式都不动。输出文件名是“原名_译文.扩展名”。
"""
import html
import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdf_overlay import CJK, CJK_LANGS, mostly_cjk, needs_translation, translate_page

SUPPORTED = (".txt", ".md", ".markdown", ".html", ".htm", ".epub", ".json", ".xlsx")

FILE_PROMPT = ("下面是一个文件里要翻译的文字，JSON 格式：blocks 里键是编号或原文本身、值是原文。"
               "把每一条翻译成{lang}，按原来的键输出 JSON：{{\"键\": \"译文\", ...}}。"
               "每条都要完整、忠实地翻译，不要漏，不要加解释。数字、网址、代码、变量名（比如 {{name}}、%s）原样保留。"
               "不要用 LaTeX。只输出 JSON。")
TAG_NOTE = ("有的原文用 <x1>…</x1>、<x2>…</x2> 这样的标记区分不同格式（比如加粗、链接）。"
            "译文里必须保留每一个标记，用它包住对应意思的译文，可以按{lang}的语序调整先后，不要新增或删掉标记。")
MD_NOTE = "原文是 Markdown：`行内代码`、链接和图片的地址、**加粗**、*斜体* 这些标记原样保留，只翻文字。"
TAG_RE = re.compile(r"<x(\d+)>(.*?)</x\1>", re.S)
CHUNK_ITEMS, CHUNK_CHARS = 40, 3000


def output_path(src, bilingual=False):
    src = Path(src)
    return src.with_name(f"{src.stem}{'_双语' if bilingual else '_译文'}{src.suffix}")


PREPARE_PROMPT = ("下面是一份要翻译成{lang}的文件的摘录（原文是{src}）。先通读，"
                  "找出文件里反复出现、全文必须统一译法的专业术语和固定说法（设备名、参数名、表头、章节名里的词，最多 30 个），"
                  "按这个行业的标准说法给出{lang}译法；再用一句话（中文）说明这是什么行业的什么文件。"
                  '只输出 JSON：{{"type": "一句话", "terms": {{"原文术语": "译法"}}}}。'
                  "术语照抄原文里的写法；普通词、数字、型号不要列。")
CONTEXT_NOTE = "\n这一批文字的位置（只用来理解意思，不用翻译）：{where}。"
DOC_NOTE = "\n整份文件是：{what}。"


def forget_document(translator):
    """换下一份文件前，把上一份通读出来的原文语言、术语、文件说明清掉。"""
    for src in getattr(translator, "doc_terms", {}):
        translator.glossary.pop(src, None)
    translator.doc_terms = {}
    translator.source_lang = None
    translator.doc_note = ""


def prepare(translator, texts):
    """开翻之前通读一遍整份文件：认出原文语言，提出全文要统一译法的术语（加进这份文件的术语表），记下这是什么文件。
    只多一次请求；同一份文件再翻会从翻译记忆里拿，不再请求。"""
    from pdf_overlay import debug_log, detect_source, parse_json
    from providers import Stopped, memo_get, memo_put, with_retry
    import hashlib
    forget_document(translator)
    translator.source_lang = detect_source(texts)
    if translator.kind != "openai":
        return
    unique = [t for t in dict.fromkeys(TAG_RE.sub(r"\2", t).strip() for t in texts) if t]
    if sum(map(len, unique)) < 800:  # 很短的文件不用
        return
    # 摘录：先放短的（标题、表头、格子里的词，最容易译错），再放正文开头，一共约 3500 字
    sample, size = [], 0
    for t in sorted(unique, key=len)[:400] + unique:
        if size > 3500:
            break
        if t not in sample:
            sample.append(t)
            size += len(t) + 1
    text = "\n".join(sample)
    key = "prep\x00" + hashlib.sha1(json.dumps([translator.provider, translator.model, translator.target_lang,
                                                  text], ensure_ascii=False).encode("utf-8")).hexdigest()
    plan = None
    cached = memo_get(key)
    if cached:
        try:
            plan = json.loads(cached)
        except ValueError:
            plan = None
    if plan is None:
        system = PREPARE_PROMPT.format(lang=translator.target_lang, src=translator.source_lang or "外文")
        try:
            plan = parse_json(translator._chat((translator.scene or "") + system, text))  # 只试一次，超时就跳过
        except Stopped:
            raise
        except Exception as e:  # 通读失败（模型没按格式回、额度用完）不影响翻译本身，后面照常翻
            debug_log("通读整份文件没成功", e)
            return
        if not isinstance(plan, dict):
            return
        memo_put(key, json.dumps(plan, ensure_ascii=False))
    whole = "\n".join(unique).lower()
    added = {}
    for src, tgt in (plan.get("terms") or {}).items():
        src, tgt = str(src).strip(), str(tgt).strip()
        # 只收原文里真有、出现两次以上的；已经有译法（用户术语表、行业库、习惯）的不覆盖
        if (not src or not tgt or len(src) > 60 or whole.count(src.lower()) < 2
                or any(k.lower() == src.lower() for k in translator.glossary)):
            continue
        if translator.target_lang not in ("中文", "繁体中文", "日文", "韩文") and CJK.search(tgt):
            continue
        added[src] = tgt
    translator.glossary.update(added)
    translator.doc_terms = added
    what = str(plan.get("type") or "").strip()
    translator.doc_note = DOC_NOTE.format(what=what[:120]) if what else ""


def translate_texts(translator, texts, on_progress=None, note="", items=CHUNK_ITEMS, chars=CHUNK_CHARS, where=None):
    """一批文字翻成目标语言，返回同样长度的列表。不用翻的（数字、已经是目标语言）原样返回。
    where：和 texts 一样长，每条在文件里的位置 (分组, 说明)，比如同一张表的格子是一组、说明里有表名和表头。
    同一组的放在一批里、带着说明一起送，短词才能按上下文译对；不同组尽量不混在一批。"""
    lang = translator.target_lang
    prepare(translator, texts)
    src = translator.source_lang
    cjk_doc = src in CJK_LANGS if src else mostly_cjk(texts)
    place = {}
    for t, w in zip(texts, where or [None] * len(texts)):
        place.setdefault(t, w)
    todo = list(dict.fromkeys(t for t in texts if t.strip() and needs_translation(t, lang, cjk_doc)))
    chunks, cur, size = [], [], 0
    for t in todo:
        group = (place.get(t) or (None, ""))[0]
        new_group = cur and group != (place.get(cur[-1]) or (None, ""))[0] and len(cur) >= items // 2
        if cur and (len(cur) >= items or size + len(t) > chars or new_group):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t)
    if cur:
        chunks.append(cur)

    done = [0]
    extra = note.format(lang=lang) + getattr(translator, "doc_note", "")

    def run(chunk):
        tagged = any("<x1>" in t for t in chunk)
        wheres = list(dict.fromkeys((place.get(t) or (None, ""))[1] for t in chunk))
        wheres = [w for w in wheres if w][:3]
        context = CONTEXT_NOTE.format(where="；".join(wheres)) if wheres else ""
        out = translate_page(translator, chunk, extra=extra + context + (TAG_NOTE.format(lang=lang) if tagged else ""),
                             prompt=FILE_PROMPT)
        done[0] += len(chunk)  # 进度按条数算，一批翻完就往前跳这一批的条数
        if on_progress:
            on_progress(done[0], len(todo))
        return out

    table = {}
    with ThreadPoolExecutor(max(1, translator.workers)) as pool:
        for chunk, out in zip(chunks, pool.map(run, chunks)):
            table.update(zip(chunk, out))
    return [table.get(t, t) for t in texts]


def keep_space(source, result):
    """原文前后的空白（缩进、换行）照搬到译文上。"""
    lead = source[:len(source) - len(source.lstrip())]
    tail = source[len(source.rstrip()):]
    return lead + result.strip() + tail


def read_text(path, with_encoding=False):
    """读文本文件：UTF-8（有没有 BOM 都行）或 GBK。with_encoding=True 时一起返回原来的编码，写回时照用。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8", "gb18030"):
        try:
            text = data.decode(enc)
            return (text, enc) if with_encoding else text
        except UnicodeDecodeError:
            continue
    text = data.decode("utf-8", errors="replace")
    return (text, "utf-8") if with_encoding else text


def spaced(texts, target_lang, outer=None):
    """译成英文这类语言时，几段挨着的文字（加粗、链接前后）中间补空格，免得“themoulded case breakeris”粘在一起。
    outer[i] 为 True 表示第 i 段是块里直接的文字（不在加粗、链接里），空格优先补在它身上。"""
    from pdf_overlay import LATIN_LANGS
    if target_lang not in LATIN_LANGS:
        return texts
    out = list(texts)
    for i in range(1, len(out)):
        j = i - 1
        while j >= 0 and not out[j]:
            j -= 1
        if j < 0 or not out[i]:
            continue
        a, b = out[j], out[i]
        if a[-1].isspace() or b[0].isspace():
            continue
        if (a[-1].isalnum() or a[-1] in ").,;:!?%\"'") and (b[0].isalnum() or b[0] in "(\"'"):
            if outer and outer[j] and not (outer[i]):
                out[j] = a + " "
            else:
                out[i] = " " + b
    return out


# ---------- TXT ----------

def translate_txt(src, translator, bilingual=False, on_progress=None):
    text, encoding = read_text(src, with_encoding=True)
    parts = re.split(r"(\n[ \t　]*\n)", text.replace("\r\n", "\n"))  # 按空行分段，空行原样留着
    paras = [p for i, p in enumerate(parts) if i % 2 == 0]
    results = translate_texts(translator, [p.strip() for p in paras], on_progress)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)
            continue
        result = results[i // 2]
        if not part.strip() or result == part.strip():
            out.append(part)
        elif bilingual:
            out.append(part.rstrip() + "\n" + result + part[len(part.rstrip()):])
        else:
            out.append(keep_space(part, result))
    dst = output_path(src, bilingual)
    try:  # 和原文件用同样的编码（原来没有 BOM 就不加）
        dst.write_text("".join(out), encoding=encoding)
    except UnicodeEncodeError:
        dst.write_text("".join(out), encoding="utf-8")
    return dst


# ---------- Markdown ----------

MD_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|>\s?|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)*)(.*?)(\s*)$")


def translate_md(src, translator, bilingual=False, on_progress=None):
    lines = read_text(src).replace("\r\n", "\n").split("\n")
    units = []  # 每项：原样保留的行（str），或 [前缀, 文字, 结尾] 要翻的
    fence = None
    for n, line in enumerate(lines):
        stripped = line.strip()
        if fence:  # 代码块里不翻
            units.append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            units.append(line)
            continue
        if n == 0 and stripped == "---":  # 文件开头的 front matter
            fence = "---"
            units.append(line)
            continue
        if (not stripped or stripped.startswith(("<!--", "<")) or re.fullmatch(r"[-*_=\s]{3,}", stripped)
                or (line.startswith(("    ", "\t")) and not (units and isinstance(units[-1], list)))):
            units.append(line)
            continue
        if stripped.startswith("|"):  # 表格：每格单独翻，分隔行不动
            if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
                units.append(line)
            else:
                units.append(["table", line])
            continue
        prefix, body, tail = MD_PREFIX.match(line).groups()
        prev = units[-1] if units else None
        if not prefix.strip() and isinstance(prev, list) and prev[0] != "table" and body:
            joiner = "" if re.search(r"[㐀-鿿]$", prev[2]) else " "  # 同一段折了行：接回去一起翻
            prev[2] += joiner + body.strip()
            continue
        units.append(["line", prefix, body, tail])

    texts = []
    for u in units:
        if isinstance(u, list):
            if u[0] == "table":
                texts += [c.strip() for c in u[1].strip().strip("|").split("|")]
            else:
                texts.append(u[2])
    results = iter(translate_texts(translator, texts, on_progress, MD_NOTE))

    out = []
    for u in units:
        if not isinstance(u, list):
            out.append(u)
        elif u[0] == "table":
            cells = [c.strip() for c in u[1].strip().strip("|").split("|")]
            new = [next(results) for _ in cells]
            if bilingual:  # 双语表格：同一格里原文换行接译文，表格结构不变
                new = [c if c == t else f"{c}<br>{t}" for c, t in zip(cells, new)]
            out.append("| " + " | ".join(new) + " |")
        else:
            _, prefix, body, tail = u
            result = next(results)
            if bilingual and result != body:  # 原文一行、译文一行；标题、引用保留标记，列表的译文缩进到条目里
                out += [prefix + body + tail, ""]
                keep = prefix.strip().startswith(("#", ">"))
                out.append((prefix if keep else re.sub(r"\S", " ", prefix)) + result)
            else:
                out.append(prefix + result + tail)
    dst = output_path(src, bilingual)
    dst.write_text("\n".join(out), encoding="utf-8")
    return dst


# ---------- HTML / ePub ----------

SKIP_TAGS = {"script", "style", "code", "pre", "kbd", "samp", "var", "noscript", "template", "svg", "math"}
INLINE_TAGS = {"a", "abbr", "b", "bdi", "bdo", "cite", "data", "dfn", "em", "font", "i", "mark", "q", "rp", "rt",
               "ruby", "s", "small", "span", "strong", "sub", "sup", "time", "u", "wbr", "br", "img", "label"}
TEXT_ATTRS = ("alt", "title", "placeholder", "aria-label")


def nodes_block(node):
    """这段文字所在的块（段落、标题、格子……），加粗、链接这些行内标签往上跳过。"""
    return next((p for p in node.parents if p.name and p.name not in INLINE_TAGS), None)


class Markup:
    """一份 HTML/XHTML 里要翻的文字：按块（段落、标题、表格格子……）收集，块里有加粗、链接时带标记。"""
    target_lang = ""

    def __init__(self, data, xml=False):
        from bs4 import BeautifulSoup
        self.xml = xml
        self.soup = BeautifulSoup(data, "xml" if xml else "html.parser")
        self.groups = []  # [(文字节点列表)]
        self.attrs = []  # [(标签, 属性名)]
        blocks = {}
        from bs4 import Comment, Declaration, Doctype, ProcessingInstruction, CData
        for node in self.soup.find_all(string=True):
            if isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction, CData)) or not node.strip():
                continue
            if any(p.name in SKIP_TAGS for p in node.parents if p.name):
                continue
            block = nodes_block(node)
            if block is None or block.name in ("[document]", "head"):
                continue
            blocks.setdefault(id(block), []).append(node)
        self.groups = list(blocks.values())
        for tag in self.soup.find_all(True):
            for attr in TEXT_ATTRS:
                if isinstance(tag.get(attr), str) and tag[attr].strip():
                    self.attrs.append((tag, attr))
            if tag.name == "meta" and str(tag.get("name", "")).lower() in ("description", "keywords") \
                    and tag.get("content"):
                self.attrs.append((tag, "content"))

    def sources(self):
        out = []
        for nodes in self.groups:
            if len(nodes) == 1:
                out.append(str(nodes[0]).strip())
            else:
                out.append("".join(f"<x{i}>{html.escape(str(n).strip(), quote=False)}</x{i}>"
                                   for i, n in enumerate(nodes, 1)))
        return out + [str(tag[attr]).strip() for tag, attr in self.attrs]

    def apply(self, results, bilingual=False):
        for nodes, result in zip(self.groups, results):
            if len(nodes) == 1:
                texts = [result]
            else:
                found = {int(m.group(1)): html.unescape(m.group(2)) for m in TAG_RE.finditer(result)}
                if sorted(found) == list(range(1, len(nodes) + 1)):
                    texts = [found[i] for i in range(1, len(nodes) + 1)]
                else:  # 标记对不上：整块译文放进第一个节点，其余清空（格式丢一点，但字不会丢）
                    texts = [TAG_RE.sub(lambda m: m.group(2), result)] + [""] * (len(nodes) - 1)
            texts = [keep_space(str(n), t) if t else "" for n, t in zip(nodes, texts)]
            texts = spaced(texts, self.target_lang, [n.parent is nodes_block(n) for n in nodes])
            for node, text in zip(nodes, texts):
                node.replace_with(text)
        for (tag, attr), result in zip(self.attrs, results[len(self.groups):]):
            tag[attr] = result
        return str(self.soup)

    def set_lang(self, target_lang):
        """<html lang="zh"> 改成目标语言，浏览器和读屏软件才知道是什么语言。"""
        root = self.soup.find("html")
        if root is not None and root.get("lang") and target_lang in LANG_CODES:
            root["lang"] = LANG_CODES[target_lang]


LANG_CODES = {"英文": "en", "中文": "zh", "日文": "ja", "韩文": "ko", "法文": "fr", "德文": "de",
              "西班牙文": "es", "俄文": "ru"}


def translate_html(src, translator, bilingual=False, on_progress=None):
    doc = Markup(read_text(src))
    doc.target_lang = translator.target_lang
    doc.set_lang(translator.target_lang)
    results = translate_texts(translator, doc.sources(), on_progress, "原文来自网页。")
    dst = output_path(src)
    dst.write_text(doc.apply(results), encoding="utf-8")
    return dst


def translate_epub(src, translator, bilingual=False, on_progress=None):
    """ePub 是一个 zip：里面的 XHTML 章节、目录（toc.ncx / nav）、书名都翻，图片、样式原样复制。"""
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        data = {i.filename: z.read(i.filename) for i in infos}
    docs = {}
    for name, raw in data.items():
        low = name.lower()
        if low.endswith((".xhtml", ".html", ".htm")):
            docs[name] = Markup(raw, xml=True)
            docs[name].target_lang = translator.target_lang
        elif low.endswith(".ncx"):
            docs[name] = NcxText(raw)
        elif low.endswith(".opf"):
            docs[name] = OpfText(raw)
            docs[name].target_lang = translator.target_lang
    names = list(docs)
    sources = [docs[n].sources() for n in names]
    flat = translate_texts(translator, [t for s in sources for t in s], on_progress, "原文来自一本电子书。")
    pos = 0
    for name, s in zip(names, sources):
        data[name] = docs[name].apply(flat[pos:pos + len(s)]).encode("utf-8")
        pos += len(s)

    dst = output_path(src)
    with zipfile.ZipFile(dst, "w") as z:
        for info in infos:  # mimetype 必须是第一个、不压缩，阅读器才认
            kind = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(info.filename, data[info.filename], compress_type=kind)
    return dst


class NcxText(Markup):
    """toc.ncx 目录：只翻 <text> 里的标题。"""

    def __init__(self, data):
        from bs4 import BeautifulSoup
        self.xml = True
        self.soup = BeautifulSoup(data, "xml")
        self.groups = [[t.string] for t in self.soup.find_all("text") if t.string and t.string.strip()]
        self.attrs = []


class OpfText(NcxText):
    """content.opf：只翻书名、简介、主题。"""

    def __init__(self, data):
        from bs4 import BeautifulSoup
        self.xml = True
        self.soup = BeautifulSoup(data, "xml")
        self.groups = [[t.string] for t in self.soup.find_all(["title", "description", "subject"])
                       if t.string and t.string.strip()]
        self.attrs = []

    def apply(self, results, bilingual=False):
        lang = self.soup.find("language")
        if lang is not None and getattr(self, "target_lang", None) in LANG_CODES:
            lang.string = LANG_CODES[self.target_lang]
        return super().apply(results, bilingual)


# ---------- JSON ----------

LOOKS_LIKE_CODE = re.compile(r"^(https?://|www\.|[\w.+-]+@[\w-]+\.|/|[A-Za-z]:\\|#[0-9a-fA-F]{3,8}$)|^[\w.\-/:#]+$")


def translate_json(src, translator, bilingual=False, on_progress=None):
    """只翻字符串值，键名、数字、网址、路径、看起来像代码的不动。"""
    raw = read_text(src)
    data = json.loads(raw)
    found = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and v.strip() and not (LOOKS_LIKE_CODE.match(v.strip()) and " " not in v.strip()
                                                         and not re.search(r"[㐀-鿿]", v)):
            found.append(v)

    walk(data)
    table = dict(zip(found, translate_texts(translator, found, on_progress)))

    def rebuild(v):
        if isinstance(v, dict):
            return {k: rebuild(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rebuild(x) for x in v]
        if isinstance(v, str) and v in table:
            return keep_space(v, table[v])
        return v

    dst = output_path(src)
    indent = 2 if "\n" in raw.strip() else None
    dst.write_text(json.dumps(rebuild(data), ensure_ascii=False, indent=indent), encoding="utf-8")
    return dst


# ---------- Excel ----------

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def translate_xlsx(src, translator, bilingual=False, on_progress=None):
    """直接改 xlsx 里存文字的地方（共享字符串表、单元格里的文字），公式、数字、图表、图片、格式都不碰。
    工作表名也翻，公式、图表、名称里引用到的表名跟着改。"""
    from lxml import etree
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        data = {i.filename: z.read(i.filename) for i in infos}
    trees, items = {}, []  # items：(那一格的 <t> 元素列表)
    for name in data:
        if name == "xl/sharedStrings.xml" or (name.startswith("xl/worksheets/") and name.endswith(".xml")):
            tree = etree.fromstring(data[name])
            trees[name] = tree
            holders = tree.iter(f"{NS}si") if name.endswith("sharedStrings.xml") else tree.iter(f"{NS}is")
            for holder in holders:
                ts = [t for t in holder.iter(f"{NS}t") if t.getparent().tag != f"{NS}rPh"]  # 拼音注音不翻
                if any((t.text or "").strip() for t in ts):
                    items.append(ts)
    sources = []
    for ts in items:
        if len(ts) == 1:
            sources.append(ts[0].text.strip())
        else:
            sources.append("".join(f"<x{i}>{html.escape(t.text or '', quote=False)}</x{i}>"
                                   for i, t in enumerate(ts, 1)))
    book = etree.fromstring(data["xl/workbook.xml"]) if "xl/workbook.xml" in data else None
    sheets = list(book.iter(f"{NS}sheet")) if book is not None else []
    results = translate_texts(translator, sources + [sh.get("name") for sh in sheets], on_progress,
                              "原文来自 Excel 表格的单元格和工作表名。")
    for ts, result in zip(items, results):
        if len(ts) == 1:
            texts = [keep_space(ts[0].text, result)]
        else:
            found = {int(m.group(1)): html.unescape(m.group(2)) for m in TAG_RE.finditer(result)}
            texts = ([found[i] for i in range(1, len(ts) + 1)] if sorted(found) == list(range(1, len(ts) + 1))
                     else [TAG_RE.sub(lambda m: m.group(2), result)] + [""] * (len(ts) - 1))
            texts = spaced(texts, translator.target_lang)
        for t, text in zip(ts, texts):
            t.text = text
            if text != text.strip():
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    renames = sheet_names([sh.get("name") for sh in sheets], results[len(sources):])
    if renames:
        trees["xl/workbook.xml"] = book
        for sh in sheets:
            sh.set("name", renames.get(sh.get("name"), sh.get("name")))
        formula_tags = {f"{NS}f", f"{NS}formula", f"{NS}formula1", f"{NS}formula2", f"{NS}definedName"}
        for tree in trees.values():
            for el in tree.iter():
                if el.tag in formula_tags and el.text:
                    el.text = rename_refs(el.text, renames)
        for name in data:  # 图表里的数据来源也写着表名
            if name.startswith("xl/") and name.endswith(".xml") and name not in trees and b"<c:f>" in data[name]:
                text = data[name].decode("utf-8")
                text = re.sub(r"(<c:f>)(.*?)(</c:f>)",
                              lambda m: m.group(1) + html.escape(rename_refs(html.unescape(m.group(2)), renames),
                                                                 quote=False) + m.group(3), text)
                data[name] = text.encode("utf-8")
    for name, tree in trees.items():
        data[name] = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)

    dst = output_path(src)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for info in infos:
            z.writestr(info.filename, data[info.filename])
    return dst


def sheet_names(old, new):
    """译好的工作表名按 Excel 的规矩整理：最多 31 个字符，不能有 \\ / ? * [ ] :，不能重名。返回 {旧名: 新名}。"""
    renames, used = {}, {n.lower() for n in old}
    for o, n in zip(old, new):
        n = re.sub(r"[\\/?*\[\]:]", " ", n or "").strip().strip("'")[:31].strip()
        if not n or n == o:
            continue
        base, k = n, 2
        while n.lower() in used:
            tail = f" ({k})"
            n = base[:31 - len(tail)] + tail
            k += 1
        used.discard(o.lower())
        used.add(n.lower())
        renames[o] = n
    return renames


def rename_refs(formula, renames):
    """公式里的 '参数表'!A1、参数表!A1 换成新表名（新表名一律加单引号，带空格也不会出错）。"""
    for old, new in renames.items():
        quoted = "'" + new.replace("'", "''") + "'!"
        formula = formula.replace("'" + old.replace("'", "''") + "'!", quoted)
        formula = re.sub(r"(?<![\w'.])" + re.escape(old) + "!", lambda m: quoted, formula)
    return formula


HANDLERS = {".txt": translate_txt, ".md": translate_md, ".markdown": translate_md, ".html": translate_html,
            ".htm": translate_html, ".epub": translate_epub, ".json": translate_json, ".xlsx": translate_xlsx}


def translate_file(src, translator, bilingual=False, on_progress=None):
    """按扩展名选对应的翻译方法，返回输出文件路径。"""
    suffix = Path(src).suffix.lower()
    if suffix == ".xls":
        raise ValueError("旧版 .xls 文件请先在 Excel 里另存为 .xlsx 再翻译。")
    if suffix not in HANDLERS:
        raise ValueError(f"暂不支持 {suffix} 文件。")
    return HANDLERS[suffix](src, translator, bilingual=bilingual, on_progress=on_progress)
