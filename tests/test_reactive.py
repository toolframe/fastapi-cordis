import asyncio
from unittest import IsolatedAsyncioTestCase

import pytest

from fastapi_cordis import (
    DependencyError,
    DrainTimeout,
    Plugin,
    PluginError,
    PluginManager,
    PluginUnavailable,
    ServiceKey,
    UnknownPlugin,
)


def states(manager):
    return {item["name"]: item for item in manager.snapshot()}


class ReactiveTests(IsolatedAsyncioTestCase):
    async def test_provider_loss_blocks_and_recovery_rebinds_transitive_consumers(self):
        events = []
        generation = 0

        def provider(ctx):
            nonlocal generation
            generation += 1
            ctx.on_dispose(events.append, "provider.closed")
            return generation

        def consumer(ctx):
            ctx.on_dispose(events.append, "consumer.closed")
            return ctx.require("provider")

        manager = PluginManager(
            [
                Plugin("provider", provider),
                Plugin("consumer", consumer, requires=("provider",)),
                Plugin(
                    "leaf", lambda ctx: ctx.require("consumer"), requires=("consumer",)
                ),
            ]
        )
        async with manager.lifespan():
            await manager.disable("provider")
            assert events == ["consumer.closed", "provider.closed"]
            snapshot = states(manager)
            assert snapshot["provider"]["enabled"] is False
            for name in ("consumer", "leaf"):
                assert snapshot[name]["enabled"] is True
                assert snapshot[name]["state"] == "blocked"
            assert snapshot["consumer"]["blockedBy"] == ["provider"]
            with pytest.raises(PluginUnavailable), manager.lease("leaf"):
                pass
            await manager.enable("provider")
            with manager.lease("leaf") as value:
                assert value == 2

    async def test_disabled_intent_and_explicit_cascade_survive_provider_recovery(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: None),
                Plugin("consumer", lambda _: None, requires=("provider",)),
                Plugin(
                    "optional", lambda _: None, requires=("provider",), enabled=False
                ),
            ]
        )
        async with manager.lifespan():
            await manager.disable("provider")
            await manager.disable("provider", cascade=True)
            await manager.enable("provider")
            assert states(manager)["consumer"]["state"] == "disabled"
            assert states(manager)["optional"]["state"] == "disabled"
            await manager.enable("consumer")
            assert states(manager)["consumer"]["state"] == "active"

    async def test_disabled_provider_can_be_enabled_after_start(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: 7, enabled=False),
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider"),
                    requires=("provider",),
                ),
            ]
        )
        with pytest.raises(DependencyError):
            manager.validate()
        async with manager.lifespan():
            assert states(manager)["consumer"]["state"] == "blocked"
            await manager.enable("provider")
            with manager.lease("consumer") as value:
                assert value == 7

    async def test_missing_provider_can_arrive_and_be_replaced_without_consumer_edits(
        self,
    ):
        key = ServiceKey("storage", int)
        manager = PluginManager(
            [
                Plugin("consumer", lambda ctx: ctx.require(key), requires=(key,)),
                Plugin("other", lambda _: 9),
            ]
        )
        with pytest.raises(DependencyError):
            manager.validate()
        async with manager.lifespan():
            assert states(manager)["consumer"]["blockedBy"] == ["storage"]
            await manager.register(Plugin("first", lambda _: 1, provides=key))
            with manager.lease("consumer") as value:
                assert value == 1
            await manager.unregister("first")
            assert states(manager)["consumer"]["state"] == "blocked"
            with pytest.raises(UnknownPlugin), manager.lease(key):
                pass
            with manager.lease("other") as value:
                assert value == 9
            await manager.register(Plugin("second", lambda _: 2, provides=key))
            with manager.lease("consumer") as value:
                assert value == 2

    async def test_failed_consumer_is_not_retried_by_provider_changes(self):
        attempts = []
        fail = True

        def consumer(ctx):
            attempts.append(True)
            if fail:
                raise ValueError("cannot start")
            return ctx.require("provider")

        manager = PluginManager(
            [
                Plugin("provider", lambda _: 1),
                Plugin("consumer", consumer, requires=("provider",), enabled=False),
            ]
        )
        async with manager.lifespan():
            with pytest.raises(ValueError):
                await manager.enable("consumer")
            await manager.disable("provider")
            await manager.enable("provider")
            assert len(attempts) == 1
            assert states(manager)["consumer"]["state"] == "failed"
            with pytest.raises(PluginError, match="retry"):
                await manager.enable("consumer")
            fail = False
            await manager.retry("consumer")
            assert len(attempts) == 2
            with manager.lease("consumer") as value:
                assert value == 1

    async def test_invalid_new_definitions_do_not_change_live_state(self):
        key = ServiceKey("storage", int)
        manager = PluginManager(
            [
                Plugin("consumer", lambda _: None, requires=(key,)),
            ]
        )
        async with manager.lifespan():
            before = manager.snapshot()
            with pytest.raises(DependencyError):
                await manager.register(Plugin("consumer", lambda _: None))
            with pytest.raises(DependencyError):
                await manager.register(
                    Plugin("wrong", lambda _: "x", provides=ServiceKey("storage", str))
                )
            with pytest.raises(DependencyError):
                await manager.register(
                    Plugin("cyclic", lambda _: 1, provides=key, requires=("consumer",))
                )
            assert manager.snapshot() == before

    async def test_failed_registration_retains_definition_for_explicit_retry(self):
        fail = True

        def provider(ctx):
            if fail:
                raise ValueError("cannot connect")
            return 1

        manager = PluginManager(
            [
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider"),
                    requires=("provider",),
                )
            ]
        )
        async with manager.lifespan():
            with pytest.raises(ValueError):
                await manager.register(Plugin("provider", provider))
            assert states(manager)["provider"]["state"] == "failed"
            assert states(manager)["consumer"]["state"] == "blocked"
            fail = False
            await manager.retry("provider")
            assert states(manager)["consumer"]["state"] == "active"

    async def test_unregister_waits_for_transitive_lease_before_removing_provider(self):
        cleaned = []

        def provider(ctx):
            ctx.on_dispose(cleaned.append, True)
            return 1

        manager = PluginManager(
            [
                Plugin("provider", provider),
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider"),
                    requires=("provider",),
                ),
            ]
        )
        async with manager.lifespan():
            with manager.lease("consumer"):
                removing = asyncio.create_task(manager.unregister("provider"))
                async with asyncio.timeout(1):
                    while states(manager)["provider"]["state"] != "draining":
                        await asyncio.sleep(0)
                assert not cleaned
                assert not removing.done()
                with pytest.raises(PluginUnavailable), manager.lease("consumer"):
                    pass
            await removing
            assert cleaned == [True]
            assert states(manager)["consumer"]["enabled"] is True

    async def test_drain_timeout_preserves_dependency_intent_and_instances(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: object()),
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider"),
                    requires=("provider",),
                ),
            ],
            drain_timeout=0.01,
        )
        async with manager.lifespan():
            with manager.lease("consumer") as original:
                with pytest.raises(DrainTimeout):
                    await manager.disable("provider")
                with manager.lease("consumer") as current:
                    assert current is original
                assert all(item["enabled"] for item in manager.snapshot())

    async def test_host_owned_dependents_reject_provider_removal_or_suspension(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: None),
                Plugin("host", lambda _: None, requires=("provider",), managed=False),
            ]
        )
        async with manager.lifespan():
            for operation in (
                manager.unregister("provider"),
                manager.disable("provider"),
            ):
                with pytest.raises(PluginError):
                    await operation
            assert all(item["state"] == "active" for item in manager.snapshot())

    async def test_explicit_start_can_retry_a_failed_initial_startup(self):
        fail = True

        def setup(ctx):
            if fail:
                raise ValueError("startup failed")
            return 1

        manager = PluginManager([Plugin("provider", setup)])
        with pytest.raises(ValueError):
            await manager.start()
        fail = False
        await manager.start()
        with manager.lease("provider") as value:
            assert value == 1
        await manager.close()
