# Contributing

This repository is an independent Python package. Development and tests do not
require a consuming application or a sibling repository.

## Set up and verify

From the repository root:

```sh
uv sync --python 3.14
uv run pytest
uv run ruff check src tests examples scripts
uv run ruff format --check src tests examples scripts
```

Python 3.11 is the minimum supported version. Check the built wheel on that
version as well as the development interpreter. The shared CI script runs lint,
format, tests, builds, strict metadata checks, and clean wheel/sdist installations:

```sh
./scripts/ci.sh 3.11
./scripts/ci.sh 3.14
```

Each invocation starts with fresh temporary environments. CI also checks Python
3.12 and 3.13. See [CI and package publishing](docs/ci.md) for the full matrix,
artifact retention, workflow validation, and external release settings. Local
verification does not upload anything.

## Commit hooks

Install [prek](https://prek.j178.dev/) and enable the repository's hooks:

```sh
uv tool install prek==0.5.5
prek install --allow-missing-config
prek run --all-files
```

The missing-config option allows older worktrees without this configuration to
keep working. Worktrees containing `.pre-commit-config.yaml` still run the checks.
The `pre-commit` hook checks the branch name, Python lint, and formatting. The
`commit-msg` hook checks Conventional Commits. Both Ruff hooks only check files;
they do not rewrite a commit's contents. Fix lint and formatting explicitly:

```sh
uvx ruff==0.16.6 check --fix src tests examples scripts
uvx ruff==0.16.6 format src tests examples scripts
prek run --all-files
```

The Ruff version and checked paths are pinned in
[the hook configuration](.pre-commit-config.yaml); the Python rules remain in
[pyproject.toml](pyproject.toml). CI uses these same hooks on all tracked files
within their configured paths. Existing package, integration, and release checks
run independently.

## Branches and commits

[cchk.toml](cchk.toml) defines the allowed branch and commit types. Work branches
use `feat/`, `fix/`, `release/`, or `chore/`, followed by lowercase ASCII letters
and digits separated by single hyphens. Release versions may also contain dots,
as in `release/v1.2.0-rc.1`; every separator must have an alphanumeric character on
both sides. The default branch `main` is the standalone exception.

Commit types are independent of branch types: for example,
`docs: clarify setup` is valid on `chore/update-docs`. Commit-check uses Conventional
Commits with its default subject length of 5–80 characters. Pull requests check
their commits and title so the title can be used for a Conventional Commit when
squash merging.

Local hooks can be bypassed. CI reports `branch-policy`, `code-quality`, and
`commit-policy`; maintainers must configure required status checks separately to
make passing checks a merge requirement.

## Keep the package boundary clear

Lifecycle orchestration, configuration, and leases belong in
`src/fastapi_cordis/_runtime.py`. Pure dependency planning belongs in `_graph.py`,
resource ownership in `_scope.py`, typed contracts in `_services.py`, and event
dispatch in `_events.py`. HTTP integration belongs in `fastapi.py`.
Application-specific discovery, persistence, installation, and migrations belong
to consuming applications.

Keep plugin imports free of resource acquisition. Cover lifecycle changes through
observable behavior: dependency ordering, active callers during shutdown,
configuration failure recovery, and cleanup after failed setup. Adapter changes
should cover request behavior and composition with an existing lifespan.

Keep each commit focused on one behavior or documentation topic, with its relevant
tests in the same commit.

Update the README and relevant guide when public behavior changes. Examples should
run from this checkout with the documented dependencies and no private services.

## Check distribution contents

After building, inspect the artifacts in `dist/`:

- The wheel contains the import package, `py.typed`, and the MIT license.
- Package metadata uses the declared version, Python requirement, and dependency
  ranges. Its long description matches the README.
- The source distribution includes the README, guides, examples, and tests needed
  to develop and understand the package independently.

The README is also the package index description. Keep it self-contained and avoid
machine-specific paths, required sibling repositories, placeholder project URLs,
or instructions implying an unpublished package is already available on PyPI.

## Prepare a release

The manual `publish.yml` workflow defaults to TestPyPI, requires a
`v<project.version>` tag, and publishes only after its complete CI matrix passes.
Publishing is a separate maintainer action. A TestPyPI release does not publish
the package to the production PyPI index.

Before a release, confirm the distribution name and version on the target index
and review the repository and documentation URLs in project metadata.
Review the supported dependency ranges, align `pyproject.toml` with `__version__`,
record release changes, and update installation guidance when a release is
available. Test and inspect the exact artifacts intended for distribution.

Configure the GitHub environments and Trusted Publishers described in
[the publishing guide](docs/ci.md) outside the source tree.
Ordinary development, documentation updates, and local builds do not require an
upload, release tag, or publishing credentials.
