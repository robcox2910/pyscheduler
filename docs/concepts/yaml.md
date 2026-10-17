# YAML (A Recipe Card With Indents)

## The Recipe Card

Think of a recipe card stuck on the fridge. It has a title, a list of
ingredients, and some steps. Nobody uses brackets or semicolons. You can
see what belongs to what because of **how far in** each line is written:

```
Pancakes
    flour: 200g
    milk: 300ml
    steps:
        - mix
        - fry
```

`flour` and `milk` are indented under `Pancakes`, so they are part of it.
`mix` and `fry` are indented under `steps`, so they are the steps.

YAML is exactly that. PyScheduler's job files are written in it, so
learning to read a recipe card means you can read a job file.

## The Pieces

| You write | It means |
| --- | --- |
| `name: nightly` | a **key** and its value |
| a line starting with `- ` | one item in a **list** |
| `[a, b]` and `{x: 1}` | a list or a map squeezed onto one line |
| `"text"` or `'text'` | text in quotes, so special characters are safe |
| `# note` | a comment: the reader skips it |
| `\|` and `>` | a block of text; `\|` keeps the line breaks, `>` joins lines with spaces |

Rules the reader enforces so mistakes show up early:

- Indent with **spaces only**. A tab in the indent is an error.
- Each key may appear **once** in a map.
- One document per file. A single `---` at the top is fine.
- No anchors (`&x`), aliases (`*x`), tags (`!!str`) or `? ` keys. Real YAML
  has them; we leave them out on purpose to keep the reader small and safe.

## Everything Starts As Text

Here is a classic trap. In many YAML readers, this list of country codes:

```
- GB
- NO
- SE
```

gives you `GB`, **`False`** and `SE`. The reader decided that `NO` means
"no", so it turned it into a boolean. People call this the **Norway
problem**. Likewise `08:00` can turn into a number of minutes, and `1e3`
into `1000.0`.

Our reader refuses to guess. **Every plain value comes back as text**,
exactly as written, plus a note saying whether it was in quotes. The code
that *uses* the value decides what it should be, and it can give a proper
error message with the line number when `retries: lots` is not a number.

## Try It

```python
from pyscheduler.yamlish import Map, Scalar, Seq, parse

card = parse(
    """
name: pancakes
code: NO          # stays the text "NO"
time: 08:00       # stays the text "08:00"
steps:
  - mix
  - fry
"""
)

assert isinstance(card, Map)
code = card.items["code"]
assert isinstance(code, Scalar)
print(code.value, code.line)  # NO 3

steps = card.items["steps"]
assert isinstance(steps, Seq)
print([s.value for s in steps.items if isinstance(s, Scalar)])  # ['mix', 'fry']
```

Every node (`Scalar`, `Seq`, `Map`) carries the **line number** it came
from, and a `Map` remembers the line of each key. When something is wrong
later, the error can say `jobs.yaml:12: unknown field 'retrys'` instead of
"something is wrong somewhere".

Problems in the YAML itself raise `YamlError`, which prints as
`filename:line: message`:

```python
from pyscheduler.yamlish import YamlError, parse

try:
    parse("a: 1\na: 2\n", "jobs.yaml")
except YamlError as err:
    print(err)  # jobs.yaml:2: duplicate key 'a'
```
