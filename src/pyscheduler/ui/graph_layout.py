"""Lay out the dependency graph: layers left to right, rows top to bottom (P3-R11).

Think of a **relay race drawn on paper**. Every runner (job) stands in
a column (a layer): the first runners on the left, and each runner one
column to the right of the furthest runner who hands them the baton.
Then, column by column, each runner steps to the average height of the
runners they are linked to (their *barycentre*), so the baton lines
cross as little as possible. Four passes, left to right and back again,
are usually enough.

Only same-date dependencies (offset 0) decide the columns. A link to an
earlier run date (``run_date: -1``) can point backwards, even at the job
itself, so it is drawn (dashed) but never moves anyone; that is why a
job waiting on its own yesterday cannot send the layout round in circles.

This module is pure: names in, positions and SVG paths out. The same
input always gives the same output (ties are settled by name).
"""

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "LAYER_GAP",
    "MARGIN",
    "NODE_H",
    "NODE_W",
    "ROW_GAP",
    "Edge",
    "Layout",
    "layout",
    "node_corner",
]

NODE_W = 160
"""The width of a job's box (px)."""

NODE_H = 36
"""The height of a job's box (px)."""

LAYER_GAP = 80
"""The space between two layers (px)."""

ROW_GAP = 16
"""The space between two rows of one layer (px)."""

MARGIN = 16
"""The space kept free around the drawing (px), so loops are not clipped."""


@dataclass(frozen=True)
class Edge:
    """One dependency: ``down`` waits for ``up``.

    Attributes:
        up: The upstream job.
        down: The downstream job.
        on: Which result counts (``success``, ``failure`` or ``done``).
        offset: The run-date offset (0 for the same date, negative for earlier ones).

    """

    up: str
    down: str
    on: str
    offset: int


@dataclass(frozen=True)
class Layout:
    """Where everything goes.

    Attributes:
        positions: Each job's ``(layer, order)``: its column and its row in it.
        paths: Each drawn edge's SVG ``d`` string.
        width: The drawing's width (px).
        height: The drawing's height (px).

    """

    positions: Mapping[str, tuple[int, int]]
    paths: Mapping[Edge, str]
    width: int
    height: int


def node_corner(position: tuple[int, int]) -> tuple[int, int]:
    """Return the top-left ``(x, y)`` of the box at ``(layer, order)``."""
    layer, order = position
    return MARGIN + layer * (NODE_W + LAYER_GAP), MARGIN + order * (NODE_H + ROW_GAP)


def _links(
    names: Sequence[str], pairs: Sequence[tuple[str, str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return each name's upstream and downstream neighbours over ``pairs``."""
    ups: dict[str, list[str]] = {name: [] for name in names}
    downs: dict[str, list[str]] = {name: [] for name in names}
    for up, down in pairs:
        ups[down].append(up)
        downs[up].append(down)
    return ups, downs


def _layers(
    names: Sequence[str], ups: Mapping[str, list[str]], downs: Mapping[str, list[str]]
) -> dict[str, int]:
    """Return each name's layer: one past its deepest upstream (longest path).

    Names are taken in topological order, smallest name first among the
    ready ones. A same-date cycle (validation forbids one, so this is
    only a guard) is broken by releasing the smallest waiting name; the
    edge that closes the loop then simply does not move anyone.
    """
    waiting = {name: len(ups[name]) for name in names}
    layer = dict.fromkeys(names, 0)
    ready = [name for name in names if not waiting[name]]
    heapq.heapify(ready)
    done: set[str] = set()
    left = iter(names)  # names in order, for breaking cycles
    while len(done) < len(names):
        if not ready:
            heapq.heappush(ready, next(name for name in left if name not in done))
        name = heapq.heappop(ready)  # each name is pushed once: done names are never released
        done.add(name)
        for down in downs[name]:
            if down in done:
                continue
            layer[down] = max(layer[down], layer[name] + 1)
            waiting[down] -= 1
            if not waiting[down]:
                heapq.heappush(ready, down)
    return layer


def _rows(
    names: Sequence[str],
    layer: Mapping[str, int],
    ups: Mapping[str, list[str]],
    downs: Mapping[str, list[str]],
    sweeps: int,
) -> dict[str, int]:
    """Return each name's row in its layer after ``sweeps`` barycentre sweeps (down, up, ...)."""
    depth = max(layer.values(), default=-1) + 1
    columns: list[list[str]] = [[] for _ in range(depth)]
    for name in names:  # names are sorted, so each column starts in name order
        columns[layer[name]].append(name)
    row = {name: index for column in columns for index, name in enumerate(column)}
    for sweep in range(sweeps):
        downward = sweep % 2 == 0
        linked = ups if downward else downs
        span = range(1, depth) if downward else range(depth - 2, -1, -1)
        for index in span:
            column = columns[index]

            def centre(name: str, linked: Mapping[str, list[str]] = linked) -> tuple[float, str]:
                """Return the mean row of the name's neighbours (its own row if it has none)."""
                others = linked[name]
                if not others:
                    return float(row[name]), name
                return sum(row[other] for other in others) / len(others), name

            column.sort(key=centre)
            row.update({name: place for place, name in enumerate(column)})
    return row


def _path(start: tuple[int, int], end: tuple[int, int]) -> str:
    """Return the cubic Bézier from the right edge of ``start``'s box to the left edge of ``end``'s."""
    x1, y1 = start[0] + NODE_W, start[1] + NODE_H // 2
    x2, y2 = end[0], end[1] + NODE_H // 2
    if start == end:  # a job waiting on its own earlier run: a loop over its box
        lift = y1 - NODE_H
        return f"M{x1} {y1} C{x1 + MARGIN} {lift} {x2 - MARGIN} {lift} {x2} {y2}"
    reach = max(abs(x2 - x1) // 2, LAYER_GAP // 2)
    lift = NODE_H if x2 <= x1 and y1 == y2 else 0  # backwards on one row: go over the boxes
    return f"M{x1} {y1} C{x1 + reach} {y1 - lift} {x2 - reach} {y2 - lift} {x2} {y2}"


def layout(nodes: Sequence[str], edges: Sequence[Edge], *, sweeps: int = 4) -> Layout:
    """Return where each job and edge goes (P3-R11).

    Args:
        nodes: The job names (duplicates are ignored).
        edges: The dependencies; one whose end is not in ``nodes`` is left out.
        sweeps: How many barycentre sweeps order the rows (down, up, down, up).

    Layers come from the longest path over offset-0 edges only; every
    edge (any offset) gets a path. Each sweep costs time in proportion
    to the edges plus sorting each layer.

    """
    names = sorted(set(nodes))
    known = set(names)
    drawn = [edge for edge in edges if edge.up in known and edge.down in known]
    same = sorted({(e.up, e.down) for e in drawn if e.offset == 0 and e.up != e.down})
    ups, downs = _links(names, same)
    layer = _layers(names, ups, downs)
    row = _rows(names, layer, ups, downs, sweeps)
    positions = {name: (layer[name], row[name]) for name in names}
    corners = {name: node_corner(place) for name, place in positions.items()}
    paths = {edge: _path(corners[edge.up], corners[edge.down]) for edge in drawn}
    depth = max(layer.values(), default=-1) + 1
    height_rows = max(row.values(), default=-1) + 1
    width = 2 * MARGIN + depth * NODE_W + max(depth - 1, 0) * LAYER_GAP
    height = 2 * MARGIN + height_rows * NODE_H + max(height_rows - 1, 0) * ROW_GAP
    return Layout(positions=positions, paths=paths, width=width, height=height)
