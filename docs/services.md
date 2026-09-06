# Service contracts and namespaces

`ServiceKey[T](name, contract)` identifies a capability independently from the
plugin that provides it. Contracts are ordinary classes or runtime-checkable
Protocols. Runtime checks use `isinstance`; they do not verify complete method
signatures. A declaration with `provides=KEY` publishes the setup return value
under that key unless setup explicitly calls `ctx.provide(KEY, value)`.

## Multiple capabilities

Declare all keys in `provides=(KEY_A, KEY_B)` and publish each during setup:

```python
from fastapi_cordis import Plugin, PluginManager, ServiceKey

NUMBER = ServiceKey("number", int)
TEXT = ServiceKey("text", str)


def provide_values(ctx):
    ctx.provide(NUMBER, 7)
    ctx.provide(TEXT, "seven")


provider = Plugin("values", provide_values, provides=(NUMBER, TEXT))
manager = PluginManager([provider])
```

Consumers declare the exact capabilities they use in `requires` and access them
with `ctx.require(KEY)` during setup. An undeclared, duplicate, incorrectly typed,
or missing publication fails setup and cleans partial resources. A plugin with
multiple keys must explicitly publish every key. Consumers cannot access an
undeclared capability merely because it shares a provider with a declared one.

Legacy setup return values are available by plugin name. Service aliases resolve
to their published values; if a service alias equals its own plugin name, the
published capability takes precedence. Leases count each provider only once even
when a consumer uses several capabilities from that provider.

## Workspace-specific providers

`Plugin(..., scope="work.a")` places a plugin in a named service namespace. Lookup
checks that scope, then `work`, then the root namespace. Sibling scopes do not
see each other's providers. The empty scope is the root and preserves legacy
unqualified names.

```python
root = Plugin("values", lambda _: 1, provides=NUMBER)
local = Plugin("values", lambda _: 2, provides=NUMBER, scope="work.a")
report = Plugin(
    "report",
    lambda ctx: ctx.require(NUMBER),
    requires=(NUMBER,),
    scope="work.a",
)
manager = PluginManager([root, local, report])
```

After starting, `manager.lease(NUMBER, scope="work.a")` and
`manager.lease("work.a/report")` both yield `2`.
`manager.lease("/number", scope="work.a")` explicitly selects the root value `1`.
A namespace inherits provider visibility; it does not create new parent instances.

`plugin.id`, snapshot `name`, and `ctx.name` identify a scoped instance as
`scope/plugin-name`, such as `work.a/values`. Root instance IDs remain unchanged.
Lifecycle management uses those IDs. `ctx.service_scope` identifies the service
namespace; `ctx.scope()` instead creates a child resource lifetime.

Only one provider may own a service name in a namespace. A local provider shadows
an ancestor even while disabled, so local consumers block until it is enabled or
removed. Incompatible local contracts are rejected rather than falling back.
Events are manager-wide; service namespaces do not filter event subscriptions.

## Changing providers

Registering a local override drains and restarts affected consumers before they
use it. Existing external leases on unchanged ancestor providers remain valid.
Removing the local provider drains its users, then lets consumers bind to an
available ancestor. The candidate graph is checked for cycles and incompatible
contracts before resources change. Protected host-owned consumers cannot be
rebound by a runtime override.

These namespaces provide dependency isolation, not a security sandbox. The host
owns which trusted definitions are created or registered. There is no Python
module reloading or automatic discovery of installed packages.
