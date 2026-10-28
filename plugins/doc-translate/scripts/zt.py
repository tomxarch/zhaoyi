"""Split a document into segments for translation, and write translations back with the layout kept.

Usage:
  python zt.py extract <file> --lang en [-o segments.json]
  python zt.py apply   <file> <translations.json> --lang en [--segments segments.json] [--bilingual] [-o out]
  python zt.py langs

Supported: .docx  .pdf (scanned or not; OCR overlay)  .xlsx  .html/.htm  .epub  .md  .txt  .json

extract writes a list of {"id", "text", "where", "limit"}:
  text   the source text. It may hold <x1>..</x1> format tags, which must be kept in the translation.
  where  where the text sits (table title and header, page, ...): context only, do not translate it.
  limit  rough max characters for a short label in a scanned PDF (null = no limit).
Texts that need no translation (numbers, model codes, text already in the target language) are left out.

apply reads translations.json as {"<id>": "<translation>"} and writes the output file next to the source
(<name>_译文.<ext>; with --bilingual, <name>_双语.<ext> for docx/txt/md). The source is never overwritten.
Segments without a translation keep their source text and are listed in the report.
"""
import argparse
import os
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

LANGS = {"en": "英文", "zh": "中文", "ja": "日文", "ko": "韩文", "fr": "法文", "de": "德文", "es": "西班牙文",
         "ru": "俄文"}
CONTEXT = re.compile(r"这一批文字的位置（只用来理解意思，不用翻译）：(.*?)。")


def lang_name(code):
    code = code.strip()
    if code in LANGS.values():
        return code
    if code.lower() in LANGS:
        return LANGS[code.lower()]
    raise SystemExit(f"unknown language {code!r}; use one of: {', '.join(LANGS)}")


class Engine:
    """Stands in for the translation model inside the engine: records what it is asked (extract),
    or answers from translations.json (apply)."""
    kind = "mt"
    provider = "plugin"
    workers = 1

    def __init__(self, target_lang, table=None):
        self.target_lang = target_lang
        self.table = table
        self.glossary, self.check_glossary, self.pairs = {}, {}, {}
        self.seen = {}  # text -> {"where", "limit"}
        self.missing = []
        self.where, self.limits = "", {}

    def _one(self, text):
        if self.table is None:
            if text not in self.seen:
                self.seen[text] = {"where": self.where, "limit": self.limits.get(text)}
            return text
        if text in self.table:
            return self.table[text]
        if text not in self.missing:
            self.missing.append(text)
        return text

    def translate_many(self, texts):
        return [self._one(t) for t in texts]

    def translate(self, text, tagged=False):
        return self._one(text)


def run(path, engine, bilingual=False, out=None):
    """Run the file through the engine in a scratch folder, so nothing next to the source is touched.
    Returns the produced file (inside the scratch folder)."""
    import shutil
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="zt_"))
    copy = work / path.name
    shutil.copy2(path, copy)
    return _run(copy, engine, bilingual)


def place_result(produced, source, out=None):
    """Move the produced file next to the source without overwriting anything: name_译文.ext, name_译文2.ext, ..."""
    import shutil
    if out:
        dst = Path(out)
        if dst.exists():
            raise SystemExit(f"{dst} already exists; pick another -o name")
    else:
        stem = produced.stem
        dst = source.with_name(produced.name)
        n = 2
        while dst.exists():
            dst = source.with_name(f"{stem}{n}{produced.suffix}")
            n += 1
    shutil.move(str(produced), dst)
    return dst


def _run(path, engine, bilingual=False):
    """Push the file through the engine's normal pipeline with the Engine as the 'model'."""
    import formats
    import pdf_overlay

    original = pdf_overlay.translate_page
    pages = [0]

    def watched(translator, texts, budgets=None, extra="", prompt=None):
        m = CONTEXT.search(extra or "")
        if prompt is None:  # called page by page from the PDF overlay
            pages[0] += 1
            page_map = getattr(engine, "page_map", None)
            engine.where = f"page {page_map[pages[0] - 1] + 1 if page_map else pages[0]}"
        else:
            engine.where = m.group(1) if m else ""
        engine.limits = {t: b for t, b in zip(texts, budgets or []) if b}
        return original(translator, texts, budgets, extra, prompt)

    pdf_overlay.translate_page = watched
    formats.translate_page = watched
    engine.page_map = None
    suffix = path.suffix.lower()
    formats.forget_document(engine)
    if suffix == ".docx":
        from docx_translate import translate_docx
        dst, notes = translate_docx(path, engine, bilingual=bilingual)
        return Path(dst)
    if suffix == ".pdf":  # text pages keep their vector page; only scanned pages go through OCR
        import pdf_text
        return Path(pdf_text.translate_pdf(path, engine, on_log=lambda m: print(m, file=sys.stderr),
                                           mode=os.environ.get("ZT_PDF_MODE", "auto")))
    if suffix in formats.HANDLERS:
        return Path(formats.translate_file(path, engine, bilingual=bilingual))
    raise SystemExit(f"unsupported file type: {suffix}")


def extract(args):
    path = Path(args.file).resolve()
    engine = Engine(lang_name(args.lang))
    run(path, engine)  # the pass-through copy stays in the scratch folder
    if path.suffix.lower() == ".docx":  # Word: exact place of every paragraph (which table, its header)
        from docx import Document
        from docx_translate import collect_segments, seg_place
        places = {}
        for seg in collect_segments(Document(path))[0]:
            places.setdefault(seg.source, seg_place(seg)[1])
        for t, v in engine.seen.items():
            v["where"] = places.get(t, "")
    segs = [{"id": f"s{n}", "text": t, "where": v["where"], "limit": v["limit"]}
            for n, (t, v) in enumerate(engine.seen.items(), 1)]
    out = Path(args.output or path.with_name(path.stem + ".segments.json"))
    out.write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    from pdf_overlay import detect_source
    print(f"{len(segs)} segments, {sum(len(s['text']) for s in segs)} chars, source language: "
          f"{detect_source(list(engine.seen)) or 'unknown'} -> {out}")


def apply(args):
    path = Path(args.file).resolve()
    seg_path = Path(args.segments or path.with_name(path.stem + ".segments.json"))
    segs = json.loads(seg_path.read_text(encoding="utf-8"))
    got = json.loads(Path(args.translations).read_text(encoding="utf-8"))
    if isinstance(got, list):  # also accept [{"id":..., "translation":...}]
        got = {g["id"]: g.get("translation", g.get("text", "")) for g in got}
    table = {s["text"]: str(got[s["id"]]) for s in segs if isinstance(got.get(s["id"]), str) and got[s["id"]].strip()}
    engine = Engine(lang_name(args.lang), table)
    dst = place_result(run(path, engine, bilingual=args.bilingual), path, args.output)
    print(f"written: {dst}")
    print(f"translated {len(table)}/{len(segs)} segments")
    if engine.missing:
        print(f"{len(engine.missing)} texts kept in the source language (no translation given):")
        for t in engine.missing[:30]:
            print("  -", t[:80])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("file")
    e.add_argument("--lang", required=True, help="target language: en zh ja ko fr de es ru")
    e.add_argument("-o", "--output")
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("translations")
    a.add_argument("--lang", required=True)
    a.add_argument("--segments")
    a.add_argument("--bilingual", action="store_true")
    a.add_argument("-o", "--output")
    sub.add_parser("langs")
    args = ap.parse_args()
    if args.cmd == "langs":
        print(", ".join(f"{k}={v}" for k, v in LANGS.items()))
    else:
        {"extract": extract, "apply": apply}[args.cmd](args)


if __name__ == "__main__":
    main()
