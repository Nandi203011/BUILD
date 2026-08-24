from __future__ import annotations

from pathlib import Path

import fitz


def render_pdf_pages(pdf_path: Path, output_dir: Path, dpi: float = 200.0) -> list[dict]:
    """Render PDF pages to PNG files and return page metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages: list[dict] = []

    with fitz.open(pdf_path) as doc:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            output_path = output_dir / f"page_{page_index + 1}.png"
            pix.save(str(output_path))
            pages.append({
                "page_number": page_index + 1,
                "image_path": str(output_path),
                "width_px": pix.width,
                "height_px": pix.height,
            })
    return pages
