# Phase 3.3 — Semantic dimension resolution

## Problem fixed

The previous pipeline let unrelated CV geometry dominate the normalized values. On a multi-view architectural sheet this caused the page border, elevation/section rectangles, floor-plan rectangles, room dimensions, and site-plan dimensions to compete as if they were the same entity.

The new pipeline separates three jobs:

1. **Vision model:** decides what a dimension means (`PLOT_WIDTH`, `BUILDING_DEPTH`, etc.).
2. **Native PDF text/dimension layer:** verifies that the printed value really exists on the page and supplies the page-space text/line anchors.
3. **Geometry:** validates/constructs spatial frames and computes derived values.

## Important changes

- Leading-dot dimensions such as `.46` and `.47` are now parsed as numeric dimensions.
- `Dimension` stores the original printed-text bounding box in addition to its associated geometry line. This prevents an incorrectly associated CAD line from moving a setback annotation to another region.
- When grounded vision semantics exist, plot/building width and depth use the semantic value instead of averaging unrelated geometry candidates.
- Site/building frames can be reconstructed from the positions of the semantic dimension labels when OpenCV does not emit a closed polygon.
- Plot area is computed from resolved plot width × depth.
- Building footprint prefers an explicit grounded `PLINTH_AREA`; otherwise it uses resolved building width × depth; only then does it fall back to polygon area.
- Floor count can be obtained from distinct VLM floor-plan regions.
- Road text is sufficient to determine front orientation, but a road width is not inferred from the size of a ROAD rectangle. Road width remains missing unless explicitly dimensioned or semantically grounded.
- If a semantic site frame exists, setback values without explicit site-plan evidence are reported as missing rather than retaining a noisy CV distance.

## ARJUN regression using the checked-in cached vision result

The supplied ARJUN sheet contains site dimensions `12.19 × 9.14`, building dimensions `11.72 × 8.22`, four floor-plan regions, plinth area `96.34`, and printed small site dimensions `.46`/`.47`. The normalized result produced by this phase is:

- plot: `12.19 × 9.14 m`
- plot area: `111.4166 m²`
- building: `11.72 × 8.22 m`
- building/plinth area: `96.34 m²`
- floor count: `4`
- front setback: `0.47 m`
- rear setback: missing (no explicit printed rear setback)
- left setback: `0.46 m`
- right setback: `0.46 m`
- coverage: `86.4683%`
- diagnostic gross FAR: `3.4587`
- road width: missing (no explicit road-width dimension)

These are extraction outputs, not regulatory conclusions. FAR and coverage are still computed by the deterministic normalization layer; municipal compliance remains downstream.
