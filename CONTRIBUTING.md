# Contributing

Start with one workflow's README and synthetic example. Choose `imagery/` or
`elevation/` as your working directory; each has a separate locked environment.
No other Found in Space repository is required to develop these producers.

```sh
UV_CACHE_DIR=.cache/uv uv sync --frozen
UV_CACHE_DIR=.cache/uv uv run ruff check src tests examples
UV_CACHE_DIR=.cache/uv uv run ruff format --check src tests examples
UV_CACHE_DIR=.cache/uv uv run pytest
```

For a focused imagery check, run `UV_CACHE_DIR=.cache/uv uv run pytest
tests/test_system_gdal.py`; it builds small projected rasters through the real
geographic and Mercator paths. Install system GDAL/PROJ first so these checks run.
For elevation, `UV_CACHE_DIR=.cache/uv uv run pytest tests/test_terrain.py` checks
scalar resampling, masks, quantization, source admission and output integrity.
CI runs both workflows and their examples independently.

Useful contributions include source admission checks, reproducible small
fixtures, coordinate and mask edge cases, numerical comparisons, and clearer
recipes. Keep expected scientific values independently justified. A screenshot
cannot replace producer validation; producer validation does not establish
desktop or headset acceptance.

Open an issue or pull request in this repository with the problem, expected
behavior, relevant source/contract and the checks you ran. Discuss new formats,
dependency coupling or large shared abstractions before implementation. Existing
commands, package names and published product formats remain compatibility
commitments. Elevation's initial Apollo products remain scratch; imagery's
publication reporting remains planned work. Its end-to-end `run` command and
the Earth RGB pyramid workflows are available in the imagery CLI.

Do not commit raw downloads, generated tile trees, credentials or local paths.
Small examples use temporary outputs; persistent products and evidence use
reviewed durable storage. Citable releases belong in Catalogs and pin clean,
public producer revisions.
