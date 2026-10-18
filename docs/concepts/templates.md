# Templates and Values (Form Letters and Labels)

## The Form Letter

A teacher sends the same letter to every parent: "Dear {parent}, {child}
did well in {subject} on {date}." The words in curly braces are blanks.
For each family the teacher fills the blanks in. The letter never changes;
only the blanks do.

PyScheduler uses the same idea for commands and log file paths. A job
can say where its log goes with blanks, and each run fills them in:

```python
from datetime import date

from pyscheduler.templates import placeholders_in, render

template = "{log_dir}/{job}/{run_date:%Y%m%d}.out"
print(placeholders_in(template))
print(render(template, {"log_dir": "/logs", "job": "backup", "run_date": date(2026, 10, 17)}))
```

The blanks PyScheduler knows are `job`, `run_date`, `run_id`, `attempt`,
`scheduled_at` and `log_dir`. A date with no format shows as `2026-10-17`;
add `:%Y%m%d` and it shows as `20261017`. To write a real curly brace, double
it: `{{` and `}}`.

## Secrets From the Environment

Passwords do not belong in a config file. Write `${DB_PASSWORD}` and the
value is taken from the environment when the file is loaded. Write `$$` for
a real dollar sign. A missing variable is an error, not an empty string.

The template fields are `command` (and each item of a command list),
`cwd`, the values of `env`, `stdout` and `stderr`: their blanks are filled
in for every attempt. A password may well contain `{` or `}`, so when a
`${NAME}` lands in a template field its braces are doubled first, and the
value comes out exactly as it went in (it is never mistaken for a blank).

The scheduler never shows an expanded value back: in a problem message or
in the API, each one (4 characters or longer) is put back as `${NAME}`.

```python
from pyscheduler.templates import expand_env

print(expand_env("pass=${DB_PASSWORD} cost=$$5", {"DB_PASSWORD": "hunter2"}))
# pass=hunter2 cost=$5
print(expand_env("pass=${DB_PASSWORD}", {"DB_PASSWORD": "a{b}c"}, template=True))
# pass=a{{b}}c, which a template renders back as pass=a{b}c
```

## Labels That Must Look Right

Durations, times, dates and names are checked strictly, like a label
maker that refuses a typo:

```python
from pyscheduler.values import check_name, parse_date, parse_duration, parse_time

print(parse_duration("1h30m"))  # 1:30:00
print(parse_time("07:05"))  # 07:05:00
print(parse_date("2026-10-17"))  # 2026-10-17
check_name("load-warehouse_2")  # fine; "a.b" would raise ValueError
```

Durations use `d`, `h`, `m` and `s`, each at most once and in that order,
and must add up to more than zero.
