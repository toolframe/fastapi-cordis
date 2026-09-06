import asyncio
from typing import Annotated, Protocol, runtime_checkable

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_cordis import (
    DependencyError,
    Plugin,
    PluginManager,
    PluginUnavailable,
    ServiceKey,
    install,
    service,
)


@runtime_checkable
class Formatter(Protocol):
    def format(self, value: str) -> str: ...


FORMATTER = ServiceKey("formatter", Formatter)


class Uppercase:
    def format(self, value: str) -> str:
        return value.upper()


class Lowercase:
    def format(self, value: str) -> str:
        return value.lower()


@pytest.mark.parametrize(
    "implementation, expected", [(Uppercase, "HELLO"), (Lowercase, "hello")]
)
def test_consumer_is_unchanged_when_provider_is_replaced(implementation, expected):
    def consumer(ctx):
        formatter = ctx.require(FORMATTER)
        return lambda: formatter.format("Hello")

    manager = PluginManager(
        [
            Plugin("consumer", consumer, requires=(FORMATTER,)),
            Plugin(
                implementation.__name__, lambda _: implementation(), provides=FORMATTER
            ),
        ]
    )

    async def run():
        async with manager.lifespan():
            with manager.lease("consumer") as value:
                assert value() == expected
            with manager.lease(FORMATTER) as formatter:
                assert formatter.format("Hello") == expected
            with manager.lease("formatter") as formatter:
                assert formatter.format("Hello") == expected
            assert manager.snapshot()[-1]["requires"] == [implementation.__name__]

    asyncio.run(run())


def test_service_dependencies_drain_before_provider_cleanup():
    cleaned = []

    def provider(ctx):
        ctx.on_dispose(cleaned.append, "closed")
        return Uppercase()

    manager = PluginManager(
        [
            Plugin("uppercase", provider, provides=FORMATTER),
            Plugin(
                "consumer", lambda ctx: ctx.require(FORMATTER), requires=(FORMATTER,)
            ),
        ]
    )

    async def run():
        async with manager.lifespan():
            with pytest.raises(DependencyError):
                await manager.disable("uppercase")
            with manager.lease("consumer") as formatter:
                stopping = asyncio.create_task(
                    manager.disable("uppercase", cascade=True)
                )
                for _ in range(20):
                    if manager.snapshot()[0]["state"] == "draining":
                        break
                    await asyncio.sleep(0)
                assert not stopping.done()
                assert not cleaned
                assert formatter.format("work") == "WORK"
                with pytest.raises(PluginUnavailable), manager.lease(FORMATTER):
                    pass
            await stopping
            assert cleaned == ["closed"]

    asyncio.run(run())


def test_reconfiguration_restarts_dependents_of_the_stable_service():
    def provider(ctx):
        return Uppercase() if ctx.config.get("uppercase", True) else Lowercase()

    manager = PluginManager(
        [
            Plugin("selected", provider, provides=FORMATTER),
            Plugin(
                "consumer", lambda ctx: ctx.require(FORMATTER), requires=(FORMATTER,)
            ),
        ]
    )

    async def run():
        async with manager.lifespan():
            with manager.lease("consumer") as formatter:
                assert formatter.format("Hello") == "HELLO"
            await manager.configure("selected", {"uppercase": False})
            with manager.lease("consumer") as formatter:
                assert formatter.format("Hello") == "hello"

    asyncio.run(run())


def test_invalid_service_value_cleans_resources_before_exposing_failure():
    cleaned = []

    def broken(ctx):
        ctx.on_dispose(cleaned.append, "closed")
        return object()

    manager = PluginManager([Plugin("broken", broken, provides=FORMATTER)])

    async def run():
        with pytest.raises(TypeError):
            async with manager.lifespan():
                pytest.fail("invalid provider started")
        assert cleaned == ["closed"]
        assert manager.snapshot()[0]["state"] == "failed"

    asyncio.run(run())


def test_aliases_are_unambiguous_and_contracts_are_checked_before_setup():
    with pytest.raises(DependencyError):
        PluginManager(
            [
                Plugin("first", lambda _: Uppercase(), provides=FORMATTER),
                Plugin("second", lambda _: Uppercase(), provides=FORMATTER),
            ]
        )
    with pytest.raises(DependencyError):
        PluginManager(
            [
                Plugin("formatter", lambda _: None),
                Plugin("second", lambda _: Uppercase(), provides=FORMATTER),
            ]
        )
    with pytest.raises(DependencyError):
        PluginManager(
            [
                Plugin("first", lambda _: Uppercase(), provides=FORMATTER),
                Plugin(
                    "consumer", lambda _: None, requires=(ServiceKey("formatter", str),)
                ),
            ]
        )
    with pytest.raises(DependencyError):
        PluginManager([Plugin("consumer", lambda _: None, requires=(FORMATTER,))])
    with pytest.raises(DependencyError):
        PluginManager(
            [
                Plugin(
                    "first",
                    lambda _: Uppercase(),
                    provides=FORMATTER,
                    requires=("consumer",),
                ),
                Plugin("consumer", lambda _: None, requires=(FORMATTER,)),
            ]
        )


def test_typed_fastapi_dependency_uses_the_selected_provider():
    manager = PluginManager(
        [Plugin("uppercase", lambda _: Uppercase(), provides=FORMATTER)]
    )
    app = FastAPI()
    install(app, manager)

    @app.get("/format")
    def format_value(formatter: Annotated[Formatter, Depends(service(FORMATTER))]):
        return formatter.format("hello")

    with TestClient(app) as client:
        assert client.get("/format").json() == "HELLO"
