"""Async lifecycles with thread-safe leases and explicit dependency ownership."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
import threading
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar, overload

from pydantic import BaseModel

from ._errors import (
    ConfigurationError,
    DependencyError,
    DrainTimeout,
    PluginError,
    PluginUnavailable,
    UnknownPlugin,
)
from ._graph import DependencyGraph, build_graph
from ._scope import EffectScope
from ._services import ServiceKey

T = TypeVar("T")
_MUTATING: ContextVar[tuple[Any, object] | None] = ContextVar(
    "cordis_mutation", default=None
)


@dataclass(frozen=True)
class Plugin:
    """One named service. Setup returns its value and registers cleanup on ctx."""

    name: str
    setup: Callable[[PluginContext], Any]
    requires: tuple[str | ServiceKey[Any], ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    config_model: type[BaseModel] | None = None
    enabled: bool = True
    managed: bool = True
    description: str = ""
    provides: ServiceKey[Any] | None = None


class PluginContext:
    def __init__(
        self, manager: PluginManager, plugin: Plugin, config: Any, stack: EffectScope
    ) -> None:
        self.name = plugin.name
        self.config = config
        self._manager = manager
        self._requires = manager._dependencies[plugin.name]
        self._stack = stack
        self._accepting = True

    @overload
    def require(self, name: ServiceKey[T]) -> T: ...

    @overload
    def require(self, name: str) -> Any: ...

    def require(self, name: str | ServiceKey[Any]) -> Any:
        """Get a declared dependency during setup, owned until this plugin stops."""
        if not self._accepting:
            raise PluginError(
                "require() is only available during setup; use a lease at call time"
            )
        provider = self._manager._resolve_service(name)
        if provider not in self._requires:
            raise DependencyError(f"{self.name!r} did not declare dependency {name!r}")
        return self._manager._values[provider]

    def on_dispose(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """Own cleanup throughout the plugin's current lifetime."""
        return self._stack.on_dispose(callback, *args, **kwargs)

    def effect(self, register: Callable[[], Callable[[], Any]]):
        return self._stack.effect(register)

    def enter_context(self, resource: AbstractContextManager[T]) -> T:
        return self._stack.enter_context(resource)

    async def enter_async_context(self, resource: AbstractAsyncContextManager[T]) -> T:
        return await self._stack.enter_async_context(resource)

    def task(self, coroutine: Coroutine[Any, Any, T], *, name: str | None = None):
        return self._stack.task(coroutine, name=name)

    def scope(self) -> EffectScope:
        return self._stack.scope()


class PluginManager:
    """One asyncio loop owns mutations; any thread may hold synchronous leases.

    Services must only be used within a lease, or by a declared dependent plugin.
    Mutations finish even when the caller is cancelled. No source-code reload,
    dependency installation, subprocess isolation, or multi-worker coordination.
    """

    def __init__(
        self, plugins: Sequence[Plugin] = (), *, drain_timeout: float = 30.0
    ) -> None:
        if not math.isfinite(drain_timeout) or drain_timeout <= 0:
            raise ValueError("drain_timeout must be a positive finite number")
        self.drain_timeout = drain_timeout
        self._plugins: dict[str, Plugin] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._enabled: dict[str, bool] = {}
        self._state: dict[str, str] = {}
        self._errors: dict[str, str | None] = {}
        self._values: dict[str, Any] = {}
        self._stacks: dict[str, EffectScope] = {}
        self._counts: dict[str, int] = {}
        self._guard = threading.RLock()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._operation_token: object | None = None
        for plugin in plugins:
            self._validate_plugin(plugin)
            if plugin.name in self._plugins:
                raise DependencyError(f"Duplicate plugin: {plugin.name!r}")
            self._plugins[plugin.name] = plugin
            self._configs[plugin.name] = self._validate_config(plugin, plugin.config)
            self._enabled[plugin.name] = plugin.enabled
            self._state[plugin.name] = "registered" if plugin.enabled else "disabled"
            self._errors[plugin.name] = None
            self._counts[plugin.name] = 0
        self._set_graph(build_graph(self._plugins))
        self._mark_blocked()

    @staticmethod
    def _validate_plugin(plugin: Plugin) -> None:
        if not isinstance(plugin, Plugin):
            raise TypeError("Expected Plugin definitions")
        if not isinstance(plugin.name, str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_.-]*", plugin.name
        ):
            raise ValueError(f"Invalid plugin name: {plugin.name!r}")
        if not callable(plugin.setup):
            raise TypeError(f"Plugin {plugin.name!r} setup must be callable")
        if not isinstance(plugin.requires, tuple) or not all(
            isinstance(n, (str, ServiceKey)) for n in plugin.requires
        ):
            raise TypeError("Plugin requires must be a tuple of names or ServiceKeys")
        if plugin.provides is not None and not isinstance(plugin.provides, ServiceKey):
            raise TypeError("Plugin provides must be a ServiceKey")

    def _set_graph(self, graph: DependencyGraph) -> None:
        self._providers = graph.providers
        self._dependencies = graph.dependencies
        self._missing = graph.missing
        self._order = graph.order

    def _resolve_service(self, reference: str | ServiceKey[Any]) -> str:
        name = reference.name if isinstance(reference, ServiceKey) else reference
        if name not in self._providers:
            raise UnknownPlugin(name)
        provider = self._providers[name]
        if (
            isinstance(reference, ServiceKey)
            and self._plugins[provider].provides != reference
        ):
            raise DependencyError(f"Incompatible contract for service {name!r}")
        return provider

    @staticmethod
    def _validate_config(plugin: Plugin, config: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(config, Mapping) or not all(
            isinstance(key, str) for key in config
        ):
            raise ConfigurationError(
                "Plugin config must be a JSON object with string keys"
            )
        try:
            detached = json.loads(json.dumps(dict(config), allow_nan=False))
            if plugin.config_model is not None:
                plugin.config_model.model_validate(detached)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"Invalid configuration for {plugin.name!r}"
            ) from error
        return detached

    def validate(self) -> None:
        """Strictly require all providers and enabled dependency intentions."""
        for name in self._order:
            if self._missing[name]:
                raise DependencyError(
                    f"Missing dependencies of {name!r}: {', '.join(self._missing[name])}"
                )
            if self._enabled[name]:
                for dependency in self._dependencies[name]:
                    if not self._enabled[dependency]:
                        raise DependencyError(
                            f"Enabled plugin {name!r} requires disabled {dependency!r}"
                        )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return detached diagnostics. Configuration and service values stay private."""
        with self._guard:
            return [
                {
                    "name": name,
                    "description": self._plugins[name].description,
                    "requires": list(self._dependencies[name]),
                    "provides": (
                        self._plugins[name].provides.name
                        if self._plugins[name].provides is not None
                        else name
                    ),
                    "enabled": self._enabled[name],
                    "managed": self._plugins[name].managed,
                    "state": self._state[name],
                    "inFlight": self._counts[name],
                    "error": self._errors[name],
                    "blockedBy": self._blocked_by(name),
                }
                for name in self._order
            ]

    def _ancestors(self, name: str) -> set[str]:
        self._lookup(name)
        names = {name}
        for dependency in self._dependencies[name]:
            names.update(self._ancestors(dependency))
        return names

    @overload
    def lease(self, name: ServiceKey[T]) -> AbstractContextManager[T]: ...

    @overload
    def lease(self, name: str) -> AbstractContextManager[Any]: ...

    @contextmanager
    def lease(self, name: str | ServiceKey[Any]) -> Iterator[Any]:
        """Hold a service and all its dependencies until actual work has finished."""
        with self._guard:
            name = self._resolve_service(name)
            names = self._ancestors(name)
            if any(self._state[item] != "active" for item in names):
                raise PluginUnavailable(name)
            for item in names:
                self._counts[item] += 1
            value = self._values[name]
        try:
            yield value
        finally:
            with self._guard:
                for item in names:
                    self._counts[item] -= 1
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._changed.set)

    def _lookup(self, name: str, *, managed: bool = False) -> Plugin:
        if name not in self._plugins:
            raise UnknownPlugin(name)
        plugin = self._plugins[name]
        if managed and not plugin.managed:
            raise PluginError(f"Plugin {name!r} is owned by the host")
        return plugin

    async def _atomic(self, operation: Callable[[], Any]) -> Any:
        owner = _MUTATING.get()
        if owner is not None and owner[0] is self and owner[1] is self._operation_token:
            raise PluginError(
                "Lifecycle callbacks cannot recursively mutate their manager"
            )
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise PluginError("PluginManager belongs to another event loop")
        self._loop = loop

        async def serialized() -> Any:
            async with self._lock:
                self._operation_token = object()
                token = _MUTATING.set((self, self._operation_token))
                try:
                    return await operation()
                finally:
                    self._operation_token = None
                    _MUTATING.reset(token)

        task = asyncio.create_task(serialized())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:  # noqa: BLE001 - task.result() propagates the original failure below.
                break
        if cancelled:
            # Retrieve any failure so an abandoned operation cannot emit a warning.
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    async def _activate(self, name: str, *, publish: bool = True) -> None:
        plugin = self._plugins[name]
        if self._missing[name] or any(
            self._state[dep] not in {"active", "starting"} or dep not in self._values
            for dep in self._dependencies[name]
        ):
            raise DependencyError(f"Dependencies of {name!r} are not active")
        stack = EffectScope()
        config = copy.deepcopy(self._configs[name])
        if plugin.config_model is not None:
            config = plugin.config_model.model_validate(config)
        context = PluginContext(self, plugin, config, stack)
        with self._guard:
            self._state[name] = "starting"
            self._errors[name] = None
        try:
            value = plugin.setup(context)
            if inspect.isawaitable(value):
                value = await value
            if plugin.provides is not None:
                plugin.provides.validate(value)
        except BaseException as error:
            context._accepting = False
            with self._guard:
                self._state[name] = "failed"
                self._errors[name] = "setup_failed"
            try:
                await stack.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve both failures in the group.
                raise BaseExceptionGroup(
                    "Plugin setup and cleanup failed", [error, cleanup_error]
                )
            raise
        finally:
            context._accepting = False
        with self._guard:
            self._stacks[name] = stack
            self._values[name] = value
            self._state[name] = "active" if publish else "starting"

    async def _dispose(self, names: set[str]) -> None:
        errors: list[BaseException] = []
        for name in reversed(self._order):
            if name not in names:
                continue
            stack = self._stacks.pop(name, None)
            with self._guard:
                self._values.pop(name, None)
                self._state[name] = "stopped"
            if stack is not None:
                try:
                    await stack.aclose()
                except BaseException as error:  # noqa: BLE001 - attempt every cleanup, then raise the group.
                    with self._guard:
                        self._state[name] = "failed"
                        self._errors[name] = "cleanup_failed"
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("Plugin cleanup failed", errors)

    def _active_dependents(self, name: str) -> set[str]:
        return {
            item
            for item in self._order
            if self._state[item] == "active" and name in self._ancestors(item)
        }

    async def _drain(self, names: set[str]) -> None:
        with self._guard:
            previous = {name: self._state[name] for name in names}
            for name in names:
                self._state[name] = "draining"
        try:
            async with asyncio.timeout(self.drain_timeout):
                while True:
                    self._changed.clear()
                    with self._guard:
                        if all(self._counts[name] == 0 for name in names):
                            return
                    await self._changed.wait()
        except BaseException as error:
            with self._guard:
                self._state.update(previous)
            if isinstance(error, TimeoutError):
                raise DrainTimeout(
                    "Plugins are still in use; no resources were disposed"
                ) from error
            raise

    def _require_running(self) -> None:
        if not self._running:
            raise PluginError("PluginManager has not started")

    def _blocked_by(self, name: str) -> list[str]:
        return [
            *self._missing[name],
            *(
                dep
                for dep in self._dependencies[name]
                if self._state[dep] not in {"active", "starting"}
            ),
        ]

    def _mark_blocked(self) -> None:
        with self._guard:
            for name in self._order:
                if self._state[name] in {"active", "starting", "draining", "failed"}:
                    continue
                if not self._enabled[name]:
                    self._state[name] = "disabled"
                elif self._blocked_by(name):
                    self._state[name] = "blocked"
                elif self._state[name] == "blocked":
                    self._state[name] = "registered"

    async def _reconcile(self) -> None:
        """Publish the newly ready group together; failed plugins require retry."""
        started: set[str] = set()
        try:
            for name in self._order:
                if (
                    self._enabled[name]
                    and self._state[name] not in {"active", "failed"}
                    and not self._blocked_by(name)
                ):
                    await self._activate(name, publish=False)
                    started.add(name)
        except BaseException as error:
            await self._drain(started)
            try:
                await self._dispose(started)
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve activation and cleanup failures.
                raise BaseExceptionGroup(
                    "Activation and cleanup failed", [error, cleanup_error]
                ) from None
            finally:
                self._mark_blocked()
            raise
        with self._guard:
            for name in started:
                self._state[name] = "active"
            self._mark_blocked()

    async def start(self) -> None:
        async def operation() -> None:
            if self._running:
                return
            # An explicit new startup attempt retries enabled startup failures.
            # Dependency recovery during a running session does not do this.
            with self._guard:
                for name in self._order:
                    if self._enabled[name] and self._state[name] == "failed":
                        self._state[name] = "registered"
                        self._errors[name] = None
            await self._reconcile()
            with self._guard:
                self._running = True

        await self._atomic(operation)

    async def close(self) -> None:
        async def operation() -> None:
            names = set(self._stacks)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                self._running = False

        await self._atomic(operation)

    @asynccontextmanager
    async def lifespan(self):
        try:
            await self.start()
            yield self
        finally:
            await self.close()

    async def enable(self, name: str) -> None:
        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            if self._state[name] == "failed":
                raise PluginError("Failed plugins require retry()")
            with self._guard:
                self._enabled[name] = True
            await self._reconcile()

        await self._atomic(operation)

    async def disable(self, name: str, *, cascade: bool = False) -> None:
        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            names = self._active_dependents(name)
            disabled = (
                {item for item in self._order if name in self._ancestors(item)}
                if cascade
                else {name}
            )
            for item in names | disabled:
                self._lookup(item, managed=True)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                with self._guard:
                    for item in disabled:
                        self._enabled[item] = False
                        if self._state[item] != "failed":
                            self._state[item] = "disabled"
                    self._mark_blocked()

        await self._atomic(operation)

    async def configure(self, name: str, config: Mapping[str, Any]) -> None:
        """Replace config and restart active dependents. Failed setup stays stopped.

        External side effects cannot be rolled back. The new config is retained so
        operators can correct it and explicitly enable the affected plugins.
        """
        # Copy at the call boundary so callers cannot mutate a queued config.
        config = self._validate_config(self._lookup(name, managed=True), config)

        async def operation() -> None:
            self._require_running()
            plugin = self._lookup(name, managed=True)
            replacement = self._validate_config(plugin, config)
            names = self._active_dependents(name)
            for item in names:
                self._lookup(item, managed=True)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                self._mark_blocked()
            self._configs[name] = replacement
            await self._reconcile()

        await self._atomic(operation)

    async def retry(self, name: str) -> None:
        """Explicitly retry one failed plugin and resume its blocked consumers."""

        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            if self._state[name] != "failed":
                raise PluginError("Only failed plugins can be retried")
            with self._guard:
                self._enabled[name] = True
                self._state[name] = "registered"
                self._errors[name] = None
            await self._reconcile()

        await self._atomic(operation)

    async def register(self, plugin: Plugin) -> None:
        """Add a definition and reconcile consumers waiting for its services."""
        self._validate_plugin(plugin)
        config = self._validate_config(plugin, plugin.config)

        async def operation() -> None:
            self._require_running()
            if plugin.name in self._plugins:
                raise DependencyError(f"Duplicate plugin: {plugin.name!r}")
            candidate = {**self._plugins, plugin.name: plugin}
            graph = build_graph(candidate)
            with self._guard:
                self._plugins = candidate
                self._configs[plugin.name] = config
                self._enabled[plugin.name] = plugin.enabled
                self._state[plugin.name] = (
                    "registered" if plugin.enabled else "disabled"
                )
                self._errors[plugin.name] = None
                self._counts[plugin.name] = 0
                self._set_graph(graph)
            await self._reconcile()

        await self._atomic(operation)

    async def unregister(self, name: str) -> None:
        """Remove a definition, leaving enabled dependents waiting for a provider."""

        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            candidate = {
                key: value for key, value in self._plugins.items() if key != name
            }
            graph = build_graph(candidate)
            names = self._active_dependents(name)
            for item in names:
                self._lookup(item, managed=True)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                self._mark_blocked()
            with self._guard:
                self._plugins = candidate
                for values in (
                    self._configs,
                    self._enabled,
                    self._state,
                    self._errors,
                    self._counts,
                ):
                    values.pop(name)
                self._set_graph(graph)
            await self._reconcile()

        await self._atomic(operation)
