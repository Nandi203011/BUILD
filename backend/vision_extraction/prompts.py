ARCHITECTURAL_PLAN_PROMPT = r"""
You are analyzing one page of an Indian architectural/building plan.

Your job is semantic interpretation, not blind OCR. Identify drawing regions and
associate visible dimensions with the correct region.

Possible region types include:
SITE_PLAN, GROUND_FLOOR_PLAN, FIRST_FLOOR_PLAN, SECOND_FLOOR_PLAN,
THIRD_FLOOR_PLAN, OTHER_FLOOR_PLAN, AREA_STATEMENT, ELEVATION, SECTION,
ROAD, PARKING, TITLE_BLOCK, SERVICE_DETAIL, OTHER.

For dimensions, use semantic types such as:
PLOT_WIDTH, PLOT_DEPTH, BUILDING_WIDTH, BUILDING_DEPTH, ROAD_WIDTH, BUILDING_HEIGHT,
FLOOR_HEIGHT, PLINTH_HEIGHT, PARAPET_HEIGHT,
FRONT_SETBACK, REAR_SETBACK, LEFT_SETBACK, RIGHT_SETBACK, ROOM_DIMENSION,
PARKING_DIMENSION, BUILDING_HEIGHT, FLOOR_HEIGHT, PLINTH_HEIGHT, PARAPET_HEIGHT, FLOOR_COUNT,
OTHER_DIMENSION, UNKNOWN.

For explicitly labelled areas/coverage/FAR values, use semantic area types:
PLOT_AREA, NET_PLOT_AREA, BUILDING_FOOTPRINT_AREA, PROPOSED_COVERAGE_AREA, PLINTH_AREA,
COVERAGE_PERCENT, FAR_AREA, FAR_RATIO, TOTAL_BUILT_UP_AREA, BUILT_UP_AREA, PLINTH_AREA, UNKNOWN_AREA.

Rules:
1. Do not assume the largest rectangle is the plot.
2. Do not treat room dimensions as plot/building dimensions.
3. Do not calculate a dimension that is not visibly supported.
4. Prefer dimensions whose text and arrows/boundaries are visually associated.
5. A value such as 9.14 and a value such as 8.22 may belong to different regions.
6. Return null when uncertain rather than inventing a value.
7. Bounding boxes use normalized image coordinates [x1,y1,x2,y2] on a 0-1000 grid.
8. Return ONLY valid JSON. No markdown fences.
9. Read each region's dimensions independently, from that region's own pixels only.
   Never reuse or infer a width/depth value from a different region (e.g. a floor
   plan) just because you expect a site plan and a floor plan to match. If the
   same building is drawn rotated between two regions, its printed width/depth
   labels may legitimately swap between the two regions -- report exactly what is
   printed in THIS region, even if it looks inconsistent with another region.
10. FRONT_SETBACK, REAR_SETBACK, LEFT_SETBACK, and RIGHT_SETBACK must only be
    emitted when a numeric label is printed directly on a dimension line drawn
    between the plot boundary and the building boundary on that edge. Do not
    output a "typical" or regulation-looking setback value (e.g. 3.0, 1.5) unless
    you can point to the exact printed digits for it in `evidence`. If no such
    label exists, omit the dimension entirely rather than estimating one.
11. The `evidence` field must be the literal digits/text you read at that bbox,
    not a description of what the dimension represents.
12. Before finishing, inspect the SITE_PLAN corners specifically for small leading-dot
    dimensions such as `.46` and `.47`. These are valid printed dimensions even though
    they do not have a leading zero. Associate each with the corresponding plot/building
    gap and emit FRONT_SETBACK/REAR_SETBACK/LEFT_SETBACK/RIGHT_SETBACK when the side
    can be determined from the road/front orientation.
13. ROAD_WIDTH must be emitted only when a numeric road-width dimension is actually
    printed. The mere presence of a large rectangle labeled ROAD is not enough to
    estimate its width.
12. Setback labels are almost always SMALL decimals (well under 2m on typical
    urban plots) printed right at a corner where the plot boundary and
    building boundary are close together — often as two stacked values at a
    single corner (e.g. ".46" above ".47"). They are visually and numerically
    distinct from PLOT_WIDTH/PLOT_DEPTH/BUILDING_WIDTH/BUILDING_DEPTH, which
    run the full length of an edge. NEVER assign a value you have already
    used for PLOT_WIDTH, PLOT_DEPTH, BUILDING_WIDTH, or BUILDING_DEPTH to a
    setback type too — if you cannot find a distinct, separately-printed
    small value for a given side's setback, omit it.

Return this structure:
{
  "page_number": PAGE_NUMBER,
  "units": null,
  "scale": null,
  "regions": [
    {"id":"region_1","type":"SITE_PLAN","bbox":[0,0,0,0],"confidence":0.0,"label":null,"evidence":null}
  ],
  "dimensions": [
    {"value":0.0,"unit":"m","type":"PLOT_WIDTH","region_id":"region_1","bbox":[0,0,0,0],"evidence":"9.14","confidence":0.0}
  ],
  "areas": [
    {"value":0.0,"unit":"m2","type":"PLOT_AREA","region_id":"region_1","evidence":"222.83","confidence":0.0}
  ],
  "warnings": []
}
"""

SITE_PLAN_FOCUS_PROMPT = r"""
You are given a CROPPED SITE PLAN from an Indian architectural drawing.
This is a focused second-pass Vision extraction. Use only the pixels in this crop.
Do not use knowledge of regulations and do not invent values.

The crop normally contains:
- an outer PLOT/SITE boundary,
- an inner PROPOSED BUILDING boundary,
- dimension labels around the outer boundary,
- small setback labels in the gaps between the two rectangles,
- a ROAD label and its numeric width.

Extract these fields whenever visibly supported:
PLOT_WIDTH, PLOT_DEPTH, BUILDING_WIDTH, BUILDING_DEPTH, ROAD_WIDTH, BUILDING_HEIGHT,
FLOOR_HEIGHT, PLINTH_HEIGHT, PARAPET_HEIGHT,
FRONT_SETBACK, REAR_SETBACK, LEFT_SETBACK, RIGHT_SETBACK.

Also extract explicitly labelled area/coverage/FAR values visible in the crop, using:
PLOT_AREA, NET_PLOT_AREA, BUILDING_FOOTPRINT_AREA, PROPOSED_COVERAGE_AREA, PLINTH_AREA,
COVERAGE_PERCENT, FAR_AREA, FAR_RATIO, TOTAL_BUILT_UP_AREA. Do not calculate an
area from dimensions unless the prompt explicitly marks it as DERIVED in evidence;
prefer the printed area statement when one is visible.

CRITICAL SETBACK RULES:
1. Do NOT require the printed number to say "front", "rear", "left", or "right".
2. Determine the side from spatial position relative to the OUTER plot boundary
   and INNER proposed-building boundary.
3. A number in the gap ABOVE the building is REAR_SETBACK.
4. A number in the gap BELOW the building, adjacent to the road/frontage, is FRONT_SETBACK.
5. A number in the gap LEFT of the building is LEFT_SETBACK.
6. A number in the gap RIGHT of the building is RIGHT_SETBACK.
7. Preserve repeated values. Three separate 0.80 labels are three separate pieces
   of evidence and must not be deduplicated.
8. Do not reuse plot/building dimensions as setbacks.
9. Read small decimals such as 0.80, .80, 1.00 exactly as printed.
10. For BUILDING_WIDTH/DEPTH, use an explicit building dimension if visible in the
    crop. If it is not printed but the inner rectangle is clearly visible and its
    dimensions can be directly derived from the visible plot dimensions and visible
    setback gaps, you may derive it, but set the evidence to the component values and
    confidence lower than a directly printed dimension.
11. ROAD_WIDTH must come from a printed road-width dimension such as "9.20m WIDE ROAD".
12. Never infer a setback from a regulation or from a typical value.
13. Every dimension must include a bbox around the actual printed digits or the exact
    geometry used for a clearly marked derived value.
14. Return ONLY valid JSON.

Return:
{
  "page_number": PAGE_NUMBER,
  "units": "m",
  "scale": null,
  "regions": [
    {"id":"site_focus","type":"SITE_PLAN","bbox":[0,0,1000,1000],"confidence":0.99,"label":"SITE PLAN","evidence":""}
  ],
  "dimensions": [
    {"value":0.0,"unit":"m","type":"PLOT_WIDTH","region_id":"site_focus","bbox":[0,0,0,0],"evidence":"12.19","confidence":0.0}
  ],
  "areas": [
    {"value":0.0,"unit":"m2","type":"PLOT_AREA","region_id":"site_focus","evidence":"222.83","confidence":0.0}
  ],
  "warnings": []
}
"""
