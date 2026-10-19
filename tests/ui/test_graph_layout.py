"""Test the graph layout: layers, ordering, edge paths and awkward graphs (Review Focus 5)."""

import time
from collections.abc import Mapping, Sequence
from itertools import pairwise

from pyscheduler.ui.graph_layout import (
    LAYER_GAP,
    MARGIN,
    NODE_H,
    NODE_W,
    ROW_GAP,
    Edge,
    Layout,
    layout,
    node_corner,
)

SUCCESS = "success"
CHAIN = 200
CHAIN_LIMIT = 0.5
"""Seconds a 200-job chain may take to lay out."""
LAST_LAYER = 2


def _dep(up: str, down: str, offset: int = 0, on: str = SUCCESS) -> Edge:
    """Return the edge ``up -> down``."""
    return Edge(up=up, down=down, on=on, offset=offset)


def _crossings(positions: Mapping[str, tuple[int, int]], edges: Sequence[Edge]) -> int:
    """Return how many pairs of edges between adjacent layers cross."""
    spans = [
        (positions[e.up], positions[e.down])
        for e in edges
        if positions[e.down][0] == positions[e.up][0] + 1
    ]
    count = 0
    for i, (a_up, a_down) in enumerate(spans):
        for b_up, b_down in spans[i + 1 :]:
            if a_up[0] != b_up[0]:
                continue
            if (a_up[1] - b_up[1]) * (a_down[1] - b_down[1]) < 0:
                count += 1
    return count


def _positioned(result: Layout, nodes: Sequence[str]) -> None:
    """Assert every node has its own place and the box holds them all."""
    assert set(result.positions) == set(nodes)
    assert len(set(result.positions.values())) == len(nodes)
    for place in result.positions.values():
        x, y = node_corner(place)
        assert x + NODE_W <= result.width
        assert y + NODE_H <= result.height


class TestLayers:
    """Layers come from the longest path over same-date edges only."""

    def test_longest_path_layers(self) -> None:
        """With a->b->c and a->c, c sits on layer 2 (the longest path wins)."""
        edges = [_dep("a", "b"), _dep("b", "c"), _dep("a", "c")]
        result = layout(["c", "b", "a"], edges)
        layers = {name: place[0] for name, place in result.positions.items()}
        assert layers == {"a": 0, "b": 1, "c": LAST_LAYER}

    def test_cross_date_edges_ignored_for_layers(self) -> None:
        """A run-date offset edge does not push its downstream to a later layer."""
        result = layout(["a", "b"], [_dep("a", "b", offset=-1)])
        assert result.positions == {"a": (0, 0), "b": (0, 1)}
        assert set(result.paths) == {_dep("a", "b", offset=-1)}


class TestOrdering:
    """Barycentre sweeps untangle crossings, deterministically."""

    def test_barycentre_reduces_crossings(self) -> None:
        """a->y and b->x cross in name order; the sweeps swap x and y."""
        edges = [_dep("a", "y"), _dep("b", "x")]
        nodes = ["a", "b", "x", "y"]
        assert _crossings({"a": (0, 0), "b": (0, 1), "x": (1, 0), "y": (1, 1)}, edges) == 1
        result = layout(nodes, edges)
        assert _crossings(result.positions, edges) == 0
        assert layout(list(reversed(nodes)), list(reversed(edges))) == result

    def test_bigger_crossing_case(self) -> None:
        """Two fans that cross in name order end with no crossings."""
        edges = [_dep("a", "z"), _dep("a", "y"), _dep("b", "x"), _dep("b", "w"), _dep("c", "v")]
        nodes = ["a", "b", "c", "v", "w", "x", "y", "z"]
        result = layout(nodes, edges)
        assert _crossings(result.positions, edges) == 0
        _positioned(result, nodes)


class TestGeometry:
    """Sizes and edge paths follow the fixed node and gap sizes."""

    def test_box_and_paths(self) -> None:
        """The box fits two layers of one row; the path runs right edge to left edge."""
        edge = _dep("a", "b")
        result = layout(["a", "b"], [edge])
        assert result.width == 2 * MARGIN + 2 * NODE_W + LAYER_GAP
        assert result.height == 2 * MARGIN + NODE_H
        x1, y1 = MARGIN + NODE_W, MARGIN + NODE_H // 2
        x2 = MARGIN + NODE_W + LAYER_GAP
        middle = (x1 + x2) // 2
        assert result.paths[edge] == f"M{x1} {y1} C{middle} {y1} {middle} {y1} {x2} {y1}"
        assert node_corner((1, 2)) == (x2, MARGIN + 2 * (NODE_H + ROW_GAP))

    def test_empty_and_unknown_ends(self) -> None:
        """No nodes gives an empty box; an edge to an unknown job is left out."""
        empty = layout([], [])
        assert empty.positions == {}
        assert empty.width == 2 * MARGIN
        result = layout(["a"], [_dep("ghost", "a")])
        assert result.positions == {"a": (0, 0)}
        assert result.paths == {}


class TestAwkward:
    """Graphs that are awkward to lay out still finish with every node placed."""

    def test_awkward_graphs(self) -> None:
        """Self -1, a->b(-1) with b->a, a disconnected job and a 200-job chain all lay out."""
        self_loop = _dep("a", "a", offset=-1)
        result = layout(["a"], [self_loop])
        _positioned(result, ["a"])
        assert result.paths[self_loop].startswith(f"M{MARGIN + NODE_W} ")

        loop = [_dep("a", "b", offset=-1), _dep("b", "a")]
        result = layout(["a", "b", "lonely"], loop)
        _positioned(result, ["a", "b", "lonely"])
        assert result.positions["b"][0] == 0
        assert result.positions["a"][0] == 1
        assert set(result.paths) == set(loop)

        names = [f"job{n:03d}" for n in range(CHAIN)]
        chain = [_dep(up, down) for up, down in pairwise(names)]
        began = time.perf_counter()
        result = layout(list(reversed(names)), chain)
        took = time.perf_counter() - began
        assert took < CHAIN_LIMIT, took
        _positioned(result, names)
        assert result.positions[names[-1]] == (CHAIN - 1, 0)

    def test_same_date_cycle_still_finishes(self) -> None:
        """A same-date cycle (which validation forbids) is broken by name, not looped on."""
        nodes = ["a", "b", "c"]
        result = layout(nodes, [_dep("a", "b"), _dep("b", "c"), _dep("c", "a")])
        _positioned(result, nodes)
