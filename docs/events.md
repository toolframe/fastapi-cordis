# Typed events

An `EventKey[T]` gives an in-process event a name and a runtime payload type.
Use a dataclass, Pydantic model, ordinary class, or runtime-checkable Protocol.
The key checks `isinstance`; it does not coerce payloads or validate method
signatures. A name keeps the same payload contract for the manager's lifetime.

```python
from dataclasses import dataclass

from fastapi_cordis import EventKey, Plugin, PluginManager


@dataclass
class ReportCreated:
    report_id: str


REPORT_CREATED = EventKey("report.created", ReportCreated)


def audit(ctx):
    async def record(event: ReportCreated):
        print(event.report_id)

    ctx.on(REPORT_CREATED, record)


async def main():
    manager = PluginManager([Plugin("audit", audit)])
    async with manager.lifespan():
        await manager.emit(REPORT_CREATED, ReportCreated("report-1"))
```

An active plugin can also call `await ctx.emit(key, payload)`. Both methods wait
for all handlers. Handlers begin in registration order, run on the manager's loop,
and may await concurrently. Synchronous handlers must be brief and nonblocking.
There is no guarantee of completion order. Payloads are shared objects; handlers
must coordinate intentional mutation themselves.

`ctx.on()` returns an idempotent async disposer. Await it to unregister early.
Otherwise the registration is removed during the plugin's cleanup, including
failed setup. A dispatch snapshots registrations: additions/removals affect later
dispatches. A snapshotted handler is still skipped if its plugin has stopped or
started draining before that invocation begins.

Every handler invocation holds a lease on its owning plugin and dependencies.
Stopping a provider therefore waits for current handlers to finish. Handler
failures are aggregated only after peers finish, and leases are released on both
failure and cancellation. Cancelling emission cancels its handlers and waits for
their cancellation cleanup, including when the caller is cancelled repeatedly.

A handler cannot stop, remove, or reconfigure itself or its dependencies while
its invocation holds those leases; the manager raises `PluginError`. Make that
management decision after emission completes. Payload validation errors omit
payload values. Handler exceptions propagate to the emitter in an exception group.

This interface provides no persistence, cross-process delivery, retries, or
message-broker guarantees. Use it for local extension points and notifications.
