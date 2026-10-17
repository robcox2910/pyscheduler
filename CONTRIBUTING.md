# Contributing to PyScheduler

Thanks for your interest in contributing! This guide covers everything you need
to get started.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.14+ | [python.org](https://www.python.org/downloads/) |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git | 2.x+ | [git-scm.com](https://git-scm.com/) |

## Setup

```bash
# Clone the repo
git clone https://github.com/robcox2910/pyscheduler.git
cd pyscheduler

# Install all dependencies (including dev tools)
uv sync --all-extras

# Install pre-commit hooks (linting + commit message validation)
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

Verify everything works:

```bash
uv run pytest --cov          # tests pass, >= 80% coverage
uv run ruff check src/ tests/  # no lint errors
uv run pyright src tests       # no type errors
```

## TDD Workflow

We follow **Red-Green-Refactor** for every code change:

1. **Red** -- Write a failing test that describes the behaviour you want.
2. **Green** -- Write the simplest code that makes the test pass.
3. **Refactor** -- Clean up while keeping all tests green.

Never skip the red step. If you can't write a failing test first, take a moment
to think about what the code should actually do.

## Branch Naming

Create a branch from `main` using one of these prefixes:

| Prefix | When to use |
|--------|-------------|
| `feat/` | New message queue feature or pattern |
| `fix/` | Bug fix |
| `refactor/` | Restructuring without behaviour change |
| `test/` | Test-only changes |
| `docs/` | Documentation updates |
| `ci/` | CI/CD workflow changes |
| `chore/` | Dependency bumps, config tweaks |

Example: `feat/delayed-messages`, `fix/priority-ordering`

## Conventional Commits

Every commit message must follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <short description in imperative mood>

Optional longer explanation.
```

Types match the branch prefixes: `feat:`, `fix:`, `refactor:`, `test:`,
`docs:`, `ci:`, `chore:`.

Good examples:

```
feat: add priority message queue
fix: resolve dead-letter requeue ordering
docs: add pub/sub concept page
```

Bad examples:

```
updated stuff          # no type, vague description
feat: adds queuing     # not imperative mood ("adds" -> "add")
```

## Code Style

### Modern Python

Use Python 3.14 idioms everywhere:

- `X | Y` for union types (not `Union[X, Y]` or `Optional[X]`)
- `StrEnum` for string enumerations
- `match/case` instead of if/elif chains where appropriate
- Direct imports: `from enum import StrEnum` not `import enum`

### Docstrings

Every public function, method, class, and module needs a docstring:

- Use **imperative mood**: "Return the record." not "Returns the record."
- One-liner for simple functions, multi-line (Google style) for complex ones.

```python
def put(self, body: str) -> Message:
    """Create a message and add it to the queue."""

def reject(self, msg: Message) -> None:
    """Reject an in-flight message, requeuing or dead-lettering it.

    Args:
        msg: The message to reject.
    """
```

### Named Constants

Use named constants instead of magic numbers in tests and source code. Ruff
rule PLR2004 enforces this.

```python
# Good
DEFAULT_MAX_RETRIES = 3
assert msg.retries == DEFAULT_MAX_RETRIES

# Bad
assert msg.retries == 3
```

## Linting and Type Checking

Run these before every commit (pre-commit hooks enforce them automatically):

```bash
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/   # format (or --check to verify)
uv run pyright src tests          # type check (strict mode)
```

Fix all violations. Inline `# noqa:` is acceptable only when tools genuinely
conflict (e.g., ARG002 vs pyright protocol parameter names).

## Testing

```bash
uv run pytest                # run all tests
uv run pytest --cov          # with coverage report
uv run pytest tests/test_x.py  # run a specific file
uv run pytest -k "test_name"   # run tests matching a pattern
```

- Minimum **80% coverage** is enforced.
- Test classes and methods need docstrings too.
- Use `pytest.raises` as a context manager for exception testing.

## Documentation Style

Our docs target a **12-year-old with an interest in computing**. Keep language
simple, use analogies, and avoid jargon without explanation.

Good:

> A message queue is like a bakery counter: the baker puts loaves on the
> counter, and customers take them one at a time. The baker doesn't wait.

Bad:

> A message queue is an asynchronous inter-process communication mechanism
> implementing the producer-consumer pattern with at-least-once delivery.

## Pull Request Process

1. Create a feature branch from `main`.
2. Make your changes following TDD.
3. Ensure all checks pass: `uv run ruff check .`, `uv run pyright src tests`,
   `uv run pytest --cov`.
4. Push your branch and open a PR against `main`.
5. PRs are squash-merged to keep history clean.

## Questions?

Open an issue on [GitHub](https://github.com/robcox2910/pyscheduler/issues) --
we're happy to help!
