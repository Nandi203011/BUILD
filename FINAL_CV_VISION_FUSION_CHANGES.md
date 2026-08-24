# Final CV + Vision Extraction/Fusion Changes

## What changed

### Independent CV
- The independent site-plan extractor is now used by the full pipeline as the authoritative CV/native/OCR evidence path.
- The legacy global candidate pool can no longer overwrite validated site-plan values.
- Raster OpenCV fallback is used for scanned/mixed pages when vector geometry is unavailable.
- OpenCV line detection now prefers LSD and falls back to HoughLinesP.
- Site-plan dimensions/setbacks remain spatially associated with the plot/building region.
- Explicit area statement values remain independent evidence: plot area, plinth/footprint area, coverage, FAR area, FAR, and total built-up area.
- Optional domain-specific PyTorch hook was added in `backend/cv_extraction/deep_geometry.py`. Generic torchvision weights are deliberately not treated as architectural truth without domain-specific weights.

### Vision
- Vision semantic types now include building/floor/plinth/parapet height and floor count.
- Site-plan prompts were tightened for small setback decimals, spatial side assignment, dimension-vs-setback separation, and explicit area statements.
- The existing focused site-plan second pass remains independent from CV/native text grounding.

### Final CV + Vision agreement
- Added `backend/spatial_reasoning/final_fusion.py`.
- Length fields and numeric area/FAR fields are reconciled independently.
- If CV and Vision agree within tolerance: `FINAL:CV+VISION_AGREED`.
- If only CV has evidence: `FINAL:CV_ONLY`.
- If only Vision has evidence: `FINAL:VISION_ONLY`.
- If they conflict: final value is **null** and the conflict is preserved; neither source is silently selected.
- `run_vision.py` now emits `final_agreed_values` alongside the raw Vision result and independent CV result.
- `run_validation.py` now prints and stores the same final agreement object.
- The full `run_pipeline.py` output uses the agreement layer whenever independent CV evidence is available.

## PLAN2 acceptance
The full pipeline now returns the independent-CV values instead of the old global 454-candidate resolver values:

- plot width: 12.192 m
- plot depth: 18.28 m
- building width: 10.5952 m
- building depth: 16.4822 m
- front setback: 1.00 m
- rear setback: 0.80 m
- left setback: 0.80 m
- right setback: 0.80 m
- road width: 9.20 m
- plot area: 222.83 m²
- building/plinth footprint: 174.52 m²
- coverage: 78.32%
- FAR area: 386.55 m²
- FAR: 1.73
- total built-up area: 579.90 m²

## Validation
25 targeted tests passed after the changes, and the full repository test suite reached 218 passed tests before the test runner process lingered during environment shutdown.
