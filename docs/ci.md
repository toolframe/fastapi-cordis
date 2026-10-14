# CI and package publishing

This repository is standalone. Its tests, build, and installation checks need no
sibling checkout, application configuration, or private package registry.

## Run the CI checks locally

Install Bash, [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.12.8,
and the Python versions to check. Network access to Python downloads and PyPI is
needed. From this repository:

```sh
uv python install 3.11 3.12 3.13 3.14
./scripts/ci.sh 3.11
./scripts/ci.sh 3.12
./scripts/ci.sh 3.13
./scripts/ci.sh 3.14
```

Each invocation creates a temporary uv cache and three fresh virtual environments;
it does not reuse `.venv`, `dist`, or a populated wheelhouse. It runs:

1. `uv sync --locked --python <version> --group dev`, preserving `uv.lock`.
2. Ruff lint and format checks on `src`, `tests`, `examples`, and `scripts`, then
   the complete pytest suite with the existing warnings policy.
3. `uv build --no-sources`, which builds a source distribution and then a wheel
   from that source distribution. Twine 7.0.0 checks both with `check --strict`.
4. Distribution checks for version, Python requirement, README, `py.typed`, the
   original MIT license, and source distribution contents (including guides,
   examples, tests, and validation scripts).
5. Separate clean installations of the wheel and source distribution with real
   runtime dependencies, followed by `uv pip check` and an isolated Python smoke
   test outside the checkout. The smoke test verifies the installed import path,
   version, Pydantic configuration, FastAPI lifespan, service lease, and cleanup.

The development tests use locked dependencies. Clean installations resolve the
published dependency ranges from PyPI, so they also exercise installation without
the development lockfile. Twine runs in its own isolated tool environment.

Temporary environments and artifacts are removed on exit. To keep the validated
wheel and source distribution, pass a destination that does not already exist
(its parent directory must exist):

```sh
./scripts/ci.sh 3.11 /tmp/fastapi-cordis-dist-311
```

Workflow and shell checks are separate from Python package checks:

```sh
actionlint  # validated with actionlint 1.7.12
bash -n scripts/ci.sh
```

## GitHub Actions

`ci.yml` runs on pull requests, pushes, manual dispatch, and reusable workflow
calls. Its Python 3.11, 3.12, 3.13, and 3.14 matrix runs the same local script on
the documented [`ubuntu-24.04` runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Every cell builds and installs both artifacts. The Python 3.11 cell saves the
validated distributions as `distributions-<commit SHA>` for seven days.

All external actions are pinned to full commit IDs, with release comments. Jobs
have timeouts, checkout credentials are not persisted, and default permissions
are `contents: read`. Pull request runs cannot publish. The validation concurrency
group differs from the publishing group; a reusable validation call cannot cancel
its caller.

## Prepare the external publishing settings

The workflow files prepare publishing; adding them does not upload a package.
Before a maintainer publishes:

1. Create or choose the actual GitHub repository and push this source there.
   Enable GitHub Actions and permit the pinned actions. No owner name is hardcoded
   and no sibling repository variables or credentials are needed.
2. Create GitHub environments named exactly `testpypi` and `pypi`. Restrict them to
   release tags and configure maintainer approval as appropriate, especially for
   `pypi`.
3. Separately configure a GitHub Trusted Publisher on TestPyPI and on PyPI for
   project name `fastapi-cordis`. Use the actual GitHub owner and repository name,
   workflow filename `publish.yml`, and the corresponding environment name
   (`testpypi` or `pypi`). If the project does not yet exist, use a pending
   publisher and confirm that the package name is available. See the official
   [new project publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   and [publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
   No API token secret is required.
4. Ensure `.github/workflows/publish.yml` is on the default branch so GitHub can
   dispatch it, and ensure the release tag includes these workflows and scripts.

## Run a release

Before tagging, align `project.version`, `fastapi_cordis.__version__`, release
notes, and the installation documentation; review metadata and dependency ranges.
The current version is `0.2.2`, so the required tag is exactly `v0.2.2`. Branches,
unprefixed tags, and tags for any other version are rejected before validation.
`python scripts/check_release.py refs/tags/v0.2.2` checks this guard locally.

A maintainer creates and pushes the tag, then manually dispatches **Publish** on
that tag. The `target` choice defaults to `testpypi`. For example, these commands
run remote publishing after the external setup has been completed:

```sh
gh workflow run publish.yml --ref v0.2.2 -f target=testpypi
# After reviewing the TestPyPI result, a separate explicit production dispatch:
gh workflow run publish.yml --ref v0.2.2 -f target=pypi
```

The publish run checks out its exact event SHA, checks the version tag, and calls
the complete CI matrix at the same revision. Only after all cells pass does it
download this run's validated Python 3.11 artifacts and upload them with the
[PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish). It does
not rebuild in the publishing job or download artifacts from a different run.
Only that final job receives `id-token: write`, with the selected GitHub
environment, for Trusted Publishing and the action's attestations. Existing
versions are not silently skipped. A tag push by itself runs CI; it does not
publish.

TestPyPI and PyPI are separate indexes. A successful TestPyPI upload does not
configure or release to PyPI. TestPyPI may lack runtime dependencies, so any
post-upload installation check should download this project's artifact from
TestPyPI and install that file with dependencies from PyPI.
