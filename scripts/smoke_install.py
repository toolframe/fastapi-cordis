"""Exercise an installed distribution without source-tree or dev dependencies."""

import asyncio
import sys
from importlib.metadata import distribution, version
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

import fastapi_cordis
from fastapi_cordis import Plugin, PluginManager, install


class Settings(BaseModel):
    message: str = "installed wheel works"


async def main() -> None:
    expected_version = sys.argv[1]
    assert fastapi_cordis.__version__ == version("fastapi-cordis") == expected_version
    module_path = Path(fastapi_cordis.__file__).resolve()
    assert module_path.is_relative_to(Path(sys.prefix).resolve()), module_path
    assert module_path.with_name("py.typed").is_file()
    assert distribution("fastapi-cordis").metadata["License-Expression"] == "MIT"

    cleaned_up = []

    def setup(context):
        context.on_dispose(lambda: cleaned_up.append(True))
        return context.config.message

    manager = PluginManager([Plugin("greeting", setup, config_model=Settings)])
    app = FastAPI()
    install(app, manager)
    async with app.router.lifespan_context(app):
        with manager.lease("greeting") as greeting:
            assert greeting == "installed wheel works"
        assert manager.snapshot()[0]["state"] == "active"
    assert cleaned_up == [True]
    print(f"Installed {expected_version}: {module_path}")
    print(
        f"Runtime dependencies: FastAPI {version('fastapi')}, Pydantic {version('pydantic')}"
    )


if __name__ == "__main__":
    asyncio.run(main())
