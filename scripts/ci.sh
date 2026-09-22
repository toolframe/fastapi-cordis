#!/usr/bin/env bash
set -euo pipefail

if (( $# > 2 )); then
  printf 'Usage: %s [python-version] [new-artifact-directory]\n' "$0" >&2
  exit 2
fi

python_version=${1:-3.11}
artifact_directory=${2:-}
if [[ -n "$artifact_directory" ]]; then
  if [[ "$artifact_directory" != /* ]]; then
    artifact_directory="$PWD/$artifact_directory"
  fi
  if [[ -e "$artifact_directory" || -L "$artifact_directory" ]]; then
    printf 'Artifact directory must not already exist: %s\n' "$artifact_directory" >&2
    exit 2
  fi
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work_directory=$(mktemp -d "${TMPDIR:-/tmp}/fastapi-cordis-ci.XXXXXX")
trap 'rm -rf "$work_directory"' EXIT
printf 'Fresh validation workspace: %s\n' "$work_directory"

export UV_PROJECT_ENVIRONMENT="$work_directory/dev"
export UV_CACHE_DIR="$work_directory/uv-cache"
export RUFF_CACHE_DIR="$work_directory/ruff-cache"
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONHOME
cd "$repo_root"

uv sync --locked --python "$python_version" --group dev
dev_python="$UV_PROJECT_ENVIRONMENT/bin/python"
"$dev_python" --version
"$UV_PROJECT_ENVIRONMENT/bin/ruff" check src tests examples scripts
"$UV_PROJECT_ENVIRONMENT/bin/ruff" format --check src tests examples scripts
"$dev_python" -m pytest -q -o "cache_dir=$work_directory/pytest-cache"

# uv's default build makes the wheel from the freshly built source distribution.
uv build --no-sources --python "$dev_python" --out-dir "$work_directory/dist"
uv tool run --isolated --python "$dev_python" --from twine==7.0.0 \
  twine check --strict "$work_directory/dist/"*
"$dev_python" scripts/check_artifacts.py "$work_directory/dist"
version=$("$dev_python" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

# Each artifact is installed with its real runtime dependencies, outside the
# source tree and without the editable development installation or test tools.
for artifact in "$work_directory/dist/"*.whl "$work_directory/dist/"*.tar.gz; do
  if [[ "$artifact" == *.whl ]]; then
    installation="$work_directory/wheel"
  else
    installation="$work_directory/sdist"
  fi
  uv venv --python "$dev_python" "$installation"
  uv pip install --python "$installation/bin/python" "$artifact"
  uv pip check --python "$installation/bin/python"
  (
    cd "$work_directory"
    "$installation/bin/python" -I "$repo_root/scripts/smoke_install.py" "$version"
  )
done

if [[ -n "$artifact_directory" ]]; then
  mkdir "$artifact_directory"
  cp "$work_directory/dist/"*.whl "$work_directory/dist/"*.tar.gz "$artifact_directory/"
  printf 'Validated distributions: %s\n' "$artifact_directory"
fi
printf 'Validation passed for Python %s\n' "$python_version"
