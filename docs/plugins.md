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

`requires` must be a tuple of registered plugin names. Names are unique, start with
a letter, and contain only letters, digits, `_`, `.`, or `-`. Missing dependencies
and cycles are rejected. An enabled plugin cannot initially depend on a disabled
plugin; disabled definitions are still validated.

Use `ctx.require(name)` only during setup, for a declared dependency. A plugin may
retain that dependency in its returned service. Other runtime callers acquire
services through `manager.lease(name)`.

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
`ctx.on_dispose` accepts registrations only while setup is running.

Setup and cleanup run on the manager's asyncio loop. Synchronous callbacks must
be brief and non-blocking. If setup creates background workers, arrange their
cancellation and completion during cleanup. The manager does not discover or
terminate those workers automatically.

Do not await lifecycle mutations on the same manager from setup or cleanup:
mutations are serialized, so the callback would wait for its own operation to end.
Lease bookkeeping supports callers in worker threads, but the service must itself
support whichever concurrency the application permits.

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
apply, but runtime enable, disable, and configure operations are rejected,
including cascades that would stop or restart a protected dependent.
