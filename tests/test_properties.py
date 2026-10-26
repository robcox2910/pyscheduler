"""Property-based tests -- rules that must hold for *every* input.

An ordinary test checks one example someone thought of: "``*/15 9-17 * * *``
fires at quarter past nine". A property test states a rule instead, such as
"whatever minutes you list, the minute dial holds exactly those minutes",
and lets Hypothesis invent hundreds of cron expressions to try to break it.
If one breaks the rule, Hypothesis *shrinks* it down to the smallest
expression that still fails, and shows you that.

Each test below says its rule in plain English in its docstring. The
strategies at the top are recipes for making random inputs; each one is
named for what it makes. See ``docs/property-testing.md`` for the
walk-through.
"""

from hypothesis import given
from hypothesis import strategies as st

from pyscheduler.schedule.cron import DAY_NAMES, MONTH_NAMES, CronExpr

FIRST_MINUTE, LAST_MINUTE = 0, 59
FIRST_HOUR, LAST_HOUR = 0, 23
FIRST_DAY, LAST_DAY = 1, 31
FIRST_MONTH, LAST_MONTH = 1, 12
SUNDAY, SATURDAY, SUNDAY_AGAIN = 0, 6, 7

# --- Strategies: recipes for random inputs -----------------------------------


def dial_values(low: int, high: int) -> st.SearchStrategy[frozenset[int]]:
    """Make any non-empty set of values that fit on a dial running ``low`` to ``high``."""
    return st.frozensets(st.integers(low, high), min_size=1)


def written_as_list(values: frozenset[int]) -> st.SearchStrategy[str]:
    """Write ``values`` as a cron list (``"5,1,30"``) in any order, the way a person might."""
    return st.permutations(sorted(values)).map(lambda order: ",".join(map(str, order)))


@st.composite
def any_dials(draw: st.DrawFn) -> dict[str, frozenset[int]]:
    """Make a random setting for each of the five dials."""
    return {
        "minutes": draw(dial_values(FIRST_MINUTE, LAST_MINUTE)),
        "hours": draw(dial_values(FIRST_HOUR, LAST_HOUR)),
        "days": draw(dial_values(FIRST_DAY, LAST_DAY)),
        "months": draw(dial_values(FIRST_MONTH, LAST_MONTH)),
        "weekdays": draw(dial_values(SUNDAY, SATURDAY)),
    }


@st.composite
def dials_and_text(draw: st.DrawFn) -> tuple[dict[str, frozenset[int]], str]:
    """Make a dial setting and the cron text that spells it out."""
    dials = draw(any_dials())
    order = ("minutes", "hours", "days", "months", "weekdays")
    fields = [draw(written_as_list(dials[name])) for name in order]
    return dials, " ".join(fields)


# --- Properties --------------------------------------------------------------


@given(dials_and_text())
def test_every_dial_holds_exactly_what_you_listed(
    case: tuple[dict[str, frozenset[int]], str],
) -> None:
    """Whatever values you list on each dial, parsing gives back exactly those values."""
    dials, text = case
    expr = CronExpr.parse(text)
    assert expr.minutes == dials["minutes"]
    assert expr.hours == dials["hours"]
    assert expr.days == dials["days"]
    assert expr.months == dials["months"]
    assert expr.weekdays == dials["weekdays"]


@given(dials_and_text())
def test_times_come_out_once_each_in_order(case: tuple[dict[str, frozenset[int]], str]) -> None:
    """Every hour-and-minute pair on the dials is a fire time, once each, earliest first."""
    dials, text = case
    times = CronExpr.parse(text).times()
    pairs = [(t.hour, t.minute) for t in times]
    expected = sorted((h, m) for h in dials["hours"] for m in dials["minutes"])
    assert pairs == expected


@given(
    st.integers(FIRST_MINUTE, LAST_MINUTE),
    st.integers(FIRST_MINUTE, LAST_MINUTE),
    st.integers(1, LAST_MINUTE + 1),
)
def test_a_range_with_a_step_counts_like_python(start: int, length: int, step: int) -> None:
    """``a-b/s`` on the minute dial means the same minutes as ``range(a, b + 1, s)``."""
    end = min(start + length, LAST_MINUTE)
    expr = CronExpr.parse(f"{start}-{end}/{step} * * * *")
    assert expr.minutes == frozenset(range(start, end + 1, step))


@given(st.sampled_from(sorted(MONTH_NAMES)))
def test_a_month_name_means_its_number(name: str) -> None:
    """Writing a month as a name (``mar``) or a number (``3``) sets the same dial."""
    by_name = CronExpr.parse(f"0 0 1 {name} *")
    by_number = CronExpr.parse(f"0 0 1 {MONTH_NAMES[name]} *")
    assert by_name.months == by_number.months


@given(st.sampled_from(sorted(DAY_NAMES)))
def test_a_day_name_means_its_number(name: str) -> None:
    """Writing a weekday as a name (``wed``) or a number (``3``) sets the same dial."""
    by_name = CronExpr.parse(f"0 0 * * {name}")
    by_number = CronExpr.parse(f"0 0 * * {DAY_NAMES[name]}")
    assert by_name.weekdays == by_number.weekdays


def test_sunday_is_both_zero_and_seven() -> None:
    """Sunday can be written ``0`` or ``7``, as in classic cron, and both mean the same day."""
    zero = CronExpr.parse(f"0 0 * * {SUNDAY}")
    seven = CronExpr.parse(f"0 0 * * {SUNDAY_AGAIN}")
    assert zero.weekdays == seven.weekdays == frozenset({SUNDAY})
