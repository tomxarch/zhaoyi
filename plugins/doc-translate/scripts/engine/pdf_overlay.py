"""PDF 进、PDF 出（像 DeepL 那样）：把译文盖在原页面的文字位置上。

每页分三步：
1. 识别：页面渲染成图片，用本地 OCR（RapidOCR，Apache-2.0）认出每一行的位置和文字，再合成段落；
   没装 RapidOCR 时交给视觉模型认字（会偶尔漏字，所以认完再查一遍）；
2. 翻译：一页的文字块编号后一次交给翻译模型（带场景、术语表），译完检查，还留着原文的再补翻；
3. 盖字：原图打底，用背景色盖住原文（表格线留着），在同一位置写上译文。
图片、logo、印章都是原图里的，位置大小不变。扫描件和普通 PDF 都能用。
"""
import base64
import io
import json
import os
import re
import time
import unicodedata
from pathlib import Path

from providers import ONE_RULE, capitalize_first, glossary_prompt, one_choice, with_retry

OCR_PROMPT = ("这是一份文档的一页。找出页面上所有文字块，原样抄写，不要翻译。"
              "每个段落、标题、表格单元格、图注、图中标签各算一块，同一段落的多行合成一块，不同列分开。"
              "box 是这块文字的位置 [ymin, xmin, ymax, xmax]，按 0 到 1000 归一化，要把这块文字完整框住。"
              "不要漏掉任何文字；印章里的字不用管。不要用 LaTeX，符号直接写成 φ、×、±、° 这样的字符。"
              "kind 标明类型：text（正文、标题）、table（表格单元格）、label（图片、线路图、尺寸图里的标注）、"
              "vertical（竖着写的字）、logo（logo、商标图形里的字）。"
              '只输出 JSON：{"blocks": [{"box": [ymin, xmin, ymax, xmax], "text": "原文", "kind": "text", '
              '"bold": false, "align": "left"}]}，align 是 left、center 或 right。')

BATCH_PROMPT = ("下面是一页文档里的文字块，JSON 格式：blocks 里键是编号或原文本身、值是原文；limits 是图和表格里的"
                "短标注在原位置大约能放下的最多字符数。把每一块翻译成{lang}，按原来的键输出 JSON：{{\"键\": \"译文\", ...}}。"
                "每块都必须完整、忠实地翻译，不能留原文，正文不要缩写、不要改写成口语；"
                "有 limits 的短标注尽量不超过：优先用简洁但完整的说法（可以分两行写），实在放不下才用行业通行的缩写，"
                "不要截断单词；只用大家都认识的缩写（No.、A、s、kV、AC），不要自造缩写（比如不要把 power-frequency current 缩成 PFA）。"
                "原文是扫描件认出来的，个别符号可能认错（比如 ~V 认成 -¥），按上下文纠正后再翻。"
                "纯数字、型号、单位原样保留；原文没有的编号、序号不要加。不要用 LaTeX。只输出 JSON。")

SHORTEN_PROMPT = ("下面这些是图和表格里的短标注，{lang}译文放回原位置时太长了。键是编号，值是 [译文, 最多字符数]。"
                  "把每条改得更短，尽量不超过最多字符数：先换更短的完整说法（去掉冠词、用简短的同义词），"
                  "还不够才用行业通行的缩写（不要自造缩写，不要截断单词），意思不能变。"
                  "按编号输出 JSON：{{\"1\": \"改短的译文\", ...}}。只输出 JSON。")

CHECK_PROMPT = ("下面是要翻译成{lang}的文字，JSON 格式：blocks 里键是编号，值是 [原文, 机器翻译的初稿]；"
                "limits 是图和表格里的短标注大约能放下的最多字符数。你来把关：对照原文检查初稿，"
                "只改有问题的：译错、漏译、术语不对、不符合上面的要求、太生硬读不通、短词按日常意思直译而不是行业说法"
                "（比如电气文件里法语 Unité 是单位、Cellule 是开关柜）。没问题的条目不要输出。"
                "按编号输出 JSON：{{\"3\": \"改好的完整译文\", ...}}，全都没问题就输出 {{}}。"
                "原文是扫描件认出来的，个别符号可能认错，按上下文纠正。不要用 LaTeX。只输出 JSON。")

# 排版可调的地方（用户提意见“表头字太小”时会调大）。min_table：表格里的字小于这么多磅，就请模型换短一点的说法
LAYOUT = {"min_table": 5.5}

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")  # 汉字、假名、韩文（不含全角标点）
LATIN_LANGS = ("英文", "法文", "德文", "西班牙文", "俄文")
CN_PUNCT = str.maketrans({"、": ", ", "，": ", ", "。": ". ", "；": "; ", "：": ": ", "（": " (", "）": ") ",
                          "【": "[", "】": "]", "“": '"', "”": '"', "‘": "'", "’": "'", "！": "! ", "？": "? ",
                          "～": "~", "—": "-", "－": "-", "／": "/", "　": " ",
                          # 英文字体里常没有的单位符号，换成普通写法，免得印出方框
                          "℃": "°C", "℉": "°F", "㎜": "mm", "㎝": "cm", "㎞": "km", "㎡": "m²", "㎏": "kg",
                          "№": "No.", "Ф": "Φ"})

# 字体：中日韩用 reportlab 自带的 CID 字体，其余用系统里的 TTF；遇到漏翻的中文用 CJK_FALLBACK 兜底
CID_FONTS = {"中文": "STSong-Light", "日文": "HeiseiKakuGo-W5", "韩文": "HYGothic-Medium"}
CJK_FALLBACK = "STSong-Light"
TTF_CANDIDATES = [  # (常规, 粗体, 窄体, 窄粗体)：窄体在实在放不下时才用
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf",
     "C:/Windows/Fonts/ARIALN.TTF", "C:/Windows/Fonts/ARIALNB.TTF"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"),
]


def strip_latex(text):
    from pdf_translate import strip_latex as strip
    return strip(text)


def register_fonts(target_lang):
    """返回 (常规, 粗体, 窄体, 窄粗体) 四个字体名；没有窄体就用常规的。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont(CJK_FALLBACK))
    if target_lang in CID_FONTS:
        name = CID_FONTS[target_lang]
        pdfmetrics.registerFont(UnicodeCIDFont(name))
        return name, name, name, name
    from reportlab.pdfbase.ttfonts import TTFont
    for paths in TTF_CANDIDATES:
        if Path(paths[0]).exists():
            names = []
            for name, path in zip(("ZY", "ZY-Bold", "ZY-Narrow", "ZY-NarrowBold"), paths):
                if Path(path).exists():
                    pdfmetrics.registerFont(TTFont(name, path))
                    names.append(name)
                else:
                    names.append(names[-2] if len(names) >= 2 else names[0])
            return tuple(names)
    return "Helvetica", "Helvetica-Bold", "Helvetica", "Helvetica-Bold"


# ---------- 第 1 步：识别 ----------

def parse_json(reply):
    reply = re.sub(r"^```\w*\n|\n```$", "", reply.strip())
    match = re.search(r"\{.*\}|\[.*\]", reply, re.S)
    text = match.group(0) if match else reply
    try:
        return json.loads(text, strict=False)  # 允许字符串里直接换行（有的模型会这样写）
    except ValueError:
        # 还是不合规（比如引号没转义、回复被截断）：按 "键": "译文" 一条条捞出来，能救几条是几条
        found = {k: v.replace('\\n', '\n').replace('\\"', '"')
                 for k, v in re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)}
        if found:
            return found
        raise


def parse_blocks(reply):
    data = parse_json(reply)
    blocks = data.get("blocks", []) if isinstance(data, dict) else data
    good = []
    for b in blocks:
        box = b.get("box") or b.get("box_2d")
        if isinstance(box, list) and len(box) == 4 and str(b.get("text", "")).strip():
            ymin, xmin, ymax, xmax = (max(0.0, min(1000.0, float(v))) for v in box)
            if ymax > ymin and xmax > xmin:
                kind = str(b.get("kind", "text")).lower()
                if kind == "logo":  # logo 原样保留，不盖不翻
                    continue
                good.append({"box": (ymin, xmin, ymax, xmax), "source": strip_latex(str(b["text"]).strip()),
                             "kind": kind if kind in ("text", "table", "label", "vertical") else "text",
                             "bold": bool(b.get("bold")), "align": b.get("align", "left")})
    return good


RECHECK_PROMPT = ("这页上已经认出来的文字被涂掉了。请检查页面上还剩下哪些文字没被涂掉，按同样的要求列出来；"
                  "一块都没剩就输出 {\"blocks\": []}。") + OCR_PROMPT


def recognize(client, model, b64, attempts=3, prompt=OCR_PROMPT):
    """识别一页的文字块。偶尔模型会返回空的，多试几次。"""
    blocks = []
    for _ in range(attempts):
        def call():
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            return parse_blocks(response.choices[0].message.content)
        try:
            blocks = with_retry(call)
        except (ValueError, KeyError, TypeError):  # JSON 不完整，重来
            continue
        if blocks or prompt is not OCR_PROMPT:
            break
    return blocks


def overlap(a, b):
    """a 有多大比例落在 b 里（框都是 ymin, xmin, ymax, xmax）。"""
    h = min(a[2], b[2]) - max(a[0], b[0])
    w = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return max(h, 0) * max(w, 0) / area if area else 0


def dedupe(blocks):
    """两块位置大半重叠的只留先认出来的那块（模型偶尔会把同一段认两遍）。"""
    kept = []
    for b in blocks:
        if not any(overlap(b["box"], k["box"]) > 0.4 or overlap(k["box"], b["box"]) > 0.4 for k in kept):
            kept.append(b)
    return kept


def recognize_all(client, model, image, target_lang):
    """认一遍，把认出来的涂掉再让模型看一遍，补上漏掉的（小模型偶尔整页只认出一两块）。
    补认的结果要过三道关：那个位置在涂掉后的图上确实还有字；不和已有的块重叠；文字不重复。
    否则模型会在空白处、图里“编”出字来。"""
    from PIL import ImageDraw
    w, h = image.size
    blocks = dedupe(recognize(client, model, to_b64(image)))
    erased = image.copy()
    draw = ImageDraw.Draw(erased)
    for b in blocks:
        ymin, xmin, ymax, xmax = b["box"]
        draw.rectangle((xmin / 1000 * w - 4, ymin / 1000 * h - 4, xmax / 1000 * w + 4, ymax / 1000 * h + 4),
                       fill=(255, 255, 255))
    seen = {re.sub(r"\s", "", b["source"]) for b in blocks}
    for b in recognize(client, model, to_b64(erased), attempts=1, prompt=RECHECK_PROMPT):
        ymin, xmin, ymax, xmax = b["box"]
        rect = (int(xmin / 1000 * w), int(ymin / 1000 * h), int(xmax / 1000 * w) + 1, int(ymax / 1000 * h) + 1)
        ink = line_fraction(erased, rect, (255, 255, 255))
        text = re.sub(r"\s", "", b["source"])
        if (needs_translation(b["source"], target_lang, cjk_doc=mostly_cjk([o["source"] for o in blocks])) and 0.02 < ink < 0.6 and text not in seen
                and not any(overlap(b["box"], o["box"]) > 0.15 or overlap(o["box"], b["box"]) > 0.15 for o in blocks)):
            blocks.append(b)
            seen.add(text)
    return blocks


# ---------- 本地 OCR（RapidOCR）：认字准、不漏、不编，优先用它 ----------

_rapid = None
LIST_MARK = re.compile(r"^\s*(?:\d+[、.．)）]|[（(]\d+[)）]|[①-⑳●•■◆▲\-–]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[)）]|第[一二三四五六七八九十百零\d]+[条章节款])")
HEADING = re.compile(r"^\s*(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、)")  # “（四）……”“三、……”这种节标题


LEGEND = re.compile(r"^\s*(?:[~～\-]?[A-Za-z0-9/\-]{1,12}\s*[：:]|[一-鿿 （）()]{1,10}[：:].{0,20}$)")  # 图注“MOA：氧化锌避雷器”、
FIELD = re.compile(r"^\s*[一-鿿 ]{1,8}[：:]")  # “总部地址：……”“邮编：……”这样一行一项的清单
# “温度：(20-23)℃”这种短的“名称：数值”行，每条单独一行（长的正文行以“密封：”开头不算）


def rapid_ocr():
    """装了 rapidocr_onnxruntime 就返回识别器，没装返回 None（退回让视觉模型认字）。"""
    global _rapid
    if _rapid is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _rapid = RapidOCR()
        except Exception:
            _rapid = False
    return _rapid or None


def skew_angle(image):
    """扫描件常放歪一点（零点几度）。表格竖线一歪，就断成好几截，找不到格子，标签和内容会被挤到一起。
    用页面上长的横线估计歪了多少度（逆时针为正）；没有长横线或几乎不歪返回 0。"""
    import cv2
    import numpy as np
    gray = np.asarray(image.convert("L"))
    ink = (gray < 150).astype(np.uint8) * 255
    w = gray.shape[1]
    lines = cv2.HoughLinesP(ink, 1, np.pi / 1800, threshold=w // 4, minLineLength=w // 3, maxLineGap=8)
    if lines is None:
        return 0.0
    angles = []
    for x0, y0, x1, y1 in np.asarray(lines).reshape(-1, 4):
        a = np.degrees(np.arctan2(y1 - y0, x1 - x0))
        if abs(a) < 3:
            angles.append(a)
    if len(angles) < 3:
        return 0.0
    angle = float(np.median(angles))
    return angle if 0.08 <= abs(angle) <= 3 else 0.0


def deskew(image):
    angle = skew_angle(image)
    if not angle:
        return image, 0.0
    from PIL import Image
    return image.rotate(angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255)), angle


def rule_segments(image):
    """找页面上的直线（表格线、方框）。返回 (横线 [(y, x0, x1)], 竖线 [(x, y0, y1)])，单位像素。
    扫描件的线常有点歪，先上下（或左右）膨胀 1 像素再找连续的墨迹。"""
    import numpy as np
    gray = np.asarray(image.convert("L"))
    ink = gray < 150
    w, h = image.size
    tall = ink.copy()
    tall[1:] |= ink[:-1]
    tall[:-1] |= ink[1:]
    # 竖线常常印得淡（灰到 150～200），扫描又有点斜，所以用淡一点的门槛、左右各放宽 1 像素
    pale = gray < 200
    wide = pale.copy()
    wide[:, 1:] |= pale[:, :-1]
    wide[:, :-1] |= pale[:, 1:]

    def runs(arr, min_len, gap=0, dots=0):
        out = []
        for k, row in enumerate(arr):
            if row.sum() < min_len:
                continue
            d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            pieces = []
            for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                if pieces and a - pieces[-1][1] <= dots:  # 淡的细线断成一点一点的，先接上
                    pieces[-1][1] = b
                else:
                    pieces.append([a, b])
            found = []
            for a, b in pieces:
                if b - a >= min_len:
                    if found and a - found[-1][1] <= gap:  # 扫描件的线常断一小截，接上
                        found[-1][1] = int(b)
                    else:
                        found.append([int(a), int(b)])
            out += [(k, a, b) for a, b in found]
        return out

    # 横线：淡的细线断成一点一点、或被字（√ 上的横）隔开一小截，接上；竖线不接，免得把上下几行字的竖笔画连成线
    return runs(tall, max(50, w // 16), max(40, w // 12), 1), runs(wide.T, max(25, h // 45))


def find_cell(line, rules, page_w=None):
    """这一行字是不是被四条线围着（表格格子、图里的方框）：是就返回格子 (x0, y0, x1, y1)，否则 None。
    OCR 的框常压到格线上，所以按字的中心找最近的线；线要比字长得多，字自己的笔画不算。"""
    if not rules:
        return None
    hs, vs = rules
    x0, y0, x1, y1 = line["x0"], line["y0"], line["x1"], line["y1"]
    lh, cx, cy = y1 - y0, (x0 + x1) / 2, (y0 + y1) / 2
    slack = max(3, 0.08 * (x1 - x0))  # 歪的扫描件：线头可能比字头差几个像素
    across = [(y, a, b) for y, a, b in hs if a <= x0 + slack and b >= x1 - slack and b - a >= max(x1 - x0, 2 * lh) + 4]
    # 高的格子里字在中间，离上下线可能有好几行远
    top = [r for r in across if cy - 8 * lh <= r[0] < cy - 0.3 * lh]
    bottom = [r for r in across if cy + 0.3 * lh < r[0] <= cy + 8 * lh]
    if not top or not bottom:
        return None
    top, bottom = max(top), min(bottom)
    edge = max(30, (page_w or 0) * 0.02)  # 扫描件页边的黑影不是表格线
    down = [x for x, a, b in vs if a <= cy - 0.2 * lh and b >= cy + 0.2 * lh and b - a >= 1.5 * lh
            and a <= bottom[0] + 3 and b >= top[0] - 3 and x > edge and (not page_w or x < page_w - edge)]
    left = [x for x in down if x < cx and x <= x0 + 0.4 * lh]
    right = [x for x in down if x > cx and x >= x1 - 0.4 * lh]
    # 竖线没找到（扫描时这一截淡了）：上下两条横线在同一处起头/收尾，那里就是表格边
    if not left and abs(top[1] - bottom[1]) <= 6 and top[1] < x0:
        left = [max(top[1], bottom[1])]
    if not right and abs(top[2] - bottom[2]) <= 6 and top[2] > x1:
        right = [min(top[2], bottom[2])]
    if not left or not right:
        return None
    return max(left), top[0], min(right), bottom[0]


def vertical_cuts(line, rules):
    """穿过这一行的表格竖线（按找到的直线算，歪一点的线也行）。"""
    x0, y0, x1, y1 = line["x0"], line["y0"], line["x1"], line["y1"]
    h = y1 - y0
    if h > 1.5 * (x1 - x0):
        return []
    xs = sorted({x for x, a, b in rules[1] if x0 + 3 < x < x1 - 3 and a <= y0 + 0.3 * h and b >= y1 - 0.3 * h
                 and b - a >= 1.5 * h})
    cuts = []
    for x in xs:  # 粗线会找到相邻的好几列，合成一条
        if cuts and x - cuts[-1][1] <= 2:
            cuts[-1][1] = x
        else:
            cuts.append([x, x])
    return [(a + b) // 2 for a, b in cuts]


def padded(crop):
    """切下来的一小格四周补一圈白边再认字，贴着边的字认得准一些。"""
    from PIL import ImageOps
    return ImageOps.expand(crop, border=10, fill=(255, 255, 255))


def split_by_bars(engine, image, line, rules):
    """一行字的框上下跨过了一条横线（OCR 把表头和下一格的数字认成一行）：在线上下切开，各认各的。"""
    import numpy as np
    if not rules:
        return [line]
    x0, y0, x1, y1 = line["x0"], line["y0"], line["x1"], line["y1"]
    if y1 - y0 > 1.5 * (x1 - x0):  # 竖排的大字：横笔画会被当成横线，不切
        return [line]
    cuts = sorted({y for y, a, b in rules[0] if y0 + 5 < y < y1 - 5 and a <= x0 and b >= x1})
    if not cuts:
        return [line]
    parts, start = [], y0
    for cut in cuts + [y1]:
        if cut - start > 8:
            result, _ = engine(np.array(padded(image.crop((x0, start, x1, cut)))))
            text = "".join(str(r[1]) for r in result or []).strip()
            if text:
                parts.append({"x0": x0, "y0": start, "x1": x1, "y1": cut, "text": text})
        start = cut + 2
    return parts or [line]


def missed_by_page(engine, image, found):
    """整页认一次时，被线穿过的小字（图里方框中的“脱离器”）有时漏掉。把页面切成四块重叠的再认一遍，
    只补上原来没有的、很有把握的汉字行；贴着切块边上的不要（可能被切断了）。"""
    import numpy as np
    w, h = image.size
    boxes = [(min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b))
             for b, _, _ in found]
    extra = []
    for tx in (0, int(w * 0.42)):
        for ty in (0, int(h * 0.42)):
            tile = (tx, ty, min(tx + int(w * 0.58), w), min(ty + int(h * 0.58), h))
            result, _ = engine(np.array(image.crop(tile)))
            for box, text, score in result or []:
                xs, ys = [p[0] + tile[0] for p in box], [p[1] + tile[1] for p in box]
                rect = (min(xs), min(ys), max(xs), max(ys))
                edge = ((rect[0] - tile[0] < 5 and tile[0] > 0) or (tile[2] - rect[2] < 5 and tile[2] < w)
                        or (rect[1] - tile[1] < 5 and tile[1] > 0) or (tile[3] - rect[3] < 5 and tile[3] < h))
                hit = any(min(rect[2], b[2]) > max(rect[0], b[0]) and min(rect[3], b[3]) > max(rect[1], b[1])
                          for b in boxes)
                if not edge and not hit and float(score) > 0.88 and len(CJK.findall(str(text))) >= 3:
                    boxes.append(rect)
                    extra.append(([[x, y] for x, y in zip(xs, ys)], text, score))
    return extra


def colored_ink(image):
    """红色（印章）、蓝色（签名笔迹）的墨迹，和黑色正文分开。返回和图一样大的 True/False 数组。"""
    import numpy as np
    a = np.asarray(image.convert("RGB"), dtype=int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > 110) & (r - g > 50) & (r - b > 35)
    blue = (b > 90) & (b - r > 35) & (b - g > 15)
    return red | blue


def photo_regions(image):
    """页面上的照片（样品照片、现场照片）：成片的深色/有底色的区域。照片里的字（柜门上的标牌）不认、不盖，
    不然涂白的地方会在照片上留下一块块白斑。返回 [(x0, y0, x1, y1)]。"""
    import cv2
    import numpy as np
    gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    step = 16
    gh, gw = h // step, w // step
    dark = (gray[:gh * step, :gw * step] < 200).reshape(gh, step, gw, step).mean(axis=(1, 3)) > 0.5
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark.astype(np.uint8), connectivity=8)
    out = []
    for k in range(1, count):
        x, y, bw, bh, area = stats[k]
        if bw * step >= w * 0.15 and bh * step >= h * 0.08 and area >= 0.6 * bw * bh:
            out.append((x * step, y * step, (x + bw) * step, (y + bh) * step))
    return out


def seal_regions(image, colored=None):
    """找成片的彩色墨迹（印章、签名）：按 8 像素一格看哪些格有彩色，连成一片、够大的算一个。
    返回 [(x0, y0, x1, y1)]。红色、蓝色的普通文字是细长一行，不会被当成印章（要求宽高都够大）。"""
    import numpy as np
    colored = colored_ink(image) if colored is None else colored
    h, w = colored.shape
    step = 8
    gh, gw = (h + step - 1) // step, (w + step - 1) // step
    padded = np.zeros((gh * step, gw * step), bool)
    padded[:h, :w] = colored
    grid = padded.reshape(gh, step, gw, step).sum(axis=(1, 3)) >= 3
    seen = np.zeros_like(grid)
    regions = []
    for gy, gx in zip(*np.nonzero(grid)):
        if seen[gy, gx]:
            continue
        stack, cells = [(gy, gx)], []
        seen[gy, gx] = True
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for dy in (-2, -1, 0, 1, 2):  # 隔一格也算连着（印章的圆圈、签名的笔画常断开）
                for dx in (-2, -1, 0, 1, 2):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        ys, xs = [c[0] for c in cells], [c[1] for c in cells]
        box = (min(xs) * step, min(ys) * step, min((max(xs) + 1) * step, w), min((max(ys) + 1) * step, h))
        if box[2] - box[0] >= w * 0.06 and box[3] - box[1] >= w * 0.04:
            regions.append(box)
    if not regions:
        return regions
    # 扫描件黑字边上常有一圈彩色的毛边，在密密的表格里也会连成一片：彩色墨迹太稀、
    # 又大多贴着黑字的，不是印章签名
    import cv2
    a = np.asarray(image.convert("RGB"), dtype=int)
    black = ((a.max(axis=2) - a.min(axis=2)) < 40) & (a.mean(axis=2) < 130)
    near = cv2.dilate(black.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    gray = np.asarray(image.convert("L"))
    real = []
    for x0, y0, x1, y1 in regions:
        c = colored[y0:y1, x0:x1]
        if (gray[y0:y1, x0:x1] < 200).mean() > 0.6:  # 大半是深色的：是照片，不是盖在白纸上的章
            continue
        if c.sum() >= 0.04 * c.size and (c & near[y0:y1, x0:x1]).sum() <= 0.45 * c.sum():
            real.append((x0, y0, x1, y1))
    return real


def split_seals(image):
    """把印章、签名（成片的红色、蓝色墨迹）从页面上拿掉：返回 (去掉印章的页面, 印章那一层)。
    认字、盖字、排版都在去掉印章的页面上做（压在章下面的字认得更准，章里的字也不会被当成正文），
    最后把印章那一层原样叠回去。没有印章时返回 (原图, None)。"""
    import numpy as np
    from PIL import Image as PILImage
    colored = colored_ink(image)
    seals = seal_regions(image, colored)
    if not seals:
        return image, None
    keep = np.zeros_like(colored)
    for x0, y0, x1, y1 in seals:
        keep[y0:y1, x0:x1] = True
    # 印章范围里放宽一点：章上的字常是淡红、粉色的（扫描后边缘发虚），也要一起拿走；黑字不动
    import cv2
    a = np.asarray(image.convert("RGB"), dtype=int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    loose = ((r > 100) & (r - g > 25) & (r - b > 15)) | ((b > 80) & (b - r > 20) & (b - g > 10))
    loose = cv2.dilate(loose.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    black = (a.max(axis=2) - a.min(axis=2) < 40) & (a.mean(axis=2) < 130)
    under = ((0.299 * r + 0.587 * g + 0.114 * b) < 75) & (r >= b)  # 红章压着的黑字：发红但很暗，是字，留着
    ink = (colored | (loose & ~black)) & keep & ~under
    clean = np.asarray(image.convert("RGB")).copy()
    clean[ink] = 255
    layer = image.convert("RGBA")
    layer.putalpha(PILImage.fromarray(ink.astype("uint8") * 255))
    return PILImage.fromarray(clean), layer


def signature_parts(image):
    """找压在正文上的黑色手写签名。返回 [(签名的墨迹 True/False 数组, 签名范围, 字高)]。
    签名和字连在一起时是一大块弯弯曲曲的墨迹：比字大得多、不是直线（表格线、图里的直线占大半的不算），
    而且它压着的那几行字在签名左右两边还接着往下写（图里的标注不会这样）。"""
    import cv2
    import numpy as np
    dark = (np.asarray(image.convert("L")) < 120).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    heights = sorted(st[i, 3] for i in range(1, n) if st[i, 4] >= 15)
    if not heights:
        return []
    ch = max(int(heights[int(len(heights) * 0.75)]), 8)  # 大致的字高
    small = [i for i in range(1, n) if st[i, 4] >= 8 and st[i, 3] <= 1.5 * ch and st[i, 2] <= 1.5 * ch]
    found = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in st[i])
        if h < 2 * ch or w < 6 * ch:
            continue
        mask = (lab[y:y + h, x:x + w] == i).astype(np.uint8)
        line = max(24, int(3 * ch))  # 三个字长的横、竖直线：表格线、图里的线，签名里几乎没有
        straight = (cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, line), np.uint8))
                    | cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((line, 1), np.uint8))).sum() / area
        if straight > 0.35:
            continue
        rows = np.zeros(h, bool)
        for j in small:
            if x <= st[j, 0] + st[j, 2] / 2 <= x + w:
                rows[max(st[j, 1] - y, 0):max(st[j, 1] + st[j, 3] - y, 0)] = True
        bands = text_bands(rows, y, ch)
        if not bands:
            continue
        beside = sum(any(b0 <= st[j, 1] + st[j, 3] / 2 <= b1 and (x - 4 * ch <= st[j, 0] + st[j, 2] < x
                                                                   or x + w < st[j, 0] <= x + w + 4 * ch)
                         for j in small) for b0, b1 in bands)
        if beside >= 0.75 * len(bands):
            full = np.zeros(dark.shape, bool)
            full[y:y + h, x:x + w] = mask > 0
            found.append((full, (x, y, x + w, y + h), ch))
    return found


def text_bands(rows, top, ch):
    """一列 True/False（这一行像素有没有字）里连成片、够高的段：就是一行一行的字。返回 [(上, 下)]。"""
    bands, k = [], 0
    while k < len(rows):
        if rows[k]:
            e = k
            while e < len(rows) and rows[e]:
                e += 1
            if e - k >= 0.5 * ch:
                bands.append((top + k, top + e))
            k = e
        else:
            k += 1
    return bands


def split_signatures(image):
    """把压在字上的黑色签名拿开：字行之间、页边空白里的笔画抠出来单独一层，最后原样叠回去。
    穿过字行的那一截：签名比字黑得多时按深浅分开，把签名补成底色再认字；分不开时认字时留着，
    叠回去时顺着行外的笔画往里接、按进出这一行的位置补一笔。
    返回 (去掉签名的页面, 签名那一层)，没有签名时返回 (原图, None)。"""
    import cv2
    import numpy as np
    from PIL import Image as PILImage
    parts = signature_parts(image)
    if not parts:
        return image, None
    a = np.asarray(image.convert("RGB"))
    clean = a.copy()
    alpha = np.zeros(a.shape[:2], np.uint8)
    color = a.copy()
    dark = np.asarray(image.convert("L")) < 120
    for mask, (x0, y0, x1, y1), ch in parts:
        # 签名左右各多看 4 个字宽，找出这一片的字行（不算签名自己的墨迹）
        lo, hi = max(x0 - 4 * ch, 0), min(x1 + 4 * ch, a.shape[1])
        rows = (dark[y0:y1, lo:hi] & ~mask[y0:y1, lo:hi]).sum(axis=1) > 1
        bands = [(b0 - 1, b1 + 1) for b0, b1 in text_bands(rows, y0, ch)]
        inside = np.zeros(a.shape[0], bool)
        for b0, b1 in bands:
            inside[max(b0, 0):b1] = True
        loose = mask & ~inside[:, None]
        gray = np.asarray(image.convert("L")).astype(int)
        band_rows = np.zeros_like(mask)
        band_rows[inside, lo:hi] = True
        text_ink = dark & ~mask & band_rows
        sig_level = np.median(gray[mask]) if mask.any() else 0
        text_level = np.percentile(gray[text_ink], 10) if text_ink.sum() > 50 else 0
        # 笔画边上那圈灰色的毛边（扫描、抗锯齿留下的）也要擦，不然认字时还看得到一个空心的签名
        halo = cv2.dilate(loose.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        clean[halo & ~(dark & ~mask)] = 255
        alpha[loose] = 255
        ink = np.median(a[loose], axis=0).astype(np.uint8) if loose.any() else np.array([20, 20, 20], np.uint8)
        if text_level - sig_level >= 20:
            # 签名比字黑得多（常见：签字笔比打印的字黑）：字行里按深浅把签名分出来，
            # 签名那一层原样留下，认字用的页面上把签名补成周围的样子（字的笔画顺着补回来）
            alpha[mask & band_rows & (gray < (sig_level + text_level) / 2)] = 255
            # 认字用的页面只补最黑的笔芯：补得多了，压着的字也跟着糊掉，反而认不准
            core = mask & band_rows & (gray < sig_level + 0.25 * (text_level - sig_level))
            hole = cv2.dilate(core.astype(np.uint8), np.ones((3, 3), np.uint8))
            clean = cv2.inpaint(np.ascontiguousarray(clean), hole, 3, cv2.INPAINT_TELEA)
            continue
        # 字行里的笔画：从行外的笔画顺着墨迹往里走半行高，拐弯、打圈的笔画头也能接上，压着的字只带进一点点
        band_h = int(np.median([b1 - b0 for b0, b1 in bands])) if bands else ch
        reach = loose.astype(np.uint8)
        m8 = mask.astype(np.uint8)
        for _ in range(max(band_h // 2, 3)):
            reach = cv2.dilate(reach, np.ones((3, 3), np.uint8)) & m8
        alpha[reach > 0] = 255
        stroke = np.zeros(a.shape[:2], np.uint8)
        for b0, b1 in bands:  # 进这一行的笔画头和出这一行的笔画头，按左右最近的配对连起来
            if b0 - 1 < 0 or b1 >= a.shape[0]:
                continue
            ends = []
            for yy in (b0 - 1, b1):
                row = np.concatenate(([0], mask[yy].astype(np.int8), [0]))
                d = np.diff(row)
                ends.append([((s + e) / 2, e - s) for s, e in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1))])
            free = list(ends[1])
            for cx, wd in ends[0]:
                if not free:
                    break
                best = min(free, key=lambda e: abs(e[0] - cx))
                if abs(best[0] - cx) <= 1.5 * (b1 - b0):
                    free.remove(best)
                    thick = max(2, int(round(min(wd, best[1], ch / 4))))
                    cv2.line(stroke, (int(cx), b0 - 1), (int(best[0]), b1), 255, thick, cv2.LINE_AA)
        add = stroke > 0
        # 穿过字行的那一截也从认字用的页面上擦掉（只擦补的那一笔经过的签名墨迹，字的笔画大多留着）
        path = cv2.dilate(stroke, np.ones((3, 3), np.uint8)) > 0
        clean[path & (mask | ~dark)] = 255
        alpha = np.maximum(alpha, stroke)
        color[add & ~loose] = ink
    layer = PILImage.fromarray(np.dstack([color, alpha]), "RGBA")
    return PILImage.fromarray(clean), layer


def in_seal(line, seals, colored, dark):
    """这一行字是不是印章（签名）自己的字：在印章范围里，而且墨迹大半是彩色的（压在章下面的黑字不算）。"""
    x0, y0, x1, y1 = line["x0"], line["y0"], line["x1"], line["y1"]
    if not any(min(x1, s[2]) > max(x0, s[0]) and min(y1, s[3]) > max(y0, s[1]) for s in seals):
        return False
    c, d = colored[y0:y1, x0:x1].sum(), (dark[y0:y1, x0:x1] & ~colored[y0:y1, x0:x1]).sum()
    return c > 1.5 * d


def ocr_lines(engine, image, rules=None):
    """返回每一行：{x0, y0, x1, y1, text}（像素）。一行如果跨过了表格的竖线、横线，按线拆成一格一格再认。"""
    import numpy as np
    result, _ = engine(np.array(image))
    lines = []
    colored = colored_ink(image)
    seals = seal_regions(image, colored)
    dark = np.asarray(image.convert("L")) < 110  # 黑字
    # 有图、有表格（直线多）的页才分块再认一遍，纯文字页不用，省时间
    extra = missed_by_page(engine, image, result or []) if rules and len(rules[0]) > 15 else []
    for box, text, score in list(result or []) + extra:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        # 汉字行置信度低一点也收（段尾“动。”这种短行常只有 0.5 多）；扫描噪点认出来多是零星字母
        if len(str(text).strip()) == 1 and CJK.search(str(text)) and float(score) < 0.9:
            continue  # 图里的螺栓、花纹常被认成一个不相干的汉字（“鹿”“中”），不确定的单字不动，保留原图
        if str(text).strip() and (float(score) > 0.6 or (float(score) > 0.45 and CJK.search(str(text)))):
            line = {"x0": int(min(xs)), "y0": int(min(ys)), "x1": int(max(xs)), "y1": int(max(ys)),
                    "text": re.sub(r"^[：:；;，,、.。·'`\s]+(?=\S)", "", str(text).strip())}  # 行首噪点认成的标点
            if in_seal(line, seals, colored, dark):
                continue  # 印章、签名自己的字不翻，原样留着
            for part in split_by_rules(engine, image, line, rules):
                lines += split_by_bars(engine, image, part, rules)
    return lines


def split_by_rules(engine, image, line, rules=None):
    """找这一行里有没有贯穿上下的竖线（表格线）；有就在线两边切开，每段单独认字。"""
    import numpy as np
    x0, y0, x1, y1 = line["x0"], line["y0"], line["x1"], line["y1"]
    h = y1 - y0
    if rules:
        cuts = vertical_cuts(line, rules)
    elif x1 - x0 < 3 * h:
        return [line]
    else:
        bg = background_color(image, (x0, y0, x1, y1))
        reach = max(3, h // 3)
        cuts, x = [], x0 + 3
        while x < x1 - 3:
            col = (x, max(y0 - reach, 0), x + 1, min(y1 + reach, image.size[1]))
            if line_fraction(image, col, bg) > 0.85:
                cuts.append(x)
                x += 4
            else:
                x += 1
    if not cuts:
        return [line]
    parts, start = [], x0
    for cut in cuts + [x1]:
        if cut - start > h * 0.6:
            result, _ = engine(np.array(padded(image.crop((start, y0, cut, y1)))))
            text = "".join(str(r[1]) for r in result or []).strip()
            if text:
                parts.append({"x0": start + 2, "y0": y0, "x1": cut - 2, "y1": y1, "text": text})
        start = cut + 3
    return parts or [line]


def ruled_between(image, x0, x1, ya, yb):
    """两行之间（ya 到 yb）有没有横穿的线：有就是表格的两格，不能合成一段。"""
    if image is None or x1 - x0 < 4:
        return False
    ya, yb = max(ya - 2, 0), min(yb + 2, image.size[1] - 1)
    if yb <= ya:
        return False
    bg = background_color(image, (x0, ya, x1, yb))
    return any(line_fraction(image, (x0, y, x1, y + 1), bg) > 0.85 for y in range(ya, yb))


def join_rows(lines):
    """同一行上被 OCR 拆开的“温   度：”“(20-23)℃”：前一截是一两个字或以冒号结尾、中间空得不多，就接成一行。"""
    lines = sorted(lines, key=lambda l: l["x0"])
    out = []
    for line in lines:
        lh = line["y1"] - line["y0"]
        for prev in out:
            ph = prev["y1"] - prev["y0"]
            if (abs((prev["y0"] + prev["y1"]) - (line["y0"] + line["y1"])) / 2 < 0.3 * min(lh, ph)
                    and 0.7 < lh / max(ph, 1) < 1.4 and -0.5 * lh <= line["x0"] - prev["x1"] < 2.5 * lh
                    and (re.search(r"[：:]$", prev["text"]) or (len(prev["text"].strip()) <= 2 and CJK.search(prev["text"])))):
                sep = " " if re.search(r"[A-Za-z0-9]$", prev["text"]) and re.match(r"[A-Za-z0-9]", line["text"]) else ""
                prev.update(text=prev["text"] + sep + line["text"], x1=max(prev["x1"], line["x1"]),
                            y0=min(prev["y0"], line["y0"]), y1=max(prev["y1"], line["y1"]))
                break
        else:
            out.append(dict(line))
    return out


def merge_lines(lines, page_w, image=None):
    """把一行一行合成段落：上下挨着、左边对齐、字一样大，上一行写满了（没以句号结尾），
    下一行也不是新的编号，中间也没有表格横线，就算同一段。"""
    lines = sorted(lines, key=lambda l: (l["y0"], l["x0"]))
    paras = []
    for line in lines:
        h = line["y1"] - line["y0"]
        vertical = h > 2.5 * (line["x1"] - line["x0"]) and len(line["text"]) >= 2
        target = None
        if not vertical and not LIST_MARK.match(line["text"]) and not LEGEND.match(line["text"]):
            for para in reversed(paras):
                last = para["lines"][-1]
                lh = last["y1"] - last["y0"]
                overlap_x = min(last["x1"], line["x1"]) - max(last["x0"], line["x0"])
                # 正文整行宽：OCR 框高会因引号等差一些，放宽；短行（标题、小标题）按字高严格分开
                wide = last["x1"] - last["x0"] > 0.6 * page_w  # 上一行是整行（段尾短行的框也常偏高）
                lo, hi = (0.7, 1.45) if wide else (0.78, 1.28)
                if (para["kind"] == "text" and -0.2 * lh <= line["y0"] - last["y1"] < 1.2 * lh
                        and lo < h / max(lh, 1) < hi and (overlap_x > 0.5 * (line["x1"] - line["x0"]) or line["x0"] < para["x0"])
                        and (abs(line["x0"] - para["x0"]) < 1.5 * lh or para["x0"] > line["x0"] > para["x0"] - 3.2 * lh)
                        and not (re.search(r"[。！？：:.!?]$", last["text"])  # 句号结尾、又没写满一整行，才算段落结束
                                 and last["x1"] - last["x0"] < 0.9 * (para["x1"] - para["x0"]))
                        and last["x1"] - last["x0"] > 0.75 * (para["x1"] - para["x0"])
                        and not (FIELD.match(line["text"]) and FIELD.match(last["text"]))  # 一行一项，不连成段
                        and not ruled_between(image, max(last["x0"], line["x0"]), min(last["x1"], line["x1"]),
                                              last["y1"], line["y0"])):
                    target = para
                    break
        if target:
            target["lines"].append(line)
            target["x0"], target["x1"] = min(target["x0"], line["x0"]), max(target["x1"], line["x1"])
        else:
            paras.append({"lines": [line], "x0": line["x0"], "x1": line["x1"],
                          "kind": "vertical" if vertical else "text"})
    return paras


def boxed(image, rect, glyph):
    """这块字左右两边不远处是不是都有竖线（表格线、方框）。"""
    x0, y0, x1, y1 = rect
    w = image.size[0]
    bg = background_color(image, rect)
    reach = int(glyph * 1.5) + 4

    def has_rule(xs):  # 扫描件的线常有点歪，看 3 像素宽的一条
        return any(line_fraction(image, (x - 1, y0, x + 2, y1), bg) > 0.3 and
                   sum(line_fraction(image, (x - 1, y, x + 2, y + 1), bg) > 0 for y in range(y0, y1)) > 0.85 * (y1 - y0)
                   for x in xs if 1 <= x < w - 2)

    def has_bar(ys):  # 上下的横线：3 像素高的一条里，字宽范围内几乎每一列都有墨
        import numpy as np
        ys = [y for y in ys if 1 <= y < image.size[1] - 3]
        if not ys or x1 - x0 < 4:
            return False
        top, bottom = min(ys), max(ys) + 3
        a = np.asarray(image.crop((x0, top, x1, bottom)).convert("RGB"), dtype=int)
        ink = np.abs(a - np.array(bg[:3])).sum(axis=2) > 70
        return any(ink[y - top:y - top + 3].any(axis=0).mean() > 0.9 for y in ys)

    return ((has_rule(range(x0 - 1, x0 - reach, -1)) and has_rule(range(x1 + 1, x1 + reach)))
            or (has_bar(range(y0 - 1, y0 - reach, -1)) and has_bar(range(y1 + 1, y1 + reach))))


def stack_lines(lines):
    """一格里的字：上下叠着、左右有重叠的行算一段（表头常是两行），左右分开的（数值和单位）各算一段。"""
    paras = []
    for line in sorted(lines, key=lambda l: (l["y0"], l["x0"])):
        lh = line["y1"] - line["y0"]
        for p in paras:
            last = p["lines"][-1]
            if (min(p["x1"], line["x1"]) - max(p["x0"], line["x0"]) > 0
                    and line["y0"] - last["y1"] < 1.2 * lh and line["y0"] > last["y0"] + 0.4 * lh):
                p["lines"].append(line)
                p["x0"], p["x1"] = min(p["x0"], line["x0"]), max(p["x1"], line["x1"])
                break
        else:
            paras.append({"lines": [line], "x0": line["x0"], "x1": line["x1"], "kind": "text"})
    # 右边和左边一段上下错开、但没高出它（“额定电压 …… kV(有效值)”这种单位）：并进左边那段一起翻，
    # 免得两段译文抢位置；右边那段更高的（斜线表头）还是分开
    def span(p):
        return p["lines"][0]["y0"], max(l["y1"] for l in p["lines"])
    merged = True
    while merged:
        merged = False
        for a in paras:
            for b in paras:
                (a0, a1), (b0, b1) = span(a), span(b)
                if (a is not b and b["x0"] >= a["x1"] - 4 and min(a1, b1) - max(a0, b0) > 0
                        and (b0 + b1) / 2 >= (a0 + a1) / 2 - 4):
                    a["lines"] += b["lines"]
                    a["x1"] = max(a["x1"], b["x1"])
                    paras.remove(b)
                    merged = True
                    break
            if merged:
                break
    # 还互相压着的（OCR 把一格里的字拆得很碎）：整格并成一段
    def ext(p):
        return p["x0"], span(p)[0], p["x1"], span(p)[1]
    merged = True
    while merged:
        merged = False
        for a in paras:
            for b in paras:
                (a0, a1, a2, a3), (b0, b1, b2, b3) = ext(a), ext(b)
                if a is not b and min(a2, b2) > max(a0, b0) and min(a3, b3) > max(a1, b1):
                    a["lines"] += b["lines"]
                    a["x0"], a["x1"] = min(a0, b0), max(a2, b2)
                    paras.remove(b)
                    merged = True
                    break
            if merged:
                break
    for p in paras:
        p["lines"] = reading_order(p["lines"])
    return paras


def reading_order(lines):
    """按行（中线差不到半行高的算同一行）从上到下、每行从左到右排。"""
    rows = []
    for line in sorted(lines, key=lambda l: (l["y0"] + l["y1"]) / 2):
        c, lh = (line["y0"] + line["y1"]) / 2, line["y1"] - line["y0"]
        if rows and abs(c - rows[-1][0]) < 0.5 * lh:
            rows[-1][1].append(line)
        else:
            rows.append([c, [line]])
    return [l for _, row in rows for l in sorted(row, key=lambda l: l["x0"])]


def split_columns(lines):
    """一格里的字分成左右好几堆、中间隔着大空白（比如底下一行“批准：  审核：  主检：”中间没有竖线）：
    按空白分成几列，每列自己合段，免得几样东西被连成一句话。"""
    if len(lines) < 2:
        return [lines]
    lh = sorted(l["y1"] - l["y0"] for l in lines)[len(lines) // 2]
    cols = []
    for l in sorted(lines, key=lambda l: l["x0"]):
        if cols and l["x0"] - cols[-1][1] < 1.5 * lh:
            cols[-1][1] = max(cols[-1][1], l["x1"])
            cols[-1][2].append(l)
        else:
            cols.append([l["x0"], l["x1"], [l]])
    return [c[2] for c in cols]


SIGN_LABEL = re.compile(r"(批\s*准|审\s*核|主\s*检|签\s*[字名章]|编\s*制|校\s*对|检\s*验|负责人|授权人|记\s*录)"
                        r"[^：:]{0,4}[：:]\s*$")


def drop_handwriting(lines):
    """“批准：”“审核：”后面手写的名字，OCR 会认成几个歪字（“张的康”）：不翻、不盖，留着原来的签名。
    认法：紧跟在这类标签右边、同一行、两三个汉字、字比标签高出一半以上。"""
    drop = set()
    for l in lines:
        if not SIGN_LABEL.search(l["text"]):
            continue
        lh = l["y1"] - l["y0"]
        near = [m for m in lines if m is not l and l["x1"] - 4 <= m["x0"] < l["x1"] + 8 * lh
                and m["y0"] < l["y1"] and m["y1"] > l["y0"] and len(m["text"].strip()) <= 4
                and CJK.search(m["text"]) and m["y1"] - m["y0"] > 1.5 * lh]
        if near:
            drop.add(id(min(near, key=lambda m: m["x0"])))
    return [l for l in lines if id(l) not in drop]


def drop_bilingual(lines):
    """原文本来就是双语的标签（“委托单位”下面一行小字“Clientele”、“检测报告”下面“TEST REPORT”）：
    不翻也不盖，原样留着；不然中文标签会和下一个标签连成一段，对不上右边的内容。"""
    drop = set()
    for l in lines:
        text = l["text"].strip()
        if not CJK.search(text) or re.search(r"[A-Za-z]", text) or len(text) > 12:
            continue
        lh = l["y1"] - l["y0"]
        cx = (l["x0"] + l["x1"]) / 2
        for m in lines:
            en = m["text"].strip()
            if (m is not l and not CJK.search(en) and len(en) <= 40 and re.search(r"[A-Za-z]{3,}", en)
                    and len(re.findall(r"[A-Za-z ]", en)) >= 0.7 * len(en)
                    and l["y0"] + 0.5 * lh < m["y0"] and m["y0"] - l["y1"] < 0.8 * lh
                    and (m["x0"] - lh < cx < m["x1"] + lh)):
                drop.update((id(l), id(m)))
                break
    return [l for l in lines if id(l) not in drop]


def cell_area(para, group, cell):
    """一格里只有一段字就写满整格（离线留 3 像素）；有好几段（单位、斜线表头）就和旁边的段从中间分开。"""
    def ext(p):
        return p["x0"], p["lines"][0]["y0"], p["x1"], max(l["y1"] for l in p["lines"])
    x0, y0, x1, y1 = cell[0] + 3, cell[1] + 3, cell[2] - 3, cell[3] - 3
    a0, b0, a1, b1 = ext(para)
    for other in group:
        if other is para:
            continue
        c0, d0, c1, d1 = ext(other)
        gx, gy = max(c0 - a1, a0 - c1), max(d0 - b1, b0 - d1)
        if gx >= gy:
            if c0 >= a1:
                x1 = min(x1, (a1 + c0) // 2 - 1)
            else:
                x0 = max(x0, (c1 + a0) // 2 + 1)
        else:
            if d0 >= b1:
                y1 = min(y1, (b1 + d0) // 2 - 1)
            else:
                y0 = max(y0, (d1 + b0) // 2 + 1)
    return min(x0, a0), min(y0, b0), max(x1, a1), max(y1, b1)


def sideways(engine, image):
    """整页的字大多是横躺着的（横版图纸放在竖页上）：返回把它转正要转的角度（PIL 的 rotate 角度），否则 0。"""
    import numpy as np
    result, _ = engine(np.array(image))
    tall = wide = 0
    for box, text, score in result or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        if len(str(text).strip()) >= 2:
            if max(ys) - min(ys) > 1.5 * (max(xs) - min(xs)):
                tall += 1
            else:
                wide += 1
    if tall < 3 or tall <= 1.5 * wide:
        return 0

    def quality(angle):
        r, _ = engine(np.array(image.rotate(angle, expand=True)))
        return sum(float(x[2]) * len(str(x[1])) for x in r or [])
    return -90 if quality(-90) >= quality(90) else 90


def ocr_blocks(engine, image):
    """本地 OCR 认字，合成段落，整理成和视觉模型一样的块格式（框按 0–1000 归一化）。"""
    w, h = image.size
    rules = rule_segments(image)
    free, cells = [], {}
    photos = photo_regions(image)
    lines = [l for l in ocr_lines(engine, image, rules)  # 照片边上的图注不算（照片的框是按 16 像素一格估的）
             if not any(a + 16 <= (l["x0"] + l["x1"]) / 2 <= c - 16 and b + 16 <= (l["y0"] + l["y1"]) / 2 <= d - 16
                        for a, b, c, d in photos)]
    for line in drop_bilingual(drop_handwriting(lines)):
        cell = find_cell(line, rules, w)
        if cell:  # 粗线、歪线会让同一格算出来差一两个像素，认成同一格
            cell = next((c for c in cells if all(abs(a - b) <= 4 for a, b in zip(c, cell))), cell)
            cells.setdefault(cell, []).append(line)
        else:
            free.append(line)
    # 同一列里一行认成了大格子（漏了中间的横线）、别的行认成了小格子：按最小的那个算，免得两段译文叠在一起
    regroup = {}
    for cell, lines in cells.items():
        for line in lines:
            cy = (line["y0"] + line["y1"]) / 2
            same = [c for c in cells if abs(c[0] - cell[0]) <= 6 and abs(c[2] - cell[2]) <= 6 and c[1] < cy < c[3]]
            regroup.setdefault(min(same, key=lambda c: c[3] - c[1]), []).append(line)
    cells = regroup
    rest = []
    for line in free:  # 同一格里别的行找到了格子，这行没找到（线歪、断了）：中心落在格子里就算进去
        cx, cy = (line["x0"] + line["x1"]) / 2, (line["y0"] + line["y1"]) / 2
        cell = next((c for c in cells if c[0] < cx < c[2] and c[1] < cy < c[3]
                     and line["x0"] > c[0] - 6 and line["x1"] < c[2] + 6), None)
        if cell:
            cells[cell].append(line)
        else:
            rest.append(line)
    for cell in list(cells):
        # 一“格”里的字左右分成几堆、中间是大空白：多半是没画竖线的一排（“批准：  审核：  主检：”），
        # 不当格子处理，各写各的位置，免得一块译文盖住旁边的标签和手写签名
        if len(split_columns(cells[cell])) > 1:
            rest += cells.pop(cell)
    paras = merge_lines(join_rows(rest), w, image)
    for cell, lines in cells.items():  # 表格格子、方框：一格里的字自己合段，译文写满这一格
        group = stack_lines(lines)
        for p in group:
            p["cell"] = cell_area(p, group, cell)
        paras += group
    paras.sort(key=lambda p: (p["lines"][0]["y0"], p["x0"]))
    heights = sorted(l["y1"] - l["y0"] for p in paras if p["kind"] == "text" for l in p["lines"])
    body_h = heights[len(heights) // 2] if heights else 0
    blocks = []
    for p in paras:
        x0, x1 = p["x0"], p["x1"]
        y0, y1 = min(l["y0"] for l in p["lines"]), max(l["y1"] for l in p["lines"])
        text = "".join(l["text"] for l in p["lines"]) if CJK.search(p["lines"][0]["text"]) \
            else " ".join(l["text"] for l in p["lines"])
        line_h = (y1 - y0) / len(p["lines"])
        # OCR 的行高有误差，只把明显大一号、又短的单行当标题
        title = (p["kind"] == "text" and body_h and len(p["lines"]) == 1 and line_h > 1.18 * body_h
                 and len(text) <= 30 and not re.match(r"\s*[（(]", text)
                 and not re.search(r"[：:]\s*\S", text))  # “报告编号：JC…”这种“名称：内容”不是标题  # “(1)……”的括号把框撑高了，不是标题
        centered = (abs((x0 + x1) / 2 - w / 2) < w * 0.04 and (title or (x1 - x0) < w * 0.7)
                    and not LIST_MARK.match(text))  # “(3)……”这种编号条目碰巧在中间，也是左对齐
        glyph = sorted(l["y1"] - l["y0"] for l in p["lines"])[len(p["lines"]) // 2]
        kind = p["kind"]
        if kind == "text" and p.get("cell"):
            kind, title = "table", False
            c0, c1 = p["cell"][0], p["cell"][2]  # 原文在格子里左右居中（表头、表格标题），译文也居中
            centered = abs((x0 + x1) - (c0 + c1)) / 2 < 0.06 * (c1 - c0) and x1 - x0 < 0.8 * (c1 - c0)
        elif kind == "text" and not HEADING.match(text) and boxed(image, (x0, y0, x1, y1), glyph):
            kind, title, centered = "table", False, False  # 左右都是竖线：表格格子或图里的方框，只写在框里
        elif kind == "text" and len(p["lines"]) == 1 and len(text) <= 10 and not title and not HEADING.match(text):
            kind = "label"  # 短的单行字：图里的标注，不往外借太多地方
        pitch = ((p["lines"][-1]["y0"] - p["lines"][0]["y0"]) / (len(p["lines"]) - 1) / h * 1000
                 if len(p["lines"]) > 1 else None)
        blocks.append({"box": (y0 / h * 1000, x0 / w * 1000, y1 / h * 1000, x1 / w * 1000), "pitch": pitch,
                       "glyph": glyph / h * 1000,
                       "source": text, "kind": kind, "bold": bool(title),
                       "align": "center" if centered and kind != "label" else "left"})
        if p.get("cell"):
            a0, b0, a1, b1 = p["cell"]
            blocks[-1]["cell"] = (b0 / h * 1000, a0 / w * 1000, b1 / h * 1000, a1 / w * 1000)
    return blocks


def to_b64(image):
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ---------- 第 2 步：翻译 ----------

# 用常见虚词粗略判断一段拉丁字母文字是哪种语言（法文译英文、英文译法文时用）
STOPWORDS = {
    "英文": {"the", "of", "and", "to", "in", "for", "with", "is", "on", "by", "be", "this", "that", "shall", "are",
             "from", "or", "as", "at", "an", "it", "not", "all", "which", "will", "must", "should", "has", "have"},
    "法文": {"le", "la", "les", "des", "du", "de", "et", "un", "une", "pour", "avec", "est", "sont", "dans", "sur",
             "par", "au", "aux", "ce", "cette", "qui", "que", "doit", "doivent", "être", "ou", "en", "leur", "selon"},
    "德文": {"der", "die", "das", "und", "ist", "mit", "für", "von", "den", "dem", "des", "ein", "eine", "nicht",
             "auf", "zu", "im", "sind", "wird", "werden", "oder", "bei", "nach"},
    "西班牙文": {"el", "la", "los", "las", "de", "del", "y", "en", "para", "con", "es", "son", "por", "una", "un",
                 "que", "se", "al", "debe", "o"},
}


def latin_language(text):
    """拉丁字母文字大概是哪种语言；看不出来（太短、全是专有名词）返回 None。"""
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿŒœ']+", text.lower())
    if len(words) < 3:
        return None
    scores = {lang: sum(w.strip("'") in stop for w in words) for lang, stop in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= max(1, 0.15 * len(words)) else None


UNITS = {"kv", "hz", "mm", "cm", "km", "va", "kva", "kw", "mw", "ka", "kg", "db", "ms", "mn", "min", "eff", "vcc", "vca",
         "ca", "cc", "rpm", "ppm", "mpa", "kpa", "bar", "nm", "ah", "mah", "ma", "mv", "kwh", "lux", "pf", "na"}


def code_like(text):
    """型号、编号、单位、纯缩写（TLB-1、kV、PLC、IP65）：哪种语言都一样，不用翻。oui、ou 这种短词还是要翻。"""
    text = re.sub(r"</?x\d+>", "", text)
    if re.search(r"(?<![A-Za-zÀ-ÿ])(à|de|du|au|ou|et|bis|und|oder|entre)(?![A-Za-zÀ-ÿ])", text, re.I):
        return False  # “375 à 500”“De 0,06 à 0,8”：夹着法文、德文小词，要译成“375 to 500”
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}", text)
    return not words or len(words) <= 3 and all(w.isupper() or w.lower() in UNITS or len(w) <= 3 and not w.islower()
                                                 for w in words)


def mostly_cjk(texts):
    """这批文字主要是中文（日文、韩文）吗。中文文件里夹的英文型号、标注不用再送去翻。"""
    texts = [t for t in texts if t.strip()]
    return bool(texts) and sum(bool(CJK.search(t)) for t in texts) >= 0.3 * len(texts)


CJK_LANGS = ("中文", "日文", "韩文")


def detect_source(texts):
    """整份文件的原文是什么语言：中文（日文、韩文都按“中文类”处理）、法文、德文……看不出来返回 None。"""
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return None
    if mostly_cjk(texts):
        return "中文"
    votes = {}
    for t in texts:
        lang = latin_language(t)
        if lang:
            votes[lang] = votes.get(lang, 0) + len(t)
    return max(votes, key=votes.get) if votes else None


def needs_translation(text, target_lang, cjk_doc=False):
    """含中文日文韩文的要翻；拉丁字母的文字（法文、德文……）只要不是目标语言也要翻；纯数字、型号原样保留。
    cjk_doc：整份是中文文件，里面的拉丁字母文字只有明显是别的语言（整句法文）才翻。"""
    if target_lang in LATIN_LANGS:
        if CJK.search(text):
            return True
        if cjk_doc:
            lang = latin_language(text)
            return bool(lang and lang != target_lang)
        if target_lang == "俄文":
            return bool(re.search(r"[A-Za-z]{2,}", text)) and not code_like(text)
        if code_like(text):
            return False
        lang = latin_language(text)
        if lang:
            return lang != target_lang
        return True  # 一两个词看不出语言（Cellule、oui、Total）：交给模型，本来就是目标语言的它会原样返回
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]{2,}", text))


FOREIGN_MARKS = re.compile(r"[éèêëàâçùûîïôœæ]", re.I)


def untranslated(source, result, target_lang):
    """译文其实还是原文（或者只剩原文的碎片）：要重翻。"""
    if target_lang in LATIN_LANGS:
        if CJK.search(result):
            return True
        if result.strip() == source.strip():
            # 法文原样返回当成英文：句子够长、能看出是别的语言才算没翻（Total、Schneider 这种本来就一样）
            lang = latin_language(source)
            return bool(lang and lang != target_lang)
        src_lang = latin_language(source)
        if target_lang == "英文" and src_lang and src_lang != "英文":
            if latin_language(result) == src_lang:  # “Isolement et vide”：还是法文
                return True
        # 译文里的词几乎全是原文里的词（Caractéristiques cellule 15kV）：没翻，只是删了几个词
        words = lambda t: [w.lower() for w in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{4,}", re.sub(r"</?x\d+>", "", t))
                           if not w.isupper()]
        got, src = words(result), set(words(source))
        if target_lang == "英文" and FOREIGN_MARKS.search(source) and len(got) >= 2 \
                and sum(w in src for w in got) >= 0.8 * len(got) and FOREIGN_MARKS.search(result):
            return True
        return False
    return result.strip() == source.strip() and needs_translation(source, target_lang)


KEY_RULE = ("\n输出的键必须和 blocks 里的键一模一样（照抄，不要翻译、不要改动键），值是这个键对应那条原文的译文，"
            "一条对一条，不要合并、拆开或跳过。")


def _letters(t):
    t = unicodedata.normalize("NFKD", re.sub(r"</?x\d+>", "", t))
    return re.sub(r"[\W_]+", "", "".join(c for c in t if not unicodedata.combining(c))).lower()


SHORT_KEY = 80  # 不超过这么长的原文直接当键：模型照抄键再写译文，一条对一条，不会拿了别的格子的译文


def batch_keys(texts):
    """每条原文的键：短的就用原文本身（模型最不容易对错位，对错了也一眼看出），长段落用 #编号。"""
    keys, seen = [], set()
    for n, t in enumerate(texts):
        norm = _letters(t)
        if len(t) <= SHORT_KEY and len(norm) >= 2 and norm not in seen and not t.startswith("#"):
            keys.append(t)
            seen.add(norm)
        else:
            keys.append(f"#{n + 1}")
    return keys


def lookup(reply, keys):
    """按键把模型的回复对回每一条；键被模型改了一点点（大小写、标点、重音、空格）也认得。找不到的是 None。"""
    exact = {str(k): v for k, v in reply.items()}
    loose = {}
    for k, v in exact.items():
        loose.setdefault(_letters(k), v)
        if re.fullmatch(r"#?\d+", k.strip()):
            loose.setdefault("#" + k.strip().lstrip("#"), v)
    out = []
    for k in keys:
        v = exact.get(k)
        if v is None:
            v = loose.get(k) if k.startswith("#") else loose.get(_letters(k))
        out.append(v)
    return out


def as_text(got):
    """模型给的值整理成一条译文；不是文字（嵌套的对象、好几项的列表）就算没给，绝不把原始回复写进文件。"""
    if isinstance(got, list):  # 有的模型回 ["原文", "译文"]
        got = [g for g in got if isinstance(g, str)]
        got = got[-1] if got else None
    if not isinstance(got, str):
        return ""
    got = got.strip()
    if re.match(r"^[\[{]\s*['\"]", got):  # 看起来像一段 JSON
        return ""
    return got


def numbers_lost(source, result):
    """原文里的数（两位以上）译文里没有了：多半是模型把这一批的编号对错了位，拿了别的格子的译文。
    比如“Tenue à fréquence industrielle 50 Hz - 1 mn”回来的是“Admissible short-time current”。这种就单独重翻。"""
    nums = lambda t: set(re.findall(r"\d{2,}", re.sub(r"(?<=\d)[ ,.\u00a0\u202f](?=\d{3}\b)", "",
                                                      re.sub(r"</?x\d+>", "", t))))
    source = re.sub(r"\d{1,2}(?=\s*月)", "", source)  # 10月 会译成 October，不算丢
    return bool(nums(source) - nums(result))


def char_budget(block, source, page_w, page_h):
    """估一估这块位置大约能放多少个英文字符：按中文字数推出原字号，英文平均字宽约半个字号。"""
    ymin, xmin, ymax, xmax = block
    w, h = (xmax - xmin) / 1000 * page_w, (ymax - ymin) / 1000 * page_h
    n = len(CJK.findall(source))
    if not n or w <= 0 or h <= 0:
        return None
    size = min((w * h / n) ** 0.5 / 1.1, h) * 0.8  # 英文可以比原来的汉字小一点、多写一行，省得用缩写
    per_line = w * 1.3 / (0.5 * size)  # 1.3：排版时还能往旁边借一点
    lines = max(1, int(h / (1.2 * size) + 0.3))
    return max(4, int(per_line * lines))


def widened(image, block):
    """算字数预算时把左右的空白也算进去（表格格子、图里标注旁边常有空位），但最多宽到 2 倍。"""
    if block.get("cell"):  # 格子里的字：整格就是能写的地方
        return block["cell"]
    w, h = image.size
    ymin, xmin, ymax, xmax = block["box"]
    rect = (int(xmin / 1000 * w), int(ymin / 1000 * h), int(xmax / 1000 * w), int(ymax / 1000 * h))
    bg = background_color(image, rect)
    room = rect[2] - rect[0]
    right = free_side(image, rect, bg, 1, room)
    left = free_side(image, rect, bg, -1, room) if block["align"] == "center" or block["kind"] == "label" else 0
    return ymin, (rect[0] - left) / w * 1000, ymax, (rect[2] + right) / w * 1000


WRAPPERS = {"blocks", "translations", "translation", "result", "results", "data", "output", "items"}


def unwrap(reply, keys=()):
    """有的模型（比如 DeepSeek）会照着输入的样子再包一层 {"blocks": {...}}，或者直接给一个列表：拆成 {"1": …}。"""
    if isinstance(reply, dict) and len(reply) == 1:
        (k, inner), = reply.items()
        if k not in keys and (isinstance(inner, dict) or (isinstance(inner, list) and k.lower() in WRAPPERS)):
            reply = inner
    elif isinstance(reply, dict) and "1" not in reply and not set(map(str, keys)) & set(reply):
        inner = [v for k, v in reply.items() if str(k).lower() in WRAPPERS and isinstance(v, (dict, list))]
        if inner:
            reply = inner[0]
    if isinstance(reply, list):
        reply = {str(n + 1): v for n, v in enumerate(reply)}
    return reply


def shorten(translator, items):
    """items：[(译文, 最多字符数)]，返回改短后的译文列表；改不了的原样返回。"""
    if translator.kind != "openai" or not items:
        return [t for t, _ in items]
    batch = {str(n + 1): [t, limit] for n, (t, limit) in enumerate(items)}
    system = SHORTEN_PROMPT.format(lang=translator.target_lang)
    try:
        reply = unwrap(parse_json(with_retry(lambda: translator._chat(system, json.dumps(batch, ensure_ascii=False)))))
    except (ValueError, AttributeError):
        return [t for t, _ in items]
    out = []
    for n, (t, _) in enumerate(items):
        got = reply.get(str(n + 1), "") if isinstance(reply, dict) else ""
        if isinstance(got, list):  # 模型有时照着输入的格式回 [译文, 字数]
            got = got[0] if got else ""
        got = str(got).strip()
        out.append(got if got and len(got) < len(t) and not CJK.search(got) else t)
    return out


def debug_log(what, detail=""):
    """批量翻译出岔子时记一笔到 %APPDATA%\\ZhaoYi\\debug.log（只记原文开头和模型回复开头，不记 key），方便查为什么请求变多。"""
    try:
        from providers import config_path
        path = config_path().with_name("debug.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} {str(detail)[:600]}\n")
    except OSError:
        pass


def translate_page(translator, texts, budgets=None, extra="", prompt=None):
    """一页的文字块一次翻完，返回同样长度的译文列表。budgets：每块大约能放多少字符（可以没有）。
    extra：追加给模型的说明（比如保留标记）；prompt：换掉默认的批量翻译说明（别的文件格式用）。"""
    lang = translator.target_lang
    results = list(texts)
    # 原文语言按整份文件判断一次（translator.source_lang），没有的话才看这一批：一批只有几格时自己判断容易看错
    src = getattr(translator, "source_lang", None)
    cjk_doc = src in CJK_LANGS if src else mostly_cjk(texts)
    todo = [i for i, t in enumerate(texts) if needs_translation(t, lang, cjk_doc)]
    if not todo:
        return results
    if translator.kind == "mt":  # 微软、谷歌机器翻译：一批送过去
        got = translator.translate_many([texts[i] for i in todo])
        for i, r in zip(todo, got):
            results[i] = r
        return results
    if translator.kind != "openai":  # DeepL、test：逐块翻
        for i in todo:
            results[i] = translator.translate(texts[i], tagged="<x1>" in texts[i])
        return results
    try:
        return _translate_page(translator, texts, results, todo, budgets, extra, prompt, lang)
    finally:  # 记下这份文件的“原文 → 译文”，提意见修改时用
        pairs = getattr(translator, "pairs", None)
        if pairs is not None:
            for i in todo:
                if results[i] != texts[i]:
                    pairs[texts[i]] = results[i]


def _translate_page(translator, texts, results, todo, budgets, extra, prompt, lang):
    # 上次（额度用完、手动停止前）已经译过的直接拿来用，不再请求
    remembered = {i: translator.recall(texts[i]) for i in todo}
    for i, hit in remembered.items():
        if hit:
            results[i] = hit
    todo = [i for i in todo if not remembered[i]]
    if not todo:
        return results
    if getattr(translator, "drafter", None):
        return _check_drafts(translator, texts, results, todo, budgets, extra, lang)

    # 一批一批翻，每批收回来先整批核对：编号对错位（拿了上一格、下一格的译文）的整批作废，拆成两半重翻；
    # 只有核对通过的才收下、才存进翻译记忆，错的永远不会被记住、下次再拿出来用
    pending = [(todo, 0)]
    singles = []
    while pending:
        group, tries = pending.pop(0)
        if len(group) == 1 and group is not todo:  # 拆到只剩一条：单独翻（单条不会对错位）
            singles.append(group[0])
            continue
        good, bad, shifted = _run_batch(translator, texts, group, budgets, extra, prompt, lang)
        for i, got in good.items():
            results[i] = got
            translator.remember(texts[i], got)
        if shifted:
            debug_log(f"这一批 {len(group)} 条对错了位，整批不要，拆小重翻",
                      [(texts[i][:30], why) for i, why in bad[:5]])
            half = (len(group) + 1) // 2
            pending[:0] = [(group[:half], tries), (group[half:], tries)]
        elif bad:
            debug_log(f"这一批 {len(group)} 条里有 {len(bad)} 条没翻好", [(texts[i][:30], why) for i, why in bad[:5]])
            missed = [i for i, _ in bad]
            if len(missed) > 1 and tries == 0:
                pending.append((missed, 1))
            else:
                singles += missed
    for i in singles:  # 还剩的一块一块单独翻
        results[i] = translator.translate(texts[i], tagged="<x1>" in texts[i])
    return results


def _run_batch(translator, texts, group, budgets, extra, prompt, lang):
    """送一批出去，返回 (核对通过的 {序号: 译文}, 没通过的 [(序号, 原因)], 这一批是否整体不可信)。
    短的原文直接当键，模型照抄键再写译文，所以一条对一条；长段落用 #编号，再看数字有没有丢。"""
    keys = batch_keys([texts[i] for i in group])
    batch = dict(zip(keys, (texts[i] for i in group)))
    limits = {k: budgets[i] for k, i in zip(keys, group) if budgets and budgets[i]}
    system = ((translator.scene or "你是专业翻译。") + (prompt or BATCH_PROMPT).format(lang=lang) + ONE_RULE + extra
              + glossary_prompt("\n".join(batch.values()), translator.glossary) + KEY_RULE)
    user = json.dumps({"blocks": batch, "limits": limits}, ensure_ascii=False)
    raw = with_retry(lambda: translator._chat(system, user))
    try:
        reply = parse_json(raw)
    except (ValueError, AttributeError):
        reply = {}
    reply = unwrap(reply, keys)
    if not isinstance(reply, dict) or not reply:
        debug_log(f"整批没解析出来（{len(group)} 条）", repr(raw[:300]) if isinstance(raw, str) else raw)
        return {}, [(i, "没解析出来") for i in group], True
    if not any(str(k) in batch for k in reply) and all(re.fullmatch(r"\d+", str(k)) for k in reply):
        # 模型没照抄键、自己编了 1、2、3：按顺序对回（和以前一样），后面靠数字、语言再核对
        reply = {key: reply.get(str(n + 1)) for n, key in enumerate(keys)}
    good, bad = {}, []
    wrong = 0
    for i, src, got in zip(group, (texts[i] for i in group), lookup(reply, keys)):
        got = as_text(got)
        if not got:
            bad.append((i, "空"))
        elif numbers_lost(src, got):
            wrong += 1
            bad.append((i, f"数字丢了：{got[:30]}"))
        elif untranslated(src, got, lang):
            bad.append((i, f"没翻：{got[:30]}"))
        else:
            good[i] = capitalize_first(src, one_choice(src, strip_latex(got)), lang)
    if wrong >= 2:  # 两条以上数字对不上：这一批多半整体错了位，“看起来没问题”的那些也不能信
        return {}, bad, True
    return good, bad, False


def _check_drafts(translator, texts, results, todo, budgets, extra, lang):
    """机器翻译先出初稿（不花钱），大模型只把有问题的条目改好输出（输出少，费用低）。"""
    drafts = translator.drafter.translate_many([texts[i] for i in todo])
    batch = {str(n + 1): [texts[i], d] for n, (i, d) in enumerate(zip(todo, drafts))}
    limits = {str(n + 1): budgets[i] for n, i in enumerate(todo) if budgets and budgets[i]}
    system = ((translator.scene or "你是专业翻译审校。") + CHECK_PROMPT.format(lang=lang) + ONE_RULE + extra
              + glossary_prompt("\n".join(texts[i] for i in todo), translator.glossary))
    user = json.dumps({"blocks": batch, "limits": limits}, ensure_ascii=False)
    try:
        reply = unwrap(parse_json(with_retry(lambda: translator._chat(system, user))))
    except (ValueError, AttributeError):
        reply = {}  # 把关这一步没解析出来，就用初稿
    if not isinstance(reply, dict):
        reply = {}
    for n, (i, draft) in enumerate(zip(todo, drafts)):
        got = reply.get(str(n + 1), "")
        if isinstance(got, list):
            got = got[-1] if got else ""
        got = str(got).strip()
        if got and not untranslated(texts[i], got, lang):
            draft = capitalize_first(texts[i], one_choice(texts[i], strip_latex(got)), lang)
        results[i] = draft
        translator.remember(texts[i], draft)
    return results


# ---------- 第 3 步：盖字 ----------

def is_ink(p, bg, threshold=70):
    return abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > threshold


def background_color(image, box):
    """取方框四周一圈像素里最亮的一半的中位数当底色（白纸、黄纸、灰底都能盖住）。"""
    x0, y0, x1, y1 = box
    w, h = image.size
    pad = 4
    strips = [image.crop((max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, w), max(y0, 1))),
              image.crop((max(x0 - pad, 0), min(y1, h - 1), min(x1 + pad, w), min(y1 + pad, h))),
              image.crop((max(x0 - pad, 0), y0, max(x0, 1), y1)),
              image.crop((min(x1, w - 1), y0, min(x1 + pad, w), y1))]
    pixels = sorted((p for s in strips if s.size[0] > 0 and s.size[1] > 0 for p in s.getdata()), key=sum)
    if not pixels:
        return (255, 255, 255)
    bright = pixels[len(pixels) // 2:]
    return bright[len(bright) // 2]


def line_fraction(image, rect, bg):
    """这一条区域里“有墨”的像素占多少。"""
    data = list(image.crop(rect).getdata())
    return sum(is_ink(p, bg) for p in data) / max(len(data), 1)


def grow_to_ink(image, box, bg, reach=1.0):
    """模型给的框常常偏小：四条边往外推，直到边外一小段距离内都没有字为止（最多推出页面的 4%）。
    往左右看得远一点（字和字之间有空隙），往上下看得近一点（免得并进上下行）。
    碰到几乎整条都是墨的（表格线、边框）就停，不把线盖掉。"""
    w, h = image.size
    x0, y0, x1, y1 = box
    limit_x, limit_y = int(w * 0.04 * reach), int(h * 0.02 * reach)
    look_x, look_y = max(4, int(w * 0.012)), max(2, int(h * 0.003))

    def has_text(look, edge):
        """look：边外的一小段；edge：紧挨着边的一条线。边上正好是表格线就不再推。"""
        if look[2] <= look[0] or look[3] <= look[1] or is_line(image, edge, bg):
            return False
        return 0 < line_fraction(image, look, bg) < 0.85

    for _ in range(limit_x + limit_y):
        moved = False
        if y0 > 0 and box[1] - y0 < limit_y and has_text((x0, max(y0 - look_y, 0), x1, y0), (x0, y0 - 1, x1, y0)):
            y0 -= 1; moved = True
        if y1 < h - 1 and y1 - box[3] < limit_y and has_text((x0, y1, x1, min(y1 + look_y, h)), (x0, y1, x1, y1 + 1)):
            y1 += 1; moved = True
        if x0 > 0 and box[0] - x0 < limit_x and has_text((max(x0 - look_x, 0), y0, x0, y1), (x0 - 1, y0, x0, y1)):
            x0 -= 1; moved = True
        if x1 < w - 1 and x1 - box[2] < limit_x and has_text((x1, y0, min(x1 + look_x, w), y1), (x1, y0, x1 + 1, y1)):
            x1 += 1; moved = True
        if not moved:
            break
    return x0, y0, x1, y1


def is_line(image, rect, bg, reach=6):
    """一条 1 像素宽的区域是不是表格线、边框：几乎全是墨，而且在两头外面还接着延伸（字的笔画不会）。"""
    x0, y0, x1, y1 = rect
    w, h = image.size
    if x1 <= x0 or y1 <= y0 or line_fraction(image, rect, bg) <= 0.7:
        return False
    if y1 - y0 == 1:  # 横线
        outside = [(max(x0 - reach, 0), y0, x0, y1), (x1, y0, min(x1 + reach, w), y1)]
    else:  # 竖线
        outside = [(x0, max(y0 - reach, 0), x1, y0), (x0, y1, x1, min(y1 + reach, h))]
    return any(r[2] > r[0] and r[3] > r[1] and line_fraction(image, r, bg) > 0.7 for r in outside)


def cover(patched, original, rect, bg):
    """用底色盖住一块，再把穿过这块的表格线、边框从原图补回来。"""
    from PIL import ImageDraw
    x0, y0, x1, y1 = rect
    ImageDraw.Draw(patched).rectangle(rect, fill=bg)
    for y in range(y0, y1 + 1):
        row = (x0, y, x1 + 1, y + 1)
        if is_line(original, row, bg):
            patched.paste(original.crop(row), row[:2])
    for x in range(x0, x1 + 1):
        col = (x, y0, x + 1, y1 + 1)
        if is_line(original, col, bg):
            patched.paste(original.crop(col), col[:2])


def wrap(text, font, size, width):
    """按宽度折行。英文按单词断，中日韩按字断。"""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines = []
    for para in text.split("\n"):
        tokens = re.findall(r"[A-Za-z0-9\u00C0-\u024F\u0370-\u04FF'’.,;:!?%()\-/+°±×φΩμ~]+\s*|\s+|.", para)
        line = ""
        for tok in tokens:
            if stringWidth(line + tok, font, size) <= width or not line.strip():
                line += tok
            else:
                lines.append(line.rstrip())
                line = tok.lstrip()
        lines.append(line.rstrip())
    return lines


def line_lead(size, lead, pitch):
    """行距倍数：知道原文行距（pitch，磅）时尽量和原文一样，限制在 1.05–1.8 倍之间。"""
    return min(max(pitch / size, 1.05), 1.8) if pitch else lead


def fit(text, font, width, height, max_size, lead=1.15, pitch=None):
    """找到能放进方框的最大字号，返回 (字号, 行列表)。lead 是行距倍数。"""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    size = max_size
    while size > 4:
        lines = wrap(text, font, size, width)
        k = line_lead(size, lead, pitch)
        if (len(lines) - 1) * size * k + size * 1.15 <= height and all(
                stringWidth(l, font, size) <= width * 1.02 for l in lines):
            return size, lines
        size -= 0.25
    return size, wrap(text, font, size, width)


def room_around(i, boxes, page_w):
    """译文一般比中文长：旁边没有别的文字块的话，允许写宽一点。返回 (最左, 最右)。"""
    x0, y0, x1, y1 = boxes[i]
    left_limit, right_limit = page_w * 0.06, page_w * 0.94
    for j, (a0, b0, a1, b1) in enumerate(boxes):
        if j == i or b1 <= y0 or b0 >= y1:  # 不在同一高度
            continue
        if a0 >= x1:
            right_limit = min(right_limit, a0 - 4)
        elif a1 <= x0:
            left_limit = max(left_limit, a1 + 4)
    return min(left_limit, x0), max(right_limit, x1)


def text_lines(image, rect, bg):
    """数一数这块原文有几行，返回原文的行高（像素）。用每一横行有没有墨来分行。"""
    x0, y0, x1, y1 = rect
    rows = [line_fraction(image, (x0, y, x1, y + 1), bg) > 0.01 for y in range(y0, y1)]
    runs, inside = 0, False
    for r in rows:
        if r and not inside:
            runs += 1
        inside = r
    return (y1 - y0) / max(runs, 1)


def draw_text(pdf, text, font, size, lines, left, right, top, height, align, lead=1.15):
    y = top - size if len(lines) > 1 else top - (height + size * 0.75) / 2  # 单行时上下居中
    for line in lines:
        if align == "center":
            pdf.drawCentredString((left + right) / 2, y, line)
        elif align == "right":
            pdf.drawRightString(right, y, line)
        else:
            pdf.drawString(left, y, line)
        y -= size * lead


def free_side(image, rect, bg, step, limit):
    """方框右边（step=1）或左边（step=-1）有多少列像素是空白（碰到字、线、图就停），最多看 limit 列。"""
    x0, y0, x1, y1 = rect
    w = image.size[0]
    for k in range(limit):
        x = x1 + k if step > 0 else x0 - 1 - k
        if x < 0 or x >= w or line_fraction(image, (x, y0, x + 1, y1), bg) > 0.01:
            return max(k - 4, 0)
    return limit


def free_below(image, rect, bg, limit):
    """方框下面有多少行像素是空白（没有字、线、图），最多看 limit 行。"""
    x0, y0, x1, y1 = rect
    h = image.size[1]
    for k in range(limit):
        y = y1 + k
        if y >= h or line_fraction(image, (x0, y, x1, y + 1), bg) > 0.01:
            return max(k - 3, 0)  # 和下面的东西留 3 像素
    return limit


def columns(body, patched, sx, sy, page_h, others=()):
    """把上下相邻、左右对齐的正文段落分成一栏一栏。两段之间夹着图（盖字后还有墨迹）或别的字块（小标题）就断开。"""
    groups = []
    for p in sorted(body, key=lambda p: -p["top"]):
        g = groups[-1] if groups else None
        if g:
            last = g[-1]
            bottom = last["top"] - last["height"]
            overlap = min(last["right"], p["right"]) - max(last["left"], p["left"])
            if overlap > 0.5 * (p["right"] - p["left"]) and -2 < bottom - p["top"] < 3 * p["cap"]:
                gap = (int(max(last["left"], p["left"]) / sx), int((page_h - bottom) / sy),
                       int(min(last["right"], p["right"]) / sx), int((page_h - p["top"]) / sy))
                clean = gap[3] <= gap[1] + 1 or line_fraction(patched, gap, background_color(patched, gap)) < 0.002
                clean = clean and not any(o["top"] > p["top"] and o["top"] - o["height"] < bottom
                                          and min(o["right"], p["right"]) > max(o["left"], p["left"])
                                          for o in others)
            else:
                clean = False
            if clean:
                g.append(p)
                continue
        groups.append([p])
    return [g for g in groups if len(g) > 1]


def para_gap(gap, size):
    """段落之间留的空：原来空多少留多少，挤的时候最多压到 0.8 个字高，但至少留半个字高，段落不贴在一起。"""
    return max(min(gap, size * 0.8), size * 0.5)


def reflow(group, layout):
    """一栏里的几段正文当成连续的流来排：同一个字号，从第一段顶上排到最后一段底下，段落跟着往下挪。
    这样一段放不下时可以占用别的段多出来的空间，整栏字号一致。"""
    top, bottom = group[0]["top"], group[-1]["top"] - group[-1]["height"]
    gaps = [max(a["top"] - a["height"] - b["top"], 0) for a, b in zip(group, group[1:])]
    cap = min(p["cap"] for p in group)
    size = cap
    while size > 4:
        need = 0
        for p in group:
            layout(p, size)
            need += (len(p["lines"]) - 1) * p["size"] * p["lead_used"] + p["size"] * 1.2
        need += sum(para_gap(g, size) for g in gaps)
        if need <= top - bottom and all(p["size"] >= size - 0.01 for p in group):
            break
        size -= 0.25
    for p in group:
        layout(p, size)
        p["height"] = (len(p["lines"]) - 1) * p["size"] * p["lead_used"] + p["size"] * 1.2
    # 前面的段落没变长时，后面的段落留在原来的位置（不往上挤）；放不下才把段间距压小
    tops = [p["top"] for p in group]

    def place(stay):
        y, out = top, []
        for p, gap, t in zip(group, [0] + gaps, tops):
            y = min(y - para_gap(gap, size), t) if stay else y - para_gap(gap, size)
            out.append(y)
            y -= p["height"]
        return out, y

    new, end = place(True)
    if end < bottom - 0.5:
        new, end = place(False)
    for p, t in zip(group, new):
        p["top"], p["reflowed"] = t, True


def draw_page(pdf, image, page_w, page_h, blocks, fonts, target_lang, shorten=None, rotate=0, seal_layer=None):
    """一页：原图打底，盖住原文，写上译文。PDF 坐标原点在左下角。返回放不太下的块数。
    译文比原文长时依次：借旁边和下面的空白 → 整页正文统一缩一点（不小于 75%）→ 让模型改短
    → 换窄字体、收紧行距 → 还不行就用小字号写完，并计数提示。shorten(items) 是改短译文的函数。"""
    from reportlab.lib.utils import ImageReader
    img_w, img_h = image.size
    # reportlab 的页面大小指转过之后看到的大小，横过来的页要反着给
    pdf.setPageSize((page_h, page_w) if rotate in (90, 270) else (page_w, page_h))
    patched = image.copy()
    # 不用翻的（已经是译文语言、纯数字、型号）原样留着，不盖
    kept = [b for b in blocks if b["text"].strip() == b["source"].strip()]
    blocks = [b for b in blocks if b["text"].strip() != b["source"].strip()]
    pixel_boxes, model_boxes, line_heights = [], [], []
    for b in blocks:
        ymin, xmin, ymax, xmax = b["box"]
        px = (int(xmin / 1000 * img_w), int(ymin / 1000 * img_h), int(xmax / 1000 * img_w), int(ymax / 1000 * img_h))
        bg = background_color(image, px)
        if b.get("glyph"):  # 本地 OCR 的框很准，只往外放几个像素，不去推（免得擦到表格里的数字和图上的线）
            e = max(2, int(b["glyph"] / 1000 * img_h * 0.12))
            m = (max(px[0] - e, 0), max(px[1] - e, 0), min(px[2] + e, img_w - 1), min(px[3] + e, img_h - 1))
        else:  # 视觉模型的框常偏小，往外推；图里的标注、表格只小范围推
            m = grow_to_ink(image, px, bg, reach=0.4 if b["kind"] in ("label", "table") else 1.0)
        if b["kind"] == "vertical":
            line_heights.append(0)
        elif b.get("glyph"):  # OCR 给了每行的字高，比自己数墨迹准
            line_heights.append(b["glyph"] / 1000 * img_h)
        else:
            line_heights.append(text_lines(image, m, bg))
        pad = max(2, int(img_h * 0.002)) if b["kind"] in ("text", "vertical") else 1
        mask = (max(m[0] - pad, 0), max(m[1] - pad, 0), min(m[2] + pad, img_w - 1), min(m[3] + pad, img_h - 1))
        cover(patched, image, mask, bg)
        q = (m[3] - m[1]) // 4  # 只看中间一半高：上下边贴着照片、格线的不算“线穿过字”
        if b["kind"] in ("label", "table") and line_fraction(patched, (m[0], m[1] + q, m[2], m[3] - q), bg) > 0.12:
            b["skip"] = mask  # 有线穿过字、盖不干净：译文写上去会和残迹叠成一团，不如保留原图
        pixel_boxes.append(m)  # 推开后的框正好包住原文，译文就写在这里
        model_boxes.append(px)
    # 推开后的框不能压到上下别的文字块
    for i, (x0, y0, x1, y1) in enumerate(pixel_boxes):
        for j, (a0, b0, a1, b1) in enumerate(model_boxes):
            if j == i or a1 <= x0 or a0 >= x1:
                continue
            if b0 >= model_boxes[i][3]:
                y1 = min(y1, b0 - 1)
            elif b1 <= model_boxes[i][1]:
                y0 = max(y0, b1 + 1)
        pixel_boxes[i] = (x0, y0, x1, max(y1, y0 + 4))
    # 两块推开后上下还叠着的，从叠住的中间切开
    for i in range(len(pixel_boxes)):
        for j in range(len(pixel_boxes)):
            a, c = pixel_boxes[i], pixel_boxes[j]
            if i == j or a[2] <= c[0] or a[0] >= c[2] or not (a[1] < c[1] < a[3]):
                continue
            cut = (c[1] + a[3]) // 2
            pixel_boxes[i] = (a[0], a[1], a[2], max(cut - 1, a[1] + 4))
            pixel_boxes[j] = (c[0], min(cut + 1, c[3] - 4), c[2], c[3])
    # 表格格子、方框里的字：写满自己那一格
    for i, b in enumerate(blocks):
        if b.get("cell"):
            ymin, xmin, ymax, xmax = b["cell"]
            pixel_boxes[i] = (int(xmin / 1000 * img_w), int(ymin / 1000 * img_h),
                              int(xmax / 1000 * img_w), int(ymax / 1000 * img_h))
    # 旁边的块往外推时可能擦到原样保留的字（kV、8/20μs 这种），从原图补回来
    for b in blocks:
        if b.get("skip"):
            patched.paste(image.crop(b["skip"]), b["skip"][:2])
    for b in kept:
        ymin, xmin, ymax, xmax = b["box"]
        rect = (int(xmin / 1000 * img_w) - 1, int(ymin / 1000 * img_h) - 1,
                int(xmax / 1000 * img_w) + 2, int(ymax / 1000 * img_h) + 2)
        patched.paste(image.crop(rect), rect[:2])
    buf = io.BytesIO()
    patched.save(buf, format="JPEG", quality=88)
    buf.seek(0)
    pdf.drawImage(ImageReader(buf), 0, 0, page_w, page_h)

    sx, sy = page_w / img_w, page_h / img_h
    boxes = [(x0 * sx, y0 * sy, x1 * sx, y1 * sy) for x0, y0, x1, y1 in pixel_boxes]
    # 同一页正文的字号统一：取正文行高的中位数
    body = sorted(h * sy for h, b in zip(line_heights, blocks) if b["kind"] == "text" and h)
    body_size = body[len(body) // 2] * 0.8 if body else 30

    # ---- 排版：先算每块放在哪、多大字号，再统一调整，最后才画 ----
    bgs = [background_color(image, (int(b["box"][1] / 1000 * img_w), int(b["box"][0] / 1000 * img_h),
                                    int(b["box"][3] / 1000 * img_w), int(b["box"][2] / 1000 * img_h)))
           for b in blocks]
    def room(i):
        """左右能借到哪里：不碰别的文字块，也不碰图上的线条、图形。"""
        lo, hi = room_around(i, boxes, page_w)
        px = pixel_boxes[i]
        right = free_side(image, px, bgs[i], 1, max(int((hi - boxes[i][2]) / sx), 0))
        left = free_side(image, px, bgs[i], -1, max(int((boxes[i][0] - lo) / sx), 0))
        return boxes[i][0] - left * sx, boxes[i][2] + right * sx

    plans = []
    for i, b in enumerate(blocks):
        if b.get("skip"):
            continue
        text = b["text"]
        if target_lang in LATIN_LANGS:
            text = re.sub(r" {2,}", " ", text.translate(CN_PUNCT)).strip()
        x0, y0, x1, y1 = boxes[i]
        height = max(y1 - y0, 6)
        plan = {"text": text, "top": page_h - y0, "height": height, "align": b["align"], "kind": b["kind"],
                "glyph": line_heights[i] or 0,
                "pitch": b["pitch"] / 1000 * page_h if b.get("pitch") else None,
                "bold": b["bold"], "lead": 1.15, "font": fonts[1] if b["bold"] else fonts[0]}
        if b.get("cell"):  # 原文在格子里上下居中（合并的大格子常这样），译文也居中
            (c0, _, c1, _), (t0, _, t1, _) = b["cell"], b["box"]
            plan["middle"] = abs((c0 + c1) - (t0 + t1)) / 2 < 0.15 * (c1 - c0)
        if CJK.search(text) and target_lang in LATIN_LANGS:
            plan["font"] = CJK_FALLBACK  # 漏翻的中文用中文字体写，至少不会变成方框
        if b["kind"] == "vertical":
            plan["cap"] = min((x1 - x0) * 0.85, 20)
            plan["left"], plan["right"] = x0, x1
            if x1 - x0 > page_w * 0.05:  # 大号竖排标题：英文横着写，以竖排的中心为中心
                lo, hi = room(i)
                mid = (x0 + x1) / 2
                half = max((x1 - x0) / 2, min(mid - lo, hi - mid, page_w * 0.3))
                plan.update(kind="vertical-title", left=mid - half, right=mid + half, cap=min((x1 - x0) * 0.8, 30))
            plans.append(plan)
            continue
        # 字号上限：不超过原文的字高
        plan["cap"] = min(line_heights[i] * sy * 0.8 if line_heights[i] else height * 0.85, 30)
        if b["kind"] == "text":
            plan["body"] = (not b["bold"] and b["align"] != "center" and (x1 - x0 > page_w * 0.35 or LIST_MARK.match(b.get("source", "")))  # 图旁的窄说明不算正文
                            and (len(text) > 25 or (LIST_MARK.match(b.get("source", "")) and len(text) > 12)))
            if plan["body"]:  # 普通正文统一字号，标题（加粗、居中、很短）按自己的
                plan["cap"] = min(plan["cap"], body_size * 1.05)
            lo, hi = room(i)
            if b["align"] == "center":
                mid = (x0 + x1) / 2
                half = min(mid - lo, hi - mid)
                plan["left"], plan["right"] = mid - half, mid + half
            elif b["align"] == "right":
                plan["left"], plan["right"] = max(lo, x1 - (x1 - x0) * 1.6), x1
            elif b["bold"] or LIST_MARK.match(b.get("source", "")):  # 左对齐的标题、编号条目：右边空着就写过去
                plan["left"], plan["right"] = x0, hi
            else:  # 左对齐的最多往右借 60%
                plan["left"], plan["right"] = x0, min(hi, x0 + (x1 - x0) * 1.6)
            # 下面空着的话也借一点（最多原高度的一半）
            px = pixel_boxes[i]
            limit = int((px[3] - px[1]) * 0.5) + 2
            for j, (a0, b0, a1, b1) in enumerate(model_boxes):  # 也不能借到下面别的文字块的位置
                if j != i and a0 < px[2] and a1 > px[0] and b0 >= px[1]:
                    limit = min(limit, max(b0 - px[3] - 3, 0))
            extra = free_below(image, px, bgs[i], limit) * sy
            plan["height"] = height + extra
        elif b["kind"] == "label":  # 短标注、表格格子：借旁边的空白（碰到线和图就停），最多两倍宽，宁可缩小也不溢出
            lo, hi = room(i)
            if b["align"] == "left" and LIST_MARK.match(b.get("source", "")):  # “3、安装”这种小标题：右边空着就一直写过去
                plan["left"], plan["right"] = x0, hi
            elif b["align"] == "left":
                plan["left"], plan["right"] = x0, min(hi, x0 + (x1 - x0) * 2)
            else:
                mid, half = (x0 + x1) / 2, (x1 - x0)
                half = max((x1 - x0) / 2, min(half, mid - lo, hi - mid))
                plan["left"], plan["right"] = mid - half, mid + half
        else:  # 表格：只写在格子里，离格线留一点距离
            inset = min(2, (x1 - x0) * 0.05)
            plan["left"], plan["right"] = x0 + inset, x1 - inset
        plans.append(plan)

    def layout(plan, max_size=None):
        width = (plan["height"] if plan["kind"] == "vertical" else plan["right"] - plan["left"])
        room = plan["right"] - plan["left"] if plan["kind"] == "vertical" else plan["height"] + 1
        plan["size"], plan["lines"] = fit(plan["text"], plan["font"], max(width, 8), room,
                                          max(max_size or plan["cap"], 4.5), plan["lead"], plan.get("pitch"))
        plan["lead_used"] = line_lead(plan["size"], plan["lead"], plan.get("pitch"))
        return plan["size"]

    for plan in plans:
        layout(plan)
    # 整页正文统一：按最挤的那块缩，但不小于原字号的 75%；比这还挤的块单独处理
    body = [p for p in plans if p.get("body")]
    for group in columns(body, patched, sx, sy, page_h, [p for p in plans if not p.get("body")]):
        reflow(group, layout)
    if body:  # 整页正文用同一个字号：取最挤那段的字号，但不小于正文字号的 75%（更挤的段落自己再缩）
        target = max(min(p["size"] for p in body), body_size * 0.75)
        for p in body:  # 上限都压到统一字号（后面换窄字体重排时也不会比别的段大）
            p["cap"] = min(p["cap"], target)
            if not p.get("reflowed") or p["size"] > target + 0.01:  # 各栏排完字号不一样的，也统一到同一个
                layout(p)

    # 同一页表格里的字也统一字号：按大多数格子的字号，太挤的格子自己再小一点
    # 原文明显大一号的格子（表格最上面的标题“外观质量及结构检查”）不跟着缩，保持大字
    glyphs = sorted(p["glyph"] for p in plans if p["kind"] == "table" and p["glyph"])
    big = glyphs[len(glyphs) // 2] * 1.15 if glyphs else 0
    for p in plans:  # 只算独占一行的格子（表格大标题），同一行还有别的格子的不算
        p["big"] = bool(big and p["kind"] == "table" and p["glyph"] > big
                        and not any(q is not p and q["kind"] == "table" and abs(q["top"] - p["top"]) < 3 for q in plans))
    # 统一字号按“放得下”的格子来定；本来就挤的格子（窄列里的长表头）不拉着整页一起变小
    cells = sorted(p["size"] for p in plans if p["kind"] == "table" and not p["big"] and p["size"] >= p["cap"] * 0.6)
    if cells:
        target = max(cells[0], cells[len(cells) // 2] * 0.85)
        for p in plans:
            if p["kind"] == "table" and not p["big"] and p["size"] > target:
                p["cap"] = target
                layout(p)
        for p in plans:  # 同一行的格子（表头）用同一个字号，不要一格大一格小
            if p["kind"] == "table" and not p["big"]:
                row = min(q["size"] for q in plans if q["kind"] == "table" and not q["big"] and abs(q["top"] - p["top"]) < 3)
                row = max(row, p["size"] * 0.8)  # 一行里有一格特别挤，别的格子不跟着缩成小字
                if p["size"] > row + 0.01:
                    p["cap"] = row
                    layout(p)

    def crowded_plans():
        return [p for p in plans if p["kind"] != "vertical" and p["size"] < p["cap"] * 0.75 - 0.01]

    # 只改短小标注和挤得看不清（小于 5.5 磅）的表格格子，正文不改写
    tight = [p for p in crowded_plans()
             if p["kind"] == "label" or (p["kind"] == "table" and p["size"] < LAYOUT["min_table"])]
    if tight and shorten and target_lang in LATIN_LANGS:  # 让模型改短
        shorter = shorten([(p["text"], max(4, int(len(p["text"]) * (p["size"] / (p["cap"] * 0.8)) ** 2)))
                           for p in tight])
        for p, t in zip(tight, shorter):
            p["text"] = t
            layout(p)
    for p in crowded_plans():  # 换窄字体、收紧行距
        narrow = fonts[3] if p["bold"] else fonts[2]
        if p["font"] != CJK_FALLBACK and narrow != p["font"]:
            p["font"] = narrow
        p["lead"], p["pitch"] = 1.0, None
        layout(p)

    pdf.setFillColorRGB(0.08, 0.08, 0.08)
    for p in plans:
        pdf.setFont(p["font"], p["size"])
        if p["kind"] == "vertical":  # 图里的竖排小字：整块转 90 度，从上往下读
            x0 = p["left"]
            pdf.saveState()
            pdf.translate(x0, p["top"])
            pdf.rotate(-90)
            draw_text(pdf, p["text"], p["font"], p["size"], p["lines"], 0, p["height"],
                      p["right"] - x0, p["right"] - x0, "left", p["lead_used"])
            pdf.restoreState()
        elif p["kind"] == "vertical-title" or p.get("middle"):
            block_h = len(p["lines"]) * p["size"] * p["lead_used"]
            draw_text(pdf, p["text"], p["font"], p["size"], p["lines"], p["left"], p["right"],
                      p["top"] - max(p["height"] - block_h, 0) / 2, block_h,
                      "center" if p["kind"] == "vertical-title" else p["align"], p["lead_used"])
        else:
            draw_text(pdf, p["text"], p["font"], p["size"], p["lines"], p["left"], p["right"],
                      p["top"], p["height"], p["align"], p["lead_used"])
    if seal_layer is not None:  # 印章、签名叠在最上面，和原件一样压着字
        buf = io.BytesIO()
        seal_layer.save(buf, format="PNG")
        buf.seek(0)
        pdf.drawImage(ImageReader(buf), 0, 0, page_w, page_h, mask="auto")
    pdf.setPageRotation(rotate)  # 横着排版的图纸页：按横向排好字，再转回原来的方向显示
    pdf.showPage()
    return len(crowded_plans())


def translate_pdf_overlay(src, translator, model, client, on_progress=None, on_log=print, dpi=150):
    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas

    model = os.environ.get("ZY_VISION_MODEL") or model
    from providers import load_config
    LAYOUT.update(load_config().get("layout", {}))  # 用户提意见调过的排版设置
    src = Path(src)
    dst = src.with_name(f"{src.stem}_译文.pdf")
    fonts = register_fonts(translator.target_lang)
    doc = pdfium.PdfDocument(str(src))
    pdf = canvas.Canvas(str(dst))
    total, missing, crowded = len(doc), 0, 0
    if not rapid_ocr() and client is None:
        raise SystemExit("This PDF has scanned pages, which need local OCR. "
                         "Install it with: pip install rapidocr_onnxruntime  "
                         "(扫描页需要本地 OCR，请先安装 rapidocr_onnxruntime)")
    on_log("认字用本地 OCR（RapidOCR）。" if rapid_ocr() else "没装 RapidOCR，认字交给视觉模型（可能漏字）。")
    for i in range(total):
        page = doc[i]
        page_w, page_h = page.get_size()
        image = page.render(scale=dpi / 72).to_pil().convert("RGB")
        engine = rapid_ocr()
        turn = sideways(engine, image) if engine else 0
        if turn:  # 整页是横过来的（图纸），转正了再认字、排版
            image = image.rotate(turn, expand=True)
            page_w, page_h = page_h, page_w
        image, _ = deskew(image)
        image, seal_layer = split_seals(image)
        if seal_layer is not None:
            on_log(f"第 {i + 1} 页有印章或签名，已单独抠出，译文写好后叠回原位。")
        image, sign_layer = split_signatures(image) if engine else (image, None)
        if sign_layer is not None:
            on_log(f"第 {i + 1} 页有黑色手写签名压在字上，已单独抠出，译文写好后叠回原位。")
            from PIL import Image as PILImage
            seal_layer = sign_layer if seal_layer is None else PILImage.alpha_composite(sign_layer, seal_layer)
        blocks = ocr_blocks(engine, image) if engine else recognize_all(client, model, image, translator.target_lang)
        if not blocks:
            on_log(f"第 {i + 1} 页没认出文字，原样保留。")
        # 字数上限只给图和表格里的短标注；正文要忠实，只翻译不压缩，放不下靠排版解决
        budgets = ([char_budget(widened(image, b), b["source"], page_w, page_h) if b["kind"] in ("label", "table") else None
                    for b in blocks] if translator.target_lang in LATIN_LANGS else None)
        translations = translate_page(translator, [b["source"] for b in blocks], budgets)
        for b, t in zip(blocks, translations):
            b["text"] = t
            missing += untranslated(b["source"], t, translator.target_lang)
        crowded += draw_page(pdf, image, page_w, page_h, blocks, fonts, translator.target_lang, 
                             lambda items: shorten(translator, items), rotate=turn % 360, seal_layer=seal_layer)
        if on_progress:
            on_progress(i + 1, total)
    pdf.save()
    if missing:
        on_log(f"有 {missing} 块文字没译出来，保留了原文，请核对。")
    if crowded:
        on_log(f"有 {crowded} 块译文比原文长很多，字号缩得比较小，请看一眼。")
    on_log("译文是盖在原页面上的，字号会随方框自动缩小，个别地方可能需要你核对一下。")
    return dst
