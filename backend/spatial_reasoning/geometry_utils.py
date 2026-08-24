"""
Generic polygon/segment geometry helpers used throughout spatial reasoning.

Deliberately dependency-free (no shapely) to match the rest of the repo.
Everything here is a pure function of `backend.schemas.geometry` primitives
and works in whatever coordinate space it is given (PAGE_POINTS or
METRIC_PLAN) — callers are responsible for converting to metric plan
space before treating a distance as a real-world metre value.
"""

from __future__ import annotations

import math
from typing import Sequence

from backend.schemas.geometry import BoundingBox, Line, Point, Polygon

Edge = Line


def polygon_edges(polygon: Polygon) -> list[Edge]:
    """Ordered ring edges of a polygon, including the closing edge."""
    pts = polygon.points
    n = len(pts)
    return [Line(start=pts[i], end=pts[(i + 1) % n]) for i in range(n)]


def polygon_perimeter(polygon: Polygon) -> float:
    return sum(e.length for e in polygon_edges(polygon))


def polygon_centroid(polygon: Polygon) -> Point:
    """Area-weighted (shoelace) centroid; falls back to vertex average for degenerate polygons."""
    pts = polygon.points
    n = len(pts)
    a_sum = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        j = (i + 1) % n
        cross = pts[i].x * pts[j].y - pts[j].x * pts[i].y
        a_sum += cross
        cx += (pts[i].x + pts[j].x) * cross
        cy += (pts[i].y + pts[j].y) * cross
    a_sum /= 2.0
    if abs(a_sum) < 1e-9:
        return Point(x=sum(p.x for p in pts) / n, y=sum(p.y for p in pts) / n)
    cx /= 6.0 * a_sum
    cy /= 6.0 * a_sum
    return Point(x=cx, y=cy)


def is_valid_polygon(
    polygon: Polygon,
    min_vertices: int = 3,
    min_area: float = 1e-6,
    max_perimeter_area_ratio: float = 1e9,
) -> bool:
    """
    General polygon validity check — deliberately NOT just
    ``polygon.area / bbox.area`` (rectangularity), which says nothing
    about degenerate/duplicate vertices or self-intersection.

    Rejects:
      - fewer than `min_vertices` distinct points
      - near-zero area (collapsed/degenerate)
      - extreme perimeter^2/area ratio (slivers, spikes, near-duplicate
        back-and-forth vertices)
      - self-intersecting rings (a simple, non-crossing ring is required)
    """
    pts = polygon.points
    if len(pts) < min_vertices:
        return False

    # Duplicate-point collapse: distinct points (within a tiny epsilon).
    distinct: list[Point] = []
    for p in pts:
        if not any(math.hypot(p.x - q.x, p.y - q.y) < 1e-6 for q in distinct):
            distinct.append(p)
    if len(distinct) < min_vertices:
        return False

    area = abs(polygon.area)
    if area < min_area:
        return False

    perimeter = polygon_perimeter(polygon)
    if area > 0:
        ratio = (perimeter * perimeter) / area
        if ratio > max_perimeter_area_ratio:
            return False

    if _polygon_self_intersects(polygon):
        return False

    return True


def _polygon_self_intersects(polygon: Polygon) -> bool:
    edges = polygon_edges(polygon)
    n = len(edges)
    if n < 4:
        return False
    for i in range(n):
        for j in range(i + 1, n):
            # Adjacent edges (including the wrap-around pair) share a
            # vertex by construction — that's not a self-intersection.
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _segments_intersect(edges[i], edges[j]):
                return True
    return False


def is_page_frame_like(
    bbox: BoundingBox,
    page_width: float,
    page_height: float,
    touch_tolerance: float = 4.0,
    area_fraction_threshold: float = 0.92,
) -> bool:
    """
    True if `bbox` looks like the drawing-sheet/page border rather than a
    real plot/site boundary: it both (a) touches or nearly touches the
    page edge on at least two sides, and (b) covers a very large fraction
    of the total page area. Neither condition alone is sufficient — a
    genuinely large plot that happens to touch one edge should not be
    rejected, and a small candidate pinned to a corner should not be
    rejected either.
    """
    if page_width <= 0 or page_height <= 0:
        return False

    page_area = page_width * page_height
    frac = (bbox.width * bbox.height) / page_area if page_area else 0.0
    if frac < area_fraction_threshold:
        return False

    touches = 0
    if bbox.min_x <= touch_tolerance:
        touches += 1
    if bbox.min_y <= touch_tolerance:
        touches += 1
    if bbox.max_x >= page_width - touch_tolerance:
        touches += 1
    if bbox.max_y >= page_height - touch_tolerance:
        touches += 1

    return touches >= 2


def dedupe_bboxes(
    items: Sequence[tuple[object, BoundingBox]], tolerance: float = 2.0
) -> list[tuple[object, BoundingBox]]:
    """
    Remove near-duplicate geometry (same item type paired with its
    bounding box) — keeps the first occurrence of each cluster of
    bounding boxes within `tolerance` page-points of each other on all
    four sides.
    """
    kept: list[tuple[object, BoundingBox]] = []
    for item, bbox in items:
        is_dup = False
        for _, kept_bbox in kept:
            if (
                abs(bbox.min_x - kept_bbox.min_x) <= tolerance
                and abs(bbox.min_y - kept_bbox.min_y) <= tolerance
                and abs(bbox.max_x - kept_bbox.max_x) <= tolerance
                and abs(bbox.max_y - kept_bbox.max_y) <= tolerance
            ):
                is_dup = True
                break
        if not is_dup:
            kept.append((item, bbox))
    return kept


def rectangularity(polygon: Polygon) -> float:
    """polygon_area / bounding_box_area, in [0, 1] for simple polygons. 1.0 = perfect rectangle."""
    bbox = polygon.bounding_box
    bbox_area = bbox.width * bbox.height
    if bbox_area <= 0:
        return 0.0
    return min(1.0, polygon.area / bbox_area)


def aspect_ratio(bbox: BoundingBox) -> float:
    """Long side / short side. Returns +inf for degenerate (zero-height/width) boxes."""
    w, h = bbox.width, bbox.height
    if min(w, h) <= 0:
        return math.inf
    return max(w, h) / min(w, h)


def point_segment_distance(p: Point, seg: Line) -> float:
    ax, ay = seg.start.x, seg.start.y
    bx, by = seg.end.x, seg.end.y
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(p.x - ax, p.y - ay)
    t = max(0.0, min(1.0, ((p.x - ax) * dx + (p.y - ay) * dy) / length_sq))
    proj_x, proj_y = ax + t * dx, ay + t * dy
    return math.hypot(p.x - proj_x, p.y - proj_y)


def segment_segment_distance(a: Line, b: Line) -> float:
    """Minimum distance between two finite segments (0 if they intersect)."""
    if _segments_intersect(a, b):
        return 0.0
    return min(
        point_segment_distance(a.start, b),
        point_segment_distance(a.end, b),
        point_segment_distance(b.start, a),
        point_segment_distance(b.end, a),
    )


def _orientation(p: Point, q: Point, r: Point) -> float:
    return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    return (
        min(p.x, r.x) - 1e-9 <= q.x <= max(p.x, r.x) + 1e-9
        and min(p.y, r.y) - 1e-9 <= q.y <= max(p.y, r.y) + 1e-9
    )


def _segments_intersect(a: Line, b: Line) -> bool:
    p1, q1, p2, q2 = a.start, a.end, b.start, b.end
    o1 = _orientation(p1, q1, p2)
    o2 = _orientation(p1, q1, q2)
    o3 = _orientation(p2, q2, p1)
    o4 = _orientation(p2, q2, q1)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 != 0 and o2 != 0:
        return True
    if abs(o1) < 1e-9 and _on_segment(p1, p2, q1):
        return True
    if abs(o2) < 1e-9 and _on_segment(p1, q2, q1):
        return True
    if abs(o3) < 1e-9 and _on_segment(p2, p1, q2):
        return True
    if abs(o4) < 1e-9 and _on_segment(p2, q1, q2):
        return True
    return False


def point_in_polygon(p: Point, polygon: Polygon) -> bool:
    """Standard ray-casting point-in-polygon test."""
    pts = polygon.points
    n = len(pts)
    inside = False
    x, y = p.x, p.y
    x1, y1 = pts[-1].x, pts[-1].y
    for i in range(n):
        x2, y2 = pts[i].x, pts[i].y
        if ((y1 > y) != (y2 > y)) and (
            x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
        ):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def point_to_polygon_boundary_distance(p: Point, polygon: Polygon) -> float:
    return min(point_segment_distance(p, e) for e in polygon_edges(polygon))


def polygon_to_edge_distance(inner: Polygon, edge: Edge) -> float:
    """
    Min distance from an (assumed interior) polygon to a single boundary
    edge of an outer polygon — used for building-to-plot-edge setbacks.

    Considers both inner vertices and inner edges vs the target edge, so
    it is correct whether the nearest approach is vertex-to-edge or
    edge-to-edge (near-parallel walls).
    """
    vertex_min = min(point_segment_distance(v, edge) for v in inner.points)
    edge_min = min(segment_segment_distance(e, edge) for e in polygon_edges(inner))
    return min(vertex_min, edge_min)


def polygon_to_edges_distance(inner: Polygon, edges: Sequence[Edge]) -> float:
    """Min distance from `inner` to any edge in a group of boundary edges (one 'side')."""
    if not edges:
        return math.inf
    return min(polygon_to_edge_distance(inner, e) for e in edges)


def scale_polygon(polygon: Polygon, points_per_metre: float) -> Polygon:
    """Convert a PAGE_POINTS polygon into METRIC_PLAN, dividing by the scale factor."""
    if points_per_metre <= 0:
        raise ValueError("points_per_metre must be > 0")
    return Polygon(
        points=[Point(x=pt.x / points_per_metre, y=pt.y / points_per_metre) for pt in polygon.points]
    )


def scale_bbox(bbox: BoundingBox, points_per_metre: float) -> BoundingBox:
    if points_per_metre <= 0:
        raise ValueError("points_per_metre must be > 0")
    return BoundingBox(
        min_x=bbox.min_x / points_per_metre,
        min_y=bbox.min_y / points_per_metre,
        max_x=bbox.max_x / points_per_metre,
        max_y=bbox.max_y / points_per_metre,
    )


def scale_line(line: Line, points_per_metre: float) -> Line:
    if points_per_metre <= 0:
        raise ValueError("points_per_metre must be > 0")
    return Line(
        start=Point(x=line.start.x / points_per_metre, y=line.start.y / points_per_metre),
        end=Point(x=line.end.x / points_per_metre, y=line.end.y / points_per_metre),
    )


def bbox_to_polygon(bbox: BoundingBox) -> Polygon:
    return Polygon(
        points=[
            Point(x=bbox.min_x, y=bbox.min_y),
            Point(x=bbox.max_x, y=bbox.min_y),
            Point(x=bbox.max_x, y=bbox.max_y),
            Point(x=bbox.min_x, y=bbox.max_y),
        ]
    )


def line_orientation_degrees(line: Line) -> float:
    """Orientation of a line, in degrees, folded into [0, 180) (direction, not sense)."""
    dx, dy = line.end.x - line.start.x, line.end.y - line.start.y
    return math.degrees(math.atan2(dy, dx)) % 180.0


def orientation_alignment(a_deg: float, b_deg: float) -> float:
    """1.0 = perfectly parallel, 0.0 = perfectly perpendicular (both mod-180 degrees)."""
    diff = abs(a_deg - b_deg) % 180.0
    diff = min(diff, 180.0 - diff)
    return 1.0 - (diff / 90.0)


def edge_midpoint(edge: Edge) -> Point:
    return Point(x=(edge.start.x + edge.end.x) / 2.0, y=(edge.start.y + edge.end.y) / 2.0)


def outward_normal(edge: Edge, centroid: Point) -> tuple[float, float]:
    """
    Unit outward-pointing normal of a polygon edge, given the polygon's
    centroid (used to disambiguate winding order without assuming it).
    """
    dx, dy = edge.end.x - edge.start.x, edge.end.y - edge.start.y
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return (0.0, 0.0)
    # two candidate normals (perpendicular to the edge)
    n1 = (-dy / length, dx / length)
    n2 = (dy / length, -dx / length)
    mid = edge_midpoint(edge)
    to_mid = (mid.x - centroid.x, mid.y - centroid.y)
    # outward normal is the one pointing away from the centroid
    if n1[0] * to_mid[0] + n1[1] * to_mid[1] >= 0:
        return n1
    return n2


__all__ = [
    "Edge",
    "polygon_edges",
    "polygon_perimeter",
    "polygon_centroid",
    "is_valid_polygon",
    "is_page_frame_like",
    "dedupe_bboxes",
    "rectangularity",
    "aspect_ratio",
    "point_segment_distance",
    "segment_segment_distance",
    "point_in_polygon",
    "point_to_polygon_boundary_distance",
    "polygon_to_edge_distance",
    "polygon_to_edges_distance",
    "scale_polygon",
    "scale_bbox",
    "scale_line",
    "bbox_to_polygon",
    "line_orientation_degrees",
    "orientation_alignment",
    "edge_midpoint",
    "outward_normal",
]
