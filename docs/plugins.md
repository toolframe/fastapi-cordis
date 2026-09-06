# Writing plugins

A plugin provides one named service. Its setup function receives a `PluginContext`
and returns the value callers will use. Setup can be synchronous or asynchronous.

## Define a service and its configuration

```python
from fastapi_cordis import Plugin, PluginContext, PluginManager
from pydantic import BaseModel, ConfigDict


class FormatterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Report"


def setup_formatter(ctx: PluginContext):
    return lambda title: f"{ctx.config.prefix}: {title}"


formatter = Plugin(
    name="formatter",
    setup=setup_formatter,
    config={"prefix": "Summary"},
    config_model=FormatterConfig,
    description="Format report titles",
)
manager = PluginManager([formatter])
```

Configuration must be JSON-serializable. Without `config_model`, `ctx.config` is
a detached dictionary. With a model, it is a validated model instance. Mutating
that instance does not update the manager's stored configuration.

Constructing the manager validates definitions and configuration without running
setup. `manager.configure(name, config)` replaces configuration; it does not merge
with previous values. Omitted model fields use their defaults or fail validation
if required. See [lifecycle behavior](lifecycle.md) for restart and failure details.

## Declare dependencies

Extend the formatter example with a service that depends on it:

```python
def setup_reporter(ctx: PluginContext):
    format_title = ctx.require("formatter")
    return lambda title: {"title": format_title(title)}


reporter = Plugin("reporter", setup_reporter, requires=("formatter",))
manager = PluginManager([reporter, formatter])
```

The formatter starts first even though it appears second in the list. The reporter
stops first. A caller leasing the reporter also protects the formatter from being
disposed before that call finishes.

`requires` must be a tuple of plugin names or typed service keys. Names are unique,
start with a letter, and contain only letters, digits, `_`, `.`, or `-`. Cycles and
incompatible service contracts are rejected. Missing or disabled providers leave
enabled consumers blocked until their dependencies become available. Call
`manager.validate()` for strict validation requiring every provider to exist and
enabled plugins to have enabled dependencies.

Use `ctx.require(name)` only during setup, for a declared dependency. A plugin may
retain that dependency in its returned service. Other runtime callers acquire
services through `manager.lease(name)`.

## Separate a service from its provider

Use a `ServiceKey` when consumers should depend on an Interface instead of a
specific implementation. The key contains a stable name and a class or
`@runtime_checkable` Protocol. A provider declares `provides=KEY`; consumers
declare `requires=(KEY,)` and call `ctx.require(KEY)`. Runtime callers use
`manager.lease(KEY)`, and FastAPI routes can use `Depends(service(KEY))`.

For example, `examples/services.py` defines a `Formatter` Protocol and a
`ServiceKey("formatter", Formatter)`. Its unchanged reporter runs first with an
uppercase provider, then with a lowercase provider:

```sh
uv run python examples/services.py
```

The output is `{'formatted': 'HELLO'}` followed by `{'formatted': 'hello'}`.
Select the provider when constructing a manager; automatic live replacement of
plugin definitions is not supported.

Each plugin still returns one value and may declare one typed service key. Names
cannot collide with another provider or another plugin's name. Consumers using
the same service name must agree on its contract. The manager validates these
definitions before setup and checks the returned service with `isinstance` before
it becomes available. A contract failure triggers setup cleanup. Runtime Protocol
checks verify member presence; static checking and behavior tests remain necessary
to verify signatures and semantics.

The manager resolves dependency order, leases, cascades, and restarts through the
selected provider. Management operations still use the actual plugin name. A
snapshot's `provides` identifies the stable service, and `requires` contains the
resolved provider plugin names. Existing string references continue to work;
strings can also refer to a provided service name.

## Register cleanup as resources are acquired

This plugin registers cleanup immediately after creating its resource:

```python
from io import StringIO

from fastapi_cordis import Plugin, PluginContext


def setup_buffer(ctx: PluginContext):
    buffer = StringIO()
    ctx.on_dispose(buffer.close)
    return buffer


buffer_plugin = Plugin("buffer", setup_buffer)
```

Pass the callback, not its result: `ctx.on_dispose(client.aclose)`, not
`ctx.on_dispose(client.aclose())`. Additional positional and keyword arguments
are supported. Cleanup runs in reverse registration order and awaits asynchronous
results. Registered cleanup also runs if a later step in setup fails.

The returned service is used as a value; returning a context manager does not
automatically enter it. Register explicit cleanup for resources you open.
`ctx.on_dispose` accepts registrations throughout the current plugin lifetime and
returns an idempotent asynchronous disposer. Await that disposer to release one
resource early. A context from a stopped generation rejects new resources.

Setup and cleanup run on the manager's asyncio loop. Synchronous callbacks must
be brief and non-blocking. Use the owned-resource helpers for background work:

```python
async def setup_client(ctx):
    client = await ctx.enter_async_context(open_client())

    async def poll():
        while True:
            await client.receive()

    ctx.task(poll(), name="client-poll")
    return client
```

Here `open_client()` is an application-provided async context manager. The runtime
cancels and joins the poll task before closing the client. Tasks must cooperate
with asyncio cancellation. Task failures are retrieved immediately and reported
when their scope closes; they are not silently abandoned.

| Helper | Behavior |
| --- | --- |
| `ctx.on_dispose(callback, *args, **kwargs)` | Own sync or async cleanup |
| `ctx.effect(register)` | Run a synchronous registration returning a cleanup callable |
| `ctx.enter_context(resource)` | Enter and own a synchronous context manager |
| `await ctx.enter_async_context(resource)` | Enter and own an async context manager |
| `ctx.task(coroutine, name=...)` | Start an owned asyncio task |
| `ctx.scope()` | Create a child `EffectScope` for resources that can close together |

The child scope has the same helpers and can be closed with `await child.aclose()`.
Parent close also closes children before the parent's resources. Resource scopes
own cleanup; they do not select different service providers. `EffectScope` can
also be used independently as an async context manager.

An effect's registration must return a disposer synchronously; the disposer itself
may be asynchronous. Use `enter_async_context` for asynchronous acquisition. If
close races with an acquisition, the acquisition settles and its resource exits
before close completes. Cancellation of the acquiring caller is propagated only
after acquisition settles. Resource context managers receive a normal exit at
disposal, rather than an exception from an earlier unrelated request.

Owned tasks may capture their plugin's declared dependencies during setup. They
do not need a permanent lease on their own plugin. Unowned tasks and worker
threads still need explicit leases or cancellation/join callbacks; the manager
does not discover arbitrary tasks or stop Python threads.

Do not await lifecycle mutations on the same manager from setup or cleanup:
recursive mutations raise `PluginError` instead of waiting for their own operation.
An owned task or cleanup callback must not close its own resource scope.
Owned work also cannot stop its own plugin or dependencies, including from task
cancellation cleanup; the manager rejects these operations before waiting.
Lease bookkeeping supports callers in worker threads, but the service must itself
support whichever concurrency the application permits.

Use `ctx.on(EventKey(...), callback)` for a listener that follows the plugin's
lifetime and `await ctx.emit(key, payload)` for asynchronous dispatch. See
[typed events](events.md) for lease protection and failure semantics.

## Distribute reusable plugins

A separate Python package can export a `Plugin` definition or a factory returning
one. The host imports it and passes the resulting definition to `PluginManager`.
Keep resource acquisition inside setup so importing or validating a plugin does
not open connections or start workers.

Declare a compatible `fastapi-cordis` dependency in the plugin package's metadata.
Before a public release exists, the host must supply its locally built wheel or
configure its resolver to use a local source. The runtime does not download
packages or scan entry points; installed plugin selection is explicit.

Set `enabled=False` for a plugin that should start disabled. Set `managed=False`
for infrastructure reserved for the host: ordinary startup and shutdown still
apply, but runtime enable, disable, retry, unregister, and configure operations are rejected,
including cascades that would stop or restart a protected dependent.

Use `await manager.register(definition)` to add an installed plugin while running.
It validates the candidate graph before changing live state and starts ready
consumers automatically. Failed setup retains the definition with a failed state,
allowing explicit retry. `await manager.unregister(name)` drains and removes a
definition; enabled consumers remain blocked until a replacement provider arrives.
These methods accept trusted Python definitions, not package names or source code.
