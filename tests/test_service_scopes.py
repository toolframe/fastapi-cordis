import asyncio
from typing import Annotated
from unittest import IsolatedAsyncioTestCase

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_cordis import (
    DependencyError,
    Plugin,
    PluginError,
    PluginManager,
    PluginUnavailable,
    ServiceKey,
    install,
    service,
)

NUMBER = ServiceKey("number", int)
TEXT = ServiceKey("text", str)


def consumer(scope, *, managed=True):
    return Plugin(
        "consumer",
        lambda ctx: ctx.require(NUMBER),
        requires=(NUMBER,),
        scope=scope,
        managed=managed,
    )


class ServiceScopeTests(IsolatedAsyncioTestCase):
    async def test_missing_explicit_paths_report_the_actual_provider_location(self):
        manager = PluginManager(
            [
                Plugin(
                    "consumer",
                    lambda ctx: (ctx.require("/root"), ctx.require("shared/value")),
                    requires=("/root", "shared/value"),
                    scope="work.a",
                )
            ]
        )
        async with manager.lifespan():
            assert manager.snapshot()[0]["blockedBy"] == ["root", "shared/value"]
            await manager.register(Plugin("root", lambda ctx: 1))
            await manager.register(Plugin("value", lambda ctx: 2, scope="shared"))
            with manager.lease("work.a/consumer") as value:
                assert value == (1, 2)

    async def test_plugin_can_publish_multiple_independent_capabilities(self):
        contexts = []

        def provider(ctx):
            contexts.append(ctx)
            ctx.provide(NUMBER, 7)
            ctx.provide(TEXT, "seven")
            return "plugin-value"

        def both(ctx):
            return (ctx.require(NUMBER), ctx.require(TEXT))

        manager = PluginManager(
            [
                Plugin("provider", provider, provides=(NUMBER, TEXT)),
                Plugin("both", both, requires=(NUMBER, TEXT)),
            ]
        )
        async with manager.lifespan():
            with manager.lease("both") as value:
                assert value == (7, "seven")
                assert manager.snapshot()[0]["inFlight"] == 1
            with manager.lease(NUMBER) as number, manager.lease("text") as text:
                assert (number, text) == (7, "seven")
            with manager.lease("provider") as value:
                assert value == "plugin-value"
            assert manager.snapshot()[0]["provides"] == ["number", "text"]
            with pytest.raises(PluginError, match="during setup"):
                contexts[0].provide(NUMBER, 9)

    async def test_missing_published_service_fails_setup_and_cleans_partial_resources(
        self,
    ):
        cleaned = []

        def provider(ctx):
            ctx.on_dispose(cleaned.append, True)
            ctx.provide(NUMBER, 1)

        manager = PluginManager([Plugin("provider", provider, provides=(NUMBER, TEXT))])
        with pytest.raises(DependencyError, match="all declared"):
            await manager.start()
        assert cleaned == [True]
        with pytest.raises(PluginUnavailable), manager.lease(NUMBER):
            pass

    async def test_services_must_be_declared_on_both_provider_and_consumer(self):
        def wrong_provider(ctx):
            ctx.provide(TEXT, "undeclared")

        with pytest.raises(DependencyError):
            await PluginManager(
                [Plugin("wrong", wrong_provider, provides=NUMBER)]
            ).start()

        def provider(ctx):
            ctx.provide(NUMBER, 1)
            ctx.provide(TEXT, "one")

        manager = PluginManager(
            [
                Plugin("provider", provider, provides=(NUMBER, TEXT)),
                Plugin("wrong", lambda ctx: ctx.require(TEXT), requires=(NUMBER,)),
            ]
        )
        with pytest.raises(DependencyError, match="did not declare"):
            await manager.start()

    async def test_scopes_resolve_local_then_ancestors_without_crossing_siblings(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: 1, provides=NUMBER),
                Plugin("provider", lambda _: 2, provides=NUMBER, scope="work.a"),
                consumer("work.a"),
                consumer("work.a.child"),
                consumer("work.b"),
            ]
        )
        async with manager.lifespan():
            for scope, expected in [("work.a", 2), ("work.a.child", 2), ("work.b", 1)]:
                with manager.lease("consumer", scope=scope) as value:
                    assert value == expected
                with manager.lease(NUMBER, scope=scope) as value:
                    assert value == expected
            with manager.lease("/number", scope="work.a") as value:
                assert value == 1
            with manager.lease("work.a/consumer") as value:
                assert value == 2
            await manager.disable("work.a/provider")
            snapshot = {item["name"]: item for item in manager.snapshot()}
            assert snapshot["work.a/consumer"]["state"] == "blocked"
            assert snapshot["work.a.child/consumer"]["state"] == "blocked"
            assert snapshot["work.b/consumer"]["state"] == "active"
            await manager.enable("work.a/provider")
            assert all(item["state"] == "active" for item in manager.snapshot())

    async def test_register_override_waits_for_consumers_and_removal_restores_parent(
        self,
    ):
        instances = []
        closed = []

        def consume(ctx):
            value = ctx.require(NUMBER)
            instances.append(value)
            ctx.on_dispose(closed.append, value)
            return value

        manager = PluginManager(
            [
                Plugin("provider", lambda _: 1, provides=NUMBER),
                Plugin("consumer", consume, requires=(NUMBER,), scope="work"),
            ]
        )
        async with manager.lifespan():
            with manager.lease("consumer", scope="work") as original:
                overriding = asyncio.create_task(
                    manager.register(
                        Plugin("override", lambda _: 2, provides=NUMBER, scope="work")
                    )
                )
                async with asyncio.timeout(1):
                    while manager.snapshot()[-1]["state"] != "draining":
                        await asyncio.sleep(0)
                assert not overriding.done()
                assert original == 1
                assert closed == []
            await overriding
            with manager.lease("consumer", scope="work") as value:
                assert value == 2
            assert closed == [1]
            await manager.unregister("work/override")
            with manager.lease("consumer", scope="work") as value:
                assert value == 1
            assert instances == [1, 2, 1]

    async def test_protected_consumer_cannot_be_rebound_by_a_new_override(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: 1, provides=NUMBER),
                consumer("work", managed=False),
            ]
        )
        async with manager.lifespan():
            before = manager.snapshot()
            with pytest.raises(PluginError, match="host"):
                await manager.register(
                    Plugin("override", lambda _: 2, provides=NUMBER, scope="work")
                )
            assert manager.snapshot() == before

    async def test_conflicting_contracts_and_scoped_cycles_fail_before_setup(self):
        with pytest.raises(DependencyError):
            PluginManager(
                [
                    Plugin("one", lambda _: 1, provides=NUMBER, scope="work"),
                    Plugin("two", lambda _: 2, provides=NUMBER, scope="work"),
                ]
            )
        with pytest.raises(DependencyError):
            PluginManager([Plugin("one", lambda _: None, provides=(NUMBER, NUMBER))])
        with pytest.raises(DependencyError):
            PluginManager(
                [
                    Plugin("a", lambda _: None, requires=("b",), scope="work"),
                    Plugin("b", lambda _: None, requires=("a",), scope="work"),
                ]
            )
        with pytest.raises(DependencyError):
            PluginManager(
                [
                    Plugin("root", lambda _: 1, provides=NUMBER),
                    Plugin(
                        "local",
                        lambda _: "wrong",
                        provides=ServiceKey("number", str),
                        scope="work",
                    ),
                    consumer("work"),
                ]
            )


@pytest.mark.parametrize("scope", [".bad", "bad.", "bad..name", "bad/name", 1, None])
def test_invalid_scopes_are_rejected(scope):
    with pytest.raises(ValueError, match="Scope"):
        PluginManager([Plugin("plugin", lambda _: None, scope=scope)])


def test_fastapi_uses_scoped_services_and_qualified_management_paths():
    manager = PluginManager(
        [
            Plugin("provider", lambda _: 1, provides=NUMBER),
            Plugin(
                "provider",
                lambda ctx: ctx.config["value"],
                config={"value": 2},
                provides=NUMBER,
                scope="work.a",
            ),
        ]
    )
    app = FastAPI()
    install(app, manager, management=True)

    @app.get("/number")
    def number(value: Annotated[int, Depends(service(NUMBER, scope="work.a"))]):
        return value

    with TestClient(app) as client:
        assert client.get("/number").json() == 2
        assert (
            client.put("/plugins/work.a/provider/config", json={"value": 3}).status_code
            == 200
        )
        assert client.get("/number").json() == 3
        assert client.post("/plugins/work.a/provider/disable").status_code == 200
        assert client.get("/number").status_code == 503
        assert client.post("/plugins/work.a/provider/enable").status_code == 200
        assert client.get("/number").json() == 3
