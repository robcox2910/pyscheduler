# Graph Layout (The Seating Plan)

## Who Sits Where

Imagine seating guests at a long wedding table plan. Guests who must be
introduced by someone else sit further along. Nobody should sit before
the person who introduces them. Then you shuffle each column up and
down so that friends sit close, and the lines of "who knows whom" don't
tangle into spaghetti.

The dependency graph does exactly that with jobs. It has two steps.

## Step 1: Layers (Columns)

A job goes in the column **one past the furthest job it waits for**.
Jobs with no dependencies are in column 0. This is called
*longest-path layering*, and it guarantees every arrow points left to
right.

Only dependencies on the **same run date** (offset 0) decide columns.
A job that waits for *yesterday's* run of something (`run_date: -1`) can
point backwards, even at itself, and would send the layout round in
circles. Those edges are still drawn, **dashed**, but they never move
anyone.

## Step 2: Rows (Barycentres)

Within a column, order matters for tidiness. The **barycentre** of a job
is the average row of the jobs it is linked to. Going column by column,
each job is sorted by its barycentre, so linked jobs line up. Four
sweeps (down, up, down, up) are plenty. Ties are settled by name, so the
same input always draws the same picture.

## Reading the Picture

- Edge by `on`: success is solid, failure is red and dashed, done is grey.
- A node is filled with the state of that date's latest run, or "no run".
- `?focus=job` lights up the job's whole upstream and downstream (done
  on the server, with no JavaScript); `?tag=` keeps one tag's jobs.

## Try It

```python
from pyscheduler.ui.graph_layout import Edge, layout

edges = [
    Edge("extract", "transform", "success", 0),
    Edge("transform", "load", "success", 0),
    Edge("fetch-rates", "load", "success", 0),
    Edge("load", "cleanup", "failure", 0),
]
nodes = ["cleanup", "load", "fetch-rates", "transform", "extract"]
result = layout(nodes, edges)
for name, (layer, row) in sorted(result.positions.items(), key=lambda kv: kv[1]):
    print(f"column {layer}, row {row}: {name}")
# column 0, row 0: extract
# column 0, row 1: fetch-rates
# column 1, row 0: transform
# column 2, row 0: load
# column 3, row 0: cleanup

# the same input in any order draws the same picture
assert layout(list(reversed(nodes)), list(reversed(edges))) == result
```
