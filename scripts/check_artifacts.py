"""Check the built package contract, including the original MIT license."""

import argparse
import email
import tarfile
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    stem = f"fastapi_cordis-{project['version']}"
    wheel_path = args.directory / f"{stem}-py3-none-any.whl"
    sdist_path = args.directory / f"{stem}.tar.gz"
    artifacts = set(args.directory.glob("*.whl")) | set(args.directory.glob("*.tar.gz"))
    assert artifacts == {wheel_path, sdist_path}, artifacts
    license_text = (root / "LICENSE").read_bytes()

    with zipfile.ZipFile(wheel_path) as wheel:
        metadata = email.message_from_bytes(wheel.read(f"{stem}.dist-info/METADATA"))
        assert metadata["Name"] == project["name"]
        assert metadata["Version"] == project["version"]
        assert metadata["Requires-Python"] == project["requires-python"]
        assert metadata["License-Expression"] == project["license"] == "MIT"
        assert metadata.get_all("License-File") == ["LICENSE"]
        assert wheel.read(f"{stem}.dist-info/licenses/LICENSE") == license_text
        assert (
            metadata.get_payload().rstrip() == (root / "README.md").read_text().rstrip()
        )
        assert "fastapi_cordis/py.typed" in wheel.namelist()
        assert "fastapi_cordis/__init__.py" in wheel.namelist()

    required = {"pyproject.toml", "README.md", "CONTRIBUTING.md", "LICENSE", "uv.lock"}
    for directory in ("src", "tests", "examples", "scripts", "docs"):
        required.update(
            str(path.relative_to(root))
            for path in (root / directory).rglob("*")
            if path.is_file()
            and (path.suffix in {".py", ".sh"} or path.name == "py.typed")
        )
    required.update(
        str(path.relative_to(root)) for path in (root / "docs").glob("*.md")
    )
    with tarfile.open(sdist_path) as sdist:
        names = set(sdist.getnames())
        missing = {f"{stem}/{name}" for name in required} - names
        assert not missing, f"Source distribution is missing: {sorted(missing)}"
        license_file = sdist.extractfile(f"{stem}/LICENSE")
        assert license_file is not None
        assert license_file.read() == license_text

    print(
        f"Distribution contents, metadata, README, typing marker and MIT license: {stem}"
    )


if __name__ == "__main__":
    main()
