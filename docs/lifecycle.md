# Lifecycle and service leases

Each manager belongs to one asyncio event loop. Start it, change plugin state, and
close it on that loop. Synchronous callers may hold leases from worker threads.

## Use the runtime without an HTTP server

```python
import asyncio

from fastapi_cordis import Plugin, PluginManager


async def main():
    manager = PluginManager([Plugin("formatter", lambda _: str.upper)])
    async with manager.lifespan():
        with manager.lease("formatter") as format_title:
            print(format_title("monthly report"))


if __name__ == "__main__":
    asyncio.run(main())
```

This prints `MONTHLY REPORT`. Use one lifespan owner per manager. A host may call
`start()` and `close()` directly instead, with cleanup in a `finally` block.

| Operation | Behavior |
| --- | --- |
| `manager.validate()` | Strictly check provider presence and enabled dependencies without executing setup |
| `await manager.start()` | Start enabled plugins whose dependencies are available |
| `await manager.close()` | Drain calls and dispose resources in reverse dependency order |
| `await manager.enable(name)` | Record enabled intent and start ready plugins |
| `await manager.disable(name)` | Stop the plugin and block enabled dependents |
| `await manager.disable(name, cascade=True)` | Explicitly disable the whole dependent group |
| `await manager.retry(name)` | Retry a failed plugin and resume ready consumers |
| `await manager.register(plugin)` | Add a definition and reconcile waiting plugins |
| `await manager.unregister(name)` | Remove a definition and block affected consumers |
| `await manager.configure(name, config)` | Validate replacement configuration and restart the affected active group |
| `manager.snapshot()` | Return detached diagnostic records |

The constructor validates plugin names, cycles, service contracts, and every
configuration. Missing providers are allowed so they can arrive later. Use the
optional strict `validate()` check if the complete graph must exist at startup.
Runtime management methods require a started manager. Updating a disabled plugin's
configuration does not enable it.

Ordinary disable changes only the named plugin's enabled intent. Consumers stop,
retain enabled intent, and enter `blocked`; enabling the provider resumes them.
With `cascade=True`, disabled intent applies to the whole dependent group,
including consumers already blocked. Re-enable that group explicitly afterward.
A failed plugin never retries merely because its dependency reappears: correct
its configuration and call `retry(name)`. Calling `start()` again after failed
initial startup explicitly retries enabled startup failures.

## Keep leases around the actual work

`with manager.lease(name) as value` protects that service and its transitive
dependencies until the block exits. Only active services can be leased; otherwise
the runtime raises `PluginUnavailable`.

Do not keep the value after releasing its lease. In a synchronous worker, acquire
the lease inside the worker function. If an HTTP request times out while its
thread continues running, that thread must retain the resource until it finishes.
Background tasks started with `ctx.task()` are cancelled and joined during their
owner's cleanup, while declared dependencies are still alive. Other background
tasks need their own lease or explicit cancellation and completion callbacks.

Do not stop or reconfigure a service while holding a lease on the affected group
in the same task: that operation would wait for the lease you still hold.

## Read plugin state

| State | Meaning |
| --- | --- |
| `registered` | Defined and enabled, but not started |
| `disabled` | Disabled by configuration or a runtime operation |
| `blocked` | Enabled, but waiting for missing or unavailable dependencies |
| `starting` | Setup is in progress, or the group is waiting to become active |
| `active` | Available for new leases |
| `draining` | Waiting for existing leases; new leases are rejected |
| `stopped` | Resources have been disposed |
| `failed` | Setup or cleanup failed |

Each snapshot contains `name`, `description`, `requires`, `provides`, `enabled`, `managed`,
`state`, `inFlight`, `blockedBy`, and `error`. Configuration and service values are omitted.
`enabled` records the desired flag; it does not prove readiness after a failure.
Check `state` for availability. `inFlight` includes leases held through dependents.
Errors use the codes `setup_failed` and `cleanup_failed`; callers or server logs
receive the underlying exceptions.

`provides` is the stable service name, or the plugin name when no service key is
declared. `requires` lists the actual provider plugins selected by the manager.
Typed service references use the same lease and drain behavior as named plugins.

## Failure and cancellation behavior

- Initial startup publishes the enabled group as active only after every setup
  succeeds. A failed setup triggers registered cleanup and disposal of services
  already started by that attempt.
- Configuration is validated before stopping services. Validation failure leaves
  the active group running with its existing configuration.
- A valid configuration replacement drains and disposes the affected active group,
  then starts it again. The group becomes available only after all setup succeeds.
- If setup fails during that replacement, partial resources are cleaned, the
  restarted group becomes unavailable, and the new configuration is retained.
  Correct the configuration, then retry the failed plugin. Blocked consumers resume. External
  effects such as writes to another system are not rolled back.
- If cleanup fails before the replacement configuration is stored, the old
  configuration remains. Cleanup errors are reported after all affected cleanup
  callbacks have been attempted, using exception groups where necessary.

`PluginManager(..., drain_timeout=30.0)` limits the wait for leases. If it expires,
`DrainTimeout` is raised, previous states are restored, and that drain operation
disposes no resources. This also applies to shutdown: finish outstanding work and
call `close()` again. A timeout does not forcibly close a resource still in use.

Owned tasks and child resource scopes close before plugin resources; registered
resource callbacks are attempted in reverse order even when another callback
fails. Task and cleanup failures are grouped. See the
[resource helpers](plugins.md#register-cleanup-as-resources-are-acquired).

The drain timeout does not bound setup or cleanup. Callbacks must cooperate with
asyncio; the runtime cannot forcibly terminate blocked Python code. When a caller
cancels a management operation, the serialized operation completes before
cancellation is propagated, so the manager is not abandoned midway through a
state change.

## Host responsibilities

Runtime changes are local to this manager and are not persisted. The application
owns durable configuration, process coordination, deployment, package installation,
and database migrations. Restart processes when deploying changed Python code or
dependencies. A plugin lifecycle does not provide a database migration transaction.
