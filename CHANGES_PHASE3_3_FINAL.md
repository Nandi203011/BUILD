# Phase 3.3 Final Fixes

## Independent CV

- Added `backend/cv_extraction/site_plan.py`.
- Added `backend/tools/run_cv.py`.
- CV validation is now independent of Vision and does not call the Vision extractor.
- Site-plan geometry is localized before measurement.
- Outer plot and nested building rectangles are reconstructed from vector lines.
- Plot dimensions are anchored to explicit site-plan dimension labels.
- Setbacks are assigned from the spatial gap between the plot and building rectangles.
- Road width comes from an explicit road-width label, never from a road rectangle's pixel width.
- Evidence and provenance are preserved for every measurement.

## Independent Vision

- Added a focused site-plan Vision second pass for independent (`--no-grounding`) validation.
- The second pass is cropped from the Vision model's own SITE_PLAN region, so CV/native text is not used to choose the crop.
- Focus prompt explicitly preserves repeated setback values and assigns front/rear/left/right from spatial position.
- Focus bboxes are remapped to full-page normalized coordinates for auditability.

## Agreement

- `backend/validation.py` now compares the independent CV result against the independent Vision result.
- Legacy `NormalizedPlan` resolution is retained only as a diagnostic regression artifact.
- Conflicts are never averaged or silently resolved.

## Real PDF acceptance

The repository packages `data/test_plans/PLAN2.pdf` and `PLAN2.expected.json`.
The complete suite passes:

`212 passed`


## Area validation update

Independent validation now covers dimensions AND area-derived/labelled fields:
- plot.area (m2)
- building.footprint_area / proposed coverage area (m2)
- coverage (%)
- far.area (m2)
- far (ratio)

CV reads explicit native area statements independently; Vision reads semantic area statements independently. The agreement layer compares them deterministically and never averages conflicts.

PLAN2 expected area evidence: plot 222.83 m2, proposed coverage 174.52 m2, coverage 78.32%, proposed FAR area 386.55 m2, achieved net FAR 1.73.
