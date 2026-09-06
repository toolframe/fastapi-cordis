import asyncio
from typing import Any, Protocol, runtime_checkable
from unittest import IsolatedAsyncioTestCase

import pytest

from fastapi_cordis import EventKey, Plugin, PluginError, PluginManager
from fastapi_cordis._events import _EventBus


class NonRuntimeProtocol(Protocol):
    value: int


@runtime_checkable
class RuntimeProtocol(Protocol):
    def value(self) -> int: ...


class EventKeyTests(IsolatedAsyncioTestCase):
    def test_key_validation_hash_and_payload_privacy(self):
        key = EventKey("data.changed", dict)
        same = EventKey("data.changed", dict)
        assert key == same
        assert hash(key) == hash(same)
        payload = {"secret": object()}
        assert key.validate(payload) is payload
        with pytest.raises(TypeError, match="data.changed") as error:
            key.validate([])
        assert repr(payload) not in str(error.value)

    def test_key_rejects_invalid_names_and_non_runtime_payload_types(self):
        for name in ("", "1event", "event name", "event/"):
            with pytest.raises(ValueError):
                EventKey(name, object)
        with pytest.raises(TypeError):
            EventKey("event", Any)
        with pytest.raises(TypeError):
            EventKey("event", list[int])
        with pytest.raises(TypeError):
            EventKey("event", NonRuntimeProtocol)
        with pytest.raises(TypeError):
            EventKey("event", 42)
        assert EventKey("none", type(None)).validate(None) is None
        assert EventKey("runtime", RuntimeProtocol)


class EventBusTests(IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_waits_for_async_handler_cleanup(self):
        bus = _EventBus()
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def handler(value):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.append(value)

        key = EventKey("cancel.again", int)
        bus.on(key, handler)
        emitting = asyncio.create_task(bus.emit(key, 1))
        await started.wait()
        emitting.cancel()
        await cleaning.wait()
        emitting.cancel()
        await asyncio.sleep(0)
        assert not emitting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await emitting
        assert cleaned == [1]

    async def test_order_and_async_callbacks_are_concurrent_and_awaited(self):
        bus = _EventBus()
        release = asyncio.Event()
        entered_event = asyncio.Event()
        entered = []
        completed = []

        def entered_callback(name: str, value: int) -> None:
            entered.append((name, value))
            if len(entered) == 2:
                entered_event.set()

        async def first(value: int):
            entered_callback("first", value)
            await release.wait()
            completed.append("first")

        async def second(value: int):
            entered_callback("second", value)
            await release.wait()
            completed.append("second")

        bus.on(EventKey("number", int), first)
        bus.on(EventKey("number", int), second)
        dispatch = asyncio.create_task(bus.emit(EventKey("number", int), 7))
        await entered_event.wait()
        assert entered == [("first", 7), ("second", 7)]
        assert not dispatch.done()
        release.set()
        await dispatch
        assert completed == ["first", "second"]

    async def test_failures_are_aggregated_after_all_callbacks_finish(self):
        bus = _EventBus()
        release = asyncio.Event()
        started = asyncio.Event()
        started_count = 0
        completed = []

        def mark_started() -> None:
            nonlocal started_count
            started_count += 1
            if started_count == 2:
                started.set()

        def sync_failure(value: int):
            completed.append("sync-start")
            raise ValueError("sync failure")

        async def async_failure(value: int):
            mark_started()
            await release.wait()
            completed.append("async-failure")
            raise RuntimeError("async failure")

        async def peer(value: int):
            mark_started()
            await release.wait()
            completed.append("peer")

        bus.on(EventKey("failure", int), sync_failure)
        bus.on(EventKey("failure", int), async_failure)
        bus.on(EventKey("failure", int), peer)
        dispatch = asyncio.create_task(bus.emit(EventKey("failure", int), 1))
        await started.wait()
        assert not dispatch.done()
        release.set()
        with pytest.raises(ExceptionGroup) as error:
            await dispatch
        assert completed == ["sync-start", "async-failure", "peer"]
        assert [type(item) for item in error.value.exceptions] == [
            ValueError,
            RuntimeError,
        ]

    async def test_unsubscribe_is_idempotent_and_duplicate_registrations_are_independent(
        self,
    ):
        bus = _EventBus()
        seen = []

        def callback(value: int):
            seen.append(value)

        first = bus.on(EventKey("duplicate", int), callback)
        second = bus.on(EventKey("duplicate", int), callback)
        first()
        first()
        await bus.emit(EventKey("duplicate", int), 1)
        assert seen == [1]
        second()
        await bus.emit(EventKey("duplicate", int), 2)
        assert seen == [1]

    async def test_dispatch_uses_snapshot_when_registration_changes(self):
        bus = _EventBus()
        seen = []
        added = False
        remove_current = None

        def late(value: int):
            seen.append(("late", value))

        def first(value: int):
            nonlocal added
            seen.append(("first", value))
            if not added:
                added = True
                bus.on(EventKey("snapshot", int), late)
                assert remove_current is not None
                remove_current()

        remove_current = bus.on(EventKey("snapshot", int), first)
        await bus.emit(EventKey("snapshot", int), 1)
        assert seen == [("first", 1)]
        await bus.emit(EventKey("snapshot", int), 2)
        assert seen == [("first", 1), ("late", 2)]

    async def test_contract_mismatch_is_rejected_before_listeners(self):
        bus = _EventBus()
        called = []
        bus.on(EventKey("contract", int), lambda value: called.append(value))
        with pytest.raises(TypeError, match="contract"):
            await bus.emit(EventKey("contract", str), "wrong")
        with pytest.raises(TypeError, match="contract"):
            bus.on(EventKey("contract", str), lambda value: called.append(value))
        assert called == []

    async def test_cancellation_cleans_up_all_dispatched_tasks(self):
        bus = _EventBus()
        started = asyncio.Event()
        cleanup = []

        async def handler(value: int):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append(value)

        bus.on(EventKey("cancel", int), handler)
        dispatch = asyncio.create_task(bus.emit(EventKey("cancel", int), 3))
        await started.wait()
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
        assert cleanup == [3]
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]


class PluginEventTests(IsolatedAsyncioTestCase):
    async def test_handlers_are_removed_on_disable_restart_and_failed_setup(self):
        key = EventKey("changed", int)
        seen = []
        contexts = []

        def setup(ctx):
            contexts.append(ctx)
            ctx.on(key, seen.append)

        manager = PluginManager([Plugin("listener", setup)])
        async with manager.lifespan():
            await manager.emit(key, 1)
            await manager.disable("listener")
            await manager.emit(key, 2)
            await manager.enable("listener")
            await contexts[-1].emit(key, 3)
            assert seen == [1, 3]
            with pytest.raises(PluginError, match="closed"):
                contexts[0].on(key, seen.append)

            def broken(ctx):
                ctx.on(key, seen.append)
                raise ValueError("setup failed")

            with pytest.raises(ValueError):
                await manager.register(Plugin("broken", broken))
            await manager.emit(key, 4)
            assert seen == [1, 3, 4]

    async def test_inflight_handlers_protect_their_transitive_resources(self):
        key = EventKey("work", str)
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []
        cleaned = []

        def provider(ctx):
            ctx.on_dispose(cleaned.append, "closed")
            return "connection"

        def listener(ctx):
            connection = ctx.require("provider")

            async def handle(value):
                entered.set()
                await release.wait()
                assert not cleaned
                seen.append((connection, value))

            ctx.on(key, handle)

        manager = PluginManager(
            [
                Plugin("provider", provider),
                Plugin("listener", listener, requires=("provider",)),
            ]
        )
        async with manager.lifespan():
            dispatch = asyncio.create_task(manager.emit(key, "first"))
            await entered.wait()
            stopping = asyncio.create_task(manager.disable("provider"))
            async with asyncio.timeout(1):
                while manager.snapshot()[0]["state"] != "draining":
                    await asyncio.sleep(0)
            assert not stopping.done()
            await manager.emit(key, "blocked")
            release.set()
            await dispatch
            await stopping
            assert seen == [("connection", "first")]
            assert cleaned == ["closed"]

    async def test_unsubscribe_and_handler_failure_do_not_leak_leases(self):
        key = EventKey("failure", int)
        disposers = []

        def setup(ctx):
            def fail(value):
                raise ValueError("handler failed")

            disposers.append(ctx.on(key, fail))

        manager = PluginManager([Plugin("listener", setup)])
        async with manager.lifespan():
            with pytest.raises(ExceptionGroup):
                await manager.emit(key, 1)
            assert manager.snapshot()[0]["inFlight"] == 0
            await disposers[0]()
            await disposers[0]()
            await manager.emit(key, 2)

    async def test_handler_cannot_deadlock_by_disabling_itself(self):
        key = EventKey("stop", int)

        def setup(ctx):
            async def handle(value):
                await manager.disable("listener")

            ctx.on(key, handle)

        manager = PluginManager([Plugin("listener", setup)])
        async with manager.lifespan():
            async with asyncio.timeout(1):
                with pytest.raises(ExceptionGroup) as failure:
                    await manager.emit(key, 1)
            assert isinstance(failure.value.exceptions[0], PluginError)
            assert manager.snapshot()[0]["state"] == "active"
