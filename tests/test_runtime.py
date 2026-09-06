import asyncio
import threading
from unittest import IsolatedAsyncioTestCase

import pytest
from pydantic import BaseModel, ConfigDict

from fastapi_cordis import (
    ConfigurationError,
    DependencyError,
    DrainTimeout,
    Plugin,
    PluginError,
    PluginManager,
    PluginUnavailable,
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = 1


async def wait_for_state(manager, name, state):
    async with asyncio.timeout(2):
        while (
            next(p for p in manager.snapshot() if p["name"] == name)["state"] != state
        ):
            await asyncio.sleep(0)


class RuntimeTests(IsolatedAsyncioTestCase):
    async def test_updated_graph_accepts_calls_only_after_all_dependents_start(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def consumer(ctx):
            value = ctx.require("provider")
            if value == 2:
                entered.set()
                await release.wait()
            return value

        manager = PluginManager(
            [
                Plugin(
                    "provider", lambda ctx: ctx.config["value"], config={"value": 1}
                ),
                Plugin("consumer", consumer, requires=("provider",)),
            ]
        )
        async with manager.lifespan():
            updating = asyncio.create_task(manager.configure("provider", {"value": 2}))
            try:
                async with asyncio.timeout(2):
                    await entered.wait()
                for name in ("provider", "consumer"):
                    with pytest.raises(PluginUnavailable), manager.lease(name):
                        pytest.fail(
                            "A partially started graph must not accept requests"
                        )
            finally:
                release.set()
                await updating
            with manager.lease("consumer") as value:
                assert value == 2

    async def test_dependency_order_and_cleanup_ownership(self):
        events = []

        async def storage(ctx):
            events.append("storage.start")
            ctx.on_dispose(events.append, "storage.stop")
            return {"items": []}

        async def exporter(ctx):
            events.append("exporter.start")
            client = ctx.require("storage")

            async def stop():
                events.append("exporter.stop")

            ctx.on_dispose(stop)
            return client

        manager = PluginManager(
            [
                Plugin("exporter", exporter, requires=("storage",)),
                Plugin("storage", storage),
            ]
        )
        async with manager.lifespan():
            with manager.lease("exporter") as value:
                value["items"].append("report")
                assert [p["inFlight"] for p in manager.snapshot()] == [1, 1]
            with manager.lease("storage") as value:
                assert value["items"] == ["report"]
        await manager.close()
        assert events == [
            "storage.start",
            "exporter.start",
            "exporter.stop",
            "storage.stop",
        ]

    async def test_failed_setup_cleans_partial_and_previous_plugins(self):
        events = []

        def good(ctx):
            ctx.on_dispose(events.append, "good.clean")

        async def bad(ctx):
            ctx.on_dispose(events.append, "partial.clean")
            raise RuntimeError("startup failure")

        manager = PluginManager(
            [Plugin("good", good), Plugin("bad", bad, requires=("good",))]
        )
        with pytest.raises(RuntimeError, match="startup failure"):
            async with manager.lifespan():
                pytest.fail("A failed manager must not become ready")
        assert events == ["partial.clean", "good.clean"]
        assert [p["state"] for p in manager.snapshot()] == ["stopped", "failed"]

    async def test_config_restarts_dependents_and_rejects_invalid_before_stop(self):
        events = []

        def provider(ctx):
            value = ctx.config.value
            ctx.on_dispose(events.append, value)
            return value

        manager = PluginManager(
            [
                Plugin("provider", provider, config_model=Settings),
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider") * 2,
                    requires=("provider",),
                ),
            ]
        )
        async with manager.lifespan():
            with pytest.raises(ConfigurationError):
                await manager.configure("provider", {"value": "wrong"})
            assert events == []
            with manager.lease("consumer") as value:
                assert value == 2
            await manager.configure("provider", {"value": 7})
            assert events == [1]
            with manager.lease("consumer") as value:
                assert value == 14
            await manager.disable("provider", cascade=True)
            assert [p["state"] for p in manager.snapshot()] == ["disabled", "disabled"]
            await manager.enable("consumer")
            assert manager.snapshot()[-1]["state"] == "blocked"
            await manager.enable("provider")
            await manager.enable("consumer")
        assert events == [1, 7, 7]

    async def test_inflight_worker_retains_lease_after_waiting_task_cancelled(self):
        entered = threading.Event()
        release = threading.Event()
        cleaned = []

        def setup(ctx):
            ctx.on_dispose(cleaned.append, True)
            return object()

        manager = PluginManager([Plugin("worker", setup)])
        await manager.start()

        def work():
            with manager.lease("worker"):
                entered.set()
                assert release.wait(2)
                assert cleaned == []

        worker = asyncio.create_task(asyncio.to_thread(work))
        await asyncio.to_thread(entered.wait)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        stopping = asyncio.create_task(manager.disable("worker"))
        try:
            await wait_for_state(manager, "worker", "draining")
            assert not stopping.done()
            with pytest.raises(PluginUnavailable), manager.lease("worker"):
                pytest.fail("New work must not enter while draining")
            assert cleaned == []
        finally:
            release.set()
            await stopping
            await manager.close()
        assert cleaned == [True]

    async def test_drain_timeout_restores_service_without_disposing(self):
        events = []

        def setup(ctx):
            ctx.on_dispose(events.append, "closed")

        manager = PluginManager([Plugin("service", setup)], drain_timeout=0.01)
        await manager.start()
        with manager.lease("service"):
            with pytest.raises(DrainTimeout):
                await manager.disable("service")
            with pytest.raises(DrainTimeout):
                await manager.close()
            assert manager.snapshot()[0]["state"] == "active"
            assert events == []
        await manager.close()
        assert events == ["closed"]

    async def test_cancelled_mutation_completes_cleanup_before_propagating(self):
        cleaned = []

        def setup(ctx):
            ctx.on_dispose(cleaned.append, True)

        manager = PluginManager([Plugin("service", setup)])
        async with manager.lifespan():
            with manager.lease("service"):
                stopping = asyncio.create_task(manager.disable("service"))
                await wait_for_state(manager, "service", "draining")
                stopping.cancel()
                await asyncio.sleep(0)
                assert cleaned == []
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert cleaned == [True]
            assert manager.snapshot()[0]["state"] == "disabled"

    async def test_update_setup_failure_closes_restarted_graph_and_keeps_other_services(
        self,
    ):
        events = []

        def setup(ctx):
            ctx.on_dispose(events.append, ctx.config["fail"])
            if ctx.config["fail"]:
                raise RuntimeError("new connection failed")
            return 5

        manager = PluginManager(
            [
                Plugin("provider", setup, config={"fail": False}),
                Plugin(
                    "consumer",
                    lambda ctx: ctx.require("provider"),
                    requires=("provider",),
                ),
                Plugin("other", lambda _: 42),
            ]
        )
        async with manager.lifespan():
            with pytest.raises(RuntimeError, match="new connection failed"):
                await manager.configure("provider", {"fail": True})
            assert events == [False, True]
            with pytest.raises(PluginUnavailable), manager.lease("consumer"):
                pass
            with manager.lease("other") as value:
                assert value == 42
            await manager.configure("provider", {"fail": False})
            await manager.retry("provider")
            await manager.enable("consumer")
            with manager.lease("consumer") as value:
                assert value == 5

    async def test_cleanup_failure_attempts_remaining_callbacks_and_plugins(self):
        events = []

        def bad_cleanup():
            raise ValueError("cleanup failed")

        def setup(ctx):
            ctx.on_dispose(events.append, ctx.name)
            ctx.on_dispose(bad_cleanup)

        manager = PluginManager([Plugin("one", setup), Plugin("two", setup)])
        await manager.start()
        with pytest.raises(ExceptionGroup):
            await manager.close()
        assert events == ["two", "one"]
        assert all(p["error"] == "cleanup_failed" for p in manager.snapshot())
        await manager.close()

    async def test_host_owned_plugins_reject_direct_and_cascaded_changes(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda _: None),
                Plugin("host", lambda _: None, requires=("provider",), managed=False),
            ]
        )
        async with manager.lifespan():
            for action in (
                manager.disable("host"),
                manager.disable("provider", cascade=True),
                manager.configure("provider", {}),
            ):
                with pytest.raises(PluginError):
                    await action
            assert all(p["state"] == "active" for p in manager.snapshot())


@pytest.mark.parametrize(
    "plugins",
    [
        [Plugin("same", lambda _: None), Plugin("same", lambda _: None)],
        [Plugin("missing", lambda _: None, requires=("unknown",))],
        [
            Plugin("a", lambda _: None, requires=("b",)),
            Plugin("b", lambda _: None, requires=("a",)),
        ],
        [
            Plugin("a", lambda _: None, enabled=False),
            Plugin("b", lambda _: None, requires=("a",)),
        ],
    ],
)
def test_invalid_graph_fails_before_setup(plugins):
    with pytest.raises(DependencyError):
        PluginManager(plugins).validate()


def test_config_and_snapshot_do_not_expose_mutable_internal_state():
    config = {"secret": "private"}
    manager = PluginManager([Plugin("sample", lambda _: None, config=config)])
    config["secret"] = "changed"
    snapshot = manager.snapshot()
    snapshot[0]["requires"].append("intruder")
    assert "private" not in repr(manager.snapshot())
    assert manager.snapshot()[0]["requires"] == []
