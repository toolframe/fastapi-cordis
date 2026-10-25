"""Reject publishing unless the workflow was dispatched on the exact version tag."""

import argparse
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="Full Git ref, such as refs/tags/v0.2.2")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    expected = f"refs/tags/v{project['version']}"
    if args.ref != expected:
        parser.error(f"publishing requires {expected!r}; received {args.ref!r}")
    print(f"Release tag matches project.version for PyPI: {args.ref}")


if __name__ == "__main__":
    main()
