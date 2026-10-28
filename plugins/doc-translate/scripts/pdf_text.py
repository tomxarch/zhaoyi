"""PDF translation that keeps the page as it is.

Each page is checked first:
- "text" page (exported from Word or a layout program, text can be selected): the original text is removed from
  the page content, everything else (images, lines, table borders, backgrounds) stays vector, and the translation
  is written at the same place with the original size, weight and colour. Shrinks only when it does not fit.
- "scan" page (the page is one big image): goes through the OCR overlay of the engine (recognise, cover, write,
  seals and signatures kept on top).
A file may mix both; each page is handled its own way.

Needs: pdfplumber, pikepdf, pypdf, reportlab (+ the engine's OCR deps for scan pages).
"""
import re
import statistics
from pathlib import Path

LABEL = re.compile(r"^[A-Za-z][\w .'/-]{0,24}:\s|^[\u4e00-\u9fff]{1,8}[:：]")
CONTACT = re.compile(r"^\s*(tel|phone|fax|e-?mail|mail|web|website|www\.|http|add(ress)?|mob(ile)?|电话|传真|邮箱|网址|地址|手机)\b",
                     re.I)
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
LIST_MARK = re.compile(r"^\s*([•·▪●○■□◆◇►✓]|([-–]|\(?\d{1,3}[.)、]|\(?[a-zA-Z][.)]|[（(][一二三四五六七八九十\d]+[)）]"
                       r"|[一二三四五六七八九十]+、)\s)")


# ---------- which pages are text, which are scans ----------

def page_kind(page):
    """'text', 'scan' or 'empty' for a pdfplumber page."""
    area = float(page.width * page.height) or 1.0
    big_image = any((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) > 0.6 * area for im in page.images)
    chars = page.chars
    if big_image:
        return "scan"  # a scan, maybe with an invisible OCR text layer: only the image shows, so work on the image
    if len(chars) < 3:
        return "scan" if page.images else "empty"
    broken = sum("(cid:" in c["text"] for c in chars)
    sideways = sum(not c.get("upright", True) for c in chars)
    if broken > 0.1 * len(chars) or sideways > 0.2 * len(chars):
        return "scan"  # no usable text (fonts without a unicode map) or mostly vertical text
    return "text"


# ---------- reading the text into blocks ----------

def _cells(page):
    cells = []
    try:
        for t in page.find_tables():
            cells += [c for c in t.cells if c]
    except Exception:  # table finding is best effort
        pass
    return cells


def _cell_of(word, cells):
    cx, cy = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
    inside = [c for c in cells if c[0] - 1 <= cx <= c[2] + 1 and c[1] - 1 <= cy <= c[3] + 1]
    return min(inside, key=lambda c: (c[2] - c[0]) * (c[3] - c[1])) if inside else None


def _join(a, b):
    if not a:
        return b
    if a.endswith("-") and b[:1].islower():
        return a[:-1] + b
    if CJK.search(a[-1:]) or CJK.search(b[:1]):
        return a + b
    return a + " " + b


def blocks(page):
    """Paragraph blocks of a text page: {text, x0, top, x1, bottom, size, bold, color, cell, align}."""
    words = page.extract_words(extra_attrs=["fontname", "size", "non_stroking_color"], keep_blank_chars=True,
                               x_tolerance=1.5, y_tolerance=2)
    words = [w for w in words if w["text"].strip() and w.get("upright", True)]
    cells = _cells(page)
    for w in words:
        w["cell"] = _cell_of(w, cells)
    # 1. words -> line pieces (same row, same cell, no big gap)
    words.sort(key=lambda w: (round(w["top"] / 2), w["x0"]))
    pieces = []
    for w in words:
        last = pieces[-1] if pieces else None
        # inside a table cell a row is one piece (justified text has wide gaps); outside, a wide gap is a new column
        if (last and last["cell"] == w["cell"] and abs(last["top"] - w["top"]) < 0.5 * w["size"]
                and 0 <= w["x0"] - last["x1"] < (page.width if w["cell"] else 2.0 * w["size"])):
            tight = w["x0"] - last["x1"] < 0.1 * w["size"] or CJK.search(last["text"][-1:] + w["text"][:1])
            last["text"] += ("" if last["text"].endswith(" ") or tight else " ") + w["text"]
            last["x1"], last["bottom"] = max(last["x1"], w["x1"]), max(last["bottom"], w["bottom"])
            last["words"].append(w)
        else:
            pieces.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"],
                           "cell": w["cell"], "words": [w]})
    for p in pieces:
        # bullets from Symbol/Wingdings fonts come out as private-use characters: make them a plain bullet
        p["text"] = re.sub(r"\s+", " ", re.sub(r"[\uf000-\uf0ff]", "•", p["text"])).strip()
        sizes = [w["size"] for w in p["words"]]
        p["size"] = statistics.median(sizes)
        chars = sum(len(w["text"]) for w in p["words"]) or 1
        p["bold"] = sum(len(w["text"]) for w in p["words"] if re.search(r"bold|black|heavy", w["fontname"], re.I)) > chars / 2
        p["color"] = p["words"][0].get("non_stroking_color")
    # 2. line pieces -> paragraphs (next line close below, same cell, same size, overlapping columns).
    # Only a line that runs (almost) to the right edge of its column was wrapped; a line that stops early
    # (address lines, "Date / Valid until" labels, list items) ends its paragraph.
    pieces.sort(key=lambda p: (p["top"], p["x0"]))

    def column_right(p):
        if p["cell"]:
            return p["cell"][2] - 2
        return max(o["x1"] for o in pieces if o["cell"] is None and abs(o["x0"] - p["x0"]) < 3 * p["size"])

    def first_word(p):
        # a wrapped line breaks because its next word did not fit; if it would have fitted, the break was hard
        w = p["words"][0]
        if CJK.search(w["text"][:1]):
            return p["size"]
        return w["x1"] - w["x0"]

    for p in pieces:
        p["col_right"] = column_right(p)
    paras = []
    for p in pieces:
        target = None
        for q in reversed(paras[-6:]):
            gap = p["top"] - q["bottom"]
            # in a cell the right edge is known, so "the next word did not fit" is enough; in free text the
            # column edge is guessed from the widest line, so the line must also run close to it
            near_edge = q["cell"] or q["last_x1"] >= q["col_right"] - max(3 * q["size"], 0.15 * (q["col_right"] - q["x0"]))
            wrapped = (near_edge and q["last_x1"] + 0.3 * q["size"] + first_word(p) > q["col_right"] - 1
                       and not re.search(r"[:：。.!?！？;；]$", q["last_text"])
                       and not CONTACT.match(p["text"]) and not LABEL.match(p["text"]))
            if (q["cell"] == p["cell"] and abs(q["size"] - p["size"]) < 0.6 and q["bold"] == p["bold"] and wrapped
                    and -0.2 * p["size"] < gap < 0.75 * p["size"] and p["x0"] < q["x1"] and p["x1"] > q["x0"]
                    and abs(p["x0"] - q["x0"]) < 3 * p["size"] and not LIST_MARK.match(p["text"])):
                target = q
                break
        if target:
            target["text"] = _join(target["text"], p["text"])
            target["last_x1"], target["last_text"] = p["x1"], p["text"]
            target["col_right"] = max(target["col_right"], p["col_right"])
            target["lines"] += 1
            target["words"] = target["words"] + p["words"]
            for k, f in (("x0", min), ("x1", max), ("bottom", max)):
                target[k] = f(target[k], p[k])
        else:
            paras.append(dict(p, lines=1, last_x1=p["x1"], last_text=p["text"]))
    for b in paras:
        # a bullet stays where it was and is drawn in a face that has it; only the words after it are translated
        m = re.match(r"^([•·▪●○■□◆◇►✓])\s*", b["text"])
        ws = b.get("words") or []
        if m and len(ws) > 1:
            first = next((w for w in ws if w["text"].strip(m.group(1) + " ")), None)
            text_x0 = first["x0"] if first and first is not ws[0] else ws[0]["x0"] + 1.5 * b["size"]
            b["mark"], b["mark_x"], b["text"], b["x0"] = m.group(1), b["x0"], b["text"][m.end():], text_x0
        elif m:
            b["mark"], b["mark_x"], b["text"] = m.group(1), b["x0"], b["text"][m.end():]
            b["x0"] += 1.5 * b["size"]
        # where each original word sat: text that stays unchanged (numbers, codes, names) is put back exactly there
        b["spans"] = [(w["x0"], w["top"], w["text"], w["size"]) for w in b.pop("words", None) or []]
        c = b["cell"]
        left, right = (b["x0"] - c[0], c[2] - b["x1"]) if c else (0, 0)
        if c and min(left, right) > max(4, 0.1 * (c[2] - c[0])) and abs(left - right) < 0.25 * (left + right):
            b["align"] = "center"
        elif c and b["lines"] == 1 and right < max(4, 0.08 * (c[2] - c[0])) and left > 3 * right + 6:
            b["align"] = "right"
        else:
            b["align"] = "left"
        above, below = (b["top"] - c[1], c[3] - b["bottom"]) if c else (0, 0)
        b["vmid"] = bool(c) and min(above, below) > 0.3 * b["size"] and abs(above - below) < 0.35 * (above + below)
    paras.sort(key=lambda b: (b["top"], b["x0"]))
    return paras


def char_limit(b, page):
    """Rough number of characters that fit in a short label's room (cells only). None for running text."""
    if not b["cell"] or b["lines"] > 3:
        return None
    c = b["cell"]
    size = b["size"] * 0.85
    per_line = (c[2] - c[0] - 4) / (0.5 * size)
    rows = max(1, int((c[3] - c[1] - 2) / (1.2 * size)))
    return max(4, int(per_line * rows))


# ---------- writing ----------

def strip_text(pdf, page):
    """Remove every text object (BT ... ET) from the page and from the forms it uses; keep everything else."""
    import pikepdf

    def clean(stream_owner, seen):
        ops = pikepdf.parse_content_stream(stream_owner)
        kept, inside = [], False
        for operands, op in ops:
            name = str(op)
            if name == "BT":
                inside = True
                continue
            if name == "ET":
                inside = False
                continue
            if not inside:
                kept.append((operands, op))
        data = pikepdf.unparse_content_stream(kept)
        res = stream_owner.get("/Resources") if hasattr(stream_owner, "get") else None
        if res is not None and "/XObject" in res:
            for _, xo in res.XObject.items():
                if xo.get("/Subtype") == "/Form" and xo.objgen not in seen:
                    seen.add(xo.objgen)
                    xo.write(clean(xo, seen))
        return data

    seen = set()
    data = clean(page, seen)
    page.Contents = pdf.make_stream(data)


def _colour(pdf_canvas, color):
    try:
        if isinstance(color, (list, tuple)):
            if len(color) == 1:
                pdf_canvas.setFillGray(float(color[0]))
                pdf_canvas.setStrokeGray(float(color[0]))
            elif len(color) == 3:
                pdf_canvas.setFillColorRGB(*map(float, color))
                pdf_canvas.setStrokeColorRGB(*map(float, color))
            elif len(color) == 4:
                pdf_canvas.setFillColorCMYK(*map(float, color))
                pdf_canvas.setStrokeColorCMYK(*map(float, color))
            return
    except (TypeError, ValueError):
        pass
    pdf_canvas.setFillGray(0)
    pdf_canvas.setStrokeGray(0)


def _draw(pdf_canvas, x, y, line, font, size, fake_bold):
    """Draw one line; fake_bold thickens a face that has no bold file by stroking the glyph outlines too."""
    if not fake_bold:
        pdf_canvas.setFont(font, size)
        pdf_canvas.drawString(x, y, line)
        return
    pdf_canvas.saveState()
    pdf_canvas.setLineWidth(size * 0.04)
    t = pdf_canvas.beginText(x, y)
    t.setFont(font, size)
    t._code.append("2 Tr")
    t.textOut(line)
    t._code.append("0 Tr")
    pdf_canvas.drawText(t)
    pdf_canvas.restoreState()


def _room(i, bs, page_w, right_edge):
    """Box the translation of block i may use: its own box, widened to the right and down while nothing is there."""
    b = bs[i]
    if b["cell"]:  # a cell may hold several blocks: each gets the cell's width, down to the next block in it
        c = b["cell"]
        mates = [o for o in bs if o["cell"] == c]
        if len(mates) == 1:
            return c[0] + 2, c[1] + 1, c[2] - 2, c[3] - 1
        below = min([o["top"] for o in mates if o["top"] > b["top"] + 1] + [c[3] - 1])
        return c[0] + 2, b["top"] - 0.5, c[2] - 2, below - 0.5
    x0, top, x1, bottom = b["x0"], b["top"], b["x1"], b["bottom"]
    right = right_edge
    below = bottom + 2 * b["size"]
    for j, o in enumerate(bs):
        if j == i:
            continue
        rows_overlap = o["top"] < bottom and o["bottom"] > top
        if rows_overlap and o["x0"] >= x1 - 1:
            right = min(right, o["x0"] - 3)
        cols_overlap = o["x0"] < max(right, x1) and o["x1"] > x0
        if cols_overlap and o["top"] >= bottom - 1:
            below = min(below, o["top"] - 1)
    return x0, top, max(x1, right), max(bottom, below)


CJK_TTF = [  # (regular, bold): Microsoft YaHei, SimHei on Windows; Noto / WenQuanYi on Linux
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
    ("C:/Windows/Fonts/msyh.ttf", "C:/Windows/Fonts/msyhbd.ttf"),
    ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
]
LATIN_TTF = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _faces(candidates, prefix, fallback):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in candidates:
        if Path(regular).exists():
            names = []
            for tag, path in (("", regular), ("-Bold", bold if Path(bold).exists() else regular)):
                name = prefix + tag
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
                names.append(name)
            if names[1] != names[0] and bold == regular:
                names[1] = names[0]
            return names
    return fallback


def cjk_faces():
    """(regular, bold) font names for CJK text. Falls back to reportlab's built-in Song (thin, no bold)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from pdf_overlay import CJK_FALLBACK
    try:
        return _faces(CJK_TTF, "ZT-CJK", None) or _cid(CJK_FALLBACK)
    except Exception:  # a broken font file must not stop the job
        return _cid(CJK_FALLBACK)


def _cid(name):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont(name))
    return [name, name]


def latin_faces():
    return _faces(LATIN_TTF, "ZT-Latin", ["Helvetica", "Helvetica-Bold"])


def write_pages(src, dst, translate, target_lang, text_pages, on_log=print):
    """Translate the text pages of src into dst. translate(list_of_texts, where) -> list of translations."""
    import io

    import pdfplumber
    import pikepdf
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    from pdf_overlay import CN_PUNCT, LATIN_LANGS, fit, register_fonts

    fonts = register_fonts(target_lang)
    cjk_fonts = cjk_faces()
    plans = {}
    with pdfplumber.open(str(src)) as doc:
        for n in text_pages:
            page = doc.pages[n]
            bs = blocks(page)
            texts = [b["text"] for b in bs]
            got = translate(texts, f"page {n + 1}", [char_limit(b, page) for b in bs])
            right_edge = max([b["x1"] for b in bs] + [page.width * 0.85])
            plans[n] = (page.width, page.height, page.bbox, bs, got, right_edge)

    pdf = pikepdf.open(str(src))
    for n in text_pages:
        strip_text(pdf, pdf.pages[n])
    stripped = io.BytesIO()
    pdf.save(stripped)
    reader = PdfReader(stripped)
    writer = PdfWriter(clone_from=reader)
    crowded = 0
    for n, (w, h, bbox, bs, got, right_edge) in plans.items():
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        for i, (b, text) in enumerate(zip(bs, got)):
            text = text or b["text"]
            if target_lang in LATIN_LANGS:
                text = re.sub(r" {2,}", " ", text.translate(CN_PUNCT)).strip()
            x0, top, x1, bottom = _room(i, bs, w, right_edge)
            if CJK.search(text):  # Chinese/Japanese/Korean text: a real CJK face with the original weight
                font = cjk_fonts[1] if b["bold"] else cjk_fonts[0]
            elif target_lang in LATIN_LANGS:
                font = fonts[1] if b["bold"] else fonts[0]
            else:  # Latin text (names, codes) left in a CJK translation: keep it in a Latin face, not the CJK one
                font = latin_faces()[1 if b["bold"] else 0]
            if text.strip() == b["text"].strip() and not b.get("mark") and b["spans"]:
                _colour(c, b["color"])
                for k, (sx, stop, stext, ssize) in enumerate(b["spans"]):
                    sfont = font if CJK.search(stext) or target_lang in LATIN_LANGS else latin_faces()[1 if b["bold"] else 0]
                    # our face may be wider than the original one: never run into the next word or out of the box
                    nxt = [o[0] for o in b["spans"][k + 1:] if abs(o[1] - stop) < 0.5 * ssize and o[0] > sx]
                    room_w = (min(nxt) - 0.25 * ssize if nxt else max(x1, b["x1"])) - sx
                    s1 = ssize
                    while s1 > 0.6 * ssize and stringWidth(stext, sfont, s1) > room_w:
                        s1 -= 0.25
                    _draw(c, sx - bbox[0], h - (stop - bbox[1]) - ssize * 0.88, stext, sfont, s1, False)
                continue
            pitch = (b["bottom"] - b["top"] - b["size"]) / (b["lines"] - 1) + b["size"] if b["lines"] > 1 else None
            size, lines = fit(text, font, x1 - x0, bottom - top + 0.2 * b["size"], b["size"], pitch=pitch)
            if b["lines"] == 1 and len(lines) > 1:  # was one line: rather a little smaller than broken in two
                s1 = b["size"]
                while s1 > 0.75 * b["size"] and stringWidth(text, font, s1) > x1 - x0:
                    s1 -= 0.25
                if stringWidth(text, font, s1) <= x1 - x0:
                    size, lines = s1, [text]
            if size < 0.7 * b["size"] and font in fonts[:2]:  # try the narrow face before shrinking further
                narrow = fonts[3] if b["bold"] else fonts[2]
                if narrow != font:
                    s2, l2 = fit(text, narrow, x1 - x0, bottom - top + 0.2 * b["size"], b["size"], pitch=pitch)
                    if s2 > size:
                        font, size, lines = narrow, s2, l2
            if size < 0.7 * b["size"]:
                crowded += 1
            lead = min(max((pitch or size * 1.15) / size, 1.05), 1.6)
            c.setFont(font, size)
            _colour(c, b["color"])
            # pdfplumber measures from the top of the crop box; reportlab from the bottom of the page
            y = h - (top - bbox[1]) - size * 0.88
            if b["cell"] and len(lines) * size * lead < (bottom - top) * 0.9 and (b["lines"] == 1 or b["vmid"]):
                y = h - (top - bbox[1]) - ((bottom - top) - size * (1 + lead * (len(lines) - 1))) / 2 - size * 0.88
            if b.get("mark"):
                c.setFont(latin_faces()[0], size)
                _colour(c, b["color"])
                c.drawString(b["mark_x"] - bbox[0], h - (top - bbox[1]) - size * 0.88, b["mark"])
            fake_bold = b["bold"] and font in (cjk_fonts[0], fonts[0]) and font == (
                cjk_fonts[1] if font == cjk_fonts[0] else fonts[1])
            for line in lines:
                x = x0 - bbox[0]
                if b["align"] == "center":
                    x = (x0 + x1) / 2 - bbox[0] - stringWidth(line, font, size) / 2
                elif b["align"] == "right":
                    x = b["x1"] - bbox[0] - stringWidth(line, font, size)
                _draw(c, x, y, line, font, size, fake_bold)
                y -= size * lead
        c.save()
        overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        page = writer.pages[n]
        page.merge_translated_page(overlay, float(page.mediabox.left), float(page.mediabox.bottom))
    with open(dst, "wb") as f:
        writer.write(f)
    if crowded:
        on_log(f"{crowded} blocks had to be set noticeably smaller than the original; check them.")
    return dst


def translate_pdf(src, engine, on_log=print, mode="auto"):
    """Translate a PDF page by page: text pages keep their vector page, scan pages go through the OCR overlay.
    engine is the zt.py Engine (records texts on extract, answers them on apply). Returns the output path."""
    import shutil
    import tempfile

    import pdfplumber
    import pikepdf

    src = Path(src)
    with pdfplumber.open(str(src)) as doc:
        kinds = [page_kind(p) if mode == "auto" else mode for p in doc.pages]
        rotated = [int(p.page_obj.attrs.get("Rotate", 0) or 0) % 360 for p in doc.pages]
    kinds = ["scan" if k == "text" and r else k for k, r in zip(kinds, rotated)]
    text_pages = [i for i, k in enumerate(kinds) if k == "text"]
    scan_pages = [i for i, k in enumerate(kinds) if k == "scan"]
    on_log(f"{len(text_pages)} text pages (kept as vector), {len(scan_pages)} scanned pages (OCR overlay)")
    dst = src.with_name(f"{src.stem}_译文.pdf")
    work = Path(tempfile.mkdtemp(prefix="ztpdf_"))

    def ask(texts, where, limits):
        engine.where = where
        engine.limits = {t: l for t, l in zip(texts, limits) if l}
        from pdf_overlay import needs_translation, mostly_cjk
        cjk = (getattr(engine, "source_lang", None) or "") in ("中文", "日文", "韩文") or mostly_cjk(texts)
        todo = [t for t in texts if needs_translation(t, engine.target_lang, cjk)]
        got = dict(zip(todo, engine.translate_many(todo)))
        return [got.get(t, t) for t in texts]

    text_out = work / "text.pdf"
    if text_pages:
        write_pages(src, text_out, ask, engine.target_lang, text_pages, on_log)
    else:
        shutil.copy2(src, text_out)
    if scan_pages:
        import pdf_overlay
        part = work / "scan.pdf"
        with pikepdf.open(str(src)) as whole:
            out = pikepdf.new()
            for i in scan_pages:
                out.pages.append(whole.pages[i])
            out.save(part)
        engine.page_map = scan_pages  # so "where" names the page number in the whole file
        done = Path(pdf_overlay.translate_pdf_overlay(part, engine, None, None, on_log=on_log))
        engine.page_map = None
        final = pikepdf.open(str(text_out))
        with pikepdf.open(str(done)) as scans:
            for k, i in enumerate(scan_pages):
                final.pages.insert(i, scans.pages[k])
                del final.pages[i + 1]
            final.save(dst)
        final.close()
    else:
        shutil.copy2(text_out, dst)
    return dst
