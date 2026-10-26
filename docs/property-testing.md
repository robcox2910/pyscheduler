# Property Testing (The Clock Tester Who Never Gets Bored)

## The Alarm Clock Tester

Remember the [alarm clock with five dials](concepts/cron.md)? Minute,
hour, day of the month, month and day of the week. You set the dials,
and PyScheduler reads them when it parses a cron expression such as
`*/15 9-17 * * mon-fri`.

How would you check that reading the dials works? The usual way: set the
minute dial to `30`, read it back, see `30`, and smile. That's an
**example test**. It's useful, but it only tells you about the one
setting you thought of.

Now imagine a tester who never gets bored. All night they set the dials
in every way they can think of: one minute, every minute, minutes
written backwards (`45,5,30`), the last day of the month, Sunday as `0`
and Sunday as `7`. For each one they check a single rule:

> Whatever values you set on a dial, reading the dial gives back exactly
> those values.

That rule is a **property**. It doesn't say what any one setting looks
like. It says something that must be true for *every* setting.

When the tester finds a setting that breaks the rule, they don't just
report "it went wrong somewhere around three in the morning". They try
again with less and less, fewer values and smaller numbers, checking it
still goes wrong each time, until it's as simple as it can be. That
boiling-down is called **shrinking**, and it's the best part: the
smallest failing setting usually points straight at the bug.

In Python, the tester is a library called
[Hypothesis](https://hypothesis.readthedocs.io/). PyScheduler's property
tests live in `tests/test_properties.py`, next to the ordinary example
tests.

## A Property from PyScheduler

Here's the rule from above, written as a test:

```python
@given(dials_and_text())
def test_every_dial_holds_exactly_what_you_listed(case):
    """Whatever values you list on each dial, parsing gives back exactly those values."""
    dials, text = case
    expr = CronExpr.parse(text)
    assert expr.minutes == dials["minutes"]
    assert expr.hours == dials["hours"]
    assert expr.days == dials["days"]
    assert expr.months == dials["months"]
    assert expr.weekdays == dials["weekdays"]
```

`@given(dials_and_text())` is the tester's instruction: "invent a random
setting for all five dials, and the cron text that spells it out". The
recipe for that is called a **strategy**. Here are the two small
strategies it's built from:

```python
def dial_values(low, high):
    """Make any non-empty set of values that fit on a dial running low to high."""
    return st.frozensets(st.integers(low, high), min_size=1)


def written_as_list(values):
    """Write values as a cron list ("5,1,30") in any order, the way a person might."""
    return st.permutations(sorted(values)).map(lambda order: ",".join(map(str, order)))
```

`dial_values(0, 59)` makes things like `{30}`, `{0, 59}` or forty random
minutes. `written_as_list` writes them out in a shuffled order, because
a person might type `45,5,30` instead of `5,30,45`, and the parser
mustn't care.

Hypothesis runs the test a hundred times with a hundred different
settings. If the parser ever reads back something different from what
was written, the test fails and Hypothesis shrinks the setting.

## More Rules the Clock Must Keep

The same file checks a few more rules, each one short:

- **Fire times come out once each, earliest first.** Every hour on the
  hour dial paired with every minute on the minute dial is a fire time,
  with none missing, none doubled, and in order.
- **A range with a step counts like Python.** `10-40/7` on the minute
  dial means exactly the minutes in `range(10, 41, 7)`: 10, 17, 24, 31
  and 38.
- **A name means its number.** `mar` and `3` set the month dial the same
  way, and `wed` and `3` set the weekday dial the same way.
- **Sunday is both `0` and `7`**, as in classic cron.

## Catching a Bug on Purpose

To prove the tester is awake, we broke the parser deliberately. In
`_expand`, which turns `10-40` into a set of numbers, we changed

```text
return set(range(start, end + 1, step))
```

to forget the `+ 1`, so every range quietly lost its last value. Four
of the six properties failed at once, and Hypothesis shrank the problem
to the simplest dial setting there is:

```text
assert frozenset() == frozenset({0})

Failing test case: test_every_dial_holds_exactly_what_you_listed(
    case=({'minutes': frozenset([0]),
      'hours': frozenset([0]),
      'days': frozenset([1]),
      'months': frozenset([1]),
      'weekdays': frozenset([0])},
     '0 0 1 1 0'),
)
```

Every dial set to its smallest value, `0 0 1 1 0`, and the minute dial,
which should hold `0`, read back as nothing at all. With a setting that
small, the missing `+ 1` is easy to spot. The bug has since been
removed again, and the properties guard against it coming back.

## Run It Yourself

```bash
uv run pytest tests/test_properties.py
```

Every run uses the same hundred examples, so a green build means the
same thing on every machine. To hunt harder before a release, try two
thousand fresh random examples:

```bash
uv run pytest tests/test_properties.py --hypothesis-profile=thorough
```

## Your Turn

Here's a rule you could add: **the month dial never rings in a month you
didn't set.** Make a random month dial and a random date, and check that
`matches_day` is `False` whenever the date's month isn't on the dial:

<!-- no-run -->
```python
@given(dial_values(1, 12), st.dates())
def test_never_rings_in_a_month_you_did_not_set(months, day):
    """A date in a month that isn't on the month dial never matches."""
    text = "0 0 * " + ",".join(map(str, sorted(months))) + " *"
    if day.month not in months:
        assert not CronExpr.parse(text).matches_day(day)
```

Try breaking `matches_day` on purpose and watch Hypothesis find the
smallest date that proves it.
