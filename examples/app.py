"""Run: uv run --with uvicorn uvicorn examples.app:app"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from fastapi_cordis import Plugin, PluginManager, install, service


class GreetingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Hello"


async def greeting(ctx):
    return lambda name: f"{ctx.config.prefix}, {name}"


manager = PluginManager([Plugin("greeting", greeting, config_model=GreetingConfig)])
app = FastAPI(title="fastapi-cordis example")
install(app, manager, management=True)


@app.get("/hello/{name}")
async def hello(
    name: str, greet: Annotated[Callable[[str], str], Depends(service("greeting"))]
):
    return {"message": greet(name)}
