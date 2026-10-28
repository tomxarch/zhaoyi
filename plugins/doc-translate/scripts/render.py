"""Render the original and the translated file side by side, page by page, for a visual check.

Usage: python render.py <original> <translated> [--pages 1,2,5] [-o outdir]
PDF works directly. DOCX/XLSX/HTML are first converted to PDF with LibreOffice (soffice) if it is installed.
Writes compare_<n>.png images (left: original, right: translation). Look at every page you deliver.
"""
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def to_pdf(path, work):
    if path.suffix.lower() == ".pdf":
        return path
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise SystemExit("needs LibreOffice (soffice) to render non-PDF files")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(work), str(path)],
                   check=True, capture_output=True, timeout=300)
    return work / (path.stem + ".pdf")


def main():
    import pypdfium2 as pdfium
    from PIL import Image
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original")
    ap.add_argument("translated")
    ap.add_argument("--pages", help="1-based page numbers, comma separated (default: all, max 30)")
    ap.add_argument("-o", "--outdir", default=".")
    args = ap.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        a = pdfium.PdfDocument(str(to_pdf(Path(args.original), Path(tmp) / "a")))
        b = pdfium.PdfDocument(str(to_pdf(Path(args.translated), Path(tmp) / "b")))
        pages = [int(p) for p in args.pages.split(",")] if args.pages else list(range(1, min(len(a), 30) + 1))
        if len(a) != len(b):
            print(f"note: page count differs ({len(a)} vs {len(b)})")
        for n in pages:
            if n > len(a) or n > len(b):
                continue
            left = a[n - 1].render(scale=1.4).to_pil()
            right = b[n - 1].render(scale=1.4).to_pil()
            img = Image.new("RGB", (left.width + right.width + 20, max(left.height, right.height)), "white")
            img.paste(left, (0, 0))
            img.paste(right, (left.width + 20, 0))
            path = out / f"compare_{n}.png"
            img.save(path)
            print(path)


if __name__ == "__main__":
    main()
