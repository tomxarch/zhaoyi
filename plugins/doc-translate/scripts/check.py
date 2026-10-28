"""Check translations before writing them back.

Usage: python check.py <segments.json> <translations.json> --lang en

Flags, per segment:
  missing      no translation given
  not-text     the value is not plain text (looks like JSON / a list) - never write that into a file
  numbers      a number of 2+ digits from the source is missing (often: the text belongs to another cell)
  untranslated still in the source language (unchanged, or only fragments / source words left)
  tags         <xN> format tags lost or changed
  options      several alternatives ("A / B", "A or B") where the source has one
  same-as      two different sources next to each other got the same translation (possible shift/merge)
  length       translation is far shorter/longer than the source (possible shift/merge)
Exit code 1 when anything is flagged.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))
from pdf_overlay import CJK, FOREIGN_MARKS, numbers_lost, untranslated  # noqa: E402
from zt import lang_name  # noqa: E402

TAGS = re.compile(r"</?x\d+>")


def problems(src, tr, lang):
    if not isinstance(tr, str) or not tr.strip():
        return ["missing"]
    if re.match(r"^\s*[\[{]\s*['\"]", tr):
        return ["not-text"]
    out = []
    if numbers_lost(src, tr):
        out.append("numbers")
    code_only = tr.strip() == src.strip() and not re.search(r"[a-z]{3}|[\u4e00-\u9fff]", src)  # USD 3,200, Q-2611, 12kV
    if not code_only and untranslated(src, tr, lang) or (tr.strip() == src.strip() and lang == "英文" and FOREIGN_MARKS.search(src)):
        out.append("untranslated")
    if sorted(TAGS.findall(src)) != sorted(TAGS.findall(tr)):
        out.append("tags")
    if re.search(r"\s/\s|\bor\b|或", tr) and not re.search(r"/|\bou\b|\bor\b|或|oder|\bo\b", src):
        out.append("options")
    plain = lambda t: len(TAGS.sub("", t))
    s, t = plain(src), plain(tr)
    cjk_src = bool(CJK.search(src)) and not CJK.search(tr)
    cjk_tr = bool(CJK.search(tr)) and not CJK.search(src)
    # Chinese -> Latin text gets ~2.5x longer; Latin -> Chinese ~0.35x
    ratio = t / max(1, s) / (2.5 if cjk_src else 0.35 if cjk_tr else 1)
    if s >= 25 and (ratio < 0.35 or ratio > 3.5):
        out.append("length")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("segments")
    ap.add_argument("translations")
    ap.add_argument("--lang", required=True)
    args = ap.parse_args()
    lang = lang_name(args.lang)
    segs = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    got = json.loads(Path(args.translations).read_text(encoding="utf-8"))
    flagged = []
    prev = None
    for seg in segs:
        tr = got.get(seg["id"])
        found = problems(seg["text"], tr, lang)
        if prev and isinstance(tr, str) and tr.strip() and tr == got.get(prev["id"]) and seg["text"] != prev["text"] \
                and seg.get("where") == prev.get("where"):
            found.append(f"same-as {prev['id']}")
        if found:
            flagged.append((seg, tr, found))
        prev = seg
    for seg, tr, found in flagged:
        print(f"{seg['id']}: {', '.join(found)}\n   src: {seg['text'][:100]}\n   got: {str(tr)[:100]}")
    print(f"\n{len(flagged)} of {len(segs)} segments flagged")
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()
