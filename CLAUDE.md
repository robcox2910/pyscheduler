# Claude Code Instructions

<!-- standard:start -->
## Code Style

- All public functions, methods, classes, and modules **must** have a docstring
- Use imperative mood: "Return the result." not "Returns the result."
- One-line docstrings for simple functions; multi-line with summary + details for complex ones
- Follow the Google docstring convention (D211, D212 selected via ruff)

## Linting

- Ruff enforces a comprehensive rule set -- run `uv run ruff check .` before committing
- Pyright runs in strict mode -- run `uv run pyright src tests` before committing
- All violations must be fixed, not ignored. Exceptions are the repo-owned `ignore` and `per-file-ignores` lists in `pyproject.toml`, which belong to this repo; the series sync never edits them

## Testing

- TDD workflow: Red-Green-Refactor
- Coverage is enforced by pytest-cov; the floor in `pyproject.toml` is repo-owned
- Test classes and methods also require docstrings
- Use named constants instead of magic values (PLR2004)
- Run `uv run pytest` before committing
- Property-based tests (Hypothesis) live in `tests/test_properties.py` and `tests/test_model.py`; the shared profiles are in the root `conftest.py` (owned by `series sync`). Run `uv run pytest --hypothesis-profile=thorough` before a release.

## Commits

- Use conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:`, `chore:`
- All changes go via PR from a feature branch (main is protected); merge when CI is green

## Dependencies

- Package manager: uv
- Run `uv sync --all-extras` after modifying dependencies
- `uv.lock` is committed; CI and pre-commit audit it with pip-audit through `uvx`, so pip-audit is not a project dependency
- Sibling projects, where used, are their PyPI releases (`fromscratch-<name>>=X.Y.Z`); never use `path` or git sources

## Releasing

- A release is a `vX.Y.Z` tag on main after the PR is merged
- Docs-only and tooling-only changes do not get a release tag
- Docs are built with `mkdocs build --strict` and deployed to GitHub Pages by the docs workflow
<!-- standard:end -->

## Project notes

- Pyright in strict mode -- run `uv run pyright src tests` before committing
- All violations must be fixed, not ignored (except D203/D213 incompatible pair)
- Minimum 80% coverage enforced by pytest-cov
- Use conventional commits (enforced by commitizen): `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:`, `chore:`
- All changes go via PR from a feature branch (main is protected)
- Add production deps to `[project.dependencies]`
