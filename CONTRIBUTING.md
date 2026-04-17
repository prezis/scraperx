# Contributing to ScraperX

Thanks for considering a contribution. ScraperX is a small, focused library — bug reports, feature requests, and PRs are all welcome.

## Quick start

```bash
git clone https://github.com/prezis/scraperx.git
cd scraperx
pip install -e ".[vision,video-discovery,whisper]"
pytest -v
```

All 212 tests should pass in under 5 seconds with zero network calls (everything is mocked).

## Design principles

- **No API keys.** Core functionality must work without any credentials. If a feature requires auth, gate it behind an optional extra.
- **Stdlib-first.** Any new dependency must justify its weight and go through an `[extras]` entry in `pyproject.toml`. The core works with Python 3.10+ stdlib only.
- **Graceful degradation.** If an optional dep (e.g., `imagehash`, `beautifulsoup4`) is missing, the library should still run — just with reduced capability. Use `try/except ImportError` and module-level flags.
- **Fallback chains.** Where possible, try multiple methods in priority order. Don't fail on the first 500.
- **Zero-network tests.** All tests mock HTTP / subprocess / filesystem. CI must be deterministic.

## How to contribute

### Report a bug
Open an issue with:
- Python version + OS
- A minimal reproducer
- What you expected vs what happened

### Propose a feature
Open an issue first so we can discuss scope. Good candidates: more video providers, better scam heuristics, additional backends. Bad candidates: anything that requires API keys in the core path.

### Submit a PR

1. Fork + branch from `main`
2. Add tests — we run `pytest -v` on every change
3. Keep PRs focused — one feature or one fix per PR
4. Update `CHANGELOG.md` under `[Unreleased]`
5. Update `README.md` if you add a public API

### Code style

- Type hints preferred but not required
- `from __future__ import annotations` in modules that use forward refs
- Keep functions small; prefer composition over inheritance
- Docstrings for public API; inline comments for tricky logic

## Testing

```bash
# All tests
pytest -v

# Specific module
pytest tests/test_scraper.py -v

# With coverage
pip install pytest-cov
pytest --cov=scraperx --cov-report=term-missing
```

Test fixtures live in `tests/` alongside the tests. Mocks use `unittest.mock`. Network calls via `urllib.request.urlopen` are patched per-test.

## Releasing (maintainers)

1. Update `scraperx/__init__.py::__version__` and `pyproject.toml::version` — **keep them in sync**
2. Update `CHANGELOG.md` — move `[Unreleased]` to the new version
3. `git tag v1.X.Y && git push --tags`
4. GitHub Actions publishes to PyPI (if configured)

## Code of conduct

Be kind. Assume good faith. Technical critique is welcome; personal attacks are not.

## Questions?

Open an issue with `question` label, or reach out via GitHub Discussions (once enabled).
