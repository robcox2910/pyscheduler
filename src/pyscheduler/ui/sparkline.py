"""A sparkline: a tiny line chart with no axes, drawn as inline SVG.

Like the squiggle a doctor's monitor draws for a heartbeat: you do not
read numbers off it, you just see at a glance whether things are steady,
creeping up or suddenly spiking. Values are spread left to right in the
order given and scaled so the smallest sits at the bottom and the
largest at the top.
"""

from collections.abc import Sequence

from pyscheduler.ui.html import Safe, h

__all__ = ["sparkline"]

PAD = 1.0
"""Space kept free around the line so its stroke is not clipped."""


def _points(values: Sequence[float], width: int, height: int) -> list[tuple[float, float]]:
    """Return the ``(x, y)`` of each value inside the box (y grows downwards)."""
    low, high = min(values), max(values)
    span = high - low
    top, bottom = PAD, height - PAD
    step = (width - 2 * PAD) / (len(values) - 1) if len(values) > 1 else 0.0
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        level = 0.5 if span == 0 else (value - low) / span
        points.append((PAD + index * step, bottom - level * (bottom - top)))
    return points


def sparkline(values: Sequence[float], *, width: int = 120, height: int = 24) -> Safe:
    """Return an inline ``<svg>`` line through ``values``, oldest first.

    No values gives an empty SVG (no path). One value gives a flat line
    across the box; equal values do the same.
    """
    shape: Safe | None = None
    if values:
        points = _points(values, width, height)
        if len(points) == 1:
            _, y = points[0]
            points = [(PAD, y), (width - PAD, y)]
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(points))
        shape = h("path", {"d": d, "fill": "none", "stroke": "currentColor", "stroke-width": "1.5"})
    return h(
        "svg",
        {
            "class": "sparkline",
            "role": "img",
            "aria-label": "recent durations",
            "width": str(width),
            "height": str(height),
            "viewBox": f"0 0 {width} {height}",
        },
        shape,
    )
