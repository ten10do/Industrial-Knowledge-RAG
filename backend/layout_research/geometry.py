from __future__ import annotations

from .models import BBox


def normalize_bbox(
    bbox: BBox | tuple[float, float, float, float] | list[float],
    *,
    page_width: float,
    page_height: float,
    origin: str,
    rotation: int = 0,
) -> tuple[BBox, float, float]:
    """Return top-left-origin coordinates and effective rotated page dimensions.

    Input coordinates describe the unrotated media box. Rotation is clockwise and
    must be one of 0, 90, 180 or 270 degrees.
    """

    box = BBox.from_value(bbox)
    if page_width <= 0 or page_height <= 0:
        raise ValueError("page dimensions must be positive")
    if origin not in {"top-left", "bottom-left"}:
        raise ValueError("origin must be 'top-left' or 'bottom-left'")
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("rotation must be 0, 90, 180 or 270")
    if origin == "bottom-left":
        box = BBox(box.x0, page_height - box.y1, box.x1, page_height - box.y0)

    points = ((box.x0, box.y0), (box.x1, box.y0), (box.x1, box.y1), (box.x0, box.y1))
    if rotation == 0:
        transformed = points
        out_width, out_height = page_width, page_height
    elif rotation == 90:
        transformed = tuple((page_height - y, x) for x, y in points)
        out_width, out_height = page_height, page_width
    elif rotation == 180:
        transformed = tuple((page_width - x, page_height - y) for x, y in points)
        out_width, out_height = page_width, page_height
    else:
        transformed = tuple((y, page_width - x) for x, y in points)
        out_width, out_height = page_height, page_width

    xs = [point[0] for point in transformed]
    ys = [point[1] for point in transformed]
    normalized = BBox(min(xs), min(ys), max(xs), max(ys)).rounded()
    return normalized, out_width, out_height


def intersection_over_union(first: BBox, second: BBox) -> float:
    width = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    height = max(0.0, min(first.y1, second.y1) - max(first.y0, second.y0))
    intersection = width * height
    union = first.area + second.area - intersection
    return intersection / union if union else 0.0
