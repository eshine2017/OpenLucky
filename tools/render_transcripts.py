# weasyprint (HTML→PDF) + PyMuPDF (PDF→PNG); pure Python, no browser needed.
from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf
import weasyprint

_WIDTH_PX = 800
_PAGE_CSS = weasyprint.CSS(string=f"@page {{ size: {_WIDTH_PX}px auto; margin: 0; }}")


def render_html_to_png(html_path: Path) -> Path:
    pdf_bytes = weasyprint.HTML(filename=str(html_path)).write_pdf(stylesheets=[_PAGE_CSS])
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=96)
    png_path = html_path.with_suffix(".png")
    pix.save(str(png_path))
    return png_path


def render_all(artifacts_dir: Path) -> list[Path]:
    per_test = sorted(p for p in artifacts_dir.glob("*.html") if p.name != "index.html")
    index = artifacts_dir / "index.html"
    queue = per_test + ([index] if index.exists() else [])
    return [render_html_to_png(p) for p in queue]


def main() -> None:
    parser = argparse.ArgumentParser(description="Render e2e transcript HTML files to PNG.")
    parser.add_argument(
        "--artifacts-dir",
        default="tests/e2e/artifacts",
        help="Directory containing transcript HTML files (default: tests/e2e/artifacts)",
    )
    args = parser.parse_args()
    for png in render_all(Path(args.artifacts_dir)):
        print(png)


if __name__ == "__main__":
    main()
