import asyncio
from contextlib import asynccontextmanager, contextmanager
from unittest import IsolatedAsyncioTestCase

import pytest

from fastapi_cordis import EffectScope, Plugin, PluginError, PluginManager


class ScopeTests(IsolatedAsyncioTestCase):
    async def test_resources_can_be_registered_after_setup_and_disposed_early(self):
        events = []
        contexts = []

        def setup(ctx):
            contexts.append(ctx)
            return ctx

        manager = PluginManager([Plugin("resources", setup)])
        async with manager.lifespan():
            ctx = contexts[-1]
            ctx.on_dispose(events.append, "last")
            dispose = ctx.effect(lambda: lambda: events.append("early"))
            await dispose()
            await dispose()
            assert events == ["early"]
            await manager.disable("resources")
            assert events == ["early", "last"]
            with pytest.raises(PluginError, match="closed"):
                ctx.on_dispose(lambda: None)
            await manager.enable("resources")
            assert contexts[-1] is not ctx
            with pytest.raises(PluginError, match="closed"):
                ctx.scope()

    async def test_context_managers_and_child_tasks_close_before_parent_resources(self):
        events = []
        started = asyncio.Event()

        @contextmanager
        def connection():
            events.append("open")
            try:
                yield object()
            finally:
                events.append("closed")

        @asynccontextmanager
        async def async_connection():
            try:
                yield object()
            finally:
                events.append("async.closed")

        async with EffectScope() as scope:
            child = scope.scope()
            scope.enter_context(connection())
            await scope.enter_async_context(async_connection())

            async def worker():
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    assert "closed" not in events
                    assert "async.closed" not in events
                    events.append("worker.joined")

            child.task(worker())
            await started.wait()
        assert events == ["open", "worker.joined", "async.closed", "closed"]
        await scope.aclose()
        with pytest.raises(PluginError, match="closed"):
            child.task(asyncio.sleep(0))

    async def test_task_and_cleanup_failures_are_reported_and_all_cleanup_runs(self):
        events = []

        async def failed_task():
            raise ValueError("task failed")

        def failed_cleanup():
            events.append("bad")
            raise RuntimeError("cleanup failed")

        scope = EffectScope()
        scope.on_dispose(events.append, "first")
        scope.on_dispose(failed_cleanup)
        scope.on_dispose(events.append, "last")
        task = scope.task(failed_task())
        with pytest.raises(ValueError):
            await task
        with pytest.raises(ExceptionGroup) as failure:
            await scope.aclose()
        assert events == ["last", "bad", "first"]
        assert {type(error) for error in failure.value.exceptions} == {
            ValueError,
            RuntimeError,
        }

    async def test_cancelled_close_waits_for_cleanup_and_remains_idempotent(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def cleanup():
            entered.set()
            await release.wait()
            events.append("closed")

        scope = EffectScope()
        scope.on_dispose(cleanup)
        closing = asyncio.create_task(scope.aclose())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(PluginError, match="closed"):
            scope.on_dispose(lambda: None)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await scope.aclose()
        assert events == ["closed"]

    async def test_close_waits_for_late_async_acquisition_and_releases_it(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        @asynccontextmanager
        async def resource():
            entered.set()
            await release.wait()
            try:
                yield "value"
            finally:
                events.append("closed")

        scope = EffectScope()
        acquiring = asyncio.create_task(scope.enter_async_context(resource()))
        await entered.wait()
        closing = asyncio.create_task(scope.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(PluginError, match="closed during"):
            await acquiring
        await closing
        assert events == ["closed"]

    async def test_cancelled_acquisition_settles_before_propagating(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        @asynccontextmanager
        async def resource():
            entered.set()
            await release.wait()
            try:
                yield "value"
            finally:
                events.append("closed")

        async with EffectScope() as scope:
            acquiring = asyncio.create_task(scope.enter_async_context(resource()))
            await entered.wait()
            acquiring.cancel()
            await asyncio.sleep(0)
            assert not acquiring.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await acquiring
        assert events == ["closed"]

    async def test_acquisition_race_cleanup_failure_is_not_lost(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        @asynccontextmanager
        async def resource():
            entered.set()
            await release.wait()
            try:
                yield None
            finally:
                raise ValueError("late exit failed")

        scope = EffectScope()
        acquiring = asyncio.create_task(scope.enter_async_context(resource()))
        await entered.wait()
        closing = asyncio.create_task(scope.aclose())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(ValueError, match="late exit"):
            await acquiring
        with pytest.raises(ExceptionGroup) as failure:
            await closing
        assert isinstance(failure.value.exceptions[0], ValueError)

    async def test_manual_disposal_racing_close_runs_cleanup_once(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def cleanup():
            entered.set()
            await release.wait()
            events.append("closed")

        scope = EffectScope()
        dispose = scope.on_dispose(cleanup)
        disposing = asyncio.create_task(dispose())
        await entered.wait()
        closing = asyncio.create_task(scope.aclose())
        disposing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await disposing
        await closing
        assert events == ["closed"]

    async def test_failed_plugin_setup_cancels_its_tasks_and_closes_resources(self):
        started = asyncio.Event()
        events = []

        async def worker():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                events.append("task.closed")

        async def setup(ctx):
            ctx.on_dispose(events.append, "resource.closed")
            ctx.task(worker())
            await started.wait()
            raise ValueError("setup failed")

        manager = PluginManager([Plugin("worker", setup)])
        with pytest.raises(ValueError, match="setup failed"):
            await manager.start()
        assert events == ["task.closed", "resource.closed"]

    async def test_recursive_mutation_in_setup_fails_instead_of_deadlocking(self):
        async def setup(ctx):
            await manager.enable("worker")

        manager = PluginManager([Plugin("worker", setup)])
        async with asyncio.timeout(1):
            with pytest.raises(PluginError, match="recursively"):
                await manager.start()

    async def test_recursive_mutation_in_cleanup_is_reported(self):
        def setup(ctx):
            ctx.on_dispose(manager.close)

        manager = PluginManager([Plugin("worker", setup)])
        await manager.start()
        async with asyncio.timeout(1):
            with pytest.raises(ExceptionGroup):
                await manager.close()

    async def test_owned_task_cannot_close_its_own_scope(self):
        scope = EffectScope()

        async def worker():
            with pytest.raises(PluginError, match="own scope"):
                await scope.aclose()

        await scope.task(worker())
        await scope.aclose()
