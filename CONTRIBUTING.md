# Contributing

This repository is an independent Python package. Development and tests do not
require a consuming application or a sibling repository.

## Set up and verify

From the repository root:

```sh
uv sync --python 3.14
uv run pytest
uv run ruff check src tests examples
uv run ruff format --check src tests examples
```

Python 3.11 is the minimum supported version. Check the built wheel on that
version as well as the development interpreter:

```sh
uv build
uv run --no-project --python 3.11 \
  --with ./dist/fastapi_cordis-0.1.0-py3-none-any.whl \
  --with pytest --with httpx python -m pytest tests
```

The filename above matches the current development version. Adjust it when the
version changes. `uv build` creates a wheel and source distribution locally; it
does not upload them.

## Keep the package boundary clear

Lifecycle ordering, configuration, leases, and resource cleanup belong in
`src/fastapi_cordis/_runtime.py`. HTTP integration belongs in
`src/fastapi_cordis/fastapi.py`. Application-specific discovery, persistence,
installation, and migrations belong to consuming applications.

Keep plugin imports free of resource acquisition. Cover lifecycle changes through
observable behavior: dependency ordering, active callers during shutdown,
configuration failure recovery, and cleanup after failed setup. Adapter changes
should cover request behavior and composition with an existing lifespan.

Use Conventional Commits (`type(scope): description`). Keep each commit focused on
one behavior or documentation topic, with its relevant tests in the same commit.

Update the README and relevant guide when public behavior changes. Examples should
run from this checkout with the documented dependencies and no private services.

## Check distribution contents

After building, inspect the artifacts in `dist/`:

- The wheel contains the import package, `py.typed`, and the MIT license.
- Package metadata uses the declared version, Python requirement, and dependency
  ranges. Its long description matches the README.
- The source distribution includes the README, guides, examples, and tests needed
  to develop and understand the package independently.

The README is also the future PyPI description. Keep it self-contained and avoid
machine-specific paths, required sibling repositories, placeholder project URLs,
or instructions implying an unpublished package is already available on PyPI.

## Prepare a future release

The package has not been published, and this repository does not currently include
an automatic publishing workflow. Publishing is a separate maintainer action.

Before the first release, confirm the distribution name on the target index and
add the actual public repository and documentation URLs to project metadata.
Review the supported dependency ranges, align `pyproject.toml` with `__version__`,
record release changes, and update installation guidance when a release is
available. Test and inspect the exact artifacts intended for distribution.

Configure release credentials or trusted publishing outside the source tree.
Ordinary development, documentation updates, and local builds do not require an
upload, release tag, or publishing credentials.
