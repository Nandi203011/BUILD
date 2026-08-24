# BUILDCheck India — Phase 2 Notes

**Owner:** Teammate 2
**Scope:** architectural-plan extraction layer only (`backend/cv_extraction/`).
No regulatory compliance, RAG/RASE, or RuleEngine logic is implemented here.

## What was built

```
backend/cv_extraction/
    raw_types.py          Internal pre-contract intermediate representation
                           (PageMetadata, RawTextItem, RawLine, RawRectangle,
                           RawPolygon, DimensionCandidate,
                           CoordinateTransformRecord, RawExtractionBundle)
    coordinates.py         Raster<->page-point conversion, rotation normalization
    pdf_native.py           PyMuPDF vector geometry + text-with-bbox extraction
    ocr_fallback.py         Tesseract OCR fallback for scanned/insufficient pages
    opencv_geometry.py      OpenCV line/contour/connected-component evidence
    dimension_candidates.py Regex-based numeric-annotation detection (unresolved)
    candidate_geometry.py   First-pass Plot/Building/Road candidate heuristics
    pdf_extractor.py        PDFHybridExtractor(GeometryExtractor) — orchestrates
                             everything above and folds it into ExtractionResult
    registry.py              Extension -> extractor registry
    debug_overlay.py         Visual debug PNG rendering per page
```

Tests: `tests/test_pdf_native_extraction.py`, `test_ocr_fallback.py`,
`test_opencv_geometry.py`, `test_coordinate_transform.py`,
`test_dimension_candidates.py`, `test_pdf_extractor_end_to_end.py`,
`test_debug_overlay.py`, using synthetic fixture PDFs in
`tests/fixtures/pdf_builders.py` (no real sample plans were committed to
`data/test_plans/` yet — drop real BBMP plan PDFs there and the same
tests/extractor will run against them unchanged).

## How the hybrid pipeline works

Per page:

1. **PDF-native pass** (`pdf_native.py`): PyMuPDF `get_text("dict")` for
   text spans with bounding boxes, font size, and orientation;
   `get_drawings()` for vector lines/rectangles/polygons. Never flattens
   to plain text.
2. **Native-text sufficiency check**: if a page has fewer than
   `MIN_NATIVE_TEXT_CHARS` non-whitespace characters of native text, it's
   flagged `is_scanned` and OCR fires (`ocr_fallback.needs_ocr`).
3. **OCR fallback** (`ocr_fallback.py`): rasterizes the page via PyMuPDF's
   `get_pixmap`, runs `pytesseract.image_to_data` for word-level
   text + bbox + confidence, converts pixel coords back to page points.
4. **OpenCV evidence** (`opencv_geometry.py`): runs on every page's raster
   (not just scanned ones) — Canny + probabilistic Hough transform for
   lines, Otsu threshold + contour detection for polygons/rectangles.
   This is always an *evidence source*, merged alongside vector geometry,
   never used to override it.
5. **Coordinate bookkeeping** (`coordinates.py`): every raster-space
   detection is converted to PAGE_POINTS using the DPI it was rasterized
   at; a `CoordinateTransformRecord` per page preserves DPI, rotation,
   page size, and a *provisional* page->metre scale (from
   `settings.default_points_per_metre`) for downstream normalization to
   use or override.
6. **Dimension candidates** (`dimension_candidates.py`): regex-scans every
   text item (native + OCR) for plausible numeric measurements (plain
   units, feet-inches marks). Never assigns semantic meaning
   (no FRONT_SETBACK/PLOT_WIDTH/etc.) — that's Phase 3's job.
7. **Candidate geometry** (`candidate_geometry.py`): rough, LOW-confidence
   `PlotCandidate`/`BuildingCandidate`/`RoadCandidate` proposals from
   closed-polygon size/containment heuristics and "road" text proximity.
   `value` is deliberately left `None` (only `raw_value` in page points +
   geometry + evidence are populated) since real-world magnitude requires
   a resolved scale, which spatial reasoning owns.

`pdf_extractor.PDFHybridExtractor.extract()` is the only method that
matters to the rest of the system — it returns exactly an
`ExtractionResult` (schemas/extraction.py), nothing else.
`extract_with_debug()` additionally returns the `RawExtractionBundle` for
tests and `debug_overlay.render_debug_overlays()`.

## Known limitations / what Phase 3 should expect

- Candidate geometry heuristics are intentionally rough and will often
  emit multiple competing candidates per region (by design — see
  `ARCHITECTURE.md`'s "resolution happens next"). Phase 3 should not
  assume exactly one plot/building/road candidate per document.
- `provisional_points_per_metre` on extracted geometry is a config
  fallback, not a real scale reading — do not treat `ValueField.value`
  on candidate width/depth/area as final; they are `None` on purpose.
- OCR requires the `tesseract` binary on `PATH` (see
  `requirements.txt` — swapping in PaddleOCR only requires a new function
  with the same `list[RawTextItem]` return shape in `ocr_fallback.py`).
- Dimension-candidate unit detection is regex-based and deliberately
  conservative; ambiguous/unit-less numbers are still surfaced (as
  low-confidence candidates), never silently dropped or promoted.

## Running it

```
pip install -r requirements.txt
pytest tests/ -v
```
